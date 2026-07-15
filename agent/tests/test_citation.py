import pytest
from pydantic import TypeAdapter, ValidationError

from copilot.schemas import (
    Citation,
    CitationSourceType,
    GuidelineCitation,
    IntakeFormCitation,
    LabPdfCitation,
)


def test_guideline_citation_carries_the_full_contract_shape() -> None:
    # Catches drift in the W2 citation contract: the eval `citation_present` rubric and the
    # citation-card UI depend on exactly these five machine-readable fields. If a field is
    # renamed or dropped, this breaks before it reaches a physician.
    citation = GuidelineCitation(
        source_id="statpearls-paroxysmal-af-2023",
        page_or_section="Antithrombotic Therapy",
        field_or_chunk_id="statpearls-paroxysmal-af-2023-antithrombotic-01",
        quote_or_value="CHA2DS2-VASc estimates AF stroke risk.",
    )
    assert citation.source_type is CitationSourceType.GUIDELINE
    assert set(citation.model_dump()) == {
        "source_type",
        "source_id",
        "page_or_section",
        "field_or_chunk_id",
        "quote_or_value",
    }


def test_citation_union_discriminates_each_source_type() -> None:
    # Catches a broken discriminator: the whole extensibility premise is that adding a document
    # source is additive and routes to its own typed arm. A mis-tagged citation deserializing to
    # the wrong variant would silently corrupt provenance for extracted facts later.
    adapter: TypeAdapter[Citation] = TypeAdapter(Citation)

    guideline = adapter.validate_python(
        {
            "source_type": "guideline",
            "source_id": "s",
            "page_or_section": "Sec",
            "field_or_chunk_id": "c",
            "quote_or_value": "q",
        }
    )
    assert isinstance(guideline, GuidelineCitation)

    lab = adapter.validate_python(
        {
            "source_type": "lab_pdf",
            "source_id": "DocumentReference/1",
            "page_or_section": "2",
            "field_or_chunk_id": "glucose",
            "quote_or_value": "182 mg/dL",
        }
    )
    assert isinstance(lab, LabPdfCitation)

    intake = adapter.validate_python(
        {
            "source_type": "intake_form",
            "source_id": "DocumentReference/2",
            "page_or_section": "1",
            "field_or_chunk_id": "chief_concern",
            "quote_or_value": "shortness of breath",
        }
    )
    assert isinstance(intake, IntakeFormCitation)


def test_citation_is_frozen() -> None:
    # Citations are value objects stamped from source records; allowing mutation would let a
    # later stage silently rewrite a claim's provenance after it was verified.
    citation = GuidelineCitation(
        source_id="s", page_or_section="x", field_or_chunk_id="c", quote_or_value="q"
    )
    with pytest.raises(ValidationError):
        citation.source_id = "tampered"  # type: ignore[misc]
