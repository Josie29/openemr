from collections.abc import Callable

import httpx
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from copilot.config import Settings
from copilot.main import create_app
from copilot.schemas import ChatResponse, Claim, SourceRef

# NOTE (flagged per implementation-prompt-01 §5): these two tests drive the agent with a
# scripted FunctionModel, so they depend on Pydantic AI's message/AgentInfo API. The pinned
# version's attribute names (output_tools vs result_tools) are handled defensively below; if
# the import surface differs on the installed version, adjust here — the behavior asserted
# (grounded answers pass, ungrounded answers are refused) is the contract that must hold.


def _final_tool_name(info: AgentInfo) -> str:
    """Return the structured-output tool name for the current Pydantic AI version."""
    tools = getattr(info, "output_tools", None) or getattr(info, "result_tools", None) or []
    return tools[0].name if tools else "final_result"


def _scripted_model(final: ChatResponse) -> FunctionModel:
    """A model that reads the patient once, then returns a fixed structured answer.

    Args:
        final: The ``ChatResponse`` the model should ultimately return.

    Returns:
        A ``FunctionModel`` that first calls ``get_patient`` then emits ``final``.
    """
    state = {"fetched": False}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not state["fetched"]:
            state["fetched"] = True
            return ModelResponse(parts=[ToolCallPart(tool_name="get_patient", args={})])
        args = final.model_dump(mode="json")
        return ModelResponse(parts=[ToolCallPart(tool_name=_final_tool_name(info), args=args)])

    return FunctionModel(respond)


def _post_chat(settings: Settings, model: FunctionModel) -> Callable[[], httpx.Response]:
    app = create_app(settings)
    client = TestClient(app)

    def call() -> httpx.Response:
        with app.state.agent.override(model=model):
            # Starlette's TestClient returns its vendored httpx Response (not importable here).
            return client.post(  # type: ignore[return-value]
                "/chat", json={"patient_id": "1", "message": "Who is this patient?"}
            )

    return call


def test_grounded_answer_reaches_the_physician(settings: Settings) -> None:
    # Guards the happy path: an answer whose every claim cites the Patient resource the tool
    # returned this turn must pass the gate and reach the caller intact.
    grounded = ChatResponse(
        summary="Marisol Reyes, 68F.",
        claims=[
            Claim(
                text="Patient was born 1958-03-12.",
                source=SourceRef(resource_type="Patient", resource_id="1", field="birth_date"),
            )
        ],
    )
    response = _post_chat(settings, _scripted_model(grounded))()

    assert response.status_code == 200
    body = response.json()
    assert body["claims"], "a grounded answer must carry its claims"
    # The gate stamps the real record value into the citation (code-populated, not model-set).
    assert body["claims"][0]["source"]["value"] == "1958-03-12"


def test_ungrounded_answer_is_refused_not_returned(settings: Settings) -> None:
    # Guards the core safety property end-to-end: a fabricated claim dressed up with a citation
    # to a resource that was never fetched must NOT reach the physician — the gate exhausts its
    # retries and the endpoint degrades to an explicit "cannot attribute" answer.
    fabricated = ChatResponse(
        summary="A1c was 9.2% last week.",
        claims=[
            Claim(
                text="A1c was 9.2% last week.",
                source=SourceRef(resource_type="Observation", resource_id="999"),  # never fetched
            )
        ],
    )
    response = _post_chat(settings, _scripted_model(fabricated))()

    assert response.status_code == 200
    body = response.json()
    assert body["claims"] == []
    assert "attribute" in body["summary"].lower()


def test_runaway_tool_loop_is_capped_and_refused(settings: Settings) -> None:
    # Guards the cost/latency ceiling behind the prod "Failed to fetch": an agent that loops a tool
    # (get_encounter_note was called ~48x on a 90+-encounter chart, hitting pydantic-ai's default
    # request_limit of 50) must be stopped at the tool-call cap and degrade to a refusal — never a
    # 500 the browser surfaces as "Failed to fetch". Without the cap + catch, this turn would 500.
    capped = settings.model_copy(update={"agent_tool_calls_limit": 3})

    def loop(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        # Never emit a final answer — keep calling a tool so the turn runs into the cap.
        return ModelResponse(parts=[ToolCallPart(tool_name="get_encounters", args={})])

    response = _post_chat(capped, FunctionModel(loop))()

    assert response.status_code == 200
    body = response.json()
    assert body["claims"] == []
    assert "attribute" in body["summary"].lower()
