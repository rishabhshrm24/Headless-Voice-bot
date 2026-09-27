"""Session evaluation: metrics derived from the event timeline, an optional LLM judge,
and human review. Judge scores and reviews are persisted with a transcript snapshot so
they survive the in-memory session tracker being restarted.
"""
import json
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional

from app import config

SLOW_MS = 5000
FALLBACK_RE = re.compile(
    r"(don'?t|do not) have (that|this|any|enough)? ?(information|info)|not (in|covered by) (my|the) knowledge|"
    r"i('m| am) not sure|i can'?t find|no (relevant )?information",
    re.I,
)
USER_EVENTS = ("user_message", "speech_transcribed")
JUDGE_CRITERIA = ("accuracy", "groundedness", "helpfulness", "conciseness")

JUDGE_PROMPT = (
    "You evaluate a support voice assistant. You get a conversation transcript and the knowledge-base "
    "excerpts the assistant retrieved. Score each criterion from 1 (poor) to 5 (excellent):\n"
    "- accuracy: the answers are correct for what the caller asked\n"
    "- groundedness: claims are supported by the retrieved excerpts; no invented facts. If nothing was "
    "retrieved and the assistant admitted it lacked the information, that is grounded.\n"
    "- helpfulness: the caller's need was actually resolved or correctly redirected\n"
    "- conciseness: short and natural to hear aloud (1-4 sentences)\n"
    'Reply with JSON only: {"accuracy": n, "groundedness": n, "helpfulness": n, "conciseness": n, '
    '"verdict": "pass" | "fail", "rationale": "<=2 sentences"}. Use "fail" if any criterion is 2 or below.'
)


def _first(details: Dict[str, Any], keys: tuple) -> str:
    for k in keys:
        v = details.get(k)
        if isinstance(v, str) and v.strip():
            return v.strip()
    return ""


ASSISTANT_EVENTS = ("assistant_reply", "speech_synthesized")


def build_turns(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Pair each caller utterance with the assistant reply that followed it."""
    turns: List[Dict[str, Any]] = []
    pending: Optional[Dict[str, Any]] = None
    sources: List[Dict[str, Any]] = []
    for e in events:
        d = e.get("details") or {}
        et = e.get("event_type")
        if et in USER_EVENTS:
            if pending is not None:
                # A new user turn arrived before the previous one got a reply - it went unanswered.
                turns.append({"user": pending["user"], "assistant": "", "latency_ms": None, "sources": []})
            pending = {"user": _first(d, ("transcript", "text", "question")), "ts": e["timestamp"]}
            sources = []
        elif et == "rag_result":
            sources = [s for s in (d.get("sources") or []) if isinstance(s, dict)]
        elif et in ASSISTANT_EVENTS:
            reply = _first(d, ("transcript", "text", "answer"))
            if pending is None:
                # No user event preceded this reply (e.g. batch chat) - the question lives on
                # the reply event itself.
                turns.append({"user": _first(d, ("question",)), "assistant": reply, "latency_ms": None, "sources": sources})
            else:
                turns.append({
                    "user": pending["user"] or _first(d, ("question",)),
                    "assistant": reply,
                    "latency_ms": max(0, round((e["timestamp"] - pending["ts"]) * 1000)),
                    "sources": sources,
                })
                pending = None
            sources = []
    if pending is not None:
        turns.append({"user": pending["user"], "assistant": "", "latency_ms": None, "sources": []})
    return turns


def _percentile(values: List[float], pct: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(round(pct * (len(ordered) - 1))))]


def compute_metrics(session: Dict[str, Any]) -> Dict[str, Any]:
    events = session.get("events", [])
    turns = build_turns(events)
    latencies = [t["latency_ms"] for t in turns if t["latency_ms"] is not None]
    feedback = next(
        (e["details"] for e in reversed(events) if e.get("event_type") == "feedback"), None
    )
    kb_hit = any(t["sources"] for t in turns)
    fallback = any(FALLBACK_RE.search(t["assistant"]) for t in turns)
    stats = session.get("stats", {})
    errors = stats.get("errors", 0)

    flags = []
    if errors:
        flags.append("error")
    if any(t["user"] and not t["assistant"] for t in turns):
        flags.append("no_reply")
    if latencies and max(latencies) > SLOW_MS:
        flags.append("slow")
    if fallback:
        flags.append("fallback")
    if turns and stats.get("rag_lookups", 0) and not kb_hit:
        flags.append("no_kb_hit")
    if feedback and (feedback.get("rating", 5) <= 2 or feedback.get("resolved") is False):
        flags.append("unhappy_caller")

    return {
        "turn_count": len(turns),
        "avg_latency_ms": round(sum(latencies) / len(latencies)) if latencies else None,
        "p95_latency_ms": _percentile(latencies, 0.95),
        "max_latency_ms": max(latencies) if latencies else None,
        "rag_lookups": stats.get("rag_lookups", 0),
        "kb_hit": kb_hit,
        "errors": errors,
        "fallback": fallback,
        "feedback": feedback,
        "flags": flags,
    }


def overall(scores: Dict[str, Any]) -> Optional[float]:
    vals = [scores[c] for c in JUDGE_CRITERIA if isinstance(scores.get(c), (int, float))]
    return round(sum(vals) / len(vals), 1) if vals else None


def _transcript_for_judge(turns: List[Dict[str, Any]]) -> str:
    parts = []
    for i, t in enumerate(turns, 1):
        parts.append(f"Turn {i}\nCaller: {t['user'] or '(none)'}")
        if t["sources"]:
            excerpts = " | ".join(s.get("text", "")[:400] for s in t["sources"][:4])
            parts.append(f"Retrieved: {excerpts}")
        else:
            parts.append("Retrieved: (nothing)")
        parts.append(f"Assistant: {t['assistant'] or '(no reply)'}")
    return "\n".join(parts)


def judge_error_reason(exc: Exception) -> str:
    """A short, non-sensitive, actionable reason for a failed judge call, for the API
    response. The full exception (which may include upstream error bodies) still goes
    to the server log via logging.exception - this is only the safe summary."""
    import openai

    if isinstance(exc, RuntimeError):
        return str(exc)  # our own config-check message, e.g. "OPENAI_API_KEY is not configured"
    if isinstance(exc, openai.AuthenticationError):
        return "the judge model rejected the API key - check OPENAI_API_KEY"
    if isinstance(exc, openai.RateLimitError):
        return "the judge model is rate-limited or out of quota - try again shortly"
    if isinstance(exc, openai.APITimeoutError):
        return "the judge model call timed out - try again"
    if isinstance(exc, openai.APIConnectionError):
        return "could not reach the judge model API - check network/connectivity"
    if isinstance(exc, (openai.NotFoundError, openai.BadRequestError)):
        return "the judge model rejected the request - check JUDGE_MODEL in config"
    if isinstance(exc, openai.PermissionDeniedError):
        return "not permitted to use the configured judge model"
    if isinstance(exc, (json.JSONDecodeError, ValueError, TypeError)):
        return "the judge model returned an unexpected response - see server logs"
    return "judge call failed - see server logs"


def judge_turns(turns: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Score a conversation with an LLM. Blocking; call from a worker thread."""
    from app.rag_store import get_client

    completion = get_client().chat.completions.create(
        model=config.JUDGE_MODEL,
        temperature=0,
        response_format={"type": "json_object"},
        messages=[
            {"role": "system", "content": JUDGE_PROMPT},
            {"role": "user", "content": _transcript_for_judge(turns)},
        ],
    )
    raw = json.loads(completion.choices[0].message.content or "{}")
    scores = {c: max(1, min(5, int(raw.get(c, 3)))) for c in JUDGE_CRITERIA}
    scores["overall"] = overall(scores)
    scores["verdict"] = "fail" if raw.get("verdict") == "fail" or min(scores[c] for c in JUDGE_CRITERIA) <= 2 else "pass"
    scores["rationale"] = str(raw.get("rationale", ""))[:500]
    scores["model"] = config.JUDGE_MODEL
    scores["judged_at"] = time.time()
    return scores


class EvalStore:
    """JSON-file store: {session_id: {snapshot, judge, review}}."""

    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        self._data: Dict[str, Dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._data = json.load(f)
        except (OSError, ValueError):
            self._data = {}

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._data, f)
        os.replace(tmp, self.path)

    def get(self, session_id: str) -> Dict[str, Any]:
        with self._lock:
            return dict(self._data.get(session_id, {}))

    def all(self) -> Dict[str, Dict[str, Any]]:
        with self._lock:
            return {k: dict(v) for k, v in self._data.items()}

    def _update(self, session_id: str, snapshot: Dict[str, Any], **fields: Any) -> None:
        with self._lock:
            rec = self._data.setdefault(session_id, {})
            rec["snapshot"] = snapshot
            rec.update(fields)
            self._save()

    def set_judge(self, session_id: str, snapshot: Dict[str, Any], judge: Dict[str, Any]) -> None:
        self._update(session_id, snapshot, judge=judge)

    def set_review(self, session_id: str, snapshot: Dict[str, Any], review: Dict[str, Any]) -> None:
        self._update(session_id, snapshot, review=review)

    def clear(self) -> None:
        with self._lock:
            self._data = {}
            self._save()


store = EvalStore(config.EVALS_PATH)


def snapshot_of(session: Dict[str, Any]) -> Dict[str, Any]:
    """Compact, restart-proof copy of a session (no raw events)."""
    return {
        "id": session["id"],
        "session_type": session.get("session_type"),
        "status": session.get("status"),
        "started_iso": session.get("started_iso"),
        "started_at": session.get("started_at"),
        "duration_seconds": session.get("duration_seconds"),
        "summary": session.get("summary"),
        "turns": build_turns(session.get("events", [])),
        "metrics": compute_metrics(session),
    }


def row_of(snap: Dict[str, Any], rec: Dict[str, Any]) -> Dict[str, Any]:
    judge = rec.get("judge")
    return {
        "id": snap["id"],
        "session_type": snap.get("session_type"),
        "status": snap.get("status"),
        "started_iso": snap.get("started_iso"),
        "started_at": snap.get("started_at"),
        "duration_seconds": snap.get("duration_seconds"),
        "summary": snap.get("summary"),
        "metrics": snap["metrics"],
        "judge": judge,
        "review": rec.get("review"),
    }


def _numeric(values: List[Any]) -> List[float]:
    """Keep only real numeric scores - guards against stale/foreign records in the JSON
    store (missing fields, nulls, or a future schema change) crashing the whole summary."""
    return [v for v in values if isinstance(v, (int, float)) and not isinstance(v, bool)]


def summarize(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    latencies = [r["metrics"]["avg_latency_ms"] for r in rows if r["metrics"]["avg_latency_ms"] is not None]
    judged = [r["judge"] for r in rows if r.get("judge")]
    rated = [r["metrics"]["feedback"]["rating"] for r in rows if r["metrics"]["feedback"]]
    resolved = [r["metrics"]["feedback"]["resolved"] for r in rows if r["metrics"]["feedback"]]
    finished = [r for r in rows if r["status"] != "active"]
    verdicts = [j.get("verdict") for j in judged if j.get("verdict") in ("pass", "fail")]
    overall_vals = _numeric(j.get("overall") for j in judged)
    criteria = {}
    for c in JUDGE_CRITERIA:
        vals = _numeric(j.get(c) for j in judged)
        criteria[c] = round(sum(vals) / len(vals), 2) if vals else None
    return {
        "sessions": len(rows),
        "judged": len(judged),
        "reviewed": sum(1 for r in rows if r.get("review")),
        "pass_rate": round(sum(1 for v in verdicts if v == "pass") / len(verdicts), 3) if verdicts else None,
        "avg_overall": round(sum(overall_vals) / len(overall_vals), 2) if overall_vals else None,
        "criteria": criteria,
        "p50_latency_ms": _percentile(latencies, 0.5),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "avg_rating": round(sum(rated) / len(rated), 2) if rated else None,
        "resolved_rate": round(sum(1 for r in resolved if r) / len(resolved), 3) if resolved else None,
        "error_rate": round(sum(1 for r in finished if r["metrics"]["errors"]) / len(finished), 3) if finished else None,
        "fallback_rate": round(sum(1 for r in rows if r["metrics"]["fallback"]) / len(rows), 3) if rows else None,
    }
