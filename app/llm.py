import asyncio
import base64
import io

from app import config
from app.prompts import SYSTEM_PROMPT
from app.rag_store import get_client, RagIndex


def build_context_block(chunks: list[dict]) -> str:
    if not chunks:
        return "(no relevant knowledge found)"
    parts = []
    for c in chunks:
        parts.append(f"[source: {c['source']}]\n{c['text']}")
    return "\n\n---\n\n".join(parts)


def answer_query(index: RagIndex, question: str, top_k: int = config.TOP_K) -> dict:
    chunks = index.search(question, top_k=top_k)
    context = build_context_block(chunks)

    client = get_client()
    completion = client.chat.completions.create(
        model=config.CHAT_MODEL,
        messages=[
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "system",
                "content": f"KNOWLEDGE CONTEXT:\n{context}",
            },
            {"role": "user", "content": question},
        ],
        temperature=0.3,
    )
    answer = completion.choices[0].message.content or ""
    return {
        "question": question,
        "answer": answer,
        "sources": [{"source": c["source"], "score": c["score"]} for c in chunks],
    }


async def answer_query_async(index: RagIndex, question: str, top_k: int = config.TOP_K) -> dict:
    return await asyncio.to_thread(answer_query, index, question, top_k)


async def answer_batch(index: RagIndex, questions: list[str], concurrency: int = 5) -> list[dict]:
    semaphore = asyncio.Semaphore(concurrency)

    async def run_one(q: str) -> dict:
        async with semaphore:
            return await answer_query_async(index, q)

    return await asyncio.gather(*(run_one(q) for q in questions))


def transcribe_audio(file_bytes: bytes, filename: str = "audio.wav") -> str:
    client = get_client()
    buffer = io.BytesIO(file_bytes)
    buffer.name = filename
    result = client.audio.transcriptions.create(model=config.STT_MODEL, file=buffer)
    return result.text


def synthesize_speech(text: str) -> bytes:
    client = get_client()
    response = client.audio.speech.create(
        model=config.TTS_MODEL,
        voice=config.TTS_VOICE,
        input=text,
        response_format="mp3",
    )
    return response.read()


def audio_to_base64(audio_bytes: bytes) -> str:
    return base64.b64encode(audio_bytes).decode("utf-8")
