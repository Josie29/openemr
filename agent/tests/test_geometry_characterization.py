import json
import os
from pathlib import Path
from typing import Any

import pytest

from copilot.ingestion.extractor import map_lab_report
from copilot.ingestion.geometry.document import DocumentGeometry
from copilot.ingestion.geometry.words import extract_word_boxes
from copilot.ingestion.schemas import LabReport, printed_text

_FIXTURES = Path(__file__).parent / "fixtures" / "documents"
_GOLDENS = _FIXTURES / "goldens"

# The lab fixture with BOTH a recorded OCR response and a source PDF, exercising the text-layer
# join map_lab_report performs against the digital PDF's word boxes. (The scanned/no-text-layer
# row-band fallback is exercised by simulation in test_extractor.py, not a golden PDF here.)
_CASES = [
    pytest.param("sergio-angulo-lab-report", id="digital-text-layer"),
]

# Boxes are compared with a tolerance rather than by exact float equality. The agent's deps float
# (no lockfile) and CI pins python 3.12 while local dev runs 3.14, so a pdfminer/pdfplumber patch
# bump can shift a glyph box in the last few decimals. rel=1e-6 is orders of magnitude tighter than
# any real geometry regression: a wrong row, a lost text-layer join, or a px/pt conversion slip
# moves a box by POINTS (1e0+), never by 1e-6.
_BOX_RELATIVE_TOLERANCE = 1e-6


def _snapshot(name: str) -> list[dict[str, Any]]:
    """Run today's extraction over one fixture and flatten every fact + box into plain dicts.

    Args:
        name: The fixture basename shared by the PDF and its recorded OCR response.

    Returns:
        One dict per extracted lab result, in extraction order.
    """
    ocr: dict[str, Any] = json.loads((_FIXTURES / "extractions" / f"{name}.ocr.json").read_text())
    words = extract_word_boxes((_FIXTURES / "pdfs" / f"{name}.pdf").read_bytes())
    report: LabReport = map_lab_report(ocr, DocumentGeometry.from_parts(ocr, words))
    return [
        {
            "test_name": result.test_name,
            # Pinned per analyte: a code silently shifting to a neighbouring row would relabel which
            # test was run, and the write-back would persist it under the wrong Observation.code.
            "loinc": result.loinc,
            "value": result.value,
            "unit": printed_text(result.unit),
            "reference_range": printed_text(result.reference_range),
            # Both are SECONDARY fields with their own box now. Pinned as booleans, not coordinates:
            # this net exists to catch the PRIMARY value box silently moving, and the secondaries'
            # exact rectangles are asserted by the column test below. What matters here is that they
            # do not regress to being unlocatable.
            "unit_located": (
                result.unit is not None and result.unit.citation.bounding_box is not None
            ),
            "reference_range_located": (
                result.reference_range is not None
                and result.reference_range.citation.bounding_box is not None
            ),
            "collection_date": (
                result.collection_date.isoformat() if result.collection_date else None
            ),
            "abnormal_flag": result.abnormal_flag.value,
            "confidence": result.confidence,
            "page": result.citation.bounding_box.page if result.citation.bounding_box else None,
            "x": result.citation.bounding_box.x if result.citation.bounding_box else None,
            "y": result.citation.bounding_box.y if result.citation.bounding_box else None,
            "width": result.citation.bounding_box.width if result.citation.bounding_box else None,
            "height": result.citation.bounding_box.height if result.citation.bounding_box else None,
        }
        for result in report.results
    ]


def _assert_matches(actual: list[dict[str, Any]], expected: list[dict[str, Any]]) -> None:
    """Compare two snapshots fact by fact, with a tolerance on the four box floats."""
    assert len(actual) == len(expected), "the number of extracted facts changed"
    for index, (got, want) in enumerate(zip(actual, expected, strict=True)):
        assert got.keys() == want.keys()
        for key, wanted in want.items():
            found = got[key]
            if key in ("x", "y", "width", "height", "confidence") and wanted is not None:
                assert found == pytest.approx(
                    wanted, rel=_BOX_RELATIVE_TOLERANCE
                ), f"fact {index} '{key}' moved"
            else:
                assert found == wanted, f"fact {index} '{key}' changed"


@pytest.mark.parametrize("name", _CASES)
def test_lab_geometry_is_unchanged(name: str) -> None:
    """Every lab fact's value AND its exact box are pinned, for the text-layer geometry path.

    This is the regression net for the geometry refactor (JOS-80): the locator abstraction must be
    behaviour-identical for lab_pdf, and test_extractor.py only spot-checks ~4 boxes across the
    whole report. This pins ALL of them, to the value and the rectangle, on the digital text-layer
    path (the scanned row-band fallback is exercised by simulation in test_extractor.py).

    If this breaks, the click-to-source overlay has silently moved: the physician clicks a lab
    value and the highlight lands somewhere else on the scan — or the fact stops being extracted
    at all. Regenerate deliberately (and review the diff) only when a geometry change is intended:

        UPDATE_GEOMETRY_GOLDEN=1 .venv/bin/python -m pytest tests/test_geometry_characterization.py
    """
    golden_path = _GOLDENS / f"{name}.geometry.json"
    actual = _snapshot(name)
    assert actual, "the fixture must extract at least one fact to be a useful regression net"

    if os.environ.get("UPDATE_GEOMETRY_GOLDEN"):
        golden_path.parent.mkdir(parents=True, exist_ok=True)
        golden_path.write_text(json.dumps(actual, indent=2) + "\n")
        pytest.skip(f"regenerated {golden_path.name}")

    expected: list[dict[str, Any]] = json.loads(golden_path.read_text())
    _assert_matches(actual, expected)


def test_secondary_fields_are_boxed_in_their_own_columns() -> None:
    """A unit and a reference range are boxed in their OWN cells, on the analyte's own row.

    This is the per-field geometry that makes each value checkable. LabDetail stamps unit and
    reference range onto the sidebar as system-authored fact, and a wrong range flips a normal value
    to abnormal — so a box that merely repeated the value's rectangle, or landed on a neighbouring
    analyte's row, would look like proof while proving nothing.
    """
    name = "sergio-angulo-lab-report"
    ocr: dict[str, Any] = json.loads((_FIXTURES / "extractions" / f"{name}.ocr.json").read_text())
    words = extract_word_boxes((_FIXTURES / "pdfs" / f"{name}.pdf").read_bytes())
    report = map_lab_report(ocr, DocumentGeometry.from_parts(ocr, words))

    glucose = next(result for result in report.results if result.test_name == "Glucose, Fasting")
    assert glucose.unit is not None and glucose.reference_range is not None
    assert glucose.test_name_citation is not None and glucose.loinc_citation is not None
    name_box = glucose.test_name_citation.bounding_box
    code_box = glucose.loinc_citation.bounding_box
    value_box = glucose.citation.bounding_box
    range_box = glucose.reference_range.citation.bounding_box
    unit_box = glucose.unit.citation.bounding_box
    assert None not in (name_box, code_box, value_box, range_box, unit_box)

    # The report's columns run TEST | LOINC | RESULT | FLAG | REFERENCE RANGE | UNITS, left to
    # right — so boxing the whole row means five rectangles in that order.
    assert name_box.x < code_box.x < value_box.x < range_box.x < unit_box.x
    # ...and every one sits on this analyte's own row, so each qualifies THIS result.
    for box in (name_box, code_box, range_box, unit_box):
        assert abs(box.y - value_box.y) < 12


def test_the_test_name_is_boxed_even_though_it_anchors_its_own_row() -> None:
    """The analyte name carries a box, not just the values beside it.

    The name is the anchor the row's other locators scan for, and its span was computed and then
    discarded — so the one column the sidebar shows as fact was the one column that could never be
    checked. A name bound to the wrong row is precisely how a value gets attributed to the wrong
    test, which makes it worth proving. Boxing it needs the row to START at the anchor rather than
    past it, so this also pins that `include_anchor` behaviour.
    """
    name = "sergio-angulo-lab-report"
    ocr: dict[str, Any] = json.loads((_FIXTURES / "extractions" / f"{name}.ocr.json").read_text())
    words = extract_word_boxes((_FIXTURES / "pdfs" / f"{name}.pdf").read_bytes())
    report = map_lab_report(ocr, DocumentGeometry.from_parts(ocr, words))

    boxed = [r for r in report.results if r.test_name_citation is not None]
    assert len(boxed) == len(report.results), "every analyte name must be locatable"

    # A multi-word name is boxed across all its words, not just the anchor token.
    wbc = next(r for r in report.results if r.test_name == "White Blood Cell Count")
    assert wbc.test_name_citation is not None
    box = wbc.test_name_citation.bounding_box
    assert box is not None and box.width > 60
