from dataclasses import dataclass, field

from copilot.fhir.client import FhirClient
from copilot.fhir.models import UploadedDocumentSummary
from copilot.ingestion.extractor import DocumentExtractor, ExtractedDocument
from copilot.ingestion.registry import DocumentFactHandle, DocumentFactRegistry
from copilot.rag.retriever import EvidenceRetriever
from copilot.retrieval import ChunkRegistry
from copilot.verification import FetchLog


@dataclass
class GraphDeps:
    """Per-request dependencies shared across the supervisor and both workers.

    The Week-2 superset of the Week-1 ``CopilotDeps``: it still carries the patient-scoped
    ``fhir`` client and the FHIR ``fetched`` log the record grounding gate reads, and adds the
    guideline-evidence side — the ``retriever`` seam (JOS-53) the evidence-retriever calls and the
    ``chunks`` registry its grounding gate reads. One deps object threads through the whole graph
    (supervisor delegates to workers with the same ``ctx.deps``), so every worker's reads
    accumulate into the two registries the final answer grounds against.

    All three registries accumulate across the conversation (like ``CopilotDeps.fetched`` today), so
    a follow-up turn can cite a record an earlier turn read, a guideline chunk an earlier turn
    retrieved, or a lab fact an earlier turn extracted.

    The document side (JOS-54): ``extractor`` OCRs an uploaded document — a lab report or an intake
    form, whichever its OpenEMR category says it is — into cited facts (None when extraction is
    unconfigured, and the intake-extractor then reports no document); ``documents`` is the registry
    those facts are grounded against, joined into the intake-extractor's and the final answer's
    grounding gates alongside ``fetched``/``chunks``.
    """

    fhir: FhirClient
    patient_id: str
    correlation_id: str
    retriever: EvidenceRetriever
    fetched: FetchLog
    chunks: ChunkRegistry
    documents: DocumentFactRegistry
    extractor: DocumentExtractor | None
    # Per-turn memo for list_documents: the discovery FHIR read happens once, so repeated tool
    # calls (e.g. a model retrying on an empty result) are cheap cache hits, not extra round-trips.
    documents_cache: list[UploadedDocumentSummary] | None = None
    # Per-turn memo for attach_and_extract, keyed by document id: OCR (Binary fetch + Mistral) is
    # the expensive hop, so re-extracting the same document in a turn returns the recorded handles.
    # The handles are the union: which kind a document yields is decided by its type, not the
    # caller.
    extracted_documents: dict[str, list[DocumentFactHandle]] = field(default_factory=dict)
    # Per-turn store of the full typed extraction, keyed by document id. The registry normalizes a
    # fact down to value + citation and drops the rest (units, range, abnormal flag, and — retained
    # nowhere else — the LOINC code), so the write-back payload (JOS-81) needs the source object.
    # Per-turn like extracted_documents: we persist what THIS turn read off the document.
    extractions: dict[str, ExtractedDocument] = field(default_factory=dict)
