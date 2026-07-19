# Week-2 Deliverables — Multimodal Evidence Agent

A single map to everything [PRD-week-2.md](../PRD-week-2.md) asks us to hand over.
Same shape as the [Week-1 map](./DELIVERABLES-week-1.md): what it is, where it
lives, how a grader verifies it — plus a **status** column mapping each requirement
to the artifact that satisfies it.

Scope note: this page covers requirements that produce a **readable artifact** —
a doc, a report, a dataset, a spec, a collection, a video. Engineering
requirements that are satisfied directly in code (correlation-ID propagation,
structured logging, retries) are listed once in §4 with a pointer, not
re-explained; the place they are *described* is
[W2_ARCHITECTURE.md](../W2_ARCHITECTURE.md).

**Live prod agent:** `https://copilot-agent-production-eb24.up.railway.app`

**Status key:** ✅ done · 🟡 partial (gap named inline) · ⛔ not started

---

## 1. Core deliverable requirements

The PRD's *Deliverable Requirements* table. (The demo video is tracked outside this
page — it is a submission artifact, not a repo one.)

| # | Deliverable | Where it lives | How to verify | Status |
|---|-------------|----------------|---------------|--------|
| 1 | Repository + setup guide + deployed link + env docs | [`README.md`](../README.md) — W1-vs-W2 delta table + "Running the Week-2 flow" (branch, services, env vars); [`agent/README.md`](../agent/README.md) for full setup | Follow `agent/README.md` from a clean checkout; or run fully offline with the three `*_MODE=fixture` vars and confirm `/ready` returns 200 | ✅ |
| 2 | Week-2 architecture doc | [`W2_ARCHITECTURE.md`](../W2_ARCHITECTURE.md) — §3 ingestion, §4 worker graph, §5 RAG, §6 data model, §7 eval gate, §8 testing, §9 observability, §11 failure modes + §11.1 backup/recovery, §12 risks | Read it | ✅ |
| 3 | Schemas for `lab_pdf` + `intake_form`, with citation fields and validation tests | [`agent/src/copilot/ingestion/schemas.py`](../agent/src/copilot/ingestion/schemas.py) | `pytest agent/tests/test_schemas.py test_probe_schemas.py test_citation.py` | ✅ three doc types shipped (`medication_list` is the stretch third) |
| 4 | 50-case eval dataset, boolean rubrics, judge config, results | [`agent/src/copilot/evals/`](../agent/src/copilot/evals/) — `cases.py` (53 cases), `rubrics.py`, `judges.py` | See §2 below | ✅ |
| 5 | CI evidence — eval suite that blocks regressions | [`.github/workflows/evals.yml`](../.github/workflows/evals.yml) | Read the bot's eval comment on any `qa/integration → main` PR | ✅ |
| 6 | Cost + latency report | [`context/planning/cost-analysis.md`](../context/planning/cost-analysis.md) (dev spend, unit economics, 100→100K projections), [`context/planning/loadtest-results.md`](../context/planning/loadtest-results.md) (p50/p95 + bottleneck) | Read both; every figure carries its Langfuse query and the 2026-07-14+ window | ✅ Week-2 measured (n=40); Week-1 kept as labelled baseline |
| 7 | Deployed application with the W2 flow working | Railway `copilot-agent` (agent) + `openemr` (module), both from `main` | `curl <agent>/ready \| jq` — six probes incl. vector index + reranker | ✅ |

---

## 2. The eval gate (the hard gate)

> *"We will introduce a small regression and confirm your CI gate fails."*

- **Dataset** — 53 cases in [`agent/src/copilot/evals/cases.py`](../agent/src/copilot/evals/cases.py),
  reconciled into the Langfuse-hosted `copilot-week2-golden-v1` by
  [`seed_dataset.py`](../agent/src/copilot/evals/seed_dataset.py). The cases live in
  the repo, so the golden set is reproducible from source alone — the hosted copy is
  a projection, not the system of record.
- **Rubrics** — all five required booleans in
  [`rubrics.py`](../agent/src/copilot/evals/rubrics.py): `schema_valid`,
  `citation_present`, `factually_consistent`, `safe_refusal`, `no_phi_in_logs`.
  Four are deterministic; `factually_consistent` is a Haiku judge
  ([`judges.py`](../agent/src/copilot/evals/judges.py)) returning a structured verdict.
- **Two independent fail clauses** — absolute floors in
  [`experiment.py`](../agent/src/copilot/evals/experiment.py) `_THRESHOLDS` (1.0 for the four
  deterministic rubrics, 0.9 for `factually_consistent`) **and** a >5% relative drop
  versus the previous run ([`baseline.py`](../agent/src/copilot/evals/baseline.py)).
  Either one fails the build.
- **Wiring** — [`evals.yml`](../.github/workflows/evals.yml) runs the CI subset
  (`copilot-week2-golden-ci`, ~$0.10/run) on every PR into `main` touching `agent/**`,
  with `should_fail_on_regression: true` and a score comment on the PR.
  [`agent-tests.yml`](../.github/workflows/agent-tests.yml) is the free deterministic
  half — `ruff`, `mypy`, `pytest` — on the same trigger.

**Known limits to state out loud:** the gate fires on `→ main` PRs only, not on
`feature → qa/integration`; and `no_phi_in_logs` inspects the answer prose for
SSN/MRN patterns, so it is a check on the *answer*, not on the traces — trace-level
scrubbing is enforced separately by the export masks (§3.8).

---

## 3. Engineering requirements that need writing down

Each of these is graded on the existence of a description, not just working code.

### 3.1 Typed contracts + schema evolution + data authority
`W2_ARCHITECTURE.md` §6 (data model & authority) and the schemas in
[`ingestion/schemas.py`](../agent/src/copilot/ingestion/schemas.py). The citation
sidecar [`sql/table.sql`](../interface/modules/custom_modules/oe-module-ai-copilot/sql/table.sql)
declares itself a rebuildable derived cache, not a system of record — that is the
data-authority statement for derived facts. Schema evolution is covered by the
**migration note in `W2_ARCHITECTURE.md` §2**: no Week-1 DB schema changed (one
`CREATE TABLE`, zero `ALTER`/`DROP` repo-wide) and no data migration; the agent's
*tool* surface did consolidate five reads into `get_patient_summary`, which the note
states plainly along with why it is safe (return models unchanged, grounding
preserved, model-facing only). It also covers the module's `$v_database` schema
revision, the install/upgrade path that must run after a code deploy, and rollback. ✅

### 3.1a Data model — owner / lineage / access / validation per artifact
`W2_ARCHITECTURE.md` §6, one table row per Week-2 artifact with the four required
attributes as columns. Covers all four types the PRD names — extracted lab
observations, intake facts, guideline chunks, citation records — plus medication-list
facts and the extraction sidecar. The section then argues the FHIR round-trip
(store-once, native FK + sidecar, idempotent derivation) and tabulates the
"derived, not confirmed" marker each fact carries in OpenEMR's own vocabulary. ✅

### 3.2 OpenAPI 3.0 spec, kept in sync
[`agent/openapi.json`](../agent/openapi.json), regenerated by
[`scripts/dump_openapi.py`](../agent/scripts/dump_openapi.py). Sync is enforced, not
asserted: [`tests/test_openapi_contract.py`](../agent/tests/test_openapi_contract.py)
fails if the committed spec drifts from the live app. Six endpoints —
`POST /chat`, `GET /documents`, `GET /documents/{id}/extraction`, `GET /evidence`,
`/health`, `/ready`. ✅

### 3.3 Runnable API collection covering W2
[`agent/api-collection/`](../agent/api-collection/) — 11 Bruno requests; `08` documents,
`09` extraction, `10` evidence, `11` the full Week-2 flow. Every agent route has a
request. ✅ *(There is no upload request because there is no upload endpoint — see §5.)*

### 3.4 Testing strategy
`W2_ARCHITECTURE.md` §8. Unit: schema validators, geometry, pricing. Integration:
ingestion→answer over [`tests/fixtures/documents/`](../agent/tests/fixtures/documents/)
(4 PDFs + 4 recorded OCR responses) with `FixtureOcrBackend` /
`FixtureEvidenceRetriever` / pydantic-ai `FunctionModel`, so CI needs no live API keys.
Behavioural: the golden set. ✅

### 3.5 Observability, debugging, incident response
`W2_ARCHITECTURE.md` §9 and §11 (per-failure "identify in logs" / "recovery action").
Correlation ID enters at [`correlation.py`](../agent/src/copilot/correlation.py) and is
stamped on the `chat-turn` root span; each supervisor routing decision is a child span
via `turn.routed(...)` in [`supervisor.py`](../agent/src/copilot/graph/supervisor.py). ✅

### 3.6 Dashboards + alert definitions
[`context/planning/alerting.md`](../context/planning/alerting.md) — **eight** alerts A1–A8 (§2),
each with threshold basis, meaning, and on-call response; wiring in §5b; dashboard in §5a.
The three the PRD names for Week 2:
**A6 extraction failure** (`extraction_error` score, count > 1/h — new score emitted by
`attach_and_extract`), **A7 RAG retrieval latency** (p95 of the `search_guidelines` span > 5 s,
no code needed), **A8 eval regression** (CI-side in `evals.yml`, not Langfuse — the >5%
between-run comparison is not expressible as a rolling-window monitor; §2 explains why).
**Six W2 dashboard tiles built** on "Clinical Co-pilot Ops" (§5a, each linked): ingestion count,
extraction failures, retrieval latency, routing decisions, extraction field pass rate, retrieval hit
rate. **SLOs measured, not guessed** (§5d): document ingestion p95 4.22 s → SLO < 8 s; evidence
retrieval p95 3.05 s → SLO < 5 s, both from live production spans.


**All seven Langfuse monitors are live and filter-verified** (A1–A7; A8 is CI-side). ✅

### 3.7 Backup and recovery
`W2_ARCHITECTURE.md` §11.1 — a per-artifact RPO/RTO table and a 5-step manual recovery
procedure. The load-bearing claim is that five of seven artifact classes are versioned
or rebuildable (corpus, index, golden set, sidecar, derived facts), so only the stored
source documents and the clinician-authored chart are irreplaceable — and both hold **synthetic
demo data only**, reproducible from fixtures. The recovery design is the deliverable and it is
complete; enabling a daily Railway backup schedule (Service → Settings → Backups; kept 6 days) is
the one-toggle production-hardening step, documented in §11.1 as an accepted posture for demo data. ✅

### 3.8 Privacy audit of the observability data
Three layers, described in `W2_ARCHITECTURE.md` §9:
**(1) trace-level scrubbing** — [`agent/src/copilot/masking.py`](../agent/src/copilot/masking.py),
installed at Langfuse client init. Both SDK hooks are wired: `mask` for payloads the
service sets, and `mask_otel_spans` for Pydantic AI's auto-instrumented spans, which is
where the PHI actually lives (`gen_ai.tool.call.result` is raw FHIR records). Installing
only the first would report scrubbing while still exporting every chart read.
**(2) prompt-level** — guideline queries constrained to de-identified terms.
**(3) eval-level** — the `no_phi_in_logs` rubric, PR-blocking in CI.
Verify: `pytest agent/tests/test_masking.py` — 7 cases including a fail-closed case and a
guard that breaks the build if Pydantic AI renames a masked attribute. ✅
*Residual risk, named in §9: `logfire.msg` is retained for span readability and is not
proven free of argument values; the masks are an exact-key denylist.*

### 3.9 Baseline CPU / memory / latency / throughput for W2 flows
[`context/planning/loadtest-results.md`](../context/planning/loadtest-results.md) +
harness at [`agent/loadtest/`](../agent/loadtest/README.md). **Latency is measured for W2**
from 40 real production turns (p50 35.0s / p95 101.8s / p99 127.4s / mean 46.7s) with a
per-component breakdown covering extraction, retrieval, and FHIR reads, plus an explicit
W1-vs-W2 comparison. **CPU/memory measured** for all four prod services over the same
window (agent 419 MB avg / 985 MB peak — ~2.6× Week-1, from in-process FastEmbed models).
**Throughput derived** — 0.18 req/s @10 users, 0.84 @50, via Little's Law on the measured
mean latency, calibrated against the Week-1 load test's realised 84%/78% efficiency. ✅

### 3.10 CI: build, lint/typecheck, tests, coverage, dependency audit, security scan
Lint/typecheck/tests: [`agent-tests.yml`](../.github/workflows/agent-tests.yml) —
`ruff`, `mypy`, `pytest` on every PR touching `agent/**`. **PHP quality
(phpstan/phpcs/rector/codespell) is enforced by the local pre-commit hook, not by CI on this
fork:** ~30 upstream workflows are scoped to `master`/`rel-*` and so never fire on `main` /
`qa/integration` PRs (see §3.12). Security scan:
[`semgrep.yml`](../.github/workflows/semgrep.yml) — PHP/JS/Node/Python rulesets, diff-scoped on
PRs, results to the Security tab. **Report-only** (no `--error`), and note it only started
covering this fork's PRs once `main`/`qa/integration` were added to its branch filter — upstream
ships it scoped to `master`/`rel-*`.
Coverage via Codecov. Dependency audit:
[`dependency-audit.yml`](../.github/workflows/dependency-audit.yml) on every PR —
`pip-audit` **blocking** on the agent's Python tree, `composer audit` + `npm audit`
**advisory** on the fork's inherited PHP/npm deps (rationale in
`W2_ARCHITECTURE.md` §8: a gate on CVEs we cannot patch without diverging from
upstream would be disabled within a week). ✅

### 3.11 Timeouts, retries, circuit breakers
**Every outbound call is bounded, and so is the turn.** Budget table in
`W2_ARCHITECTURE.md` §10; values live in [`config.py`](../agent/src/copilot/config.py),
each set *above* the largest latency observed in production
([`loadtest-results.md`](../context/planning/loadtest-results.md)) so they cut genuine
hangs rather than trimming the tail — LLM 60s (max seen 41.3s), Mistral OCR 30s (16.5s),
Cohere rerank 10s + 2 **SDK** retries that honour 429/5xx (5.2s), Qdrant 5s, FHIR 10s + 2
transport retries. Workers keep `retries=2` and the per-tool call budgets
([`graph/budget.py`](../agent/src/copilot/graph/budget.py)); readiness probes 5s.

**Turn deadline 85s**, set deliberately *under* the sidebar's 90s `CHAT_TIMEOUT_MS`.
Before it, a slow turn ran unbounded server-side while the browser gave up and told the
physician the assistant "may be offline" — measured p95 is 101.8s, so this was already
happening on real turns, with the turn still billing and nothing in the trace to tell it
apart from a healthy one. It now degrades to a plain "took longer than I can spend"
answer, logs `reason=turn_deadline`, and scores `turn_timeout` distinctly from
`tool_ceiling` and `verification_grounding`.

**Retrieval failure degrades instead of failing the turn**
([`graph/workers.py`](../agent/src/copilot/graph/workers.py)), matching what extraction
already did — and deliberately *not* reusing the empty-corpus wording, because "no
guideline covers this" is a clinical statement a physician may act on and asserting it
because a service was unreachable would be a false negative dressed as evidence.

**No circuit breaker — accepted risk, argued in `W2_ARCHITECTURE.md` §12** with the
tripwires that would reverse it (a sustained dependency failure rate on the A3 monitor,
or multi-replica deployment). Three external dependencies, one replica, and two of the
three now degrade gracefully.

Verify: `pytest agent/tests/test_retriever.py test_chat_flow.py test_graph_flow.py`. Each
new test was confirmed to fail without its fix — the wiring test catches a dropped kwarg
(`assert 5 == 7`, 5 being the SDK default), the deadline test 500s without the wrapper. ✅
*(Caveat: `mistralai` is an optional extra excluded from CI, so the OCR timeout is the one
value with no automated coverage — worth exercising against a real document.)*

### 3.12 Inherited-workflow branch filters (fork hazard)
Not a PRD line item, but it decides whether several of the rows above are true. Upstream
develops on `master`/`rel-*`; this fork develops on `main`/`qa/integration`. Around **30
inherited workflows filter `pull_request.branches` to `master`/`rel-*` and therefore never run
here** — including `phpstan.yml`, `styling.yml`, `isolated-tests.yml`, `js-test.yml`,
`conventional-commits.yml`, `pre-commit.yml`, and (until now) `semgrep.yml`. What *does* run on
our PRs: `agent-tests.yml`, `evals.yml`, `dependency-audit.yml`, `semgrep.yml`, and
`validate-codecov.yml`. Audit with:

```bash
python - <<'PY'
import yaml, glob, os
for f in sorted(glob.glob('.github/workflows/*.yml')):
    on = (yaml.safe_load(open(f)) or {}).get(True) or {}
    pr = on.get('pull_request') if isinstance(on, dict) else None
    br = pr.get('branches') if isinstance(pr, dict) else None
    if br and not any(b in ('main', 'qa/integration') for b in br):
        print('dead on our PRs:', os.path.basename(f), br)
PY
```

Deliberately **not** mass-enabled: switching on the full upstream PHP suite would red-build on our
module's own findings. This is an accepted, documented fork posture — re-enable individually, with
intent — not an outstanding task. ✅


---

## 4. Satisfied in code — described, not re-documented

These need no artifact of their own; `W2_ARCHITECTURE.md` is where they are explained.

| Requirement | Implementation | Described in |
|---|---|---|
| Correlation ID across boundaries | [`correlation.py`](../agent/src/copilot/correlation.py) | §9 |
| Structured, PHI-free logging | [`observability.py`](../agent/src/copilot/observability.py) `TurnTrace` + [`masking.py`](../agent/src/copilot/masking.py) export masks | §9 |
| Distributed tracing, worker spans as children | `turn.routed(...)`, pydantic-ai OTel | §9 |
| `/health` + `/ready` with meaningful, degradable probes | [`health.py`](../agent/src/copilot/health.py) — 6 probes incl. document storage, vector index, reranker | §10 |
| Schema is the source of truth, not VLM output | flat `_*Probe` models → validated Pydantic; geometry placed separately | §3 |
| Per-turn cost + token accounting | [`pricing.py`](../agent/src/copilot/pricing.py) `turn_cost_usd` | §9 |
| Timeouts on every outbound call + a turn deadline | [`config.py`](../agent/src/copilot/config.py) budgets, wired at each client; `asyncio.timeout` on the turn | §10 |
| Graceful degradation when a dependency is down | extraction and retrieval both report the gap and let the turn answer | §10, §12 |

---

## 5. The one architectural deviation to defend

The PRD's Stage 1 says *"accepts a file … stores the source document in OpenEMR."*
**We do not accept file uploads.** `attach_and_extract(document_id)`
([`graph/workers.py`](../agent/src/copilot/graph/workers.py)) operates on documents
that already exist in OpenEMR, discovered as FHIR `DocumentReference`s and fetched as
`Binary` bytes. Upload is OpenEMR core's job; the agent is read-only over the chart
and writes derived facts back through the module's session-authenticated
[`persist-facts.php`](../interface/modules/custom_modules/oe-module-ai-copilot/public/persist-facts.php).

This is defensible — it avoids a second document store and keeps OpenEMR
authoritative — but a grader following the PRD literally will look for an upload step,
so it belongs in the demo narration and in `W2_ARCHITECTURE.md` §12.

Related deviation: the graph is **pydantic-ai**, not LangGraph (an allowed
"other inspectable orchestration framework" — see
[`context/decisions/agent-framework-week2.md`](../context/decisions/agent-framework-week2.md)),
and the critic is a **deterministic grounding gate**
([`graph/gate.py`](../agent/src/copilot/graph/gate.py)) rather than an LLM critic —
stricter than the stretch requirement, and cheaper.

---

## 6. Stretch deliverables already shipped

| Item | Where |
|---|---|
| Third document type (`medication_list`) | [`ingestion/schemas.py`](../agent/src/copilot/ingestion/schemas.py), [`context/specs/medication-list-extraction.md`](../context/specs/medication-list-extraction.md) |
| Critic equivalent that rejects uncited claims | [`graph/gate.py`](../agent/src/copilot/graph/gate.py), [`verification.py`](../agent/src/copilot/verification.py) |
| Click-to-source UI + PDF bounding-box overlay | [`public/source-view.php`](../interface/modules/custom_modules/oe-module-ai-copilot/public/source-view.php), [`assets/js/ai-copilot.js`](../interface/modules/custom_modules/oe-module-ai-copilot/public/assets/js/ai-copilot.js) |
| Lab trend chart from extracted Observations | `ai-copilot.js` `renderLabCharts()` |
| Contextual retrieval improvements | 8-topic / 55-chunk corpus at [`rag/corpus/`](../agent/src/copilot/rag/corpus/), anchor backfill, RRF fusion + Cohere rerank |
