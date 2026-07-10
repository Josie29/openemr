<?php

/**
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\AiCopilot\Smart;

use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\FHIR\Config\ServerConfig;
use OpenEMR\FHIR\SMART\SMARTLaunchToken;
use OpenEMR\Modules\AiCopilot\Config\CopilotConfig;
use OpenEMR\Modules\AiCopilot\Exception\LaunchException;
use OpenEMR\Services\PatientService;

/**
 * Starts the SMART EHR-launch chain for the patient whose chart is open.
 *
 * This is the module-code equivalent of core's `SmartLaunchController::redirectAndLaunchSmartApp()`,
 * with one deliberate difference: rather than bouncing through the client's registered
 * `initiate_login_uri`, we go straight to `/authorize`. That skips
 * `ClientEntity::getLaunchUri()`, which concatenates `?launch=…` onto the registered URI with no
 * `?`-vs-`&` handling and would produce a malformed URL for any launch URI carrying a query string.
 *
 * Nothing here touches core. `SMARTLaunchToken` is a published, autoloaded class.
 */
final readonly class LaunchController
{
    public function __construct(
        private CopilotConfig $config,
        private ServerConfig $serverConfig,
        private LaunchStateCodec $stateCodec,
        private PatientService $patientService,
    ) {
    }

    /**
     * Build the authorization URL for the given chart and arm the callback.
     *
     * @param int $pid The internal patient id of the open chart.
     * @param string $redirectUri The module's callback URL, exactly as registered on the client.
     *
     * @return string An absolute URL the hidden iframe should navigate to.
     *
     * @throws LaunchException If the patient has no FHIR UUID.
     * @throws \Random\RandomException If the platform CSPRNG is unavailable.
     * @throws \JsonException If the state payload cannot be encoded.
     */
    public function buildAuthorizeUrl(int $pid, string $redirectUri): string
    {
        $patientUuid = $this->resolvePatientUuid($pid);

        $pkce = PkceChallenge::create();
        // The verifier + expected patient ride back to the callback inside `state`, sealed with
        // authenticated encryption -- not the core session, whose write races the proxy and logs the
        // physician out. See LaunchStateCodec.
        $state = $this->stateCodec->encode($pkce->verifier, $patientUuid);

        $launchToken = new SMARTLaunchToken($patientUuid);
        // Must be one of SMARTLaunchToken::VALID_INTENTS -- an unrecognised intent is silently
        // dropped on deserialize rather than rejected, which would fail confusingly downstream.
        $launchToken->setIntent(SMARTLaunchToken::INTENT_PATIENT_DEMOGRAPHICS_DIALOG);

        // iss and aud are the same value in an EHR launch, and CustomAuthCodeGrant hard-requires aud
        // to equal the FHIR base whenever a launch parameter is present.
        $fhirBaseUrl = $this->serverConfig->getFhirUrl();

        $query = [
            'response_type' => 'code',
            'client_id' => $this->config->clientId,
            'redirect_uri' => $redirectUri,
            'scope' => CopilotScopes::asString(),
            'state' => $state,
            'aud' => $fhirBaseUrl,
            'iss' => $fhirBaseUrl,
            'launch' => $launchToken->serialize(),
            'code_challenge' => $pkce->challenge,
            'code_challenge_method' => PkceChallenge::METHOD,
        ];

        return $this->serverConfig->getAuthorizeUrl() . '?' . http_build_query($query);
    }

    /**
     * @throws LaunchException If the patient row carries no UUID even after backfill.
     */
    private function resolvePatientUuid(int $pid): string
    {
        // Seed and legacy rows can predate the UUID column; core's own launcher backfills here too.
        UuidRegistry::createMissingUuidsForTables(['patient_data']);

        $uuidBytes = $this->patientService->getUuid((string) $pid);
        if ($uuidBytes === false) {
            throw new LaunchException(sprintf('Patient %d has no FHIR UUID', $pid));
        }

        return UuidRegistry::uuidToString($uuidBytes);
    }
}
