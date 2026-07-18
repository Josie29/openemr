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
 * Builds the stable identity a fact is recorded under in the extraction sidecar.
 *
 * The identity lands in `ai_copilot_document_facts.field`, which is part of that table's UNIQUE key,
 * so it must be **stable across re-extraction** — re-reading the same document must produce the same
 * key or the upsert becomes an insert and citations accumulate duplicates.
 */
final readonly class FactIdentity
{
    /** Matches the `field` column width. */
    private const MAX_LENGTH = 64;

    /** Leaves room for '-' plus an 8-char digest when a value has to be shortened. */
    private const TRUNCATED_PREFIX_LENGTH = self::MAX_LENGTH - 9;

    /**
     * A stable, human-readable key such as `allergy:penicillin` or `medication:metformin`.
     *
     * Lower-cased and whitespace-collapsed so that trivial extraction differences ("Penicillin " vs
     * "penicillin") resolve to the same fact rather than a second citation row.
     *
     * @param string $kind The fact family — 'allergy' or 'medication'.
     * @param string $value The clinical identity (substance or drug name).
     */
    public static function for(string $kind, string $value): string
    {
        $normalized = strtolower(trim((string) preg_replace('/\s+/', ' ', $value)));
        $key = $kind . ':' . $normalized;

        if (strlen($key) <= self::MAX_LENGTH) {
            return $key;
        }

        // A plain truncation could collapse two genuinely different long substances onto one key,
        // silently merging their citations. The digest keeps a shortened key unique to its source.
        $digest = substr(hash('sha256', $key), 0, 8);

        return substr($key, 0, self::TRUNCATED_PREFIX_LENGTH) . '-' . $digest;
    }
}
