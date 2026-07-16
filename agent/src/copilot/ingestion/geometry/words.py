import io
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger("copilot")

# A checkbox on a printed form is a small near-square rule. These bounds (points) accept the usual
# 6-10pt box while rejecting a table cell, a field underline, or a section rule.
_CHECKBOX_MIN_SIDE = 4.0
_CHECKBOX_MAX_SIDE = 14.0
_CHECKBOX_SQUARENESS = 3.0
# Glyphs a form uses to tick a box. Containment does the real work — "TX" contains an X, so a mark
# only counts when its centre actually sits inside a box.
_MARK_GLYPHS = frozenset({"✕", "✗", "✘", "☒", "X", "x", "✓", "✔"})
# Slack (points) when testing whether a mark's centre falls in a box; a tick often overhangs.
_MARK_SLACK = 1.5


@dataclass(frozen=True, slots=True)
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


@dataclass(frozen=True, slots=True)
class Checkbox:
    """A tick box on a form: its square on the page (points), and whether it is marked.

    Load-bearing for grounding, not just geometry. A form preprints every option it offers, so the
    option's TEXT proves nothing — "Male" and "Female" are both on the page, and a condition is
    printed whether or not the patient claims it. Only the mark asserts the answer, and a mark is
    drawn as a glyph inside a box rule: invisible in the text layer's words, which is why the
    boxes are extracted separately from the page's rects.
    """

    x0: float
    top: float
    x1: float
    bottom: float
    page: int  # 1-based
    ticked: bool


def extract_checkboxes(pdf_bytes: bytes) -> list[Checkbox]:
    """Find every tick box in a digital PDF and decide whether each is marked.

    A box is a small near-square rect; it is ticked when a mark glyph's centre falls inside it.
    Testing containment rather than merely looking for the glyph is what keeps an unrelated "X"
    (in "TX 78745", say) from reading as a tick.

    Returns an empty list when the document has no vector rects (a scan), when ``pdfplumber`` is
    unavailable, or when the bytes cannot be parsed — the caller then has no checkbox evidence and
    treats checkbox-backed facts as unprovable rather than guessing.

    Args:
        pdf_bytes: The raw PDF bytes.

    Returns:
        Every checkbox found, in reading order, or ``[]``.
    """
    try:
        import pdfplumber
    except ImportError:
        logger.warning("pdfplumber not installed; no checkbox evidence available")
        return []
    boxes: list[Checkbox] = []
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page_index, page in enumerate(pdf.pages, start=1):
                marks = [
                    (
                        (float(c["x0"]) + float(c["x1"])) / 2,
                        (float(c["top"]) + float(c["bottom"])) / 2,
                    )
                    for c in page.chars
                    if str(c.get("text", "")) in _MARK_GLYPHS
                ]
                for rect in page.rects:
                    square = _as_square(rect)
                    if square is None:
                        continue
                    x0, top, x1, bottom = square
                    boxes.append(
                        Checkbox(
                            x0=x0,
                            top=top,
                            x1=x1,
                            bottom=bottom,
                            page=page_index,
                            ticked=any(
                                x0 - _MARK_SLACK <= mx <= x1 + _MARK_SLACK
                                and top - _MARK_SLACK <= my <= bottom + _MARK_SLACK
                                for mx, my in marks
                            ),
                        )
                    )
    except Exception:
        # pdfplumber/pdfminer raise a variety of parse errors; treat any as "no checkbox evidence".
        logger.warning("pdfplumber checkbox extraction failed", exc_info=True)
        return []
    return sorted(boxes, key=lambda box: (box.page, box.top, box.x0))


def _as_square(rect: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """Return a rect's corners when it is small and square enough to be a tick box, else None."""
    try:
        x0, top = float(rect["x0"]), float(rect["top"])
        x1, bottom = float(rect["x1"]), float(rect["bottom"])
    except (KeyError, TypeError, ValueError):
        return None
    width, height = x1 - x0, bottom - top
    if not (_CHECKBOX_MIN_SIDE < width < _CHECKBOX_MAX_SIDE):
        return None
    if not (_CHECKBOX_MIN_SIDE < height < _CHECKBOX_MAX_SIDE):
        return None
    if abs(width - height) > _CHECKBOX_SQUARENESS:
        return None
    return x0, top, x1, bottom


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
