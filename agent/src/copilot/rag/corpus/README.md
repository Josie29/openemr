# Clinical-Guideline Corpus (Week 2 — JOS-52)

A small, static, in-repo corpus of clinical-practice-guideline chunks feeding the hybrid-RAG retriever (JOS-53, Qdrant), reproducible from this repo alone. Each chunk carries `{chunk_id, guideline, source, source_url, section, date, text}`, feeding the citation contract (`source` -> `source_id`, `section` -> `page_or_section`, `chunk_id` -> `field_or_chunk_id`).

Curated toward criteria / screening / monitoring / classification content only, with NO dosing or treatment directives (persona guardrail). Every chunk was adversarially verified against its cited source; chunks that failed verification were pruned, so only verified chunks persist.

## Coverage

| Topic | File | Verified chunks |
| --- | --- | --- |
| afib-anticoagulation | `afib-anticoagulation.jsonl` | 8 |
| asthma | `asthma.jsonl` | 7 |
| ckd | `ckd.jsonl` | 7 |
| heart-failure | `heart-failure.jsonl` | 7 |
| hypertension | `hypertension.jsonl` | 5 |
| lipids | `lipids.jsonl` | 6 |
| nsaid-safety | `nsaid-safety.jsonl` | 8 |
| t2dm | `t2dm.jsonl` | 7 |
| **Total** | | **55** |

## Rejected chunks (failed adversarial verification, pruned) — latest run

None in the latest run.

## Regeneration

Regenerate via the corpus-curation workflow (custom agents in `.claude/agents/`: guideline-researcher, corpus-chunker, citation-verifier). Pass topic slugs as workflow args for a subset, or none for all.
