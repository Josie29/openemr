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
 * One click-to-source rectangle on a document page, in PDF user-space points (72-DPI, top-left
 * origin) -- the exact space the viewer renders in, so it maps straight onto the page with no
 * conversion.
 *
 * A parsed value object rather than a bare float array: the viewer draws whatever it is handed, so
 * the invariants are enforced once here at the boundary and every downstream reader can trust them.
 * A zero-area or negative box is not a box -- the extractor never emits one (its `BoundingBox`
 * requires `width`/`height` > 0), so one arriving over the URL is malformed input, not geometry.
 */
final readonly class SourceBox
{
    /**
     * @param float    $x      Left edge in PDF points.
     * @param float    $y      Top edge in PDF points.
     * @param float    $width  Box width in PDF points; must be positive.
     * @param float    $height Box height in PDF points; must be positive.
     * @param int|null $label  The badge number this box shows, matching the sidebar's numbered fact
     *                         list. Carried explicitly because the viewer used to derive it from
     *                         the box's array position, which only agreed with the sidebar while
     *                         each fact contributed exactly one box. Null keeps the legacy
     *                         four-field contract, where position is still used.
     *
     * @throws \DomainException If any edge is non-finite, an origin is negative, an extent is not
     *                          positive, or the label is not a positive number.
     */
    public function __construct(
        public float $x,
        public float $y,
        public float $width,
        public float $height,
        public ?int $label = null,
    ) {
        foreach (['x' => $x, 'y' => $y, 'width' => $width, 'height' => $height] as $name => $value) {
            if (!is_finite($value)) {
                throw new \DomainException("Box {$name} must be finite.");
            }
        }
        if ($x < 0 || $y < 0) {
            throw new \DomainException('Box origin must not be negative.');
        }
        if ($width <= 0 || $height <= 0) {
            throw new \DomainException('Box extent must be positive.');
        }
        if ($label !== null && $label < 1) {
            throw new \DomainException('Box label must be a positive number.');
        }
    }

    /**
     * The shape the viewer's JS consumes, matching the keys the single-box contract already used.
     *
     * `n` is omitted when unlabelled, so a legacy four-field URL decodes to exactly the array the
     * viewer consumed before.
     *
     * @return array{x: float, y: float, w: float, h: float, n?: int}
     */
    public function toViewArray(): array
    {
        $view = ['x' => $this->x, 'y' => $this->y, 'w' => $this->width, 'h' => $this->height];
        if ($this->label !== null) {
            $view['n'] = $this->label;
        }

        return $view;
    }
}
