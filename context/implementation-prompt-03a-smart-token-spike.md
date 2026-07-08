# Implementation Prompt 03a — SMART patient-scoped token spike (de-risk the load-bearing assumption)

> **How to use this file.** Hand it to a fresh Claude Code session. This is a **spike**, not
> a production increment: the goal is to *learn and prove*, and the code is throwaway. It is
> sequenced **before** the full PHP module (`-03b`) on purpose — it validates the single
> assumption the whole architecture rests on, using the least code that can prove it.

---

## 0. Why this spike exists (read this first)

`ARCHITECTURE.md` [§5 Authorization & trust boundaries](../ARCHITECTURE.md#5-authorization--trust-boundaries)
makes one load-bearing claim: the PHP module can **mint a SMART `patient/*.read` token
scoped to one patient**, the agent reads FHIR under that token, and because the token binds a
single patient, the IDOR gap is *physically unreachable through the agent*. Everything —
the authorization model, the "no DB creds" data path (§4), the "one PHI→LLM seam" (§9) —
depends on that token flow actually existing and working the way the doc assumes.

**We have not proven it yet.** It is also the **highest-uncertainty work in the project**:
OAuth2 client registration, SMART scopes, the patient-launch context, and OpenEMR's
extension internals are the least-familiar surface. Deferring it to the end (behind the
Python agent, which is the comfortable part) is the classic sequencing mistake — you'd
discover a broken foundational assumption with no time to react. This spike pulls that risk
forward.

**The one question to answer:** *Can we obtain a bearer token, scoped to a single patient,
that successfully reads that patient's `Patient` FHIR resource — and get denied when we try
to read a different patient?* If yes, the architecture stands. If no, we learn now, while
it's cheap to change.

## 1. Read first

1. `ARCHITECTURE.md` §4 (data-access model) and §5 (authorization) — the assumption under test.
2. `FHIR_README.md` — OpenEMR's FHIR R4 endpoints, base URL, and the SMART/OAuth2 setup
   (registration, authorize, token endpoints; scope syntax; how patient context is granted).
3. `API_README.md` — the REST/OAuth2 surface, client registration, and any enablement steps
   (API on/off globals, client approval/trust, allowed grant types).
4. `context/agent-workflow.md` — the five FHIR reads the agent ultimately needs (this spike
   only exercises `Patient`, but confirm the scopes you'd need for the full set).
5. The audit's note (`AUDIT.md`, and `ARCHITECTURE.md` §2) that the FHIR/OAuth2 path is the
   *strong* half of OpenEMR — `league/oauth2-server`, fail-closed default-deny, token-embedded
   scope enforcement, `ScopeRepository::finalizeScopes` anti-escalation. You are riding this
   path; understand what it enforces.

Ground everything in the **actual OpenEMR code and the running dev stack**, not assumptions —
the FHIR/OAuth internals live in `src/RestControllers/`, `src/Common/Auth/OpenIDConnect/`,
and `src/Common/Http/`. When a doc and the code disagree, the code wins; note it.

## 2. What to prove (acceptance — the spike is done when…)

Against the **running OpenEMR dev stack** (`docker/development-easy`, or a worktree stack via
`openemr-cmd`) with the seed patient data:

1. **A client is registered** with OpenEMR's OAuth2 server requesting SMART `patient/*.read`
   (at least `patient/Patient.read`) — document the exact registration call and any manual
   enablement/trust/approval step OpenEMR requires (these are the steps `-03b` automates).
2. **A patient-scoped bearer token is minted** for one specific seed patient. Document the
   exact grant/flow that produces a token carrying patient context (SMART launch context vs.
   authorization_code vs. whatever OpenEMR actually supports for `patient/` scopes) — this is
   the crux; record precisely how the "one patient" binding is established in the token.
3. **A FHIR read succeeds:** `GET /apis/default/fhir/Patient/{id}` with that token returns the
   seed patient's resource. Capture the raw JSON (it becomes a fixture for `-01`'s FHIR client).
4. **The negative case is proven:** attempting to read a *different* patient's resource with
   the same token is **denied** (or scoped out). This is the actual security claim — without it
   the "IDOR unreachable" argument in §5 is unverified. Capture the response.
5. **Findings are written up** in `context/smart-token-spike-findings.md`: the working flow
   step by step, every enablement/config prerequisite, what OpenEMR does vs. what §5 assumed,
   and any correction §5 or `-03b` needs. Include the reproducible commands.

## 3. Scope discipline (this is a spike — do NOT build)

- **No production code, no `oe-module-ai-copilot` module, no UI.** Throwaway scripts
  (`tmp/`, curl, a short PHP or Python script) are fine and expected.
- **No agent service changes.** This spike feeds `-01`/`-03b` via findings + a fixture JSON;
  it does not touch `/agent/`.
- **No core patches.** If proving the flow seems to *require* a core change, that itself is a
  finding — stop and report it; it would contradict `ARCHITECTURE.md` §3's "zero core patches."
- Don't gold-plate the token flow (refresh, rotation, error paths) — prove the happy path
  plus the one negative (cross-patient denied). Robustness is `-03b`'s job.

## 4. Surface, don't silently resolve — likely discoveries to flag

These would change the architecture, so raise them rather than working around them:

- **`patient/` scopes need an EHR-launch context OpenEMR only grants interactively**, and a
  standalone/programmatic mint isn't straightforward. If so, `-03b`'s "module mints the token"
  design needs the specific mechanism spelled out — capture exactly how the module would do it
  from an authenticated session (the SMART EHR-launch path is the likely answer; confirm it).
- **The negative case is *not* actually denied** (a `patient/*.read` token can reach another
  patient). That contradicts §5's core claim — flag immediately; it's the most important
  possible finding.
- **Enablement requires manual admin steps** (enable API globals, approve/trust the client).
  Fine for the spike, but each becomes a documented prerequisite or an automated install step
  in `-03b` — list them.
- **The FHIR `Patient` resource is missing fields** `-01`'s `PatientDemographics` expects —
  note it as a data-quality finding and pin the real field set for `-01`'s contract.
- Anything in `FHIR_README.md`/`API_README.md` is stale vs. the actual OAuth2 code — the code
  wins; record the drift.

## 5. Output

- `context/smart-token-spike-findings.md` — the write-up (§2.5).
- A captured `Patient` FHIR JSON for the seed patient, saved where `-01`'s fixture client can
  consume it (coordinate the path with `context/implementation-prompt-01-walking-skeleton.md`
  §1.2 — `agent/tests/fixtures/`).
- A short recommendation: does `ARCHITECTURE.md` §5 stand as written, or need a correction
  before `-03b`? One paragraph, decision-grade.
