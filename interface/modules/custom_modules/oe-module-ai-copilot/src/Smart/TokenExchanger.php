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

use GuzzleHttp\Client;
use GuzzleHttp\Exception\GuzzleException;
use JsonException;
use OpenEMR\Modules\AiCopilot\Config\CopilotConfig;
use OpenEMR\Modules\AiCopilot\Exception\LaunchException;

/**
 * Exchanges an authorization code for a patient-scoped access token, server-side.
 *
 * The client secret and the PKCE verifier both stay in PHP; only the resulting access token ever
 * reaches the browser. The request goes to the *container-internal* token URL, because
 * `ServerConfig::getTokenUrl()` returns the browser-facing address (e.g. `http://localhost:8301`),
 * which resolves to nothing from inside the OpenEMR container.
 */
final readonly class TokenExchanger
{
    private const TIMEOUT_SECONDS = 10.0;

    public function __construct(
        private CopilotConfig $config,
        private Client $httpClient,
    ) {
    }

    /**
     * @param string $code The authorization code from the callback.
     * @param string $codeVerifier The PKCE verifier held in the session since the launch leg.
     * @param string $redirectUri The same redirect_uri sent on the authorize request.
     * @param string $siteId OpenEMR site id.
     * @param string $webRoot OpenEMR web root, possibly empty.
     *
     * @return PatientScopedToken The access token and the patient it is bound to.
     *
     * @throws LaunchException On any non-2xx response, transport failure, malformed JSON, or a body
     *     missing the fields a patient-scoped token must carry.
     */
    public function exchange(
        string $code,
        string $codeVerifier,
        string $redirectUri,
        string $siteId,
        string $webRoot,
    ): PatientScopedToken {
        $tokenUrl = $this->config->internalTokenUrl($siteId, $webRoot);

        try {
            $response = $this->httpClient->post($tokenUrl, [
                'form_params' => [
                    'grant_type' => 'authorization_code',
                    'code' => $code,
                    'redirect_uri' => $redirectUri,
                    'client_id' => $this->config->clientId,
                    // This client is registered with token_endpoint_auth_method=client_secret_post.
                    'client_secret' => $this->config->clientSecret,
                    'code_verifier' => $codeVerifier,
                ],
                'headers' => ['Accept' => 'application/json'],
                'timeout' => self::TIMEOUT_SECONDS,
                // Fail closed on any non-2xx rather than letting Guzzle's default throw semantics
                // diverge from the FHIR client's. OpenEMR surfaces authorization denials as a bare
                // HTTP 500 with no machine-readable body (see smart-token-spike-findings.md), so
                // there is nothing to gain from special-casing 401/403/404 -- treat all alike.
                'http_errors' => false,
            ]);
        } catch (GuzzleException $exception) {
            throw new LaunchException('Token exchange transport failure', previous: $exception);
        }

        $status = $response->getStatusCode();
        if ($status < 200 || $status >= 300) {
            throw new LaunchException(sprintf('Token endpoint returned HTTP %d', $status));
        }

        try {
            $body = json_decode((string) $response->getBody(), associative: true, flags: JSON_THROW_ON_ERROR);
        } catch (JsonException $exception) {
            throw new LaunchException('Token endpoint returned malformed JSON', previous: $exception);
        }

        if (!is_array($body)) {
            throw new LaunchException('Token endpoint returned a non-object body');
        }

        return PatientScopedToken::fromTokenResponse($body);
    }
}
