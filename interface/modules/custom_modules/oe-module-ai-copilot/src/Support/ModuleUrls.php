<?php

/**
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\AiCopilot\Support;

use OpenEMR\Modules\AiCopilot\Bootstrap;
use OpenEMR\Modules\AiCopilot\Exception\CopilotConfigurationException;

/**
 * The module's browser-facing URLs, derived from OpenEMR's configured public address.
 *
 * Everything hangs off `site_addr_oath` (via `ServerConfig::getOauthAddress()`), which is the same
 * value core uses to build the authorize, token, and FHIR base URLs. Deriving from it rather than
 * from `$_SERVER['HTTP_HOST']` means the registered `redirect_uri` matches what the authorization
 * server expects even when the app is reached through a proxy.
 */
final readonly class ModuleUrls
{
    private const MODULE_PATH = '/interface/modules/custom_modules/' . Bootstrap::MODULE_DIRECTORY;

    private function __construct(
        public string $publicBaseUrl,
        public string $origin,
    ) {
    }

    /**
     * @param string $oauthAddress The value of `ServerConfig::getOauthAddress()`, e.g. `http://localhost:8301`.
     * @param string $webRoot OpenEMR's web root, often the empty string.
     *
     * @throws CopilotConfigurationException If `site_addr_oath` is not an absolute http(s) URL.
     */
    public static function create(string $oauthAddress, string $webRoot): self
    {
        $parts = parse_url($oauthAddress);
        if (!is_array($parts) || !isset($parts['scheme'], $parts['host'])) {
            throw new CopilotConfigurationException(
                'OpenEMR global site_addr_oath must be an absolute http(s) URL'
            );
        }

        // An origin is scheme + host + explicit port only -- no path, no trailing slash. This is the
        // exact string postMessage compares against, so a webRoot suffix here would silently drop
        // every relayed token.
        $origin = $parts['scheme'] . '://' . $parts['host'];
        if (isset($parts['port'])) {
            $origin .= ':' . $parts['port'];
        }

        return new self(rtrim($oauthAddress, '/') . $webRoot, $origin);
    }

    /** The OAuth2 `redirect_uri`. Must byte-match the value registered on the oauth_clients row. */
    public function callbackUrl(): string
    {
        return $this->publicBaseUrl . self::MODULE_PATH . '/public/callback.php';
    }

    /** The hidden iframe's entry point. */
    public function launchUrl(): string
    {
        return $this->publicBaseUrl . self::MODULE_PATH . '/public/launch.php';
    }
}
