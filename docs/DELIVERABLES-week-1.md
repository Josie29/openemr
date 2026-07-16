# Week-1 Deliverables — Production & Operations Evidence

A single map to the six production-polishing items for the AgentForge Clinical
Co-Pilot. Every item already exists in the repo — this page points to where each
one lives and how to verify it. (This doc sits in `/docs/` alongside the upstream
OpenEMR release docs. The core Week-1 design/persona/audit deliverables are the
root [ARCHITECTURE.md](../ARCHITECTURE.md), [USERS.md](../USERS.md), and
[AUDIT.md](../AUDIT.md), linked from the [README](../README.md).)

**Live prod agent:** `https://copilot-agent-production-eb24.up.railway.app`

| # | Deliverable | Where it lives | How to verify |
|---|-------------|----------------|---------------|
| 1 | Health & ready endpoints | [`agent/src/copilot/main.py`](../agent/src/copilot/main.py), [`health.py`](../agent/src/copilot/health.py); doc [`agent/README.md`](../agent/README.md#making-ready-green) | `curl <agent>/health` and `/ready` (below) — no login |
| 2 | Runnable API collection | [`agent/api-collection/`](../agent/api-collection/) (Bruno, 7 requests) | Open in Bruno, run 04→07 |
| 3 | Live dashboard evidence | [`context/planning/alerting.md`](../context/planning/alerting.md) §5a (6 Langfuse widgets) | Open the widget links (Langfuse login) |
| 4 | Alerts configured | [`context/planning/alerting.md`](../context/planning/alerting.md) §2–§5 (A1–A5, live) | Read the alert table below |
| 5 | Load test — 10 & 50 users | [`context/planning/loadtest-results.md`](../context/planning/loadtest-results.md) | Numbers inlined below |
| 6 | Exported eval results | [`agent/src/copilot/evals/README.md`](../agent/src/copilot/evals/README.md); CI [`evals.yml`](../.github/workflows/evals.yml) | Read the CI eval comment on any `qa→main` PR |

---

## 1. Health & readiness endpoints

`/health` (liveness, always 200 if the process is up) and `/ready` (200 only when
all five dependency probes — FHIR, LLM, Langfuse, Qdrant, Cohere — pass; 503 with a
per-dependency report otherwise). Defined in
[`agent/src/copilot/main.py`](../agent/src/copilot/main.py) (`/health`, `/ready`)
with probe logic in [`agent/src/copilot/health.py`](../agent/src/copilot/health.py);
documented in [`agent/README.md` → "Making `/ready` green"](../agent/README.md#making-ready-green).

Verify against live prod (no auth needed):

```bash
curl https://copilot-agent-production-eb24.up.railway.app/health
curl -s https://copilot-agent-production-eb24.up.railway.app/ready | jq
```

## 2. Runnable API collection

A [Bruno](https://www.usebruno.com/) collection at
[`agent/api-collection/`](../agent/api-collection/) that exercises the deployed agent
end to end without reading source: liveness, readiness, the auth boundary, and a
multi-turn grounded conversation. Seven requests — `01 Health`, `02 Ready`,
`03 Chat–No Token (401)`, `04 Auth–Refresh Token`, `05 Chat–New Turn`,
`06 Chat–Follow-up`, `07 Chat–Patient Mismatch (403)`. Run: install Bruno, copy
`environments/prod.example.bru` → `prod.bru` (credentials delivered with the
submission), select the `prod` env, run `04` then `05→07`. Full instructions in the
[collection README](../agent/api-collection/README.md). (A second FHIR-substrate
collection lives at [`agent/fhir-substrate/`](../agent/fhir-substrate/).)

## 3. Live dashboard evidence

A Langfuse "Clinical Co-Pilot — Ops" dashboard assembled from six pre-built widgets,
each linked in [`context/planning/alerting.md`](../context/planning/alerting.md)
§5a: **Turn volume**, **Turn latency p50/p95**, **Turn & tool errors**,
**Verification grounding rate**, **Cost per turn p95**, **Spend by model** (all
filtered to `environment = production`). The widget URLs open the live views.

> Note: these are live Langfuse links and require access to the Langfuse project
> (`cmrc3jeu000w3ad0cigwzi04s`, `us.cloud.langfuse.com`). The auth-free proof points
> are the `/health` + `/ready` endpoints (item 1) and the public CI eval runs (item 6).

## 4. Alerts

Five Langfuse Cloud monitors, **live** (created in Langfuse, Slack automation
attached). Each documents meaning + on-call response in
[`context/planning/alerting.md`](../context/planning/alerting.md) §2, with the
as-built config in §5. All evaluate over a rolling 1-hour window, `environment = production`.

| ID | Alert | Threshold | Severity |
|----|-------|-----------|----------|
| A1 | p95 latency breach | `chat-turn` p95 > 45s (warn) / 60s (page) | warn → page |
| A2 | Turn error count | `turn_error` count > 3 (warn) / 10 (page) | warn → page |
| A3 | Tool failure count | `tool_error` count > 3 | warn |
| A4 | Verification refusal spike | avg `verification_grounding` < 0.85 | page |
| A5 | Cost-per-turn spike | p95 `turn_cost` > $0.20 | warn |

## 5. Load / stress test — 10 & 50 concurrent users

Full writeup + method + infra profiles in
[`context/planning/loadtest-results.md`](../context/planning/loadtest-results.md);
Locust harness in [`agent/loadtest/`](../agent/loadtest/README.md). Deployed prod
agent, `POST /chat`, 2026-07-12. Both levels returned a **0.00% error rate**, no
timeouts:

| Level | Requests | Errors | p50 | p95 | p99 | Throughput |
|-------|----------|--------|-----|-----|-----|------------|
| 10 users | 23 | 0 (0.00%) | 19.0s | 32.0s | 38.0s | 0.42 req/s |
| 50 users | 129 | 0 (0.00%) | 17.0s | 34.0s | 47.0s | 2.18 req/s |

Median latency stayed stable across the 5× load increase (agent is
LLM-latency-bound, not resource-bound — peaked at 0.31 vCPU / 0.26 GB at 50 users).

## 6. Evaluation results

A Langfuse-hosted eval suite (7 cases against bundled FHIR fixtures, real Claude
model) scoring four metrics — `tool_correctness` and `no_fabrication` (deterministic),
`faithfulness` and `completeness` (Haiku LLM-as-judge). Run-level means are checked
against regression thresholds in `experiment.py` (`_THRESHOLDS`). Suite docs +
how-to-run in [`agent/src/copilot/evals/README.md`](../agent/src/copilot/evals/README.md).

**Verify without any login:** the CI workflow
[`.github/workflows/evals.yml`](../.github/workflows/evals.yml) runs the suite on
every release-promotion PR (`qa/integration → main` touching `agent/**`) and
**comments the scores on the PR** — read the eval comment on any such PR in the
GitHub history. Per-run detail (cost, per-case traces) lives in the Langfuse
**Experiments** table.
