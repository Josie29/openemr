from collections.abc import Sequence

from copilot.ingestion.geometry.words import Word
from copilot.ingestion.schemas import BoundingBox

# Breathing room (points) added around a word's glyph box so the highlight frames the value
# instead of clipping its ascenders/descenders. Small enough to stay clear of adjacent rows
# (lab rows are ~12-13 pt apart, a padded value box ~11 pt tall).
BOX_PADDING = 2.0


# Punctuation stripped from the ENDS of a value before matching. Quotes are in the set because a
# form prints a quoted answer with typographic quotes (“…”) while the extractor echoes it with
# straight ones ('…') — a difference in the delimiter only, which would otherwise sink an
# otherwise-verbatim 28-word span and drop the fact. Only the ends are stripped, so punctuation
# INSIDE a value stays significant.
_STRIPPABLE = ".,:;()[]\"'“”‘’"


def norm(text: str) -> str:
    """Lowercase, collapse whitespace, and strip surrounding punctuation for tolerant matching.

    Absorbs the differences between how a form prints a value and how the extractor reports it: a
    label in caps ("DATE OF BIRTH") or with a trailing colon ("Date of Birth:") folds to one key,
    and a quoted free-text answer matches whichever quote glyphs each side used.
    """
    collapsed = " ".join(str(text).split()).lower()
    return collapsed.strip(_STRIPPABLE)


def first_token(text: str) -> str:
    """The normalized first whitespace-delimited token of a name (anchors a multi-word name).

    Normalizes the token itself, so a trailing comma on a leading word ("Glucose," in "Glucose,
    Fasting") is stripped to match the bare anchor word the PDF text layer reports.
    """
    parts = str(text).split()
    return norm(parts[0]) if parts else ""


def merge_and_pad(span: Sequence[Word], padding: float = BOX_PADDING) -> BoundingBox:
    """Merge one or more words into a single padded box.

    Args:
        span: The words to enclose; must be non-empty and on one page.
        padding: Breathing room added on every side, in points.

    Returns:
        The enclosing :class:`BoundingBox` in PDF points.

    Raises:
        ValueError: If ``span`` is empty.
    """
    if not span:
        raise ValueError("cannot merge an empty span")
    x0 = min(word.x0 for word in span)
    x1 = max(word.x1 for word in span)
    top = min(word.top for word in span)
    bottom = max(word.bottom for word in span)
    return BoundingBox(
        page=span[0].page,
        x=max(x0 - padding, 0.0),
        y=max(top - padding, 0.0),
        width=max(x1 - x0, 1.0) + 2 * padding,
        height=max(bottom - top, 1.0) + 2 * padding,
    )


def match_span(
    words: Sequence[Word], value: str, max_span_words: int = 1
) -> tuple[Word, ...] | None:
    """Find the left-most contiguous run of words whose joined text equals ``value``.

    The one span matcher every text-derived locator shares. ``max_span_words`` is what lets the same
    function serve both document types: at 1 it degenerates to the single-word equality the lab
    table join has always done, so the lab path is unchanged **by construction**; intake raises it
    so a multi-word value ("2117 Cypress Bend Dr, Austin, TX 78745") merges into one box.

    Left-most wins because a lab row prints the current result before the "prior result" column, so
    a value equal to its own prior draw still boxes the current one.

    Args:
        words: The candidate words, already narrowed to one scope and in reading order.
        value: The verbatim value to match, normalized before comparison.
        max_span_words: The most words that may be joined to form the value.

    Returns:
        The matching words in order, or None when the value is not present in ``words``.
    """
    target = norm(value)
    if not target or max_span_words < 1:
        return None
    for start in range(len(words)):
        # A span cannot straddle a page, and joining beyond the run length is wasted work.
        limit = min(max_span_words, len(words) - start)
        for length in range(1, limit + 1):
            span = tuple(words[start : start + length])
            if span[-1].page != span[0].page:
                break
            if norm(" ".join(word.text for word in span)) == target:
                return span
    return None
