<?php

/**
 * @package   OpenEMR\Modules\AiCopilot
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\AiCopilot\Fact;

/**
 * One payload's facts, sorted by family at the trust boundary.
 *
 * Each family takes a different write path (a lab means four rows down the procedure chain; an intake
 * fact means a `lists` row; family history means a `history_data` row; chief concern a new encounter;
 * a demographic an accept-gated `patient_data` overwrite), so they are separated once here rather than
 * re-inspected downstream. The projectors read the family they own.
 */
final readonly class ParsedFacts
{
    /**
     * @param list<DerivedLabResult> $labs
     * @param list<DerivedAllergy> $allergies
     * @param list<DerivedMedication> $medications
     * @param list<DerivedFamilyHistory> $familyHistory
     * @param list<DerivedChiefConcern> $chiefConcerns
     * @param list<DerivedDemographic> $demographics
     */
    public function __construct(
        public array $labs,
        public array $allergies,
        public array $medications,
        public array $familyHistory,
        public array $chiefConcerns,
        public array $demographics,
    ) {
    }

    public function hasLabs(): bool
    {
        return $this->labs !== [];
    }

    public function hasIntakeFacts(): bool
    {
        return $this->allergies !== [] || $this->medications !== [];
    }
}
