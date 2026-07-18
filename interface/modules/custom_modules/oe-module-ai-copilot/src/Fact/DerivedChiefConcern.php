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
 * The chief concern read off an intake form, ready to persist as a visit's reason.
 *
 * Mirrors the agent's `IntakeForm.chief_concern` (`agent/src/copilot/ingestion/schemas.py`). It
 * becomes `form_encounter.reason` on a new intake-derived encounter (reads back as
 * `Encounter.reasonCode`). There is at most one per document, so its sidecar identity is fixed.
 *
 * The text is stored verbatim as read — the chief concern *is* the reason for the visit, and
 * inventing structure (an HPI, coded reason) around it would assert more than the form said.
 */
final readonly class DerivedChiefConcern
{
    /** The stable sidecar identity — a document has one chief concern. */
    public const FACT_KEY = 'chief_concern';

    /** form_encounter.reason is longtext; cap to keep a runaway payload out of the chart. */
    private const MAX_LENGTH = 2000;

    /**
     * @param string $text The chief concern as written; becomes `form_encounter.reason`.
     * @param BoundingBox|null $box Where on the page it was read, when the extractor resolved it.
     * @param int $page 1-based page within the source document.
     * @param float|null $confidence Extractor confidence 0.0-1.0, or null when not reported.
     *
     * @throws \DomainException When the fact could not be persisted faithfully.
     */
    public function __construct(
        public string $text,
        public ?BoundingBox $box = null,
        public int $page = 1,
        public ?float $confidence = null,
    ) {
        if (trim($text) === '') {
            throw new \DomainException('A chief concern needs text — it becomes the visit reason.');
        }
        if (strlen($text) > self::MAX_LENGTH) {
            throw new \DomainException('Chief concern is too long to store as a visit reason.');
        }
        if ($page < 1) {
            throw new \DomainException('Page numbers are 1-based.');
        }
        if ($confidence !== null && ($confidence < 0.0 || $confidence > 1.0)) {
            throw new \DomainException('Confidence must fall between 0.0 and 1.0.');
        }
    }

    public function factKey(): string
    {
        return self::FACT_KEY;
    }
}
