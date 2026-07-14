<?php

/**
 * Register + enable the AI Co-Pilot custom module in a fresh OpenEMR database.
 *
 * A freshly-installed OpenEMR DB (e.g. a new dev worktree) has no `modules`
 * row for this module, so `ModulesApplication` never loads its
 * `openemr.bootstrap.php` and the sidebar cannot mount. This CLI script does
 * exactly what the Module Manager UI does — register the custom module
 * (`modules` + `module_acl_sections` rows, `type=0`) and flip `mod_active=1`
 * — by calling the same `InstModuleTable` methods, so the result is
 * byte-compatible with the UI path. Idempotent: re-registering is a no-op and
 * enabling an already-enabled module just re-sets the flag.
 *
 * Run inside the openemr container from the webroot:
 *   php interface/modules/custom_modules/oe-module-ai-copilot/scripts/register-enable-module.php
 *
 * The module ships no install SQL or ACL (`version.php` has `$v_database = 0`),
 * so register + enable is the whole job — no `installSQL`/ACL step is needed.
 *
 * @package   OpenEMR\Modules\AiCopilot
 * @link      https://www.open-emr.org
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

use Installer\Model\InstModuleTable;
use OpenEMR\Common\Database\QueryUtils;

// CLI bootstrap: no login/session. The webroot is the same absolute path in
// every dev-easy stack (primary or worktree), so this constant is portable.
$ignoreAuth = true;
$_GET['site'] = $_GET['site'] ?? 'default';
require_once '/var/www/localhost/htdocs/openemr/interface/globals.php';

const MODULE_DIR = 'oe-module-ai-copilot';

/**
 * A no-op PSR-ish container. InstModuleTable's constructor requires a
 * ContainerInterface, but register()/updateRegistered() never touch it — it is
 * only used by the module-configuration UI accessors we do not call here.
 */
$container = new class implements \Interop\Container\ContainerInterface {
    public function get($id)
    {
        throw new \RuntimeException("container not used in CLI register/enable: $id");
    }

    public function has($id): bool
    {
        return false;
    }
};

$table = new InstModuleTable($container);

// (a) Register. base defaults to "custom_modules" => type=0, mod_active=0.
//     rel_path mirrors the Module Manager's custom branch ("<dir>/index.php").
//     Returns false (no-op) if a modules row for this directory already exists.
$registerResult = $table->register(MODULE_DIR, MODULE_DIR . '/index.php');
echo $registerResult === false
    ? "register: already present (no-op)\n"
    : "register: inserted (result={$registerResult})\n";

// register() sets mod_id explicitly, so LAST_INSERT_ID/return value are not
// reliable — re-query the id by directory before enabling.
$rows = QueryUtils::fetchRecords(
    "SELECT mod_id, mod_active, type FROM modules WHERE mod_directory = ?",
    [MODULE_DIR]
);
if (empty($rows)) {
    fwrite(STDERR, "FATAL: no modules row after register()\n");
    exit(1);
}
$modId = (int) $rows[0]['mod_id'];
echo "mod_id={$modId} type={$rows[0]['type']} mod_active(before)={$rows[0]['mod_active']}\n";

// (b) Enable. Same call EnableModule() makes: UPDATE modules SET mod_active=1.
$table->updateRegistered($modId, "mod_active=1");

$after = QueryUtils::fetchRecords("SELECT mod_active FROM modules WHERE mod_id = ?", [$modId]);
$enabled = (int) ($after[0]['mod_active'] ?? 0) === 1;
echo "mod_active(after)={$after[0]['mod_active']}\n";
echo $enabled
    ? "OK: module enabled — ModulesApplication will load openemr.bootstrap.php\n"
    : "FAIL: enable did not stick (check module dependencies)\n";

exit($enabled ? 0 : 1);
