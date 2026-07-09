<?php

/**
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\AiCopilot\Config;

use OpenEMR\Modules\AiCopilot\Exception\CopilotConfigurationException;

/**
 * The module's runtime configuration, parsed once from the environment at the request edge.
 *
 * Secrets live in the environment (docker compose / Railway service variables), never in the
 * database and never in the repository — this is the AUDIT.md secrets-hygiene finding applied
 * to the module. Reading the environment is confined to {@see self::fromEnvironment()}; the
 * rest of the module depends on this typed object, per CLAUDE.md's parse-don't-validate rule.
 */
final readonly class CopilotConfig
{
    /**
     * @param string $clientId OAuth2 client id registered with OpenEMR for this module.
     * @param string $clientSecret OAuth2 client secret. Server-side only — never rendered to the browser.
     * @param string $agentBaseUrl Browser-reachable base URL of the Python agent service, no trailing slash.
     * @param string $oauthInternalBaseUrl Base URL the *container* uses to reach OpenEMR's own token
     *     endpoint. This is deliberately not {@see \OpenEMR\FHIR\Config\ServerConfig::getTokenUrl()},
     *     which returns the public address (e.g. http://localhost:8301) that resolves to nothing from
     *     inside the container. No trailing slash.
     */
    private function __construct(
        public string $clientId,
        public string $clientSecret,
        public string $agentBaseUrl,
        public string $oauthInternalBaseUrl,
    ) {
    }

    /**
     * Parse the module's configuration from environment variables.
     *
     * @return self The validated configuration.
     *
     * @throws CopilotConfigurationException If a required variable is missing or a URL is malformed.
     */
    public static function fromEnvironment(): self
    {
        return new self(
            clientId: self::requireEnv('AI_COPILOT_CLIENT_ID'),
            clientSecret: self::requireEnv('AI_COPILOT_CLIENT_SECRET'),
            agentBaseUrl: self::requireUrl('AI_COPILOT_AGENT_URL'),
            oauthInternalBaseUrl: rtrim(self::readEnv('AI_COPILOT_OAUTH_INTERNAL_BASE') ?? 'http://localhost', '/'),
        );
    }

    /**
     * Whether the module is configured well enough to attempt a launch.
     *
     * The render listener uses this to stay silent on the chart rather than mounting a panel that
     * is guaranteed to fail on first interaction.
     */
    public static function isConfigured(): bool
    {
        foreach (['AI_COPILOT_CLIENT_ID', 'AI_COPILOT_CLIENT_SECRET', 'AI_COPILOT_AGENT_URL'] as $key) {
            if (self::readEnv($key) === null) {
                return false;
            }
        }
        return true;
    }

    /** The module's `POST /chat` endpoint on the agent service. */
    public function chatUrl(): string
    {
        return $this->agentBaseUrl . '/chat';
    }

    /**
     * OpenEMR's own token endpoint, addressed from inside the container.
     *
     * @param string $siteId The OpenEMR site id (usually `default`).
     * @param string $webRoot The application web root, possibly empty.
     */
    public function internalTokenUrl(string $siteId, string $webRoot): string
    {
        return $this->oauthInternalBaseUrl . $webRoot . '/oauth2/' . $siteId . '/token';
    }

    /**
     * Read one environment variable, tolerating the several places PHP SAPIs expose them.
     *
     * mod_php populates `getenv()` (verified in the dev container); some php-fpm configurations
     * only populate the process environment, which `filter_input(INPUT_ENV, ...)` reads. Checking
     * both keeps the module working across the dev container and Railway without a SAPI-specific
     * Apache `PassEnv` directive. Direct `$_SERVER`/`$_ENV` access is avoided per the project's
     * ForbidRequestGlobals PHPStan rule.
     *
     * @param string $key The variable name.
     *
     * @return string|null The trimmed value, or null when unset or empty.
     */
    private static function readEnv(string $key): ?string
    {
        $raw = getenv($key);
        if (!is_string($raw) || trim($raw) === '') {
            $raw = filter_input(INPUT_ENV, $key);
        }
        if (!is_string($raw)) {
            return null;
        }
        $value = trim($raw);
        return $value === '' ? null : $value;
    }

    /**
     * @throws CopilotConfigurationException If the variable is unset or empty.
     */
    private static function requireEnv(string $key): string
    {
        $value = self::readEnv($key);
        if ($value === null) {
            throw new CopilotConfigurationException(sprintf('Missing required environment variable %s', $key));
        }
        return $value;
    }

    /**
     * @throws CopilotConfigurationException If the variable is unset, empty, or not an absolute http(s) URL.
     */
    private static function requireUrl(string $key): string
    {
        $value = self::requireEnv($key);
        $scheme = parse_url($value, PHP_URL_SCHEME);
        if (!in_array($scheme, ['http', 'https'], strict: true) || parse_url($value, PHP_URL_HOST) === null) {
            throw new CopilotConfigurationException(sprintf('%s must be an absolute http(s) URL', $key));
        }
        return rtrim($value, '/');
    }
}
