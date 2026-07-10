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

use OpenEMR\Common\Crypto\CryptoInterface;
use OpenEMR\Modules\AiCopilot\Exception\LaunchException;
use Psr\Clock\ClockInterface;

/**
 * Carries the launch->callback bridge (PKCE verifier + expected patient) inside the OAuth `state`
 * parameter as an encrypted, authenticated blob, instead of the physician's core session.
 *
 * Why not the session: the launch runs in a hidden iframe that would otherwise open a *write*
 * session to stash the verifier. Behind a TLS-terminating proxy that write races OpenEMR's JS
 * `restoreSession()` cookie rewrite (the mechanism that supports separate concurrent patient
 * logins), desyncs the core session id, and the parent frame's next poll is bounced to the login
 * screen. Round-tripping the bridge through `state` needs no session write on either leg, so the
 * race cannot occur.
 *
 * Confidentiality is required because `state` travels in the authorize and callback URLs -- and thus
 * in access logs and browser history. The verifier is the PKCE secret that makes an intercepted
 * authorization code useless, so it must not be readable there. {@see CryptoInterface::encryptStandard}
 * is authenticated encryption (AES + HMAC): it gives confidentiality and integrity in one primitive.
 * The HMAC is what proves this server minted the blob, replacing the random-`state` comparison the
 * session flow relied on for CSRF protection. A short TTL bounds replay; the single-use
 * authorization code closes the rest.
 */
final readonly class LaunchStateCodec
{
    /**
     * How long an encrypted state stays valid. The launch->callback round trip is a single redirect
     * through /authorize -- a few seconds -- so this only has to outlast proxy and network latency.
     * The shorter it is, the tighter the replay window on a captured blob.
     */
    private const TTL_SECONDS = 300;

    public function __construct(
        private CryptoInterface $crypto,
        private ClockInterface $clock,
    ) {
    }

    /**
     * Seal the bridge state into an opaque `state` value for the authorize request.
     *
     * @param string $codeVerifier The PKCE verifier; confidential, never readable in the URL.
     * @param string $patientUuid The FHIR Patient logical id of the chart that launched.
     *
     * @return string An encrypted, authenticated token safe to pass as `state`.
     *
     * @throws \JsonException If the payload cannot be encoded (unreachable for these string inputs).
     */
    public function encode(string $codeVerifier, string $patientUuid): string
    {
        $payload = json_encode([
            'v' => $codeVerifier,
            'p' => $patientUuid,
            'exp' => $this->clock->now()->getTimestamp() + self::TTL_SECONDS,
        ], JSON_THROW_ON_ERROR);

        return $this->crypto->encryptStandard($payload);
    }

    /**
     * Recover and validate the bridge state returned on the callback.
     *
     * @param string $state The `state` parameter exactly as echoed by the authorization server.
     *
     * @return array{codeVerifier: string, patientUuid: string}
     *
     * @throws LaunchException If the blob fails authentication, is malformed, or has expired.
     */
    public function decode(string $state): array
    {
        $plaintext = $this->crypto->decryptStandard($state);
        if ($plaintext === false) {
            // Wrong key, tampering, or truncation -- indistinguishable here and all fail-closed.
            throw new LaunchException('SMART launch state failed authentication');
        }

        try {
            $payload = json_decode($plaintext, associative: true, flags: JSON_THROW_ON_ERROR);
        } catch (\JsonException $exception) {
            throw new LaunchException('SMART launch state is not valid JSON', previous: $exception);
        }

        if (
            !is_array($payload)
            || !is_string($payload['v'] ?? null)
            || !is_string($payload['p'] ?? null)
            || !is_int($payload['exp'] ?? null)
        ) {
            throw new LaunchException('SMART launch state is missing required fields');
        }

        if ($payload['exp'] < $this->clock->now()->getTimestamp()) {
            throw new LaunchException('SMART launch state has expired');
        }

        return ['codeVerifier' => $payload['v'], 'patientUuid' => $payload['p']];
    }
}
