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
 * One demographic field read off an intake form, ready to overwrite `patient_data` — behind a gate.
 *
 * Mirrors a field of the agent's `Demographics` model (`agent/src/copilot/ingestion/schemas.py`).
 * Unlike every other derived fact this is a **destructive in-place overwrite** of clinician-entered
 * identity data with no not-confirmed marker, so it is never written automatically: a clinician
 * reviews a chart-vs-document diff and accepts it per field ({@see DemographicProjector}), which makes
 * them the author of the change.
 *
 * The value is the verbatim printed text (so it can be located on the page). Turning it into what
 * `patient_data` stores — a normalized date, a canonical sex — happens at write time, not here.
 */
final readonly class DerivedDemographic
{
    /** patient_data's widest mapped column (street) is 255; refuse rather than truncate identity data. */
    private const MAX_LENGTH = 255;

    /**
     * @param DemographicField $field Which demographic this is; picks the `patient_data` column.
     * @param string $value The value as printed on the form.
     * @param BoundingBox|null $box Where on the page it was read, when the extractor resolved it.
     * @param int $page 1-based page within the source document.
     * @param float|null $confidence Extractor confidence 0.0-1.0, or null when not reported.
     *
     * @throws \DomainException When the fact could not be persisted faithfully.
     */
    public function __construct(
        public DemographicField $field,
        public string $value,
        public ?BoundingBox $box = null,
        public int $page = 1,
        public ?float $confidence = null,
    ) {
        if (trim($value) === '') {
            throw new \DomainException('A demographic fact needs a value.');
        }
        if (strlen($value) > self::MAX_LENGTH) {
            throw new \DomainException('Demographic value is too long to store without truncating it.');
        }
        if ($page < 1) {
            throw new \DomainException('Page numbers are 1-based.');
        }
        if ($confidence !== null && ($confidence < 0.0 || $confidence > 1.0)) {
            throw new \DomainException('Confidence must fall between 0.0 and 1.0.');
        }
    }

    /** The stable sidecar identity — one per demographic field. */
    public function factKey(): string
    {
        return FactIdentity::for('demographic', $this->field->value);
    }
}
