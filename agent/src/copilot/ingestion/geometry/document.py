from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Any

from copilot.ingestion.errors import ExtractionError
from copilot.ingestion.geometry.spans import norm
from copilot.ingestion.geometry.words import Checkbox, Word, extract_checkboxes, extract_word_boxes
from copilot.ingestion.schemas import BoundingBox

_POINTS_PER_INCH = 72.0


@dataclass(frozen=True, slots=True)
class PageDims:
    """One page's extent, in PDF points."""

    page: int  # 1-based
    width: float
    height: float

    @property
    def box(self) -> BoundingBox:
        """The whole page as a box — the coarsest possible fallback."""
        return BoundingBox(page=self.page, x=0, y=0, width=self.width, height=self.height)


@dataclass(frozen=True, slots=True)
class OcrTable:
    """A table the OCR found on a page: its block box (points) and its parsed rows.

    Mistral returns whole-table geometry, not per-cell boxes, so the box locates the block and the
    rows carry the text — banding the block's y-range by row count is how a row is placed when the
    text layer cannot pin the value exactly.
    """

    page: int
    box: BoundingBox | None
    rows: tuple[tuple[str, ...], ...]

    def row_index_of(self, key: str) -> int | None:
        """The visual index of the row whose first cell matches ``key``, or None.

        Args:
            key: The row's leading cell text (a lab test name).
        """
        target = norm(key)
        for index, cells in enumerate(self.rows):
            if cells and norm(cells[0]) == target:
                return index
        return None


@dataclass(frozen=True, slots=True)
class DocumentGeometry:
    """Every source of box evidence for one document, normalized to PDF points, built once.

    The seam that stops each locator re-deriving geometry and re-doing unit conversion: the OCR
    reports native pixels at some DPI, the PDF text layer reports points, and the click-to-source
    overlay renders in points. Converting **here, once**, means no locator downstream ever sees a
    DPI, and every box that leaves this layer is already in the space the overlay draws in.
    """

    words: tuple[Word, ...]
    tables: tuple[OcrTable, ...]
    pages: tuple[PageDims, ...]
    checkboxes: tuple[Checkbox, ...] = ()
    # False when the checkbox detector could not RUN (pdfplumber missing, or unparseable bytes) —
    # distinct from running and finding none. A locator that owns a checkbox-gated field must refuse
    # rather than defer to a text match when this is False, or it boxes the preprinted option.
    checkboxes_available: bool = True

    def page(self, page: int) -> PageDims | None:
        """The dims for a 1-based page number, or None when the document has no such page."""
        return next((dims for dims in self.pages if dims.page == page), None)

    def table_on(self, page: int) -> OcrTable | None:
        """The first table on a 1-based page number, or None."""
        return next((table for table in self.tables if table.page == page), None)

    @classmethod
    def from_document(cls, pdf_bytes: bytes, ocr: dict[str, Any]) -> "DocumentGeometry":
        """Build the geometry for a document from its bytes and its OCR response.

        The production entry point: it reads both text-layer sources (words and checkboxes) off the
        bytes and folds in the OCR's coarse page/table geometry.

        Args:
            pdf_bytes: The raw PDF bytes.
            ocr: The raw OCR response dict.

        Returns:
            The normalized :class:`DocumentGeometry`, every box in PDF points.

        Raises:
            ExtractionError: If the response carries no usable page.
        """
        detected = extract_checkboxes(pdf_bytes)
        return cls.from_parts(
            ocr,
            extract_word_boxes(pdf_bytes),
            checkboxes=detected if detected is not None else [],
            checkboxes_available=detected is not None,
        )

    @classmethod
    def from_parts(
        cls,
        ocr: dict[str, Any],
        words: list[Word],
        checkboxes: list[Checkbox] | None = None,
        checkboxes_available: bool = True,
    ) -> "DocumentGeometry":
        """Build the geometry from a raw OCR response plus already-extracted text-layer evidence.

        Args:
            ocr: The raw OCR response dict (``pages[].dimensions``/``blocks``/``tables``).
            words: The text-layer words in points; empty for a scanned/image-only PDF, in which
                case only the OCR's coarse table/page geometry is available.
            checkboxes: The page's tick boxes, when the document has any.
            checkboxes_available: False when the checkbox detector could not run, so a
                checkbox-gated field must refuse rather than fall back to a text match.

        Returns:
            The normalized :class:`DocumentGeometry`, every box in PDF points.

        Raises:
            ExtractionError: If the response carries no usable page.
        """
        raw_pages = ocr.get("pages") or []
        if not raw_pages or not isinstance(raw_pages[0], dict):
            raise ExtractionError("OCR response carries no usable page")
        pages: list[PageDims] = []
        tables: list[OcrTable] = []
        for raw_page in raw_pages:
            if not isinstance(raw_page, dict):
                continue
            page_no = int(raw_page.get("index", 0)) + 1  # `index` is 0-based; pages are 1-based.
            dims = raw_page.get("dimensions") or {}
            scale = _points_per_pixel(dims)
            page_dims = _page_dims(dims, page_no, scale)
            if page_dims is not None:
                pages.append(page_dims)
            tables.append(
                OcrTable(
                    page=page_no,
                    box=_table_box(raw_page, page_no, scale),
                    rows=_parse_table_rows(_table_html(raw_page)),
                )
            )
        return cls(
            words=tuple(words),
            tables=tuple(tables),
            pages=tuple(pages),
            checkboxes=tuple(checkboxes or ()),
            checkboxes_available=checkboxes_available,
        )


def _points_per_pixel(dims: dict[str, Any]) -> float:
    """The factor converting the OCR's native pixels to PDF points: ``point = pixel * 72 / dpi``."""
    dpi = float(dims.get("dpi") or _POINTS_PER_INCH)
    return _POINTS_PER_INCH / dpi if dpi else 1.0


def _page_dims(dims: dict[str, Any], page_no: int, scale: float) -> PageDims | None:
    """One page's extent in points, or None when the response carries no usable dimensions.

    Returning None (rather than a zero-extent page) keeps the caller from emitting a degenerate box
    the schema would reject.
    """
    width = dims.get("width")
    height = dims.get("height")
    if not (
        isinstance(width, (int, float))
        and isinstance(height, (int, float))
        and width > 0
        and height > 0
    ):
        return None
    return PageDims(page=page_no, width=float(width) * scale, height=float(height) * scale)


def _table_box(raw_page: dict[str, Any], page_no: int, scale: float) -> BoundingBox | None:
    """The page's first ``table`` block as a box in points, or None.

    Tolerant of a malformed block (missing or non-numeric corner keys): such a block is skipped
    rather than raising, so a bad OCR payload degrades to "no table box" instead of crashing.
    """
    corners = ("top_left_x", "top_left_y", "bottom_right_x", "bottom_right_y")
    for block in raw_page.get("blocks") or []:
        if not isinstance(block, dict) or block.get("type") != "table":
            continue
        nums = [c for c in (block.get(key) for key in corners) if isinstance(c, (int, float))]
        if len(nums) != 4:
            continue
        x0, y0, x1, y1 = (float(nums[0]), float(nums[1]), float(nums[2]), float(nums[3]))
        # Require a sane box (positive extent); an inverted/degenerate one would produce a negative
        # row-band coordinate that fails BoundingBox validation — fall through to the page box.
        if x1 > x0 and y1 > y0 and x0 >= 0 and y0 >= 0:
            return BoundingBox(
                page=page_no,
                x=x0 * scale,
                y=y0 * scale,
                width=max(x1 - x0, 1.0) * scale,
                height=max(y1 - y0, 1.0) * scale,
            )
    return None


def _table_html(raw_page: dict[str, Any]) -> str:
    """Return the page's table HTML (from ``tables[0]`` or the table block), or ''."""
    tables = raw_page.get("tables") or []
    if tables and isinstance(tables[0], dict):
        content = tables[0].get("content")
        if isinstance(content, str):
            return content
    for block in raw_page.get("blocks") or []:
        content = block.get("content")
        if block.get("type") == "table" and isinstance(content, str):
            return content
    return ""


class _TableRowParser(HTMLParser):
    """Extracts table rows as lists of cell texts, in document (visual) order."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[tuple[str, ...]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag: str) -> None:
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append("".join(self._cell).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None:
            self.rows.append(tuple(self._row))
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _parse_table_rows(html: str) -> tuple[tuple[str, ...], ...]:
    """Parse an HTML table into rows of cell strings, in visual order."""
    if not html:
        return ()
    parser = _TableRowParser()
    parser.feed(html)
    return tuple(parser.rows)
