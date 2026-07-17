from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DocType(StrEnum):
    """The two document types the ingestion flow supports (PRD Core Req 1).

    Resolved deterministically from the OpenEMR document category, not classified by the model:
    the ``Lab Report`` category maps to ``LAB_PDF``, ``Patient Information`` to ``INTAKE_FORM``.
    The category picks the schema; the model never decides what kind of document it is.
    """

    LAB_PDF = "lab_pdf"
    INTAKE_FORM = "intake_form"


def paths_by_doc_type(*, lab_pdf: str | None, intake_form: str | None) -> dict[DocType, str]:
    """Pair each document type with its configured file path, dropping the unconfigured ones.

    Settings carry one flat scalar per document type (a `dict` field would be JSON-decoded inside
    pydantic-settings' env source, before any validator runs). This turns that pair back into the
    mapping the extractor and the fixture FHIR client both want, in the one module that owns
    ``DocType`` — so ``config`` stays a leaf that knows nothing about the ingestion schemas, and
    neither caller has to spell the mapping out again.

    Args:
        lab_pdf: Path configured for a lab report, if any.
        intake_form: Path configured for an intake form, if any.

    Returns:
        The configured paths by document type; empty when neither is set.
    """
    configured = {DocType.LAB_PDF: lab_pdf, DocType.INTAKE_FORM: intake_form}
    return {doc_type: path for doc_type, path in configured.items() if path}


class SourceType(StrEnum):
    """Citation source vocabulary (W2_ARCHITECTURE §3.3).

    Extraction facts cite a document (``LAB_PDF`` / ``INTAKE_FORM``); the answer layer also cites
    ``GUIDELINE`` (RAG) and ``OPENEMR_RECORD`` (baseline chart). All four live here so the
    citation contract is one shared shape across the project.
    """

    LAB_PDF = "lab_pdf"
    INTAKE_FORM = "intake_form"
    GUIDELINE = "guideline"
    OPENEMR_RECORD = "openemr_record"


class AbnormalFlag(StrEnum):
    """Lab abnormal indicator.

    Values mirror OpenEMR ``procedure_result.abnormal`` so a derived result round-trips to the
    chart without remapping (W2_ARCHITECTURE §6).
    """

    NO = "no"
    YES = "yes"
    HIGH = "high"
    LOW = "low"


class BoundingBox(BaseModel):
    """Box locating a value on a source page, in PDF points, emitted by the extractor.

    Drives the click-to-source overlay (W2_ARCHITECTURE §3.3). Coordinates are PDF user-space points
    (72-DPI) with a top-left origin — the exact space the overlay renders in — so the overlay maps
    them straight onto the rendered page with no conversion. Boxes come from the PDF text layer
    where present (already in points); the scanned fallback converts its native pixels upstream.
    """

    model_config = ConfigDict(frozen=True)

    page: int = Field(ge=1, description="1-based page the box is on")
    x: float = Field(ge=0, description="Left edge in PDF points")
    y: float = Field(ge=0, description="Top edge in PDF points")
    width: float = Field(gt=0, description="Box width in PDF points")
    height: float = Field(gt=0, description="Box height in PDF points")


class Citation(BaseModel):
    """Machine-readable citation for one extracted fact — the W2 citation contract (§3.3).

    Contract shape: ``{source_type, source_id, page_or_section, field_or_chunk_id,
    quote_or_value}`` plus a native ``bounding_box`` for lab_pdf facts. Split by who fills what,
    exactly as Week 1's ``SourceRef`` is: the **extractor** supplies only what it read from the
    page (``quote_or_value`` and ``bounding_box``); everything that *identifies* the source is
    **stamped by code**, so the model can never fabricate a provenance pointer.
    """

    model_config = ConfigDict(frozen=True)

    quote_or_value: str = Field(
        min_length=1,
        description="The value/text EXACTLY as it appears on the source page, copied verbatim.",
    )
    bounding_box: BoundingBox | None = Field(
        default=None,
        description="Box locating the value on the page (PDF points). Required for lab_pdf facts.",
    )
    source_type: SourceType | None = Field(
        default=None,
        description="Leave empty — the system stamps this from the document's category.",
    )
    source_id: str | None = Field(
        default=None,
        description="Leave empty — the system stamps the source document id.",
    )
    page_or_section: str | None = Field(
        default=None,
        description="Leave empty — the system fills the page number from the bounding box.",
    )
    field_or_chunk_id: str | None = Field(
        default=None,
        description="Leave empty — the system stamps the schema field path this fact populates.",
    )


class LabResult(BaseModel):
    """One extracted lab analyte with its citation (PRD Core Req 2 — lab fields).

    Values are captured **verbatim as printed** — never rounded, converted, or inferred. Numeric
    parsing for the trend widget happens downstream at the persistence boundary, not here.

    ``loinc`` is **optional, deliberately.** OpenEMR publishes ``procedure_result.result_code`` as a
    LOINC code unconditionally, so write-back needs one and refuses a result without it (JOS-81).
    But a report that prints no codes must still yield usable facts — the value, the name and the
    box are what answer the physician's question — so requiring it here would turn "cannot persist
    this" into "cannot read this at all", which is a much worse failure. The two concerns are
    separate: extraction takes what the page offers; persistence sets its own bar.

    A code that fails validation (:func:`copilot.ingestion.loinc.parse`) arrives here as None rather
    than as a guess. A misread code silently mislabels *which test was run*, which is worse than no
    code, because nothing downstream can tell it from a correct one.
    """

    model_config = ConfigDict(frozen=True)

    test_name: str = Field(
        min_length=1,
        description="Analyte/test name exactly as printed, e.g. 'Hemoglobin A1c'.",
    )
    loinc: str | None = Field(
        default=None,
        description=(
            "The LOINC code printed beside the analyte, e.g. '2823-3'. Null when the report prints "
            "none. Read it off the page — never supply a code from memory."
        ),
    )
    value: str = Field(
        min_length=1,
        description="Result value verbatim, e.g. '8.2' or 'Positive'. Never round or convert.",
    )
    unit: str | None = Field(
        default=None, description="Unit as printed, e.g. '%'. Null if unitless/qualitative."
    )
    reference_range: str | None = Field(
        default=None, description="Reference range verbatim, e.g. '4.0-5.6'. Null if not printed."
    )
    collection_date: date | None = Field(
        default=None,
        description="Specimen collection date if printed (ISO 8601). Null if absent — never infer.",
    )
    abnormal_flag: AbnormalFlag = Field(
        description="Abnormal indicator; use 'no' if the report shows none."
    )
    citation: Citation = Field(description="Where on the source page this value was read from.")
    confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Extractor per-field confidence (0-1). System-set from the extractor.",
    )

    @model_validator(mode="after")
    def _require_bounding_box(self) -> "LabResult":
        """Enforce that every lab_pdf fact resolves to a pixel location on the source.

        A lab value with no bounding box cannot back the required click-to-source overlay, so it
        is a schema violation — surfaced upstream as a low-confidence refusal, never shipped with
        a fabricated rectangle (PRD Core Req 5; W2_ARCHITECTURE §3.3).

        Raises:
            ValueError: If the citation carries no bounding box.
        """
        if self.citation.bounding_box is None:
            raise ValueError("lab result citation must carry a bounding_box (PRD Core Req 5)")
        return self


class LabReport(BaseModel):
    """Strict-schema extraction of a ``lab_pdf`` document (PRD Core Req 2).

    The canonical contract for lab extraction: raw extractor output is parsed into this model at
    the ingestion boundary (W2_ARCHITECTURE §3.1 step 3). Anything the extractor emits that is not
    in this schema is dropped; a required field it omits fails validation and triggers a bounded
    retry.
    """

    model_config = ConfigDict(frozen=True)

    results: list[LabResult] = Field(
        description="Every lab result read from the report, each individually cited."
    )


class CitedText(BaseModel):
    """A single free-text intake value with its source citation."""

    model_config = ConfigDict(frozen=True)

    value: str = Field(min_length=1, description="The value verbatim as printed on the form.")
    citation: Citation = Field(description="Where on the form this value was read from.")
    confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Extractor per-field confidence (0-1). System-set from the extractor.",
    )


class Demographics(BaseModel):
    """Patient demographics captured from an intake form (PRD Core Req 2).

    Each field is captured verbatim; typed parsing (date of birth → date, sex → enum) happens at
    the persistence boundary, not here. Any field absent or illegible on the scan is null — never
    inferred.
    """

    model_config = ConfigDict(frozen=True)

    full_name: CitedText | None = Field(default=None, description="Patient full name as printed.")
    date_of_birth: CitedText | None = Field(default=None, description="Date of birth as printed.")
    sex: CitedText | None = Field(default=None, description="Sex/gender as printed.")
    address: CitedText | None = Field(default=None, description="Mailing address as printed.")
    phone: CitedText | None = Field(default=None, description="Contact phone as printed.")


class Medication(BaseModel):
    """A current medication reported on an intake form."""

    model_config = ConfigDict(frozen=True)

    name: str = Field(min_length=1, description="Medication name as printed, e.g. 'Metformin'.")
    dose: str | None = Field(
        default=None, description="Dose/strength as printed, e.g. '500 mg'. Null if not given."
    )
    frequency: str | None = Field(
        default=None, description="Frequency as printed, e.g. 'twice daily'. Null if not given."
    )
    citation: Citation = Field(description="Where on the form this medication was read from.")
    confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Extractor per-field confidence (0-1). System-set from the extractor.",
    )


class Allergy(BaseModel):
    """An allergy reported on an intake form."""

    model_config = ConfigDict(frozen=True)

    substance: str = Field(
        min_length=1, description="Allergen/substance as printed, e.g. 'Penicillin'."
    )
    reaction: str | None = Field(
        default=None, description="Reaction as printed, e.g. 'hives'. Null if not given."
    )
    citation: Citation = Field(description="Where on the form this allergy was read from.")
    confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Extractor per-field confidence (0-1). System-set from the extractor.",
    )


class FamilyHistoryItem(BaseModel):
    """One family-history entry reported on an intake form."""

    model_config = ConfigDict(frozen=True)

    condition: str = Field(
        min_length=1, description="Condition as printed, e.g. 'Type 2 diabetes'."
    )
    relation: str | None = Field(
        default=None, description="Affected relative, e.g. 'mother'. Null if not given."
    )
    citation: Citation = Field(description="Where on the form this entry was read from.")
    confidence: float | None = Field(
        default=None,
        ge=0,
        le=1,
        description="Extractor per-field confidence (0-1). System-set from the extractor.",
    )


class IntakeForm(BaseModel):
    """Strict-schema extraction of an ``intake_form`` document (PRD Core Req 2).

    The canonical contract for intake extraction (W2_ARCHITECTURE §3.1 step 3). List sections are
    always present but may be empty when the form reports none; an empty list means 'none read
    from the form' — which the answer layer treats as missing-data, not as an affirmative 'none'.
    """

    model_config = ConfigDict(frozen=True)

    demographics: Demographics = Field(description="Patient demographics block.")
    chief_concern: CitedText | None = Field(
        default=None, description="Chief concern / reason for visit as printed. Null if absent."
    )
    current_medications: list[Medication] = Field(
        description="Every current medication read from the form, each cited."
    )
    allergies: list[Allergy] = Field(description="Every allergy read from the form, each cited.")
    family_history: list[FamilyHistoryItem] = Field(
        description="Every family-history entry read from the form, each cited."
    )
