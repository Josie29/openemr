import logging

from langfuse import get_client
from pydantic import BaseModel, Field

logger = logging.getLogger("copilot.evals.baseline")

# The PRD's regression allowance: "The build must fail if any category regresses by more than 5%
# or drops below the pass threshold." Read as a RELATIVE drop against the previous run's score for
# the same category, so the allowance scales with the baseline (a 0.90 baseline tolerates 0.855,
# not 0.85). The two clauses are independent — a category can clear its absolute floor and still
# breach this, which is the whole point of checking both.
MAX_RELATIVE_REGRESSION = 0.05


class RunScores(BaseModel):
    """One dataset run's run-level rubric means — the baseline a new run is compared against."""

    run_name: str = Field(description="The Langfuse dataset-run name the scores came from")
    pr_url: str | None = Field(
        default=None, description="The PR whose CI produced the run, when it came from a PR"
    )
    scores: dict[str, float] = Field(
        description="Run-level metric name (e.g. 'mean_safe_refusal') to its mean"
    )


def fetch_previous_run(dataset_name: str) -> RunScores | None:
    """Return the most recent existing run's rubric means for a dataset, or None if there is none.

    Called BEFORE the current experiment runs, so "most recent" is unambiguously the previous run —
    typically the last promotion PR's gate execution. No run-name exclusion is needed or attempted;
    ordering by creation time is what makes this the previous PR's result rather than our own.

    A failure to read the baseline is logged and swallowed rather than raised. The absolute floors
    are enforced independently and are the stronger of the PRD's two clauses, so a Langfuse hiccup
    degrades the gate to floors-only instead of reddening a promotion for a reason unrelated to
    agent quality. The caller reports when it ran without a baseline, so a skipped delta check is
    visible rather than silent.

    Args:
        dataset_name: The hosted dataset the gate scores.

    Returns:
        The previous run's scores, or None when no prior run exists or the lookup failed.
    """
    try:
        client = get_client()
        runs = client.api.datasets.get_runs(dataset_name=dataset_name, limit=50)
        previous = sorted(runs.data, key=lambda run: run.created_at, reverse=True)
        if not previous:
            logger.info("no prior run to compare against", extra={"dataset": dataset_name})
            return None
        latest = previous[0]

        # Run-level scores attach to the dataset run and are read by its id. The API spells that
        # parameter `experiment_id`; a dataset run IS the experiment in this model, and passing the
        # run id is what returns the five `mean_*` values rather than the per-item scores.
        scored = client.api.scores_v3.get_many_v3(experiment_id=latest.id, limit=100)
        scores = {
            score.name: float(score.value)
            for score in scored.data
            if isinstance(score.value, int | float)
        }
        if not scores:
            logger.warning(
                "prior run carries no run-level scores; skipping the delta check",
                extra={"dataset": dataset_name, "run": latest.name},
            )
            return None

        metadata = latest.metadata if isinstance(latest.metadata, dict) else {}
        return RunScores(
            run_name=latest.name,
            pr_url=metadata.get("langfuse.pr_url"),
            scores=scores,
        )
    except Exception:
        # Broad by intent: any failure to READ the baseline must degrade to floors-only, never
        # abort the gate. Narrowing this to the SDK's error types would let an unforeseen transport
        # or serialization error fail a promotion PR for a reason unrelated to agent quality.
        logger.warning("could not read the eval baseline; enforcing floors only", exc_info=True)
        return None


def regressions(current: dict[str, float], baseline: RunScores) -> list[str]:
    """Return the categories that regressed more than 5% against the baseline run.

    Args:
        current: This run's run-level metric means.
        baseline: The previous run's scores.

    Returns:
        Human-readable ``metric: value < allowed (baseline X, -Y%)`` strings, one per breach; empty
        when every shared category held.
    """
    breaches: list[str] = []
    for metric, previous in baseline.scores.items():
        if metric not in current or previous <= 0:
            continue
        allowed = previous * (1 - MAX_RELATIVE_REGRESSION)
        value = current[metric]
        if value < allowed:
            drop = (previous - value) / previous * 100
            breaches.append(
                f"{metric}: {value:.3f} < {allowed:.3f} allowed "
                f"(baseline {previous:.3f} from '{baseline.run_name}', -{drop:.1f}%)"
            )
    return breaches
