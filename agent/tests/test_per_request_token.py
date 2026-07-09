import httpx
import pytest
from fastapi import Request
from fastapi.responses import JSONResponse

from copilot.config import FhirClientMode, ModelTier, Settings
from copilot.fhir.client import FhirError, HttpFhirClient
from copilot.main import _bearer_token, _resolve_request_fhir, create_app

# These tests guard ARCHITECTURE.md §5's load-bearing claim: the agent reads FHIR under the SMART
# token of whoever is asking, so a patient/*.read token binds the turn to exactly one patient. If
# they are removed, the agent can silently regress to using one static env-var token for every
# caller — which enforces no per-patient scoping at all and makes the IDOR argument false in
# production while still passing every other test in this suite.

_FHIR_BASE = "https://openemr.example/apis/default/fhir"


def _http_settings() -> Settings:
    """Settings for live-FHIR mode with a static fallback token configured."""
    return Settings(
        model_tier=ModelTier.SONNET,
        fhir_client_mode=FhirClientMode.HTTP,
        fhir_base_url=_FHIR_BASE,
        fhir_bearer_token="static-fallback-token",
        anthropic_api_key=None,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )


def _request_with_headers(headers: dict[str, str]) -> Request:
    """Build a bare ASGI Request carrying the given headers."""
    raw = [(k.lower().encode(), v.encode()) for k, v in headers.items()]
    return Request({"type": "http", "method": "POST", "path": "/chat", "headers": raw})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("http://localhost:8301", ["http://localhost:8301"]),
        ("http://a.example, https://b.example", ["http://a.example", "https://b.example"]),
        ("", []),
    ],
)
def test_cors_origins_parse_from_a_plain_env_string(
    monkeypatch: pytest.MonkeyPatch, raw: str, expected: list[str]
) -> None:
    # pydantic-settings JSON-decodes complex fields inside the env source, *before* validators run,
    # so without the NoDecode annotation a bare "http://localhost:8301" raises SettingsError and the
    # service dies at startup. This is exactly how the browser origin is supplied in every
    # deployment, so the failure would be total and would only show up on boot.
    monkeypatch.setenv("COPILOT_CORS_ORIGINS", raw)
    assert Settings().cors_origins == expected


def test_cors_origins_default_to_empty_not_wildcard(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail closed: an unconfigured service must permit no browser origin at all. A "*" default would
    # let any page on the internet spend a stolen SMART token against /chat.
    monkeypatch.delenv("COPILOT_CORS_ORIGINS", raising=False)
    assert Settings().cors_origins == []


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        ("Bearer abc123", "abc123"),
        ("bearer abc123", "abc123"),
        ("Basic abc123", None),
        ("Bearer ", None),
        ("abc123", None),
    ],
)
def test_bearer_token_parsing(header: str, expected: str | None) -> None:
    # A malformed Authorization header must yield no token rather than a garbage one, so the
    # caller falls back to the static token (or is rejected) instead of sending "Basic abc123".
    assert _bearer_token(_request_with_headers({"authorization": header})) == expected


def test_no_header_falls_back_to_the_static_token_when_configured() -> None:
    # With no caller token but a static token configured (dev / walking-skeleton), the turn uses the
    # static token rather than being rejected. NOTE: production omits the static token, so a
    # tokenless /chat is rejected with 401 instead (see test_tools.py). This fallback is a dev-only
    # escape hatch — the moment COPILOT_FHIR_BEARER_TOKEN is unset, the service fails closed.
    resolved = _resolve_request_fhir(_request_with_headers({}), _http_settings(), "cid")
    assert not isinstance(resolved, JSONResponse)
    client, _ = resolved
    assert client._client.headers["Authorization"] == "Bearer static-fallback-token"


def test_empty_token_sends_no_authorization_header() -> None:
    # httpx rejects "Bearer " (trailing space) as an illegal header value, so a client built without
    # a static token would fail *every* request — including the unauthenticated /metadata probe that
    # /ready depends on. Since the recommended production posture is no static token at all, this
    # would have pinned /ready red forever.
    client = HttpFhirClient(_FHIR_BASE, "", timeout_seconds=10.0, max_retries=1)
    assert "Authorization" not in client._client.headers


def test_fixture_mode_ignores_the_callers_token() -> None:
    # In fixture mode there is no live FHIR server to authorize against, so a stray Authorization
    # header must not cause the service to build an HTTP client pointed at nothing — the shared
    # fixture client serves the read and there is no per-request client to close.
    settings = Settings(
        model_tier=ModelTier.SONNET,
        fhir_client_mode=FhirClientMode.FIXTURE,
        anthropic_api_key=None,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )
    app = create_app(settings)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/chat",
        "headers": [(b"authorization", b"Bearer patient-scoped-token")],
        "app": app,
    }
    resolved = _resolve_request_fhir(Request(scope), settings, "cid")

    assert not isinstance(resolved, JSONResponse)
    client, per_request = resolved
    assert client is app.state.fhir  # the shared fixture client, not one built from the header
    assert per_request is None  # nothing to close


async def test_callers_token_reaches_the_fhir_server() -> None:
    # The whole point, asserted on the wire: the token on the *request* — not the one in the
    # environment — authorizes this turn's FHIR reads. A regression that stored the caller's token
    # but kept sending the static one upstream would pass every other test here.
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization", "")
        # A denied cross-patient read comes back as a bare 500, not a 403; any non-2xx is a denial.
        return httpx.Response(500, json={"message": "patient id invalid"})

    request = _request_with_headers({"authorization": "Bearer patient-scoped-token"})
    resolved = _resolve_request_fhir(request, _http_settings(), "cid")
    assert not isinstance(resolved, JSONResponse)
    client, _ = resolved

    # HttpFhirClient builds its own AsyncClient, so there is no transport seam to inject through.
    # Swap it, preserving the headers the constructor set — those are exactly what we are testing.
    client._client = httpx.AsyncClient(
        base_url=_FHIR_BASE,
        headers=dict(client._client.headers),
        transport=httpx.MockTransport(handler),
    )

    # Fail-closed on the non-standard 500 denial surface, rather than parsing an error body as data.
    with pytest.raises(FhirError):
        await client.get_patient("a-patient-this-token-cannot-read")

    assert seen["authorization"] == "Bearer patient-scoped-token"
    await client.aclose()
