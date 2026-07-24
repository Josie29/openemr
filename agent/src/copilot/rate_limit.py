import hashlib
import json
import math
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

from starlette.types import ASGIApp, Message, Receive, Scope, Send

from copilot.config import Settings
from copilot.correlation import current_correlation_id

# AF-VULN-0002: a per-principal rolling-window rate limiter, added as pure-ASGI middleware (the same
# shape as CorrelationIdMiddleware). Every /chat turn drives a multi-agent LLM pipeline plus
# external OCR, so request volume converts directly into compute and spend — without a ceiling a
# single caller can amplify cost and degrade availability of a clinical system. This bounds each
# principal to a fixed number of requests per window and advertises the standard RateLimit-*
# headers, returning 429 with Retry-After when exceeded.
#
# In-process and single-instance: the counters live in this worker's memory and reset on restart,
# and are NOT shared across Railway replicas — the same assumption ConversationStore already makes.
# A Redis/`limits` backend is the multi-instance follow-up. This also does not detect silent
# platform-edge limiting; it is the application-level guard the pentest asked us to advertise.

# Liveness/readiness probes must never be throttled — an orchestrator polling them is not abuse, and
# a 429 there would flap the deployment.
_EXEMPT_PATHS = frozenset({"/health", "/ready"})


class RouteClass(StrEnum):
    """Which limit bucket a request counts against.

    Split so a ``CHAT`` flood (the expensive LLM path) cannot exhaust a principal's ``READ`` budget
    and vice versa — each is counted independently under its own limit.
    """

    CHAT = "chat"
    READ = "read"


@dataclass(frozen=True)
class RateDecision:
    """The outcome of one limiter check — everything the middleware needs to build its headers.

    Attributes:
        allowed: Whether the request may proceed.
        limit: The bucket's per-window ceiling (the ``RateLimit-Limit`` value).
        remaining: Requests left in the current window after this one (0 when rejected).
        reset_seconds: Whole seconds until the window frees at least one slot (``RateLimit-Reset``,
            and ``Retry-After`` when rejected).
    """

    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int


@dataclass
class SlidingWindowRateLimiter:
    """A per-key sliding-window counter, bounded in memory and testable via an injected clock.

    Each key (a ``(principal, route-class)`` pair) holds a deque of the monotonic timestamps of its
    recent requests; on each check the entries older than one window are dropped, and the request is
    allowed only when fewer than ``limit`` remain. Sliding rather than fixed-window so a caller
    cannot burst ``2 * limit`` across a window boundary.

    The key set is capped at ``max_keys`` (least-recently-used eviction) so a stream of distinct
    principals/IPs cannot grow the store without bound.
    """

    window_seconds: float
    max_keys: int
    clock: Callable[[], float] = time.monotonic
    _hits: "OrderedDict[str, deque[float]]" = field(default_factory=OrderedDict)

    def check(self, key: str, limit: int) -> RateDecision:
        """Record a request against ``key`` (if allowed) and return the resulting decision.

        Args:
            key: The bucket key, e.g. ``"<principal>:chat"``.
            limit: The maximum number of requests allowed in one window for this key.

        Returns:
            The :class:`RateDecision`. A rejected request is NOT recorded, so a throttled caller
            does not push its own window forward and starve itself indefinitely.
        """
        now = self.clock()
        cutoff = now - self.window_seconds
        hits = self._hits.get(key)
        if hits is None:
            hits = deque()
            self._hits[key] = hits
        # Drop timestamps that have aged out of the window.
        while hits and hits[0] <= cutoff:
            hits.popleft()
        self._hits.move_to_end(key)  # mark most-recently-used for LRU eviction
        self._evict_if_needed()

        if len(hits) >= limit:
            # Rejected: the window frees a slot when its oldest surviving hit ages out.
            reset = math.ceil(hits[0] + self.window_seconds - now) if hits else 0
            return RateDecision(
                allowed=False, limit=limit, remaining=0, reset_seconds=max(reset, 1)
            )
        hits.append(now)
        remaining = limit - len(hits)
        reset = math.ceil(hits[0] + self.window_seconds - now)
        return RateDecision(
            allowed=True, limit=limit, remaining=remaining, reset_seconds=max(reset, 0)
        )

    def _evict_if_needed(self) -> None:
        """Evict least-recently-used keys until the store is within ``max_keys``."""
        while len(self._hits) > self.max_keys:
            self._hits.popitem(last=False)


class RateLimitMiddleware:
    """ASGI middleware enforcing :class:`SlidingWindowRateLimiter` per principal and route class.

    Runs inside CORS and correlation (so a 429 still carries CORS + correlation-id headers) but
    outside the application. A disabled limiter, an exempt path, and a preflight ``OPTIONS`` all
    pass straight through.
    """

    def __init__(
        self,
        app: ASGIApp,
        settings: Settings,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._app = app
        self._settings = settings
        self._limiter = SlidingWindowRateLimiter(
            window_seconds=settings.rate_limit_window_seconds,
            max_keys=settings.rate_limit_max_principals,
            clock=clock,
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._settings.rate_limit_enabled:
            await self._app(scope, receive, send)
            return

        method: str = scope.get("method", "GET")
        path: str = scope.get("path", "")
        # OPTIONS is CORS preflight (answered by CORSMiddleware above us); exempt probes bypass too.
        if method == "OPTIONS" or path in _EXEMPT_PATHS:
            await self._app(scope, receive, send)
            return

        route_class = self._classify(method, path)
        limit = (
            self._settings.rate_limit_chat_per_window
            if route_class is RouteClass.CHAT
            else self._settings.rate_limit_read_per_window
        )
        key = f"{_principal(scope)}:{route_class.value}"
        decision = self._limiter.check(key, limit)

        if not decision.allowed:
            await self._reject(decision, send)
            return

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.extend(_rate_headers(decision))
            await send(message)

        await self._app(scope, receive, send_with_headers)

    @staticmethod
    def _classify(method: str, path: str) -> RouteClass:
        """Map a request to its limit bucket — the LLM-backed POST /chat is the expensive one."""
        if method == "POST" and path == "/chat":
            return RouteClass.CHAT
        return RouteClass.READ

    async def _reject(self, decision: RateDecision, send: Send) -> None:
        """Emit a 429 with Retry-After and the RateLimit-* headers, without calling the app."""
        body = json.dumps(
            {"error": "rate limit exceeded", "correlation_id": current_correlation_id()}
        ).encode()
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"retry-after", str(decision.reset_seconds).encode()),
            *_rate_headers(decision),
        ]
        await send({"type": "http.response.start", "status": 429, "headers": headers})
        await send({"type": "http.response.body", "body": body})


def _rate_headers(decision: RateDecision) -> list[tuple[bytes, bytes]]:
    """The standard ``RateLimit-*`` advertisement headers for a decision."""
    return [
        (b"ratelimit-limit", str(decision.limit).encode()),
        (b"ratelimit-remaining", str(decision.remaining).encode()),
        (b"ratelimit-reset", str(decision.reset_seconds).encode()),
    ]


def _principal(scope: Scope) -> str:
    """Identify the caller a request counts against: its SMART token, else its client IP.

    The bearer token is hashed (never stored raw) so the limiter's key set cannot leak credentials.
    A tokenless request falls back to the client IP — the first hop of ``X-Forwarded-For`` since the
    service sits behind Railway's TLS proxy, else the direct peer.

    Args:
        scope: The ASGI connection scope.

    Returns:
        A stable principal key: ``"tok:<hash>"`` or ``"ip:<address>"``.
    """
    token = _bearer_token(scope)
    if token is not None:
        digest = hashlib.sha256(token.encode()).hexdigest()[:32]
        return f"tok:{digest}"
    return f"ip:{_client_ip(scope)}"


def _bearer_token(scope: Scope) -> str | None:
    """Extract the SMART bearer token from an ASGI scope's Authorization header, if well-formed."""
    for name, value in scope.get("headers", []):
        if name == b"authorization" and value:
            scheme, _, token = bytes(value).decode().partition(" ")
            if scheme.lower() == "bearer" and token.strip():
                return token.strip()
            return None
    return None


def _client_ip(scope: Scope) -> str:
    """The caller's IP: the first X-Forwarded-For hop (behind the proxy), else the direct peer."""
    for name, value in scope.get("headers", []):
        if name == b"x-forwarded-for" and value:
            first_hop = bytes(value).decode().split(",", 1)[0].strip()
            if first_hop:
                return first_hop
    client = scope.get("client")
    if client:
        return str(client[0])
    return "unknown"
