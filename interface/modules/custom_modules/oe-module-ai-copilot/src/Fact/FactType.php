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
 * Backed because the value arrives over the wire from the sidebar. Deliberately short: demographics,
 * chief concern, and family history are extracted by the agent but have no honest write target in
 * this fork, so they are not persistable and get no case here.
 */
enum FactType: string
{
    case Lab = 'lab';
    case Allergy = 'allergy';
    case Medication = 'medication';
}
