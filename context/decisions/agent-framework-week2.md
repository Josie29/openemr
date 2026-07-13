# Agent Framework — Week 2 Multi-Agent Decision Evidence

**Purpose:** Working analysis behind the Week-2 Architecture Defense. Week 2 mandates a
**supervisor + 2 workers** (intake-extractor, evidence-retriever) with **inspectable, logged
routing and explicit handoffs** ([`PRD-week-2.md`](../../PRD-week-2.md) §"Multi-agent
architecture", Core Req 4). That requirement reopens the Week-1 framework choice, which picked
**Pydantic AI** for a single agent and parked LangGraph as the runner-up
([`agent-tech-stack.md`](agent-tech-stack.md) Decision 1;
[`ARCHITECTURE.md`](../../ARCHITECTURE.md) §6.1 tripwires). This file re-runs that pick against
the multi-agent shape. Decision evidence, not a deliverable — `ARCHITECTURE.md` /
`W2_ARCHITECTURE.md` remain the source of truth.

**Grounding:** [`PRD-week-2.md`](../../PRD-week-2.md) (supervisor+workers, citation contract,
eval gate, correlation-ID + per-worker child-span requirements),
[`agent-tech-stack.md`](agent-tech-stack.md) (Week-1 pick + Decision-1 table),
[`ARCHITECTURE.md`](../../ARCHITECTURE.md) §6/§6.1 (single-agent verdict + pre-registered
tripwires — Week 2's supervisor+workers **fires tripwire #3**, "conflicting-source
reconciliation becomes its own reasoning stage": the evidence-retriever is a distinct
guideline-evidence role, separate from the record-reading extractor), and the current agent
[`agent/src/copilot/agent.py`](../../agent/src/copilot/agent.py) — one `pydantic_ai.Agent`, 6
FHIR read tools, one `@agent.output_validator` grounding gate raising `ModelRetry` (`retries=2`).
Framework facts verified against 2026 sources (linked at the end).

---

## Fixed constraints (from Week 1 — do NOT re-litigate)

Python · Railway deploy · **Claude** (Anthropic) models · **Langfuse** (OTel-native)
observability · **Pydantic v2** contracts throughout · BAA assumed for every vendor. These are
decided; the framework is judged only on how cleanly it lands the multi-agent requirement on
top of them. **The `output_validator` grounding gate is the crown jewel** — it MUST survive any
framework port, because it is the deterministic half of the Week-1 verification strategy
([`ARCHITECTURE.md`](../../ARCHITECTURE.md) §7) and the mechanical enforcement of Week 2's
citation contract.

---

## The decision, in one sentence

Week 2 does **not** justify abandoning Pydantic AI. It now ships first-class multi-agent
patterns (agent delegation, programmatic hand-off, and `pydantic-graph`), so a supervisor +
2 workers is expressible **without** rewriting the tools, schemas, gate, or Langfuse wiring —
whereas LangGraph or the OpenAI Agents SDK would force a port of the crown-jewel gate for a
routing capability we can already get.

---

## Contenders (ordered by fit to *this* problem)

| Framework | Multi-agent model | Grounding gate survives? | Migration from today | Pydantic-native typing | Langfuse / OTel fit | Maturity · lock-in |
|---|---|---|---|---|---|---|
| **Pydantic AI** *(incumbent)* | Supervisor delegates to workers **as tools** (`ctx.usage` threads token accounting through the chain); also programmatic hand-off + `pydantic-graph` FSM for explicit routing | **Yes, unchanged.** `@agent.output_validator` + `ModelRetry` is a per-agent pre-return hook — attach the grounding gate to the extractor/critic worker *and* the final answer; it is the same code | **Near-zero.** Keep 6 FHIR tools, `CopilotDeps`, `ChatResponse`, `resolve_claims`, the gate, Langfuse setup; wrap workers as delegate agents + a supervisor | **Native — it *is* Pydantic.** Worker I/O and structured outputs are validated models, no adapter | Native OTel; Langfuse has a first-party Pydantic AI integration; delegated agent runs nest under the parent span, workers get distinct `name` → child spans | GA, Pydantic-team backed; provider-neutral (Claude native), no lock-in |
| **LangGraph** *(runner-up)* | **Explicit** supervisor graph: `create_supervisor` / `Command` handoffs pass control + message history between nodes; routing is a first-class inspectable edge | Not first-class. No pre-return validate-then-retry primitive; rebuild the gate as a `verify → revise` node/edge loop (doable, but it's a port) | **High.** Re-express tools as graph nodes, adopt `MessagesState`, re-wire the gate and Langfuse callback; biggest rewrite of the three | Good but not intrinsic — Pydantic models sit *on* the graph, TypedDict state is the native currency | Langfuse `CallbackHandler` (LangChain); mature, but routing spans follow LangChain's shape not ours | Most mature multi-agent + durable checkpointing; heaviest; LangChain-adjacent gravity |
| **OpenAI Agents SDK** | **Handoffs + guardrails** are the core primitives — supervisor `handoff()` to workers, built-in tracing of the handoff chain | Weak fit. Output **guardrails trip a tripwire and *halt*** — they flag/stop, they do **not** feed a correction back to the model the way `ModelRetry` does. The self-correcting gate would be hand-built | **High.** New agent/handoff/guardrail model; Claude is **second-class via LiteLLM**; native tracing targets OpenAI's platform | Weaker — not Pydantic-first; guardrail/output types are the SDK's own | Langfuse via OpenInference OTel instrumentation works; but the SDK's *native* tracing wants OpenAI's dashboard | Production-ready, small primitive set; **OpenAI-first** — Anthropic is the adapter path, mild lock-in pull |
| CrewAI / Google ADK *(brief)* | Role-based crews / code-first multi-agent | Hand-built gate either way | High | Non-native | OTel varies | Don't change the conclusion: both are role-delegation-first (over-scoped for 2 workers) and neither offers a first-class equivalent to the gate. ADK's edges (Vertex, Gemini) are off-table on Railway+Claude — same finding as Week 1 |

---

## Pick: **Pydantic AI** (runner-up **LangGraph**)

Each reason traces to a Week-2 requirement:

1. **The supervisor+workers requirement is satisfiable in-framework, so the mandate is met
   without a rewrite.** PRD Core Req 4 permits "LangGraph, the OpenAI Agents SDK, **or another
   inspectable orchestration framework**." Pydantic AI's **agent delegation** (supervisor calls
   each worker from inside a tool, then resumes control) plus **programmatic hand-off** for the
   supervisor's route decision *is* an inspectable orchestration framework — the delegation call
   is ordinary typed Python, and `pydantic-graph` is available if the routing later wants an
   explicit finite-state machine. The "small graph: one supervisor, one intake-extractor, one
   evidence-retriever" (PRD Stage 3) maps directly.

2. **The crown-jewel grounding gate survives byte-for-byte** (PRD "Vision extraction without
   invention" + citation contract + `factually_consistent`/`citation_present` eval rubrics).
   `@agent.output_validator` + `ModelRetry` is a *per-agent* pre-return hook, so the same gate
   attaches to the extractor worker (reject an extracted fact not traceable to a source span),
   the evidence-retriever (reject an evidence claim without chunk metadata), **and** the final
   supervisor answer — one defensible seam, reused, not rebuilt. LangGraph and the OpenAI Agents
   SDK both force a rebuild: LangGraph has no validate-then-retry primitive, and the OpenAI SDK's
   output guardrails *halt* on a tripwire rather than feeding a correction back to the model.
   Confirmed still GA in 2026, including the per-output retry budget (`retries={'output': N}`,
   matching today's `retries=2`).

3. **Migration cost is near-zero** — the decisive practical fact on a one-week sprint with an
   Architecture Defense in 4 hours. The 6 FHIR tools, `CopilotDeps`, `ChatResponse`,
   `resolve_claims`, the gate, and the Langfuse wiring all carry over unchanged; Week 2 *adds*
   two delegate agents, the two extraction schemas (`lab_pdf`, `intake_form`), and the RAG tool.
   LangGraph or the OpenAI SDK would spend that same week porting Week-1 surface area instead of
   building the Week-2 capability — the opposite of the PRD's "good Week 1 architecture should
   compound here."

4. **Pydantic-native typing carries the canonical-contract requirement for free.** PRD
   Engineering Reqs make the extraction schemas "the source of truth — not what the model happens
   to return," and demand a typed contract on *every* interface including **supervisor handoffs**.
   In Pydantic AI those handoff payloads and worker outputs are validated Pydantic v2 models with
   no adapter — the schema *is* the boundary. TypedDict-state (LangGraph) or the SDK's own types
   are a layer to bridge.

5. **Langfuse / OTel fit lands the per-worker tracing requirement.** PRD requires a full
   multi-agent trace reconstructable from the **correlation ID alone**, with **each worker
   invocation a child span of the supervisor span**. Pydantic AI emits native OTel; the correlation
   ID rides as a span attribute (as today); delegated agents nest under the parent trace and each
   worker's distinct `name` becomes its child span — exactly the shape the PRD's distributed-tracing
   line asks for. Langfuse has a first-party Pydantic AI integration, so no new observability code.

---

## Runner-up: **LangGraph** — and exactly when I'd switch

LangGraph is the *correct* upgrade the moment the routing stops being expressible as
"supervisor calls worker, worker returns, supervisor decides next." Switch when **any** of:

- **Durable, resumable state mid-flow** — e.g. document ingestion pauses for a human to confirm a
  low-confidence extracted field, then resumes hours later. LangGraph's checkpointer is the most
  mature answer; Pydantic AI would need to hand-roll persistence. (This is `ARCHITECTURE.md` §6.1
  tripwire #2, "branching durable state with a human-in-the-loop mid-flow" — **not** in
  `PRD-week-2.md` today.)
- **Branching routing with cycles the supervisor can't express procedurally** — a real
  `extract → conflict-resolve → re-retrieve → verify → escalate` loop where the path is
  data-dependent and needs to be a diagrammable, replayable graph for auditors.
- **The critic agent (PRD extension) grows into an adjudication stage** with its own back-and-forth
  — `ARCHITECTURE.md` §6.1 tripwire #1.

At that point migrate the supervisor to a LangGraph graph and keep the workers as-is — the port is
contained precisely *because* the tools, schemas, and gate were kept framework-neutral. For the
Week-2 "small graph" (PRD's own word: *small*), LangGraph is machinery we'd pay for now and grow
into later; the pick stays reversible.

**On the OpenAI Agents SDK:** its handoff primitive is genuinely the cleanest *named* handoff of
the three and its tracing of the handoff chain is nice — but two constraints sink it here. Claude
is second-class (via LiteLLM), against a fixed-Claude constraint; and its output guardrails halt
rather than self-correct, so the grounding gate — the one thing that must survive — would be
rebuilt. Reach for it only if the stack were OpenAI-first, which it is not.

---

## The single biggest risk of this pick (state it in the defense)

Pydantic AI's multi-agent patterns are **younger and less battle-tested** than LangGraph's
supervisor tooling, and its supervisor routing is *procedural Python* rather than a first-class,
diagrammable graph object. A grader who equates "inspectable routing" with "a rendered graph with
labeled edges" (the LangGraph/OpenAI-SDK aesthetic) may read delegation-via-tool-call as less
inspectable. **Mitigation:** make routing legible on our terms — log every supervisor route
decision as a structured event (which worker, why, correlation ID) and surface it as a Langfuse
child span, so the routing is inspectable *in the trace* even though it's expressed in code. If
that proves insufficient for the eval/observability bar, `pydantic-graph` gives an explicit FSM
inside the same framework before any cross-framework migration is needed.

---

## Sources (2026, verified)

- Pydantic AI multi-agent: [Multi-Agent Patterns — Pydantic Docs](https://pydantic.dev/docs/ai/guides/multi-agent-applications/), [pydantic-ai `docs/multi-agent-applications.md`](https://github.com/pydantic/pydantic-ai/blob/main/docs/multi-agent-applications.md) (agent delegation via tools; `ctx.usage` threads token accounting; delegated runs nest in the parent trace with distinct `name`), [`pydantic-graph` / Agents docs](https://pydantic.dev/docs/ai/core-concepts/agent/)
- Grounding gate still GA: [Output — Pydantic Docs](https://pydantic.dev/docs/ai/core-concepts/output/) (`output_validator` + `ModelRetry`), [pydantic-ai `docs/retries.md`](https://github.com/pydantic/pydantic-ai/blob/main/docs/retries.md) (per-output retry budget, defaults 1, settable), [exceptions API](https://pydantic.dev/docs/ai/api/pydantic-ai/exceptions/)
- LangGraph supervisor/handoffs: [langgraph-supervisor-py](https://github.com/langchain-ai/langgraph-supervisor-py), [`create_supervisor` reference](https://reference.langchain.com/python/langgraph-supervisor/supervisor/create_supervisor), [handoffs how-to (`Command`)](https://reference.langchain.com/python/langgraph-supervisor/handoff)
- OpenAI Agents SDK: [Guardrails — OpenAI Agents SDK](https://openai.github.io/openai-agents-python/guardrails/) (output guardrails trip a tripwire / halt), [OpenAI Agents SDK with LiteLLM](https://docs.litellm.ai/docs/tutorials/openai_agents_sdk) (Claude via `LitellmModel`, adapter path)
- Langfuse / OTel fit: [Observability for Pydantic AI with Langfuse](https://langfuse.com/integrations/frameworks/pydantic-ai), [Trace the OpenAI Agents SDK with Langfuse](https://langfuse.com/integrations/frameworks/openai-agents), [LangChain/LangGraph tracing](https://langfuse.com/integrations/frameworks/langchain), [OpenTelemetry for LLM Observability](https://langfuse.com/integrations/native/opentelemetry)
- Landscape comparisons (2026): [open-techstack — LangGraph vs OpenAI Agents SDK vs PydanticAI (2026)](https://open-techstack.com/blog/langgraph-vs-openai-agents-sdk-vs-pydanticai-2026/), [Speakeasy — agent framework comparison](https://www.speakeasy.com/blog/ai-agent-framework-comparison), [LangChain — best AI agent frameworks 2026](https://www.langchain.com/resources/ai-agent-frameworks)
