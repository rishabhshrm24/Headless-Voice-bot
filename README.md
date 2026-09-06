# Voicebot Agent (OpenAI Realtime + RAG + Live Activity Monitor)

A hostable voicebot with OpenAI Realtime & RAG integration and a **built-in web UI dashboard** for live session activity monitoring.

## What's included
- **Session Activity Web Dashboard** (`/` or `/dashboard`): Real-time visual monitoring of all agent sessions (WebSocket Voice, HTTP Voice, Text Chat, Batch), event timelines, transcripts, RAG context retrievals, and KPI metrics.
- **RAG**: your domain docs (`data/docs/*.txt|.md|.pdf`) are chunked, embedded with
  OpenAI embeddings, and stored in a local vector index (numpy, no external DB needed).
- **Text/RAG API** (`/chat`, `/chat/batch`): fastest way to batch-test Q&A accuracy.
- **Voice-in/voice-out over HTTP** (`/voice/query`): upload an audio file, get back
  transcript + RAG answer + spoken (mp3) reply. Easy to script for audio batch tests.
- **True live voice** (`/ws/voice`): WebSocket bridge to OpenAI's **Realtime API**
  (the "live" model) with RAG exposed as a tool the model calls mid-conversation.

## 1. Setup

```powershell
cd "Headless voicebot"
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
# edit .env and set OPENAI_API_KEY
```

## 2. Add your domain knowledge
Drop `.txt`, `.md`, or `.pdf` files into `data/docs/` (a sample file is already there).
Then build the index:

```powershell
uvicorn app.main:app --reload
# in another terminal:
curl -X POST http://localhost:8000/ingest
```

Re-run `/ingest` any time you change the docs (or click "Re-index Knowledge" directly in the web UI).

## 3. Run the server & Open Web UI

```powershell
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Open your browser at:
👉 **`http://localhost:8000/`** (or `http://localhost:8000/dashboard`)

### Web UI Features:
- **Live Metrics**: Active WebSocket sessions, total interactions, RAG searches, average latency, and indexed knowledge chunks.
- **Session Feed**: Real-time stream of all incoming sessions with instant filtering (by channel type: Live Voice, Voice Query, Text Chat, Batch; and status: Active, Completed, Error).
- **Detailed Inspector**:
  - **Timeline View**: Chronological micro-events (Speech transcribed, RAG searches, Function calls, Assistant replies).
  - **Conversation View**: Clean user & assistant transcript dialogues with expandable knowledge citation tags.
  - **RAG Context View**: Inspect exact text chunks and similarity scores retrieved from the vector index.
  - **Raw JSON View**: Full telemetry payload with one-click export.
- **Interactive Agent Test Runner**: Built-in tester to run live text chats, upload audio queries, execute batch runs, or trigger knowledge base re-indexing directly from the browser.

## 4. API reference

### UI & Monitoring Endpoints
- `GET /` or `GET /dashboard`: Web UI Session Activity Dashboard.
- `GET /api/sessions`: List sessions with optional filters (`limit`, `type`, `status`, `search`).
- `GET /api/sessions/{session_id}`: Get full event timeline and details for a session.
- `POST /api/sessions/clear`: Clear session history.
- `GET /api/stats`: Real-time session and RAG metrics.
- `WS /ws/monitor`: Real-time live event stream WebSocket for monitoring UIs.

### `GET /health`
Quick check + how many chunks are indexed.

### `POST /chat`
```json
{ "question": "What is your refund policy?" }
```
Returns `{ question, answer, sources }`.

### `POST /chat/batch`  ← use this for automated batch testing
```json
{ "questions": ["What is your refund policy?", "What is your refund policy?", "..."] }
```
Returns `{ "results": [ { question, answer, sources }, ... ] }`.
Runs questions concurrently (default concurrency 5).

### `POST /voice/query` (multipart form, field `file`)
Upload a `.wav`/`.mp3`/`.m4a` file. Returns:
```json
{ "transcript": "...", "answer": "...", "sources": [...], "audio_base64": "...", "audio_format": "mp3" }
```

### `WS /ws/voice`
Raw bridge to OpenAI's Realtime API. Send/receive Realtime API events directly
(e.g. `input_audio_buffer.append`, `response.audio.delta`) per
[OpenAI's Realtime docs](https://platform.openai.com/docs/guides/realtime). RAG lookups
happen automatically via the `search_knowledge_base` tool the model can call.

## 5. Batch-testing from another tool

Any tool that can send HTTP POST + JSON can drive this. A helper script is included:

```powershell
# from a questions.json file: {"questions": [...]}  or a plain JSON array
python scripts/batch_test.py scripts/sample_questions.json results.csv

# repeat one question N times (useful for consistency testing)
python scripts/batch_test.py --repeat "What is your refund policy?" 10 results.csv
```

This writes a CSV with `question,answer,sources` — easy to diff/inspect across runs.

## Notes / next steps for production
- Swap the numpy index for a real vector DB (pgvector/Chroma/Pinecone) if the KB grows large.
- Add auth (API key header) before exposing publicly.
- Add rate limiting and request logging for the realtime WebSocket.
- Consider streaming `/chat` responses if UIs need low first-token latency.
