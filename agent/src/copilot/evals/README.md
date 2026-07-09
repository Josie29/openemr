# Agent evals — grounding & faithfulness

A Langfuse-hosted eval suite for the Clinical Co-Pilot agent. Each case is a physician question
run against the bundled FHIR **fixtures** (deterministic, no live OpenEMR, no PHI) with the **real
Claude model**, so we score genuine model behavior. Results land in Langfuse as a dataset run you
can compare across commits.

## What each case is scored on

Two deterministic checks and two Haiku LLM-as-judge rubrics. The judges cover the *semantic* gap
the agent's deterministic grounding gate (`enforce_grounding`) cannot — the gate already guarantees
every `Claim` cites a real fetched field, so the judges never re-check citations.

| Metric | Kind | What it catches |
|--------|------|-----------------|
| `tool_correctness` | deterministic | Agent failed to read a resource the question needs |
| `no_fabrication` | deterministic | A forbidden allergen/drug/overreach phrase appears in the answer |
| `faithfulness` | Haiku judge | The `summary` prose asserts something the verified `claims` don't support |
| `completeness` | Haiku judge | The answer omits a fact the physician needs |

Run-level means (`mean_faithfulness`, etc.) are checked against the thresholds in
`experiment.py` (`_THRESHOLDS`).

## Files

- `cases.py` — the 7 cases (typed) across 3 fixture patients + the dataset name. (Trimmed from an
  initial 11 to cut cost; removed cases were archived in the Langfuse dataset, not deleted.)
- `runner.py` — runs one agent turn in fixture mode, captures the tools it called.
- `judges.py` — the two Haiku LLM-as-judge rubrics.
- `seed_dataset.py` — upserts the cases into the Langfuse-hosted dataset (idempotent by `case_id`).
- `experiment.py` — the experiment: task + 4 evaluators + run-level means + regression thresholds.

## Running it locally

Requires `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_HOST`, and an Anthropic key in the
environment (or `agent/.env`). From `agent/`:

```bash
# 1. Seed / update the hosted dataset (safe to re-run; only adds new cases).
python -m copilot.evals.seed_dataset

# 2. Run the experiment against the hosted dataset and print run-level means + the run URL.
python -m copilot.evals.experiment
```

`experiment.py`'s `__main__` runs `run_local()`, which reports regressions but never exits non-zero
— it's for iterating on the suite, not gating.

## Model under eval

The agent-under-test and the judges both run on the **cheapest tier (Haiku)** by default, so eval
runs are inexpensive — these cases check grounding/faithfulness behavior, not the top-tier reasoning
the service reserves Sonnet/Opus for. To evaluate the production tier instead, set the full
identifier:

```bash
COPILOT_EVAL_MODEL_TIER=anthropic:claude-sonnet-5 python -m copilot.evals.experiment
```

The resolved tier is printed locally and recorded on each Langfuse run as `metadata.agent_model`,
so a Haiku run is distinguishable from a Sonnet run in the run-comparison view. Note that scores
from a Haiku run reflect Haiku's behavior, not the deployed tier's.

## Cost & tracing

Each run instruments the agent and the judges (`configure_observability` → `Agent.instrument_all()`),
so every case's token usage flows to Langfuse and it computes cost. The experiment runner tags these
traces `environment=sdk-experiment`, so they're segregated from dev/prod. See per-run cost in the
Langfuse **Experiments** table (Total Cost column) or per-trace in the dataset run. Cost is computed
server-side during ingestion, so it populates a few seconds after the run finishes.

## CI (report-only)

`.github/workflows/evals.yml` runs the suite via `langfuse/experiment-action` on every PR touching
`agent/**`, and on manual dispatch. It **comments** the scores on the PR but does not fail the job.

Required repo secrets (`gh secret set <NAME>`):

- `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_BASE_URL` (your region's host)
- `ANTHROPIC_API_KEY`

### Turning it into an enforcing gate

Once the judges are calibrated (see the Langfuse skill's `judge-calibration.md`), flip one input in
the workflow:

```yaml
should_fail_on_regression: "true"
```

The thresholds are already defined in `experiment.py`; a run-level mean below its threshold raises
`RegressionError`, which will then fail the check.
