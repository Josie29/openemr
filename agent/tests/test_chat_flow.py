import httpx
from fastapi.testclient import TestClient

from copilot.config import Settings
from copilot.graph.outputs import ExtractorOutput
from copilot.graph.routing import Route
from copilot.main import create_app
from copilot.schemas import ChatResponse, Claim, SourceRef
from graph_script import (
    looping_tool_model,
    override_graph,
    raising_model,
    route_model,
    worker_model,
)

# These tests drive the supervisor graph via /chat with scripted models (no live LLM). The behavior
# asserted — a grounded answer reaches the physician, an ungrounded one is refused, a runaway worker
# is capped, and an unexpected error is contained — is the safety contract that must hold whether
# /chat runs one agent (Week 1) or the supervisor graph (Week 2).

_BIRTH_CLAIM = Claim(
    text="Patient was born 1958-03-12.",
    source=SourceRef(resource_type="Patient", resource_id="1", field="birth_date"),
)


def _post(app: object, message: str = "Who is this patient?") -> httpx.Response:
    client = TestClient(app)  # type: ignore[arg-type]
    return client.post(  # type: ignore[return-value]
        "/chat", json={"patient_id": "1", "message": message}
    )


def test_grounded_answer_reaches_the_physician(settings: Settings) -> None:
    # Guards the happy path end to end: the extractor reads the Patient, the answerer restates the
    # claim, and it passes both gates and reaches the caller with the real record value stamped —
    # plus the canonical wire citation the sidebar consumes.
    app = create_app(settings)
    with override_graph(
        app.state.graph,
        router=route_model([Route.EXTRACT_INTAKE, Route.ANSWER]),
        extractor=worker_model(
            [("get_patient", {})], ExtractorOutput(summary="68F", claims=[_BIRTH_CLAIM])
        ),
        answerer=worker_model(
            [], ChatResponse(summary="Marisol Reyes, 68F.", claims=[_BIRTH_CLAIM])
        ),
    ):
        response = _post(app)

    assert response.status_code == 200
    body = response.json()
    assert body["claims"], "a grounded answer must carry its claims"
    assert body["claims"][0]["source"]["value"] == "1958-03-12"  # stamped from the record
    assert body["claims"][0]["citations"][0]["source_type"] == "openemr_record"  # wire citation


def test_ungrounded_answer_is_refused_not_returned(settings: Settings) -> None:
    # Guards the core safety property end to end: a fabricated claim citing a resource that was
    # never fetched must NOT reach the physician — the extractor's gate exhausts its retries and the
    # endpoint degrades to an explicit "cannot attribute" answer rather than shipping it.
    fabricated = ExtractorOutput(
        summary="A1c was 9.2%.",
        claims=[
            Claim(
                text="A1c was 9.2% last week.",
                source=SourceRef(resource_type="Observation", resource_id="999"),  # never fetched
            )
        ],
    )
    app = create_app(settings)
    with override_graph(
        app.state.graph,
        router=route_model([Route.EXTRACT_INTAKE]),
        extractor=worker_model([], fabricated),
    ):
        response = _post(app)

    assert response.status_code == 200
    body = response.json()
    assert body["claims"] == []
    assert "attribute" in body["summary"].lower()


def test_runaway_tool_loop_is_capped_and_refused(settings: Settings) -> None:
    # Guards the cost/latency ceiling behind the prod "Failed to fetch": a worker that loops a tool
    # must be stopped at the tool-call cap and degrade to a refusal — never a 500. Without the cap +
    # catch, this turn would run away and surface to the browser as a bare failure.
    capped = settings.model_copy(update={"agent_tool_calls_limit": 3})
    app = create_app(capped)
    with override_graph(
        app.state.graph,
        router=route_model([Route.EXTRACT_INTAKE]),
        extractor=looping_tool_model("get_encounters"),
    ):
        response = _post(app)

    assert response.status_code == 200
    body = response.json()
    assert body["claims"] == []
    assert "attribute" in body["summary"].lower()


def test_unexpected_error_is_caught_not_leaked(settings: Settings) -> None:
    # Guards the catch-all boundary: any unforeseen failure (here the router raising) must return a
    # controlled error response — never an uncaught exception the browser shows as a bare 500 — and
    # must not leak internal detail into the user-facing body.
    app = create_app(settings)
    with override_graph(
        app.state.graph,
        router=raising_model(RuntimeError("internal detail that must not leak")),
    ):
        response = _post(app)

    assert response.status_code == 500
    body = response.json()
    assert "could not be completed" in body["error"]
    assert "internal detail" not in str(body)  # the exception message never reaches the client


def test_refusal_still_returns_a_conversation_id(settings: Settings) -> None:
    # A refusal must still return 200 with a conversation id — the module keeps the thread going
    # regardless of outcome. Guards that the graph refusal path preserves the id contract.
    unfetched = ExtractorOutput(
        summary="bad",
        claims=[Claim(text="x", source=SourceRef(resource_type="Observation", resource_id="0"))],
    )
    app = create_app(settings)
    with override_graph(
        app.state.graph,
        router=route_model([Route.EXTRACT_INTAKE]),
        extractor=worker_model([], unfetched),
    ):
        response = _post(app)

    assert response.status_code == 200
    assert response.json()["conversation_id"]
