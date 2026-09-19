import asyncio
import logging
import os
import time
from typing import Optional

from fastapi import Depends, FastAPI, UploadFile, File, HTTPException, WebSocket, Request, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

from app import auth, config
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

# Interactive API docs would advertise every endpoint to anyone who finds the URL.
app = FastAPI(title="Headless Voicebot POC", version="0.1.0", docs_url=None, redoc_url=None, openapi_url=None)

# Same-origin needs no CORS; only origins listed in ALLOWED_ORIGINS may call cross-site.
app.add_middleware(
    CORSMiddleware,
    allow_origins=config.ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _no_indexing(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noimageindex, noarchive"
    response.headers["Cache-Control"] = "private, no-store"
    return response


STATIC_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
ADMIN = [Depends(auth.require_admin)]

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


class FeedbackRequest(BaseModel):
    session_id: str
    rating: int = Field(ge=1, le=5)
    resolved: bool
    comment: Optional[str] = Field(default=None, max_length=2000)


class OpenAIChatMessage(BaseModel):
    role: str
    content: str


class OpenAIChatCompletionsRequest(BaseModel):
    model: str | None = None
    messages: list[OpenAIChatMessage]


# ==========================================
# UI & Dashboard Endpoints
# ==========================================

_LOGIN_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow, noimageindex"><title>Sign in</title>
<style>
body{margin:0;min-height:100vh;display:grid;place-items:center;font-family:system-ui,sans-serif;background:#0f1115;color:#e8eaed}
form{width:min(320px,86vw);display:grid;gap:12px}
input,button{font:inherit;padding:12px 14px;border-radius:10px;border:1px solid #333944;background:#181b22;color:inherit}
button{background:#3b6cf6;border-color:#3b6cf6;cursor:pointer;font-weight:600}
p{margin:0;min-height:1.2em;color:#f28b82;font-size:14px}
</style></head><body>
<form id="f"><input id="c" type="password" placeholder="Access code" autocomplete="off" autofocus required>
<button>Continue</button><p id="e"></p></form>
<script>
document.getElementById("f").onsubmit=async(ev)=>{ev.preventDefault();
const r=await fetch("/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({code:document.getElementById("c").value})});
if(r.ok){const d=await r.json();location.href=d.next}else{document.getElementById("e").textContent="Invalid code"}};
</script></body></html>"""

_failed_logins: dict[str, list[float]] = {}


def _login_throttled(host: str) -> bool:
    now = time.monotonic()
    recent = [t for t in _failed_logins.get(host, []) if now - t < 300]
    _failed_logins[host] = recent
    return len(recent) >= 10


class LoginRequest(BaseModel):
    code: str = Field(max_length=200)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return HTMLResponse(_LOGIN_HTML)


@app.post("/login")
async def login(req: LoginRequest, request: Request):
    """Exchange an invite code (or the admin key) for a session cookie."""
    host = request.client.host if request.client else "unknown"
    if _login_throttled(host):
        raise HTTPException(status_code=429, detail="Too many attempts, try again later")
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"

    if auth.is_admin_key(req.code):
        kind, secret, nxt = "admin", req.code, "/dashboard"
    else:
        secret = auth.match_code(req.code)
        if secret is None:
            _failed_logins.setdefault(host, []).append(time.monotonic())
            raise HTTPException(status_code=401, detail="Invalid code")
        kind, nxt = "access", "/call"

    resp = JSONResponse({"status": "ok", "next": nxt})
    name, value = auth.cookie_for(kind, secret)
    resp.set_cookie(name, value, httponly=True, samesite="strict", secure=secure, max_age=60 * 60 * 24 * 30)
    return resp


@app.post("/logout")
async def logout():
    resp = JSONResponse({"status": "ok"})
    resp.delete_cookie(auth.ACCESS_COOKIE)
    resp.delete_cookie(auth.ADMIN_COOKIE)
    return resp


@app.get("/robots.txt", response_class=PlainTextResponse)
async def robots():
    return "User-agent: *\nDisallow: /\n"


@app.get("/static/{path:path}")
async def static_files(path: str, request: Request):
    """Front-end assets (incl. the logo/orb) are only served to signed-in visitors."""
    auth.require_caller(request)
    root = os.path.realpath(STATIC_DIR)
    full = os.path.realpath(os.path.join(root, path))
    if not full.startswith(root + os.sep) or not os.path.isfile(full):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(full)


@app.get("/", response_class=HTMLResponse)
@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard(request: Request):
    """Serves the Session Activity & Agent Monitoring Web UI."""
    if not auth.is_admin(request):
        return RedirectResponse("/login")
    index_html = os.path.join(STATIC_DIR, "index.html")
    if os.path.exists(index_html):
        return FileResponse(index_html)
    return HTMLResponse("<h2>Voicebot UI is loading...</h2>")


@app.get("/call", response_class=HTMLResponse)
async def call_ui(request: Request):
    """Serves the end-user voice call page."""
    if auth.caller_code(request) is None:
        return RedirectResponse("/login")
    call_html = os.path.join(STATIC_DIR, "call.html")
    if os.path.exists(call_html):
        return FileResponse(call_html)
    return HTMLResponse("<h2>Voice call UI is loading...</h2>")


# ==========================================
# Session Monitoring REST & WebSocket APIs
# ==========================================

@app.get("/api/sessions", dependencies=ADMIN)
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


@app.get("/api/sessions/{session_id}", dependencies=ADMIN, responses={404: {"description": "Session not found"}})
def get_session_detail(session_id: str):
    """Retrieve complete event timeline and details for a single session."""
    session = tracker.get_session(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session.to_dict(include_events=True)


@app.post("/api/sessions/clear", dependencies=ADMIN)
def clear_sessions():
    """Clear all session tracking history."""
    tracker.clear()
    return {"status": "ok", "message": "Session history cleared"}


@app.post("/api/sessions/{session_id}/end", dependencies=ADMIN, responses={404: {"description": "No active live session found"}})
async def end_session(session_id: str):
    """Force-close an active live voice WebSocket session from the dashboard."""
    closed = await tracker.close_session(session_id)
    if not closed:
        raise HTTPException(status_code=404, detail="No active live session found")
    return {"status": "ok", "message": f"Session {session_id} ended"}


@app.get("/api/stats", dependencies=ADMIN)
def get_stats():
    """Retrieve aggregated agent activity metrics & knowledge base status."""
    stats = tracker.get_stats()
    stats["rag_index_chunks"] = 0 if _index.is_empty else len(_index.texts)
    return stats


@app.get("/api/knowledge-base", dependencies=ADMIN)
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


# ==========================================
# Feedback
# ==========================================

@app.post("/feedback", dependencies=[Depends(auth.require_caller)], responses={404: {"description": "Session not found"}})
async def submit_feedback(req: FeedbackRequest):
    """Record end-of-call rating/feedback against the originating voice session."""
    session = tracker.get_session(req.session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    tracker.record_event(
        req.session_id,
        "feedback",
        "User feedback submitted",
        {"rating": req.rating, "resolved": req.resolved, "comment": req.comment},
    )
    return {"status": "ok"}


@app.websocket("/ws/monitor")
async def ws_monitor(websocket: WebSocket):
    """WebSocket endpoint for real-time live monitoring feed in the web UI."""
    if not (auth.is_admin(websocket) and auth.origin_ok(websocket)):
        await websocket.close(code=4401)
        return
    await tracker.register_monitor(websocket)


# ==========================================
# Core Agent Endpoints (Instrumented)
# ==========================================

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/ingest", dependencies=ADMIN)
def ingest():
    """Rebuild the RAG index from files in data/docs."""
    global _index
    stats = build_index()
    _index = load_index()
    return stats


@app.post("/chat", dependencies=ADMIN, responses={400: {"description": "question must not be empty"}})
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


@app.post("/chat/batch", dependencies=ADMIN, responses={400: {"description": "questions must not be empty"}})
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


@app.post("/v1/chat/completions", dependencies=ADMIN, responses={400: {"description": "no user message found in messages"}})
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


@app.post("/voice/query", dependencies=ADMIN, responses={400: {"description": "could not transcribe audio"}})
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
        {
            "filename": filename,
            "size_bytes": len(audio_bytes),
            "content_type": file.content_type,
            "audio_base64": audio_to_base64(audio_bytes),
        },
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
            {
                "answer": result["answer"],
                "audio_format": "mp3",
                "size_bytes": len(speech_bytes),
                "audio_base64": audio_to_base64(speech_bytes),
            },
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
    code = auth.caller_code(websocket)
    if code is None or not auth.origin_ok(websocket):
        await websocket.close(code=4401)
        return
    busy = auth.try_begin_call(code)
    if busy:
        await websocket.close(code=4429, reason=busy)
        return

    started = time.monotonic()

    async def _cut_off():
        # The bridge stops as soon as the client socket closes.
        await asyncio.sleep(auth.max_seconds_for(code))
        await websocket.close(code=4408, reason="Call time limit reached")

    limiter = asyncio.create_task(_cut_off())
    try:
        await run_voice_bridge(websocket, _index)
    finally:
        limiter.cancel()
        auth.end_call(code, started)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=True)
