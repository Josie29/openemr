<?php

/**
 * Reset a patient back to baseline by deleting every fact the AI Clinical Co-Pilot wrote
 * back to the chart — leaving the Synthea/demo baseline (and anything a clinician entered)
 * completely untouched.
 *
 * WHY THIS EXISTS
 * The write-back path (persist-facts.php -> DerivedFactPersister) is idempotent: once a med,
 * allergy, or lab is on the chart, re-running the same extraction dedups against it and writes
 * nothing. That makes it impossible to re-test the write-back + dashboard-refresh flow without
 * first removing what was already written. This script is that "undo", so a document can be
 * re-synthesized from a clean baseline as many times as needed.
 *
 * WHAT COUNTS AS "DERIVED" (the delete predicates)
 * Every derived row carries a marker that baseline rows never do — so the delete is scoped by
 * the marker, NOT by the rebuildable sidecar cache (a cache wipe must not strand chart rows):
 *   - medications  lists.type='medication' + lists_medication.request_intent='proposal'
 *                  (IntakeFactWriter::DERIVED_INTENT; baseline meds leave request_intent NULL)
 *   - allergies    lists.type='allergy'    + lists.verification='unconfirmed'
 *                  (IntakeFactWriter::DERIVED_VERIFICATION; baseline allergies leave it '')
 *   - labs         procedure_result.result_status='preliminary' under one of the patient's orders
 *                  (LabResultWriter::DERIVED_STATUS; baseline labs are 'final'). The whole
 *                  order -> order_code -> report -> result chain is created per source document
 *                  and is exclusively derived, so the emptied order/report/code rows are removed too.
 * The citation-geometry sidecar (ai_copilot_document_facts) is a rebuildable cache with no
 * clinical value once the rows it points at are gone, so its rows for this patient are cleared last.
 *
 * SAFETY
 * All deletes run in one transaction (rolled back on any error), and every parent-row delete
 * (report/order_code/order) is guarded by NOT EXISTS so a row is only removed once it has no
 * remaining children — a derived chain can never take a baseline row with it. --dry-run reports
 * the counts it *would* delete and changes nothing.
 *
 * RUN (in the openemr container, as the web user — never root):
 *   openemr-cmd e 'php interface/modules/custom_modules/oe-module-ai-copilot/scripts/reset-patient-facts.php <pid> [--dry-run]'
 * or against a specific worktree/stack:
 *   openemr-cmd worktree exec <branch> e 'php .../scripts/reset-patient-facts.php <pid> [--dry-run]'
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// --- Minimal CLI bootstrap of the OpenEMR runtime (site + ignore interactive auth) ---
$_GET['site'] = $_GET['site'] ?? 'default';
$_SERVER['HTTP_HOST'] = $_SERVER['HTTP_HOST'] ?? 'localhost';
$_SERVER['REQUEST_URI'] = $_SERVER['REQUEST_URI'] ?? '/';
$_SERVER['SERVER_NAME'] = $_SERVER['SERVER_NAME'] ?? 'localhost';
$ignoreAuth = true;
$sessionAllowWrite = true;

$fileroot = dirname(__DIR__, 5);
require $fileroot . '/interface/globals.php';

use OpenEMR\Common\Database\QueryUtils;

/**
 * Parse and validate the CLI arguments.
 *
 * @param list<string> $argv The raw process arguments ($argv).
 * @return array{pid: int, dryRun: bool}
 * @throws InvalidArgumentException When no positive integer pid is supplied.
 */
function parseArgs(array $argv): array
{
    $dryRun = in_array('--dry-run', $argv, true);
    $positionals = array_values(array_filter(
        array_slice($argv, 1),
        static fn(string $a): bool => !str_starts_with($a, '--'),
    ));

    $pid = isset($positionals[0]) ? filter_var($positionals[0], FILTER_VALIDATE_INT) : false;
    if ($pid === false || $pid <= 0) {
        throw new InvalidArgumentException(
            'Usage: reset-patient-facts.php <pid> [--dry-run]  (pid must be a positive integer)',
        );
    }

    return ['pid' => $pid, 'dryRun' => $dryRun];
}

/**
 * The procedure_result ids of the patient's derived (preliminary) lab results.
 *
 * @return list<int>
 */
function derivedLabResultIds(int $pid): array
{
    $rows = QueryUtils::fetchRecords(
        'SELECT presult.procedure_result_id AS id FROM procedure_result AS presult'
        . ' JOIN procedure_report AS preport ON preport.procedure_report_id = presult.procedure_report_id'
        . ' JOIN procedure_order AS porder ON porder.procedure_order_id = preport.procedure_order_id'
        . " WHERE porder.patient_id = ? AND presult.result_status = 'preliminary'",
        [$pid],
    );

    return array_map(static fn(array $row): int => (int) $row['id'], $rows);
}

/**
 * Build a positional-placeholder fragment (`?, ?, ?`) for an IN clause.
 *
 * @param list<int> $ids
 */
function inPlaceholders(array $ids): string
{
    return implode(', ', array_fill(0, count($ids), '?'));
}

$args = parseArgs($argv);
$pid = $args['pid'];
$dryRun = $args['dryRun'];

// --- Count what is derived, up front, so the report reads the same in dry-run and real mode. ---
// Capture the exact derived medication lists.id set (join on the 'proposal' marker) rather than a
// bare count: the delete below removes precisely these ids. A baseline med carries no
// lists_medication row at all, so it can never appear here — and can never be swept up.
$medListIds = array_map(
    static fn(array $r): int => (int) $r['id'],
    QueryUtils::fetchRecords(
        'SELECT lists.id AS id FROM lists'
        . ' JOIN lists_medication ON lists_medication.list_id = lists.id'
        . " WHERE lists.pid = ? AND lists.type = 'medication' AND lists_medication.request_intent = 'proposal'",
        [$pid],
    ),
);
$medCount = count($medListIds);
$allergyCount = (int) QueryUtils::fetchSingleValue(
    "SELECT COUNT(*) AS n FROM lists WHERE pid = ? AND type = 'allergy' AND verification = 'unconfirmed'",
    'n',
    [$pid],
);
$labResultIds = derivedLabResultIds($pid);
$sidecarCount = (int) QueryUtils::fetchSingleValue(
    'SELECT COUNT(*) AS n FROM ai_copilot_document_facts WHERE pid = ?',
    'n',
    [$pid],
);

echo "Derived facts for patient {$pid}:" . PHP_EOL;
echo "  medications (proposal):     {$medCount}" . PHP_EOL;
echo "  allergies (unconfirmed):    {$allergyCount}" . PHP_EOL;
echo '  lab results (preliminary):  ' . count($labResultIds) . PHP_EOL;
echo "  sidecar geometry rows:      {$sidecarCount}" . PHP_EOL;

if ($dryRun) {
    echo PHP_EOL . 'Dry run — nothing deleted.' . PHP_EOL;
    exit(0);
}

if ($medCount === 0 && $allergyCount === 0 && $labResultIds === [] && $sidecarCount === 0) {
    echo PHP_EOL . 'Already at baseline — nothing to delete.' . PHP_EOL;
    exit(0);
}

sqlBeginTrans();
try {
    // Medications: delete exactly the derived lists.id set captured above — child (FK) first, then
    // the parent. Deleting by explicit id (never "parent with no child") is what keeps baseline meds
    // safe: those have no lists_medication row, so they were never in $medListIds to begin with.
    if ($medListIds !== []) {
        $min = inPlaceholders($medListIds);
        QueryUtils::sqlStatementThrowException(
            "DELETE FROM lists_medication WHERE list_id IN ({$min})",
            $medListIds,
        );
        QueryUtils::sqlStatementThrowException(
            "DELETE FROM lists WHERE id IN ({$min})",
            $medListIds,
        );
    }

    // Allergies: single table, matched by the 'unconfirmed' verification marker.
    QueryUtils::sqlStatementThrowException(
        "DELETE FROM lists WHERE pid = ? AND type = 'allergy' AND verification = 'unconfirmed'",
        [$pid],
    );

    // Labs: delete the preliminary results, then the now-childless report/order_code/order rows.
    // Each parent delete is guarded by NOT EXISTS so it fires only once its children are gone —
    // a baseline order can never be swept up because none of its results are 'preliminary'.
    if ($labResultIds !== []) {
        $in = inPlaceholders($labResultIds);
        $reportIds = array_map(
            static fn(array $r): int => (int) $r['id'],
            QueryUtils::fetchRecords(
                "SELECT DISTINCT procedure_report_id AS id FROM procedure_result WHERE procedure_result_id IN ({$in})",
                $labResultIds,
            ),
        );

        QueryUtils::sqlStatementThrowException(
            "DELETE FROM procedure_result WHERE procedure_result_id IN ({$in})",
            $labResultIds,
        );

        if ($reportIds !== []) {
            $rin = inPlaceholders($reportIds);
            $orderIds = array_map(
                static fn(array $r): int => (int) $r['id'],
                QueryUtils::fetchRecords(
                    "SELECT DISTINCT procedure_order_id AS id FROM procedure_report WHERE procedure_report_id IN ({$rin})",
                    $reportIds,
                ),
            );

            QueryUtils::sqlStatementThrowException(
                "DELETE FROM procedure_report WHERE procedure_report_id IN ({$rin})"
                . ' AND NOT EXISTS (SELECT 1 FROM procedure_result pr'
                . ' WHERE pr.procedure_report_id = procedure_report.procedure_report_id)',
                $reportIds,
            );

            if ($orderIds !== []) {
                $oin = inPlaceholders($orderIds);
                QueryUtils::sqlStatementThrowException(
                    "DELETE FROM procedure_order_code WHERE procedure_order_id IN ({$oin})"
                    . ' AND NOT EXISTS (SELECT 1 FROM procedure_report prep'
                    . ' WHERE prep.procedure_order_id = procedure_order_code.procedure_order_id)',
                    $orderIds,
                );
                QueryUtils::sqlStatementThrowException(
                    "DELETE FROM procedure_order WHERE procedure_order_id IN ({$oin})"
                    . ' AND NOT EXISTS (SELECT 1 FROM procedure_report prep'
                    . ' WHERE prep.procedure_order_id = procedure_order.procedure_order_id)',
                    $orderIds,
                );
            }
        }
    }

    // Sidecar geometry cache last: it only points at rows we just deleted.
    QueryUtils::sqlStatementThrowException(
        'DELETE FROM ai_copilot_document_facts WHERE pid = ?',
        [$pid],
    );

    sqlCommitTrans();
} catch (\Throwable $e) {
    sqlRollbackTrans();
    // Surface the failed operation; the original DB error is chained via getPrevious().
    throw new RuntimeException("Failed to reset derived facts for patient {$pid}; rolled back.", previous: $e);
}

echo PHP_EOL . "Reset complete — patient {$pid} is back to baseline." . PHP_EOL;
