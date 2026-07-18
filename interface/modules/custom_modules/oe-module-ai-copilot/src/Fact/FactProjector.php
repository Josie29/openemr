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
 * Persists one fact family into its native OpenEMR destination.
 *
 * This is the seam that replaced the hardcoded family-by-family chain in the persister: every fact
 * kind the agent extracts now has a projector that owns (a) which {@see FactType}s it handles, (b)
 * whether it writes automatically or behind an accept gate ({@see ProjectionMode}), and (c) the write
 * itself. Adding a new kind is registering a projector, not editing the persister, the parser's
 * dispatch, and the endpoint in three places.
 *
 * A projector's `write()` runs in its own transaction (managed inside the writer it delegates to) and
 * may throw — the persister catches per-projector so one family's failure never discards another's
 * committed rows.
 */
interface FactProjector
{
    /**
     * A short, stable family label used in failure reporting and logs (e.g. 'labs', 'intake',
     * 'family_history'). Kept stable because the endpoint surfaces it to the sidebar's failed line.
     */
    public function familyName(): string;

    /**
     * The fact types this projector consumes from a parsed payload.
     *
     * @return list<FactType>
     */
    public function handles(): array;

    /** Whether this family writes on arrival or only after a clinician accept. */
    public function mode(): ProjectionMode;

    /** True when the payload carries at least one fact this projector would write. */
    public function hasWork(ParsedFacts $parsed): bool;

    /**
     * Persist this family's facts from the payload.
     *
     * @throws \Throwable When the write fails; the persister catches it and records the family as
     *                    failed. Writers convert expected DB failures to `\RuntimeException`.
     */
    public function write(ParsedFacts $parsed, ProjectionRequest $request): FamilyOutcome;
}
