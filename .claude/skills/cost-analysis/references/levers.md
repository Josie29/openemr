# The non-linear cost levers

These are why an honest projection is never `cost_per_token × N`. Each entry gives the mechanism,
the formula, and the volume/condition at which it starts to matter, so a projection can show
*which* lever bent the curve at each scale tier. Provider-agnostic — the numbers below are typical
ranges; use the live pricing pulled in the skill's step 2.

## 1. Prompt caching
**Mechanism.** The stable prefix of every request (system prompt + tool/output schemas + any fixed
context) is identical turn to turn. Providers let you cache it: the first request pays a *cache
write* premium; subsequent requests within the TTL pay a deeply discounted *cache read* on that
prefix and full price only on the variable suffix.

**Formula (input side).**
```
input_cost = cache_write_rate × cached_tokens              (first / cold)
           = cache_read_rate  × cached_tokens + input_rate × variable_tokens   (warm)
```
Typical: cache read ≈ 0.1× input rate; cache write ≈ 1.25× input rate.

**Breakeven.** Caching wins once a cached prefix is reused more than ~2–3× before its TTL expires.
That means it's near-useless at trickle volume and dominant at scale: model it *off* at the 100-user
tier and *on* from the tier where sustained request rate keeps the prefix warm. Model the write
premium and the TTL — don't just multiply input by 0.1.

**When it doesn't apply.** A prefix that changes every request (per-user system prompt, rotating
context) can't be cached. Restructure to hoist the stable part into the cacheable prefix if caching
matters.

## 2. Tiered model routing
**Mechanism.** Not every sub-task needs the top model. Route cheap/mechanical steps (classification,
extraction, a pre-check) to the cheapest tier, keep a mid tier as the workhorse, reserve the top
tier for genuinely hard cases. The **blended** rate — not any single tier — is what scales.

**Formula.**
```
blended_$/req = Σ  tier_fraction[i] × tier_cost_per_req[i]
```

**At scale.** The routing mix is itself a lever you tune *upward* as volume grows: at 100 users you
might run everything on the workhorse for simplicity; at 100K the pressure to push more fraction to
the cheap tier (and cap the top tier) is what keeps the bill defensible. Show the mix shifting
across tiers, not a fixed split.

**Discipline.** Measure the *actual* mix from observability (skill step 1) before assuming an
aspirational one. A design that says "we route to Haiku" but sends 100% to Sonnet in practice has no
routing lever yet — say so.

## 3. Retry / tool-loop overhead
**Mechanism.** Verification retries, tool-loop iterations, and validation failures re-invoke the
model, resending input each time. A 15% retry rate ≈ 15% more turns, weighted by which tier the
retry lands on.

**Formula.**
```
effective_$/req = base_$/req × (1 + retry_rate × retry_cost_multiplier)
```
`retry_cost_multiplier` ≈ the fraction of a full turn a retry costs (often ~1, sometimes less if the
retry is a cheap re-ground).

**Signal.** Retry rate is both a cost and a *quality* signal — a rising rate raises spend and flags
a regression. Pull it from observability, don't guess.

## 4. Batch API
**Mechanism.** Latency-tolerant work (eval runs, backfills, overnight summarization) can go through
a provider's batch endpoint at ~50% off, in exchange for asynchronous completion (minutes–hours).

**Applies to.** Evals, dataset regeneration, offline analytics — **not** interactive user turns,
which are latency-critical. In the projection, split workload into interactive (full price) vs
batchable (half price) and only discount the batchable slice.

## 5. Committed-use / provisioned throughput
**Mechanism.** At high sustained volume, providers offer committed-use discounts or provisioned
throughput (reserved capacity at a lower effective rate, sometimes with a latency/rate-limit
guarantee). Kicks in only at the top tiers where volume is predictable enough to commit.

**Model.** Apply only at 10K–100K where sustained volume justifies a commitment; note the tradeoff
(you pay for reserved capacity whether or not you use it, so it needs a stable floor of demand).

## 6. Volume-driven caching beyond the prompt
Distinct from prompt caching (#1): as volume grows, **response/semantic caching** (identical or
near-identical questions answered from a cache) and **context caching** (a shared corpus cached
once) start to pay off. These are architectural additions that appear as inflection points (skill
step 6), not just price discounts — model the eng cost of adding them, and the token savings after.

## 7. Output-token minimization
**Mechanism.** Output tokens are the expensive side (often 4–5× input rate). Structured output with
tight schemas, `max_tokens` caps, and "answer then stop" prompting cut the output bill directly.
Usually a fixed design choice rather than a scale lever, but worth stating as a standing control —
a verbose free-text answer costs multiples of a structured one for the same information.

## 8. Model choice / distillation at the top
At the highest tier, fine-tuning or distilling a smaller model on the hot path can beat calling a
frontier model per request — if volume amortizes the training cost and quality holds. This is a
100K-tier architectural option, not a day-one lever; flag it as a future inflection with a breakeven
("worth it above ~X requests/mo if quality parity holds"), don't bake it into the base projection.

---

**How to use these in a projection.** For each scale tier, walk this list and mark each lever
active / inactive / partial, then compute the blended per-request cost with the active ones applied.
The table's per-tier "lever note" names the one that moved the number most. A projection where the
same levers are active at 100 and 100K users is almost certainly wrong.
