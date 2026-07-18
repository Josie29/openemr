import json
from pathlib import Path
from typing import Any

import pytest

from copilot.ingestion.extractor import map_intake_form
from copilot.ingestion.geometry.document import DocumentGeometry
from copilot.ingestion.geometry.words import extract_checkboxes
from copilot.ingestion.schemas import Citation, IntakeForm

_FIXTURES = Path(__file__).parent / "fixtures" / "documents"

# The two committed intake fixtures are the SAME patient's facts in deliberately disjoint layouts:
# v1 uses tables + checkboxes and stacks values BELOW their labels; v2 has neither and prints values
# to the RIGHT. One spec set must extract both — that is what stops the locators overfitting to
# whichever form happened to be in front of us.
_FORMS = [
    pytest.param("sergio-angulo-intake-form", id="v1-tables-checkboxes-values-below"),
    pytest.param("sergio-angulo-intake-form-v2", id="v2-linear-no-tables-values-right"),
]


def _load(name: str) -> dict[str, Any]:
    path = _FIXTURES / "extractions" / f"{name}.ocr.json"
    parsed: dict[str, Any] = json.loads(path.read_text())
    return parsed


def _all_citations(form: IntakeForm) -> list[Citation]:
    """Every citation the form emitted, across all its sections."""
    demographics = (
        form.demographics.full_name,
        form.demographics.date_of_birth,
        form.demographics.sex,
        form.demographics.address,
        form.demographics.phone,
        form.chief_concern,
    )
    return [
        *(cited.citation for cited in demographics if cited is not None),
        *(allergy.citation for allergy in form.allergies),
        *(item.citation for item in form.family_history),
    ]


def _pdf(name: str) -> bytes:
    return (_FIXTURES / "pdfs" / f"{name}.pdf").read_bytes()


def _extract(name: str, ocr: dict[str, Any] | None = None) -> IntakeForm:
    """Map one intake fixture through the real pipeline."""
    resolved = ocr or _load(name)
    return map_intake_form(resolved, DocumentGeometry.from_document(_pdf(name), resolved))


@pytest.mark.parametrize("name", _FORMS)
def test_one_spec_set_extracts_both_layouts(name: str) -> None:
    """The same INTAKE_SPECS locate the same facts on two structurally unrelated forms.

    v1 renders allergies and family history as tables with tick boxes; v2 renders them as
    em-dash-delimited lines under a heading, with no boxes at all. A locator chain tuned to either
    would return nothing on the other.

    If this breaks, the geometry has been re-welded to one document layout — the exact bug JOS-80
    exists to remove. Adding a third form should mean adding label aliases to a FieldSpec until this
    passes with it in _FORMS, not writing new code.
    """
    form = _extract(name)
    demographics = form.demographics

    # Every field PRD-week-2 Core Req 2 names, on both layouts.
    assert demographics.full_name is not None
    assert "Angulo" in demographics.full_name.value
    assert demographics.date_of_birth is not None
    assert demographics.sex is not None
    assert demographics.sex.value == "Male"
    assert demographics.address is not None
    assert "Cypress Bend" in demographics.address.value
    assert demographics.phone is not None
    assert form.chief_concern is not None
    assert "check-up" in form.chief_concern.value

    assert len(form.allergies) >= 4
    assert len(form.family_history) >= 5

    # The same five conditions are claimed on both forms, however each renders them.
    conditions = " | ".join(item.condition.lower() for item in form.family_history)
    for expected in ("asthma", "high blood pressure", "kidney disease"):
        assert expected in conditions
    # ...and the conditions the form merely OFFERS never appear, on either layout.
    for unclaimed in ("heart disease", "cancer", "stroke"):
        assert unclaimed not in conditions


@pytest.mark.parametrize("name", _FORMS)
def test_every_emitted_intake_fact_carries_a_box(name: str) -> None:
    """No intake fact ships without geometry, on either layout.

    IntakeForm's sub-models — unlike LabResult — have no bounding-box validator, so nothing in the
    schema enforces this; the mapper's precision floor is its only owner. If this breaks, a fact
    reaches the sidebar with a citation the physician cannot click through to verify.
    """
    citations = _all_citations(_extract(name))
    assert citations, "the fixture must yield facts for this test to mean anything"
    for citation in citations:
        assert citation.bounding_box is not None


def test_unticked_options_are_refuted_not_boxed() -> None:
    """A fabricated fact whose value is merely PREPRINTED on the form is refused, not cited.

    This is the highest-stakes test on the branch. v1 preprints every option it offers: "Female"
    sits on the page as a bare word next to an UNTICKED box, and so do "Heart Disease" and "Cancer".
    Only the tick asserts an answer. A locator that just text-matched would find those words, box
    them, and hand back a citation — and the grounding gate would pass the claim, because the
    citation contract only requires the value to appear on the page, which it does.

    If this breaks, click-to-source launders a hallucination: the physician clicks a fabricated
    "family history of cancer", sees the highlight land on real text, and believes it.
    """
    ocr = _load("sergio-angulo-intake-form")
    annotation = ocr["document_annotation"]
    annotation = json.loads(annotation) if isinstance(annotation, str) else dict(annotation)
    annotation["sex"] = "Female"  # printed on the page; its box is NOT ticked
    annotation["family_history"] = [
        {"condition": "Heart Disease", "relation": "Father"},  # printed, NOT ticked
        {"condition": "Cancer", "relation": "Mother"},  # printed, NOT ticked
        {"condition": "Asthma", "relation": "Mother, Brother"},  # printed AND ticked
    ]
    ocr["document_annotation"] = annotation

    form = _extract("sergio-angulo-intake-form", ocr)

    assert form.demographics.sex is None, "an unticked option must not become a fact"
    assert [item.condition for item in form.family_history] == ["Asthma"], (
        "only the ticked condition may survive"
    )


def test_ticked_option_is_boxed_over_its_mark() -> None:
    """A checkbox-backed fact's box covers the tick, not just the option's preprinted label.

    The box is the evidence a physician checks. Framing only the word "Asthma" would show them text
    the form prints for every patient; the box has to include the mark that makes it this patient's.
    """
    form = _extract("sergio-angulo-intake-form")
    asthma = next(item for item in form.family_history if item.condition == "Asthma")
    box = asthma.citation.bounding_box
    assert box is not None
    # The tick box sits at x~46.9 and the label starts right of it; the citation must span both.
    assert box.x < 50, "the box must start at the tick, not at the label text"


def test_linear_form_reads_sex_as_printed_text() -> None:
    """On a form with no tick boxes, a printed answer is still extracted.

    The mirror of the refusal test, and why evidence is a property of the BOX rather than a rule on
    the field: v2 states "Sex: Male" as plain text, so requiring a CHECKED_MARK for `sex` would make
    the field permanently unextractable there. The checkbox locator must defer, not refuse, when the
    form simply has no boxes.
    """
    pdf = _pdf("sergio-angulo-intake-form-v2")
    assert extract_checkboxes(pdf) == [], "v2 is the no-checkbox layout this test relies on"

    ocr = _load("sergio-angulo-intake-form-v2")
    annotation = ocr["document_annotation"]
    annotation = json.loads(annotation) if isinstance(annotation, str) else dict(annotation)
    annotation["sex"] = "Male"
    ocr["document_annotation"] = annotation

    form = _extract("sergio-angulo-intake-form-v2", ocr)
    assert form.demographics.sex is not None
    assert form.demographics.sex.value == "Male"


def test_multi_word_values_merge_into_one_box() -> None:
    """A value spanning several words is boxed once, over all of them.

    An address is seven tokens and a date is five ("03 / 14 / 1979"); the lab path only ever matched
    single words. If this regresses, the overlay highlights the street number and drops the street.
    """
    form = _extract("sergio-angulo-intake-form")
    address = form.demographics.address
    assert address is not None
    box = address.citation.bounding_box
    assert box is not None
    # The full address spans ~170pt; a single-token box would be a fraction of that.
    assert box.width > 120


def test_date_of_birth_is_boxed_as_the_form_prints_it() -> None:
    """The date is cited in the form's own format, not a normalized one.

    The extractor will happily return "1979-03-14" for a form that prints "03 / 14 / 1979" unless
    the probe insists on verbatim text — and a normalized value cannot be found on the page, so the
    field is silently dropped. This pins the probe's verbatim instruction: if it is relaxed, this
    fixture's date stops being extractable at all.
    """
    form = _extract("sergio-angulo-intake-form")
    date_of_birth = form.demographics.date_of_birth
    assert date_of_birth is not None
    assert date_of_birth.value == "03 / 14 / 1979"
    assert date_of_birth.citation.bounding_box is not None


def test_quoted_free_text_still_matches_the_page() -> None:
    """A quoted answer is located despite the page and the extractor using different quote glyphs.

    v2 prints the visit reason wrapped in typographic quotes; the extractor echoes it with straight
    ones. Only the delimiters differ, but an exact match would fail and drop a 28-word chief concern
    that is otherwise perfectly verbatim.
    """
    form = _extract("sergio-angulo-intake-form-v2")
    assert form.chief_concern is not None
    assert "rescue inhaler" in form.chief_concern.value
    assert form.chief_concern.citation.bounding_box is not None


def test_chief_concern_box_spans_its_whole_paragraph() -> None:
    """A free-text answer is boxed across every line it occupies, not just its first.

    The concern runs to several lines; boxing only line one would send the physician to a fragment
    of the sentence the claim rests on.
    """
    form = _extract("sergio-angulo-intake-form")
    assert form.chief_concern is not None
    box = form.chief_concern.citation.bounding_box
    assert box is not None
    # A single ~13pt line could not enclose a paragraph; this one wraps.
    assert box.height > 20


def test_checkbox_detection_ignores_letter_x_in_prose() -> None:
    """An "X" in ordinary text is not a tick.

    The form's own address ends "TX 78745". Treating any X glyph as a mark would tick whatever box
    happened to be nearest and start asserting facts the patient never claimed.
    """
    boxes = extract_checkboxes(_pdf("sergio-angulo-intake-form"))
    assert len(boxes) == 10, "v1 has ten tick boxes"
    # Five conditions plus one sex option are ticked; the rest are offered but unclaimed.
    assert sum(1 for box in boxes if box.ticked) == 6


@pytest.mark.parametrize("name", _FORMS)
def test_intake_boxes_meet_the_precision_floor(name: str) -> None:
    """Every intake box is at least line-accurate — never a whole-page rectangle.

    "Has a box" is not "is click-to-source": a page-sized highlight tells the physician nothing. If
    this breaks, a citation looks verifiable in the UI while pointing at the entire scan.
    """
    page_height = 792.0  # US Letter, the fixtures' size
    for citation in _all_citations(_extract(name)):
        box = citation.bounding_box
        assert box is not None
        assert box.height < page_height / 4, f"'{citation.quote_or_value}' got a page-sized box"
