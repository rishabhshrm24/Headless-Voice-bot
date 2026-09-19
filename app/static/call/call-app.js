const THEME_KEY = "voicebot_theme";

const SVG_OPEN =
  '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">';
const PHONE_PATH =
  '<path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6 19.8 19.8 0 0 1-3.1-8.7A2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z"/>';
const ICONS = {
  mic: `${SVG_OPEN}<rect x="9" y="3" width="6" height="11" rx="3"/><path d="M5 11a7 7 0 0 0 14 0"/><path d="M12 18v3"/></svg>`,
  micOff: `${SVG_OPEN}<path d="M9 9v2a3 3 0 0 0 5.1 2.1M15 9.3V6a3 3 0 0 0-5.9-.8"/><path d="M5 11a7 7 0 0 0 11.2 5.6M19 11a7 7 0 0 1-.6 2.8"/><path d="M12 18v3"/><path d="M3 3l18 18"/></svg>`,
  chat: `${SVG_OPEN}<path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>`,
  phone: `${SVG_OPEN}${PHONE_PATH}</svg>`,
  phoneDown: `${SVG_OPEN}<g transform="rotate(135 12 12)">${PHONE_PATH}</g></svg>`,
  sun: `${SVG_OPEN}<circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>`,
  moon: `${SVG_OPEN}<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>`,
};

// How long the bot's audio must stay quiet before we leave the "Speaking"
// state - long enough to bridge natural pauses between words/sentences.
const SPEAK_HOLD_MS = 900;

function formatDuration(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  const mm = String(m).padStart(2, "0");
  const ss = String(sec).padStart(2, "0");
  return h > 0 ? `${h}:${mm}:${ss}` : `${mm}:${ss}`;
}

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
  btn.innerHTML = isDark ? ICONS.sun : ICONS.moon;
  btn.setAttribute("aria-label", isDark ? "Switch to light theme" : "Switch to dark theme");
}

function toggleTheme() {
  const current = document.documentElement.dataset.theme ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem(THEME_KEY, next);
  updateThemeIcon();
}

import AudioCapture from "./audio-capture.js";
import AudioPlayback from "./audio-playback.js";
import VoiceSocket from "./voice-socket.js";
import OrbController from "./orb.js";

class CallApp {
  constructor() {
    this.orb = new OrbController(document.getElementById("orbWrap"));
    this.statusPill = document.getElementById("statusPill");
    this.statusText = document.getElementById("statusText");
    this.statusTimer = document.getElementById("statusTimer");
    this.caption = document.getElementById("caption");
    this.captionRole = document.getElementById("captionRole");
    this.captionText = document.getElementById("captionText");
    this.controlPill = document.getElementById("controlPill");
    this.drawer = document.getElementById("drawer");
    this.transcriptView = document.getElementById("transcriptView");
    this.transcriptMessages = document.getElementById("transcriptMessages");
    this.feedbackView = document.getElementById("feedbackView");

    // Wrap the feedback form's existing children (rating row, resolved row,
    // comment box, submit button, etc.) in a dedicated container so the form
    // can be hidden/shown as a unit without ever destroying its DOM nodes.
    // This keeps the listeners bound in _bindDrawerControls() valid across
    // every call, not just the first one.
    this.feedbackForm = document.createElement("div");
    this.feedbackForm.className = "feedback-form";
    while (this.feedbackView.firstChild) {
      this.feedbackForm.appendChild(this.feedbackView.firstChild);
    }

    this.feedbackThanks = document.createElement("h3");
    this.feedbackThanks.textContent = "Thanks for your feedback!";
    this.feedbackThanks.style.display = "none";

    this.feedbackError = document.createElement("p");
    this.feedbackError.className = "feedback-error";
    this.feedbackError.style.display = "none";
    this.feedbackForm.appendChild(this.feedbackError);

    this.feedbackView.appendChild(this.feedbackForm);
    this.feedbackView.appendChild(this.feedbackThanks);

    this.capture = null;
    this.playback = null;
    this.socket = null;
    this.sessionId = null;
    this.muted = false;
    this.selectedRating = null;
    this.selectedResolved = null;
    this.state = "idle"; // "idle" | "connecting" | "active" | "ended"
    this.timerId = null;
    this.callStartedAt = 0;
    this.speaking = false;
    this._lastSpokeAt = -Infinity;
    this._interruptedItems = new Set();

    this._renderIdleControls();
    this._bindDrawerControls();
  }

  // tone drives the status dot color / pulse ("idle" | "connecting" |
  // "listening" | "speaking" | "muted" | "error" | "ended"); animate shows
  // the trailing "..." dots for in-progress states.
  _setStatus(text, tone = "idle", animate = false) {
    this.statusText.textContent = text;
    this.statusPill.dataset.tone = tone;
    this.statusPill.dataset.animate = animate ? "true" : "false";
  }

  _startTimer() {
    this._stopTimer();
    this.callStartedAt = Date.now();
    const tick = () => {
      this.statusTimer.textContent = formatDuration((Date.now() - this.callStartedAt) / 1000);
    };
    tick();
    this.timerId = setInterval(tick, 1000);
  }

  // Stops the ticking but leaves the final duration visible (e.g. next to
  // "Call ended") until the next call resets it.
  _stopTimer() {
    if (this.timerId) {
      clearInterval(this.timerId);
      this.timerId = null;
    }
  }

  _clearTimer() {
    this._stopTimer();
    this.statusTimer.textContent = "";
  }

  // Shows the latest line on the main stage under the orb. role is "You" /
  // "Voicebot" for spoken lines, or empty for a plain hint.
  _setCaption(text, role = "") {
    this.captionRole.textContent = role;
    this.captionText.textContent = text;
    this.caption.dataset.empty = role ? "false" : "true";
    this.caption.classList.remove("caption--in");
    void this.caption.offsetWidth; // restart the fade-in animation
    this.caption.classList.add("caption--in");
  }

  _makeButton({ className = "control-btn", icon, label, text, id }) {
    const btn = document.createElement("button");
    btn.className = className;
    btn.type = "button";
    if (id) btn.id = id;
    btn.setAttribute("aria-label", label);
    btn.title = label;
    btn.innerHTML = icon;
    if (text) {
      const span = document.createElement("span");
      span.className = "control-label";
      span.textContent = text;
      btn.appendChild(span);
    }
    return btn;
  }

  _renderActiveControls() {
    this.controlPill.innerHTML = "";
    this.controlPill.classList.remove("control-pill--idle");

    const muteBtn = this._makeButton({
      className: "control-btn control-btn--labeled",
      icon: ICONS.mic,
      label: "Mute microphone",
      text: "Mute",
      id: "muteBtn",
    });
    muteBtn.addEventListener("click", () => this.toggleMute());

    const drawerBtn = this._makeButton({ icon: ICONS.chat, label: "Show transcript" });
    drawerBtn.addEventListener("click", () => this.toggleDrawer());

    const endBtn = this._makeButton({
      className: "control-btn end-call",
      icon: ICONS.phoneDown,
      label: "End call",
    });
    endBtn.addEventListener("click", () => this.endCall());

    this.controlPill.appendChild(muteBtn);
    this.controlPill.appendChild(drawerBtn);
    this.controlPill.appendChild(endBtn);
  }

  _renderIdleControls() {
    this.controlPill.innerHTML = "";
    this.controlPill.classList.add("control-pill--idle");
    const btn = this._makeButton({
      className: "control-btn start-call",
      icon: `<span class="btn-icon">${ICONS.phone}</span>`,
      label: "Start call",
      text: "Start call",
      id: "startCallBtn",
    });
    btn.addEventListener("click", () => this.startCall());
    this.controlPill.appendChild(btn);
  }

  // The caller started talking while the bot was still speaking: cut the
  // bot off right away. The server's VAD cancels/ignores the rest of the
  // reply on its side, but the audio already queued in the browser would
  // otherwise play to the end - so stop it here, and tell the server how
  // much was actually heard so the conversation history matches.
  _handleBargeIn() {
    if (this.state !== "active" || !this.playback) return;

    const info = this.playback.flush();
    if (!info) return; // bot wasn't speaking; this is just the caller talking

    if (info.itemId) {
      this._interruptedItems.add(info.itemId);
      if (this.socket) {
        this.socket.sendEvent({
          type: "conversation.item.truncate",
          item_id: info.itemId,
          content_index: 0,
          audio_end_ms: info.playedMs,
        });
      }
    }

    // Drop out of "Speaking" immediately instead of waiting out the hold.
    this._lastSpokeAt = -Infinity;
    if (!this.muted) {
      this.speaking = false;
      this.orb.setState("listening");
      this._setStatus("Listening", "listening", true);
    }
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
    muteBtn.innerHTML = this.muted ? ICONS.micOff : ICONS.mic;
    const label = document.createElement("span");
    label.className = "control-label";
    label.textContent = this.muted ? "Muted" : "Mute";
    muteBtn.appendChild(label);
    const title = this.muted ? "Unmute microphone" : "Mute microphone";
    muteBtn.setAttribute("aria-label", title);
    muteBtn.title = title;
    this.orb.setState(this.muted ? "muted" : "listening");
    this.speaking = false;
    if (this.muted) {
      this._setStatus("Muted", "muted");
    } else {
      this._setStatus("Listening", "listening", true);
    }
  }

  appendTranscriptMessage(role, text) {
    this._setCaption(text, role === "Bot" ? "Voicebot" : role);
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
    if (this.state !== "idle") return;

    this._resetDrawer();
    this.state = "connecting";
    this._clearTimer();
    this.speaking = false;
    this._lastSpokeAt = -Infinity;
    this._interruptedItems = new Set();
    this.muted = false;
    this._setStatus("Connecting", "connecting", true);
    this._setCaption("Getting things ready…");
    this.orb.setState("idle");

    this.capture = new AudioCapture();
    this.playback = new AudioPlayback();
    // Create (and attempt to resume) the playback AudioContext synchronously
    // here, still inside the click handler's call stack, rather than after
    // the mic-permission-prompt/socket-connect awaits below — browsers are
    // far more willing to let an AudioContext start "running" (instead of
    // "suspended", which produces no sound) when it's created close to a
    // real user gesture like this button click.
    this.playback.start();

    // Request the microphone before opening the socket: if the user denies
    // (or capture otherwise fails), we never burn a server session / upstream
    // OpenAI Realtime connection.
    try {
      await this.capture.start({
        onChunk: (base64) => {
          if (this.socket) this.socket.sendAudioChunk(base64);
        },
        onLevel: (level) => {
          if (!this.muted) this.orb.setLevel(level);
        },
      });
    } catch (e) {
      this.state = "idle";
      if (this.playback) this.playback.stop();
      if (e && e.name === "NotAllowedError") {
        this._setStatus("Microphone permission denied", "error");
        this._setCaption("Allow microphone access in your browser, then try again.");
      } else {
        this._setStatus("Could not access microphone", "error");
        this._setCaption("Check that a microphone is connected, then try again.");
      }
      this._renderIdleControls();
      return;
    }

    const proto = location.protocol === "https:" ? "wss" : "ws";
    this.socket = new VoiceSocket(`${proto}://${location.host}/ws/voice`);
    const socket = this.socket;

    this.socket.on("session_id", ({ sessionId }) => {
      this.sessionId = sessionId;
    });

    this.socket.on("audio_delta", ({ audio, itemId }) => {
      // Audio still in flight from a reply the caller already cut off.
      if (itemId && this._interruptedItems.has(itemId)) return;
      this.playback.enqueue(audio, itemId);
    });

    this.socket.on("speech_started", () => this._handleBargeIn());

    this.socket.on("bot_transcript", ({ transcript, itemId }) => {
      const cutOff = itemId && this._interruptedItems.has(itemId);
      this.appendTranscriptMessage("Bot", cutOff ? `${transcript} (interrupted)` : transcript);
    });

    this.socket.on("user_transcript", ({ transcript }) => {
      this.appendTranscriptMessage("You", transcript);
    });

    this.socket.on("error", ({ error }) => {
      console.error("Voice socket error", error);
    });

    this.socket.on("close", () => {
      if (this.socket === socket && (this.state === "connecting" || this.state === "active")) {
        this.endCall({ abrupt: true });
      }
    });

    try {
      await this.socket.connect();
    } catch (e) {
      this.state = "idle";
      this._setStatus("Connection failed", "error");
      this._setCaption("Couldn't reach the assistant. Please try again.");
      this.capture.stop();
      if (this.playback) this.playback.stop();
      this._renderIdleControls();
      return;
    }

    this.playback.onLevel((level) => {
      if (this.muted) {
        // Keep the orb/status pinned to "muted" instead of letting bot
        // playback level drag it back into a speaking/listening state.
        this.orb.setState("muted");
        this.orb.setLevel(0);
        return;
      }
      // Playback level drops to 0 in the tiny gaps between chunks of speech,
      // which used to flip the pill/orb between "Speaking" and "Listening"
      // several times a second. Enter "speaking" immediately, but only leave
      // it after SPEAK_HOLD_MS of continuous quiet.
      const now = performance.now();
      if (level > 0.02) this._lastSpokeAt = now;
      const stillSpeaking = now - this._lastSpokeAt < SPEAK_HOLD_MS;

      if (stillSpeaking) {
        this.orb.setState("speaking");
        if (level > 0.02) this.orb.setLevel(level);
        if (!this.speaking) {
          this.speaking = true;
          this._setStatus("Speaking", "speaking");
        }
      } else {
        this.orb.setState("listening");
        if (this.speaking) {
          this.speaking = false;
          this._setStatus("Listening", "listening", true);
        }
      }
    });

    this.state = "active";
    this._setStatus("Listening", "listening", true);
    this._setCaption("Go ahead, I'm listening.");
    this._startTimer();
    this.orb.setState("listening");
    this._renderActiveControls();
  }

  endCall({ abrupt = false } = {}) {
    this._stopTimer();
    this.speaking = false;

    if (this.capture) this.capture.stop();
    if (this.playback) this.playback.stop();
    if (this.socket) this.socket.close();

    this.state = "ended";
    this.orb.setState("idle");
    this.orb.setLevel(0);
    this._renderIdleControls();

    this.drawer.dataset.open = "true";
    this.transcriptView.style.display = "none";
    this.feedbackView.style.display = "block";
    this.feedbackForm.style.display = "block";
    this.feedbackThanks.style.display = "none";
    this.feedbackError.style.display = "none";
    this._setStatus(abrupt ? "Call ended unexpectedly" : "Call ended", abrupt ? "error" : "ended");
    this._setCaption("How did it go? Leave quick feedback on the right.");
  }

  // Returns the drawer/feedback UI to a clean baseline: transcript view
  // showing, feedback view hidden, and any stale rating/resolved selections
  // or comment text cleared. Called at the top of startCall() (so a new call
  // never inherits a previous call's feedback state) and by skipFeedback().
  _resetDrawer() {
    this.drawer.dataset.open = "false";
    this.transcriptView.style.display = "block";
    this.feedbackView.style.display = "none";
    this.feedbackForm.style.display = "block";
    this.feedbackThanks.style.display = "none";
    this.feedbackError.style.display = "none";

    this.selectedRating = null;
    this.selectedResolved = null;
    [...document.querySelectorAll(".rating-option")].forEach((b) => (b.dataset.selected = "false"));
    [...document.querySelectorAll(".resolved-option")].forEach((b) => (b.dataset.selected = "false"));
    const commentEl = document.getElementById("feedbackComment");
    if (commentEl) commentEl.value = "";
  }

  skipFeedback() {
    this._resetDrawer();
    this.sessionId = null;
    this.state = "idle";
    this._clearTimer();
    this._setStatus("Idle");
    this._setCaption("Tap Start call to talk to the assistant");
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

    const skipBtn = document.getElementById("skipFeedbackBtn");
    if (skipBtn) skipBtn.addEventListener("click", () => this.skipFeedback());
  }

  async submitFeedback() {
    if (!this.sessionId || this.selectedRating === null || this.selectedResolved === null) return;

    this.feedbackError.style.display = "none";

    let response;
    try {
      response = await fetch("/feedback", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          session_id: this.sessionId,
          rating: this.selectedRating,
          resolved: this.selectedResolved,
          comment: document.getElementById("feedbackComment").value || null,
        }),
      });
    } catch (e) {
      this.feedbackError.textContent = "Couldn't send feedback. Please try again.";
      this.feedbackError.style.display = "block";
      return;
    }

    if (!response.ok) {
      this.feedbackError.textContent = "Couldn't send feedback. Please try again.";
      this.feedbackError.style.display = "block";
      return;
    }

    this.feedbackForm.style.display = "none";
    this.feedbackThanks.style.display = "block";
    this.transcriptMessages.innerHTML = "";
    this.sessionId = null;
    this.state = "idle";

    // Keep the drawer open showing the thank-you message briefly so the user
    // actually sees it, then reset everything (form selections, comment
    // text, transcript/feedback view visibility) and close the drawer.
    setTimeout(() => {
      this._resetDrawer();
      this._clearTimer();
      this._setStatus("Idle");
      this._setCaption("Tap Start call to talk to the assistant");
    }, 1500);
  }
}

initTheme();
document.getElementById("themeToggle").addEventListener("click", toggleTheme);
new CallApp();
