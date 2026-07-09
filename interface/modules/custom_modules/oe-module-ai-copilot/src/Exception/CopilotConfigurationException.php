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
 * The module is missing configuration it cannot run without.
 *
 * Messages name the offending environment variable but never echo its value, so they are safe to
 * log. They are still never surfaced to the browser (CLAUDE.md: no `getMessage()` in user output).
 */
final class CopilotConfigurationException extends RuntimeException
{
}
