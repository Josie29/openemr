# Vector Store + Hybrid Retrieval — Week-2 Decision Evidence

**Purpose:** Working analysis behind Week-2 `../W2_ARCHITECTURE.md` (RAG design) —
decides the **vector store**, **hybrid retrieval strategy**, and **reranker** for the
Stage-2 "basic hybrid RAG" requirement. Decision evidence for the Week-2 architecture
defense, not a deliverable; `W2_ARCHITECTURE.md` is the source of truth. Extends the
stack chosen in `agent-tech-stack.md` (standalone Python/FastAPI agent, Pydantic AI,
Claude, Langfuse, httpx) — this doc only adds the retrieval layer.

**Grounding:** `PRD-week-2.md` Stage 2 + Core Agent Requirement 3 — *"Index a small
clinical-guideline corpus. Retrieve with sparse+dense search, rerank candidate chunks
with Cohere Rerank or an equivalent reranker, and feed only the top grounded evidence
to the answer model."* Evidence snippets must carry source metadata; ColQwen2 /
multi-vector is explicitly **stretch, not core**. Also `agent-tech-stack.md` (Railway
deploy, Pydantic-native contracts, blanket-BAA posture), the Week-2 engineering req
*"/ready must check … vector index and reranker API reachability … return degraded if
unavailable"*, and the Common Pitfall *"narrower than the spec and stronger because of
it."* Landscape verified against 2026 sources (linked at end).

---

## Fixed constraints (from `agent-tech-stack.md` — don't re-litigate)

- **Python agent, one standalone service, Railway deploy**, same project/region as
  OpenEMR (internal private networking available).
- **OpenEMR runs on MySQL, not Postgres.** There is **no existing Postgres** to reuse —
  choosing pgvector means standing up a **new** Railway Postgres service, weighed honestly
  below. This is the single most important footprint fact.
- **Blanket BAA** assumed for every managed vendor, so PHI-in-vendor is acceptable and
  self-hosting is **not** required for compliance. BAA availability is therefore **not a
  differentiator** — picks are made on hybrid quality, ops fit, and ergonomics.
- **Corpus is small and static** (a curated set of guideline chunks the office follows).
  Raw scale is **not** the deciding axis; native hybrid quality, Railway footprint,
  metadata filtering, and Python/Pydantic ergonomics are.

Three things are open — **store, hybrid strategy, reranker**. The reranker is an API call
regardless of store, so it's decided independently.

---

## Contenders — vector store

Judged on: (1) native sparse+dense hybrid + fusion quality, (2) Railway footprint
(in-process vs dedicated service vs new Postgres), (3) metadata filtering + Python/Pydantic
ergonomics, (4) cost / maturity / lock-in.

| Store | Footprint on Railway | Native hybrid (sparse+dense) | Metadata filter + ergonomics | Cost / maturity / lock-in |
|---|---|---|---|---|
| **Qdrant** *(pick)* | **Dedicated service**, official one-click Railway template (v1.18.x), volume pre-mounted, reachable over **private networking** + API key | **First-class.** Universal Query API: `prefetch` a dense + a sparse vector, fuse with `Fusion.RRF` (rank-based, no score-scale tuning) or DBSF. **FastEmbed is built into `qdrant-client`** — generates dense **and** sparse (`Qdrant/bm25`, or the newer neural `Qdrant/minicoil-v1`) with no separate embedding service | Rich payload filters (scope by `guideline`/`source`/`section`); typed Python client, plays cleanly with Pydantic DTOs | Apache-2.0; very mature for hybrid; managed cloud has a free 1 GB tier; low lock-in (open format, self-host or cloud) |
| **LanceDB** *(runner-up)* | **Embedded / in-process** — zero extra services; index is files on a **Railway volume** (or object storage) | **Yes.** BM25 full-text via Tantivy (`create_fts_index()`) + vector, combined by a built-in `RRFReranker()` by default; other rerankers pluggable | Metadata as columns (SQL-style filter); Pythonic, Pydantic model integration | Apache-2.0; embedded model is newest of the batch but hybrid is solid; near-zero lock-in (Lance files) |
| **pgvector (+ pg_search / pg_textsearch)** | **NEW Postgres service** — nothing to reuse (OpenEMR is MySQL). BM25 is **not** in pgvector itself; needs a second extension (ParadeDB `pg_search` or Tiger Data `pg_textsearch`) | Achievable but **assembled**: dense via pgvector, BM25 via the extension, **RRF hand-written in SQL**. Native `tsvector` FTS alone is *not* true BM25 | SQL `WHERE` filters are excellent; but you own schema, indexes, and the fusion query | pgvector Apache-2.0; extensions vary; most moving parts to hand-build; you now operate a general-purpose RDBMS for a vector job |
| **Weaviate** | **Dedicated service**, self-host Docker; heavier (modules/config, higher memory floor) | **First-class.** `alpha`-weighted hybrid, `relativeScoreFusion` (default since 1.24) or `rankedFusion` | GraphQL/collections; good filtering; more concepts to learn than Qdrant | BSD; very mature; heaviest footprint here — over-scoped for a tiny corpus |
| **Chroma** | **Embedded** or Chroma Cloud | **Now native** (2026): first-class sparse vectors (BM25/SPLADE) + `Search()` API fusing via RRF — but the **newest** hybrid path of the group | Simple metadata filter; easiest DX for a quick start | Apache-2.0; hybrid maturity still settling; low lock-in |
| Milvus / Pinecone | Dedicated / managed | Both do hybrid well | Fine | **Doesn't change the conclusion** — Milvus is over-scaled ops for a small corpus; Pinecone is managed-only lock-in with no footprint win over Qdrant's template. Excluded. |

---

## Pick: Qdrant · native RRF over dense + sparse · Cohere Rerank

**Store — Qdrant** (dedicated Railway service via the one-click template, private
networking). **Hybrid — Qdrant Universal Query API**: `prefetch` a dense vector (semantic)
and a sparse vector (lexical), fuse with **`Fusion.RRF`**; embeddings generated by
**FastEmbed inside `qdrant-client`** (dense model + `Qdrant/bm25` sparse, or neural
`minicoil-v1`). **Rerank — Cohere Rerank** (pin `rerank-v4.0-fast`) on the fused top-k,
feeding only the top grounded snippets to the answer model.

Each reason traces to a requirement:

1. **Native sparse+dense+fusion is the PRD core requirement — Qdrant makes it one API
   call, not an assembly.** Core Req 3 asks literally for "sparse+dense search, rerank."
   The Query API does prefetch-both-then-`Fusion.RRF` first-class; **RRF is rank-based**,
   so it sidesteps the score-scale mismatch (bounded cosine vs unbounded BM25) that makes
   naive alpha-weighting unreliable — no tuning to defend. This is the seam a grader will
   poke, and it's a documented feature, not glue code.
2. **FastEmbed collapses the embedding layer into the client — the footprint stays at one
   added service.** `qdrant-client` ships FastEmbed, which produces **both** the dense and
   the sparse (BM25 / neural miniCOIL) vectors in-process. No separate embedding service,
   no separate sparse encoder to run — the whole retrieval stack is *one* new Railway
   service plus a library. That's the ergonomics win that makes "dedicated service" cheap.
3. **Railway fit is a solved path, and it gives a real `/ready` dependency to check.**
   Official one-click template, volume pre-mounted for persistence, reachable over Railway
   **private networking** with an API key. The engineering req wants `/ready` to validate
   the vector index and return *degraded* if unreachable — pinging Qdrant's private URL is
   exactly that check, cleanly demonstrable, versus an embedded store where "reachability"
   is just "did the file open."
4. **Metadata filtering + Pydantic ergonomics + low lock-in.** Payload filters scope
   retrieval by `guideline` / `source` / `section` (needed for the citation contract's
   `source_id` / `page_or_section`); the typed Python client maps onto our Pydantic DTO
   standard; Apache-2.0 with open format means self-host today, managed cloud later, no
   trap. Maturity for *hybrid specifically* is the strongest of the shortlist.

**Cost note:** for a small static corpus the store is a cheap always-on Railway container;
Qdrant's managed cloud free tier (1 GB) would also cover the corpus outright if we'd rather
not run the container — either way, near-zero.

---

## Reranker: Cohere Rerank (`rerank-v4.0-fast`), runner-up Voyage / Jina

The reranker is a hosted API call independent of the store. Options, verified 2026:

| Reranker | Quality / latency | Pricing (2026) | Call |
|---|---|---|---|
| **Cohere Rerank** *(pick)* | Strongest general default, broadest docs, lowest activation cost; ~80–150 ms p50 on sub-2k-token chunks | `rerank-v4.0-fast` **$2 / 1k searches**, `rerank-v4.0-pro` $2.50 / 1k (1 search = 1 query, ≤100 docs; docs >500 tok split + billed as extra). **Note: `rerank-3.5` is deprecated — pin a v4.0 model.** | HIPAA BAA available (given under our blanket-BAA posture) |
| **Voyage `rerank-2.5`** | Comparable quality/latency to Cohere (~0.6 s) | **First 200 M tokens free**, then token-metered (`-lite` ~$0.02 / 1 M tok) — effectively free at our corpus size | Clean swap |
| **Jina `reranker-v3`** | **Fastest tier — sub-200 ms**; top Hit@1 | 10 M tokens free, then ~$0.02 / 1 M tok | Reach for it if latency, not quality, becomes the constraint |
| **bge-reranker-v2-m3** | Solid open cross-encoder | Free (self-host) | Adds a model to host — over-scoped vs the <15 s budget; only if we ever want zero API deps |

**Pick — Cohere Rerank**, exactly as the PRD names it, pinned to `rerank-v4.0-fast`:
strongest default with the least to defend, cost is rounding error on a small corpus, and
it's the reference every grader knows. It's a config-level swap to Voyage (free to 200 M
tokens) or **Jina v3 if the latency report shows rerank on the critical path**.

---

## Runner-up: LanceDB (embedded) — and exactly when it flips the decision

**LanceDB is the choice if the second-service ops cost isn't worth it for a tiny, static
corpus.** It is the strongest *embedded* option: BM25 FTS via Tantivy + vector, fused by a
built-in `RRFReranker()` — same retrieval shape as the pick, but **in-process with zero
extra services**, the index living on a Railway volume. It genuinely minimizes moving parts.

**I'd switch to LanceDB if** any of these hold:
- We decide a second always-on Railway service (and its private-network wiring) isn't
  justified for a corpus of a few hundred static chunks, and prefer the index baked onto a
  volume beside the agent.
- Cold-start / single-service simplicity for the demo outweighs having a real external
  `/ready` dependency to show.

**I'd switch to pgvector only if** the project later adopts Postgres as a first-class store
for *other* Week-2 data (e.g. citation/lineage records) — then consolidating retrieval into
the same DB beats running Qdrant separately. As long as OpenEMR is MySQL and we'd be adding
Postgres **solely** for vectors, pgvector is a new service *plus* a BM25 extension *plus*
hand-written RRF — strictly more assembly than Qdrant for no footprint saving, so it loses.

**Weaviate / Chroma:** Weaviate's hybrid is equally first-class but its footprint is the
heaviest here — over-scoped for a curated guideline set. Chroma's native hybrid only landed
in 2026 and is the least battle-tested path; fine for a spike, not what I'd defend today.

---

## Recommended retrieval flow

```
Guideline corpus (curated chunks, source metadata per chunk)
   │  FastEmbed (in qdrant-client): dense embed + sparse (bm25 / minicoil-v1)
   ▼
Qdrant  (Railway service · private net · payload = {guideline, source, section})
   │  Universal Query API: prefetch(dense) + prefetch(sparse) → Fusion.RRF → top-k
   ▼
Cohere Rerank  (rerank-v4.0-fast)  → keep top-n grounded snippets
   ▼
Pydantic AI answer model  ← receives ONLY the reranked evidence + source metadata
   │  (citation contract: source_type / source_id / page_or_section / chunk_id / quote)
   ▼
output_validator gate (from agent-tech-stack.md) — no unattributable claim ships
```

`/ready` pings Qdrant (private URL) **and** the Cohere endpoint, returning *degraded* if
either is unreachable, per the Week-2 engineering requirement.

---

## Open questions to validate before locking

1. **Dense embedding model** — FastEmbed default vs a clinical-tuned embedder; settle
   empirically once the 50-case eval set exists (retrieval hit-rate is a rubric input).
2. **Fusion choice** — start `Fusion.RRF` (no tuning); only try DBSF if eval hit-rate
   demands score-aware fusion.
3. **Reranker on the critical path?** — if the latency report puts rerank in the
   <15 s budget's way, evaluate Jina v3 (sub-200 ms) as the drop-in.

---

## Sources (2026, verified)

- Qdrant hybrid / fusion: [Hybrid Queries — Qdrant docs](https://qdrant.tech/documentation/search/hybrid-queries/), [Hybrid Search & Universal Query API](https://qdrant.tech/course/essentials/day-3/hybrid-search/), [FastEmbed miniCOIL](https://qdrant.tech/documentation/fastembed/fastembed-minicoil/), [miniCOIL article](https://qdrant.tech/articles/minicoil/), [fastembed BM25 source](https://github.com/qdrant/fastembed/blob/main/fastembed/sparse/bm25.py)
- Qdrant on Railway: [Railway Qdrant template](https://railway.com/deploy/qdrant-vector-database), [Deploy Qdrant](https://railway.com/deploy/qdrant), [Self-hosting Qdrant with Docker 2026](https://sliplane.io/blog/self-hosting-qdrant-with-docker-on-ubuntu-server)
- LanceDB hybrid: [LanceDB Hybrid Search docs](https://docs.lancedb.com/search/hybrid-search), [Hybrid search + custom reranking](https://medium.com/etoai/hybrid-search-and-custom-reranking-with-lancedb-4c10a6a3447e)
- pgvector hybrid (needs extension + hand-rolled RRF): [ParadeDB — Hybrid Search in Postgres: the Missing Manual](https://www.paradedb.com/blog/hybrid-search-in-postgresql-the-missing-manual), [Tiger Data — Elasticsearch's Hybrid Search now in Postgres (BM25+Vector+RRF)](https://www.tigerdata.com/blog/elasticsearchs-hybrid-search-now-in-postgres-bm25-vector-rrf), [Katz — Hybrid search with pgvector](https://jkatz05.com/post/postgres/hybrid-search-postgres-pgvector/)
- Weaviate hybrid: [Weaviate hybrid docs](https://docs.weaviate.io/weaviate/search/hybrid), [Fusion algorithms](https://weaviate.io/blog/hybrid-search-fusion-algorithms)
- Chroma hybrid (new in 2026): [Chroma — Sparse vector search is here](https://www.trychroma.com/project/sparse-vector-search), [Chroma Hybrid Search guide](https://chroma-core-chroma.mintlify.app/guides/hybrid-search)
- Rerankers: [Cohere pricing 2026 (Rerank v4.0 fast $2/1k, pro $2.50/1k; v3.5 deprecated)](https://www.aipricing.guru/cohere-pricing/), [Best Rerankers leaderboard — Agentset](https://agentset.ai/rerankers), [Reranking guide — Cohere/Voyage/Jina 2026](https://localaimaster.com/blog/reranking-cross-encoders-guide), [Voyage AI pricing (200M rerank tokens free)](https://docs.voyageai.com/docs/pricing), [Jina Reranker API](https://jina.ai/reranker/)
- Cohere HIPAA BAA: [Cohere review 2026 — enterprise RAG / BAA](https://aiagentsquare.com/agents/cohere)
