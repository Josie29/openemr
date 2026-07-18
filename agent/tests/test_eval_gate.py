from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from copilot.evals.baseline import RunScores, regressions
from copilot.evals.cases import CASES, CI_CASES, CI_DATASET_NAME, DATASET_NAME, EvalCase
from copilot.evals.experiment import _THRESHOLDS, _check_regression

_WORKFLOW = Path(__file__).parents[2] / ".github" / "workflows" / "evals.yml"
_ACTION = "langfuse/experiment-action"

# The repo's two hosted datasets, by the name the workflow would reference. Used to resolve the
# gate's real case count from the workflow rather than assuming it — see _gated_cases.
_CASES_BY_DATASET: dict[str, list[EvalCase]] = {
    CI_DATASET_NAME: CI_CASES,
    DATASET_NAME: CASES,
}


def _gate_inputs() -> dict[str, str]:
    """Return the `with:` inputs of the workflow step that runs the paid eval.

    Returns:
        The step's inputs, as written in ``.github/workflows/evals.yml``.

    Raises:
        AssertionError: If the workflow has no step using the Langfuse experiment action — the gate
            would then not run at all, and every assertion built on it would vacuously pass.
    """
    workflow = yaml.safe_load(_WORKFLOW.read_text())
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            if step.get("uses", "").startswith(_ACTION):
                inputs: dict[str, str] = step["with"]
                return inputs
    raise AssertionError(f"no {_ACTION} step in {_WORKFLOW.name}: the hard gate does not run")


def _gated_cases() -> list[EvalCase]:
    """Return the repo cases behind the dataset the workflow actually scores.

    Resolving this from the workflow — rather than hardcoding ``CI_CASES`` — is what keeps the
    equivalence argument below honest. Repointing ``dataset_name`` at the 53-case set is a one-line
    YAML edit that changes the gate's arithmetic completely, and a guard reading ``CI_CASES``
    directly would stay green through it.

    Returns:
        The cases the gate scores on a promotion PR.

    Raises:
        AssertionError: If the workflow names a dataset the repo does not define. That is the
            fossil-dataset failure seed_dataset() documents: seeding mirrors the repo's datasets, so
            a gate pointed elsewhere scores stale items nobody can see from the code.
    """
    name = _gate_inputs()["dataset_name"]
    if name not in _CASES_BY_DATASET:
        raise AssertionError(
            f"the gate scores dataset {name!r}, which the repo does not seed "
            f"(known: {sorted(_CASES_BY_DATASET)}). Seeding cannot keep it current."
        )
    return _CASES_BY_DATASET[name]

# The PRD's hard gate: "we will introduce a small regression and confirm your CI gate fails. If the
# eval gate does not block the regression, the Week 2 build does not pass." These tests are the
# free, model-call-free proof that the blocking logic actually blocks — the paid eval only proves it
# on the day it runs, and only for the regressions that happen to occur.


def _run(**overrides: float) -> Any:
    """Build a stand-in ExperimentResult whose run-level means default to a perfect score.

    Args:
        **overrides: Rubric means to lower, keyed by run-metric name (e.g. ``mean_safe_refusal``).

    Returns:
        An object exposing the ``run_evaluations`` attribute ``_check_regression`` reads.
    """
    scores = dict.fromkeys(_THRESHOLDS, 1.0)
    scores.update(overrides)
    return SimpleNamespace(
        run_evaluations=[SimpleNamespace(name=name, value=value) for name, value in scores.items()]
    )


def test_perfect_run_does_not_block() -> None:
    # Catches a gate that fails closed on a clean run — a gate nobody can merge past gets disabled,
    # and a disabled gate blocks nothing.
    assert _check_regression(_run()) == []


def test_one_failed_case_blocks_every_rubric() -> None:
    # THE hard-gate test: the grader injects a small regression, which on the 3-case subset means a
    # single case flipping (mean 1.0 -> 0.67). Every rubric must block on that. If this passes for
    # any rubric, a real regression reaches prod with a green check.
    for metric in _THRESHOLDS:
        breached = _check_regression(_run(**{metric: 2 / 3}))
        assert breached, f"{metric}: one failing case out of three did not block the release"


def test_faithfulness_judge_tolerates_phrasing_noise() -> None:
    # The one rubric an LLM judge can fail without the code being wrong. If this floor were also
    # 1.0, flaky judge phrasing would redden clean PRs, and the team would learn to bypass the gate.
    assert _check_regression(_run(mean_factually_consistent=0.95)) == []


def _baseline(**scores: float) -> RunScores:
    """Build a previous-run baseline, defaulting every rubric to a perfect score."""
    values = dict.fromkeys(_THRESHOLDS, 1.0)
    values.update(scores)
    return RunScores(run_name="previous-pr-run", pr_url=None, scores=values)


def test_regression_beyond_five_percent_blocks_even_above_the_floor() -> None:
    # The PRD's first clause, checked directly rather than inferred: a category that regresses more
    # than 5% against the last PR must block EVEN IF it still clears its absolute threshold. Only
    # the faithfulness rubric has slack to demonstrate this (floor 0.9) — 0.93 passes the floor but
    # is a 7% drop from a 1.0 baseline. Without this the gate would let quality erode run over run
    # in the band between the baseline and the floor.
    result = _run(mean_factually_consistent=0.93)
    assert _check_regression(result, _baseline()) , (
        "a 7% regression cleared the gate because it stayed above the absolute floor"
    )
    # ...and the same score is clean when the previous run was already there (no regression).
    assert _check_regression(result, _baseline(mean_factually_consistent=0.93)) == []


def test_regression_within_five_percent_is_tolerated() -> None:
    # The allowance must be real, not nominal: judge phrasing noise inside the 5% band should not
    # red a promotion, or the team learns to bypass the gate. 0.96 vs a 1.0 baseline is a 4% drop.
    assert _check_regression(_run(mean_factually_consistent=0.96), _baseline()) == []


def test_the_five_percent_allowance_scales_with_the_baseline() -> None:
    # Relative, not absolute percentage points. Against a 0.90 baseline the allowance is 0.855, so
    # 0.86 holds and 0.85 breaches; an absolute-5pp reading would wrongly tolerate 0.85. Exercises
    # regressions() directly rather than through _check_regression, because both those scores sit
    # below the 0.9 faithfulness floor — going through the gate would report a floor breach and
    # prove nothing about the delta clause this test is about.
    previous = _baseline(mean_factually_consistent=0.90)
    assert regressions({"mean_factually_consistent": 0.86}, previous) == []
    assert regressions({"mean_factually_consistent": 0.85}, previous)


def test_missing_baseline_still_enforces_the_absolute_thresholds() -> None:
    # A Langfuse hiccup or a first-ever run degrades the gate to floors-only, never to nothing. If
    # this regressed, an unreadable baseline would turn the hard gate into a green rubber stamp.
    assert _check_regression(_run(mean_safe_refusal=2 / 3), None)
    assert _check_regression(_run(), None) == []


@pytest.mark.parametrize("flag", ["should_fail_on_regression", "should_fail_on_script_error"])
def test_gate_is_armed(flag: str) -> None:
    # The gate has been disarmed before, during cost iteration, and a disarmed gate is invisible:
    # the job still runs and still reports green, so nothing about the PR looks different. These two
    # flags are the only difference between a gate and a report. should_fail_on_script_error matters
    # as much as the regression flag — an eval that could not run has not passed, and a green check
    # for a harness that never scored anything claims safety it did not verify.
    assert _gate_inputs().get(flag) == "true", (
        f"{flag} is not 'true': the eval reports regressions instead of blocking them, "
        "and the PRD's hard gate fails open."
    )


def test_gate_scores_a_dataset_the_repo_seeds() -> None:
    # Ties the workflow's dataset_name to a dataset seed_dataset() mirrors from the repo. When these
    # drift, seeding keeps one dataset current while the gate scores another that slowly fossilizes
    # -- which already happened once: the first enforcing run scored three questions the repo had
    # not defined for weeks, including one expecting a decline for labs the agent had learned to
    # read, and reported it as an agent regression.
    assert _gated_cases(), "the gate's dataset resolves to zero cases: it scores nothing"
