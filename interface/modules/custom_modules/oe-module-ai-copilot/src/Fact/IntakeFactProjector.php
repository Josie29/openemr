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
 * Projects extracted allergies and medications into `lists`. Thin adapter over {@see IntakeFactWriter}.
 *
 * Allergies and medications share one projector because they share one transaction — a medication
 * spans `lists` + `lists_medication`, and the writer commits both families together.
 */
final readonly class IntakeFactProjector implements FactProjector
{
    public function familyName(): string
    {
        return 'intake';
    }

    public function handles(): array
    {
        return [FactType::Allergy, FactType::Medication];
    }

    public function mode(): ProjectionMode
    {
        return ProjectionMode::Auto;
    }

    public function hasWork(ParsedFacts $parsed): bool
    {
        return $parsed->hasIntakeFacts();
    }

    public function write(ParsedFacts $parsed, ProjectionRequest $request): FamilyOutcome
    {
        $outcome = (new IntakeFactWriter($request->sidecar))->write(
            $request->pid,
            $request->documentId,
            $request->contentHash,
            $parsed->allergies,
            $parsed->medications,
            $request->username,
        );

        return new FamilyOutcome($outcome->written, $outcome->skipped);
    }
}
