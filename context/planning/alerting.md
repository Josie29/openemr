# Alerting — Clinical Co-Pilot agent

**Purpose:** Define the production alerts for the agent service and seed the PRD's
**Alert definitions** engineering requirement (≥3 alerts; each documents what it means and the
on-call response). The alerts below are specified against the signals the Langfuse
instrumentation emits (ARCHITECTURE.md §10). **Wiring status:** the definitions were the
Early-Submission deliverable; the code prerequisite (the `turn_error` / `tool_error` scores for
A2/A3) is now **merged**, so all five alerts are wire-ready. Creating the monitors + Slack
automation in the Langfuse UI is the remaining Final-push step — see §4.

**Alerting philosophy:** three of these map to the PRD's named operational alerts (latency,
error rate, tool failure); two more (A4, A5) map to *this* agent's specific failure modes —
trust and cost — which the PRD explicitly invites ("metrics relevant to the specific agent
design"). **Thresholds are calibrated to the observed production baseline** (22 live `chat-turn`
traces, 2026-07-10), so an alert means *degradation beyond normal*, not a standing breach —
recalibrate as traffic grows. Observed baseline: latency **p50 21.5s / p95 29s / p99 38s**, cost
**p50 $0.062 / p95 $0.092**. (Note the latency baseline itself sits well above the case study's
"answer in seconds" bar — that's a product-latency concern tracked separately, not something an
alert threshold should fire on every turn.)

---

## 1. Signal inventory — what the agent emits today

Every `POST /chat` turn is one Langfuse `chat-turn` trace (`observability.py`), carrying:

| Signal | Source | Alertable today? |
|---|---|---|
| Turn / span latency | OTel auto-instrumentation (generation + tool spans) | Yes |
| Token usage & cost | OTel auto-instrumentation | Yes |
| Tool calls (each FHIR read = one span) | `agent.py` `_track` tool wrappers | Yes |
| `verification_grounding` (0/1) | `observe_turn` → `score_trace` (`observability.py`) | Yes |
| `turn_error` (=1 on any failed turn) | `TurnTrace.errored()` in `/chat` handlers | Yes |
| `tool_error` (=1 on a FHIR read failure) | `TurnTrace.errored(tool_failure=True)` | Yes |

**Signal note — why explicit error scores, not span status.** The `/chat` route catches
`ModelHTTPError` / `FhirError` *inside* the `observe_turn` span, so the span closes cleanly and
would look successful; and a Langfuse Monitor can't filter observations down to "tool span +
error level" (monitor filters are model / tags / user / environment). So failed turns emit
explicit numeric scores — `turn_error` (every 502) and `tool_error` (the FHIR-read subset) — the
same monitorable mechanism `verification_grounding` already uses. `errored()` also sets the span
`level=ERROR` for trace-view/dashboard visibility. This closed the two gaps this doc previously
listed; A2/A3 are now wire-ready with no further code.

---

## 2. Alert definitions

Each monitor evaluates over a rolling **1-hour window** (the shortest Langfuse Monitor window;
options are 1h / 1d / 1w). Thresholds are **absolute counts, not rates** — Langfuse monitors one
metric against a threshold and can't natively divide errors ÷ total, and at demo traffic a count
over 1h is the less-noisy signal anyway (5% of 3 turns is noise). The count thresholds (A2/A3)
need no baseline; A1/A5 are calibrated to live production data (§2 header) — revisit as traffic
grows.

### A1 — p95 latency breach  *(PRD: p95 latency)*
- **Fires when:** `chat-turn` p95 latency > **45 s** (warn), > **60 s** (page).
- **Severity:** warn → page.
- **Monitor:** Observations data source → p95 latency, filtered to `name = chat-turn`.
- **Threshold basis:** observed p95 ≈ 29 s, p99 ≈ 38 s, max 40 s (2026-07-10). 45 s is ~1.5× the
  normal p95 — clearly abnormal without firing on routine slow turns; 60 s is severe degradation.
- **What it means:** turns are running materially slower than the ~30 s p95 baseline. Usual
  causes: FHIR read slowness, an over-long tool chain, grounding-gate retries, or model-provider
  latency. (Reducing the *baseline* itself toward the "seconds" bar is separate product work.)
- **On-call response:** open the slowest in-window trace in Langfuse and read which span dominates
  — FHIR span → check OpenEMR `/ready` and the FHIR endpoint; generation span → check Anthropic
  status; repeated grounding-gate retries → quality regression, escalate to the agent owner (see
  A4).

### A2 — Turn error count breach  *(PRD: error rate)*
- **Fires when:** `turn_error` count > **3** in 1h (warn at 3, page at 10).
- **Severity:** warn → page.
- **Signal:** numeric score `turn_error` (=1 on every 502 turn).
- **What it means:** the agent is failing to answer — LLM provider rejection (billing, rate
  limit, outage) or a FHIR read failure the agent degrades to "data temporarily unavailable."
- **On-call response:** hit the agent's `/ready` (breaks down FHIR / LLM / Langfuse); whichever is
  `ok:false` is the culprit. LLM → Anthropic status + API key/billing; FHIR → OpenEMR service.

### A3 — Tool failure count breach  *(PRD: tool failure rate)*
- **Fires when:** `tool_error` count > **3** in 1h.
- **Severity:** warning.
- **Signal:** numeric score `tool_error` (=1 on a FHIR read failure — the `turn_error` subset).
- **What it means:** the agent's data reads are failing; the physician sees "data unavailable"
  gaps, which erodes trust in the tool.
- **On-call response:** check the OpenEMR FHIR endpoint reachability and the SMART token path
  (expired/invalid token → all reads 401). Correlate with A2 — if `turn_error` ≈ `tool_error`,
  it's an OpenEMR-side outage, not the agent.

### A4 — Verification refusal spike  *(agent-specific: trust)*
- **Fires when:** avg `verification_grounding` < **0.85** in 1h (= >15% of turns refused).
- **Severity:** page (trust regression, not just an ops blip).
- **Monitor:** Numeric Scores data source → avg of `verification_grounding`, `<` operator. Needs
  **no code** — the score already flows on every turn.
- **What it means:** the model is increasingly producing claims it can't ground in the record, so
  the gate is refusing them. This is the failure mode the whole verification design exists to
  catch — a spike means answers are degrading in *quality*, not availability.
- **On-call response:** pull the refused traces in Langfuse; look for a common patient/question
  shape or a recent prompt/model change. A deploy correlation → consider rollback. Do **not**
  relax the gate to clear the alert.

### A5 — Cost-per-turn spike  *(agent-specific: cost)*
- **Fires when:** p95 cost/turn > **$0.20**.
- **Severity:** warning.
- **Monitor:** Observations data source → p95 cost, filtered to `name = chat-turn`.
- **Threshold basis:** observed p95 ≈ $0.092, max $0.13 (2026-07-10). $0.20 ≈ 2× the normal p95 —
  a genuine spike (runaway tool-chaining / mis-routed Opus turn / retry storm), not routine
  variance. Revisit if tier routing or the model mix changes.
- **What it means:** runaway tool-chaining, a tier-routing bug sending cheap turns to Opus, or a
  retry storm — each multiplies spend (ARCHITECTURE.md §12, `estimated-token-spend.md`).
- **On-call response:** in Langfuse, compare token/tool-count distribution vs baseline; check
  whether tier routing (`config.py` → `ModelTier`) is selecting the intended model and whether
  grounding-gate retries are inflating turn count.

---

## 3. Summary table

All monitors evaluate over a rolling **1-hour** window, filtered to `environment = production`.

| ID | Alert | Langfuse data source | Threshold | Severity | PRD requirement |
|---|---|---|---|---|---|
| A1 | p95 latency breach | Observations (p95 latency) | > 45 s (page > 60 s) | warn → page | p95 latency |
| A2 | Turn error count | Numeric score `turn_error` (count) | > 3 (page > 10) | warn → page | error rate |
| A3 | Tool failure count | Numeric score `tool_error` (count) | > 3 | warn | tool failure rate |
| A4 | Verification refusal spike | Numeric score `verification_grounding` (avg) | < 0.85 | page | (agent-specific) |
| A5 | Cost-per-turn spike | Observations (p95 cost) | > $0.20 | warn | (agent-specific) |

Thresholds calibrated from 22 live production turns (2026-07-10); revisit as traffic grows.

---

## 4. Wiring plan — Langfuse Cloud Monitors

All five alerts fire from **Langfuse Cloud → Monitors** against the trace/score data the agent
already emits — no Prometheus, Grafana, or `/metrics` endpoint needed. (An earlier draft weighed a
Grafana path; it's unnecessary now that Langfuse Monitors cover latency, cost, and score
thresholds natively. Monitors are Langfuse **Cloud-only** — fine, we're on Cloud.) The code
prerequisite — explicit `turn_error` / `tool_error` scores for A2/A3 — is **already merged** (see
§1); nothing else is blocking.

**Monitors/Automations are UI-only** — the Langfuse public API (verified via `langfuse-cli api
__schema`, 2026-07-10) exposes no monitors/automations/alerts resource, so these can't be scripted;
create them by hand in the UI. Thresholds are already calibrated (§3), so setup is mechanical.

**Per-alert setup** (Langfuse → *Monitors* → *New monitor*): configure each row of §3's table —
data source, metric, the threshold value (and the optional Warning threshold where §2 lists a
warn→page split), a **1-hour** window, and a filter of `environment = production` (this excludes
the `sdk-experiment` eval traces). For A1/A5 also filter `name = chat-turn`.

**Notification action** (Langfuse → *Automations* → *New automation* → **Slack**): create one
Slack automation and attach it to all five monitors. Page-severity alerts (A2 page, A4) and
warn-severity alerts can route to the same channel initially; split channels later if noise
warrants. Webhook and GitHub-Actions actions exist as alternatives but Slack is the least-effort
given our existing workspace. Langfuse auto-disables an automation after 5 consecutive delivery
failures, re-enabled from the Automations page.

**Dashboards (same tool, satisfies the PRD real-time dashboard requirement):** a Langfuse
Dashboard renders total requests, error rate (`turn_error`), p50/p95 latency, tool-call counts,
and verification pass/fail (`verification_grounding`) — build it alongside the monitors so the
alert and the dashboard read from one source.

**Remaining (Final push):** (1) deploy the `turn_error`/`tool_error` scores to prod (promote
`qa/integration → main`) so A2/A3 have live signal — A1/A4/A5 already have it; (2) create the five
monitors + the Slack automation in the Langfuse UI (thresholds per §3, all pre-calibrated); (3)
build the dashboard. No further code changes are required.
