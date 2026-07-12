# Sergio Angulo — Co-Pilot demo script (final submission)

The showcase patient is **Sergio Angulo, pid 23** in both prod and local (local is a
data clone of prod — see `context/decisions/synthetic-data-generation.md` and the
`clone-prod-to-local.sh` script). This doc is the demo runbook: what the agent can do,
what to ask so it looks good, what to avoid, and how to rebuild the demo data.

## What the agent reads today (6 tools)

Defined in `agent/src/copilot/agent.py`, backed by `agent/src/copilot/fhir/client.py`:

| Tool | FHIR resource | Answers |
|---|---|---|
| `get_patient` | `Patient` | Who is this — name, DOB, sex |
| `get_problems` | `Condition` | Active + resolved problem list |
| `get_medications` | `MedicationRequest` | Current meds (deduplicated) |
| `get_allergies` | `AllergyIntolerance` | Allergies |
| `get_encounters` | `Encounter` | Visit list (metadata only, no note body) |
| `get_encounter_note` | `DocumentReference` (`category=clinical-note`) | The **free-text narrative** for one encounter |

The intended chain for a "why / what happened" question is
`get_encounters` → pick the relevant visit → `get_encounter_note(encounter_id)`.

## Clinical note vs DiagnosticReport — the distinction (and our boundary)

This is the debate from the JOS-33 work, resolved deliberately:

- **Clinical note = the reasoning.** A `DocumentReference` (clinical-note) holds the
  clinician's narrative — HPI, assessment, plan, *rationale*. "Why did they do X? What's
  the plan? Is this resolved?" is answered here. **This is wired** (`get_encounter_note`).
- **DiagnosticReport = the evidence.** Labs, imaging, pathology, cardiology reports — the
  *findings of a study*. "What did the echo/biopsy/spirometry show? What were the labs?" is
  answered here. **This is NOT wired** — it is the deliberate future "tool #7", filed as
  **JOS-37** (related to JOS-33), kept out of scope on purpose (own OAuth scope, own context
  cost, prose-faithfulness work).

Mental model for the demo: *the note is the reasoning; the diagnostic report is the proof.*
UC-3 "why was prednisone used" → the **reason** ("asthma exacerbation requiring admission")
lives in the **note**; the **proof** ("spirometry FEV1 …") would live in a DiagnosticReport
we do not read yet.

**Demo consequence:** ask *narrative / why / status* questions (agent shines); avoid
*"what did the test/lab/imaging show"* questions (agent has no DiagnosticReport/Observation
tool and will correctly say it can't see results). If asked, that's the natural segue to
JOS-37 as articulated next work.

## Sergio's seeded note timeline

Three progress notes seeded by
`interface/modules/custom_modules/oe-module-ai-copilot/scripts/seed_demo_clinical_notes.php`,
each anchored to a **real** encounter and every clinical claim tied to a real problem/med/allergy:

| Encounter date | Visit | Note anchors the question… |
|---|---|---|
| **2022-01-22** | Asthma follow-up (2 days after a hospital admission for asthma) | "Why is he on prednisone?" / "Has he ever been hospitalized for his asthma?" |
| **2026-01-06** | ER visit — concussion | "What happened at his January ER visit?" + NSAID-safety hook |
| **2026-06-03** | General exam — current state | Comprehensive summary; records the concussion as resolved |

Backing coded data the agent also reads (all real in his record):
- **Asthma** active (since 2025-05-28); **budesonide** controller + **albuterol/Ventolin** rescue (since 2016); **prednisone 5 mg** (2020-10-09); hospital admission for asthma 2022-01-20.
- **Environmental allergies + anaphylaxis risk**: mold, dust mite, animal dander, grass/tree pollen; food: **fish, peanut**; **fexofenadine** + **epinephrine auto-injector** (since 2005).
- **Aspirin allergy** — yet prescribed **ibuprofen** and **naproxen** (other NSAIDs).
- **Concussion** 2026-01-06 (ER), resolved 2026-02-22.

## Demo Q&A — questions that make the agent look good

1. **"Give me a summary of Sergio Angulo."**
   → demographics + active problems (asthma) + meds + allergies. Shows breadth in one turn.
2. **"Why is he on prednisone?"** *(the UC-3 money question)*
   → reads the 2022-01-22 note: prednisone was a systemic-steroid course during an asthma
   exacerbation that required hospital admission; now reserved for future flares. The "why"
   resolves to prose, cross-checked against the med list.
3. **"Has he ever been hospitalized for his asthma?"**
   → 2022 admission; the agent navigates encounters → the follow-up note. Timeline reasoning.
4. **"Why does he carry an epinephrine auto-injector?"**
   → allergies (anaphylaxis risk, food + environmental) + note. Verifiable "why".
5. **"What happened at his ER visit in January?"**
   → the 2026-01-06 concussion note. Recent, specific narrative.
6. **"Is his concussion still an active problem?"**
   → resolved (problem list end-date + the 2026-06-03 note). Active-vs-resolved discrimination.
7. **"Are there any medication-safety concerns?"** *(the "smart" moment — no new tool needed)*
   → the agent cross-references the **aspirin allergy** + **ibuprofen/naproxen** (NSAIDs) +
   **asthma** and flags NSAID caution. Pure clinical decision support from the existing tools.

### Questions to avoid (out of scope today)
- "What did his spirometry / pulmonary-function test show?"
- "What were his latest labs / kidney function?"
- "What did the head CT show?"
These need **DiagnosticReport/Observation** (JOS-37) — the agent will say it can't see results.
Use only as a deliberate "here's what's next" beat.

## Rebuilding the demo data (reproducible)

From the primary checkout, with the dev-easy stack up and `railway` authed:

```sh
# 1. Clone prod's patient/clinical data down to local (preserves local login + SMART):
interface/modules/custom_modules/oe-module-ai-copilot/scripts/clone-prod-to-local.sh

# 2. Seed Sergio's 3 clinical notes (idempotent; run as the web user, not root):
openemr-cmd e "su -s /bin/sh apache -c 'php /var/www/localhost/htdocs/openemr/interface/modules/custom_modules/oe-module-ai-copilot/scripts/seed_demo_clinical_notes.php'"
```

Expected after seeding: `pid 23: FHIR DocumentReference?category=clinical-note -> count=3 OK`.

> Prod's `form_clinical_notes` is currently empty, so the notes live **local-only** for now.
> To make them appear in the prod demo too, the seeder has to reach `main` and be run in the
> prod container (feature branch → qa/integration → squash to main → `railway ssh`), per the
> branching workflow in `CLAUDE.md`.
