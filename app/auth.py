"""Access control: invite codes for callers, an admin key for the dashboard/API.

Everything fails closed: with no ACCESS_CODES / ADMIN_KEY configured, nobody gets in.
Credentials live in HttpOnly SameSite=Strict cookies, so the existing front end
(same-origin fetch + WebSocket) works without sending anything explicitly.
"""
import asyncio
import hashlib
import hmac
import time
from urllib.parse import urlparse

from fastapi import HTTPException
from starlette.requests import HTTPConnection

from app import config

ACCESS_COOKIE = "vb_access"
ADMIN_COOKIE = "vb_admin"


def _token(secret: str) -> str:
    return hashlib.sha256(("voicebot-auth:" + secret).encode()).hexdigest()


def _eq(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def match_code(value: str) -> str | None:
    """Return the matching invite code, or None."""
    if not value:
        return None
    for code in config.ACCESS_CODES:
        if _eq(value, code):
            return code
    return None


def is_admin_key(value: str) -> bool:
    return bool(config.ADMIN_KEY) and bool(value) and _eq(value, config.ADMIN_KEY)


def _cookie_admin(conn: HTTPConnection) -> bool:
    return bool(config.ADMIN_KEY) and _eq(conn.cookies.get(ADMIN_COOKIE, ""), _token(config.ADMIN_KEY))


def _header_admin(conn: HTTPConnection) -> bool:
    return is_admin_key(conn.headers.get("x-admin-key", ""))


def is_admin(conn: HTTPConnection) -> bool:
    return _cookie_admin(conn) or _header_admin(conn)


def caller_code(conn: HTTPConnection) -> str | None:
    """The invite code presented by this connection (admins count as their own caller)."""
    cookie = conn.cookies.get(ACCESS_COOKIE, "")
    for code in config.ACCESS_CODES:
        if _eq(cookie, _token(code)):
            return code
    if is_admin(conn):
        return "__admin__"
    return None


def require_admin(conn: HTTPConnection) -> None:
    if not is_admin(conn):
        raise HTTPException(status_code=401, detail="Admin access required")


def require_caller(conn: HTTPConnection) -> str:
    code = caller_code(conn)
    if code is None:
        raise HTTPException(status_code=401, detail="Access code required")
    return code


def origin_ok(conn: HTTPConnection) -> bool:
    """Reject cross-site browser WebSocket connections."""
    origin = conn.headers.get("origin")
    if not origin:
        return True  # non-browser client; still needs a valid credential
    if origin in config.ALLOWED_ORIGINS:
        return True
    return urlparse(origin).netloc == conn.headers.get("host", "")


def cookie_for(kind: str, secret: str) -> tuple[str, str]:
    return (ADMIN_COOKIE if kind == "admin" else ACCESS_COOKIE), _token(secret)


# ---- call limits (in-memory; resets on restart) ----

_active: set[str] = set()
_used: dict[tuple[str, str], float] = {}


def _today() -> str:
    return time.strftime("%Y-%m-%d", time.gmtime())


def seconds_left_today(code: str) -> float:
    return max(0.0, config.DAILY_CALL_SECONDS - _used.get((code, _today()), 0.0))


def try_begin_call(code: str) -> str | None:
    """Reserve a call slot. Returns an error string if the caller may not start one."""
    if code in _active:
        return "A call is already active for this code"
    if seconds_left_today(code) <= 0:
        return "Daily call limit reached"
    _active.add(code)
    return None


def end_call(code: str, started: float) -> None:
    _active.discard(code)
    key = (code, _today())
    _used[key] = _used.get(key, 0.0) + (time.monotonic() - started)


def max_seconds_for(code: str) -> float:
    return min(config.MAX_CALL_SECONDS, seconds_left_today(code))
