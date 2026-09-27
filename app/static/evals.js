(() => {
  "use strict";

  const CRITERIA = ["accuracy", "groundedness", "helpfulness", "conciseness"];
  const FLAG_LABELS = {
    error: "Error", no_reply: "No reply", slow: "Slow", fallback: "Didn't know",
    no_kb_hit: "No KB match", unhappy_caller: "Unhappy caller",
  };
  const TYPE_LABELS = {
    websocket_voice: "Live voice", http_voice: "Voice", text_chat: "Text chat",
    batch_chat: "Batch", openai_compat: "API",
  };

  const $ = (id) => document.getElementById(id);
  let rows = [];
  let openId = null;

  // ---- helpers (all dynamic text goes through textContent) ----
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const c of children.flat()) {
      if (c == null || c === false) continue;
      el.append(c.nodeType ? c : document.createTextNode(String(c)));
    }
    return el;
  }
  const ms = (v) => (v == null ? "–" : v >= 1000 ? (v / 1000).toFixed(1) + " s" : Math.round(v) + " ms");
  const pct = (v) => (v == null ? "–" : Math.round(v * 100) + "%");
  const num = (v, d = 1) => (v == null ? "–" : Number(v).toFixed(d));
  const dur = (s) => (s == null ? "–" : s >= 60 ? Math.floor(s / 60) + "m " + Math.round(s % 60) + "s" : Math.round(s) + "s");
  function when(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  function badge(text, tone) { return h("span", { class: "badge " + (tone || "") }, text); }

  let toastTimer;
  function toast(msg, isErr) {
    document.querySelectorAll(".toast").forEach((t) => t.remove());
    const t = h("div", { class: "toast" + (isErr ? " err" : ""), role: "status" }, msg);
    document.body.append(t);
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => t.remove(), 3200);
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    if (res.status === 401) { location.href = "/login"; throw new Error("Signed out"); }
    if (!res.ok) {
      let detail = "Request failed";
      try { detail = (await res.json()).detail || detail; } catch (_) { /* keep default */ }
      throw new Error(typeof detail === "string" ? detail : "Request failed");
    }
    return res.json();
  }

  // ---- derived state ----
  function needsAttention(r) {
    return r.metrics.flags.length > 0 || (r.judge && r.judge.verdict === "fail") || (r.review && r.review.verdict === "bad");
  }
  function judgeable(r) {
    return !r.judge && r.status !== "active" && r.metrics.turn_count > 0;
  }

  // ---- summary ----
  function renderSummary(s) {
    const kpis = [
      ["Sessions", s.sessions, null, s.judged + " scored · " + s.reviewed + " reviewed"],
      ["Pass rate", pct(s.pass_rate), null, s.judged ? "of " + s.judged + " scored" : "Score sessions to see this"],
      ["Avg quality", num(s.avg_overall), "/ 5", "LLM judge"],
      ["Median response", ms(s.p50_latency_ms), null, "p95 " + ms(s.p95_latency_ms)],
      ["Caller rating", num(s.avg_rating), "/ 5", s.resolved_rate == null ? "No feedback yet" : pct(s.resolved_rate) + " resolved"],
      ["Error rate", pct(s.error_rate), null, "Didn't know: " + pct(s.fallback_rate)],
    ];
    $("kpis").replaceChildren(...kpis.map(([label, value, unit, hint]) =>
      h("div", { class: "card kpi" },
        h("div", { class: "label" }, label),
        h("div", { class: "value" }, value, unit && h("small", null, " " + unit)),
        h("div", { class: "hint" }, hint))));

    $("criteria").replaceChildren(...(s.judged
      ? CRITERIA.map((c) => h("div", { class: "criterion" },
          h("span", { class: "name" }, c),
          h("div", { class: "track" }, h("div", { class: "fill", style: "width:" + ((s.criteria[c] || 0) / 5) * 100 + "%" })),
          h("span", { class: "num" }, num(s.criteria[c]))))
      : [h("div", { class: "empty-note" }, "No scored sessions yet. Use “Evaluate unscored”.")]));
  }

  function renderLatency() {
    const pts = rows.filter((r) => r.metrics.avg_latency_ms != null).slice(0, 40).reverse();
    if (!pts.length) { $("latency").replaceChildren(h("div", { class: "empty-note" }, "No timed replies yet.")); return; }
    const max = Math.max(...pts.map((r) => r.metrics.avg_latency_ms), 1);
    $("latency").replaceChildren(
      h("div", { class: "chart", role: "img", "aria-label": "Average response time per session" },
        pts.map((r) => h("div", {
          class: "bar" + (r.metrics.avg_latency_ms > 5000 ? " slow" : ""),
          style: "height:" + Math.max(4, (r.metrics.avg_latency_ms / max) * 100) + "%",
          title: ms(r.metrics.avg_latency_ms) + " · " + when(r.started_iso),
        }))),
      h("div", { class: "chart-foot" }, h("span", null, "Older"), h("span", null, "Peak " + ms(max) + " · orange = over 5 s"), h("span", null, "Newer")));
  }

  // ---- table ----
  function visibleRows() {
    const q = $("search").value.trim().toLowerCase();
    const f = $("filter").value;
    return rows.filter((r) => {
      if (q && !((r.summary || "") + " " + r.id).toLowerCase().includes(q)) return false;
      switch (f) {
        case "attention": return needsAttention(r);
        case "unjudged": return !r.judge;
        case "fail": return r.judge && r.judge.verdict === "fail";
        case "pass": return r.judge && r.judge.verdict === "pass";
        case "unreviewed": return !r.review;
        default: return true;
      }
    });
  }

  function verdictBadge(r) {
    if (!r.judge) return badge("Not scored");
    return badge(r.judge.verdict === "pass" ? "Pass" : "Fail", r.judge.verdict === "pass" ? "good" : "bad");
  }

  function renderTable() {
    const list = visibleRows();
    if (!rows.length) {
      $("tableWrap").replaceChildren(h("div", { class: "state" }, h("strong", null, "No sessions yet"), "Place a call or send a chat and it will appear here."));
      return;
    }
    if (!list.length) {
      $("tableWrap").replaceChildren(h("div", { class: "state" }, h("strong", null, "No sessions match"), "Try a different search or filter."));
      return;
    }
    const head = h("thead", null, h("tr", null,
      ["Session", "Turns", "Response", "Quality", "Verdict", "Flags", "Review"].map((t) => h("th", { scope: "col" }, t))));
    const body = h("tbody", null, list.map((r) => {
      const open = () => openDrawer(r.id);
      return h("tr", {
        tabindex: "0", onclick: open,
        onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } },
      },
        h("td", null,
          h("div", { class: "session-title" }, r.summary || "(no question)"),
          h("div", { class: "session-meta" }, (TYPE_LABELS[r.session_type] || r.session_type) + " · " + when(r.started_iso) + " · " + dur(r.duration_seconds))),
        h("td", { class: "num-cell" }, r.metrics.turn_count),
        h("td", { class: "num-cell" }, ms(r.metrics.avg_latency_ms)),
        h("td", { class: "num-cell" }, r.judge ? num(r.judge.overall) + " / 5" : "–"),
        h("td", null, verdictBadge(r)),
        h("td", null, h("div", { class: "flags" }, r.metrics.flags.map((f) => badge(FLAG_LABELS[f] || f, f === "error" || f === "no_reply" || f === "unhappy_caller" ? "bad" : "warn")))),
        h("td", null, r.review ? badge(r.review.verdict === "good" ? "Good" : "Bad", r.review.verdict === "good" ? "good" : "bad") : badge("Pending")));
    }));
    $("tableWrap").replaceChildren(h("table", null, head, body));
  }

  // ---- drawer ----
  function closeDrawer() {
    openId = null;
    if (location.hash) history.replaceState(null, "", location.pathname);
    $("drawer").classList.remove("open");
    $("drawer").setAttribute("aria-hidden", "true");
    $("scrim").classList.remove("open");
  }

  async function openDrawer(id) {
    openId = id;
    history.replaceState(null, "", "#" + encodeURIComponent(id));
    $("drawer").classList.add("open");
    $("drawer").setAttribute("aria-hidden", "false");
    $("scrim").classList.add("open");
    $("drawer").replaceChildren(h("div", { class: "state" }, "Loading…"));
    try {
      const detail = await api("/api/evals/" + encodeURIComponent(id));
      if (openId === id) renderDrawer(detail);
    } catch (e) {
      $("drawer").replaceChildren(h("div", { class: "state" }, h("strong", null, "Couldn't load session"), e.message));
    }
  }

  function criteriaBars(j) {
    // A stored judge record can be missing a criterion or have it as null (older schema,
    // partial write) - guard against NaN widths and "null"/"undefined" showing in the UI.
    return CRITERIA.map((c) => {
      const v = typeof j[c] === "number" && !Number.isNaN(j[c]) ? j[c] : null;
      return h("div", { class: "criterion" },
        h("span", { class: "name" }, c),
        h("div", { class: "track" }, h("div", { class: "fill", style: "width:" + ((v || 0) / 5) * 100 + "%" })),
        h("span", { class: "num" }, v == null ? "–" : v));
    });
  }

  function renderDrawer(d) {
    const m = d.metrics;
    let verdict = d.review ? d.review.verdict : null;
    const note = h("textarea", { class: "field", placeholder: "Optional note for the team", maxlength: "2000", "aria-label": "Review note" });
    note.value = (d.review && d.review.note) || "";
    const saveBtn = h("button", { class: "btn primary small", type: "button", disabled: !verdict }, "Save review");
    const segBtn = (v, label) => {
      const b = h("button", { type: "button", class: v, "aria-pressed": String(verdict === v) }, label);
      b.addEventListener("click", () => {
        verdict = v;
        seg.querySelectorAll("button").forEach((x) => x.setAttribute("aria-pressed", String(x === b)));
        saveBtn.disabled = false;
      });
      return b;
    };
    const seg = h("div", { class: "seg", role: "group", "aria-label": "Reviewer verdict" }, segBtn("good", "Good"), segBtn("bad", "Needs work"));

    saveBtn.addEventListener("click", async () => {
      saveBtn.disabled = true;
      try {
        await api("/api/evals/" + encodeURIComponent(d.id) + "/review", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ verdict, note: note.value.trim() || null }),
        });
        toast("Review saved");
        await load();
      } catch (e) { toast(e.message, true); saveBtn.disabled = false; }
    });

    const judgeBtn = h("button", { class: "btn small", type: "button", disabled: d.metrics.turn_count === 0 }, d.judge ? "Re-run judge" : "Run judge");
    judgeBtn.addEventListener("click", async () => {
      judgeBtn.disabled = true; judgeBtn.textContent = "Scoring…";
      try {
        await api("/api/evals/" + encodeURIComponent(d.id) + "/judge", { method: "POST" });
        toast("Session scored");
        await load();
        if (openId === d.id) openDrawer(d.id);
      } catch (e) { toast(e.message, true); judgeBtn.disabled = false; judgeBtn.textContent = "Run judge"; }
    });

    const turns = d.turns.length
      ? d.turns.map((t) => h("div", { class: "turn" },
          h("div", { class: "bubble user" + (t.user ? "" : " empty") }, t.user || "(no caller speech)"),
          h("div", { class: "bubble bot" + (t.assistant ? "" : " empty") }, t.assistant || "(no reply)"),
          h("div", { class: "turn-meta" },
            t.latency_ms != null && badge(ms(t.latency_ms), t.latency_ms > 5000 ? "warn" : ""),
            badge(t.sources.length ? t.sources.length + " KB chunks" : "No KB chunks", t.sources.length ? "info" : "")),
          t.sources.length > 0 && h("details", { class: "src" },
            h("summary", null, "Sources used"),
            t.sources.map((s) => h("blockquote", null, h("strong", null, s.source || "source"), ": ", (s.text || "").slice(0, 400))))))
      : [h("div", { class: "empty-note" }, "This session has no conversation turns.")];

    $("drawer").replaceChildren(
      h("div", { class: "drawer-head" },
        h("div", null,
          h("h2", null, d.summary || "Session"),
          h("div", { class: "session-meta mono" }, d.id + " · " + (TYPE_LABELS[d.session_type] || d.session_type) + " · " + when(d.started_iso))),
        h("button", { class: "icon-btn", type: "button", "aria-label": "Close", onclick: closeDrawer }, "×")),
      h("div", { class: "drawer-body" },
        h("div", { class: "card mini" },
          h("div", null, h("div", { class: "label" }, "Avg response"), h("div", { class: "value" }, ms(m.avg_latency_ms))),
          h("div", null, h("div", { class: "label" }, "Duration"), h("div", { class: "value" }, dur(d.duration_seconds))),
          h("div", null, h("div", { class: "label" }, "Caller rating"), h("div", { class: "value" }, m.feedback ? m.feedback.rating + " / 5" : "–"))),
        m.flags.length > 0 && h("div", { class: "flags" }, m.flags.map((f) => badge(FLAG_LABELS[f] || f, "warn"))),
        h("div", { class: "card panel" },
          h("div", { class: "review-foot" }, h("h3", { class: "section-title", style: "margin:0" }, "Judge"), h("span", { class: "spacer" }), d.judge && verdictBadge(d), judgeBtn),
          d.judge
            ? [h("div", { style: "margin-top:12px" }, criteriaBars(d.judge)), h("p", { class: "rationale" }, d.judge.rationale)]
            : h("p", { class: "rationale" }, "Not scored yet.")),
        m.feedback && m.feedback.comment && h("div", { class: "card panel" }, h("h3", { class: "section-title" }, "Caller comment"), h("div", null, m.feedback.comment)),
        h("div", { class: "card panel" }, h("h3", { class: "section-title" }, "Transcript"), h("div", { class: "turns" }, turns)),
        h("div", { class: "card panel" },
          h("h3", { class: "section-title" }, "Your review"), seg, note,
          h("div", { class: "review-foot" }, saveBtn, d.review && h("span", { class: "session-meta" }, "Saved " + new Date(d.review.reviewed_at * 1000).toLocaleString())))));
  }

  // ---- loading + actions ----
  async function load() {
    try {
      const data = await api("/api/evals");
      rows = data.sessions;
      renderSummary(data.summary);
      renderLatency();
      renderTable();
    } catch (e) {
      $("tableWrap").replaceChildren(h("div", { class: "state" }, h("strong", null, "Couldn't load evaluations"), e.message));
    }
  }

  async function judgeAll() {
    const todo = rows.filter(judgeable).slice(0, 10);
    if (!todo.length) { toast("Nothing left to score"); return; }
    const btn = $("judgeAll");
    btn.disabled = true;
    let done = 0, failed = 0;
    for (const r of todo) {
      btn.textContent = "Scoring " + (done + failed + 1) + " of " + todo.length + "…";
      try { await api("/api/evals/" + encodeURIComponent(r.id) + "/judge", { method: "POST" }); done++; }
      catch (_) { failed++; }
    }
    btn.disabled = false; btn.textContent = "Evaluate unscored";
    toast("Scored " + done + (failed ? ", " + failed + " failed" : ""), failed > 0 && done === 0);
    await load();
  }

  $("refresh").addEventListener("click", load);
  $("judgeAll").addEventListener("click", judgeAll);
  $("search").addEventListener("input", renderTable);
  $("filter").addEventListener("change", renderTable);
  $("scrim").addEventListener("click", closeDrawer);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && openId) closeDrawer(); });
  $("logout").addEventListener("click", async () => {
    try { await fetch("/logout", { method: "POST" }); } finally { location.href = "/login"; }
  });

  load().then(() => { if (location.hash.length > 1) openDrawer(decodeURIComponent(location.hash.slice(1))); });
})();
