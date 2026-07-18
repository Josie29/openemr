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
 * Projects extracted demographics into `patient_data` — accept-gated. Adapter over {@see DemographicWriter}.
 *
 * The only {@see GatedProjector}: the write is a destructive overwrite with no marker, so absent an
 * accept the persister asks for {@see preview()} (a chart-vs-document diff) instead of writing.
 */
final readonly class DemographicProjector implements GatedProjector
{
    public function familyName(): string
    {
        return 'demographics';
    }

    public function handles(): array
    {
        return [FactType::Demographic];
    }

    public function mode(): ProjectionMode
    {
        return ProjectionMode::AcceptGated;
    }

    public function hasWork(ParsedFacts $parsed): bool
    {
        return $parsed->demographics !== [];
    }

    public function preview(ParsedFacts $parsed, ProjectionRequest $request): array
    {
        return (new DemographicWriter($request->sidecar))->preview($request->pid, $parsed->demographics);
    }

    public function write(ParsedFacts $parsed, ProjectionRequest $request): FamilyOutcome
    {
        return (new DemographicWriter($request->sidecar))->write(
            $request->pid,
            $request->documentId,
            $request->contentHash,
            $parsed->demographics,
            $request->username,
        );
    }
}
