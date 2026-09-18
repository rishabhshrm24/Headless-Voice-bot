# Voice Call UI — Design Spec

Date: 2026-09-18

## Purpose

The voicebot currently has no end-user interface — only an internal admin
monitoring dashboard (`app/static/index.html`) that shows session activity
for operators. This spec adds a new, separate end-user page where a real
person can start a live voice call with the bot, see it as a talking orb,
mute/end the call, glance at a live transcript, and leave feedback when the
call ends.

## Scope

- New static page: `app/static/call.html`, served at `GET /call`
  (new route in `app/main.py`, alongside the existing UI routes).
- No changes to the existing admin dashboard or its route.
- One new backend endpoint: `POST /feedback`.
- One small, backward-compatible addition to `app/realtime_bridge.py`:
  emit a `voicebot.session_id` event right after connect so the client
  can associate its `/feedback` submission with the right session (see
  "Backend: `POST /feedback`" below). No other change to the voice
  WebSocket protocol — everything else in
  `docs/CLIENT_VOICE_INTEGRATION.md` still applies as-is.
- Plain HTML/CSS/vanilla JS, no build step, consistent with the existing
  static dashboard's approach (Tailwind via CDN is acceptable, same as
  `index.html`).

## Page structure

Single full-viewport view, no navigation chrome:

- Centered talking orb (the provided SVG, gradient `#FA8BFF → #2BD2FF →
  #2BFF88`), large, as the focal point.
- Status pill above/below the orb: `Connecting…` / `Listening` / `Speaking`
  / `Muted`.
- Floating control pill below the orb: Mute toggle button, End Call button
  (red/destructive), Chat-drawer toggle icon.
- Right-side drawer (slide-in, hidden by default): shows the live
  transcript while a call is active. Manually toggleable via the drawer
  icon during the call.
- Theme toggle (sun/moon icon, top-right corner): light/dark, defaults to
  `prefers-color-scheme`, overridable and persisted in `localStorage`.

## Call lifecycle

1. **Landing / idle** — orb at rest (slow breathing animation), a single
   "Start Call" control replaces the mute/end pill. Drawer hidden.
2. **Connecting** — on click, open `wss://<host>/ws/voice`, request mic
   permission, start capture. Orb shows a connecting pulse.
3. **Active call** — bidirectional audio streaming per the existing spec
   (`input_audio_buffer.append` outbound; handle
   `response.output_audio.delta`, `response.output_audio_transcript.done`,
   `conversation.item.input_audio_transcription.completed`, `error`
   inbound). Orb reacts to live audio amplitude (see Audio pipeline).
   Transcript events append to the drawer's message list whether or not
   the drawer is currently open.
4. **Mute** — stops sending `input_audio_buffer.append` frames (mic capture
   keeps running locally so the orb can still show local input level
   dimmed, but no audio is sent to the server). Button toggles to a
   filled/active state.
5. **End call** — user clicks End Call, or the socket closes/errors
   unrecoverably. Client closes the WebSocket (per spec, closing the
   socket ends the session — no explicit end message). Mic capture stops.
   Drawer force-opens and swaps from transcript view to the feedback view.
6. **Feedback** — shown once per call, in the drawer:
   - Quality rating: 5-option emoji/star scale (Awful → Great, following
     the Circle/Whereby pattern).
   - "Was your issue resolved?" Yes/No toggle.
   - Optional free-text comment (textarea).
   - Submit button → `POST /feedback`. On success, drawer shows a brief
     thank-you and a "Start New Call" control appears, returning the page
     to idle state. Skipping is allowed (a small "Skip" link/close on the
     drawer returns to idle without submitting).

## Orb visual states

CSS custom properties drive four states, all built on the base SVG:

| State | Behavior |
|---|---|
| Idle | Slow scale/opacity breathing loop (no audio involved) |
| Listening (user speaking, unmuted) | Orb scale/glow amplitude-driven from live mic input level |
| Speaking (bot audio playing) | Orb scale/glow amplitude-driven from decoded output audio level; gradient shifts slightly toward the cyan/green end |
| Muted | Desaturated/dimmed orb + small mic-off badge overlay |

## Audio pipeline

- **Capture**: `getUserMedia` → `AudioContext` → `AudioWorkletNode`
  (`audio-processor` worklet) that receives Float32 frames at the
  context's native sample rate, downsamples to 24000 Hz mono, converts to
  PCM16 (int16), base64-encodes, and posts chunks (~100ms) to the main
  thread, which sends them as `input_audio_buffer.append` frames.
- **Amplitude for orb (listening)**: an `AnalyserNode` tapped off the mic
  stream feeds an RMS/peak level read on an animation frame loop.
- **Playback**: inbound `response.output_audio.delta` base64 PCM16 chunks
  are decoded and pushed into a playback queue, scheduled back-to-back on
  a dedicated `AudioContext` (24000 Hz) using `AudioBufferSourceNode`
  chaining, so audio is gapless even though chunks arrive incrementally.
- **Amplitude for orb (speaking)**: computed directly from each decoded
  PCM16 chunk's RMS as it's scheduled for playback (no need for a second
  `AnalyserNode` since we already have the raw samples).
- Fallback: if `AudioWorklet` isn't available (very old browsers), this is
  out of scope — no `ScriptProcessorNode` fallback, since target browsers
  for this feature are current Chrome/Edge/Safari/Firefox.

## Error handling

- Mic permission denied → show an inline message in place of the orb
  controls; don't attempt to connect.
- WebSocket connect failure / timeout → status pill shows "Connection
  failed", offer a Retry control, return to idle.
- `error` event from the server mid-call → log to console, show a
  transient toast/status message; keep the call open unless the socket
  itself closes (server decides whether the error is fatal).
- Unexpected socket close mid-call → treat the same as user-initiated end
  call (proceed to feedback view) but note the abrupt end in the status
  pill briefly ("Call ended unexpectedly") before showing feedback.

## Backend: `POST /feedback`

New endpoint in `app/main.py`, alongside the existing voice/session
endpoints.

Request body:
```json
{
  "session_id": "ws_abcd1234",
  "rating": 4,
  "resolved": true,
  "comment": "optional free text"
}
```

- `session_id` — the id `run_voice_bridge` already generates server-side
  for the `/ws/voice` connection (`tracker.create_session(...)`). The
  client currently has no way to learn this id, since only OpenAI-schema
  events are relayed today. Small addition to `app/realtime_bridge.py`:
  immediately after `tracker.register_voice_socket(...)`, send one extra
  JSON frame the client doesn't have to send anything to trigger:
  `{"type": "voicebot.session_id", "session_id": "<id>"}`. This is a new
  event type, not a replacement of any existing one — per
  `docs/CLIENT_VOICE_INTEGRATION.md` ("ignore/pass through any other
  event types you don't need"), this is backward compatible with other
  existing clients (e.g. `scripts/live_voice_client.py`), which already
  ignore unrecognized event types. The call UI's client stores this id
  on receipt and includes it in the `/feedback` POST.
- `rating` — integer 1–5.
- `resolved` — boolean.
- `comment` — optional string.

Behavior: record the feedback via `tracker.record_event(session_id,
"feedback", "User feedback submitted", {...})` so it surfaces in the
existing admin monitor dashboard/timeline for that session — no new
storage layer needed, consistent with how the rest of session activity is
already tracked. Return `{"status": "ok"}`. No auth (matches the rest of
the API).

## Theming

- CSS custom properties (`--bg`, `--surface`, `--text`, `--orb-glow`,
  etc.) defined on `:root` for light, overridden under
  `@media (prefers-color-scheme: dark)` and by a `data-theme="dark"`
  attribute set via the toggle button. Persist the explicit choice in
  `localStorage`; absent a stored choice, follow system preference.
- Visual direction: modern SaaS/glassmorphism — blurred translucent
  surfaces for the control pill and drawer, soft shadows, the orb as the
  single saturated color accent against an otherwise neutral background
  in both themes.

## Out of scope

- Persisting feedback to a database (log-only via session_tracker, same
  durability tier as all other session data today).
- User accounts/auth.
- Reconnect-mid-call logic beyond a manual Retry from idle.
- Mobile app / native wrapper — this is a responsive web page only.
