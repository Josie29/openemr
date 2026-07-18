from copilot.fhir.models import Allergy as FhirAllergy
from copilot.fhir.models import Medication as FhirMedication
from copilot.fhir.models import PatientDemographics
from copilot.ingestion.extractor import ExtractedDocument
from copilot.ingestion.registry import DocumentFactRegistry, FactKind
from copilot.ingestion.schemas import (
    AbnormalFlag,
    Allergy,
    BoundingBox,
    Citation,
    CitedText,
    Demographics,
    DocType,
    IntakeForm,
    LabReport,
    LabResult,
    Medication,
    MedicationList,
)
from copilot.schemas import (
    Claim,
    FhirCitation,
    IntakeFormCitation,
    LabPdfCitation,
    MedicationListCitation,
    SourceRef,
)
from copilot.verification import CompositeResolver, FetchLog, ground_claims

# FHIR logical ids are uuids; document-fact ids are `<document_id>#<ordinal>`. The two namespaces
# never overlap — which is the whole point of the first test below.
_FHIR_PATIENT_ID = "8a1e0b3c-1f6d-4c2e-9d55-2b7a6f0c4e11"
_FHIR_ALLERGY_ID = "c4f2d9a7-3b81-4e5f-8a0c-6d19e2b7f503"
_FHIR_MEDICATION_ID = "2d70b8e5-9c14-4a63-b7f8-05ce31a9d6b2"


def _box(page: int = 1) -> BoundingBox:
    """A stand-in overlay box; its exact geometry is irrelevant to these tests."""
    return BoundingBox(page=page, x=72.0, y=144.0, width=96.0, height=12.0)


def _citation(value: str, page: int = 1) -> Citation:
    """The extractor's citation for a value it located on the page."""
    return Citation(quote_or_value=value, bounding_box=_box(page))


def _cited(value: str) -> CitedText:
    """One free-text intake value, located on the form."""
    return CitedText(value=value, citation=_citation(value))


def _intake_form() -> IntakeForm:
    """A minimal intake form stating one demographic and one allergy."""
    return IntakeForm(
        demographics=Demographics(full_name=_cited("Sergio Angulo")),
        allergies=[
            Allergy(substance="Penicillin", reaction="hives", citation=_citation("Penicillin"))
        ],
        family_history=[],
    )


def _medication_list() -> MedicationList:
    """A minimal medication list stating one cited medication."""
    return MedicationList(
        medications=[
            Medication(
                name="Metformin",
                dose="500 mg",
                frequency="twice daily",
                citation=_citation("Metformin"),
            )
        ]
    )


def _lab_report() -> LabReport:
    """A minimal lab report with one cited, boxed result."""
    return LabReport(
        results=[
            LabResult(
                test_name="Creatinine",
                value="1.44",
                unit="mg/dL",
                abnormal_flag=AbnormalFlag.HIGH,
                citation=_citation("1.44", page=2),
            )
        ]
    )


def _intake_document(document_id: str = "doc-intake") -> ExtractedDocument:
    """One extracted intake form, ready to record."""
    return ExtractedDocument(
        document_id=document_id, doc_type=DocType.INTAKE_FORM, report=_intake_form()
    )


def _lab_document(document_id: str = "doc-lab") -> ExtractedDocument:
    """One extracted lab report, ready to record."""
    return ExtractedDocument(
        document_id=document_id, doc_type=DocType.LAB_PDF, report=_lab_report()
    )


def _medication_list_document(document_id: str = "doc-meds") -> ExtractedDocument:
    """One extracted medication list, ready to record."""
    return ExtractedDocument(
        document_id=document_id, doc_type=DocType.MEDICATION_LIST, report=_medication_list()
    )


def test_document_facts_and_fetched_records_ground_side_by_side_under_a_shared_tag() -> None:
    """A document fact and a FHIR record sharing a resource_type each ground to their own value.

    If this breaks, one resolver shadows the other and a claim grounds against the wrong source —
    an intake form's self-reported "Penicillin" answering as the chart's confirmed allergy, or the
    reverse. Either way the physician reads a value attributed to a source it never came from.
    """
    # Intake facts are tagged by their eventual write target, so Patient / AllergyIntolerance /
    # MedicationRequest now name BOTH a record a read tool fetches and a fact read off a form. That
    # retires the old type-disjointness guarantee; what replaces it is ID-disjointness — a document
    # fact's `docid#ordinal` is never a FHIR uuid, so each resolver misses the other's ids and
    # CompositeResolver's first-non-None dispatch stays unambiguous. FamilyMemberHistory is left out
    # deliberately: no read tool fetches it, so it cannot collide with the FetchLog.
    fetched = FetchLog()
    fetched.record_all(PatientDemographics(resource_id=_FHIR_PATIENT_ID, full_name="Marisol Reyes"))
    fetched.record_all(FhirAllergy(resource_id=_FHIR_ALLERGY_ID, substance="Latex"))
    fetched.record_all(FhirMedication(resource_id=_FHIR_MEDICATION_ID, name="Lisinopril"))
    documents = DocumentFactRegistry()
    # Demographics + allergies come from the intake form; medications from a medication list — the
    # two document types are mutually exclusive in what they extract.
    by_kind = {
        handle.kind: handle
        for handle in (*documents.record(_intake_document()), *documents.record(_medication_list_document()))
    }
    resolver = CompositeResolver((fetched, documents))

    fhir_cases = (
        ("Patient", _FHIR_PATIENT_ID, "full_name", "Marisol Reyes"),
        ("AllergyIntolerance", _FHIR_ALLERGY_ID, "substance", "Latex"),
        ("MedicationRequest", _FHIR_MEDICATION_ID, "name", "Lisinopril"),
    )
    for resource_type, resource_id, field, expected in fhir_cases:
        resolution = resolver.resolve(
            SourceRef(resource_type=resource_type, resource_id=resource_id, field=field)
        )
        assert resolution is not None
        assert resolution.value == expected  # the chart's value, not the document's
        assert resolution.doc_type is None  # a fetched record has no document provenance
        assert resolution.document_id is None

    document_cases = (
        (FactKind.DEMOGRAPHIC, "Patient", "Sergio Angulo", "doc-intake", DocType.INTAKE_FORM),
        (FactKind.ALLERGY, "AllergyIntolerance", "Penicillin", "doc-intake", DocType.INTAKE_FORM),
        (FactKind.MEDICATION, "MedicationRequest", "Metformin", "doc-meds", DocType.MEDICATION_LIST),
    )
    for kind, resource_type, expected, document_id, doc_type in document_cases:
        handle = by_kind[kind]
        assert handle.resource_type == resource_type  # tagged by write target, shared with FHIR
        resolution = resolver.resolve(
            SourceRef(resource_type=handle.resource_type, resource_id=handle.resource_id)
        )
        assert resolution is not None
        assert resolution.value == expected  # the document's value, not the chart's
        assert resolution.document_id == document_id
        assert resolution.doc_type is doc_type


def test_intake_fact_projects_to_an_intake_form_citation() -> None:
    """An intake fact's grounded citation projects to IntakeFormCitation, carrying page and box.

    Routing is by doc_type, not by resource type or by the box's presence; if it regressed, an
    intake fact would serialize as `source_type: "lab_pdf"` and the sidebar would attribute the
    physician's evidence to a lab report the patient never had.
    """
    documents = DocumentFactRegistry()
    handle = documents.record(_intake_document())[0]
    claim = Claim(
        text="The form gives the patient's name as Sergio Angulo.",
        source=SourceRef(resource_type=handle.resource_type, resource_id=handle.resource_id),
    )

    grounded, offenders = ground_claims([claim], documents)

    assert not offenders
    citation = grounded[0].source.to_citation()
    assert isinstance(citation, IntakeFormCitation)
    # Asserted on the SERIALIZED form, not the attribute: the attribute's type already pins the tag,
    # so checking it here would be a tautology. What consumers actually read is the wire value that
    # main.py emits via `to_citation().model_dump(mode="json")`.
    assert citation.model_dump(mode="json")["source_type"] == "intake_form"
    assert citation.source_id == "doc-intake"
    assert citation.quote_or_value == "Sergio Angulo"  # stamped by code from the recorded fact
    assert citation.page == 1
    assert citation.bounding_box == _box()


def test_medication_list_fact_projects_to_a_medication_list_citation() -> None:
    """A medication-list fact's grounded citation projects to MedicationListCitation.

    Routing is by doc_type, so if it regressed a medication read off a medication list would
    serialize as `source_type: "intake_form"` and the sidebar would mislabel the provenance of the
    physician's evidence — the exact confusion the distinct source type exists to prevent.
    """
    documents = DocumentFactRegistry()
    handle = documents.record(_medication_list_document())[0]
    claim = Claim(
        text="The medication list includes Metformin.",
        source=SourceRef(resource_type=handle.resource_type, resource_id=handle.resource_id),
    )

    grounded, offenders = ground_claims([claim], documents)

    assert not offenders
    citation = grounded[0].source.to_citation()
    assert isinstance(citation, MedicationListCitation)
    assert citation.model_dump(mode="json")["source_type"] == "medication_list"
    assert citation.source_id == "doc-meds"
    assert citation.quote_or_value == "Metformin"  # stamped by code from the recorded fact
    assert citation.page == 1
    assert citation.bounding_box == _box()


def test_lab_fact_still_projects_to_a_lab_pdf_citation() -> None:
    """A lab fact keeps projecting to LabPdfCitation now that routing moved to doc_type.

    The lab path shipped first and the sidebar consumes it; re-routing intake must not silently
    re-tag every existing lab citation.
    """
    documents = DocumentFactRegistry()
    handle = documents.record(_lab_document())[0]
    claim = Claim(
        text="Creatinine was 1.44 mg/dL (high).",
        source=SourceRef(
            resource_type=handle.resource_type, resource_id=handle.resource_id, field="value"
        ),
    )

    grounded, offenders = ground_claims([claim], documents)

    assert not offenders
    citation = grounded[0].source.to_citation()
    assert isinstance(citation, LabPdfCitation)
    assert citation.model_dump(mode="json")["source_type"] == "lab_pdf"
    assert citation.quote_or_value == "1.44"
    assert citation.page == 2
    assert citation.bounding_box == _box(page=2)


def test_stamp_strips_model_authored_document_provenance_from_a_fhir_claim() -> None:
    """A FHIR claim projects to FhirCitation, with any model-authored doc_type and box stripped.

    doc_type is what selects the document citation arm, so a model that invents one on a plain
    chart claim would get a source-overlay drawn over a scan the fact was never read from — the
    exact laundering the gate exists to prevent.
    """
    fetched = FetchLog()
    fetched.record_all(PatientDemographics(resource_id=_FHIR_PATIENT_ID, full_name="Marisol Reyes"))
    claim = Claim(
        text="The patient is Marisol Reyes.",
        source=SourceRef(
            resource_type="Patient",
            resource_id=_FHIR_PATIENT_ID,
            field="full_name",
            doc_type=DocType.INTAKE_FORM,  # fabricated: this fact came from the chart, not a form
            document_id="doc-intake",
            page=1,
            bounding_box=_box(),
        ),
    )

    grounded, offenders = ground_claims([claim], fetched)

    assert not offenders  # the claim itself grounds normally
    stamped = grounded[0].source
    assert stamped.value == "Marisol Reyes"
    assert stamped.doc_type is None
    assert stamped.document_id is None
    assert stamped.bounding_box is None
    assert isinstance(stamped.to_citation(), FhirCitation)


def test_claim_citing_an_unrecorded_document_fact_does_not_ground() -> None:
    """A claim citing a document fact this turn did not extract is refused as an offender.

    Without this, a model could cite a plausible-looking `docid#ordinal` it never read and have the
    gate wave through a fabricated fact under real document provenance.
    """
    documents = DocumentFactRegistry()
    documents.record(_intake_document())
    claim = Claim(
        text="The form reports an allergy to sulfa drugs.",
        source=SourceRef(resource_type="AllergyIntolerance", resource_id="doc-intake#99"),
    )

    grounded, offenders = ground_claims([claim], documents)

    assert not grounded
    assert offenders == [claim]
