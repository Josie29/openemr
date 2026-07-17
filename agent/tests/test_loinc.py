import pytest

from copilot.ingestion.loinc import check_digit, is_valid, parse


class TestCheckDigit:
    """Without a correct check digit, a misread code is indistinguishable from a real one."""

    def test_reproduces_the_users_guide_worked_example(self) -> None:
        """The guide's own example (12345 -> 5). If this breaks, the algorithm is wrong and every
        validation verdict below is meaningless."""
        assert check_digit("12345") == 5

    @pytest.mark.parametrize(
        "code",
        [
            "1558-6",  # Fasting glucose
            "2823-3",  # Potassium
            "2951-2",  # Sodium
            "17861-6",  # Calcium
            "98979-8",  # eGFR CKD-EPI 2021
            "706-2",  # Basophils/100 leukocytes
            "789-8",  # Erythrocytes
            "3097-3",  # BUN/Creatinine ratio
        ],
    )
    def test_accepts_real_loinc_codes(self, code: str) -> None:
        """Real codes sourced from loinc.org panel pages — these are the ones printed on the lab
        fixture, so a regression here would reject the whole report."""
        base, printed = code.split("-")
        assert check_digit(base) == int(printed)

    def test_agrees_with_luhn_across_the_whole_loinc_code_space(self) -> None:
        """LOINC's guide phrases the algorithm as concatenate-then-double, which looks unlike the
        familiar per-digit Luhn — so a reader may "fix" it into Luhn, or doubt this implementation
        because it doesn't look like Luhn. Both are safe: the two agree on every 1-to-5 digit base
        there is. Doubling a multi-digit number carries 10 into the next place, costing the digit
        sum 9 for exactly the digits >4 — the same digits Luhn adjusts by 9.
        """

        def luhn_style(base: str) -> int:
            total = 0
            for i, char in enumerate(base[::-1]):
                n = int(char)
                if i % 2 == 0:
                    n *= 2
                    if n > 9:
                        n -= 9
                total += n
            return (10 - (total % 10)) % 10

        # Exhaustive: LOINC numeric codes are at most 5 digits, so this IS the whole space.
        mismatches = [str(n) for n in range(1, 100000) if check_digit(str(n)) != luhn_style(str(n))]
        assert mismatches == []

    def test_rejects_a_non_numeric_base(self) -> None:
        with pytest.raises(ValueError):
            check_digit("LP123")


class TestIsValid:
    def test_rejects_a_single_transposed_digit(self) -> None:
        """The whole point: catch an OCR misread. 2823-3 is potassium; 2832-3 is not, and would
        silently mislabel which test was run."""
        assert is_valid("2823-3")
        assert not is_valid("2832-3")

    @pytest.mark.parametrize(
        "code", ["", "abc", "2823", "2823-", "-3", "2823-33", "123456-7", "LP12345-6"]
    )
    def test_rejects_malformed_input(self, code: str) -> None:
        assert not is_valid(code)

    @pytest.mark.parametrize("code", ["11491-6", "5895-7", "6649-9"])
    def test_accepts_the_terms_loinc_publishes_with_bad_check_digits(self, code: str) -> None:
        """LOINC's users' guide lists 14 real terms whose check digit is wrong — a known defect on
        their side. Rejecting them would refuse valid codes that appear on real reports."""
        assert is_valid(code)


class TestParse:
    def test_returns_a_valid_code_trimmed(self) -> None:
        assert parse("  2823-3 ") == "2823-3"

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_treats_an_absent_code_as_absent(self, raw: str | None) -> None:
        """A report that prints no code is normal and must not be an error — the result is still a
        usable fact, it just cannot be written back."""
        assert parse(raw) is None

    def test_discards_rather_than_repairs_a_bad_code(self) -> None:
        """Refusing is the whole design: a 'corrected' code would be a fabricated clinical
        assertion, and downstream could not tell it from one actually read off the page."""
        assert parse("2832-3") is None
