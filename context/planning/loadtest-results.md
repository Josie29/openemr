# Load / Stress Test Results — Co-Pilot `/chat`

**Issue:** JOS-18 (Load/stress tests: 10 & 50 concurrent users → p50/p95/p99 + error rate)
**Feeds:** JOS-20 (AI cost analysis) and the PRD "baseline infrastructure profiles" requirement.
**Run date:** 2026-07-12, ~09:38–09:44 CDT (14:38–14:44 UTC)
**Target:** deployed prod agent `https://copilot-agent-production-eb24.up.railway.app` (`POST /chat`)
**Patient:** Adrian Becker (`a234013f-932b-434c-8f21-9edc54ff3892`), SMART patient-scoped token.

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

## Baseline infrastructure profiles

Railway service metrics over the run window (1h lookback, 60s samples). `copilot-agent`
handled only the load and its numbers are clean; the `openemr` memory peak also reflects a
pre-run redeploy/boot, so it overstates load-driven usage.

| Service        | CPU avg / peak (vCPU) | Memory avg / peak (GB) | Network TX peak |
|----------------|-----------------------|------------------------|-----------------|
| copilot-agent  | 0.009 / **0.31**      | 0.15 / **0.26**        | 0.054 GB/min    |
| openemr        | 0.059 / 0.93          | 1.32 / 5.73*           | —               |

\* openemr peak memory includes the redeploy that preceded the run, not purely load.

**Reading:** at 50 concurrent users the agent used ~1/3 of a single vCPU and ~260 MB. The
compute bottleneck is not the agent process — it is LLM provider latency/concurrency and the
downstream OpenEMR FHIR calls. A single Railway replica absorbed the load without autoscaling.

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
