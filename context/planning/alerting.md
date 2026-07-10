# Alerting — Clinical Co-Pilot agent

**Purpose:** Define the production alerts for the agent service and seed the PRD's
**Alert definitions** engineering requirement (≥3 alerts; each documents what it means and the
on-call response). This is the *definition* layer — the alerts below are specified against the
signals the Langfuse instrumentation already emits (ARCHITECTURE.md §10). **Wiring status:**
definitions are the Early-Submission deliverable; live threshold-firing is scheduled for the
Final (Sunday) push — see §4.

**Alerting philosophy:** three of these map to the PRD's named operational alerts (latency,
error rate, tool failure); two more (A4, A5) map to *this* agent's specific failure modes —
trust and cost — which the PRD explicitly invites ("metrics relevant to the specific agent
design"). Thresholds are anchored to the case study's "answer in seconds, 90 seconds between
rooms" bar, not pulled from generic SRE defaults.

---

## 1. Signal inventory — what the agent emits today

Every `POST /chat` turn is one Langfuse `chat-turn` trace (`observability.py`), carrying:

| Signal | Source | Alertable today? |
|---|---|---|
| Turn / span latency | OTel auto-instrumentation (generation + tool spans) | Yes |
| Token usage & cost | OTel auto-instrumentation | Yes |
| Tool calls (each FHIR read = one span) | `agent.py` `_track` tool wrappers | Yes |
| `verification_grounding` (0/1) | `observe_turn` → `score_trace` (`observability.py:78`) | Yes |
| Turn errors (5xx) | `/chat` `except` blocks (`main.py`) | **Weak — see gap** |

**Two gaps to close before wiring (A2/A3 depend on the first):**

1. **Error-level not stamped on failed turns.** The `ModelHTTPError` / `FhirError` handlers in
   `/chat` return 502 but don't mark the Langfuse span error-level; only the gate-refusal path
   sets `verified(passed=False)`. Fix: stamp `level=ERROR` (or an `error` score) on the span in
   those handlers so A2/A3 have a countable signal. One-line change per handler, deferred to the
   wiring pass.
2. **Tool failures aren't aggregated.** A FHIR tool failure surfaces as a span exception but not
   as a rolled-up rate; the wiring pass adds the query/metric that A3 counts.

---

## 2. Alert definitions

Rolling **15-minute window**, minimum **5 turns** in-window before any rate/percentile alert can
fire (avoids single-request noise on a low-traffic demo service). Thresholds are starting points
to calibrate against the first ~50 live turns.

### A1 — p95 latency breach  *(PRD: p95 latency)*
- **Fires when:** `chat-turn` p95 latency > **8 s**.
- **Severity:** warning at 8 s, page at 15 s.
- **What it means:** physicians are waiting past the "answer in seconds" bar the product is built
  around. Usual causes: FHIR read slowness, an over-long tool chain, grounding-gate retries, or
  model-provider latency.
- **On-call response:** open the slowest in-window trace in Langfuse and read which span dominates
  — FHIR span → check OpenEMR `/ready` and the FHIR endpoint; generation span → check Anthropic
  status; repeated grounding-gate retries → quality regression, escalate to the agent owner (see
  A4).

### A2 — Turn error rate breach  *(PRD: error rate)*
- **Fires when:** share of turns returning 5xx > **5%**.
- **Severity:** page.
- **What it means:** the agent is failing to answer — LLM provider rejection (billing, rate
  limit, outage) or a FHIR read failure the agent degrades to "data temporarily unavailable."
- **On-call response:** hit the agent's `/ready` (breaks down FHIR / LLM / Langfuse); whichever is
  `ok:false` is the culprit. LLM → Anthropic status + API key/billing; FHIR → OpenEMR service.
- **Depends on:** gap #1 (error-level stamping) being wired.

### A3 — Tool failure rate breach  *(PRD: tool failure rate)*
- **Fires when:** FHIR tool-call failures > **10%** of tool calls.
- **Severity:** warning.
- **What it means:** the agent's data reads are failing even if some turns still answer; the
  physician sees "data unavailable" gaps, which erodes trust in the tool.
- **On-call response:** check the OpenEMR FHIR endpoint reachability and the SMART token path
  (expired/invalid token → all reads 401). Correlate with A2 — if both fire, it's an
  OpenEMR-side outage, not the agent.
- **Depends on:** gap #2 (tool-failure aggregation) being wired.

### A4 — Verification refusal spike  *(agent-specific: trust)*
- **Fires when:** `verification_grounding` false-rate > **15%**.
- **Severity:** page (trust regression, not just an ops blip).
- **What it means:** the model is increasingly producing claims it can't ground in the record, so
  the gate is refusing them. This is the failure mode the whole verification design exists to
  catch — a spike means answers are degrading in *quality*, not availability.
- **On-call response:** pull the refused traces in Langfuse; look for a common patient/question
  shape or a recent prompt/model change. A deploy correlation → consider rollback. Do **not**
  relax the gate to clear the alert.

### A5 — Cost-per-turn spike  *(agent-specific: cost)*
- **Fires when:** mean cost/turn > **2× rolling baseline**.
- **Severity:** warning.
- **What it means:** runaway tool-chaining, a tier-routing bug sending cheap turns to Opus, or a
  retry storm — each multiplies spend (ARCHITECTURE.md §12, `estimated-token-spend.md`).
- **On-call response:** in Langfuse, compare token/tool-count distribution vs baseline; check
  whether tier routing (`config.py` → `ModelTier`) is selecting the intended model and whether
  grounding-gate retries are inflating turn count.

---

## 3. Summary table

| ID | Alert | Threshold (15-min window) | Severity | PRD requirement |
|---|---|---|---|---|
| A1 | p95 latency breach | p95 > 8 s (page > 15 s) | warn → page | p95 latency |
| A2 | Turn error rate | 5xx > 5% | page | error rate |
| A3 | Tool failure rate | FHIR tool failures > 10% | warn | tool failure rate |
| A4 | Verification refusal spike | grounding false-rate > 15% | page | (agent-specific) |
| A5 | Cost-per-turn spike | mean cost/turn > 2× baseline | warn | (agent-specific) |

---

## 4. Wiring plan (Final / Sunday)

Definitions above are done; firing them needs a threshold-alerting surface Langfuse doesn't
natively provide for percentile/rate thresholds. Two paths, to decide during the Final push:

- **Path A — Langfuse-native where it fits.** Dashboards for the metrics; Langfuse evaluators for
  the score-based A4. Cheapest, but weak on p95/rate threshold *paging*.
- **Path B — `/metrics` + external rules.** Add a Prometheus `/metrics` endpoint to the agent and
  express A1–A3/A5 as Railway/Grafana alert rules. More work, proper paging.

**Prerequisites regardless of path:** close signal gaps #1 and #2 in §1 so A2/A3 have countable
signals. Recommended order: gaps → Path A dashboards + A4 → Path B if time permits.
