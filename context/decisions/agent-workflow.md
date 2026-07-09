# Agent Workflow — Stage 5 Decision Evidence

**Purpose:** The missing middle between `../USERS.md` (what the physician needs) and the
stack decisions in `deployment-strategy.md` (where the agent lives) and
`agent-tech-stack.md` (what fills it). For each use case in `USERS.md` this doc specifies
the concrete **tools**, **data**, and **orchestration strategy** the agent uses to satisfy
it — then aggregates those five analyses into the architecture's load-bearing conclusion:
**single agent, or multi-agent?** Decision evidence for the architecture defense, not a
deliverable; `../ARCHITECTURE.md` remains the source of truth.

**Grounding:** `../USERS.md` (UC-1–UC-5, guardrails, <15s latency budget),
`deployment-strategy.md` (Option D — FHIR-only, patient-scoped token, module-launch
authorization), `agent-tech-stack.md` (Pydantic AI, `output_validator` gate, Claude
tiered, five FHIR resources), `../AUDIT.md` (IDOR finding, med-storage quirks, PHI
boundary).

**This doc resolves `agent-tech-stack.md`'s open question #1** — *"Single-agent
assumption — confirmed against `USERS.md`?"* — by deriving the answer from the use cases
rather than assuming it. The verdict (Section 6) feeds back into that doc.

---

## Method — bottom-up, not top-down

The sibling docs already *assume* one agent; that assumption is exactly what the PRD warns
against — *"every agent capability must trace to a specific user problem"* (PRD Agent
Requirements). So this doc runs the derivation in the honest direction: take each use case
as a given, work out the minimum tool set, data reads, and control flow it actually
requires, and record whether that use case *on its own* demands agent-to-agent delegation.
Only after all five are on the table (Section 4) do we aggregate to the architecture
verdict (Section 6). If the evidence pointed at multi-agent, this doc would say so and
`agent-tech-stack.md`'s framework pick would need revisiting. It points single — but the
reasoning, not the conclusion, is the deliverable.

A definition we use throughout, because the whole verdict turns on it:

> **Multi-agent** has two common shapes; the one that matters here is the
> **orchestrator → sub-agents** pattern — a top-level agent *delegates* to sub-agents, where
> each sub-agent is itself LLM-driven, with **its own prompt, its own context window, and its
> own tool subset**, and returns a distilled result. (The role-handoff shape — supervisor
> routing to workers, peer handoff — is the same idea from a different angle.) In a **single
> agent**, those same capabilities are **tools** (deterministic functions) or **inline
> reasoning steps** directly under one loop, all sharing one context and one prompt.
>
> So the decisive question is not "are there multiple steps?" but **"does a capability need
> its own isolated LLM context/prompt (→ sub-agent), or does it belong inline as a tool or a
> step under one agent?"** A sub-agent earns its keep only when a subtask (a) reads far more
> than the orchestrator needs to keep — *context isolation*, the strongest reason here;
> (b) needs a materially different prompt/tool set; or (c) is a genuinely parallel *reasoning*
> decomposition. Absent those, folding the capability in as a tool is simpler, faster, and
> cheaper. Multiple tool calls, a diff, a validation step, parallel *fetches* — none are
> sub-agents. Conflating them is the usual way a design over-claims "multi-agent."

---

## Shared tool inventory

Every use case draws from one small, fixed tool set — five FHIR R4 reads under the
SMART `patient/*.read` token (`deployment-strategy.md`, Option D), plus one optional
composite. Defined once here; the per-UC sections reference tools by name. Every return
type is a Pydantic v2 model (`agent-tech-stack.md`: `fhir.resources` parses each FHIR
resource into a typed object at the boundary — parse-don't-validate).

| Tool | FHIR resource | Returns (typed) | Notes |
|---|---|---|---|
| `get_patient()` | `Patient` | `PatientDemographics` | Name, DOB, sex, identifiers. One call, one resource. |
| `get_problems()` | `Condition` | `list[Problem]` | SNOMED-coded active/inactive problem list, with onset/abatement dates and clinical status. |
| `get_medications()` | `MedicationRequest` | `list[Medication]` | **Dedup + text-fallback logic lives here** (below). RxNorm code where present, `medicationCodeableConcept.text` always. Start/stop dates, status. |
| `get_allergies()` | `AllergyIntolerance` | `list[Allergy]` | Substance, reaction, criticality. |
| `get_encounters()` | `Encounter` | `list[Encounter]` | Encounter dates, type, reason. Ordered; supports "last N" and date-bounded reads. |
| `get_patient_snapshot()` | *composite* | `PatientSnapshot` | **Optional, latency-only.** One call bundling the five reads (the single custom endpoint `deployment-strategy.md` reserved for the <15s budget). Used when per-resource round-trips would blow the budget; behind the same patient-scoped token. |

**Two data realities the med tool must own** (from `deployment-strategy.md`, verified
against the seed DB), not pushed onto the agent's reasoning:

- **Dedup, not omission.** FHIR `MedicationRequest` unions `prescriptions` and
  `lists WHERE type='medication'`; a med in both without the internal link surfaces as two
  resources (one RxNorm-coded, one text-only — 242/249 overlap in our seed). `get_medications()`
  dedups, keying on drug *text* (the list branch has no code to match on).
- **Text fallback for coding-completeness.** List-originated meds lack structured RxNorm,
  so any code-based cross-referencing (UC-4) falls back to name/text matching. The tool
  exposes both the code (nullable) and the text (always) so the agent can cite either.

No tool ever writes. No tool holds DB credentials. No tool can read a patient other than
the one the token is scoped to — which is what makes the IDOR gap unreachable through the
agent for UC-1–UC-4 (`deployment-strategy.md`, Authorization model).

---

## Per-use-case breakdown

Each UC below states: the ask, the data, the tools + **call pattern**, the orchestration
(how one turn flows), what the **verification gate** must catch for this UC, how the named
failure mode is handled concretely, and the **single-vs-multi signal** this UC contributes
on its own.

### UC-1 — Pre-visit orientation

- **Ask:** *"Give me the picture on my 9am."*
- **Data:** `Patient`, `Condition`, `MedicationRequest`, `AllergyIntolerance`, last 1–2 `Encounter`.
- **Tools + call pattern:** all five, **parallel fan-out** in a single tool-loop step —
  the reads are independent, so they issue concurrently (or as one `get_patient_snapshot()`
  if latency demands). No read depends on another's result.
- **Orchestration:** one turn — fan-out fetch → synthesize a 3–4 line orientation →
  verification gate → stream. This is the *opening* turn; the physician's follow-up
  (whatever it is) reuses the same fetched context in the next turn (see UC-3 mechanics).
- **Verification gate catches:** every clause of the summary cites a source row; the
  "single most relevant change" flagged is one the data actually supports, not an inferred
  narrative.
- **Failure mode (sparse record):** if the fan-out returns one encounter / near-empty
  lists, the summary says so plainly ("new patient, one encounter on file") rather than
  padding. The gate rejects any orientation richer than the data warrants.
- **Single-vs-multi signal:** **single.** Parallel fetch + one synthesis + one validation
  is a tool loop, not delegation.

### UC-2 — What's changed since the last visit

- **Ask:** *"What's different since I last saw her?"*
- **Data:** `Condition`/`MedicationRequest`/`AllergyIntolerance` with dates + status;
  `Encounter` dates to establish the "prior visit" boundary.
- **Tools + call pattern:** `get_encounters()` first (to fix the comparison date), then the
  three list reads — **sequential** only because the encounter date parameterizes the
  diff; the three lists still fan out among themselves.
- **Orchestration:** one turn — fetch → compute the delta (new/started/stopped/resolved
  since the prior-encounter date) → the model *filters* the delta to what's clinically
  salient → gate → stream. The diff arithmetic is deterministic; the salience judgment is
  the model's.
- **Verification gate catches:** each reported change maps to an actual state transition in
  the data (a med with a stop-date after the prior encounter, a problem with a later
  onset); no "change" is asserted that the two snapshots don't support.
- **Failure mode (only one encounter):** "no prior visit to compare against" — the gate
  blocks any fabricated delta when there is no second reference point.
- **Single-vs-multi signal:** **single.** A parameterized fetch and a filtered diff is one
  agent's work; "diff" is not a second agent.

### UC-3 — Targeted recall / history drill-down (multi-turn)

- **Ask:** *"Why was she started on metformin and then taken off it?"* → *"Did anyone try
  anything after that?"*
- **Data:** `MedicationRequest` (start/stop dates), `Encounter` history — read iteratively
  as the thread narrows.
- **Tools + call pattern:** **iterative, multi-turn.** The first ask may fetch meds +
  encounters; the follow-up reuses conversation context and issues *additional* targeted
  reads (e.g. encounters in the window after the metformin stop) without re-fetching
  everything. This is the canonical tool-loop-across-turns case.
- **Orchestration:** multi-turn — the agent maintains conversation state (message history)
  so the follow-up resolves against the first answer. Each turn: reason about what's still
  missing → fetch it → answer → gate. Follow-ups typically need *fewer* reads than the
  opener because context is warm.
- **Verification gate catches:** the reconstructed thread cites the specific encounters/med
  rows it's built from; where the *rationale* isn't in the chart, the answer says so
  instead of inventing one.
- **Failure mode (record shows *that* but not *why*):** state what's documented ("stopped
  2023-04; the chart doesn't record the reason") — the gate rejects an invented cause.
- **Single-vs-multi signal:** **single, and this is the case people mistake for
  multi-agent.** Multi-turn context + iterative tool calls is exactly what one
  conversational agent with a tool loop provides. There is no second *role* here — the same
  agent, same instructions, more turns.

### UC-4 — Medication ↔ problem reconciliation

- **Ask:** *"Anything on her med list that doesn't line up with her problems, or clash with
  an allergy?"*
- **Data:** `MedicationRequest` (RxNorm + text), `Condition` (SNOMED), `AllergyIntolerance`.
- **Tools + call pattern:** three **parallel** reads, then a single cross-referencing
  reasoning step over all three lists.
- **Orchestration:** one turn — fan-out fetch → the model cross-references (med without a
  matching indication; problem without expected therapy; med vs allergy substance) → gate →
  stream. The dedup and RxNorm/text-fallback are already handled *inside* `get_medications()`,
  so the agent reasons over clean, typed lists — the coding-completeness weakness
  (`deployment-strategy.md`) degrades to name matching but stays visible and citable.
- **Verification gate catches:** the hardest gate in the set. Every flag must (a) name the
  specific med row and problem/allergy row it pairs, and (b) be phrased as a *candidate for
  review*, never an asserted interaction — enforcing the USERS.md guardrail *"no
  possible-interaction stated as definite."*
- **Failure mode (RxNorm-less list meds):** cross-reference falls back to text matching;
  the flag says which basis it used so the physician can weight it.
- **Single-vs-multi signal:** **this is the UC `agent-tech-stack.md` flagged as maybe
  graph-shaped — and it is still single.** Cross-referencing three already-fetched lists is
  one reasoning step with one output contract (a list of candidate mismatches). It would
  become multi-agent only if each flag needed its own independent adjudication sub-agent
  with a different role — which the USERS.md scope explicitly rejects (flags are surfaced
  for the physician, not adjudicated by the system). See the tripwires in Section 6.

### UC-5 — Cross-cover cold-start (the authorization case)

- **Ask:** *"I'm covering — who is this patient and what do I need to know right now?"*
- **Data:** same clinical resources as UC-1/UC-3, **plus the authorization decision
  itself.**
- **Tools + call pattern:** identical synthesis to UC-1 (fan-out or snapshot) — *once
  execution reaches the agent at all.*
- **Orchestration — the distinction is a gate that sits before the agent, not inside it:**
  1. The physician opens the copilot on a patient outside their panel.
  2. **The module (PHP), not the agent, checks authorization** at launch: care-team
     membership (`care_teams`/`care_team_member`) or break-the-glass group membership
     (`BreakglassChecker`). This is `deployment-strategy.md`'s "enforcement stays at the
     OpenEMR boundary."
  3. **Pass** → the module mints the patient-scoped token and the agent runs UC-1-style
     synthesis. **Fail** → refuse and write an audit entry; the agent is never invoked and
     receives no token.
- **Verification gate catches:** same source-attribution as UC-1. Authorization is *not*
  the gate's job — it's already decided upstream; by the time the agent has a token, scope
  is guaranteed by construction (the token binds one patient).
- **Failure mode (no relationship, no break-the-glass):** refuse at the module, log the
  attempt. The agent tier never sees the request.
- **Single-vs-multi signal:** **single — and notably, authorization does *not* argue for a
  second agent.** It's a policy gate at the boundary (a natural fit for
  code/`BreakglassChecker`, not an LLM), so it adds zero agents. A tempting-but-wrong design
  would make "authorization" a supervisor agent; that would move a security decision into a
  non-deterministic LLM, which the audit posture forbids.

---

## Cross-cutting orchestration mechanics

The five UCs share one machine. Described once:

- **Turn lifecycle.** Request enters the FastAPI service → a **correlation ID** is minted
  (PRD engineering req) and rides every log line, tool call, and LLM call → Pydantic AI
  runs the **tool loop** (fetch what's needed, possibly across several tool calls) →
  the response passes the **`output_validator` gate** → streams back over SSE so first
  token lands inside the walk-between-rooms budget.
- **The verification gate (one seam for all five UCs).** `@agent.output_validator`
  (`agent-tech-stack.md`, Decision 1) runs *before* any response reaches the physician. It
  enforces the two USERS.md guardrails that recur in every UC above: **source attribution**
  (every claim cites a fetched row; unattributable → `ModelRetry` forces a correction) and
  **domain constraints** (UC-4 flags phrased as candidates, never asserted facts; nothing
  answered from data we don't have). A cheap Haiku 4.5 pre-check may run *inside* this
  validator — that is a model-routing optimization within one agent, **not** a second
  agent.
- **Multi-turn context.** Conversation state (message history) persists across turns so
  UC-3 follow-ups resolve against prior answers and warm tool results. One agent, more
  turns — no handoff.
- **Authorization boundary.** Enforced once, upstream, in PHP (UC-5). The agent tier is
  authorization-*naïve by construction*: it only ever holds a token already scoped to one
  authorized patient. This keeps a security-critical decision deterministic and auditable
  instead of delegating it to an LLM.
- **Failure handling.** A failed FHIR read surfaces as a typed error the agent reports
  ("couldn't retrieve medications") rather than fabricating around — the PRD's graceful-
  degradation requirement, and the reason every tool returns typed results the agent can
  detect the absence in.

---

## Verdict — single agent

Aggregating the per-UC signals:

| UC | Call pattern | Needs a second *role*? |
|---|---|---|
| UC-1 | parallel fetch → synthesize | No |
| UC-2 | parameterized fetch → filtered diff | No |
| UC-3 | iterative, multi-turn tool loop | No |
| UC-4 | parallel fetch → cross-reference | No |
| UC-5 | policy gate (upstream, non-LLM) → UC-1 synthesis | No |

**Every use case is one conversational agent over the five-tool set, with a verification
validator and multi-turn context.** No use case requires a second LLM-driven agent with a
distinct role handing off control. The things that *look* like extra agents are all
single-agent mechanics: parallel fetches (UC-1/UC-4), a deterministic diff (UC-2),
multi-turn iteration (UC-3), an upstream policy gate (UC-5), and a Haiku validation
pre-check (all). This confirms `agent-tech-stack.md`'s framework pick: **Pydantic AI, one
agent, `output_validator` as the gate** — its verify-then-answer shape is exactly what the
five UCs need, and none of them justify LangGraph's multi-node/handoff machinery.

**On UC-4 specifically** (the one flagged as possibly graph-shaped): it is a single
cross-referencing step producing one output contract — a list of candidate mismatches for
physician review. It is not multi-agent because the design deliberately *does not*
adjudicate the flags (USERS.md guardrail); it surfaces them. Nothing hands off.

### Tool vs. sub-agent — the orchestrator lens

The verdict above holds under the sharper framing too: *would any capability be better as a
context-isolated sub-agent under an orchestrator than as a tool/step under one agent?* Walking
it:

- **The five FHIR reads are deterministic tools** — I/O, no LLM, no context of their own.
  Nothing to isolate.
- **The reasoning capabilities** (orientation UC-1, diff-salience UC-2, thread reconstruction
  UC-3, cross-reference UC-4) run over data *already fetched* and share the conversation's
  context. None reads more than the orchestrator needs to keep, **because FHIR returns bounded,
  structured data** — problem/med/allergy lists are short coded lists. No capability clears the
  bar for its own context window, so a sub-agent would add a hop and a failure surface to
  isolate a context that isn't large.

The one genuine pressure point is `Encounter` history (USERS.md: "40 encounters deep,"
free-text notes). Dumping every note into the main context is where a retrieval/summarization
sub-agent would first pay off. We contain that by **tool design, not topology**:
`get_encounters()` returns bounded metadata and supports last-N / date-windowed / targeted
reads, so the agent pulls only the notes a thread needs — solving the context-volume problem a
sub-agent would otherwise solve, without the extra hop against the <15s budget. This is a
deliberate bet, and its failure condition is the first tripwire below.

### Tripwires — what would flip this to multi-agent

Recorded so the defense can show the verdict is conditional, not dogmatic. Revisit an
orchestrator + sub-agent split (or LangGraph) **only if** one of these becomes true:

1. **Encounter free-text retrieval at scale (the context-isolation tripwire — most likely to
   land).** If a UC needs to reason over the *full text* of a deep chart ("has she ever
   mentioned chest pain in any note?", "summarize all 40 encounters"), a retrieval/
   summarization **sub-agent** that reads the bulk in its own context and returns only
   citations becomes the right call — the classic orchestrator → sub-agent split. Today's
   tools return bounded structured metadata, so the main agent never holds the whole chart and
   this stays inline. It flips the moment a UC genuinely needs whole-chart free-text reasoning.
2. **UC-4 grows an adjudication step** — if the product later needs the *system* to
   resolve each flag (confirm/dismiss with independent reasoning per flag) rather than
   surface it, that's a fan-out of role-distinct adjudicators → multi-agent.
3. **A UC needs branching durable state with human-in-the-loop mid-flow** — e.g.
   `gather → conflict-resolve → escalate to a human → resume`. The current shape is
   verify-then-answer with no mid-flow human step.
4. **Conflicting-source reconciliation becomes its own reasoning stage** — if
   "synthesizing conflicting records" (PRD, Speed vs Completeness) needs a dedicated
   reconciliation agent distinct from the summarizer, rather than one agent reasoning over
   both.

None of these are in `USERS.md` today. If one lands, the reversible-by-config posture of
the stack (`agent-tech-stack.md`) means swapping the framework is a contained change — but
we do not pay for that machinery now.

### Not multi-agent (say it plainly for the interview)

- Model routing (Haiku pre-check, Opus for hard cases) — one agent, different models per
  *call*.
- The verification validator — an inline **critic step**, not an orchestrator delegating to
  independent workers. (A generator+critic *can* be framed as two agents; ours shares one
  context and one loop and nothing hands off, so we call it a step — defensible either way,
  and worth stating honestly rather than over-claiming.)
- Authorization at the module — a policy gate in code, not an agent.
- Parallel tool fetches — concurrency, not delegation.

---

## Feedback into `agent-tech-stack.md`

This doc closes that file's **open question #1**. Proposed edit (not yet applied — apply on
confirmation):

> **1. Single-agent assumption** — ~~confirmed against `USERS.md`? If any UC needs real
> delegation, revisit LangGraph (1-day spike on the UC-4 med/problem flow).~~
> **Confirmed** against all five use cases in `context/decisions/agent-workflow.md` — every UC is one
> conversational agent over the five-tool set with a verification validator; no capability
> needs role-delegation *or a context-isolated sub-agent*. Under the orchestrator → sub-agent
> lens, our capabilities are deterministic tools (FHIR reads) or inline reasoning over bounded
> structured data — nothing large enough to isolate. UC-4 cross-references three lists in a
> single reasoning step, not a graph. The nearest tripwire is whole-chart free-text encounter
> retrieval, contained today by tool design; tripwires that would reopen this are recorded in
> `agent-workflow.md` §6.

The LangGraph/ADK framework spike (open question #2) still stands on its own merits — it
validates *how cleanly Pydantic AI expresses the gate*, independent of the single-vs-multi
question this doc settles.

---

## Traceability

| Orchestration decision | Traces to | PRD requirement |
|---|---|---|
| Five FHIR read tools + optional snapshot | UC-1–UC-5 | Tool design; every capability maps to a UC |
| Parallel fetch (UC-1, UC-4) | UC-1, UC-4 | Speed vs Completeness (<15s budget) |
| Deterministic diff, model-filtered salience | UC-2 | "What changed is a judgment, not a lookup" |
| Multi-turn tool loop + conversation state | UC-3 (follow-ups across all) | Agentic Chatbot (multi-turn required) |
| Med dedup + RxNorm/text fallback in tool | UC-4 | Data-quality failure mode (AUDIT.md) |
| `output_validator` gate (source attribution + constraints) | Every UC + guardrails | Verification System |
| Correlation ID across the turn | Every UC | Observability / correlation IDs |
| Authorization gate upstream in PHP (non-LLM) | UC-5 | Authorization & Access Control; closes IDOR |
| **Single-agent architecture** | Aggregate of UC-1–UC-5 | Agent surface area determined by user needs |

If an orchestration choice in `../ARCHITECTURE.md` does not map to a row above, it is out
of scope until a use case here justifies it.
