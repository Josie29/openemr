<?php

/**
 * Seed demo free-text clinical notes so the Co-Pilot's `get_encounter_note` tool
 * (FHIR `DocumentReference?category=clinical-note`) returns real narrative.
 *
 * WHY THIS EXISTS
 * The Synthea import populates coded resources (Condition/MedicationRequest/…) but leaves
 * `form_clinical_notes` empty, so the note tool returns nothing on every patient (JOS-33).
 * This seeder writes a small set of reviewed, clinically-coherent progress notes tied to a
 * patient's real problems/meds, so UC-3 ("why was X started/stopped") resolves to prose.
 *
 * THE ONE RULE THAT MATTERS — leave `clinical_notes_category` EMPTY.
 * OpenEMR routes a clinical note by category: an EMPTY `clinical_notes_category` surfaces the
 * note as a FHIR `DocumentReference` (category `clinical-note`) — which is what the agent reads.
 * A SET category (cardiology/radiology/pathology) routes the note to `DiagnosticReport` instead,
 * which the agent does NOT read (see FhirClinicalNotesService::searchForOpenEMRRecords, the
 * `clinical_notes_category` MISSING filter). Setting a category here makes the note vanish from
 * the tool. Do not set one.
 *
 * SAFE CREATION PATH
 * Uses ClinicalNotesService (createClinicalNotesParentForm + saveArray) so the `forms` parent
 * row and the note UUID are created correctly — never raw INSERTs.
 *
 * IDEMPOTENT
 * Skips a patient if an active clinical note already exists on the target encounter, so it is
 * safe to re-run (e.g. after a prod->local clone).
 *
 * RUN (in the openemr container, as the web user — never root):
 *   openemr-cmd e 'php interface/modules/custom_modules/oe-module-ai-copilot/scripts/seed_demo_clinical_notes.php'
 * or against a specific worktree/stack:
 *   openemr-cmd worktree exec <branch> e 'php .../scripts/seed_demo_clinical_notes.php'
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

// --- Minimal CLI bootstrap of the OpenEMR runtime (site + ignore interactive auth) ---
$_GET['site'] = $_GET['site'] ?? 'default';
$_SERVER['HTTP_HOST'] = $_SERVER['HTTP_HOST'] ?? 'localhost';
$_SERVER['REQUEST_URI'] = $_SERVER['REQUEST_URI'] ?? '/';
$_SERVER['SERVER_NAME'] = $_SERVER['SERVER_NAME'] ?? 'localhost';
$ignoreAuth = true;
$sessionAllowWrite = true;

$fileroot = dirname(__DIR__, 5);
require $fileroot . '/interface/globals.php';
require_once $fileroot . '/library/forms.inc.php';

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Services\ClinicalNotesService;
use OpenEMR\Services\FHIR\FhirDocumentReferenceService;

/**
 * The demo notes to seed.
 *
 * Each note targets ONE patient encounter, selected by a stable `anchor` rather than a raw
 * encounter id (ids are environment-specific; this keeps the seed portable and — because local
 * is a clone of prod — lands on the same real visits in both). An anchor is either:
 *   - ['latest' => true]         → the patient's most-recent encounter, or
 *   - ['date' => 'YYYY-MM-DD']   → the encounter on that date (the real visit the note describes).
 *
 * Every note MUST leave `clinical_notes_category` empty (see file header) so it routes to a FHIR
 * `DocumentReference` (what the agent reads), not a `DiagnosticReport`.
 *
 * The set below is Sergio Angulo (prod pid 23), the demo's primary showcase patient. The three
 * notes form a verifiable timeline the Co-Pilot can reason over — every clinical claim is anchored
 * to a real problem/med/allergy in his record, so UC-3 "why was X started/stopped" resolves to prose:
 *   1. 2022-01-22  asthma follow-up after hospitalization → the definitive "why prednisone" anchor
 *   2. 2026-01-06  ER visit for concussion               → recent narrative + NSAID-safety hook
 *   3. 2026-06-03  general exam / current state          → comprehensive summary, resolved concussion
 *
 * @var list<array{pid: int, anchor: array{latest?: bool, date?: string}, type_code: string, type_text: string, description: string}>
 */
const DEMO_NOTES = [
    // 1. Asthma exacerbation requiring hospital admission (2022-01-20 admission → 2022-01-22 follow-up).
    //    Anchors "why is he on prednisone?" to a real systemic-steroid event, and "has he ever been
    //    hospitalized for asthma?" to a real admission.
    [
        'pid' => 23,
        'anchor' => ['date' => '2022-01-22'],
        'type_code' => '11506-3',
        'type_text' => 'Progress Note',
        'description' => <<<TXT
Asthma follow-up after hospital admission.

Seen two days after an emergency hospital admission for an acute asthma exacerbation. During the admission he required a short course of systemic corticosteroids (prednisone 5 mg taper) on top of his usual budesonide inhalation suspension controller and albuterol (Ventolin) rescue inhaler.

Symptoms have returned to baseline with no ongoing dyspnea or nocturnal cough. Continue the daily budesonide controller and reserve prednisone for future exacerbations only. Reviewed inhaler technique, early rescue-inhaler use, and an asthma action plan. Routine follow-up arranged.
TXT,
    ],

    // 2. Emergency-department visit for concussion (2026-01-06). Anchors "what happened at his
    //    January ER visit?" and reinforces the NSAID-safety theme (aspirin allergy + asthma).
    [
        'pid' => 23,
        'anchor' => ['date' => '2026-01-06'],
        'type_code' => '11506-3',
        'type_text' => 'Progress Note',
        'description' => <<<TXT
Emergency department visit - head injury.

Presented following a head injury. Concussion without loss of consciousness. Neurologic examination non-focal with no red-flag features; head imaging not indicated by clinical decision rule.

Diagnosed concussion with no loss of consciousness. Discharged with return precautions, activity modification, and outpatient follow-up. Analgesia with acetaminophen; NSAIDs were deliberately avoided given his documented aspirin allergy and asthma.
TXT,
    ],

    // 3. Current comprehensive state at the most recent general exam (2026-06-03). Also records that
    //    the January concussion has since resolved, so "is the concussion still active?" resolves.
    [
        'pid' => 23,
        'anchor' => ['date' => '2026-06-03'],
        'type_code' => '11506-3',
        'type_text' => 'Progress Note',
        'description' => <<<TXT
Progress note - general examination.

Established patient seen for routine general examination and medication review.

Asthma is the primary active problem. He is maintained on budesonide inhalation suspension as a daily controller and albuterol (Ventolin) as a rescue inhaler, with short courses of prednisone 5 mg reserved for exacerbations. He reports good control since the last visit, with only occasional rescue-inhaler use and no flares, so no steroid course was needed today. Continue the current inhaled regimen and review inhaler technique at the next visit.

He has a history of environmental allergies with prior anaphylaxis: he takes fexofenadine for allergic rhinitis and carries an epinephrine auto-injector, confirmed today to be unexpired. Reinforced allergen avoidance and auto-injector use.

The concussion sustained in January 2026, which prompted his emergency room visit, has fully resolved with no residual headache or cognitive symptoms.

Naproxen is used intermittently for musculoskeletal pain; he was counseled to limit NSAID use given his asthma and aspirin allergy. No new complaints today. Continue current regimen with routine follow-up.
TXT,
    ],
];

/**
 * Resolve a note's target encounter (id + date) from its anchor.
 *
 * @param array{latest?: bool, date?: string} $anchor Stable selector — most-recent encounter, or
 *                                                     the encounter on a specific date.
 * @return array{encounter: int, date: string}|null Null when no matching encounter exists.
 */
function resolveEncounter(int $pid, array $anchor): ?array
{
    if (($anchor['date'] ?? null) !== null) {
        $row = QueryUtils::fetchRecords(
            "SELECT encounter, DATE(`date`) AS d FROM form_encounter WHERE pid = ? AND DATE(`date`) = ? ORDER BY encounter DESC LIMIT 1",
            [$pid, $anchor['date']]
        );
    } else {
        $row = QueryUtils::fetchRecords(
            "SELECT encounter, DATE(`date`) AS d FROM form_encounter WHERE pid = ? ORDER BY `date` DESC LIMIT 1",
            [$pid]
        );
    }
    if (empty($row)) {
        return null;
    }
    return ['encounter' => (int) $row[0]['encounter'], 'date' => (string) $row[0]['d']];
}

$fhir = new FhirDocumentReferenceService();
$notes = new ClinicalNotesService();

/** @var array<int, string> Cache each patient's UUID string, keyed by pid, for the FHIR re-read. */
$patientUuids = [];

foreach (DEMO_NOTES as $note) {
    $pid = $note['pid'];

    $puuidRaw = QueryUtils::fetchSingleValue("SELECT uuid FROM patient_data WHERE pid = ?", 'uuid', [$pid]);
    if (!is_string($puuidRaw) || $puuidRaw === '') {
        echo "pid $pid: SKIP — no such patient in this environment\n";
        continue;
    }
    $puuid = UuidRegistry::uuidToString($puuidRaw);
    $patientUuids[$pid] = $puuid;

    $enc = resolveEncounter($pid, $note['anchor']);
    if ($enc === null) {
        $where = $note['anchor']['date'] ?? 'latest';
        echo "pid $pid: SKIP — no encounter matches anchor ($where)\n";
        continue;
    }

    $existing = QueryUtils::fetchRecords(
        "SELECT id FROM form_clinical_notes WHERE pid = ? AND encounter = ? AND activity = 1",
        [$pid, $enc['encounter']]
    );
    if (!empty($existing)) {
        echo "pid $pid: SKIP — active clinical note already on encounter {$enc['encounter']} ({$enc['date']}) (idempotent)\n";
        continue;
    }

    $formId = $notes->createClinicalNotesParentForm($pid, $enc['encounter'], 1);
    $notes->saveArray([
        'form_id' => $formId,
        'pid' => $pid,
        'encounter' => $enc['encounter'],
        'authorized' => 1,
        'activity' => 1,
        'date' => $enc['date'],
        'user' => 'admin',
        'groupname' => 'Default',
        'code' => $note['type_code'],
        'codetext' => $note['type_text'],
        'clinical_notes_type' => 'progress_note',
        // clinical_notes_category intentionally omitted — see file header (routes to
        // DocumentReference, not DiagnosticReport).
        'description' => $note['description'],
    ]);
    echo "pid $pid: CREATED note on encounter {$enc['encounter']} ({$enc['date']})\n";
}

// Verify each seeded patient's notes round-trip through the exact FHIR path the agent uses.
foreach ($patientUuids as $pid => $puuid) {
    $res = $fhir->getAll(['category' => 'clinical-note', 'patient' => $puuid], $puuid);
    $count = count($res->getData());
    echo "pid $pid: FHIR DocumentReference?category=clinical-note -> count=$count "
        . ($count > 0 ? "OK\n" : "FAIL (check clinical_notes_category is empty)\n");
}
