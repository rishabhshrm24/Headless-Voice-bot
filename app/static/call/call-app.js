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
    if (this.state !== "idle") return;

    this._resetDrawer();
    this.state = "connecting";
    this._setStatus("Connecting…");
    this.orb.setState("idle");

    this.capture = new AudioCapture();
    this.playback = new AudioPlayback();

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
      if (e && e.name === "NotAllowedError") {
        this._setStatus("Microphone permission denied");
      } else {
        this._setStatus("Could not access microphone");
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
      if (this.socket === socket && (this.state === "connecting" || this.state === "active")) {
        this.endCall({ abrupt: true });
      }
    });

    try {
      await this.socket.connect();
    } catch (e) {
      this.state = "idle";
      this._setStatus("Connection failed");
      this.capture.stop();
      this._renderIdleControls();
      return;
    }

    this.playback.start();
    this.playback.onLevel((level) => {
      if (this.muted) {
        // Keep the orb/status pinned to "muted" instead of letting bot
        // playback level drag it back into a speaking/listening state.
        this.orb.setState("muted");
        this.orb.setLevel(0);
        return;
      }
      if (level > 0.02) {
        this.orb.setState("speaking");
        this.orb.setLevel(level);
      } else {
        this.orb.setState("listening");
      }
    });

    this.state = "active";
    this._setStatus("Listening");
    this.orb.setState("listening");
    this._renderActiveControls();
  }

  endCall({ abrupt = false } = {}) {
    if (abrupt) this._setStatus("Call ended unexpectedly");

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
    this._setStatus(abrupt ? "Call ended unexpectedly" : "Call ended");
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
    this._setStatus("Idle");
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
      this._setStatus("Idle");
    }, 1500);
  }
}

initTheme();
document.getElementById("themeToggle").addEventListener("click", toggleTheme);
new CallApp();
