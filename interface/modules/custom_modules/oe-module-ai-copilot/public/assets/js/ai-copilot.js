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

    /**
     * Show the in-flight indicator between the question and the answer (spec §5.3.1): an animated
     * typing indicator plus an optional grounded caption. Returned so the caller can swap it for the
     * answer (or an error) when the turn resolves. This node is the seam a future streaming increment
     * fills token-by-token.
     *
     * @returns {HTMLElement} The pending-turn node, already appended to the transcript.
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

        appendNode(pending);
        return pending;
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
        appendNode(wrapper);
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

    function renderClaim(claim) {
        var item = document.createElement('li');
        item.className = 'ai-copilot__claim';

        var text = document.createElement('span');
        text.className = 'ai-copilot__claim-text';
        text.textContent = claim.text;
        item.appendChild(text);

        var source = claim.source || {};
        var cite = document.createElement('cite');
        cite.className = 'ai-copilot__citation';

        // Show a human source label ("Allergy intolerance"), not the raw FHIR UUID, which is
        // clinician-noise. Keep the full `Type/id` reference in attributes for hover + audit.
        var resource = document.createElement('span');
        resource.className = 'ai-copilot__citation-resource';
        resource.textContent = humanizeToken(source.resource_type);
        if (source.resource_type && source.resource_id) {
            var ref = source.resource_type + '/' + source.resource_id;
            cite.title = ref;
            cite.setAttribute('data-resource-ref', ref);
        }
        cite.appendChild(resource);

        if (source.field) {
            var field = document.createElement('span');
            field.className = 'ai-copilot__citation-field';
            // `value` is stamped in by the agent from the fetched record, never written by the model.
            var label = humanizeToken(source.field);
            field.textContent = source.value ? label + ': ' + source.value : label;
            cite.appendChild(field);
        }

        item.appendChild(cite);
        return item;
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
        appendQuestion(message);
        setBusy(true);
        setStatus('', false);
        var pending = appendPending(); // in-flight indicator; swapped for the answer or an error below
        var pidForTurn = activePid;

        sendTurn(message)
            .then(function (response) {
                return response.json().then(function (body) {
                    if (!response.ok) {
                        throw new Error(body.error || labels.unavailable);
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
                appendError(err.message || labels.unavailable);
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

    function wireChips() {
        var chips = els.transcript.querySelectorAll('.ai-copilot__chip');
        Array.prototype.forEach.call(chips, function (chip) {
            chip.addEventListener('click', function () {
                // Populate the input for review -- do NOT auto-send (spec §6).
                els.input.value = chip.getAttribute('data-prompt') || chip.textContent;
                els.input.focus();
            });
        });
    }

    function onSubmit(event) {
        event.preventDefault();
        var message = els.input.value.trim();
        if (message === '') {
            return;
        }
        els.input.value = '';
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
            hasConversation: els.sidebar.dataset.labelHasConversation
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
