# Project bindings — AgentForge Clinical Co-Pilot

This is the only file to rewrite when reusing the cost-analysis skill in another project. It maps
the generic method to *this* app: observability backend, model tiers, workload, output location.

## Observability backend — Langfuse

Actual per-request tokens and cost are recorded per trace by the agent's instrumentation
(`agent/src/copilot/observability.py`, ARCHITECTURE.md §10). Cost is computed server-side at
ingestion, so it populates a few seconds after a run.

**Pull actuals with the `langfuse` skill** (its `SKILL.md` + `references/cli.md` cover the CLI;
don't reimplement). Start by discovering the schema, then list/aggregate traces:

```bash
npx langfuse-cli api __schema                 # discover resources
npx langfuse-cli api traces --help            # args for listing/filtering traces
npx langfuse-cli api observations --help       # per-generation token + cost detail
```

**Segment by environment tag** — the instrumentation separates traces so dev/eval/prod don't
pollute each other:
- `environment=sdk-experiment` — eval runs (`agent/src/copilot/evals/experiment.py`). Count this as
  eval spend, not production unit cost.
- the configured `LANGFUSE_TRACING_ENVIRONMENT` (dev vs prod) — live `/chat` turns.

**What to pull** (skill step 1): total cost + trace count per environment; input/output/cache-read/
cache-write token distribution; per-turn cost mean/p50/p95; the `verification_grounding` score
distribution (retry/refusal signal); and the model actually used per trace (for the real tier mix —
see the tier note below).

If Langfuse is unreachable, fall back to `context/planning/estimated-token-spend.md` and mark
numbers "estimated, unreconciled."

## Pricing — verify via the `claude-api` skill

Re-fetch Anthropic's current catalog every run (**do not** price from memory). Pull input / output /
**cache-read / cache-write** per-Mtok for each tier below.

**Live pricing landmine:** Sonnet 5 is in an introductory window (**$2/$10 per Mtok**) **through
2026-08-31**, reverting to **$3/$15** after. A production projection that outlives August must model
the *standard* rate, or show both. State which you used.

## Model tiers (`agent/src/copilot/config.py` → `ModelTier`)

| Tier | Model ID | Role in routing |
|---|---|---|
| Haiku 4.5 | `anthropic:claude-haiku-4-5` | Cheap sub-tasks / verification pre-check; bulk eval iteration |
| Sonnet 5 | `anthropic:claude-sonnet-5` | Workhorse; **current default** (`model_tier` default) |
| Opus 4.8 | `anthropic:claude-opus-4-8` | Reserved for hard cases |

**Reality check (skill step 5, tiered-routing lever):** the tiers are declared but the walking
skeleton runs Sonnet-only. Measure the *actual* mix from Langfuse before claiming a routing split —
if it's ~100% Sonnet today, say so, and model the routing lever as a *planned* mix at higher tiers,
not a present one. This honesty is exactly what the PRD's "not flat token×N" requirement is after.

## Turn-type profiles (reconcile against actuals)

From `context/planning/estimated-token-spend.md` — replace these assumed counts with measured p50/p95
once ≥~50 real turns exist:

| Profile | Shape | Assumed in / out tokens |
|---|---|---|
| A — Light turn | 1 FHIR tool, demographics answer | ~2,000 / 300 |
| B — Full UC-1 turn | 5 FHIR tools fanned out, richer synthesis | ~8,000 / 600 |

The system prompt (~350 tok) + tool/output schemas dominate a light turn and are the stable,
**cacheable** prefix (lever #1). Resource payloads dominate a full turn.

## Workload model (skill step 4)

The interview benchmark (ARCHITECTURE.md §12) is a **500-bed hospital, ~300 concurrent clinical
users**. Convert users → turns with these coefficients (all go in the assumptions log; tune them):
- Turns per active physician per shift: a physician with a ~20-patient day opens the Co-Pilot a
  few times per patient → order ~40–80 turns/shift. State your pick.
- Active-user fraction and shift/working-day model for the monthly rollup.
- **Peak concurrency** is the sizing input, not daily volume — the agent service scales
  horizontally and independently of OpenEMR (§12), so concurrency sizes replicas + rate-limit
  headroom, not the clinical app.

## Architectural inflection points (skill step 6) — this app's known ceilings

Extend these; don't just restate them:
- **Prompt caching** becomes worth it once sustained turn rate keeps the stable prefix warm — off at
  100 users, on from the tier where the request rate clears the breakeven (lever #1).
- **The dependency-side ceiling is OpenEMR, not the model.** The audit's **audit-on-read write
  amplification** and **N+1 uncached list lookups** mean one patient summary can fire 40–60+ SQL
  queries, each doubled by two audit INSERTs. Under concurrent agent load this — not token spend —
  is the real ceiling. Mitigations (index remediation, `ExecuteNoLog` on hot read paths, a composite
  snapshot endpoint replacing per-resource round-trips) are an **OpenEMR-side dependency, not agent
  scope**; flag them as such (ARCHITECTURE.md §12).
- **Committed-use / provisioned throughput** only at 10K–100K where sustained volume justifies it.
- **Dev vs runtime billing:** Claude Max / Claude Code covers *dev-time* use, **not** the deployed
  service's programmatic API calls — production inference bills per-token. Never let the subscription
  hide runtime cost (ARCHITECTURE.md §12 note).

## Files

- **Reconcile against / update:** `context/planning/estimated-token-spend.md` (the estimate baseline;
  its own notes say to reconcile it against the first ~50 real turns).
- **Write the deliverable to:** `context/planning/cost-analysis.md`.
- **Cross-link:** `ARCHITECTURE.md` §12 (the architectural view that defers "the full numbers" to
  this deliverable) — and update §12's pointer if the filename changes.
- **Requirement source:** `PRD.md` → Submission Requirements → *AI Cost Analysis* ("Actual dev spend
  and projected production costs at 100 / 1K / 10K / 100K users, plus architectural changes needed at
  each level. Not simply cost-per-token × n users.").

## Reuse elsewhere

To point this skill at another project, rewrite this file only: swap the observability backend and
its query recipes (LangSmith/Braintrust/OTel instead of Langfuse), the pricing-verification source,
the model/tier table, the workload model, the app's inflection points, and the file paths. `SKILL.md`
and `references/levers.md` are provider- and project-agnostic and stay unchanged.
