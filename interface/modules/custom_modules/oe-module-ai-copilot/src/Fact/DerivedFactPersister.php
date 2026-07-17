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

use OpenEMR\Common\Logging\SystemLogger;

/**
 * Persists one document's derived facts across both fact families, family by family.
 *
 * Each family (labs down the procedure chain, intake facts to `lists`) writes in its own
 * transaction and is caught on its own. That isolation is the point: a failure persisting
 * medications must not discard labs that already committed, and the caller must be able to report
 * exactly what reached the chart rather than a blanket failure that hides a partial success.
 */
final readonly class DerivedFactPersister
{
    public function __construct(private ExtractionSidecar $sidecar)
    {
    }

    /**
     * Write every persistable fact, isolating each family's failure from the other.
     *
     * Never throws for a write failure — a failed family is recorded in the outcome's `failed` list
     * and logged, so a partial success is reported truthfully. (A programming error inside a writer
     * would still surface as a `\Throwable`, but the writers convert write failures to
     * `\RuntimeException`, which is caught here.)
     *
     * @param string $username The session user authorizing the write, for provenance.
     */
    public function persist(
        int $pid,
        int $documentId,
        string $contentHash,
        ParsedFacts $parsed,
        string $username,
    ): PersistOutcome {
        $written = [];
        $skipped = [];
        $failed = [];
        $orderId = null;

        if ($parsed->hasLabs()) {
            try {
                $outcome = (new LabResultWriter($this->sidecar))
                    ->write($pid, $documentId, $contentHash, $parsed->labs, $username);
                $written = [...$written, ...$outcome->written];
                $skipped = [...$skipped, ...$outcome->skipped];
                $orderId = $outcome->procedureOrderId;
            } catch (\Throwable $e) {
                $failed[] = 'labs';
                $this->logFailure('lab', $pid, $documentId, ['lab_count' => count($parsed->labs)], $e);
            }
        }

        if ($parsed->hasIntakeFacts()) {
            try {
                $outcome = (new IntakeFactWriter($this->sidecar))->write(
                    $pid,
                    $documentId,
                    $contentHash,
                    $parsed->allergies,
                    $parsed->medications,
                    $username,
                );
                $written = [...$written, ...$outcome->written];
                $skipped = [...$skipped, ...$outcome->skipped];
            } catch (\Throwable $e) {
                $failed[] = 'intake';
                $this->logFailure('intake', $pid, $documentId, [
                    'allergy_count' => count($parsed->allergies),
                    'medication_count' => count($parsed->medications),
                ], $e);
            }
        }

        return new PersistOutcome($written, $skipped, $orderId, $failed);
    }

    /**
     * @param array<string, int> $counts Family-specific fact counts for the log context.
     */
    private function logFailure(string $family, int $pid, int $documentId, array $counts, \Throwable $e): void
    {
        // Log the real exception (it may carry SQL or paths); the endpoint returns only a generic
        // message. Context is enough to find the failed write without the extracted values.
        (new SystemLogger())->error("Co-Pilot {$family} write-back failed", [
            'pid' => $pid,
            'document_id' => $documentId,
            ...$counts,
            'exception' => $e,
        ]);
    }
}
