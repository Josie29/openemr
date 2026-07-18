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

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Services\PatientService;

/**
 * Overwrites `patient_data` with agent-derived demographics — only for fields a clinician accepted.
 *
 * This is the one write with no honest not-confirmed marker: `patient_data` has no verification
 * concept, and a write here silently replaces clinician-entered identity data in place. So it never
 * runs automatically. {@see preview()} returns a chart-vs-document diff for the sidebar's review card;
 * {@see write()} runs only once the clinician has accepted, which makes them the author.
 *
 * The extracted value is the verbatim printed text (so it could be located on the page); turning it
 * into what `patient_data` stores — a normalized date, a canonical sex — happens here. A value that
 * cannot be normalized safely (an unparseable date, an ambiguous sex) is skipped rather than written
 * wrong. `full_name` is skipped entirely — splitting one printed name into fname/mname/lname is
 * guesswork — but still shown in the preview so a clinician sees the discrepancy.
 */
final readonly class DemographicWriter
{
    public function __construct(
        private ExtractionSidecar $sidecar,
        private PatientService $patients = new PatientService(),
    ) {
    }

    /**
     * The chart-vs-document diff for the review card. Read-only — never mutates the chart.
     *
     * @param list<DerivedDemographic> $facts
     *
     * @return list<array{field: string, chart: string, extracted: string, page: int}>
     */
    public function preview(int $pid, array $facts): array
    {
        $diff = [];
        foreach ($facts as $fact) {
            $diff[] = [
                'field' => $fact->field->value,
                'chart' => $this->chartValue($pid, $fact->field),
                'extracted' => $fact->value,
                'page' => $fact->page,
            ];
        }

        return $diff;
    }

    /**
     * Persist the accepted demographics for one source document.
     *
     * @param list<DerivedDemographic> $facts The fields the clinician accepted.
     *
     * @throws \RuntimeException When the update fails; the transaction is rolled back.
     */
    public function write(
        int $pid,
        int $documentId,
        string $contentHash,
        array $facts,
        string $username,
    ): FamilyOutcome {
        $written = [];
        $skipped = [];

        /** @var array<string, string> $update column => normalized value */
        $update = [];
        /** @var list<DerivedDemographic> $toCite */
        $toCite = [];

        foreach ($facts as $fact) {
            $key = $fact->factKey();
            $column = $fact->field->patientColumn();
            $normalized = $column === null ? null : $this->normalize($fact->field, $fact->value);
            // Not writable (full_name) or not safely normalizable, or already equal to the chart value.
            if ($column === null || $normalized === null || $normalized === $this->chartValue($pid, $fact->field)) {
                $skipped[] = $key;
                continue;
            }
            $update[$column] = $normalized;
            $written[] = $key;
            $toCite[] = $fact;
        }

        if ($update === []) {
            return new FamilyOutcome($written, $skipped);
        }

        sqlBeginTrans();
        try {
            $result = $this->patients->update($this->patientUuid($pid), $update);
            if (!$result->isValid()) {
                throw new \RuntimeException('The patient service rejected a demographic update.');
            }
            foreach ($toCite as $fact) {
                $this->sidecar->record(
                    documentId: $documentId,
                    contentHash: $contentHash,
                    pid: $pid,
                    factTable: 'patient_data',
                    factId: $pid,
                    field: $fact->factKey(),
                    page: $fact->page,
                    box: $fact->box,
                    confidence: $fact->confidence,
                    username: $username,
                );
            }
            sqlCommitTrans();

            return new FamilyOutcome($written, $skipped);
        } catch (\Throwable $e) {
            sqlRollbackTrans();
            throw new \RuntimeException('Failed to persist accepted demographics.', previous: $e);
        }
    }

    /**
     * The value `patient_data` currently holds for a field, for the diff.
     *
     * `full_name` has no single column, so it is reconstructed from the name parts.
     */
    private function chartValue(int $pid, DemographicField $field): string
    {
        if ($field === DemographicField::FullName) {
            $row = QueryUtils::fetchRecords(
                "SELECT TRIM(CONCAT_WS(' ', fname, mname, lname)) AS full FROM patient_data WHERE pid = ?",
                [$pid],
            );

            return (string) ($row[0]['full'] ?? '');
        }

        $column = $field->patientColumn();
        if ($column === null) {
            return '';
        }

        // $column comes from the DemographicField enum, never user input, so it is safe to inline.
        return (string) (QueryUtils::fetchSingleValue(
            "SELECT `$column` FROM patient_data WHERE pid = ?",
            $column,
            [$pid],
        ) ?? '');
    }

    /**
     * Normalize a printed value into what `patient_data` stores, or null when it cannot be done safely.
     */
    private function normalize(DemographicField $field, string $value): ?string
    {
        return match ($field) {
            DemographicField::DateOfBirth => $this->normalizeDate($value),
            DemographicField::Sex => $this->normalizeSex($value),
            // Address (→ street) and phone are stored as printed.
            default => trim($value),
        };
    }

    /**
     * Parse a printed date into `Y-m-d`, or null if no known format matches.
     *
     * The extractor emits the date exactly as printed ("03 / 14 / 1979"), which `patient_data.DOB`
     * cannot store — so it is parsed here, and refused rather than guessed if it does not match.
     */
    private function normalizeDate(string $value): ?string
    {
        $compact = str_replace(' ', '', trim($value));
        foreach (['m/d/Y', 'n/j/Y', 'Y-m-d', 'm-d-Y', 'n-j-Y', 'Y/m/d'] as $format) {
            $date = \DateTimeImmutable::createFromFormat('!' . $format, $compact);
            if ($date !== false && $date->format($format) === $compact) {
                return $date->format('Y-m-d');
            }
        }

        return null;
    }

    /** Canonicalize a printed sex to the 'Male'/'Female' values `patient_data.sex` stores, or null. */
    private function normalizeSex(string $value): ?string
    {
        $v = strtolower(trim($value));

        return match (true) {
            $v === 'm' || $v === 'male' => 'Male',
            $v === 'f' || $v === 'female' => 'Female',
            default => null,
        };
    }

    /**
     * @throws \RuntimeException When the patient has no uuid.
     */
    private function patientUuid(int $pid): string
    {
        $uuid = QueryUtils::fetchSingleValue('SELECT uuid FROM patient_data WHERE pid = ?', 'uuid', [$pid]);
        if ($uuid === null) {
            throw new \RuntimeException('The session patient has no uuid.');
        }

        return UuidRegistry::uuidToString($uuid);
    }
}
