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
 * Projects extracted family history into `history_data`. Thin adapter over {@see FamilyHistoryWriter}.
 */
final readonly class FamilyHistoryProjector implements FactProjector
{
    public function familyName(): string
    {
        return 'family_history';
    }

    public function handles(): array
    {
        return [FactType::FamilyHistory];
    }

    public function mode(): ProjectionMode
    {
        return ProjectionMode::Auto;
    }

    public function hasWork(ParsedFacts $parsed): bool
    {
        return $parsed->familyHistory !== [];
    }

    public function write(ParsedFacts $parsed, ProjectionRequest $request): FamilyOutcome
    {
        return (new FamilyHistoryWriter($request->sidecar))->write(
            $request->pid,
            $request->documentId,
            $request->contentHash,
            $parsed->familyHistory,
            $request->username,
        );
    }
}
