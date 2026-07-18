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
 * Persists agent-derived lab results into OpenEMR's native lab chain.
 *
 * There is no FHIR or REST write path for lab Observations — both surfaces are read-only (see
 * `context/specs/derived-fact-write-back.md`). The only working substrate is a multi-row insert
 * down OpenEMR's own chain, which then projects back out as a FHIR Observation:
 *
 *     procedure_order → procedure_order_code → procedure_report → procedure_result
 *
 * Synthesizing an order for a result that was never ordered is house style, not an invention: the
 * HL7 receiver does exactly this when results arrive with no matching order
 * (`interface/orders/receive_hl7_results.inc.php:1109-1131`).
 *
 * Everything written here is marked `preliminary` — these facts were read off a PDF by a model, not
 * confirmed by a clinician, and `Observation.status` says so.
 */
final readonly class LabResultWriter
{
    /** Marks derived results as not-yet-clinician-confirmed. A FHIR-valid Observation.status. */
    private const DERIVED_STATUS = 'preliminary';

    /** procedure_order_code.procedure_source '2' = results received from an external source. */
    private const SOURCE_EXTERNAL = '2';

    /** One report per document; its seq must match the order_code's or the chain breaks (see below). */
    private const ORDER_SEQ = 1;

    public function __construct(private ExtractionSidecar $sidecar)
    {
    }

    /**
     * Persist derived lab results for one source document.
     *
     * Idempotent per `W2_ARCHITECTURE.md` §6: a result already present for this document is
     * skipped, so re-running extraction never duplicates clinical rows. The check runs against the
     * destination table rather than the sidecar deliberately — the sidecar is a rebuildable cache
     * (§6), so gating on it would let a cache wipe license duplicate records.
     *
     * @param list<DerivedLabResult> $results
     *
     * @throws \RuntimeException When the chain cannot be written; the transaction is rolled back.
     */
    public function write(
        int $pid,
        int $documentId,
        string $contentHash,
        array $results,
        string $username,
    ): LabWriteOutcome {
        if ($results === []) {
            throw new \DomainException('Refusing to write an empty result set.');
        }

        sqlBeginTrans();
        try {
            $reportId = $this->findReportForDocument($pid, $documentId)
                ?? $this->createChain($pid, $results[0]);

            $written = [];
            $skipped = [];
            foreach ($results as $result) {
                if ($this->alreadyPersisted($documentId, $result->loincCode)) {
                    $skipped[] = $result->loincCode;
                    continue;
                }
                $resultId = $this->insertResult($reportId, $documentId, $result);
                $this->sidecar->record(
                    documentId: $documentId,
                    contentHash: $contentHash,
                    pid: $pid,
                    factTable: 'procedure_result',
                    factId: $resultId,
                    field: $result->loincCode,
                    page: $result->page,
                    box: $result->box,
                    confidence: $result->confidence,
                    username: $username,
                );
                $written[] = $result->loincCode;
            }

            $orderId = $this->orderIdForReport($reportId);
            sqlCommitTrans();

            return new LabWriteOutcome($orderId, $written, $skipped);
        } catch (\Throwable $e) {
            // Without this, a mid-chain failure strands an order with no results — a phantom lab
            // order in the patient's chart.
            sqlRollbackTrans();
            throw new \RuntimeException('Failed to persist derived lab results.', previous: $e);
        }
    }

    /**
     * The existing report for this document, if we have already written results from it.
     *
     * Uses `procedure_result.document_id` — the schema's own native link back to the source blob —
     * rather than stamping a marker into a clinical column.
     */
    private function findReportForDocument(int $pid, int $documentId): ?int
    {
        $reportId = QueryUtils::fetchSingleValue(
            'SELECT presult.procedure_report_id FROM procedure_result AS presult'
            . ' JOIN procedure_report AS preport ON preport.procedure_report_id = presult.procedure_report_id'
            . ' JOIN procedure_order AS porder ON porder.procedure_order_id = preport.procedure_order_id'
            . ' WHERE presult.document_id = ? AND porder.patient_id = ? AND porder.activity = 1'
            . ' LIMIT 1',
            'procedure_report_id',
            [$documentId, $pid],
        );

        return $reportId === null ? null : (int) $reportId;
    }

    /**
     * Build order → order_code → report for a document we have not extracted before.
     *
     * @return int The new procedure_report_id.
     */
    private function createChain(int $pid, DerivedLabResult $first): int
    {
        // procedure_order carries the patient link — procedure_result has no patient column at all.
        // activity=1 is mandatory: ProcedureService::search filters on it, so activity=0 reads as
        // if the order does not exist.
        $orderId = (int) QueryUtils::sqlInsert(
            'INSERT INTO procedure_order SET patient_id = ?, provider_id = 0, lab_id = 0,'
            . ' activity = 1, date_ordered = NOW(), date_collected = NOW(),'
            . " order_status = 'completed', procedure_order_type = 'laboratory_test'",
            [$pid],
        );

        // REQUIRED, despite holding no result data. ProcedureService::search joins
        // `preport.procedure_order_seq = order_codes.procedure_order_seq` with order_codes LEFT
        // joined — with no row here that predicate compares against NULL, never matches, and every
        // result silently vanishes from FHIR. A clean insert and zero Observations, no error.
        QueryUtils::sqlInsert(
            'INSERT INTO procedure_order_code SET procedure_order_id = ?, procedure_order_seq = ?,'
            . ' procedure_code = ?, procedure_name = ?, procedure_order_title = ?,'
            . ' procedure_source = ?',
            [$orderId, self::ORDER_SEQ, $first->loincCode, $first->label, $first->label, self::SOURCE_EXTERNAL],
        );

        // seq MUST equal the order_code's seq above — that equality is the join predicate.
        // review_status is left at its 'received' default: nothing has reviewed these facts.
        return (int) QueryUtils::sqlInsert(
            'INSERT INTO procedure_report SET procedure_order_id = ?, procedure_order_seq = ?,'
            . ' date_report = NOW(), date_collected = NOW(), report_status = ?',
            [$orderId, self::ORDER_SEQ, self::DERIVED_STATUS],
        );
    }

    /** Has this document already contributed this LOINC code? */
    private function alreadyPersisted(int $documentId, string $loincCode): bool
    {
        return QueryUtils::fetchSingleValue(
            'SELECT procedure_result_id FROM procedure_result WHERE document_id = ? AND result_code = ? LIMIT 1',
            'procedure_result_id',
            [$documentId, $loincCode],
        ) !== null;
    }

    /**
     * @return int The new procedure_result_id.
     */
    private function insertResult(int $reportId, int $documentId, DerivedLabResult $result): int
    {
        // document_id is the native FK back to the source PDF (its column comment: "references
        // documents.id if this result is a document") — the provenance mechanism the architecture
        // wants, without a FHIR Provenance store. uuid is left NULL: ProcedureService's constructor
        // backfills it via UuidRegistry::createMissingUuidsForTables on first read.
        return (int) QueryUtils::sqlInsert(
            'INSERT INTO procedure_result SET procedure_report_id = ?, document_id = ?,'
            . ' result_code = ?, result_text = ?, result = ?, units = ?, `range` = ?,'
            . ' abnormal = ?, result_status = ?, result_data_type = ?, date = NOW()',
            [
                $reportId,
                $documentId,
                $result->loincCode,
                $result->label,
                $result->value,
                $result->units,
                $result->referenceRange,
                $result->abnormal->value,
                self::DERIVED_STATUS,
                is_numeric($result->value) ? 'N' : 'S',
            ],
        );
    }

    private function orderIdForReport(int $reportId): int
    {
        return (int) QueryUtils::fetchSingleValue(
            'SELECT procedure_order_id FROM procedure_report WHERE procedure_report_id = ?',
            'procedure_order_id',
            [$reportId],
        );
    }
}
