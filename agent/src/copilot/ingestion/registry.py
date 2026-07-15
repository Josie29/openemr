from dataclasses import dataclass, field

from pydantic import BaseModel, ConfigDict, Field

from copilot.fhir.models import ResourceIdentity
from copilot.ingestion.extractor import ExtractedDocument
from copilot.ingestion.schemas import LabResult
from copilot.schemas import SourceRef
from copilot.verification import Resolution

# The resource-type tag a document-extracted lab fact carries on its SourceRef. A derived lab value
# round-trips to OpenEMR as a FHIR `Observation` (W2_ARCHITECTURE §6), so the claim cites
# ("Observation", <fact id>) and this registry grounds it — flowing through the SAME SourceRef/gate
# machinery as a FHIR-record or guideline claim. No FHIR read tool fetches Observations, so this
# resource type is unique to document facts and never collides with the FetchLog. `to_citation`
# routes a stamped SourceRef to a LabPdfCitation on the presence of the bounding box, not this tag.
DOCUMENT_FACT_RESOURCE_TYPE = "Observation"


class LabFactHandle(BaseModel):
    """The citable view of one extracted lab fact returned by ``attach_and_extract``.

    Carries the citation handle (``resource_type``/``resource_id``) the model must copy verbatim
    into a claim, plus the human-readable fields it states — mirroring how ``search_guidelines``
    returns snippets the model then cites. The overlay geometry is NOT exposed here: it is stamped
    onto the ``SourceRef`` by the grounding gate (code-authored), never by the model.
    """

    model_config = ConfigDict(frozen=True)

    resource_type: str = Field(description="Cite this verbatim as the claim's source resource_type")
    resource_id: str = Field(description="Cite this verbatim as the claim's source resource_id")
    test_name: str = Field(description="Analyte/test name as printed on the report")
    value: str = Field(description="Result value verbatim")
    unit: str | None = Field(default=None, description="Unit as printed, if any")
    reference_range: str | None = Field(default=None, description="Reference range, if printed")
    abnormal_flag: str = Field(description="Abnormal indicator: no | yes | high | low")


@dataclass(frozen=True)
class _RecordedFact:
    """One extracted lab fact plus the source document it grounds against."""

    result: LabResult
    document_id: str


@dataclass
class DocumentFactRegistry:
    """Registry of the lab facts a turn extracted — the document-extraction resolver (JOS-54).

    The extraction counterpart to :class:`~copilot.verification.FetchLog` (FHIR records) and
    :class:`~copilot.retrieval.ChunkRegistry` (guideline chunks): ``attach_and_extract`` records the
    facts it read from a document, and this resolves a claim's citation against them so the one
    grounding gate that checks FHIR and guideline claims also checks document facts. A claim grounds
    only when it cites a fact recorded this turn; its value is stamped from the recorded fact (never
    the model's say-so), and the click-to-source overlay provenance — document id, page, and box
    (already in PDF points from the extractor) — is stamped alongside it for the sidebar (the JOS-57
    seam).
    """

    _facts: dict[str, _RecordedFact] = field(default_factory=dict)

    def record(self, extracted: ExtractedDocument) -> list[LabFactHandle]:
        """Record an extracted document's lab facts and return their citable handles.

        Args:
            extracted: One document's strict extraction (a cited ``LabReport``).

        Returns:
            One :class:`LabFactHandle` per lab fact, for the model to state and cite.
        """
        handles: list[LabFactHandle] = []
        for ordinal, result in enumerate(extracted.report.results):
            resource_id = f"{extracted.document_id}#{ordinal}"
            self._facts[resource_id] = _RecordedFact(
                result=result, document_id=extracted.document_id
            )
            handles.append(
                LabFactHandle(
                    resource_type=DOCUMENT_FACT_RESOURCE_TYPE,
                    resource_id=resource_id,
                    test_name=result.test_name,
                    value=result.value,
                    unit=result.unit,
                    reference_range=result.reference_range,
                    abnormal_flag=result.abnormal_flag.value,
                )
            )
        return handles

    def resolve(self, ref: SourceRef) -> Resolution | None:
        """Ground a document-fact citation and return its value plus click-to-source overlay.

        Args:
            ref: The claim's citation (expected to name a recorded document fact).

        Returns:
            The :class:`~copilot.verification.Resolution` (value + identity + document id/page/box
            in PDF points) when the fact was recorded this turn; otherwise None (wrong resource type
            or an unrecorded id).
        """
        if ref.resource_type != DOCUMENT_FACT_RESOURCE_TYPE:
            return None
        fact = self._facts.get(ref.resource_id)
        if fact is None:
            return None
        result = fact.result
        box = result.citation.bounding_box  # already in PDF points from the extractor
        return Resolution(
            value=result.value,
            identity=ResourceIdentity(
                label=result.test_name,
                date=result.collection_date.isoformat() if result.collection_date else None,
                date_label="Collected",
            ),
            document_id=fact.document_id,
            page=box.page if box is not None else None,
            bounding_box=box,
        )
