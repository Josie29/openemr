# Design Spec — Clinical Co-Pilot docked sidebar

> Status: agreed design, pre-implementation. Supersedes the Dashboard-card placement
> shipped in `implementation-prompt-03b-copilot-widget.md`. This spec is the source of
> truth for the sidebar rework; an implementation prompt can be derived from it.

## 1. Overview

Replace the in-chart Dashboard card with a **VS Code-style docked sidebar**: an
always-reachable AI panel pinned to the right edge of the EHR, scoped to the active
patient, toggled open by a button in the patient banner. It persists across chart
sub-views and remembers each patient's conversation on the server.

**One-liner:** the physician's persistent, patient-scoped, grounded copilot — one toggle
away from any chart view, with its history where it belongs (the server), not the browser.

## 2. Goals / Non-goals

**Goals**
- A docked, resizable, full-height right panel that survives navigation between chart views.
- Toggle from the patient banner; open/closed and width are remembered.
- Per-patient conversation history, stored server-side (per user + patient).
- Clickable suggested questions that populate the input for review before sending.
- Clear-conversation control.
- Zero core patches (verified: all mount points are module-reachable events).

**Non-goals (this increment)**
- Streaming / token-by-token output (future).
- Tailored, conversation-aware follow-up suggestions (static starters only for now).
- Human-readable / clickable clinical citations (still renders structured citations).
- Proactive on-open orientation summary (future).
- Audit logging + retention policy (deferred; schema kept promotable — see §7).
- Care-team / break-glass gating on conversation access (future; UC-5 in
  `context/decisions/deployment-strategy.md`).

## 3. Placement & mount (feasibility confirmed — zero core edits)

| Element | Mount point | Notes |
|---|---|---|
| Sidebar container | `Main\Tabs\RenderEvent::EVENT_BODY_RENDER_POST` | Rendered into the outer shell `<body>`; persists across iframe sub-view navigation (class doc: content here "sticks around through every sub tab content frame"). |
| JS / CSS assets | `ScriptFilterEvent` / `StyleFilterEvent`, pageName `main.php` | Same asset mechanism already used for the card, retargeted to the shell. Style event carries the full path — match on a `main.php` suffix, not exact string. |
| Toggle button | JS-injected into the patient banner | Banner is Knockout-rendered from `#patient-data-template` and re-renders on patient change; DOB/Age is a single `str_dob` span with no discrete "Age" node. Button is (re)appended by our JS into `#attendantData` via a MutationObserver / KO subscription. |
| Active pid (shell JS) | `app_view_model.application_data.patient().pid()` | Reactive observable; updates on every patient switch. Fallback: `top.getSessionValue('pid')`. |

The token/launch flow (`public/launch.php`, `public/callback.php`) is **unchanged**:
`launch.php` reads pid from the session, which the shell keeps current, so a token minted
from the shell binds to the active patient.

## 4. Geometry

- Docked right, `position: fixed`, full height via **`dvh`** (not `vh`).
- **Push, not overlay**: `body { margin-right: var(--copilot-width) }` when open, so the
  chart reflows beside the panel rather than being covered. Fallback to overlay only if the
  push fights OpenEMR's frame layout.
- **Default width 20vw**, user-resizable via a left-edge drag handle. Clamp **320px–50vw**.
- Width + open/closed persisted in `localStorage` as a **user-global UI preference** (not
  per-patient) — the panel should feel the same everywhere. This is a non-PHI UI pref, so
  the browser is the correct place for it (contrast with conversations — §7).

## 5. Toggle button & visibility

- Lives in the patient banner next to DOB/Age; icon + "Co-Pilot" label; reflects open state
  (`aria-expanded`, active styling).
- **Only present when a patient is active.** No patient context → no button, and any open
  sidebar hides/disables. The copilot is meaningless without a patient.
- On patient switch, the button persists (banner re-renders; our observer re-injects it) and
  the sidebar re-scopes to the new patient (§6, §8).

## 6. Interaction

**Suggested questions**
- Rendered as clickable chips in the empty state.
- Clicking a chip **populates the input** (does not auto-send). The user reviews/edits, then
  presses Enter (or Send). This keeps the human in the loop on every prompt.
- Static starter set this increment. Examples, chart-aware where cheap:
  "Summarize this patient", "What's overdue?", "Any medication–allergy conflicts?",
  "Recent visits". Tailored follow-ups are a future increment (returned inline on the turn
  via a `suggested_followups` field, not a second call).

**Clear conversation**
- Available whenever the thread has content.
- Confirm before clearing (destructive). Clears the **current patient's** thread only.
- Clearing deletes the server-side thread for (user, patient) and resets the panel to the
  empty state.

**Conversation persistence (per patient, server-side)**
- On open (or patient switch), the panel loads that (user, patient) thread from the server
  and renders it, defaulting to the prior conversation with the clear option.
- Restored turns are **history**: read-only, timestamped. A new turn mints/uses a fresh
  token (§8) and appends to the thread.

## 7. Data model & persistence

Conversations are stored **server-side, keyed by user + patient** — decided because a thread
contains clinical Q&A and stamped record values (PHI); the browser (localStorage) would put
PHI at rest on a potentially shared workstation. Server storage is ACL-adjacent, off-device,
and promotable to an audited record later.

**Table** (module-owned; kept minimal but promotable):

```
aicopilot_conversation
  id          BIGINT PK AUTO_INCREMENT
  uuid        BINARY(16)         -- OpenEMR convention for new rows
  user_id     <provider user id> -- the authoring physician; from session, never the client
  pid         BIGINT             -- patient
  thread      LONGTEXT/JSON      -- serialized transcript (see below)
  created     DATETIME
  updated     DATETIME
  UNIQUE KEY (user_id, pid)       -- one thread per user per patient
```

- **Thread JSON**: ordered list of turns; each turn `{ question, summary, claims[], asked_at }`
  where `claims[]` is the rendered `ChatResponse` claims (text + source with stamped value),
  so the transcript re-renders faithfully offline. Values are as-of-then — see staleness (§9).
- **Promotable to a record later**: `user_id`/`pid`/`created`/`updated` already support an
  audit view; adding an append-only audit log + retention job is additive, no reshape.

**Persistence mechanism (resolved defaults, override if desired):**
- **Schema install via the module's `sql/install.sql`** + bump `$v_database` in `version.php`,
  run by the Module Manager on install. This is the conventional self-installing-module path;
  it is the deliberate exception to CLAUDE.md's "new schema uses Doctrine Migrations" rule,
  because a custom module must carry its own installable schema.
- **Access via a `ConversationService extends BaseService`** using `QueryUtils` (per CLAUDE.md
  service-layer + DB conventions). No direct DB connections.

**Endpoints** (module `public/` scripts; session auth via `globals.php`; CSRF on writes):
- Load: return the thread for (session user, active pid).
- Save: upsert the thread for (session user, active pid) — **user_id derived from the session
  `authUserID`, never from the client.** Called after each completed turn (debounced).
- Clear: delete the thread for (session user, active pid).

**Save cadence (resolved default):** persist after each completed turn (debounced), so a
crash or navigation never loses more than the in-flight turn.

## 8. Token & data flow (unchanged mechanism, shell-initiated)

- Sidebar JS runs in the shell (top window). On first turn intent, it runs the hidden-iframe
  SMART launch → `launch.php` (session pid) → `callback.php` → `postMessage` token back.
- Chat XHR goes browser → agent `/chat` with `Authorization: Bearer <token>` and
  `patient_id` = the token's own `patient` claim (never a page-derived id).
- **Token lifetime ≠ conversation lifetime.** The 1h token is transient; the thread persists.
  A restored transcript is read-only history until a new token is minted for the next turn.
- **Patient switch**: on active-pid change (KO observable), drop the held token and reset the
  visible transcript, then load the new patient's thread. The next turn re-launches for the
  new patient. This guarantees the token, the displayed thread, and the chart never diverge.

## 9. Things to get right (surfaced risks)

- **Staleness of restored threads.** Stored citations are as-of-when-asked. Show a per-turn
  timestamp and an "answers reflect the record at that time" affordance so persistence does
  not silently undermine the groundedness guarantee. Do not re-verify old turns automatically.
- **Shared workstation.** Even server-side, scope threads to the owning `user_id`; one
  physician must not see another's thread through the copilot. (Supervisor/care-team access is
  future.)
- **Multi-patient tabs.** OpenEMR can hold several patient tabs; "active patient" = the focused
  tab's pid (the KO observable). The sidebar swaps thread + token on active-patient change.
- **Thread growth.** Cap stored turns (e.g. last N) or serialized size to bound the row.
- **No-patient context.** Button/sidebar hidden when no chart is open.
- **Concurrency.** Same user+patient in two browser tabs → last-write-wins upsert; acceptable.

## 10. Acceptance criteria

- [ ] Toggle button appears in the patient banner only when a patient is active; toggles the panel.
- [ ] Sidebar is docked right, full `dvh` height, default 20vw, resizable (320px–50vw), pushing the chart.
- [ ] Open/closed + width persist across views and logins (localStorage, user-global UI pref).
- [ ] Panel persists across chart sub-view navigation without reload.
- [ ] Suggested-question chips populate the input (do not auto-send).
- [ ] A completed turn is saved server-side for (user, patient) and reloads on reopen / patient-return.
- [ ] Clear removes the current patient's thread after confirmation.
- [ ] Switching patients swaps the thread and re-scopes the token; no cross-patient bleed.
- [ ] Conversation storage is server-side, keyed by user+patient, user_id from session; no PHI in the browser.
- [ ] Dashboard card mount removed.
- [ ] Zero core patches beyond the already-flagged EHR-launch `csrf` fix.

## 11. Future increments (captured, not built here)

Streaming output; conversation-tailored follow-up suggestions (inline `suggested_followups`);
human-readable/clickable citations; proactive on-open orientation; audit logging + retention
(promote the schema to a record); care-team/break-glass gating on conversation access.
