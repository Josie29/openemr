# Spec — Evidence Relevance Gating & Presentation

**Status:** draft (design contract, pre-implementation). **Branch:** `copilot-evidence-ux`.
**Builds on:** JOS-53 hybrid RAG (`context/specs/hybrid-rag-pipeline.md`), JOS-56 supervisor +
evidence-retriever. **Governed by:** `W2_ARCHITECTURE.md` §3.3 (citation contract), §5 (RAG).
**Design exploration:** the three-level mockup —
https://claude.ai/code/artifact/67d2ab09-3306-4f46-8a2f-fa1f45bfb897

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

---

## 8. Open questions (for eval / iteration, not blocking)

- **τ, K values** — set empirically once the eval set exists.
- **Relevance visibility** — show the raw score (0.94), a coarse High/Medium band, or ordering
  only? (Physicians may distrust a naked ML number.)
- **Evidence default state** — expanded (trust-forward) or collapsed (answer-forward) on a strong
  match.
- **Grade badges** — render only where `grade` is reliably parseable from `text`; full support
  needs the corpus re-chunk.
