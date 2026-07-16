# Spec — write-back of agent-derived facts (JOS-81)

**Status:** agreed, pre-implementation.
**Issue:** [JOS-81](https://linear.app/josiemachalek/issue/JOS-81). Related: JOS-80 (intake extraction),
JOS-70 (conditional extraction), JOS-49 (data authority).
**Implements:** `W2_ARCHITECTURE.md` §3.4 (one-time transform, idempotency), §6 (data authority,
FHIR round-trip without duplicates, sidecar-in-OpenEMR).

## Goal

Facts the agent extracts from a document persist into OpenEMR as **native clinical records**, linked
back to the source document, and flagged as **agent-derived rather than clinician-confirmed** —
written under the physician's session ACL, never by a service credential.

PRD-week-2 Core Req 1 requires the ingestion tool to "persist derived facts as appropriate FHIR
resources or OpenEMR records." Today nothing persists: the agent's FHIR client is read-only and
derived facts live only in an in-memory `DocumentFactRegistry` that dies on restart.

## The auth decision (the reason this issue exists)

Write-back is **structurally blocked** on every surface the agent can reach. Verified:

| Path | Verdict | Evidence |
|---|---|---|
| `patient/<X>.write` scope | Never constructed; cannot be granted | `ServerScopeListEntity.php:104-120` builds only `user/$resource.write` for `['Patient','Practitioner','Organization']`; SMARTv2 emits no `.c`/`.u` (`:167` `// we'll ignore write for now`) |
| `system/<X>.write` (SMART Backend Services) | Grant fully exists; **the write scope does not** | `CustomClientCredentialsGrant.php:28` implements the ONC backend-services grant, and `oe-system`/`USER_ROLE_SYSTEM` is real (`UuidUserAccount.php:104-107`) — but the write loop at `ServerScopeListEntity.php:118-120` has no system branch, unlike every read loop. System scopes are read + `$export` only. |
| FHIR write routes | Only Organization / Patient / Practitioner exist | `_rest_routes_fhir_r4_us_core_3_1_0.inc.php:546,553,560,569,677,684`. No POST/PUT for Observation, AllergyIntolerance, MedicationRequest, Condition, DocumentReference, Provenance. |
| FHIR `Patient` write under a patient token | 403 — unreachable | `:561`, `:570` call `RestConfig::request_authorization_check($request,"patients","demo")` unconditionally, with no `isPatientRequest()` branch (contrast `GET` at `:578-590`). |
| Legacy `/api` (has real inserts) | Users-role only; patient **and** system tokens rejected | `BearerTokenAuthorizationStrategy.php:373` (403 without `api:oemr`), `:383-392` role matrix — `patient` → `/portal/`+`/fhir/`, `system` → `/fhir/` only. |

**Decision: writes go through a session-authenticated module endpoint**, mirroring the proven
`source-view.php` shape — `globals.php` bootstrap (enforces auth), `CsrfUtils::verifyCsrfToken`,
`AclMain::aclCheckCore`, and **pid from `$session->get('pid')`, never the URL** (`source-view.php:26,44,49,54`;
the pid rule is an explicit IDOR defense).

**Consequence — the write is posted from the browser, not the Python worker.** The worker runs on
Railway with a patient-scoped SMART token and has no OpenEMR session cookie; giving it one would mean
inventing the service credential we just ruled out, and letting it supply the pid would reintroduce
the IDOR `source-view.php` defends against. So the agent returns facts as it does today and the
sidebar posts them.

- **Phase 1 (this issue):** auto-persist on arrival — no confirmation step.
- **Phase 2 (follow-up):** same endpoint behind a clinician-accept gate. Purely additive.

**Accepted risk:** browser-posted facts are client-supplied and not verifiable against the agent's
in-memory registry, so a crafted request could persist facts the agent never extracted. **Bounded, not
eliminated:** the endpoint is ACL-gated to a physician who can already write these records through the
normal OpenEMR UI, so it is not privilege escalation. Documented rather than hidden.

## Data model

Every derived fact carries a native "not clinician-confirmed" marker in OpenEMR's own vocabulary — no
core changes, nothing masquerading as physician-authored. **The markers are not symmetric**, because
what each resource can express differs:

| Fact | Table | Derived marker | Reads back as | Strength |
|---|---|---|---|---|
| Labs | `procedure_result` | `result_status='preliminary'` | Observation `status: preliminary` | Strong |
| Allergies | `lists` (`type='allergy'`) | `verification='unconfirmed'` | AllergyIntolerance `verificationStatus: unconfirmed` | Strong |
| Medications | `lists_medication` | `request_intent='proposal'` (+ `lists.comments`) | MedicationRequest `intent: proposal` (+ `note`) | **Weaker — see below** |
| Demographics | — | none exists | — | **Do not write** |

`preliminary` is in the FHIR-valid status list (`FhirObservationLaboratoryService.php:357-364`), so it
survives the round-trip. `lists.verification` genuinely defaults to `unconfirmed` on read: the coding
block is hardcoded `unconfirmed` and only overridden `if (!empty($dataRecord['verification']))`
(`FhirAllergyIntoleranceService.php:223-237`), and the column is `NOT NULL DEFAULT ''`. Stay inside the
seeded set (`unconfirmed|confirmed|refuted|entered-in-error`, `database.sql:6871-6874`) — the FHIR
layer passes the value through verbatim with no allowlist, so an unseeded value yields a code with a
NULL display.

### Medications — the marker is weaker, and that is a documented limitation

**`lists.verification` is never read for a medication.** Only the allergy and condition services read
that column; writing it on a `type='medication'` row is a **silent no-op** — it would sit in the
database and never surface. Nor is `status` usable: `PrescriptionService::getBaseSQL:234-238` derives
it from a `CASE` over `enddate` + `activity` alone, so a `lists` medication can only ever be `active`,
`completed`, or `stopped`. `draft` exists in `FHIRMedicationStatusEnum` but **no column produces it**.

What *is* expressible is `lists_medication.request_intent='proposal'` → `MedicationRequest.intent`
(`PrescriptionService.php:211-212`, `FhirMedicationRequestService.php:498-507`). Its seeded
`list_options` description (`database.sql:12314`) reads: *"The request is a suggestion made by
someone/something that doesn't have an intention to ensure it occurs and without providing an
authorization to act."* That is a literal description of an agent-derived medication. It must be set
**explicitly** — the `lists` branch defaults to `plan` when NULL.

**The honest caveat:** a consumer filtering only on `status` sees an ordinary active medication. The
signal is coded and spec-blessed, but weaker than the allergy path. Reinforce it with a disclosure in
`lists.comments` (surfaces as `MedicationRequest.note`, `PrescriptionService.php:216`). Do not rely on
`lists_medication.is_primary_record` → `reported`: on a configured install a `reportedReference` to the
primary organization wins and the flag is discarded (`FhirMedicationRequestService.php:405-417`).

### Demographics — out of scope, deliberately

`PatientService`/`FhirPatientService` have no verification concept, `patient_data` has no such column,
and FHIR `Patient` has no `verificationStatus`. Writing extracted demographics would be an **unflagged
in-place overwrite of clinician-entered chart data**, with no way for any reader to know it was
machine-derived. There is no honest way to do it, so we do not. This also resolves the destination
JOS-80 deferred to us for `chief_concern` (tagged `Patient` "for want of a closer write target") — it
has no honest home either and stays unpersisted.

### Insert paths

- **Allergies:** `AllergyIntoleranceService::insert` (`:254-290`). It hardcodes `date=NOW()`,
  `activity=1`, `type='allergy'`, and `buildInsertColumns` whitelists against the real `lists` columns,
  so `verification` passes straight through. The validator requires only `title` + `puuid`.
- **Medications:** `PatientIssuesService::createIssue` (`:60-87`), which inserts the `lists` row and
  delegates to `MedicationPatientIssueService::createIssue` (`:33-49`) for the `lists_medication` side —
  the only path that reaches `request_intent`. Not `ListService::insert` (hardcodes 6 columns, cannot
  reach `lists_medication`).

Phase 2 becomes a state transition on these same rows: `preliminary`→`final`,
`unconfirmed`→`confirmed`, `proposal`→`order`.

### An empty list is missing data, not a negative finding

`IntakeForm`'s docstring is explicit: an empty `allergies` list means **"none read from the form"**, not
an affirmative "no known allergies". Persisting an empty list as NKDA would fabricate a clinical
assertion the document never made. Write nothing for an empty section.

### Bounding boxes are optional for intake facts

Only `LabResult` enforces a box (`_require_bounding_box`, `schemas.py:140-153`); an intake medication
with `bounding_box: null` is valid. So the intake path needs an optional citation, and the sidecar's
`bbox` column already allows `''` for exactly this. `BoundingBox` itself keeps refusing zero-area boxes
— correct for labs, and the nullability belongs at the fact level, not inside the value object.

### Labs — the four-row chain

`procedure_result` holds the fact but **has no patient column**; patient linkage runs through the
parent chain. Writing one lab result means writing four rows:

```
procedure_order (patient_id ← the ONLY non-defaulted column; activity=1)
  └─ procedure_order_code (REQUIRED — see trap)
       └─ procedure_report (procedure_order_seq MUST equal the order_code's)
            └─ procedure_result (one row per lab value; document_id → documents.id)
```

Synthesizing an order for an unordered result is **house style**, not a fiction we invent: the HL7
receiver does exactly this when results arrive with no matching order
(`receive_hl7_results.inc.php:1109-1131`), leaving `order_status` empty and `procedure_order_type`
defaulted. The `spike/w2-vision-writes` branch (`569827f03`) proved this chain end-to-end — 2 derived
labs round-tripped as LOINC-coded FHIR Observations with `valueQuantity`, idempotently.

> **The trap.** `ProcedureService::search` joins
> `preport.procedure_order_seq = order_codes.procedure_order_seq` (`:210-211`) with `order_codes`
> LEFT-joined. Omit the `procedure_order_code` row and the predicate compares against NULL, never
> matches, and **the results vanish from FHIR with no error** — a clean insert and zero Observations.
> This is the one failure mode that looks exactly like success. It gets an explicit test.

Other gates: `WHERE activity = 1` (`ProcedureService.php:189`); `result_code` **and** `result_text`
both non-empty or `Observation.code` degrades to a nullFlavor UNK
(`FhirObservationLaboratoryService.php:251-261`); `result` must not be `DNR`/`TNP`.

### The sidecar — provenance and the bounding box

**Problem:** a persisted fact carries the *value* but not the *pixel rectangle*. `procedure_result`
has a native `document_id` FK (`database.sql:10507`) but no geometry; `lists` has **no document link
at all**; `FhirProvenanceService` has no `entity`/`derivedFrom` support (`:99-153`) and hardwires the
author to `lists.user` → `users.username`. The spike hit this for real and stashed the bbox as JSON in
`procedure_result.comments`, flagging the FHIR round-trip as lossy for click-to-source geometry.

So facts could persist while the citation — the actual product — is lost on restart.

**Decision: a module-owned sidecar table**, per §6 ("stored alongside the source in OpenEMR, not a new
agent datastore" — the agent deliberately holds no datastore of its own). Keyed on
`(document_id, content_hash)` per §3.4, holding the validated facts plus page/bbox citations. It is a
**rebuildable derived cache, not a system of record** (§6 table) — OpenEMR remains the single source of
truth.

Rejected alternatives:
- **`lists.external_id`** — holds the CDA act's `id/@extension` and is the CDA importer's dedupe key
  (`CdaTemplateImportDispose.php:161`). Overloading it would corrupt CDA re-import idempotency. Live
  data-integrity bug, not a style objection.
- **`audit_master`/`audit_details`** — OpenEMR's staging buffer for document-derived facts, but it
  carries no document id, is EAV-keyed to pseudo-tables `lists1/2/3`, is welded to
  `CarecoordinationTable`, and on approval `insert_patient()` receives `$document_id` and **discards
  it** (`:464`, `:952-954`). CDA import records no provenance on the resulting row. Wrong granularity;
  no pattern to inherit.
- **`procedure_result.comments`** (the spike's workaround) — labs-only, unstructured, no home for
  intake facts.

Shape mirrors `clinical_notes_documents` (`database.sql:15136`) — join table, `created_at`/`created_by`,
unique pair key. Note that precedent is **schema-only with no PHP consumer**: a shape to copy, not an
integration point to hook.

**Accepted limitation:** provenance is visible to the module and to SQL, **not through FHIR**. A
`GET /fhir/AllergyIntolerance` will not show that a fact came from a document. Documented; smaller than
the claim §3.1 makes today.

## Non-goals

- **Skipping the VLM on re-extraction (§3.4 steps 1-2).** The sidecar is a module table with no FHIR
  resource, so the Railway worker cannot read it — the same wall as writes. This issue owns *idempotent
  persistence* (the endpoint checks `(document_id, content_hash)` and never duplicates records, which is
  what §6 requires); **JOS-70** owns not re-running OCR.
- **Family history.** No FHIR resource, no service, no structured table — nine fixed free-text columns
  (`history_data.relatives_*`, `database.sql:2954-2962`). No target exists. JOS-80 tags it
  `FamilyMemberHistory` aspirationally.
- New FHIR write routes, core scope changes, a service credential, or a Provenance store.
- Demographics write-back (`Patient` is writable, but only via a `user/`-scoped ACL route).

## Acceptance

- Extract from the Sergio lab PDF → `GET /fhir/Observation?patient=<uuid>` returns the results with
  `status: "preliminary"`, `valueQuantity`, and units — surviving an agent restart.
- An allergy → `GET /fhir/AllergyIntolerance` shows `verification: unconfirmed`.
- Every persisted fact resolves through the sidecar to document + page + bbox; click-to-source works
  from persisted state, not just the in-memory registry.
- Re-running extraction on the same document creates **no duplicate records** (§6 store-once).
- A re-upload (new content hash) extracts as a new version; prior facts stay traceable to the prior one.
- **Regression test for the trap:** a chain written without `procedure_order_code` must fail loudly in
  our code, not silently return zero Observations.
- `W2_ARCHITECTURE.md` §3.1 (×2) and §6 corrected — see below.

## Doc corrections required (CLAUDE.md: docs must not drift)

1. **§3.1** "each validated lab result becomes a FHIR `Observation`" — true only via the
   `procedure_order → order_code → report → result` chain; there is no Observation write route.
2. **§3.1** "tagged with provenance pointing back to the source `DocumentReference`" — not
   expressible in FHIR. `FhirProvenanceService` synthesizes Provenance on the fly (`:99`, `:275`),
   has no `entity`/`derivedFrom`, and is hardcoded to `author` (`:162`). The real mechanism is
   `procedure_result.document_id` + the sidecar.
3. **§6** "Intake facts are persisted as appropriate OpenEMR records" — only once this ships, and via
   the session endpoint, not a patient-scoped write.

## Risks

- **Silent-vanish on missing `order_code`** — the headline trap; explicit test.
- **UUID backfill contradiction — resolve empirically before relying on either.** The spike reports
  "UUID registration needed or `procedure_result.uuid` is NULL and the Observation has no id", but
  `ProcedureService::__construct:46-55` calls `UuidRegistry::createMissingUuidsForTables`, which should
  backfill on read. One is wrong; the failure mode is id-less Observations.
- **Partial chain failure** — order written, result insert fails → orphan order. Needs a transaction.
- **Concurrency** (§3.4) — two turns touching an un-extracted document could double-write; per-document
  lock or upsert keyed on `(document_id, content_hash)`.
- **`$sessionAllowWrite`** — do not set it; `source-view.php:23-24` notes writing the session races the
  TLS-proxy session rotation (the known SMART launch race). DB writes are unaffected.
