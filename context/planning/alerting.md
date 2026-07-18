# Alerting — Clinical Co-Pilot agent

**Purpose:** Define the production alerts for the agent service and seed the PRD's
**Alert definitions** engineering requirement (≥3 alerts; each documents what it means and the
on-call response). The alerts below are specified against the signals the Langfuse
instrumentation emits (ARCHITECTURE.md §10). **Wiring status: live.** All five monitors are created
in Langfuse Cloud and a Slack automation is attached to them. The code prerequisites — the
`turn_error` / `tool_error` scores (A2/A3) and the `turn_cost` score (A5) — ship in the agent; A1
and A4 already carry live production signal, and A2/A3/A5 populate as failing/costed turns occur in
the deployed `copilot-agent` service. §5 is the as-built configuration.

**Alerting philosophy:** three of these map to the PRD's named operational alerts (latency,
error rate, tool failure); two more (A4, A5) map to *this* agent's specific failure modes —
trust and cost — which the PRD explicitly invites ("metrics relevant to the specific agent
design"). **Thresholds are calibrated to the observed production baseline** (22 live `chat-turn`
traces, 2026-07-10), so an alert means *degradation beyond normal*, not a standing breach —
recalibrate as traffic grows. Observed baseline: latency **p50 21.5s / p95 29s / p99 38s**, cost
**p50 $0.062 / p95 $0.092**. (Note the latency baseline itself sits well above the case study's
"answer in seconds" bar — that's a product-latency concern tracked separately, not something an
alert threshold should fire on every turn.)

> **⚠️ RE-BASELINE — the A1/A5 baselines are Week-1-derived and provisional.** The p95 latency
> (~29s → A1) and p95 `turn_cost` (~$0.092 → A5) baselines were measured on 22 **single-agent**
> turns (2026-07-10) = one LLM call per turn. `/chat` now runs the Week-2 supervisor graph, where a
> turn makes ~3–5 LLM calls (router + N worker hops + answerer), so **both p95 latency and p95
> `turn_cost` will step up materially**. Treat the A1 and A5 thresholds below as provisional until
> re-baselined against accumulated graph traffic; the A2/A3/A4 thresholds (counts / grounding avg)
> are unaffected.

---

## 1. Signal inventory — what the agent emits today

Every `POST /chat` turn is one Langfuse `chat-turn` trace (`observability.py`), carrying:

| Signal | Source | Alertable today? |
|---|---|---|
| Turn / span latency | OTel auto-instrumentation (generation + tool spans) | Yes |
| Token usage & cost | OTel auto-instrumentation | Yes |
| Tool calls (each FHIR read = one span) | `register_fhir_read_tools` (intake-extractor worker) + `search_guidelines` (evidence-retriever) | Yes |
| `verification_grounding` (0/1) | `TurnTrace.verified()` (called from `main.py`) | Yes |
| `tool_ceiling` (=1 when the turn hit the per-turn tool-call ceiling) | `TurnTrace.limited()` in `/chat` | Yes |
| `turn_error` (=1 on any failed turn) | `TurnTrace.errored()` in `/chat` handlers | Yes |
| `tool_error` (=1 on a FHIR read failure) | `TurnTrace.errored(tool_failure=True)` | Yes |
| `turn_cost` (turn model cost, USD) | `TurnTrace.costed()` in `/chat`; priced by `pricing.turn_cost_usd` | Yes |

**Signal note — why explicit error scores, not span status.** The `/chat` route catches
`ModelHTTPError` / `FhirError` *inside* the `observe_turn` span, so the span closes cleanly and
would look successful; and a Langfuse Monitor can't filter observations down to "tool span +
error level" (monitor filters are model / tags / user / environment). So failed turns emit
explicit numeric scores — `turn_error` (every 502) and `tool_error` (the FHIR-read subset) — the
same monitorable mechanism `verification_grounding` already uses. `errored()` also sets the span
`level=ERROR` for trace-view/dashboard visibility. This closed the two signal gaps this doc
previously listed, and the A2/A3 monitors run against these scores.

**Signal note — why an explicit `turn_cost` score (A5).** Langfuse's auto-instrumentation attaches
cost to each *generation* child span, not to the `chat-turn` root (root observation cost reads
$0). A Monitor evaluates *observation-level* cost, so filtering to `name = chat-turn` would watch a
constant 0 and never fire, and monitoring the generation spans instead measures per-*generation*
cost, which a multi-generation turn (grounding retry, tool loop) splits — not per-turn cost. So the
route computes each turn's dollar cost from its token usage (`pricing.turn_cost_usd`, priced by
`ModelTier`) and emits it as the `turn_cost` numeric score, giving A5 a true per-turn value to
threshold. Cost is emitted on the *answered* path only (a turn that errors before returning has no
usage to price); those turns are already covered by A2/A3. `turn_cost` correctly aggregates usage
across the **whole** graph, and per-route child spans land under `chat-turn` (verified live).

**Week-2 signals — not yet emitted (tracked).** The PRD-week-2 dashboard/alerts call for a few
graph-shaped signals this doc does *not* yet cover: a **routing-decision distribution** metric
(today routing is only per-trace `route:*` spans with no aggregatable score), **per-worker
latency/cost**, and **RAG retrieval-hit-rate** — none are emitted yet. They pair with new alerts
not in §2: extraction-failure-rate, RAG-retrieval-latency, and eval-regression. This is a recorded
gap only: the MVP observability slice is **JOS-59** and the full dashboards/alerts are **JOS-64** —
the code work is out of scope for this change.

**Prompt Management.** The service now syncs `copilot-answerer-prompt`; the Week-1
`copilot-system-prompt` is orphaned by the service (kept only for the single-agent eval harness).
Only 1 of the graph's 4 prompts (router / extractor / retriever / answerer) is versioned — full
coverage is a **JOS-64** follow-up.

---

## 2. Alert definitions

Each monitor evaluates over a rolling **1-hour window** (Langfuse's "Over the past" dropdown offers
5 min → 1 week; we pick 1h — long enough that a count over the window is a stable signal at demo
traffic, short enough to page promptly). Thresholds are **absolute counts, not rates** — Langfuse monitors one
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
  catch — a spike means answers are degrading in *quality*, not availability. A tool-call-ceiling
  refusal is deliberately **not** counted here — it never reached the gate — and emits the separate
  `tool_ceiling` score instead, so a large-chart runaway can't masquerade as a trust regression.
- **On-call response:** pull the refused traces in Langfuse; look for a common patient/question
  shape or a recent prompt/model change. A deploy correlation → consider rollback. Do **not**
  relax the gate to clear the alert.

### A5 — Cost-per-turn spike  *(agent-specific: cost)*
- **Fires when:** p95 `turn_cost` > **$0.20** in 1h.
- **Severity:** warning.
- **Monitor:** Numeric Scores data source → p95 of `turn_cost`, `>` operator. (Not the Observations
  cost metric — see the §1 signal note: turn cost is $0 on the `chat-turn` root observation.)
- **Threshold basis:** observed p95 ≈ $0.092, max $0.13 (2026-07-10, measured at trace level). $0.20
  ≈ 2× the normal p95 — a genuine spike (runaway tool-chaining / mis-routed Opus turn / retry
  storm), not routine variance. Revisit if tier routing or the model mix changes.
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
| A5 | Cost-per-turn spike | Numeric score `turn_cost` (p95) | > $0.20 | warn | (agent-specific) |

Thresholds calibrated from 22 live production turns (2026-07-10); revisit as traffic grows.

---

## 4. Wiring plan — Langfuse Cloud Monitors

All five alerts fire from **Langfuse Cloud → Monitors** against the trace/score data the agent
already emits — no Prometheus, Grafana, or `/metrics` endpoint needed. (An earlier draft weighed a
Grafana path; it's unnecessary now that Langfuse Monitors cover latency, cost, and score
thresholds natively. Monitors are Langfuse **Cloud-only** — fine, we're on Cloud.) The code
prerequisites — the `turn_error` / `tool_error` scores (A2/A3) and the `turn_cost` score (A5) —
ship in the agent (see §1).

**Monitors/Automations are UI-only** — the Langfuse public API (verified via `langfuse-cli api
__schema`, 2026-07-10) exposes no monitors/automations/alerts resource, so these can't be scripted;
create them by hand in the UI. Thresholds are already calibrated (§3), so setup is mechanical.

**Per-alert setup** (Langfuse → *Monitors* → *New monitor*): configure each row of §3's table —
data source, metric, the threshold value (and the optional Warning threshold where §2 lists a
warn→page split), a **1-hour** window, and a filter of `environment = production` (this excludes
the `sdk-experiment` eval traces). For A1 also filter `name = chat-turn` (A5 is a numeric score,
not an observation, so it needs no name filter).

**Notification action** (Langfuse → *Automations* → *New automation* → **Slack**): create one
Slack automation and attach it to all five monitors. Page-severity alerts (A2 page, A4) and
warn-severity alerts can route to the same channel initially; split channels later if noise
warrants. Webhook and GitHub-Actions actions exist as alternatives but Slack is the least-effort
given our existing workspace. Langfuse auto-disables an automation after 5 consecutive delivery
failures, re-enabled from the Automations page.

**Dashboards (same tool, satisfies the PRD real-time dashboard requirement):** a Langfuse
Dashboard renders total requests, error rate (`turn_error`), p50/p95 latency, tool-call counts,
and verification pass/fail (`verification_grounding`) — reading from the same source as the alerts.
§5 records the as-built monitor, automation, and dashboard configuration.

---

## 5. Execution runbook

Langfuse project: `cmrc3jeu000w3ad0cigwzi04s` on `https://us.cloud.langfuse.com`. Everything below
is UI work (monitors/automations have no API); the dashboard widgets are already created via API.

### 5a. Dashboard — assemble from pre-built widgets

Six widgets are already created (each URL opens the widget preview). In Langfuse → **Dashboards →
New dashboard** (name it "Clinical Co-Pilot — Ops"), then **Add widget → Select existing** for each:

| Widget | Signal | Alert |
|---|---|---|
| [Turn volume](https://us.cloud.langfuse.com/project/cmrc3jeu000w3ad0cigwzi04s/widgets/cmrgz5cmp0dhoad0cfbl6ai1q) | answered turns/interval | context |
| [Turn latency p50/p95](https://us.cloud.langfuse.com/project/cmrc3jeu000w3ad0cigwzi04s/widgets/cmrgz5no80dhrad0c4z5u39w8) | `chat-turn` latency | A1 |
| [Turn & tool errors](https://us.cloud.langfuse.com/project/cmrc3jeu000w3ad0cigwzi04s/widgets/cmrgz5pzz0dhuad0cmhj5y7gz) | `turn_error`/`tool_error` counts | A2/A3 |
| [Verification grounding rate](https://us.cloud.langfuse.com/project/cmrc3jeu000w3ad0cigwzi04s/widgets/cmrgz5sw60dhzad0cb1crpmlp) | avg `verification_grounding` | A4 |
| [Cost per turn p95](https://us.cloud.langfuse.com/project/cmrc3jeu000w3ad0cigwzi04s/widgets/cmrgz5v2a0d32ad0d2v5iud5c) | p95 `turn_cost` | A5 |
| [Spend by model](https://us.cloud.langfuse.com/project/cmrc3jeu000w3ad0cigwzi04s/widgets/cmrgz5wy80cz3ad0dc1ufrrmt) | generation cost by model | A5 context |

All six are already filtered to `environment = production`. Set the dashboard date picker to the
range you want; widgets inherit it.

### 5b. Monitors — one per row (Monitors → New monitor)

For each, set the data source + metric, the threshold, the **Over the past** window, and filters.
Every monitor gets `environment = production`; A1 additionally gets `name = chat-turn`. **Over the
past** is the "Over the past" dropdown in Alert Conditions (options run 5 min → 1 week); all five
use **1 hour**, matching the §2/§3 calibration.

The **Data source** column names the view and, for the numeric-score monitors, the score to filter
to. A numeric-score monitor counts/aggregates **every** score unless you filter it, so each of
A2–A5 gets a `Score Name = <score>` filter (Filters → Column **Score Name**, `=`, the score name)
*in addition to* `Environment = production`. That name filter — not the Measure — is what isolates
`turn_error` from `tool_error`, `verification_grounding`, `turn_cost`, etc.

| # | Name | Data source | Metric (agg) | Extra filter | Trigger / Threshold | Warn | Over the past | No-data |
|---|---|---|---|---|---|---|---|---|
| A1 | p95 latency | Observations | latency **p95** | `name = chat-turn` | above, > 60000 ms (page) | 45000 ms | 1 hour | Treat as 0 |
| A2 | turn errors | Numeric Scores, `Score Name = turn_error` | count | — | above, > 10 (page) | 3 | 1 hour | Treat as 0 |
| A3 | tool failures | Numeric Scores, `Score Name = tool_error` | count | — | above, > 3 | — | 1 hour | Treat as 0 |
| A4 | grounding | Numeric Scores, `Score Name = verification_grounding` | **avg** | — | **below**, < 0.85 | — | 1 hour | **Show severity: NO DATA** |
| A5 | cost/turn | Numeric Scores, `Score Name = turn_cost` | value **p95** | — | above, > 0.20 | — | 1 hour | Treat as 0 |

Notes: latency is in **milliseconds** in Langfuse (45s = 45000). A1/A2 have a warn→page split — put
the page value in ALERT Threshold and the warn value in WARNING Threshold; A3/A5 need only the ALERT
row. A4 sets the "Trigger when the value is" selector to **below** (refusal *rises* as the average
*falls*); the rest use **above**.

**No-data handling (Advanced Options → "When there is no data").** This one is load-bearing for A4.
Traffic is sparse and bursty, so most 1-hour windows contain no scores. For the four **above**
monitors (A1/A2/A3/A5) leave the default **Treat missing data as 0** — an empty window reads 0,
which is *below* their thresholds, so they correctly stay quiet. A4 triggers **below** 0.85, so
"treat as 0" would fire on every empty hour (0 < 0.85) even when every real score is 1.0; set A4 to
**Show severity: NO DATA** so silent windows are a distinct no-data state, not a false alert. (Only
A4 needs this; do not change the other four.)

### 5c. Slack automation (Automations → New automation → Slack)

Authorize the Slack workspace, pick the channel (e.g. `#copilot-alerts`), then attach the one
automation to all five monitors. Langfuse auto-disables an automation after 5 consecutive delivery
failures — re-enable from the Automations page. Split page vs warn into separate channels later only
if volume warrants.
