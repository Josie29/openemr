# Spec: Multi-line auto-growing chat input (JOS-36)

**Status:** Draft · **Priority:** Low (nice-to-have UX) · **Linear:** JOS-36
**Branch:** `01josie/jos-36-make-the-sidebar-chat-input-multi-line-auto-growing-textarea`

## Goal

Replace the single-line `<input>` in the Co-Pilot composer with a multi-line,
auto-growing `<textarea>` so a physician can see more of a longer question as
they type, without the message list or send button being pushed off-screen.

## Non-goals

- No change to the send/transcript/streaming pipeline (`submitMessage` →
  `sendTurn` and downstream stay untouched).
- No rich text, markdown preview, attachments, or slash-command affordances.
- No change to the chip behaviour (chips still *populate, don't auto-send* — spec §6).
- No server-side/agent changes; this is a pure frontend (Twig markup + CSS + JS) edit.

## Affected files

| File | Location | Change |
|------|----------|--------|
| `CopilotSidebarController.php` | `:158-164` | `<input type="text">` → `<textarea rows="1">` |
| `ai-copilot.css` | `.ai-copilot__composer` `:380`, `.ai-copilot__input` `:387` | flex alignment + sizing caps |
| `ai-copilot.js` | composer wiring `:626-697` | auto-grow fn, `input`/`keydown` handlers, reset calls |
| `version.php` | `$v_js_includes` | bump on promotion (assets changed) |

## Design

### 1. Markup (`CopilotSidebarController.php:158`)

Swap the element, keep the classes, id, `autocomplete`, and `placeholder`. A
textarea has no `type`; `rows="1"` is the collapsed baseline (CSS enforces the
real min/max height). `els.input.value` reads/writes identically on a textarea,
so `ai-copilot.js` lines 632/640/644 keep working unchanged.

```html
<textarea
    class="ai-copilot__input form-control"
    id="ai-copilot-input"
    rows="1"
    autocomplete="off"
    placeholder="{$placeholder}"
></textarea>
```

The `<label class="sr-only" for="ai-copilot-input">` stays — the accessible name
is unchanged.

### 2. CSS (`ai-copilot.css`)

**The layout gotcha.** `.ai-copilot__composer` currently sets no `align-items`,
so it defaults to `stretch`. With a single-line input that's invisible; with a
growing textarea the send button would stretch to the textarea's full height.
Anchor the row to the bottom so the button stays one-line-tall next to the last
line of text:

```css
.ai-copilot__composer {
    display: flex;
    align-items: flex-end;   /* NEW — keep send button bottom-aligned as input grows */
    gap: var(--copilot-space-2);
    padding: var(--copilot-space-3);
    border-top: 1px solid var(--copilot-border);
}
```

Size the textarea in `rem`, cap it, and scroll internally past the cap. All
values on the existing token scale / `line-height` in `rem`:

```css
.ai-copilot__input {
    flex: 1 1 auto;
    min-width: 0;
    resize: none;                 /* no manual drag handle */
    line-height: 1.4rem;          /* one text line */
    min-height: 2.25rem;          /* ~1 line + vertical padding; matches old input height */
    max-height: 7.25rem;          /* ~5 lines before internal scroll */
    overflow-y: auto;             /* scrollbar only appears past max-height */
    box-sizing: border-box;       /* so scrollHeight math (JS) includes padding/border */
}
```

`box-sizing: border-box` matters: the JS auto-grow uses `scrollHeight`, which
includes padding; without border-box the `max-height` cap and the measured
height disagree by the padding amount and the cap drifts. (Confirm Bootstrap's
`.form-control` doesn't already set it globally — if it does, this line is
belt-and-braces.)

Numbers to finalize against the rendered box (tune during the browser check):
`min-height` should match the *current* input's height so the collapsed state
looks unchanged; `max-height` = `~5 × line-height + vertical padding`.

### 3. JS (`ai-copilot.js`)

**a. Auto-grow function.** Reset to `auto`, then grow to content height. CSS
`max-height` + `overflow-y:auto` cap it — no JS clamp needed.

```js
function autoGrowInput() {
    els.input.style.height = 'auto';
    els.input.style.height = els.input.scrollHeight + 'px';
}
```

**b. Wire it — `.value` writes do NOT fire `input`.** Bind the `input` event for
typing, and call `autoGrowInput()` manually everywhere code sets the value:

- `init()` (`:693` block): `els.input.addEventListener('input', autoGrowInput);`
- After reset in `onSubmit` (`:644`): call `autoGrowInput()` right after
  `els.input.value = ''` so it collapses back to one row.
- In `wireChips` (`:632`): call `autoGrowInput()` after setting the chip prompt so
  a multi-line chip expands the box.
- On open/focus (`openSidebar`, near `els.input.focus()` `:198`): call once so a
  restored draft (if any) is sized correctly.

**c. Key handling — Enter = send, Shift+Enter = newline.** There is no `keydown`
handler today (send is form-submit/click). A textarea inserts a newline on Enter
by default, so intercept it:

```js
function onInputKeydown(event) {
    if (event.key !== 'Enter' || event.shiftKey) {
        return;                       // Shift+Enter (or any other key) → default newline
    }
    if (event.isComposing || event.keyCode === 229) {
        return;                       // IME candidate selection — don't hijack Enter
    }
    event.preventDefault();
    els.form.requestSubmit(els.send); // reuse the existing submit path (validation + onSubmit)
}
```

Registered in `init()`: `els.input.addEventListener('keydown', onInputKeydown);`

Using `form.requestSubmit(els.send)` (not a hand-rolled call to `onSubmit`) keeps
the single source of truth: it fires the same `submit` event `onSubmit` already
listens for at `:693`, respects the disabled state during `setBusy(true)`
(`:288` disables input+send), and inherits the empty-message guard at `:641`.

## Edge cases & failure modes

- **Busy state:** `setBusy(true)` disables the textarea, so `requestSubmit` on a
  disabled control is a no-op — no double-send while a turn is streaming. Verify.
- **IME (Japanese/Chinese/Korean):** the `isComposing`/`229` guard prevents Enter
  from sending mid-composition. Without it, confirming an IME candidate would send.
- **Paste of a large block:** grows to `max-height` then scrolls — the `input`
  event fires on paste, so `autoGrowInput` runs. No special-casing needed.
- **Empty/whitespace Enter:** `onSubmit` already trims and returns on empty
  (`:640-643`); Enter inherits that guard for free.
- **Reopen with a draft:** the value survives in the DOM across close/open (sidebar
  is hidden, not destroyed), so the open-time `autoGrowInput()` call restores the
  correct height.

## Accessibility

- Accessible name unchanged (sr-only label + placeholder retained).
- Enter-to-send / Shift+Enter-for-newline is the conventional chat affordance;
  the visible send button remains the discoverable path. Optional: mention the
  Shift+Enter shortcut in the existing `els.hint` copy — out of scope unless cheap.

## Acceptance criteria

1. Composer renders a `<textarea>` that starts visually identical to the old
   one-line input (same collapsed height, placeholder, focus ring).
2. Typing wraps to new lines and the box grows up to ~5 lines, then scrolls
   internally; the message list above and the send button never move off-screen.
3. **Enter sends** the message (same result as clicking Send); **Shift+Enter**
   inserts a newline without sending.
4. After a send, the box collapses back to one row.
5. Clicking a chip populates the box and it sizes to the prompt (multi-line chip
   → multi-line box), without auto-sending.
6. Send button stays bottom-aligned and one-line-tall as the input grows.
7. No regression in the send/stream pipeline, busy-disable, or clear-thread flow.

## Verification (no automated harness for this layer)

This is Twig/CSS/JS with no PHPUnit/Jest coverage in the module. Verify live in
the running OpenEMR sidebar (dev stack, or Selenium per CLAUDE.md's browser-debug
section): type a multi-line question, confirm grow + cap + internal scroll, Enter
vs Shift+Enter, chip populate, and post-send collapse. Screenshot the collapsed
and expanded states.

## Promotion note

Per the branching workflow, this changes module `.js`/`.css`, so **bump
`$v_js_includes` in `version.php`** when promoting `qa/integration → main`, or
returning prod users keep the cached single-line box.
