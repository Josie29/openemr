# Sergio Angulo — Co-Pilot demo script

The showcase patient is **Sergio Angulo, pid 23** in both prod and local (local is a data clone of
prod — see `context/decisions/synthetic-data-generation.md` and `clone-prod-to-local.sh`). This doc
is the demo runbook: what the agent does, what to ask so it looks good, and how to rebuild/reset the
demo data.

## What the agent does today

The Week-2 agent is a supervisor graph over three routes; the sidebar renders the results.

| Capability | Backed by | Answers / shows |
|---|---|---|
| **Record read** — `get_patient_summary` | `Patient`/`Condition`/`MedicationRequest`/`AllergyIntolerance`/`Encounter` in one call | Who is this, problems, meds, allergies, recent visits |
| **Lab read** — `get_lab_observations` (JOS-82) | `Observation` (laboratory), LOINC-filterable, oldest-first | "What is his hemoglobin / how has it changed over time" |
| **Note read** — `get_encounter_note` | `DocumentReference` (clinical-note) | The free-text narrative for one visit |
| **Document extraction** — attach + OCR (JOS-54/80/87) | Mistral OCR → strict schema, LOINC-coded | Reads an uploaded lab PDF / intake form |
| **Guideline retrieval** — `search_guidelines` (JOS-53) | Hybrid RAG over the in-repo corpus | Evidence-backed guidance for the patient's problems |
| **Write-back** — persist derived facts (JOS-81) | `procedure_result` (`preliminary`) → reads back as FHIR `Observation` | Extracted labs land in the chart, flagged not-yet-confirmed |

The sidebar renders grounded evidence cards + click-to-source, a write-back confirmation card, and —
new — a **lab-trend chart** (JOS-83): one static line per LOINC, `final` results in record-green and
`preliminary` (agent-derived) ones in document-amber.

## The Week-2 showcase: upload → extract → write-back → chart

This is the headline beat and the full multimodal loop on one screen:

1. **Upload** Sergio's lab PDF (`agent/tests/fixtures/documents/pdfs/sergio-angulo-lab-report.pdf`).
2. **Ask a lab question** so a worker OCR-extracts it — e.g. *"What does his uploaded lab report show?"*
   The agent extracts LOINC-coded results (Hemoglobin `718-7` = 15.1 g/dL, Creatinine `2160-0` = 1.44,
   the full CMP+CBC), each cited with a click-to-source bounding box.
3. **Write-back** fires automatically: the extracted facts persist as `preliminary` `procedure_result`
   rows and a confirmation card appears.
4. **Ask the trend** — *"How has hemoglobin changed over time?"* (also a one-click starter chip). The
   chart plots Sergio's historical hemoglobin (green, `final`) **plus the just-written 15.1 point in
   amber** (`preliminary`, not yet clinician-confirmed). The summary flags the unconfirmed reading.

That amber point is the derived write-back showing up as a real, readable Observation — the whole
extract→persist→read loop, visible.

### Rehearse → reset → run (how to make the amber point *appear* live)

Write-back is idempotent on `(documents.id, LOINC)`, and each **new upload** of the PDF gets a fresh
`documents.id` — so a fresh upload always writes. To make the demo show the point *appear* rather than
finding a leftover from rehearsal:

1. **Rehearse** on the target env (after promotion + `sync-copilot-scopes.sh --prod`): run steps 1–4,
   confirm the amber point renders.
2. **Reset** the rehearsal's writes:
   ```sh
   # dry run first — lists exactly what would be removed (never touches real 'final' results):
   interface/modules/custom_modules/oe-module-ai-copilot/scripts/reset-derived-facts.sh --pid 23 --prod
   # then apply:
   interface/modules/custom_modules/oe-module-ai-copilot/scripts/reset-derived-facts.sh --pid 23 --prod --confirm
   ```
   Delete/re-upload the document too, so the next upload gets a fresh `documents.id`. Sergio is back to
   history-only (no amber point).
3. **Run** the demo live: upload again → extract → write → the amber point appears on the chart in
   front of the audience.

`reset-derived-facts.sh` removes only rows carrying the derived signature (`result_status='preliminary'`
+ `document_id`, synthesized order) — Sergio's real historical labs are never touched. Drop `--prod` to
operate on a local stack.

## Sergio's seeded note timeline

Three progress notes seeded by
`interface/modules/custom_modules/oe-module-ai-copilot/scripts/seed_demo_clinical_notes.php`, each
anchored to a **real** encounter with every claim tied to a real problem/med/allergy:

| Encounter date | Visit | Anchors the question… |
|---|---|---|
| **2022-01-22** | Asthma follow-up (2 days after a hospital admission for asthma) | "Why is he on prednisone?" / "Ever hospitalized for asthma?" |
| **2026-01-06** | ER visit — concussion | "What happened at his January ER visit?" + NSAID-safety hook |
| **2026-06-03** | General exam — current state | Comprehensive summary; records the concussion as resolved |

Backing coded data the agent also reads (all real in his record):
- **Asthma** active (since 2025-05-28); **budesonide** controller + **albuterol/Ventolin** rescue; **prednisone 5 mg**; hospital admission for asthma 2022-01-20.
- **Environmental allergies + anaphylaxis risk**: mold, dust mite, animal dander, grass/tree pollen; food: **fish, peanut**; **fexofenadine** + **epinephrine auto-injector**.
- **Aspirin allergy** — yet prescribed **ibuprofen** and **naproxen** (other NSAIDs).
- **Concussion** 2026-01-06 (ER), resolved 2026-02-22.
- **Lab history**: real CBC/chem draws (e.g. Hemoglobin 718-7 across 2021→2026) that the trend chart plots.

## Demo Q&A — questions that make the agent look good

1. **"Give me a summary of Sergio Angulo."** → demographics + problems + meds + allergies in one turn.
2. **"How has hemoglobin changed over time?"** *(lab-trend chart)* → the line chart; after an upload it
   also carries the amber preliminary point. Starter chip, one click.
3. **"What does his uploaded lab report show?"** *(vision extraction + write-back)* → OCR'd LOINC-coded
   results with click-to-source; the write-back card confirms they persisted.
4. **"Why is he on prednisone?"** *(UC-3)* → the 2022-01-22 note: a steroid course during an asthma
   exacerbation that required admission; cross-checked against the med list.
5. **"Has he ever been hospitalized for his asthma?"** → 2022 admission; encounters → follow-up note.
6. **"What happened at his ER visit in January?"** → the 2026-01-06 concussion note.
7. **"Is his concussion still an active problem?"** → resolved (problem-list end-date + 2026-06-03 note).
8. **"Are there any medication-safety concerns?"** *(the "smart" moment)* → cross-references the **aspirin
   allergy** + **ibuprofen/naproxen** + **asthma** and flags NSAID caution — decision support from the record.
9. **"What do guidelines recommend for his active problems?"** *(hybrid RAG)* → retrieved, cited guidance.

### Still out of scope today
- "What did his spirometry / head CT show?" — imaging/PFT live in a **DiagnosticReport** the agent does
  not read (deliberate future work, JOS-37). Labs are now in scope; imaging is not. Use only as a
  "here's what's next" beat.

## Rebuilding / resetting the demo data

From the primary checkout, dev-easy stack up and `railway` authed:

```sh
# 1. Clone prod's patient/clinical data down to local (preserves local login + SMART client):
interface/modules/custom_modules/oe-module-ai-copilot/scripts/clone-prod-to-local.sh

# 2. Seed Sergio's 3 clinical notes (idempotent; run as the web user, not root):
openemr-cmd e "su -s /bin/sh apache -c 'php /var/www/localhost/htdocs/openemr/interface/modules/custom_modules/oe-module-ai-copilot/scripts/seed_demo_clinical_notes.php'"

# 3. Clear any derived write-backs from a prior rehearsal (dry run first):
interface/modules/custom_modules/oe-module-ai-copilot/scripts/reset-derived-facts.sh --pid 23
interface/modules/custom_modules/oe-module-ai-copilot/scripts/reset-derived-facts.sh --pid 23 --confirm
```

Expected after seeding: `pid 23: FHIR DocumentReference?category=clinical-note -> count=3 OK`.

> Prod's `form_clinical_notes` is currently empty, so the notes live **local-only** until the seeder
> reaches `main` and is run in the prod container (per the branching workflow in `CLAUDE.md`). The lab
> history and the upload→extract→write→chart flow work on prod once JOS-82/83/81/87 are promoted **and**
> `sync-copilot-scopes.sh --prod` has run (the token needs `patient/Observation.read`).
