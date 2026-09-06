# Continuous Voice Call Integration Spec (Client-Side)

This document specifies how an external tool should integrate with the Headless
Voicebot's realtime voice API to run a **continuous, streaming call** (not a
one-shot request). No changes are required on the voicebot server — it already
supports this via a native WebSocket endpoint.

## Server support confirmed

- Server: FastAPI + `uvicorn[standard]`, deployed on Render (`runtime: docker`, `render.yaml`).
- Native WebSocket routes exist: `/ws/voice` (voice bridge) and `/ws/monitor` (live dashboard feed).
- Render web services proxy `Upgrade: websocket` correctly — this is already proven in production by the live monitor dashboard.
- No auth, no session-registration call, no special headers required at the API layer.

## 1. Connection

| | |
|---|---|
| Endpoint | `wss://headless-voicebot-poc.onrender.com/ws/voice` (prod) / `ws://localhost:8000/ws/voice` (local) |
| Protocol | Plain WebSocket, JSON text frames |
| Auth | None at the API layer |
| Lifetime | One socket = one call. Keep it open for the entire conversation — do not reconnect per turn |
| Timeout | Use a 60s+ connect timeout; Render free tier may cold-start (30-60s) on first hit after idle |

## 2. Audio format (both directions)

- Encoding: **PCM16** (signed 16-bit little-endian)
- Sample rate: **24000 Hz**
- Channels: **mono**
- Wire format: raw PCM bytes → base64-encoded → embedded in JSON text frames (never sent as raw binary WebSocket frames)

## 3. Outbound events (client → bot)

Send continuously while capturing audio:

```json
{ "type": "input_audio_buffer.append", "audio": "<base64 PCM16 chunk>" }
```

- Send in small chunks (e.g. 100ms of audio per message) back-to-back for the whole call.
- Server VAD (voice activity detection) is enabled by default — do **not** send manual
  `input_audio_buffer.commit` / `response.create` unless you want to override auto turn-detection.
- Optional: send a `session.update` event right after connecting only if you need to override
  voice/instructions; otherwise the server already configures the session with sane defaults.

## 4. Inbound events (bot → client) — handle at minimum

| Event type | Action required |
|---|---|
| `response.output_audio.delta` | `event["audio"]` is base64 PCM16 — decode and play/queue immediately |
| `response.output_audio_transcript.done` | `event["transcript"]` — full text of what the bot said (for logging/UI) |
| `conversation.item.input_audio_transcription.completed` | `event["transcript"]` — text of what the caller said |
| `response.output_text.done` | Text-only reply, if applicable |
| `error` | `event["error"]` — log it; decide whether to continue or terminate the call |

Ignore/pass through any other event types you don't need — the schema follows OpenAI's
Realtime API, so additional event types may appear over time.

## 5. Call termination

- No explicit "end session" message exists. **Closing the WebSocket ends the session.**
- Close it when: the user hangs up, your tool decides the conversation is done, or on an
  unrecoverable `error`.
- Handle disconnects/exceptions gracefully — do not leave the mic stream open after the socket closes.

## 6. Concurrency requirement

The implementation must send and receive **simultaneously** on the same socket (audio streams
both directions at once). Run the sender and receiver as concurrent tasks/threads, not a
sequential request/response loop.

## 7. Dependencies needed by the client

- A WebSocket client library (e.g. Python `websockets`, or the equivalent for your language).
- An audio capture source producing PCM16 24kHz mono (resample if your mic/telephony input differs).
- An audio playback sink accepting PCM16 24kHz mono.

## Reference implementation

A working Python example matching this exact spec lives in the voicebot repo at
`scripts/live_voice_client.py`. It uses `websockets` + `sounddevice` to stream mic input and
play back bot audio over a single persistent connection. Use it as a template; port the same
event flow (sections 3–6 above) to your tool's language/runtime if it isn't Python.
