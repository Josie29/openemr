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
 * The abnormal-flag values `procedure_result.abnormal` accepts.
 *
 * Backed because the value is persisted. The cases mirror the column's own comment in
 * `sql/database.sql` ('no,yes,high,low') and the agent's `AbnormalFlag` Python enum, which is why
 * a derived lab result maps across without translation.
 */
enum AbnormalFlag: string
{
    case No = 'no';
    case Yes = 'yes';
    case High = 'high';
    case Low = 'low';
}
