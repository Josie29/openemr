# Agent Tech Stack — Stage 5 Decision Evidence

**Purpose:** Working analysis behind `../ARCHITECTURE.md` (PRD Stage 5) — decides what
fills the standalone Python agent service that `deployment-strategy.md` (Option D)
selected. Decision evidence for the architecture defense, not a deliverable;
`ARCHITECTURE.md` is the source of truth.

**Grounding:** `PRD.md` (agent / verification / observability / eval + engineering
requirements), `deployment-strategy.md` (Option D), `../AUDIT.md` (PHI boundary),
`../USERS.md` (<15s latency budget). Framework/tool landscape verified against 2026
sources (linked at the end); model facts against the current Anthropic model catalog.

---

## Fixed constraints (from Option D — don't re-litigate)

- **Python** — all agent logic lives in one standalone Python service.
- **FHIR R4 only**, under a SMART `patient/*.read` token — the agent holds **no DB
  credentials**, so there's no ORM / SQL-client decision to make.
- **Railway** deploy, same project/region as OpenEMR (internal networking).
- **BAA assumed for every external vendor** — we extend the PRD's "act as if a BAA
  exists with all LLM providers" posture to *all* external services (LLM providers **and**
  the observability vendor). Sending PHI to any managed vendor is therefore acceptable,
  nothing needs self-hosting for compliance, and **BAA availability is not a
  differentiator** — every option below is assumed covered, so picks are made on merits
  (capability, fit, cost, lock-in).

Three decisions are genuinely open — **framework, LLM, observability** — and get a
section each below. They differ in *stakes*, not openness: the framework choice shapes the
most code and is the hardest to swap later, so it carries a validation spike; the LLM and
observability picks are equally open but cheaply reversible (a provider-neutral framework
makes the model a config swap, OTel-native tracing makes the backend one too). The rest
are near-defaults given the constraints; listed once at the end.

---

## Recommended stack

```
Physician browser
   │  native chat panel in the OpenEMR module (deployment-strategy.md, Option D)
   ▼
FastAPI service       /health · /ready (pings FHIR + LLM + Langfuse) · SSE · correlation IDs
   │
Pydantic AI agent     tool loop + output_validator  ← the verification gate
   │     │
   │     └─ Claude    Sonnet 5 default · Haiku 4.5 cheap checks · Opus 4.8 hard cases (streaming)
   │
fhir.resources+httpx  SMART patient-scoped FHIR reads (no DB creds)
   │
Langfuse (cloud)      traces · token/cost · dashboards · eval scores
```

Contracts are Pydantic v2 throughout; evals run in Pydantic Evals + pytest and score to
Langfuse; verification is the Pydantic AI `output_validator` raising `ModelRetry`.

---

## Decision 1 — Agent framework (highest-stakes, least reversible)

Every PRD capability is a *single* conversational agent over a small fixed tool set
(5 FHIR reads) with a hard verification gate and a tight latency budget — **not** a
multi-agent / role-delegation problem, so frameworks built around that (CrewAI, etc.)
are over-scoped. The contenders:

| Framework | Model | Strengths here | Weaknesses here |
|---|---|---|---|
| **Pydantic AI** | Any (Claude native) | Typed end-to-end; `output_validator` + `ModelRetry` **is** the verification gate; `TestModel`/`FunctionModel` make evals trivial; FastAPI-native; native OTel/Langfuse; matches our Pydantic code standards | Verification-as-explicit-graph less first-class than LangGraph; younger than LangChain-era tools |
| **LangGraph** | Any | Explicit graph models `generate → verify → revise` as nodes/edges; durable checkpointing + human-in-the-loop; most mature persistence | Heavier; more moving parts than a single verify-then-answer loop needs |
| **OpenAI Agents SDK** | OpenAI-native (others via LiteLLM) | Small primitive set (agents/handoffs/guardrails); built-in tracing | OpenAI-first; tracing wants OpenAI's platform; less typed than Pydantic AI |
| **Google ADK** | Gemini/Vertex-native (others via LiteLLM) | Code-first multi-agent; built-in eval + SSE/bidi streaming; Vertex Agent Engine deploy; **you have prior experience** | Its edges (Vertex deploy, Gemini) are off-table on Railway + Claude; multi-agent focus over-scoped; gate + typing less first-class than Pydantic AI |
| **Claude Agent SDK** | Claude only | Same harness as Claude Code; bundled bash/file/web tools, subagents, MCP | Built for coding/computer-use; bundles a CLI + tool surface we'd disable — too much to defend for a locked-down read-only clinical tool |
| **Raw Anthropic SDK loop** | Claude only | Maximum control, zero abstraction | Rebuilds session state, retries, tracing, validation by hand |

### Pick: Pydantic AI (runner-up LangGraph)

Each reason traces to a PRD requirement:

1. **The verification requirement maps to a first-class hook.** *"Every response must
   pass through a verification layer before reaching the user"* **is**
   `@agent.output_validator` — a function that runs pre-return and raises `ModelRetry` to
   force a correction. Source-attribution and clinical-constraint checks live in one
   defensible seam, not bolted on.
2. **Contracts for free.** The PRD demands strict schemas for every tool I/O. Pydantic AI
   *is* Pydantic — tool I/O and structured responses are validated models, no adapter.
3. **Evals get easy.** `TestModel`/`FunctionModel` exercise boundary/invariant/regression
   cases (missing data, IDOR-style refusal, "claims cite a source") **without real LLM
   calls** — deterministic and CI-friendly, exactly the PRD's test-design bar.
4. **Coheres with the stack + our standards.** FastAPI-native, native OTel → Langfuse,
   strict-typing conventions — and provider-neutral, so a model swap is a config change.

**When I'd switch to LangGraph:** if the flow becomes a real multi-step graph with
branching and durable resumable state (e.g. `gather → conflict-resolve → verify →
escalate` with human-in-the-loop). For the current verify-then-answer shape that's cost
without payoff — worth a 1-day spike if UC-4 (med/problem cross-referencing) looks
graph-shaped.

**On Google ADK (you've used it):** prior experience is a genuine velocity pro — same
category as the Claude Max dev synergy — and ADK is model-agnostic, so it *could* drive
Claude via LiteLLM. But its headline advantages (Vertex Agent Engine deploy, Gemini,
heavy multi-agent, A/V streaming) are off-table on Railway + Claude or over-scoped for one
agent, and against Pydantic AI it loses on the two things this build leans on hardest: a
first-class verification gate (`output_validator` vs building one) and Pydantic-native
typing. If familiarity proves decisive it's defensible — fold it into the same spike as
LangGraph and judge it on how cleanly it expresses the gate.

---

## Decision 2 — LLM + provider

Provider-neutral framework means the model is a swappable config choice, so this is a
real but low-risk decision. Approximate flagship pricing per Mtok, input/output —
**verify at build time**, these move monthly:

| Provider (tiers) | ~Price in/out | Strengths | Notes |
|---|---|---|---|
| **Claude (Anthropic)** — Opus 4.8 · Sonnet 5 · Haiku 4.5 | $5/$25 · $3/$15 (intro $2/$10 to 2026-08-31) · $1/$5 | Best-in-class agentic tool-use + structured outputs, adaptive thinking, 1M context | **Recommended** — ecosystem alignment + Claude Max dev synergy |
| **OpenAI (GPT-5.x)** | ~$1.75/$14; higher ~$5/$30 | Strong general reasoning, large ecosystem | Viable; no subscription synergy, a second provider to manage |
| **Google Gemini (2.5/3 Pro · Flash)** | ~$1/$10 · ~$0.30/$2.50 | Cheapest at Flash tier, very large context | Agentic/tool maturity trails Claude & OpenAI |
| **Open-weight self-hosted** (Llama/Qwen) | infra only | No per-token cost; full infra control | Its main draw — PHI in-house — is moot under blanket BAA; leaves GPU ops + eval burden + weaker clinical reasoning. Over-scoped for a 3-week sprint |

### Pick: Claude, tiered

**Sonnet 5** workhorse · **Haiku 4.5** for cheap sub-tasks (query classification, a
verification pre-check) · **Opus 4.8** reserved for the hardest reasoning if evals show
Sonnet 5 short. **Stream (SSE)** so first-token latency fits the walk-between-rooms budget.

1. **Agentic fit** — strongest tool-use + structured-output reliability, which the
   verification gate leans on directly.
2. **Claude Max synergy — dev-time, stated honestly.** You already pay for Claude Max, so
   *building* the agent (Claude Code, native SDK/model knowledge, prompt iteration) carries
   no incremental cost. **Caveat for the defense:** Claude Max covers interactive/Claude
   Code use, **not** programmatic API calls from a deployed service — production inference
   bills per-token via an API key. The synergy is **dev velocity, not free runtime**, and
   doesn't change the cost-at-scale math.
3. **Reversible** — if evals favor GPT-5 or Gemini on a sub-task, swapping is config.
4. **Feeds the cost analysis** — the 3-tier routing is what makes the
   cost-at-100/1K/10K/100K-users projection defensible rather than flat token×N.

---

## Decision 3 — Observability

Decided purely on merits (BAA is a given, so PHI-in-traces isn't a constraint):

| Option | Hosting / license | ~Cost (entry → paid) | Fit |
|---|---|---|---|
| **Langfuse** | OSS (MIT); self-host or cloud | Free Hobby (50k events/mo) → $29 Core → $199 Pro; **self-host free** | **Recommended** — all-in-one (traces + cost + evals + dashboards), OTel-native, framework-agnostic; cheapest on-ramp |
| **Braintrust** | Closed; cloud | Free Starter (1 GB, 10k scores) → $249 Pro | Eval-first / regression; more eval platform than prod-ops; priciest paid step |
| **LangSmith** | Closed; cloud | Free dev → $39/seat + trace overage ($2.50/1k) | LangChain/LangGraph-native — loses its edge off LangGraph; per-seat billing |
| **Arize Phoenix** | OSS (Elastic 2.0); self-host or AX cloud | **Free** (OSS self-host, uncapped); AX cloud free → $50 Pro | Rich eval metrics, OTel/OpenInference; strong free/self-host option |

Billing units differ (Langfuse events · Braintrust GB+scores · LangSmith seats+traces ·
Phoenix spans), so headline prices aren't directly comparable — read them as on-ramp
signals and **verify at build time**.

### Pick: Langfuse (managed cloud)

1. **All-in-one** — covers the PRD's observability *and* eval-scoring in one tool (traces
   + token/cost + dashboards + datasets), no stitching two products together.
2. **Framework/model-agnostic + OTel-native** — clean Pydantic AI fit; correlation IDs
   ride as trace/span attributes; no vendor path lock-in the way LangSmith→LangGraph is.
3. **Reversible** — managed cloud or self-host the same OSS tool; doesn't trap us either way.
4. **Cheapest on-ramp** — free Hobby covers the build, $29 Core if we outgrow it, and the
   OSS self-host stays free at any scale. Braintrust jumps to $249 Pro and LangSmith meters
   per-seat + per-trace — so cost quietly confirms the pick rather than driving it.

Reach for **Braintrust** if eval-regression becomes the dominant need; **Arize Phoenix**
if we'd rather self-host an OSS stack.

---

## Near-default layers (constrained, not contested)

- **Service — FastAPI.** Async (matters under the PRD's 10/50-concurrent load tests),
  Pydantic-native, trivial split `/health` (process alive) vs `/ready` (actually pings
  OpenEMR FHIR + LLM provider + Langfuse), SSE for streaming. Litestar is a fine alt.
- **Contracts — Pydantic v2.** Strict schemas per the PRD engineering req; the framework
  and FHIR layers already speak it.
- **FHIR client — `fhir.resources` + httpx.** `fhir.resources` gives **Pydantic models
  for FHIR R4**, so the tool layer parses `Patient`/`Condition`/`MedicationRequest`/
  `AllergyIntolerance`/`Encounter` into typed objects (parse-don't-validate at the
  boundary). httpx carries the SMART-token'd calls with timeouts/retries. Also home to the
  med-dedup / text-match fallback logic from `deployment-strategy.md`.
- **Eval — Pydantic Evals + pytest, scored in Langfuse.** pytest runs boundary/invariant/
  regression cases in CI; Pydantic Evals structures datasets + scorers; Langfuse holds
  online scores and regression history.
- **Verification — Pydantic AI `output_validator` + `ModelRetry`.** The gate itself
  (Decision 1); enforces source-attribution and clinical constraints before output.

---

## Open questions to validate before locking

1. **Single-agent assumption** — confirmed against `USERS.md`? If any UC needs real
   delegation, revisit LangGraph (1-day spike on the UC-4 med/problem flow).
2. **Framework spike** — prototype the verify-then-answer gate in Pydantic AI, LangGraph,
   and (given your experience) ADK; pick on how cleanly each expresses "unattributable
   claim → force a retry."
3. **Model routing thresholds** — which sub-tasks go to Haiku vs Sonnet; settle
   empirically once the eval suite exists, since it drives the cost-at-scale analysis.

---

## Sources (2026, verified)

- Frameworks: [open-techstack — LangGraph vs OpenAI Agents SDK vs PydanticAI (2026)](https://open-techstack.com/blog/langgraph-vs-openai-agents-sdk-vs-pydanticai-2026/), [Speakeasy — framework comparison](https://www.speakeasy.com/blog/ai-agent-framework-comparison/), [Langfuse — OSS framework comparison](https://langfuse.com/blog/2025-03-19-ai-agent-comparison)
- Claude Agent SDK: [Anthropic — overview](https://platform.claude.com/docs/en/agent-sdk/overview), [anthropics/claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python)
- Google ADK: [ADK docs](https://google.github.io/adk-docs/), [Google Cloud — ADK overview](https://docs.cloud.google.com/agent-builder/agent-development-kit/overview), [LiteLLM — Google ADK](https://docs.litellm.ai/docs/projects/Google%20ADK) — model-agnostic via LiteLLM (incl. Anthropic); SSE default since v1.22 + native bidi A/V; Vertex Agent Engine deploy; built-in eval with multi-turn datasets + regression detection
- Observability: [MLflow — top 5 agent observability tools 2026](https://mlflow.org/top-5-agent-observability-tools/), [Latitude — best LLM observability tools 2026](https://latitude.so/blog/best-llm-observability-tools-agents-latitude-vs-langfuse-langsmith), [Laminar — Langfuse alternatives 2026](https://laminar.sh/article/langfuse-alternatives-2026)
- Observability pricing (2026): [Langfuse](https://langfuse.com/pricing) (free / $29 / $199; self-host free), [Braintrust](https://www.braintrust.dev/pricing) (free / $249), [LangSmith](https://www.langchain.com/pricing) ($39/seat + $2.50/1k traces), [Arize Phoenix](https://arize.com/phoenix/) (OSS free; AX $0 / $50)
- Claude pricing: current Anthropic model catalog (Sonnet 5 $3/$15 intro $2/$10 through 2026-08-31; Haiku 4.5 $1/$5; Opus 4.8 $5/$25)
- Competitor pricing (approximate, verify at build): [IntuitionLabs — AI API pricing 2026](https://intuitionlabs.ai/articles/ai-api-pricing-comparison-grok-gemini-openai-claude), [Google Gemini pricing](https://ai.google.dev/gemini-api/docs/pricing), [OpenAI pricing](https://developers.openai.com/api/docs/pricing)
