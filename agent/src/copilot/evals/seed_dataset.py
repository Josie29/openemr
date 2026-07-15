import logging

from langfuse import get_client

from copilot.evals.cases import CASES, CI_CASES, CI_DATASET_NAME, DATASET_NAME, EvalCase

logger = logging.getLogger("copilot.evals.seed")

_DESCRIPTION = (
    "Clinical Co-Pilot graph golden set: physician questions against fixture patients, scored on "
    "the boolean rubrics schema_valid, citation_present, factually_consistent, safe_refusal, "
    "no_phi_in_logs."
)


def _seed_one(dataset_name: str, cases: list[EvalCase], description: str) -> int:
    """Upsert one set of cases into a hosted dataset, idempotently by ``case_id``.

    Args:
        dataset_name: The Langfuse dataset to create/append to.
        cases: The cases to seed.
        description: The dataset description (set on create).

    Returns:
        The number of items newly created (already-present cases are skipped).
    """
    client = get_client()
    client.create_dataset(
        name=dataset_name,
        description=description,
        metadata={"owner": "agentforge", "suite": "golden"},
    )
    existing = client.get_dataset(dataset_name)
    seen: set[str] = {
        item.metadata["case_id"]
        for item in existing.items
        if isinstance(item.metadata, dict) and "case_id" in item.metadata
    }
    created = 0
    for case in cases:
        if case.case_id in seen:
            continue
        client.create_dataset_item(
            dataset_name=dataset_name,
            input=case.input(),
            expected_output=case.expected.model_dump(mode="json"),
            metadata={
                "case_id": case.case_id,
                "intent": case.intent,
                "primary_rubric": case.primary_rubric.value,
                "mechanism": case.mechanism,
                "route": case.route.value,
            },
        )
        created += 1
    return created


def seed_dataset() -> None:
    """Upsert the golden set into both hosted datasets, idempotently by ``case_id``.

    Seeds the full 50 into ``copilot-golden-v1`` (the on-demand, approval-gated full run) and the
    3-case CI subset into ``copilot-golden-ci`` (the cheap report-only auto-gate). Seeding makes no
    model calls — it is free; only *running* an experiment against a dataset costs money.

    Raises:
        RuntimeError: If Langfuse credentials are not configured (nothing can be seeded).
    """
    client = get_client()
    if not client.auth_check():
        raise RuntimeError(
            "Langfuse is not configured/authenticated. Set LANGFUSE_PUBLIC_KEY, "
            "LANGFUSE_SECRET_KEY, and LANGFUSE_HOST before seeding."
        )

    full_created = _seed_one(DATASET_NAME, CASES, _DESCRIPTION)
    ci_created = _seed_one(CI_DATASET_NAME, CI_CASES, _DESCRIPTION + " (CI auto-gate subset.)")

    logger.info(
        "datasets seeded",
        extra={
            "full_dataset": DATASET_NAME,
            "full_created": full_created,
            "full_total": len(CASES),
            "ci_dataset": CI_DATASET_NAME,
            "ci_created": ci_created,
            "ci_total": len(CI_CASES),
        },
    )
    print(  # noqa: T201 - CLI entrypoint; user-facing output is intended
        f"Seeded '{DATASET_NAME}': {full_created} new / {len(CASES)} total. "
        f"Seeded '{CI_DATASET_NAME}': {ci_created} new / {len(CI_CASES)} total."
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed_dataset()
