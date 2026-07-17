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
use OpenEMR\Services\AllergyIntoleranceService;
use OpenEMR\Services\PatientIssuesService;

/**
 * Persists agent-derived allergies and medications into OpenEMR's `lists` table.
 *
 * Both become `lists` rows, but they are **not symmetric** in how honestly they can be flagged as
 * agent-derived, because the two FHIR projections expose different fields:
 *
 * - An allergy carries `verification='unconfirmed'`, which `FhirAllergyIntoleranceService` maps
 *   straight to `AllergyIntolerance.verificationStatus`. Strong signal.
 * - A medication cannot. **Nothing reads `lists.verification` for a medication row** — only the
 *   allergy and condition services touch that column, so writing it there is a silent no-op. And
 *   `MedicationRequest.status` is derived from a `CASE` over `enddate` + `activity` alone
 *   (`PrescriptionService:234-238`), so it can only ever be active/completed/stopped. The honest
 *   marker left is `lists_medication.request_intent='proposal'` → `MedicationRequest.intent`, whose
 *   own list_options description is "a suggestion made by someone/something that doesn't have an
 *   intention to ensure it occurs and without providing an authorization to act". A consumer
 *   filtering only on `status` still sees an ordinary active medication — that limitation is real
 *   and documented in `context/specs/derived-fact-write-back.md`.
 *
 * Demographics, chief concern, and family history are deliberately not written: no honest
 * derived-marker exists for any of them.
 */
final readonly class IntakeFactWriter
{
    /** The one intent value that says "a suggestion, with no authority behind it". */
    private const DERIVED_INTENT = 'proposal';

    /** lists.verification value meaning "not clinician-confirmed". Allergies only — see class doc. */
    private const DERIVED_VERIFICATION = 'unconfirmed';

    private const TYPE_ALLERGY = 'allergy';
    private const TYPE_MEDICATION = 'medication';

    public function __construct(
        private ExtractionSidecar $sidecar,
        private AllergyIntoleranceService $allergyService = new AllergyIntoleranceService(),
        private PatientIssuesService $issuesService = new PatientIssuesService(),
    ) {
    }

    /**
     * Persist derived intake facts for one source document.
     *
     * An empty list is **not** a negative finding. `IntakeForm`'s own docstring is explicit that an
     * empty `allergies` list means "none read from the form", not "no known allergies" — so an empty
     * section writes nothing rather than asserting NKDA, which the document never said.
     *
     * @param list<DerivedAllergy> $allergies
     * @param list<DerivedMedication> $medications
     *
     * @throws \RuntimeException When the write fails; the transaction is rolled back.
     */
    public function write(
        int $pid,
        int $documentId,
        string $contentHash,
        array $allergies,
        array $medications,
        string $username,
    ): IntakeWriteOutcome {
        $written = [];
        $skipped = [];

        sqlBeginTrans();
        try {
            foreach ($allergies as $allergy) {
                $key = $allergy->factKey();
                $listId = $this->writeAllergy($pid, $allergy);
                if ($listId === null) {
                    $skipped[] = $key;
                    continue;
                }
                $this->recordCitation($documentId, $contentHash, $pid, $listId, $key, $allergy->page, $allergy->box, $allergy->confidence, $username);
                $written[] = $key;
            }

            foreach ($medications as $medication) {
                $key = $medication->factKey();
                $listId = $this->writeMedication($pid, $medication, $username);
                if ($listId === null) {
                    $skipped[] = $key;
                    continue;
                }
                $this->recordCitation($documentId, $contentHash, $pid, $listId, $key, $medication->page, $medication->box, $medication->confidence, $username);
                $written[] = $key;
            }

            sqlCommitTrans();

            return new IntakeWriteOutcome($written, $skipped);
        } catch (\Throwable $e) {
            // A medication spans two tables (lists + lists_medication); a failure between them would
            // strand a medication with no intent, i.e. one that reads back as an ordinary plan.
            sqlRollbackTrans();
            throw new \RuntimeException('Failed to persist derived intake facts.', previous: $e);
        }
    }

    /**
     * @return int|null The new lists.id, or null when the chart already holds this allergy.
     *
     * @throws \RuntimeException
     */
    private function writeAllergy(int $pid, DerivedAllergy $allergy): ?int
    {
        if ($this->alreadyOnChart($pid, self::TYPE_ALLERGY, $allergy->substance)) {
            return null;
        }

        // insert() hardcodes date/activity/type and mints the uuid itself; buildInsertColumns
        // whitelists against the real `lists` columns, so `verification` passes straight through.
        $result = $this->allergyService->insert([
            'puuid' => $this->patientUuid($pid),
            'title' => $allergy->substance,
            'reaction' => $allergy->reaction ?? '',
            'verification' => self::DERIVED_VERIFICATION,
        ]);

        if (!$result->isValid() || $result->getData() === []) {
            throw new \RuntimeException('The allergy service rejected a derived allergy.');
        }

        $data = $result->getData();

        return (int) $data[0]['id'];
    }

    /**
     * @return int|null The new lists.id, or null when the chart already holds this medication.
     *
     * @throws \RuntimeException
     */
    private function writeMedication(int $pid, DerivedMedication $medication, string $username): ?int
    {
        if ($this->alreadyOnChart($pid, self::TYPE_MEDICATION, $medication->name)) {
            return null;
        }

        $record = [
            'pid' => $pid,
            'type' => self::TYPE_MEDICATION,
            'title' => $medication->name,
            // createIssue sets neither, and both columns default to NULL. Without activity=1 the
            // status CASE falls through to 'stopped' and the row would not read back as a current
            // medication at all.
            'activity' => 1,
            'date' => date('Y-m-d H:i:s'),
            // createIssue does not mint a uuid (AllergyIntoleranceService::insert does), and
            // PrescriptionService's constructor backfills prescriptions/patient/encounter/users/drugs
            // but NOT `lists` — so without this the row reads back with no FHIR id.
            'uuid' => (new UuidRegistry(['table_name' => 'lists']))->createUuid(),
            'user' => $username,
            'comments' => $this->disclosure($medication),
            'medication' => [
                // Explicit: the lists branch reads IF(request_intent IS NULL, 'plan', ...), so a
                // missing value would silently present this as a clinician's plan.
                'request_intent' => self::DERIVED_INTENT,
                'drug_dosage_instructions' => $medication->dosageInstructions(),
            ],
        ];

        $created = $this->issuesService->createIssue($record);
        if (empty($created['id'])) {
            throw new \RuntimeException('The issues service did not return an id for a derived medication.');
        }

        return (int) $created['id'];
    }

    /**
     * The human-readable half of the medication disclosure.
     *
     * Surfaces verbatim as `MedicationRequest.note` (`PrescriptionService:216`). It exists because
     * `intent: proposal` is a coded signal a casual reader will not see — this says the same thing
     * in words, in the chart, where a clinician actually looks.
     */
    private function disclosure(DerivedMedication $medication): string
    {
        $note = 'Extracted from a patient-supplied document by the AI Clinical Co-Pilot. '
            . 'Not confirmed by a clinician.';
        $dosage = $medication->dosageInstructions();

        return $dosage === null ? $note : $note . ' Reported as: ' . $dosage . '.';
    }

    /**
     * Is this fact already on the chart as an active record?
     *
     * Checks the destination rather than the sidecar on purpose: the sidecar is a rebuildable cache
     * (`W2_ARCHITECTURE.md` §6), so gating on it would let a cache wipe license duplicate clinical
     * rows. `lists` has no document_id column, so unlike labs this dedupes on the clinical identity
     * itself — a patient should not acquire a second active Penicillin allergy because a second
     * document mentioned it.
     */
    private function alreadyOnChart(int $pid, string $type, string $title): bool
    {
        return QueryUtils::fetchSingleValue(
            'SELECT id FROM lists WHERE pid = ? AND type = ? AND activity = 1'
            . ' AND LOWER(TRIM(title)) = LOWER(TRIM(?)) LIMIT 1',
            'id',
            [$pid, $type, $title],
        ) !== null;
    }

    private function recordCitation(
        int $documentId,
        string $contentHash,
        int $pid,
        int $listId,
        string $key,
        int $page,
        ?BoundingBox $box,
        ?float $confidence,
        string $username,
    ): void {
        $this->sidecar->record(
            documentId: $documentId,
            contentHash: $contentHash,
            pid: $pid,
            factTable: 'lists',
            factId: $listId,
            field: $key,
            page: $page,
            box: $box,
            confidence: $confidence,
            username: $username,
        );
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
