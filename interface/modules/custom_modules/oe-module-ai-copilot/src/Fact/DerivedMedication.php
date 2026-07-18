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
 * One medication read off an uploaded medication list, ready to persist as a `lists` row.
 *
 * Mirrors the agent's `Medication` model (`agent/src/copilot/ingestion/schemas.py`): a name plus
 * unparsed free-text dose and frequency. The extractor reports no route, status, dates, or RxNorm
 * code, so none are invented here — `PrescriptionService`'s `lists` branch selects `NULL` for
 * route, unit, and rxnorm anyway ("we don't have rxnorm codes for free text meds").
 *
 * As with allergies the bounding box is optional — only `LabResult` requires one agent-side.
 */
final readonly class DerivedMedication
{
    /** Matches lists.title. */
    private const MAX_NAME_LENGTH = 255;

    /**
     * @param string $name The drug name; becomes `lists.title`.
     * @param string|null $dose Free text as written, e.g. '500 mg'.
     * @param string|null $frequency Free text as written, e.g. 'twice daily'.
     * @param BoundingBox|null $box Where on the page it was read, when the extractor resolved it.
     * @param int $page 1-based page within the source document.
     * @param float|null $confidence Extractor confidence 0.0-1.0, or null when not reported.
     *
     * @throws \DomainException When the fact could not be persisted faithfully.
     */
    public function __construct(
        public string $name,
        public ?string $dose = null,
        public ?string $frequency = null,
        public ?BoundingBox $box = null,
        public int $page = 1,
        public ?float $confidence = null,
    ) {
        if (trim($name) === '') {
            throw new \DomainException('A medication needs a name — it becomes the record title.');
        }
        if (strlen($name) > self::MAX_NAME_LENGTH) {
            throw new \DomainException('Medication name is too long to store without truncating it.');
        }
        if ($page < 1) {
            throw new \DomainException('Page numbers are 1-based.');
        }
        if ($confidence !== null && ($confidence < 0.0 || $confidence > 1.0)) {
            throw new \DomainException('Confidence must fall between 0.0 and 1.0.');
        }
    }

    /**
     * Dose and frequency recombined as written, for `lists_medication.drug_dosage_instructions`.
     *
     * Surfaces verbatim as `MedicationRequest.dosageInstruction.text`
     * (`FhirMedicationRequestService:264-266`). The agent supplies these unparsed, and they stay
     * unparsed — inferring a structured `doseAndRate` from free text is how a "500 mg" becomes a
     * "500 mL".
     */
    public function dosageInstructions(): ?string
    {
        $parts = array_filter([$this->dose, $this->frequency], static fn(?string $p): bool => $p !== null && trim($p) !== '');

        return $parts === [] ? null : implode(' ', array_map('trim', $parts));
    }

    /** The stable sidecar identity for this medication. */
    public function factKey(): string
    {
        return FactIdentity::for('medication', $this->name);
    }
}
