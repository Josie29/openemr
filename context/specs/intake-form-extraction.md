# Spec — `intake_form` extraction + pluggable box geometry (JOS-80)

Status: **BUILT** (JOS-80, branch `feature/intake-form-extraction`), verified end-to-end against a
live OpenEMR + real Mistral OCR on 2026-07-16. This document is the design contract as agreed; the
"As built" section at the end records where the code departed from it and why. `W2_ARCHITECTURE.md`
§3.5/§3.6 is the durable architectural record.
Tracks: [JOS-80](https://linear.app/josiemachalek/issue/JOS-80/complete-intake-form-extraction-the-unshipped-half-of-jos-54).
Related: [JOS-81](https://linear.app/josiemachalek/issue/JOS-81/write-back-of-agent-derived-facts-no-patient-scoped-write-surface) (write-back — **separate track**), JOS-54 (lab half), JOS-55 (schemas).

> **Superseded in part (JOS-91): medications moved out of `intake_form`.** When this spec was
> built, the intake form owned both allergies **and** medications. A later increment added a **third
> document type, `medication_list`**, and moved medication extraction there — see
> [`medication-list-extraction.md`](medication-list-extraction.md). The two document types are now
> **mutually exclusive in what they own**: `intake_form` owns **allergies** (plus display-only
> demographics, chief concern, family history) and **no longer extracts or persists medications**;
> `medication_list` owns medications. `IntakeForm.current_medications` was removed from the schema.
> The medication *locator* (`FieldId.CURRENT_MEDICATIONS`) and the medication *write path*
> (`type='medication'` → `IntakeFactWriter::writeMedication`) were reused unchanged by the new type,
> so the geometry and write-back discussion below still holds for medications — it just now fires for
> a `medication_list`, not an intake form.

## 1. Why

PRD-week-2 Core Req 1 requires `attach_and_extract` to support **both** `lab_pdf` and
`intake_form`; Core Req 2 fixes the required intake fields. JOS-54 scoped both and shipped only
the lab half. `W2_ARCHITECTURE.md` §3.1/§3.2/§6/§12 already describes the intake path as though it
exists — so this is a **code** gap against an already-agreed design.

Building it surfaced a deeper problem: **box geometry is welded to the lab table layout.** Fixing
that is in scope here, because intake cannot be built correctly on top of it.

## 2. Goals

- `attach_and_extract` returns cited intake facts for a `Patient Information` document.
- Box location is **pluggable per field**, not hardcoded to one document shape.
- A box proves the fact it is attached to (see §5.4 — the checkbox hazard).
- Intake facts serialize under the existing citation contract as `intake_form`.
- A lab case and an intake case coexist offline, in one process, under fixture replay.

## 3. Non-goals

- **Write-back / persistence** (JOS-81) — no patient-scoped write surface exists in this fork.
  Facts stay in the per-session `DocumentFactRegistry`, exactly as labs do today. **Being handled
  on a separate track; do not touch it here.**
- **Correcting the `W2_ARCHITECTURE.md` claims that depend on write-back** — false today for the
  lab path too, so they belong to JOS-81, not this branch.
- Frontend work. The sidebar dispatches on the presence of a `bounding_box`
  (`ai-copilot.js:833-840`); `source-view.php` is type-blind. Verified: no change needed.
- A reasoning-VLM swap for free-text fields. `W2_ARCHITECTURE.md` §12 already names Claude vision
  as the fallback **if intake evals regress** — later, behind this same schema boundary.

## 4. What already exists — do not rebuild

| Piece | Where |
|---|---|
| `IntakeForm` + sub-models, fully specified | `agent/src/copilot/ingestion/schemas.py:265+` |
| Intake contract tests, passing | `agent/tests/test_schemas.py:152+` |
| `DocType.INTAKE_FORM`, `SourceType.INTAKE_FORM` | `agent/src/copilot/ingestion/schemas.py:16,28` |
| `IntakeFormCitation` (declared, dead) | `agent/src/copilot/schemas.py:296-302` |
| Intake PDFs (`sergio-angulo-intake-form.pdf`, `-v2.pdf`) | `agent/tests/fixtures/documents/pdfs/` |

## 5. Design

### 5.1 Doc-type resolution — code decides, never the model

`DocType`'s docstring already fixes the rule: the OpenEMR **category** picks the schema —
`Lab Report` → `LAB_PDF`, `Patient Information` → `INTAKE_FORM`. Discovery returns a doc_type per
document, and `attach_and_extract(document_id)` looks the type up from the discovery cache. **The
tool must not take a model-supplied `doc_type`** — that would let the model choose which schema to
read a document through.

`_is_lab_document()` (`agent/src/copilot/fhir/models.py:550`) currently *excludes* intake forms by
design. Generalize discovery rather than adding a parallel lab-shaped tool.

### 5.2 Fact tagging — tag by eventual write target

Decided with JOS-81's findings in hand, so tags are forward-compatible with the write that ships
later:

| Fact | Tag | Rationale |
|---|---|---|
| Lab result | `Observation` (unchanged) | `procedure_result` reads back as a FHIR Observation |
| Demographics | `Patient` | `PUT /fhir/Patient` / `PatientService::update` |
| Allergies | `AllergyIntolerance` | `lists` type=allergy, `verification='unconfirmed'` |
| ~~Medications~~ | ~~`MedicationRequest`~~ | **Moved to `medication_list` (JOS-91)** — intake no longer extracts medications. The tag/write path (`lists` type=medication) is unchanged; it now fires for a med-list document |
| Family history | `FamilyMemberHistory` | Aspirational; see below |

**Accepted limitation:** `FamilyMemberHistory` has no route, controller, service, or structured
table here (family history is nine fixed `history_data.relatives_*` free-text columns). The tag
cannot round-trip. Taken deliberately for badge honesty and symmetry; recorded in JOS-81.

**Invariant change — test it, don't assume it.** `registry.py:16` records that *"No FHIR read tool
fetches Observations, so this resource type is unique to document facts and never collides with the
FetchLog."* The new tags **are** fetched by read tools, so the guarantee weakens from
**type-disjoint** to **id-disjoint**: document facts use `docid#ordinal`, which never matches a FHIR
uuid, and `CompositeResolver` tries `FetchLog` first, falling through on a miss. The reasoning
holds, but it is now load-bearing — cover it with an explicit test and rewrite the comment.

### 5.3 Geometry — pluggable locators, not one hardcoded chain

**The problem.** Box location assumes a lab table end to end, in three places:

- `locate_value_box` requires the anchor word left of `_LEFT_MARGIN_MAX = 200.0` pts — a module
  constant encoding "the test-name column sits at the left of a lab table."
- `_estimate_row_box` needs an OCR **table block** plus parsed HTML table rows; it bands the
  y-range by row count. No table, no box.
- `map_lab_report` hardcodes the order (text-layer → table band → page) and converts px→pt inline.

**Measured against the real intake form.** One document, **four layout idioms**:

| Section | Idiom | Markup |
|---|---|---|
| Demographics | label:value | `<div class="k">Date of Birth</div><div class="v hand">03 / 14 / 1979</div>` |
| Sex | checkbox + option | `<span class="box"><span class="x mark">✕</span></span><span>Male</span>` |
| Medications, allergies | real header table | `<th>Medication</th><th>Dose / Strength</th>` |
| Family history | checkbox rows | `<div class="row"><span class="box"><span class="x mark">✕</span></span><span class="cond">Asthma</span><span class="rel hand">Mother, Brother</span></div>` |

Also: the form is **multi-column** — 193 of 334 page-1 words sit right of x=200, with label blocks
at x≈39 *and* x≈492 — so most intake labels are rejected as anchors today. And intake values span
multiple words (`2117 Cypress Bend Dr, Austin, TX 78745`), needing span-merging.

**Conclusion: bind locators per FIELD, not per doc type.** "Tables vs forms" is the wrong axis — a
single intake form needs three different strategies, and labs need a chain of their own.

#### The seams

**(a) Normalize once.** A `DocumentGeometry`, built per document from `(pdf_bytes, ocr_response)`,
carrying every evidence source already in **PDF points**: text-layer words, OCR blocks, parsed
tables, page dims. No locator ever sees a DPI; `_px_to_points` stops being an inline concern of
`map_lab_report`.

**(b) Make box quality explicit.** Today `_require_bounding_box` is satisfied by `_page_box` — a
whole-page rectangle — so "every lab fact is click-to-source" can mean "somewhere on page 1". A
box needs to declare what it is:

```python
class BoxPrecision(StrEnum):
    EXACT     = "exact"      # merged word span — the printed value itself
    ROW_BAND  = "row_band"   # right row, full table width
    LINE_BAND = "line_band"  # right text line, coarse x
    PAGE      = "page"       # "somewhere on this page"

@dataclass(frozen=True)
class LocatedBox:
    box: BoundingBox
    precision: BoxPrecision
    locator: str             # which strategy fired — for traces
    evidence: Evidence       # PRINTED_VALUE | CHECKED_MARK — what the box actually proves
```

A per-doc-type **precision floor** then makes "intake requires a box" mean something real instead
of being satisfiable by a page rectangle.

**(c) A locator protocol + strategy library**, replacing the hardcoded sequence:

```python
class ValueLocator(Protocol):
    name: str
    def locate(self, req: LocateRequest, doc: DocumentGeometry, state: LocatorState) -> LocatedBox | None: ...
```

| Locator | Role |
|---|---|
| `AnchoredValueLocator(direction, anchor_region, max_gap)` | Generalizes **both** today's lab row match and intake label:value; merges multi-word spans. `_LEFT_MARGIN_MAX` becomes a per-instance `anchor_region`, not a module constant |
| `TableCellLocator(header=...)` | Header-matched cells (medications, allergies) |
| `CheckboxLocator` | Marked glyph nearest a label; boxes mark+option; emits `CHECKED_MARK` |
| `TableRowBandLocator` | Today's `_estimate_row_box` |
| `LineBandLocator` | Form fallback when there is no text layer |
| `PageBoxLocator` | Last resort, honestly labelled `PAGE` |
| `OcrNativeBoxLocator` | Stub for the per-field boxes `W2_ARCHITECTURE.md` §3.1 claims Mistral returns. The code comment says it returns whole-table blocks only — reconcile separately |

`LocatorState` carries the per-document cursor that today threads awkwardly through
`map_lab_report`, so repeated anchors still map to successive rows in order.

**(d) Bind chains per field**, first hit wins:

```python
LAB    = LocatorChain([AnchoredValueLocator(LEFT_COLUMN), TableRowBandLocator(), PageBoxLocator()])
INTAKE = {
  "demographics.sex":            [CheckboxLocator(), LineBandLocator()],
  "demographics.*":              [AnchoredValueLocator(RIGHT), LineBandLocator()],
  "current_medications[].name":  [TableCellLocator("Medication"), TableRowBandLocator()],
  "allergies[].substance":       [TableCellLocator("Allergy / Substance"), TableRowBandLocator()],
  "family_history[]":            [CheckboxLocator(), LineBandLocator()],
}
```

**Migration safety.** The lab chain reproduces today's exact three-step order, so `map_lab_report`
behaviour is unchanged and the existing 388-line `test_extractor.py` is the regression net for the
refactor. Land the refactor green **before** adding intake chains.

### 5.4 The checkbox hazard — a grounding bug, not a geometry one

For `sex` and family history the value text is **preprinted**: "Male" *and* "Female" are both on
the page; "Asthma" is printed whether or not it is ticked. The only evidence for the fact is the ✕
in the adjacent box.

So a text-match locator would box "Diabetes" for a fabricated *"family history: diabetes"* claim,
and the grounding gate would pass it — the citation contract only asks that `quote_or_value` appear
"EXACTLY as it appears on the source page", which it does. **A box pointing at text that does not
support the claim is worse than no box**: it launders a hallucination through click-to-source.

**Decision: the `evidence` discriminator is enforced, not just recorded.** A checkbox-derived fact
is grounded only by `CHECKED_MARK`; its box spans the mark plus its option text, so a physician
clicking through sees the tick. The grounding gate **rejects** a checkbox-derived fact whose box is
backed only by `PRINTED_VALUE`.

### 5.5 Citation routing — fixes a latent bug

`SourceRef.to_citation()` (`agent/src/copilot/schemas.py:97-106`) branches on
`bounding_box is not None` and hardcodes `LabPdfCitation`, so an intake fact would serialize as
`source_type: "lab_pdf"`. Route by doc type and light up `IntakeFormCitation`. Invisible today only
because the JS reads the raw `SourceRef`, never `claim.citations[]` — fix it before anything
consumes that union.

### 5.6 Two doc types under fixture replay

Single-valued three layers deep; all three change together:

- `ocr_fixture_path` / `document_pdf_path` (`config.py:157-166`) are global scalars → per-doc-type.
- `FixtureOcrBackend.process` ignores `pdf_bytes` **and** `doc_type` (`extractor.py:146-156`),
  returning one recording for every call → select by doc type.
- `_fixture_extractor_for` (`evals/runner.py:109`) keys on `get_lab_documents`; its docstring states
  the invariant — *"safe because the golden set has exactly one patient with a lab document"* —
  which an intake case breaks.

### 5.7 Fixture recording is a prerequisite

**There is no committed way to record a fixture** (only `agent/scripts/ocr_spike.py`,
self-described as "NOT production code"). Promote it to a supported script first — every golden
depends on it. The recorder must call the **production** OCR backend and declare no probes of its
own; `ocr_spike.py` duplicating the probes is how a recording drifts from the request production
actually makes.

### 5.8 Probe rules learned from real recordings (do not regress these)

The committed intake `*.ocr.json` held only `address`, `allergies`, `family_history`. That was
**not** a spike-schema artifact — it is what Mistral returns. Three findings, each verified against
a live call, each with a test pinning it:

1. **Every probe field must be REQUIRED (nullable, never defaulted).**
   `response_format_from_pydantic_model` leaves a defaulted field out of the JSON schema's
   `required` list, and Mistral then **silently omits it** from `document_annotation`. Typing a
   field `str | None = None` cost six of nine intake fields; `str | None` with no default returns
   all nine (v1: 10 → 21 facts mapped). This is also why the lab path never hit it —
   `_LabReportProbe.results` has no default.
2. **Demand verbatim text on every field.** Mistral returned `date_of_birth: "1979-03-14"` for a
   form printing `03 / 14 / 1979`. A normalized value cannot be located on the page, so the fact is
   dropped — correct (never fabricate a box) but the field is lost. An explicit "EXACTLY as printed,
   do NOT reformat" instruction fixes it.
3. **Normalize quote glyphs when matching.** A form prints a quoted answer with typographic quotes
   (`“…”`); the extractor echoes it with straight ones (`'…'`). Only the delimiters differ, but an
   exact match drops an otherwise-verbatim 28-word chief concern. `spans.norm` strips quote glyphs
   from span ends.

Free-text fields also need a span limit past their real length (the fixture's chief concern is 43
words); a limit below it silently drops the field.

## 6. Sequencing

1. **Geometry refactor, lab-only, behaviour-identical.** `DocumentGeometry`, `BoxPrecision`,
   `LocatedBox`, the locator protocol, and the lab chain. Existing tests must pass untouched.
2. Fixture recorder script (unblocks all goldens).
3. `_IntakeFormProbe` + per-doc-type OCR request shaping; lift the `LAB_PDF` refusal.
4. Intake locators (`AnchoredValueLocator` RIGHT, `TableCellLocator`, `CheckboxLocator`,
   `LineBandLocator`) + per-field chains; re-record intake goldens (digital + scanned).
5. `map_intake_form`; widen `ExtractedDocument.report` to `LabReport | IntakeForm`.
6. Generalized discovery + category→doc_type resolution.
7. Registry intake arm + tags; `to_citation` routing.
8. `evidence` enforcement in the grounding gate.
9. Per-doc-type fixture/config plumbing; eval runner.
10. Tests + intake eval case; seed the intake `DocumentReference`.
11. Live verification in the worktree stack (:8302).

## 7. Acceptance

- `attach_and_extract` on a `Patient Information` doc returns cited intake facts; doc_type comes
  from the category, never the model.
- Lab extraction behaviour is **unchanged** by the refactor — existing tests pass without edits.
- Every intake fact carries a box meeting the doc type's **precision floor**; a **scanned** form
  degrades to a coarse box, not zero facts.
- A checkbox-derived fact whose box is backed only by preprinted text is **rejected** by the gate.
- Intake facts serialize as `IntakeFormCitation` with `source_type: "intake_form"`.
- Click-to-source opens the intake PDF at the right box, with no frontend change.
- A lab and an intake case coexist under fixture replay in one process; the intake eval case passes.
- The grounding gate rejects an ungrounded intake claim; id-disjointness vs `FetchLog` is tested.

## 8. Risks

| Risk | Mitigation |
|---|---|
| Refactoring shipped lab geometry regresses labs | Land step 1 green with existing tests unmodified, before any intake work |
| Precision floor + no text layer silently drops every fact on a scan | `LineBandLocator` fallback + a scanned intake fixture, both required to ship |
| Checkbox facts launder hallucinations through click-to-source | `evidence` discriminator enforced by the gate (§5.4) |
| Mistral OCR is weak on free-text intake (`W2_ARCHITECTURE.md` §12) | Strict schema refuses unsupported fields; intake eval rubrics are the tripwire; Claude vision stays the named fallback behind the same boundary |
| Tag change weakens the FetchLog non-collision guarantee | Explicit id-disjointness test; rewrite the `registry.py:16` comment to state the real invariant |
| Intake is far more PHI-dense than labs (name, DOB, address, phone) | Facts flow to Mistral and into traces — confirm PHI hygiene holds under existing observability rules before enabling live |


---

## 9. As built — where the code departed from this spec

Every item here was forced by measurement, not preference. `W2_ARCHITECTURE.md` §3.5/§3.6 carries
the durable version; this is the delta from what was agreed above.

1. **`ValueScope` was dropped.** §5.3 proposed "one span matcher + N scope resolvers". It does not
   survive contact: a scope narrows candidate words for the matcher, but `TableRowBandLocator` and
   `PageBoxLocator` produce a box **directly** and never span-match. Locators now share the
   `spans` helpers directly — same pluggability, one less layer that half the implementations
   would have ignored.
2. **`directions=(RIGHT, BELOW)`, not `RIGHT`.** §5.3's binding would have extracted **nothing**
   from v1: its values sit BELOW their labels (label `top=106.9`, value `top=116.4`, same `x0`).
   v2 is the opposite. Both orders are needed, tried in order.
3. **`LocateResult` is three-valued, not `LocatedBox | None`.** Without `REFUTED`, `CheckboxLocator`
   returning None for an unticked option lets the chain fall through to a coarser locator, which
   matches the preprinted text and hands back a box — laundering the claim the refusal existed to
   stop. See §3.6 of the architecture doc.
4. **Evidence is per located BOX, never per field.** §5.4 implied "sex requires CHECKED_MARK". v2
   states `Sex: Male` as plain text, so that rule makes the field permanently unextractable there —
   and fails the anti-overfit criterion outright.
5. **Enforcement is at map time, not in the gate.** §5.4 said "the gate rejects a checkbox fact
   backed only by PRINTED_VALUE". The gate only sees *recorded* facts, so that means recording a
   fact you intend to refuse. A refused fact is never recorded → the model cannot cite it → the gate
   rejects the claim as ungrounded. Same observable guarantee, no trap.
6. **`Evidence` → `BoxEvidence`** (collides with `copilot.schemas.Evidence`, the guideline card).
7. **Fixture BYTES must be id-keyed, not doc-type-keyed** (§5.6 missed this). `FixtureFhirClient`
   served one PDF for any id, so an intake extraction would read the lab report's page: right
   values, nothing locatable, every fact silently dropped. Now resolved per seeded document's own
   category.
8. **Probe before recorder** (§6 had them reversed) — a usable recording cannot exist before the
   probe does.
9. **`pdf_geometry.py` survives as a compat shim** — `test_extractor.py` and `test_overlay_stamp.py`
   both import it. Deleting it is a separate cleanup.
10. **The fixtures were never a "spike shape".** §5.7 originally claimed the committed intake
    recordings were an artifact of an ad-hoc JOS-47 probe. They were not: that is what Mistral
    returns for a probe whose fields are optional. See §5.8 — the required-fields rule is the real
    cause, and it cost six of nine fields.

**Not done here, tracked elsewhere:** the scanned-intake fixture and the fixture/chart mismatch
(JOS-86); write-back (JOS-81); `chief_concern` is tagged `Patient` because no `FactKind` fits.
