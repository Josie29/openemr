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
 * What a write-back attempt actually did.
 *
 * `$skipped` is not a failure — it is the idempotency guard reporting that these results were
 * already persisted for this document (`W2_ARCHITECTURE.md` §6, store-once).
 */
final readonly class LabWriteOutcome
{
    /**
     * @param list<string> $written LOINC codes newly persisted.
     * @param list<string> $skipped LOINC codes already present for this document.
     */
    public function __construct(
        public int $procedureOrderId,
        public array $written,
        public array $skipped,
    ) {
    }
}
