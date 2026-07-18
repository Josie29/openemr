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
 * What an intake-fact write attempt actually did.
 *
 * `$skipped` is not a failure — it is the idempotency guard reporting that the patient already
 * carries an active record for that fact.
 */
final readonly class IntakeWriteOutcome
{
    /**
     * @param list<string> $written Fact keys newly persisted.
     * @param list<string> $skipped Fact keys the chart already held.
     */
    public function __construct(
        public array $written,
        public array $skipped,
    ) {
    }
}
