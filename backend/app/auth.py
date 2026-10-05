import hmac
import os

from fastapi import BackgroundTasks, Header, HTTPException, Request

from . import db, settings


def secrets_match(candidate: str, expected: str) -> bool:
    """Constant-time comparison that can't be tripped by the input's encoding.

    `hmac.compare_digest` raises TypeError on a `str` containing non-ASCII characters,
    which turned any non-ASCII credential into an unauthenticated 500 (SEC-012).
    Comparing the UTF-8 bytes removes that failure mode entirely.
    """
    return hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def require_api_key(authorization: str = Header(default="")) -> None:
    if not secrets_match(authorization, f"Bearer {settings.API_KEY}"):
        raise HTTPException(status_code=401, detail="invalid or missing API key")


def session_binding() -> str:
    """Fingerprint of the current API_KEY, stored in the session cookie at login.

    The cookie is signed (SECRET_KEY) but not encrypted, so the key itself must never
    go in it; an HMAC keyed with SECRET_KEY reveals nothing about API_KEY. Comparing it
    on every request means rotating API_KEY revokes every outstanding session, which
    the signature check alone cannot do (SEC-007).
    """
    return hmac.new(
        settings.SECRET_KEY.encode("utf-8"),
        b"session-binding:" + settings.API_KEY.encode("utf-8"),
        "sha256",
    ).hexdigest()


def is_authenticated(request: Request) -> bool:
    if not request.session.get("authenticated"):
        return False
    bound = request.session.get("binding")
    return isinstance(bound, str) and secrets_match(bound, session_binding())


def _bump_active_window() -> None:
    """Opens its own connection rather than reusing the request's.

    Runs as a background task, which FastAPI executes after the response has
    already been sent — by then a yield-dependency's connection has already
    been closed (FastAPI >=0.106 runs that cleanup before background tasks),
    so this can't borrow one from the request.
    """
    conn = db.get_connection(os.environ.get("DATABASE_PATH", "app.db"))
    try:
        db.bump_active_interval(conn, settings.ACTIVE_INTERVAL_DURATION_MIN)
    finally:
        conn.close()


def require_session(request: Request, background_tasks: BackgroundTasks) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=307, headers={"Location": "/login"})
    # Every page a logged-in user actually navigates to counts as activity: this
    # is deliberately the single seam all browser routes share, so a menu click,
    # a filter change, or just reloading a chat all extend the fast-poll window —
    # not only the handful of routes that create an agent job. Scheduled as a
    # background task (not called synchronously here) so the poll-mode badge on
    # *this* response still reflects the state the user was in when they clicked,
    # and only the *next* request sees the window as open.
    background_tasks.add_task(_bump_active_window)
