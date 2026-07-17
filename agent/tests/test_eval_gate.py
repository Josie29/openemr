from types import SimpleNamespace
from typing import Any

from copilot.evals.cases import CI_CASES
from copilot.evals.experiment import _THRESHOLDS, _check_regression

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


def test_ci_subset_leaves_no_gap_between_the_prds_two_clauses() -> None:
    # The PRD requires failing if a category "regresses by more than 5% OR drops below the pass
    # threshold". We implement only the second clause, as an absolute floor. That is safe ONLY while
    # no representable score can sit inside a floor's slack: on N cases the means are k/N, so a
    # sub-1.0 score cannot exceed a floor above (N-1)/N. Growing CI_CASES or lowering a floor can
    # silently open a band where a >5% regression clears the floor and ships. This test fails when
    # that happens, so the doc's equivalence claim (W2_ARCHITECTURE.md section 7) cannot rot.
    n = len(CI_CASES)
    highest_failing_mean = (n - 1) / n
    for metric, floor in _THRESHOLDS.items():
        assert highest_failing_mean < floor, (
            f"{metric}: with {n} CI cases a run can score {highest_failing_mean:.3f} — a "
            f"{(1 - highest_failing_mean) * 100:.0f}% regression that clears the {floor} floor. "
            "Raise the floor or implement the PRD's explicit >5% delta clause."
        )
