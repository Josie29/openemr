from collections.abc import Callable

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


def _post_chat(settings: Settings, model: FunctionModel) -> Callable[[], object]:
    app = create_app(settings)
    client = TestClient(app)

    def call() -> object:
        with app.state.agent.override(model=model):
            return client.post("/chat", json={"patient_id": "1", "message": "Who is this patient?"})

    return call


def test_grounded_answer_reaches_the_physician(settings: Settings) -> None:
    # Guards the happy path: an answer whose every claim cites the Patient resource the tool
    # returned this turn must pass the gate and reach the caller intact.
    grounded = ChatResponse(
        summary="Marisol Reyes, 68F.",
        claims=[
            Claim(
                text="Patient is female, born 1958-03-12.",
                source=SourceRef(resource_type="Patient", resource_id="1", field="birthDate"),
            )
        ],
    )
    response = _post_chat(settings, _scripted_model(grounded))()

    assert response.status_code == 200
    body = response.json()
    assert body["claims"], "a grounded answer must carry its claims"
    assert all(c["source"]["resource_type"] == "Patient" for c in body["claims"])


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
