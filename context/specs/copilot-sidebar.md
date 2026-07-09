# Design Spec — Clinical Co-Pilot: Launcher + Panel

> Status: agreed design, pre-implementation. Supersedes the Dashboard-card placement
> shipped in `implementation-prompt-03b-copilot-widget.md`. This spec is the source of
> truth for the sidebar rework; an implementation prompt can be derived from it.
>
> Filename kept as `copilot-sidebar.md` for inbound references (CLAUDE.md, module
> README, `Bootstrap.php`, execution prompts); the design is now decomposed into two
> named components — see §2.

## 1. Overview

Replace the in-chart Dashboard card with a **VS Code-style docked sidebar**: an
always-reachable AI surface pinned to the right edge of the EHR, scoped to the active
patient. It persists across chart sub-views and remembers each patient's conversation on
the server.

**Clinical Co-Pilot** is the product. It is delivered as **two distinct UI components**
(§2), so the rest of this spec can speak about each without ambiguity:

- **Co-Pilot Launcher** — the pill in the patient banner. Summons/dismisses the Panel and
  signals availability. It is *not* the feature; it is the door to it.
- **Co-Pilot Panel** — the docked right-edge surface. The actual copilot: transcript,
  input, suggested questions, persistence, patient-scope display.

"Clinical Co-Pilot" remains the **user-facing product label** (Panel header, brand).
"Launcher" and "Panel" are the **internal/spec names** for the two parts.

**One-liner:** the physician's persistent, patient-scoped, grounded copilot — one
Launcher click away from any chart view, its history where it belongs (the server), not
the browser.

## 2. Component model

The single-blob `ai-copilot.js` today mixes both concerns under one `ai-copilot-*`
prefix (`ai-copilot-toggle` = the button, `ai-copilot-sidebar` = the panel). This spec
draws the boundary explicitly:

| | **Co-Pilot Launcher** | **Co-Pilot Panel** |
|---|---|---|
| **Role** | Availability signal + open/close control | The copilot surface |
| **Lives in** | Patient banner (`#attendantData`) | Shell `<body>`, docked right |
| **Owns** | Its own injection/re-injection, open-state reflection, availability | Geometry, transcript, input, chips, clear, persistence, token flow, patient-scope display |
| **DOM root** | `#copilot-launcher` | `#copilot-panel` |
| **Depends on** | Active pid (present? → visible), shared open-state | Active pid (which thread/token), shared open-state, shared width |
| **Lifecycle risk** | Banner re-renders on patient switch → must re-inject (§4) | Must survive iframe sub-view navigation → mounts in shell (§7) |

**Shared UI state** both components read (§6): `open` (bool), `width` (px), `activePid`.
Neither component owns this state directly; a thin coordinator holds it so the Launcher's
pill styling and the Panel's visibility never disagree. This is the one-source-of-truth
fix for "button says open, panel says closed" drift.

Naming convention going forward: DOM ids/classes migrate from `ai-copilot-*` to
`copilot-launcher*` / `copilot-panel*`; the module/service/PHP prefix `aicopilot`
(table, module dir) is unaffected.

## 3. Goals / Non-goals

**Goals**
- A docked, resizable, full-height right **Panel** that survives navigation between chart views.
- A **Launcher** in the patient banner; open/closed and width are remembered.
- Per-patient conversation history, stored server-side (per user + patient).
- Clickable suggested questions that populate the input for review before sending.
- Clear-conversation control.
- Specified Panel states for loading and failure (§5.3) — not just the happy path.
- Zero core patches (verified: all mount points are module-reachable events).

**Non-goals (this increment)**
- Streaming / token-by-token output (future). The Panel still needs a "thinking" state (§5.3).
- Tailored, conversation-aware follow-up suggestions (static starters only for now).
- Human-readable / clickable clinical citations (still renders structured citations —
  see the developer-facing rendering note in §5.2).
- Proactive on-open orientation summary (future).
- A global keyboard shortcut to toggle the Launcher (future; Esc-to-close is in scope, §5.4).
- Audit logging + retention policy (deferred; schema kept promotable — see §9).
- Care-team / break-glass gating on conversation access (future; UC-5 in
  `context/decisions/deployment-strategy.md`).

## 4. Co-Pilot Launcher (the banner pill)

### 4.1 Placement & injection

- Lives in the patient banner next to DOB/Age; icon + "Co-Pilot" label.
- See §4.4 for iconography — the current pill's ring-dot glyph is a signifier bug.
- The banner is Knockout-rendered from `#patient-data-template` and **re-renders on every
  patient change**, wiping any injected node. The Launcher is therefore (re)appended into
  `#attendantData` by our JS via a MutationObserver / KO subscription. This re-injection is
  the Launcher's single biggest reliability concern (§10).
- JS-injected (no core template edit). See mount table §7.

### 4.2 States

The Launcher is a small component with a real state machine — specify it so it never
lies about the Panel:

| State | When | Appearance |
|---|---|---|
| **Absent** | No active patient | Not rendered; any open Panel hides (§6). The copilot is meaningless without a patient. |
| **Available — closed** | Patient active, Panel closed | Default pill, `aria-expanded="false"`. |
| **Available — open** | Patient active, Panel open | Active styling (filled), `aria-expanded="true"`. |
| **Has-conversation hint** | Patient active + a saved thread exists for (user, pid) | A subtle dot on the pill: "you've discussed this patient before." **In scope this increment** (decided). The presence check reuses the §9 Load endpoint — no extra call if load is eager. |

### 4.3 Accessibility

- `role="button"`, keyboard-focusable, Enter/Space toggles.
- `aria-expanded` reflects Panel open state; `aria-controls="copilot-panel"` links the two.
- On open, move focus into the Panel (input or first chip). On close, return focus to the
  Launcher. (Focus contract shared with §5.4.)

### 4.4 Iconography (UX fix)

**Problem.** Today's pill uses a filled-circle-with-ring glyph — the visual vocabulary of a
**radio button / active filter**. In a banner already dense with toggles, a physician reads
"a filter is on," not "AI assistant." The signifier contradicts the function.

**Direction.** Swap to the now-conventional **AI affordance: a sparkle/sparkles glyph**
(`✦`/`✨`-style), the pattern users already associate with LLM assistants (Copilot, Gemini,
etc.). It is unambiguous and, importantly, does **not** collide with the banner's existing
"Messages" chat metaphor the way a bare chat-bubble would.

**Decision (locked):** ship **a four-point sparkle glyph + the "Co-Pilot" text label** — the
clearest option for first-time users. Rejected alternatives: sparkle-in-a-chat-bubble
(deferred; adds a "dialogue" connotation we don't need yet) and icon-only (hurts
discoverability before the pattern is learned).

- **AI accent, not functional blue.** The sparkle carries a **violet→blue gradient** when
  active, deliberately distinct from the banner's plain functional blue, so it reads as
  *assistant* rather than *another toggle*.
- **State expression** (pill doubles as the drawer control): closed → outline/ghost sparkle
  on a tinted pill; open → filled gradient pill with a white sparkle (matches
  `aria-expanded`). A trailing chevron rotates 90° on open to imply "this opens a right-side
  drawer." Keep the label.
- **Has-conversation hint dot** (§4.2) sits on the pill's top-right corner when a saved
  thread exists for the active patient.

Reference implementation of all of the above: the approved design mock
(`context/specs/assets/copilot-mock.html`), rendered as an Artifact for review.

Whatever glyph ships, it must be visually distinct from the banner's radio/checkbox/toggle
controls — that distinctness is the acceptance bar, and the ring-dot fails it.

## 5. Co-Pilot Panel (the docked surface)

### 5.1 Geometry

- Docked right, `position: fixed`, full height via **`dvh`** (not `vh`).
- **Push, not overlay**: `body { margin-right: var(--copilot-width) }` when open, so the
  chart reflows beside the Panel rather than being covered. Fallback to overlay only if the
  push fights OpenEMR's frame layout.
- **Default width 20vw**, user-resizable via a left-edge drag handle. Clamp **320px–50vw**.
- Width + open/closed persisted (§6).

### 5.2 Anatomy

Three regions, top to bottom:

- **Header** — product label "Clinical Co-Pilot"; **patient subtitle** (e.g. "Bessie
  Muller (2)"); Clear control; Close (×). The patient subtitle is deliberately redundant
  with the banner: it is a **scope-safety affordance** confirming *which* patient the
  Panel is grounded to, reinforcing the "token, thread, and chart never diverge" guarantee
  (§8). Keep it.
- **Body** — either the **empty state** (suggested-question chips) or the **transcript**
  (ordered turns). Each restored turn is read-only history with a timestamp (§8, §10).
  - Citations currently render developer-facing (`Patient/<uuid>` + `field = value`
    blocks). That is acceptable this increment (human-readable citations are a §3
    non-goal) but is explicitly a known rough edge, not the intended end state.
- **Footer** — input ("Ask about this patient…") + Send. Enter sends; the input is
  disabled while a turn is in flight (§5.3).

### 5.3 States

The current spec only describes the happy path. Specify the rest:

| State | Trigger | Panel shows |
|---|---|---|
| **No-patient** | Active pid cleared | Panel hidden entirely (§6); no empty state, no header. |
| **Empty** | Patient active, no/just-cleared thread | Header + suggested-question chips + input. |
| **Loaded** | Saved thread for (user, pid) | Header + transcript + input. |
| **Turn in flight** ("thinking") | Turn sent, awaiting agent | See §5.3.1 — required loading indicator between submit and response. |
| **Error** | Token mint fails, agent unreachable, or save fails | Non-destructive inline error on the affected turn with a retry affordance; the transcript and input stay usable. Never expose raw `getMessage()` (CLAUDE.md). |

### 5.3.1 Loading indicator (submit → response)

Between the user sending a turn and the agent's response landing, the Panel **must** show a
loading state — the round-trip is a grounded lookup and can take seconds; a silent frozen
input reads as "broken."

- **Echo the question immediately.** On submit, append the user's question to the transcript
  at once (optimistic), so they see it registered before the answer exists.
- **Pending answer bubble.** Directly under the echoed question, render an in-progress
  placeholder with an **animated indicator** — a typing-style animated ellipsis or a small
  shimmer where the answer text will appear. Prefer an animated dot/typing indicator over a
  spinner: it reads as "composing a reply," not "system busy."
- **Optional grounded label.** A short status caption ("Checking the record…") reinforces
  that the copilot is reading FHIR, not free-associating. Keep it generic — do not claim a
  specific step we cannot guarantee.
- **Input disabled** while in flight (no double-submit); re-enabled when the turn resolves
  (success or §5.3 error).
- **This is the streaming seam.** The pending bubble is the exact slot a future streaming
  increment fills token-by-token — building it now means streaming is a swap, not a rework.

### 5.4 Focus & keyboard

- On open: focus the input (or first chip in the empty state).
- **Esc** closes the Panel and returns focus to the Launcher.
- Focus stays within the Panel while open is not required (it's a non-modal dock, push not
  overlay) — do **not** trap focus; the physician must still reach the chart beside it.

### 5.5 Interaction

**Suggested questions**
- Rendered as clickable chips in the empty state.
- Clicking a chip **populates the input** (does not auto-send). The user reviews/edits,
  then presses Enter (or Send). Keeps the human in the loop on every prompt.
- Static starter set this increment. Examples, chart-aware where cheap: "Summarize this
  patient", "What's overdue?", "Any medication–allergy conflicts?", "Recent visits".
  Tailored follow-ups are future (returned inline via a `suggested_followups` field, not a
  second call).

**Clear conversation**
- Available whenever the thread has content.
- Confirm before clearing (destructive). Clears the **current patient's** thread only.
- Deletes the server-side thread for (user, patient) and resets the Panel to Empty.

**Conversation persistence (per patient, server-side)**
- On open (or patient switch), the Panel loads that (user, patient) thread and renders it,
  defaulting to the prior conversation with the Clear option.
- Restored turns are **history**: read-only, timestamped. A new turn mints/uses a fresh
  token (§8) and appends to the thread.

## 6. Shared state & coordination

Both components read one shared state object; neither mutates the other's DOM directly.

| State | Store | Scope | Who reads |
|---|---|---|---|
| `open` | in-memory + `localStorage` | user-global UI pref | Launcher (pill styling, `aria-expanded`), Panel (visibility) |
| `width` | `localStorage` | user-global UI pref | Panel (geometry), `--copilot-width` |
| `activePid` | `app_view_model.application_data.patient().pid()` (KO observable); fallback `top.getSessionValue('pid')` | reactive | Launcher (present? → visible), Panel (which thread/token) |

- **Open/closed + width are user-global**, not per-patient — the copilot should feel the
  same everywhere. Both are non-PHI UI prefs, so `localStorage` is the correct home
  (contrast conversations — §9).
- **Toggle action**: the Launcher requests a toggle; the coordinator flips `open` and both
  components re-render from it. This is the "button and panel never disagree" fix.
- **On patient switch** (`activePid` change): the Launcher re-injects (banner re-render);
  the Panel drops the held token, resets the visible transcript, and loads the new
  patient's thread (§8). If no patient, both hide.

## 7. Placement & mount (feasibility confirmed — zero core edits)

| Element | Mount point | Notes |
|---|---|---|
| Panel container | `Main\Tabs\RenderEvent::EVENT_BODY_RENDER_POST` | Rendered into the outer shell `<body>`; persists across iframe sub-view navigation (class doc: content here "sticks around through every sub tab content frame"). |
| JS / CSS assets | `ScriptFilterEvent` / `StyleFilterEvent`, pageName `main.php` | Same asset mechanism already used for the card, retargeted to the shell. Style event carries the full path — match on a `main.php` suffix, not exact string. |
| Launcher | JS-injected into the patient banner | Banner is Knockout-rendered from `#patient-data-template` and re-renders on patient change; DOB/Age is a single `str_dob` span with no discrete "Age" node. Re-appended into `#attendantData` via MutationObserver / KO subscription (§4.1). |
| Active pid (shell JS) | `app_view_model.application_data.patient().pid()` | Reactive observable; updates on every patient switch. Fallback: `top.getSessionValue('pid')`. |

The token/launch flow (`public/launch.php`, `public/callback.php`) is **unchanged**:
`launch.php` reads pid from the session, which the shell keeps current, so a token minted
from the shell binds to the active patient.

## 8. Token & data flow (unchanged mechanism, shell-initiated)

- Panel JS runs in the shell (top window). On first turn intent, it runs the hidden-iframe
  SMART launch → `launch.php` (session pid) → `callback.php` → `postMessage` token back.
- Chat XHR goes browser → agent `/chat` with `Authorization: Bearer <token>` and
  `patient_id` = the token's own `patient` claim (never a page-derived id).
- **Token lifetime ≠ conversation lifetime.** The 1h token is transient; the thread
  persists. A restored transcript is read-only history until a new token is minted for the
  next turn.
- **Patient switch**: on `activePid` change (KO observable), drop the held token and reset
  the visible transcript, then load the new patient's thread. The next turn re-launches for
  the new patient. This guarantees the token, the displayed thread, and the chart never
  diverge — the Panel's patient subtitle (§5.2) makes that scope visible.

## 9. Data model & persistence

Conversations are stored **server-side, keyed by user + patient** — decided because a
thread contains clinical Q&A and stamped record values (PHI); the browser (localStorage)
would put PHI at rest on a potentially shared workstation. Server storage is ACL-adjacent,
off-device, and promotable to an audited record later.

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
  so the transcript re-renders faithfully offline. Values are as-of-then — see staleness (§10).
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

## 10. Things to get right (surfaced risks)

- **Launcher re-injection race.** The banner re-renders on patient switch and wipes the
  pill; the observer must re-inject idempotently (never two pills, never a gap). This is the
  Launcher's primary failure mode (§4.1).
- **Open/closed drift.** Launcher styling and Panel visibility must read the *same* `open`
  state (§6) — no independent booleans.
- **Staleness of restored threads.** Stored citations are as-of-when-asked. Show a per-turn
  timestamp and an "answers reflect the record at that time" affordance so persistence does
  not silently undermine the groundedness guarantee. Do not re-verify old turns automatically.
- **Shared workstation.** Even server-side, scope threads to the owning `user_id`; one
  physician must not see another's thread. (Supervisor/care-team access is future.)
- **Multi-patient tabs.** OpenEMR can hold several patient tabs; "active patient" = the
  focused tab's pid (the KO observable). The Panel swaps thread + token on active-patient change.
- **Panel error states.** Token/agent/save failures must degrade gracefully (§5.3), never
  leave a dead input or leak `getMessage()`.
- **Thread growth.** Cap stored turns (e.g. last N) or serialized size to bound the row.
- **No-patient context.** Launcher and Panel both hidden when no chart is open.
- **Concurrency.** Same user+patient in two browser tabs → last-write-wins upsert; acceptable.

## 11. Acceptance criteria

**Co-Pilot Launcher**
- [ ] Appears in the patient banner only when a patient is active; toggles the Panel.
- [ ] Re-injects correctly on patient switch (banner re-render) — exactly one pill, always.
- [ ] Reflects Panel open state (`aria-expanded`, active styling) and links `aria-controls`.
- [ ] Uses a sparkle glyph + "Co-Pilot" label with the violet→blue AI accent, visually
      distinct from banner filter/toggle controls — the ring-dot glyph is gone (§4.4).
- [ ] Closed → ghost sparkle; open → filled gradient + rotated chevron (matches `aria-expanded`).
- [ ] Shows a has-conversation hint dot when a saved thread exists for (user, pid) (§4.2).
- [ ] Keyboard-operable (Enter/Space); moves focus into the Panel on open, back on close.

**Co-Pilot Panel**
- [ ] Docked right, full `dvh` height, default 20vw, resizable (320px–50vw), pushing the chart.
- [ ] Persists across chart sub-view navigation without reload.
- [ ] Header shows product label + patient subtitle scoping the Panel to the active patient.
- [ ] Suggested-question chips populate the input (do not auto-send).
- [ ] On submit, echoes the question immediately and shows an animated loading indicator
      until the response lands; input disabled while in flight (§5.3.1).
- [ ] Degrades gracefully on token/agent/save error with retry; no raw error messages.
- [ ] Esc closes the Panel and returns focus to the Launcher.
- [ ] A completed turn is saved server-side for (user, patient) and reloads on reopen / return.
- [ ] Clear removes the current patient's thread after confirmation.
- [ ] Switching patients swaps the thread and re-scopes the token; no cross-patient bleed.

**Shared / system**
- [ ] Open/closed + width persist across views and logins (localStorage, user-global UI pref).
- [ ] Launcher styling and Panel visibility never disagree (single shared `open` state).
- [ ] Conversation storage is server-side, keyed by user+patient, user_id from session; no PHI in the browser.
- [ ] Dashboard card mount removed.
- [ ] Zero core patches beyond the already-flagged EHR-launch `csrf` fix.

## 12. Future increments (captured, not built here)

Streaming output (fills the §5.3 thinking slot token-by-token); conversation-tailored
follow-up suggestions (inline `suggested_followups`); human-readable/clickable citations
(replacing the §5.2 developer-facing rendering); proactive on-open orientation; global
keyboard shortcut to toggle the Launcher; audit logging + retention (promote the schema to
a record); care-team/break-glass gating on conversation access.
