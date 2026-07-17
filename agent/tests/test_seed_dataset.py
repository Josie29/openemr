from typing import Any

import pytest

from copilot.evals import seed_dataset
from copilot.evals.cases import EvalCase, ExpectedOutcome, RouteBucket
from copilot.evals.rubrics import ExpectedBehavior, RubricName


class _FakeItem:
    """A hosted dataset item, mimicking only what the seeder reads back."""

    def __init__(self, item_id: str, metadata: dict[str, Any], message: str) -> None:
        self.id = item_id
        self.metadata = metadata
        self.input: dict[str, Any] = {"message": message}
        self.expected_output: dict[str, Any] = {}
        self.status = "ACTIVE"


class _FakeClient:
    """A Langfuse stand-in reproducing the two API behaviours the real seeder depends on.

    Both were confirmed against the live API before this fake was written: creating an item with an
    existing id upserts it, and ``get_dataset`` returns ACTIVE items only.
    """

    def __init__(self, existing: list[_FakeItem] | None = None) -> None:
        self.items: dict[str, _FakeItem] = {item.id: item for item in existing or []}

    def auth_check(self) -> bool:
        return True

    def create_dataset(self, **kwargs: Any) -> None:
        return None

    def get_dataset(self, name: str) -> Any:
        active = [item for item in self.items.values() if item.status == "ACTIVE"]
        return type("_D", (), {"items": active})()

    def create_dataset_item(self, **kwargs: Any) -> None:
        item_id = kwargs["id"]
        status = kwargs.get("status")
        existing = self.items.get(item_id)
        if existing is None:
            self.items[item_id] = _FakeItem(
                item_id, kwargs["metadata"], (kwargs["input"] or {}).get("message", "")
            )
            self.items[item_id].status = getattr(status, "value", status) or "ACTIVE"
            return
        existing.metadata = kwargs["metadata"]
        existing.input = kwargs["input"]
        if status is not None:
            existing.status = getattr(status, "value", status)


def _case(case_id: str, message: str) -> EvalCase:
    """Build a minimal golden case for seeding assertions."""
    return EvalCase(
        case_id=case_id,
        patient_id="23",
        message=message,
        intent="probe",
        primary_rubric=RubricName.CITATION_PRESENT,
        mechanism="probe",
        route=RouteBucket.RECORD,
        expected=ExpectedOutcome(behavior=ExpectedBehavior.ANSWER),
    )


@pytest.fixture
def patch_client(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Swap the seeder's Langfuse client for a fake; returns an installer."""

    def install(client: _FakeClient) -> _FakeClient:
        monkeypatch.setattr(seed_dataset, "get_client", lambda: client)
        return client

    return install


def test_edited_case_overwrites_the_hosted_copy(patch_client: Any) -> None:
    # THE bug this seeder was rewritten for. The old version skipped any case_id it already saw, so
    # editing a case's question updated the repo and nothing else — the hosted dataset kept asking
    # the original question forever. The CI gate then spent its first enforcing run scoring three
    # questions the repo had not contained for weeks and blamed the agent for the mismatch.
    client = patch_client(
        _FakeClient([_FakeItem("ds:alpha", {"case_id": "alpha"}, "the ORIGINAL question")])
    )
    seed_dataset._seed_one("ds", [_case("alpha", "the EDITED question")], "d")
    assert client.items["ds:alpha"].input["message"] == "the EDITED question"


def test_case_deleted_from_the_repo_is_archived(patch_client: Any) -> None:
    # A case removed from the repo used to linger and keep getting scored, so the gate's verdict
    # covered cases no reader of the code could find. Archived rather than deleted: past runs
    # against it stay readable, but get_dataset (what an experiment scores) no longer returns it.
    client = patch_client(
        _FakeClient([_FakeItem("ds:fossil", {"case_id": "fossil"}, "a retired question")])
    )
    outcome = seed_dataset._seed_one("ds", [_case("alpha", "a live question")], "d")
    assert client.items["ds:fossil"].status == "ARCHIVED"
    assert outcome.archived == 1
    assert [item.metadata["case_id"] for item in client.get_dataset("ds").items] == ["alpha"]


def test_restored_case_is_reactivated_not_left_invisible(patch_client: Any) -> None:
    # Guards the subtlety that makes the fix correct: the live API leaves an ARCHIVED item archived
    # when an upsert omits the status. Without an explicit status=ACTIVE, a case that was retired
    # and later restored would seed "successfully" and never be scored again — silently reopening
    # the exact hole this module exists to close.
    fossil = _FakeItem("ds:alpha", {"case_id": "alpha"}, "old")
    fossil.status = "ARCHIVED"
    client = patch_client(_FakeClient([fossil]))
    seed_dataset._seed_one("ds", [_case("alpha", "restored")], "d")
    assert client.items["ds:alpha"].status == "ACTIVE"
    assert [item.metadata["case_id"] for item in client.get_dataset("ds").items] == ["alpha"]


def test_legacy_random_id_row_is_retired_not_duplicated(patch_client: Any) -> None:
    # Caught on the first live run against the real datasets, which doubled them (3 items -> 6,
    # 52 -> 104). Rows the ORIGINAL seeder wrote have no deterministic id — Langfuse assigned a
    # random UUID — so upserting by canonical id creates a second row beside the first rather than
    # replacing it. They carry a case_id the repo still defines, so an archive pass keyed on
    # case_id spares them and every case gets scored twice, from two rows whose content can drift
    # apart. Retirement therefore keys on the item id: not at its canonical id, not active.
    legacy = _FakeItem("f47ac10b-58cc-4372-a567-0e02b2c3d479", {"case_id": "alpha"}, "old copy")
    client = patch_client(_FakeClient([legacy]))
    seed_dataset._seed_one("ds", [_case("alpha", "the live question")], "d")
    active = client.get_dataset("ds").items
    assert [item.id for item in active] == ["ds:alpha"]
    assert client.items[legacy.id].status == "ARCHIVED"
    assert active[0].input["message"] == "the live question"


def test_item_ids_do_not_collide_across_datasets() -> None:
    # The SDK requires item ids to be globally unique and never reused across datasets, and the CI
    # subset shares case_ids with the full set. A bare case_id would make seeding one dataset
    # clobber the other's item.
    ci = seed_dataset._item_id("copilot-week2-golden-ci", "angulo-lab-ckd-nsaid")
    full = seed_dataset._item_id("copilot-week2-golden-v1", "angulo-lab-ckd-nsaid")
    assert ci != full
