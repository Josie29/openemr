---
name: cost-analysis
description: >-
  Produce or refresh an AI cost analysis for an LLM/agent application: actual dev spend pulled
  from observability, verified current model pricing, per-unit (per-turn / per-request) unit
  economics, and projected production cost at 100 / 1K / 10K / 100K users with the non-linear
  levers (prompt caching, tiered model routing, retry overhead, batching, committed-use) applied
  — plus the architectural changes each scale tier forces. Use when asked to build or update a
  cost analysis, project AI costs at scale, estimate production spend, size an LLM budget, or
  answer "what will this cost at N users." The discipline is: never flat cost-per-token × N.
allowed-tools:
  - Read
  - Write
  - Edit
  - Grep
  - Glob
  - Bash(npx langfuse-cli api * list *)
  - Bash(npx langfuse-cli api * get *)
  - Bash(npx langfuse-cli api __schema *)
  - Bash(bunx langfuse-cli api * list *)
  - Bash(bunx langfuse-cli api * get *)
---

# AI Cost Analysis

Turn an LLM/agent application's real usage into a defensible cost story: what it costs today, what
it will cost at scale, and what has to change architecturally at each step. The output is a
written deliverable, not a number — a hospital CTO (or a grader) should be able to trace every
projected figure back to a measured input and a stated assumption.

## Core discipline — the one rule

**Never flat cost-per-token × N.** A projection that multiplies a single per-turn cost by user
count is wrong and reads as unserious, because the levers that dominate real spend are
*non-linear*: prompt caching gets cheaper with volume, tiered routing changes the blended rate,
retries add overhead, batching halves latency-tolerant work, and committed-use pricing kicks in at
the top. Every scale tier in the output must show which levers are active and how they bend the
curve. See `references/levers.md` for each lever's formula and breakeven.

Two more standing rules:

1. **Actuals over assumptions.** Pull real per-request tokens and cost from the observability
   backend before modeling anything. Assumptions are the fallback, not the default — and every one
   that survives goes in an explicit assumptions log with its source.
2. **Prices move — verify at build time.** Never price from memory. Re-fetch the current model
   catalog every run (introductory windows expire, tiers get repriced). The project bindings say
   how.

## Project bindings

Everything project-specific — the observability backend and how to query it, the model tiers and
IDs, the workload model, where the deliverable is written, and which files to reconcile against —
lives in **`references/project.md`**. Read it first. To reuse this skill in another project, that
one file is what you rewrite; the method below and `references/levers.md` stay as-is.

## Workflow

Work these in order. Steps 1–3 establish grounded unit economics; 4–6 project and pressure-test;
7 writes the deliverable.

### 1. Pull actual spend from observability
Read `references/project.md` for the backend and query recipes, then pull, for the dev/eval period
to date, segmented by environment tag and by model:
- Total spend and total requests (→ mean cost/request).
- Token distribution per request: input, output, **and cache-read vs cache-write** if the backend
  separates them (it's the difference between a real and a fake caching story).
- Per-request **p50 and p95** cost/tokens — projecting off the mean alone hides the tail that
  dominates a peak-load bill.
- **Retry rate** and **tier mix** (what fraction of requests hit each model). Both are levers you
  model later, so measure them now.

If the backend is unreachable, say so in the output, fall back to the estimate file named in
`project.md`, and mark every downstream number "estimated, unreconciled."

### 2. Verify current pricing
Fetch the current per-Mtok input / output / **cache-read / cache-write** price for every tier in
use (recipe in `project.md`). Note any introductory or expiring pricing windows explicitly — a
projection that silently assumes a promo rate is a landmine.

### 3. Build per-unit economics
Reconcile measured tokens (step 1) against verified pricing (step 2) into a **cost per unit of
work** (per turn / per request), broken out by:
- **Turn/request type** if the workload has distinct shapes (e.g. a light lookup vs a full
  multi-tool synthesis) — the profiles are in `project.md`.
- **Model tier**, so the routing lever in step 5 has real per-tier numbers to blend.
State the delta between these actuals and the prior estimate, and update the estimate file.

### 4. Model the workload
From `project.md`'s workload model, convert users → requests: requests per active user per day ×
active-user fraction × working days, plus **peak concurrency** (not just daily volume — concurrency
sizes the infra and the rate-limit headroom). Keep every coefficient in the assumptions log.

### 5. Project across scale tiers with levers applied
For each of 100 / 1K / 10K / 100K users, compute monthly cost **with the active levers applied at
that tier**, not a scaled constant:
- **Tiered routing** — blend per-tier costs by the mix you'll actually run at that scale (cheap
  tier for sub-tasks, mid workhorse, top tier reserved). The blend shifts as you tune it upward.
- **Prompt caching** — once volume clears the breakeven, the stable system-prompt + tool-schema
  input goes to cache-read rates. Model the cache-write overhead and TTL, don't just discount.
- **Retry overhead** — add the measured retry rate as fractional extra turns.
- **Batching / committed-use** — apply where they legitimately kick in (see `levers.md`).
Present as a table: users → requests/mo → blended $/req → $/mo, with a one-line note on which lever
moved the number at each tier.

### 6. Identify architectural inflection points
For each scale tier, state **what breaks and what changes** — this is the part a naive analysis
skips and the part that gets graded. Rate-limit ceilings forcing a request queue, caching layers
becoming mandatory, a composite/batch endpoint replacing per-item round-trips, provisioned
throughput or committed-use contracts, multi-region, distillation/fine-tune of hot paths, and any
*dependency-side* ceiling (the data source, not the model). `project.md` lists this app's known
inflection points; extend them, don't just restate them.

### 7. Write the deliverable
Write to the path in `project.md` using the structure below. Cross-link the architectural doc and
the estimate file it reconciles. Lead with a short summary a busy reader can act on.

## Output structure

```
# AI Cost Analysis — <app>

## Summary            one paragraph: dev spend to date, headline $/req, and the
                      100→100K trajectory in one sentence with the dominant lever named.
## Actual dev spend   from observability: total $, total requests, $/req (mean, p50, p95),
                      token profile, cache-hit rate, retry rate, tier mix. Date + source stamped.
## Per-unit economics per-turn/request cost by type × tier; delta vs prior estimate.
## Workload model     users → requests conversion, peak concurrency, every coefficient.
## Projections        table: 100/1K/10K/100K → requests/mo → blended $/req → $/mo, lever notes.
## Architectural      per-tier: what breaks, what changes. Include dependency-side ceilings.
   inflection
## Assumptions log    every assumption, its value, and its source/confidence.
## Sensitivity        the 2–3 assumptions the total is most sensitive to, and the swing if wrong.
```

## Guardrails
- Stamp the date and pricing source on the analysis; prices and promo windows expire.
- Distinguish **dev-time tooling spend** (may be covered by a subscription) from **production
  per-token API spend** (always billed) — never let a subscription hide runtime cost.
- If a number is estimated rather than measured, label it inline. Don't launder assumptions into
  the actuals table.
- Keep the assumptions log exhaustive enough that someone can rerun the model with different
  coefficients — the model is the deliverable, the single total is not.
