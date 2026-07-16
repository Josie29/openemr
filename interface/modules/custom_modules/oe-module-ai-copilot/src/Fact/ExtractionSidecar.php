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

/**
 * The extraction sidecar — citation geometry for persisted derived facts.
 *
 * Exists because a persisted fact carries its *value* but not the pixel rectangle it was read
 * from: `procedure_result` has a `document_id` FK but no geometry, `lists` has no document link at
 * all, and `FhirProvenanceService` cannot express "derived from document X". Without this, facts
 * would survive a restart while their citations did not.
 *
 * Per `W2_ARCHITECTURE.md` §6 this is a **rebuildable derived cache, not a system of record** —
 * OpenEMR's own tables remain authoritative for the facts themselves. Nothing clinical should be
 * gated on a sidecar row existing.
 */
final readonly class ExtractionSidecar
{
    private const TABLE = 'ai_copilot_document_facts';

    /**
     * Record where a persisted fact was read from.
     *
     * Upserts on `(document_id, content_hash, fact_table, field)` — §3.4's own suggested guard —
     * so a re-run refreshes the citation rather than colliding.
     *
     * @param string $factTable Destination table the fact was written to.
     * @param int $factId Primary key of the written row.
     * @param string $field Field identity — a LOINC code for labs, a stable fact key for intake facts.
     * @param BoundingBox|null $box Null when the extractor resolved no geometry, which is legitimate
     *                              for intake facts (only lab results require a box). Stored as '',
     *                              and `citationsFor()` skips those rows — the fact still persists,
     *                              it simply cannot be clicked back to the page.
     *
     * @throws \RuntimeException When the write fails.
     */
    public function record(
        int $documentId,
        string $contentHash,
        int $pid,
        string $factTable,
        int $factId,
        string $field,
        int $page,
        ?BoundingBox $box,
        ?float $confidence,
        string $username,
    ): void {
        QueryUtils::sqlStatementThrowException(
            'INSERT INTO ' . self::TABLE . ' (document_id, content_hash, pid, fact_table, fact_id,'
            . ' field, page, bbox, confidence, created_by)'
            . ' VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)'
            . ' ON DUPLICATE KEY UPDATE fact_id = VALUES(fact_id), page = VALUES(page),'
            . ' bbox = VALUES(bbox), confidence = VALUES(confidence)',
            [
                $documentId,
                $contentHash,
                $pid,
                $factTable,
                $factId,
                $field,
                $page,
                $box?->toJson() ?? '',
                $confidence,
                $username,
            ],
        );
    }

    /**
     * Every citation recorded for a document version, keyed by field.
     *
     * @return array<string, array{page: int, box: BoundingBox, fact_table: string, fact_id: int}>
     */
    public function citationsFor(int $documentId, string $contentHash): array
    {
        $rows = QueryUtils::fetchRecords(
            'SELECT field, page, bbox, fact_table, fact_id FROM ' . self::TABLE
            . ' WHERE document_id = ? AND content_hash = ?',
            [$documentId, $contentHash],
        );

        $citations = [];
        foreach ($rows as $row) {
            $bbox = (string) $row['bbox'];
            if ($bbox === '') {
                continue;
            }
            $citations[(string) $row['field']] = [
                'page' => (int) $row['page'],
                'box' => BoundingBox::fromJson($bbox),
                'fact_table' => (string) $row['fact_table'],
                'fact_id' => (int) $row['fact_id'],
            ];
        }

        return $citations;
    }
}
