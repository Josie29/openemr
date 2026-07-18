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
 * One lab value extracted from a source document, ready to persist.
 *
 * Parsed at the boundary (`persist-facts.php`), so anything downstream can trust it. The
 * constructor enforces the constraints OpenEMR's FHIR projection imposes but does not itself
 * check — get these wrong and the Observation degrades or vanishes rather than erroring.
 */
final readonly class DerivedLabResult
{
    /**
     * @param string $loincCode LOINC code; becomes `procedure_result.result_code`.
     * @param string $label Human description; becomes `result_text`.
     * @param string $value The measured value; becomes `result`.
     * @param string $units Unit of measure, may be empty for unitless results.
     * @param string $referenceRange 'low-high' — parsed into the FHIR referenceRange.
     * @param BoundingBox $box Where on the page this value was read from.
     * @param int $page 1-based page within the source document.
     * @param float|null $confidence Extractor confidence, 0.0-1.0, or null when not reported.
     *
     * @throws \DomainException When a constraint the FHIR read path depends on is violated.
     */
    public function __construct(
        public string $loincCode,
        public string $label,
        public string $value,
        public string $units,
        public string $referenceRange,
        public AbnormalFlag $abnormal,
        public BoundingBox $box,
        public int $page = 1,
        public ?float $confidence = null,
    ) {
        // FhirObservationLaboratoryService only emits Observation.code when BOTH result_code and
        // result_text are non-empty; otherwise it silently degrades to a nullFlavor UNK concept.
        if (trim($loincCode) === '') {
            throw new \DomainException('A derived lab result needs a LOINC code, or its Observation.code degrades to UNK.');
        }
        if (trim($label) === '') {
            throw new \DomainException('A derived lab result needs a label, or its Observation.code degrades to UNK.');
        }
        if (trim($value) === '') {
            throw new \DomainException('A derived lab result needs a value.');
        }
        // ProcedureService excludes these two sentinels on read ('did not report' / 'test not
        // performed'), so a result stored with either would insert cleanly and never read back.
        if (in_array(strtoupper(trim($value)), ['DNR', 'TNP'], true)) {
            throw new \DomainException("The value '$value' is a reserved sentinel that OpenEMR filters out on read.");
        }
        if ($page < 1) {
            throw new \DomainException('Page numbers are 1-based.');
        }
        if ($confidence !== null && ($confidence < 0.0 || $confidence > 1.0)) {
            throw new \DomainException('Confidence must fall between 0.0 and 1.0.');
        }
    }
}
