import logging

from langfuse import get_client

from copilot.evals.cases import CASES, DATASET_NAME

logger = logging.getLogger("copilot.evals.seed")


def seed_dataset() -> None:
    """Upsert the eval cases into the Langfuse-hosted dataset, idempotently by ``case_id``.

    Creates the dataset if it does not exist, then adds only cases whose ``case_id`` is not already
    present — so re-running after adding a case appends just the new items instead of duplicating
    the suite. Hosting the dataset in Langfuse (rather than a local list) is what gives the
    experiment run the side-by-side comparison UI.

    Raises:
        RuntimeError: If Langfuse credentials are not configured (nothing can be seeded).
    """
    client = get_client()
    if not client.auth_check():
        raise RuntimeError(
            "Langfuse is not configured/authenticated. Set LANGFUSE_PUBLIC_KEY, "
            "LANGFUSE_SECRET_KEY, and LANGFUSE_HOST before seeding."
        )

    client.create_dataset(
        name=DATASET_NAME,
        description="Clinical Co-Pilot grounding & faithfulness eval: physician questions against "
        "fixture patients, scored on tool-correctness, no-fabrication, faithfulness, completeness.",
        metadata={"owner": "agentforge", "suite": "grounding"},
    )

    existing = client.get_dataset(DATASET_NAME)
    seen: set[str] = {
        item.metadata["case_id"]
        for item in existing.items
        if isinstance(item.metadata, dict) and "case_id" in item.metadata
    }

    created = 0
    for case in CASES:
        if case.case_id in seen:
            continue
        client.create_dataset_item(
            dataset_name=DATASET_NAME,
            input=case.input(),
            expected_output=case.expected.model_dump(mode="json"),
            metadata={"case_id": case.case_id, "intent": case.intent},
        )
        created += 1

    logger.info(
        "dataset seeded",
        # Avoid reserved LogRecord attribute names (e.g. 'created') in the extra dict.
        extra={"dataset_name": DATASET_NAME, "created_count": created, "skipped_count": len(seen)},
    )
    print(  # noqa: T201 - this is a CLI entrypoint; user-facing output is intended
        f"Seeded '{DATASET_NAME}': {created} item(s) created, {len(seen)} already present "
        f"({len(CASES)} cases total)."
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    seed_dataset()
