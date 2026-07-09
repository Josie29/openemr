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

use OpenEMR\Common\Session\SessionUtil;
use OpenEMR\Modules\AiCopilot\Exception\LaunchException;
use Symfony\Component\HttpFoundation\Session\SessionInterface;

/**
 * The short-lived state the launch leg hands to the callback leg, via the physician's core session.
 *
 * Holding the PKCE verifier server-side (rather than in the browser) is what keeps the authorization
 * code useless to anyone who intercepts the callback redirect. The expected patient UUID is stashed
 * alongside it so the callback can prove the token it received is bound to the chart the physician
 * actually had open, rather than trusting the redirect's contents.
 *
 * Writes go through {@see SessionUtil}, which forces a writable session for the duration of the
 * write — a direct `$session->set()` silently no-ops on OpenEMR's default read-and-close sessions
 * (the ForbidDirectSessionWrite PHPStan rule enforces this). Reads use the injected session
 * directly, which is safe on a closed session.
 */
final readonly class LaunchSession
{
    private const KEY_STATE = 'aicopilot_state';
    private const KEY_VERIFIER = 'aicopilot_code_verifier';
    private const KEY_PATIENT_UUID = 'aicopilot_patient_uuid';

    public function __construct(private SessionInterface $session)
    {
    }

    /**
     * Record a launch in flight, replacing any previous one.
     *
     * @param string $state Opaque CSRF value echoed back on the callback.
     * @param string $codeVerifier The PKCE verifier; never leaves the server.
     * @param string $patientUuid The FHIR Patient logical id of the open chart.
     */
    public function begin(string $state, string $codeVerifier, string $patientUuid): void
    {
        SessionUtil::setSession([
            self::KEY_STATE => $state,
            self::KEY_VERIFIER => $codeVerifier,
            self::KEY_PATIENT_UUID => $patientUuid,
        ]);
    }

    /**
     * Consume the in-flight launch, verifying the returned `state` first.
     *
     * Single-use by construction: the keys are cleared before this returns, on both the success and
     * the failure path, so a replayed callback finds nothing to match against.
     *
     * @param string $state The `state` parameter as returned by the authorization server.
     *
     * @return array{codeVerifier: string, patientUuid: string}
     *
     * @throws LaunchException If no launch is in flight, or `state` does not match.
     */
    public function consume(string $state): array
    {
        $expectedState = $this->session->get(self::KEY_STATE);
        $codeVerifier = $this->session->get(self::KEY_VERIFIER);
        $patientUuid = $this->session->get(self::KEY_PATIENT_UUID);

        $this->clear();

        if (!is_string($expectedState) || !is_string($codeVerifier) || !is_string($patientUuid)) {
            throw new LaunchException('No SMART launch is in flight for this session');
        }
        // hash_equals: constant-time, so a mismatched state cannot be discovered by timing.
        if (!hash_equals($expectedState, $state)) {
            throw new LaunchException('SMART launch state mismatch; refusing to exchange the code');
        }

        return ['codeVerifier' => $codeVerifier, 'patientUuid' => $patientUuid];
    }

    public function clear(): void
    {
        SessionUtil::unsetSession([self::KEY_STATE, self::KEY_VERIFIER, self::KEY_PATIENT_UUID]);
    }
}
