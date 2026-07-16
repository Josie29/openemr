<?php

/**
 * Isolated coverage for the AI Co-Pilot derived-fact trust boundary (JOS-81).
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Modules\AiCopilot;

use OpenEMR\Modules\AiCopilot\Fact\AbnormalFlag;
use OpenEMR\Modules\AiCopilot\Fact\FactPayloadParser;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\Attributes\Test;
use PHPUnit\Framework\TestCase;

/**
 * These facts arrive from the browser, so the parser is the boundary between untrusted JSON and
 * clinical records. Everything here is about refusing malformed input rather than coercing it —
 * a coerced fact becomes a wrong value in someone's chart.
 */
class FactPayloadParserIsolatedTest extends TestCase
{
    private FactPayloadParser $parser;

    protected function setUp(): void
    {
        $this->parser = new FactPayloadParser();
    }

    #[Test]
    public function parsesAWellFormedLabFact(): void
    {
        $parsed = $this->parser->parseLabResults([$this->validFact()]);

        $this->assertCount(1, $parsed);
        $this->assertSame('4548-4', $parsed[0]->loincCode);
        $this->assertSame('8.2', $parsed[0]->value);
        $this->assertSame('%', $parsed[0]->units);
        $this->assertSame(AbnormalFlag::High, $parsed[0]->abnormal);
        $this->assertSame(2, $parsed[0]->page);
        $this->assertSame(0.98, $parsed[0]->confidence);
        $this->assertSame(72.0, $parsed[0]->box->x);
    }

    /**
     * An absent flag is the common case (most results are normal) and must not be an error, or
     * every normal lab value would be rejected.
     */
    #[Test]
    public function treatsAnAbsentAbnormalFlagAsNormal(): void
    {
        $fact = $this->validFact();
        unset($fact['abnormal']);

        $parsed = $this->parser->parseLabResults([$fact]);

        $this->assertSame(AbnormalFlag::No, $parsed[0]->abnormal);
    }

    /**
     * Parsing is all-or-nothing. A partial write would leave the chart holding some of a
     * document's results and not others, with nothing signalling that anything was dropped.
     */
    #[Test]
    public function rejectsTheWholeBatchWhenAnyFactIsMalformed(): void
    {
        $bad = $this->validFact();
        unset($bad['loinc']);

        $this->expectException(\DomainException::class);

        $this->parser->parseLabResults([$this->validFact(), $bad]);
    }

    /**
     * @param array<string, mixed> $fact
     */
    #[Test]
    #[DataProvider('malformedFactProvider')]
    public function rejectsMalformedFacts(array $fact, string $because): void
    {
        $this->expectException(\DomainException::class, $because);

        $this->parser->parseLabResults([$fact]);
    }

    /**
     * @return array<string, array{array<string, mixed>, string}>
     *
     * @codeCoverageIgnore Data providers run before coverage instrumentation starts.
     */
    public static function malformedFactProvider(): array
    {
        $base = [
            'loinc' => '4548-4',
            'label' => 'Hemoglobin A1c',
            'value' => '8.2',
            'bbox' => ['x' => 72.0, 'y' => 310.5, 'w' => 148.0, 'h' => 12.0],
        ];

        return [
            // Both must be non-empty or FhirObservationLaboratoryService degrades Observation.code
            // to a nullFlavor UNK — the fact would persist but read back meaningless.
            'missing loinc' => [array_diff_key($base, ['loinc' => null]), 'code degrades to UNK'],
            'missing label' => [array_diff_key($base, ['label' => null]), 'code degrades to UNK'],
            'missing value' => [array_diff_key($base, ['value' => null]), 'a result needs a value'],
            // ProcedureService filters these sentinels out on read, so the row would insert
            // cleanly and never come back.
            'DNR sentinel' => [[...$base, 'value' => 'DNR'], 'filtered out on read'],
            'TNP sentinel' => [[...$base, 'value' => 'tnp'], 'filtered out on read'],
            'unknown abnormal flag' => [[...$base, 'abnormal' => 'critical'], 'not a column value'],
            'missing bbox' => [array_diff_key($base, ['bbox' => null]), 'citation needs geometry'],
            'bbox missing a side' => [[...$base, 'bbox' => ['x' => 1, 'y' => 1, 'w' => 1]], 'incomplete box'],
            'non-numeric bbox' => [[...$base, 'bbox' => ['x' => 'a', 'y' => 1, 'w' => 1, 'h' => 1]], 'not numeric'],
            // A zero-area box renders as an invisible highlight — a citation pointing nowhere.
            'zero-area bbox' => [[...$base, 'bbox' => ['x' => 1, 'y' => 1, 'w' => 0, 'h' => 5]], 'invisible highlight'],
            'negative origin' => [[...$base, 'bbox' => ['x' => -1, 'y' => 1, 'w' => 5, 'h' => 5]], 'off-page origin'],
            'page zero' => [[...$base, 'page' => 0], 'pages are 1-based'],
            'confidence above one' => [[...$base, 'confidence' => 1.5], 'out of range'],
            'non-numeric confidence' => [[...$base, 'confidence' => 'high'], 'not numeric'],
        ];
    }

    /** @return array<string, mixed> */
    private function validFact(): array
    {
        return [
            'loinc' => '4548-4',
            'label' => 'Hemoglobin A1c/Hemoglobin.total in Blood',
            'value' => '8.2',
            'units' => '%',
            'range' => '4.0-5.6',
            'abnormal' => 'high',
            'page' => 2,
            'bbox' => ['x' => 72.0, 'y' => 310.5, 'w' => 148.0, 'h' => 12.0],
            'confidence' => 0.98,
        ];
    }
}
