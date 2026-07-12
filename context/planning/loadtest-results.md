# Load / Stress Test Results — Co-Pilot `/chat`

**Issues:** JOS-18 (Load/stress tests: 10 & 50 concurrent users → p50/p95/p99 + error rate);
JOS-19 (Baseline infrastructure profiles under load — see the profiles section below).
**Feeds:** JOS-20 (AI cost analysis).
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
