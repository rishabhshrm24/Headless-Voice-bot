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
