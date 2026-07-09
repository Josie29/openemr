<?php

/**
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\AiCopilot\Smart;

/**
 * Renders the terminal page of the launch chain: a document, loaded inside the hidden iframe, whose
 * only job is to hand the token up to the chart page and then go quiet.
 *
 * The whole point of running the redirect dance in a hidden frame is that the physician's chart
 * never navigates. `postMessage` is the one channel that crosses a browsing-context boundary, and
 * it is pinned to an explicit target origin so the token cannot be delivered to a frame that
 * happens to have been swapped underneath us.
 */
final readonly class TokenRelayView
{
    /** Namespacing marker so the chart page can ignore unrelated postMessage traffic. */
    public const MESSAGE_SOURCE = 'oe-module-ai-copilot';

    public const TYPE_TOKEN = 'token';
    public const TYPE_ERROR = 'error';

    public function __construct(private string $targetOrigin)
    {
    }

    public function renderToken(PatientScopedToken $token): string
    {
        return $this->render([
            'source' => self::MESSAGE_SOURCE,
            'type' => self::TYPE_TOKEN,
            'accessToken' => $token->accessToken,
            'expiresIn' => $token->expiresIn,
            'patient' => $token->patientUuid,
            'scope' => $token->scope,
        ]);
    }

    /**
     * @param string $reason A short, non-sensitive code the panel can show. Never an exception
     *     message: those can carry SQL, file paths, or the client secret's surroundings.
     */
    public function renderError(string $reason): string
    {
        return $this->render([
            'source' => self::MESSAGE_SOURCE,
            'type' => self::TYPE_ERROR,
            'reason' => $reason,
        ]);
    }

    /**
     * @param array<string, mixed> $payload
     */
    private function render(array $payload): string
    {
        // JSON_HEX_* keeps the encoded payload from terminating the <script> block or opening an
        // HTML comment, the two classic ways a JSON-in-HTML embedding turns into XSS.
        $json = json_encode(
            $payload,
            JSON_THROW_ON_ERROR | JSON_HEX_TAG | JSON_HEX_AMP | JSON_HEX_APOS | JSON_HEX_QUOT
        );
        $origin = json_encode($this->targetOrigin, JSON_THROW_ON_ERROR | JSON_HEX_TAG);

        return <<<HTML
            <!doctype html>
            <html lang="en">
            <head><meta charset="utf-8"><title>Clinical Co-Pilot</title></head>
            <body>
            <script>
            (function () {
                var payload = {$json};
                var targetOrigin = {$origin};
                if (window.parent && window.parent !== window) {
                    window.parent.postMessage(payload, targetOrigin);
                }
            })();
            </script>
            </body>
            </html>
            HTML;
    }
}
