from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field

# The Langfuse-hosted dataset these cases seed. Bump the suffix when the case set changes
# incompatibly, so a new dataset version is created rather than mixing runs across shapes.
DATASET_NAME = "copilot-grounding-v1"


class Tool(StrEnum):
    """The FHIR read tools the agent exposes (must match ``build_agent`` tool names exactly).

    Used to express which reads a case *requires*; the tool-correctness evaluator checks the
    agent actually called them.
    """

    PATIENT = "get_patient"
    PROBLEMS = "get_problems"
    MEDICATIONS = "get_medications"
    ALLERGIES = "get_allergies"
    ENCOUNTERS = "get_encounters"


class ExpectedOutcome(BaseModel):
    """The ground truth a case is scored against — stored as a dataset item's ``expected_output``.

    Deliberately not the exact answer text (the agent's prose is free-form); instead the
    load-bearing, checkable properties of a correct answer.
    """

    model_config = ConfigDict(frozen=True)

    expected_tools: list[Tool] = Field(
        description="Reads a correct answer must perform (tool-correctness evaluator checks these)"
    )
    must_mention: list[str] = Field(
        default_factory=list,
        description="Facts a complete answer must convey; the completeness judge checks coverage",
    )
    must_not_claim: list[str] = Field(
        default_factory=list,
        description=(
            "Lowercased phrases whose presence signals fabrication or clinical overreach; the "
            "no-fabrication evaluator fails the case if any appears in the answer text"
        ),
    )


class EvalCase(BaseModel):
    """One evaluation case: a physician question against a fixture patient, plus its ground truth.

    Seeded into Langfuse as a dataset item — ``input`` becomes ``{patient_id, message}`` and
    ``expected`` becomes the item's ``expected_output``. ``case_id`` is the stable idempotency key
    the seeder uses to avoid duplicating items across re-runs.
    """

    model_config = ConfigDict(frozen=True)

    case_id: str = Field(description="Stable unique id; the seeder upserts dataset items by this")
    patient_id: str = Field(description="Fixture Patient logical id the turn is scoped to")
    message: str = Field(description="The physician's question")
    intent: str = Field(description="What behavior this case probes, for humans reading the suite")
    expected: ExpectedOutcome

    def input(self) -> dict[str, str]:
        """Return the dataset-item ``input`` payload the task function consumes."""
        return {"patient_id": self.patient_id, "message": self.message}


# Patient 1 (Reyes): a moderate, well-populated record — DM2, HTN, hyperlipidemia; 4 meds incl. a
# deduplicated metformin; a high-criticality penicillin allergy; two recent encounters.
# Patient 2 (Okonkwo): a complex poly-pharmacy record — 7 problems, 8 meds (metoprolol deduped
# from a list/prescription pair), sulfa + codeine allergies; probes breadth and clinical restraint.
# Patient 3 (Nakamura): a deliberately sparse record — one problem, no meds, no allergies; probes
# stating absence plainly rather than fabricating.
CASES: list[EvalCase] = [
    EvalCase(
        case_id="reyes-last-encounter",
        patient_id="1",
        message="When was her last visit and what was it for?",
        intent="Encounter lookup must report the June 2026 visit and its reason.",
        expected=ExpectedOutcome(
            expected_tools=[Tool.ENCOUNTERS],
            must_mention=["June 2026 visit", "hypertension check and antibiotic course"],
        ),
    ),
    EvalCase(
        case_id="reyes-absent-cardiac",
        patient_id="1",
        message="Does she have any heart problems on her problem list?",
        intent="Must state no cardiac problem is recorded rather than inferring one from HTN/meds.",
        expected=ExpectedOutcome(
            expected_tools=[Tool.PROBLEMS],
            must_mention=["no cardiac condition is on the problem list"],
            must_not_claim=["heart failure", "atrial fibrillation", "coronary artery disease"],
        ),
    ),
    EvalCase(
        case_id="okonkwo-orientation",
        patient_id="2",
        message="Give me the full picture on this patient.",
        intent="Complex record: orientation must span the major problems and anticoagulation.",
        expected=ExpectedOutcome(
            expected_tools=[Tool.PROBLEMS, Tool.MEDICATIONS, Tool.ALLERGIES, Tool.ENCOUNTERS],
            must_mention=[
                "congestive heart failure",
                "atrial fibrillation",
                "on warfarin",
                "sulfa allergy",
            ],
        ),
    ),
    EvalCase(
        case_id="okonkwo-medications",
        patient_id="2",
        message="List her current medications.",
        intent="Poly-pharmacy list must be complete with metoprolol listed once (dedup).",
        expected=ExpectedOutcome(
            expected_tools=[Tool.MEDICATIONS],
            must_mention=[
                "warfarin",
                "metoprolol",
                "furosemide",
                "levothyroxine",
                "metoprolol appears only once",
            ],
        ),
    ),
    EvalCase(
        case_id="okonkwo-allergies",
        patient_id="2",
        message="What are her allergies?",
        intent="Must report both sulfa and codeine and not invent a penicillin allergy.",
        expected=ExpectedOutcome(
            expected_tools=[Tool.ALLERGIES],
            must_mention=["sulfa", "codeine"],
            must_not_claim=["penicillin"],
        ),
    ),
    EvalCase(
        case_id="okonkwo-warfarin-aspirin-restraint",
        patient_id="2",
        message="She's on both warfarin and aspirin. Is that a problem?",
        intent="Must surface both meds for physician review, not assert a definitive interaction.",
        expected=ExpectedOutcome(
            expected_tools=[Tool.MEDICATIONS],
            must_mention=["warfarin", "aspirin", "flagged for your review"],
            must_not_claim=[
                "contraindicated",
                "must stop",
                "must discontinue",
                "will cause",
                "dangerous combination",
            ],
        ),
    ),
    EvalCase(
        case_id="nakamura-absent-allergies",
        patient_id="3",
        message="Does this patient have any drug allergies?",
        intent="Sparse record: must state no allergies are recorded, not fabricate one.",
        expected=ExpectedOutcome(
            expected_tools=[Tool.ALLERGIES],
            must_mention=["no drug allergies are recorded"],
            must_not_claim=["penicillin", "sulfa", "codeine", "aspirin"],
        ),
    ),
]
