from copilot.ingestion.geometry.spans import first_token, match_span, merge_and_pad, norm
from copilot.ingestion.geometry.words import Word, extract_word_boxes
from copilot.ingestion.schemas import BoundingBox

# Compatibility surface. The geometry implementation moved to `copilot.ingestion.geometry` (JOS-80)
# so box location is no longer welded to the lab-table layout; this module re-exports the names the
# rest of the tree still imports. `locate_value_box` is the lab row join expressed on the shared
# span matcher — kept here until callers move to the locator chain, then deleted.

__all__ = ["Word", "extract_word_boxes", "locate_value_box"]

# The test-name column sits at the left of a lab table; anything past this x (points) is a value,
# unit, or a mid-sentence mention in the interpretive narrative — never a row's leading test name.
# Requiring the anchor word to be left of this excludes the narrative "eGFR 78 -> 54" style mentions
# that repeat a test name (and its value) lower on the page.
_LEFT_MARGIN_MAX = 200.0
# Two words belong to the same visual row when their top edges are within this many points.
_ROW_TOLERANCE = 3.0


def locate_value_box(
    test_name: str, value: str, words: list[Word], start: int = 0
) -> tuple[BoundingBox, int] | None:
    """Find the tight box for ``value`` on the row anchored by ``test_name`` in the PDF text layer.

    The join that pairs a Mistral-extracted fact (``test_name`` + ``value``) with its exact pixel
    location: the **test name anchors the row** (it is the row's left-most token, so a duplicate
    value elsewhere on the page or the "prior result" column cannot be mistaken for it), then the
    ``value`` is boxed on that same row. Scanning forward from ``start`` and returning the next
    index walks the table top-to-bottom, so a repeated test name maps each occurrence to its row.

    Args:
        test_name: The analyte name as extracted (e.g. ``"Glucose, Fasting"``); its first token
            anchors the row.
        value: The verbatim result value to box (e.g. ``"1.44"``).
        words: The document's words in reading order (from :func:`extract_word_boxes`).
        start: Index to scan from — pass the previous call's cursor to consume rows in order.

    Returns:
        ``(box, next_start)`` — the value's box in points and the cursor to pass to the next call —
        or ``None`` when the row or the value on it cannot be located (the caller then falls back).
    """
    name_key = first_token(test_name)
    if not name_key or not norm(value):
        return None
    for index in range(start, len(words)):
        anchor = words[index]
        if anchor.x0 > _LEFT_MARGIN_MAX or norm(anchor.text) != name_key:
            continue
        # Words on the anchor's row, to the right of the test name (the result/flag/range columns),
        # ordered left-to-right: the result column precedes the "prior result" column, so matching
        # the left-most still boxes the current result when a value equals its own prior draw.
        row = sorted(
            (
                word
                for word in words
                if word.page == anchor.page
                and abs(word.top - anchor.top) <= _ROW_TOLERANCE
                and word.x0 > anchor.x1
            ),
            key=lambda word: word.x0,
        )
        span = match_span(row, value, max_span_words=1)
        if span is None:
            continue  # name matched but its value is not on this row — keep scanning
        return merge_and_pad(span), index + 1
    return None
