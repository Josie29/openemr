import httpx
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from copilot.config import Settings
from copilot.conversation import ConversationStore
from copilot.main import create_app
from copilot.schemas import ChatResponse, Claim, SourceRef

# One claim citing a problem-list Condition; reused across turns to prove the FetchLog persists.
_DM_CLAIM = Claim(
    text="Active problem: type 2 diabetes mellitus.",
    source=SourceRef(resource_type="Condition", resource_id="cond-dm2", field="display"),
)


def _final_tool_name(info: AgentInfo) -> str:
    tools = getattr(info, "output_tools", None) or getattr(info, "result_tools", None) or []
    return tools[0].name if tools else "final_result"


def _scripted(actions: list[tuple[str, object]]) -> FunctionModel:
    """A model that consumes a fixed action script across (possibly several) agent runs.

    Args:
        actions: Ordered steps — ``("tool", name)`` calls a tool, ``("final", ChatResponse)`` emits
            the structured answer. State persists across turns, so index 2 is turn 2's first step.

    Returns:
        A ``FunctionModel`` replaying that script.
    """
    state = {"i": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        kind, payload = actions[state["i"]]
        state["i"] += 1
        if kind == "tool":
            return ModelResponse(parts=[ToolCallPart(tool_name=str(payload), args={})])
        assert isinstance(payload, ChatResponse)
        return ModelResponse(
            parts=[
                ToolCallPart(tool_name=_final_tool_name(info), args=payload.model_dump(mode="json"))
            ]
        )

    return FunctionModel(respond)


def _client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings))


def _post(client: TestClient, body: dict[str, object]) -> httpx.Response:
    # Starlette's TestClient returns its vendored httpx Response (not importable here).
    return client.post("/chat", json=body)  # type: ignore[return-value]


def test_followup_can_cite_a_resource_fetched_in_an_earlier_turn(settings: Settings) -> None:
    # THE load-bearing multi-turn test: turn 2 calls NO tool yet cites the Condition fetched in
    # turn 1. It can only ground if the FetchLog accumulates across the conversation. If someone
    # reverts to a per-turn FetchLog, turn 2's answer is wrongly refused and this fails.
    turn1 = ChatResponse(summary="She has diabetes.", claims=[_DM_CLAIM])
    turn2 = ChatResponse(summary="Yes — type 2, still active.", claims=[_DM_CLAIM])
    model = _scripted([("tool", "get_problems"), ("final", turn1), ("final", turn2)])

    app = create_app(settings)
    client = TestClient(app)
    with app.state.agent.override(model=model):
        first = client.post("/chat", json={"patient_id": "1", "message": "What's on her list?"})
        cid = first.json()["conversation_id"]
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
    # Turn 2 grounded against turn 1's fetch — the real record value is stamped in.
    assert second.json()["claims"][0]["source"]["value"] == "Type 2 diabetes mellitus"


def test_new_turn_returns_a_conversation_id(settings: Settings) -> None:
    # Guards the contract with the module: every answered turn hands back an id to continue on.
    model = _scripted([("final", ChatResponse(summary="No problems on file.", claims=[]))])
    app = create_app(settings)
    with app.state.agent.override(model=model):
        response = TestClient(app).post("/chat", json={"patient_id": "1", "message": "hi"})

    assert response.status_code == 200
    assert response.json()["conversation_id"]


def test_reusing_a_conversation_for_a_different_patient_is_refused(settings: Settings) -> None:
    # Guards the security boundary: a conversation is bound to one patient, so a follow-up naming
    # a different patient must be refused (403) rather than leak another patient's accrued history.
    model = _scripted([("final", ChatResponse(summary="ok", claims=[]))])
    app = create_app(settings)
    client = TestClient(app)
    with app.state.agent.override(model=model):
        first = client.post("/chat", json={"patient_id": "1", "message": "hi"})
        cid = first.json()["conversation_id"]
        crossed = client.post(
            "/chat", json={"patient_id": "2", "message": "hi", "conversation_id": cid}
        )

    assert crossed.status_code == 403


def test_unknown_conversation_id_is_404(settings: Settings) -> None:
    # Guards that a stale/forged id doesn't silently start a fresh thread under the same id.
    response = _post(
        _client(settings), {"patient_id": "1", "message": "hi", "conversation_id": "nope"}
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
