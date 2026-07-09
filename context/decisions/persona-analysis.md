# Persona Analysis — Stage 4 Decision Evidence

**Purpose:** Working analysis behind `../USERS.md` (PRD Stage 4). Compares candidate
target users for the Clinical Co-Pilot and records *why* one was chosen and the rest
rejected. This is decision evidence for the Tuesday architecture defense — not a
deliverable itself. `USERS.md` is the source of truth; this doc shows the road not taken.

**Grounding:** Every persona is scored against what our **actual seed data supports**
(per `../AUDIT.md`). Defensibility hinges on whether the agent's claims can trace to
real records. Our OpenEMR dev DB (Synthea synthetic seed): dense SNOMED-coded problem
lists, RxNorm-coded medications, allergies, and ~1,186 encounters across 25 patients
spanning decades — but **hollow vitals** (25/25 NULL bps/weight), **no populated labs**,
zero contact data, and empty ICD-10 terminology tables. A persona whose use cases need
data we don't have is indefensible regardless of how compelling the story.

---

## The lens: what makes a persona defensible to a hospital CTO

A CTO greenlights an AI copilot when six things line up. Every persona is scored on these:

| Dimension | The question it answers |
|---|---|
| **Data backing** | Can the agent's answers trace to records that actually exist in OpenEMR? |
| **Agent-shape fit** | Is a *conversation* genuinely the right surface — or would a dashboard / sorted list / chart do it better and cheaper? |
| **Pain acuity & ROI** | How expensive is this person's time, and how measurable is the saving? |
| **Authorization tractability** | Can we cleanly answer "is this user allowed to see this patient?" — the #1 audit finding (IDOR, role-scoped not patient-scoped)? |
| **Blast radius** | If the agent is wrong, how bad and how recoverable? Lower = easier to ship an MVP behind. |
| **Narrowness** | Is this a real, bounded person with one workflow — not "clinicians need info"? |

---

## The eight personas

### A. Primary care physician — full clinic day *(PRD-referenced)*

| Attribute | Detail |
|---|---|
| **Who** | Family medicine PCP. 20 patients, 15-min slots, owns a continuity panel. |
| **The 30-sec-before moment** | 8:50am, about to open the first chart of a patient last seen 7 months ago. Or: hand on the exam-room door, 90 seconds to recall who this is. |
| **Pain points** | Chart is dense and non-chronological; "what's changed since last visit" requires manually diffing two encounters; med list and problem list drift; pre-charting eats lunch and evenings ("pajama time"). |
| **Workflow** | Before: skims last note + problem list. Needs: a 3-line "who is this, what's open, what changed." After: walks in oriented, adjusts the plan. |
| **Data needed / have it?** | Problem list yes, meds yes, allergies yes, encounter history yes, "what changed" (encounter diff) yes. Vitals trend weak (hollow), labs no. Core use cases fully backed. |
| **Latency tolerance** | Seconds. 90-sec window is the hard ceiling. |
| **Cost of wrong answer** | High if acted on, but physician is expert-in-loop and catches most errors — recoverable. |
| **Refusals needed** | No dosing/treatment recommendations; no claims not in the record; "I don't have labs" honesty. |
| **Why an agent** | The question varies per patient ("why was she on metformin then off it?") — you can't pre-build a widget for every follow-up. Multi-turn drill-down into an unfamiliar-today history is the shape. |
| **Authorization** | Own panel → cleanest possible mapping onto a per-provider patient scope. |

### B. ED resident — overnight intake *(PRD-referenced)*

| Attribute | Detail |
|---|---|
| **Who** | PGY-2 on overnight, rapid undifferentiated intake. |
| **The 30-sec-before moment** | New arrival, chief complaint "chest pain," never seen this person, needs the dangerous history now. |
| **Pain points** | Zero prior relationship; must surface allergies, anticoagulants, cardiac/renal history under time and cognitive load; high interruption. |
| **Data needed / have it?** | Real-time vitals no (25/25 NULL bps/weight), triage/labs no, active orders no. Prior problem/med/allergy history yes — but the *acute* data the ED workflow lives on is absent. |
| **Latency tolerance** | Sub-second; interrupt-driven. |
| **Cost of wrong answer** | Very high, fast-acting, hard to recover (missed anticoagulant → catastrophic). |
| **Why an agent** | Strong in theory (triage synthesis). |
| **Verdict blocker** | Most ED use cases can't be defended against our record — the data isn't there. **Reject for this MVP.** |

### C. Hospitalist — rounding on 12 admissions *(PRD-referenced)*

| Attribute | Detail |
|---|---|
| **Who** | Hospitalist, 12 inpatients to round on before noon. |
| **Pain points** | Overnight events, active orders, daily progress-note synthesis across a service. |
| **Data needed / have it?** | Admission H&P no, daily progress notes no, inpatient orders no, vitals trends no. Our data is **outpatient** — no inpatient census/order structure exists. |
| **Why an agent** | Reasonable in principle. |
| **Verdict blocker** | Workflow doesn't match the data shape at all. **Reject for this MVP.** |

### D. Clinical pharmacist — medication reconciliation

| Attribute | Detail |
|---|---|
| **Who** | Ambulatory clinical pharmacist doing med rec / polypharmacy review, often at transitions of care. |
| **The 30-sec-before moment** | Opening a poly-pharmacy patient (12+ meds) to find duplications, interactions, and problem-without-med / med-without-problem mismatches. |
| **Pain points** | Med rec is manual, error-prone, and a top patient-safety failure mode (Joint Commission NPSG). Cross-referencing meds ↔ problems ↔ allergies by hand is slow. |
| **Data needed / have it?** | Meds yes (183 rows, RxNorm), allergies yes, problem list yes. This is *exactly* the data we have best. Caveat: meds are **prefix-less RxNorm** (audit finding) — the agent must know the coding scheme, a solvable tool-design detail. |
| **Latency tolerance** | Seconds-to-a-minute; less interrupt-driven than a physician. |
| **Cost of wrong answer** | High (drug safety) but pharmacist is the domain expert double-checking — recoverable, and this *is* the safety net. |
| **Why an agent** | "Which of these could interact, and is anything prescribed without a matching indication?" is open-ended reasoning across three lists — not a fixed report. Follow-ups ("what about renal dosing given her CKD?") demand multi-turn. |
| **Authorization** | Pharmacist role → cleanly modeled. |

### E. Referral specialist / consultant (e.g. cardiology)

| Attribute | Detail |
|---|---|
| **Who** | Cardiologist seeing a referral — a **stranger's** 20-year record — for the first time. |
| **The 30-sec-before moment** | Patient referred for "AFib eval." Needs the cardiac-relevant thread pulled out of two decades of unrelated encounters. |
| **Pain points** | Signal-in-noise: the relevant 5% of a long chart is buried; building a pre-consult mental model is pure manual archaeology. |
| **Data needed / have it?** | Longitudinal problems yes, meds yes, encounter history yes — decades of it (encounters span 1945–2026). Ideal for "synthesize the relevant thread." Labs/imaging no (a gap, but the history thread is the core value). |
| **Latency tolerance** | Seconds; pre-visit prep, slightly more forgiving than between-rooms. |
| **Cost of wrong answer** | Moderate — informs a consult, expert reviews everything. |
| **Why an agent** | **Sharpest "why an agent" wedge in the set.** Synthesizing an unfamiliar long record around a *specific clinical question* is the canonical LLM strength; a dashboard can't know that "AFib eval" is today's lens. Multi-turn is natural ("any prior rate-control tried?"). |
| **Authorization** | Consult relationship → interesting scope case (not the PCP's own panel → motivates real access modeling). |

### F. Cross-cover / covering physician (locum or weekend cover)

| Attribute | Detail |
|---|---|
| **Who** | Physician covering **another doctor's** panel — no prior relationship with any of them. |
| **The 30-sec-before moment** | A page about a patient they've never met; must get oriented from zero in under a minute. |
| **Pain points** | Every patient is a cold-start; the owning physician's context is inaccessible; highest "unfamiliar patient" pressure of any persona. |
| **Data needed / have it?** | Same as PCP (problems/meds/allergies/history) — fully backed. |
| **Latency tolerance** | Seconds; often urgent. |
| **Cost of wrong answer** | High but expert-in-loop. |
| **Why an agent** | Same synthesis strength as the consultant, under more time pressure. |
| **Authorization** | **Best possible showcase for the audit's #1 finding** — covering a panel you don't own is *exactly* the break-the-glass / cross-panel-access problem. Turns the IDOR gap into a designed feature. |

### G. Care coordinator / chronic-care panel manager (RN)

| Attribute | Detail |
|---|---|
| **Who** | Population-health RN managing a chronic-disease panel (diabetes, CHF), closing care gaps. |
| **Pain points** | Which of my 200 patients are overdue / off-target / missing a med? |
| **Data needed / have it?** | Problems yes, meds yes — but the work is **panel-level aggregation**, and labs/A1c (the actual gap signal) are no. |
| **Why an agent** | **Weak.** "Show me everyone overdue for X" is a sorted list / dashboard by definition — the PRD explicitly warns this is the anti-pattern. Fails the agent-shape test. |
| **Verdict** | Poor fit for a *conversational* agent; also data-light. **Reject.** |

### H. Medical assistant — pre-visit chart prep / rooming

| Attribute | Detail |
|---|---|
| **Who** | MA rooming the patient and prepping the chart 60 seconds before the physician walks in. |
| **Pain points** | Assembles the "prep summary" the doctor relies on; narrow clinical authority. |
| **Data needed / have it?** | Problems/meds/allergies yes. |
| **Why an agent** | Real workflow, but it's largely a **templated summary** (a report), and the MA's scope-of-practice limits what an agent should surface to them. |
| **Verdict** | Viable but weaker ROI (MA time < physician time) and narrower authorization story. **Fold into A** as a shared feature rather than a standalone persona. |

---

## Cross-persona defensibility scoring

Scored High / Med / Low against the CTO lens. **Bold** = the differentiator that decides each row.

| Persona | Data backing | Agent-shape fit | Pain / ROI | Authz tractability | Blast radius (lower=better) | CTO verdict |
|---|---|---|---|---|---|---|
| **A. Primary care physician** | **High** | High | **High** (MD time) | **High** (own panel) | Med | **Strongest overall** |
| E. Referral specialist | High | **High** (sharp wedge) | High | Med (consult scope) | Low | **Strong** |
| F. Cross-cover physician | High | High | High | **High** (showcases IDOR fix) | Med | **Strong** |
| D. Clinical pharmacist | **High** (best data match) | High | Med | High | Low–Med | Strong (safety angle) |
| H. Medical assistant | High | **Low** (mostly a report) | Low–Med | Med | Low | Fold into A |
| G. Care coordinator | Med | **Low** (dashboard) | Med | High | Low | Reject (anti-pattern) |
| B. ED resident | **Low** (no acute data) | High | High | Med | **High** | Reject (data) |
| C. Hospitalist | **Low** (no inpatient data) | Med | High | Med | High | Reject (data) |

---

## Recommendation

**Anchor `USERS.md` on the Primary Care Physician (A)** — the only persona scoring High on
data backing, ROI, *and* authorization while matching the PRD's scenario verbatim. That
combination is what survives CTO scrutiny.

The PCP alone, though, doesn't maximally exercise the two things interviewers probe hardest
— the **authorization boundary** and the **"why an agent not a dashboard"** defense. So
sharpen it:

1. **Primary persona: the PCP on a continuity-panel clinic day.** Core use cases =
   pre-visit chart prep + between-rooms recall + "what's changed since last visit." All
   fully data-backed; clean own-panel authorization.
2. **Fold in the cross-cover moment (F) as the PCP's hardest day** — covering a colleague's
   panel. Not a second persona; the same physician in the scenario that turns the audit's #1
   finding (IDOR / no patient-level scoping) into a *designed* access-control feature. Best
   interview material for "where are the trust boundaries."
3. **Borrow the consultant's synthesis wedge (E)** as the sharpest justification for the
   conversational shape: "surface the cardiac thread from 20 years of chart" can't be a widget.

Result: one narrow, defensible user whose workflow (a) is fully backed by real data,
(b) forces a real authorization design, and (c) makes the agent — not a dashboard — obviously
the right tool.

**Secondary option on file:** the clinical pharmacist (D) — if we'd rather lead with a
patient-safety story (med reconciliation) than a physician-time story. Tightest data match
of any persona. Documented here so the choice is a decision, not an omission.
