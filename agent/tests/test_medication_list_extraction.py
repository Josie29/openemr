import json
from pathlib import Path
from typing import Any

from copilot.ingestion.extractor import map_medication_list
from copilot.ingestion.geometry.document import DocumentGeometry
from copilot.ingestion.schemas import MedicationList

_FIXTURES = Path(__file__).parent / "fixtures" / "documents"
_NAME = "sergio-angulo-medication-list"


def _load(name: str) -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(
        (_FIXTURES / "extractions" / f"{name}.ocr.json").read_text()
    )
    return parsed


def _pdf(name: str) -> bytes:
    return (_FIXTURES / "pdfs" / f"{name}.pdf").read_bytes()


def _extract(name: str, ocr: dict[str, Any] | None = None) -> MedicationList:
    """Map one medication-list fixture through the real pipeline."""
    resolved = ocr or _load(name)
    return map_medication_list(resolved, DocumentGeometry.from_document(_pdf(name), resolved))


def test_extracts_every_medication_with_name_dose_and_frequency() -> None:
    """Every medication on the list is read with the three fields the write path persists.

    The medication list is the document type that owns medications (PRD-week-2 Core Req 1's third
    type). If a row's name, dose, or frequency stops coming through, the co-pilot writes an
    incomplete `lists`/`lists_medication` record — a proposed medication with no dose or cadence the
    physician can act on. Pins the whole extraction contract for this document type.
    """
    medications = _extract(_NAME).medications

    # The Wells Branch Pharmacy fixture prints six active medications.
    assert len(medications) == 6

    by_name = {med.name: med for med in medications}
    # Names are read verbatim (brand in parens), which is what makes each locatable on the page.
    assert "Budesonide (Pulmicort)" in by_name
    assert "Naproxen (Aleve)" in by_name

    # Dose and frequency ride every row — they become the medication's dosage instructions on write.
    for med in medications:
        assert med.dose, f"{med.name} lost its dose"
        assert med.frequency, f"{med.name} lost its frequency"

    budesonide = by_name["Budesonide (Pulmicort)"]
    assert budesonide.dose is not None and budesonide.dose.value == "0.5 mg"
    assert budesonide.frequency is not None and budesonide.frequency.value == "twice daily"


def test_dose_and_frequency_are_independently_locatable() -> None:
    """Each qualifier carries its OWN box, in its own column — not a copy of the name's box.

    Dose and frequency are the values a transcription error is most dangerous in ("500 mg" vs
    "5000 mg"), yet they used to ship as unlocatable text beneath a UI implying everything shown was
    read off the page. Boxing them per-field is what makes each one checkable. They must also be
    DISTINCT boxes on the medication's own row: reusing the name's box would point the overlay at
    the wrong cell, and matching another drug's row would attribute the wrong dose.
    """
    by_name = {med.name: med for med in _extract(_NAME).medications}
    budesonide = by_name["Budesonide (Pulmicort)"]
    assert budesonide.dose is not None and budesonide.frequency is not None

    name_box = budesonide.citation.bounding_box
    dose_box = budesonide.dose.citation.bounding_box
    frequency_box = budesonide.frequency.citation.bounding_box
    assert name_box is not None and dose_box is not None and frequency_box is not None

    # The fixture's columns run Medication | Dose / Strength | Frequency, left to right.
    assert name_box.x < dose_box.x < frequency_box.x
    # ...and all three sit on one row, so the dose belongs to THIS drug.
    assert abs(dose_box.y - name_box.y) < 12
    assert abs(frequency_box.y - name_box.y) < 12


def test_every_medication_carries_a_box() -> None:
    """No medication ships without geometry.

    `Medication` has no bounding-box validator (unlike `LabResult`), so the mapper's precision floor
    is the only thing that enforces this. If it breaks, a medication reaches the sidebar with a
    citation the physician cannot click through to the source page to verify.
    """
    medications = _extract(_NAME).medications
    assert medications, "the fixture must yield medications for this test to mean anything"
    for med in medications:
        assert med.citation.bounding_box is not None


def test_medication_boxes_meet_the_precision_floor() -> None:
    """Every medication box is at least line-accurate — never a whole-page rectangle.

    "Has a box" is not "is click-to-source": a page-sized highlight verifies nothing. If this
    regresses, a medication citation looks verifiable in the UI while pointing at the entire page.
    """
    page_height = 792.0  # US Letter, the fixture's size
    for med in _extract(_NAME).medications:
        box = med.citation.bounding_box
        assert box is not None
        assert box.height < page_height / 4, f"'{med.name}' got a page-sized box"


def test_a_medication_whose_name_is_not_on_the_page_is_dropped() -> None:
    """A medication the page does not actually print is refused, not cited with a fabricated box.

    A value that cannot be located earns no box — the same discipline the intake path enforces. If
    this breaks, click-to-source could launder a hallucinated medication onto a real page region.
    """
    ocr = _load(_NAME)
    annotation = ocr["document_annotation"]
    annotation = json.loads(annotation) if isinstance(annotation, str) else dict(annotation)
    annotation["medications"] = [
        {"name": "Warfarin (Coumadin)", "dose": "5 mg", "frequency": "daily"},  # not on the page
        {"name": "Budesonide (Pulmicort)", "dose": "0.5 mg", "frequency": "twice daily"},  # is
    ]
    ocr["document_annotation"] = annotation

    names = [med.name for med in _extract(_NAME, ocr).medications]
    assert names == ["Budesonide (Pulmicort)"], "only the medication actually on the page survives"
