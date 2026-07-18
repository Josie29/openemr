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
 * The demographic fields the agent reads off an intake form.
 *
 * Backed because the value arrives over the wire from the sidebar, matching the field names the
 * agent's `Demographics` model emits (`agent/src/copilot/ingestion/schemas.py`).
 *
 * `patientColumn()` returns the `patient_data` column a field overwrites, or null when the field is
 * not safely mappable in this version. `full_name` is null: splitting one printed name into
 * `fname`/`mname`/`lname` is lossy guesswork, so it is surfaced in the review card but not written
 * until the intake schema supplies a structured name (deferred, see the spec's §5.6).
 */
enum DemographicField: string
{
    case FullName = 'full_name';
    case DateOfBirth = 'date_of_birth';
    case Sex = 'sex';
    case Address = 'address';
    case Phone = 'phone';

    /** Human label for the review card. */
    public function label(): string
    {
        return match ($this) {
            self::FullName => 'Full name',
            self::DateOfBirth => 'Date of birth',
            self::Sex => 'Sex',
            self::Address => 'Address',
            self::Phone => 'Phone',
        };
    }

    /**
     * The `patient_data` column this field overwrites, or null when it is not written in this version.
     *
     * Address maps to `street` wholesale — coarse (city/state/zip stay on one line) but not lossy,
     * and it is what a clinician can correct in one place.
     */
    public function patientColumn(): ?string
    {
        return match ($this) {
            self::FullName => null,
            self::DateOfBirth => 'DOB',
            self::Sex => 'sex',
            self::Address => 'street',
            self::Phone => 'phone_home',
        };
    }
}
