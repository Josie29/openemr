# Spec — `medication_list` extraction (third document type, JOS-91)

Status: **BUILT** (JOS-91). This is the PRD's optional third document type, added on top of the
`lab_pdf` + `intake_form` core after both shipped. `W2_ARCHITECTURE.md` §3.1/§3.2/§6 is the durable
architectural record; this document is the design contract.
Related: [`intake-form-extraction.md`](intake-form-extraction.md) (JOS-80 — medications used to live
here), [`derived-fact-write-back.md`](derived-fact-write-back.md) (JOS-81 — the write path this reuses).

## 1. Why

PRD-week-2 Core Req 1 lists a **third** document type beyond the lab PDF and intake form. A
patient's medications arrive most often as their own artifact — a pharmacy medication profile, a
discharge medication list, a reconciliation sheet — not buried in a front-desk intake form.

**The decision that shapes everything else: medications move OUT of `intake_form` and become a
`medication_list`.** The two document types are now **mutually exclusive in what they own** —
`intake_form` owns allergies (plus display-only demographics, chief concern, family history);
`medication_list` owns medications. This is honest about where the data actually comes from and keeps
each schema single-purpose. It is *not* additive on the intake side: `IntakeForm.current_medications`
was **removed**, so a form that lists both allergies and medications is no longer the medication
source — a dedicated medication list is.

## 2. Goals

- `attach_and_extract` returns cited medication facts for a `Medication List` document.
- A medication fact persists to OpenEMR **exactly as it did from the intake form** — no PHP change.
- Medication facts serialize under the citation contract as `medication_list`.
- Reuse, not fork: the `Medication` schema, the medication locator, and the write path already exist
  from the intake work; this type wires them to a new category and a new citation arm.

## 3. Non-goals

- **A new write path.** The persist endpoint routes on each fact's `type` (`lab` / `allergy` /
  `medication`), never on document type, so a `medication` fact already has a home
  (`IntakeFactWriter::writeMedication`). Nothing in PHP changes (§5.4).
- **Changing the `Medication` model.** Its fields (name, dose, frequency, citation) describe the drug
  itself and are identical whichever document reported it — so it is shared, not copied.
- **Frontend work beyond a label.** The sidebar already maps `source_type` onto trust tiers; a
  `medication_list` citation lands in the existing **document** tier. Only the human label
  ("Medication list") is new.
- **Medication reconciliation / de-duplication** against the chart's existing medication list. A med
  list the patient brought in is surfaced as document-derived facts; merging it with the chart is out
  of scope.

## 4. What already exists — do not rebuild

| Piece | Where |
|---|---|
| `Medication` model (shared with intake) | `agent/src/copilot/ingestion/schemas.py` |
| `FieldId.CURRENT_MEDICATIONS` + its locator chain | `agent/src/copilot/ingestion/geometry/fields.py` |
| Medication write path (`type='medication'` → `lists`/`lists_medication`) | `interface/.../src/Fact/IntakeFactWriter.php` |
| Category → doc_type resolver | `agent/src/copilot/fhir/models.py` (`resolve_doc_type`) |

## 5. Design

### 5.1 The category seam — a seeded `Medication List` category

Doc type is resolved from the OpenEMR **category**, never the model (`resolve_doc_type`): `Lab
Report` (substring) → `lab_pdf`, `Patient Information` (exact) → `intake_form`, `Medication List`
**and** `Medical Record` (exact) → `medication_list`. Like `Patient Information`, medication-list
matches its category **exactly** — a loose match risks reading an unrelated document through the
medication schema.

`Medical Record` is a **demo fallback**: the seeded `Medication List` category does not reliably
surface in OpenEMR's cached Documents tree (a session/iframe cache quirk, not a data problem — the
row is correct and renders in a fresh session), so the demo uploads the med list under the
always-present default `Medical Record` category. **Tradeoff:** any `Medical Record` upload then
extracts as a medication list. Acceptable for the demo; gate behind config or drop before promoting
to prod, where the proper `Medication List` category should be seeded instead.

`Medication List` is **not an OpenEMR default category** — only the first two ship with OpenEMR. It
is seeded per deployment by `scripts/seed-medication-list-category.sh`: an **idempotent** MPTT
(nested-set) append of the node as the last child of the categories root, no-op if it already exists.
Until it is seeded, a med-list PDF uploaded under any other category simply resolves to a different
type (or none) and is not read as a medication list. The bootstrap/setup path runs the seed so a
fresh worktree or a fresh prod deploy has the category.

### 5.2 The schema

```python
class MedicationList(BaseModel):
    medications: list[Medication]     # each Medication carries name/dose/frequency + citation
```

`MedicationList` is the strict-schema contract for `medication_list`, parsed at the ingestion
boundary exactly as `LabReport` / `IntakeForm` are. `medications` is always present but may be empty
— an empty list means **"none read from the document"**, which the answer layer treats as
missing-data, never an affirmative "no medications" (the same rule intake's empty `allergies`
follows). `Medication` is the **shared, unchanged** sub-model, so a med-list medication and an
(historical) intake medication are byte-identical facts.

### 5.3 Geometry — reuse the medication locator

The locator layer binds a chain per FIELD, not per doc type (`W2_ARCHITECTURE.md` §3.5), so the
field that used to place intake medications places med-list medications untouched.
`FieldId.CURRENT_MEDICATIONS` gets a `FieldSpec` under a new `MEDICATION_LIST_SPECS` map (keyed by
`DocType.MEDICATION_LIST` in `_SPECS_BY_DOC_TYPE`), with label aliases for the wording a real med
list uses (`active medication list`, `medication list`, `current medications`, `medications`) and a
`SectionSpanLocator → LineBandLocator` chain.

**Locatability constraint on the fixture.** A drug name must sit on **its own single-line row** so
the section/line locators can band it to a box that meets the precision floor — a value that cannot
be located is dropped rather than boxed wrongly (the verbatim/locatability discipline from §3.5). The
fixture is authored to that shape.

### 5.4 Write-back — the endpoint is document-type-agnostic

A medication fact carries `type='medication'`. The persist endpoint (`persist-facts.php` →
`FactPayloadParser`) dispatches on that `type`, so the fact routes to `IntakeFactWriter::writeMedication`
and writes the `lists` + `lists_medication` rows with `request_intent='proposal'` — **identical to
the intake path, zero PHP changes.** The only thing that changed is *which document produces the
fact*: a `medication_list` instead of an `intake_form`. The `proposal` intent (weaker than the
allergy/lab markers, for the reasons in [`derived-fact-write-back.md`](derived-fact-write-back.md)) is
unchanged and still the honest "agent-derived, not clinician-ordered" signal.

### 5.5 Provenance — a new `MedicationListCitation` arm

`SourceRef.to_citation` routes a document fact to a citation arm by its `doc_type`. A new
`CitationSourceType.MEDICATION_LIST` value and a `MedicationListCitation` arm (in
`agent/src/copilot/schemas.py`) carry medication provenance. It is **structurally identical to
`IntakeFormCitation`** — a boxed document value with no `lab_detail` (that analyte metadata is a
lab-only concern) — but a distinct `source_type` tag so the sidebar can label the provenance
"Medication list". The discriminated citation union is therefore five-valued: `guideline`, `lab_pdf`,
`intake_form`, `medication_list`, `fhir` — collapsing to the three UI trust tiers, with the three
document arms sharing the **document** tier.

## 6. Acceptance

- `attach_and_extract` on a `Medication List` document returns cited medication facts; doc_type comes
  from the seeded category, never the model.
- `intake_form` extraction no longer returns medications; an intake form with a medication section
  does not produce medication facts.
- A medication fact persists to `lists` / `lists_medication` with `request_intent='proposal'` and
  round-trips as `MedicationRequest.intent: proposal` — through the **unchanged** write path.
- Medication facts serialize as `MedicationListCitation` with `source_type: "medication_list"`, and
  the sidebar renders them in the document trust tier labelled "Medication list".
- `seed-medication-list-category.sh` is idempotent: re-running it leaves exactly one `Medication
  List` category and does not corrupt the MPTT ranges.
- Every medication fact carries a box meeting the medication-list precision floor; a drug that cannot
  be located is dropped, not boxed onto unrelated ink.
