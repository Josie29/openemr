# Target User & Use Cases

**Deliverable:** PRD Stage 4. This document is the source of truth `ARCHITECTURE.md`
traces back to — every agent capability built in Stage 5 must point to a use case here
(referenced by ID). The persona was chosen from a documented comparison of eight
candidates in [`context/persona-analysis.md`](context/persona-analysis.md).

---

## The one-sentence thesis

**A primary care physician needs to walk into each room already knowing who this patient
is, what's open, and what changed — in the seconds before the door opens — and the
questions that get them there are different for every patient and can't be pre-built into
a fixed screen.** That last clause is why the answer is a conversational agent, not
another dashboard.

---

## The user

| | |
|---|---|
| **Who** | A family medicine primary care physician. |
| **Panel** | ~1,800 patients in a continuity panel; sees the same people over years. |
| **A day** | 20 patients, 15-minute slots, 8:00am–5:00pm. Charts between rooms and after clinic. |
| **Expertise** | Full clinical authority; the expert-in-the-loop. The agent informs judgment, never replaces it. |
| **What they will not tolerate** | A confident wrong answer; a claim they can't verify against the chart in one click; latency that outlasts the walk between two rooms; being told to trust data the system doesn't actually have. |
| **Access model** | Owns their panel. Some days they **cross-cover** a colleague's panel — patients they've never met and don't normally have a treatment relationship with (see UC-5). |

### The 30 seconds before they open the agent

It's 8:52am. The 9:00 patient was last seen seven months ago for something the physician no
longer remembers. The chart is 40 encounters deep, non-chronological, and the one thing
that matters today — a medication a specialist changed in the interim — is buried on
screen four. The physician has maybe 90 seconds before walking in, and the patient will read
hesitation as "my doctor doesn't remember me."

### What they do with the output

They walk in oriented, open with the right question, and adjust the plan. The agent's
output is a **launchpad for their own reasoning**, not a decision. Its job is to collapse
"five minutes of chart archaeology" into "fifteen seconds of reading, then a follow-up
question."

---

## Why a conversational agent (the core justification)

The physician's information need is **unpredictable and patient-specific, and it moves
turn by turn.** For this patient it's "why did her metformin stop?"; for the next it's
"has he ever tolerated a statin?"; for the next, "what did cardiology actually say?" You
cannot pre-render a widget for every question across 1,800 patients — the space is
open-ended. A dashboard answers the questions its designer anticipated; a conversation
answers the one the physician actually has, right now, then the follow-up it provokes.

That is the irreducible reason for this shape. Each use case below is an instance of it,
and each states its *specific* why-an-agent rather than repeating this one.

---

## Use cases

Each use case is data-backed by our actual OpenEMR record (dense SNOMED problem lists,
RxNorm medications, allergies, and decades of encounters — per `AUDIT.md`). None depend
on data we don't have (labs, live vitals). Each names the failure mode it must handle.

### UC-1 — Pre-visit orientation ("who is this, what's open, what changed")

- **Trigger:** 8:52am, opening the next chart. A patient last seen months ago.
- **Ask:** *"Give me the picture on my 9am."*
- **Agent does:** Pulls active problems, current meds, allergies, and the last 1–2
  encounters; returns a 3–4 line orientation with the single most relevant change flagged.
- **Data:** `lists` (problems, meds, allergies), `form_encounter` history.
- **Why an agent, not a dashboard:** The opening summary is the *first turn of a
  conversation*, not the destination. The physician's next move is always a question the
  summary provokes ("wait, who changed the metformin?") — and that follow-up is
  unpredictable. A static summary screen ends where the real need begins.
- **Failure mode it must handle:** Sparse record (new patient, one encounter) → say so
  plainly rather than padding.
- **Success:** Oriented in <15 seconds of reading; walks in without re-opening the chart.

### UC-2 — "What's changed since the last visit"

- **Trigger:** Returning patient; the physician needs the delta, not the whole history.
- **Ask:** *"What's different since I last saw her?"*
- **Agent does:** Diffs the current problem/med list against the state at the prior
  encounter — new problems, started/stopped meds, new allergies — and names what's
  clinically worth noticing.
- **Data:** `lists` with `date`/`enddate`/`activity`, `form_encounter` dates.
- **Why an agent:** "What changed" is a *judgment*, not a field lookup — a diff of two
  encounters includes noise, and which change matters depends on the patient. The agent
  filters the delta to what's salient; a changelog widget would dump all of it.
- **Failure mode:** Only one encounter on file → "no prior visit to compare against."
- **Success:** The one meaningful interval change surfaced without the physician reading two
  full notes.

### UC-3 — Targeted recall / history drill-down (multi-turn)

- **Trigger:** In or between rooms; a specific question about this patient's past.
- **Ask:** *"Why was she started on metformin and then taken off it?"* → follow-up:
  *"Did anyone try anything after that?"*
- **Agent does:** Reasons over the longitudinal record to reconstruct the thread, then
  answers follow-ups in the same context.
- **Data:** `lists` (med start/stop dates), `form_encounter` history.
- **Why an agent:** This is the canonical case. The question is open-ended, the answer
  requires synthesizing scattered encounters around *one specific thread*, and the
  follow-up depends on the first answer. Multi-turn context is the whole point — no fixed
  surface can hold "whatever the physician asks next."
- **Failure mode:** The record doesn't explain the *why* (only that it happened) → state
  what's documented and that the rationale isn't in the chart, rather than inventing one.
- **Success:** The thread reconstructed and a follow-up answered without a chart dive.

### UC-4 — Medication ↔ problem reconciliation

- **Trigger:** Poly-pharmacy patient; the physician wants a sanity check before prescribing.
- **Ask:** *"Anything on her med list that doesn't line up with her problems, or clash
  with an allergy?"*
- **Agent does:** Cross-references meds against active problems (med without a matching
  indication; problem without expected therapy) and against the allergy list; surfaces
  mismatches for the physician to judge.
- **Data:** `lists` medications (RxNorm), problems (SNOMED), allergies.
- **Why an agent:** "Does anything not line up?" is open-ended cross-referencing across
  three lists with clinical judgment about what counts as a mismatch — not a joinable
  query. The physician then interrogates each flag ("she's on it for something off-label,
  ignore that").
- **Failure mode:** Meds are stored as **prefix-less RxNorm integers** (audit finding) —
  the agent must resolve the coding scheme correctly or it will mislabel every med. Must
  never *state a drug interaction as fact* — it surfaces *possible* mismatches for review.
- **Success:** A real mismatch caught, or a clean bill, in one turn — with each flag
  traceable to the specific med/problem rows.

### UC-5 — Cross-cover cold-start (the hard day, and the authorization case)

- **Trigger:** The physician is covering a colleague's panel. A page about a patient they've
  never met; zero prior relationship; must orient from nothing.
- **Ask:** *"I'm covering — who is this patient and what do I need to know right now?"*
- **Agent does:** Same synthesis as UC-1/UC-3, but the patient is outside the physician's own
  panel — so access is **explicitly authorized, scoped, and audited** (break-the-glass),
  not silently granted.
- **Data:** Same clinical tables; **plus** the authorization decision itself.
- **Why an agent:** Highest "unfamiliar patient" pressure of any moment — cold-start
  synthesis under time pressure is exactly where open-ended Q&A beats hunting a chart.
- **Why it's in this doc:** It is the use case that forces the architecture to answer the
  audit's #1 finding — **no patient-level access scoping (IDOR)**. Cross-cover is where
  "any authenticated user can pull any patient" stops being acceptable. The agent must
  enforce *is this user allowed to see this patient*, degrade to break-the-glass when the
  relationship is emergency/coverage rather than ownership, and write an audit trail.
- **Failure mode:** No treatment relationship and no break-the-glass justification →
  **refuse**, and log the attempt.
- **Success:** Legitimate coverage oriented in seconds; illegitimate access refused and
  audited.

---

## What the agent must refuse (non-goals & guardrails)

These are as load-bearing as the use cases — they define the trust boundary.

- **No treatment or dosing recommendations.** It surfaces what the record says; it does
  not tell a physician what to prescribe. (Out of scope for this user's stated need.)
- **No claim that can't be traced to a record.** Every statement cites the source row;
  an unattributable claim is not stated as fact. (PRD verification requirement.)
- **No answering from data we don't have.** Asked about labs or a live vital, it says the
  record doesn't contain it — it does not infer or fabricate. (Grounded in `AUDIT.md`
  data-quality gaps.)
- **No cross-patient access without authorization.** Patient scope is enforced; UC-5
  cross-panel access is authorized, scoped, and audited or it is refused.
- **No possible-interaction stated as definite.** UC-4 flags are candidates for physician
  review, never asserted clinical facts.
- **Not patient-facing.** This is a clinician tool inside OpenEMR.

---

## Traceability

| Downstream (Stage 5) | Traces to |
|---|---|
| Any retrieval / summarization capability | UC-1, UC-2, UC-3 |
| Multi-turn conversation & context retention | UC-3 (and follow-ups across all) |
| Tool chaining across problem/med/allergy lists | UC-4 |
| Verification / source-attribution layer | Every UC + "no untraceable claim" guardrail |
| Patient-level authorization & break-the-glass | UC-5 (closes AUDIT.md IDOR finding) |

If a capability proposed in `ARCHITECTURE.md` does not map to a row above, it is out of
scope until a use case here justifies it.
