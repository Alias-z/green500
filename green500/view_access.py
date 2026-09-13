"""Password sessions for the read-only Green500 web view."""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import secrets
import threading
import time
from collections import deque
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from green500.config import load_settings

router = APIRouter()

COOKIE_NAME = "green500_view_session"
SESSION_SECONDS = 30 * 24 * 60 * 60
FAILED_LOGIN_WINDOW_SECONDS = 60
FAILED_LOGIN_LIMIT = 5
FAILED_LOGIN_CLIENT_LIMIT = 10_000
MAX_SESSION_TOKEN_CHARACTERS = 80
SIGNING_KEY_FILE = ".viewer-session-key"

_SESSION_TOKEN_PATTERN = re.compile(r"([0-9]{1,12})\.([0-9a-f]{64})")
_FAILED_LOGINS: dict[str, deque[float]] = {}
_FAILED_LOGINS_LOCK = threading.Lock()


class LoginRequest(BaseModel):
    """Accept only the viewer password expected by the login endpoint."""

    model_config = ConfigDict(extra="forbid")
    password: str = Field(max_length=4096)


def _external_scheme(request: Request) -> str:
    """Return the browser-facing scheme supplied by the local reverse proxy."""
    forwarded = request.headers.get("x-forwarded-proto", "").split(",", 1)[0].strip()
    if forwarded in {"http", "https"}:
        return forwarded
    return request.url.scheme


def _normalized_origin(value: str) -> str | None:
    """Normalize one HTTP origin for an exact same-origin comparison."""
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    if parsed.username or parsed.password or parsed.path not in {"", "/"}:
        return None
    if parsed.query or parsed.fragment:
        return None
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        port is None
        or (parsed.scheme == "http" and port == 80)
        or (parsed.scheme == "https" and port == 443)
    ):
        authority = parsed.hostname
    else:
        authority = f"{parsed.hostname}:{port}"
    return f"{parsed.scheme}://{authority}".casefold()


def _request_origin(request: Request) -> str:
    """Build the public origin while the application listens on local port 19973."""
    host = request.headers.get("x-forwarded-host", "").split(",", 1)[0].strip()
    if not host:
        host = request.headers.get("host", request.url.netloc)
    return f"{_external_scheme(request)}://{host}"


def _require_same_origin(request: Request) -> None:
    """Reject browser writes from a page on another origin."""
    origin = request.headers.get("origin")
    if not origin:
        return
    supplied = _normalized_origin(origin)
    expected = _normalized_origin(_request_origin(request))
    if (
        supplied is None
        or expected is None
        or not hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
    ):
        raise HTTPException(403, "Cross-origin login actions are not allowed.")


def _client_key(request: Request) -> str:
    """Identify a browser for a small per-process failed-login allowance."""
    for header in ("cf-connecting-ip", "x-forwarded-for"):
        value = request.headers.get(header, "").split(",", 1)[0].strip()
        if value:
            return value[:200]
    return request.client.host[:200] if request.client else "unknown"


def _is_throttled(client_key: str, now: float) -> bool:
    """Limit repeated failures without retaining successful login history."""
    with _FAILED_LOGINS_LOCK:
        _remove_old_failed_logins(now)
        return len(_FAILED_LOGINS.get(client_key, ())) >= FAILED_LOGIN_LIMIT


def _remove_old_failed_logins(now: float) -> None:
    """Drop expired client entries while the failed-login lock is held."""
    cutoff = now - FAILED_LOGIN_WINDOW_SECONDS
    for client_key in list(_FAILED_LOGINS):
        failures = _FAILED_LOGINS[client_key]
        while failures and failures[0] <= cutoff:
            failures.popleft()
        if not failures:
            del _FAILED_LOGINS[client_key]


def _record_failed_login(client_key: str, now: float) -> None:
    """Record one failed comparison for this process only."""
    with _FAILED_LOGINS_LOCK:
        _remove_old_failed_logins(now)
        if (
            client_key not in _FAILED_LOGINS
            and len(_FAILED_LOGINS) >= FAILED_LOGIN_CLIENT_LIMIT
        ):
            oldest_client = min(
                _FAILED_LOGINS,
                key=lambda key: _FAILED_LOGINS[key][-1],
            )
            del _FAILED_LOGINS[oldest_client]
        _FAILED_LOGINS.setdefault(client_key, deque()).append(now)


def _clear_failed_logins(client_key: str) -> None:
    """Forget failures after the configured password succeeds."""
    with _FAILED_LOGINS_LOCK:
        _FAILED_LOGINS.pop(client_key, None)


def _read_signing_secret(path) -> bytes:
    """Read the exact private key without following a symbolic link."""
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fchmod(descriptor, 0o600)
        secret = os.read(descriptor, 33)
    finally:
        os.close(descriptor)
    if len(secret) != 32:
        raise ValueError("The viewer session key must contain exactly 32 bytes.")
    return secret


def _signing_secret() -> bytes:
    """Load one persistent key, creating it once without overwrite races."""
    data_dir = load_settings().data_dir
    data_dir.mkdir(parents=True, exist_ok=True)
    path = data_dir / SIGNING_KEY_FILE
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except FileExistsError:
        for _ in range(20):
            try:
                return _read_signing_secret(path)
            except ValueError:
                time.sleep(0.01)
        return _read_signing_secret(path)

    secret = secrets.token_bytes(32)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(secret)
            handle.flush()
            os.fsync(handle.fileno())
            os.fchmod(handle.fileno(), 0o600)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    directory = os.open(data_dir, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
    return secret


def _session_token(now: float | None = None) -> str:
    """Sign an absolute expiry; the cookie contains no password or Ops token."""
    expires_at = int((time.time() if now is None else now) + SESSION_SECONDS)
    payload = str(expires_at)
    signature = hmac.new(
        _signing_secret(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return f"{payload}.{signature}"


def _valid_session(token: str | None, now: float | None = None) -> bool:
    """Verify the signature in constant time and reject expired sessions."""
    if not token or len(token) > MAX_SESSION_TOKEN_CHARACTERS:
        return False
    matched = _SESSION_TOKEN_PATTERN.fullmatch(token)
    if not matched:
        return False
    payload, supplied_signature = matched.groups()
    expires_at = int(payload)
    if expires_at <= (time.time() if now is None else now):
        return False
    expected_signature = hmac.new(
        _signing_secret(), payload.encode("ascii"), hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(supplied_signature, expected_signature)


def _view_password() -> str:
    """Read the viewer password without exposing it in responses or cookies."""
    return getattr(load_settings(), "view_password", "")


def authorize_view(request: Request) -> None:
    """Allow a configured viewer session, or local setup with no viewer password."""
    if not _view_password():
        return
    if not _valid_session(request.cookies.get(COOKIE_NAME)):
        raise HTTPException(401, "Enter the Green500 viewer password.")


def refresh_session_cookie(response: Response, request: Request) -> bool:
    """Renew one valid configured session for another complete 30-day period."""
    if not _view_password() or not _valid_session(request.cookies.get(COOKIE_NAME)):
        return False
    response.set_cookie(
        key=COOKIE_NAME,
        value=_session_token(),
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=_external_scheme(request) == "https",
        samesite="lax",
        path="/",
    )
    return True


@router.post("/api/login")
def login(value: LoginRequest, request: Request):
    """Create a 30-day viewer session after a same-origin password check."""
    _require_same_origin(request)
    configured_password = _view_password()
    if not configured_password:
        return {"authenticated": True}

    now = time.time()
    client_key = _client_key(request)
    if _is_throttled(client_key, now):
        raise HTTPException(429, "Too many failed login attempts. Try again shortly.")
    if not hmac.compare_digest(
        value.password.encode("utf-8"), configured_password.encode("utf-8")
    ):
        _record_failed_login(client_key, now)
        raise HTTPException(401, "The viewer password is incorrect.")

    _clear_failed_logins(client_key)
    response = JSONResponse({"authenticated": True})
    response.set_cookie(
        key=COOKIE_NAME,
        value=_session_token(now),
        max_age=SESSION_SECONDS,
        httponly=True,
        secure=_external_scheme(request) == "https",
        samesite="lax",
        path="/",
    )
    return response


@router.post("/api/logout")
def logout(request: Request):
    """Expire the viewer cookie after the same-origin browser check."""
    _require_same_origin(request)
    response = JSONResponse({"authenticated": False})
    response.delete_cookie(
        key=COOKIE_NAME,
        httponly=True,
        secure=_external_scheme(request) == "https",
        samesite="lax",
        path="/",
    )
    return response


@router.get("/api/session")
def session(request: Request):
    """Report viewer authentication without revealing session details."""
    configured_password = _view_password()
    authenticated = not configured_password or _valid_session(
        request.cookies.get(COOKIE_NAME)
    )
    response = JSONResponse({"authenticated": authenticated})
    if configured_password and authenticated:
        refresh_session_cookie(response, request)
    return response
