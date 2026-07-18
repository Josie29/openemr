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
 * The session-derived context a projector needs to write, resolved once at the endpoint.
 *
 * Everything here comes from the authenticated OpenEMR session, never the request payload — the pid
 * is the IDOR-safe session pid, and the author/facility ids identify the logged-in clinician who is
 * authorizing the write. Projectors take this rather than reaching into `$_SESSION`/`$GLOBALS`
 * themselves, keeping superglobal access confined to the endpoint entry point.
 */
final readonly class ProjectionRequest
{
    /**
     * @param int $pid Session patient id.
     * @param int $documentId Source document row id (documents.id).
     * @param string $contentHash SHA-256 of the stored document bytes — the extraction version key.
     * @param string $username Session `authUser`; provenance for the sidecar and for `lists.user`.
     * @param bool $accept Whether the clinician has accepted gated (overwrite) facts this request.
     * @param ExtractionSidecar $sidecar Citation store the projectors record geometry into.
     * @param int|null $authUserId Session `authUserID`; the encounter/forms author, when needed.
     * @param int|null $authProviderId Session `authProvider`; the encounter provider group, when needed.
     * @param int|null $facilityId Session facility; a projector falls back to a default when null/0.
     */
    public function __construct(
        public int $pid,
        public int $documentId,
        public string $contentHash,
        public string $username,
        public bool $accept,
        public ExtractionSidecar $sidecar,
        public ?int $authUserId = null,
        public ?int $authProviderId = null,
        public ?int $facilityId = null,
    ) {
    }
}
