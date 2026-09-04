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
            "modalities": ["audio", "text"],
            "instructions": SYSTEM_PROMPT,
            "voice": config.TTS_VOICE,
            "input_audio_format": "pcm16",
            "output_audio_format": "pcm16",
            "input_audio_transcription": {"model": "whisper-1"},
            "turn_detection": {"type": "server_vad"},
            "tools": [SEARCH_TOOL],
            "tool_choice": "auto",
        },
    }


async def run_voice_bridge(client_ws: WebSocket, index: RagIndex) -> None:
    await client_ws.accept()

    headers = [
        ("Authorization", f"Bearer {config.OPENAI_API_KEY}"),
        ("OpenAI-Beta", "realtime=v1"),
    ]

    async with websockets.connect(
        OPENAI_REALTIME_URL, additional_headers=headers, max_size=None
    ) as openai_ws:
        await openai_ws.send(json.dumps(session_update_payload()))

        async def client_to_openai():
            try:
                while True:
                    data = await client_ws.receive_text()
                    # Client forwards raw Realtime API events (e.g. input_audio_buffer.append)
                    await openai_ws.send(data)
            except WebSocketDisconnect:
                pass
            except Exception as e:
                logger.warning("client_to_openai stopped: %s", e)

        async def openai_to_client():
            try:
                async for message in openai_ws:
                    event = json.loads(message)
                    event_type = event.get("type")

                    if event_type == "response.function_call_arguments.done":
                        await handle_function_call(openai_ws, event, index)
                        continue

                    await client_ws.send_text(message)
            except Exception as e:
                logger.warning("openai_to_client stopped: %s", e)

        await asyncio.gather(client_to_openai(), openai_to_client())


async def handle_function_call(openai_ws, event: dict, index: RagIndex) -> None:
    call_id = event.get("call_id")
    name = event.get("name")
    raw_args = event.get("arguments", "{}")

    output = ""
    if name == "search_knowledge_base":
        try:
            args = json.loads(raw_args)
        except json.JSONDecodeError:
            args = {}
        query = args.get("query", "")
        results = index.search(query) if query else []
        output = json.dumps(
            [{"source": r["source"], "text": r["text"]} for r in results]
        )
    else:
        output = json.dumps({"error": f"unknown tool {name}"})

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
