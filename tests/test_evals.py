import asyncio
import json
import time

import httpx
import openai
import pytest
from fastapi.testclient import TestClient

from app import evals
from app.session_tracker import tracker


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setattr(evals, "store", evals.EvalStore(str(tmp_path / "evals.json")))
    tracker.clear()
    yield
    tracker.clear()


def _voice_session(reply="Our hours are 9 to 5.", results=1, rating=None, error=False):
    s = tracker.create_session("websocket_voice")
    tracker.record_event(s.id, "speech_transcribed", "User spoke", {"transcript": "What are your hours?"})
    tracker.record_event(s.id, "rag_query", "Search", {"query": "hours"})
    tracker.record_event(
        s.id, "rag_result", "Found", {"results_count": results, "sources": [{"source": "faq.md", "text": "Open 9-5", "score": 0.9}] * results}
    )
    tracker.record_event(s.id, "assistant_reply", "Reply", {"transcript": reply})
    if rating is not None:
        tracker.record_event(s.id, "feedback", "fb", {"rating": rating, "resolved": rating > 2, "comment": None})
    if error:
        tracker.record_event(s.id, "error", "boom", {"error": "x"})
    tracker.end_session(s.id)
    return s


def test_build_turns_pairs_user_with_reply_and_sources():
    s = _voice_session()
    turns = evals.build_turns(s.to_dict()["events"])
    assert len(turns) == 1
    assert turns[0]["user"] == "What are your hours?"
    assert turns[0]["assistant"] == "Our hours are 9 to 5."
    assert turns[0]["latency_ms"] is not None and turns[0]["latency_ms"] >= 0
    assert turns[0]["sources"][0]["source"] == "faq.md"


def test_multiple_user_turns_before_a_reply_are_not_lost():
    s = tracker.create_session("websocket_voice")
    tracker.record_event(s.id, "speech_transcribed", "u1", {"transcript": "First question?"})
    tracker.record_event(s.id, "speech_transcribed", "u2", {"transcript": "Second question?"})
    tracker.record_event(s.id, "assistant_reply", "a", {"transcript": "Answering the second."})
    tracker.end_session(s.id)
    turns = evals.build_turns(s.to_dict()["events"])
    assert [t["user"] for t in turns] == ["First question?", "Second question?"]
    assert turns[0]["assistant"] == ""
    assert turns[1]["assistant"] == "Answering the second."


def test_sources_do_not_leak_into_a_later_reply_without_a_new_rag_result():
    s = tracker.create_session("websocket_voice")
    tracker.record_event(s.id, "speech_transcribed", "u1", {"transcript": "Q1"})
    tracker.record_event(s.id, "rag_result", "r", {"sources": [{"source": "faq.md", "text": "x"}]})
    tracker.record_event(s.id, "assistant_reply", "a1", {"transcript": "A1"})
    tracker.record_event(s.id, "speech_transcribed", "u2", {"transcript": "Q2"})
    tracker.record_event(s.id, "assistant_reply", "a2", {"transcript": "A2"})
    tracker.end_session(s.id)
    turns = evals.build_turns(s.to_dict()["events"])
    assert turns[0]["sources"] != []
    assert turns[1]["sources"] == []


def test_http_voice_speech_synthesized_counts_as_the_reply():
    s = tracker.create_session("http_voice")
    tracker.record_event(s.id, "speech_transcribed", "u", {"transcript": "What are your hours?"})
    tracker.record_event(s.id, "speech_synthesized", "a", {"answer": "Nine to five."})
    tracker.end_session(s.id)
    turns = evals.build_turns(s.to_dict()["events"])
    assert turns[0]["assistant"] == "Nine to five."
    m = evals.compute_metrics(s.to_dict())
    assert "no_reply" not in m["flags"]


def test_batch_chat_replies_carry_their_own_question():
    s = tracker.create_session("batch_chat")
    tracker.record_event(s.id, "user_message", "batch", {"questions": ["Q1?", "Q2?"]})
    tracker.record_event(s.id, "assistant_reply", "r1", {"question": "Q1?", "answer": "A1"})
    tracker.record_event(s.id, "assistant_reply", "r2", {"question": "Q2?", "answer": "A2"})
    tracker.end_session(s.id)
    turns = evals.build_turns(s.to_dict()["events"])
    assert [(t["user"], t["assistant"]) for t in turns] == [("Q1?", "A1"), ("Q2?", "A2")]


def test_unanswered_user_turn_is_flagged():
    s = tracker.create_session("websocket_voice")
    tracker.record_event(s.id, "speech_transcribed", "spoke", {"transcript": "Hello?"})
    tracker.end_session(s.id)
    m = evals.compute_metrics(s.to_dict())
    assert "no_reply" in m["flags"]


def test_metrics_flag_fallback_errors_and_unhappy_caller():
    s = _voice_session(reply="I don't have that information.", results=0, rating=1, error=True)
    m = evals.compute_metrics(s.to_dict())
    assert m["fallback"] is True
    assert m["errors"] == 1
    assert {"fallback", "error", "unhappy_caller"} <= set(m["flags"])
    assert m["feedback"]["rating"] == 1


def test_clean_session_has_no_flags():
    s = _voice_session(rating=5)
    m = evals.compute_metrics(s.to_dict())
    assert m["flags"] == []
    assert m["kb_hit"] is True


def test_summarize_aggregates():
    _voice_session(rating=5)
    _voice_session(rating=1)
    rows = [evals.row_of(evals.snapshot_of(s.to_dict()), {}) for s in tracker.all_sessions()]
    summary = evals.summarize(rows)
    assert summary["sessions"] == 2
    assert summary["avg_rating"] == 3.0
    assert summary["resolved_rate"] == 0.5
    assert summary["pass_rate"] is None  # nothing judged yet


def test_summarize_tolerates_malformed_judge_records():
    """A stale/foreign record in the JSON store (missing fields, nulls, unknown verdict)
    must not crash the summary - it should just be excluded from the affected averages."""
    good = _voice_session(rating=4)
    stale = _voice_session(rating=4)
    rows = [evals.row_of(evals.snapshot_of(s.to_dict()), {}) for s in [good, stale]]
    rows[0]["judge"] = {"accuracy": 5, "groundedness": 5, "conciseness": 5, "overall": 5.0, "verdict": "pass"}
    rows[1]["judge"] = {"accuracy": None, "overall": "n/a", "verdict": "unknown"}  # malformed/older schema
    summary = evals.summarize(rows)  # must not raise
    assert summary["judged"] == 2
    assert summary["avg_overall"] == 5.0  # the malformed "n/a" is excluded, not averaged in
    assert summary["pass_rate"] == 1.0  # "unknown" verdict excluded from the pass/fail denominator
    assert summary["criteria"]["accuracy"] == 5.0  # None on the malformed record excluded, good's 5 kept
    assert summary["criteria"]["helpfulness"] is None  # missing from both records entirely -> no crash


def test_evals_endpoints_require_admin(caller_client):
    assert TestClient(caller_client.app).get("/api/evals").status_code == 401
    assert caller_client.get("/api/evals").status_code == 401
    assert caller_client.post("/api/evals/x/judge").status_code == 401
    assert caller_client.put("/api/evals/x/review", json={"verdict": "good"}).status_code == 401


def test_evals_page_redirects_non_admin(caller_client):
    r = caller_client.get("/evals", follow_redirects=False)
    assert r.status_code in (302, 307)
    assert r.headers["location"] == "/login"


def test_list_and_detail(admin_client):
    s = _voice_session(rating=4)
    data = admin_client.get("/api/evals").json()
    assert data["summary"]["sessions"] == 1
    assert data["sessions"][0]["id"] == s.id
    detail = admin_client.get(f"/api/evals/{s.id}").json()
    assert detail["turns"][0]["user"] == "What are your hours?"
    assert admin_client.get("/api/evals/nope").status_code == 404


def test_judge_stores_scores(admin_client, monkeypatch):
    def fake(turns):
        return {"accuracy": 5, "groundedness": 4, "helpfulness": 5, "conciseness": 4, "overall": 4.5, "verdict": "pass", "rationale": "ok", "model": "m", "judged_at": 1.0}

    monkeypatch.setattr(evals, "judge_turns", fake)
    s = _voice_session()
    assert admin_client.post(f"/api/evals/{s.id}/judge").json()["overall"] == 4.5
    row = admin_client.get("/api/evals").json()["sessions"][0]
    assert row["judge"]["verdict"] == "pass"


async def test_judge_does_not_block_the_event_loop(monkeypatch):
    """The judge endpoint's store write is synchronous file I/O; it must be offloaded to a
    thread so a slow disk doesn't stall every other in-flight request (e.g. a live voice call)."""
    from app.main import app

    s = _voice_session()
    monkeypatch.setattr(evals, "judge_turns", lambda turns: {
        "accuracy": 5, "groundedness": 5, "helpfulness": 5, "conciseness": 5,
        "overall": 5.0, "verdict": "pass", "rationale": "ok", "model": "m", "judged_at": 1.0,
    })
    real_save = evals.EvalStore._save
    def slow_save(self):
        time.sleep(0.3)
        real_save(self)
    monkeypatch.setattr(evals.EvalStore, "_save", slow_save)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/login", json={"code": "test-admin"})
        started = time.monotonic()
        health_done_at = []

        async def ping_health():
            await asyncio.sleep(0.05)  # let the judge request start first
            await client.get("/health")
            health_done_at.append(time.monotonic() - started)

        results = await asyncio.gather(
            client.post(f"/api/evals/{s.id}/judge"),
            ping_health(),
        )
        assert results[0].status_code == 200
        # /health must complete well before the 0.3s blocking save finishes - proving the
        # event loop was free to serve it concurrently, not queued behind the disk write.
        assert health_done_at[0] < 0.2


def test_judge_failure_returns_502(admin_client, monkeypatch):
    def boom(turns):
        raise RuntimeError("no key")

    monkeypatch.setattr(evals, "judge_turns", boom)
    s = _voice_session()
    r = admin_client.post(f"/api/evals/{s.id}/judge")
    assert r.status_code == 502
    assert r.json()["detail"] == "no key"  # our own config message passes through verbatim


def _http_error(cls, status):
    req = httpx.Request("POST", "http://x")
    return cls("msg", response=httpx.Response(status, request=req), body=None)


@pytest.mark.parametrize("exc, expected_fragment", [
    (RuntimeError("OPENAI_API_KEY is not configured"), "OPENAI_API_KEY is not configured"),
    (_http_error(openai.AuthenticationError, 401), "rejected the API key"),
    (_http_error(openai.RateLimitError, 429), "rate-limited"),
    (openai.APITimeoutError(request=httpx.Request("POST", "http://x")), "timed out"),
    (openai.APIConnectionError(request=httpx.Request("POST", "http://x")), "could not reach"),
    (_http_error(openai.NotFoundError, 404), "JUDGE_MODEL"),
    (json.JSONDecodeError("bad", "doc", 0), "unexpected response"),
    (ValueError("not an int"), "unexpected response"),
    (Exception("something else"), "see server logs"),
])
def test_judge_error_reason_is_specific_and_safe(exc, expected_fragment):
    reason = evals.judge_error_reason(exc)
    assert expected_fragment in reason
    # never leaks a raw upstream error body/stack - only our own short, fixed wording
    assert len(reason) < 100


def test_judge_rejects_session_without_turns(admin_client):
    s = tracker.create_session("websocket_voice")
    tracker.end_session(s.id)
    assert admin_client.post(f"/api/evals/{s.id}/judge").status_code == 400


def test_review_saved_and_validated(admin_client):
    s = _voice_session()
    assert admin_client.put(f"/api/evals/{s.id}/review", json={"verdict": "meh"}).status_code == 422
    r = admin_client.put(f"/api/evals/{s.id}/review", json={"verdict": "bad", "note": "wrong hours"})
    assert r.status_code == 200
    assert admin_client.get(f"/api/evals/{s.id}").json()["review"]["note"] == "wrong hours"


def test_reviewed_session_survives_tracker_restart(admin_client):
    s = _voice_session()
    admin_client.put(f"/api/evals/{s.id}/review", json={"verdict": "good"})
    tracker.clear()
    data = admin_client.get("/api/evals").json()
    assert [r["id"] for r in data["sessions"]] == [s.id]
    assert data["sessions"][0]["review"]["verdict"] == "good"
    assert admin_client.get(f"/api/evals/{s.id}").json()["turns"][0]["assistant"]


def test_judge_scores_are_clamped(monkeypatch):
    class Msg:
        content = '{"accuracy": 9, "groundedness": 1, "helpfulness": 4, "conciseness": 4, "verdict": "pass", "rationale": "r"}'

    class Client:
        class chat:
            class completions:
                @staticmethod
                def create(**kw):
                    class C:
                        choices = [type("Ch", (), {"message": Msg})]

                    return C

    import app.rag_store as rag

    monkeypatch.setattr(rag, "get_client", lambda: Client)
    out = evals.judge_turns([{"user": "q", "assistant": "a", "latency_ms": 1, "sources": []}])
    assert out["accuracy"] == 5
    assert out["verdict"] == "fail"  # groundedness <= 2 forces a fail
