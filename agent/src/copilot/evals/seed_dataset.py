import logging

from langfuse import get_client
from langfuse.api.commons.types.dataset_status import DatasetStatus
from pydantic import BaseModel, Field

from copilot.evals.cases import CASES, CI_CASES, CI_DATASET_NAME, DATASET_NAME, EvalCase

logger = logging.getLogger("copilot.evals.seed")

# Built from the value rather than written as `DatasetStatus.ACTIVE`: the SDK's own StrEnum shim
# (`langfuse/api/core/enum.py`) is defined behind a `sys.version_info` branch, and mypy resolves its
# members to plain `str` — which then fails the SDK's own `status: DatasetStatus | None` signature.
# The constructor form is identical at runtime and types correctly.
_ACTIVE = DatasetStatus("ACTIVE")
_ARCHIVED = DatasetStatus("ARCHIVED")


class SeedOutcome(BaseModel):
    """What one dataset's reconcile did, for the operator's log line."""

    upserted: int = Field(description="Cases written from the repo (created or updated in place)")
    archived: int = Field(
        description="Hosted items retired because the repo no longer defines them"
    )

_DESCRIPTION = (
    "Clinical Co-Pilot graph golden set: physician questions against fixture patients, scored on "
    "the boolean rubrics schema_valid, citation_present, factually_consistent, safe_refusal, "
    "no_phi_in_logs."
)


def _item_id(dataset_name: str, case_id: str) -> str:
    """The deterministic Langfuse item id for one case in one dataset.

    Deriving the id from the case is what makes seeding a mirror rather than an append: Langfuse
    upserts when an item is created with an id that already exists, so an edited case updates in
    place instead of the hosted copy silently keeping its original wording.

    The dataset name is part of the id because the SDK requires ids to be globally unique and never
    reused across datasets — and the CI subset shares ``case_id`` values with the full set.

    Args:
        dataset_name: The hosted dataset the item belongs to.
        case_id: The repo's stable identifier for the case.

    Returns:
        A stable, dataset-scoped item id.
    """
    return f"{dataset_name}:{case_id}"


def _seed_one(dataset_name: str, cases: list[EvalCase], description: str) -> SeedOutcome:
    """Reconcile one hosted dataset so it mirrors the repo's cases exactly.

    Three things happen, and all three are needed for the hosted set to be trustworthy:

    - **Every repo case is upserted** under a deterministic id, so an edited message or expectation
      overwrites the hosted copy.
    - **``status=ACTIVE`` is always explicit.** An upsert that omits the status leaves an archived
      item archived, so a case that was retired and later restored would come back invisible and
      never be scored — the same silent hole this function exists to close.
    - **Anything the repo no longer defines is ARCHIVED**, not deleted, so past run history against
      it survives while future runs skip it.

    Args:
        dataset_name: The Langfuse dataset to create/reconcile.
        cases: The cases the repo defines for this dataset.
        description: The dataset description (set on create).

    Returns:
        The counts of upserted and archived items.
    """
    client = get_client()
    client.create_dataset(
        name=dataset_name,
        description=description,
        metadata={"owner": "agentforge", "suite": "golden"},
    )
    for case in cases:
        client.create_dataset_item(
            dataset_name=dataset_name,
            id=_item_id(dataset_name, case.case_id),
            input=case.input(),
            expected_output=case.expected.model_dump(mode="json"),
            metadata={
                "case_id": case.case_id,
                "intent": case.intent,
                "primary_rubric": case.primary_rubric.value,
                "mechanism": case.mechanism,
                "route": case.route.value,
            },
            status=_ACTIVE,
        )

    # Read AFTER upserting, and match on the ITEM ID rather than the case_id. Anything not sitting
    # at its canonical id is a fossil: either a case the repo dropped, or — the case that bit on the
    # first live run — a copy the original seeder created before ids were deterministic, which
    # Langfuse gave a random UUID. Those legacy rows carry a case_id the repo still defines, so a
    # case_id match would spare them and leave every case scored twice, from two rows, one of them
    # un-updatable.
    wanted_ids = {_item_id(dataset_name, case.case_id) for case in cases}
    archived = 0
    for item in client.get_dataset(dataset_name).items:
        metadata = item.metadata if isinstance(item.metadata, dict) else {}
        case_id = metadata.get("case_id")
        if item.id in wanted_ids:
            continue
        client.create_dataset_item(
            dataset_name=dataset_name,
            id=item.id,
            input=item.input,
            expected_output=item.expected_output,
            metadata=item.metadata,
            status=_ARCHIVED,
        )
        archived += 1
        logger.warning(
            "archived a hosted case the repo no longer defines",
            extra={"dataset": dataset_name, "case_id": case_id, "item_id": item.id},
        )
    return SeedOutcome(upserted=len(cases), archived=archived)


def seed_dataset() -> None:
    """Reconcile both hosted datasets so they mirror the repo's golden set exactly.

    Seeds the full 53 into ``copilot-week2-golden-v1`` (the on-demand, approval-gated full run) and
    the 3-case CI subset into ``copilot-week2-golden-ci`` — the cheap subset the blocking gate
    scores on promotion PRs. Reconciling makes no model calls — it is free; only *running* an
    experiment
    against a dataset costs money. Run it whenever the cases change.

    **This is a mirror, not an append.** The original seeder created any ``case_id`` it did not
    already see and skipped the rest, so it could neither update an edited case nor retire a
    deleted one. Both datasets fossilized: the CI gate spent its first enforcing run scoring three
    questions the repo had not defined for weeks — including one that expected a *decline* for
    kidney labs the agent had since learned to read — and reported the resulting failure as an
    agent regression. A gate scoring cases the repo no longer contains is not testing the build; it
    is reporting on a dataset nobody can see from the code.

    Raises:
        RuntimeError: If Langfuse credentials are not configured (nothing can be seeded).
    """
    client = get_client()
    if not client.auth_check():
        raise RuntimeError(
            "Langfuse is not configured/authenticated. Set LANGFUSE_PUBLIC_KEY, "
            "LANGFUSE_SECRET_KEY, and LANGFUSE_HOST before seeding."
        )

    full = _seed_one(DATASET_NAME, CASES, _DESCRIPTION)
    ci = _seed_one(CI_DATASET_NAME, CI_CASES, _DESCRIPTION + " (CI auto-gate subset.)")

    logger.info(
        "datasets reconciled",
        extra={
            "full_dataset": DATASET_NAME,
            "full_upserted": full.upserted,
            "full_archived": full.archived,
            "ci_dataset": CI_DATASET_NAME,
            "ci_upserted": ci.upserted,
            "ci_archived": ci.archived,
        },
    )
    print(  # noqa: T201 - CLI entrypoint; user-facing output is intended
        f"Reconciled '{DATASET_NAME}': {full.upserted} upserted, {full.archived} archived. "
        f"Reconciled '{CI_DATASET_NAME}': {ci.upserted} upserted, {ci.archived} archived."
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed_dataset()
