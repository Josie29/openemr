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
 * One allergy read off an intake form, ready to persist as a `lists` row.
 *
 * Mirrors the agent's `Allergy` model (`agent/src/copilot/ingestion/schemas.py`): a substance, an
 * optional free-text reaction, and a citation. Deliberately nothing more — the extractor reports no
 * severity, onset, status, or coded identifier, so this object invents none.
 *
 * Unlike a lab result, the bounding box is **optional**: only `LabResult` requires one agent-side
 * (`_require_bounding_box`), so an allergy the model read without resolving geometry is still a
 * valid fact — it simply cannot be clicked back to the page.
 */
final readonly class DerivedAllergy
{
    /** Matches lists.title. */
    private const MAX_SUBSTANCE_LENGTH = 255;

    /** Matches lists.reaction. */
    private const MAX_REACTION_LENGTH = 255;

    /**
     * @param string $substance What the patient reacts to; becomes `lists.title`.
     * @param string|null $reaction Free-text reaction; becomes `lists.reaction`.
     * @param BoundingBox|null $box Where on the page it was read, when the extractor resolved it.
     * @param int $page 1-based page within the source document.
     * @param float|null $confidence Extractor confidence 0.0-1.0, or null when not reported.
     *
     * @throws \DomainException When the fact could not be persisted faithfully.
     */
    public function __construct(
        public string $substance,
        public ?string $reaction = null,
        public ?BoundingBox $box = null,
        public int $page = 1,
        public ?float $confidence = null,
    ) {
        if (trim($substance) === '') {
            throw new \DomainException('An allergy needs a substance — it becomes the record title.');
        }
        // Truncation here would silently alter a clinical value, so refuse instead.
        if (strlen($substance) > self::MAX_SUBSTANCE_LENGTH) {
            throw new \DomainException('Allergy substance is too long to store without truncating it.');
        }
        if ($reaction !== null && strlen($reaction) > self::MAX_REACTION_LENGTH) {
            throw new \DomainException('Allergy reaction is too long to store without truncating it.');
        }
        if ($page < 1) {
            throw new \DomainException('Page numbers are 1-based.');
        }
        if ($confidence !== null && ($confidence < 0.0 || $confidence > 1.0)) {
            throw new \DomainException('Confidence must fall between 0.0 and 1.0.');
        }
    }

    /** The stable sidecar identity for this allergy. */
    public function factKey(): string
    {
        return FactIdentity::for('allergy', $this->substance);
    }
}
