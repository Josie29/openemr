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

/**
 * A PKCE verifier/challenge pair (RFC 7636).
 *
 * OpenEMR's authorization server advertises S256 as the only supported challenge method, so plain
 * is not modelled here at all.
 */
final readonly class PkceChallenge
{
    /** RFC 7636 §4.1 permits 43-128 characters; 32 random bytes base64url-encodes to 43. */
    private const VERIFIER_BYTES = 32;

    public const METHOD = 'S256';

    private function __construct(
        public string $verifier,
        public string $challenge,
    ) {
    }

    /**
     * Generate a fresh, cryptographically random verifier and its S256 challenge.
     *
     * @throws \Random\RandomException If the platform CSPRNG is unavailable.
     */
    public static function create(): self
    {
        $verifier = self::base64UrlEncode(random_bytes(self::VERIFIER_BYTES));
        // S256: BASE64URL(SHA256(ASCII(verifier))) -- hash over the *encoded* verifier, raw binary out.
        $challenge = self::base64UrlEncode(hash('sha256', $verifier, binary: true));

        return new self($verifier, $challenge);
    }

    /** Base64url per RFC 7636 §A: standard base64, `+/` swapped for `-_`, padding stripped. */
    private static function base64UrlEncode(string $bytes): string
    {
        return rtrim(strtr(base64_encode($bytes), '+/', '-_'), '=');
    }
}
