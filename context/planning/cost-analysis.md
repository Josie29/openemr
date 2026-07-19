# AI Cost Analysis — Clinical Co-Pilot agent

**Date:** 2026-07-18 (Week-2 refresh; Week-1 sections retained as the labelled baseline).
**Pricing source:** Anthropic model catalog. The Sonnet 5 rate in force was **re-verified
empirically this refresh** by reconciling measured tokens against Langfuse's server-side cost:
`2,317,801 in × $2/Mtok + 172,167 out × $10/Mtok = $6.3573`, matching Langfuse's `totalCost` to
the cent — so the **introductory $2/$10 window (through 2026-08-31)** is confirmed live, and
`agent/src/copilot/pricing.py`'s table is correct, not stale.
**Actuals sources:** Langfuse project `cmrc3jeu000w3ad0cigwzi04s` — Week-2 window
**2026-07-14 → 07-19** (`environment=production`), plus the Week-1 datasets below.
Reconciles [`estimated-token-spend.md`](estimated-token-spend.md); cited by
[`ARCHITECTURE.md` §12](../../ARCHITECTURE.md) and
[`W2_ARCHITECTURE.md` §12](../../W2_ARCHITECTURE.md).

> **Two architectures, two baselines — do not blend them.** The Week-2 multi-agent graph reached
> prod on **2026-07-14** (`de08af937`). Every figure below is windowed to one architecture or the
> other. A window starting before 07-14 straddles both and halves the apparent per-turn cost.

> This is a cost *model*, not a single number. Every projected figure traces back to a measured
> input and a stated coefficient (assumptions log). The PRD's requirement — **not flat
> cost-per-token × N** — is carried by the levers that bend the per-turn cost *down* as volume grows
> (caching, tiered routing, committed-use) and by the ones that blow it *up* if unmanaged (the
> agentic tail, and in Week 2 the hop count).

---

## Summary

**Week 2 costs 2.8× per turn and 3× the p95 latency of Week 1, and the drivers are measured, not
inferred.** A Week-2 graph turn costs **$0.159 all-in** (total provider spend ÷ all turns; the
`turn_cost` score reads $0.145 but covers only answered turns — see the coverage gap below)
against Week-1's $0.057. The cause is **hop count**: 330 model generations over 40 turns ≈ **8.25
generations/turn**, versus ~3.7 for the Week-1 single agent — a router hop plus a worker run plus
an answerer, each re-sending context.

**The single largest lever is unused.** The token profile is **93% input** (2.32M in / 172K out),
which is the exact shape prompt caching is built for — and the usage breakdown shows
**zero cache-read and zero cache-write tokens**, so caching is not enabled at all.

On the $0.159 base (→ **$0.238** at standard Sonnet pricing after 2026-08-31), with caching,
tiered routing and committed-use applied per tier, blended per-turn cost falls from **$0.238** at
100 users to **~$0.071** at 100K. Projected LLM spend: **~$19K/mo at 100 users → ~$5.7M/mo at
100K**. Above ~10K users the binding ceiling remains the **OpenEMR/FHIR data path**, unchanged
from Week 1 — and Week 2 adds a *second* non-Anthropic cost line (rerank) that is negligible today
and material at 100K.

**Latency now breaches its own budget at the median.** Measured `chat-turn` p50 **35.0s** against
the inherited **<15s** target ([`ARCHITECTURE.md` §2](../../ARCHITECTURE.md)); p95 **101.8s**
against alert A1's 60s page threshold. See [`loadtest-results.md`](loadtest-results.md).

> **Sample-size caveat, stated up front.** The Week-2 window holds **n=40 turns** (30 with cost
> scores). A p95 over 40 samples is effectively the second-slowest turn. These figures are
> directionally sound and adequate for a projection base, but they are **indicative, not stable**,
> and should be re-measured once a few hundred production turns exist.

---

## Week-2 actuals (measured) — the current architecture

Window **2026-07-14 → 2026-07-19**, `environment=production`, Langfuse.

| Metric | Measured | vs Week-1 baseline |
|---|---|---|
| Turns (`chat-turn`) | **40** | 152 (load test) |
| Model generations | **330** → **8.25 / turn** | ~3.7 / turn |
| Tokens | **2,317,801 in / 172,167 out** (93% input) | — |
| Provider-priced spend | **$6.3573** (Sonnet 5 only) | ~$9.0 / 152 turns |
| **Cost / turn — all-in** | **$0.159** | **$0.057** (2.8×) |
| Cost / turn — `turn_cost` score | avg $0.145 · p50 $0.134 · p95 **$0.311** | avg $0.057 · p95 $0.116 |
| Latency `chat-turn` | p50 **35.0s** · p95 **101.8s** · p99 127.4s · mean 46.7s | p50 17–19s · p95 32–34s |
| Grounding (`verification_grounding`) | avg **0.811** | — |
| Errors | `turn_error` 2 · `tool_ceiling` 1 (of 40) | 0 |

**Per-tool latency** (same window): `attach_and_extract` p50 1.6s / p95 4.2s / p99 13.8s ·
`search_guidelines` p50 318ms / p95 3.0s · FHIR reads p50 0.4–2.4s. Every dependency is fast
relative to the turn: `chat claude-sonnet-5` alone runs p95 **20.1s** per generation, and there are
~8.25 of them. **The turn is model-bound and hop-bound, not dependency-bound.**

### Three measurement gaps that affect these numbers

1. **`turn_cost` under-reports spend by ~36%.** Scores sum to $4.3439 across 30 turns, but the
   provider charged $6.3573 across 40. `main.py` calls `turn.costed()` on the *answered* path only,
   so errored, ceilinged and abandoned turns consume tokens and emit no score. **The A5 cost alert
   therefore watches a number that is structurally low** — and even so, measured p95 ($0.311)
   already exceeds A5's $0.20 threshold. The all-in $0.159/turn used for projections divides total
   provider cost by *all* turns and does not have this bias.
2. **Non-Anthropic vendors are unpriced.** `pricing.py` prices Anthropic only; there is no cost
   accounting anywhere for **Mistral OCR** (document extraction), **Cohere Rerank**, or the
   **Qdrant** service. Volumes are tiny today (32 extractions, 35 rerank searches in the window) so
   the omission is immaterial at present scale — but it is a real gap in the projection at 10K+,
   modelled explicitly below and flagged as *estimated, unverified pricing*.
3. **Tier attribution is flat.** `turn_cost_usd(tier, usage)` prices a whole graph turn at
   `settings.model_tier`. Harmless today (prod measured **100% Sonnet 5**), but it would misprice
   any turn the moment a cheap-router split ships — which is the first lever below.

---

## Week-1 actuals (measured) — retained baseline

*The sections below measured the **Week-1 single agent** (`/chat`, FHIR tool loop, no graph, no
RAG, no ingestion). They are kept for the Week-1 → Week-2 comparison the engineering requirements
ask for, and are **superseded** as the projection base by the Week-2 numbers above.*

### A. Load test — the Week-1 projection base (post-cap, 152 turns)

From [`loadtest-results.md`](loadtest-results.md) (JOS-18), deployed prod agent, Jul 12 14:38–14:44
UTC, 10 then 50 concurrent users, `turn_cost` scores:

| Metric | Value |
|---|---|
| Total run spend | ~$9.0 over ~152 turns |
| **Cost/turn — avg** | **$0.057** |
| Cost/turn — p50 / p95 / p99 | $0.046 / $0.116 / **$0.141** |
| Error rate | 0.00% (both levels, no timeouts) |
| Latency p50 / p99 | ~17–19s / 38–47s |
| Throughput (1 replica) | 0.42 → **2.18 req/s** (10→50 users) |

The distribution is **tight** — p99 is 3.1× the median, not 30×. This is the same agent as the
organic sample below, but **with `agent_tool_calls_limit = 12` in effect**, and it is the sanctioned
per-turn figure the JOS-18 deliverable hands to JOS-20.

### B. Organic / dev traffic — tail, tier-mix & quality evidence (Langfuse)

Window Jul 8–12; **disjoint from the load test** (last trace 14:35 UTC, 3 min before the load run
started), so these are *different* turns — manual demo + dev, some **pre-cap**.

| Environment | Role | Turns | Total cost | Input tok | Output tok |
|---|---|---:|---:|---:|---:|
| `production` | live demo/manual | 38 | $4.012 | 1,802,545 | 72,293 |
| `development` + `default` | local dev | 49 | $1.094 | 1,126,644 | 64,143 |
| `sdk-experiment` | **eval** (not unit cost) | — | $0.889 | 623,627 | 52,992 |
| | **Live total** | **87** | **$5.106** | | |
| | **Grand total (incl. eval)** | | **$5.995** | | |

Production per-turn (exact percentiles, N=38): mean **$0.106**, p50 $0.062, p95 $0.134, **p99 $1.224,
max $1.858**. What this sample uniquely tells us:

- **The pre-cap tail — the cap's justification.** Removing the two turns above $1.20 drops the
  remaining 36-turn mean to **$0.026**: 5% of turns drove ~77% of the bill. The load test (post-cap,
  152 turns) shows the same agent with **no such tail** (p99 $0.141) — direct before/after evidence
  that the tool-call cap worked. `fix/agent-tool-call-cap` landed in this window.
- **Tier mix is 92% Sonnet 5 / 8% Haiku 4.5 by cost** (121 vs 19 billed generations) — *not*
  Sonnet-only. The Haiku slice is the grounding pre-check, so the tiered-routing lever is **already
  partially live in production**, correcting the estimate baseline's "walking-skeleton-is-Sonnet-only"
  assumption. Model IDs: `claude-sonnet-5`, `claude-haiku-4-5-20251001`; recorded prices confirm the
  Sonnet **intro** rate ($2/$10).
- **Cache hit rate: zero / unobservable.** Langfuse records only `input`/`output` buckets — no
  cache-read/write split — confirming **prompt caching is not yet enabled**. It is a *pure future
  lever*, not a current saving.
- **Grounding retry signal:** `verification_grounding` in production is **72.5% grounded (185/70)** →
  a **27.5% ungrounded rate**, each triggering a `ModelRetry` re-run. A cost multiplier *and* a
  quality metric.

---

## Per-unit economics & reconciliation vs the estimate

**Verified pricing (USD per 1M tokens):**

| Tier | Input | Output | Cache read (~0.1×) | Cache write 5m (~1.25×) |
|---|---:|---:|---:|---:|
| Haiku 4.5 | $1.00 | $5.00 | $0.10 | $1.25 |
| Sonnet 5 — intro (→2026-08-31) | $2.00 | $10.00 | $0.20 | $2.50 |
| Sonnet 5 — standard (after) | $3.00 | $15.00 | $0.30 | $3.75 |
| Opus 4.8 | $5.00 | $25.00 | $0.50 | $6.25 |

**Week-1 per-turn token profile** (organic prod, blended): ~**47K input / 1.9K output** across
~3.7 model calls/turn — the Pydantic-AI tool loop resends the growing context on each call, so
**input dominates** (85% of Sonnet cost). Week-1 projection base was load-test **$0.057 avg**
(intro pricing) → **~$0.083/turn** standard.

**Week-2 per-turn token profile — the current base.** ~**57.9K input / 4.3K output** across **8.25
model calls/turn** (2,317,801 in ÷ 40 turns; 330 generations ÷ 40). Same amplification mechanism as
Week 1, applied over more than twice the hops: the router re-reads the question each iteration, each
worker re-sends its own context, and the answerer re-sends every worker's claims.

| | Week 1 | Week 2 | Δ |
|---|---:|---:|---:|
| Model calls / turn | ~3.7 | **8.25** | 2.2× |
| Input tokens / turn | ~47K | **57.9K** | 1.2× |
| Output tokens / turn | ~1.9K | **4.3K** | 2.3× |
| **$ / turn (intro pricing)** | $0.057 | **$0.159** | **2.8×** |

**Base for Week-2 projections = $0.159/turn** (intro pricing, all-in) → **$0.238/turn** at standard
Sonnet pricing (100% Sonnet measured, so the full 1.5× uplift applies — no Haiku share to soften it,
unlike Week 1's 1.46×).

**Delta vs [`estimated-token-spend.md`](estimated-token-spend.md):** the estimate's "Full UC-1 turn"
assumed 8K in / 600 out / $0.022 (Sonnet intro). Measured is ~47K in / 1.9K out / $0.057 — the
estimate **under-counted tool-loop input amplification** (~6× on input) because it priced a single
model call, not the loop. The estimate file's "reconcile against the first ~50 real turns" caveat is
discharged here.

---

## Workload model

Users → turns (every coefficient tunable; see assumptions log). Benchmark from ARCHITECTURE §12: a
500-bed hospital, ~300 concurrent clinical users.

- **Turns/active user/shift:** 60 (a ~20-patient day, opened ~3×/patient; midpoint of the 40–80 range).
- **Active-user fraction:** 0.6 on a working day. **Working days/month:** 22. → **~800 turns / user / month.**
- **Peak concurrency** sizes replicas, not the token bill. The load test held **50 concurrent users on
  one replica at 2.18 req/s**; peak in-flight turns ≈ users with `wait=0`.

| Users | Turns/mo | Peak concurrent turns | Replicas @ ~50/replica |
|---:|---:|---:|---:|
| 100 | 80,000 | ~7 | 1 |
| 1,000 | 800,000 | ~70 | ~2 |
| 10,000 | 8,000,000 | ~700 | ~14 |
| 100,000 | 80,000,000 | ~7,000 | ~140 |

Replica count is cheap (agent is 0.31 vCPU/replica); the real scaling constraints are **LLM-provider
concurrency (ITPM/OTPM)** and **OpenEMR FHIR throughput** — see inflection points.

---

## Projections (levers applied per tier) — Week-2 base

Base = **$0.238/turn** (Week-2 measured $0.159 all-in → standard Sonnet pricing). Each tier stacks
the levers that legitimately activate at that volume, so blended $/turn **falls** while volume rises.

| Users | Turns/mo | Active levers (vs prior tier) | Blended $/turn | **$/mo** |
|---:|---:|---|---:|---:|
| **100** | 80K | Tool-call cap + per-tool budget (tail control). Caching *off* (bursty, sub-breakeven). Routing **100% Sonnet as-measured**. | $0.238 | **~$19.0K** |
| **1,000** | 800K | **+ Prompt caching on** (×0.65 — the 93%-input profile is the ideal shape); **+ cheap-tier router** (the router hop is ~1/3 of calls and needs no reasoning depth) → 80/20 blend. | $0.134 | **~$107K** |
| **10,000** | 8M | + Caching mature (×0.9); routing 65/35 (workers to Haiku, answerer stays Sonnet); **batch API** on the eval/backfill slice (−50% there). | $0.102 | **~$819K** |
| **100,000** | 80M | + **Committed-use / provisioned throughput** (×0.75); semantic/response caching; **distill the router** onto a small model. | $0.071 | **~$5.7M** |

**Lever that moved the number most, per tier:** 100 → *tail control*; 1K → **prompt caching**
(unused today, and the largest single win available); 10K → *aggressive routing + batching*; 100K →
*committed-use + distillation*. Per-turn cost drops **$0.238 → $0.071 (−70%)** across a 1000× volume
increase — the non-linear bend the PRD requires. At the Sonnet **intro** rate every figure is ~33%
lower; a projection outliving **2026-08-31** must use the standard rate shown.

**Why caching is the headline lever here and not in Week 1:** Week 2's turn is 93% input tokens
across 8.25 calls that re-send a largely identical prefix (system prompt, tool schemas, patient
context, accumulated worker claims). That is the exact shape cache-read pricing (0.1×) rewards. The
measured usage breakdown shows **no cache tokens of either kind**, so none of this discount is being
taken today.

### Non-Anthropic vendor costs — *estimated, unverified pricing*

Week 2 adds vendors that `pricing.py` does not account for. Volumes measured in the window are tiny
(32 extractions, 35 rerank searches), so today's omission is immaterial — but the slope is not.
**These rates were not verified this refresh** and must be confirmed before being relied on:

| Vendor | Unit | Today (measured volume) | At 100K users (80M turns/mo) |
|---|---|---|---|
| Cohere Rerank | per 1K searches | 35 searches ≈ $0.07 | ~24M searches (30% of turns) → **material, verify rate** |
| Mistral OCR | per page | 32 extractions ≈ cents | scales with *documents*, not turns — far slower growth |
| Qdrant (Railway) | fixed monthly | ~$5–10 | grows with corpus + replicas, not turns |

The structural point holds regardless of the exact rates: **rerank scales with turns and therefore
joins the per-turn unit cost at scale, while OCR scales with document volume and Qdrant is a fixed
floor.** Only the first belongs in a per-turn projection.

---

## Architectural inflection points (what breaks, what changes)

- **100 users:** One agent replica (load-tested to 50 concurrent at 0.31 vCPU). No caching needed.
  The cost guardrail is the **tool-call cap** — pre-cap, one runaway turn ($1.86) cost more than ~30
  typical turns; post-cap the load test shows p99 at $0.141. Priority: hold the cap and drive down the
  **27.5% grounding-retry rate** (each retry is a full re-run).
- **1,000 users:** **Prompt caching becomes mandatory** — breakeven cleared, and the tool loop's
  resent context is highly cacheable. Verify **LLM-provider rate-limit headroom** (ITPM/OTPM) — the
  load test proved the agent is provider-latency-bound, not CPU-bound, so provider concurrency is the
  first ceiling. Grounding-retry rate graduates from quality metric to budget line.
- **10,000 users:** Agent scales horizontally (~14 replicas, independent of OpenEMR per §12). **The
  binding ceiling is now the data path, and the load test measured it:** at 50 concurrent, OpenEMR
  peaked at 0.93 vCPU and MySQL at 0.80 vCPU while the agent sat at 0.31 — because each `/chat` fans
  out to multiple FHIR reads, and the audit's **audit-on-read write amplification** + **N+1 uncached
  list lookups** mean one summary fires 40–60+ queries, each doubled by two audit INSERTs. At ~700
  concurrent sessions this saturates the DB long before tokens matter. Mitigations (index remediation,
  `ExecuteNoLog` on hot read paths, a **composite snapshot endpoint** replacing per-resource FHIR
  round-trips) are an **OpenEMR-side dependency, not agent scope** (§12). Committed-use starts to pay.
- **100,000 users:** Provisioned throughput / committed-use with Anthropic; multi-region; **distill
  the grounding pre-check** onto a fine-tuned small model; add a **semantic/response caching layer**.
  The FHIR source becomes the hard ceiling — a read-replica or batch snapshot pipeline feeding the
  agent replaces per-turn live reads.

---

## Assumptions log

| Assumption | Value | Source / confidence |
|---|---|---|
| Turns per active user per shift | 60 | Midpoint of §12's 40–80 range; **medium** |
| Active-user fraction (working day) | 0.6 | Estimate; **low–medium** |
| Working days / month | 22 | Standard; **high** |
| → Turns / user / month | ~800 | Derived |
| Peak factor / replica capacity | 50 concurrent / replica | **Measured** (JOS-18); high |
| **Week-2 base $/turn (intro)** | **$0.159** all-in | **Measured** (Langfuse 07-14→07-19, $6.3573 ÷ 40 turns); **medium** — n=40 |
| **Week-2 base $/turn (standard)** | **$0.238** | Derived: 100% Sonnet × 1.5 uplift; **medium** |
| **Model calls / turn (Week 2)** | **8.25** | **Measured** (330 generations ÷ 40 turns); medium — n=40 |
| Week-1 base $/turn (intro / standard) | $0.057 / $0.083 | **Measured** (JOS-18, 152 turns, post-cap); high — superseded as base |
| Production tier mix | **100% Sonnet 5** (Week 2) | **Measured** (Langfuse); high |
| Grounding rate | 0.811 avg (Week 2) | **Measured** (Langfuse, n=37); medium |
| Rerank share of turns @scale | 30% | Estimate; **low** — no measured retrieval-rate-per-turn |
| Cohere / Mistral / Qdrant rates | not verified | **Estimated, unverified**; low — confirm before relying |
| Prompt-caching input reduction @≥1K | ~35% (×0.65) | Modeled (0.1× cache-read on stable+resent prefix); **medium** |
| Committed-use discount @100K | 25% (×0.75) | Typical range; **low** (contract-dependent) |
| Sonnet pricing basis for projections | Standard $3/$15 | Intro expires 2026-08-31; **high** |
| Cache-read/write split in actuals | none recorded | **Measured**: caching not yet enabled |

---

## Sensitivity (what the total is most exposed to)

1. **Hops per turn (highest sensitivity, and new in Week 2).** Per-turn cost is close to linear in
   model calls, and Week 2 measured **8.25/turn** against Week 1's 3.7. If routing discipline slips
   and the mean drifts to ~12, the base moves $0.159 → **~$0.23** (intro), a **~1.4× swing on the
   entire bill**. Conversely a mean of 6 would pull it to ~$0.116. The per-tool budget
   (`graph/budget.py`) and the single-dispatch-per-worker guard are what hold this number down; both
   are load-bearing cost controls, not just correctness controls.
2. **Sample size (n=40).** Every Week-2 figure rests on 40 turns over five days. The mean is far more
   trustworthy than the p95 here; a single runaway turn moves the tail materially. **This is the
   weakest input in the model** and the cheapest to fix — a few hundred production turns would settle
   it. Treat the projections as order-of-magnitude until then.
3. **Turns/user/month coefficient (±).** The projection is *linear* in this single coefficient:
   halving 800→400 halves every monthly figure. Biggest lever on the *absolute* total, and the
   softest of the workload assumptions.
4. **Prompt-caching realization.** Modeled at ×0.65 from 1K users. If the prefix caches less than
   assumed (TTL expiry between bursty clinical turns, or a prefix that varies per turn), that shrinks
   toward ×0.85 and the 10K figure rises from ~$819K to **~$1.0M/mo**. Because caching is currently
   **measured at zero**, this is entirely unrealized upside — and measuring a real hit rate is the
   single cheapest way to de-risk the projection.

---

## Reproducing these numbers

Every Week-2 figure comes from Langfuse, `environment=production`, window
**2026-07-14T00:00:00Z → 2026-07-19T23:59:59Z**:

| Figure | Query |
|---|---|
| Spend, tokens, generation count | `view=observations`, metrics `totalCost:sum`, `inputTokens:sum`, `outputTokens:sum`, `count:count`, dimension `providedModelName` |
| Cache tokens (returned none) | `view=observations`, metric `usageByType:sum`, dimension `usageType` |
| Turn count + latency percentiles | `view=observations`, metrics `count:count`, `latency:p50/p95/p99`, dimension `name` (row `chat-turn`) |
| `turn_cost` / grounding / errors | `view=scores-numeric`, metrics `count:count`, `value:p50/p95/avg/sum`, dimension `name` |

---

*Dev-time vs runtime billing: the $6.36 Week-2 (plus ~$6 organic + ~$9 load-test Week-1) spend above
is real per-token API spend on the deployed service. Claude Max / Claude Code covers **dev-time** use
only — it does **not** subsidize the deployed agent's programmatic calls (§12). Never let the
subscription hide runtime cost.*

*Project spend to date across all environments (2026-06-01 → 07-19): **~$38.7** — prod $32.19,
dev $4.25, eval/experiments $2.24.*
