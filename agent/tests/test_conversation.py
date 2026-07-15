from fastapi.testclient import TestClient

from copilot.config import Settings
from copilot.conversation import ConversationStore
from copilot.graph.outputs import ExtractorOutput
from copilot.graph.routing import Route
from copilot.main import create_app
from copilot.schemas import ChatResponse, Claim, SourceRef
from graph_script import override_graph, route_model, worker_model

# One claim citing a problem-list Condition; reused across turns to prove the FetchLog persists.
_DM_CLAIM = Claim(
    text="Active problem: type 2 diabetes mellitus.",
    source=SourceRef(resource_type="Condition", resource_id="cond-dm2", field="display"),
)
_EMPTY = ChatResponse(summary="No problems on file.", claims=[])


def test_followup_can_cite_a_resource_fetched_in_an_earlier_turn(settings: Settings) -> None:
    # THE load-bearing multi-turn test: turn 2 routes straight to ANSWER and cites the Condition
    # fetched in turn 1 without re-reading it. It can only ground if the FetchLog accumulates across
    # the conversation. If someone reverts to a per-turn FetchLog, turn 2 is wrongly refused.
    app = create_app(settings)
    client = TestClient(app)

    with override_graph(
        app.state.graph,
        router=route_model([Route.EXTRACT_INTAKE, Route.ANSWER]),
        extractor=worker_model(
            [("get_problems", {})], ExtractorOutput(summary="DM", claims=[_DM_CLAIM])
        ),
        answerer=worker_model([], ChatResponse(summary="She has diabetes.", claims=[_DM_CLAIM])),
    ):
        first = client.post("/chat", json={"patient_id": "1", "message": "What's on her list?"})
    cid = first.json()["conversation_id"]

    # Turn 2: no worker runs (route straight to ANSWER); the answerer grounds against the FetchLog
    # accumulated in turn 1.
    with override_graph(
        app.state.graph,
        router=route_model([Route.ANSWER]),
        answerer=worker_model([], ChatResponse(summary="Yes — still active.", claims=[_DM_CLAIM])),
    ):
        second = client.post(
            "/chat",
            json={
                "patient_id": "1",
                "message": "Is the diabetes still active?",
                "conversation_id": cid,
            },
        )

    assert first.status_code == 200
    assert cid
    assert second.status_code == 200
    assert second.json()["claims"][0]["source"]["value"] == "Type 2 diabetes mellitus"


def test_new_turn_returns_a_conversation_id(settings: Settings) -> None:
    # Guards the contract with the module: every answered turn hands back an id to continue on.
    app = create_app(settings)
    with override_graph(
        app.state.graph,
        router=route_model([Route.ANSWER]),
        answerer=worker_model([], _EMPTY),
    ):
        response = TestClient(app).post("/chat", json={"patient_id": "1", "message": "hi"})

    assert response.status_code == 200
    assert response.json()["conversation_id"]


def test_reusing_a_conversation_for_a_different_patient_is_refused(settings: Settings) -> None:
    # Guards the security boundary: a conversation is bound to one patient, so a follow-up naming a
    # different patient must be refused (403) rather than leak another patient's accrued history.
    app = create_app(settings)
    client = TestClient(app)
    with override_graph(
        app.state.graph,
        router=route_model([Route.ANSWER]),
        answerer=worker_model([], _EMPTY),
    ):
        first = client.post("/chat", json={"patient_id": "1", "message": "hi"})
        cid = first.json()["conversation_id"]
        crossed = client.post(
            "/chat", json={"patient_id": "2", "message": "hi", "conversation_id": cid}
        )

    assert crossed.status_code == 403


def test_unknown_conversation_id_is_404(settings: Settings) -> None:
    # Guards that a stale/forged id doesn't silently start a fresh thread under the same id.
    response = TestClient(create_app(settings)).post(
        "/chat", json={"patient_id": "1", "message": "hi", "conversation_id": "nope"}
    )
    assert response.status_code == 404


def test_store_expires_a_session_after_its_ttl() -> None:
    # Guards the memory bound / staleness policy: a session past its TTL is gone on next access.
    now = {"t": 0.0}
    store = ConversationStore(ttl_seconds=100, max_sessions=10, clock=lambda: now["t"])
    cid, _ = store.create("1")

    now["t"] = 50.0
    assert store.get(cid) is not None  # within TTL; access refreshes last_used to 50

    now["t"] = 200.0  # 200 - 50 = 150 > 100
    assert store.get(cid) is None
