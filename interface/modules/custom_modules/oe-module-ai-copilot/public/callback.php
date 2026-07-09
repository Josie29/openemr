<?php

/**
 * Terminal leg of the SMART EHR-launch chain, still inside the panel's hidden iframe.
 *
 * Receives the authorization code, exchanges it server-side for a patient-scoped access token, and
 * relays that token up to the chart page via postMessage. Every failure is fail-closed: the panel
 * is told the launch failed and no token is produced.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// The callback consumes (and therefore clears) the PKCE verifier it finds in the session.
$sessionAllowWrite = true;

require_once __DIR__ . '/../../../../globals.php';

use GuzzleHttp\Client;
use OpenEMR\BC\ServiceContainer;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\FHIR\Config\ServerConfig;
use OpenEMR\Modules\AiCopilot\Config\CopilotConfig;
use OpenEMR\Modules\AiCopilot\Exception\LaunchException;
use OpenEMR\Modules\AiCopilot\Smart\LaunchSession;
use OpenEMR\Modules\AiCopilot\Smart\TokenExchanger;
use OpenEMR\Modules\AiCopilot\Smart\TokenRelayView;
use OpenEMR\Modules\AiCopilot\Support\ModuleUrls;

$globalsBag = OEGlobalsBag::getInstance();
$session = SessionWrapperFactory::getInstance()->getActiveSession();
$logger = ServiceContainer::getLogger();

$serverConfig = new ServerConfig();
$urls = ModuleUrls::create($serverConfig->getOauthAddress(), $globalsBag->getWebRoot());
$relay = new TokenRelayView($urls->origin);
$launchSession = new LaunchSession($session);

header('Content-Type: text/html; charset=utf-8');
// The relayed document embeds a bearer token. Keep it out of every cache between here and the frame.
header('Cache-Control: no-store, private');

try {
    // An authorization-server error (access_denied, invalid_scope, ...) arrives as a query param,
    // not as a non-2xx. Discard the in-flight launch before doing anything else.
    $error = filter_input(INPUT_GET, 'error');
    if (is_string($error)) {
        $launchSession->clear();
        throw new LaunchException(sprintf('Authorization server refused the launch: %s', $error));
    }

    $code = filter_input(INPUT_GET, 'code');
    $state = filter_input(INPUT_GET, 'state');
    if (!is_string($code) || $code === '' || !is_string($state) || $state === '') {
        throw new LaunchException('Callback is missing code or state');
    }

    // Verifies state and clears the session keys, so a replayed callback finds nothing.
    ['codeVerifier' => $codeVerifier, 'patientUuid' => $expectedPatientUuid] = $launchSession->consume($state);

    $config = CopilotConfig::fromEnvironment();
    $exchanger = new TokenExchanger($config, new Client());

    $token = $exchanger->exchange(
        $code,
        $codeVerifier,
        $urls->callbackUrl(),
        $serverConfig->getSiteId(),
        $globalsBag->getWebRoot()
    );

    // The decisive invariant: this token must be bound to the chart that launched it.
    $token->assertBoundTo($expectedPatientUuid);

    $logger->debug('AiCopilot: patient-scoped token issued', [
        'patient' => $token->patientUuid,
        'expires_in' => $token->expiresIn,
        'scope' => $token->scope,
    ]);

    echo $relay->renderToken($token);
} catch (\Exception $exception) {
    // Never surface $exception->getMessage() -- it can carry the token URL and transport detail. A
    // raw \Error (a coding bug) is left to surface as a 500 in the Apache log, not swallowed here.
    $logger->error('AiCopilot: SMART token exchange failed', ['exception' => $exception]);
    echo $relay->renderError('token_exchange_failed');
}
