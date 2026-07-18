<?php

/**
 * Write-back of agent-derived facts (JOS-81) — session-authenticated persist endpoint.
 *
 * The agent's FHIR client is read-only, and no write surface it could reach exists: `patient/*.write`
 * is never constructed, `system/*.write` likewise (despite a working client_credentials grant), there
 * is no FHIR write route for Observation/AllergyIntolerance/MedicationRequest, and `/api` is
 * users-role-only. See `context/specs/derived-fact-write-back.md` for the evidence.
 *
 * So write-back runs here instead, in the OpenEMR session — the same trick `source-view.php` uses to
 * read documents. The sidebar posts the facts the agent returned; the write is authorized by the
 * logged-in clinician's own ACL, not by a service credential. The agent never holds write authority.
 *
 * POST JSON — each fact carries a `type` discriminator:
 *   { "csrf_token": "...", "document": "<uuid>", "accept": false, "facts": [
 *       {type:"lab", loinc, label, value, units, range, abnormal, page, bbox:{x,y,w,h}, confidence},
 *       {type:"allergy", substance, reaction, page, bbox?, confidence},
 *       {type:"medication", name, dose, frequency, page, bbox?, confidence},
 *       {type:"family_history", condition, relation, page, bbox?, confidence},
 *       {type:"chief_concern", text, page, bbox?, confidence},
 *       {type:"demographic", field, value, page, bbox?, confidence}
 *   ] }
 *
 * Every kind now has a native destination. Most write on arrival; demographics is the exception —
 * it overwrites clinician-entered identity data with no marker, so it is gated: posted without
 * `accept:true` it returns a chart-vs-document `preview` (per field) and writes nothing, and the
 * sidebar re-posts the accepted fields with `accept:true`. See
 * `context/specs/intake-write-back-completion.md`.
 *
 * @package   OpenEMR\Modules\AiCopilot
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// Pin the site behind the TLS proxy before globals.php (which 400s on an empty site id), then
// bootstrap OpenEMR — this enforces authentication. Read-only session (no $sessionAllowWrite:
// writing it races the proxy session rotation, per launch.php). DB writes are unaffected.
$_GET['site'] ??= 'default';
require_once __DIR__ . '/../../../../globals.php';

use OpenEMR\Common\Acl\AclMain;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\ModulesClassLoader;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Modules\AiCopilot\Fact\DerivedFactPersister;
use OpenEMR\Modules\AiCopilot\Fact\ExtractionSidecar;
use OpenEMR\Modules\AiCopilot\Fact\FactPayloadParser;

$classLoader = new ModulesClassLoader(OEGlobalsBag::getInstance()->getProjectDir());
$classLoader->registerNamespaceIfNotExists(
    'OpenEMR\\Modules\\AiCopilot\\',
    dirname(__DIR__) . DIRECTORY_SEPARATOR . 'src'
);

$session = SessionWrapperFactory::getInstance()->getActiveSession();

/** Fail closed with a JSON body — never leak internals to the caller. */
function respond(int $code, array $body): never
{
    http_response_code($code);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store, private');
    header('X-Content-Type-Options: nosniff');
    echo json_encode($body, JSON_THROW_ON_ERROR);
    exit;
}

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    respond(405, ['error' => 'This endpoint accepts POST only.']);
}

$raw = file_get_contents('php://input');
try {
    $payload = json_decode((string) $raw, true, 32, JSON_THROW_ON_ERROR);
} catch (JsonException) {
    respond(400, ['error' => 'Request body is not valid JSON.']);
}
if (!is_array($payload)) {
    respond(400, ['error' => 'Request body must be a JSON object.']);
}

$csrfToken = $payload['csrf_token'] ?? null;
if (!is_string($csrfToken) || !CsrfUtils::verifyCsrfToken($csrfToken, session: $session)) {
    respond(403, ['error' => 'CSRF verification failed.']);
}

// Two gates: you must be allowed to read the source document (as source-view.php requires) and to
// write medical records. Deliberately NOT 'patients'/'sign' — that is the authority to sign results
// off, which core's orders_results.php requires because it makes them official. Everything written
// here is `preliminary` precisely because nothing has signed it.
if (!AclMain::aclCheckCore('patients', 'docs') || !AclMain::aclCheckCore('patients', 'med')) {
    respond(403, ['error' => 'Not authorized to persist facts to this chart.']);
}

// The patient comes from the session, never the payload (a caller-supplied pid would be an IDOR
// vector — the same reason source-view.php reads it from the session).
$pid = $session->get('pid');
if (!is_numeric($pid) || (int) $pid <= 0) {
    respond(400, ['error' => 'No patient chart is open in this session.']);
}
$pid = (int) $pid;

$uuid = $payload['document'] ?? null;
if (!is_string($uuid) || !preg_match('/^[0-9a-fA-F-]{36}$/', $uuid)) {
    respond(400, ['error' => 'Missing or malformed document id.']);
}

$document = Document::getDocumentForUuid($uuid);
if (
    $document === null
    || $document->is_deleted()
    || !$document->can_access()
    || (int) $document->get_foreign_id() !== $pid
) {
    // Same response for not-found and not-authorized so we do not confirm existence.
    respond(404, ['error' => 'Document not found.']);
}

$facts = $payload['facts'] ?? null;
if (!is_array($facts) || $facts === []) {
    respond(400, ['error' => 'No facts supplied.']);
}

try {
    $parsed = (new FactPayloadParser())->parse($facts);
} catch (\DomainException $e) {
    // Safe to echo: these messages are authored by our own parser and describe the caller's own
    // payload. Anything from deeper in the stack is not — see the Throwable arm below.
    respond(422, ['error' => $e->getMessage()]);
}

// The content hash is computed here, from the bytes OpenEMR actually stored, rather than accepted
// from the caller. It is the extraction version key (W2_ARCHITECTURE 3.4), so a caller-supplied
// value could be used to force a re-write of facts that are already persisted.
$bytes = $document->get_data();
if (!is_string($bytes) || $bytes === '') {
    respond(409, ['error' => 'The source document has no retrievable content.']);
}
$contentHash = hash('sha256', $bytes);

$documentId = (int) $document->get_id();
$username = (string) ($session->get('authUser') ?? '');

// Gated (overwrite) families write only when the clinician has accepted their review card.
$accept = ($payload['accept'] ?? false) === true;

// Session context for families that create native records under the clinician's identity (an
// encounter's author/provider/facility). From the session, never the payload.
$sessionInt = static function (string $key) use ($session): ?int {
    $value = $session->get($key);
    return is_numeric($value) ? (int) $value : null;
};

// Each fact family writes in its own transaction and is caught on its own, so a failure in one
// does not discard another that already committed. The outcome reports exactly what landed.
$outcome = (new DerivedFactPersister(new ExtractionSidecar()))->persist(
    $pid,
    $documentId,
    $contentHash,
    $parsed,
    $username,
    $accept,
    $sessionInt('authUserID'),
    $sessionInt('authProvider'),
    $sessionInt('facilityId'),
);

$response = [
    'written' => $outcome->written,
    'skipped' => $outcome->skipped,
    'content_hash' => $contentHash,
];
if ($outcome->procedureOrderId !== null) {
    $response['order_id'] = $outcome->procedureOrderId;
}
if ($outcome->hasPreview()) {
    // The chart-vs-document diff for gated fields the clinician has not yet accepted.
    $response['preview'] = $outcome->preview;
}
if ($outcome->hasFailures()) {
    $response['failed'] = $outcome->failed;
}

if (!$outcome->hasFailures()) {
    // Everything requested reached the chart (or is a preview awaiting accept).
    respond(200, $response);
}
if ($outcome->anythingLanded()) {
    // Partial success: some facts persisted, some family failed. Report what landed rather than a
    // blanket 500 that would hide the facts that did — the client's confirmation count stays honest.
    respond(207, $response);
}
// Nothing landed and something failed — a genuine server error.
$response['error'] = 'Could not persist the derived facts.';
respond(500, $response);
