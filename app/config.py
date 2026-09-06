import os
import logging
from dotenv import load_dotenv

load_dotenv()

logger = logging.getLogger("config")


def _get(name: str, default: str = "") -> str:
    return os.getenv(name, default)


OPENAI_API_KEY = _get("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    logger.warning(
        "OPENAI_API_KEY is not set. Monitoring UI will still work, but agent LLM/voice generation calls will require a key."
    )

CHAT_MODEL = _get("CHAT_MODEL", "gpt-4o-mini")
EMBED_MODEL = _get("EMBED_MODEL", "text-embedding-3-small")
REALTIME_MODEL = _get("REALTIME_MODEL", "gpt-4o-realtime-preview-2024-12-17")
TTS_MODEL = _get("TTS_MODEL", "tts-1")
TTS_VOICE = _get("TTS_VOICE", "alloy")
STT_MODEL = _get("STT_MODEL", "whisper-1")

TOP_K = int(_get("TOP_K", "4"))
CHUNK_SIZE = int(_get("CHUNK_SIZE", "800"))
CHUNK_OVERLAP = int(_get("CHUNK_OVERLAP", "100"))

HOST = _get("HOST", "0.0.0.0")
PORT = int(_get("PORT", "8000"))

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS_DIR = os.path.join(BASE_DIR, "data", "docs")
INDEX_PATH = os.path.join(BASE_DIR, "storage", "index.npz")
