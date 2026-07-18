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
 * Projects extracted lab results down the native lab chain. Thin adapter over {@see LabResultWriter}.
 */
final readonly class LabProjector implements FactProjector
{
    public function familyName(): string
    {
        return 'labs';
    }

    public function handles(): array
    {
        return [FactType::Lab];
    }

    public function mode(): ProjectionMode
    {
        return ProjectionMode::Auto;
    }

    public function hasWork(ParsedFacts $parsed): bool
    {
        return $parsed->hasLabs();
    }

    public function write(ParsedFacts $parsed, ProjectionRequest $request): FamilyOutcome
    {
        $outcome = (new LabResultWriter($request->sidecar))->write(
            $request->pid,
            $request->documentId,
            $request->contentHash,
            $parsed->labs,
            $request->username,
        );

        return new FamilyOutcome($outcome->written, $outcome->skipped, $outcome->procedureOrderId);
    }
}
