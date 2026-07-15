import asyncio
import base64
import json
import logging
from dataclasses import dataclass
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from copilot.config import ExtractorMode, Settings
from copilot.ingestion.schemas import (
    AbnormalFlag,
    BoundingBox,
    Citation,
    DocType,
    LabReport,
    LabResult,
)

logger = logging.getLogger("copilot")


class ExtractionError(RuntimeError):
    """Raised when an OCR response cannot be mapped into the strict ingestion schema.

    Carries enough context to log and let the agent degrade (report that the document could not be
    read, never fabricate facts), without leaking transport detail into user-facing output.
    """


# --- Mistral OCR schema-mode probe (JOS-47 spike, productionized) ------------------------------
# Deliberately FLAT: the values Mistral extracts into `document_annotation`. Geometry does NOT come
# from here (Mistral returns whole-table blocks, not per-field boxes) — it is estimated per row from
# the table block below. Kept minimal so the schema-mode request stays cheap and robust.


class _LabResultProbe(BaseModel):
    test_name: str = Field(description="Analyte/test name as printed")
    value: str = Field(description="Result value verbatim")
    unit: str | None = Field(default=None, description="Unit if printed")
    reference_range: str | None = Field(default=None, description="Reference range if printed")
    collection_date: str | None = Field(default=None, description="Collection date if printed")
    abnormal_flag: str | None = Field(default=None, description="Abnormal flag if shown")


class _LabReportProbe(BaseModel):
    results: list[_LabResultProbe] = Field(description="Every lab result on the report")


# --- OCR backends ------------------------------------------------------------------------------


class OcrBackend(Protocol):
    """A source of a raw OCR response for a document's bytes.

    Two implementations share this protocol: :class:`MistralOcrBackend` calls the live API and
    :class:`FixtureOcrBackend` replays a recorded response, so extraction tests run offline. Both
    return the raw ``resp.model_dump()`` dict that :func:`map_lab_report` maps into a
    :class:`~copilot.ingestion.schemas.LabReport`.
    """

    async def process(self, pdf_bytes: bytes, doc_type: DocType) -> dict[str, Any]:
        """Run OCR over ``pdf_bytes`` and return the raw response dict.

        Raises:
            ExtractionError: If the OCR call fails or the document type is unsupported.
        """
        ...


class MistralOcrBackend:
    """Live Mistral OCR (``mistral-ocr-latest``) in schema mode (JOS-54, W2_ARCHITECTURE §3.1).

    Productionizes the JOS-47 spike call: schema-mode field extraction plus paragraph/block boxes
    (``include_blocks``) and per-word confidence. The synchronous SDK call is run in a worker thread
    so it never blocks the event loop serving other turns.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def process(self, pdf_bytes: bytes, doc_type: DocType) -> dict[str, Any]:
        """OCR a document via Mistral schema mode.

        Args:
            pdf_bytes: The raw document bytes.
            doc_type: Which document schema to extract (only ``LAB_PDF`` is wired in this slice).

        Returns:
            The raw ``resp.model_dump()`` dict (``document_annotation`` + ``pages[].blocks``).

        Raises:
            ExtractionError: If the SDK import fails, the doc type is unsupported, or OCR fails.
        """
        if doc_type is not DocType.LAB_PDF:
            raise ExtractionError(f"extraction not implemented for {doc_type.value}")
        try:
            # mistralai 2.x is a namespace package: Mistral lives in the `client` subpackage.
            from mistralai.client import Mistral
            from mistralai.extra import response_format_from_pydantic_model
        except ImportError as exc:
            raise ExtractionError(
                "mistralai is not installed (install the [extraction] extra)"
            ) from exc

        b64 = base64.b64encode(pdf_bytes).decode()
        client = Mistral(api_key=self._api_key)
        try:
            # The SDK call is synchronous/blocking — run it off the event loop.
            resp = await asyncio.to_thread(
                client.ocr.process,
                model="mistral-ocr-latest",  # alias -> mistral-ocr-4-0
                document={
                    "type": "document_url",
                    "document_url": f"data:application/pdf;base64,{b64}",
                },
                document_annotation_format=response_format_from_pydantic_model(_LabReportProbe),
                include_blocks=True,  # OCR 4+: block bboxes (whole-table for tabular data)
                confidence_scores_granularity="word",  # opt-in per-word confidence
                table_format="html",  # lab data is tabular
            )
        except Exception as exc:
            # The SDK raises varied transport/validation errors; treat all as an OCR failure.
            raise ExtractionError("Mistral OCR request failed") from exc
        data: dict[str, Any] = resp.model_dump()
        return data


class FixtureOcrBackend:
    """Replays a recorded Mistral OCR response (``*.ocr.json``) with no live API call.

    The extraction counterpart to ``FixtureFhirClient`` / ``FixtureEvidenceRetriever``: it lets the
    graph run the real mapping pipeline over a deterministic response in tests and offline dev.
    """

    def __init__(self, fixture_path: str) -> None:
        self._fixture_path = Path(fixture_path)

    async def process(self, pdf_bytes: bytes, doc_type: DocType) -> dict[str, Any]:
        """Return the recorded OCR response, ignoring the input bytes.

        Raises:
            ExtractionError: If the fixture file is missing or not valid JSON.
        """
        try:
            data: dict[str, Any] = json.loads(self._fixture_path.read_text())
        except (OSError, ValueError) as exc:
            raise ExtractionError(f"could not read OCR fixture {self._fixture_path}") from exc
        return data


# --- Document byte-source ----------------------------------------------------------------------


class DocumentByteSource(Protocol):
    """Where the extractor gets a document's bytes, given its id.

    Production fetches the bytes from OpenEMR by document id (deferred — a SMART Binary-scope wall;
    see the seam spec). The demo slice reads a committed fixture PDF, so the extraction is real
    while the byte-fetch is stubbed.
    """

    def fetch(self, document_id: str) -> bytes:
        """Return the raw bytes for ``document_id``.

        Raises:
            ExtractionError: If the bytes cannot be read.
        """
        ...


class FixturePdfByteSource:
    """Serves the bytes of a single committed lab PDF regardless of id (demo byte-source).

    The demo has one uploaded lab document; its real ``document_id`` is discovered live (so the
    citation + chart-pane viewer resolve the actual OpenEMR record), but the bytes fed to OCR come
    from this fixture until the production OpenEMR fetch lands.
    """

    def __init__(self, pdf_path: str) -> None:
        self._pdf_path = Path(pdf_path)

    def fetch(self, document_id: str) -> bytes:
        """Return the fixture PDF bytes.

        Raises:
            ExtractionError: If the fixture PDF is missing.
        """
        try:
            return self._pdf_path.read_bytes()
        except OSError as exc:
            raise ExtractionError(f"could not read document fixture {self._pdf_path}") from exc


# --- Extraction result + facade ----------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedDocument:
    """One document's strict extraction plus the page DPI needed to place its boxes.

    ``report`` carries the cited lab facts with **native-pixel** boxes (per the ingestion schema);
    ``page_dpi`` maps each 1-based page to its render DPI so the document-fact registry can convert
    those boxes to the PDF points the click-to-source overlay expects (the JOS-57 seam).
    """

    document_id: str
    doc_type: DocType
    report: LabReport
    page_dpi: dict[int, float]


class DocumentExtractor:
    """Fetches a document's bytes, OCRs them, and maps the result into a strict ``LabReport``."""

    def __init__(self, ocr: OcrBackend, byte_source: DocumentByteSource) -> None:
        self._ocr = ocr
        self._byte_source = byte_source

    async def extract(self, document_id: str, doc_type: DocType) -> ExtractedDocument:
        """Extract one document end-to-end: bytes -> OCR -> strict ``LabReport`` + page DPI.

        Args:
            document_id: The source document's FHIR ``DocumentReference`` id (used for citations).
            doc_type: The document schema to extract (``LAB_PDF`` in this slice).

        Returns:
            The parsed :class:`ExtractedDocument`.

        Raises:
            ExtractionError: If the byte fetch, OCR, or mapping fails.
        """
        pdf_bytes = self._byte_source.fetch(document_id)
        raw = await self._ocr.process(pdf_bytes, doc_type)
        report, page_dpi = map_lab_report(raw)
        return ExtractedDocument(
            document_id=document_id, doc_type=doc_type, report=report, page_dpi=page_dpi
        )


def build_extractor(settings: Settings) -> DocumentExtractor | None:
    """Construct the document extractor from settings, or None when extraction is unconfigured.

    Returns None (extraction disabled, the intake-extractor simply reports no document) when the
    byte-source PDF is unset, or when the selected backend lacks its credential/fixture — so a
    missing key degrades to "no document facts", never a crash.

    Args:
        settings: Service settings selecting the extractor mode and paths.

    Returns:
        A wired :class:`DocumentExtractor`, or None when extraction cannot be configured.
    """
    if settings.document_pdf_path is None:
        return None
    byte_source = FixturePdfByteSource(settings.document_pdf_path)
    if settings.extractor_mode is ExtractorMode.FIXTURE:
        if settings.ocr_fixture_path is None:
            logger.warning("extractor FIXTURE mode without ocr_fixture_path; extraction disabled")
            return None
        ocr: OcrBackend = FixtureOcrBackend(settings.ocr_fixture_path)
    else:
        if settings.mistral_api_key is None:
            logger.warning("extractor MISTRAL mode without an API key; extraction disabled")
            return None
        ocr = MistralOcrBackend(settings.mistral_api_key)
    return DocumentExtractor(ocr, byte_source)


# --- OCR response -> strict LabReport mapping --------------------------------------------------
#
# Mistral OCR 4 returns the field VALUES in `document_annotation` (schema mode) but only WHOLE-TABLE
# geometry — the lab table is a single `table` block; the HTML table output carries no per-cell
# coordinates. So a per-value box is ESTIMATED: split the table block's y-range by the table's row
# order (from its HTML) and give each value a full-width band at its row (the chosen strategy — a
# correct row band, not a tight box). Boxes are emitted in native page pixels per the ingestion
# schema; the document-fact registry converts them to PDF points using the page DPI.


def map_lab_report(ocr: dict[str, Any]) -> tuple[LabReport, dict[int, float]]:
    """Map a raw Mistral OCR response into a strict ``LabReport`` with estimated per-row boxes.

    Args:
        ocr: The raw OCR response dict (``document_annotation`` + ``pages[].blocks``/``tables``).

    Returns:
        A ``(LabReport, page_dpi)`` pair — ``page_dpi`` maps each 1-based page to its render DPI so
        the registry can convert the native-pixel boxes to PDF points.

    Raises:
        ExtractionError: If the response has no usable page, or ``document_annotation`` is present
            but not valid JSON. Any mapping failure surfaces as this one type so the caller can
            degrade to "no facts" rather than crashing the turn.
    """
    annotation = ocr.get("document_annotation")
    if isinstance(annotation, str):
        try:
            annotation = json.loads(annotation)
        except ValueError as exc:  # JSONDecodeError subclasses ValueError
            raise ExtractionError("OCR document_annotation is not valid JSON") from exc
    if not isinstance(annotation, dict):
        annotation = {}
    raw_annotation_results = annotation.get("results")
    raw_results = raw_annotation_results if isinstance(raw_annotation_results, list) else []

    pages = ocr.get("pages") or []
    if not pages or not isinstance(pages[0], dict):
        raise ExtractionError("OCR response carries no usable page")
    page = pages[0]
    page_no = int(page.get("index", 0)) + 1  # `index` is 0-based; BoundingBox.page is 1-based.
    dims = page.get("dimensions") or {}
    dpi = float(dims.get("dpi") or 72.0)

    table_box = _find_table_box(page)
    # Coarse whole-page box, used only when there is no table block to locate rows on (finding #4):
    # surface the values on a page-wide overlay rather than dropping the clinical facts entirely.
    fallback_box = _page_box(page, page_no)
    if table_box is None and raw_results:
        logger.warning(
            "OCR response has no table block; using a whole-page fallback box for lab values",
            extra={"result_count": len(raw_results)},
        )
    rows = _parse_table_rows(_table_html(page))
    name_to_index = {_norm(cells[0]): i for i, cells in enumerate(rows) if cells}
    total_rows = len(rows) or 1

    results: list[LabResult] = []
    for ordinal, raw in enumerate(raw_results):
        if not isinstance(raw, dict):
            continue  # skip a malformed (non-object) result entry rather than crashing the turn
        box = _estimate_row_box(
            str(raw.get("test_name", "")), ordinal, len(raw_results),
            table_box, name_to_index, total_rows, page_no,
        ) or fallback_box
        if box is None:
            # No table geometry and no usable page dimensions — drop rather than fabricate a box.
            logger.warning(
                "dropping lab result with no locatable box",
                extra={"test": raw.get("test_name")},
            )
            continue
        results.append(_build_lab_result(raw, box, page))
    return LabReport(results=results), {page_no: dpi}


def _build_lab_result(raw: dict[str, Any], box: BoundingBox, page: dict[str, Any]) -> LabResult:
    """Build one cited ``LabResult`` from a document_annotation row and its estimated box."""
    value = str(raw.get("value", "")).strip()
    return LabResult(
        test_name=str(raw.get("test_name", "")).strip(),
        value=value,
        unit=_clean(raw.get("unit")),
        reference_range=_clean(raw.get("reference_range")),
        collection_date=_parse_date(raw.get("collection_date")),
        abnormal_flag=_map_abnormal(raw.get("abnormal_flag")),
        citation=Citation(quote_or_value=value, bounding_box=box),
        confidence=_value_confidence(value, page),
    )


def _estimate_row_box(
    test_name: str,
    ordinal: int,
    result_count: int,
    table_box: tuple[float, float, float, float] | None,
    name_to_index: dict[str, int],
    total_rows: int,
    page_no: int,
) -> BoundingBox | None:
    """Estimate the full-width row band for one lab value within the table block.

    The value's visual row index is found by matching its test name against the parsed table rows
    (falling back to its ordinal position when the name is not matched); the table block's y-range
    is split evenly by row count to give that row's band.

    Returns:
        A native-pixel :class:`BoundingBox` spanning the table width at the value's row, or None
        when the page has no table block to place it on.
    """
    if table_box is None:
        return None
    x0, y0, x1, y1 = table_box
    row_index = name_to_index.get(_norm(test_name))
    if row_index is None:
        # Name not matched (annotation reworded it): estimate by ordinal across the table height.
        denom = result_count or 1
        row_index = min(total_rows - 1, round((ordinal + 0.5) / denom * total_rows))
    row_height = (y1 - y0) / total_rows
    band_top = y0 + row_index * row_height
    return BoundingBox(
        page=page_no,
        x=x0,
        y=band_top,
        width=max(x1 - x0, 1.0),
        height=max(row_height, 1.0),
    )


def _find_table_box(page: dict[str, Any]) -> tuple[float, float, float, float] | None:
    """Return the (x0, y0, x1, y1) box of the page's first ``table`` block, or None.

    Tolerant of a malformed block (missing or non-numeric corner keys): such a block is skipped
    rather than raising, so a bad OCR payload degrades to "no table box" instead of crashing.
    """
    corners = ("top_left_x", "top_left_y", "bottom_right_x", "bottom_right_y")
    for block in page.get("blocks") or []:
        if not isinstance(block, dict) or block.get("type") != "table":
            continue
        nums = [c for c in (block.get(key) for key in corners) if isinstance(c, (int, float))]
        if len(nums) != 4:
            continue
        x0, y0, x1, y1 = (float(nums[0]), float(nums[1]), float(nums[2]), float(nums[3]))
        # Require a sane box (positive extent); an inverted/degenerate one would produce a negative
        # row-band coordinate that fails BoundingBox validation — fall through to the page box.
        if x1 > x0 and y1 > y0 and x0 >= 0 and y0 >= 0:
            return (x0, y0, x1, y1)
    return None


def _page_box(page: dict[str, Any], page_no: int) -> BoundingBox | None:
    """A whole-page native-pixel box — the coarse fallback when no table rows can be located.

    Returns None when the page carries no usable dimensions (so the caller drops the value rather
    than emit a zero-area box the schema would reject).
    """
    dims = page.get("dimensions") or {}
    width = dims.get("width")
    height = dims.get("height")
    if not (
        isinstance(width, (int, float))
        and isinstance(height, (int, float))
        and width > 0
        and height > 0
    ):
        return None
    return BoundingBox(page=page_no, x=0, y=0, width=float(width), height=float(height))


def _table_html(page: dict[str, Any]) -> str:
    """Return the page's table HTML (from ``tables[0]`` or the table block), or ''."""
    tables = page.get("tables") or []
    if tables and isinstance(tables[0], dict):
        content = tables[0].get("content")
        if isinstance(content, str):
            return content
    for block in page.get("blocks") or []:
        content = block.get("content")
        if block.get("type") == "table" and isinstance(content, str):
            return content
    return ""


class _TableRowParser(HTMLParser):
    """Extracts table rows as lists of cell texts, in document (visual) order."""

    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
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
            self.rows.append(self._row)
            self._row = None

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)


def _parse_table_rows(html: str) -> list[list[str]]:
    """Parse an HTML table into a list of rows, each a list of cell strings, in visual order."""
    if not html:
        return []
    parser = _TableRowParser()
    parser.feed(html)
    return parser.rows


def _value_confidence(value: str, page: dict[str, Any]) -> float | None:
    """Per-value confidence: average the OCR word confidences whose text matches the value.

    Falls back to the page's average confidence when no word matches (e.g. multi-token values), and
    to None when the response carries no confidence scores at all.
    """
    scores = page.get("confidence_scores")
    if not isinstance(scores, dict):
        return None
    target = _norm(value)
    words = scores.get("word_confidence_scores")
    if isinstance(words, list) and target:
        matched = [
            float(w["confidence"])
            for w in words
            if isinstance(w, dict) and _norm(str(w.get("text", ""))) == target
            and isinstance(w.get("confidence"), (int, float))
        ]
        if matched:
            return sum(matched) / len(matched)
    avg = scores.get("average_page_confidence_score")
    return float(avg) if isinstance(avg, (int, float)) else None


def _map_abnormal(flag: Any) -> AbnormalFlag:
    """Map a printed abnormal flag (``H``/``L``/``A``/blank) to the schema enum."""
    token = str(flag or "").strip().upper()
    if not token or token == "N":
        return AbnormalFlag.NO
    if token.startswith("H"):
        return AbnormalFlag.HIGH
    if token.startswith("L"):
        return AbnormalFlag.LOW
    return AbnormalFlag.YES


def _parse_date(raw: Any) -> date | None:
    """Parse an ISO collection date, or None when absent/unparseable (never infer)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _clean(raw: Any) -> str | None:
    """Return a stripped non-empty string, or None."""
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def _norm(text: str) -> str:
    """Lowercase and collapse whitespace, for tolerant name/value matching."""
    return " ".join(str(text).split()).lower()
