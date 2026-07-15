# VLM / Document-Extraction — Week-2 Decision Evidence (JOS-47)

**Purpose:** Working analysis behind the Week-2 Architecture Defense (`W2_ARCHITECTURE.md`
§3, "extraction approach"). Decides **which model/approach turns a scanned lab PDF or
intake form into strict-schema JSON with per-field source citations and a PDF
bounding-box overlay**. Decision evidence, not a deliverable — `W2_ARCHITECTURE.md` /
`ARCHITECTURE.md` remain the source of truth.

**Grounding:** [`PRD-week-2.md`](../../PRD-week-2.md) Core Reqs 1–2 (ingestion + strict
`lab_pdf`/`intake_form` schemas), Core Req 5 (citation contract
`{source_type, source_id, page_or_section, field_or_chunk_id, quote_or_value}` **plus a
required visual PDF bounding-box overlay**), Core Req 7 (extraction confidence), Core Req 6
(`schema_valid` / `citation_present` / `factually_consistent` eval rubrics), and the "Vision
extraction **without invention**" mandate. Stack + BAA posture from
[`agent-tech-stack.md`](agent-tech-stack.md) (Claude tiered, Pydantic AI, Langfuse, BAA
assumed for all vendors) and [`agent-framework-week2.md`](agent-framework-week2.md) (the
intake-extractor worker; `@agent.output_validator` grounding gate is the crown jewel).
Model + doc-AI facts verified against 2026 sources (linked at the end).

---

## Fixed constraints (from Week 1 — don't re-litigate)

- **Agent orchestration stays Claude-native** — Pydantic AI + Anthropic for the supervisor,
  workers, and reasoning. The **document extractor is a separate concern** behind the schema
  boundary: a specialized doc-AI service, swappable without touching the agent.
- **Railway** deploy; **BAA assumed for every vendor**, so PHI-to-vendor is not a
  differentiator — picks are made on capability, fit, and simplicity.
- **The schema is the contract.** PRD Engineering Reqs: "Do not let raw VLM output bypass
  schema validation. The schema is the source of truth — not what the model happens to
  return." Whatever reads the page, its output is parsed into a strict Pydantic v2 model or
  it is rejected.

## The decisive technical fact (why this isn't just "use Claude vision")

Core Req 5 demands a **pixel-accurate PDF bounding-box overlay per field**, on top of strict
schema and per-field confidence. Two Anthropic-API constraints, both verified against the
current docs, make Claude vision the wrong tool for that specific job:

1. **Claude's Citations API grounds to page/section, not pixels.** For a PDF it returns
   `page_location` (start/end page); for text, `char_location`. It does **not** emit pixel
   bounding boxes — so it cannot, by itself, drive the required overlay.
2. **Citations are incompatible with structured outputs** (`output_config.format` +
   `citations: enabled` returns 400). You cannot get Claude's native citations **and**
   strict-schema JSON from the *same* call.

The previous pass tried to work around this by **splitting the job three ways** — Claude
vision for extraction, a local OCR pass for word boxes, and a fuzzy quote↔token match to glue
them. That is three moving parts and a string-alignment heuristic on the critical path to the
overlay. The clean resolution is the opposite: pick an extractor **built for geometry**.
**Mistral OCR 4 schema mode returns typed fields + native pixel bounding boxes + per-word
confidence in one pass** — the three things Core Reqs 2, 5, and 7 ask for — with **no separate
OCR step and no fuzzy match**. Its raw output is still validated into the strict Pydantic
schema (the boundary holds), and the `output_validator` grounding gate still rejects any fact
that doesn't resolve to a source span.

---

## Contenders

| Approach | Strict Pydantic | Native **pixel bbox overlay** | Confidence signal | Messy-scan accuracy | Latency / cost | Stack fit |
|---|---|---|---|---|---|---|
| **Mistral OCR 4 — schema mode** *(pick)* | Raw output validated into the `lab_pdf`/`intake_form` model — schema *is* the boundary | **Native** — pixel bboxes per typed block, one pass | **Native** per-field / per-word confidence | Strong on tables, checkboxes, degraded scans | One managed API call, well under 15s; ~$5/1k pages | One extra external API (like Cohere); reuses the `output_validator` gate + Langfuse wiring |
| **Other geometry-native extractors** (AWS Textract · Azure Document Intelligence · Google Document AI · Landing AI ADE) *(near alternatives)* | Same adapter-into-schema step | **Native** field-level bboxes + confidence | Native | Equivalent class | Fast, cheap | Equivalent-class swap targets if Mistral disappoints |
| **Claude vision + local OCR + quote↔token match** *(rejected)* | Native (Claude in Pydantic AI) | Assembled — bbox from OCR-token match to Claude's quote | Two-signal (self-report + match score) | Strong semantics, but bbox rides a fragile match | One Claude call + local OCR pass | **Rejected:** bbox fragility + three-part complexity on the critical path |
| **Claude vision alone, coords as schema fields** | Native | In-schema model-emitted bbox | Model self-report only | Bbox "approximate, not pixel-perfect" — overlay drifts | Cheapest/simplest | Overlay fidelity is the risk |
| **Open VLM self-hosted** (Qw-VL-class) | Native-ish | Hand-built | Weak/none | Trails on clinical reasoning | GPU ops burden | Over-scoped for a 3-week sprint; BAA moots its only draw |

---

## Pick: **Mistral OCR 4 (schema mode) — native bbox + typed blocks + per-word confidence, one pass**

Each reason traces to a PRD requirement:

1. **The required bbox overlay is native, not assembled** (Core Req 5). Mistral returns each
   typed field with its **pixel bounding box + page** directly — no separate OCR pass, no
   quote↔token match, nothing fragile on the critical path to the overlay. This satisfies the
   overlay requirement in a single call and sidesteps both verified Claude-API limits
   (Citations = page-level only; Citations ⊥ structured output).

2. **The strict schema stays the contract** (Core Req 2 + "no raw VLM output bypasses the
   schema"). Mistral's raw output is validated into the `lab_pdf` / `intake_form` Pydantic
   models — the models are the boundary, not what the extractor returns. The
   `@agent.output_validator` grounding gate — the Week-1 crown jewel — attaches unchanged to
   the intake-extractor worker and **rejects any extracted fact that doesn't resolve to a
   source span**, which *is* the "Vision extraction without invention" mandate mechanically
   enforced.

3. **Confidence is a native per-field / per-word signal** (Core Req 7). Mistral emits
   confidence directly, so a low-confidence or unresolved field is exactly the unsupported
   case the overlay must surface and the gate can refuse — no derived match-score proxy.

4. **Simplicity.** One pass replaces the three-part Claude-vision + OCR + match pipeline: no
   local OCR service to run, no fuzzy string alignment to tune. The extractor is one managed
   API, wired like Cohere Rerank already is.

5. **Cost/latency.** Fast and cheap (~$5/1k pages), comfortably inside the <15s budget.

---

## When I'd switch

Mistral is a doc-AI OCR engine, strongest on *structured* pages (lab tables, form fields).
Its weaker axis is **semantic normalization of free-text intake** — unit/reference-range
inference, abnormal-flag reasoning, free-text chief concern — where a reasoning VLM would do
better. **Switch if the eval gate regresses** (`schema_valid` / `factually_consistent` /
`citation_present`) on the **free-text intake** path. A possible fallback to revisit then is
Claude vision for the reasoning-heavy fields — but it is **not built now**; the swap stays
contained because extraction is kept behind the typed schema boundary.

---

## The single biggest risk of this pick (state it in the defense)

**Mistral's semantic normalization on free-text intake is weaker than a reasoning VLM's.** An
OCR-first extractor excels at localized structured fields but may under-normalize free-text
(e.g. mapping a messy chief-concern narrative or an unlabeled unit). **Mitigation:** the
strict schema + `output_validator` gate still refuse any unsupported field rather than guess,
and the `factually_consistent` / `schema_valid` rubrics on the intake cases are the tripwire.
If the intake path fails the eval bar, that is the signal to revisit a reasoning-VLM fallback
(above), not a silent degradation.

---

## Sources (2026, verified)

> **Verify at build:** re-confirm Mistral OCR 4 schema-mode capabilities (native bbox + typed
> blocks + per-word confidence) and current pricing against the live docs before wiring it in
> — the figures below are from 2026 launch coverage and may have moved.

- Dedicated doc-AI (native bbox + grounding): [Mistral OCR 4](https://mistral.ai/news/ocr-4/)
  ([MarkTechPost writeup](https://www.marktechpost.com/2026/06/23/mistral-ocr-4/) — schema
  mode: bbox + typed blocks + per-word confidence, ~$5/1k pages, self-hostable),
  [Landing AI ADE](https://landing.ai/) ([ADE APIs](https://landing.ai/llms) — per-field
  visual grounding/bboxes via DPT),
  [Azure Document Intelligence](https://azure.microsoft.com/en-us/products/ai-foundry/tools/document-intelligence),
  Google Document AI, AWS Textract (equivalent-class geometry-native extractors)
- Landscape (2026): [Lido — best document-extraction APIs for developers](https://www.lido.app/blog/best-document-extraction-apis-for-developers),
  [LlamaIndex — Landing AI alternatives](https://www.llamaindex.ai/insights/best-alternatives-to-landingai)
- Pydantic AI strict output + `output_validator` (the schema boundary + grounding gate):
  [Output — Pydantic Docs](https://pydantic.dev/docs/ai/core-concepts/output/)
- Why Claude vision is the *wrong* tool for the bbox job (rejected alternative): Anthropic
  [Citations](https://platform.claude.com/docs/en/build-with-claude/citations) (PDF →
  `page_location`, text → `char_location`; **no pixel bbox**),
  [Structured outputs](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)
  (Citations ⊥ `output_config.format` → 400),
  [Vision](https://platform.claude.com/docs/en/build-with-claude/vision) (coords ~1:1 with
  pixels but **approximate, not pixel-perfect**) — the fallback to revisit only if intake
  evals regress
