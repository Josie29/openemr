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

use JsonException;

/**
 * The pixel rectangle a derived fact was read from, in PDF points (scale-1 space).
 *
 * This is the geometry `source-view.php` renders the click-to-source highlight from. It has no
 * native home anywhere in OpenEMR's schema or in FHIR, which is why the extraction sidecar exists.
 */
final readonly class BoundingBox
{
    public function __construct(
        public float $x,
        public float $y,
        public float $width,
        public float $height,
    ) {
        // A zero-area box would render as an invisible highlight — a citation that silently points
        // nowhere. Refuse it here rather than let it reach the viewer.
        if ($width <= 0.0 || $height <= 0.0) {
            throw new \DomainException('A bounding box must have positive width and height.');
        }
        if ($x < 0.0 || $y < 0.0) {
            throw new \DomainException('A bounding box origin must be non-negative.');
        }
    }

    /**
     * Parse the sidecar's stored JSON back into a box.
     *
     * @throws \DomainException When the JSON is malformed or the values are not a valid box.
     */
    public static function fromJson(string $json): self
    {
        try {
            $decoded = json_decode($json, true, 8, JSON_THROW_ON_ERROR);
        } catch (JsonException $e) {
            throw new \DomainException('Stored bounding box is not valid JSON.', previous: $e);
        }

        if (!is_array($decoded)) {
            throw new \DomainException('Stored bounding box is not an object.');
        }

        foreach (['x', 'y', 'w', 'h'] as $key) {
            if (!isset($decoded[$key]) || !is_numeric($decoded[$key])) {
                throw new \DomainException("Stored bounding box is missing numeric '$key'.");
            }
        }

        return new self(
            (float) $decoded['x'],
            (float) $decoded['y'],
            (float) $decoded['w'],
            (float) $decoded['h'],
        );
    }

    /**
     * Serialize for the sidecar's `bbox` column.
     *
     * Keys are the short form `source-view.php` already accepts as query params (x/y/w/h).
     *
     * @throws JsonException When encoding fails.
     */
    public function toJson(): string
    {
        return json_encode(
            ['x' => $this->x, 'y' => $this->y, 'w' => $this->width, 'h' => $this->height],
            JSON_THROW_ON_ERROR,
        );
    }
}
