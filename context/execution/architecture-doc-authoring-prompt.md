# Authoring Prompt — Write `./ARCHITECTURE.md` (PRD Stage 5 hard-gate deliverable)

> **How to use this file.** This is a *prompt*, not a deliverable. Hand it to a fresh
> Claude Code session as its opening instruction. Its **sole job** is to author
> `./ARCHITECTURE.md` at repo root by synthesizing decision evidence that already exists —
> **no code, no scaffolding, no other deliverable.** A single-focus session writes a
> sharper document than one where the doc is a warm-up before building.
>
> **Supersedes** `context/execution/implementation-prompt-01-walking-skeleton.md` "Phase 0". Once
> `ARCHITECTURE.md` exists, prompt-01 should be run assuming the doc is already written —
> its Phase 0 is redundant with this.

---

## 0. Mission

Produce `./ARCHITECTURE.md` — the PRD Stage 5 hard-gate deliverable and the **source of
truth** every downstream agent capability must trace back to. The standard is not "a good
design doc." The standard, stated verbatim in the PRD, is a document that **could be
defended in front of a hospital CTO deciding whether to put this in front of their
physicians.** It is defended live (the "Architecture Defense" checkpoint). Write it so the
author of the defense can hold every claim in it under questioning.

The architecture is **already decided** — Option D, single-agent, Pydantic AI + Claude +
Langfuse, FHIR-only. Your task is to **consolidate and present** that decision defensibly,
**not to re-open it.** If you believe a decision is wrong, flag it in a "risks/open
questions" section — do not silently redesign.

## 1. Read first — source of truth, in this order

Do not write a word of the deliverable until these are read. Every section you write must
trace to them; where they conflict with each other, the **PRD wins**, then the deliverables
(`AUDIT.md`, `USERS.md`), then the `/context` evidence.

1. **`PRD.md`** — the north star. Anchor especially on: Stage 5 ("Develop the AI
   Integration Plan") and its hard-gate wording; the **Agent Requirements** (agentic
   chatbot / verification / observability / eval); the **Engineering Requirements**
   (correlation IDs, canonical schemas, `/health` vs `/ready`, dashboards, alerts,
   load tests, cost analysis); the **Submission** row for `ARCHITECTURE.md`; and the
   **Interview Preparation** questions (§ "Your Architecture" and § "Production Thinking")
   — those are the questions the doc must pre-answer.
2. **`USERS.md`** — the target user and UC-1…UC-5. This is the document `ARCHITECTURE.md`
   must trace *back to*: every agent capability maps to a use case here, or it is out of
   scope. UC-5 (cross-cover) is the authorization test case.
3. **`AUDIT.md`** — the security/compliance findings the design must answer to: the IDOR
   gap, the new PHI→LLM outbound flow (BAA + §164.514 de-identification), secrets hygiene,
   no in-app TLS/HSTS, med-storage data-quality quirks. The Interview prep asks "how did
   the audit change the plan?" — the doc must show that lineage.
4. **`context/decisions/deployment-strategy.md`** — **Option D is selected.** The full A/B/C/D
   comparison, the Railway topology (both diagrams), the FHIR-only + patient-scoped-token
   + no-DB-creds data model, and the authorization model. This is the bulk of the
   "where the agent lives" content.
5. **`context/decisions/agent-tech-stack.md`** — the stack and *why*: Pydantic AI (runner-up
   LangGraph), Claude tiered (Sonnet 5 default / Haiku 4.5 cheap checks / Opus 4.8 hard
   cases), Langfuse, FastAPI, `fhir.resources` + httpx, Pydantic Evals. The verification
   gate is Pydantic AI's `output_validator` raising `ModelRetry`.
6. **`context/decisions/agent-workflow.md`** — the single-vs-multi-agent derivation, the five-tool
   inventory, the per-UC orchestration breakdown, and the traceability matrix. This is
   the "how the agent works per use case" content and the load-bearing **single-agent
   verdict** (with the tripwires that would flip it).
7. **`context/decisions/persona-analysis.md`**, **`context/decisions/synthetic-data-generation.md`**,
   **`context/decisions/patient-data-exposure-map.md`** — supporting evidence; cite where relevant.

If anything in these turns out to be stale against the actual code or live seed DB, **stop
and flag it** rather than repeating it — do not invent.

---

## 2. What `ARCHITECTURE.md` must contain

Write it at **repo root** (`./ARCHITECTURE.md` — the PRD expects deliverables at root; the
reasoning stays in `/context/`). Use this structure. Adapt headings to read naturally, but
cover every part — each maps to a graded requirement or an Interview question.

1. **One-page summary (~500 words) — FIRST, and brevity is graded.** High-level
   architecture, the key decisions, the major considerations, and the tradeoffs — dense
   and skimmable. A hospital CTO reads only this page; it must stand alone. The PRD calls
   the brevity requirement "intentional" — do not overrun it, and do not bury the lede.
2. **Context & constraints.** The three that shape everything, each pulled from a
   deliverable, not asserted: the target user and the **<15s latency budget** (USERS.md);
   the **IDOR gap, PHI→LLM/BAA boundary, and secrets/TLS posture** (AUDIT.md); the demo-
   data-only + assumed-BAA posture (PRD).
3. **System topology — Option D.** Both diagrams (the PHP module shim *and* the standalone
   Python agent service), plus the current Railway state they extend. State plainly what
   runs where and why the two-concern split (UI/auth in PHP, agent logic in Python).
4. **Data-access model.** FHIR R4 only, under a SMART `patient/*.read` token; the agent
   holds **no DB credentials**. The five resources (`Patient`, `Condition`,
   `MedicationRequest`, `AllergyIntolerance`, `Encounter`) + the optional latency-only
   snapshot endpoint. Note the verified fact that FHIR `MedicationRequest` unions both med
   sources (hides no meds) and that dedup/text-fallback lives in the med tool.
5. **Authorization & trust boundaries.** Where they are and how each is enforced (this is
   an explicit Interview question). The patient-scoped token makes the IDOR gap
   *unreachable through the agent* for UC-1–4; UC-5 is gated at the module launch point
   (care-team / break-the-glass) **in PHP, not the LLM**. Enforcement stays at the OpenEMR
   boundary. Say explicitly why authorization is deliberately *not* an agent's job.
6. **The agent.** The **single-agent verdict** and its justification (with the tripwires
   that would flip it to multi-agent — show the verdict is conditional, not dogmatic).
   Pydantic AI, the tool inventory, the per-UC call patterns (parallel fan-out / sequential
   / iterative multi-turn / cross-reference), and the model tiering. Every capability traces
   to a UC.
7. **Verification strategy.** The load-bearing section — the Interview asks "why designed
   this way?" The `output_validator` gate: **where** in the flow it sits (before any
   response reaches the physician), **what** it catches (source attribution + domain
   constraints), how `ModelRetry` forces a correction, and its **known limitations**. State
   the limits honestly — a gate that claims to catch everything is not defensible.
8. **Failure modes & graceful degradation.** The Interview asks "what does the agent do
   when a tool fails or a record is missing?" Answer concretely for: a failed FHIR read, a
   sparse/empty record, a malformed model output, an LLM timeout. Tie each to the PRD's
   graceful-degradation requirement — a clinical tool that silently fails is worse than none.
9. **PHI / BAA / de-identification seam.** The single Python LLM-call code path as the one
   place every outbound-PHI call passes through — the natural redaction + logging seam, and
   the answer to the audit's flagged new-outbound-flow finding.
10. **Observability & operations.** Correlation IDs across every boundary; Langfuse for
    traces / token+cost / dashboards / eval scores; the split `/health` (process alive) vs
    `/ready` (actually pings FHIR + LLM + Langfuse). Name the three PRD alerts (p95 latency,
    error rate, tool-failure rate).
11. **Evaluation approach.** Pydantic Evals + pytest scored to Langfuse; the boundary /
    invariant / regression framing (missing data, IDOR-style refusal, "claims cite a
    source"); `TestModel`/`FunctionModel` for deterministic, no-real-LLM CI runs.
12. **Scale & cost trajectory (brief).** Address the Interview's "scale to a 500-bed
    hospital, 300 concurrent clinical users" and "what changes at each level." Keep it
    tight and **point to the separate AI Cost Analysis deliverable** for the
    100/1K/10K/100K-user numbers — don't duplicate it here. Explain how the 3-tier model
    routing is what makes that projection defensible rather than flat token×N.
13. **Rejected alternatives.** State what was *not* chosen and why — Options A/B/C (embed /
    standalone / API-boundary), the framework runners-up (LangGraph, ADK, Claude Agent SDK,
    raw SDK), and the model/observability alternatives. Condensed, but present: hiding the
    roads not taken reads as not having considered them.
14. **Traceability matrix.** Capability → UC (USERS.md) → PRD requirement. Reuse/refine the
    matrix already in `agent-workflow.md`. Anything with no UC row is out of scope.
15. **Risks & open questions.** What would change the design, what's still unproven (the
    framework spike, model-routing thresholds), and the **single most concerning failure
    mode** (an Interview question) — named and reasoned, not hedged.

End with a one-line pointer: the detailed decision evidence lives in `/context/*.md`
(`deployment-strategy.md`, `agent-tech-stack.md`, `agent-workflow.md`).

---

## 3. Acceptance criteria — the doc is done when…

1. It opens with a **~500-word one-page summary** covering architecture, key decisions,
   considerations, and tradeoffs — and stays near that length.
2. **Every agent capability traces to a UC** in `USERS.md`; no capability exists that no
   UC needs (the PRD is explicit: no use case → no feature).
3. It **states the rejected A/B/C options and framework runners-up** and why each lost.
4. It **pre-answers every Interview question** in PRD § "Your Architecture" and
   § "Production Thinking" — verification design, tool-failure/missing-record behavior,
   trust boundaries, 500-bed/300-concurrent scale, path to real-physician reliance, and the
   most concerning failure mode.
5. It shows the **audit → plan lineage** — how AUDIT.md findings (IDOR, PHI/BAA,
   data-quality) shaped specific decisions.
6. Diagrams render (Mermaid, matching `deployment-strategy.md`'s style) and the topology
   matches Option D exactly.
7. It reads as **defensible to a hospital CTO** — honest about limitations, no overclaiming,
   tradeoffs visible.

## 4. Guardrails — tone, scope, and what NOT to do

- **Do not write code, scaffold `/agent/`, or touch OpenEMR core.** This session produces
  one markdown file. Scaffolding is prompt-01's job.
- **Do not re-litigate settled decisions.** Option D, single-agent, Pydantic AI, Claude,
  Langfuse, FHIR-only are decided. Present them with their justification; disagreement goes
  in § Risks, not into a redesign.
- **Do not invent capabilities, tools, or use cases** beyond what USERS.md / agent-workflow.md
  establish. Surface area is set by user need, not by what's interesting.
- **Do not overclaim.** Every limitation the evidence docs admit (the med coding-completeness
  weakness, the gate's known limits, Claude Max ≠ free runtime, the still-open IDOR fix in
  core) must survive into the deliverable. A CTO trusts a doc that names its own weaknesses.
- **Cite, don't dump.** Synthesize the `/context` evidence into a clean deliverable; link
  back to it as "decision evidence" rather than pasting it wholesale. `ARCHITECTURE.md`
  should be readable on its own.
- **Match the repo's doc conventions** — Mermaid diagrams, the `context/*` cross-reference
  style, no emojis.

## 5. Surface, don't silently resolve

Stop and flag rather than guessing if: a `/context` claim contradicts `AUDIT.md`/`USERS.md`
or the actual code; a UC in `USERS.md` has no corresponding capability in `agent-workflow.md`
(a gap); or the ~500-word summary can't honestly cover the design without overrunning
(means the design has more load-bearing decisions than a one-pager holds — flag which to
promote). These change the deliverable; don't paper over them.
