from dataclasses import dataclass

from copilot.fhir.client import FhirClient
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

    Both registries accumulate across the conversation (like ``CopilotDeps.fetched`` today), so a
    follow-up turn can cite a record an earlier turn read or a guideline chunk an earlier turn
    retrieved.
    """

    fhir: FhirClient
    patient_id: str
    correlation_id: str
    retriever: EvidenceRetriever
    fetched: FetchLog
    chunks: ChunkRegistry
