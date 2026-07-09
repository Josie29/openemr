import logging
from collections.abc import Callable
from typing import Any

from langfuse import Evaluation, RegressionError, RunnerContext, get_client
from langfuse.experiment import EvaluatorFunction, RunEvaluatorFunction

from copilot.config import get_settings
from copilot.evals.cases import DATASET_NAME
from copilot.evals.judges import judge_completeness, judge_faithfulness
from copilot.evals.runner import resolve_eval_model_tier, run_case
from copilot.observability import configure_observability
from copilot.schemas import ChatResponse

logger = logging.getLogger("copilot.evals.experiment")

# Regression thresholds. The report-only CI gate does not fail on these yet (see the workflow), but
# the checks run and surface in the PR comment — flip should_fail_on_regression to true to enforce.
_THRESHOLDS: dict[str, float] = {
    "mean_faithfulness": 0.9,  # summary must almost never over-state beyond the claims
    "mean_no_fabrication": 1.0,  # any fabricated/absent-data claim is a hard regression
    "mean_tool_correctness": 0.9,  # the agent must read the resources the question needs
    "mean_completeness": 0.8,  # answers may occasionally miss a secondary fact
}


def _api_key() -> str:
    """Return the Anthropic API key the judges use, or fail loudly if unset.

    Returns:
        The configured Anthropic API key.

    Raises:
        RuntimeError: If no Anthropic API key is configured (the LLM judges cannot run).
    """
    key = get_settings().anthropic_api_key
    if not key:
        raise RuntimeError(
            "ANTHROPIC_API_KEY (or COPILOT_ANTHROPIC_API_KEY) is required to run the LLM judges."
        )
    return key


async def task(*, item: Any, **_: Any) -> dict[str, Any]:
    """Run the agent on one dataset item and return its observable behavior.

    Args:
        item: The Langfuse dataset item; ``item.input`` is ``{patient_id, message}``.

    Returns:
        A serializable record of the turn: the structured ``response``, the ``tools_called``, and
        whether the grounding gate ``refused``.
    """
    run = await run_case(item.input["patient_id"], item.input["message"])
    return {
        "response": run.response.model_dump(mode="json"),
        "tools_called": run.tools_called,
        "refused": run.refused,
    }


def _response(output: dict[str, Any]) -> ChatResponse:
    """Rebuild the typed answer from a task output record."""
    return ChatResponse.model_validate(output["response"])


def _answer_text(response: ChatResponse) -> str:
    """Concatenate the summary and every claim's text, lowercased, for substring tripwires."""
    return " ".join([response.summary, *(claim.text for claim in response.claims)]).lower()


def eval_tool_correctness(
    *, output: dict[str, Any], expected_output: dict[str, Any], **_: Any
) -> Evaluation:
    """Score whether the agent called every FHIR read the question requires."""
    expected = set(expected_output.get("expected_tools", []))
    called = set(output.get("tools_called", []))
    missing = expected - called
    return Evaluation(
        name="tool_correctness",
        value=0.0 if missing else 1.0,
        comment=f"missing tools: {sorted(missing)}" if missing else "all required tools called",
    )


def eval_no_fabrication(
    *, output: dict[str, Any], expected_output: dict[str, Any], **_: Any
) -> Evaluation:
    """Fail the case if any forbidden phrase (fabricated fact / overreach) appears in the answer.

    A deterministic tripwire: cheaper and more reliable than an LLM for catching a named allergen
    or drug the record does not contain, or a definitive-interaction phrase the restraint rule
    forbids.
    """
    text = _answer_text(_response(output))
    forbidden = expected_output.get("must_not_claim", [])
    hits = [phrase for phrase in forbidden if phrase.lower() in text]
    return Evaluation(
        name="no_fabrication",
        value=0.0 if hits else 1.0,
        comment=f"forbidden phrases present: {hits}" if hits else "no forbidden phrases",
    )


async def eval_faithfulness(*, output: dict[str, Any], **_: Any) -> Evaluation:
    """Score whether the summary stays within what the verified claims support (Haiku judge)."""
    verdict = await judge_faithfulness(_response(output), api_key=_api_key())
    return Evaluation(
        name="faithfulness", value=1.0 if verdict.passed else 0.0, comment=verdict.reasoning
    )


async def eval_completeness(
    *, input: Any, output: dict[str, Any], expected_output: dict[str, Any], **_: Any  # noqa: A002
) -> Evaluation:
    """Score whether the answer conveys every required fact for the question (Haiku judge)."""
    verdict = await judge_completeness(
        input["message"],
        expected_output.get("must_mention", []),
        _response(output),
        api_key=_api_key(),
    )
    return Evaluation(
        name="completeness", value=1.0 if verdict.passed else 0.0, comment=verdict.reasoning
    )


def _mean_run_evaluator(metric: str, out_name: str) -> Callable[..., Evaluation]:
    """Build a run-level evaluator that averages one item metric across the dataset run.

    Args:
        metric: The per-item evaluation name to average (e.g. ``"faithfulness"``).
        out_name: The run-level score name to emit (e.g. ``"mean_faithfulness"``).

    Returns:
        A run-evaluator function suitable for ``run_evaluators=[...]``.
    """

    def run_evaluator(*, item_results: list[Any], **_: Any) -> Evaluation:
        values = [
            float(ev.value)
            for result in item_results
            for ev in result.evaluations
            if ev.name == metric
        ]
        mean = sum(values) / len(values) if values else 0.0
        return Evaluation(
            name=out_name,
            value=mean,
            comment=f"mean of {len(values)} case(s)",
            data_type="NUMERIC",
        )

    return run_evaluator


_EVALUATORS: list[EvaluatorFunction] = [
    eval_tool_correctness,
    eval_no_fabrication,
    eval_faithfulness,
    eval_completeness,
]
_RUN_EVALUATORS: list[RunEvaluatorFunction] = [
    _mean_run_evaluator("faithfulness", "mean_faithfulness"),
    _mean_run_evaluator("no_fabrication", "mean_no_fabrication"),
    _mean_run_evaluator("tool_correctness", "mean_tool_correctness"),
    _mean_run_evaluator("completeness", "mean_completeness"),
]


def _check_regression(result: Any) -> list[str]:
    """Return the list of run metrics that fell below their threshold (empty if all pass).

    Args:
        result: The ``ExperimentResult`` from a run.

    Returns:
        Human-readable ``metric: value < threshold`` strings for each breached threshold.
    """
    scores = {ev.name: float(ev.value) for ev in result.run_evaluations}
    return [
        f"{name}: {scores[name]:.2f} < {threshold:.2f}"
        for name, threshold in _THRESHOLDS.items()
        if name in scores and scores[name] < threshold
    ]


def _enable_tracing() -> None:
    """Instrument the agent + judges so each eval run reports token usage and cost to Langfuse.

    Reuses the service's ``Agent.instrument_all()`` wiring, which instruments every agent — the
    agent-under-test *and* the Haiku judges — so their generations export model/tokens and Langfuse
    computes cost. The experiment runner already tags these traces ``environment=sdk-experiment``,
    which segregates them from dev/prod traces in the project.
    """
    configure_observability(get_settings())


def experiment(context: RunnerContext) -> Any:
    """CI entrypoint invoked by ``langfuse/experiment-action``.

    Runs every dataset item through the agent and the four evaluators, then raises
    ``RegressionError`` when a run-level mean is below its threshold. Under the report-only workflow
    the raised regression does not fail the job — it is surfaced in the PR comment — so flipping to
    an enforcing gate is a single ``should_fail_on_regression`` change.

    Args:
        context: The action-provided runner context (already bound to the dataset).

    Returns:
        The ``ExperimentResult`` for the action to serialize into its report.

    Raises:
        RegressionError: When one or more run-level means breach ``_THRESHOLDS``.
    """
    _enable_tracing()
    result = context.run_experiment(
        name="copilot-grounding",
        task=task,
        evaluators=_EVALUATORS,
        run_evaluators=_RUN_EVALUATORS,
        max_concurrency=4,
        metadata={"agent_model": resolve_eval_model_tier().value},
    )
    breaches = _check_regression(result)
    if breaches:
        logger.warning("eval regression", extra={"breaches": breaches})
        raise RegressionError(result=result)
    return result


def run_local() -> None:
    """Run the experiment locally against the hosted dataset (smoke test, never fails the process).

    Loads the Langfuse-hosted dataset directly and prints the run-level means and the run URL.
    Unlike the CI entrypoint, a regression is reported but not raised — this is for iterating on the
    suite, not gating.
    """
    _enable_tracing()
    agent_model = resolve_eval_model_tier().value
    print(f"Agent model under eval: {agent_model}")  # noqa: T201 - CLI entrypoint
    client = get_client()
    dataset = client.get_dataset(DATASET_NAME)
    result = client.run_experiment(
        name="copilot-grounding-local",
        data=dataset.items,
        task=task,
        evaluators=_EVALUATORS,
        run_evaluators=_RUN_EVALUATORS,
        max_concurrency=4,
        metadata={"agent_model": agent_model},
    )
    print("\nRun-level means:")  # noqa: T201 - CLI entrypoint
    for ev in result.run_evaluations:
        print(f"  {ev.name}: {float(ev.value):.2f}")  # noqa: T201

    # Per-case failures with the evaluator's reasoning — the diagnostic view for iterating on the
    # suite (which cases fail which metric, and why the judge said so).
    print("\nPer-case failures (metric < 1.0):")  # noqa: T201
    any_failure = False
    for item_result in result.item_results:
        # item is a hosted DatasetItem here (data=dataset.items); typed as Any to read .input
        # uniformly without narrowing the LocalExperimentItem|DatasetItem union.
        raw_item: Any = item_result.item
        message = raw_item.input.get("message", "?")
        for ev in item_result.evaluations:
            if float(ev.value) < 1.0:
                any_failure = True
                print(f"  [{ev.name}] {message}\n      -> {ev.comment}")  # noqa: T201
    if not any_failure:
        print("  none")  # noqa: T201

    breaches = _check_regression(result)
    print("\nRegressions:", breaches or "none")  # noqa: T201
    if result.dataset_run_url:
        print("Dataset run:", result.dataset_run_url)  # noqa: T201
    # Flush so the instrumented generation spans (token usage → cost) reach Langfuse before exit.
    get_client().flush()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    run_local()
