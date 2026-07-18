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
 * The result of persisting one document's facts across every fact family.
 *
 * `$failed` names the families whose write threw (each in its own transaction, so a failure there
 * left nothing behind and did not discard a family that already committed). The endpoint maps this
 * to an HTTP status: none failed → 200, some failed but something landed → 207, nothing landed → 500.
 *
 * `$preview` carries the chart-vs-document diff a gated family (demographics) returned *instead* of
 * writing, because the request was not accepted. It is not a failure — it is the review card the
 * sidebar renders before the clinician accepts.
 */
final readonly class PersistOutcome
{
    /**
     * @param list<string> $written Identities newly persisted (LOINC codes, allergy/medication keys).
     * @param list<string> $skipped Identities already present for this document (idempotent no-ops).
     * @param list<string> $failed Fact families whose write failed (e.g. 'labs', 'family_history').
     * @param list<array{field: string, chart: string, extracted: string, page: int}> $preview
     *        Gated fields awaiting a clinician accept, with their chart and document values.
     */
    public function __construct(
        public array $written,
        public array $skipped,
        public ?int $procedureOrderId,
        public array $failed,
        public array $preview = [],
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

    /** True when a gated family returned a review diff instead of writing. */
    public function hasPreview(): bool
    {
        return $this->preview !== [];
    }
}
