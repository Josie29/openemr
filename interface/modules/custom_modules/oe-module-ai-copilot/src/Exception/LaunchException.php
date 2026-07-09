<?php

/**
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\AiCopilot\Exception;

use RuntimeException;

/**
 * The SMART EHR-launch chain failed and no patient-scoped token could be issued.
 *
 * Every construction site is a fail-closed branch: a bad `state`, a non-2xx token exchange, or a
 * token whose `patient` claim does not match the chart the physician has open. The message is for
 * the log; the browser only ever sees a generic failure.
 */
final class LaunchException extends RuntimeException
{
}
