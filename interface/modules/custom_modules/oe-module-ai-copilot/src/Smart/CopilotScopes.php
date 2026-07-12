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
 * The exact scope set this module registers and requests.
 *
 * Two constraints pin this list, both verified against core:
 *
 * 1. **`launch`, never `launch/patient`.** `SMARTAuthorizationController::needSMARTAuthorization()`
 *    tests `str_contains($scopes, 'launch/patient')` — a substring match on the raw scope string.
 *    Requesting `launch/patient` re-triggers the interactive patient-select picker, which is exactly
 *    what an EHR launch exists to bypass.
 * 2. **`launch` must be present in the *granted* scopes.**
 *    `SMARTSessionTokenContextBuilder::getContextForScopes()` only calls `getEHRLaunchContext()` —
 *    the branch that copies the launch token's patient into the token response's `patient` claim —
 *    when `launch` is granted. Without it the response carries no patient binding.
 *
 * And because `AuthorizationController::processAuthorizeFlowForLaunch()` overrides the request's
 * scopes with whatever is registered on the `oauth_clients` row, this list must match the client
 * registration exactly. See the module README's admin prerequisites.
 */
final readonly class CopilotScopes
{
    /**
     * @var list<string> Ordered so the string form is stable and diffable against the DB row.
     */
    public const SCOPES = [
        'openid',
        'fhirUser',
        'online_access',
        'launch',
        'patient/Patient.read',
        'patient/Condition.read',
        'patient/MedicationRequest.read',
        'patient/AllergyIntolerance.read',
        'patient/Encounter.read',
        'patient/DocumentReference.read',
    ];

    /** The space-delimited form used in the `scope` query parameter and the client registration. */
    public static function asString(): string
    {
        return implode(' ', self::SCOPES);
    }
}
