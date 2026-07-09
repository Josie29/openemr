<?php

/**
 * AI Clinical Co-Pilot module bootstrap.
 *
 * Included by OpenEMR\Core\ModulesApplication on every page load, but only while the module row
 * has mod_active = 1.
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

use OpenEMR\Core\ModulesClassLoader;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Modules\AiCopilot\Bootstrap;

$globalsBag = OEGlobalsBag::getInstance();

$classLoader = new ModulesClassLoader($globalsBag->getProjectDir());
$classLoader->registerNamespaceIfNotExists(
    'OpenEMR\\Modules\\AiCopilot\\',
    __DIR__ . DIRECTORY_SEPARATOR . 'src'
);

$bootstrap = new Bootstrap($globalsBag->getKernel()->getEventDispatcher(), $globalsBag);
$bootstrap->subscribeToEvents();
