# Voice Call UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a new end-user page at `/call` where a person can have a live voice conversation with the bot via a talking orb UI, with mute/end-call controls, a live-transcript drawer, and a post-call rating/feedback panel — separate from the existing admin monitoring dashboard.

**Architecture:** A single static page (`app/static/call.html`, vanilla HTML/CSS/JS, no build step) served by a new FastAPI route, talking directly to the existing `/ws/voice` WebSocket using the protocol documented in `docs/CLIENT_VOICE_INTEGRATION.md`. Two small backend additions support it: a new `voicebot.session_id` event emitted once per call from `app/realtime_bridge.py`, and a new `POST /feedback` endpoint in `app/main.py` that logs submitted feedback onto the existing session via `session_tracker`. All audio capture/playback/resampling and orb animation logic lives in JS modules loaded by `call.html`; there is no server-side change to audio handling.

**Tech Stack:** FastAPI (existing), vanilla JS + Web Audio API (`AudioWorklet`, `AnalyserNode`), Tailwind CSS via CDN (matches `app/static/index.html`), pytest + FastAPI `TestClient` for backend tests (matches `tests/test_rag_sources.py`).

**Spec:** `docs/superpowers/specs/2026-09-18-voice-call-ui-design.md`

## Global Constraints

- Audio wire format (both directions): PCM16 signed little-endian, 24000 Hz, mono, base64-encoded inside JSON text frames — never raw binary WS frames. (Spec: Audio pipeline; `docs/CLIENT_VOICE_INTEGRATION.md` §2)
- Outbound frames while unmuted: `{ "type": "input_audio_buffer.append", "audio": "<base64 PCM16>" }`, sent continuously in ~100ms chunks. Do not send manual `input_audio_buffer.commit` / `response.create` (server VAD handles turn-taking). (`docs/CLIENT_VOICE_INTEGRATION.md` §3)
- Inbound event types to handle: `response.output_audio.delta`, `response.output_audio_transcript.done`, `conversation.item.input_audio_transcription.completed`, `response.output_text.done`, `error`, plus the new `voicebot.session_id`. Unknown event types must be ignored, never throw. (`docs/CLIENT_VOICE_INTEGRATION.md` §4; spec: Backend `POST /feedback`)
- Ending a call = closing the WebSocket. No explicit "end session" message exists. (`docs/CLIENT_VOICE_INTEGRATION.md` §5)
- No build step: plain `<script>` tags / ES modules loaded directly by the browser, consistent with `app/static/index.html`. (Spec: Scope)
- No new persistence layer for feedback — record via `tracker.record_event(...)`, same durability tier as all other session data today. (Spec: Backend `POST /feedback`)
- Theme: CSS custom properties on `:root`, dark override via `@media (prefers-color-scheme: dark)` AND a `data-theme="dark"` attribute toggle, explicit choice persisted in `localStorage`. (Spec: Theming)

---

## File Structure

| File | Responsibility |
|---|---|
| `app/main.py` (modify) | New `GET /call` route; new `POST /feedback` route. |
| `app/realtime_bridge.py` (modify) | Emit `voicebot.session_id` event once per call, right after the socket is registered. |
| `app/static/call.html` (create) | Page markup: orb, status pill, control pill, drawer (transcript + feedback views), theme toggle. Loads the JS modules below as `<script type="module">`. |
| `app/static/call.css` (create) | All styling: layout, light/dark theme variables, orb animation keyframes, glassmorphism surfaces. |
| `app/static/call/audio-worklet-capture.js` (create) | `AudioWorkletProcessor` subclass: receives Float32 mic frames, downsamples to 24000 Hz, converts to PCM16, posts chunks to main thread. |
| `app/static/call/audio-capture.js` (create) | Main-thread module: `getUserMedia`, wires up `AudioContext` + the capture worklet + an `AnalyserNode` for mic level, exposes start/stop/mute and a level callback. |
| `app/static/call/audio-playback.js` (create) | Main-thread module: decodes base64 PCM16 chunks, schedules gapless playback via `AudioBufferSourceNode` chaining on a 24000 Hz `AudioContext`, exposes a level callback computed from each chunk's RMS. |
| `app/static/call/voice-socket.js` (create) | Wraps the `/ws/voice` WebSocket: connect/close, send `input_audio_buffer.append`, dispatch inbound events by type via a small event-emitter interface. |
| `app/static/call/orb.js` (create) | Drives the orb's visual state (`idle` / `listening` / `speaking` / `muted`) and animates a CSS custom property (`--orb-level`) from level callbacks using `requestAnimationFrame`. |
| `app/static/call/call-app.js` (create) | Top-level controller: wires the above modules together, owns call lifecycle state machine (idle → connecting → active → ended), drawer transcript/feedback rendering, feedback submission, theme toggle persistence. Entry point loaded by `call.html`. |
| `tests/test_feedback_endpoint.py` (create) | Backend test for `POST /feedback`. |
| `tests/test_call_route.py` (create) | Backend test for `GET /call`. |
| `tests/test_realtime_bridge_session_id_event.py` (create) | Backend test for the new `voicebot.session_id` event. |

Client-side JS modules have no automated test harness in this repo (no JS test runner exists today, and the spec doesn't ask for one) — they're verified by manual browser testing in the final task, per file structure boundaries above (capture / playback / socket / orb / controller each independently readable and swappable).

---

## Task 1: `POST /feedback` endpoint

**Files:**
- Modify: `app/main.py` (add a `FeedbackRequest` pydantic model and the route, near the other REST endpoints — after the `/api/stats`/`/api/knowledge-base` block, before the `Core Agent Endpoints` section header, since this is UI-facing but not session-monitoring-specific; place it in a new `# Feedback` section comment directly above it)
- Test: `tests/test_feedback_endpoint.py`

**Interfaces:**
- Consumes: `tracker` (`app.session_tracker.tracker`, already imported in `app/main.py`) — uses `tracker.create_session(...)` (test setup only) and the route uses `tracker.record_event(session_id, event_type, title, details) -> Optional[SessionEvent]`.
- Produces: `POST /feedback` accepting JSON body `{session_id: str, rating: int, resolved: bool, comment: Optional[str]}`, returning `{"status": "ok"}` on success or 404 if `session_id` doesn't exist. Later tasks (call-app.js) POST to this endpoint.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_feedback_endpoint.py
from fastapi.testclient import TestClient

from app.main import app
from app.session_tracker import tracker


def test_feedback_records_event_on_existing_session():
    session = tracker.create_session(session_type="websocket_voice", summary="test call")
    client = TestClient(app)

    resp = client.post("/feedback", json={
        "session_id": session.id,
        "rating": 4,
        "resolved": True,
        "comment": "worked great",
    })

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
    stored = tracker.get_session(session.id).to_dict(include_events=True)
    feedback_events = [e for e in stored["events"] if e["event_type"] == "feedback"]
    assert len(feedback_events) == 1
    assert feedback_events[0]["details"] == {
        "rating": 4,
        "resolved": True,
        "comment": "worked great",
    }


def test_feedback_comment_is_optional():
    session = tracker.create_session(session_type="websocket_voice", summary="test call 2")
    client = TestClient(app)

    resp = client.post("/feedback", json={
        "session_id": session.id,
        "rating": 2,
        "resolved": False,
    })

    assert resp.status_code == 200
    stored = tracker.get_session(session.id).to_dict(include_events=True)
    feedback_events = [e for e in stored["events"] if e["event_type"] == "feedback"]
    assert feedback_events[0]["details"]["comment"] is None


def test_feedback_404s_for_unknown_session():
    client = TestClient(app)

    resp = client.post("/feedback", json={
        "session_id": "does-not-exist",
        "rating": 3,
        "resolved": True,
    })

    assert resp.status_code == 404
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_feedback_endpoint.py -v`
Expected: FAIL — `404 Not Found` for all three (route doesn't exist yet), or a collection error if imports don't resolve.

- [ ] **Step 3: Write minimal implementation**

In `app/main.py`, add near the other request models (next to `BatchChatRequest`):

```python
class FeedbackRequest(BaseModel):
    session_id: str
    rating: int
    resolved: bool
    comment: Optional[str] = None
```

Add the route after the `/api/knowledge-base` endpoint (still inside the `# Session Monitoring REST & WebSocket APIs` region is fine since it operates on `tracker`, but give it its own comment header):

```python
# ==========================================
# Feedback
# ==========================================

@app.post("/feedback", responses={404: {"description": "Session not found"}})
def submit_feedback(req: FeedbackRequest):
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_feedback_endpoint.py -v`
Expected: PASS (3 passed)

- [ ] **Step 5: Commit**

```bash
git add app/main.py tests/test_feedback_endpoint.py
git commit -m "feat: add POST /feedback endpoint for end-of-call ratings"
```

---

## Task 2: `voicebot.session_id` event from the voice bridge

**Files:**
- Modify: `app/realtime_bridge.py:151-166` (`run_voice_bridge`, right after `tracker.register_voice_socket(session_id, client_ws)`)
- Test: `tests/test_realtime_bridge_session_id_event.py`

**Interfaces:**
- Consumes: `client_ws: WebSocket` (already available in `run_voice_bridge`), `session_id: str` (already available).
- Produces: one JSON text frame sent on `client_ws` immediately after registration, shape `{"type": "voicebot.session_id", "session_id": "<id>"}`. `voice-socket.js` (Task 5) listens for this type to learn its session id for the `/feedback` POST.

This event must be sent before the OpenAI connection attempt, so the client has its session id even if the upstream OpenAI connection later fails.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_realtime_bridge_session_id_event.py
import asyncio
import json

import pytest

from app import realtime_bridge


class FakeWebSocket:
    def __init__(self):
        self.sent = []
        self.client = None

    async def accept(self):
        pass

    async def send_text(self, text):
        self.sent.append(text)

    async def receive_text(self):
        # Block forever so _client_loop never returns on its own in this test;
        # the test only drives run_voice_bridge far enough to observe the
        # session_id frame, then cancels.
        await asyncio.sleep(3600)


@pytest.mark.asyncio
async def test_sends_session_id_event_before_openai_connect(monkeypatch):
    fake_ws = FakeWebSocket()

    async def fake_connect(*args, **kwargs):
        raise RuntimeError("stop after session id is sent")

    monkeypatch.setattr(realtime_bridge.websockets, "connect", fake_connect)

    with pytest.raises(RuntimeError):
        await realtime_bridge.run_voice_bridge(fake_ws, index=None)

    assert len(fake_ws.sent) == 1
    payload = json.loads(fake_ws.sent[0])
    assert payload["type"] == "voicebot.session_id"
    assert isinstance(payload["session_id"], str) and payload["session_id"]
```

Check `requirements.txt` / existing test config for `pytest-asyncio` before writing this — if it isn't already a dependency, the test file needs `pytest.ini`/`pyproject` marker config or the test must instead drive the coroutine with `asyncio.run(...)` directly instead of the `@pytest.mark.asyncio` decorator. Verify with:

```bash
python -m pip show pytest-asyncio
```

If not installed, add `pytest-asyncio` to `requirements.txt` and add to `pytest.ini` (create if absent):

```ini
[pytest]
asyncio_mode = auto
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_realtime_bridge_session_id_event.py -v`
Expected: FAIL — `fake_ws.sent` is empty (`AssertionError: assert 0 == 1`), since `run_voice_bridge` doesn't send this frame yet. (If pytest-asyncio wiring is broken you'll instead see a collection/async warning — fix that first before treating this as a real failure.)

- [ ] **Step 3: Write minimal implementation**

In `app/realtime_bridge.py`, modify `run_voice_bridge`:

```python
async def run_voice_bridge(client_ws: WebSocket, index: RagIndex) -> None:
    await client_ws.accept()

    client_host = client_ws.client.host if client_ws.client else "unknown"
    session = tracker.create_session(
        session_type="websocket_voice",
        client_host=client_host,
        summary="Live Voice Session (WebSocket)",
    )
    session_id = session.id

    headers = [
        ("Authorization", f"Bearer {config.OPENAI_API_KEY}"),
    ]

    tracker.register_voice_socket(session_id, client_ws)
    await client_ws.send_text(json.dumps({"type": "voicebot.session_id", "session_id": session_id}))
    try:
        async with websockets.connect(
            OPENAI_REALTIME_URL, extra_headers=headers, max_size=None
        ) as openai_ws:
            ...
```

(Only the `await client_ws.send_text(...)` line is new; everything else in the function is unchanged.)

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_realtime_bridge_session_id_event.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/realtime_bridge.py tests/test_realtime_bridge_session_id_event.py requirements.txt pytest.ini
git commit -m "feat: emit voicebot.session_id event at start of voice bridge session"
```

---

## Task 3: `GET /call` route + page skeleton

**Files:**
- Modify: `app/main.py` (new route, in the `# UI & Dashboard Endpoints` section, after `dashboard()`)
- Create: `app/static/call.html`
- Create: `app/static/call.css`
- Test: `tests/test_call_route.py`

**Interfaces:**
- Produces: `GET /call` returns the contents of `app/static/call.html` with `Content-Type: text/html`. This task only builds static markup/CSS structure and the theme toggle; no JS call logic yet (Tasks 4-8 add the modules `call.html` references, which don't exist until those tasks — so this task's `call.html` must load them with plain `<script type="module" src="...">` tags that 404 harmlessly until later tasks create the files; the page must still render its static markup and theme toggle correctly with those 404s, since module scripts fail independently of page render).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_call_route.py
from fastapi.testclient import TestClient

from app.main import app


def test_call_route_serves_html():
    client = TestClient(app)

    resp = client.get("/call")

    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    assert "<title>" in resp.text
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_call_route.py -v`
Expected: FAIL with 404 (route doesn't exist).

- [ ] **Step 3: Write minimal implementation**

Add to `app/main.py`, directly below the existing `dashboard()` function:

```python
@app.get("/call", response_class=HTMLResponse)
async def call_ui():
    """Serves the end-user voice call page."""
    call_html = os.path.join(STATIC_DIR, "call.html")
    if os.path.exists(call_html):
        return FileResponse(call_html)
    return HTMLResponse("<h2>Voice call UI is loading...</h2>")
```

Create `app/static/call.css`:

```css
:root {
  --bg: #f4f5f7;
  --surface: rgba(255, 255, 255, 0.72);
  --surface-border: rgba(15, 23, 42, 0.08);
  --text: #0f172a;
  --text-muted: #64748b;
  --danger: #e11d48;
  --danger-hover: #be123c;
  --orb-glow: rgba(43, 210, 255, 0.35);
  --shadow: 0 20px 60px rgba(15, 23, 42, 0.12);
}

:root[data-theme="dark"] {
  --bg: #0b0d12;
  --surface: rgba(23, 26, 33, 0.72);
  --surface-border: rgba(255, 255, 255, 0.08);
  --text: #f1f5f9;
  --text-muted: #94a3b8;
  --danger: #fb7185;
  --danger-hover: #f43f5e;
  --orb-glow: rgba(43, 210, 255, 0.45);
  --shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
}

@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #0b0d12;
    --surface: rgba(23, 26, 33, 0.72);
    --surface-border: rgba(255, 255, 255, 0.08);
    --text: #f1f5f9;
    --text-muted: #94a3b8;
    --danger: #fb7185;
    --danger-hover: #f43f5e;
    --orb-glow: rgba(43, 210, 255, 0.45);
    --shadow: 0 20px 60px rgba(0, 0, 0, 0.5);
  }
}

* { box-sizing: border-box; }

html, body {
  margin: 0;
  height: 100%;
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, sans-serif;
  transition: background 0.3s ease, color 0.3s ease;
}

.call-shell {
  position: relative;
  height: 100vh;
  width: 100vw;
  overflow: hidden;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}

.theme-toggle {
  position: absolute;
  top: 20px;
  right: 20px;
  width: 40px;
  height: 40px;
  border-radius: 999px;
  border: 1px solid var(--surface-border);
  background: var(--surface);
  backdrop-filter: blur(12px);
  color: var(--text);
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 18px;
}

.status-pill {
  margin-bottom: 24px;
  padding: 6px 16px;
  border-radius: 999px;
  background: var(--surface);
  border: 1px solid var(--surface-border);
  color: var(--text-muted);
  font-size: 13px;
  font-weight: 600;
  letter-spacing: 0.02em;
  backdrop-filter: blur(12px);
}

.orb-wrap {
  width: 260px;
  height: 260px;
  display: flex;
  align-items: center;
  justify-content: center;
  --orb-level: 0;
}

.orb-wrap svg {
  width: 100%;
  height: 100%;
  filter: drop-shadow(0 0 calc(20px + 60px * var(--orb-level)) var(--orb-glow));
  transform: scale(calc(1 + 0.12 * var(--orb-level)));
  transition: transform 0.05s linear, filter 0.05s linear;
}

.orb-wrap[data-state="idle"] svg {
  animation: orb-breathe 4s ease-in-out infinite;
}

.orb-wrap[data-state="muted"] svg {
  filter: grayscale(0.6) brightness(0.85);
}

@keyframes orb-breathe {
  0%, 100% { transform: scale(1); }
  50% { transform: scale(1.04); }
}

.control-pill {
  margin-top: 32px;
  display: flex;
  align-items: center;
  gap: 12px;
  padding: 10px;
  border-radius: 999px;
  background: var(--surface);
  border: 1px solid var(--surface-border);
  box-shadow: var(--shadow);
  backdrop-filter: blur(16px);
}

.control-btn {
  width: 48px;
  height: 48px;
  border-radius: 999px;
  border: none;
  background: transparent;
  color: var(--text);
  font-size: 18px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

.control-btn[data-active="true"] {
  background: var(--surface-border);
}

.control-btn.end-call {
  background: var(--danger);
  color: white;
}

.control-btn.end-call:hover {
  background: var(--danger-hover);
}

.control-btn.start-call {
  width: auto;
  padding: 0 24px;
  border-radius: 999px;
  background: var(--danger);
  color: white;
  font-weight: 600;
}

.drawer {
  position: fixed;
  top: 0;
  right: 0;
  height: 100%;
  width: 360px;
  max-width: 90vw;
  background: var(--surface);
  border-left: 1px solid var(--surface-border);
  backdrop-filter: blur(20px);
  box-shadow: var(--shadow);
  transform: translateX(100%);
  transition: transform 0.25s ease;
  display: flex;
  flex-direction: column;
  padding: 20px;
  overflow-y: auto;
}

.drawer[data-open="true"] {
  transform: translateX(0);
}

.transcript-message {
  margin-bottom: 12px;
  font-size: 14px;
  line-height: 1.4;
}

.transcript-message .role {
  font-weight: 700;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.04em;
  color: var(--text-muted);
  display: block;
  margin-bottom: 2px;
}

.feedback-view .rating-row {
  display: flex;
  gap: 8px;
  margin: 12px 0;
}

.feedback-view .rating-option {
  flex: 1;
  padding: 10px 0;
  text-align: center;
  border-radius: 12px;
  border: 1px solid var(--surface-border);
  background: transparent;
  cursor: pointer;
  color: var(--text);
}

.feedback-view .rating-option[data-selected="true"] {
  border-color: var(--danger);
  background: var(--surface-border);
}

.feedback-view .resolved-row {
  display: flex;
  gap: 8px;
  margin: 12px 0;
}

.feedback-view .resolved-option {
  flex: 1;
  padding: 8px 0;
  border-radius: 12px;
  border: 1px solid var(--surface-border);
  background: transparent;
  cursor: pointer;
  color: var(--text);
}

.feedback-view .resolved-option[data-selected="true"] {
  border-color: var(--danger);
  background: var(--surface-border);
}

.feedback-view textarea {
  width: 100%;
  min-height: 80px;
  border-radius: 12px;
  border: 1px solid var(--surface-border);
  background: transparent;
  color: var(--text);
  padding: 10px;
  font-family: inherit;
  resize: vertical;
}

.feedback-view .submit-btn {
  margin-top: 12px;
  width: 100%;
  padding: 12px;
  border-radius: 999px;
  border: none;
  background: var(--danger);
  color: white;
  font-weight: 600;
  cursor: pointer;
}
```

Create `app/static/call.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Talk to Voicebot</title>
  <link rel="stylesheet" href="/static/call.css" />
</head>
<body>
  <div class="call-shell">
    <button class="theme-toggle" id="themeToggle" aria-label="Toggle theme" type="button">🌙</button>

    <div class="status-pill" id="statusPill">Idle</div>

    <div class="orb-wrap" id="orbWrap" data-state="idle">
      <svg width="346" height="346" viewBox="0 0 346 346" fill="none" xmlns="http://www.w3.org/2000/svg">
        <g filter="url(#filter0_d_719_62)">
          <rect x="35" y="21" width="276" height="276" rx="138" fill="white"/>
          <path d="M173 257C227.124 257 271 213.124 271 159C271 104.876 227.124 61 173 61C118.876 61 75 104.876 75 159C75 213.124 118.876 257 173 257Z" fill="url(#paint0_linear_719_62)"/>
          <mask id="mask0_719_62" style="mask-type:luminance" maskUnits="userSpaceOnUse" x="75" y="61" width="196" height="196">
            <path d="M173 257C227.124 257 271 213.124 271 159C271 104.876 227.124 61 173 61C118.876 61 75 104.876 75 159C75 213.124 118.876 257 173 257Z" fill="white"/>
          </mask>
          <g mask="url(#mask0_719_62)">
            <path opacity="0.4" d="M61 187.004C107.667 131.004 154.333 131.004 201 187.004C247.667 243.004 285 233.671 313 159.004" stroke="white" stroke-width="14"/>
            <path opacity="0.6" d="M33 145.006C107.667 201.006 173 196.339 229 131.006C285 65.6723 313 84.339 313 187.006" stroke="white" stroke-width="8.4"/>
            <path opacity="0.1" d="M135.2 257C189.324 257 233.2 213.124 233.2 159C233.2 104.876 189.324 61 135.2 61C81.076 61 37.2 104.876 37.2 159C37.2 213.124 81.076 257 135.2 257Z" fill="white"/>
          </g>
          <path opacity="0.5" d="M173 257C227.124 257 271 213.124 271 159C271 104.876 227.124 61 173 61C118.876 61 75 104.876 75 159C75 213.124 118.876 257 173 257Z" stroke="white" stroke-width="2.8"/>
        </g>
        <defs>
          <filter id="filter0_d_719_62" x="0" y="0" width="346" height="346" filterUnits="userSpaceOnUse" color-interpolation-filters="sRGB">
            <feFlood flood-opacity="0" result="BackgroundImageFix"/>
            <feColorMatrix in="SourceAlpha" type="matrix" values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 127 0" result="hardAlpha"/>
            <feMorphology radius="1.4" operator="dilate" in="SourceAlpha" result="effect1_dropShadow_719_62"/>
            <feOffset dy="14"/>
            <feGaussianBlur stdDeviation="16.8"/>
            <feComposite in2="hardAlpha" operator="out"/>
            <feColorMatrix type="matrix" values="0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0.28 0"/>
            <feBlend mode="normal" in2="BackgroundImageFix" result="effect1_dropShadow_719_62"/>
            <feBlend mode="normal" in="SourceGraphic" in2="effect1_dropShadow_719_62" result="shape"/>
          </filter>
          <linearGradient id="paint0_linear_719_62" x1="33" y1="19" x2="313" y2="299" gradientUnits="userSpaceOnUse">
            <stop stop-color="#FA8BFF"/>
            <stop offset="0.52" stop-color="#2BD2FF"/>
            <stop offset="1" stop-color="#2BFF88"/>
          </linearGradient>
        </defs>
      </svg>
    </div>

    <div class="control-pill" id="controlPill">
      <button class="control-btn start-call" id="startCallBtn" type="button">Start Call</button>
    </div>

    <aside class="drawer" id="drawer" data-open="false">
      <div class="transcript-view" id="transcriptView">
        <h3>Transcript</h3>
        <div id="transcriptMessages"></div>
      </div>
      <div class="feedback-view" id="feedbackView" style="display:none">
        <h3>How was the call?</h3>
        <div class="rating-row" id="ratingRow">
          <button class="rating-option" data-value="1" type="button">😞</button>
          <button class="rating-option" data-value="2" type="button">🙁</button>
          <button class="rating-option" data-value="3" type="button">😐</button>
          <button class="rating-option" data-value="4" type="button">🙂</button>
          <button class="rating-option" data-value="5" type="button">😄</button>
        </div>
        <p>Was your issue resolved?</p>
        <div class="resolved-row" id="resolvedRow">
          <button class="resolved-option" data-value="true" type="button">Yes</button>
          <button class="resolved-option" data-value="false" type="button">No</button>
        </div>
        <textarea id="feedbackComment" placeholder="Anything else you'd like to add? (optional)"></textarea>
        <button class="submit-btn" id="submitFeedbackBtn" type="button">Submit</button>
      </div>
    </aside>
  </div>

  <script type="module" src="/static/call/call-app.js"></script>
</body>
</html>
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_call_route.py -v`
Expected: PASS

Manually verify markup/theme rendering: `python -m uvicorn app.main:app --reload`, open `http://localhost:8000/call` in a browser. The page should render the idle orb, status pill "Idle", "Start Call" button, and a browser console error for the missing `call-app.js` module (expected — created in Task 8). Toggling `data-theme` via devtools (`document.documentElement.dataset.theme = "dark"`) should flip the palette.

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/static/call.html app/static/call.css tests/test_call_route.py
git commit -m "feat: add /call page skeleton with orb, controls, and drawer markup"
```

---

## Task 4: Mic capture module (`audio-worklet-capture.js` + `audio-capture.js`)

**Files:**
- Create: `app/static/call/audio-worklet-capture.js`
- Create: `app/static/call/audio-capture.js`

**Interfaces:**
- Consumes: browser `AudioContext`, `navigator.mediaDevices.getUserMedia`.
- Produces (from `audio-capture.js`, the module other tasks import):
  ```js
  // default export: a class
  class AudioCapture {
    // async, requests mic permission and starts the worklet + analyser.
    // onChunk: (base64Pcm16Chunk: string) => void — called ~10x/sec while unmuted and not stopped.
    // onLevel: (level: number 0..1) => void — called every animation frame with current mic RMS level, active whether muted or not.
    async start({ onChunk, onLevel }) {}
    setMuted(muted /* boolean */) {}
    stop() {}
  }
  export default AudioCapture;
  ```

- [ ] **Step 1: Create the AudioWorkletProcessor**

`app/static/call/audio-worklet-capture.js`:

```js
// Runs on the audio rendering thread. Receives Float32 frames at the
// context's native sample rate, downsamples to 24000 Hz mono, converts to
// PCM16, and posts ~100ms chunks back to the main thread as base64 strings
// via a small side-channel (base64 encoding done in-worklet since atob/btoa
// are available in AudioWorkletGlobalScope).
class CaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this.targetRate = 24000;
    this.nativeRate = sampleRate; // global in AudioWorkletGlobalScope
    this.ratio = this.nativeRate / this.targetRate;
    this.buffer = [];
    this.samplesPerChunk = Math.round(this.targetRate * 0.1); // 100ms
    this.resampleAccumulator = [];
  }

  // Simple linear-interpolation downsampler.
  _downsample(float32Input) {
    const outLength = Math.floor(float32Input.length / this.ratio);
    const out = new Float32Array(outLength);
    for (let i = 0; i < outLength; i++) {
      const srcIndex = i * this.ratio;
      const i0 = Math.floor(srcIndex);
      const i1 = Math.min(i0 + 1, float32Input.length - 1);
      const frac = srcIndex - i0;
      out[i] = float32Input[i0] * (1 - frac) + float32Input[i1] * frac;
    }
    return out;
  }

  _floatToPcm16(float32) {
    const pcm16 = new Int16Array(float32.length);
    for (let i = 0; i < float32.length; i++) {
      const s = Math.max(-1, Math.min(1, float32[i]));
      pcm16[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
    }
    return pcm16;
  }

  _pcm16ToBase64(pcm16) {
    const bytes = new Uint8Array(pcm16.buffer);
    let binary = "";
    for (let i = 0; i < bytes.length; i++) {
      binary += String.fromCharCode(bytes[i]);
    }
    return btoa(binary);
  }

  process(inputs) {
    const input = inputs[0];
    if (!input || input.length === 0) return true;
    const channel = input[0]; // mono
    if (!channel || channel.length === 0) return true;

    const downsampled = this._downsample(channel);
    this.resampleAccumulator.push(...downsampled);

    while (this.resampleAccumulator.length >= this.samplesPerChunk) {
      const chunk = this.resampleAccumulator.splice(0, this.samplesPerChunk);
      const pcm16 = this._floatToPcm16(Float32Array.from(chunk));
      const base64 = this._pcm16ToBase64(pcm16);
      this.port.postMessage({ type: "chunk", base64 });
    }

    // Also post raw level info every process() call (~2.9ms at 128 frames)
    // for responsive orb animation; downstream code throttles to rAF.
    let sumSquares = 0;
    for (let i = 0; i < channel.length; i++) sumSquares += channel[i] * channel[i];
    const rms = Math.sqrt(sumSquares / channel.length);
    this.port.postMessage({ type: "level", rms });

    return true;
  }
}

registerProcessor("capture-processor", CaptureProcessor);
```

- [ ] **Step 2: Create the main-thread capture module**

`app/static/call/audio-capture.js`:

```js
export default class AudioCapture {
  constructor() {
    this.audioContext = null;
    this.workletNode = null;
    this.mediaStream = null;
    this.muted = false;
    this._onChunk = null;
    this._onLevel = null;
    this._latestLevel = 0;
    this._rafId = null;
  }

  async start({ onChunk, onLevel }) {
    this._onChunk = onChunk;
    this._onLevel = onLevel;

    this.mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    this.audioContext = new AudioContext();
    await this.audioContext.audioWorklet.addModule("/static/call/audio-worklet-capture.js");

    const source = this.audioContext.createMediaStreamSource(this.mediaStream);
    this.workletNode = new AudioWorkletNode(this.audioContext, "capture-processor");

    this.workletNode.port.onmessage = (event) => {
      const { type } = event.data;
      if (type === "chunk") {
        if (!this.muted && this._onChunk) this._onChunk(event.data.base64);
      } else if (type === "level") {
        this._latestLevel = event.data.rms;
      }
    };

    source.connect(this.workletNode);
    // Worklet doesn't need to reach speakers; connect to a muted destination
    // path is unnecessary since we never connect workletNode to context.destination.

    this._tickLevel();
  }

  _tickLevel() {
    if (this._onLevel) this._onLevel(Math.min(1, this._latestLevel * 6));
    this._rafId = requestAnimationFrame(() => this._tickLevel());
  }

  setMuted(muted) {
    this.muted = muted;
  }

  stop() {
    if (this._rafId) cancelAnimationFrame(this._rafId);
    if (this.workletNode) this.workletNode.port.onmessage = null;
    if (this.mediaStream) {
      this.mediaStream.getTracks().forEach((track) => track.stop());
    }
    if (this.audioContext) {
      this.audioContext.close();
    }
    this.audioContext = null;
    this.workletNode = null;
    this.mediaStream = null;
  }
}
```

- [ ] **Step 3: Manual verification (deferred to Task 8's end-to-end check)**

This module has no standalone test harness (no JS mic input available in pytest/CI). Its correctness is verified in Task 8's manual browser walkthrough: mic permission prompt appears, `onLevel` visibly drives orb glow while idle-testing in the console (`import('/static/call/audio-capture.js')` from devtools and manually calling `start` is possible but not required — full verification happens wired into `call-app.js`).

- [ ] **Step 4: Commit**

```bash
git add app/static/call/audio-worklet-capture.js app/static/call/audio-capture.js
git commit -m "feat: add mic capture AudioWorklet + main-thread wrapper"
```

---

## Task 5: Playback module (`audio-playback.js`)

**Files:**
- Create: `app/static/call/audio-playback.js`

**Interfaces:**
- Consumes: base64 PCM16 24kHz mono chunks pushed in via `enqueue(base64Chunk)`.
- Produces (the class other tasks import):
  ```js
  class AudioPlayback {
    start() {}                     // creates/resumes the playback AudioContext
    enqueue(base64Pcm16Chunk) {}   // decode + schedule for gapless playback
    onLevel(callback) {}           // callback: (level: number 0..1) => void, called ~per animation frame while audio is playing, 0 when idle
    stop() {}                      // stop playback, clear queue
  }
  export default AudioPlayback;
  ```

- [ ] **Step 1: Write the module**

`app/static/call/audio-playback.js`:

```js
export default class AudioPlayback {
  constructor() {
    this.audioContext = null;
    this.nextStartTime = 0;
    this._levelCallback = null;
    this._currentLevel = 0;
    this._rafId = null;
  }

  start() {
    if (!this.audioContext) {
      this.audioContext = new AudioContext({ sampleRate: 24000 });
      this.nextStartTime = this.audioContext.currentTime;
    }
    this._tickLevel();
  }

  onLevel(callback) {
    this._levelCallback = callback;
  }

  _base64ToPcm16(base64) {
    const binary = atob(base64);
    const bytes = new Uint8Array(binary.length);
    for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);
    return new Int16Array(bytes.buffer);
  }

  _pcm16ToFloat32(pcm16) {
    const float32 = new Float32Array(pcm16.length);
    for (let i = 0; i < pcm16.length; i++) {
      float32[i] = pcm16[i] / (pcm16[i] < 0 ? 0x8000 : 0x7fff);
    }
    return float32;
  }

  enqueue(base64Chunk) {
    if (!this.audioContext) return;
    const pcm16 = this._base64ToPcm16(base64Chunk);
    const float32 = this._pcm16ToFloat32(pcm16);

    let sumSquares = 0;
    for (let i = 0; i < float32.length; i++) sumSquares += float32[i] * float32[i];
    this._currentLevel = Math.min(1, Math.sqrt(sumSquares / float32.length) * 6);

    const buffer = this.audioContext.createBuffer(1, float32.length, 24000);
    buffer.copyToChannel(float32, 0);

    const source = this.audioContext.createBufferSource();
    source.buffer = buffer;
    source.connect(this.audioContext.destination);

    const now = this.audioContext.currentTime;
    const startAt = Math.max(now, this.nextStartTime);
    source.start(startAt);
    this.nextStartTime = startAt + buffer.duration;

    source.onended = () => {
      if (this.audioContext && this.audioContext.currentTime >= this.nextStartTime - 0.01) {
        this._currentLevel = 0;
      }
    };
  }

  _tickLevel() {
    if (this._levelCallback) this._levelCallback(this._currentLevel);
    this._rafId = requestAnimationFrame(() => this._tickLevel());
  }

  stop() {
    if (this._rafId) cancelAnimationFrame(this._rafId);
    if (this.audioContext) {
      this.audioContext.close();
    }
    this.audioContext = null;
    this.nextStartTime = 0;
    this._currentLevel = 0;
  }
}
```

- [ ] **Step 2: Manual verification (deferred to Task 8)**

No standalone harness; verified end-to-end in Task 8 by hearing bot audio play back and watching the orb pulse while it plays.

- [ ] **Step 3: Commit**

```bash
git add app/static/call/audio-playback.js
git commit -m "feat: add gapless PCM16 playback module"
```

---

## Task 6: WebSocket wrapper (`voice-socket.js`)

**Files:**
- Create: `app/static/call/voice-socket.js`

**Interfaces:**
- Consumes: nothing from other client modules directly; opens its own `WebSocket`.
- Produces (the class `call-app.js` imports):
  ```js
  class VoiceSocket {
    constructor(url /* string, e.g. wss://host/ws/voice */) {}
    connect() {}                          // returns a Promise that resolves once the socket is open
    sendAudioChunk(base64Pcm16) {}        // sends { type: "input_audio_buffer.append", audio }
    on(eventType, handler) {}             // eventType: "session_id" | "audio_delta" | "bot_transcript" | "user_transcript" | "error" | "close"
                                           // handler receives the relevant payload (see Step 1 mapping table)
    close() {}
  }
  export default VoiceSocket;
  ```

- [ ] **Step 1: Write the module**

Event mapping table (raw server `type` → this module's emitted event name → payload passed to handler):

| Server `type` | Emitted as | Handler payload |
|---|---|---|
| `voicebot.session_id` | `session_id` | `{ sessionId: string }` |
| `response.output_audio.delta` | `audio_delta` | `{ audio: string }` (base64 PCM16) |
| `response.output_audio_transcript.done` | `bot_transcript` | `{ transcript: string }` |
| `conversation.item.input_audio_transcription.completed` | `user_transcript` | `{ transcript: string }` |
| `error` | `error` | `{ error: any }` |
| (socket closed) | `close` | `{ code: number, reason: string }` |
| anything else | (ignored) | — |

```js
export default class VoiceSocket {
  constructor(url) {
    this.url = url;
    this.ws = null;
    this.handlers = {};
  }

  on(eventType, handler) {
    this.handlers[eventType] = handler;
  }

  _emit(eventType, payload) {
    if (this.handlers[eventType]) this.handlers[eventType](payload);
  }

  connect() {
    return new Promise((resolve, reject) => {
      this.ws = new WebSocket(this.url);

      this.ws.onopen = () => resolve();
      this.ws.onerror = (err) => {
        this._emit("error", { error: err });
        reject(err);
      };
      this.ws.onclose = (event) => {
        this._emit("close", { code: event.code, reason: event.reason });
      };
      this.ws.onmessage = (event) => this._handleMessage(event.data);
    });
  }

  _handleMessage(raw) {
    let data;
    try {
      data = JSON.parse(raw);
    } catch (e) {
      return;
    }

    switch (data.type) {
      case "voicebot.session_id":
        this._emit("session_id", { sessionId: data.session_id });
        break;
      case "response.output_audio.delta":
        this._emit("audio_delta", { audio: data.audio });
        break;
      case "response.output_audio_transcript.done":
        this._emit("bot_transcript", { transcript: data.transcript });
        break;
      case "conversation.item.input_audio_transcription.completed":
        this._emit("user_transcript", { transcript: data.transcript });
        break;
      case "error":
        this._emit("error", { error: data.error });
        break;
      default:
        break; // ignore unknown event types per protocol spec
    }
  }

  sendAudioChunk(base64Pcm16) {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) return;
    this.ws.send(JSON.stringify({ type: "input_audio_buffer.append", audio: base64Pcm16 }));
  }

  close() {
    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }
  }
}
```

- [ ] **Step 2: Manual verification (deferred to Task 8)**

Verified end-to-end: connecting successfully transitions status pill to "Connecting" → "Listening", and a live call round-trips audio.

- [ ] **Step 3: Commit**

```bash
git add app/static/call/voice-socket.js
git commit -m "feat: add /ws/voice client wrapper with typed event dispatch"
```

---

## Task 7: Orb animation controller (`orb.js`)

**Files:**
- Create: `app/static/call/orb.js`

**Interfaces:**
- Consumes: a DOM element reference (the `#orbWrap` div from `call.html`).
- Produces (the class `call-app.js` imports):
  ```js
  class OrbController {
    constructor(orbElement /* HTMLElement, e.g. document.getElementById("orbWrap") */) {}
    setState(state /* "idle" | "listening" | "speaking" | "muted" */) {}
    setLevel(level /* number 0..1 */) {}  // updates --orb-level CSS custom property
  }
  export default OrbController;
  ```

- [ ] **Step 1: Write the module**

```js
export default class OrbController {
  constructor(orbElement) {
    this.el = orbElement;
    this.state = "idle";
  }

  setState(state) {
    this.state = state;
    this.el.dataset.state = state;
  }

  setLevel(level) {
    const clamped = Math.max(0, Math.min(1, level));
    this.el.style.setProperty("--orb-level", clamped.toFixed(3));
  }
}
```

(The visual mapping of `--orb-level` to scale/glow is already defined in `call.css`'s `.orb-wrap svg` rule from Task 3 — this module only ever writes the state attribute and the CSS variable, keeping all visual tuning in CSS.)

- [ ] **Step 2: Manual verification (deferred to Task 8)**

Verified end-to-end: orb visibly breathes when idle, glows/scales with mic input while listening, glows/scales with bot audio while speaking, and dims when muted.

- [ ] **Step 3: Commit**

```bash
git add app/static/call/orb.js
git commit -m "feat: add orb state/level controller"
```

---

## Task 8: Call controller (`call-app.js`) — wires everything together

**Files:**
- Create: `app/static/call/call-app.js`

**Interfaces:**
- Consumes: `AudioCapture` (Task 4), `AudioPlayback` (Task 5), `VoiceSocket` (Task 6), `OrbController` (Task 7), and the DOM elements defined in `call.html` (Task 3): `#themeToggle`, `#statusPill`, `#orbWrap`, `#controlPill`, `#startCallBtn` (created dynamically at runtime — see below), `#drawer`, `#transcriptView`, `#transcriptMessages`, `#feedbackView`, `#ratingRow`, `#resolvedRow`, `#feedbackComment`, `#submitFeedbackBtn`.
- Produces: nothing consumed by other files — this is the page's entry point, loaded via `<script type="module" src="/static/call/call-app.js">` in `call.html`.

State machine: `idle → connecting → active → ended`. `ended` shows feedback then returns to `idle`.

- [ ] **Step 1: Write the theme toggle logic**

```js
const THEME_KEY = "voicebot_theme";

function initTheme() {
  const stored = localStorage.getItem(THEME_KEY);
  if (stored === "dark" || stored === "light") {
    document.documentElement.dataset.theme = stored;
  }
  updateThemeIcon();
}

function updateThemeIcon() {
  const btn = document.getElementById("themeToggle");
  const isDark = document.documentElement.dataset.theme === "dark" ||
    (!document.documentElement.dataset.theme && window.matchMedia("(prefers-color-scheme: dark)").matches);
  btn.textContent = isDark ? "☀️" : "🌙";
}

function toggleTheme() {
  const current = document.documentElement.dataset.theme ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem(THEME_KEY, next);
  updateThemeIcon();
}
```

- [ ] **Step 2: Write the call lifecycle controller**

```js
import AudioCapture from "./audio-capture.js";
import AudioPlayback from "./audio-playback.js";
import VoiceSocket from "./voice-socket.js";
import OrbController from "./orb.js";

class CallApp {
  constructor() {
    this.orb = new OrbController(document.getElementById("orbWrap"));
    this.statusPill = document.getElementById("statusPill");
    this.controlPill = document.getElementById("controlPill");
    this.drawer = document.getElementById("drawer");
    this.transcriptView = document.getElementById("transcriptView");
    this.transcriptMessages = document.getElementById("transcriptMessages");
    this.feedbackView = document.getElementById("feedbackView");

    this.capture = null;
    this.playback = null;
    this.socket = null;
    this.sessionId = null;
    this.muted = false;
    this.selectedRating = null;
    this.selectedResolved = null;

    this._bindStartButton();
    this._bindDrawerControls();
  }

  _bindStartButton() {
    const btn = document.getElementById("startCallBtn");
    btn.addEventListener("click", () => this.startCall());
  }

  _setStatus(text) {
    this.statusPill.textContent = text;
  }

  _renderActiveControls() {
    this.controlPill.innerHTML = "";

    const muteBtn = document.createElement("button");
    muteBtn.className = "control-btn";
    muteBtn.type = "button";
    muteBtn.textContent = "🎙️";
    muteBtn.id = "muteBtn";
    muteBtn.addEventListener("click", () => this.toggleMute());

    const drawerBtn = document.createElement("button");
    drawerBtn.className = "control-btn";
    drawerBtn.type = "button";
    drawerBtn.textContent = "💬";
    drawerBtn.addEventListener("click", () => this.toggleDrawer());

    const endBtn = document.createElement("button");
    endBtn.className = "control-btn end-call";
    endBtn.type = "button";
    endBtn.textContent = "⏹";
    endBtn.addEventListener("click", () => this.endCall());

    this.controlPill.appendChild(muteBtn);
    this.controlPill.appendChild(drawerBtn);
    this.controlPill.appendChild(endBtn);
  }

  _renderIdleControls() {
    this.controlPill.innerHTML = "";
    const btn = document.createElement("button");
    btn.className = "control-btn start-call";
    btn.id = "startCallBtn";
    btn.type = "button";
    btn.textContent = "Start Call";
    btn.addEventListener("click", () => this.startCall());
    this.controlPill.appendChild(btn);
  }

  toggleDrawer() {
    const open = this.drawer.dataset.open === "true";
    this.drawer.dataset.open = (!open).toString();
  }

  toggleMute() {
    this.muted = !this.muted;
    this.capture.setMuted(this.muted);
    const muteBtn = document.getElementById("muteBtn");
    muteBtn.dataset.active = this.muted.toString();
    this.orb.setState(this.muted ? "muted" : "listening");
    this._setStatus(this.muted ? "Muted" : "Listening");
  }

  appendTranscriptMessage(role, text) {
    const el = document.createElement("div");
    el.className = "transcript-message";
    const roleEl = document.createElement("span");
    roleEl.className = "role";
    roleEl.textContent = role;
    const textEl = document.createElement("span");
    textEl.textContent = text;
    el.appendChild(roleEl);
    el.appendChild(textEl);
    this.transcriptMessages.appendChild(el);
    this.transcriptMessages.scrollTop = this.transcriptMessages.scrollHeight;
  }

  async startCall() {
    this._setStatus("Connecting…");
    this.orb.setState("idle");

    this.capture = new AudioCapture();
    this.playback = new AudioPlayback();

    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.socket = new VoiceSocket(`${proto}://${location.host}/ws/voice`);

    this.socket.on("session_id", ({ sessionId }) => {
      this.sessionId = sessionId;
    });

    this.socket.on("audio_delta", ({ audio }) => {
      this.playback.enqueue(audio);
    });

    this.socket.on("bot_transcript", ({ transcript }) => {
      this.appendTranscriptMessage("Bot", transcript);
    });

    this.socket.on("user_transcript", ({ transcript }) => {
      this.appendTranscriptMessage("You", transcript);
    });

    this.socket.on("error", ({ error }) => {
      console.error("Voice socket error", error);
    });

    this.socket.on("close", () => {
      if (this.controlPill.querySelector(".end-call")) {
        this.endCall({ abrupt: true });
      }
    });

    try {
      await this.socket.connect();
    } catch (e) {
      this._setStatus("Connection failed");
      this._renderIdleControls();
      return;
    }

    try {
      await this.capture.start({
        onChunk: (base64) => this.socket.sendAudioChunk(base64),
        onLevel: (level) => {
          if (!this.muted) this.orb.setLevel(level);
        },
      });
    } catch (e) {
      this._setStatus("Microphone permission denied");
      this.socket.close();
      this._renderIdleControls();
      return;
    }

    this.playback.start();
    this.playback.onLevel((level) => {
      if (level > 0.02) {
        this.orb.setState("speaking");
        this.orb.setLevel(level);
      } else if (!this.muted) {
        this.orb.setState("listening");
      }
    });

    this._setStatus("Listening");
    this.orb.setState("listening");
    this._renderActiveControls();
  }

  endCall({ abrupt = false } = {}) {
    if (abrupt) this._setStatus("Call ended unexpectedly");

    if (this.capture) this.capture.stop();
    if (this.playback) this.playback.stop();
    if (this.socket) this.socket.close();

    this.orb.setState("idle");
    this.orb.setLevel(0);
    this._renderIdleControls();

    this.drawer.dataset.open = "true";
    this.transcriptView.style.display = "none";
    this.feedbackView.style.display = "block";
    this._setStatus(abrupt ? "Call ended unexpectedly" : "Call ended");
  }

  _bindDrawerControls() {
    document.getElementById("ratingRow").addEventListener("click", (e) => {
      const btn = e.target.closest(".rating-option");
      if (!btn) return;
      this.selectedRating = parseInt(btn.dataset.value, 10);
      [...document.querySelectorAll(".rating-option")].forEach((b) =>
        (b.dataset.selected = (b === btn).toString())
      );
    });

    document.getElementById("resolvedRow").addEventListener("click", (e) => {
      const btn = e.target.closest(".resolved-option");
      if (!btn) return;
      this.selectedResolved = btn.dataset.value === "true";
      [...document.querySelectorAll(".resolved-option")].forEach((b) =>
        (b.dataset.selected = (b === btn).toString())
      );
    });

    document.getElementById("submitFeedbackBtn").addEventListener("click", () => this.submitFeedback());
  }

  async submitFeedback() {
    if (!this.sessionId || this.selectedRating === null) return;

    await fetch("/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        session_id: this.sessionId,
        rating: this.selectedRating,
        resolved: this.selectedResolved,
        comment: document.getElementById("feedbackComment").value || null,
      }),
    });

    this.feedbackView.innerHTML = "<h3>Thanks for your feedback!</h3>";
    this.transcriptMessages.innerHTML = "";
    this.transcriptView.style.display = "block";
    this.feedbackView.style.display = "none";
    this.drawer.dataset.open = "false";
    this._setStatus("Idle");
    this.sessionId = null;
    this.selectedRating = null;
    this.selectedResolved = null;
  }
}

initTheme();
document.getElementById("themeToggle").addEventListener("click", toggleTheme);
new CallApp();
```

- [ ] **Step 3: Manual end-to-end verification**

This is the integration point for Tasks 3-8 — run it for real:

1. Ensure `OPENAI_API_KEY` is set in the environment (`.env` per `app/config.py`).
2. `python -m uvicorn app.main:app --reload`
3. Open `http://localhost:8000/call` in Chrome or Edge (both have solid `AudioWorklet` support).
4. Click "Start Call" — grant mic permission when prompted.
5. Confirm: status pill moves Connecting → Listening; orb glows/scales while you speak.
6. Speak a question the bot's knowledge base can answer; confirm you hear the bot's voice reply and the orb glows/scales while it's speaking.
7. Click the mute button; confirm the orb dims and no further `input_audio_buffer.append` frames are sent (check the Network tab's WS frames, or add a temporary `console.log` in `sendAudioChunk` and remove it after verifying).
8. Click the drawer toggle mid-call; confirm transcript messages (both your speech and the bot's replies) are visible, populated from the `user_transcript`/`bot_transcript` events.
9. Click End Call; confirm the drawer force-opens showing the rating/resolved/comment feedback form.
10. Select a rating, select Yes/No, optionally add a comment, click Submit; confirm a 200 response in the Network tab and the "Thanks for your feedback!" message appears, drawer closes, and the "Start Call" button reappears (idle state restored).
11. Toggle the theme button; confirm the whole page (background, orb glow color, drawer, control pill) switches between light and dark, and reload the page to confirm the choice persisted via `localStorage`.
12. Verify feedback landed server-side: `curl http://localhost:8000/api/sessions/<the session id shown you can find via /api/sessions>` and confirm a `feedback` event with the submitted rating/resolved/comment appears in its `events` array — or simpler, open `http://localhost:8000/dashboard`, find the just-ended session, and confirm the feedback event shows in its timeline.

If any step fails, fix the relevant module (Task 3-7 file) before proceeding — do not patch around it in `call-app.js` alone if the bug is actually in a lower-level module.

- [ ] **Step 4: Commit**

```bash
git add app/static/call/call-app.js
git commit -m "feat: wire up call-app controller for full /call page lifecycle"
```

---

## Task 9: Full backend regression check

**Files:** none (verification only)

- [ ] **Step 1: Run the full backend test suite**

Run: `python -m pytest tests/ -v`
Expected: all tests pass, including `tests/test_rag_sources.py` (pre-existing) and the three new test files from Tasks 1-3. (Scope to the `tests/` directory — `scripts/batch_test.py` and `scripts/voice_roundtrip_test.py` are pre-existing standalone scripts that import `requests`, which isn't a project dependency; pytest's default discovery picks them up and errors on collection if invoked bare as `pytest -v` from the repo root. This is a pre-existing condition unrelated to this plan's changes — do not fix it as part of this task.)

- [ ] **Step 2: Confirm no unintended route collisions**

Run: `python -c "from app.main import app; print(sorted(r.path for r in app.routes))"`
Expected output includes `/call` and `/feedback` alongside all pre-existing routes (`/`, `/dashboard`, `/api/sessions`, `/ws/voice`, etc.) — no duplicates, no accidental overwrite of an existing path.

- [ ] **Step 3: Commit (only if Steps 1-2 required a fix)**

If everything already passed, there's nothing to commit here — this task is a checkpoint, not a code change. If a fix was needed, commit it with a message describing what regression it addressed.

---

## Self-Review Notes

- **Spec coverage:** Page structure (Task 3), call lifecycle states (Task 8), orb visual states (Task 7 + CSS in Task 3), audio pipeline capture/playback (Tasks 4-5), error handling — mic denied / connect failure / server error / abrupt close (Task 8 `startCall`/`endCall`), `POST /feedback` (Task 1), `voicebot.session_id` event (Task 2), theming (Task 3 CSS + Task 8 theme toggle) — all covered. Out-of-scope items (DB persistence, auth, reconnect-mid-call, native app) intentionally have no task.
- **Type consistency:** `AudioCapture.start({onChunk, onLevel})`, `AudioPlayback.enqueue(base64)/onLevel(cb)`, `VoiceSocket.on(eventType, handler)` event names, and `OrbController.setState/setLevel` are used identically in Task 8 as defined in Tasks 4-7.
- **No placeholders:** every step has literal, complete code — no TBD/TODO markers.
