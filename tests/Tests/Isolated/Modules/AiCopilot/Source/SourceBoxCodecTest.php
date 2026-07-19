<?php

/**
 * Isolated SourceBoxCodec Test
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Modules\AiCopilot\Source;

use OpenEMR\Modules\AiCopilot\Source\SourceBox;
use OpenEMR\Modules\AiCopilot\Source\SourceBoxCodec;
use PHPUnit\Framework\Attributes\DataProvider;
use PHPUnit\Framework\TestCase;

// The ai-copilot module is loaded by OpenEMR's runtime module system, not the root composer
// autoloader, so pull the classes under test in directly (same pattern as the Smart isolated tests).
require_once __DIR__ . '/../../../../../../interface/modules/custom_modules/oe-module-ai-copilot/src/Source/SourceBox.php';
require_once __DIR__ . '/../../../../../../interface/modules/custom_modules/oe-module-ai-copilot/src/Source/SourceBoxCodec.php';

/**
 * Guards what the click-to-source viewer is willing to draw on a physician's source document.
 *
 * The rectangles arrive over the URL and the viewer draws whatever it is handed, so this decoder is
 * the only thing standing between a malformed parameter and a box drawn over the wrong part of a
 * scan -- which would point a physician at text that does not say what the citation claims.
 */
final class SourceBoxCodecTest extends TestCase
{
    /**
     * The ordinary case: several boxes on one page, decoded in order.
     *
     * Order is the contract -- it is the number badge the viewer draws and the sidebar's fact list
     * mirrors. If the order drifted, fact 2 would point at box 3's rectangle.
     */
    public function testDecodesBoxesInOrder(): void
    {
        $boxes = SourceBoxCodec::decode('10,20,100,12;10.5,40.25,80,12;30,60,50,14');

        $this->assertCount(3, $boxes);
        $this->assertContainsOnlyInstancesOf(SourceBox::class, $boxes);
        $this->assertSame(10.0, $boxes[0]->x);
        $this->assertSame(40.25, $boxes[1]->y);
        $this->assertSame(50.0, $boxes[2]->width);
        $this->assertSame(['x' => 10.0, 'y' => 20.0, 'w' => 100.0, 'h' => 12.0], $boxes[0]->toViewArray());
    }

    /**
     * A single box still decodes -- the shape the back-compat x/y/w/h path folds into.
     */
    public function testDecodesASingleBox(): void
    {
        $boxes = SourceBoxCodec::decode('1,2,3,4');

        $this->assertCount(1, $boxes);
        $this->assertSame(3.0, $boxes[0]->width);
    }

    /**
     * A malformed box is skipped and its neighbours survive.
     *
     * Refusing the whole page over one bad float would make the citation LESS inspectable -- the
     * document is the point, and it is worth showing with the boxes that did parse.
     */
    public function testSkipsMalformedBoxesButKeepsValidNeighbours(): void
    {
        $boxes = SourceBoxCodec::decode('10,20,100,12;garbage;30,60,50,14');

        $this->assertCount(2, $boxes);
        $this->assertSame(10.0, $boxes[0]->x);
        $this->assertSame(30.0, $boxes[1]->x);
    }

    /**
     * Non-numeric and non-finite edges are rejected rather than cast.
     *
     * This is the case a bare `(float)` cast gets wrong and silently: PHP turns "abc" into 0.0 and
     * "1e999" into INF, so without the is_numeric gate the viewer would draw an invented rectangle
     * at the page origin, or one with infinite extent, and present it as the cited evidence.
     */
    #[DataProvider('malformedEdgeProvider')]
    public function testRejectsEdgesACastWouldSilentlyInvent(string $packed): void
    {
        $this->assertSame([], SourceBoxCodec::decode($packed));
    }

    /**
     * @return array<string, array{string}>
     *
     * @codeCoverageIgnore Data providers run before coverage instrumentation starts.
     */
    public static function malformedEdgeProvider(): array
    {
        return [
            'non-numeric text' => ['abc,20,100,12'],
            'empty edge' => [',20,100,12'],
            'overflow to INF' => ['1e999,20,100,12'],
            'NaN literal' => ['NaN,20,100,12'],
            'too few edges' => ['10,20,100'],
            // Five fields is the LABELLED form (x,y,w,h,n); six is still malformed.
            'too many edges' => ['10,20,100,12,99,7'],
            'fractional label' => ['10,20,100,12,1.5'],
            'zero label' => ['10,20,100,12,0'],
            'negative label' => ['10,20,100,12,-1'],
            'zero width' => ['10,20,0,12'],
            'negative height' => ['10,20,100,-12'],
            'negative origin' => ['-10,20,100,12'],
        ];
    }

    /**
     * A fifth field carries the badge number the sidebar assigned the box's fact.
     *
     * The viewer used to derive that number from the box's position in the array, which agreed with
     * the sidebar only while every fact contributed exactly one box. Carrying it explicitly is what
     * lets one fact own several boxes -- a value and the reference range qualifying it -- without
     * every badge after it renumbering itself.
     */
    public function testDecodesTheBadgeLabelFromAFifthField(): void
    {
        $boxes = SourceBoxCodec::decode('10,20,100,12,3;30,60,50,14,7');

        $this->assertCount(2, $boxes);
        $this->assertSame(3, $boxes[0]->label);
        $this->assertSame(7, $boxes[1]->label);
        $this->assertSame(['x' => 10.0, 'y' => 20.0, 'w' => 100.0, 'h' => 12.0, 'n' => 3], $boxes[0]->toViewArray());
    }

    /**
     * An unlabelled four-field box still decodes, and omits `n` entirely.
     *
     * The single-box `View source` path and any older link still pack four fields, so the legacy
     * contract has to keep producing exactly the array the viewer consumed before.
     */
    public function testUnlabelledBoxesKeepTheLegacyViewShape(): void
    {
        $boxes = SourceBoxCodec::decode('10,20,100,12');

        $this->assertCount(1, $boxes);
        $this->assertNull($boxes[0]->label);
        $this->assertSame(['x' => 10.0, 'y' => 20.0, 'w' => 100.0, 'h' => 12.0], $boxes[0]->toViewArray());
    }

    /**
     * Absent, blank, and separator-only input all yield no overlay rather than an error.
     */
    #[DataProvider('emptyInputProvider')]
    public function testEmptyInputYieldsNoBoxes(?string $packed): void
    {
        $this->assertSame([], SourceBoxCodec::decode($packed));
    }

    /**
     * @return array<string, array{string|null}>
     *
     * @codeCoverageIgnore Data providers run before coverage instrumentation starts.
     */
    public static function emptyInputProvider(): array
    {
        return [
            'absent' => [null],
            'blank' => [''],
            'whitespace' => ['   '],
            'separators only' => [';;;'],
        ];
    }

    /**
     * A trailing separator is tolerated -- it is a naturally-generated shape, not an error.
     */
    public function testToleratesTrailingSeparator(): void
    {
        $boxes = SourceBoxCodec::decode('10,20,100,12;');

        $this->assertCount(1, $boxes);
    }

    /**
     * A hand-crafted URL cannot inflate the DOM with thousands of positioned nodes.
     */
    public function testCapsTheNumberOfBoxes(): void
    {
        $packed = implode(';', array_fill(0, 200, '10,20,100,12'));

        $this->assertCount(50, SourceBoxCodec::decode($packed));
    }
}
