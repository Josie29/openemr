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
 * Parses the sidebar's derived-fact payload into typed facts at the trust boundary.
 *
 * These facts arrive from the browser, not from the agent service directly — the agent runs with a
 * patient-scoped SMART token and no OpenEMR session, so it cannot reach a session-authenticated
 * endpoint (see `context/specs/derived-fact-write-back.md`). That means the payload is
 * client-supplied and must be treated as untrusted input: parse it into value objects that enforce
 * their own invariants, and reject anything malformed rather than coercing it.
 *
 * The mapping mirrors the agent's `LabResult` Pydantic model, whose `AbnormalFlag` enum shares its
 * values with `procedure_result.abnormal` — so the two line up without translation.
 */
final readonly class FactPayloadParser
{
    /**
     * Parse a mixed payload, sorting each fact to its destination by its `type` discriminator.
     *
     * @param array<mixed> $facts The decoded `facts` array from the request body.
     *
     * @throws \DomainException When any fact is malformed or carries an unpersistable type.
     *                          All-or-nothing, as below.
     */
    public function parse(array $facts): ParsedFacts
    {
        $labs = [];
        $allergies = [];
        $medications = [];

        foreach ($facts as $index => $fact) {
            if (!is_array($fact)) {
                throw new \DomainException("Fact #$index is not an object.");
            }

            $rawType = $fact['type'] ?? null;
            if (!is_string($rawType) || $rawType === '') {
                throw new \DomainException("Fact #$index is missing 'type'.");
            }
            // No default. A fact whose type we do not recognise must not be quietly treated as a
            // lab, and the agent extracts kinds we deliberately never persist (demographics, chief
            // concern, family history) — those must be refused here, not written somewhere wrong.
            $type = FactType::tryFrom($rawType);
            if ($type === null) {
                throw new \DomainException("Fact #$index has an unpersistable type '$rawType'.");
            }

            match ($type) {
                FactType::Lab => $labs[] = $this->parseLabResult($fact, $index),
                FactType::Allergy => $allergies[] = $this->parseAllergy($fact, $index),
                FactType::Medication => $medications[] = $this->parseMedication($fact, $index),
            };
        }

        return new ParsedFacts($labs, $allergies, $medications);
    }

    /**
     * @param array<mixed> $facts The decoded `facts` array from the request body.
     *
     * @return list<DerivedLabResult>
     *
     * @throws \DomainException When any fact is malformed. All-or-nothing: a partial write would
     *                          leave the chart holding some of a document's results and not others,
     *                          with no signal that anything was dropped.
     */
    public function parseLabResults(array $facts): array
    {
        $parsed = [];
        foreach ($facts as $index => $fact) {
            if (!is_array($fact)) {
                throw new \DomainException("Fact #$index is not an object.");
            }
            $parsed[] = $this->parseLabResult($fact, $index);
        }

        return $parsed;
    }

    /**
     * @param array<mixed> $fact
     *
     * @throws \DomainException
     */
    private function parseAllergy(array $fact, int $index): DerivedAllergy
    {
        return new DerivedAllergy(
            substance: $this->stringField($fact, 'substance', $index),
            reaction: $this->nullableStringField($fact, 'reaction', $index),
            box: $this->parseOptionalBox($fact, $index),
            page: $this->parsePage($fact, $index),
            confidence: $this->parseConfidence($fact, $index),
        );
    }

    /**
     * @param array<mixed> $fact
     *
     * @throws \DomainException
     */
    private function parseMedication(array $fact, int $index): DerivedMedication
    {
        return new DerivedMedication(
            name: $this->stringField($fact, 'name', $index),
            dose: $this->nullableStringField($fact, 'dose', $index),
            frequency: $this->nullableStringField($fact, 'frequency', $index),
            box: $this->parseOptionalBox($fact, $index),
            page: $this->parsePage($fact, $index),
            confidence: $this->parseConfidence($fact, $index),
        );
    }

    /**
     * @param array<mixed> $fact
     *
     * @throws \DomainException
     */
    private function parseLabResult(array $fact, int $index): DerivedLabResult
    {
        $abnormalRaw = $this->stringField($fact, 'abnormal', $index, required: false);
        $abnormal = $abnormalRaw === ''
            ? AbnormalFlag::No
            : AbnormalFlag::tryFrom($abnormalRaw);
        if ($abnormal === null) {
            throw new \DomainException("Fact #$index has an unrecognised abnormal flag '$abnormalRaw'.");
        }

        // DerivedLabResult's constructor enforces the rest (non-empty code/label/value, no
        // DNR/TNP sentinel, 1-based page, confidence range) — the constraints OpenEMR's FHIR
        // projection depends on but does not itself check.
        return new DerivedLabResult(
            loincCode: $this->stringField($fact, 'loinc', $index),
            label: $this->stringField($fact, 'label', $index),
            value: $this->stringField($fact, 'value', $index),
            units: $this->stringField($fact, 'units', $index, required: false),
            referenceRange: $this->stringField($fact, 'range', $index, required: false),
            abnormal: $abnormal,
            box: $this->parseBox($fact, $index),
            page: $this->parsePage($fact, $index),
            confidence: $this->parseConfidence($fact, $index),
        );
    }

    /**
     * A box for a fact that is allowed not to have one.
     *
     * Only lab results require geometry agent-side (`LabResult._require_bounding_box`); an intake
     * fact the model read without resolving a box is still a valid fact — it just cannot be clicked
     * back to the page. A box that is *present* must still be well-formed: a malformed box is a bug,
     * not an absence.
     *
     * @param array<mixed> $fact
     *
     * @throws \DomainException
     */
    private function parseOptionalBox(array $fact, int $index): ?BoundingBox
    {
        if (!isset($fact['bbox']) || $fact['bbox'] === null) {
            return null;
        }

        return $this->parseBox($fact, $index);
    }

    /**
     * @param array<mixed> $fact
     *
     * @throws \DomainException
     */
    private function parseConfidence(array $fact, int $index): ?float
    {
        if (!isset($fact['confidence'])) {
            return null;
        }
        if (!is_numeric($fact['confidence'])) {
            throw new \DomainException("Fact #$index has a non-numeric confidence.");
        }

        return (float) $fact['confidence'];
    }

    /**
     * An optional free-text field, absent rather than empty when the extractor did not read one.
     *
     * @param array<mixed> $fact
     *
     * @throws \DomainException
     */
    private function nullableStringField(array $fact, string $key, int $index): ?string
    {
        if (!isset($fact[$key]) || $fact[$key] === '') {
            return null;
        }

        return $this->stringField($fact, $key, $index);
    }

    /**
     * @param array<mixed> $fact
     *
     * @throws \DomainException
     */
    private function parseBox(array $fact, int $index): BoundingBox
    {
        $box = $fact['bbox'] ?? null;
        if (!is_array($box)) {
            throw new \DomainException("Fact #$index is missing its bounding box.");
        }
        foreach (['x', 'y', 'w', 'h'] as $key) {
            if (!isset($box[$key]) || !is_numeric($box[$key])) {
                throw new \DomainException("Fact #$index has a bounding box missing numeric '$key'.");
            }
        }

        return new BoundingBox((float) $box['x'], (float) $box['y'], (float) $box['w'], (float) $box['h']);
    }

    /**
     * @param array<mixed> $fact
     *
     * @throws \DomainException
     */
    private function parsePage(array $fact, int $index): int
    {
        $page = $fact['page'] ?? 1;
        if (!is_int($page) && !(is_string($page) && ctype_digit($page))) {
            throw new \DomainException("Fact #$index has a non-integer page.");
        }

        return (int) $page;
    }

    /**
     * @param array<mixed> $fact
     *
     * @throws \DomainException
     */
    private function stringField(array $fact, string $key, int $index, bool $required = true): string
    {
        $value = $fact[$key] ?? null;
        if ($value === null || $value === '') {
            if ($required) {
                throw new \DomainException("Fact #$index is missing '$key'.");
            }
            return '';
        }
        if (!is_string($value) && !is_numeric($value)) {
            throw new \DomainException("Fact #$index has a non-scalar '$key'.");
        }

        return trim((string) $value);
    }
}
