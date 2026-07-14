/**
 * Clinical Co-Pilot docked sidebar.
 *
 * Runs in the OpenEMR outer shell (interface/main/tabs/main.php). Renders a VS Code-style docked
 * panel, toggled from a button injected into the patient banner, scoped to the active patient. The
 * panel holds a SMART patient-scoped token obtained through the hidden-iframe EHR-launch flow; the
 * token's own `patient` claim -- not any id read from the page -- is what is sent to the agent, so
 * the browser cannot ask about a patient the token does not permit.
 *
 * See context/specs/copilot-sidebar.md.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

(function () {
    'use strict';

    var EXPIRY_SKEW_MS = 60 * 1000; // re-launch this long before the token's stated expiry
    var LAUNCH_TIMEOUT_MS = 30 * 1000; // a launch that never posts back must not hang the panel
    var LAUNCH_FAILURES = ['launch_failed', 'token_exchange_failed', 'launch_timeout'];

    var LS_OPEN = 'aicopilot.open';
    var LS_WIDTH = 'aicopilot.width'; // user-global UI prefs; NOT PHI (spec §4)
    var MIN_WIDTH_PX = 320;
    var MAX_WIDTH_FRAC = 0.5; // of viewport width

    var config = null;
    var labels = {};
    var els = {};
    var toggleBtn = null;

    // Per-active-patient runtime state.
    var activePid = null;
    var token = null; // {accessToken, patient, expiresAt}
    var launchInFlight = null;

    // Click-to-source PDF preview (JOS-57). A monotonic id guards against a stale render (a slow PDF
    // fetch) painting over a newer click; `previewTrigger` is the chip we return focus to on close.
    var previewRequestId = 0;
    var previewTrigger = null;

    // ---- config / labels -------------------------------------------------

    function readConfig() {
        var node = document.getElementById('ai-copilot-config');
        if (!node) {
            return null;
        }
        try {
            return JSON.parse(node.textContent);
        } catch (err) {
            return null;
        }
    }

    // ---- active patient --------------------------------------------------

    /**
     * Read the active patient's pid from the shell's Knockout view model.
     *
     * @returns {string|null} The pid as a string, or null when no chart is open.
     */
    function currentPid() {
        try {
            var vm = window.app_view_model;
            var patient = vm && vm.application_data && vm.application_data.patient
                ? vm.application_data.patient()
                : null;
            if (!patient || typeof patient.pid !== 'function') {
                return null;
            }
            var pid = patient.pid();
            return pid ? String(pid) : null;
        } catch (err) {
            return null;
        }
    }

    function currentPatientName() {
        // Prefer the banner's own name node: it is reliably populated by the time the banner shows a
        // patient, whereas the KO name observable can lag the pid observable on a fresh switch.
        var nameNode = document.querySelector('#attendantData .ptName');
        if (nameNode && nameNode.textContent.trim()) {
            // The banner node carries newlines and the "(pubpid)" suffix; collapse to one clean line.
            return nameNode.textContent.replace(/\s+/g, ' ').trim();
        }
        try {
            var vm = window.app_view_model;
            var patient = vm && vm.application_data && vm.application_data.patient
                ? vm.application_data.patient()
                : null;
            if (patient && typeof patient.name === 'function') {
                return patient.name() || '';
            }
        } catch (err) {
            // fall through
        }
        return '';
    }

    /**
     * Reconcile UI + state with the currently active patient.
     *
     * Called whenever the banner re-renders (which happens on every patient switch) and on init.
     * Shows/hides the toggle for the no-patient case, and on an actual patient change drops the held
     * token and resets the transcript so a token, the visible thread, and the chart never diverge.
     */
    function syncPatient() {
        ensureToggleInBanner();

        var pid = currentPid();
        if (pid === activePid) {
            return;
        }

        // Patient changed (or cleared): abandon the old patient's token and view.
        activePid = pid;
        token = null;
        launchInFlight = null;

        if (pid === null) {
            // No chart open -> the copilot has no subject. Hide it entirely.
            if (toggleBtn) {
                toggleBtn.hidden = true;
            }
            closeSidebar();
            return;
        }

        if (toggleBtn) {
            toggleBtn.hidden = false;
        }
        els.patient.textContent = currentPatientName();
        resetTranscript();
        setHasConversation(false); // unknown until the thread loads; loadThread flips it on when it exists
        loadThread(pid); // Phase 3 stub: restores this patient's saved conversation
    }

    // ---- banner toggle button --------------------------------------------

    /**
     * Ensure the toggle button lives inside the patient banner.
     *
     * The banner (`#attendantData`) is Knockout-rendered and replaced wholesale on every patient
     * change, which discards any node we placed inside it. We hold the button in a JS variable and
     * re-append it whenever it has been evicted, so it survives re-renders. Placed next to the
     * DOB/Age line (`.mt-2`) when present, else appended to the banner.
     */
    function ensureToggleInBanner() {
        var banner = document.getElementById('attendantData');
        if (!banner || !toggleBtn) {
            return;
        }
        if (banner.contains(toggleBtn)) {
            return;
        }
        var anchor = banner.querySelector('.form-group .mt-2') || banner.querySelector('.mt-2') || banner;
        anchor.appendChild(toggleBtn);
    }

    /**
     * Toggle the Launcher's has-conversation hint dot (spec §4.2): a saved thread exists for the
     * active patient. Called by the Phase 3 load path; until persistence lands there is nothing to
     * detect, so the dot stays hidden.
     *
     * @param {boolean} has Whether a saved conversation exists for the active patient.
     */
    function setHasConversation(has) {
        if (!els.hint) {
            return;
        }
        els.hint.hidden = !has;
        if (has && labels.hasConversation) {
            els.hint.title = labels.hasConversation;
        }
    }

    // ---- open / close / resize -------------------------------------------

    function isOpen() {
        return !els.sidebar.hidden;
    }

    function openSidebar() {
        if (currentPid() === null) {
            return; // never open without a patient
        }
        els.sidebar.hidden = false;
        els.sidebar.setAttribute('aria-hidden', 'false');
        document.body.classList.add('ai-copilot-open');
        if (toggleBtn) {
            toggleBtn.setAttribute('aria-expanded', 'true');
        }
        try {
            window.localStorage.setItem(LS_OPEN, '1');
        } catch (err) {
            // storage unavailable -> just skip persistence
        }
        els.input.focus();
        autoGrowInput(); // size a restored draft correctly on open
    }

    function closeSidebar() {
        els.sidebar.hidden = true;
        els.sidebar.setAttribute('aria-hidden', 'true');
        document.body.classList.remove('ai-copilot-open');
        if (toggleBtn) {
            toggleBtn.setAttribute('aria-expanded', 'false');
        }
        try {
            window.localStorage.setItem(LS_OPEN, '0');
        } catch (err) {
            // ignore
        }
    }

    function toggleSidebar() {
        if (isOpen()) {
            closeSidebar();
        } else {
            openSidebar();
        }
    }

    function clampWidth(px) {
        var max = Math.floor(window.innerWidth * MAX_WIDTH_FRAC);
        return Math.max(MIN_WIDTH_PX, Math.min(px, max));
    }

    function applyWidth(px) {
        document.documentElement.style.setProperty('--ai-copilot-width', clampWidth(px) + 'px');
    }

    function restoreWidth() {
        try {
            var saved = parseInt(window.localStorage.getItem(LS_WIDTH), 10);
            if (!isNaN(saved)) {
                applyWidth(saved);
            }
        } catch (err) {
            // ignore -> CSS default (20vw) stands
        }
    }

    function initResize() {
        var dragging = false;

        function onMove(e) {
            if (!dragging) {
                return;
            }
            // Panel is docked right: width = distance from the pointer to the right viewport edge.
            applyWidth(window.innerWidth - e.clientX);
        }

        function onUp() {
            if (!dragging) {
                return;
            }
            dragging = false;
            document.body.style.userSelect = '';
            document.body.classList.remove('ai-copilot-resizing');
            window.removeEventListener('pointermove', onMove);
            window.removeEventListener('pointerup', onUp);
            var current = getComputedStyle(document.documentElement).getPropertyValue('--ai-copilot-width');
            try {
                window.localStorage.setItem(LS_WIDTH, String(parseInt(current, 10)));
            } catch (err) {
                // ignore
            }
        }

        els.resizer.addEventListener('pointerdown', function (e) {
            dragging = true;
            document.body.style.userSelect = 'none'; // stop text selection while dragging
            document.body.classList.add('ai-copilot-resizing');
            window.addEventListener('pointermove', onMove);
            window.addEventListener('pointerup', onUp);
            e.preventDefault();
        });
    }

    // ---- status / transcript rendering -----------------------------------

    function setStatus(message, isError) {
        els.status.textContent = message || '';
        els.status.classList.toggle('ai-copilot__status--error', Boolean(isError));
    }

    function setBusy(busy) {
        els.send.disabled = busy;
        els.input.disabled = busy;
    }

    function resetTranscript() {
        // Rebuild the empty state (intro + chips) from the server-rendered template we cached.
        els.transcript.innerHTML = '';
        if (els.emptyTemplate) {
            els.transcript.appendChild(els.emptyTemplate.cloneNode(true));
            wireChips();
        }
        els.clear.hidden = true;
    }

    function appendNode(node) {
        var empty = els.transcript.querySelector('.ai-copilot__empty');
        if (empty) {
            empty.remove();
        }
        els.transcript.appendChild(node);
        els.transcript.scrollTop = els.transcript.scrollHeight;
        els.clear.hidden = false;
    }

    function appendQuestion(text) {
        var el = document.createElement('p');
        el.className = 'ai-copilot__question';
        el.textContent = text;
        appendNode(el);
    }

    function appendError(message) {
        var el = document.createElement('p');
        el.className = 'ai-copilot__error';
        el.textContent = message;
        appendNode(el);
    }

    // The Co-Pilot spark, matching the banner toggle's icon. Built with the SVG namespace (not
    // innerHTML) so it needs no HTML sanitising and satisfies the no-inner-html lint rule.
    var AVATAR_PATH = 'M12 2c.5 4 1 6.5 10 10-9 3.5-9.5 6-10 10-.5-4-1-6.5-10-10 9-3.5 9.5-6 10-10z';

    /**
     * Build the small Co-Pilot avatar that leads every assistant turn (answer or pending), marking
     * it as the assistant speaking — the counterpart to the physician's right-aligned question bubble.
     *
     * @returns {HTMLElement} A decorative avatar span (aria-hidden — the turn's text is the content).
     */
    function buildAvatar() {
        var ns = 'http://www.w3.org/2000/svg';
        var avatar = document.createElement('span');
        avatar.className = 'ai-copilot__avatar';
        avatar.setAttribute('aria-hidden', 'true');

        var svg = document.createElementNS(ns, 'svg');
        svg.setAttribute('viewBox', '0 0 24 24');
        var path = document.createElementNS(ns, 'path');
        path.setAttribute('d', AVATAR_PATH);
        svg.appendChild(path);
        avatar.appendChild(svg);
        return avatar;
    }

    // Wrap an assistant-side node (answer bubble or pending indicator) in an avatar-led row.
    function assistantTurn(bubble) {
        var row = document.createElement('div');
        row.className = 'ai-copilot__turn';
        row.appendChild(buildAvatar());
        row.appendChild(bubble);
        return row;
    }

    /**
     * Show the in-flight indicator between the question and the answer (spec §5.3.1): an animated
     * typing indicator plus an optional grounded caption, led by the Co-Pilot avatar. The whole row
     * is returned so the caller can swap it for the answer (or an error) when the turn resolves; the
     * inner ``.ai-copilot__pending`` bubble is the seam a future streaming increment fills.
     *
     * @returns {HTMLElement} The pending-turn row, already appended to the transcript.
     */
    function appendPending() {
        var pending = document.createElement('div');
        pending.className = 'ai-copilot__pending';
        // This is the only in-turn progress affordance, so let it announce: role="status" gives it an
        // implicit aria-live="polite" region for screen readers.
        pending.setAttribute('role', 'status');

        var dots = document.createElement('div');
        dots.className = 'ai-copilot__dots';
        for (var i = 0; i < 3; i++) {
            dots.appendChild(document.createElement('span'));
        }
        pending.appendChild(dots);

        if (labels.thinking) {
            var caption = document.createElement('p');
            caption.className = 'ai-copilot__thinking';
            caption.textContent = labels.thinking;
            pending.appendChild(caption);
        }

        var row = assistantTurn(pending);
        appendNode(row);
        return row;
    }

    function removePending(pending) {
        if (pending && pending.parentNode) {
            pending.parentNode.removeChild(pending);
        }
    }

    /**
     * Render one grounded answer: prose summary, then every claim with its structured citation.
     *
     * @param {{summary: string, claims: Array<{text: string, source: Object}>}} answer
     */
    function appendAnswer(answer) {
        var wrapper = document.createElement('article');
        wrapper.className = 'ai-copilot__answer';

        var summary = document.createElement('p');
        summary.className = 'ai-copilot__summary';
        summary.textContent = answer.summary || '';
        wrapper.appendChild(summary);

        var claims = Array.isArray(answer.claims) ? answer.claims : [];
        if (claims.length > 0) {
            // Provenance is collapsed by default: the clinician reads the narrative; the evidence
            // trail is one click away for anyone who wants to verify or audit a claim.
            var details = document.createElement('details');
            details.className = 'ai-copilot__evidence';

            var toggle = document.createElement('summary');
            toggle.className = 'ai-copilot__evidence-toggle';
            toggle.textContent = 'Show evidence (' + claims.length + ')';
            details.appendChild(toggle);

            var list = document.createElement('ol');
            list.className = 'ai-copilot__claims';
            claims.forEach(function (claim) {
                list.appendChild(renderClaim(claim));
            });
            details.appendChild(list);
            wrapper.appendChild(details);
        }

        var followUps = renderFollowUps(answer.follow_ups);
        if (followUps) {
            wrapper.appendChild(followUps);
        }
        appendNode(assistantTurn(wrapper));
    }

    /**
     * Build the per-answer follow-up suggestions: agent-proposed next questions, rendered as chips
     * that populate the composer on click (never auto-send, matching the starter chips / spec §6).
     * Only the latest answer carries these; removeFollowUps() clears prior ones when a new turn
     * starts, so the panel never accumulates stale suggestions.
     *
     * @param {Array<string>|undefined} suggestions The `follow_ups` strings from the answer payload.
     * @returns {HTMLElement|null} The follow-ups block, or null when there is nothing to suggest.
     */
    function renderFollowUps(suggestions) {
        var prompts = Array.isArray(suggestions)
            ? suggestions.filter(function (s) { return typeof s === 'string' && s.trim() !== ''; })
            : [];
        if (prompts.length === 0) {
            return null;
        }

        var block = document.createElement('div');
        block.className = 'ai-copilot__followups';
        // A labelled group so screen readers announce these as suggested questions, not stray buttons.
        block.setAttribute('role', 'group');
        if (labels.followUps) {
            block.setAttribute('aria-label', labels.followUps);
            var heading = document.createElement('p');
            heading.className = 'ai-copilot__followups-label';
            heading.textContent = labels.followUps;
            block.appendChild(heading);
        }

        prompts.forEach(function (prompt) {
            var chip = document.createElement('button');
            chip.type = 'button';
            chip.className = 'ai-copilot__chip ai-copilot__chip--followup';
            chip.setAttribute('data-prompt', prompt);
            chip.textContent = prompt;
            wireChip(chip);
            block.appendChild(chip);
        });
        return block;
    }

    // Drop any prior answer's follow-up chips: they were suggestions for the previous turn, so once
    // the physician asks something new they are stale. Keeps only the newest answer's suggestions.
    function removeFollowUps() {
        var blocks = els.transcript.querySelectorAll('.ai-copilot__followups');
        Array.prototype.forEach.call(blocks, function (block) {
            block.remove();
        });
    }

    /**
     * Turn a machine token into a human-readable label:
     * "clinical_status" -> "Clinical status", "AllergyIntolerance" -> "Allergy intolerance".
     *
     * @param {string} token snake_case field name or CamelCase FHIR resource type
     * @returns {string}
     */
    function humanizeToken(token) {
        if (!token) {
            return '';
        }
        var spaced = String(token)
            .replace(/_/g, ' ')                       // snake_case -> spaced words
            .replace(/([a-z0-9])([A-Z])/g, '$1 $2')   // CamelCase -> spaced words
            .trim()
            .toLowerCase();
        return spaced.charAt(0).toUpperCase() + spaced.slice(1);
    }

    /**
     * Format a record's key date for display: "2026-06-03T00:00:00+00:00" -> "Jun 3, 2026".
     *
     * Always formats in UTC. A date-only or midnight-UTC FHIR value (…T00:00:00+00:00) would
     * otherwise render as the *previous* calendar day for any browser west of UTC — the classic
     * off-by-one. Clinical record dates are day-granular, so UTC day is the right unit.
     *
     * @param {string} raw the record's date string as stamped by the agent
     * @returns {string} a human date, or the input verbatim if it isn't a parseable date
     */
    function formatCitationDate(raw) {
        if (!raw) {
            return '';
        }
        var ms = Date.parse(raw);
        if (isNaN(ms)) {
            return String(raw);                   // unrecognized format: show verbatim, never "Invalid Date"
        }
        try {
            return new Intl.DateTimeFormat('en-US', {
                year: 'numeric', month: 'short', day: 'numeric', timeZone: 'UTC'
            }).format(new Date(ms));
        } catch (e) {
            return String(raw);
        }
    }

    // Stamp the full `Type/id` reference onto a citation element for hover + audit (and, for
    // documents, as the hook the deep-link click handler reads).
    function tagResourceRef(cite, source) {
        if (source.resource_type && source.resource_id) {
            var ref = source.resource_type + '/' + source.resource_id;
            cite.title = ref;
            cite.setAttribute('data-resource-ref', ref);
        }
    }

    /**
     * Render a document/note citation: an evidence-forward chip that leads with the record's own
     * name ("Progress Note") and shows the verbatim note span (`quote`) that grounds the claim.
     *
     * The FHIR-type word ("Document reference") is clinician-noise here — it just duplicates the
     * note-type label — so it lives only in title/data-resource-ref, not as a visible badge. The
     * quote is the point: the gate already proved it is a real substring of the fetched note, so
     * showing it lets the physician read the record's own words instead of trusting the paraphrase.
     *
     * @param {object} source the citation's SourceRef
     * @returns {HTMLElement} a <cite> chip
     */
    function renderDocumentCitation(source) {
        var cite = document.createElement('cite');
        cite.className = 'ai-copilot__citation ai-copilot__citation--note';
        tagResourceRef(cite, source);

        // Header row: the record's own name + its key date.
        var header = document.createElement('span');
        header.className = 'ai-copilot__citation-header';

        var name = document.createElement('span');
        name.className = 'ai-copilot__citation-name';
        name.textContent = (source.label && source.label !== source.value)
            ? source.label
            : humanizeToken(source.resource_type);
        header.appendChild(name);

        if (source.date && source.date !== source.value) {
            var when = document.createElement('span');
            when.className = 'ai-copilot__citation-date';
            var pretty = formatCitationDate(source.date);
            when.textContent = source.date_label ? source.date_label + ' ' + pretty : pretty;
            header.appendChild(when);
        }
        cite.appendChild(header);

        if (source.quote) {
            var quote = document.createElement('blockquote');
            quote.className = 'ai-copilot__citation-quote';
            quote.textContent = source.quote;
            cite.appendChild(quote);
        }

        return cite;
    }

    /**
     * Render a coded (structured-resource) citation: a resource-type badge ("Condition"), the
     * record's own name + key date ("Asthma", "Onset 2019-04-02"), and the grounded field value.
     * Unlike a document, the type badge here is informative — it names the *kind* of record, which
     * the record's own name does not — so it stays.
     *
     * @param {object} source the citation's SourceRef
     * @returns {HTMLElement} a <cite> chip
     */
    function renderCodedCitation(source) {
        var cite = document.createElement('cite');
        cite.className = 'ai-copilot__citation';
        tagResourceRef(cite, source);

        var resource = document.createElement('span');
        resource.className = 'ai-copilot__citation-resource';
        resource.textContent = humanizeToken(source.resource_type);
        cite.appendChild(resource);

        // The record's own name + key date, stamped from the cited record by the agent (never
        // model-authored). Skip either when it merely repeats the cited field value (e.g. an
        // encounter whose cited field IS its start date) so the chip doesn't say the same thing twice.
        if (source.label && source.label !== source.value) {
            var name = document.createElement('span');
            name.className = 'ai-copilot__citation-name';
            name.textContent = source.label;
            cite.appendChild(name);
        }
        if (source.date && source.date !== source.value) {
            var when = document.createElement('span');
            when.className = 'ai-copilot__citation-date';
            var pretty = formatCitationDate(source.date);
            when.textContent = source.date_label ? source.date_label + ' ' + pretty : pretty;
            cite.appendChild(when);
        }

        if (source.field) {
            var field = document.createElement('span');
            field.className = 'ai-copilot__citation-field';
            // `value` is stamped in by the agent from the fetched record, never written by the model.
            var label = humanizeToken(source.field);
            field.textContent = source.value ? label + ': ' + source.value : label;
            cite.appendChild(field);
        }

        return cite;
    }

    // Render one citation as a <cite> chip. Documents/notes get the evidence-forward treatment
    // (name-led, verbatim quote); every other resource keeps the coded chip with its type badge. A
    // claim can draw on more than one record, so this runs once per citation (primary `source` +
    // each supporting).
    function renderCitation(source) {
        source = source || {};
        var cite = source.resource_type === 'DocumentReference'
            ? renderDocumentCitation(source)
            : renderCodedCitation(source);
        // Click-to-source (JOS-57): only when the agent stamped a bounding box -- i.e. the fact was
        // derived from an uploaded PDF. Absent a bbox the chip stands exactly as before; a rectangle
        // is never fabricated.
        if (hasBoundingBox(source)) {
            cite.appendChild(buildViewSourceButton(source));
        }
        return cite;
    }

    function renderClaim(claim) {
        var item = document.createElement('li');
        item.className = 'ai-copilot__claim';

        var text = document.createElement('span');
        text.className = 'ai-copilot__claim-text';
        text.textContent = claim.text;
        item.appendChild(text);

        // Primary citation, then any supporting citations. A statement that draws on more than one
        // record (a visit and a diagnosis) shows a chip for each, so the physician can see every
        // record it rests on — and spot when two are unrelated (e.g. different dates).
        item.appendChild(renderCitation(claim.source));
        var supporting = claim.supporting || [];
        for (var i = 0; i < supporting.length; i++) {
            item.appendChild(renderCitation(supporting[i]));
        }

        return item;
    }

    // ---- click-to-source PDF preview (JOS-57) ----------------------------
    //
    // When a citation carries a `bounding_box`, the fact was read off an uploaded PDF. The chip gets a
    // "View source" button that opens a preview pane inside this docked panel (never a Bootstrap
    // modal -- it stays a child of the fixed panel, well below the 1050+ dialog band), fetches the
    // Binary through the same SMART token the chat uses, renders the cited page with the vendored
    // pdf.js, and draws the cited rectangle over it.

    /**
     * Narrow, defensive check that a citation carries a usable bounding box: the box object plus a
     * document id and four finite numeric edges. Anything malformed falls back to "no rectangle".
     *
     * @param {object} source The citation's SourceRef.
     * @returns {boolean} True when a click-to-source affordance should be rendered.
     */
    function hasBoundingBox(source) {
        var bb = source && source.bounding_box;
        if (!bb || typeof bb !== 'object' || !source.document_id) {
            return false;
        }
        return isFiniteNumber(bb.x) && isFiniteNumber(bb.y)
            && isFiniteNumber(bb.width) && isFiniteNumber(bb.height);
    }

    function isFiniteNumber(value) {
        return typeof value === 'number' && isFinite(value);
    }

    /**
     * Resolve the 1-based page to render: prefer the SourceRef's `page`, fall back to the box's own
     * `page`, else page 1. Floored so a fractional value can't index a fractional page.
     *
     * @param {object} source The citation's SourceRef.
     * @returns {number} A 1-based, integral page number.
     */
    function normalizePage(source) {
        var page = source.page;
        if (!isFiniteNumber(page) || page < 1) {
            var boxPage = source.bounding_box && source.bounding_box.page;
            page = (isFiniteNumber(boxPage) && boxPage >= 1) ? boxPage : 1;
        }
        return Math.floor(page);
    }

    /**
     * Build the "View source" button appended to a bbox-bearing citation. The document id, page, and
     * serialized box ride on the button's dataset so the click handler is self-contained.
     *
     * @param {object} source The citation's SourceRef (already passed hasBoundingBox).
     * @returns {HTMLButtonElement} The keyboard-accessible affordance.
     */
    function buildViewSourceButton(source) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'ai-copilot__view-source';
        button.textContent = labels.viewSource;
        button.dataset.documentId = String(source.document_id);
        button.dataset.page = String(normalizePage(source));
        button.dataset.bbox = JSON.stringify(source.bounding_box);
        button.dataset.docTitle = (source.label && source.label !== source.value)
            ? source.label
            : (humanizeToken(source.resource_type) || labels.previewSourceFallback);
        button.addEventListener('click', onViewSourceClick);
        return button;
    }

    // Read the request off the clicked chip and hand it to the preview pane.
    function onViewSourceClick(event) {
        var button = event.currentTarget;
        var bbox = null;
        try {
            bbox = JSON.parse(button.dataset.bbox);
        } catch (err) {
            bbox = null;
        }
        var documentId = button.dataset.documentId;
        var page = parseInt(button.dataset.page, 10);
        if (!documentId || !bbox || isNaN(page)) {
            return;
        }
        previewTrigger = button;
        openPreview({
            documentId: documentId,
            page: page,
            bbox: bbox,
            title: button.dataset.docTitle || labels.previewSourceFallback
        });
    }

    /**
     * Lazily build the preview pane and cache its parts on `els`. Built once and reused across
     * clicks; a child of the fixed sidebar so it overlays the transcript without escaping the panel.
     */
    function ensurePreviewPane() {
        if (els.preview) {
            return;
        }
        var preview = document.createElement('div');
        preview.className = 'ai-copilot__preview';
        preview.setAttribute('role', 'dialog');
        preview.setAttribute('aria-label', labels.previewSourceFallback);
        preview.setAttribute('aria-hidden', 'true');
        preview.hidden = true;

        var header = document.createElement('header');
        header.className = 'ai-copilot__preview-header';

        var title = document.createElement('p');
        title.className = 'ai-copilot__preview-title';
        header.appendChild(title);

        var close = document.createElement('button');
        close.type = 'button';
        close.className = 'ai-copilot__preview-close';
        close.setAttribute('aria-label', labels.previewClose);
        close.textContent = '×'; // U+00D7 multiplication sign -- matches the panel's close glyph
        header.appendChild(close);
        preview.appendChild(header);

        var body = document.createElement('div');
        body.className = 'ai-copilot__preview-body';

        var status = document.createElement('p');
        status.className = 'ai-copilot__preview-status';
        status.setAttribute('role', 'status');
        status.setAttribute('aria-live', 'polite');
        status.hidden = true;
        body.appendChild(status);

        var wrap = document.createElement('div');
        wrap.className = 'ai-copilot__preview-canvas-wrap';

        var canvas = document.createElement('canvas');
        canvas.className = 'ai-copilot__preview-canvas';
        wrap.appendChild(canvas);

        var bbox = document.createElement('div');
        bbox.className = 'ai-copilot__preview-bbox';
        bbox.setAttribute('aria-hidden', 'true');
        bbox.hidden = true;
        wrap.appendChild(bbox);

        body.appendChild(wrap);
        preview.appendChild(body);

        close.addEventListener('click', closePreview);
        // Escape closes the pane without collapsing the whole sidebar (stop it reaching a shell handler).
        preview.addEventListener('keydown', function (event) {
            if (event.key === 'Escape') {
                event.stopPropagation();
                closePreview();
            }
        });

        els.sidebar.appendChild(preview);
        els.preview = preview;
        els.previewTitle = title;
        els.previewClose = close;
        els.previewStatus = status;
        els.previewBody = body;
        els.previewCanvasWrap = wrap;
        els.previewCanvas = canvas;
        els.previewBbox = bbox;
    }

    /**
     * Reveal the preview pane for a fresh request, reset its canvas/overlay, and kick off the render.
     *
     * @param {{documentId: string, page: number, bbox: object, title: string}} req The source to show.
     */
    function openPreview(req) {
        ensurePreviewPane();
        var requestId = ++previewRequestId; // supersede any in-flight render
        els.previewTitle.textContent = req.title;
        els.preview.hidden = false;
        els.preview.setAttribute('aria-hidden', 'false');
        els.previewBbox.hidden = true;
        clearPreviewCanvas();
        setPreviewStatus(labels.previewLoading, false);
        els.previewClose.focus();
        renderPdfPreview(req, requestId);
    }

    /**
     * Close the preview, invalidate any in-flight render, and return focus to the triggering chip.
     */
    function closePreview() {
        previewRequestId++; // any render still resolving will see a stale id and bail
        if (els.preview) {
            els.preview.hidden = true;
            els.preview.setAttribute('aria-hidden', 'true');
        }
        if (previewTrigger && typeof previewTrigger.focus === 'function') {
            previewTrigger.focus();
        }
        previewTrigger = null;
    }

    function setPreviewStatus(message, isError) {
        els.previewStatus.textContent = message || '';
        els.previewStatus.hidden = !message;
        els.previewStatus.classList.toggle('ai-copilot__preview-status--error', Boolean(isError));
    }

    function clearPreviewCanvas() {
        var canvas = els.previewCanvas;
        var ctx = canvas.getContext('2d');
        if (ctx) {
            ctx.clearRect(0, 0, canvas.width, canvas.height);
        }
    }

    // Fetch the PDF bytes for a Binary through the SMART token, same base URL the chat is scoped to.
    function fetchBinary(documentId, accessToken) {
        return fetch(config.fhirBaseUrl + '/Binary/' + encodeURIComponent(documentId), {
            headers: {
                'Authorization': 'Bearer ' + accessToken,
                'Accept': 'application/pdf'
            }
        });
    }

    // Fetch, and on a 401 drop the token and re-launch once (mirrors sendTurn's single-retry).
    function fetchBinaryWithReauth(documentId, accessToken) {
        return fetchBinary(documentId, accessToken).then(function (response) {
            if (response.status !== 401) {
                return response;
            }
            token = null;
            return ensureToken().then(function (fresh) {
                return fetchBinary(documentId, fresh.accessToken);
            });
        });
    }

    /**
     * Fetch the cited PDF and render its page with the vendored pdf.js, then draw the bounding box.
     * Every step re-checks `requestId` so a superseding click (or a close) abandons this render
     * instead of clobbering the newer one. Auth/404/parse failures surface inline in the pane.
     *
     * @param {{documentId: string, page: number, bbox: object}} req The source to render.
     * @param {number} requestId This render's generation, compared against `previewRequestId`.
     */
    function renderPdfPreview(req, requestId) {
        var pdfjs = window.pdfjsLib;
        if (!pdfjs) {
            setPreviewStatus(labels.previewError, true);
            return;
        }
        if (!config.fhirBaseUrl) {
            setPreviewStatus(labels.previewError, true);
            return;
        }

        // Point pdf.js at the locally-vendored worker (CSP forbids its CDN default). Idempotent.
        if (config.pdfWorkerUrl && !pdfjs.GlobalWorkerOptions.workerSrc) {
            pdfjs.GlobalWorkerOptions.workerSrc = config.pdfWorkerUrl;
        }

        ensureToken()
            .then(function (fresh) {
                return fetchBinaryWithReauth(req.documentId, fresh.accessToken);
            })
            .then(function (response) {
                if (!response.ok) {
                    // Both messages are ours (localized), safe to show; flag them so the catch can tell
                    // them from a raw fetch/parse failure.
                    var reason = response.status === 404 ? labels.previewNotFound : labels.previewError;
                    var httpErr = new Error(reason);
                    httpErr.controlled = true;
                    throw httpErr;
                }
                return response.arrayBuffer();
            })
            .then(function (buffer) {
                if (requestId !== previewRequestId) {
                    return null; // a newer click (or close) won the race -- drop this one
                }
                return pdfjs.getDocument({ data: new Uint8Array(buffer) }).promise;
            })
            .then(function (pdf) {
                if (!pdf || requestId !== previewRequestId) {
                    return null;
                }
                var pageNum = Math.min(Math.max(req.page, 1), pdf.numPages);
                return pdf.getPage(pageNum);
            })
            .then(function (page) {
                if (!page || requestId !== previewRequestId) {
                    return;
                }
                paintPage(page, req.bbox, requestId);
            })
            .catch(function (err) {
                if (requestId !== previewRequestId) {
                    return;
                }
                setPreviewStatus(err && err.controlled ? err.message : labels.previewError, true);
            });
    }

    /**
     * Rasterize the page to the canvas at the pane width, then overlay the bounding box.
     *
     * The canvas backing store is scaled by devicePixelRatio for a crisp render, while its CSS size
     * stays in layout pixels so the bbox overlay (also in layout pixels) lines up. The box is given
     * in native page pixels, which equal the scale-1 viewport's units, so one `scale` maps both.
     *
     * @param {object} page A pdf.js PDFPageProxy.
     * @param {{x: number, y: number, width: number, height: number}} bbox Native-page-pixel rectangle.
     * @param {number} requestId This render's generation.
     */
    function paintPage(page, bbox, requestId) {
        var cssWidth = els.previewCanvasWrap.clientWidth || els.previewBody.clientWidth;
        var base = page.getViewport({ scale: 1 });
        var scale = cssWidth / base.width;
        var viewport = page.getViewport({ scale: scale });
        var outputScale = window.devicePixelRatio || 1;

        var canvas = els.previewCanvas;
        canvas.width = Math.floor(viewport.width * outputScale);
        canvas.height = Math.floor(viewport.height * outputScale);
        canvas.style.width = Math.floor(viewport.width) + 'px';
        canvas.style.height = Math.floor(viewport.height) + 'px';

        var renderTask = page.render({
            canvasContext: canvas.getContext('2d'),
            viewport: viewport,
            // Map the extra device-pixel resolution onto the same viewport (identity when DPR is 1).
            transform: outputScale !== 1 ? [outputScale, 0, 0, outputScale, 0, 0] : undefined
        });

        renderTask.promise.then(function () {
            if (requestId !== previewRequestId) {
                return;
            }
            setPreviewStatus('', false);
            positionBbox(bbox, scale);
        }).catch(function () {
            if (requestId !== previewRequestId) {
                return;
            }
            setPreviewStatus(labels.previewError, true);
        });
    }

    // Place the highlight rectangle over the rendered page and scroll it into view.
    function positionBbox(bbox, scale) {
        var box = els.previewBbox;
        box.style.left = (bbox.x * scale) + 'px';
        box.style.top = (bbox.y * scale) + 'px';
        box.style.width = (bbox.width * scale) + 'px';
        box.style.height = (bbox.height * scale) + 'px';
        box.hidden = false;
        if (typeof box.scrollIntoView === 'function') {
            box.scrollIntoView({ block: 'center', inline: 'nearest' });
        }
    }

    // ---- SMART launch + chat (reused mechanism) --------------------------

    function tokenIsFresh() {
        return token !== null && Date.now() < token.expiresAt - EXPIRY_SKEW_MS;
    }

    function runLaunch() {
        return new Promise(function (resolve, reject) {
            var frame = document.createElement('iframe');
            frame.hidden = true;
            frame.setAttribute('aria-hidden', 'true');
            frame.title = 'SMART launch';
            frame.style.display = 'none';

            var settled = false;
            var timer = null;

            function cleanup() {
                window.removeEventListener('message', onMessage);
                if (timer !== null) {
                    window.clearTimeout(timer);
                }
                if (frame.parentNode) {
                    frame.parentNode.removeChild(frame);
                }
            }

            function onMessage(event) {
                if (event.source !== frame.contentWindow) {
                    return;
                }
                if (event.origin !== config.expectedOrigin) {
                    return;
                }
                var data = event.data;
                if (!data || data.source !== config.messageSource) {
                    return;
                }
                settled = true;
                cleanup();
                if (data.type === 'token') {
                    resolve({
                        accessToken: data.accessToken,
                        patient: data.patient,
                        expiresAt: Date.now() + (data.expiresIn * 1000)
                    });
                } else {
                    reject(new Error(data.reason || 'launch_failed'));
                }
            }

            window.addEventListener('message', onMessage);
            timer = window.setTimeout(function () {
                if (!settled) {
                    cleanup();
                    reject(new Error('launch_timeout'));
                }
            }, LAUNCH_TIMEOUT_MS);

            frame.src = config.launchUrl + '?csrf_token=' + encodeURIComponent(config.csrfToken);
            document.body.appendChild(frame);
        });
    }

    function ensureToken() {
        if (tokenIsFresh()) {
            return Promise.resolve(token);
        }
        if (launchInFlight) {
            return launchInFlight;
        }
        // No progress message here: the in-flight pending bubble (appendPending) is the single
        // "working" affordance, and it is already on screen before this runs.
        launchInFlight = runLaunch()
            .then(function (fresh) {
                token = fresh;
                launchInFlight = null;
                return fresh;
            })
            .catch(function (err) {
                token = null;
                launchInFlight = null;
                throw err;
            });
        return launchInFlight;
    }

    function postChat(message) {
        return fetch(config.chatUrl, {
            method: 'POST',
            mode: 'cors',
            headers: {
                'Content-Type': 'application/json',
                'Authorization': 'Bearer ' + token.accessToken
            },
            body: JSON.stringify({ patient_id: token.patient, message: message })
        });
    }

    function sendTurn(message) {
        return ensureToken()
            .then(postChat.bind(null, message))
            .then(function (response) {
                if (response.status !== 401) {
                    return response;
                }
                token = null; // expired between the freshness check and the FHIR read -> re-launch once
                return ensureToken().then(postChat.bind(null, message));
            });
    }

    function submitMessage(message) {
        removeFollowUps(); // the prior answer's suggestions no longer apply to this new question
        appendQuestion(message);
        setBusy(true);
        setStatus('', false);
        var pending = appendPending(); // in-flight indicator; swapped for the answer or an error below
        var pidForTurn = activePid;

        sendTurn(message)
            .then(function (response) {
                // Read the body defensively: a proxy 5xx (or a crashed agent) can return HTML or an
                // empty body, which response.json() would reject with a raw SyntaxError. Parse what
                // we can and treat anything unparseable as an empty body.
                return response.text().then(function (text) {
                    var body = {};
                    try {
                        body = text ? JSON.parse(text) : {};
                    } catch (parseErr) {
                        body = {};
                    }
                    if (!response.ok) {
                        // Errors the agent authored (its {error} contract) are safe to show; flag
                        // them so the catch below can tell them from raw browser failures.
                        var httpErr = new Error(body.error || labels.unavailable);
                        httpErr.controlled = true;
                        throw httpErr;
                    }
                    return body;
                });
            })
            .then(function (answer) {
                removePending(pending);
                appendAnswer(answer);
                persistTurn(pidForTurn, message, answer); // Phase 3 stub
            })
            .catch(function (err) {
                removePending(pending);
                if (LAUNCH_FAILURES.indexOf(err.message) !== -1) {
                    setStatus(labels.authFailed, true);
                    appendError(labels.authFailed);
                    return;
                }
                // Only surface a message we authored (the agent's {error} body). Raw browser
                // failures — "Failed to fetch" (network/timeout/CORS), JSON parse errors from a
                // proxy error page — get the friendly fallback, never the raw text in front of a
                // clinician.
                appendError(err.controlled ? err.message : labels.unavailable);
            })
            .finally(function () {
                setBusy(false);
                els.input.focus();
            });
    }

    // ---- conversation persistence (Phase 3 stubs) ------------------------
    // Implemented in Phase 3 against config.conversationUrl. Left as no-ops so the turn lifecycle
    // and patient-switch code is stable across phases.

    function loadThread(_pid) {
        // Phase 3: GET the saved thread for (session user, pid) and re-render it as history.
    }

    function persistTurn(_pid, _question, _answer) {
        // Phase 3: POST-upsert the thread for (session user, pid), debounced.
    }

    function clearThread() {
        // Phase 3: DELETE the thread for (session user, pid). For now, just reset the view.
        resetTranscript();
        setHasConversation(false); // no saved thread remains for this patient
    }

    // ---- wiring ----------------------------------------------------------

    /**
     * Copy a prompt into the composer for the physician to review, then focus it.
     *
     * Shared by the starter chips and the per-answer follow-up chips: both populate the input and
     * do NOT auto-send (spec §6) -- the physician always reviews before the record is queried.
     *
     * @param {string} prompt The question text to place in the composer.
     */
    function populateInput(prompt) {
        els.input.value = prompt;
        autoGrowInput(); // size to the (possibly multi-line) chip prompt
        els.input.focus();
    }

    // Bind one chip so clicking it populates the composer with its prompt.
    function wireChip(chip) {
        chip.addEventListener('click', function () {
            populateInput(chip.getAttribute('data-prompt') || chip.textContent);
        });
    }

    function wireChips() {
        var chips = els.transcript.querySelectorAll('.ai-copilot__chip');
        Array.prototype.forEach.call(chips, wireChip);
    }

    // Grow the textarea to fit its content; CSS max-height + overflow-y cap it.
    // Setting .value in JS does NOT fire 'input', so callers that assign the value
    // (send-reset, chip populate, open) must invoke this explicitly.
    function autoGrowInput() {
        els.input.style.height = 'auto';
        els.input.style.height = els.input.scrollHeight + 'px';
    }

    // Enter sends; Shift+Enter inserts a newline (textarea default).
    function onInputKeydown(event) {
        if (event.key !== 'Enter' || event.shiftKey) {
            return;
        }
        // Don't hijack Enter while an IME candidate is being confirmed.
        if (event.isComposing || event.keyCode === 229) {
            return;
        }
        event.preventDefault();
        // Reuse the form submit path (same validation/onSubmit; no-op while disabled).
        els.form.requestSubmit(els.send);
    }

    function onSubmit(event) {
        event.preventDefault();
        var message = els.input.value.trim();
        if (message === '') {
            return;
        }
        els.input.value = '';
        autoGrowInput(); // collapse back to one row after send
        submitMessage(message);
    }

    function onClear() {
        if (window.confirm(labels.clearConfirm)) {
            clearThread();
        }
    }

    function init() {
        els.sidebar = document.getElementById('ai-copilot-sidebar');
        if (!els.sidebar) {
            return;
        }
        config = readConfig();
        if (!config) {
            return;
        }

        labels = {
            authFailed: els.sidebar.dataset.labelAuthFailed,
            unavailable: els.sidebar.dataset.labelUnavailable,
            clearConfirm: els.sidebar.dataset.labelClearConfirm,
            thinking: els.sidebar.dataset.labelThinking,
            hasConversation: els.sidebar.dataset.labelHasConversation,
            followUps: els.sidebar.dataset.labelFollowUps,
            // Click-to-source (JOS-57). Defaulted here so the feature works before the PHP config
            // island grows these data-label-* attributes; add them there for localization.
            viewSource: els.sidebar.dataset.labelViewSource || 'View source',
            previewClose: els.sidebar.dataset.labelPreviewClose || 'Close source preview',
            previewSourceFallback: els.sidebar.dataset.labelPreviewSource || 'Source document',
            previewLoading: els.sidebar.dataset.labelPreviewLoading || 'Loading source document...',
            previewError: els.sidebar.dataset.labelPreviewError || 'Could not load the source document.',
            previewNotFound: els.sidebar.dataset.labelPreviewNotFound || 'Source document not found.'
        };

        els.resizer = document.getElementById('ai-copilot-resizer');
        els.status = document.getElementById('ai-copilot-status');
        els.patient = document.getElementById('ai-copilot-patient');
        els.transcript = document.getElementById('ai-copilot-transcript');
        els.input = document.getElementById('ai-copilot-input');
        els.send = document.getElementById('ai-copilot-send');
        els.form = document.getElementById('ai-copilot-form');
        els.clear = document.getElementById('ai-copilot-clear');
        els.close = document.getElementById('ai-copilot-close');
        els.hint = document.getElementById('ai-copilot-hint');

        // Cache the server-rendered empty state so resetTranscript() can restore it verbatim.
        els.emptyTemplate = els.transcript.querySelector('.ai-copilot__empty').cloneNode(true);

        // Adopt the toggle button out of the shell body; we manage its placement in the banner.
        toggleBtn = document.getElementById('ai-copilot-toggle');
        if (toggleBtn && toggleBtn.parentNode) {
            toggleBtn.parentNode.removeChild(toggleBtn);
        }

        // Events.
        els.form.addEventListener('submit', onSubmit);
        els.input.addEventListener('input', autoGrowInput);
        els.input.addEventListener('keydown', onInputKeydown);
        els.clear.addEventListener('click', onClear);
        els.close.addEventListener('click', closeSidebar);
        if (toggleBtn) {
            toggleBtn.addEventListener('click', toggleSidebar);
        }
        wireChips();
        initResize();
        restoreWidth();

        // Track patient changes: the banner re-renders on every switch, so observing it catches
        // both the button eviction and the patient change in one signal.
        var banner = document.getElementById('attendantData');
        if (banner) {
            new MutationObserver(function () {
                syncPatient();
            }).observe(banner, { childList: true, subtree: true });
        }

        syncPatient();

        // Restore the open state -- but only if a patient is active (openSidebar guards this).
        var wantOpen = false;
        try {
            wantOpen = window.localStorage.getItem(LS_OPEN) === '1';
        } catch (err) {
            wantOpen = false;
        }
        if (wantOpen) {
            openSidebar();
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', init);
    } else {
        init();
    }
}());
