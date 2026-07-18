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
 * One family-history item read off an intake form, ready to persist to `history_data`.
 *
 * Mirrors the agent's `FamilyHistoryItem` model (`agent/src/copilot/ingestion/schemas.py`): a
 * condition and the relative it belongs to. It becomes free text appended to the per-relative column
 * OpenEMR's History → Family History tab renders (`history_mother`, `history_father`, …).
 *
 * Unlike labs the box is optional. Deliberately no coded diagnosis: `history_data` has paired `dc_*`
 * columns for SNOMED/ICD codes, but the extractor reads none off the form, and fabricating one from
 * free text would launder a guess into a coded assertion — the same refusal the allergy and lab
 * paths make. The relation drives *which* column; the condition is the value.
 */
final readonly class DerivedFamilyHistory
{
    /** history_data per-relative columns are longtext; cap to a sane length rather than store prose. */
    private const MAX_LENGTH = 255;

    /**
     * @param string $condition The condition or diagnosis; the value appended to the relative column.
     * @param string $relation The relative it belongs to (mother, father, brother, …); picks the column.
     * @param BoundingBox|null $box Where on the page it was read, when the extractor resolved it.
     * @param int $page 1-based page within the source document.
     * @param float|null $confidence Extractor confidence 0.0-1.0, or null when not reported.
     *
     * @throws \DomainException When the fact could not be persisted faithfully.
     */
    public function __construct(
        public string $condition,
        public string $relation,
        public ?BoundingBox $box = null,
        public int $page = 1,
        public ?float $confidence = null,
    ) {
        if (trim($condition) === '') {
            throw new \DomainException('A family-history item needs a condition.');
        }
        if (trim($relation) === '') {
            throw new \DomainException('A family-history item needs a relation to place it on a relative.');
        }
        if (strlen($condition) > self::MAX_LENGTH || strlen($relation) > self::MAX_LENGTH) {
            throw new \DomainException('Family-history text is too long to store without truncating it.');
        }
        if ($page < 1) {
            throw new \DomainException('Page numbers are 1-based.');
        }
        if ($confidence !== null && ($confidence < 0.0 || $confidence > 1.0)) {
            throw new \DomainException('Confidence must fall between 0.0 and 1.0.');
        }
    }

    /** The stable sidecar identity — one per (relative, condition) pair. */
    public function factKey(): string
    {
        return FactIdentity::for('family_history', $this->relation . ' ' . $this->condition);
    }
}
