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
 * One payload's facts, sorted by destination.
 *
 * The two families take different write paths (a lab means four rows down the procedure chain; an
 * intake fact means a `lists` row), so they are separated once at the boundary rather than
 * re-inspected downstream.
 */
final readonly class ParsedFacts
{
    /**
     * @param list<DerivedLabResult> $labs
     * @param list<DerivedAllergy> $allergies
     * @param list<DerivedMedication> $medications
     */
    public function __construct(
        public array $labs,
        public array $allergies,
        public array $medications,
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
