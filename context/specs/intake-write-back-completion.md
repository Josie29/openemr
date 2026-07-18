# Spec — complete intake-fact write-back (family history, chief concern, demographics)

**Status:** DRAFT, pre-implementation. Decisions resolved 2026-07-17: chief concern → new encounter
(§5.3); reshaping the intake form + schema to OpenEMR's native collection points is in scope (§5.6).
**Issue:** TBD (extends [JOS-81](https://linear.app/josiemachalek/issue/JOS-81) write-back; JOS-80 intake extraction; JOS-49 data authority).
**Implements:** the still-unwritten intake fact types the extractor already produces, closing the gap in
`W2_ARCHITECTURE.md` §6 ("Intake facts are persisted as appropriate OpenEMR records" — currently true
for allergies only, now that JOS-91 moved medications to the `medication_list` document type).

## Goal

Every intake fact the extractor produces persists into OpenEMR as a **native clinical record**,
written under the physician's session ACL, flagged as agent-derived rather than clinician-confirmed.
No intake fact is left extract-only. Today three are: **family history, chief concern, demographics.**

## What changed since JOS-81 — the "no target" claim was too pessimistic

JOS-81 judged every fact by *"round-trips as a FHIR resource carrying a built-in not-confirmed
marker."* That is the right bar for labs/allergies/meds and the wrong bar for the rest — it declared
family history and chief concern homeless when they have real, callable native targets that simply
don't surface through FHIR. Corrected evidence:

| Fact | Native target | Callable write path | FHIR read-back? | Not-confirmed marker |
|---|---|---|---|---|
| Lab result | `procedure_*` chain | (JOS-81 writer) | ✅ Observation | `result_status='preliminary'` (strong) |
| Allergy | `lists` type=allergy | `AllergyIntoleranceService::insert` | ✅ AllergyIntolerance | `verification='unconfirmed'` (strong) |
| Medication (now a `medication_list` fact — JOS-91) | `lists`+`lists_medication` | `PatientIssuesService::createIssue` | ✅ MedicationRequest | `request_intent='proposal'` (weak) |
| **Family history** | `history_data` (`HIS` layout) | **`SocialHistoryService::updateHistoryDataForPatientPid($pid, $cols)`** | ❌ none | **none — use `created_by` authorship** |
| **Chief concern** | `form_encounter.reason` (longtext) | **`EncounterService::insertEncounter($puuid, $data)`** | ✅ Encounter.reasonCode | note text + authorship |
| **Demographics** | `patient_data` | **`PatientService::update($puuid, $data)`** | ✅ Patient | **none possible — in-place overwrite → HITL gate** |

**Consequence:** the axis is not "writable vs not." It is **auto vs human-gated**, and only two
things push a type to the gate: (a) no marker can be set without a human, and (b) the write is a
destructive in-place overwrite. Only **demographics** hits both. Everything else is auto-writable.

## Design

### 5.1 Replace the triple refusal with a projector registry

Today an unpersistable type is refused in three places — `wire.py` omits it, `FactType.php` has no
case, `FactPayloadParser` throws `DomainException`. Adding a type touches 5 sites. Replace with one
declarative registry keyed by `FactKind`:

```
FactKind        Target service                         Marker                      Mode
LAB_RESULT      LabResultWriter (procedure_*)          result_status=preliminary   AUTO
ALLERGY         IntakeFactWriter::writeAllergy         verification=unconfirmed     AUTO
MEDICATION      IntakeFactWriter::writeMedication      request_intent=proposal      AUTO
FAMILY_HISTORY  SocialHistoryService                   created_by=copilot user      AUTO       ← new
CHIEF_CONCERN   EncounterService::insertEncounter      reason text + authorship     AUTO       ← new (§5.3)
DEMOGRAPHIC     PatientService::update                 (none) → clinician authors   ACCEPT_GATED ← new
```

- `wire.py` emits **all** facts (each has a real destination); the all-or-nothing payload rejection
  and the "omit demographics" special-case both disappear.
- "Unpersistable" stops being a thrown exception — a fact's mode is data, not a control-flow refusal.
- Mirrors the Python-side pluggable locator-chain philosophy already adopted for geometry (JOS-80):
  one place to add a type, per-type quirks (order_code row, `activity=1`, uuid backfill, append-only
  dedup) become registry config, not tribal knowledge.

Endpoint flow: `parse → for each fact, dispatch to its projector; if mode=ACCEPT_GATED require the
accept flag on the payload, else skip the fact (never silently write it)`.

### 5.2 Family history → `SocialHistoryService` (AUTO)

- **Write:** `SocialHistoryService::updateHistoryDataForPatientPid($pid, $cols)` — copies the latest
  `history_data` row forward and inserts a new one (append-only, versioned). Handles uuid/date/created_by.
- **Mapping** — the extractor produces `{condition, relation}` items; `history_data` is one column
  **per relative**. Normalize `relation` → the finite column set:
  `Mother→history_mother, Father→history_father, Sibling/Brother/Sister→history_siblings,
  Spouse→history_spouse, Child/Son/Daughter→history_offspring`. "Mother, Brother" fans out to two
  columns. Append the condition text (don't clobber existing content in that column). **§5.6's
  condition×relative grid makes this mapping direct and removes the ambiguous-relation cases** — prefer
  it over free-form relation parsing.
- **The Relatives tab** (`relatives_cancer/diabetes/…`) is a *separate* 9-condition free-text set —
  set the matching column to the condition text when the extracted condition maps to one; otherwise
  the per-relative columns carry it. Do not treat these as booleans (they're `longtext`, size-20 text).
- **Do NOT populate `dc_*` (diagnosis codes).** Same rule as the allergy-substance decision: never
  fabricate a SNOMED/ICD code from free text. Intake forms don't print codes; leave `dc_*` empty. A
  physician can code it later via the History form's code picker.
- **Marker (no verification column exists):** write the row **authored by a dedicated "AI Co-Pilot"
  `created_by` user**, so a reader can tell it wasn't the physician; the append-only model means a
  physician editing it forward = a new row they authored = confirmation-by-authorship. Reinforce with
  a short "(Co-Pilot, unconfirmed)" annotation in the free-text value.
- **Idempotency:** append-only + no natural dedup key → **guard before insert** (skip if the latest
  row's target column already contains the item), or row-bloat on every re-extraction.

### 5.3 Chief concern → a new encounter (AUTO) — RESOLVED

Create a dedicated **intake-derived encounter** via `EncounterService::insertEncounter($puuid, $data)`
and put the chief concern in `form_encounter.reason` (longtext, reads back as `Encounter.reasonCode`).

- **`reason` — put as much as makes sense, but only grounded text.** The field is longtext, so it
  holds the full chief-concern statement verbatim (not truncated to a keyword), prefixed with a short
  derived marker, e.g. `"[Co-Pilot, from intake form] " + chief_concern`. Do **not** synthesize an HPI
  or fold in unrelated facts — the text must stay covered by the `chief_concern` citation (the box on
  the form). Everything written here must remain click-to-source.
- **Encounter metadata:** date = the intake form's date (or today if absent); a distinct
  `encounter_type` / class marking it intake-derived if the schema allows; authored by the Co-Pilot
  `created_by` user (same authorship-marker approach as family history — encounters have no
  `verification` column either).
- **Idempotency (required):** auto-creating on every extraction would spawn duplicate visits. Guard on
  **one intake-derived encounter per `(document_id, content_hash)`** — check the sidecar / existing
  encounters before inserting; re-extraction updates the existing one rather than creating another.
- Alternative sink considered and rejected for now: `insertSoapNote` (chief concern as a SOAP
  subjective line) — needs an encounter anyway and is heavier; revisit only if reason-on-encounter
  reads poorly in the UI.

### 5.4 Demographics → `PatientService::update` behind a HITL gate (ACCEPT_GATED)

Auto-writing demographics is an **unflagged in-place overwrite** of clinician-entered identity with no
marker and no versioning — the one genuinely unsafe write. The gate makes it honest: a physician who
reviews and clicks Accept has *authored* the change, identical to typing it.

**This is also the right clinical UX, not just safety ceremony** — the agent already surfaces
*"Date of birth discrepancy between chart and intake form requires verification"* (chart DOB
2005-03-16 vs form). So the gate card should render a **diff: chart value vs extracted value**, per
field, and let the physician accept per-field. Never blind-overwrite.

**Gate mechanics (modest — reuses existing auth):**
1. Sidebar renders ACCEPT_GATED facts as a **review card** (chart-vs-extracted diff), not auto-posted.
2. On Accept, the sidebar POSTs those facts to the **same** `persist-facts.php` with `accept: true`.
3. Endpoint: for a gated `FactKind`, run the projector **only** when `accept=true`; the existing
   session bootstrap + CSRF + ACL + pid-from-session already gate it. No new endpoint, no new auth.
4. Demographics writes only the accepted fields via `PatientService::update`; prefer filling empty
   chart fields over overwriting populated ones unless the physician explicitly picks the extracted value.

Phase-2 note: this is the same accept-gate JOS-81 planned for *all* types; here it's required for
demographics and optional-but-available for the rest.

### 5.5 Provenance / sidecar (mostly unchanged)

The `ai_copilot_document_facts` sidecar continues to hold page/bbox citations for click-to-source.
Caveat: `history_data`, `patient_data`, and `form_encounter` have **no `document_id` FK** (unlike
`procedure_result`), so their provenance link lives **only** in the sidecar (keyed on
`(document_id, content_hash, fact_table, field)` + pid). This is the same "provenance visible to the
module + SQL, not through FHIR" limitation JOS-81 already documents — no worse, just extended.

### 5.6 In scope: align the intake form + schema to native collection points

Rather than force awkward mappings from an arbitrary form onto OpenEMR's finite columns, **co-design
the intake fixture, the `IntakeForm` schema, and the write targets** so extracted fields land cleanly.
This is the cleanest way to make "every fact writes" true without lossy normalization.

**Guardrail — stay realistic.** The product story is *"handles real-world intake documents,"* so the
form must not be reverse-engineered into a 1:1 mirror of `patient_data`/`history_data`. The test:
every aligned field must be something a real front-desk intake form plausibly collects. Where it isn't,
keep the form messy and absorb it in the mapper.

Concrete alignments (all realistic):

- **Family history → a condition × relative grid.** OpenEMR's `relatives_*` set (cancer, TB, diabetes,
  high blood pressure, heart problems, stroke, epilepsy, mental illness, suicide) **is** a standard
  intake family-history checklist — aligning the form's family-history section to it is faithful, not
  overfit. The grid maps directly: ticked `(condition, relative)` → `relatives_<condition>` (+ the
  per-relative `history_<relative>` free-text column for the relation detail). A free-text "other
  family history" row absorbs anything off the checklist. This **removes the fuzzy relation
  normalization** (§5.2's "maternal grandmother" risk) at the source.
- **Chief concern / reason for visit.** Real forms have exactly this field — already aligned; no change.
- **Demographics.** Name, DOB, sex, address, phone already match `patient_data`; no change.

Schema/fixture work this implies: adjust the intake HTML fixture + re-record the OCR golden
(`record_ocr_fixture.py`), and align the `IntakeForm` sub-models to the collected shape. Keep JOS-86
(fixture-vs-demo-patient mismatch) in mind — regenerate against pid 23 so the demo is coherent, and
keep the checkbox-grounding hazard (§5.4 of the JOS-80 spec) — a ticked grid cell is `CHECKED_MARK`
evidence, not preprinted text.

## Non-goals

- A FHIR `FamilyMemberHistory` / write route — none exists; family history stays module-native.
- Fabricating diagnosis codes (`dc_*`) or allergy/med codes from free text.
- Re-running OCR idempotency (JOS-70 owns that).
- Changing the auth model — everything rides the existing session-authed endpoint.

## Migration / housekeeping

- **`scripts/reset-patient-facts.php` must cover the new types** — currently deletes derived labs
  (`preliminary`), allergies (`unconfirmed`), meds (`proposal`). Add: `history_data` rows authored by
  the Co-Pilot user; `form_encounter` rows written for chief concern; and demographics — which, being
  an overwrite, **cannot be cleanly reverted** (flag this: reset can't undo an accepted demographic
  edit, another reason it's gated). Delete by explicit id set, never by absence-of-child (the med-wipe
  lesson).
- A dedicated "AI Co-Pilot" service `users` row is a prerequisite for the family-history authorship marker.

## Acceptance

- Extract Sergio's intake form → family history appears on **History → Family History** authored by
  the Co-Pilot user, mapped to the right relative columns, `dc_*` empty.
- Chief concern creates one intake-derived encounter whose `reason` holds the full grounded
  chief-concern text; re-extraction updates it, never spawning a second visit.
- Demographics are **never** written without `accept=true`; the review card shows the chart-vs-form
  DOB diff; accepting writes only the chosen fields.
- Re-running extraction writes **no duplicate** family-history rows (idempotency guard).
- Every written fact still resolves through the sidecar to document + page + bbox.
- `reset-patient-facts.php` cleans every new fact type it can (demographics documented as irreversible).
- `W2_ARCHITECTURE.md` §6 corrected (below).

## Risks

- **Family-history row bloat** — append-only + no dedup key → the idempotency guard is load-bearing.
- **Relation normalization misses** — "maternal grandmother", "twin", etc. don't map to the 5 relative
  columns; decide a fallback (drop to a general column, or skip) rather than mis-filing.
- **Encounter spam** — auto-creating a visit per extraction pollutes the visit list; the
  `(document_id, content_hash)` idempotency guard (§5.3) is load-bearing, same as family history's.
- **Form-alignment overfit** — reshaping the fixture toward OpenEMR's columns can erode the
  "handles messy real forms" story; the §5.6 realism guardrail (every aligned field is plausibly
  collected) is the check. Re-recording goldens also risks regressing the JOS-80 locator/evidence tests.
- **Demographics overwrite is irreversible** — the gate + per-field diff + prefer-empty rule are the
  only guards; the reset script cannot undo it.

## W2_ARCHITECTURE.md §6 corrections required (docs must not drift)

- §6 table row "Intake facts — allergies only (medications moved to `medication_list`, JOS-91) …
  Demographics, chief concern, family history are not persisted": becomes **all intake facts
  persist**, with family history →
  `history_data`, chief concern → `form_encounter.reason`, demographics → `patient_data` behind a
  clinician-accept gate. The "no honest destination exists" framing was the FHIR-round-trip lens and
  is superseded.
