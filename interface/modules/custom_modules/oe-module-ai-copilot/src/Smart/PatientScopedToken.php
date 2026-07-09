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

use OpenEMR\Modules\AiCopilot\Exception\LaunchException;

/**
 * A SMART access token together with the single patient it is bound to.
 *
 * The `patient` claim is the authoritative identifier for the rest of the flow: it is the id the
 * token actually permits reads against, so the widget sends *this* as `ChatRequest.patient_id`
 * rather than a locally derived one. The two therefore cannot drift.
 */
final readonly class PatientScopedToken
{
    private function __construct(
        public string $accessToken,
        public int $expiresIn,
        public string $patientUuid,
        public string $scope,
    ) {
    }

    /**
     * Parse OpenEMR's token-endpoint response body.
     *
     * @param array<array-key, mixed> $body The decoded JSON response. Keys are read defensively, so
     *     the caller need not have narrowed them to strings first (json_decode yields mixed keys).
     *
     * @return self The parsed token.
     *
     * @throws LaunchException If any required field is absent or of the wrong type. A response with
     *     no `patient` claim means the launch context never reached the token — most often because
     *     the client's registered scopes are missing `launch` — and must never be treated as usable.
     */
    public static function fromTokenResponse(array $body): self
    {
        $accessToken = $body['access_token'] ?? null;
        $expiresIn = $body['expires_in'] ?? null;
        $patientUuid = $body['patient'] ?? null;
        $scope = $body['scope'] ?? '';

        if (!is_string($accessToken) || $accessToken === '') {
            throw new LaunchException('Token response contained no access_token');
        }
        if (!is_int($expiresIn) && !is_string($expiresIn)) {
            throw new LaunchException('Token response contained no usable expires_in');
        }
        if (!is_string($patientUuid) || $patientUuid === '') {
            throw new LaunchException(
                'Token response carried no patient claim; the token is not patient-scoped'
            );
        }

        return new self($accessToken, (int) $expiresIn, $patientUuid, is_string($scope) ? $scope : '');
    }

    /**
     * Fail closed unless this token is bound to the patient whose chart initiated the launch.
     *
     * Defence in depth. The launch token already carries the patient context server-side, but this
     * makes the cross-patient test from the SMART spike an invariant of the code rather than a
     * property we merely observed once.
     *
     * @param string $expectedPatientUuid The chart's patient UUID, from the launch leg.
     *
     * @throws LaunchException If the token is bound to a different patient.
     */
    public function assertBoundTo(string $expectedPatientUuid): void
    {
        if (!hash_equals($expectedPatientUuid, $this->patientUuid)) {
            throw new LaunchException('Token patient claim does not match the chart that launched it');
        }
    }
}
