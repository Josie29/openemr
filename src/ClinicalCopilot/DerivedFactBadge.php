<?php

/**
 * Chart badge for a Co-Pilot-proposed fact a clinician has not yet confirmed.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\ClinicalCopilot;

/**
 * Single source for the "AI-proposed, unconfirmed" marker shared by the chart surfaces that show
 * derived facts (Patient Issues list, lab report). Core-side so it never depends on the module.
 */
final class DerivedFactBadge
{
    /** FHIR AllergyIntolerance.verificationStatus stamped on a derived allergy. */
    public const ALLERGY_VERIFICATION = 'unconfirmed';

    /** FHIR MedicationRequest.intent stamped on a derived medication. */
    public const MEDICATION_INTENT = 'proposal';

    /** FHIR Observation.status stamped on a derived lab report. */
    public const LAB_STATUS = 'preliminary';

    /**
     * Badge markup when $isDerived, else '' (so callers can echo unconditionally).
     *
     * @param string $extraClass Extra CSS class(es), e.g. 'ml-1' for inline spacing.
     */
    public static function html(bool $isDerived, string $extraClass = ''): string
    {
        if (!$isDerived) {
            return '';
        }

        $classes = trim('badge badge-warning ' . $extraClass);

        return '<span class="' . attr($classes) . '" data-toggle="tooltip" title="'
            . attr(xl('AI-proposed from a patient document — not yet clinician-confirmed'))
            . '">' . xlt('Unconfirmed') . '</span>';
    }
}
