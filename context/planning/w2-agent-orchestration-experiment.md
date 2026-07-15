# Agent-Team + Workflow Orchestration Experiment (Week 2)

**Purpose:** A deliberate learning experiment in autonomous multi-agent orchestration
in Claude Code, run on a real Week-2 substrate (JOS-52 clinical-guideline corpus).
Captures the harness-engineering findings so the pattern is reusable for later
Week-2 workflows. Not a PRD deliverable itself; the corpus it produced
(`agent/src/copilot/rag/corpus/`) feeds JOS-53 (Qdrant hybrid RAG).

**Grounding:** Substrate = JOS-52. Custom agents live in `.claude/agents/`
(`guideline-researcher`, `corpus-chunker`, `citation-verifier`); the orchestration
script is `.claude/workflows/corpus-curation.js`. Ran in an isolated raw git
worktree (`feature/w2-guideline-corpus` off `qa/integration`), agent-only, no
OpenEMR/docker.

---

## What was built

A layered harness:

1. **Custom agent team** (`.claude/agents/`) — three reusable roles with scoped
   tools and guardrails: a researcher (finds one citable source per topic,
   extracts criteria/screening/monitoring statements), a chunker (writes
   layout-aware chunks with citation metadata to `<topic>.jsonl`), and an
   adversarial verifier (re-fetches the source and tries to *refute* each chunk;
   defaults to fail).
2. **Workflow orchestration** (`corpus-curation.js`) — `fan-out -> pipeline
   (research -> chunk -> parallel verify -> prune) -> synthesize`. No barrier
   between topics; each topic flows independently.
3. **Verification moved into the harness** (replacing human review): the
   `citation-verifier` stage plus a `prune` stage that drops any chunk that
   fails, so the persisted corpus contains verified chunks only.

## Result

**55 verified chunks across 8 topics** (afib 8, asthma 7, ckd 7, heart-failure 7,
hypertension 5, lipids 6, nsaid-safety 8, t2dm 7), each carrying
`{chunk_id, guideline, source, source_url, section, date, text}` — the citation
contract for JOS-53. Reproducible from the repo alone.

## The verifier earned its place

With no human gate, the adversarial verifier caught three *distinct* failure
modes across the runs — evidence the in-harness quality gate works:

- **Faithfulness drift:** a t2dm chunk omitted "Asian American" from the USPSTF
  earlier-age screening group — a subtle factual error, caught and pruned.
- **Guardrail violation:** a ckd chunk ("stop metformin ... don't restart for 48h"
  around contrast) faithfully matched KDIGO yet was correctly rejected as a
  *treatment directive*, which the persona may not surface. Proves the invariant
  ordering (can confirm faithfulness and still fail on guardrail).
- **Unverifiable source:** chunks citing paywalled publishers (ADA
  `diabetesjournals.org`, KDIGO PDF, ACC/AHA `ahajournals.org`) that 403 on fetch
  were failed rather than trusted.

## Harness-engineering lessons (the durable findings)

1. **Context stayed flat by construction.** ~6.3M subagent tokens were processed
   entirely out of the main conversation; only compact per-run summaries (counts +
   failure reasons) ever reached main. Levers that did the work: main orchestrates
   and never reads; agents return schemas not prose; chunk payloads written to disk
   with only paths/manifests returned; a synthesize/reduce stage collapsed N results
   to one.

2. **Workflow resume caching is PREFIX-based, not per-call.** It caches "the longest
   unchanged prefix of `agent()` calls." Because a `pipeline()` interleaves topics
   *concurrently*, editing an early-scheduled agent (a t2dm/ckd prompt) invalidated
   the cache for everything scheduled after it — a "targeted" resume re-ran all 77
   agents, zero cache benefit. **Corollary:** targeted resume only saves tokens if
   the changed calls come last. For genuinely targeted re-curation, run a **scoped
   fresh workflow** (`args = [oneTopic]`) instead — the afib fix was 11 agents /
   411K tokens vs a full ~2.9M-token re-run.

3. **Researcher agents are non-deterministic in source selection.** A full re-run
   flipped afib from 7 verified chunks to 3 because the researcher re-picked a
   paywalled source. Don't rely on re-running to be idempotent; **pin fetchable
   source domains** in the prompt (NIH/NCBI/USPSTF/CDC/NKF over society-journal
   paywalls) and let the verifier + prune catch residuals.

4. **`args` arrives stringified.** The Workflow harness passed `args` as a
   JSON-encoded string, so an `Array.isArray(args)` guard silently fell through
   (ran all 8 topics instead of the intended 2-topic slice). Parse defensively:
   `typeof args === 'string' ? JSON.parse(args) : args`.

5. **Freshly-written `.claude/agents/` are not resolvable mid-session.** The agent
   registry loads at session start, so `agentType: 'guideline-researcher'` isn't
   available in the session that authored it. This run carried each role's contract
   *inline in the `agent()` prompt* (good self-contained-script practice anyway);
   the committed agent files activate as `agentType` in the next session.

6. **Synthesize from on-disk truth, not just the run's results.** Making the README
   step scan the corpus dir (rather than only this run's topics) let a scoped
   single-topic run still emit a complete all-8 README instead of clobbering it.

## Cost

Three runs: full-8 (71 agents / 2.8M tok), full resume that busted cache
(77 / 2.9M), scoped afib fix (11 / 0.4M). ~6.1M subagent tokens total. The lesson-2
insight (scoped fresh runs over full resumes) is the main cost lever going forward.

## Reuse

Regenerate with the Workflow tool over `.claude/workflows/corpus-curation.js`
(pass topic slugs as `args`, or none for all). Extend the team with a
coverage-critic role and dual-source cross-verification for a higher-assurance
second pass (see the experiment plan's "deepen" option).
