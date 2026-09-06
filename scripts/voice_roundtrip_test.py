"""
End-to-end voice test: synthesizes a spoken question with OpenAI TTS, sends it to
the running bot's /voice/query endpoint, and saves the bot's spoken reply as mp3.

Usage:
    python scripts/voice_roundtrip_test.py "What are your support hours?"
"""
import sys
import base64
import requests

sys.path.insert(0, ".")
from app import config
from app.llm import synthesize_speech

BOT_URL = "http://localhost:8000/voice/query"
QUESTION_AUDIO = "question.mp3"
REPLY_AUDIO = "reply.mp3"


def main():
    question = sys.argv[1] if len(sys.argv) > 1 else "What are your support hours?"

    print(f"Synthesizing question audio: {question!r}")
    question_bytes = synthesize_speech(question)
    with open(QUESTION_AUDIO, "wb") as f:
        f.write(question_bytes)

    print(f"Sending {QUESTION_AUDIO} to {BOT_URL} ...")
    with open(QUESTION_AUDIO, "rb") as f:
        resp = requests.post(
            BOT_URL,
            files={"file": (QUESTION_AUDIO, f, "audio/mpeg")},
        )
    resp.raise_for_status()
    data = resp.json()

    print("Transcript:", data["transcript"])
    print("Answer:", data["answer"])

    with open(REPLY_AUDIO, "wb") as f:
        f.write(base64.b64decode(data["audio_base64"]))
    print(f"Saved bot's spoken reply to {REPLY_AUDIO}")


if __name__ == "__main__":
    main()
