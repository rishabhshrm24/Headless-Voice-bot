import logging

from fastapi import FastAPI, UploadFile, File, HTTPException, WebSocket
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

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Headless Voicebot POC", version="0.1.0")

_index: RagIndex = load_index()


@app.on_event("startup")
def _auto_ingest():
    # Free-tier disks are ephemeral, so rebuild the index from data/docs on every boot.
    global _index
    if _index.is_empty:
        build_index()
        _index = load_index()


class ChatRequest(BaseModel):
    question: str


class BatchChatRequest(BaseModel):
    questions: list[str]


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


@app.post("/chat")
async def chat(req: ChatRequest):
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="question must not be empty")
    return await answer_query_async(_index, req.question)


@app.post("/chat/batch")
async def chat_batch(req: BatchChatRequest):
    """Run the same/varied questions in one call. Use this from external test tools."""
    if not req.questions:
        raise HTTPException(status_code=400, detail="questions must not be empty")
    results = await answer_batch(_index, req.questions)
    return {"results": results}


@app.post("/voice/query")
async def voice_query(file: UploadFile = File(...)):
    """Upload an audio file (wav/mp3/m4a). Returns transcript, RAG answer, and spoken reply (base64 mp3)."""
    audio_bytes = await file.read()
    transcript = transcribe_audio(audio_bytes, filename=file.filename or "audio.wav")
    if not transcript.strip():
        raise HTTPException(status_code=400, detail="could not transcribe audio")

    result = await answer_query_async(_index, transcript)
    speech_bytes = synthesize_speech(result["answer"])

    return {
        "transcript": transcript,
        "answer": result["answer"],
        "sources": result["sources"],
        "audio_base64": audio_to_base64(speech_bytes),
        "audio_format": "mp3",
    }


@app.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket):
    """Realtime (live) voice bridge: client streams PCM16 audio events, model replies with
    streamed audio + can call `search_knowledge_base` for RAG."""
    await run_voice_bridge(websocket, _index)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host=config.HOST, port=config.PORT, reload=True)
