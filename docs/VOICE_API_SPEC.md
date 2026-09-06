# Voicebot – Voice Interaction API Spec

Base URL (deployed): `https://headless-voicebot-poc.onrender.com`
Base URL (local dev): `http://localhost:8000`
Auth: none at the API layer (server holds its own `OPENAI_API_KEY`; add your own auth/gateway before exposing publicly)

> **Cold start warning:** the deployed service runs on Render's free tier, which spins down after inactivity. The first request after idle time can take ~30-60s to respond while the instance wakes up. Client code should use a generous timeout (60s+) and/or retry-on-timeout for the first call in a session.

There are two ways to interact with voice: a **one-shot HTTP upload** (simple, stateless) and a **realtime WebSocket bridge** (streaming, low-latency, conversational).

---

## 1. One-shot voice query — `POST /voice/query`

Use this for: sending a single pre-recorded/synthesized audio clip and getting one spoken+text reply back. Good for batch testing, IVR-style integrations, or any client that doesn't need live streaming.

### Request

- Method: `POST`
- Content-Type: `multipart/form-data`
- Field: `file` — audio file, field name must be exactly `file`

| Property | Value |
|---|---|
| Accepted formats | wav, mp3, m4a (anything OpenAI Whisper transcription accepts) |
| Max size | not enforced by app (bound by your reverse proxy / server config) |
| Sample rate | any (server does not resample before transcription) |

**curl**
```bash
curl -X POST https://headless-voicebot-poc.onrender.com/voice/query \
  -F "file=@question.wav;type=audio/wav"
```

### Response — `200 OK`

```json
{
  "transcript": "What are your support hours?",
  "answer": "We are available Monday through Friday, 9am to 5pm.",
  "sources": [
    {
      "source": "sample_kb.md",
      "score": 0.83,
      "text": "We are available Monday through Friday, 9am to 5pm."
    }
  ],
  "audio_base64": "SUQzBAAAAAAAI1RTU0UAAAAPAAADTGF2ZjU4LjI5...",
  "audio_format": "mp3"
}
```

| Field | Type | Description |
|---|---|---|
| `transcript` | string | Speech-to-text of the uploaded audio |
| `answer` | string | RAG-generated text answer |
| `sources` | array | KB chunks used; for DeepEval RAG metrics, each item should include `text`, `source`, and `score` |
| `audio_base64` | string | Base64-encoded **mp3** bytes of the spoken answer — **must be decoded client-side** to play/save |
| `audio_format` | string | Always `"mp3"` currently |

> DeepEval retrieval metrics require the full chunk text, not just a filename or a score. Each `sources` item should therefore include `text` so the tester can populate `retrieval_context` for faithfulness, contextual recall, and contextual precision checks.

Response is **always JSON** — audio is never returned as a raw binary stream from this endpoint. To get playable audio, base64-decode `audio_base64` and write it to a `.mp3` file / audio buffer.

### Errors

| Status | Condition |
|---|---|
| `400` | Audio could not be transcribed (empty/unintelligible) |
| `500` | Upstream OpenAI error (missing `OPENAI_API_KEY`, rate limit, etc.) |

### Reference client (Python)
```python
import base64, requests

with open("question.wav", "rb") as f:
    resp = requests.post(
        "https://headless-voicebot-poc.onrender.com/voice/query",
        files={"file": ("question.wav", f, "audio/wav")},
    )
data = resp.json()

print(data["transcript"], data["answer"])
with open("reply.mp3", "wb") as out:
    out.write(base64.b64decode(data["audio_base64"]))
```

---

## 2. Realtime streaming voice — `WS /ws/voice`

Use this for: live, low-latency, back-and-forth conversation (mic streaming in, audio streaming out), e.g. a phone/IVR bridge or a live web mic UI. This proxies directly to OpenAI's Realtime API session-by-session.

### Connection
```
wss://headless-voicebot-poc.onrender.com/ws/voice   (deployed)
ws://localhost:8000/ws/voice                        (local dev)
```

### Protocol
Client and server exchange raw JSON text frames following **OpenAI Realtime API** event schema (`input_audio_format` / `output_audio_format` = `pcm16`). The server does not alter the event shape — it forwards client events upstream and relays model events back, while also transcribing tool calls to the RAG index.

**Client → Server** (send continuously while capturing mic audio)
```json
{ "type": "input_audio_buffer.append", "audio": "<base64 PCM16 chunk>" }
```
Then, to signal end of user turn (if not using server VAD):
```json
{ "type": "input_audio_buffer.commit" }
{ "type": "response.create" }
```
> Server VAD is enabled by default (`turn_detection: server_vad`), so the server auto-detects end-of-speech — clients typically only need to stream `input_audio_buffer.append` continuously.

**Server → Client** (relayed from OpenAI, forwarded verbatim), notable event types:
| Event type | Meaning |
|---|---|
| `response.audio.delta` | Base64 PCM16 audio chunk of the bot's spoken reply (stream continuously, play as it arrives) |
| `response.audio_transcript.done` | Full text transcript of the bot's spoken reply |
| `conversation.item.input_audio_transcription.completed` | Transcript of what the user said |
| `response.text.done` | Text-only reply (if requested) |
| `error` | Error payload from the Realtime API |

Audio format both directions: **PCM16, 24kHz mono** (per OpenAI Realtime defaults) — not mp3, and not base64-wrapped-in-JSON like the HTTP endpoint's final blob; it's chunked as a continuous stream of `response.audio.delta` events.

The bot can also internally call a `search_knowledge_base` tool mid-conversation to pull RAG context — this is transparent to the client (just adds latency before the next audio chunk).

### Minimal client responsibilities
1. Capture mic → PCM16 24kHz → base64 → send as `input_audio_buffer.append` events.
2. Listen for `response.audio.delta` events → decode base64 → play/queue as PCM16 audio.
3. Handle `error` events and disconnects gracefully.

---

## Which endpoint should the dev team use?

| Need | Endpoint |
|---|---|
| Upload a fixed audio file, get one reply (batch tests, simple integrations, non-realtime IVR) | `POST /voice/query` |
| Live mic-in/speaker-out conversation with low latency | `WS /ws/voice` |

---

## Related endpoints (text-only, for reference)
- `POST /chat` — `{ "question": "..." }` → `{ question, answer, sources }`
- `POST /chat/batch` — `{ "questions": [...] }` → `{ results: [...] }`
- `GET /health` — liveness + indexed chunk count
