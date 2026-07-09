<?php

/**
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\AiCopilot;

use OpenEMR\BC\ServiceContainer;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Events\Core\ScriptFilterEvent;
use OpenEMR\Events\Core\StyleFilterEvent;
use OpenEMR\Events\Main\Tabs\RenderEvent;
use OpenEMR\Modules\AiCopilot\Config\CopilotConfig;
use OpenEMR\Modules\AiCopilot\Controller\CopilotSidebarController;
use Psr\Log\LoggerInterface;
use Symfony\Component\EventDispatcher\EventDispatcherInterface;

/**
 * Wires the co-pilot into the OpenEMR outer application shell (interface/main/tabs/main.php).
 *
 * The panel is a docked right sidebar rendered once into the shell body, so it persists across
 * every patient-chart sub-view without reloading (see context/specs/copilot-sidebar.md). This
 * replaces the earlier Dashboard-card mount, which lived inside the demographics iframe and
 * vanished on every sub-view navigation.
 *
 * Follows the direct-`addListener` pattern used by the other custom modules rather than
 * `EventSubscriberInterface`, which OpenEMR's module loader does not consult.
 */
final class Bootstrap
{
    public const MODULE_DIRECTORY = 'oe-module-ai-copilot';

    private const MODULE_INSTALLATION_PATH = '/interface/modules/custom_modules/' . self::MODULE_DIRECTORY;

    /**
     * The script name of the outer application shell. Both filter events surface it: the script
     * event as a bare basename, the style event as the full path — so match a suffix, not equality.
     */
    private const SHELL_PAGE = 'main.php';

    private readonly LoggerInterface $logger;

    public function __construct(
        private readonly EventDispatcherInterface $eventDispatcher,
        private readonly OEGlobalsBag $globalsBag,
    ) {
        $this->logger = ServiceContainer::getLogger();
    }

    public function subscribeToEvents(): void
    {
        // Render the sidebar into the shell body — content here survives sub-view navigation.
        $this->eventDispatcher->addListener(
            RenderEvent::EVENT_BODY_RENDER_POST,
            $this->renderSidebar(...)
        );
        $this->eventDispatcher->addListener(ScriptFilterEvent::EVENT_NAME, $this->addSidebarScript(...));
        $this->eventDispatcher->addListener(StyleFilterEvent::EVENT_NAME, $this->addSidebarStylesheet(...));
    }

    /**
     * Render the sidebar shell (container + config island) at the end of the shell <body>.
     *
     * The markup is patient-agnostic: it carries no pid. The active patient is resolved
     * client-side from the shell's Knockout observable, and the panel only reveals itself once a
     * patient chart is open. This keeps a single sidebar instance that re-scopes as the physician
     * switches patients, rather than one bound to whichever chart happened to render it.
     */
    public function renderSidebar(RenderEvent $event): void
    {
        // Without the FHIR API there is no patient-scoped data for the copilot to read, so mounting
        // it could only ever fail — stay out of the shell entirely.
        if (!$this->globalsBag->getBoolean('rest_fhir_api')) {
            return;
        }

        // Stay silent rather than mounting a panel whose first interaction is guaranteed to fail.
        if (!CopilotConfig::isConfigured()) {
            $this->logger->info('AiCopilot: sidebar suppressed, module is not configured', [
                'module' => self::MODULE_DIRECTORY,
            ]);
            return;
        }

        try {
            $controller = new CopilotSidebarController(
                CopilotConfig::fromEnvironment(),
                $this->globalsBag->getWebRoot() . self::MODULE_INSTALLATION_PATH
            );
            echo $controller->renderSidebar();
        } catch (\Exception $exception) {
            // A broken sidebar must never take the whole EHR shell down with it. Log and render
            // nothing. A raw \Error (a programming bug) is left to propagate per ForbiddenCatchType.
            $this->logger->error('AiCopilot: failed to render sidebar', ['exception' => $exception]);
        }
    }

    /**
     * Enqueue the sidebar JS onto the outer shell.
     *
     * ScriptFilterEvent hands the paths through ModulesApplication::filterSafeLocalModuleFiles(),
     * which drops anything resolving outside interface/modules — so this must stay a local path.
     */
    public function addSidebarScript(ScriptFilterEvent $event): ScriptFilterEvent
    {
        if (!$this->isShellPage($event->getPageName())) {
            return $event;
        }
        $scripts = $event->getScripts();
        $scripts[] = $this->assetPath('js/ai-copilot.js');
        $event->setScripts($scripts);
        return $event;
    }

    public function addSidebarStylesheet(StyleFilterEvent $event): StyleFilterEvent
    {
        if (!$this->isShellPage($event->getPageName())) {
            return $event;
        }
        $styles = $event->getStyles();
        $styles[] = $this->assetPath('css/ai-copilot.css');
        $event->setStyles($styles);
        return $event;
    }

    /**
     * Whether the given page name refers to the outer shell.
     *
     * The script filter passes a basename (`main.php`) and the style filter the full script path
     * (`/interface/main/tabs/main.php`), so a suffix match handles both without a false positive on
     * unrelated pages (no other rendered page is named main.php in this flow).
     */
    private function isShellPage(string $pageName): bool
    {
        return str_ends_with($pageName, self::SHELL_PAGE);
    }

    /**
     * Build the webroot-relative URL for a bundled asset.
     *
     * Deliberately appends no cache-buster: the shell runs every module-enqueued script/style
     * through Header::createElement(), which already appends `?v={v_js_includes}` (src/Core/Header.php).
     * That token is per-request in dev (OPENEMR__ENVIRONMENT=dev) and the release version in prod, so
     * bump $v_js_includes in version.php when shipping JS/CSS changes. Adding our own query here would
     * only produce a doubled `?v=...&v=...`.
     *
     * @param string $relativePath Asset path relative to public/assets/ (e.g. "js/ai-copilot.js").
     * @return string The webroot-relative URL.
     */
    private function assetPath(string $relativePath): string
    {
        return $this->globalsBag->getWebRoot() . self::MODULE_INSTALLATION_PATH . '/public/assets/' . $relativePath;
    }
}
