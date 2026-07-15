import pytest
from pydantic import ValidationError

from copilot.ingestion.schemas import (
    AbnormalFlag,
    Allergy,
    BoundingBox,
    Citation,
    CitedText,
    Demographics,
    FamilyHistoryItem,
    IntakeForm,
    LabReport,
    LabResult,
    Medication,
    SourceType,
)


def _valid_box() -> BoundingBox:
    """A well-formed bounding box (all constraints satisfied)."""
    return BoundingBox(page=1, x=10.0, y=20.0, width=100.0, height=12.0)


def _valid_citation(*, box: bool = True) -> Citation:
    """A citation as the extractor emits it: verbatim value plus an optional box."""
    return Citation(
        quote_or_value="1.44",
        bounding_box=_valid_box() if box else None,
        source_type=SourceType.LAB_PDF,
        source_id="doc-1",
    )


def _valid_lab_result(**overrides: object) -> LabResult:
    """A fully-specified, box-carrying LabResult; keyword overrides let tests mutate one field."""
    fields: dict[str, object] = {
        "test_name": "Creatinine",
        "value": "1.44",
        "abnormal_flag": AbnormalFlag.HIGH,
        "citation": _valid_citation(),
    }
    fields.update(overrides)
    return LabResult(**fields)  # type: ignore[arg-type]


# --- lab_pdf contract ---------------------------------------------------------------------------


def test_lab_result_happy_path() -> None:
    """A fully-cited lab value constructs and preserves its printed value/flag verbatim.

    If this breaks, the canonical lab contract can't represent a normal extracted fact at all.
    """
    result = _valid_lab_result()
    assert result.value == "1.44"  # verbatim, never rounded
    assert result.abnormal_flag is AbnormalFlag.HIGH
    assert result.citation.bounding_box is not None


def test_lab_result_without_bounding_box_is_rejected() -> None:
    """A lab fact whose citation has no box is a schema violation (PRD Core Req 5).

    Catches the bug where a boxless lab value slips through and the click-to-source overlay has no
    rectangle to place it on — the whole point of _require_bounding_box.
    """
    with pytest.raises(ValidationError):
        _valid_lab_result(citation=_valid_citation(box=False))


@pytest.mark.parametrize(
    "field,value",
    [
        ("page", 0),  # page is 1-based
        ("x", -1.0),
        ("y", -1.0),
        ("width", 0.0),  # zero-area box locates nothing
        ("height", 0.0),
        ("width", -5.0),
    ],
)
def test_bounding_box_rejects_out_of_range_geometry(field: str, value: float) -> None:
    """Degenerate boxes (negative coords, zero/negative area, page < 1) are rejected.

    Catches raw VLM geometry that would render as an off-page or inverted overlay rectangle.
    """
    box = {"page": 1, "x": 10.0, "y": 20.0, "width": 100.0, "height": 12.0, field: value}
    with pytest.raises(ValidationError):
        BoundingBox(**box)  # type: ignore[arg-type]


@pytest.mark.parametrize("confidence", [1.5, -0.1])
def test_lab_result_rejects_confidence_outside_unit_interval(confidence: float) -> None:
    """Per-field confidence must be a probability in [0, 1]; anything else is malformed."""
    with pytest.raises(ValidationError):
        _valid_lab_result(confidence=confidence)


@pytest.mark.parametrize("confidence", [0.0, 1.0, None])
def test_lab_result_accepts_confidence_at_and_within_bounds(confidence: float | None) -> None:
    """The confidence bounds are inclusive, and None (unset) is allowed."""
    assert _valid_lab_result(confidence=confidence).confidence == confidence


@pytest.mark.parametrize("missing", ["test_name", "value", "abnormal_flag", "citation"])
def test_lab_result_requires_core_fields(missing: str) -> None:
    """Omitting any required lab field is rejected rather than defaulted to a silent blank.

    Catches raw VLM output that drops the test name, value, flag, or citation entirely.
    """
    fields: dict[str, object] = {
        "test_name": "Creatinine",
        "value": "1.44",
        "abnormal_flag": AbnormalFlag.HIGH,
        "citation": _valid_citation(),
    }
    del fields[missing]
    with pytest.raises(ValidationError):
        LabResult(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize("field", ["test_name", "value"])
def test_lab_result_rejects_blank_verbatim_strings(field: str) -> None:
    """An empty test name/value is 'raw VLM output slipping through' and must be rejected."""
    with pytest.raises(ValidationError):
        _valid_lab_result(**{field: ""})


def test_citation_rejects_blank_quote_or_value() -> None:
    """A citation must carry a non-empty verbatim quote; an empty string cites nothing."""
    with pytest.raises(ValidationError):
        Citation(quote_or_value="", bounding_box=_valid_box())


def test_lab_report_accepts_empty_results() -> None:
    """An empty report is valid (missing-data), and a populated one round-trips its results.

    Catches a contract that would reject a report with nothing extracted instead of surfacing it.
    """
    assert LabReport(results=[]).results == []
    report = LabReport(results=[_valid_lab_result()])
    assert report.results[0].value == "1.44"


def test_lab_result_is_frozen() -> None:
    """LabResult is an immutable value object; mutation after construction is rejected."""
    result = _valid_lab_result()
    with pytest.raises(ValidationError):
        result.value = "9.9"  # type: ignore[misc]


# --- intake_form contract -----------------------------------------------------------------------


def _cited(value: str) -> CitedText:
    """A CitedText with a valid citation (intake citations do not require a box)."""
    return CitedText(value=value, citation=_valid_citation(box=False))


def test_intake_form_happy_path() -> None:
    """A fully-populated intake form constructs and every section round-trips.

    First-ever construction coverage for the intake models; if it breaks, the intake contract
    can't represent a real filled-out form.
    """
    form = IntakeForm(
        demographics=Demographics(full_name=_cited("Sergio Angulo")),
        chief_concern=_cited("chest pain"),
        current_medications=[
            Medication(name="Metformin", dose="500 mg", citation=_valid_citation(box=False))
        ],
        allergies=[Allergy(substance="Penicillin", citation=_valid_citation(box=False))],
        family_history=[
            FamilyHistoryItem(condition="Type 2 diabetes", citation=_valid_citation(box=False))
        ],
    )
    assert form.demographics.full_name is not None
    assert form.demographics.full_name.value == "Sergio Angulo"
    assert form.current_medications[0].name == "Metformin"
    assert form.allergies[0].substance == "Penicillin"
    assert form.family_history[0].condition == "Type 2 diabetes"


def test_demographics_accepts_all_fields_absent() -> None:
    """Every demographic field is optional — an all-None block (illegible scan) is valid."""
    demo = Demographics()
    assert demo.full_name is None
    assert demo.date_of_birth is None


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Medication(name="Metformin"),
        lambda: Allergy(substance="Penicillin"),
        lambda: FamilyHistoryItem(condition="Type 2 diabetes"),
        lambda: CitedText(value="chest pain"),
    ],
)
def test_intake_items_require_a_citation(factory: object) -> None:
    """Every intake fact must be cited; an uncited raw fact is rejected, never stored bare.

    Catches VLM output that asserts a med/allergy/family-history item with no source pointer.
    """
    with pytest.raises(ValidationError):
        factory()  # type: ignore[operator]


def test_cited_text_does_not_require_a_bounding_box() -> None:
    """Intake citations may omit a box (unlike lab facts) — pins that intentional asymmetry.

    If this starts failing, a schema change has wrongly forced boxes onto free-text intake values.
    """
    text = CitedText(value="chest pain", citation=_valid_citation(box=False))
    assert text.citation.bounding_box is None


@pytest.mark.parametrize(
    "factory",
    [
        lambda: Medication(name="", citation=_valid_citation(box=False)),
        lambda: Allergy(substance="", citation=_valid_citation(box=False)),
        lambda: FamilyHistoryItem(condition="", citation=_valid_citation(box=False)),
        lambda: CitedText(value="", citation=_valid_citation(box=False)),
    ],
)
def test_intake_items_reject_blank_verbatim_strings(factory: object) -> None:
    """Blank med name/substance/condition/value is unvalidated VLM slop and must be rejected."""
    with pytest.raises(ValidationError):
        factory()  # type: ignore[operator]


def test_intake_form_accepts_empty_sections() -> None:
    """Empty list sections are valid — they mean 'none read from the form' (missing-data)."""
    form = IntakeForm(
        demographics=Demographics(),
        current_medications=[],
        allergies=[],
        family_history=[],
    )
    assert form.current_medications == []
    assert form.chief_concern is None


@pytest.mark.parametrize(
    "missing", ["demographics", "current_medications", "allergies", "family_history"]
)
def test_intake_form_requires_core_sections(missing: str) -> None:
    """Omitting demographics or any list section is rejected — the shape is always fully present."""
    fields: dict[str, object] = {
        "demographics": Demographics(),
        "current_medications": [],
        "allergies": [],
        "family_history": [],
    }
    del fields[missing]
    with pytest.raises(ValidationError):
        IntakeForm(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "factory",
    [
        lambda: CitedText(value="x", citation=_valid_citation(box=False), confidence=1.5),
        lambda: Medication(name="Metformin", citation=_valid_citation(box=False), confidence=-0.1),
    ],
)
def test_intake_items_reject_confidence_outside_unit_interval(factory: object) -> None:
    """Intake per-field confidence, like lab, must be a probability in [0, 1]."""
    with pytest.raises(ValidationError):
        factory()  # type: ignore[operator]
