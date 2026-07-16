<?php

/**
 * DB-backed coverage for AI Co-Pilot derived-fact write-back (JOS-81).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Services\Modules\AiCopilot;

use OpenEMR\Common\Database\QueryUtils;
use OpenEMR\Common\Uuid\UuidRegistry;
use OpenEMR\Core\ModulesClassLoader;
use OpenEMR\Core\OEGlobalsBag;
use OpenEMR\Modules\AiCopilot\Fact\AbnormalFlag;
use OpenEMR\Modules\AiCopilot\Fact\BoundingBox;
use OpenEMR\Modules\AiCopilot\Fact\DerivedLabResult;
use OpenEMR\Modules\AiCopilot\Fact\ExtractionSidecar;
use OpenEMR\Modules\AiCopilot\Fact\LabResultWriter;
use OpenEMR\Services\FHIR\Observation\FhirObservationLaboratoryService;
use OpenEMR\Tests\Fixtures\FixtureManager;
use PHPUnit\Framework\Attributes\Test;
use PHPUnit\Framework\TestCase;

/**
 * Proves that facts the agent reads off a PDF actually reach the chart and come back out through
 * FHIR. There is no write route for lab Observations, so the writer inserts down OpenEMR's native
 * chain (procedure_order -> procedure_order_code -> procedure_report -> procedure_result) and
 * relies on the FHIR projection to re-materialize them. Every assertion here is about that
 * round-trip, because the inserts succeeding proves nothing on its own — see the order_code test.
 */
class DerivedFactWriteBackTest extends TestCase
{
    private const MODULE_DIR = __DIR__ . '/../../../../../interface/modules/custom_modules/oe-module-ai-copilot';

    /** Synthetic source document. document_id carries no FK constraint, so no blob is needed. */
    private const DOCUMENT_ID = 987654;
    private const CONTENT_HASH = '0000000000000000000000000000000000000000000000000000000000000001';

    private const HBA1C = '4548-4';
    private const GLUCOSE = '2345-7';

    private FixtureManager $fixtureManager;
    private LabResultWriter $writer;
    private ExtractionSidecar $sidecar;
    private int $pid;
    private string $puuid;

    public static function setUpBeforeClass(): void
    {
        $classLoader = new ModulesClassLoader(OEGlobalsBag::getInstance()->getProjectDir());
        $classLoader->registerNamespaceIfNotExists(
            'OpenEMR\\Modules\\AiCopilot\\',
            self::MODULE_DIR . '/src'
        );

        // The sidecar ships in the module's install script, which a stock OpenEMR test database has
        // never run. Provision it from that same file rather than restating the DDL here, so the
        // schema has exactly one definition. The #-prefixed lines are SqlUpgradeService directives.
        $script = file_get_contents(self::MODULE_DIR . '/sql/table.sql');
        if ($script === false) {
            self::fail('Could not read the module install script.');
        }
        $ddl = implode(
            "\n",
            array_filter(
                explode("\n", $script),
                static fn(string $line): bool => !str_starts_with(trim($line), '#')
                    && !str_starts_with(trim($line), '--')
            )
        );
        foreach (array_filter(array_map('trim', explode(';', $ddl))) as $statement) {
            QueryUtils::sqlStatementThrowException(
                str_replace('CREATE TABLE ', 'CREATE TABLE IF NOT EXISTS ', $statement)
            );
        }
    }

    protected function setUp(): void
    {
        $this->fixtureManager = new FixtureManager();
        $this->fixtureManager->installPatientFixtures();

        $patient = QueryUtils::fetchRecords(
            'SELECT pid, uuid FROM patient_data WHERE pubpid LIKE ? ORDER BY pid LIMIT 1',
            [FixtureManager::PATIENT_FIXTURE_PUBPID_PREFIX . '%']
        );
        $this->pid = (int) $patient[0]['pid'];
        $this->puuid = UuidRegistry::uuidToString($patient[0]['uuid']);

        $this->sidecar = new ExtractionSidecar();
        $this->writer = new LabResultWriter($this->sidecar);
        $this->removeChain();
    }

    protected function tearDown(): void
    {
        $this->removeChain();
        $this->fixtureManager->removePatientFixtures();
    }

    #[Test]
    public function derivedLabResultsRoundTripAsFhirObservations(): void
    {
        $this->writer->write($this->pid, self::DOCUMENT_ID, self::CONTENT_HASH, $this->results(), 'admin');

        $observations = $this->readDerivedObservations();

        $this->assertCount(2, $observations, 'Both derived results should re-materialize as Observations.');
        $this->assertSame('8.2', (string) $observations[self::HBA1C]['valueQuantity']['value']);
        $this->assertSame('%', $observations[self::HBA1C]['valueQuantity']['unit']);
        $this->assertSame('126', (string) $observations[self::GLUCOSE]['valueQuantity']['value']);
        $this->assertSame('mg/dL', $observations[self::GLUCOSE]['valueQuantity']['unit']);
    }

    /**
     * A model read these off a PDF; no clinician confirmed them. If this regressed to 'final', the
     * chart would present agent output as clinician-confirmed results.
     */
    #[Test]
    public function derivedResultsAreMarkedPreliminaryNotFinal(): void
    {
        $this->writer->write($this->pid, self::DOCUMENT_ID, self::CONTENT_HASH, $this->results(), 'admin');

        foreach ($this->readDerivedObservations() as $code => $observation) {
            $this->assertSame('preliminary', $observation['status'], "$code must be flagged as derived.");
        }
    }

    /**
     * The PRD requires derived observations round-trip "without creating duplicate or untraceable
     * records". Without this, every follow-up question that re-triggers extraction would deposit
     * another copy of the same labs into the chart.
     */
    #[Test]
    public function reExtractingTheSameDocumentDoesNotDuplicateResults(): void
    {
        $first = $this->writer->write($this->pid, self::DOCUMENT_ID, self::CONTENT_HASH, $this->results(), 'admin');
        $second = $this->writer->write($this->pid, self::DOCUMENT_ID, self::CONTENT_HASH, $this->results(), 'admin');

        $this->assertSame([self::HBA1C, self::GLUCOSE], $first->written);
        $this->assertSame([], $second->written, 'A repeat write must persist nothing.');
        $this->assertSame([self::HBA1C, self::GLUCOSE], $second->skipped);
        $this->assertSame(2, $this->countResultRows());
        $this->assertSame($first->procedureOrderId, $second->procedureOrderId, 'The order should be reused.');
    }

    /**
     * THE TRAP. ProcedureService::search joins
     * `preport.procedure_order_seq = order_codes.procedure_order_seq` with order_codes LEFT joined,
     * so with no procedure_order_code row the predicate compares against NULL, never matches, and
     * every result disappears from FHIR — while sitting intact in the database, with no error
     * raised anywhere.
     *
     * This test pins the behaviour so that if anyone "simplifies" the writer by dropping what looks
     * like a redundant row holding no result data, the failure is loud here instead of silent in
     * production. It asserts the trap exists, and the tests above assert we avoid it.
     */
    #[Test]
    public function removingTheOrderCodeSilentlyHidesResultsFromFhir(): void
    {
        $this->writer->write($this->pid, self::DOCUMENT_ID, self::CONTENT_HASH, $this->results(), 'admin');
        $this->assertCount(2, $this->readDerivedObservations(), 'Sanity: visible while the chain is intact.');

        QueryUtils::sqlStatementThrowException(
            'DELETE FROM procedure_order_code WHERE procedure_order_id IN'
            . ' (SELECT procedure_order_id FROM procedure_report WHERE procedure_report_id IN'
            . ' (SELECT procedure_report_id FROM procedure_result WHERE document_id = ?))',
            [self::DOCUMENT_ID]
        );

        $this->assertSame(2, $this->countResultRows(), 'The results are still in the database...');
        $this->assertCount(0, $this->readDerivedObservations(), '...but FHIR silently returns none.');
    }

    /**
     * A persisted Observation carries the value but not the pixel rectangle, so without the sidecar
     * click-to-source breaks the moment the agent restarts and the in-memory registry is gone.
     */
    #[Test]
    public function sidecarRecordsCitationGeometryForEachFact(): void
    {
        $this->writer->write($this->pid, self::DOCUMENT_ID, self::CONTENT_HASH, $this->results(), 'admin');

        $citations = $this->sidecar->citationsFor(self::DOCUMENT_ID, self::CONTENT_HASH);

        $this->assertCount(2, $citations);
        $this->assertSame(1, $citations[self::HBA1C]['page']);
        $this->assertSame(72.0, $citations[self::HBA1C]['box']->x);
        $this->assertSame(310.5, $citations[self::HBA1C]['box']->y);
        $this->assertSame('procedure_result', $citations[self::HBA1C]['fact_table']);
        $this->assertGreaterThan(0, $citations[self::HBA1C]['fact_id']);
    }

    /** Provenance back to the source PDF — the chain Observation -> document must stay walkable. */
    #[Test]
    public function eachPersistedResultLinksBackToItsSourceDocument(): void
    {
        $this->writer->write($this->pid, self::DOCUMENT_ID, self::CONTENT_HASH, $this->results(), 'admin');

        $orphans = QueryUtils::fetchSingleValue(
            'SELECT COUNT(*) AS c FROM procedure_result WHERE procedure_report_id IN'
            . ' (SELECT procedure_report_id FROM procedure_result WHERE document_id = ?)'
            . ' AND document_id = 0',
            'c',
            [self::DOCUMENT_ID]
        );

        $this->assertSame(0, (int) $orphans, 'No derived result may lack its source document link.');
    }

    /** @return list<DerivedLabResult> */
    private function results(): array
    {
        return [
            new DerivedLabResult(
                loincCode: self::HBA1C,
                label: 'Hemoglobin A1c/Hemoglobin.total in Blood',
                value: '8.2',
                units: '%',
                referenceRange: '4.0-5.6',
                abnormal: AbnormalFlag::High,
                box: new BoundingBox(72.0, 310.5, 148.0, 12.0),
                page: 1,
                confidence: 0.98,
            ),
            new DerivedLabResult(
                loincCode: self::GLUCOSE,
                label: 'Glucose [Mass/volume] in Serum or Plasma',
                value: '126',
                units: 'mg/dL',
                referenceRange: '70-99',
                abnormal: AbnormalFlag::High,
                box: new BoundingBox(72.0, 328.5, 148.0, 12.0),
                page: 1,
                confidence: 0.97,
            ),
        ];
    }

    /**
     * Read back through the same projection the REST layer serves.
     *
     * @return array<string, array<string, mixed>> Keyed by LOINC code.
     */
    private function readDerivedObservations(): array
    {
        $service = new FhirObservationLaboratoryService();
        $observations = $service->getAll(['patient' => $this->puuid], $this->puuid)->getData();

        $found = [];
        foreach ($observations as $observation) {
            $decoded = json_decode(json_encode($observation), true);
            $code = $decoded['code']['coding'][0]['code'] ?? '';
            if (in_array($code, [self::HBA1C, self::GLUCOSE], true)) {
                $found[$code] = $decoded;
            }
        }

        return $found;
    }

    private function countResultRows(): int
    {
        return (int) QueryUtils::fetchSingleValue(
            'SELECT COUNT(*) AS c FROM procedure_result WHERE document_id = ?',
            'c',
            [self::DOCUMENT_ID]
        );
    }

    /** Tear the synthetic chain down leaf-first so no orphan order survives between tests. */
    private function removeChain(): void
    {
        $reports = QueryUtils::fetchRecords(
            'SELECT DISTINCT procedure_report_id FROM procedure_result WHERE document_id = ?',
            [self::DOCUMENT_ID]
        );
        foreach ($reports as $report) {
            $reportId = (int) $report['procedure_report_id'];
            $orderId = QueryUtils::fetchSingleValue(
                'SELECT procedure_order_id FROM procedure_report WHERE procedure_report_id = ?',
                'procedure_order_id',
                [$reportId]
            );
            QueryUtils::sqlStatementThrowException(
                'DELETE FROM procedure_result WHERE procedure_report_id = ?',
                [$reportId]
            );
            QueryUtils::sqlStatementThrowException(
                'DELETE FROM procedure_report WHERE procedure_report_id = ?',
                [$reportId]
            );
            if ($orderId !== null) {
                QueryUtils::sqlStatementThrowException(
                    'DELETE FROM procedure_order_code WHERE procedure_order_id = ?',
                    [$orderId]
                );
                QueryUtils::sqlStatementThrowException(
                    'DELETE FROM procedure_order WHERE procedure_order_id = ?',
                    [$orderId]
                );
            }
        }
        QueryUtils::sqlStatementThrowException(
            'DELETE FROM ai_copilot_document_facts WHERE document_id = ?',
            [self::DOCUMENT_ID]
        );
    }
}
