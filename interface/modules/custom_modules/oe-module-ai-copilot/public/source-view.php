<?php

/**
 * Click-to-source document viewer (JOS-57) — opens in the OpenEMR chart pane.
 *
 * A session-authenticated page that renders a stored source document with a
 * bounding-box highlight over the cited value. Because it runs in the OpenEMR
 * session (not the patient-scoped SMART token), it reads the document via the
 * core document ACL — sidestepping the FHIR Binary scope the browser token lacks.
 *
 * Two modes:
 *   ?doc=<uuid>&csrf_token=<t>&format=pdf   -> streams the raw PDF bytes
 *   ?doc=<uuid>&csrf_token=<t>&page=&x=&y=&w=&h=&label=  -> renders the pdf.js viewer
 *
 * @package   OpenEMR\Modules\AiCopilot
 * @link      https://www.open-emr.org
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// Pin the site behind the TLS proxy before globals.php (which 400s on an empty site id),
// then bootstrap OpenEMR — this enforces authentication. Read-only session (no
// $sessionAllowWrite: writing it races the proxy session rotation, per launch.php).
$_GET['site'] ??= 'default';
require_once __DIR__ . '/../../../../globals.php';

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;

$session = SessionWrapperFactory::getInstance()->getActiveSession();

/** Fail closed with a plain status — never leak document bytes or internals. */
function deny(int $code, string $message): never
{
    http_response_code($code);
    header('Content-Type: text/plain; charset=utf-8');
    echo $message;
    exit;
}

$csrfToken = filter_input(INPUT_GET, 'csrf_token');
if (!is_string($csrfToken) || !CsrfUtils::verifyCsrfToken($csrfToken, session: $session)) {
    deny(403, 'CSRF verification failed.');
}

// Core gate for viewing patient documents.
if (!AclMain::aclCheckCore('patients', 'docs')) {
    deny(403, 'Not authorized to view patient documents.');
}

// The patient comes from the session, never the URL (a URL pid would be an IDOR vector).
$pid = $session->get('pid');
if (!is_numeric($pid) || (int) $pid <= 0) {
    deny(400, 'No patient chart is open in this session.');
}
$pid = (int) $pid;

$uuid = filter_input(INPUT_GET, 'doc');
if (!is_string($uuid) || !preg_match('/^[0-9a-fA-F-]{36}$/', $uuid)) {
    deny(400, 'Missing or malformed document id.');
}

$document = Document::getDocumentForUuid($uuid);
if (
    $document === null
    || $document->is_deleted()
    || !$document->can_access()
    || (int) $document->get_foreign_id() !== $pid
) {
    // Same response for not-found and not-authorized so we don't confirm existence.
    deny(404, 'Document not found.');
}

// --- Mode 1: stream the raw bytes -----------------------------------------------------------------
if (filter_input(INPUT_GET, 'format') === 'pdf') {
    header('Content-Type: ' . $document->get_mimetype());
    header('Cache-Control: no-store, private');
    header('X-Content-Type-Options: nosniff');
    echo $document->get_data();
    exit;
}

// --- Mode 2: render the annotated viewer ----------------------------------------------------------
$page = max(1, (int) (filter_input(INPUT_GET, 'page') ?? 1));
$bbox = [
    'x' => (float) (filter_input(INPUT_GET, 'x') ?? 0),
    'y' => (float) (filter_input(INPUT_GET, 'y') ?? 0),
    'w' => (float) (filter_input(INPUT_GET, 'w') ?? 0),
    'h' => (float) (filter_input(INPUT_GET, 'h') ?? 0),
];
$hasBox = $bbox['w'] > 0 && $bbox['h'] > 0;
$label = filter_input(INPUT_GET, 'label');
$label = is_string($label) && $label !== '' ? $label : 'Source document';
// The chart-tab title (the tabs framework reads .title / <b> / <title>). A "Source:" prefix reads
// instantly against the function-named tabs (Dashboard, Visit History) as "a document, not a screen".
$tabTitle = $label === 'Source document' ? $label : 'Source: ' . $label;

// Same-origin relative URLs (this page lives in .../public/). The bytes URL re-verifies CSRF.
$bytesUrl = 'source-view.php?doc=' . attr_url($uuid) . '&csrf_token=' . attr_url($csrfToken) . '&format=pdf';
$pdfJs = 'assets/vendor/pdfjs/pdf.min.js';
$pdfWorker = 'assets/vendor/pdfjs/pdf.worker.min.js';

// Data the viewer JS needs, JSON-encoded so it is inert (no markup/code injection).
$viewData = json_encode(
    ['bytesUrl' => $bytesUrl, 'workerSrc' => $pdfWorker, 'page' => $page, 'bbox' => $bbox, 'hasBox' => $hasBox],
    JSON_THROW_ON_ERROR | JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT
);
?>
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <!-- The tabs framework titles the tab from .title / <b> / <title>, in that order. -->
    <title><?php echo text($tabTitle); ?></title>
    <script src="<?php echo attr($pdfJs); ?>"></script>
    <style>
        :root { --line: #d3d9df; --muted: #5b6770; --accent: #1e4ed8; --ink: #1f2a33; --surface: #f6f8fa; }
        * { box-sizing: border-box; }
        body { margin: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; color: var(--ink); background: var(--surface); }
        .doc-head { display: flex; align-items: baseline; gap: 0.75rem; padding: 0.75rem 1rem; border-bottom: 1px solid var(--line); background: #fff; }
        .doc-head .title { font-size: 1.05rem; font-weight: 650; }
        .doc-head .sub { font-size: 0.8rem; color: var(--muted); }
        .doc-stage { padding: 1.25rem; display: flex; justify-content: center; }
        .doc-wrap { position: relative; box-shadow: 0 2px 10px rgba(31, 42, 51, 0.12); background: #fff; line-height: 0; }
        canvas { display: block; max-width: 100%; }
        .doc-bbox { position: absolute; border: 2px solid var(--accent); background: rgba(30, 78, 216, 0.16); box-shadow: 0 0 0 1px rgba(255,255,255,0.6); pointer-events: none; }
        .doc-status { padding: 2rem 1rem; text-align: center; color: var(--muted); }
        .doc-status.err { color: #b02a2a; }
    </style>
</head>
<body>
    <div class="doc-head">
        <span class="title"><?php echo text($tabTitle); ?></span>
        <span class="sub">page <?php echo (int) $page; ?></span>
    </div>
    <div class="doc-stage">
        <div class="doc-wrap" id="wrap">
            <div class="doc-status" id="status">Loading source document&hellip;</div>
        </div>
    </div>
    <script>
        (function () {
            var data = <?php echo $viewData; ?>;
            var wrap = document.getElementById('wrap');
            var statusEl = document.getElementById('status');
            function fail(msg) { statusEl.textContent = msg; statusEl.className = 'doc-status err'; }
            if (!window.pdfjsLib) { fail('Could not load the PDF viewer.'); return; }
            pdfjsLib.GlobalWorkerOptions.workerSrc = data.workerSrc;

            fetch(data.bytesUrl, { credentials: 'same-origin' })
                .then(function (r) { if (!r.ok) { throw new Error('fetch ' + r.status); } return r.arrayBuffer(); })
                .then(function (buf) { return pdfjsLib.getDocument({ data: buf }).promise; })
                .then(function (pdf) { return pdf.getPage(Math.min(data.page, pdf.numPages)); })
                .then(function (page) {
                    // Scale to fit the available width; bbox values are PDF points (scale-1 space).
                    var maxW = Math.min(wrap.parentElement.clientWidth - 8, 1100);
                    var scale = maxW / page.getViewport({ scale: 1 }).width;
                    var vp = page.getViewport({ scale: scale });
                    var canvas = document.createElement('canvas');
                    var ratio = window.devicePixelRatio || 1;
                    canvas.width = Math.floor(vp.width * ratio);
                    canvas.height = Math.floor(vp.height * ratio);
                    canvas.style.width = vp.width + 'px';
                    canvas.style.height = vp.height + 'px';
                    wrap.innerHTML = '';
                    wrap.appendChild(canvas);
                    var ctx = canvas.getContext('2d');
                    ctx.scale(ratio, ratio);
                    return page.render({ canvasContext: ctx, viewport: vp }).promise.then(function () {
                        if (!data.hasBox) { return; }
                        var box = document.createElement('div');
                        box.className = 'doc-bbox';
                        box.style.left = (data.bbox.x * scale) + 'px';
                        box.style.top = (data.bbox.y * scale) + 'px';
                        box.style.width = (data.bbox.w * scale) + 'px';
                        box.style.height = (data.bbox.h * scale) + 'px';
                        wrap.appendChild(box);
                        box.scrollIntoView({ block: 'center' });
                    });
                })
                .catch(function () { fail('Could not load the source document.'); });
        })();
    </script>
</body>
</html>
