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
 * Projects an extracted chief concern into a new encounter. Thin adapter over {@see ChiefConcernWriter}.
 */
final readonly class ChiefConcernProjector implements FactProjector
{
    public function familyName(): string
    {
        return 'chief_concern';
    }

    public function handles(): array
    {
        return [FactType::ChiefConcern];
    }

    public function mode(): ProjectionMode
    {
        return ProjectionMode::Auto;
    }

    public function hasWork(ParsedFacts $parsed): bool
    {
        return $parsed->chiefConcerns !== [];
    }

    public function write(ParsedFacts $parsed, ProjectionRequest $request): FamilyOutcome
    {
        return (new ChiefConcernWriter($request->sidecar))->write(
            $request->pid,
            $request->documentId,
            $request->contentHash,
            $parsed->chiefConcerns,
            $request,
            $request->username,
        );
    }
}
