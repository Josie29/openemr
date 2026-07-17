# Spec — Evidence Relevance Gating & Presentation

**Status:** draft (design contract, pre-implementation). **Branch:** `copilot-evidence-ux`.
**Builds on:** JOS-53 hybrid RAG (`context/specs/hybrid-rag-pipeline.md`), JOS-56 supervisor +
evidence-retriever. **Governed by:** `W2_ARCHITECTURE.md` §3.3 (citation contract), §5 (RAG).
**Design exploration:** the three-level mockup —
https://claude.ai/code/artifact/67d2ab09-3306-4f46-8a2f-fa1f45bfb897
**Provenance tiering (§3.5, JOS-88):** options + why Organize won —
https://claude.ai/code/artifact/45c9737e-24be-48fc-93c8-30d2e6355054 ·
resolved open questions (collapse, empty tiers, combining) —
https://claude.ai/code/artifact/c99deb35-c85a-4cce-bccb-cbce84e9cd52

This spec is the agreed design *before* code. It defines what the evidence path should return and
how the sidebar should render it. It does **not** restate the retrieval mechanics (that's the
hybrid-rag spec) — it changes what survives retrieval, how it's shaped on the wire, and how it's
shown.

---

## 1. Problem

The evidence panel today (see the current-state column of the artifact) has three coupled defects,
all traceable to two root causes verified in code:

1. **No relevance gate anywhere.** `EvidenceRetriever.retrieve` (`agent/src/copilot/rag/retriever.py:90-162`)
   prefetches 20 dense + 20 sparse → RRF → Cohere rerank → returns the top `rerank_top_n` (**5**,
   `config.py:134`) with **no minimum-score filter**. A topically-wrong query (e.g. a lipids
   question for a 23-year-old) still returns 5 snippets and grounds an answer on them. The
   `rerank_score` that could gate this is computed (`retriever.py:160`) and then **dropped** in
   `ChunkRegistry.resolve` — it never reaches the answer or the UI.

2. **"Evidence" is coupled to claims, not sources.** The sidebar's "Show evidence (N)" uses
   `claims.length` (`interface/modules/custom_modules/oe-module-ai-copilot/public/assets/js/ai-copilot.js:426`)
   — N is *how many sentences the answer model wrote*, not how many distinct sources grounded the
   answer. Each claim renders its own paraphrased sentence + a slug chip, so the evidence list
   **restates the summary** (visible in the asthma result: the summary and the three evidence items
   say nearly the same thing twice) and the same source appears multiple times.

Consequence: the count is meaningless, weak matches are indistinguishable from strong ones, the
verbatim guideline quote (already on the wire in `claim.citations[]`) is never shown, and there is
no "we found nothing relevant" state.

---

## 2. Goal & non-goals

**Goal.** Make the evidence path **relevance-gated and source-shaped**: filter weak chunks
*before the answer model sees them*, return a deduplicated, relevance-ranked list of the sources
that actually grounded the answer, and render each as a verbatim quote with a human-readable
citation. The answer prose links to evidence rather than restating it.

**Non-goals (this increment).**
- **The "no strong match" empty state** — deliberately tabled. This spec *creates the gate* that
  makes it possible (§3.1); the empty-state UX (copy, whether to answer from model knowledge with
  a disclaimer, etc.) is a **follow-up**. Here, when nothing clears the floor, the evidence section
  is simply absent and the answer is composed without guideline grounding.
- **Level 3 patient-applied verdict** (artifact column 03) — separate, larger effort with its own
  clinical-safety bar. Out of scope here.
- **Corpus re-chunk** for structured `grade` / `organization` fields — orthogonal (tracked with
  the corpus work). Grade badges render only where the value is reliably parseable.
- **Reranker/threshold tuning** — we ship sane defaults and mark them as eval targets (§6).

---

## 3. Design

### 3.1 Relevance gate — "threshold, then cap" (upstream)

Replace "top-K, no threshold" with **"filter by floor τ, then keep the top K survivors."** One
mechanism serves both cases: strong matches surface; nothing above τ → the evidence set is empty
(the tabled no-match state).

**The gate is upstream — it filters what the answer model grounds on, not just what the UI shows.**
So the weak-match answer is never composed in the first place.

- After Cohere rerank, drop every snippet with `relevance_score < τ`.
- From the survivors, keep the top **K** by score.
- If the survivor set is empty, the evidence-retriever returns `[]`; the supervisor proceeds to
  `answer` with no guideline evidence (the answerer composes from what else it has, or states it
  lacks corpus support — empty-state copy is the follow-up).

**Defaults (eval-tuning targets, §6):** τ = **0.5**, K = **3**. Cohere rerank should fetch a few
more than K (e.g. `top_n = 8`) so the floor has candidates to cut. These become config:
`retrieval_relevance_floor: float = 0.5` and an evidence cap (reuse/rename `rerank_top_n`).

> **Calibration caveat.** Cohere relevance scores are **not calibrated across queries** — 0.5 on
> one query ≠ 0.5 on another. τ is a pragmatic floor, not a probability. It must be set against the
> eval set, and we should watch for both failure directions (§7).

### 3.2 Decouple evidence from claims — a source-shaped payload

Add a top-level **`evidence[]`** array to the `/chat` response: the deduplicated, relevance-ranked
list of the sources that grounded the answer. The evidence *section* renders from this array; its
count is `evidence.length` (distinct sources), not claim count.

- **Dedup by `chunk_id`** — each distinct retrieved snippet is one card. Multiple claims citing the
  same chunk collapse to one entry.
- **Order by `relevance_score`, descending.**
- Claims remain (they carry the inline citation anchors, §3.3) but no longer *define* the evidence
  list.

Each `evidence[]` entry carries (all already available per the hybrid-rag feasibility map, some
need threading through `ChunkRegistry.resolve` — the "backend-lite" tier of the artifact):

| Field | Source | Tier |
|---|---|---|
| `quote` | chunk `text` (verbatim) | already on wire (`claim.citations[].quote_or_value`) |
| `source_id` | corpus `source` → humanized (e.g. "GINA") | already on wire |
| `section` | corpus `section` | already on wire |
| `chunk_id` | chunk id (dedup key) | already on wire |
| `relevance_score` | Cohere rerank score | **thread through** (dropped today) |
| `source_url` | corpus `source_url` (clickable) | **thread through** |
| `year` | corpus `date` | **thread through** |

### 3.3 Answer prose links, does not restate

The summary is the *synthesized* answer; evidence cards are the *grounding*. They must not
duplicate each other.

- The answerer emits inline citation markers (`[n]`) that map claims → `evidence[]` indices.
- Prompt change: synthesize, don't enumerate — the summary should not be a sentence-per-snippet
  paraphrase of the evidence.
- Frontend renders `[n]` as an affordance that scrolls to / highlights `evidence[n]`.

### 3.4 Presentation (artifact Levels 1–2)

Render each `evidence[]` entry as a card: **verbatim quote** + human citation line
(`GINA · 2022 · Ch. 2 Assessment`) + relevance signal + `View source ↗`. Lead the answer with a
scannable TL;DR / criteria structure where the content supports it. Evidence section default state
and relevance-visibility per §6 open questions.

### 3.5 Provenance tiering (JOS-88)

**Problem.** The panel renders a machine-coded FHIR `Observation` and a value a vision model read
off a scan **identically** — same prose line, same left bar, same `View source`. Click-to-source is
what makes this unsafe rather than untidy: the verification gesture *launders* the weaker fact. A
physician clicks, sees a box drawn on a scan, and reads that as confirmation. A structured
`Observation` earns that trust; a VLM box has not.

The cause is that the sidebar never reads the `source_type` discriminant. `ai-copilot.js:695`
splits the world with `isGuidelineRef()` — a `resource_type === 'guideline'` string check on the
legacy Week-1 `SourceRef` — and `ai-copilot.js:429` counts `evidence.length + recordClaims.length`,
collapsing FHIR and both document arms into one bucket. The four-arm union built in JOS-57 is
unused by the layer that needs it.

**Presentation-layer only.** No retriever, router, or graph change; no eval risk. Every field this
needs (`source_type`, `document_id`, `page`, `bounding_box`) is on the citation today and is
system-set, never model-written (`verification.py:295-298`).

**Three tiers, not four.** `LAB_PDF` and `INTAKE_FORM` merge: they make the same claim about trust
(a model read this off a scan) and differ only in *which* document — which becomes the grouping key
one level down. The tag still drives the switch; it just does not map 1:1 to a heading.

| Tier | `source_type` | Combining unit | Rendered as | Default |
|---|---|---|---|---|
| **Guidelines** | `GUIDELINE` | per chunk (τ-capped at K) | numbered quote cards (§3.4, unchanged) | collapsed |
| **From the record** | `FHIR` | resource type + collection date | shared source chip; singletons as rows | collapsed |
| **Read from documents** | `LAB_PDF`, `INTAKE_FORM` | `document_id` → `page` | one card per document page; one preview, all boxes numbered | **open** |

**Tier collapse.** Tiers are collapsible, but the section is *already* a collapsed `<details>`
(§3.4), so uniformly collapsing tiers inside it puts every quote two clicks away — and two-click
evidence is evidence nobody checks. Therefore:

- Tier **headers and counts stay visible** whenever the section is open, even with bodies
  collapsed. The composition line (`2 guideline · 4 record · 3 read from scan`) **is** the safety
  signal and costs zero clicks: the physician learns the answer leaned on three machine-read facts
  before opening anything.
- The **document tier defaults open**; the other two default collapsed. Hiding the tier that most
  needs scrutiny behind an extra click would quietly invert this spec's purpose.

**Empty tiers are omitted, not zeroed.** `Guidelines (0)` reads as retrieval *failing* when the
honest meaning is "nothing cleared τ" — the gate working (§3.1). This is the §5 **No strong match**
rule applied one level down.

**Combining within the document tier.** Three facts read off one intake form page are *one document
read three times*. Render one card per (`document_id`, `page`), and one preview drawing **every**
box, each carrying a number badge that matches the card's fact list. One CTA per card
(`Check all N against the scan`), not one per fact.

Drawing all boxes is not a convenience. It answers a question the per-fact view *structurally
cannot*: **what did the model not read?** Three boxes on a twelve-field form tells the reader the
extraction was partial; three separate single-box previews let them assume it was complete. The
rendered page around the boxes is the denominator — no "N of M" count is needed, and none is
available (the wire carries only the facts the answer *cited*, never the extractor's total).

**Numbered badges, not selected-lit/rest-dimmed** (revised after the live turn, JOS-88 phase 2). An
earlier draft of this section had each fact open the preview with its own box lit and the others
dimmed. Numbering is strictly better: it keeps an unambiguous fact→box link *while* every box stays
equally visible — which is the coverage signal — where dimming trades one for the other. It also
needs no selection state to encode or re-render, and it collapses N per-fact buttons into one card
CTA, which was the actual defect the live turn exposed (seven near-identical `Check against the
scan` buttons stacked down one lab card). A number is also legible where contrast alone is not.

**A fact with no box gets no number** and is excluded from the CTA's count — a dash, not an index
pointing at nothing. `bounding_box` is nullable and ingestion already drops what it cannot box
(§3.5 below), so this is a defensive branch, but the count must never promise a check the overlay
cannot deliver.

**Boxless facts need no new state.** `bounding_box` is nullable on the wire, but ingestion already
refuses to claim what it cannot box: lab values that miss the precision floor are skipped
(`extractor.py:528`) and a lab citation without a box raises (`ingestion/schemas.py:171`); intake
fields that cannot be proven on the page are dropped (`_locate` → `None`, `extractor.py:585`). The
gate stamps the box only from that sidecar, so a document fact cannot reach the wire unboxed. The
nullable must **stay** nullable — `to_citation` routes on `doc_type`, never on the box, because
branching on the box would silently demote a document fact to a FHIR citation (`schemas.py:100-109`).
The panel therefore carries a **defensive branch, not a designed state**: a boxless document fact is
excluded from the card's CTA count and withheld from the overlay.

### 3.6 The lab table — system-stamped cells replace model prose (JOS-88 phase 2)

A `lab_pdf` card renders its facts as a table (`Analyte` | `Value` | `Ref`, `tabular-nums`) instead
of one model-authored sentence per result, **and drops the prose**. This is §3.3 ("answer prose
links, does not restate") applied to the lab card: the sentence *"Potassium is high at 5.4 mmol/L
(reference range 3.5–5.1)"* restates, less reliably, data the extractor already read off the page.

The argument is trust, not density: **every cell in the table is system-stamped by the grounding gate
from the extraction; the sentence is the model's retelling of the same numbers.** Where the two
disagree, the table is right. Rendering both would show a physician the weaker version alongside the
stronger one and imply they carry equal weight.

**The backend this required.** `LabResult` always had `test_name`/`unit`/`reference_range`/
`abnormal_flag`, but they reached only the model-facing `LabFactHandle` — the registry flattened them
away before the wire. They now travel as one embedded `LabDetail` sub-model across all four hops
(`_RecordedFact` → `Resolution` → `SourceRef` → `LabPdfCitation`). One optional sub-model rather than
four loose scalars, so each hop keeps the single shape `registry.py`'s normalization rule exists to
protect — `None` for a non-lab fact, not four dead columns.

- **`test_name` is inside `LabDetail`** rather than read from the already-stamped `SourceRef.label`,
  because that stamp is *conditional* on the resolution carrying an identity. The Analyte cell must
  not depend on a branch that can fall through to model-authored text while Value and Ref cannot.
- **`lab_detail` is stamped unconditionally** (`verification.py`), like `bounding_box`: written for
  every fact, `None` for non-lab. **A model-authored `reference_range` could make a normal value read
  as abnormal — or hide an abnormal one — under a UI that says the cells came off the page.** This is
  the rule the whole feature rests on; the gate never *verifies* `lab_detail`, it stamps it.
- **It hangs on `LabPdfCitation`, not `DocumentCitationBase`.** The base holds what both document arms
  carry; an intake citation would inherit a `reference_range` that is meaningless for a date of birth
  and that nothing can ever populate.
- **`abnormal_flag: "no"` renders as nothing**, never the literal word. The enum mirrors OpenEMR's
  `procedure_result.abnormal` and now crosses to the frontend.
- **Absent `lab_detail` falls back to prose rows** — a claim grounded before the field existed, or a
  hand-built ref. Also makes deploy order free: an old sidebar ignores the new field.

---

## 4. Contract changes (summary)

- **Retriever** (`rag/retriever.py`): apply τ-floor + K-cap after rerank; carry `relevance_score`,
  `source_url`, `year` onto the returned snippet projection (stop dropping them in
  `ChunkRegistry.resolve`).
- **Response schema** (`agent/src/copilot/schemas.py`): add top-level `evidence: list[Evidence]`
  to `ChatResponse`; `Evidence` per §3.2. Additive — `claims`/`SourceRef` unchanged.
- **Answerer** (prompt + output): emit `[n]` markers; synthesize rather than enumerate.
- **Frontend** (`ai-copilot.js` + CSS): render the evidence section from `response.evidence`
  (count = distinct sources), show quote + rich citation, inline `[n]` linking. Retire
  `claims.length` as the evidence count.
- **Frontend — provenance tiering** (§3.5, JOS-88): switch on `source_type`; delete
  `isGuidelineRef` and the `resource_type === 'guideline'` check. Group into three tiers, omit
  empty ones, report counts per tier. Combine FHIR facts by resource type + collection date into a
  shared source chip; combine document facts by `document_id` → `page` into one card with a single
  all-boxes preview. Requires a `$v_js_includes` bump (touches `.js`/`.css`).
- **Lab metadata to the wire** (§3.6, JOS-88 phase 2): a `LabDetail` sub-model
  (`test_name`/`unit`/`reference_range`/`abnormal_flag`) embedded on `_RecordedFact` → `Resolution`
  → `SourceRef` → `LabPdfCitation`, stamped **unconditionally** in `verification.py`. This is the one
  backend change the tiering needed; the tiering itself (§3.5) was presentation-only.
- **Viewer** (`public/source-view.php` + `src/Source/`): a packed `boxes=x,y,w,h;…` URL param
  replacing the single `x/y/w/h` (which still works, folded into a one-element list), decoded by
  `SourceBoxCodec` into typed `SourceBox` values — parsed at the boundary, unit-tested without a
  bootstrap, mirroring `Smart/LaunchStateCodec`. Parsing happens *after* every existing gate (CSRF,
  ACL, session pid, document access); the param is pure geometry and carries no identity.

---

## 5. UX states

| State | Condition | Render |
|---|---|---|
| **Grounded** | ≥1 snippet ≥ τ | Answer + `evidence[]` cards (this spec's focus) |
| **No strong match** | 0 snippets ≥ τ | No evidence section; answer composed without grounding *(empty-state copy = follow-up)* |
| **Degraded** | Qdrant/Cohere down | Existing `/ready` degraded path; retrieval error handling unchanged |

---

## 6. Acceptance criteria

1. A well-matched query (e.g. "How is asthma symptom control assessed?") returns an `evidence[]`
   whose count = **distinct sources** shown, ordered by relevance, each with a verbatim quote and a
   human citation — no duplicate source cards, no summary/evidence restatement.
2. A topically-mismatched query (the lipids-for-a-23-year-old case) returns **no evidence section**
   because nothing clears τ, and the answer does not fabricate guideline grounding.
3. `relevance_score`, `source_url`, and `year` are present on each `evidence[]` entry.
4. τ and K are configurable (`config.py`) with the documented defaults, changeable without code
   edits.
5. The evidence count in the UI is driven by `evidence.length`, not `claims.length`.

**Provenance tiering (§3.5, JOS-88).**

6. The sidebar switches on `source_type`; `isGuidelineRef` and the `resource_type === 'guideline'`
   string check no longer exist.
7. A FHIR-sourced fact and a document-extracted fact are distinguishable **at a glance, without
   reading the label** — the criterion the tiering exists to satisfy. If all three tiers read as
   equally confident cards, this increment has added visual variety and no safety.
8. Counts are reported per tier; a tier with zero items renders nothing at all (no `(0)` row).
9. Tier headers and counts are visible whenever the evidence section is open, with tier bodies
   collapsible and the document tier open by default.
10. Four FHIR record facts sharing a resource type and collection date share **one** source chip,
    not four identical ones. *(Corrected: an earlier draft said "FHIR lab facts … one panel table".
    Wrong tier — an uploaded report's results are `lab_pdf` **document** facts, so the table belongs
    to the document tier, per §3.6. The record tier keeps prose rows + a shared chip.)*
11. Three facts from one intake-form page render as **one** card with **one** preview showing all
    three boxes, each numbered to match the fact list — not three previews of the same page, and not
    three per-fact buttons. *(Corrected from "selected lit, others dimmed" — see §3.5.)*
12. An unknown or future `source_type` degrades to the most conservative tier rather than rendering
    as a fact of record.
13. A document fact arriving without a `bounding_box` (defensive; unreachable via ingestion today)
    is excluded from the card's CTA count and is not given a box overlay.

---

## 7. Risks & mitigations

- **Over-suppression (clinical-safety).** Too high a τ hides guidance a physician needed, and a
  missing evidence section reads as "no guidance exists." *Mitigation:* start conservative, tune
  against the eval set, log gated-out near-misses (score just under τ), and design the follow-up
  empty state to say "no strong corpus match" — not "no such guidance."
- **Score miscalibration.** A single global τ mis-serves queries whose scores run high or low.
  *Mitigation:* treat τ as provisional; the eval set is the arbiter; consider a relative gate
  (e.g. drop snippets far below the top score) as a later refinement.
- **Upstream gate changes answers.** Filtering before the answerer alters answer content, not just
  presentation — bigger blast radius than a UI-only change. *Mitigation:* covered by the eval set;
  the gate is a documented, single-point config.
- **Empty-evidence answers.** With the gate upstream, some answers now carry no grounding.
  *Mitigation:* the answerer must be explicit about lack of corpus support rather than sounding
  authoritative (ties into the tabled empty-state work).
- **Tiering that decorates instead of calibrating (§3.5).** Three tasteful cards in three accent
  colors would satisfy "grouped by provenance" while leaving the laundering fully intact.
  *Mitigation:* acceptance criterion 7 is written as the falsifiable version of this — glanceable
  distinction, not labelled distinction. The lowest tier must read as weaker, not merely different.
- **Alarm fatigue on the document tier (§3.5).** Extraction is usually *correct*; if machine-read
  facts are stamped loudly enough, physicians learn to route around the tier — worse than today,
  and it disfigures the bbox work the demo rests on. *Mitigation:* why the tiering carries the
  signal (dashed stripe, "read from a scan") rather than a warning treatment, and why only the
  document tier's **call to action** shifts from passive (`View source`, reads as a receipt) to
  imperative (`Check against the scan`). The laundering lives in the affordance, not the label.
- **Absent tier vs. unconsulted source (§3.5).** Omitting empty tiers means "no document was read"
  and "no document exists" look identical. *Mitigation:* accepted here — that distinction belongs
  to the answer prose, not a zero-count row. Tracked as a follow-up, not fixed in this increment.

---

## 8. Open questions (for eval / iteration, not blocking)

- **τ, K values** — set empirically once the eval set exists.
- **Relevance visibility** — show the raw score (0.94), a coarse High/Medium band, or ordering
  only? (Physicians may distrust a naked ML number.)
- ~~**Evidence default state**~~ — *answered by §3.5.* The section stays collapsed (answer-forward),
  but opening it reveals tier headers + counts immediately; the document tier is open, the rest
  collapsed. Trust-forward and answer-forward stop competing once the composition line carries the
  signal on its own.
- ~~**Group headers vs. inline stripe alone**~~ — *answered.* The first live turn produced the panel
  this needed (16 record + 7 read-from-scan). **Keep the per-item chip in the record tier; drop it in
  the document tier.** They encode different things: the tier header names the *tier* (record vs
  read-off-a-scan), while the chip names the *resource kind* (Condition vs Observation vs
  AllergyIntolerance) — which varies *within* the record tier, so it is not redundant with the header
  and cannot be folded into it. The document tier is the opposite case: its card header already names
  the document, so a per-fact chip would only repeat it; those facts carry a number badge instead
  (§3.5). *Caveat: the reasoning is sound but 16 chips' density has not been eyeballed with the
  record tier expanded on a live turn — revisit if it reads as noise.*
- **Grade badges** — render only where `grade` is reliably parseable from `text`; full support
  needs the corpus re-chunk.
