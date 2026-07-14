# Spec — Hybrid RAG Pipeline + Qdrant on Railway (JOS-53)

**Status:** draft (Phase 0). **Owner:** JOS-53. **Consumes:** JOS-52 corpus
(`agent/src/copilot/rag/corpus/*.jsonl`, 55 verified chunks). **Consumed by:** JOS-56
(supervisor + evidence-retriever worker). **Governed by:** `W2_ARCHITECTURE.md` §3.3
(citation contract), §5 (RAG design), §6 (data model & authority), §10 (deployment);
decision evidence in `context/decisions/vector-db-week2.md`.

This spec is the design contract agreed *before* implementation. It does not restate the
decision evidence (which store / reranker and why — that's `vector-db-week2.md`); it defines
*what we build*, the interfaces, and the acceptance criteria.

---

## 1. Goal & non-goals

**Goal.** Stand up the Stage-2 "basic hybrid RAG" evidence path: index the curated
guideline corpus into Qdrant, retrieve with sparse+dense hybrid fused by RRF, rerank with
Cohere, and return the top grounded snippets — each carrying machine-readable citation
metadata in the unified Week-2 shape — to the answer model via an `evidence-retriever` tool.
Deploy Qdrant on Railway (private networking) and make it a real `/ready` dependency.

**Acceptance (from the Linear issue).**
1. The evidence-retriever returns **cited** guideline snippets for a clinical query.
2. Qdrant is reachable via `/ready` (returns *degraded* when it — or Cohere — is down).

**Non-goals (this increment).**
- Document ingestion / extraction (`lab_pdf`, `intake_form`) — later issue. We only leave
  **typed, reserved seams** in the citation union for them; we do not build the extractor,
  the bbox overlay, or the `Observation` derivation here.
- The supervisor/worker graph (JOS-56). We expose the retriever as a tool + a callable the
  worker graph can wire; we do not build the graph.
- Migrating the Week-1 FHIR `SourceRef` onto the new union (deferred — see §3.3). The FHIR
  grounding gate is untouched this PR.
- Reranker/embedder tuning. Start with documented defaults (FastEmbed default dense +
  `Qdrant/bm25` sparse + `Fusion.RRF`, Cohere `rerank-v4.0-fast`); empirical tuning happens
  once the 50-case eval set exists (`vector-db-week2.md` open questions).
- ColQwen2 / multi-vector visual retrieval (explicitly stretch, not core).

---

## 2. Data model & authority (alignment with W2_ARCHITECTURE §6)

We touch two of the four Week-2 artifact types; the other two are named only to keep the
citation union's seams honest.

| Artifact | Owner (authoritative) | Lineage | Access | Validation |
|---|---|---|---|---|
| **Guideline chunks** | Versioned repo corpus (`rag/corpus/*.jsonl`), indexed into Qdrant | Curated from published guidelines (JOS-52); reproducible from the repo alone | Non-PHI; read by the evidence-retriever | Chunk carries `{chunk_id, guideline, source, source_url, section, date, text}` |
| **Citation records** | The agent (emitted per claim) | Composed from a retrieval result (this increment) or an extraction (later) | Rides with the answer payload | Must satisfy the full citation shape (§3.3) — an evidence claim without chunk metadata is refused |
| Extracted lab observations | OpenEMR FHIR `Observation` | *(later issue)* | — | *(reserved seam only)* |
| Intake facts | OpenEMR | *(later issue)* | — | *(reserved seam only)* |

**Qdrant is authoritative for nothing patient-specific.** It holds only the non-PHI
guideline corpus, rebuildable from the repo — a disposable index, not a system of record.
No PHI is ever sent to Qdrant or Cohere in this path (the query is the physician's question +
guideline text; patient facts come from the separate FHIR tools).

---

## 3. The citation contract

### 3.1 The unified shape (W2_ARCHITECTURE §3.3)

Every clinical claim — retrieved *or* (later) extracted — carries citation metadata in one
shape, keyed on `source_type`:

```
{ source_type, source_id, page_or_section, field_or_chunk_id, quote_or_value }
```

We model this as a **discriminated union** on `source_type` so each source kind is a typed
variant and adding a new one (document extraction) is additive, not a rewrite:

```python
class GuidelineCitation(BaseModel):        # implemented this increment
    source_type: Literal["guideline"]
    source_id: str          # corpus document/source id, e.g. "statpearls-paroxysmal-af-2023"
    page_or_section: str    # the chunk's section heading
    field_or_chunk_id: str  # the Qdrant chunk id (chunk_id)
    quote_or_value: str     # the retrieved snippet text, verbatim

# Reserved seams — declared, NOT implemented here (document-extraction issue fills them):
#   LabPdfCitation(source_type="lab_pdf", ... + bounding-box coords)   [W2_ARCH §3.3]
#   IntakeFormCitation(source_type="intake_form", ...)

Citation = Annotated[
    GuidelineCitation,   # | LabPdfCitation | IntakeFormCitation  (added later)
    Field(discriminator="source_type"),
]
```

Field mapping for the `guideline` arm (from the corpus schema):

| Contract field | Corpus source | Note |
|---|---|---|
| `source_type` | literal `"guideline"` | discriminator |
| `source_id` | chunk `source` | the guideline document id |
| `page_or_section` | chunk `section` | section heading |
| `field_or_chunk_id` | chunk `chunk_id` | the Qdrant point id / chunk id |
| `quote_or_value` | chunk `text` (the retrieved snippet) | verbatim |

`guideline` (the topic slug, e.g. `afib-anticoagulation`) and `source_url` are carried as
**presentation metadata** on the evidence snippet for the citation card / click-to-source —
they are not part of the minimum contract shape but travel with the result.

### 3.2 Why not reuse the Week-1 `SourceRef`

The Week-1 `SourceRef` (`schemas.py`) is FHIR-shaped (`resource_type/resource_id/field/
quote`) and predates the Week-2 unified contract. Guideline evidence is not a FHIR resource
and does not resolve against a `FetchLog` record the way a FHIR field does, so bolting it
onto `SourceRef` would overload that model and split its meaning. Instead we introduce the
unified `Citation` union alongside it, designed to eventually absorb FHIR as a `fhir` arm.

### 3.3 Coexistence with the FHIR grounding gate (deliberate, temporary)

This increment leaves the Week-1 FHIR `SourceRef` and its `enforce_grounding` output
validator **untouched**. Guideline evidence flows as its own typed `Citation` on the
evidence snippets the retriever returns; the evidence-retriever enforces its own guardrail
(reject an evidence claim lacking chunk metadata — W2_ARCHITECTURE §4.3). Convergence of the
two citation systems into one discriminated union (adding the `fhir` arm and routing the
grounding gate by `source_type`) is a **follow-up**, tracked so the split does not calcify.
*Rationale: keeps JOS-53's blast radius on the RAG path; avoids rewriting Week-1 FHIR claim
paths + tests before document extraction forces the unification anyway.*

> **Coordination note — JOS-56.** The supervisor/worker branch also touches `schemas.py`.
> The `Citation` union is **additive** (new symbols, no edits to `SourceRef`/`Claim`/
> `ChatResponse`), so the merge should be clean; flag if JOS-56 also adds a citation type.

---

## 4. Retrieval pipeline

```
55 guideline chunks (rag/corpus/*.jsonl)
  → FastEmbed (in qdrant-client): dense vector + sparse vector (Qdrant/bm25)
  → Qdrant collection: named dense vector + named sparse vector, payload = full chunk metadata
  → query: prefetch(dense, k) + prefetch(sparse, k) → FusionQuery(Fusion.RRF) → fused top-k
  → payload filters (optional): scope by guideline / source / section
  → Cohere rerank-v4.0-fast(query, fused candidates) → keep top-n
  → evidence snippets, each carrying a GuidelineCitation + presentation metadata
  → answer model (via evidence-retriever tool)
```

Defaults (revisit empirically with the eval set): fused `k ≈ 20–40`, reranked `top_n ≈ 3–5`.
RRF is rank-based, so no dense/sparse score-scale tuning to defend.

### 4.1 Module layout (mirrors the `fhir/` precedent)

- `rag/retriever.py` — `EvidenceRetriever` Protocol + `QdrantEvidenceRetriever` impl
  (constructor-injected Qdrant client + Cohere client + settings; no global state). Returns a
  typed `EvidenceSnippet` (Pydantic) `{citation: GuidelineCitation, guideline, source_url,
  rerank_score}`. A `FixtureEvidenceRetriever` (or a `retrieval_mode` flag) lets tests and
  offline dev run with no live Qdrant/Cohere, matching `FhirClientMode`.
- `rag/index.py` — content-correct indexer: creates the collection if absent, **recreates** it
  when forced or when a stale collection's dense dimension no longer matches the embedding model
  (a silent model change would otherwise break every query), then **upserts all chunks** with
  deterministic (uuid5) ids so an edit to an existing `chunk_id` is reflected in place, never
  served stale. Safe to run on every service start (content-correct, not merely append-safe).
  Runs against local Docker Qdrant and prod.
- `rag/models.py` — `EvidenceSnippet`, retriever DTOs (kept out of `schemas.py`, which stays
  the answer/citation contract).
- `config.py` — `qdrant_url`, `qdrant_api_key`, `qdrant_collection`, `cohere_api_key`,
  `rerank_model` (default `rerank-v4.0-fast`), `retrieval_top_k`, `rerank_top_n`,
  `retrieval_mode` (fixture|live), embedding model ids. Secrets from env only (AUDIT.md).

### 4.2 SDK specifics (verified 2026 — Phase-1 scouts)

**Pinned deps** (add to `agent/pyproject.toml`):
`qdrant-client[fastembed]>=1.18,<2`, `cohere>=5.8,<6`. FastEmbed rides the `[fastembed]`
extra (no separate install); the v2 Cohere clients live in the `cohere` package.

**Qdrant — production path** (`create_collection`/`upsert`/`query_points`, NOT the
`.add()`/`.query()` convenience layer). Let the client embed via FastEmbed by passing
`models.Document(text=..., model=...)` — local inference in-process, no embed calls.
- Dense: `"BAAI/bge-small-en-v1.5"` (384-dim, COSINE). Sparse: `"Qdrant/bm25"` —
  **requires** `models.Modifier.IDF` on the sparse vector params, and `options={"avg_len":
  <mean tokens/chunk>}` on each `Document` for BM25 length normalization. (`Qdrant/minicoil-v1`
  is a same-shape neural swap if lexical recall proves weak.)
- Collection: dense in `vectors_config={"dense": VectorParams(size=384, distance=COSINE)}`,
  sparse in the **separate** `sparse_vectors_config={"sparse": SparseVectorParams(modifier=
  Modifier.IDF)}`.
- Hybrid query: `query_points(prefetch=[Prefetch(query=Document(...), using="dense",
  limit=k), Prefetch(query=Document(..., model="Qdrant/bm25", options={"avg_len":...}),
  using="sparse", limit=k)], query=models.FusionQuery(fusion=models.Fusion.RRF), limit=k,
  with_payload=True)`. Read `res.points[i].payload` / `.score`.
- Filters: `query_filter=models.Filter(must=[FieldCondition(key="guideline",
  match=MatchValue(value=...))])`; optional payload indexes on guideline/source/section.
- Async: `AsyncQdrantClient` mirrors sync (await the same calls). FastEmbed compute is
  CPU-bound/sync under the hood — index once at startup; single-query embed is fine (wrap in
  `run_in_executor` only if latency shows it).
- Liveness: unauthenticated `GET http://<host>:6333/readyz` (or `/livez`); client-level
  `get_collections()` also validates auth.

**Cohere rerank** — `AsyncClientV2()` (reads `CO_API_KEY`; kwarg is `api_key=`). Construct
once at app startup, `await client.close()` on shutdown (holds an httpx pool). Call:
`await co.rerank(model="rerank-v4.0-fast", query=q, documents=[c.text for c in cands],
top_n=n)`; `documents` takes `list[str]` directly in v2. Response `.results` is sorted
best-first; each result has `.index` (into the input list) + `.relevance_score` — map back
to candidates by `.index`. Probe via `check_api_key()` (fallback `models.list(page_size=1)`)
— never a live rerank (billable). Our short chunks are well under the 500-token
billing-split threshold; each rerank call = 1 search unit.

---

## 5. `/ready` (W2_ARCHITECTURE §10, engineering requirement)

Extend `health.py` `DependencyName` + `check_readiness` with two probes:
- **Qdrant** — cheapest liveness against the private URL (`/readyz` or `get_collections`);
  *degraded* if unreachable.
- **Cohere** — cheapest reachability that does not burn a rerank search unit; *degraded* if
  unreachable.

`/ready` returns 200 only when all probes pass; the report is the 503 body otherwise
(existing pattern). In `retrieval_mode=fixture`, retrieval is served in-process, so both probes
report **`ok=true` with detail `"fixture mode"`** (not a network call) — local/offline `/ready`
stays green. In live mode a missing URL/key reports `not configured` and an unreachable
endpoint reports `unreachable`, both `ok=false` (fail closed → 503).

---

## 6. Testing strategy

- **Unit** (isolated, no network): `Citation`/`GuidelineCitation` validation incl.
  discriminator; retriever result-shaping (fused+reranked candidates → `EvidenceSnippet` with
  correct `GuidelineCitation` mapping); the "reject evidence claim without chunk metadata"
  guardrail; indexer payload construction.
- **Integration** (local Docker Qdrant, Cohere stubbed or a tiny live call): index a small
  fixture corpus → hybrid query → (stub) rerank → assert a cited snippet comes back with the
  right chunk_id/section/source and that payload filters scope correctly.
- **`/ready`**: Qdrant + Cohere probes report reachable when up, degraded when down
  (following `test_ready.py`).
- Behavior-first per the testing rules; each non-trivial test documents the user-facing
  break it guards.

---

## 7. Deployment (Phase 4 — after local green; verified 2026 Railway facts)

1. **Deploy Qdrant** on Railway — official `qdrant` template *or* a raw pinned
   `qdrant/qdrant:<tag>` service (pin for reproducibility; templates track `latest`). Same
   project + environment as `copilot-agent` (private networking is per-environment).
2. **On the Qdrant service, set:**
   - `QDRANT__SERVICE__API_KEY=<strong key>` (required on all data-plane requests).
   - **`QDRANT__SERVICE__HOST=::`** — IPv6 dual-stack bind. **Without this, internal
     connections silently fail** (Railway private net is IPv6; Qdrant defaults to IPv4-only).
     This is the single most likely deploy failure.
   - Volume mounted at `/qdrant/storage` (template pre-mounts; add manually for raw image).
3. **On `copilot-agent`, set:** `QDRANT_URL=http://qdrant.railway.internal:6333` (plain
   `http` — Wireguard already encrypts; TLS would fail), `QDRANT_API_KEY=<same value>`,
   `COHERE_API_KEY=<key>`. Ports: REST 6333, gRPC 6334. `qdrant-client` (Python) needs no
   IPv6 flag — its socket layer resolves AAAA natively.
4. **Index the prod corpus** via the **content-correct indexer in the agent's start command**
   (`python -m copilot.rag.index` before uvicorn): upserts all chunks (deterministic ids →
   idempotent, refreshes edited chunks), recreates on a dense-dimension change. Runs inside the
   private net — no public exposure, self-heals on redeploy. Keep it out of the build (no
   private networking at build time). `/readyz`, `/livez`, `/healthz` are auth-exempt, so the
   probe needs no key.
5. **Confirm `/ready`** reports Qdrant + Cohere reachable (returns degraded if either down).
6. **Update `W2_ARCHITECTURE.md`** §10 (deployment) + §5 if pipeline detail changed
   (CLAUDE.md: keep architecture docs current in the same change).

**Deploy status (2026-07-14 — infra provisioned, deferred-index path).** The `qdrant`
service is live on Railway (project `agentforge-openemr`, env `production`): image
`qdrant/qdrant:v1.18.2`, `QDRANT__SERVICE__HOST=::` (IPv6 bind), volume `qdrant-volume` at
`/qdrant/storage`, `QDRANT__SERVICE__API_KEY` set (64-char), private-only at
`qdrant.railway.internal:6333` (no public domain). **Corpus not yet indexed** — it
auto-indexes at promotion via the start-command indexer (in-network, reads the key from the
container env). **Remaining promotion steps** (do when JOS-53 + JOS-56 promote to `main`):
set on `copilot-agent` — `QDRANT_URL=http://qdrant.railway.internal:6333`,
`QDRANT_API_KEY=${{qdrant.QDRANT__SERVICE__API_KEY}}` (Railway reference, no secret copy), and
`COHERE_API_KEY` (user-set); add `python -m copilot.rag.index` to the start command; confirm
`/ready` reports Qdrant + Cohere reachable.

---

## 8. Risks & mitigations

- **SDK drift** (qdrant-client/fastembed/cohere APIs changed since training cutoff) →
  mitigated by Phase-1 research scouts pinning the real 2026 surface before code.
- **PHI leakage to vendors** → the retrieval query is the clinical question + guideline text,
  never patient identifiers; assert no PHI in the retriever path; `no_phi_in_logs` eval
  rubric covers logs.
- **Private-only Qdrant is hard to index into** → resolved by the chosen indexing approach
  (§7.3); document it so re-indexing is reproducible.
- **Citation-union merge collision with JOS-56** → union is additive; coordinate on
  `schemas.py` (see §3.3 note).
- **Empty retrieval** → retriever returns "no supporting guideline found"; the answer must
  separate "record says X" from "no guideline evidence retrieved" rather than inventing one
  (W2_ARCHITECTURE §11).

## 9. Tracked follow-ups (out of scope here, don't lose)

- **Secrets as `SecretStr`.** All service secrets (`anthropic_api_key`, `langfuse_secret_key`,
  `qdrant_api_key`, `cohere_api_key`) are plain `str`. No call site dumps `Settings` today, so
  there is no active leak — but hardening all four to `pydantic.SecretStr` (masked in repr /
  `model_dump`, `.get_secret_value()` at the few consumption points) removes the latent risk
  by construction. Do it as one consistent change across all secrets, not piecemeal.
- **Citation-union convergence.** Migrate the Week-1 FHIR `SourceRef` into a `fhir` arm of the
  `Citation` union and route the grounding gate by `source_type` (§3.3) — naturally forced when
  document extraction lands.
