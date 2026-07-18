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
 * The kinds of derived fact this module can persist.
 *
 * Backed because the value arrives over the wire from the sidebar. Every kind the agent extracts now
 * has a native OpenEMR destination and a {@see FactProjector} that writes it — labs and the two
 * intake families were always here; family history, chief concern, and demographics were added once
 * their native targets were found (`context/specs/intake-write-back-completion.md`). Demographics
 * still differs: it is the one destructive overwrite, so its projector is accept-gated, not that it
 * is unpersistable.
 */
enum FactType: string
{
    case Lab = 'lab';
    case Allergy = 'allergy';
    case Medication = 'medication';
    case FamilyHistory = 'family_history';
    case ChiefConcern = 'chief_concern';
    case Demographic = 'demographic';
}
