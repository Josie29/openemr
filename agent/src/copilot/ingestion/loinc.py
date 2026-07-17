import re

# A numeric LOINC code: up to 5 digits, a hyphen, then one Mod 10 check digit. Codes with the "LP"
# (part) and "LA" (answer) alpha prefixes use a different Mod 10 variant and are not lab-result
# identifiers, so they are deliberately out of scope here.
_LOINC_PATTERN = re.compile(r"^(\d{1,5})-(\d)$")

# LOINC's own users' guide records these as a known defect: real, published terms whose printed
# check digit does not satisfy the algorithm. Refusing them would reject valid codes, so they are
# accepted on the authority of the source that defines the checksum in the first place.
# https://loinc.org/kb/users-guide/calculating-mod-10-check-digits/
_KNOWN_INVALID_CHECK_DIGITS = frozenset(
    {
        "11491-6",
        "11501-6",
        "5895-7",
        "5896-5",
        "5897-3",
        "5928-1",
        "6028-3",
        "6415-5",
        "6580-7",
        "6645-7",
        "6646-5",
        "6647-3",
        "6648-1",
        "6649-9",
    }
)


def check_digit(base: str) -> int:
    """Compute the Mod 10 check digit for a LOINC code's numeric part.

    Implements the eight-step algorithm from LOINC's users' guide verbatim, which phrases it
    unusually: the odd-position digits are concatenated into a *number* and that number is doubled
    (531 -> 1062), rather than each digit being doubled individually as in the familiar Luhn.

    The two are nevertheless **equivalent** — verified exhaustively over every 1-to-5 digit base in
    ``test_loinc`` (all 99,999 agree). Doubling a multi-digit number carries 10 into the next place,
    which costs the digit sum 9, for exactly the digits greater than 4 — the same digits where Luhn
    subtracts 9. Same arithmetic, different description. The guide's phrasing is followed here
    anyway, so this reads as its source does.

    Worked example from the guide, reproduced by ``test_loinc``: for "12345", the odd positions
    (right-to-left: 5, 3, 1) form 531, doubled to 1062; the even positions (4, 2) form 42; prepended
    gives 421062; the digits sum to 15; the next multiple of 10 is 20; the check digit is 5.

    Args:
        base: The numeric part of a LOINC code, without the hyphen or check digit.

    Returns:
        The check digit, 0-9.

    Raises:
        ValueError: If ``base`` is not a non-empty string of digits.
    """
    if not base or not base.isdigit():
        raise ValueError(f"LOINC base must be digits, got {base!r}")

    reversed_digits = base[::-1]
    odd_positions = reversed_digits[0::2]  # positions 1, 3, 5... counting from the right
    even_positions = reversed_digits[1::2]  # positions 2, 4, 6...
    # Concatenate-then-double, per the guide — doubling each digit separately gives a different
    # (wrong) answer for any base with more than one odd-position digit.
    doubled = str(int(odd_positions) * 2)
    total = sum(int(char) for char in even_positions + doubled)
    return (10 - (total % 10)) % 10


def is_valid(code: str) -> bool:
    """Report whether a string is a well-formed numeric LOINC code with a correct check digit.

    This validates the code's *integrity*, not its meaning: it catches an OCR misread (one wrong
    digit fails the checksum ~90% of the time) but cannot tell whether the code names the analyte
    printed beside it. A checksum-valid code is still only as trustworthy as the page it was read
    from.

    Args:
        code: The candidate code, e.g. "2823-3".

    Returns:
        True when the code parses and its check digit is correct, or when it is one of the 14 terms
        LOINC publishes with a known-bad check digit.
    """
    match = _LOINC_PATTERN.match(code.strip())
    if match is None:
        return False
    if code.strip() in _KNOWN_INVALID_CHECK_DIGITS:
        return True

    base, printed = match.groups()
    return check_digit(base) == int(printed)


def parse(raw: str | None) -> str | None:
    """Normalize an extracted LOINC code, returning None for anything untrustworthy.

    A wrong LOINC silently mislabels *which test was run* — worse than no code at all, because a
    downstream consumer has no way to tell a misread code from a correct one. So this refuses rather
    than repairs: a code that does not validate is discarded, never "corrected" to a nearby one.

    Args:
        raw: The code as the extractor read it off the page, or None if the page printed none.

    Returns:
        The trimmed code when it is well-formed and passes its check digit; None otherwise.
    """
    if raw is None:
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    return candidate if is_valid(candidate) else None
