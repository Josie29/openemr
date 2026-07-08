# Implementation Prompt 03b — oe-module-ai-copilot: widget + real SMART EHR-launch auth

> **How to use this file.** Hand it to a fresh Claude Code session rooted in the
> `feature/copilot-php-frontend` worktree (`/Users/josiemachalek/Projects/gauntlet-ai/projects/openemr-wt-feature-copilot-php-frontend`).
> This is the full `-03b` scope: both the chat widget UI *and* the real SMART
> EHR-launch token-minting flow the `-03a` spike proved works — not the
> interim-auth shortcut. Read `context/execution/implementation-prompt-03a-smart-token-spike.md`
> and `context/execution/smart-token-spike-findings.md` first; this prompt assumes
> that spike's findings as settled fact and does not re-derive them.

---

## 0. Why this exists, and the one open design question to resolve first

`ARCHITECTURE.md` §4/§5 and `context/decisions/deployment-strategy.md` (Option D,
selected) specify: UI + auth live in a thin PHP module
(`oe-module-ai-copilot`), agent logic lives entirely in the standalone Python
service (`agent/`, already built), and the module mints a patient-scoped SMART
token so the audit's IDOR finding is unreachable through the agent. The `-03a`
spike proved the mechanism works end to end — see
§4 of the findings doc for the precise correction: it's a **SMART EHR-launch
authorization_code flow** (first-party trusted client + launch context for the
open patient + skip-consent app setting), not a silent server-side mint.

**What the spike didn't resolve, and you need to decide early:** the EHR-launch
flow is a browser redirect dance (`/oauth2/default/authorize` → login/consent →
redirect back with a `code` → token exchange). Our widget is natively embedded
via `RenderEvent` — **no iframe**, by design (`deployment-strategy.md`'s stated
advantage over Option A/B). A full-page redirect would navigate the physician
away from the chart mid-task, which defeats the point of a persistent
in-chart panel.

**Recommended resolution (do this unless you find a better one):** run the
launch → authorize → callback → token-exchange chain inside a **hidden iframe
or popup**, not the top-level chart page. The widget's JS opens it, the
callback page (still inside the hidden frame) receives the token and relays it
to the parent page via `postMessage`, then closes/hides itself. This is a
standard SMART-app pattern for exactly this situation — resolve it this way
unless research turns up something clearly better; if you change it, note why
in your summary.

---

## 1. Read first

1. `ARCHITECTURE.md` §4 (data-access model) and §5 (authorization & trust
   boundaries) — the target design.
2. `context/decisions/deployment-strategy.md` — Option D, the selected
   architecture this module implements.
3. `context/execution/implementation-prompt-03a-smart-token-spike.md` +
   `context/execution/smart-token-spike-findings.md` — the proven mechanism,
   step by step, including the exact OAuth2 endpoints, required scopes, PKCE
   requirement, and the enablement prerequisites (§3 of the findings doc).
4. `agent/README.md` and `agent/src/copilot/schemas.py` — the `/chat` contract
   this widget calls: `ChatRequest{patient_id, message}` →
   `ChatResponse{summary, claims: [{text, source: {resource_type, resource_id,
   field, value}}]}`.
5. `interface/modules/custom_modules/oe-module-dashboard-context/` — reference
   module. Copy its `openemr.bootstrap.php` / `composer.json` / `src/Bootstrap.php`
   structure exactly; it already demonstrates the `RenderEvent` listener
   pattern (`src/Bootstrap.php` — note the pattern is direct
   `$eventDispatcher->addListener(...)` calls, **not**
   `EventSubscriberInterface`).
6. `src/Events/PatientDemographics/RenderEvent.php` — the event class. Mount
   point: dispatched from `interface/patient_file/summary/demographics.php` at
   `EVENT_SECTION_LIST_RENDER_AFTER` (and `EVENT_RENDER_POST_PAGELOAD` — check
   both call sites and pick whichever gives you a stable DOM anchor for the
   panel).
7. Before writing the OAuth callback route: grep the existing modules
   (`oe-module-weno`, `oe-module-comlink-telehealth` are good candidates) for
   how a module exposes a directly-URL-reachable PHP endpoint (for inbound
   webhooks/callbacks). Confirm the pattern before assuming one — this repo's
   module system may or may not route bare scripts under the module directory
   automatically.

---

## 2. Scope, in sections — sequencing and parallelization

**Section A — module skeleton (sequential, do first, blocks everything else).**
Create `interface/modules/custom_modules/oe-module-ai-copilot/` with
`openemr.bootstrap.php`, `info.txt`, `composer.json` (PSR-4 autoload under
`OpenEMR\Modules\AiCopilot\`, `extra.openemr` manifest), and a minimal
`src/Bootstrap.php` that registers (but doesn't yet implement) a
`RenderEvent` listener. Get the module recognized and loading before building
anything inside it — verify via OpenEMR's module manager UI or a log line
before moving on.

Once Section A is done, **Sections B and C are independent of each other** —
different files, no shared state, no data dependency in either direction. Spawn
them as **parallel subagents** (the `Agent` tool, `general-purpose` or
project-appropriate subagent type) rather than building them serially:

**Section B — widget UI + chat wiring (parallel track 1).**
- The `RenderEvent` handler renders the panel shell (HTML/CSS/JS asset
  enqueue) into the chart page.
- Chat JS calls the agent's `POST /chat` directly from the browser (per
  `deployment-strategy.md`'s design — not proxied through PHP) with
  `patient_id` = the open chart's pid and an `Authorization: Bearer <token>`
  header sourced from wherever Section C's token relay lands it (design the
  interface between B and C now, even though C isn't built yet — e.g., "the
  token is available on `window.aiCopilotToken` once the launch flow
  completes" — write it down and have both tracks target that contract).
- Render `ChatResponse.summary` and each `claims[].text` with its
  `claims[].source` as a visible citation (this is the grounding UI the
  verification gate's whole design exists to support — don't collapse it into
  plain text).
- Handle the no-token-yet and token-expired states explicitly (don't let the
  panel silently fail — show a "connecting..." or "re-authorize" state and
  trigger Section C's flow).

**Section C — SMART EHR-launch flow (parallel track 2).**
- A launch controller: on chart open (or on first widget interaction — your
  call, document which), generates a launch token carrying `patient=<pid>`
  per the findings doc §4, and redirects the hidden iframe/popup to
  `/oauth2/default/authorize` with `launch=<token>`, the scope set from the
  spike (`openid fhirUser online_access launch/patient patient/*.read`), PKCE
  S256 challenge, and `aud` = the FHIR base.
- A callback endpoint (see §1.7 above for how modules expose one) that
  receives `code`, exchanges it at `/oauth2/default/token` with
  `code_verifier` + the module's registered client credentials, and relays
  the resulting `access_token` (+ `expires_in`) back to the parent page via
  `postMessage`.
- Client registration + enablement + the "OAuth2 EHR-Launch Authorization
  Flow Skip" app setting (findings doc §3, §4): **document these as one-time
  manual admin prerequisites** (Administration → Config → Connectors) rather
  than building installer automation — out of scope for this pass, per the
  Non-goals below. State the exact steps in the module's README.
- Fail-closed on non-2xx per the findings doc's explicit warning: a
  cross-patient or otherwise denied FHIR read comes back as a bare HTTP 500,
  not a clean 403 — treat any non-2xx as a hard denial, don't special-case
  403/404.

**Section D — agent-side changes (small, but coordinate — see warning below).**
Two minimal edits to `agent/src/copilot/main.py` / `config.py`:
1. Add `CORSMiddleware` allowing the OpenEMR origin (confirmed missing —
   there is currently no CORS handling at all, so the browser will block
   Section B's fetch call outright without this).
2. `/chat` must accept and use a **per-request** bearer token (an
   `Authorization` header) for its FHIR calls, instead of only the
   statically-configured `COPILOT_FHIR_BEARER_TOKEN`. This is what makes
   §5's "IDOR unreachable through the agent" claim actually true in
   production — right now the agent uses one static token for every request
   regardless of who's asking, which does not enforce per-patient scoping.

> **⚠️ Cross-branch coordination required.** `agent/src/copilot/main.py` and
> `config.py` are also being actively developed in the *other* Claude Code
> session, on `feature/agent-walking-skeleton`, in the primary checkout. Keep
> this edit as small and isolated as possible (ideally: one middleware
> registration, one header-read + fallback-to-env-var change), call it out
> explicitly and separately in your summary/commit, and flag to the user that
> this will need to be reconciled with the other branch before either merges
> — don't let it get lost inside a large PHP-focused commit.

**Section E — integration + end-to-end verification (sequential, after B, C, D land).**
Wire B's token-consumption contract to C's actual `postMessage` payload shape.
Manually verify end to end: open a patient chart in the worktree's OpenEMR
(`https://localhost:9301`), confirm the widget mounts, confirm the launch flow
completes and a token lands in the widget, send a chat message, confirm the
response renders with citations, then confirm — per the spike's decisive
test — that reopening the panel for a **different** patient gets a token
bound to *that* patient, not a stale one.

---

## 3. Non-goals (explicitly deferred, don't build these here)

- Installer automation for OAuth client registration/enablement/skip-consent
  — documented manual admin steps only, this pass.
- Refresh-token / silent-renewal (`offline_access`) — the findings doc's
  1-hour `online_access` token is enough for one chart-orientation session;
  on expiry, re-run the launch flow.
- Any core OpenEMR patch — the findings doc confirmed zero core changes are
  needed; if you find yourself editing anything outside
  `interface/modules/custom_modules/oe-module-ai-copilot/` and the two
  flagged `agent/` files, stop and reconsider.
- Production-grade error UX, retry/backoff polish, or styling beyond
  "clearly usable" — this is a working demo for the sprint's checkpoints, not
  a polished release.
- The other four FHIR tools, faithfulness/domain verification, SSE
  streaming, or tiered model routing — those are `-02`, tracked separately
  and owned by the backend session.

## 4. Acceptance criteria

- [ ] Module loads and is recognized by OpenEMR's module manager
- [ ] Chat panel renders natively in the patient chart via `RenderEvent` (no iframe for the panel itself)
- [ ] EHR-launch flow runs in a hidden iframe/popup, not a top-level redirect — chart page never navigates away
- [ ] A patient-scoped token is obtained per chart-open and used as the `Authorization` header on `/chat`
- [ ] Agent's `/chat` accepts and uses that per-request token for its FHIR calls (not just the static env token)
- [ ] CORS allows the browser-side call from the OpenEMR origin
- [ ] Chat response renders with visible source citations, not flattened text
- [ ] Reopening the panel on a different patient yields a token bound to that patient (manually verified, per the spike's cross-patient test)
- [ ] Any FHIR non-2xx (including the known bare-500 denial) is treated as a hard failure, not silently ignored
- [ ] The `agent/` edits are isolated, minimal, and called out separately for cross-branch reconciliation

## References

- `ARCHITECTURE.md` §4, §5
- `context/decisions/deployment-strategy.md` — Option D
- `context/execution/implementation-prompt-03a-smart-token-spike.md`
- `context/execution/smart-token-spike-findings.md`
- `agent/README.md`, `agent/src/copilot/schemas.py`
- `interface/modules/custom_modules/oe-module-dashboard-context/` (structural reference)
- `src/Events/PatientDemographics/RenderEvent.php`
