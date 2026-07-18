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
 * Persists one document's derived facts, family by family, through the projector registry.
 *
 * The registry (not this class) knows which families exist and how each writes — adding a fact kind
 * is registering a projector, not editing a chain here. This class owns only the cross-family
 * concerns: per-family isolation (each projector's write is caught on its own, so one family's
 * failure never discards another's committed rows), the accept gate (a gated family with no accept
 * yields a preview instead of writing), and aggregating every family's outcome into one.
 */
final readonly class DerivedFactPersister
{
    public function __construct(
        private ExtractionSidecar $sidecar,
        private ProjectorRegistry $registry = new ProjectorRegistry(),
    ) {
    }

    /**
     * Write every persistable fact, isolating each family's failure from the others.
     *
     * Never throws for a write failure — a failed family is recorded in the outcome's `failed` list
     * and logged, so a partial success is reported truthfully. A gated family (demographics) with
     * `$accept = false` contributes a review diff to the outcome's `preview` and writes nothing.
     *
     * @param string $username The session user authorizing the write, for provenance.
     * @param bool $accept Whether the clinician accepted gated (overwrite) facts this request.
     * @param int|null $authUserId Session author id, needed by families that create encounters.
     * @param int|null $authProviderId Session provider group, needed by families that create encounters.
     * @param int|null $facilityId Session facility, or null to let a family resolve a default.
     */
    public function persist(
        int $pid,
        int $documentId,
        string $contentHash,
        ParsedFacts $parsed,
        string $username,
        bool $accept = false,
        ?int $authUserId = null,
        ?int $authProviderId = null,
        ?int $facilityId = null,
    ): PersistOutcome {
        $request = new ProjectionRequest(
            pid: $pid,
            documentId: $documentId,
            contentHash: $contentHash,
            username: $username,
            accept: $accept,
            sidecar: $this->sidecar,
            authUserId: $authUserId,
            authProviderId: $authProviderId,
            facilityId: $facilityId,
        );

        $written = [];
        $skipped = [];
        $failed = [];
        $preview = [];
        $orderId = null;

        foreach ($this->registry->all() as $projector) {
            if (!$projector->hasWork($parsed)) {
                continue;
            }

            // A destructive, unmarked write (demographics) is never made without an explicit accept —
            // instead the projector yields the chart-vs-document diff the sidebar reviews.
            if ($projector->mode() === ProjectionMode::AcceptGated && !$accept) {
                if ($projector instanceof GatedProjector) {
                    $preview = [...$preview, ...$projector->preview($parsed, $request)];
                }
                continue;
            }

            try {
                $outcome = $projector->write($parsed, $request);
                $written = [...$written, ...$outcome->written];
                $skipped = [...$skipped, ...$outcome->skipped];
                if ($outcome->procedureOrderId !== null) {
                    $orderId = $outcome->procedureOrderId;
                }
            } catch (\Throwable $e) {
                $failed[] = $projector->familyName();
                $this->logFailure($projector->familyName(), $pid, $documentId, $e);
            }
        }

        return new PersistOutcome($written, $skipped, $orderId, $failed, $preview);
    }

    private function logFailure(string $family, int $pid, int $documentId, \Throwable $e): void
    {
        // Log the real exception (it may carry SQL or paths); the endpoint returns only a generic
        // message. Context is enough to find the failed write without the extracted values.
        (new SystemLogger())->error("Co-Pilot {$family} write-back failed", [
            'pid' => $pid,
            'document_id' => $documentId,
            'exception' => $e,
        ]);
    }
}
