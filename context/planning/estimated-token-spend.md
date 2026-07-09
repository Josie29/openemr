# Estimated Token Spend — Clinical Co-Pilot agent

**Purpose:** Back-of-envelope cost model for the agent across the three Claude tiers it routes
between (`config.py` → `ModelTier`), to size dev/test spend and seed the PRD's **AI Cost
Analysis** deliverable. These are **estimates from assumed token counts** — replace them with
the real per-turn tokens/cost the Langfuse instrumentation now records (ARCHITECTURE.md §10)
once live turns exist.

**Pricing source:** Anthropic model catalog, verified via the `claude-api` skill on
2026-07-08. **Verify at build time — prices move.** Sonnet 5 is in an introductory-price
window ($2/$10 per Mtok) **through 2026-08-31**; it reverts to $3/$15 after. Costs below ignore
**prompt caching**, which would cut the (stable) system-prompt + tool-schema input cost by
~90% on cache reads — a real lever the full cost analysis will model.

---

## 1. Per-token pricing (USD per 1M tokens)

| Model (tier) | Model ID | Input $/Mtok | Output $/Mtok |
|---|---|---:|---:|
| **Haiku 4.5** (cheap) | `claude-haiku-4-5` | $1.00 | $5.00 |
| **Sonnet 5** — intro (now → 2026-08-31) | `claude-sonnet-5` | $2.00 | $10.00 |
| **Sonnet 5** — standard (after 2026-08-31) | `claude-sonnet-5` | $3.00 | $15.00 |
| **Opus 4.8** (hard cases) | `claude-opus-4-8` | $5.00 | $25.00 |

## 2. Assumed token profile per turn

A "turn" = one `POST /chat` (Pydantic AI runs a tool loop, so input is resent across the
model calls in the loop). Two scenarios bracket the range:

| Scenario | What it is | Input tok/turn | Output tok/turn |
|---|---|---:|---:|
| **A — Light turn** *(current skeleton)* | 1 FHIR tool (`get_patient`), demographics answer. Conservative round-up of the measured ~1.6K in / ~220 out. | 2,000 | 300 |
| **B — Full UC-1 turn** *(target)* | 5 FHIR tools fanned out, richer context + longer synthesis. Med-heavy patients push higher. | 8,000 | 600 |

> These are illustrative. The system prompt (~350 tok) + tool/output schemas dominate a light
> turn; resource payloads (problem/med lists) dominate a full turn.

## 3. Cost per turn

| Model (tier) | A — Light | B — Full |
|---|---:|---:|
| Haiku 4.5 | $0.0035 | $0.0110 |
| Sonnet 5 (intro) | $0.0070 | $0.0220 |
| Sonnet 5 (standard) | $0.0105 | $0.0330 |
| Opus 4.8 | $0.0175 | $0.0550 |

*Under ~2¢ per turn even at the full-turn Sonnet-standard rate.*

## 4. Cost per 1,000 turns

| Model (tier) | A — Light | B — Full |
|---|---:|---:|
| Haiku 4.5 | $3.50 | $11.00 |
| Sonnet 5 (intro) | $7.00 | $22.00 |
| Sonnet 5 (standard) | $10.50 | $33.00 |
| Opus 4.8 | $17.50 | $55.00 |

## 5. What your $20 credit buys (approx. turns)

| Model (tier) | A — Light turns | B — Full turns |
|---|---:|---:|
| Haiku 4.5 | ~5,700 | ~1,800 |
| Sonnet 5 (intro) | ~2,850 | ~900 |
| Sonnet 5 (standard) | ~1,900 | ~600 |
| Opus 4.8 | ~1,140 | ~360 |

**Takeaway for the sprint:** even the most expensive combination (Opus, full turns) gives
~360 turns on $20 — plenty. Do bulk iteration on **Haiku** (`COPILOT_MODEL_TIER=anthropic:claude-haiku-4-5`),
where $20 is thousands of turns, and switch to **Sonnet** to judge answer quality.

---

## Notes / caveats

- **Estimates, not actuals.** Token counts are assumed; Langfuse now records true tokens +
  cost per trace — reconcile these against the first ~50 real turns and update this file.
- **Caching not modeled.** The system prompt and tool schemas are identical every turn — prompt
  caching makes those input tokens ~10× cheaper on reads, materially lowering the input side.
- **Tiered routing is the scale lever.** The 3-tier split (Haiku for cheap sub-tasks / the
  future verification pre-check, Sonnet as workhorse, Opus reserved) is what keeps the
  cost-at-100/1K/10K/100K-users projection defensible rather than flat token×N — the point the
  PRD's AI Cost Analysis must make (`ARCHITECTURE.md` §12).
- **Verification retries cost extra.** A `ModelRetry` from the grounding gate re-runs the model,
  adding roughly one more turn's tokens; a high retry rate is both a quality signal and a cost
  signal (visible in Langfuse).
