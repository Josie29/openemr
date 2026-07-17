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
 * The result of persisting one document's facts across both fact families.
 *
 * `$failed` names the families whose write threw (each in its own transaction, so a failure there
 * left nothing behind and did not discard a family that already committed). The endpoint maps this
 * to an HTTP status: none failed → 200, some failed but something landed → 207, nothing landed → 500.
 */
final readonly class PersistOutcome
{
    /**
     * @param list<string> $written Identities newly persisted (LOINC codes, allergy/medication keys).
     * @param list<string> $skipped Identities already present for this document (idempotent no-ops).
     * @param list<string> $failed Fact families whose write failed: 'labs' and/or 'intake'.
     */
    public function __construct(
        public array $written,
        public array $skipped,
        public ?int $procedureOrderId,
        public array $failed,
    ) {
    }

    /** True when a family threw — the request was not fully honoured. */
    public function hasFailures(): bool
    {
        return $this->failed !== [];
    }

    /** True when at least one fact reached the chart (written now, or already present). */
    public function anythingLanded(): bool
    {
        return $this->written !== [] || $this->skipped !== [];
    }
}
