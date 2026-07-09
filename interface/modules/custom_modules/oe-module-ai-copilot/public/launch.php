<?php

/**
 * Entry point of the SMART EHR-launch chain, loaded inside the panel's hidden iframe.
 *
 * Mints a launch token carrying the open chart's patient context, arms the PKCE/state pair in the
 * physician's session, and redirects the frame to OpenEMR's authorize endpoint. The chart page in
 * the parent frame never navigates.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// Must precede globals.php: the session is opened read-only unless this is set, and the launch leg
// has to persist the PKCE verifier for the callback leg to find.
$sessionAllowWrite = true;

require_once __DIR__ . '/../../../../globals.php';

use OpenEMR\BC\ServiceContainer;
use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\FHIR\Config\ServerConfig;
use OpenEMR\Modules\AiCopilot\Config\CopilotConfig;
use OpenEMR\Modules\AiCopilot\Smart\LaunchController;
use OpenEMR\Modules\AiCopilot\Smart\LaunchSession;
use OpenEMR\Modules\AiCopilot\Smart\TokenRelayView;
use OpenEMR\Modules\AiCopilot\Support\ModuleUrls;
use OpenEMR\Services\PatientService;

$globalsBag = OEGlobalsBag::getInstance();
$session = SessionWrapperFactory::getInstance()->getActiveSession();
$logger = ServiceContainer::getLogger();

// Built before the try block so the error path can still relay a message to the parent frame.
$serverConfig = new ServerConfig();
$relay = new TokenRelayView(
    ModuleUrls::create($serverConfig->getOauthAddress(), $globalsBag->getWebRoot())->origin
);

header('Content-Type: text/html; charset=utf-8');

try {
    // globals.php has already enforced authentication; this proves the request originated from the
    // chart page we rendered, not from a third-party page embedding our launch URL.
    $csrfToken = filter_input(INPUT_GET, 'csrf_token');
    if (!is_string($csrfToken) || !CsrfUtils::verifyCsrfToken($csrfToken, session: $session)) {
        throw new \RuntimeException('CSRF verification failed for the co-pilot launch');
    }

    // The chart's pid comes from the server session, never from the query string -- a pid parameter
    // would be an IDOR vector straight past the very control this module exists to enforce.
    $pid = $session->get('pid');
    if (!is_numeric($pid) || (int) $pid <= 0) {
        throw new \RuntimeException('No patient chart is open in this session');
    }

    $config = CopilotConfig::fromEnvironment();
    $urls = ModuleUrls::create($serverConfig->getOauthAddress(), $globalsBag->getWebRoot());

    $launchController = new LaunchController(
        $config,
        $serverConfig,
        new LaunchSession($session),
        new PatientService()
    );

    $authorizeUrl = $launchController->buildAuthorizeUrl((int) $pid, $urls->callbackUrl());

    header('Location: ' . $authorizeUrl, response_code: 302);
    exit;
} catch (\Exception $exception) {
    // The frame is hidden, so an error page nobody sees is useless -- relay the failure to the panel
    // instead. The reason is a fixed string; the detail stays in the log. A raw \Error (a coding
    // bug) is left to surface as a 500 in the Apache log rather than silently swallowed here.
    $logger->error('AiCopilot: SMART launch could not be started', ['exception' => $exception]);
    echo $relay->renderError('launch_failed');
}
