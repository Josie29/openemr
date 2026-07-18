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
 * The fixed set of projectors, one per fact family, in a stable write order.
 *
 * This is the single place a new fact kind is registered — it replaced the family-by-family `if`
 * chain the persister, the parser dispatch, and the endpoint used to share. The persister iterates
 * {@see all()} and asks each projector whether it has work, so it never names a family itself.
 *
 * Order matters only for readability of the aggregated outcome (the sidebar lists what landed);
 * families are otherwise independent and isolated.
 */
final readonly class ProjectorRegistry
{
    /** @var list<FactProjector> */
    private array $projectors;

    public function __construct()
    {
        $this->projectors = [
            new LabProjector(),
            new IntakeFactProjector(),
            new FamilyHistoryProjector(),
            new ChiefConcernProjector(),
            new DemographicProjector(),
        ];
    }

    /**
     * @return list<FactProjector>
     */
    public function all(): array
    {
        return $this->projectors;
    }
}
