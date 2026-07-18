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
 * What one projector's write actually did, in the vocabulary the persister aggregates.
 *
 * The pre-existing family writers each have their own outcome type ({@see LabWriteOutcome},
 * {@see IntakeWriteOutcome}); this is the common shape a {@see FactProjector} returns so the persister
 * can fold every family together without knowing which one it is. `$procedureOrderId` is lab-specific
 * (only the lab chain synthesizes an order) and is null for every other family.
 */
final readonly class FamilyOutcome
{
    /**
     * @param list<string> $written Fact identities newly persisted.
     * @param list<string> $skipped Fact identities already present (idempotent no-ops).
     * @param int|null $procedureOrderId The synthesized lab order id, when this family created one.
     */
    public function __construct(
        public array $written,
        public array $skipped,
        public ?int $procedureOrderId = null,
    ) {
    }

    public static function empty(): self
    {
        return new self([], []);
    }
}
