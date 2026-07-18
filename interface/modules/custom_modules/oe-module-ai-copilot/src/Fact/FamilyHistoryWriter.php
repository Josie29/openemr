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

use OpenEMR\Services\SocialHistoryService;

/**
 * Persists agent-derived family history into OpenEMR's `history_data` table.
 *
 * This is the target JOS-81 believed did not exist: `history_data` has a free-text column per
 * relative (`history_mother`, `history_father`, …) that the History → Family History tab renders, and
 * `SocialHistoryService` writes it. The claim was true only through the FHIR lens — there is no FHIR
 * FamilyMemberHistory read-back and no `verification` column here.
 *
 * Absent a marker column, the not-confirmed signal is an **inline annotation** on the value itself
 * ({@see self::MARKER}). `SocialHistoryService` also locks `created_by` to the session user, so
 * authorship cannot carry the signal; the annotation does, and it is visible where a clinician
 * actually reads it. Diagnosis codes (`dc_*`) are left empty — the form prints none and fabricating
 * one would launder a guess into a coded assertion.
 *
 * `history_data` is append-only: each write inserts a new row carrying the prior row forward, so the
 * relation columns are *appended* to, never clobbered, and re-extraction is idempotent — a condition
 * already present on the latest row is skipped and no new row is written.
 */
final readonly class FamilyHistoryWriter
{
    /** The inline not-confirmed marker, since history_data has no verification column. */
    private const MARKER = ' [AI Co-Pilot - unconfirmed, from intake form]';

    public function __construct(
        private ExtractionSidecar $sidecar,
        private SocialHistoryService $history = new SocialHistoryService(),
    ) {
    }

    /**
     * Persist derived family-history items for one source document.
     *
     * @param list<DerivedFamilyHistory> $items
     *
     * @throws \RuntimeException When the write fails; the transaction is rolled back.
     */
    public function write(
        int $pid,
        int $documentId,
        string $contentHash,
        array $items,
        string $username,
    ): FamilyOutcome {
        // The latest history_data snapshot (SELECT *), or false when the patient has none yet.
        $current = $this->history->getHistoryData($pid);
        $currentRow = is_array($current) ? $current : [];

        $written = [];
        $skipped = [];
        /** @var array<string, string> $delta Column => its full new value (existing text + appends). */
        $delta = [];
        /** @var list<DerivedFamilyHistory> $toCite Items that produced a new append. */
        $toCite = [];

        foreach ($items as $item) {
            $key = $item->factKey();
            $column = self::columnFor($item->relation);
            if ($column === null) {
                // A relation we cannot place on a specific relative column (e.g. a grandparent) — skip
                // rather than mis-file it under the wrong relative.
                $skipped[] = $key;
                continue;
            }

            $existing = $delta[$column] ?? (string) ($currentRow[$column] ?? '');
            if (self::alreadyContains($existing, $item->condition)) {
                $skipped[] = $key;
                continue;
            }

            $entry = $item->condition . self::MARKER;
            $delta[$column] = $existing === '' ? $entry : $existing . '; ' . $entry;
            $written[] = $key;
            $toCite[] = $item;
        }

        if ($delta === []) {
            // Everything was already on the chart (or unmappable) — no new row, which is the
            // idempotency guarantee append-only tables cannot give for free.
            return new FamilyOutcome($written, $skipped);
        }

        sqlBeginTrans();
        try {
            $rowId = $this->persist($pid, $currentRow, $delta);
            foreach ($toCite as $item) {
                $this->sidecar->record(
                    documentId: $documentId,
                    contentHash: $contentHash,
                    pid: $pid,
                    factTable: 'history_data',
                    factId: $rowId,
                    field: $item->factKey(),
                    page: $item->page,
                    box: $item->box,
                    confidence: $item->confidence,
                    username: $username,
                );
            }
            sqlCommitTrans();

            return new FamilyOutcome($written, $skipped);
        } catch (\Throwable $e) {
            sqlRollbackTrans();
            throw new \RuntimeException('Failed to persist derived family history.', previous: $e);
        }
    }

    /**
     * Write one new history_data row carrying the delta, and return its id.
     *
     * Uses `updateHistoryDataForPatientPid` (which copies the latest row forward) when a row exists,
     * and `create` otherwise — the former fails on a patient with no history row because it never
     * re-supplies the pid.
     *
     * @param array<string, mixed> $currentRow
     * @param array<string, string> $delta
     *
     * @throws \RuntimeException When the service does not return a row id.
     */
    private function persist(int $pid, array $currentRow, array $delta): int
    {
        $saved = isset($currentRow['pid'])
            ? $this->history->updateHistoryDataForPatientPid($pid, $delta)
            : $this->history->create(['pid' => $pid, ...$delta]);

        if (!is_array($saved) || empty($saved['id'])) {
            throw new \RuntimeException('The history service did not return a row id.');
        }

        return (int) $saved['id'];
    }

    /**
     * The `history_data` column a relation belongs to, or null when it maps to no specific relative.
     *
     * Grandparents and other relatives OpenEMR does not model get null (skipped, not mis-filed).
     */
    private static function columnFor(string $relation): ?string
    {
        $r = strtolower(trim($relation));

        return match (true) {
            str_contains($r, 'grand') => null,
            str_contains($r, 'mother') || str_contains($r, 'mom') => 'history_mother',
            str_contains($r, 'father') || str_contains($r, 'dad') => 'history_father',
            str_contains($r, 'brother') || str_contains($r, 'sister') || str_contains($r, 'sibling') => 'history_siblings',
            str_contains($r, 'spouse') || str_contains($r, 'husband') || str_contains($r, 'wife') => 'history_spouse',
            str_contains($r, 'son') || str_contains($r, 'daughter') || str_contains($r, 'child') || str_contains($r, 'offspring') => 'history_offspring',
            default => null,
        };
    }

    /** Case-insensitive test for whether a relative's column already records this condition. */
    private static function alreadyContains(string $existing, string $condition): bool
    {
        return $existing !== '' && str_contains(strtolower($existing), strtolower(trim($condition)));
    }
}
