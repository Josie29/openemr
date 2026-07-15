import io
import logging
from dataclasses import dataclass

from copilot.ingestion.schemas import BoundingBox

logger = logging.getLogger("copilot")

# The test-name column sits at the left of a lab table; anything past this x (points) is a value,
# unit, or a mid-sentence mention in the interpretive narrative — never a row's leading test name.
# Requiring the anchor word to be left of this excludes the narrative "eGFR 78 -> 54" style mentions
# that repeat a test name (and its value) lower on the page.
_LEFT_MARGIN_MAX = 200.0
# Two words belong to the same visual row when their top edges are within this many points.
_ROW_TOLERANCE = 3.0
# Breathing room (points) added around a word's glyph box so the highlight frames the value
# instead of clipping its ascenders/descenders. Small enough to stay clear of adjacent rows
# (lab rows are ~12-13 pt apart, a padded value box ~11 pt tall).
_BOX_PADDING = 2.0


@dataclass(frozen=True)
class Word:
    """One text token from a PDF's text layer, with its box in PDF points (top-left origin).

    ``pdfplumber`` reports coordinates directly in points on a top-left origin — the exact space the
    click-to-source overlay renders in — so no DPI conversion is ever needed for a digital PDF.
    """

    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    page: int  # 1-based


def extract_word_boxes(pdf_bytes: bytes) -> list[Word]:
    """Extract every word from a digital PDF's text layer, in reading order, boxed in points.

    Returns an empty list when the document has no text layer (a scanned/image-only PDF), when
    ``pdfplumber`` is unavailable, or when the bytes cannot be parsed — the caller then falls back
    to the coarse OCR row-estimate rather than failing.

    Args:
        pdf_bytes: The raw PDF bytes.

    Returns:
        Words across all pages in reading order (page, then top, then left), or ``[]``.
    """
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed; using the OCR row-estimate for box geometry")
        return []
    words: list[Word] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page_index, page in enumerate(pdf.pages, start=1):
                for raw in page.extract_words():
                    words.append(
                        Word(
                            text=str(raw["text"]),
                            x0=float(raw["x0"]),
                            top=float(raw["top"]),
                            x1=float(raw["x1"]),
                            bottom=float(raw["bottom"]),
                            page=page_index,
                        )
                    )
    except Exception:
        # pdfplumber/pdfminer raise a variety of parse errors; treat any as "no text layer".
        logger.warning(
            "pdfplumber word extraction failed; using the OCR row-estimate", exc_info=True
        )
        return []
    return words


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
    name_key = _first_token(test_name)
    value_key = _norm(value)
    if not name_key or not value_key:
        return None
    for index in range(start, len(words)):
        anchor = words[index]
        if anchor.x0 > _LEFT_MARGIN_MAX or _norm(anchor.text) != name_key:
            continue
        # Words on the anchor's row, to the right of the test name (the result/flag/range columns).
        row_matches = [
            word
            for word in words
            if word.page == anchor.page
            and abs(word.top - anchor.top) <= _ROW_TOLERANCE
            and word.x0 > anchor.x1
            and _norm(word.text) == value_key
        ]
        if not row_matches:
            continue  # name matched but its value is not on this row — keep scanning
        # Prefer the left-most match: the result column precedes the "prior result" column, so if a
        # value equals its own prior draw this still boxes the current result, not the prior one.
        target = min(row_matches, key=lambda word: word.x0)
        box = BoundingBox(
            page=target.page,
            x=max(target.x0 - _BOX_PADDING, 0.0),
            y=max(target.top - _BOX_PADDING, 0.0),
            width=max(target.x1 - target.x0, 1.0) + 2 * _BOX_PADDING,
            height=max(target.bottom - target.top, 1.0) + 2 * _BOX_PADDING,
        )
        return box, index + 1
    return None


def _first_token(text: str) -> str:
    """The normalized first whitespace-delimited token of a name (anchors a multi-word name).

    Normalizes the token itself, so a trailing comma on a leading word ("Glucose," in "Glucose,
    Fasting") is stripped to match the bare anchor word the PDF text layer reports.
    """
    parts = str(text).split()
    return _norm(parts[0]) if parts else ""


def _norm(text: str) -> str:
    """Lowercase, collapse whitespace, and strip surrounding punctuation for tolerant matching."""
    collapsed = " ".join(str(text).split()).lower()
    return collapsed.strip(".,:;()[]")
