<?php

/**
 * Version descriptor for the AI Clinical Co-Pilot module.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// Consumed by the Module Manager (InstallerController::getModuleVersionFromFile). $v_database is
// the install/upgrade revision of sql/table.sql (the extraction sidecar); bump it whenever that
// schema changes so the Module Manager re-runs the script.
$v_major = 0;
$v_minor = 2;
$v_patch = 0;
$v_database = 1;
$v_tag = '';
