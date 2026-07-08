from collections.abc import Callable

import httpx
from fastapi.testclient import TestClient
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from copilot.config import FhirClientMode, Settings
from copilot.fhir.fixtures import FixtureFhirClient
from copilot.main import create_app
from copilot.schemas import ChatResponse, Claim, SourceRef


async def test_medications_are_deduplicated(seed_client: FixtureFhirClient) -> None:
    # Guards the FHIR prescriptions/lists UNION reality (deployment-strategy.md): the seed has
    # metformin recorded twice (one RxNorm-coded, one text-only). If dedup breaks, the physician
    # sees the same drug twice and any UC-4 count is wrong.
    meds = await seed_client.get_medications("1")
    names = [m.name for m in meds]

    assert names.count("metformin 500 mg tablet") == 1
    metformin = next(m for m in meds if m.name == "metformin 500 mg tablet")
    # The coded variant must win the dedup so downstream cross-referencing keeps the RxNorm code.
    assert metformin.rxnorm_code == "860975"


async def test_problems_carry_status_and_code(seed_client: FixtureFhirClient) -> None:
    # Guards problem-list parsing: a claim can only cite what we project, so display/status/code
    # must survive parsing or UC-1/UC-2 lose their grounding fields.
    problems = await seed_client.get_problems("1")
    dm = next(p for p in problems if p.code == "44054006")

    assert dm.display == "Type 2 diabetes mellitus"
    assert dm.clinical_status == "active"


async def test_allergies_render_substance_and_reaction(seed_client: FixtureFhirClient) -> None:
    # Guards allergy parsing including the reaction join — UC-4's allergy cross-check cites these.
    allergies = await seed_client.get_allergies("1")

    assert len(allergies) == 1
    assert allergies[0].substance == "Penicillin"
    assert allergies[0].criticality == "high"
    assert allergies[0].reactions == "Hives"


async def test_encounters_expose_date_and_reason(seed_client: FixtureFhirClient) -> None:
    # Guards encounter metadata parsing (dates/reason) that UC-1's "recent visits" leans on.
    encounters = await seed_client.get_encounters("1")

    assert len(encounters) == 2
    assert any(e.reason == "Diabetes follow-up" for e in encounters)
    assert all(e.start_date is not None for e in encounters)


def _final_tool_name(info: AgentInfo) -> str:
    tools = getattr(info, "output_tools", None) or getattr(info, "result_tools", None) or []
    return tools[0].name if tools else "final_result"


def _scripted_orientation(tool_names: list[str], final: ChatResponse) -> FunctionModel:
    """A model that calls a sequence of tools, then returns a fixed structured answer.

    Args:
        tool_names: The tools to call, in order, before answering.
        final: The ``ChatResponse`` to return once the tools have run.

    Returns:
        A ``FunctionModel`` scripting that sequence.
    """
    state = {"step": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        step = state["step"]
        state["step"] += 1
        if step < len(tool_names):
            return ModelResponse(parts=[ToolCallPart(tool_name=tool_names[step], args={})])
        return ModelResponse(
            parts=[
                ToolCallPart(tool_name=_final_tool_name(info), args=final.model_dump(mode="json"))
            ]
        )

    return FunctionModel(respond)


def _post_chat(settings: Settings, model: FunctionModel) -> Callable[[], httpx.Response]:
    app = create_app(settings)
    client = TestClient(app)

    def call() -> httpx.Response:
        with app.state.agent.override(model=model):
            # Starlette's TestClient returns its vendored httpx Response (not importable here).
            return client.post(  # type: ignore[return-value]
                "/chat", json={"patient_id": "1", "message": "Give me the picture."}
            )

    return call


def test_uc1_orientation_grounds_across_problem_and_medication(settings: Settings) -> None:
    # Guards UC-1 end-to-end across multiple resource types: an orientation that fetches problems
    # and meds and cites one of each must pass the gate with the REAL record values stamped in.
    # If cross-resource grounding regresses, multi-tool answers would be wrongly refused.
    orientation = ChatResponse(
        summary="68F with type 2 diabetes; on metformin.",
        claims=[
            Claim(
                text="Active problem: type 2 diabetes mellitus.",
                source=SourceRef(
                    resource_type="Condition", resource_id="cond-dm2", field="display"
                ),
            ),
            Claim(
                text="Currently on metformin 500 mg.",
                source=SourceRef(
                    resource_type="MedicationRequest", resource_id="med-metformin", field="name"
                ),
            ),
        ],
    )
    response = _post_chat(
        settings, _scripted_orientation(["get_problems", "get_medications"], orientation)
    )()

    assert response.status_code == 200
    body = response.json()
    values = {c["source"]["value"] for c in body["claims"]}
    assert "Type 2 diabetes mellitus" in values
    assert "metformin 500 mg tablet" in values


def test_chat_without_a_patient_token_is_rejected_in_http_mode() -> None:
    # Guards the contract with the PHP module (deployment-strategy.md, Option D): in live HTTP mode
    # the agent must refuse a /chat call that carries no patient-scoped token BEFORE any FHIR read
    # or LLM call — the token is what binds a turn to one authorized patient. No network is touched.
    http_settings = Settings(
        fhir_client_mode=FhirClientMode.HTTP,
        fhir_base_url="https://openemr.example/apis/default/fhir",
        fhir_bearer_token=None,
        anthropic_api_key=None,
        langfuse_public_key=None,
        langfuse_secret_key=None,
    )
    with TestClient(create_app(http_settings)) as client:
        response = client.post("/chat", json={"patient_id": "1", "message": "Give me the picture."})

    assert response.status_code == 401
    assert "token" in response.json()["error"].lower()
