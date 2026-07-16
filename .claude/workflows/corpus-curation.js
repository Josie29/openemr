export const meta = {
  name: 'corpus-curation',
  description: 'Curate + chunk a small clinical-guideline corpus (JOS-52): research one authoritative source per topic, chunk with citation metadata, adversarially verify each chunk vs its source, synthesize a coverage report.',
  phases: [
    { title: 'Discover', detail: 'select grounded guideline topics from args' },
    { title: 'Curate', detail: 'research one source + write chunks, per topic' },
    { title: 'Verify', detail: 'adversarially verify each chunk against its source' },
    { title: 'Synthesize', detail: 'coverage report + corpus README' },
  ],
}

// corpus-curation: JOS-52 clinical-guideline corpus generator.
// Agent team: guideline-researcher -> corpus-chunker -> citation-verifier (adversarial),
// then prune failed chunks and synthesize the README from on-disk truth.
// Invoke via the Workflow tool: pass topic slugs as `args` for a subset, or none for all.
// REPO_ROOT is '.' because workflow subagents run with cwd = repo checkout root.
const REPO_ROOT = '.'
const CORPUS_REL = 'agent/src/copilot/rag/corpus'

const TOPICS = [
  { slug: 'hypertension', name: 'Hypertension management', focus: 'BP diagnostic thresholds, screening intervals, BP targets by risk group, monitoring cadence. Grounded in seed patients Reyes + Okonkwo (essential hypertension).' },
  { slug: 't2dm', name: 'Type 2 diabetes management and screening', focus: 'screening criteria/intervals, A1c diagnostic + target thresholds, monitoring. Grounded in Reyes + Okonkwo (T2DM on metformin).', source_hint: 'The primary ADA Standards of Care on diabetesjournals.org is paywalled and returns HTTP 403 to automated fetches. Use instead a publicly fetchable open-access source that returns real content (verify with WebFetch BEFORE committing to it): prefer USPSTF "Screening for Prediabetes and Type 2 Diabetes" (uspreventiveservicestaskforce.org), NIDDK (niddk.nih.gov), CDC (cdc.gov), or an NCBI/PMC-hosted rendering, for screening criteria/intervals, A1c diagnostic thresholds, and monitoring.' },
  { slug: 'lipids', name: 'Lipid management / statin therapy', focus: 'ASCVD risk stratification, lipid screening intervals, criteria for when statin therapy is indicated (criteria, NOT dosing). Grounded in Reyes + Okonkwo (hyperlipidemia).' },
  { slug: 'afib-anticoagulation', name: 'Atrial fibrillation and anticoagulation', focus: 'CHA2DS2-VASc stroke-risk stratification, HAS-BLED bleeding risk, criteria for when anticoagulation is indicated, INR monitoring criteria. Grounded in Okonkwo (AFib on warfarin + aspirin).', source_hint: 'The ACC/AHA 2023 AF guideline on ahajournals.org returns HTTP 403 to automated fetches. Use instead a publicly fetchable open-access source that returns real content (verify with WebFetch BEFORE committing): prefer NCBI StatPearls / Bookshelf (ncbi.nlm.nih.gov/books), CDC (cdc.gov), or an NCBI/PMC-hosted rendering, for CHA2DS2-VASc stroke-risk stratification criteria, HAS-BLED / bleeding-risk criteria, and INR monitoring targets. Criteria / monitoring / classification only.' },
  { slug: 'heart-failure', name: 'Heart failure management', focus: 'HF classification/staging (NYHA class, ACC/AHA stages A-D), diagnostic criteria, monitoring. Grounded in Okonkwo (CHF). Criteria/classification only, NOT drug dosing.' },
  { slug: 'ckd', name: 'Chronic kidney disease (stage 3)', focus: 'CKD staging by GFR/albuminuria criteria, monitoring cadence, nephrotoxin/NSAID avoidance guidance. Grounded in Okonkwo (CKD stage 3).', source_hint: 'The KDIGO 2024 CKD guideline PDF on kdigo.org returns HTTP 403 to automated fetches. Use instead a publicly fetchable open-access source that returns real content (verify with WebFetch BEFORE committing to it): prefer NIDDK (niddk.nih.gov), National Kidney Foundation (kidney.org), or an NCBI/PMC-hosted rendering, for CKD staging (CGA / GFR G1-G5 / albuminuria A1-A3 criteria), monitoring cadence, and nephrotoxin/NSAID-avoidance criteria. Extract criteria/classification/monitoring ONLY -- do NOT extract medication-management directives such as stopping/restarting metformin around iodinated contrast.' },
  { slug: 'asthma', name: 'Asthma management (GINA)', focus: 'GINA severity/control classification and step-assessment criteria, monitoring/review cadence. Grounded in seed patient Nakamura + demo patient Sergio (mild intermittent asthma).' },
  { slug: 'nsaid-safety', name: 'Drug-allergy and NSAID safety / medication reconciliation', focus: 'allergy cross-reactivity criteria (aspirin/NSAID, sulfa, penicillin), medication-reconciliation guidance, criteria for flagging drug-allergy conflicts. Grounded in Sergio (aspirin allergy yet prescribed ibuprofen/naproxen), Reyes (penicillin), Okonkwo (sulfa/codeine). Serves med-reconciliation use case UC-4.', source_hint: 'The NICE CG183 drug-allergy guideline on nice.org.uk returns HTTP 403 to automated fetches, so anchor_quote cannot be backfilled from it (JOS-85) — do NOT use it. Use instead a publicly fetchable open-access source that returns real content to a plain fetch (verify with WebFetch BEFORE committing): prefer NCBI StatPearls / Bookshelf (ncbi.nlm.nih.gov/books) or a PMC-hosted rendering — e.g. "Penicillin Allergy" (NBK459320, verified fetchable) for penicillin cross-reactivity, plus a StatPearls/PMC article on NSAID/aspirin hypersensitivity cross-reactivity for the aspirin/NSAID side. Extract cross-reactivity criteria, drug-allergy documentation, and medication-reconciliation criteria ONLY — no dosing directives.' },
]

const GUARDRAILS = [
  'HARD GUARDRAILS (a violation makes the chunk unusable):',
  '- NO dosing or treatment directives. Do not extract "give/start/titrate drug X at N mg" or prescribing instructions. The clinical persona is forbidden from making dosing recommendations. Curate toward WHEN to screen, WHAT defines/classifies the condition, WHAT to monitor and how often, HOW it is staged/risk-stratified.',
  '- COPYRIGHT/FAIR USE: store citation metadata + SHORT verbatim quotes only (roughly 1-3 sentences). Never reproduce whole sections.',
  '- NO PHI: public guideline text only; no patient identifiers or invented clinical values.',
  '- NO INVENTION: every statement must be traceable to a verbatim quote from the source.',
].join('\n')

const RESEARCH_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    topic: { type: 'string' },
    source: {
      type: 'object', additionalProperties: false,
      properties: {
        title: { type: 'string' }, publisher: { type: 'string' }, url: { type: 'string' },
        year: { type: 'string' }, source_id: { type: 'string', description: 'short stable slug e.g. ada-soc-2025' },
      },
      required: ['title', 'publisher', 'url', 'year', 'source_id'],
    },
    statements: {
      type: 'array',
      items: {
        type: 'object', additionalProperties: false,
        properties: {
          section: { type: 'string' }, heading: { type: 'string' },
          quote: { type: 'string', description: 'short verbatim quote' },
          kind: { type: 'string', enum: ['criteria', 'screening', 'monitoring', 'classification'] },
        },
        required: ['section', 'heading', 'quote', 'kind'],
      },
    },
    note: { type: 'string', description: 'one-line caveat or empty string' },
  },
  required: ['topic', 'source', 'statements', 'note'],
}

const MANIFEST_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    topic: { type: 'string' },
    corpus_path: { type: 'string', description: 'repo-relative path written' },
    chunk_count: { type: 'integer' },
    chunk_ids: { type: 'array', items: { type: 'string' } },
    metadata_complete: { type: 'boolean' },
    note: { type: 'string' },
  },
  required: ['topic', 'corpus_path', 'chunk_count', 'chunk_ids', 'metadata_complete', 'note'],
}

const VERDICT_SCHEMA = {
  type: 'object', additionalProperties: false,
  properties: {
    chunk_id: { type: 'string' },
    verdict: { type: 'string', enum: ['pass', 'fail'] },
    failed_invariant: { type: 'string', enum: ['faithful', 'guardrail', 'metadata', 'phi', ''] },
    reason: { type: 'string' },
  },
  required: ['chunk_id', 'verdict', 'failed_invariant', 'reason'],
}

const researchPrompt = (t) => {
  const parts = [
    'ROLE: clinical-evidence librarian curating a guideline corpus for a family-medicine primary-care Co-Pilot. Your final message IS structured data, not prose.',
    `TOPIC: ${t.name} (slug: ${t.slug}).`,
    `FOCUS: ${t.focus}`,
  ]
  // Only topics whose primary source is fetch-blocked carry a hint; absent for
  // all others so their prompt stays byte-identical and replays from cache.
  if (t.source_hint) parts.push(`SOURCE REQUIREMENT (overrides the general preference below): ${t.source_hint}`)
  parts.push(
    'TASK: Find ONE authoritative, publicly-citable source with a stable public URL. Prefer specialty-society guidelines (ACC/AHA, ADA Standards of Care, GINA, KDIGO, GOLD, ACR), USPSTF statements, or NIH/CDC clinical references. Reject blogs, secondary summaries, drug-marketing pages, and paywalled PDFs you cannot actually read. WebFetch and READ it, then extract 4-8 criteria/screening/monitoring/classification statements a PCP consults for pre-visit orientation, each with its section/heading and a SHORT verbatim supporting quote.',
    GUARDRAILS,
    'If no solid public source is available, return an empty statements array with an explanatory note rather than fabricating.',
    'Return {topic, source:{title,publisher,url,year,source_id}, statements:[{section,heading,quote,kind}], note}.',
  )
  return parts.join('\n\n')
}

const chunkPrompt = (research, t) => [
  'ROLE: mechanical corpus chunker. You shape and PERSIST retrieval chunks; you do not add clinical content. Final message IS a manifest, not prose.',
  `INPUT (researched guideline for topic "${t.slug}"):`,
  JSON.stringify(research),
  `TASK: Turn each statement into ONE self-contained chunk (verbatim quote plus, if needed, a short framing clause so it reads standalone; layout-aware, do not merge unrelated statements or split a criterion mid-thought). Build each chunk object with ALL fields: chunk_id (stable slug "<source_id>-<section-slug>-<NN>" zero-padded), guideline ("${t.slug}"), source (the source_id), source_url (the source url), section (the statement section), date (the source year), text (the chunk text). Then WRITE all chunks as JSONL (one JSON object per line, UTF-8, newline-terminated) to the ABSOLUTE path ${REPO_ROOT}/${CORPUS_REL}/${t.slug}.jsonl, creating parent dirs if needed. Overwrite any existing file (idempotent).`,
  'RULES: never invent clinical content (text derives from the quotes); keep chunks short (fair use); every chunk MUST have every field non-empty. If any chunk is missing a field, set metadata_complete=false and explain in note.',
  `Return {topic:"${t.slug}", corpus_path:"${CORPUS_REL}/${t.slug}.jsonl", chunk_count, chunk_ids:[...], metadata_complete, note}. Do NOT echo chunk payloads.`,
].join('\n\n')

const verifyPrompt = (manifest, chunkId) => [
  'ROLE: skeptical fact-checker. Default stance is REJECTION: a chunk passes only if you positively confirm all four invariants. You replace human review, so err toward failing. Final message IS a verdict object.',
  `TASK: Read the chunk whose chunk_id is "${chunkId}" from the JSONL file at ${REPO_ROOT}/${manifest.corpus_path} (Read or Grep for that line; parse its JSON). Then verify, stopping at the first failure:`,
  '1. faithful — WebFetch the chunk\'s source_url and confirm the chunk text is genuinely supported by the cited section, with no invention/overstatement/drift. If you cannot fetch the source or cannot locate supporting text, FAIL.',
  '2. guardrail — must be criteria/screening/monitoring/classification content, NOT a dosing or treatment directive. A dosing directive is an automatic FAIL.',
  '3. metadata — every field (chunk_id, guideline, source, source_url, section, date, text) present and non-empty, and section specific enough to resolve a citation (not just the document title).',
  '4. phi — no patient identifiers, real names, or invented clinical values.',
  'Return {chunk_id, verdict:"pass"|"fail", failed_invariant:"faithful"|"guardrail"|"metadata"|"phi"|"" , reason}. reason = one concise sentence citing specific evidence.',
].join('\n\n')

const prunePrompt = (absPath, failedIds) => [
  'ROLE: mechanical file finalizer. You remove rejected chunks so the persisted corpus contains verified chunks only. Final message is a one-line status, no prose.',
  `TASK: Read the JSONL file at ${absPath}. Remove every line whose "chunk_id" is in this list: ${JSON.stringify(failedIds)}. Rewrite the file IN PLACE with only the remaining lines (valid JSONL, one object per line, newline-terminated, original order preserved). Do not alter surviving lines.`,
  'Return exactly "pruned <N>" where N is the number of lines removed, or a one-line error string.',
].join('\n\n')

const buildReadme = (summary) => {
  const lines = []
  lines.push('# Clinical-Guideline Corpus (Week 2 — JOS-52)')
  lines.push('')
  lines.push('Small, static, in-repo corpus of clinical-practice-guideline chunks feeding the hybrid-RAG retriever (JOS-53, Qdrant). Reproducible from this repo alone. Each chunk carries `{chunk_id, guideline, source, source_url, section, date, text}` so retrieval hits resolve back to a citation (`source` -> `source_id`, `section` -> `page_or_section`, `chunk_id` -> `field_or_chunk_id`).')
  lines.push('')
  lines.push('Curated toward criteria / screening / monitoring / classification content only — NO dosing or treatment directives (persona guardrail). Every chunk was adversarially verified against its cited source; chunks that failed verification were pruned, so the persisted corpus contains verified chunks only.')
  lines.push('')
  lines.push('## Coverage')
  lines.push('')
  lines.push('| Topic | File | Verified chunks | Rejected + pruned | Metadata complete |')
  lines.push('| --- | --- | --- | --- | --- |')
  for (const s of summary) {
    lines.push(`| ${s.topic} | \`${s.topic}.jsonl\` | ${s.kept} | ${s.pruned} | ${s.metadata_complete ? 'yes' : 'NO'} |`)
  }
  lines.push('')
  const anyFail = summary.some(s => s.failed > 0)
  if (anyFail) {
    lines.push('## Rejected chunks (failed adversarial verification, pruned from corpus)')
    lines.push('')
    for (const s of summary) {
      for (const f of s.failures) {
        lines.push(`- **${f.chunk_id}** (${s.topic}) — failed \`${f.invariant}\`: ${f.reason}`)
      }
    }
    lines.push('')
  }
  lines.push('## Regeneration')
  lines.push('')
  lines.push('Regenerate via the `corpus-curation` workflow (custom agents in `.claude/agents/`: guideline-researcher, corpus-chunker, citation-verifier). Pass topic slugs as workflow `args` to curate a subset, or none for all.')
  return lines.join('\n')
}

// ---- orchestration ----
// args may arrive as a real array or a JSON-encoded string (harness stringifies);
// normalize both. Empty/absent -> curate all topics.
let topicArg = args
if (typeof topicArg === 'string') {
  try { topicArg = JSON.parse(topicArg) } catch (e) { topicArg = [] }
}
const selected = (Array.isArray(topicArg) && topicArg.length)
  ? TOPICS.filter(t => topicArg.includes(t.slug))
  : TOPICS

phase('Discover')
log(`Curating ${selected.length} topic(s): ${selected.map(t => t.slug).join(', ')}`)

phase('Curate')
const results = await pipeline(
  selected,
  (t) => agent(researchPrompt(t), { label: `research:${t.slug}`, phase: 'Curate', schema: RESEARCH_SCHEMA }),
  (research, t) => agent(chunkPrompt(research, t), { label: `chunk:${t.slug}`, phase: 'Curate', schema: MANIFEST_SCHEMA, effort: 'low' }),
  (manifest, t) => parallel((manifest.chunk_ids || []).map(cid => () =>
    agent(verifyPrompt(manifest, cid), { label: `verify:${cid}`, phase: 'Verify', schema: VERDICT_SCHEMA })
  )).then(verdicts => ({ topic: t.slug, manifest, verdicts: verdicts.filter(Boolean) })),
  // Prune: rewrite each file to keep only verified-pass chunks. No agent spawned
  // when a topic has zero failures (keeps cached-topic re-runs free on resume).
  async (verified, t) => {
    const failedIds = verified.verdicts.filter(v => v.verdict === 'fail').map(v => v.chunk_id)
    if (!failedIds.length) return { ...verified, kept: verified.manifest.chunk_count, pruned: 0 }
    const absPath = `${REPO_ROOT}/${verified.manifest.corpus_path}`
    const status = await agent(prunePrompt(absPath, failedIds), { label: `prune:${t.slug}`, phase: 'Synthesize', effort: 'low' })
    return { ...verified, kept: verified.manifest.chunk_count - failedIds.length, pruned: failedIds.length, prune_status: status }
  },
)

const clean = results.filter(Boolean)
const summary = clean.map(r => {
  const failedV = r.verdicts.filter(v => v.verdict === 'fail')
  return {
    topic: r.topic,
    corpus_path: r.manifest.corpus_path,
    curated: r.manifest.chunk_count,
    kept: r.kept,
    pruned: r.pruned,
    metadata_complete: r.manifest.metadata_complete,
    passed: r.verdicts.filter(v => v.verdict === 'pass').length,
    failed: failedV.length,
    failures: failedV.map(f => ({ chunk_id: f.chunk_id, invariant: f.failed_invariant, reason: f.reason })),
  }
})

// Synthesize from ON-DISK truth (all *.jsonl in the corpus dir), so a scoped
// run (e.g. one topic) still produces a complete all-topics README rather than
// clobbering it with only this run's topics.
phase('Synthesize')
// Deep-link data (JOS-85): now that the corpus is final on disk, backfill a verbatim `anchor_quote`
// onto every chunk so the sidebar's "View source" can text-fragment to the exact passage. It is
// deterministic (longest common substring vs each fetched source), so it runs as one Bash step, not
// an LLM task. Best-effort — a source that blocks the fetch just leaves that chunk without an
// anchor (the sidebar falls back to a plain link), which must not fail the curation run.
const anchoring = await agent(
  'ROLE: corpus anchor step. Run this exact command from a shell and report its final summary line '
  + `verbatim as your entire reply:\n\n    cd ${REPO_ROOT} && make corpus-anchors\n\n`
  + 'It writes a verbatim anchor_quote into the corpus .jsonl files for deep-linking.',
  { label: 'anchor:corpus', phase: 'Synthesize', agentType: 'general-purpose', effort: 'low' },
)
log(`Anchor backfill — ${anchoring}`)

const thisRunFailures = summary.flatMap(s => s.failures.map(f => ({ topic: s.topic, ...f })))
const synth = await agent([
  'ROLE: corpus finalizer. You scan the on-disk corpus and (re)write its README from disk truth. Final message is a compact JSON object, no prose.',
  'TASK:',
  `1. List every *.jsonl file in ${REPO_ROOT}/${CORPUS_REL}/ . For each, count its lines (= verified chunks kept); the topic is the filename without the .jsonl extension.`,
  `2. Write ${REPO_ROOT}/${CORPUS_REL}/README.md with EXACTLY these sections:`,
  '   - H1: "# Clinical-Guideline Corpus (Week 2 — JOS-52)"',
  '   - Intro paragraph: a small, static, in-repo corpus of clinical-practice-guideline chunks feeding the hybrid-RAG retriever (JOS-53, Qdrant), reproducible from this repo alone; each chunk carries {chunk_id, guideline, source, source_url, section, date, text, anchor_quote} feeding the citation contract (source -> source_id, section -> page_or_section, chunk_id -> field_or_chunk_id); anchor_quote is a verbatim source span backfilled for "View source" deep-linking (JOS-85).',
  '   - Note paragraph: curated toward criteria / screening / monitoring / classification content only, NO dosing or treatment directives (persona guardrail); every chunk was adversarially verified against its cited source; chunks that failed verification were pruned, so only verified chunks persist.',
  '   - "## Coverage": a Markdown table with columns | Topic | File | Verified chunks | — one row per file sorted alphabetically, then a Total row.',
  `   - "## Rejected chunks (failed adversarial verification, pruned) — latest run": render each of these as "- **<chunk_id>** (<topic>) — failed \`<invariant>\`: <reason>". If the list is empty, write "None in the latest run." The list: ${JSON.stringify(thisRunFailures)}`,
  '   - "## Regeneration": regenerate via the corpus-curation workflow (custom agents in .claude/agents/: guideline-researcher, corpus-chunker, citation-verifier); pass topic slugs as workflow args for a subset, or none for all.',
  '3. Return {total_chunks:<int>, per_topic:{"<topic>":<int>, ...}} reflecting the on-disk line counts.',
].join('\n'), { label: 'synthesize:readme', phase: 'Synthesize' })

return {
  scope: selected.map(t => t.slug),
  corpus: synth,
  this_run: summary.map(s => ({ topic: s.topic, curated: s.curated, kept: s.kept, pruned: s.pruned })),
}
