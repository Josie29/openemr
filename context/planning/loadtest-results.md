# Latency Results — Co-Pilot `/chat`

> ## ⚠️ Two architectures on this page — read the labels
>
> **Everything below the "Week-1" heading measured the Week-1 single agent.** The Week-2
> multi-agent graph reached prod on **2026-07-14** (`de08af937`) and is **~3× slower at p95**. The
> Week-1 synthetic load test has **not** been re-run against it — see *Week-2 latency* immediately
> below for what is measured, and *Why no Week-2 load test* for why.

---

## Week-2 latency (current architecture) — from real production traffic

**Source:** Langfuse, `environment=production`, window **2026-07-14 → 2026-07-19**. **n=40 turns.**
No synthetic load; these are real turns.

| Metric | Week-2 | Week-1 baseline (load test) |
|---|---|---|
| `chat-turn` p50 | **35.0s** | 17–19s |
| `chat-turn` p95 | **101.8s** | 32–34s |
| `chat-turn` p99 | **127.4s** | 38–47s |
| `chat-turn` mean | **46.7s** | 17.9–20.0s |
| Errors | `turn_error` 2 · `tool_ceiling` 1 (of 40) | 0.00% (of 152) |
| Throughput @10 / @50 users | **0.18 / 0.84 req/s** *(derived — see below)* | 0.42 / 2.18 req/s *(measured)* |
| CPU / memory | measured — see infrastructure profile below | 0.31 vCPU / 0.26 GB peak |

**Against the budget:** the inherited target is **<15s** ([`ARCHITECTURE.md` §2](../../ARCHITECTURE.md)).
Week 2 misses it **at the median**, not just the tail. Alert thresholds set against Week-1 behaviour
are now breached — A1 pages at p95 >60s (measured 101.8s), A5 warns at p95 `turn_cost` >$0.20
(measured $0.311), A4 floors grounding at 0.85 (measured 0.811). `alerting.md` anticipated this and
carries an explicit RE-BASELINE warning; these measurements discharge it.

> **n=40.** A p95 over 40 samples is effectively the second-slowest turn. Directionally reliable,
> not statistically stable.

### Where the time goes

| Component | p50 | p95 | p99 |
|---|---:|---:|---:|
| `chat-turn` (end to end) | 35.0s | 101.8s | 127.4s |
| `chat claude-sonnet-5` (per generation) | 1.9s | **20.1s** | 31.0s |
| `attach_and_extract` | 1.6s | 4.2s | 13.8s |
| `search_guidelines` | 318ms | 3.0s | 5.2s |
| FHIR reads (`get_*`) | 0.4–3.1s | 0.5–3.7s | ≤3.7s |

**The turn is model-bound and hop-bound.** Every dependency is fast relative to the whole: the
slowest non-model call (`attach_and_extract` p99 13.8s) is a tenth of the turn's p99. With **8.25
model generations per turn** at p95 20.1s each, model time dominates — end-to-end latency tracks
*hop count × model latency*, not any single slow dependency.

**Not measurable yet — the routing-vs-worker split.** `route:*` spans in prod report p50 0ms / p95
1ms because prod still runs the pre-nesting tracing, which closed each hand-off span before the
worker ran. The fix (nested `supervisor → route → worker → sub-call` spans) is on `qa/integration`
(`1dd9dd9d1`) and unlanded. Once promoted, per-hand-off cost becomes directly queryable and this
section should be re-measured.

### Week-2 throughput — derived from measured latency

Throughput is the one figure real traffic cannot supply directly (40 organic turns over five
days is not a concurrency test). It is instead **derived** from Week-2's measured mean turn latency
using Little's Law, **calibrated against the Week-1 load test** so the derivation carries that run's
real-world overhead rather than assuming an ideal system.

**Step 1 — calibrate on Week 1, where both sides are measured.** Little's Law gives ideal
throughput `X = N / R` for `N` concurrent users at mean turn latency `R`:

| Level | Mean latency `R` | Ideal `N/R` | **Measured** | Realised efficiency |
|---|---:|---:|---:|---:|
| 10 users | 20.0s | 0.50 req/s | 0.42 req/s | **84%** |
| 50 users | 17.9s | 2.79 req/s | 2.18 req/s | **78%** |

The 16–22% shortfall is connection setup, spawn ramp and response handling — overhead any real
client pays. Efficiency degrades slightly with concurrency, as expected.

**Step 2 — apply to Week 2's measured mean of 46.7s**, carrying the same per-level efficiency:

| Level | Ideal `N/R` | × efficiency | **Derived throughput** | vs Week 1 |
|---|---:|---:|---:|---:|
| 10 users | 0.214 req/s | × 0.84 | **0.18 req/s** | 43% |
| 50 users | 1.071 req/s | × 0.78 | **0.84 req/s** | 38% |

**Week-2 throughput is roughly 40% of Week-1's** — consistent with a mean turn that is 2.3–2.6×
longer, which is exactly what 8.25 model generations per turn (vs ~3.7) predicts. Throughput here is
the reciprocal of latency, and latency is hop-count-bound.

**Why the agent is not the constraint.** The infrastructure profile below shows the service peaking
at **0.041 vCPU of 8** and **985 MB of 8 GB** under organic load; Week 1 sustained 50 concurrent
users at 0.31 vCPU. There is no resource ceiling anywhere near these levels — each concurrent turn
is a mostly-idle process waiting on a model response. The real ceiling at 50+ concurrent is
**provider-side**: 50 in-flight turns × 8.25 generations means sustained parallel demand on
Anthropic ITPM/OTPM limits, which is a rate-limit question, not a capacity one.

> **Basis:** derived from measured inputs (Week-2 mean latency, Week-1 calibration), not from a
> Week-2 load run. Little's Law assumes steady state and stable mean service time; with a p95 of
> 101.8s against a 46.7s mean, the distribution is right-skewed, so real throughput under sustained
> load would likely land modestly **below** these figures. Treat them as an upper-ish bound. A
> confirming run costs ~$12 of live spend (method in the Week-1 section — `LEVELS="10 50"
> DURATION=60s agent/loadtest/run.sh`).

### Week-2 baseline infrastructure profile

**Source:** `railway metrics -s <service> -e production --since 5d --json`, window
**2026-07-14 15:20 → 2026-07-19 15:20 UTC** — the same Week-2 window as the latency numbers.

| Service | CPU avg / max (vCPU) | Memory avg / max | Limit | Util |
|---|---|---|---|---|
| **copilot-agent** | 0.0025 / **0.041** | **419 MB / 985 MB** | 8 vCPU · 8 GB | 5.7% mem |
| openemr | 0.0052 / 0.045 | 874 MB / 4,520 MB | 8 vCPU · 8 GB | 7.9% mem |
| MySQL | 0.0026 / 0.008 | 1,103 MB / 1,176 MB | 8 vCPU · 8 GB | 14.4% mem |
| qdrant | 0.0014 / 0.002 | 158 MB / 255 MB | 8 vCPU · 8 GB | 2.6% mem |

**Read these as an idle-to-light-traffic baseline, not a saturation profile.** They cover organic
traffic (~40 turns over five days), whereas Week-1's figures came from 50 concurrent synthetic
users. **CPU is therefore not comparable across the two** — Week-1's 0.31 vCPU peak was measured
under deliberate load, Week-2's 0.041 under none. Reading "Week 2 uses less CPU" from this table
would be wrong.

**Memory is comparable, and it moved:** the agent's average resident set is **419 MB against
Week-1's ~160 MB**, roughly 2.6×, with a 985 MB peak. The likely cause is in-process embedding:
`qdrant-client[fastembed]` loads dense and sparse embedding models into the agent's own memory
(`agent/src/copilot/rag/retriever.py`), which Week 1 had no equivalent of. This is a **standing**
cost — it scales with replica count, not with traffic — so it changes per-replica sizing even
though headroom today is vast (5.7% of an 8 GB limit).

**Qdrant is cheap** (158 MB avg, 0.0014 vCPU) on a 55-chunk corpus, as expected for an index that
small.

### Method note — how Week 2 was measured without a synthetic load run

The Week-2 PRD's *Cost and Latency Report* asks for "actual dev spend, projected production cost,
p50/p95 latency, and bottleneck analysis." The 10-and-50-user stress run was a *Week-1* requirement
and is retained below as the baseline. Week 2 covers the same ground from three sources:

| Figure | Source |
|---|---|
| Latency p50/p95/p99, per-component breakdown | **Measured** — Langfuse, 40 real production turns |
| CPU / memory, all four services | **Measured** — Railway metrics, same window |
| Throughput @10 / @50 users | **Derived** — Little's Law on measured mean latency, calibrated on the Week-1 run |
| Cost per turn | **Measured** — Langfuse provider-priced spend ÷ turns |

Real traffic gives better latency evidence than a synthetic run would (it is what physicians
actually experienced), and it costs nothing. A confirming load run would firm up the one derived
row at **~$12** of live Sonnet spend — worth doing before a capacity commitment, not before a
demo.

---

# Week-1 baseline — Load / Stress Test Results

**Issues:** JOS-18 (Load/stress tests: 10 & 50 concurrent users → p50/p95/p99 + error rate);
JOS-19 (Baseline infrastructure profiles under load — see the profiles section below).
**Feeds:** JOS-20 (AI cost analysis).
**Run date:** 2026-07-12, ~09:38–09:44 CDT (14:38–14:44 UTC)
**Target:** deployed prod agent `https://copilot-agent-production-eb24.up.railway.app` (`POST /chat`)
**Patient:** Adrian Becker (`a234013f-932b-434c-8f21-9edc54ff3892`), SMART patient-scoped token.
**Architecture measured:** **Week-1 single agent** — FHIR tool loop, no supervisor graph, no RAG,
no document ingestion. Superseded by the Week-2 section above.

## Summary

The deployed Clinical Co-Pilot was load-tested at **10 and 50 concurrent users** using the
Locust harness in `agent/loadtest/`. Both levels returned a **0.00% error rate** with **no
timeouts**. Median latency stayed stable (~17–19s) as load rose 5×; only tail latency grew
(p99 38s → 47s). The agent proved **LLM-latency-bound, not resource-bound** — it peaked at
0.31 vCPU / 0.26 GB RAM while serving 50 concurrent users. Total measured LLM spend for the
whole exercise was **~$9**.

## Method

- **Harness:** `agent/loadtest/` (Locust). Each simulated user posts a random clinical
  question from a 15-question corpus to `POST /chat`, waits for the full non-streaming JSON
  answer, then immediately fires the next (`wait_time = constant(0)`, so in-flight requests ≈
  user count). A response counts as success only if HTTP 200 **and** carries a `summary` field.
- **Levels:** 10 users, then 50 users, spawn rate 10/s, **60s each** (trimmed from a longer
  run to control cost — still satisfies the "at least 10 and 50 concurrent" requirement).
- **One `/chat` request = one full agentic turn** (FHIR tool loop + grounding-gate retries),
  fresh conversation each request.
- **Reproduce:** `LEVELS="10 50" DURATION=60s agent/loadtest/run.sh`
- **Raw artifacts (gitignored, local only):**
  - `agent/loadtest/results/20260712-093754/` (10 users)
  - `agent/loadtest/results/20260712-094320/` (50 users)

## Results — latency, error rate, throughput

Percentiles are from the Locust `*_stats.csv` (authoritative artifact). Locust reports
histogram-approximated percentiles, hence the rounded values.

| Level        | Requests | Errors      | p50   | p95   | p99   | Avg    | Max    | Throughput |
|--------------|----------|-------------|-------|-------|-------|--------|--------|------------|
| 10 users     | 23       | 0 (0.00%)   | 19.0s | 32.0s | 38.0s | 20.0s  | 37.6s  | 0.42 req/s |
| 50 users     | 129      | 0 (0.00%)   | 17.0s | 34.0s | 47.0s | 17.9s  | 54.4s  | 2.18 req/s |

(Request counts are final-flush totals; a few in-flight requests at shutdown are excluded.)

## Baseline infrastructure profiles (JOS-19)

Railway service metrics for all three services in the stack, 1h lookback at 60s samples,
spanning the load-test window. **The test was the only sustained load in the window, so the
`peak` column is the "under load" signal**; `avg` is diluted by idle minutes on either side.
Together with the latency (p50/p95/p99) and throughput (req/s) in the Results table above,
this covers all four dimensions the requirement names (CPU, memory, latency, throughput).

| Service        | CPU avg / peak (vCPU) | Memory avg / peak (GB) | Net RX peak (GB/min) | Net TX peak (GB/min) |
|----------------|-----------------------|------------------------|----------------------|----------------------|
| copilot-agent  | 0.009 / **0.31**      | 0.16 / **0.26**        | 0.015                | 0.054                |
| openemr        | 0.059 / **0.93**      | 1.58 / 5.73\*          | 0.434                | 0.009                |
| MySQL          | 0.022 / **0.80**      | 0.93 / 1.08            | —                    | —                    |

\* openemr's 5.73 GB memory peak includes a redeploy/boot that preceded the run; load-driven
memory sat in the ~1–3 GB range. copilot-agent and MySQL figures are clean of that.

**Reading:**
- **The agent process is the cheapest tier under load** — 0.31 vCPU / 0.26 GB at 50 concurrent
  users. It spends each request waiting on the LLM, not computing.
- **The load lands hardest on the FHIR data path.** openemr peaked at 0.93 vCPU and MySQL at
  0.80 vCPU because each `/chat` fans out to multiple FHIR reads. This — not the agent — is the
  infra pressure point and the first thing to scale for more concurrency.
- A single Railway replica per service absorbed 50 concurrent users without autoscaling.

**Using this as a comparison baseline:** re-run `agent/loadtest/run.sh` after any agent or
infra change and diff the new numbers against this table plus the latency/throughput results
above. A regression shows up as higher peak CPU/memory at equal concurrency, or degraded
p95/p99 latency and throughput.

## Cost (measured)

From Langfuse `turn_cost` scores over the run window (both levels + 2 smoke tests):

| Metric                    | Value                       |
|---------------------------|-----------------------------|
| Total run spend           | **~$9.0** (~152 turns)      |
| Cost/turn — avg           | **$0.057**                  |
| Cost/turn — p50 / p95 / p99 | $0.046 / $0.116 / $0.141  |

Per-turn cost is load-independent (each request does the same LLM work regardless of
concurrency), so this $0.057 avg is a stable per-request figure for projection.

## Findings

1. **Zero errors at both levels, no timeouts** (max 54s against a 120s client cap). The agent
   handles 50 concurrent clinical users reliably.
2. **Median latency is stable under load** (~17–19s across a 5× user increase); degradation
   appears only in the tail (p99 38s → 47s) — the expected signature of headroom, not saturation.
3. **Throughput scales near-linearly** — 0.42 → 2.18 req/s for 10 → 50 users (5.3×).
4. **Agent is LLM-latency-bound, not resource-bound** — 0.31 vCPU / 0.26 GB peak. Vertical
   scaling of the agent container would not help; scaling levers are LLM provider concurrency
   and OpenEMR FHIR throughput.
5. **Measured cost/turn is $0.057 avg** — the empirical input for the JOS-20 cost analysis,
   replacing the earlier estimate in `estimated-token-spend.md`.

## Feeds JOS-20

- Per-request unit cost: **$0.057 avg** ($0.046 p50 / $0.116 p95 / $0.141 p99).
- Throughput ceiling observed on one replica: **~2.2 req/s** at 50 concurrent (LLM-latency-bound).
- Infra headroom: agent compute is negligible; production scaling cost is dominated by LLM
  spend and by the number of agent replicas needed to hold latency as concurrency grows.

## Caveats

- **60s per level** (cost-controlled). p99 at the 50-user level derives from ~129 samples —
  usable but approximate; p50/p95 are solid.
- Percentiles are Locust's histogram approximation (nearest ~1s bucket).
- Prompt caching is **not** enabled today, so per-turn cost has no cache discount yet.
- Single Railway replica, no autoscaling; results describe that topology.
