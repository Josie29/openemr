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
 * A projector whose write is destructive and therefore gated behind a clinician accept.
 *
 * When the request has not been accepted, the persister asks a gated projector for a {@see preview()}
 * instead of writing — a chart-vs-document diff the sidebar renders as a per-field review card. The
 * clinician then re-posts with `accept: true` and the persister calls the ordinary {@see write()}.
 * The preview is read-only: it must not mutate the chart.
 */
interface GatedProjector extends FactProjector
{
    /**
     * The chart-vs-document diff for this family's gated facts.
     *
     * @return list<array{field: string, chart: string, extracted: string, page: int}>
     *         One entry per extracted field: its current chart value and the value read off the
     *         document, so the clinician can decide per field.
     */
    public function preview(ParsedFacts $parsed, ProjectionRequest $request): array;
}
