import logging
import os
from typing import Optional

from fastapi import FastAPI, UploadFile, File, HTTPException, WebSocket, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app import config
from app.rag_store import build_index, load_index, RagIndex
from app.llm import (
    answer_query_async,
    answer_batch,
    transcribe_audio,
    synthesize_speech,
    audio_to_base64,
)
from app.realtime_bridge import run_voice_bridge
from app.session_tracker import tracker

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Headless Voicebot POC", version="0.1.0")

# Enable CORS for remote web monitoring dashboards
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files directory
STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
if os.path.exists(STATIC_DIR):
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

_index: RagIndex = load_index()


@app.on_event("startup")
def _auto_ingest():
    # Free-tier disks are ephemeral, so rebuild the index from data/docs on every boot if key is set.
    global _index
    if _index.is_empty:
        if config.OPENAI_API_KEY:
            try:
                build_index()
                _index = load_index()
            except Exception as e:
                logging.warning("Auto ingest failed: %s", e)
        else:
            logging.info("Auto ingest skipped (OPENAI_API_KEY not configured).")


class ChatRequest(BaseModel):
    question: str


class BatchChatRequest(BaseModel):
    questions: list[str]


class OpenAIChatMessage(BaseModel):
    role: str
    content: str


class OpenAIChatCompletionsRequest(BaseModel):
    model: str | None = None
    messages: list[OpenAIChatMessage]


# ==========================================
# UI & Dashboard Endpoints
# ==========================================

@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Serves the Session Activity & Agent Monitoring Web UI."""
    index_html = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_html):
        return FileResponse(index_html)
    return HTMLResponse("<h2>Voicebot UI is loading...</h2>")


# ==========================================
# Session Monitoring REST & WebSocket APIs
# ==========================================

@app.get("/api/sessions")
def list_sessions(
    limit: int = Query(50, ge=1, le=200),
    type: Optional[str] = Query(None),
    status: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
):
    """Retrieve filtered session history for UI monitoring."""
    return tracker.list_sessions(
        limit=limit,
        session_type=type,
        status=status,
        search=search,
    )


@app.get("/api/sessions/{session_id}", responses={404: {"description": "Session not found"}})
def get_session_detail(session_id: str):
    """Retrieve complete event timeline and details for a single session."""
    session = tracker.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_dict(include_events=True)


@app.post("/api/sessions/clear")
def clear_sessions():
    """Clear all session tracking history."""
    tracker.clear()
    return {"status": "ok", "message": "Session history cleared"}


@app.get("/api/stats")
def get_stats():
    """Retrieve aggregated agent activity metrics & knowledge base status."""
    stats = tracker.get_stats()
    stats["rag_index_chunks"] = 0 if _index.is_empty else len(_index.texts)
    return stats


@app.get("/api/knowledge-base")
def get_knowledge_base_info():
    """Get information about knowledge base files and indexed chunks."""
    docs = []
    if os.path.exists(config.DOCS_DIR):
        for fname in os.listdir(config.DOCS_DIR):
            fpath = os.path.join(config.DOCS_DIR, fname)
            if os.path.isfile(fpath) and not fname.startswith("."):
                size_kb = round(os.path.getsize(fpath) / 1024, 1)
                docs.append({
                    "filename": fname,
                    "size_kb": size_kb,
                })
    return {
        "docs": docs,
        "total_chunks": 0 if _index.is_empty else len(_index.texts),
    }


@app.websocket("/ws/monitor")
async def ws_monitor(websocket: WebSocket):
    """WebSocket endpoint for real-time live monitoring feed in the web UI."""
    await tracker.register_monitor(websocket)


# ==========================================
# Core Agent Endpoints (Instrumented)
# ==========================================

@app.get("/health")
def health():
    return {"status": "ok", "index_chunks": 0 if _index.is_empty else len(_index.texts)}


@app.post("/ingest")
def ingest():
    """Rebuild the RAG index from files in data/docs."""
    global _index
    stats = build_index()
    _index = load_index()
    return stats


@app.post("/chat", responses={400: {"description": "question must not be empty"}})
async def chat(req: ChatRequest, request: Request):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")

    client_host = request.client.host if request.client else "127.0.0.1"
    session = tracker.create_session(
        session_type="text_chat",
        client_host=client_host,
        summary=req.question,
    )

    tracker.record_event(
        session.id,
        "user_message",
        f"User asked: {req.question[:60]}...",
        {"question": req.question},
    )

    try:
        tracker.record_event(
            session.id,
            "rag_query",
            f"Searching KB: {req.question[:50]}...",
            {"query": req.question},
        )
        result = await answer_query_async(_index, req.question)

        if result.get("sources"):
            tracker.record_event(
                session.id,
                "rag_result",
                f"Retrieved {len(result['sources'])} knowledge chunks",
                {"sources": result["sources"]},
            )

        tracker.record_event(
            session.id,
            "assistant_reply",
            f"Reply: {result['answer'][:60]}...",
            {"answer": result["answer"], "sources": result.get("sources", [])},
        )
        tracker.end_session(session.id, status="completed")
        return result
    except Exception as e:
        logging.exception("Error during chat processing: %s", e)
        tracker.end_session(session.id, status="error", error=str(e))
        raise


@app.post("/chat/batch", responses={400: {"description": "questions must not be empty"}})
async def chat_batch(req: BatchChatRequest, request: Request):
    """Run the same/varied questions in one call. Use this from external test tools."""
    if not req.questions:
        raise HTTPException(status_code=400, detail="questions must not be empty")

    client_host = request.client.host if request.client else "127.0.0.1"
    session = tracker.create_session(
        session_type="batch_chat",
        client_host=client_host,
        summary=f"Batch test ({len(req.questions)} questions)",
    )

    tracker.record_event(
        session.id,
        "user_message",
        f"Batch started with {len(req.questions)} queries",
        {"questions": req.questions},
    )

    try:
        results = await answer_batch(_index, req.questions)
        for i, r in enumerate(results):
            tracker.record_event(
                session.id,
                "assistant_reply",
                f"Q{i+1}: {r['question'][:40]}...",
                {
                    "question": r["question"],
                    "answer": r["answer"],
                    "sources": r.get("sources", []),
                },
            )
        tracker.end_session(session.id, status="completed")
        return {"results": results}
    except Exception as e:
        logging.exception("Error during batch chat processing: %s", e)
        tracker.end_session(session.id, status="error", error=str(e))
        raise


@app.post("/v1/chat/completions", responses={400: {"description": "no user message found in messages"}})
async def openai_chat_completions(req: OpenAIChatCompletionsRequest, request: Request):
    """OpenAI-compatible endpoint for tools that speak the standard chat-completions format."""
    user_messages = [m.content for m in req.messages if m.role == "user"]
    if not user_messages:
        raise HTTPException(status_code=400, detail="no user message found in messages")

    question = user_messages[-1]
    client_host = request.client.host if request.client else "127.0.0.1"
    session = tracker.create_session(
        session_type="openai_compat",
        client_host=client_host,
        summary=question,
    )

    tracker.record_event(
        session.id,
        "user_message",
        f"OpenAI API message: {question[:50]}...",
        {"question": question},
    )

    try:
        result = await answer_query_async(_index, question)
        if result.get("sources"):
            tracker.record_event(
                session.id,
                "rag_result",
                f"Retrieved {len(result['sources'])} knowledge chunks",
                {"sources": result["sources"]},
            )

        tracker.record_event(
            session.id,
            "assistant_reply",
            f"OpenAI API response: {result['answer'][:50]}...",
            {"answer": result["answer"]},
        )
        tracker.end_session(session.id, status="completed")

        return {
            "id": f"chatcmpl-{session.id}",
            "object": "chat.completion",
            "model": config.CHAT_MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": result["answer"]},
                    "finish_reason": "stop",
                }
            ],
        }
    except Exception as e:
        logging.exception("Error in openai_chat_completions: %s", e)
        tracker.end_session(session.id, status="error", error=str(e))
        raise


@app.post("/voice/query", responses={400: {"description": "could not transcribe audio"}})
async def voice_query(file: UploadFile = File(...), request: Request = None):
    """Upload an audio file (wav/mp3/m4a). Returns transcript, RAG answer, and spoken reply (base64 mp3)."""
    client_host = request.client.host if request and request.client else "127.0.0.1"
    filename = file.filename or "audio.wav"
    session = tracker.create_session(
        session_type="http_voice",
        client_host=client_host,
        summary=f"Voice Query ({filename})",
    )

    audio_bytes = await file.read()
    tracker.record_event(
        session.id,
        "audio_received",
        f"Audio file received: {filename}",
        {"filename": filename, "size_bytes": len(audio_bytes), "content_type": file.content_type},
    )

    try:
        transcript = transcribe_audio(audio_bytes, filename=filename)
        if not transcript.strip():
            raise HTTPException(status_code=400, detail="could not transcribe audio")

        tracker.record_event(
            session.id,
            "speech_transcribed",
            f"Speech transcribed: {transcript[:50]}...",
            {"transcript": transcript},
        )

        result = await answer_query_async(_index, transcript)
        if result.get("sources"):
            tracker.record_event(
                session.id,
                "rag_result",
                f"Retrieved {len(result['sources'])} knowledge chunks",
                {"sources": result["sources"]},
            )

        speech_bytes = synthesize_speech(result["answer"])
        tracker.record_event(
            session.id,
            "speech_synthesized",
            f"Synthesized voice reply ({len(speech_bytes)} bytes)",
            {"answer": result["answer"], "audio_format": "mp3", "size_bytes": len(speech_bytes)},
        )

        tracker.end_session(session.id, status="completed")

        return {
            "transcript": transcript,
            "answer": result["answer"],
            "sources": result["sources"],
            "audio_base64": audio_to_base64(speech_bytes),
            "audio_format": "mp3",
        }
    except Exception as e:
        logging.exception("Error in voice_query: %s", e)
        tracker.end_session(session.id, status="error", error=str(e))
        raise


@app.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket):
    """Realtime (live) voice bridge: client streams PCM16 audio events, model replies with
    streamed audio + can call `search_knowledge_base` for RAG."""
    await run_voice_bridge(websocket, _index)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=True)
