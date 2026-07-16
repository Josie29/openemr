import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from copilot.fhir.fixtures import FixtureFhirClient
from copilot.fhir.models import UploadedDocumentSummary
from copilot.graph.deps import GraphDeps
from copilot.graph.outputs import ExtractorOutput
from copilot.graph.workers import build_intake_extractor
from copilot.ingestion.extractor import (
    DocumentExtractor,
    ExtractedDocument,
    FixtureOcrBackend,
    FixturePdfByteSource,
    map_lab_report,
)
from copilot.ingestion.pdf_geometry import Word, extract_word_boxes, locate_value_box
from copilot.ingestion.registry import (
    DOCUMENT_FACT_RESOURCE_TYPE,
    DocumentFactHandle,
    DocumentFactRegistry,
    LabFactHandle,
)
from copilot.ingestion.schemas import AbnormalFlag, DocType, IntakeForm, LabReport, LabResult
from copilot.retrieval import ChunkRegistry
from copilot.schemas import CitationSourceType, Claim, LabPdfCitation, SourceRef
from copilot.verification import FetchLog, ground_claims
from graph_script import StubRetriever

_FIXTURES = Path(__file__).parent / "fixtures" / "documents"
_LAB_OCR = _FIXTURES / "extractions" / "sergio-angulo-lab-report.ocr.json"
_LAB_PDF = _FIXTURES / "pdfs" / "sergio-angulo-lab-report.pdf"


def _ocr() -> dict[str, Any]:
    parsed: dict[str, Any] = json.loads(_LAB_OCR.read_text())
    return parsed


def _words() -> list[Word]:
    return extract_word_boxes(_LAB_PDF.read_bytes())


def _lab_handles(handles: list[DocumentFactHandle], test_name: str) -> Iterator[LabFactHandle]:
    """Yield the recorded lab handles for a test name, narrowing the registry's handle union.

    The registry records whatever kind of fact a document yields (a lab result, an allergy, a
    demographic), so its handles are a tagged union; these tests are about the lab arm.
    """
    return (h for h in handles if isinstance(h, LabFactHandle) and h.test_name == test_name)


def _find(report: LabReport | IntakeForm, test_name: str) -> LabResult | None:
    """Return the extracted result with the given test name, or None.

    Takes the union `ExtractedDocument.report` now carries (a lab_pdf maps to a LabReport, an
    intake_form to an IntakeForm) and narrows to the lab side these tests are about.
    """
    if not isinstance(report, LabReport):
        return None
    return next((r for r in report.results if r.test_name == test_name), None)


def test_pdf_geometry_locates_a_tight_box_on_the_right_row() -> None:
    """The pdfplumber join boxes the value tightly on its own row, not the interpretive narrative.

    The report repeats "1.44"/"54" in the bottom narrative ("creatinine 1.06 -> 1.44"); a value-only
    match would highlight THAT instead of the result cell. Anchoring on the test name prevents it —
    if this breaks, click-to-source points the clinician at prose, not the lab value.
    """
    words = _words()
    assert words, "the digital fixture must expose a text layer"
    located = locate_value_box("Creatinine", "1.44", words, 0)
    assert located is not None
    box, _ = located
    assert box.width < 40  # a tight box on "1.44", not a full-width row band
    assert 280 < box.y < 292  # the result row (~283.6), NOT the narrative down near y~599


def test_map_lab_report_extracts_values_flags_and_tight_boxes() -> None:
    """Every lab value is extracted verbatim, flagged correctly, and carries a tight points box.

    Guards the whole document path: wrong values/flags, or a box that can't place the value, defeats
    the point of the click-to-source overlay.
    """
    report = map_lab_report(_ocr(), _words())
    assert len(report.results) >= 20  # the CMP + CBC panels, one result each

    creatinine = _find(report, "Creatinine")
    assert creatinine is not None
    assert creatinine.value == "1.44"  # verbatim, never rounded
    assert creatinine.abnormal_flag is AbnormalFlag.HIGH  # printed "H"
    box = creatinine.citation.bounding_box
    assert box is not None
    assert box.width < 40  # tight box on the value, in points — not the old full-width band
    for result in report.results:
        assert result.citation.bounding_box is not None  # the schema + overlay require a box


def test_boxes_follow_table_row_order() -> None:
    """A later analyte sits lower on the page than an earlier one — boxes track the real rows."""
    report = map_lab_report(_ocr(), _words())
    glucose = _find(report, "Glucose, Fasting")
    creatinine = _find(report, "Creatinine")
    assert glucose is not None and glucose.citation.bounding_box is not None
    assert creatinine is not None and creatinine.citation.bounding_box is not None
    assert creatinine.citation.bounding_box.y > glucose.citation.bounding_box.y


def test_falls_back_to_coarse_estimate_without_a_text_layer() -> None:
    """A scanned PDF exposes no text layer (empty words); every value still gets a points box.

    This is the graceful-degradation path — a scan loses the tight box but keeps the clinical facts
    on a coarse row/page band rather than dropping them.
    """
    report = map_lab_report(_ocr(), [])  # empty words simulates a scanned/image-only PDF
    assert len(report.results) >= 20
    creatinine = _find(report, "Creatinine")
    assert creatinine is not None and creatinine.citation.bounding_box is not None
    # The fallback is a wide band (converted to points), not the tight text-layer box.
    assert creatinine.citation.bounding_box.width > 100


async def test_fixture_extractor_round_trip() -> None:
    """The DocumentExtractor wires byte-source + OCR backend + geometry end to end."""
    extractor = DocumentExtractor(ocr=FixtureOcrBackend({DocType.LAB_PDF: str(_LAB_OCR)}))
    extracted = await extractor.extract(
        "doc-abc", DocType.LAB_PDF, FixturePdfByteSource(str(_LAB_PDF))
    )
    assert extracted.document_id == "doc-abc"
    assert extracted.doc_type is DocType.LAB_PDF
    potassium = _find(extracted.report, "Potassium")
    assert potassium is not None and potassium.citation.bounding_box is not None
    assert potassium.citation.bounding_box.width < 40  # tight text-layer box


def test_registry_passes_the_points_box_through_unchanged() -> None:
    """The registry stamps the extractor's box as-is — it is already in PDF points, no conversion.

    If a stray conversion crept back in, the overlay would render ~1.3x off (the JOS-57 space bug).
    """
    report = map_lab_report(_ocr(), _words())
    registry = DocumentFactRegistry()
    handles = registry.record(
        ExtractedDocument(document_id="doc-xyz", doc_type=DocType.LAB_PDF, report=report)
    )
    assert handles and all(h.resource_type == DOCUMENT_FACT_RESOURCE_TYPE for h in handles)

    creatinine = _find(report, "Creatinine")
    assert creatinine is not None
    handle = next(_lab_handles(handles, "Creatinine"))
    resolution = registry.resolve(
        SourceRef(resource_type=handle.resource_type, resource_id=handle.resource_id, field="value")
    )
    assert resolution is not None
    assert resolution.value == "1.44"
    assert resolution.document_id == "doc-xyz"
    assert resolution.bounding_box == creatinine.citation.bounding_box  # passthrough, no conversion


def test_registry_ignores_unrecorded_and_foreign_citations() -> None:
    """A claim citing a fact the turn did not extract must not ground (no fabricated provenance)."""
    registry = DocumentFactRegistry()  # empty — nothing recorded this turn
    assert (
        registry.resolve(
            SourceRef(
                resource_type=DOCUMENT_FACT_RESOURCE_TYPE, resource_id="never#0", field="value"
            )
        )
        is None
    )
    # A FHIR citation is not this registry's to resolve — it defers (returns None) to the FetchLog.
    fhir_ref = SourceRef(resource_type="Condition", resource_id="c1", field="x")
    assert registry.resolve(fhir_ref) is None


def test_grounded_document_claim_projects_to_lab_pdf_citation() -> None:
    """End-to-end stamp: a claim citing an extracted fact grounds and projects to a LabPdfCitation.

    This is the seam the sidebar consumes — if the box/document_id don't land on the SourceRef and
    survive to_citation(), click-to-source shows no overlay.
    """
    report = map_lab_report(_ocr(), _words())
    registry = DocumentFactRegistry()
    handles = registry.record(
        ExtractedDocument(document_id="doc-777", doc_type=DocType.LAB_PDF, report=report)
    )
    handle = next(_lab_handles(handles, "Creatinine"))
    claim = Claim(
        text="Creatinine was 1.44 mg/dL (high).",
        source=SourceRef(
            resource_type=handle.resource_type, resource_id=handle.resource_id, field="value"
        ),
    )

    grounded, offenders = ground_claims([claim], registry)
    assert not offenders  # the claim cites a fact recorded this turn, so it grounds
    stamped = grounded[0].source
    assert stamped.value == "1.44"  # stamped by code from the extracted fact
    assert stamped.document_id == "doc-777"
    assert stamped.bounding_box is not None

    citation = stamped.to_citation()
    assert isinstance(citation, LabPdfCitation)
    assert citation.source_type is CitationSourceType.LAB_PDF
    assert citation.source_id == "doc-777"  # the document, not the synthetic Observation key
    assert citation.bounding_box == stamped.bounding_box  # the overlay box survives the projection


def _final_tool_name(info: AgentInfo) -> str:
    """Return the structured-output tool name for the current Pydantic AI version."""
    tools = getattr(info, "output_tools", None) or getattr(info, "result_tools", None) or []
    return tools[0].name if tools else "final_result"


async def test_intake_extractor_extracts_and_grounds_a_lab_fact() -> None:
    """The intake-extractor's attach_and_extract tool feeds its own grounding gate end-to-end.

    Drives the worker with a scripted model that extracts the uploaded report, then cites one lab
    fact. If the tool didn't record into the document registry, or the gate didn't join it, the
    claim would be rejected as ungrounded — so this guards the whole tool -> registry -> gate ->
    overlay-stamp path the sidebar depends on.
    """
    extractor = DocumentExtractor(ocr=FixtureOcrBackend({DocType.LAB_PDF: str(_LAB_OCR)}))
    deps = GraphDeps(
        # get_document_bytes serves this fixture PDF, so attach_and_extract's FhirBinaryByteSource
        # exercises the real fetch -> OCR path offline (mirroring the Binary fetch in prod).
        fhir=FixtureFhirClient.from_seed({DocType.LAB_PDF: str(_LAB_PDF)}),
        patient_id="1",
        correlation_id="test-cid",
        retriever=StubRetriever(snippets=()),
        extractor=extractor,
        fetched=FetchLog(),
        chunks=ChunkRegistry(),
        documents=DocumentFactRegistry(),
        # Pre-seed discovery: attach_and_extract only extracts a doc list_lab_documents returned.
        documents_cache=[
            UploadedDocumentSummary(
                resource_id="labreport-2026-07", doc_type=DocType.LAB_PDF, title="lab.pdf"
            )
        ],
    )

    state = {"extracted": False}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not state["extracted"]:
            state["extracted"] = True
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="attach_and_extract",
                        args={"document_id": "labreport-2026-07"},
                    )
                ]
            )
        # Cite the first extracted fact — Glucose is ordinal 0, so its id is
        # "labreport-2026-07#0" (the seeded document id, which the fixture byte source keys on).
        output = ExtractorOutput(
            summary="Fasting glucose is high.",
            claims=[
                Claim(
                    text="Fasting glucose was 108 mg/dL (high).",
                    source=SourceRef(
                        resource_type=DOCUMENT_FACT_RESOURCE_TYPE,
                        resource_id="labreport-2026-07#0",
                        field="value",
                    ),
                )
            ],
        )
        args = output.model_dump(mode="json")
        return ModelResponse(parts=[ToolCallPart(tool_name=_final_tool_name(info), args=args)])

    agent = build_intake_extractor(FunctionModel(respond))
    result = await agent.run("What was the fasting glucose?", deps=deps)

    source = result.output.claims[0].source
    assert source.value == "108"  # stamped by the gate from the extracted fact, not the model
    assert source.document_id == "labreport-2026-07"
    assert source.bounding_box is not None
    assert source.to_citation().source_type is CitationSourceType.LAB_PDF


class _SpyExtractor(DocumentExtractor):
    """A DocumentExtractor that counts extract() calls, delegating to the fixture pipeline."""

    def __init__(self) -> None:
        super().__init__(ocr=FixtureOcrBackend({DocType.LAB_PDF: str(_LAB_OCR)}))
        self.calls = 0

    async def extract(
        self, document_id: str, doc_type: DocType, byte_source: object
    ) -> ExtractedDocument:
        self.calls += 1
        return await super().extract(document_id, doc_type, byte_source)  # type: ignore[arg-type]


def _extractor_deps(
    extractor: DocumentExtractor, cache: list[UploadedDocumentSummary]
) -> GraphDeps:
    return GraphDeps(
        fhir=FixtureFhirClient.from_seed({DocType.LAB_PDF: str(_LAB_PDF)}),
        patient_id="1",
        correlation_id="test-cid",
        retriever=StubRetriever(snippets=()),
        extractor=extractor,
        fetched=FetchLog(),
        chunks=ChunkRegistry(),
        documents=DocumentFactRegistry(),
        documents_cache=cache,
    )


async def test_attach_and_extract_ignores_undiscovered_document() -> None:
    """A document_id list_lab_documents never surfaced is a no-op: no Binary fetch, no OCR, no fact.

    Guards against the model guessing an id and wasting the expensive extraction hop.
    """
    spy = _SpyExtractor()
    deps = _extractor_deps(
        spy,
        [
            UploadedDocumentSummary(
                resource_id="real-doc", doc_type=DocType.LAB_PDF, title="lab.pdf"
            )
        ],
    )
    state = {"tried": False}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not state["tried"]:
            state["tried"] = True
            return ModelResponse(
                parts=[ToolCallPart(tool_name="attach_and_extract", args={"document_id": "ghost"})]
            )
        out = ExtractorOutput(summary="No lab document available.", claims=[])
        return ModelResponse(
            parts=[ToolCallPart(tool_name=_final_tool_name(info), args=out.model_dump(mode="json"))]
        )

    result = await build_intake_extractor(FunctionModel(respond)).run("synthesize", deps=deps)
    assert spy.calls == 0  # never fetched/OCR'd the guessed id
    assert result.output.claims == []  # nothing fabricated


async def test_attach_and_extract_memoizes_per_document() -> None:
    """Re-extracting the same document in a turn returns the recorded handles — OCR runs once."""
    spy = _SpyExtractor()
    deps = _extractor_deps(
        spy,
        [
            UploadedDocumentSummary(
                resource_id="labreport-2026-07", doc_type=DocType.LAB_PDF, title="lab.pdf"
            )
        ],
    )
    state = {"i": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        state["i"] += 1
        if state["i"] <= 2:  # call attach_and_extract twice on the same doc
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="attach_and_extract",
                        args={"document_id": "labreport-2026-07"},
                    )
                ]
            )
        out = ExtractorOutput(
            summary="Glucose high.",
            claims=[
                Claim(
                    text="Fasting glucose 108 (high).",
                    source=SourceRef(
                        resource_type=DOCUMENT_FACT_RESOURCE_TYPE,
                        resource_id="labreport-2026-07#0",
                        field="value",
                    ),
                )
            ],
        )
        return ModelResponse(
            parts=[ToolCallPart(tool_name=_final_tool_name(info), args=out.model_dump(mode="json"))]
        )

    result = await build_intake_extractor(FunctionModel(respond)).run("synthesize", deps=deps)
    assert spy.calls == 1  # extracted once despite two attach_and_extract calls
    assert result.output.claims[0].source.value == "108"  # the memoized fact still grounds


def _ocr_with(results: object, blocks: list[dict[str, object]]) -> dict[str, object]:
    """Build a minimal OCR response with the given annotation results and page blocks."""
    return {
        "document_annotation": json.dumps({"results": results}),
        "pages": [
            {
                "index": 0,
                "dimensions": {"dpi": 93, "width": 791, "height": 1023},
                "blocks": blocks,
                "tables": [],
            }
        ],
    }


def test_blank_value_or_name_rows_are_skipped_not_crashed() -> None:
    """A row with a blank value/name is dropped, not crashed on (schema min_length regression).

    Live OCR emits spacer/subtotal/pending rows with an empty value or test name. Since the schema
    now enforces min_length=1 on LabResult.value/test_name and Citation.quote_or_value, building a
    fact from such a row raises a ValidationError that escapes map_lab_report's ExtractionError
    contract and crashes the physician's whole /chat turn. The mapper must skip these rows instead.
    """
    ocr = _ocr_with(
        [
            {"test_name": "Glucose", "value": "108", "abnormal_flag": "H"},
            {"test_name": "", "value": "  ", "abnormal_flag": "no"},  # spacer/subtotal row
            {"test_name": "Potassium", "value": "", "abnormal_flag": "no"},  # pending result
            {"test_name": "Sodium", "value": "140", "abnormal_flag": "no"},
        ],
        [
            {
                "type": "table",
                "content": "<table><tr><td>Glucose</td><td>108</td></tr></table>",
                "top_left_x": 10,
                "top_left_y": 10,
                "bottom_right_x": 100,
                "bottom_right_y": 40,
            }
        ],
    )
    # Empty words = scanned-PDF path (coarse OCR row-estimate). Must not raise a ValidationError.
    report = map_lab_report(ocr, [])
    assert [r.test_name for r in report.results] == ["Glucose", "Sodium"]
