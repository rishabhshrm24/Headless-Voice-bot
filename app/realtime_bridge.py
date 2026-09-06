"""
Bridges a client WebSocket (sending/receiving PCM16 audio) to OpenAI's Realtime
("live") API. RAG is exposed to the model as a tool (`search_knowledge_base`) so it
can pull domain knowledge mid-conversation instead of relying on a static prompt.
"""
import json
import asyncio
import logging

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from app import config
from app.prompts import SYSTEM_PROMPT
from app.rag_store import RagIndex
from app.session_tracker import tracker

logger = logging.getLogger("realtime_bridge")

OPENAI_REALTIME_URL = f"wss://api.openai.com/v1/realtime?model={config.REALTIME_MODEL}"

SEARCH_TOOL = {
    "type": "function",
    "name": "search_knowledge_base",
    "description": "Search the domain knowledge base for information relevant to the user's question.",
    "parameters": {
        "type": "object",
        "properties": {
            "query": {"type": "string", "description": "The search query"},
        },
        "required": ["query"],
    },
}


def session_update_payload() -> dict:
    return {
        "type": "session.update",
        "session": {
            "type": "realtime",
            "instructions": SYSTEM_PROMPT,
            "audio": {
                "input": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "transcription": {"model": "whisper-1"},
                    "turn_detection": {"type": "server_vad"},
                },
                "output": {
                    "format": {"type": "audio/pcm", "rate": 24000},
                    "voice": config.TTS_VOICE,
                },
            },
            "tools": [SEARCH_TOOL],
            "tool_choice": "auto",
        },
    }


def _handle_client_text_event(data: str, session_id: str) -> None:
    try:
        msg = json.loads(data)
        if msg.get("type") == "conversation.item.create":
            item = msg.get("item", {})
            content = item.get("content", [])
            text_parts = [c.get("text", "") for c in content if c.get("text")]
            if text_parts:
                tracker.record_event(
                    session_id,
                    "user_message",
                    "Client sent text item",
                    {"text": " ".join(text_parts)},
                )
    except Exception:
        pass


async def _handle_openai_event(
    event: dict, message: str, openai_ws, client_ws: WebSocket, index: RagIndex, session_id: str
) -> None:
    event_type = event.get("type")

    if event_type == "conversation.item.input_audio_transcription.completed":
        transcript = event.get("transcript", "").strip()
        if transcript:
            tracker.record_event(
                session_id,
                "speech_transcribed",
                f"User spoke: {transcript[:50]}...",
                {"transcript": transcript},
            )
    elif event_type == "response.output_audio_transcript.done":
        transcript = event.get("transcript", "").strip()
        if transcript:
            tracker.record_event(
                session_id,
                "assistant_reply",
                f"Voice reply: {transcript[:50]}...",
                {"transcript": transcript},
            )
    elif event_type == "response.output_text.done":
        text = event.get("text", "").strip()
        if text:
            tracker.record_event(
                session_id,
                "assistant_reply",
                f"Text reply: {text[:50]}...",
                {"text": text},
            )
    elif event_type == "response.function_call_arguments.done":
        await handle_function_call(openai_ws, event, index, session_id)
        return
    elif event_type == "error":
        err = event.get("error", {})
        tracker.record_event(
            session_id,
            "error",
            f"Realtime API error: {err.get('message', 'Unknown error')}",
            {"error": err},
        )

    await client_ws.send_text(message)


async def _client_loop(client_ws: WebSocket, openai_ws, session_id: str) -> None:
    try:
        while True:
            data = await client_ws.receive_text()
            _handle_client_text_event(data, session_id)
            await openai_ws.send(data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        logger.warning("client_loop stopped: %s", e)


async def _openai_loop(
    openai_ws, client_ws: WebSocket, index: RagIndex, session_id: str
) -> None:
    try:
        async for message in openai_ws:
            event = json.loads(message)
            await _handle_openai_event(event, message, openai_ws, client_ws, index, session_id)
    except Exception as e:
        logger.warning("openai_loop stopped: %s", e)


async def run_voice_bridge(client_ws: WebSocket, index: RagIndex) -> None:
    await client_ws.accept()

    client_host = client_ws.client.host if client_ws.client else "unknown"
    session = tracker.create_session(
        session_type="websocket_voice",
        client_host=client_host,
        summary="Live Voice Session (WebSocket)",
    )
    session_id = session.id

    headers = [
        ("Authorization", f"Bearer {config.OPENAI_API_KEY}"),
    ]

    try:
        async with websockets.connect(
            OPENAI_REALTIME_URL, extra_headers=headers, max_size=None
        ) as openai_ws:
            await openai_ws.send(json.dumps(session_update_payload()))
            tracker.record_event(
                session_id,
                "realtime_connected",
                "Connected to OpenAI Realtime API",
                {"model": config.REALTIME_MODEL, "voice": config.TTS_VOICE},
            )
            await asyncio.gather(
                _client_loop(client_ws, openai_ws, session_id),
                _openai_loop(openai_ws, client_ws, index, session_id),
            )
            tracker.end_session(session_id, status="completed")

    except Exception as e:
        logger.exception("Voice bridge error: %s", e)
        tracker.end_session(session_id, status="error", error=str(e))


async def handle_function_call(
    openai_ws, event: dict, index: RagIndex, session_id: str
) -> None:
    call_id = event.get("call_id")
    name = event.get("name")
    raw_args = event.get("arguments", "{}")

    tracker.record_event(
        session_id,
        "tool_call",
        f"Model called tool: {name}",
        {"call_id": call_id, "name": name, "arguments": raw_args},
    )

    output = ""
    if name == "search_knowledge_base":
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {}
        query = args.get("query", "")
        tracker.record_event(
            session_id,
            "rag_query",
            f"Searching Knowledge Base: {query}",
            {"query": query},
        )
        results = index.search(query) if query else []
        output = json.dumps(
            [{"source": r["source"], "text": r["text"]} for r in results]
        )
        tracker.record_event(
            session_id,
            "rag_result",
            f"Found {len(results)} relevant chunks",
            {"query": query, "results_count": len(results), "sources": results},
        )
    else:
        output = json.dumps({"error": f"unknown tool {name}"})
        tracker.record_event(
            session_id,
            "error",
            f"Unknown tool requested: {name}",
            {"name": name},
        )

    await openai_ws.send(
        json.dumps(
            {
                "type": "conversation.item.create",
                "item": {
                    "type": "function_call_output",
                    "call_id": call_id,
                    "output": output,
                },
            }
        )
    )
    await openai_ws.send(json.dumps({"type": "response.create"}))
