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
     * Render one grounded answer: the synthesized prose, then a collapsed evidence section grouping
     * the answer's provenance into trust tiers (spec §3.5).
     *
     * @param {{summary: string, evidence: Array<Object>,
     *   claims: Array<{text: string, source: Object, citations: Array<{source_type: string}>}>}} answer
     */
    function appendAnswer(answer) {
        var wrapper = document.createElement('article');
        wrapper.className = 'ai-copilot__answer';

        var summary = document.createElement('p');
        summary.className = 'ai-copilot__summary';
        summary.textContent = answer.summary || '';
        wrapper.appendChild(summary);

        // Evidence is grouped into provenance tiers (spec §3.5). Counting distinct sources — not raw
        // claim sentences — is what makes the counts honest: a source cited by three sentences is one
        // piece of evidence, not three.
        var section = renderEvidenceSection(
            Array.isArray(answer.evidence) ? answer.evidence : [],
            Array.isArray(answer.claims) ? answer.claims : []
        );
        if (section) {
            wrapper.appendChild(section);
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
    function renderCitation(source, tier) {
        source = source || {};
        var cite = source.resource_type === 'DocumentReference'
            ? renderDocumentCitation(source)
            : renderCodedCitation(source);
        // Click-to-source (JOS-57): only when the agent stamped a bounding box -- i.e. the fact was
        // derived from an uploaded PDF. Absent a bbox the chip stands exactly as before; a rectangle
        // is never fabricated.
        if (hasBoundingBox(source)) {
            cite.appendChild(buildViewSourceButton(source, tier));
        }
        return cite;
    }

    // ---- provenance tiers (spec §3.5) ------------------------------------
    //
    // A coded record value and a value a vision model read off a scan are not the same kind of fact.
    // Rendering them alike is what lets click-to-source *launder* the weaker one: the physician
    // clicks, sees a box drawn on a scan, and reads that as confirmation. The tier is switched off
    // the wire's `source_type` discriminant (`main.py` projects every claim's source + supporting
    // into `citations[]`) — never re-derived from `resource_type`, which cannot tell a Patient read
    // from FHIR apart from a Patient read off an intake form.
    //
    // Four `source_type` values collapse to three tiers: LAB_PDF and INTAKE_FORM make the same claim
    // about trust and differ only in *which* document — the grouping key one level down, inside the
    // tier.

    var TIER_GUIDELINE = 'guideline';
    var TIER_RECORD = 'record';
    var TIER_DOCUMENT = 'document';

    /**
     * Map a citation's `source_type` onto its trust tier.
     *
     * An unrecognized or missing tag degrades to the document tier — the most conservative one — so
     * a future union arm can never render as a fact of record by default.
     *
     * @param {string|undefined} sourceType The wire `source_type` discriminant.
     * @returns {string} One of TIER_GUIDELINE, TIER_RECORD, TIER_DOCUMENT.
     */
    function tierForSourceType(sourceType) {
        switch (sourceType) {
            case 'guideline':
                return TIER_GUIDELINE;
            case 'fhir':
                return TIER_RECORD;
            case 'lab_pdf':
            case 'intake_form':
                return TIER_DOCUMENT;
            default:
                return TIER_DOCUMENT;
        }
    }

    /**
     * The claim's primary citation. `main.py` projects `[source, *supporting]` in order, so index 0
     * is the primary — and a claim's tier is its primary citation's tier.
     *
     * @param {object} claim A claim from the answer payload.
     * @returns {object|null} The primary citation, or null when the claim carries none.
     */
    function primaryCitation(claim) {
        var citations = (claim && Array.isArray(claim.citations)) ? claim.citations : [];
        return citations.length > 0 ? citations[0] : null;
    }

    function claimTier(claim) {
        var citation = primaryCitation(claim);
        return tierForSourceType(citation ? citation.source_type : undefined);
    }

    function claimsInTier(claims, tier) {
        return claims.filter(function (claim) {
            return claimTier(claim) === tier;
        });
    }

    /**
     * Build the collapsed evidence section, grouped into provenance tiers.
     *
     * The section's own summary carries the composition line ("2 guideline · 4 record · 3 read from
     * scan") because that *is* the safety signal and it must cost zero clicks: the physician learns
     * the answer leaned on three machine-read facts before deciding whether to open anything.
     *
     * Tiers are individually collapsible, but the document tier defaults open — hiding the tier that
     * most needs scrutiny behind an extra click would quietly invert the point of the grouping.
     * Empty tiers are omitted rather than rendered as "(0)": a zero row reads as retrieval having
     * *failed*, when the honest meaning is that nothing qualified.
     *
     * @param {Array<object>} evidence Deduped, relevance-ranked guideline sources.
     * @param {Array<object>} claims The answer's claims, each carrying `citations[]`.
     * @returns {HTMLElement|null} The section, or null when no tier has anything to show.
     */
    function renderEvidenceSection(evidence, claims) {
        var recordClaims = claimsInTier(claims, TIER_RECORD);
        var documentClaims = claimsInTier(claims, TIER_DOCUMENT);

        var tiers = [];
        if (evidence.length > 0) {
            tiers.push({
                key: TIER_GUIDELINE,
                heading: labels.tierGuidelines,
                short: labels.tierGuidelinesShort,
                count: evidence.length,
                open: false,
                build: function () { return renderGuidelineTier(evidence); }
            });
        }
        if (recordClaims.length > 0) {
            tiers.push({
                key: TIER_RECORD,
                heading: labels.tierRecord,
                short: labels.tierRecordShort,
                count: recordClaims.length,
                open: false,
                build: function () { return renderRecordTier(recordClaims); }
            });
        }
        if (documentClaims.length > 0) {
            tiers.push({
                key: TIER_DOCUMENT,
                heading: labels.tierDocuments,
                short: labels.tierDocumentsShort,
                count: documentClaims.length,
                open: true,
                build: function () { return renderDocumentTier(documentClaims); }
            });
        }
        if (tiers.length === 0) {
            return null;
        }

        // Provenance is collapsed by default: the clinician reads the narrative; the evidence trail
        // is one click away for anyone who wants to verify or audit it.
        var details = document.createElement('details');
        details.className = 'ai-copilot__evidence';

        var toggle = document.createElement('summary');
        toggle.className = 'ai-copilot__evidence-toggle';
        toggle.textContent = labels.evidence + ' · ' + tiers.map(function (tier) {
            return tier.count + ' ' + tier.short;
        }).join(' · ');
        details.appendChild(toggle);

        var body = document.createElement('div');
        body.className = 'ai-copilot__evidence-body';
        tiers.forEach(function (tier) {
            body.appendChild(renderTier(tier));
        });
        details.appendChild(body);
        return details;
    }

    /**
     * Wrap one tier's content in its own collapsible block, headed by a name + count that stay
     * visible whether or not the body is open.
     *
     * @param {{key: string, heading: string, count: number, open: boolean, build: function}} tier
     * @returns {HTMLElement} The tier block.
     */
    function renderTier(tier) {
        var block = document.createElement('details');
        block.className = 'ai-copilot__tier ai-copilot__tier--' + tier.key;
        block.open = tier.open;

        var head = document.createElement('summary');
        head.className = 'ai-copilot__tier-head';

        var dot = document.createElement('span');
        dot.className = 'ai-copilot__tier-dot';
        head.appendChild(dot);

        var name = document.createElement('span');
        name.className = 'ai-copilot__tier-name';
        name.textContent = tier.heading;
        head.appendChild(name);

        var count = document.createElement('span');
        count.className = 'ai-copilot__tier-count';
        count.textContent = String(tier.count);
        head.appendChild(count);

        block.appendChild(head);

        var body = document.createElement('div');
        body.className = 'ai-copilot__tier-body';
        body.appendChild(tier.build());
        block.appendChild(body);
        return block;
    }

    // Guidelines: deduped, relevance-ranked source cards, one per distinct source (spec §3.4).
    function renderGuidelineTier(evidence) {
        var sources = document.createElement('ol');
        sources.className = 'ai-copilot__sources';
        evidence.forEach(function (entry, index) {
            sources.appendChild(renderEvidenceCard(entry, index));
        });
        return sources;
    }

    /**
     * Bucket items by a derived key, preserving first-seen order.
     *
     * @param {Array<object>} items The items to bucket.
     * @param {function(object): string} keyOf Derives each item's bucket key.
     * @returns {Array<{key: string, items: Array<object>}>} Buckets in first-seen order.
     */
    function bucketBy(items, keyOf) {
        var order = [];
        var buckets = Object.create(null);
        items.forEach(function (item) {
            var key = keyOf(item);
            if (!buckets[key]) {
                buckets[key] = { key: key, items: [] };
                order.push(buckets[key]);
            }
            buckets[key].items.push(item);
        });
        return order;
    }

    /**
     * The record tier: coded EMR values.
     *
     * Claims sharing a resource type *and* date are one panel from one draw, not N independent
     * findings — so they share a single source chip instead of repeating an identical
     * "Observation · Collected Jul 8, 2026" once per row. The claim prose is kept verbatim because
     * it carries the units, reference range, and interpretation that the wire's `SourceRef` does
     * not (it has `label`, `value`, and `date`, and nothing else).
     *
     * A lone claim, or one missing either grouping key, keeps its own chip — there is nothing to
     * share with.
     *
     * @param {Array<object>} claims Claims whose primary citation is `fhir`.
     * @returns {HTMLElement} The tier's list.
     */
    function renderRecordTier(claims) {
        var list = document.createElement('ol');
        list.className = 'ai-copilot__claims';
        var loners = 0;
        bucketBy(claims, function (claim) {
            var source = claim.source || {};
            // Only a claim with BOTH keys can join a panel; anything else gets a key unique to
            // itself so it renders on its own.
            if (!source.resource_type || !source.date) {
                loners += 1;
                return 'ungrouped:' + loners;
            }
            return source.resource_type + '|' + source.date;
        }).forEach(function (bucket) {
            if (bucket.items.length < 2) {
                list.appendChild(renderClaim(bucket.items[0]));
                return;
            }
            list.appendChild(renderPanel(bucket.items));
        });
        return list;
    }

    /**
     * Render a group of same-type, same-date record claims as one panel: their prose stacked under a
     * single shared source chip.
     *
     * @param {Array<object>} claims Two or more claims sharing a resource type and date.
     * @returns {HTMLElement} An <li> panel.
     */
    function renderPanel(claims) {
        var item = document.createElement('li');
        item.className = 'ai-copilot__claim ai-copilot__panel';

        claims.forEach(function (claim) {
            var text = document.createElement('span');
            text.className = 'ai-copilot__claim-text';
            text.textContent = claim.text;
            item.appendChild(text);
        });

        // One chip for the whole panel: same record type, same draw, so N copies would say the same
        // thing N times.
        item.appendChild(renderCitation(claims[0].source, TIER_RECORD));
        return item;
    }

    /**
     * The document tier: facts a vision model read off an uploaded scan.
     *
     * Grouped by `document_id` and page — three facts read off one intake form page are *one document
     * read three times*, not three documents. Each card heads with the document's kind and page, then
     * lists its facts. Each fact still opens its own single-box preview; consolidating them into one
     * preview that draws every box (spec §3.5) needs the viewer's URL contract to carry more than one
     * rectangle, so it lands separately.
     *
     * @param {Array<object>} claims Claims whose primary citation is `lab_pdf` or `intake_form`.
     * @returns {HTMLElement} The tier's list of document cards.
     */
    function renderDocumentTier(claims) {
        var wrap = document.createElement('div');
        wrap.className = 'ai-copilot__docs';
        bucketBy(claims, function (claim) {
            var source = claim.source || {};
            return String(source.document_id || 'unknown') + '|' + String(normalizePage(source));
        }).forEach(function (bucket) {
            wrap.appendChild(renderDocumentCard(bucket.items));
        });
        return wrap;
    }

    /**
     * Render one document's facts as a single card.
     *
     * @param {Array<object>} claims The facts read off this document page, in answer order.
     * @returns {HTMLElement} The document card.
     */
    function renderDocumentCard(claims) {
        var card = document.createElement('section');
        card.className = 'ai-copilot__doc';

        var source = claims[0].source || {};
        var head = document.createElement('p');
        head.className = 'ai-copilot__doc-head';
        var page = normalizePage(source);
        head.textContent = docKindLabel(source) + ' · ' + labels.page + ' ' + page;
        card.appendChild(head);

        var list = document.createElement('ol');
        list.className = 'ai-copilot__doc-facts';
        claims.forEach(function (claim) {
            var item = document.createElement('li');
            item.className = 'ai-copilot__doc-fact';

            var text = document.createElement('span');
            text.className = 'ai-copilot__claim-text';
            text.textContent = claim.text;
            item.appendChild(text);

            // Per-fact affordance: the overlay draws one box per request today. A fact whose value
            // could not be boxed gets no button rather than a promise the overlay cannot keep.
            if (hasBoundingBox(claim.source)) {
                item.appendChild(buildViewSourceButton(claim.source, TIER_DOCUMENT));
            }
            list.appendChild(item);
        });
        card.appendChild(list);
        return card;
    }

    /**
     * Name the kind of document a fact was read from. The wire carries `doc_type` and `document_id`
     * but no document *title*, so the kind is the most specific name available.
     *
     * @param {object} source The citation's SourceRef.
     * @returns {string} A human-readable document kind.
     */
    function docKindLabel(source) {
        switch (source && source.doc_type) {
            case 'lab_pdf':
                return labels.docLabReport;
            case 'intake_form':
                return labels.docIntakeForm;
            default:
                return labels.docGeneric;
        }
    }

    // Derive a short issuing-body label from a corpus source id: the first hyphen segment,
    // upper-cased ("gina-main-report-2022" -> "GINA", "uspstf-..." -> "USPSTF"). The corpus has no
    // structured organization field yet, so this is a display heuristic over the slug.
    function sourceOrg(sourceId) {
        if (!sourceId) {
            return 'Guideline';
        }
        var head = String(sourceId).split('-')[0];
        return head ? head.toUpperCase() : 'Guideline';
    }

    // Percent-encode one text-fragment token. encodeURIComponent covers ',' and '&'; the fragment
    // grammar also reserves '-' (prefix/suffix marker), which it leaves alone — so encode it too.
    function fragmentToken(text) {
        return encodeURIComponent(text).replace(/-/g, '%2D');
    }

    /**
     * Build a "View source" href that jumps to — and, in Chromium, highlights — the cited passage,
     * using a URL text fragment (`…#:~:text=start,end`). This works in Chrome's PDF viewer (110+)
     * and on HTML sources alike. It is built from the chunk's `anchor_quote` — a span copied
     * verbatim from the source (the curated card `quote` is lightly reworded and would not match) —
     * so it lands on the exact passage instead of the document's first page. A long anchor becomes a
     * start,end range (robust to rendering quirks in the middle); a short one is matched whole. If
     * the browser can't find the text it simply opens the source at the top — never worse.
     *
     * @param {string} url the source URL (already scheme-checked by the caller)
     * @param {?string} anchor the verbatim source span (evidence[].anchor_quote), if any
     * @returns {string} the URL, with a text-fragment anchor when one can be built
     */
    function sourceDeepLink(url, anchor) {
        var words = String(anchor || '').trim().split(/\s+/).filter(Boolean);
        if (words.length < 4) {
            return url; // too little text to anchor on reliably — link the source as-is
        }
        if (words.length <= 12) {
            return url + '#:~:text=' + fragmentToken(words.join(' '));
        }
        var start = fragmentToken(words.slice(0, 6).join(' '));
        var end = fragmentToken(words.slice(-5).join(' '));
        return url + '#:~:text=' + start + ',' + end;
    }

    /**
     * Render one guideline source as an evidence card: a numbered header (issuing body, year), the
     * verbatim retrieved quote, and a section line with an optional link to the source. Built from
     * the response's deduped, relevance-ranked `evidence[]` — one card per distinct source, so the
     * count reflects sources, not claim sentences.
     *
     * `relevance_score` orders the cards server-side but is not shown: the rerank score is
     * uncalibrated across queries, so a High/Medium badge would flicker on noise and imply a
     * confidence it can't honestly convey. The [n] rank is the only relevance cue.
     *
     * @param {{source_id: string, section: string, quote: string, relevance_score: number,
     *   source_url: ?string, year: ?string, anchor_quote: ?string}} entry one evidence[] item
     *   (relevance_score is unused here — it orders the cards server-side)
     * @param {number} index zero-based rank (drives the [n] badge)
     * @returns {HTMLElement} an <li> source card
     */
    function renderEvidenceCard(entry, index) {
        entry = entry || {};
        var card = document.createElement('li');
        card.className = 'ai-copilot__source';

        var header = document.createElement('div');
        header.className = 'ai-copilot__source-header';

        var num = document.createElement('span');
        num.className = 'ai-copilot__source-num';
        num.textContent = String(index + 1);
        header.appendChild(num);

        var org = document.createElement('span');
        org.className = 'ai-copilot__source-org';
        org.textContent = sourceOrg(entry.source_id);
        header.appendChild(org);

        if (entry.year) {
            var year = document.createElement('span');
            year.className = 'ai-copilot__source-year';
            year.textContent = entry.year;
            header.appendChild(year);
        }

        card.appendChild(header);

        if (entry.quote) {
            var quote = document.createElement('blockquote');
            quote.className = 'ai-copilot__source-quote';
            quote.textContent = entry.quote;
            card.appendChild(quote);
        }

        var meta = document.createElement('div');
        meta.className = 'ai-copilot__source-meta';
        if (entry.section) {
            var section = document.createElement('span');
            section.className = 'ai-copilot__source-section';
            section.textContent = entry.section;
            meta.appendChild(section);
        }
        // Corpus-owned https guideline URL; guard the scheme anyway so a malformed value can never
        // become a javascript: link.
        if (entry.source_url && /^https?:\/\//i.test(entry.source_url)) {
            var link = document.createElement('a');
            link.className = 'ai-copilot__source-link';
            link.href = sourceDeepLink(entry.source_url, entry.anchor_quote);
            link.target = '_blank';
            link.rel = 'noopener noreferrer';
            link.textContent = 'View source ↗';   // U+2197 north-east arrow
            meta.appendChild(link);
        }
        if (meta.childNodes.length > 0) {
            card.appendChild(meta);
        }

        return card;
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
        // record it rests on — and spot when two are unrelated (e.g. different dates). Each chip is
        // tiered by its OWN citation, not the claim's: a claim sourced to a coded record may still
        // lean on a scan-read fact, and that chip must carry the weaker provenance honestly.
        var citations = Array.isArray(claim.citations) ? claim.citations : [];
        item.appendChild(renderCitation(claim.source, tierForSourceType(
            citations[0] ? citations[0].source_type : undefined
        )));
        var supporting = claim.supporting || [];
        for (var i = 0; i < supporting.length; i++) {
            var tag = citations[i + 1];
            item.appendChild(renderCitation(
                supporting[i], tierForSourceType(tag ? tag.source_type : undefined)
            ));
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
     * Build the click-to-source button appended to a bbox-bearing citation. The document id, page,
     * and serialized box ride on the button's dataset so the click handler is self-contained.
     *
     * The document tier's wording is deliberately different. "View source" reads as a *receipt* —
     * it implies the checking is already done, which is exactly how a machine-read fact launders
     * itself past a physician who clicks, sees a box on a scan, and takes that as confirmation. For
     * a fact a model read off a scan the affordance must instead read as an open task, so the label
     * flips to the imperative. The laundering lives in the affordance, not the label (spec §3.5).
     *
     * @param {object} source The citation's SourceRef (already passed hasBoundingBox).
     * @param {string} [tier] The citation's provenance tier; document-tier facts get the imperative.
     * @returns {HTMLButtonElement} The keyboard-accessible affordance.
     */
    function buildViewSourceButton(source, tier) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'ai-copilot__view-source';
        if (tier === TIER_DOCUMENT) {
            button.className += ' ai-copilot__view-source--check';
            button.textContent = labels.checkSource;
        } else {
            button.textContent = labels.viewSource;
        }
        button.dataset.documentId = String(source.document_id);
        button.dataset.page = String(normalizePage(source));
        button.dataset.bbox = JSON.stringify(source.bounding_box);
        button.dataset.docTitle = (source.label && source.label !== source.value)
            ? source.label
            : (humanizeToken(source.resource_type) || labels.viewSource);
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
        if (!documentId || isNaN(page)) {
            return;
        }
        openSourceInChartTab(documentId, page, bbox, button.dataset.docTitle || labels.viewSource);
    }

    // Open the cited source as a tab in OpenEMR's main content (chart) area, via the
    // session-authenticated viewer (source-view.php) — it reads the document through the core
    // document ACL, so no patient-scoped SMART Binary scope is needed. The sidebar runs in the top
    // window, so top.navigateTab / top.activateTabByName are callable directly.
    function openSourceInChartTab(documentId, page, bbox, title) {
        var url = config.sourceViewUrl
            + '?doc=' + encodeURIComponent(documentId)
            + '&page=' + encodeURIComponent(page)
            + '&csrf_token=' + encodeURIComponent(config.csrfToken)
            + '&label=' + encodeURIComponent(title);
        if (bbox) {
            url += '&x=' + encodeURIComponent(bbox.x)
                + '&y=' + encodeURIComponent(bbox.y)
                + '&w=' + encodeURIComponent(bbox.width)
                + '&h=' + encodeURIComponent(bbox.height);
        }
        var tabName = 'ai_doc_' + documentId;
        var win = window.top;
        if (win && typeof win.navigateTab === 'function') {
            if (typeof win.restoreSession === 'function') {
                win.restoreSession();
            }
            win.navigateTab(url, tabName, function () {
                if (typeof win.activateTabByName === 'function') {
                    win.activateTabByName(tabName, true);
                }
            });
        } else {
            window.open(url, '_blank');
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
            // Provenance tiering (JOS-88, spec §3.5). `checkSource` is deliberately imperative: it
            // is the document tier's whole safety mechanism, not a synonym for viewSource.
            checkSource: els.sidebar.dataset.labelCheckSource || 'Check against the scan',
            evidence: els.sidebar.dataset.labelEvidence || 'Evidence',
            page: els.sidebar.dataset.labelPage || 'p.',
            tierGuidelines: els.sidebar.dataset.labelTierGuidelines || 'Guidelines',
            tierGuidelinesShort: els.sidebar.dataset.labelTierGuidelinesShort || 'guideline',
            tierRecord: els.sidebar.dataset.labelTierRecord || 'From the record',
            tierRecordShort: els.sidebar.dataset.labelTierRecordShort || 'record',
            tierDocuments: els.sidebar.dataset.labelTierDocuments || 'Read from documents',
            tierDocumentsShort: els.sidebar.dataset.labelTierDocumentsShort || 'read from scan',
            docLabReport: els.sidebar.dataset.labelDocLabReport || 'Lab report',
            docIntakeForm: els.sidebar.dataset.labelDocIntakeForm || 'Intake form',
            docGeneric: els.sidebar.dataset.labelDocGeneric || 'Uploaded document'
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
