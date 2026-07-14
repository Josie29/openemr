<?php

/**
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\AiCopilot\Controller;

use OpenEMR\Common\Csrf\CsrfUtils;
use OpenEMR\Common\Session\SessionWrapperFactory;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\FHIR\Config\ServerConfig;
use OpenEMR\Modules\AiCopilot\Config\CopilotConfig;
use OpenEMR\Modules\AiCopilot\Smart\TokenRelayView;
use OpenEMR\Modules\AiCopilot\Support\ModuleUrls;

/**
 * Renders the docked co-pilot sidebar into the OpenEMR outer shell.
 *
 * Emits a patient-agnostic shell plus a JSON configuration island; all behaviour lives in the
 * enqueued `ai-copilot.js`. The markup carries **no patient id** — the active patient is resolved
 * client-side from the shell's Knockout observable, and every agent turn is scoped by the SMART
 * token's own `patient` claim, so the browser can never ask about a patient the token does not
 * permit. The sidebar starts hidden and is revealed by the banner toggle only when a chart is open.
 */
final readonly class CopilotSidebarController
{
    /**
     * Static starter prompts shown in the empty state. Clicking one *populates the input* for the
     * physician to review and send — it does not auto-send (spec §6). Chart-aware and
     * conversation-tailored prompts are a later increment.
     *
     * @var list<string>
     */
    private const STARTER_PROMPTS = [
        'Summarize this patient',
        "What's overdue?",
        'Any medication or allergy conflicts?',
        'Summarize recent visits',
    ];

    /**
     * @param CopilotConfig $config Module configuration (agent URL, OAuth client).
     * @param string $moduleWebPath Web-root-relative base path of the module, e.g.
     *     `/interface/modules/custom_modules/oe-module-ai-copilot`. Used to build the same-origin
     *     conversation-persistence endpoint URL.
     */
    public function __construct(
        private CopilotConfig $config,
        private string $moduleWebPath,
    ) {
    }

    /**
     * @return string The sidebar markup, ready to echo into the shell <body>.
     */
    public function renderSidebar(): string
    {
        $globalsBag = OEGlobalsBag::getInstance();
        $session = SessionWrapperFactory::getInstance()->getActiveSession();
        $urls = ModuleUrls::create((new ServerConfig())->getOauthAddress(), $globalsBag->getWebRoot());

        $sidebarConfig = [
            'launchUrl' => $urls->launchUrl(),
            'chatUrl' => $this->config->chatUrl(),
            // Same-origin (the OpenEMR host), so a web-root-relative path is enough.
            'conversationUrl' => $this->moduleWebPath . '/public/conversation.php',
            'csrfToken' => CsrfUtils::collectCsrfToken(session: $session),
            'expectedOrigin' => $urls->origin,
            'messageSource' => TokenRelayView::MESSAGE_SOURCE,
            // JOS-57 click-to-source: same-origin FHIR base for Binary (source-document) reads,
            // and the locally-vendored pdf.js worker URL for the preview overlay.
            'fhirBaseUrl' => (new ServerConfig())->getFhirUrl(),
            'pdfWorkerUrl' => $this->moduleWebPath . '/public/assets/vendor/pdfjs/pdf.worker.min.js',
        ];

        // JSON_HEX_TAG closes off the `</script>`-in-a-string escape; the payload is inert data read
        // back with JSON.parse, so no value here is ever evaluated as markup or code.
        $configJson = json_encode(
            $sidebarConfig,
            JSON_THROW_ON_ERROR | JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT
        );

        $title = xlt('Clinical Co-Pilot');
        $toggleLabel = xlt('Co-Pilot');
        $placeholder = xla('Ask about this patient...');
        $sendLabel = xlt('Ask');
        $clearLabel = xlt('Clear');
        $closeLabel = xla('Close Co-Pilot');
        $resizeLabel = xla('Resize Co-Pilot');
        $introLabel = xlt('Ask a question to orient on this chart. Every answer cites the record it came from.');

        // Localized strings the JS needs live on data attributes rather than in the static JS bundle,
        // which cannot be run through xl().
        $dataLabels = implode(' ', [
            'data-label-toggle="' . xla('Co-Pilot') . '"',
            'data-label-auth-failed="' . xla('Could not authorize against the record. Try again.') . '"',
            'data-label-unavailable="' . xla('The co-pilot could not answer that. Please try again.') . '"',
            'data-label-clear-confirm="' . xla('Clear this conversation? This cannot be undone.') . '"',
            // Caption under the animated indicator while a turn is in flight (spec §5.3.1).
            'data-label-thinking="' . xla('Checking the record...') . '"',
            'data-label-has-conversation="' . xla('You have a saved conversation for this patient') . '"',
            // Heading over the agent-proposed follow-up chips shown under each answer.
            'data-label-follow-ups="' . xla('Ask next') . '"',
        ]);

        $chips = $this->renderStarterChips();

        // The Co-Pilot spark, matching the banner toggle and the JS-built answer avatars. Static
        // markup (no user data), so it is safe to inline into the heredoc.
        $spark = '<svg viewBox="0 0 24 24" fill="currentColor" aria-hidden="true">'
            . '<path d="M12 2c.5 4 1 6.5 10 10-9 3.5-9.5 6-10 10-.5-4-1-6.5-10-10 9-3.5 9.5-6 10-10z"/></svg>';

        return <<<HTML
            <aside
                class="ai-copilot"
                id="ai-copilot-sidebar"
                {$dataLabels}
                hidden
                aria-hidden="true"
                aria-label="{$title}"
            >
                <script type="application/json" id="ai-copilot-config">{$configJson}</script>
                <div
                    class="ai-copilot__resizer"
                    id="ai-copilot-resizer"
                    role="separator"
                    aria-orientation="vertical"
                    aria-label="{$resizeLabel}"
                    tabindex="0"
                ></div>
                <header class="ai-copilot__header">
                    <div class="ai-copilot__heading">
                        <h2 class="ai-copilot__title">{$title}</h2>
                        <p class="ai-copilot__patient" id="ai-copilot-patient"></p>
                    </div>
                    <div class="ai-copilot__actions">
                        <button
                            class="ai-copilot__clear"
                            id="ai-copilot-clear"
                            type="button"
                            hidden
                        >{$clearLabel}</button>
                        <button
                            class="ai-copilot__close"
                            id="ai-copilot-close"
                            type="button"
                            aria-label="{$closeLabel}"
                        >&times;</button>
                    </div>
                    <p class="ai-copilot__status" id="ai-copilot-status" role="status" aria-live="polite"></p>
                </header>
                <div class="ai-copilot__transcript" id="ai-copilot-transcript">
                    <div class="ai-copilot__empty" id="ai-copilot-empty">
                        <div class="ai-copilot__turn">
                            <span class="ai-copilot__avatar" aria-hidden="true">{$spark}</span>
                            <div class="ai-copilot__answer ai-copilot__welcome">
                                <p class="ai-copilot__intro">{$introLabel}</p>
                                <ul class="ai-copilot__chips" id="ai-copilot-chips">
                                    {$chips}
                                </ul>
                            </div>
                        </div>
                    </div>
                </div>
                <form class="ai-copilot__composer" id="ai-copilot-form">
                    <label class="sr-only" for="ai-copilot-input">{$placeholder}</label>
                    <textarea
                        class="ai-copilot__input form-control"
                        id="ai-copilot-input"
                        rows="1"
                        autocomplete="off"
                        placeholder="{$placeholder}"
                    ></textarea>
                    <button class="ai-copilot__send btn btn-primary" id="ai-copilot-send" type="submit">
                        {$sendLabel}
                    </button>
                </form>
            </aside>
            <button
                class="ai-copilot-toggle"
                id="ai-copilot-toggle"
                type="button"
                aria-expanded="false"
                aria-controls="ai-copilot-sidebar"
                hidden
            >
                <span class="ai-copilot-toggle__icon" aria-hidden="true"><svg viewBox="0 0 24 24" width="14" height="14" fill="currentColor"><path d="M12 2c.5 4 1 6.5 10 10-9 3.5-9.5 6-10 10-.5-4-1-6.5-10-10 9-3.5 9.5-6 10-10z"/></svg></span>
                <span class="ai-copilot-toggle__label">{$toggleLabel}</span>
                <span class="ai-copilot-toggle__chevron" aria-hidden="true"><svg viewBox="0 0 24 24" width="10" height="10" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="9 6 15 12 9 18"></polyline></svg></span>
                <span class="ai-copilot-toggle__hint" id="ai-copilot-hint" hidden aria-hidden="true"></span>
            </button>
            HTML;
    }

    /**
     * Render the static starter-prompt chips. Each carries its full prompt in a data attribute; the
     * JS copies that into the input on click rather than sending it.
     *
     * @return string The `<li><button>` chip markup.
     */
    private function renderStarterChips(): string
    {
        $chips = '';
        foreach (self::STARTER_PROMPTS as $prompt) {
            $label = xlt($prompt);
            $promptAttr = attr($prompt);
            $chips .= <<<HTML
                <li>
                    <button class="ai-copilot__chip" type="button" data-prompt="{$promptAttr}">{$label}</button>
                </li>
                HTML;
        }
        return $chips;
    }
}
