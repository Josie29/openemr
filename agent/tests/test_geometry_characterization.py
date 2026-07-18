import json
import os
from pathlib import Path
from typing import Any

import pytest

from copilot.ingestion.extractor import map_lab_report
from copilot.ingestion.geometry.document import DocumentGeometry
from copilot.ingestion.geometry.words import extract_word_boxes
from copilot.ingestion.schemas import LabReport

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
            "unit": result.unit,
            "reference_range": result.reference_range,
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
