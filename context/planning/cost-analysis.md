# AI Cost Analysis — Clinical Co-Pilot agent

**Date:** 2026-07-12. **Pricing source:** Anthropic model catalog, verified via the `claude-api`
skill on 2026-06-24 (Sonnet 5 in its introductory window through **2026-08-31**). **Actuals
sources:** (1) the JOS-18 load test — 152 controlled turns, [`loadtest-results.md`](loadtest-results.md);
(2) Langfuse project `cmrc3jeu000w3ad0cigwzi04s`, all organic/dev traffic to date (window
**Jul 8–12, 2026**). Reconciles and supersedes the assumptions in
[`estimated-token-spend.md`](estimated-token-spend.md); cited by
[`ARCHITECTURE.md` §12](../../ARCHITECTURE.md).

> This is a cost *model*, not a single number. Every projected figure traces back to a measured
> input and a stated coefficient (assumptions log). The PRD's requirement — **not flat
> cost-per-token × N** — is carried by the levers that bend the per-turn cost *down* as volume grows
> (caching, tiered routing, committed-use) and by the one that would blow it *up* if unmanaged (the
> runaway-agentic-turn tail — which the shipped tool-call cap has already bounded).

---

## Summary

Two measured datasets exist and they agree once you account for the tool-call cap that shipped
between them. **Post-cap, under realistic load (JOS-18, 152 turns), a production turn costs $0.057
avg with a tight distribution** (p50 $0.046 / p99 $0.141). **Pre-cap organic traffic (Langfuse, 87
turns)** shows what the cap fixed: two runaway agentic turns out of 38 drove ~77% of that sample's
spend (max $1.86/turn). So the dominant cost driver is **bounding the agentic tail**, and the
evidence says it is already bounded. On the $0.057 base (→ ~$0.083 at standard Sonnet pricing after
the intro window), with caching + tiered-routing + committed-use applied per tier, blended cost per
turn falls from ~**$0.083** at 100 users to ~**$0.026** at 100K users. Projected production LLM
spend: **~$6.6K/mo at 100 users → ~$2.1M/mo at 100K users** — sub-linear in per-turn cost,
super-linear only in raw volume. Above ~10K users the binding ceiling stops being tokens and becomes
the **OpenEMR/FHIR data path**, which the load test already measured as the infra pressure point
(agent 0.31 vCPU peak vs OpenEMR 0.93 / MySQL 0.80).

---

## Actual dev spend (measured)

### A. Load test — the projection base (post-cap, 152 turns)

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

**Measured per-turn token profile** (organic prod, blended): ~**47K input / 1.9K output** across
~3.7 model calls/turn — the Pydantic-AI tool loop resends the growing context on each call, so
**input dominates** (85% of Sonnet cost). **Base for projections = load-test $0.057 avg** (intro
pricing); at standard Sonnet pricing that is **~$0.083/turn** (Sonnet is 92% of cost × 1.5 uplift +
8% Haiku unchanged = 1.46×).

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

## Projections (levers applied per tier)

Base = **$0.083/turn** (load-test $0.057 avg → standard Sonnet pricing; tail already bounded by the
cap). Each tier stacks the levers that legitimately activate at that volume, so blended $/turn
**falls** while volume rises.

| Users | Turns/mo | Active levers (vs prior tier) | Blended $/turn | **$/mo** |
|---:|---:|---|---:|---:|
| **100** | 80K | Tool-call cap (tail control). Caching *off* (bursty, sub-breakeven). Routing 92/8 as-measured. | $0.083 | **~$6.6K** |
| **1,000** | 800K | **+ Prompt caching on** (sustained rate keeps the stable + resent-context prefix warm; ×0.65); routing tuned 80/20. | $0.054 | **~$43K** |
| **10,000** | 8M | + Caching mature (×0.9 more); routing 65/35 (cap Sonnet, push sub-tasks to Haiku); **batch API** for eval/backfill (−50% on that slice). | $0.042 | **~$340K** |
| **100,000** | 80M | + **Committed-use / provisioned throughput** (×0.75); semantic/response caching; **distill the grounding pre-check** onto a small model. | $0.026 | **~$2.1M** |

**Lever that moved the number most, per tier:** 100 → *tail control (the cap)*; 1K → *prompt
caching*; 10K → *aggressive routing + batching*; 100K → *committed-use + distillation*. Per-turn cost
drops **$0.083 → $0.026 (−69%)** across a 1000× volume increase — the non-linear bend the PRD
requires. At the Sonnet **intro** rate every figure is ~30% lower; a projection outliving 2026-08-31
must use the standard rate shown.

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
| Base $/turn (intro) | $0.057 avg | **Measured** (JOS-18, 152 turns, post-cap); high |
| Base $/turn (standard) | $0.083 | Derived from measured mix × standard pricing; **medium** |
| Production tier mix | 92% Sonnet / 8% Haiku | **Measured** (Langfuse); high |
| Grounding-retry rate | 27.5% ungrounded | **Measured** (Langfuse); high |
| Prompt-caching input reduction @≥1K | ~35% (×0.65) | Modeled (0.1× cache-read on stable+resent prefix); **medium** |
| Committed-use discount @100K | 25% (×0.75) | Typical range; **low** (contract-dependent) |
| Sonnet pricing basis for projections | Standard $3/$15 | Intro expires 2026-08-31; **high** |
| Cache-read/write split in actuals | none recorded | **Measured**: caching not yet enabled |

---

## Sensitivity (what the total is most exposed to)

1. **The agentic tail / cap integrity (highest sensitivity).** The cap is load-proven to bound the
   tail ($0.057 avg, p99 $0.141). If it regresses or is removed, per-turn reverts toward the pre-cap
   organic mean (**$0.106**, intro) — a **~1.9× swing on the entire bill** ($6.6K → ~$12.5K/mo at 100
   users, scaling through). This is the number to watch. Tightening the cap + cutting the 27.5% retry
   rate could instead pull the base below $0.045.
2. **Turns/user/month coefficient (±).** The projection is *linear* in this single coefficient:
   halving 800→400 halves every monthly figure; doubling doubles it. Biggest lever on the *absolute*
   total, and the softest assumption (active-user fraction and opens-per-patient are estimates).
3. **Prompt-caching realization.** If the resent-context prefix caches less than modeled (TTL expiry
   between bursty clinical turns, or a per-turn-varying prefix), the ×0.65 at 1K+ shrinks toward
   ×0.85, raising the 10K figure from ~$340K to ~$420K/mo. **Enabling and measuring cache hit rate is
   the cheapest de-risking action available today** — it is currently zero.

---

*Dev-time vs runtime billing: the ~$6 organic + ~$9 load-test spend above is real per-token API spend
on the deployed service. Claude Max / Claude Code covers **dev-time** use only — it does **not**
subsidize the deployed agent's programmatic calls (§12). Never let the subscription hide runtime cost.*
