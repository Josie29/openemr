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
 * Whether a fact family may be written on arrival, or only after a clinician accepts it.
 *
 * The axis is not "writable vs not" — every extracted fact now has a native target. It is whether the
 * write can be made *honestly* without a human in the loop:
 *
 * - {@see self::Auto} — the destination carries a native "not clinician-confirmed" marker (labs
 *   `preliminary`, allergies `unconfirmed`, medications `proposal`) or the value itself is annotated
 *   as Co-Pilot-derived (family history, chief concern). A machine reader can tell it apart from
 *   clinician-entered data, so it is safe to persist as soon as it is extracted.
 * - {@see self::AcceptGated} — the destination has no marker and the write is a **destructive
 *   in-place overwrite** of clinician-entered chart data (demographics → `patient_data`). The only
 *   honest way to write it is for a clinician to review a chart-vs-document diff and accept it, which
 *   makes them the author. Absent that accept, the projector yields a preview instead of writing.
 */
enum ProjectionMode
{
    case Auto;
    case AcceptGated;
}
