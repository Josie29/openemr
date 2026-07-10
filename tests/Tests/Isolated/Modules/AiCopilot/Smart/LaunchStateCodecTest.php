<?php

/**
 * Isolated LaunchStateCodec Test
 *
 * @package   OpenEMR
 * @link      https://www.open-emr.org
 * @author    Josie Machalek <01josie@gmail.com>
 * @copyright Copyright (c) 2026 Josie Machalek
 * @license   https://github.com/openemr/openemr/blob/master/LICENSE GNU General Public License 3
 */

declare(strict_types=1);

namespace OpenEMR\Tests\Isolated\Modules\AiCopilot\Smart;

use DateTimeImmutable;
use OpenEMR\Common\Crypto\CryptoInterface;
use OpenEMR\Modules\AiCopilot\Exception\LaunchException;
use OpenEMR\Modules\AiCopilot\Smart\LaunchStateCodec;
use PHPUnit\Framework\TestCase;
use Psr\Clock\ClockInterface;

// The ai-copilot module is loaded by OpenEMR's runtime module system, not the root composer
// autoloader, so pull the classes under test in directly (same pattern as the FaxSMS isolated tests).
require_once __DIR__ . '/../../../../../../interface/modules/custom_modules/oe-module-ai-copilot/src/Exception/LaunchException.php';
require_once __DIR__ . '/../../../../../../interface/modules/custom_modules/oe-module-ai-copilot/src/Smart/LaunchStateCodec.php';

/**
 * Guards the launch->callback bridge that replaced the session-write SMART flow. If this breaks, the
 * co-pilot either loses the PKCE verifier mid-launch (no token issued) or accepts a forged/expired
 * `state`, which is the seam an attacker would use to bind the token to a patient they chose.
 */
final class LaunchStateCodecTest extends TestCase
{
    private const VERIFIER = 'v-abc123-code-verifier';
    private const PATIENT = 'a2340392-d8bc-46bb-8bc3-b844252bdd29';

    /**
     * The verifier and patient sealed at launch come back intact at the callback -- the whole point
     * of the bridge. Uses identity crypto so the assertion is about the codec, not the cipher.
     */
    public function testEncodeThenDecodeReturnsSealedValues(): void
    {
        $codec = new LaunchStateCodec($this->identityCrypto(), $this->clockAt('2026-07-10 12:00:00'));

        $state = $codec->encode(self::VERIFIER, self::PATIENT);

        $this->assertSame(
            ['codeVerifier' => self::VERIFIER, 'patientUuid' => self::PATIENT],
            $codec->decode($state),
        );
    }

    /**
     * A blob that fails authenticated decryption (tampered, wrong key, truncated) is rejected. This is
     * the check that a forged `state` cannot smuggle in an attacker-chosen patient.
     */
    public function testDecodeRejectsUnauthenticatedState(): void
    {
        $crypto = $this->createMock(CryptoInterface::class);
        $crypto->method('decryptStandard')->willReturn(false);
        $codec = new LaunchStateCodec($crypto, $this->clockAt('2026-07-10 12:00:00'));

        $this->expectException(LaunchException::class);
        $codec->decode('tampered-ciphertext');
    }

    /**
     * A blob older than its TTL is refused even though it decrypts cleanly -- bounding the replay
     * window on a launch URL captured from access logs or history.
     */
    public function testDecodeRejectsExpiredState(): void
    {
        $crypto = $this->identityCrypto();
        $atLaunch = new LaunchStateCodec($crypto, $this->clockAt('2026-07-10 12:00:00'));
        $state = $atLaunch->encode(self::VERIFIER, self::PATIENT);

        // Six minutes later: past the 5-minute TTL. A fresh codec models the callback request's clock.
        $atCallback = new LaunchStateCodec($crypto, $this->clockAt('2026-07-10 12:06:00'));

        $this->expectException(LaunchException::class);
        $atCallback->decode($state);
    }

    /**
     * Content that decrypts to something other than this codec's JSON shape is rejected, not fatal --
     * defends against a blob encrypted with our key but not minted by this codec.
     */
    public function testDecodeRejectsMalformedPayload(): void
    {
        $crypto = $this->createMock(CryptoInterface::class);
        $crypto->method('decryptStandard')->willReturn('not-our-json');
        $codec = new LaunchStateCodec($crypto, $this->clockAt('2026-07-10 12:00:00'));

        $this->expectException(LaunchException::class);
        $codec->decode('whatever');
    }

    /**
     * Identity crypto: encrypt and decrypt are pass-throughs so each test exercises the codec's own
     * logic (JSON build, shape validation, TTL) rather than CryptoGen, which has its own coverage.
     */
    private function identityCrypto(): CryptoInterface
    {
        $crypto = $this->createMock(CryptoInterface::class);
        $crypto->method('encryptStandard')->willReturnCallback(static fn(?string $value): string => (string) $value);
        $crypto->method('decryptStandard')->willReturnCallback(static fn(?string $value): string => (string) $value);

        return $crypto;
    }

    private function clockAt(string $time): ClockInterface
    {
        $clock = $this->createMock(ClockInterface::class);
        $clock->method('now')->willReturn(new DateTimeImmutable($time));

        return $clock;
    }
}
