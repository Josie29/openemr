# Implementation Prompt 01 — Ingest ARCHITECTURE.md, plan, then build the agent-service walking skeleton

> **How to use this file.** This is a prompt, not a deliverable. Hand it to a fresh
> Claude Code session (or work it yourself) as the starting instruction for the first
> coding increment of the Clinical Co-Pilot. The architecture is already decided and
> written up in [`ARCHITECTURE.md`](../ARCHITECTURE.md) (Option D) — **ingest it, do not
> re-derive or re-litigate it.** Follow-up prompts (`-02`, `-03`, …) add breadth once this
> thin slice is proven end to end.

---

## 0. Mission

Build the **first vertical slice** of the AgentForge Clinical Co-Pilot: a single agent
turn that flows end to end — HTTP request → correlation ID → one FHIR tool call → LLM →
verification gate → structured response → trace in Langfuse. Breadth (more tools, deeper
verification, evals, load tests, the PHP module) comes later. **This increment proves the
pipeline, not the product.**

The guiding discipline is **clean, spec-driven, production-grade code from the first
commit** — this repo's whole thesis is that the gap between a demo and something a
hospital CTO would trust is the entire project. Technical debt taken here compounds.

## 1. Read first (source of truth, in order)

Do not start coding until these are read — every decision below traces to them:

1. **[`ARCHITECTURE.md`](../ARCHITECTURE.md) — the primary source of truth.** The written,
   defended AI Integration Plan (Option D). It is the spec this code is written against;
   the code must match it, not diverge from it. Everything else on this list is supporting
   detail *behind* this document. If your implementation would deviate from `ARCHITECTURE.md`,
   stop and flag it — do not silently drift.
2. `PRD-week-1.md` — the north star. Agent / Verification / Observability / Eval requirements
   (§ "Agent Requirements") and the **Engineering Requirements** section (correlation IDs,
   canonical schemas, `/health` vs `/ready`, dashboards, alerts) are acceptance criteria,
   not aspirations.
3. `USERS.md` — the target user and UC-1…UC-5. Every capability must trace to a use case.
   UC-5 (cross-cover) is the authorization test case.
4. `AUDIT.md` — the security/compliance constraints the design must respect (IDOR gap, new
   PHI→LLM flow / BAA + de-identification seam, secrets hygiene, no in-app TLS).
5. `context/decisions/agent-workflow.md` — **the most directly actionable doc for this work.** The
   middle layer between `USERS.md` and the architecture: it fixes the **canonical tool
   inventory** (`get_patient` → `PatientDemographics`, `get_problems`, `get_medications`,
   `get_allergies`, `get_encounters`, plus an optional `get_patient_snapshot` composite),
   the per-UC call patterns and orchestration, what the verification gate must catch per UC,
   and the **single-agent verdict**. Use its tool names and typed return models verbatim;
   do not invent new ones.
6. `context/decisions/deployment-strategy.md`, `context/decisions/agent-tech-stack.md` — decision evidence
   *behind* `ARCHITECTURE.md` (the roads not taken: why Option D, why Pydantic AI / Claude /
   Langfuse). Consult for the *why* when a decision is unclear; `ARCHITECTURE.md` is what to
   build to.
7. `context/decisions/synthetic-data-generation.md` — supporting (seed patient data).

If anything below contradicts `ARCHITECTURE.md`, **the architecture doc wins** — stop and
flag it.

---

## Phase 0 — Ingest the architecture and produce a plan (before writing code)

`ARCHITECTURE.md` already exists and is the source of truth — this step **reads and
internalizes it**, it does not rewrite it. Do not touch `ARCHITECTURE.md` or the
`context/*.md` decision-evidence docs in this increment.

Produce a short written **implementation plan** for the walking skeleton (Phase 1 below)
before touching code — this is the spec-driven step, and it keeps the code honest to the
architecture:

- **Trace, don't invent.** For each thing you're about to build (the `/chat` route, the
  `get_patient` tool, the `output_validator` gate, `/health`+`/ready`, correlation IDs,
  Langfuse wiring), cite the section of `ARCHITECTURE.md` (and the UC in `USERS.md`) it
  implements. Anything that doesn't trace to the architecture is scope creep — cut it or
  flag it.
- **Name the contracts first.** List the Pydantic v2 models you'll define (`/chat`
  request/response, `PatientDemographics`, the tool I/O) and confirm their shapes match the
  canonical inventory in `context/decisions/agent-workflow.md`. Contracts are the source of truth, not
  the implementation (PRD engineering req).
- **Confirm the cut line.** Restate what this increment builds vs. defers (§ Non-goals), so
  the plan is a thin vertical slice and not a creeping rebuild.
- **Surface conflicts now.** If `ARCHITECTURE.md`, `agent-workflow.md`, and the PRD
  disagree on any detail (a tool name, a return shape, where the gate sits), raise it before
  coding rather than picking one silently.

If working with a human reviewer, get sign-off on this plan before Phase 1. Keep it short —
it's a map for the slice, not a second architecture doc.

---

## Phase 1 — The walking skeleton (agent service)

### 1.1 What "done" looks like (the vertical slice)

A running FastAPI service, deployed to Railway in the existing project, that answers **one**
request end to end:

```
POST /chat  { patient_id, message }
  → correlation-ID middleware stamps a request-scoped ID
  → Pydantic AI agent runs ONE tool: get_patient() → PatientDemographics (FHIR R4 Patient)
    (canonical name + return model from context/decisions/agent-workflow.md — use it verbatim)
  → Claude (Sonnet 5) produces a structured answer
  → output_validator gate rejects any claim without a source citation (ModelRetry on fail)
  → response returned as a typed Pydantic model, every claim carrying a source reference
  → the full turn (steps, timings, tokens, cost, verification pass/fail) lands in Langfuse
    under the correlation ID
```

This is the single turn from `ARCHITECTURE.md` [§6.2 Turn lifecycle](../ARCHITECTURE.md#62-turn-lifecycle),
narrowed to one tool. Build it to match that section; don't re-derive the flow.

Plus the two probes, built exactly as `ARCHITECTURE.md`
[§10 Observability & operations](../ARCHITECTURE.md#10-observability--operations) specifies
them — do not restate or reinterpret; implement §10:

- `GET /health` — 200 if the process is alive.
- `GET /ready` — pings FHIR base + Claude API + Langfuse, returns 503 with a per-dependency
  breakdown if any is down, never 200 unconditionally. Note §10's constraint: the **LLM probe
  uses a cheap metadata call, not a full completion.**

**Deliberately one tool.** `get_patient()` reads the simplest FHIR resource (`Patient`) and
is needed by every UC — it's the right first tool to prove parse-don't-validate with
`fhir.resources`. The other four tools from the `context/decisions/agent-workflow.md` inventory
(`get_problems`, `get_medications`, `get_allergies`, `get_encounters`), the med dedup /
text-fallback logic that lives inside `get_medications()`, and clinical-constraint
verification are **explicit non-goals of this increment** (see § Non-goals) and come in
prompt `-02`.

### 1.2 Decouple from the PHP module — mock FHIR first

The SMART launch / patient-scoped token flow lives in the PHP module, which **does not exist
yet**. Do not block on it. Put the FHIR client behind an interface with two implementations:

- A **fixture-backed client** (records/replays real FHIR R4 JSON for a seed patient) used by
  tests and initial local dev. This lets the whole agent service be built and tested with
  zero dependency on the PHP module or a live token.
- A **real httpx client** that carries a SMART `patient/*.read` bearer token (supplied via
  env var for now; the module mints it later) with explicit timeouts and bounded retries.

Same `FhirClient` protocol, swapped by config. When the PHP module lands (prompt `-03`), only
the token source changes — the agent service does not.

### 1.3 Repo layout

Per `ARCHITECTURE.md` [§3.2 Target state](../ARCHITECTURE.md#32-target-state-option-d), all
agent logic ships from a top-level **`/agent/` directory in this same git repo**, deployed as
its own Railway service in the same project/region as OpenEMR. Create that directory. The
file tree below is a *suggested* internal shape for the Python project (adapt to Pydantic AI /
FastAPI idiom, don't cargo-cult) — the top-level facts (same repo, own service) are fixed by
§3.2, the sub-structure is yours:

```
agent/
  pyproject.toml            # deps: fastapi, pydantic-ai, fhir.resources, httpx,
                            #       langfuse, pydantic-settings, uvicorn; dev: pytest,
                            #       pydantic-evals, ruff, mypy
  README.md                 # how to run locally, env vars, how to hit /chat
  Dockerfile                # for the Railway service
  src/copilot/
    main.py                 # FastAPI app, routes, middleware wiring
    config.py               # pydantic-settings; ALL config/secrets from env (no literals)
    correlation.py          # correlation-ID middleware + context propagation
    agent.py                # Pydantic AI agent definition + the tool(s)
    verification.py         # output_validator (source-attribution gate) + ModelRetry
    fhir/
      client.py             # FhirClient protocol + real (httpx) impl
      fixtures.py           # fixture-backed impl for tests/dev
      models.py             # typed wrappers over fhir.resources where useful
    schemas.py              # Pydantic v2 request/response/tool-IO contracts
    observability.py        # Langfuse setup, trace/span helpers, token+cost capture
    health.py               # /health + /ready dependency checks
  tests/
    conftest.py
    test_chat_flow.py       # end-to-end with TestModel/FunctionModel + fixture FHIR
    test_verification.py    # the gate rejects an unattributed claim
    test_ready.py           # /ready reports each dependency's status
    fixtures/               # recorded FHIR R4 JSON for the seed patient
```

Do not touch OpenEMR core or the existing PHP under `interface/`, `src/`, `library/` in this
increment — the agent service is self-contained.

### 1.4 The verification gate — build the deterministic half only

`ARCHITECTURE.md` [§7 Verification strategy](../ARCHITECTURE.md#7-verification-strategy) is
the load-bearing section — read it in full and build to it. It defines the gate as Pydantic
AI's `@agent.output_validator` (runs after the model, before the physician; `ModelRetry` on
failure) enforcing **three** checks. This increment builds **only the first**:

- ✅ **Grounding (deterministic).** The structured response is a model where each claim
  carries a `source_ref`; the validator is *code* that rejects any claim lacking a citation or
  whose citation doesn't resolve to a resource a tool returned this turn. This is the whole
  gate for the skeleton — and per §7 it's the deterministic, can't-be-fooled guarantee.
- ⛔ **Faithfulness (probabilistic Haiku judge)** and ⛔ **domain constraints** (§7's other
  two checks) → **deferred to prompt `-02`**, alongside the tools they apply to. Do not build
  the Haiku entailment judge in this slice.

Match §7's contract for `source_ref` and the structured-claim shape; do not invent a parallel
one.

### 1.5 Engineering requirements — implement per ARCHITECTURE §10

The PRD engineering requirements are specified concretely in `ARCHITECTURE.md`
[§10 Observability & operations](../ARCHITECTURE.md#10-observability--operations). Build to
§10; the notes below only say **which parts land in this slice** vs. defer — they do not
redefine the requirements:

- **Correlation IDs** (§10) — wire in **now**: the inbound-header/generate-if-absent behavior
  and propagation into every log line, tool call, LLM call, and Langfuse trace attribute.
- **Canonical Pydantic v2 contracts** (PRD; `ARCHITECTURE.md` §4 tool table) — **now**: strict
  models for `/chat` request/response and the `get_patient` tool I/O, matching the canonical
  shapes in `context/decisions/agent-workflow.md`.
- **`/health` vs `/ready`** (§10) — **now**, as in §1.1 above.
- **Langfuse tracing** (§10) — **now**, for the single turn: step order, per-step latency,
  tool success/failure, tokens + cost, verification pass/fail.
- **One test per behavior, guarding a named failure mode** (testing rules; `ARCHITECTURE.md`
  §11 eval framing): end-to-end flow, gate rejecting an unattributed claim, `/ready` reporting
  a down dependency — deterministic via `TestModel`/`FunctionModel` + the fixture FHIR client.

**Deferred to later prompts** (scheduled, not forgotten) — the rest of §10 and the PRD
engineering block: the §10 dashboards and three alert definitions, the Postman/Bruno
collection, load/stress tests (10/50 concurrent), baseline infra profiles, and the AI cost
analysis (`ARCHITECTURE.md` §12).

---

## 2. Clean-code guardrails (Python)

This service is greenfield — hold it to the standard, not to OpenEMR's legacy PHP patterns.
Apply the global Python rules already in effect (`~/.claude/rules/`), specifically:

- **Type hints everywhere** — every function signature (params + return), class attributes,
  and any non-obvious variable. Prefer `X | None` over `Optional[X]`. `mypy` clean.
- **Pydantic for structured data** — when a function returns >2 related values, model it;
  compose models rather than subclassing. No raw tuples/NamedTuples for structured returns.
- **Google-style docstrings** on every non-trivial function/class, with `Args`/`Returns`/
  `Raises` as applicable. **No module-level docstrings.**
- **Enums (`StrEnum`/`Enum`) over string literals** for any closed option set (verification
  outcomes, readiness states, model tiers) — especially values crossing module boundaries.
- **Error handling:** catch only what you can meaningfully handle or report; specific
  exception types, never bare `except`; log with enough context to debug (structured
  context, not string interpolation — mirrors the PHP PSR-3 rule). Let exceptions propagate
  otherwise. **Never** put a raw provider/exception message in a user-facing response.
- **Dependency injection** — construct the FHIR client, LLM client, and Langfuse handle at
  the edge (FastAPI dependencies / app factory) and inject them; no global singletons reached
  into from business logic, no `new`-in-the-middle-of-logic equivalents.
- **Parse, don't validate** — at the FHIR boundary, parse JSON into typed `fhir.resources`
  models immediately; downstream code works with types that guarantee their own validity.
- **Config/secrets from env only** (`pydantic-settings`). No committed credentials — the
  audit flagged secrets hygiene; Railway env vars hold everything sensitive.
- **Immutability by default** — frozen Pydantic models / `frozen dataclass` for value objects
  and DTOs; mutable state is the exception.

Run `ruff` + `mypy` before declaring any step done.

---

## 3. Acceptance criteria (this increment is done when…)

1. A short implementation plan exists (Phase 0) in which every skeleton component traces to
   a section of `ARCHITECTURE.md` and a UC in `USERS.md`, with the Pydantic contracts named
   up front — and the built code matches that plan (no silent drift from the architecture).
2. `POST /chat` answers a real question about the seed patient's demographics, and **every
   factual claim in the response carries a source reference** to the FHIR record it came from.
3. The `output_validator` gate (grounding check per `ARCHITECTURE.md` §7) demonstrably rejects
   (via `ModelRetry`) a response containing an unattributed claim — proven by a test.
4. `/health` and `/ready` behave as `ARCHITECTURE.md` §10 specifies (`/ready` 200 only when
   FHIR + Claude + Langfuse are reachable, 503 with a per-dependency breakdown otherwise, LLM
   probed via a cheap metadata call).
5. A single request produces one Langfuse trace, keyed by correlation ID, showing step order,
   per-step latency, tool success/failure, and tokens + cost.
6. Tests pass deterministically with no real LLM call (`TestModel`/`FunctionModel`) and no
   live FHIR server (fixture client). `ruff` and `mypy` are clean.
7. The service builds and runs on Railway in the existing project (internal networking to
   OpenEMR); the deployed URL is recorded.

## 4. Non-goals (explicitly out of scope for this prompt — do NOT build)

- The other four tools from `context/decisions/agent-workflow.md` (`get_problems`, `get_medications`,
  `get_allergies`, `get_encounters`), the `get_patient_snapshot` composite, med dedup, and
  text-match fallback → prompt `-02`.
- The faithfulness (Haiku entailment judge) and domain-constraint checks — §7's other two
  verification checks → prompt `-02`, with the tools they apply to. This slice's gate is
  §7's **grounding** check only.
- Multi-turn conversation / context retention → later (UC-3 needs it; this slice is one turn).
- SSE streaming and tiered model routing (Haiku/Opus) → later; use Sonnet 5, non-streamed.
- The PHP `oe-module-ai-copilot` module, SMART launch flow, care-team/breakglass gate →
  prompt `-03`. This slice uses a fixture/env-var token.
- Dashboards, alerts, the API collection, load tests, baseline profiles, cost analysis →
  scheduled for their own increments.

Building any of these now is scope creep — note it as a follow-up and move on.

## 5. Surface, don't silently resolve

If you hit any of these, stop and flag rather than guessing — they change the design:

- The seed patient's FHIR `Patient` resource is missing fields the summary needs (a
  data-quality failure mode the PRD cares about).
- Langfuse's Python SDK doesn't cleanly capture per-step cost for Pydantic AI (may need a
  custom span attribute) — `ARCHITECTURE.md` §10 asserts cost capture; flag if the SDK
  can't deliver it cleanly.
- Building to `ARCHITECTURE.md` forces a concrete choice the doc leaves open (e.g. §7's exact
  `source_ref` shape / citation granularity, or the §4 `PatientDemographics` field set) —
  make the minimal choice for the skeleton and flag it as a contract the follow-up prompts
  inherit, so `-02`/`-03` don't each re-guess it.
- `ARCHITECTURE.md`, `context/decisions/agent-workflow.md`, or the PRD turn out to disagree, or any of
  them is stale against the actual code / live seed DB — flag it; **`ARCHITECTURE.md` is the
  tiebreaker** (verify against source, then reconcile, as the deployment doc models).
```
