from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.testclient import TestClient

from copilot.config import Settings
from copilot.correlation import CorrelationIdMiddleware
from copilot.rate_limit import RateLimitMiddleware, SlidingWindowRateLimiter

# AF-VULN-0002: the Co-Pilot must bound per-principal request volume and advertise it, so a caller
# cannot amplify LLM/OCR cost or degrade availability without limit. These tests pin the behavior
# the pentest asked for — a 429 with Retry-After past the ceiling, standard RateLimit-* headers on
# every response, per-principal isolation, and untouched liveness probes.


class _FakeClock:
    """A hand-advanced monotonic clock so window expiry is deterministic in tests."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_limiter_allows_up_to_the_ceiling_then_rejects() -> None:
    """The Nth+1 request in a window is refused; the refusal is not itself counted.

    Breaks if the window off-by-ones (allowing limit+1) or if a rejected request advances the window
    so a throttled caller can never recover.
    """
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(window_seconds=60.0, max_keys=100, clock=clock)

    assert limiter.check("p:chat", limit=2).allowed
    assert limiter.check("p:chat", limit=2).allowed
    first_reject = limiter.check("p:chat", limit=2)
    assert not first_reject.allowed
    assert first_reject.remaining == 0
    assert first_reject.reset_seconds >= 1
    # Still rejected while the window holds; the rejects did not push the window forward.
    assert not limiter.check("p:chat", limit=2).allowed


def test_limiter_window_slides_with_the_clock() -> None:
    """Once the oldest hit ages past the window, a slot frees — a fixed cap that recovers over time.

    Breaks if timestamps never expire (a caller is throttled forever after one burst).
    """
    clock = _FakeClock()
    limiter = SlidingWindowRateLimiter(window_seconds=60.0, max_keys=100, clock=clock)
    assert limiter.check("p:chat", limit=1).allowed
    assert not limiter.check("p:chat", limit=1).allowed
    clock.now += 61  # advance past the window
    assert limiter.check("p:chat", limit=1).allowed


def test_limiter_keys_are_independent() -> None:
    """One principal exhausting its budget does not affect another — the limit is per key."""
    limiter = SlidingWindowRateLimiter(window_seconds=60.0, max_keys=100, clock=_FakeClock())
    assert limiter.check("a:chat", limit=1).allowed
    assert not limiter.check("a:chat", limit=1).allowed
    assert limiter.check("b:chat", limit=1).allowed  # different principal, own budget


def test_limiter_evicts_least_recently_used_keys() -> None:
    """The tracked key set is capped so a flood of distinct IPs cannot exhaust memory."""
    limiter = SlidingWindowRateLimiter(window_seconds=60.0, max_keys=2, clock=_FakeClock())
    limiter.check("a", limit=5)
    limiter.check("b", limit=5)
    limiter.check("c", limit=5)  # evicts "a" (least recently used)
    assert set(limiter._hits.keys()) == {"b", "c"}


def _app(clock: _FakeClock, **overrides: object) -> FastAPI:
    """A minimal app carrying the same middleware stack + order as create_app, for integration."""
    kwargs: dict[str, object] = {
        "rate_limit_enabled": True,
        "rate_limit_window_seconds": 60.0,
        "rate_limit_chat_per_window": 2,
        "rate_limit_read_per_window": 3,
        "anthropic_api_key": None,
        "langfuse_public_key": None,
        "langfuse_secret_key": None,
        **overrides,
    }
    settings = Settings(**kwargs)  # type: ignore[arg-type]
    app = FastAPI()

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "alive"}

    @app.post("/chat")
    async def chat() -> dict[str, str]:
        return {"ok": "1"}

    @app.get("/documents")
    async def documents() -> dict[str, str]:
        return {"ok": "1"}

    app.add_middleware(RateLimitMiddleware, settings=settings, clock=clock)
    app.add_middleware(CorrelationIdMiddleware)
    app.add_middleware(
        CORSMiddleware, allow_origins=["http://localhost:8301"], allow_methods=["GET", "POST"]
    )
    return app


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_chat_burst_returns_429_with_retry_after_and_headers() -> None:
    """Past the /chat ceiling the caller gets 429 + Retry-After; every response advertises a limit.

    This is the exact gap AF-VULN-0002 reported: no ceiling and no RateLimit-* headers on the
    LLM-backed endpoint.
    """
    client = TestClient(_app(_FakeClock()))
    r1 = client.post("/chat", headers=_auth("tok-a"))
    r2 = client.post("/chat", headers=_auth("tok-a"))
    r3 = client.post("/chat", headers=_auth("tok-a"))

    assert r1.status_code == 200
    assert r1.headers["RateLimit-Limit"] == "2"
    assert r1.headers["RateLimit-Remaining"] == "1"
    assert r2.status_code == 200
    assert r3.status_code == 429
    assert r3.json()["error"] == "rate limit exceeded"
    assert int(r3.headers["Retry-After"]) >= 1
    assert r3.headers["RateLimit-Remaining"] == "0"


def test_chat_and_read_budgets_are_separate() -> None:
    """A /chat flood does not consume the read budget — the buckets are split by route class."""
    client = TestClient(_app(_FakeClock()))
    client.post("/chat", headers=_auth("tok-a"))
    client.post("/chat", headers=_auth("tok-a"))
    assert client.post("/chat", headers=_auth("tok-a")).status_code == 429
    # Reads for the same principal still have their own (looser) budget.
    assert client.get("/documents", headers=_auth("tok-a")).status_code == 200


def test_distinct_tokens_get_independent_budgets() -> None:
    """Two SMART tokens are two principals — one hitting its cap never throttles the other."""
    client = TestClient(_app(_FakeClock()))
    client.post("/chat", headers=_auth("tok-a"))
    client.post("/chat", headers=_auth("tok-a"))
    assert client.post("/chat", headers=_auth("tok-a")).status_code == 429
    assert client.post("/chat", headers=_auth("tok-b")).status_code == 200


def test_health_probe_is_never_throttled() -> None:
    """Liveness must not 429 — an orchestrator polling it is not abuse; a 429 would flap deploys."""
    client = TestClient(_app(_FakeClock()))
    for _ in range(10):
        assert client.get("/health").status_code == 200


def test_429_still_carries_cors_headers() -> None:
    """A rejection sits inside CORS, so the browser sees the response instead of a CORS error."""
    client = TestClient(_app(_FakeClock()))
    origin = {"Origin": "http://localhost:8301"}
    client.post("/chat", headers={**_auth("tok-a"), **origin})
    client.post("/chat", headers={**_auth("tok-a"), **origin})
    rejected = client.post("/chat", headers={**_auth("tok-a"), **origin})
    assert rejected.status_code == 429
    assert rejected.headers["access-control-allow-origin"] == "http://localhost:8301"


def test_disabled_limiter_passes_everything_through() -> None:
    """With rate limiting off, no request is throttled and no RateLimit-* header is added."""
    client = TestClient(_app(_FakeClock(), rate_limit_enabled=False))
    for _ in range(5):
        r = client.post("/chat", headers=_auth("tok-a"))
        assert r.status_code == 200
    assert "RateLimit-Limit" not in r.headers
