# Week 2 Architecture Defense — Deck Content

**Source of truth for the deck content.** The HTML artifact is a *render* of this file —
edit here, review/diff here, and I regenerate the artifact from it. Structural notes and
open questions are at the bottom.

- **Artifact:** https://claude.ai/code/artifact/53c8d7ca-aab3-48e3-b394-3ceed18273cd
  (rendered from `w2-arch-defense-deck.html` in this directory — publish source of truth)
- **Format target:** 3–5 min spoken defense.
- **Decision state legend:** 🟢 Decided · 🔵 Partial · 🟠 Pending · 🔴 Hard gate · 🟣 → Task (JOS-xx)

---

## Slide map (order + state at a glance)

| # | Eyebrow | Title | State |
|---|---------|-------|-------|
| 1 | — | The Multimodal Evidence Agent (title) | 🟢 LIVE |
| 2 | 01 · Scenario | The signal is buried in the documents | — |
| 3 | 02 · Baseline | The baseline we build on | 🟢 Shipped |
| 4 | 03 · Framing | The single-agent tripwire has fired | — |
| 5 | 04 · Target architecture | Supervisor + two workers, one grounding gate | 🔵 shape decided |
| 6 | 05 · Decision 1 | Pydantic AI over LangGraph — concretely | 🟢 Decided (JOS-45) |
| 7 | 06 · Decision 2 | See the document without inventing facts | 🔵 Partial (JOS-47) |
| 8 | 07 · Decision 3 | Ground the answer in guideline evidence | 🟢 Decided (JOS-46) |
| 9 | 08 · Decision 4 | What runs where on Railway | 🔵 Partial (JOS-48) |
| 10 | 09 · Decision 5 | One source of truth per data type | 🔵 Partial (JOS-49) |
| 11 | 10 · Eval gate | CI that can actually block a regression | 🔴 Hard gate (JOS-50) |
| 12 | 11 · Observability | Trace the whole graph; watch the new cost | 🟢 Extend existing |
| 13 | 12 · Risks | Where this fails, and the mitigation | — |
| 14 | 13 · What we defend | The decided-vs-open map | — |

---

## Slide 1 — Title 🟢

- **Eyebrow:** Architecture Defense · Week 2
- **Headline:** The Multimodal Evidence Agent
- **Subhead:** Teaching the Clinical Co-Pilot to **see** clinical documents, route work across a small graph, and gate every change with evals.
- **Banner:** LIVE — framework + vector DB decided; VLM, data model & eval-gate still open. Each decision is a task under JOS-43.
- **Meta row:** AgentForge · Clinical Co-Pilot · OpenEMR fork · Gauntlet AI Austin Track · 2026-07-13

## Slide 2 — The scenario (01)

- **Headline:** The signal is buried in the documents
- **Lede:** A physician is prepping a follow-up visit. The chart has structured OpenEMR data — but the recent, important information sits in a **scanned lab PDF** and a **front-desk intake form**.
- **Callout (quote):** "What changed, what should I pay attention to, and what evidence supports the recommendation?"
- **Points:**
  - **See documents** — ingest a lab PDF and an intake form, extract structured facts.
  - **Separate fact from evidence** — patient-record facts vs. guideline evidence, each cited.
  - **Stay grounded when it's messy** — imperfect scan, incomplete record, follow-up questions.

## Slide 3 — Week 1 baseline (02) 🟢 Shipped

- **Headline:** The baseline we build on
- **Left column:**
  - **Single Pydantic AI agent** — 6 patient-scoped FHIR read tools.
  - **Grounding output-validator** — `ModelRetry` rejects any claim it didn't actually read. The verification seam.
  - **Langfuse observability** — OTel auto-instrument, correlation IDs, cost/latency scores.
- **Right column:**
  - **Eval harness** — Langfuse Experiments, deterministic + LLM-judge scorers.
  - **Deployed on Railway** — OpenEMR (PHP) + separate Python/FastAPI agent + MySQL.
  - **Tiered Claude** — Sonnet 5 workhorse; Haiku 4.5 judge; Opus 4.8 reserved.
- **Closing line:** Good Week-1 architecture should **compound** here. Week-2 extends these seams rather than replacing them.

## Slide 4 — The tripwire (03)

- **Headline:** The single-agent tripwire has fired
- **Lede:** Week 1 chose a single agent **bottom-up, not by default** — `ARCHITECTURE.md §6.1` pre-registered the exact conditions that would flip it to multi-agent.
- **Card A — Pre-registered tripwires:**
  - Independent workers with distinct responsibilities
  - Routing decisions that must be inspectable
  - Explicit, logged handoffs between steps
- **Card B — Week 2 requires:**
  - Supervisor + intake-extractor + evidence-retriever
  - Inspectable supervisor routing
  - Explicit worker handoffs
- **Callout:** Not a reversal of a Week-1 mistake — a **pre-planned expansion firing on schedule.**

## Slide 5 — Target architecture (04) 🔵

- **Headline:** Supervisor + two workers, one grounding gate
- **Diagram (top → bottom):**
  1. **Physician panel** — native chat in the OpenEMR module · SSE stream
  2. ▼ **Supervisor** — decides: extract? retrieve evidence? answer ready? — logged, inspectable handoffs
  3. ▼ two workers: **Intake-Extractor** (VLM → strict schema → citations · lab PDF + intake form) · **Evidence-Retriever** (hybrid RAG + rerank over guideline corpus)
  4. ▼ **Grounding gate (output-validator)** — every clinical claim carries machine-readable citation metadata, or it's rejected
  5. ▼ substrate: **FHIR R4** (reads + derived Observations, round-tripped) · **Qdrant + Cohere rerank** (hybrid guideline evidence) · **Langfuse** (spans, cost, eval scores)

## Slide 6 — Decision 1: Framework (05) 🟢 Decided · JOS-45

- **Headline:** Pydantic AI over LangGraph — concretely
- **Lede:** LangGraph is a reasonable multi-agent default and owns one thing outright: a rendered graph. But our flow is shallow, and our verification gate is already first-class in Pydantic AI.
- **Head-to-head table** (✓ = winner of that row):

  | Criterion | Pydantic AI | LangGraph |
  |---|---|---|
  | Our flow: supervisor → 2 workers → verify | ✓ Delegation + hand-off express it directly | A full `StateGraph` for a shallow, near-linear flow |
  | Verification gate | ✓ `@output_validator` + `ModelRetry` — one hook, self-correcting | Rebuild it: a validate node + conditional edge + retry counter in graph state |
  | Migration from today's agent | ✓ Near-zero — tools, deps, contracts, Langfuse carry over | Rewrite the turn loop; port every tool to a node |
  | Inspectable routing (PRD) | Structured route events + Langfuse child spans | ✓ Native graph object — its real edge |

- **Callout (the decider):** The gate decides it. LangGraph makes us **rebuild** self-correcting verification that Pydantic AI gives as one decorator — and PRD Core Req 4 accepts "another inspectable orchestration framework," i.e. inspectable *handoffs*, not a rendered graph.
- **Footnote:** OpenAI Agents SDK also considered — rejected: Claude second-class via LiteLLM, and its guardrails halt rather than self-correct.
- **When LangGraph wins:** durable resumable state or human-in-the-loop mid-flow (§6.1 tripwires #1/#2 — not present today). In-framework escalation is `pydantic-graph` first, before any cross-framework move.

## Slide 7 — Decision 2: Ingestion + VLM (06) 🔵 Partial · JOS-47

- **Headline:** See the document without inventing facts
- **Lede:** `attach_and_extract(patient_id, file, doc_type)` for **lab_pdf** and **intake_form**.
- **🟢 Decided:**
  - Strict **Pydantic schema is the canonical contract** — raw VLM output never bypasses validation.
  - Citation shape: `{source_type, source_id, page_or_section, field_or_chunk_id, quote_or_value}`
  - PDF **bounding-box overlay** required.
- **🟠 Pending:**
  - Which **VLM** — Claude vision vs. a dedicated extractor.
  - How **extraction confidence** surfaces unsupported fields.
  - Store source in OpenEMR (`DocumentReference`) + FHIR round-trip for derived Observations.

## Slide 8 — Decision 3: Hybrid RAG (07) 🟢 Decided · JOS-46

- **Headline:** Ground the answer in guideline evidence
- **Lede:** **Qdrant** (dedicated Railway service) · hybrid in one Universal Query API call · **Cohere Rerank** on the fused top-k · only top grounded snippets reach the answer model.
- **Options:** Qdrant ✓ · LanceDB (runner-up) · pgvector (rejected — MySQL, not Postgres) · Weaviate · Chroma
- **Points:**
  - **Native sparse + dense + RRF fusion** in one API call — rank-based fusion sidesteps the BM25-vs-cosine score-scale problem (no alpha to tune or defend).
  - **One added service, not two** — FastEmbed folds embedding + sparse encoding into the client; yields a real `/ready` vector-index dependency an in-process store can't.
  - **Reranker** — Cohere `rerank-v4.0-fast`, the PRD-named default (~$2/1k, negligible on a small corpus).
- **When we'd switch to LanceDB** (embedded): if a second always-on service isn't worth it for a few-hundred-chunk static corpus.

## Slide 9 — Decision 4: Deployment topology (08) 🔵 Partial · JOS-48

- **Headline:** What runs where on Railway
- **Today:** OpenEMR (Apache/PHP + module) · Python/FastAPI agent · MySQL.
- **Week 2 adds:** **Qdrant** service (private networking) · **Cohere Rerank** API · document storage. Agent stays one Pydantic AI service — no new agent service.
- **Points:**
  - **`/ready` becomes dependency-aware** — validates document storage, vector index, and reranker reachability; returns *degraded*, not binary up/down.
  - **No orphaned state** — new services follow the same deploy/redeploy discipline as `main` → prod.

## Slide 10 — Decision 5: Data authority & lineage (09) 🔵 Partial · JOS-49

- **Headline:** One source of truth per data type
- **Lede:** Four new data types enter the system. Each needs an owner, lineage, access control, and validation — no silent overwrites.
- **Types:**
  - **Extracted lab observations** — derived → FHIR Observation, round-tripped.
  - **Intake facts** — demographics, meds, allergies, family history.
  - **Guideline chunks** — corpus, versioned, reproducible from repo.
  - **Citation records** — link every claim back to a source.
- **Callout:** Uploaded documents and derived observations must round-trip through OpenEMR **without creating duplicate or untraceable records.**

## Slide 11 — The eval gate (10) 🔴 Hard gate · JOS-50

- **Headline:** CI that can actually block a regression
- **Lede:** Graders will inject a small regression and confirm the CI gate fails. A demo that can't block regressions has not met the Week-2 bar.
- **Today:** 7 cases, report-only CI (`should_fail_on_regression: false`); runs only on `qa → main` promotion PRs.
- **Week 2 target:** 50 cases, boolean rubrics; PR-blocking git hook — fails if any category regresses >5% or drops below threshold.
- **Rubric categories:** `schema_valid` · `citation_present` · `factually_consistent` · `safe_refusal` · `no_phi_in_logs`

## Slide 12 — Observability & cost/latency (11) 🟢 Extend existing

- **Headline:** Trace the whole graph; watch the new cost
- **Left:**
  - **Per-worker spans** as children of the supervisor span.
  - **New metrics** — ingestion latency, extraction confidence, retrieval hit rate, routing decisions.
  - **Correlation ID** propagates into ingestion, handoffs, VLM/retrieval calls, FHIR writes.
- **Right:**
  - **New cost drivers** — VLM pass + embedding + rerank + extra inference hops.
  - **The lever** — tiered routing (Haiku / Sonnet / Opus) keeps the <15s budget and the cost curve defensible.
  - **PHI-free** — no raw document text or identifiers in traces, evals, or cost reports.

## Slide 13 — Risks & tradeoffs (12)

- **Headline:** Where this fails, and the mitigation
- **VLM invents field labels** → schema + citation + confidence make unsupported extracted facts *visible*, not silent.
- **Supervisor becomes a black box** → every routing decision and handoff is logged and explainable.
- **Scope creep — 5 doc types** → ship **two** reliably before adding a third. Narrower is stronger.
- **Multi-agent latency vs. <15s** → routing discipline + streaming; tiered models on cheap sub-tasks.
- **Callout (disqualifier):** PHI leakage into SaaS observability is the disqualifier — scrubbing + a CI PHI-detection check guard it.

## Slide 14 — What we defend / next (13)

- **Headline:** The decided-vs-open map
- **🟢 Decided:** Framework (Pydantic AI) · vector DB (Qdrant + Cohere) · verification gate · Pydantic contracts · Langfuse · Claude tiers · eval direction · citation shape
- **🔵 Partial:** Architecture shape · ingestion & VLM · data authority · deployment topology
- **🟠 Pending:** VLM model · data-model spec · eval-gate build
- **Points:**
  - **Working method** — this deck is the skeleton; each pending slide becomes a task under **JOS-43**; decisions flow back into these slides and `W2_ARCHITECTURE.md`.
  - **Checkpoints (CT)** — Architecture Defense (now) → MVP Tue → Early Thu → Final Sun, 2026-07-19.

---

## Structure notes (my critique — for the rethink pass)

Concrete things to weigh before we lock the structure:

1. **14 slides is a lot for 3–5 min** (~13–20s each). If the defense is time-boxed, target ~10–11. Candidate trims below.
2. **Merge Decisions 4 + 5 (slides 9 + 10)?** Deployment topology and data authority are both "supporting infrastructure" and both 🔵 Partial. One combined "Infrastructure & data ownership" slide would save a slide and read as one thought.
3. **Slide 13 (Risks) may be cuttable** for a *live* defense — risks are strong in the written `W2_ARCHITECTURE.md`, and a spoken defense often lands better ending on the decided/open map (slide 14). Keep it only if graders explicitly grade risk awareness in the room.
4. **Slides 4 (tripwire) + 5 (architecture)** are the spine — consider making the architecture diagram the single longest-dwell slide and compressing others around it.
5. **Ordering question:** should the **eval hard-gate (slide 11)** move earlier / get more prominence? It's the PRD's explicit hard gate and the thing graders actively try to break — burying it at #11 undersells it. Option: promote it to right after the architecture (become slide 6), so "how we prove quality" is framed before the individual tech decisions.
6. **Decision slides are asymmetric** — 6 and 8 are 🟢 fully argued; 7, 9, 10 are 🔵 skeletons. Once JOS-47/49/50 resolve, those slides need the same head-to-head rigor as slide 6, or they'll look thin next to it.

Tell me which of these to act on (or your own changes) and I'll revise the content here, then re-render the artifact.
