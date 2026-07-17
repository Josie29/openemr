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
| **From the record** | `FHIR` | resource type + collection date | panel table; singletons as rows | collapsed |
| **Read from documents** | `LAB_PDF`, `INTAKE_FORM` | `document_id` → `page` | one card per document; one preview, all boxes | **open** |

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

**Combining within the document tier.** Three facts read off one intake form are *one document read
three times*. Render one card per `document_id`, and one preview per `page` with **every** box
drawn — the selected fact's box lit, the rest dimmed, numbered to match the fact list.

Drawing all boxes is not a convenience. It answers a question the per-fact view *structurally
cannot*: **what did the model not read?** "3 of 12 fields read" is only visible when the boxes share
a page. Three boxes on a twelve-field form tells the reader the extraction was partial; three
separate single-box previews let them assume it was complete. Dimming (rather than lighting all
boxes equally) preserves the unambiguous fact→box link that per-fact previews had; the numbering is
the fallback where contrast alone does not carry.

**Boxless facts need no new state.** `bounding_box` is nullable on the wire, but ingestion already
refuses to claim what it cannot box: lab values that miss the precision floor are skipped
(`extractor.py:528`) and a lab citation without a box raises (`ingestion/schemas.py:171`); intake
fields that cannot be proven on the page are dropped (`_locate` → `None`, `extractor.py:585`). The
gate stamps the box only from that sidecar, so a document fact cannot reach the wire unboxed. The
nullable must **stay** nullable — `to_citation` routes on `doc_type`, never on the box, because
branching on the box would silently demote a document fact to a FHIR citation (`schemas.py:100-109`).
The panel therefore carries a **defensive branch, not a designed state**: a boxless document fact is
excluded from the card's CTA count and withheld from the overlay.

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
  panel table (`tabular-nums`); combine document facts by `document_id` → `page` into one card with
  a single all-boxes preview. **No backend change** — every field this needs is already on the wire
  and system-set. Requires a `$v_js_includes` bump (touches `.js`/`.css`).

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
10. Four FHIR lab facts from one collection date render as **one** panel table with one
    `View source`, not four prose lines with four identical source cards.
11. Three facts from one intake-form page render as **one** card with **one** preview showing all
    three boxes — selected lit, others dimmed, numbered to the fact list — not three previews of
    the same page.
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
- **Group headers vs. inline stripe alone** — with tiers grouped, does each item still need its own
  provenance chip, or does the header carry it? A function of list length; answer against a real
  13-item panel, not a mockup.
- **Grade badges** — render only where `grade` is reliably parseable from `text`; full support
  needs the corpus re-chunk.
