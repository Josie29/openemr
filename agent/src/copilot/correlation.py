import uuid
from contextvars import ContextVar

from starlette.types import ASGIApp, Message, Receive, Scope, Send

CORRELATION_HEADER = "x-correlation-id"

_correlation_id: ContextVar[str] = ContextVar("correlation_id", default="")


def current_correlation_id() -> str:
    """Return the correlation id bound to the request currently being handled.

    Returns:
        The correlation id, or an empty string outside a request scope.
    """
    return _correlation_id.get()


class CorrelationIdMiddleware:
    """ASGI middleware that stamps every request with a correlation id (ARCHITECTURE.md §10).

    Accepts an inbound ``X-Correlation-ID`` header and generates one when absent, binds it to
    a context variable so every log line, tool call, and LLM call this turn can carry it, and
    echoes it back on the response so a full trace reconstructs from logs alone.
    """

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        correlation_id = _extract_header(scope) or uuid.uuid4().hex
        token = _correlation_id.set(correlation_id)

        async def send_with_header(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = message.setdefault("headers", [])
                headers.append((CORRELATION_HEADER.encode(), correlation_id.encode()))
            await send(message)

        try:
            await self._app(scope, receive, send_with_header)
        finally:
            _correlation_id.reset(token)


def _extract_header(scope: Scope) -> str | None:
    """Extract an inbound correlation-id header from an ASGI scope, if present.

    Args:
        scope: The ASGI connection scope.

    Returns:
        The header value, or None if the client did not send one.
    """
    target = CORRELATION_HEADER.encode()
    for name, value in scope.get("headers", []):
        if name == target and value:
            return value.decode()
    return None
