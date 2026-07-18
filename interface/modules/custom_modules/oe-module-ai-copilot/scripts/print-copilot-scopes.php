<?php

/**
 * Print the canonical SMART scope string — `CopilotScopes::asString()` — to stdout.
 *
 * The registered `oauth_clients.scope` row (not this code) is what a launched token is granted, so
 * tooling that keeps the two in sync needs the code's value as plain text. Emitting it from PHP,
 * rather than re-parsing the const array in shell, keeps `CopilotScopes` the single source of truth
 * and avoids escaping the namespace across shell layers. Consumed by `sync-copilot-scopes.sh` and
 * `bootstrap-worktree-copilot.sh`.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

require __DIR__ . '/../src/Smart/CopilotScopes.php';

echo \OpenEMR\Modules\AiCopilot\Smart\CopilotScopes::asString();
