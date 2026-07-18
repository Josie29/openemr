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

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Services\EncounterService;

/**
 * Persists an agent-derived chief concern as a new intake-derived encounter's reason.
 *
 * The chief concern *is* the reason for the visit, so it lands in `form_encounter.reason` (reads back
 * as `Encounter.reasonCode`). `EncounterService::insertEncounter` also writes the `forms` registry row
 * (`formdir='newpatient'`) that makes the encounter appear in the "Select Encounter" list — the direct
 * analog of the lab chain's mandatory `procedure_order_code` row, handled for us by the service.
 *
 * There is no verification column on an encounter either, so the reason text carries an inline marker
 * ({@see self::MARKER}) naming its source. Idempotency runs off the sidecar: one intake-derived
 * encounter per (document, content hash) — a re-run refreshes that encounter's reason rather than
 * creating a second visit.
 */
final readonly class ChiefConcernWriter
{
    /** The inline source marker prefixed onto the visit reason. */
    private const MARKER = '[AI Co-Pilot, from intake form] ';

    /** pc_catid 5 = a standard office visit; the validator requires a visit category. */
    private const VISIT_CATEGORY = 5;

    /** _ActEncounterCode 'AMB' = ambulatory, EncounterService's own default class. */
    private const CLASS_AMBULATORY = 'AMB';

    public function __construct(
        private ExtractionSidecar $sidecar,
        private EncounterService $encounters = new EncounterService(),
    ) {
    }

    /**
     * Persist the chief concern for one source document.
     *
     * @param list<DerivedChiefConcern> $concerns At most one per document; extras are ignored.
     *
     * @throws \RuntimeException When the write fails; the transaction is rolled back.
     */
    public function write(
        int $pid,
        int $documentId,
        string $contentHash,
        array $concerns,
        ProjectionRequest $request,
        string $username,
    ): FamilyOutcome {
        if ($concerns === []) {
            return FamilyOutcome::empty();
        }

        $concern = $concerns[0];
        $key = $concern->factKey();
        $reason = self::MARKER . $concern->text;

        sqlBeginTrans();
        try {
            $existingId = $this->sidecar->factIdFor($documentId, $contentHash, 'form_encounter', $key);

            if ($existingId !== null) {
                // Same document version — update the reason on the encounter we already created,
                // never a second visit.
                QueryUtils::sqlStatementThrowException(
                    'UPDATE form_encounter SET reason = ? WHERE id = ?',
                    [$reason, $existingId],
                );
                $encounterRowId = $existingId;
                $written = [];
                $skipped = [$key];
            } else {
                $encounterRowId = $this->createEncounter($pid, $reason, $request, $username);
                $written = [$key];
                $skipped = [];
            }

            $this->sidecar->record(
                documentId: $documentId,
                contentHash: $contentHash,
                pid: $pid,
                factTable: 'form_encounter',
                factId: $encounterRowId,
                field: $key,
                page: $concern->page,
                box: $concern->box,
                confidence: $concern->confidence,
                username: $username,
            );

            sqlCommitTrans();

            return new FamilyOutcome($written, $skipped);
        } catch (\Throwable $e) {
            sqlRollbackTrans();
            throw new \RuntimeException('Failed to persist a derived chief concern.', previous: $e);
        }
    }

    /**
     * Create the intake-derived encounter and return its `form_encounter.id`.
     *
     * @throws \RuntimeException When the service rejects the encounter or returns no id.
     */
    private function createEncounter(int $pid, string $reason, ProjectionRequest $request, string $username): int
    {
        $result = $this->encounters->insertEncounter($this->patientUuid($pid), [
            'pc_catid' => self::VISIT_CATEGORY,
            'class_code' => self::CLASS_AMBULATORY,
            'reason' => $reason,
            'facility_id' => $this->resolveFacility($request->facilityId),
            'provider_id' => $request->authUserId ?? 0,
            'user' => $username,
            // Empty group lets FormService fall back to the session's provider group.
            'group' => '',
            // date omitted → EncounterService defaults it to today.
        ]);

        if (!$result->isValid() || !$result->hasData()) {
            throw new \RuntimeException('The encounter service rejected a derived chief concern.');
        }

        $record = $result->getFirstDataResult();
        $encounterRowId = (int) ($record['id'] ?? 0);
        if ($encounterRowId <= 0 && !empty($record['encounter'])) {
            $encounterRowId = (int) QueryUtils::fetchSingleValue(
                'SELECT id FROM form_encounter WHERE encounter = ? AND pid = ? ORDER BY id DESC LIMIT 1',
                'id',
                [(int) $record['encounter'], $pid],
            );
        }
        if ($encounterRowId <= 0) {
            throw new \RuntimeException('The encounter service returned no encounter id.');
        }

        return $encounterRowId;
    }

    /**
     * The facility to book the encounter under: the session facility, else the default service
     * location, else any facility. Zero only if the install has none (it always has one).
     */
    private function resolveFacility(?int $facilityId): int
    {
        if ($facilityId !== null && $facilityId > 0) {
            return $facilityId;
        }

        $id = QueryUtils::fetchSingleValue(
            'SELECT id FROM facility WHERE service_location = 1 ORDER BY id LIMIT 1',
            'id',
            [],
        ) ?? QueryUtils::fetchSingleValue('SELECT id FROM facility ORDER BY id LIMIT 1', 'id', []);

        return $id === null ? 0 : (int) $id;
    }

    /**
     * @throws \RuntimeException When the patient has no uuid.
     */
    private function patientUuid(int $pid): string
    {
        $uuid = QueryUtils::fetchSingleValue('SELECT uuid FROM patient_data WHERE pid = ?', 'uuid', [$pid]);
        if ($uuid === null) {
            throw new \RuntimeException('The session patient has no uuid.');
        }

        return UuidRegistry::uuidToString($uuid);
    }
}
