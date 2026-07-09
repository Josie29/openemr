# Implementation Prompt 04 — oe-module-ai-copilot: docked sidebar rework

> **How to use this file.** Hand it to a fresh Claude Code session rooted in the
> `feature/copilot-php-frontend` worktree
> (`/Users/josiemachalek/Projects/gauntlet-ai/projects/openemr-wt-feature-copilot-php-frontend`).
> This executes the design in `context/specs/copilot-sidebar.md` — read that spec first;
> it is the source of truth. This prompt is the imperative build plan; where they differ,
> the spec wins and you should flag the drift.

> **Build status (as of first pass).** Phases 1–2 are **built and verified** (shell mount +
> full sidebar behavior: toggle, push layout, resize, chips-populate-not-send, patient-switch
> reset). **Phase 3 (server-side conversation persistence) was deferred** by decision —
> conversations are in-memory for now (reset on reload). The JS carries clearly marked Phase 3
> hooks and the config island already exposes `conversationUrl`, so resuming is self-contained.
> Phase 4 (staleness polish) is moot until Phase 3 lands.

---

## 0. What this is, and what it builds on

`context/specs/copilot-sidebar.md` replaces the co-pilot's Dashboard-card placement with a
**VS Code-style docked, resizable, full-height right sidebar**, toggled from the patient
banner, with **server-side per-user+patient conversation persistence**.

**Foundation already in place (do not rebuild — reuse):** the SMART EHR-launch flow
(`public/launch.php`, `public/callback.php`, `src/Smart/*`), the agent-side CORS +
per-request bearer token, the module skeleton, the OAuth client, and the core `csrf` fix.
See `implementation-prompt-03b-copilot-widget.md` (its Section B UI is the only part
superseded). **The launch/token flow is reused unchanged** — the sidebar mints and consumes
tokens exactly as the card did; only the mount surface and the surrounding UI change.

**Testing note:** cross-feature test strategy is being decided separately. For this pass,
verify via `php -l`, PHPCS, PHPStan (level 10, full codebase, filter to changed files), and a
live manual drive. Do not invest in new automated test suites here.

## 1. Read first

1. `context/specs/copilot-sidebar.md` — the design contract (goals, geometry, interaction,
   data model, risks, acceptance criteria). Everything below implements it.
2. The feasibility facts (already confirmed against core — treat as settled):
   - Shell assets: `main.php:318` → `Header::setupHeader` dispatches `ScriptFilterEvent`
     (pageName `main.php`) / `StyleFilterEvent` (full path — match a `main.php` suffix).
   - Sidebar body mount: `Main\Tabs\RenderEvent::EVENT_BODY_RENDER_POST`
     (`src/Events/Main/Tabs/RenderEvent.php`; dispatched `main.php:567`) — persists across
     iframe sub-view navigation.
   - Banner anchor: `#attendantData .form-group .mt-2` (the `str_dob` span in
     `interface/main/tabs/templates/patient_data_template.php`); Knockout-rendered, so the
     button is (re)injected by JS on render, not static markup.
   - Active pid from shell JS: `app_view_model.application_data.patient().pid()` (reactive KO
     observable; updates on patient switch). Fallback `top.getSessionValue('pid')`.
3. The current module: `interface/modules/custom_modules/oe-module-ai-copilot/`. Note what
   changes (`Bootstrap.php`, `Controller/CopilotPanelController.php`, the JS/CSS assets) vs.
   what is reused verbatim (`public/launch.php`, `public/callback.php`, `src/Smart/*`,
   `src/Config/*`, `src/Support/ModuleUrls.php`).

## 2. Scope, in phases (sequential; each independently verifiable)

**Phase 1 — Shell mount plumbing (PHP/events).**
- `Bootstrap.php`: remove the `PatientDemographics\RenderEvent` card listener. Retarget the
  `ScriptFilterEvent`/`StyleFilterEvent` listeners from `demographics.php` to `main.php`
  (remember the style event carries the full path). Add a
  `Main\Tabs\RenderEvent::EVENT_BODY_RENDER_POST` listener that echoes the sidebar shell.
- Rework the controller (rename to a sidebar renderer) to emit the sidebar container +
  config island. The config island gains the conversation endpoint URLs alongside the
  existing `launchUrl`/`chatUrl`/`csrfToken`/`expectedOrigin`/`messageSource`.
- **Guard:** only render when a user session + patient context make sense; the button itself
  is gated client-side to appear only when a patient is active.
- *Verify:* the sidebar container + assets appear in the shell on every chart sub-view; no
  behavior yet; no PHP errors.

**Phase 2 — Sidebar behavior (JS/CSS; no persistence yet).**
- `ai-copilot.js` (substantial rework): open/close toggle; inject the banner button via a
  `MutationObserver` on `#attendantData` (re-inject on Knockout re-render); read pid from the
  KO observable and subscribe to changes; left-edge resize handle writing
  `--copilot-width`; persist width + open/closed to `localStorage` (user-global UI pref);
  push layout (`body { margin-right: var(--copilot-width) }` when open); hide when no patient;
  reset transcript + drop token on patient switch. Reuse the existing chat + hidden-iframe
  launch flow verbatim. Suggested-question chips **populate the input, do not auto-send**.
- `ai-copilot.css`: fixed, full `dvh` height, docked right; default width 20vw, clamp
  320px–50vw; resize handle; toggle-button styling; chip styling; the push margin.
- *Verify:* toggle/resize/persist/push all work; chat works against a live token; patient
  switch resets. If push fights OpenEMR's frame layout, fall back to overlay and note it.

**Phase 3 — Server-side conversation persistence.**
- Schema: `sql/install.sql` creating `aicopilot_conversation` (see spec §7 — `id`, `uuid`,
  `user_id`, `pid`, `thread` JSON, `created`, `updated`, `UNIQUE(user_id, pid)`); bump
  `$v_database` in `version.php`. Because the module row was hand-registered during 03b,
  either drive the install via the Module Manager (the `$v_database` bump surfaces the SQL
  button) or apply `install.sql` directly and document both.
- `ConversationService extends BaseService` using `QueryUtils`: `load`, `upsert`, `clear`,
  all scoped by `user_id` (from the session `authUserID`, **never** the client) + `pid`.
- `public/conversation.php`: session-authenticated endpoints (load/save/clear); CSRF on
  writes; `user_id` derived from session; `pid` = active patient (validate).
- JS: load the thread on open / patient-switch; save after each completed turn (debounced);
  wire the clear button (confirm first).
- *Verify:* a turn persists and reloads on reopen; patient-switch swaps threads; clear
  empties it; a second user cannot see the first's thread (user_id from session).

**Phase 4 — Chips + staleness polish.**
- Static starter chips (chart-aware where cheap — e.g. surface an "overdue" chip only if that
  signal is readily available). Restored turns show a per-turn timestamp + an "answers reflect
  the record at that time" affordance (spec §9). Do not auto-re-verify old turns.

**Phase 5 — Verify + gates.**
- `php -l` on all changed PHP; PHPCS on the module; PHPStan level 10 on the full codebase
  filtered to changed files (fix at source, no new baseline entries); confirm the agent tests
  still pass (no agent files change this pass). Live drive of the full flow.

## 3. Non-goals (deferred; do not build)

- Streaming / token-by-token output.
- Conversation-tailored follow-up suggestions (static starters only; `suggested_followups`
  is a later agent-side change and would need cross-branch coordination).
- Human-readable / clickable clinical citations (keep structured citations for now).
- Proactive on-open orientation summary.
- Audit logging + retention (schema is kept promotable; do not build the machinery).
- Care-team / break-glass gating on conversation access (UC-5, future).
- New automated test suites (owner is handling cross-feature testing separately).

## 4. Constraints & coordination

- **Zero core patches** beyond the already-shipped EHR-launch `csrf` fix in
  `src/RestControllers/AuthorizationController.php`. If you find yourself editing core, stop.
- **No agent-file changes** this pass — so no new cross-branch reconciliation beyond 03b's.
- All new PHP: `declare(strict_types=1)`, PSR-4 under `OpenEMR\Modules\AiCopilot\`, typed
  signatures, `QueryUtils`/`BaseService` for DB, `SessionUtil` for session writes, no direct
  superglobals (use `filter_input`), catch narrowing per the `ForbiddenCatchType` rule.
- Secrets/config stay in env (`CopilotConfig`); conversation PHI stays server-side (never
  localStorage — only the non-PHI UI prefs go there).

## 5. Acceptance criteria

Mirror `context/specs/copilot-sidebar.md` §10. In short:
- [ ] Banner toggle appears only with an active patient; toggles the docked panel.
- [ ] Sidebar: right-docked, full `dvh`, default 20vw, resizable 320px–50vw, pushes the chart.
- [ ] Open/closed + width persist across views and logins (localStorage UI pref).
- [ ] Panel persists across chart sub-view navigation without reload.
- [ ] Chips populate the input (no auto-send).
- [ ] A completed turn saves server-side (user+patient) and reloads on reopen / patient-return.
- [ ] Clear removes the current patient's thread after confirmation.
- [ ] Patient switch swaps thread + re-scopes token; no cross-patient or cross-user bleed.
- [ ] Conversation storage server-side, user_id from session; no PHI in the browser.
- [ ] Dashboard card mount removed.

## References
- `context/specs/copilot-sidebar.md` (source of truth)
- `context/execution/implementation-prompt-03b-copilot-widget.md` (foundation; Section B superseded)
- `context/execution/smart-token-spike-findings.md`
- `src/Events/Main/Tabs/RenderEvent.php`, `src/Core/Header.php`,
  `interface/main/tabs/templates/patient_data_template.php`
