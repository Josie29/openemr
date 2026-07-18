<?php

/**
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Modules\AiCopilot\Source;

/**
 * Decodes the `boxes` URL parameter -- every click-to-source rectangle cited on one document page --
 * into typed {@see SourceBox} values (JOS-88).
 *
 * Packed as one scalar (`x,y,w,h;x,y,w,h`) rather than `boxes[]`, because `filter_input(INPUT_GET,
 * ...)` reads scalars: an array parameter would need `FILTER_REQUIRE_ARRAY` or raw `$_GET` handling,
 * cutting against how the viewer reads every other parameter. Order is meaningful -- it is the number
 * badge the viewer draws and the sidebar's fact list mirrors.
 *
 * A malformed box is SKIPPED, not fatal. That matches the single-box behaviour this replaces (a bad
 * rectangle yielded `$hasBox = false` and the page still rendered): the source document is the point,
 * and it is worth showing without an overlay. Refusing the whole page over one bad float would make
 * the citation less inspectable, not more.
 *
 * Parsing lives here rather than inline in the page so it is unit-testable without a bootstrap --
 * the same reason {@see \OpenEMR\Modules\AiCopilot\Smart\LaunchStateCodec} was factored out of
 * launch.php.
 */
final readonly class SourceBoxCodec
{
    /** Separates one box from the next. */
    private const BOX_SEPARATOR = ';';

    /** Separates a box's four edges. */
    private const EDGE_SEPARATOR = ',';

    /**
     * Upper bound on boxes drawn for one page. A cited page carries a handful; this only exists so a
     * hand-crafted URL cannot inflate the DOM with thousands of absolutely-positioned nodes.
     */
    private const MAX_BOXES = 50;

    /**
     * Decode the packed `boxes` parameter into rectangles, in the order they were given.
     *
     * @param string|null $packed The raw parameter (`x,y,w,h;x,y,w,h`), or null when absent.
     *
     * @return list<SourceBox> The valid boxes, capped at self::MAX_BOXES. Empty when the parameter
     *                         is absent, blank, or wholly malformed -- the viewer then renders the
     *                         page with no overlay.
     */
    public static function decode(?string $packed): array
    {
        if ($packed === null || trim($packed) === '') {
            return [];
        }

        $boxes = [];
        foreach (explode(self::BOX_SEPARATOR, $packed) as $chunk) {
            if (count($boxes) >= self::MAX_BOXES) {
                break;
            }
            $box = self::decodeOne($chunk);
            if ($box !== null) {
                $boxes[] = $box;
            }
        }

        return $boxes;
    }

    /**
     * Decode one `x,y,w,h` or `x,y,w,h,n` chunk.
     *
     * The optional fifth field is the badge number the box shows. Four fields stay valid so an
     * older link (or the single-box `View source` path) keeps working unchanged.
     *
     * @param string $chunk One box's comma-separated edges, optionally followed by its label.
     *
     * @return SourceBox|null Null when the chunk is not four or five numbers describing a real
     *                        rectangle.
     */
    private static function decodeOne(string $chunk): ?SourceBox
    {
        $chunk = trim($chunk);
        if ($chunk === '') {
            return null; // a trailing or doubled separator, not an error
        }

        $fields = explode(self::EDGE_SEPARATOR, $chunk);
        if (count($fields) !== 4 && count($fields) !== 5) {
            return null;
        }

        $values = [];
        foreach ($fields as $field) {
            $field = trim($field);
            // is_numeric FIRST: a bare (float) cast turns "abc" into 0.0 and "1e999" into INF, so
            // casting without this gate would silently invent a rectangle at the origin.
            if (!is_numeric($field)) {
                return null;
            }
            $values[] = (float) $field;
        }

        // A label is a badge ordinal, so a fractional or out-of-range one is malformed, not
        // something to round into a plausible number.
        $label = null;
        if (count($values) === 5) {
            if ($values[4] != (int) $values[4]) {
                return null;
            }
            $label = (int) $values[4];
        }

        try {
            return new SourceBox($values[0], $values[1], $values[2], $values[3], $label);
        } catch (\DomainException) {
            return null; // non-finite, negative origin, zero-area, or a non-positive label
        }
    }
}
