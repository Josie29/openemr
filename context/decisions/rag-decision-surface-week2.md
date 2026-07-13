# The RAG Decision Surface — Week-2 Reference Field Guide

**Purpose:** A panoramic map of the decisions a RAG pipeline forces, the *term of art*
for each, the options under it, and a recommended pick for **this** build. This is a
**reference/primer**, not decision evidence — the actual verdicts for the two contested
Week-2 RAG choices live in their own records and are the source of truth:

- **Vector store · hybrid strategy · reranker · embeddings** → [`vector-db-week2.md`](vector-db-week2.md)
- **Supervisor + workers orchestration (incl. agentic RAG)** → [`agent-framework-week2.md`](agent-framework-week2.md)

Use this doc to get the vocabulary and see the whole board; use those two for the
reasoned decisions. Cited from Week-2 `../W2_ARCHITECTURE.md` (RAG design) as the
terminology reference.

**Scope for every "pick" below:** small clinical-guideline corpus (hundreds–low-thousands
of chunks, static) · standalone Python/FastAPI agent · Pydantic AI · Claude · Langfuse ·
Railway. Picks are defaults for this footprint, not universal truths.

**How to read it:**

- **Term** — the vocabulary to grab: the name of the decision itself.
- **Your pick** — recommended default for this build; not the only valid answer.
- **★** — a decision you actually make on day one (vs. a near-non-decision at this scale).

> Verify all model names and pricing at build time — they move monthly.

---

## 00 · Ingestion & Chunking

Turn raw documents into retrievable units. The decisions here set the ceiling on
everything downstream — you can't retrieve what you chunked badly.

### Chunking strategy ★
**Term:** chunking / text splitting / document segmentation

- **Fixed-size** — N tokens or characters per chunk.
- **Chunk overlap** — sliding-window param; adjacent chunks share a tail so ideas aren't severed.
- **Recursive character splitting** — split on a separator hierarchy (para → sentence → word); common default.
- **Semantic chunking** — split where consecutive-sentence embeddings diverge (topic boundaries).
- **Layout-aware / structural** — split on headings, tables, sections.
- **Proposition-based** — decompose into atomic factual statements.

**Your pick:** **Layout-aware** — your sources are clinical PDFs; structure is signal.

### Indexing granularity
**Term:** what you embed vs. what you return

- **Parent-document retrieval** — a.k.a. small-to-big: embed small for precision, return the parent for context.
- **Sentence-window retrieval** — embed single sentences, return a window around the hit.
- **Auto-merging retrieval** — merge adjacent hits back into their parent when enough fire.
- **Hierarchical indexing / RAPTOR** — a tree of recursive summaries; retrieve at multiple abstraction levels.

**Your pick:** Add **parent-document** only if chunks lose context in evals; skip for v0.

### Metadata enrichment
**Term:** metadata / payload

- **Attach fields** — source, section, publish date, specialty — what powers metadata filtering later.

**Your pick:** Tag **source + section + date** now; it's cheap and unlocks filtering for free.

### Contextual retrieval
**Term:** contextual retrieval (Anthropic technique)

- **Context prefix** — prepend an LLM-generated blurb to each chunk before embedding so isolated chunks keep their document context.

**Your pick:** High-ROI quality upgrade — add once v0 hybrid works.

---

## 01 · Embedding & Indexing

How text becomes searchable. The big fork is which retrieval paradigm you index for —
and it's not either/or.

### Retrieval paradigm ★
**Term:** sparse vs. dense vs. multi-vector

- **Sparse / lexical** — term-matching: BM25 (standard), TF-IDF, SPLADE (learned sparse).
- **Dense** — neural embeddings in a vector space; where "cosine similarity" lives.
- **Multi-vector / late interaction** — ColBERT, ColQwen2 — one vector per token; more precise, more storage.

**Your pick:** **Hybrid** sparse + dense (BM25 + embeddings). Multi-vector is stretch.

### Embedding model
**Term:** encoder / bi-encoder

- **Dimensionality** — vector width (768 / 1536 / 3072); more expressive, more storage.
- **Matryoshka (MRL)** — embeddings you can truncate to fewer dims with graceful loss.
- **Bi- vs cross-encoder** — bi-encoder (this) encodes separately & is indexable; cross-encoder (rerank) encodes together.
- **Domain-specific vs general** — clinical/bio embedders vs general-purpose.

**Your pick:** **voyage-3.5** — upgrade to voyage-3-large if clinical-term recall lags.

### Vector index / ANN algorithm
**Term:** approximate nearest neighbor (ANN) index

- **Flat / brute-force** — exact nearest neighbor (kNN); no index.
- **HNSW** — graph-based ANN; the common default at scale.
- **IVF / IVFFlat / IVFPQ** — inverted-file clustering indexes.
- **DiskANN** — disk-resident ANN for corpora too big for RAM.
- **ScaNN / Annoy / LSH** — other ANN families.

**Your pick:** **Flat / exact** — your corpus is far too small to approximate. A non-decision.

### Vector quantization
**Term:** quantization

- **Scalar / Product (PQ) / Binary** — compress vectors to save memory & speed.

**Your pick:** **None** — irrelevant below ~1M vectors.

---

## 02 · Query & Retrieval

What happens per request. This is where the similarity-metric question lives — and where
most "advanced RAG" actually plugs in.

### Similarity metric ★
**Term:** distance / similarity metric (vector distance function)

- **Cosine similarity** — angle between vectors; the text default; ignores magnitude.
- **Dot / inner product (IP)** — angle + magnitude; equals cosine when vectors are L2-normalized.
- **Euclidean (L2)** — straight-line distance.
- **Manhattan (L1) / Hamming** — L1 rarely for text; Hamming for binary vectors.

> **Don't pick this freely.** Use whatever metric the embedding model was trained with —
> modern text embedders (Voyage, OpenAI, Cohere) are normalized for cosine/dot product;
> L2 on them silently degrades recall.

**Your pick:** **Cosine**, matched to Voyage. This is the umbrella term for the whole family.

### Query transformation
**Term:** query rewriting / query expansion

- **Query rewriting / expansion** — clean up or enrich the raw query.
- **HyDE** — hypothetical document embeddings: embed a fake ideal answer, search with that.
- **Multi-query / fan-out** — generate variants, retrieve each, union the results.
- **Query decomposition** — split a complex question into sub-questions; this is multi-hop.
- **Step-back prompting** — ask a more general question first for foundational context.
- **Query routing** — classify & route to the right index/tool; the seed of agentic RAG.

**Your pick:** Start with **rewriting**; add multi-hop later as a graph node.

### Hybrid fusion method ★
**Term:** rank fusion

- **Reciprocal Rank Fusion (RRF)** — combine by rank position; robust, parameter-light; common default.
- **Relative score / convex fusion** — normalize & weight raw scores.
- **Distribution-Based Score Fusion (DBSF)** — normalize by score distribution.

**Your pick:** **RRF** — the robust default for merging BM25 + dense.

### Retrieval depth
**Term:** top-k

- **Wide-then-narrow** — retrieve a wide k (50–100), then narrow via reranking.

**Your pick:** **k ≈ 50** → rerank down to the top 5–8 fed to the model.

### Metadata filtering
**Term:** pre-filtering vs. post-filtering

- **Pre-filter** — apply the WHERE before the ANN search; more correct, harder for the index.
- **Post-filter** — filter the results after search.

**Your pick:** Trivial at your scale (filter-then-search is nearly free); either works.

---

## 03 · Post-Retrieval Processing

Clean the candidate set before it reaches the model. The single highest-leverage quality
stage.

### Reranking ★
**Term:** two-stage retrieval / retrieve-then-rerank

- **Cross-encoder rerankers** — Cohere Rerank, Voyage, Jina, bge — score query+doc together.
- **LLM-as-reranker / listwise** — RankGPT-style; the LLM orders candidates.
- **Late-interaction rerank** — ColBERT-style scoring as the rerank step.

**Your pick:** **Cohere Rerank 3.5** — one API call, ~free at your volume.

### Context post-processing
**Term:** contextual compression

- **Contextual compression** — strip irrelevant sentences/chunks to save tokens & cut distraction.
- **Maximal Marginal Relevance (MMR)** — diversity-aware selection; penalizes near-duplicate chunks.
- **Deduplication** — drop redundant hits.
- **Context reordering** — mitigates "lost in the middle"; put the strongest chunks at the ends.

**Your pick:** Add **MMR + reorder** once you feed many chunks at once.

---

## 04 · Generation & Grounding

Assemble evidence into the prompt and constrain the answer to it. Where the citation
contract is enforced.

### Context assembly
**Term:** prompt packing / context budget

- **Templating + budget** — how retrieved evidence is formatted into the prompt and the token budget it gets.

**Your pick:** Cap evidence tokens; prompt-cache the schema & instructions (−90%).

### Synthesis strategy
**Term:** stuff / refine / tree-summarize

- **Stuff / compact** — cram all evidence into one call.
- **Refine** — iterate the answer across chunks.
- **Tree summarize** — hierarchical summarization.

**Your pick:** **Stuff** — your corpus fits the window comfortably.

### Grounding & attribution ★
**Term:** groundedness / faithfulness (citation contract)

- **Machine-readable provenance** — every claim carries a source; answer must be entailed by retrieved context, not parametric memory.

**Your pick:** **Required** — this is the PRD's citation contract, not optional.

---

## 05 · Evaluation

Its own decision surface. This is what your CI gate scores against, so treat the metric
choice as a contract.

### Retrieval metrics
**Term:** recall@k / precision@k / MRR / NDCG

- **Recall@k / Precision@k** — did the right chunk make the top-k, and how clean is it.
- **MRR** — mean reciprocal rank; how high the first correct hit sits.
- **NDCG** — normalized discounted cumulative gain; rank-weighted relevance.
- **Context precision / recall** — RAG-specific: is the retrieved context on-target and complete.

**Your pick:** **Recall@k + context precision** on your golden set.

### Generation metrics ★
**Term:** the RAG triad

- **Faithfulness / groundedness** — is the answer entailed by the context.
- **Answer relevance** — does it address the question.
- **Context relevance** — was the retrieved context relevant to begin with.

**Your pick:** **All three**, as boolean rubrics — matches your CI gate.

### Eval framework
**Term:** RAGAS / TruLens

- **RAGAS / TruLens** — purpose-built RAG eval libraries.
- **Pydantic Evals + Langfuse** — structured datasets + scorers, scored to your existing observability.

**Your pick:** **Pydantic Evals + Langfuse** — already your Week-1 stack.

---

## 06 · Architectural Patterns

Named end-to-end loops that compose the stages above. Mostly orchestration-layer — this
is where "advanced RAG" meets your agent graph (see [`agent-framework-week2.md`](agent-framework-week2.md)).

### End-to-end pattern
**Term:** agentic / self / corrective / adaptive / graph RAG

- **Agentic RAG** — an agent classifies the query and routes to the right tool/strategy dynamically.
- **Self-RAG** — the model decides when to retrieve and critiques its own retrievals.
- **Corrective RAG (CRAG)** — grade retrieved docs; if weak, fall back (e.g. web search).
- **Adaptive RAG** — pick strategy by query complexity.
- **Graph RAG** — retrieve over an entity-relationship graph.

**Your pick:** **Agentic RAG** = your supervisor/worker graph. Graph RAG is a deliberate
later escalation (needs a graph substrate).

---

## Priority for your build

The same surface, ranked by leverage — where to actually spend effort first.

| Bucket | Decisions |
| --- | --- |
| **Day-one decisions** | Chunking strategy (layout-aware for PDFs) · Hybrid fusion (RRF over BM25 + dense) |
| **Near non-decisions** | Similarity metric (cosine, matched to embedder) · ANN index (flat/exact — corpus is tiny) |
| **Highest-ROI upgrades** | Reranking (Cohere Rerank 3.5) · Contextual retrieval (context prefix before embedding) |
| **What CI scores** | The RAG triad (faithfulness / answer / context) · Retrieval recall@k on the golden set |
| **Scale you don't have yet** | Quantization / PQ (millions of vectors) · DiskANN / multi-vector (large or precision-critical corpora) |

---

*Verify model names and pricing at build time — they move monthly. A rendered, scannable
version of this guide exists as a Claude artifact; this markdown is the versioned source of record.*
