import threading
import time
from collections import defaultdict

from fastapi import Request

MAX_ATTEMPTS = 5
WINDOW_SECONDS = 300
# Hard cap on how many buckets may be tracked at once, so `_failures` cannot grow
# without bound (SEC-014). Deliberately *not* a cap on total attempts: a global attempt
# ceiling would let anyone lock the real owner out of /login at will, repeatably, by
# burning failures from addresses that aren't the owner's. Bucket keys are already
# unspoofable under a correct TRUSTED_PROXIES setup, so bounding memory is the only job
# left here — an over-cap situation evicts the least recently active bucket rather than
# refusing anybody's login.
MAX_TRACKED_KEYS = 4096

_lock = threading.Lock()
_failures: dict[str, list[float]] = defaultdict(list)


def _client_key(request: Request) -> str:
    """The throttle bucket for this request.

    Defaults to the immediate peer address. `X-Forwarded-For` is client-supplied, so it
    is honored only when the operator has declared both how many proxy hops to trust
    (`TRUSTED_PROXY_HOPS`) and which peers are that proxy (`TRUSTED_PROXIES`) — the two
    are validated together at startup in `settings`. Trusting the header unconditionally
    (SEC-001) meant a fresh header value bought a fresh bucket, i.e. unlimited guesses
    at the single secret gating remote code execution.
    """
    # Imported inside the function on purpose: `settings` validates the whole secret
    # environment at import time, and this module is imported by test fixtures that have
    # no such environment. By the time a request reaches here the app has started, so
    # settings' fail-fast has already run.
    from . import settings

    peer = request.client.host if request.client else "unknown"
    if settings.TRUSTED_PROXY_HOPS <= 0 or peer not in settings.TRUSTED_PROXIES:
        return peer
    hops = settings.TRUSTED_PROXY_HOPS
    forwarded = [p.strip() for p in request.headers.get("x-forwarded-for", "").split(",") if p.strip()]
    if len(forwarded) < hops:
        # Fewer hops than configured: the request did not traverse the expected proxy
        # chain, so the header is not the one we were told to trust.
        return peer
    return forwarded[-hops]


def _prune(now: float) -> None:
    """Drops expired timestamps across every bucket and forgets emptied buckets.
    Called under `_lock`. Sweeping globally (rather than only the bucket being touched)
    is what keeps `_failures` bounded."""
    for key in list(_failures):
        attempts = _failures[key]
        attempts[:] = [t for t in attempts if now - t < WINDOW_SECONDS]
        if not attempts:
            del _failures[key]


def _evict_over_cap() -> None:
    """Keeps at most MAX_TRACKED_KEYS buckets, dropping the least recently active first.
    Called under `_lock`, after `_prune`. Eviction only forgets a bucket early; it never
    blocks a login, which is the point — see MAX_TRACKED_KEYS."""
    excess = len(_failures) - MAX_TRACKED_KEYS
    if excess <= 0:
        return
    stale_first = sorted(_failures, key=lambda key: _failures[key][-1])
    for key in stale_first[:excess]:
        del _failures[key]


def seconds_until_unlocked(request: Request) -> float | None:
    key = _client_key(request)
    now = time.monotonic()
    with _lock:
        _prune(now)
        attempts = _failures.get(key, [])
        if len(attempts) >= MAX_ATTEMPTS:
            return WINDOW_SECONDS - (now - attempts[0])
    return None


def record_failure(request: Request) -> None:
    now = time.monotonic()
    with _lock:
        _prune(now)
        _failures[_client_key(request)].append(now)
        _evict_over_cap()


def record_success(request: Request) -> None:
    with _lock:
        _failures.pop(_client_key(request), None)


def reset() -> None:
    with _lock:
        _failures.clear()
