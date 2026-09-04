import os
import glob
import numpy as np
from openai import OpenAI

from app import config

_client: OpenAI | None = None


def get_client() -> OpenAI:
    global _client
    if _client is None:
        _client = OpenAI(api_key=config.OPENAI_API_KEY)
    return _client


def _read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        return f.read()


def _read_pdf_file(path: str) -> str:
    from pypdf import PdfReader

    reader = PdfReader(path)
    return "\n".join(page.extract_text() or "" for page in reader.pages)


def load_documents(docs_dir: str) -> list[tuple[str, str]]:
    """Returns list of (source_name, raw_text)."""
    docs: list[tuple[str, str]] = []
    for path in glob.glob(os.path.join(docs_dir, "**", "*"), recursive=True):
        if not os.path.isfile(path):
            continue
        ext = os.path.splitext(path)[1].lower()
        name = os.path.relpath(path, docs_dir)
        try:
            if ext in (".txt", ".md"):
                docs.append((name, _read_text_file(path)))
            elif ext == ".pdf":
                docs.append((name, _read_pdf_file(path)))
        except Exception as e:
            print(f"Skipping {path}: {e}")
    return docs


def chunk_text(text: str, chunk_size: int, overlap: int) -> list[str]:
    text = " ".join(text.split())
    if not text:
        return []
    chunks = []
    start = 0
    while start < len(text):
        end = min(start + chunk_size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks


def embed_texts(texts: list[str]) -> np.ndarray:
    client = get_client()
    vectors: list[list[float]] = []
    batch_size = 100
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        resp = client.embeddings.create(model=config.EMBED_MODEL, input=batch)
        vectors.extend(item.embedding for item in resp.data)
    return np.array(vectors, dtype=np.float32)


def build_index(docs_dir: str = config.DOCS_DIR, index_path: str = config.INDEX_PATH) -> dict:
    docs = load_documents(docs_dir)
    all_chunks: list[str] = []
    all_sources: list[str] = []
    for source, raw_text in docs:
        for chunk in chunk_text(raw_text, config.CHUNK_SIZE, config.CHUNK_OVERLAP):
            all_chunks.append(chunk)
            all_sources.append(source)

    if not all_chunks:
        os.makedirs(os.path.dirname(index_path), exist_ok=True)
        np.savez(
            index_path,
            vectors=np.zeros((0, 1), dtype=np.float32),
            texts=np.array([], dtype=object),
            sources=np.array([], dtype=object),
        )
        return {"documents": len(docs), "chunks": 0}

    vectors = embed_texts(all_chunks)
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    np.savez(
        index_path,
        vectors=vectors,
        texts=np.array(all_chunks, dtype=object),
        sources=np.array(all_sources, dtype=object),
    )
    return {"documents": len(docs), "chunks": len(all_chunks)}


class RagIndex:
    def __init__(self, vectors: np.ndarray, texts: list[str], sources: list[str]):
        self.vectors = vectors
        self.texts = texts
        self.sources = sources

    @property
    def is_empty(self) -> bool:
        return self.vectors.shape[0] == 0

    def search(self, query: str, top_k: int = config.TOP_K) -> list[dict]:
        if self.is_empty:
            return []
        query_vec = embed_texts([query])[0]
        norms = np.linalg.norm(self.vectors, axis=1) * np.linalg.norm(query_vec)
        norms[norms == 0] = 1e-10
        scores = (self.vectors @ query_vec) / norms
        top_idx = np.argsort(-scores)[:top_k]
        return [
            {
                "text": self.texts[i],
                "source": self.sources[i],
                "score": float(scores[i]),
            }
            for i in top_idx
        ]


def load_index(index_path: str = config.INDEX_PATH) -> RagIndex:
    if not os.path.exists(index_path):
        return RagIndex(np.zeros((0, 1), dtype=np.float32), [], [])
    data = np.load(index_path, allow_pickle=True)
    return RagIndex(data["vectors"], list(data["texts"]), list(data["sources"]))
