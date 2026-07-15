import json
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelMessage, ModelResponse, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from copilot.fhir.fixtures import FixtureFhirClient
from copilot.graph.deps import GraphDeps
from copilot.graph.outputs import ExtractorOutput
from copilot.graph.workers import build_intake_extractor
from copilot.ingestion.extractor import (
    DocumentExtractor,
    ExtractedDocument,
    ExtractionError,
    FixtureOcrBackend,
    FixturePdfByteSource,
    map_lab_report,
)
from copilot.ingestion.registry import DOCUMENT_FACT_RESOURCE_TYPE, DocumentFactRegistry
from copilot.ingestion.schemas import AbnormalFlag, DocType, LabReport, LabResult
from copilot.retrieval import ChunkRegistry
from copilot.schemas import CitationSourceType, Claim, LabPdfCitation, SourceRef
from copilot.verification import FetchLog, ground_claims
from graph_script import StubRetriever

_FIXTURES = Path(__file__).parent / "fixtures" / "documents"
_LAB_OCR = _FIXTURES / "extractions" / "sergio-angulo-lab-report.ocr.json"
_LAB_PDF = _FIXTURES / "pdfs" / "sergio-angulo-lab-report.pdf"


def _find(report: LabReport, test_name: str) -> LabResult | None:
    """Return the extracted result with the given test name, or None."""
    return next((r for r in report.results if r.test_name == test_name), None)


def test_map_lab_report_extracts_values_flags_and_boxes() -> None:
    """Regression: every lab value is extracted verbatim, flagged correctly, and carries a box.

    If this breaks, the intake-extractor ships lab facts the click-to-source overlay can't place
    (or misreads high/low), which is the whole point of the document-extraction path.
    """
    import json

    report, page_dpi = map_lab_report(json.loads(_LAB_OCR.read_text()))

    assert page_dpi == {1: 93.0}  # the page render DPI drives the px->points conversion downstream
    assert len(report.results) >= 20  # the CMP + CBC panels, one result each

    creatinine = _find(report, "Creatinine")
    assert creatinine is not None
    assert creatinine.value == "1.44"  # verbatim, never rounded
    assert creatinine.abnormal_flag is AbnormalFlag.HIGH  # printed "H"
    # Every result must carry a locatable box — the schema requires it and the overlay needs it.
    for result in report.results:
        assert result.citation.bounding_box is not None
        assert result.citation.bounding_box.page == 1


def test_row_bands_step_down_the_page_in_table_order() -> None:
    """Catches a broken row estimator: later analytes must sit lower on the page than earlier ones.

    The overlay lands on the correct ROW only if the estimated y-bands preserve table order; a
    regression that collapses or reverses them would put every box on the wrong line.
    """
    import json

    report, _ = map_lab_report(json.loads(_LAB_OCR.read_text()))
    glucose = _find(report, "Glucose, Fasting")
    creatinine = _find(report, "Creatinine")
    assert glucose is not None and glucose.citation.bounding_box is not None
    assert creatinine is not None and creatinine.citation.bounding_box is not None
    # Creatinine is printed several rows below glucose, so its band must be lower on the page.
    assert creatinine.citation.bounding_box.y > glucose.citation.bounding_box.y


async def test_fixture_extractor_round_trip() -> None:
    """The DocumentExtractor wires byte-source + OCR backend + mapping into an ExtractedDocument."""
    extractor = DocumentExtractor(
        ocr=FixtureOcrBackend(str(_LAB_OCR)),
        byte_source=FixturePdfByteSource(str(_LAB_PDF)),
    )
    extracted = await extractor.extract("doc-abc", DocType.LAB_PDF)
    assert extracted.document_id == "doc-abc"
    assert extracted.doc_type is DocType.LAB_PDF
    assert _find(extracted.report, "Potassium") is not None


def test_registry_resolve_converts_pixels_to_points() -> None:
    """Registry stamps overlay geometry in PDF POINTS, converting the extractor's native pixels.

    The overlay scales as if the box is in 72-DPI points; if the registry forgot to convert, boxes
    would render ~1.3x too large and off the value (the JOS-57 coordinate-space contract).
    """
    import json

    report, page_dpi = map_lab_report(json.loads(_LAB_OCR.read_text()))
    registry = DocumentFactRegistry()
    extracted = ExtractedDocument(
        document_id="doc-xyz", doc_type=DocType.LAB_PDF, report=report, page_dpi=page_dpi
    )
    handles = registry.record(extracted)
    assert handles and all(h.resource_type == DOCUMENT_FACT_RESOURCE_TYPE for h in handles)

    creatinine = _find(report, "Creatinine")
    assert creatinine is not None and creatinine.citation.bounding_box is not None
    creatinine_native = creatinine.citation.bounding_box
    creatinine_handle = next(h for h in handles if h.test_name == "Creatinine")
    resolution = registry.resolve(
        SourceRef(resource_type=creatinine_handle.resource_type,
                  resource_id=creatinine_handle.resource_id, field="value")
    )
    assert resolution is not None
    assert resolution.value == "1.44"
    assert resolution.document_id == "doc-xyz"
    assert resolution.bounding_box is not None
    # points = pixels * 72 / dpi (dpi 93). Assert the x was scaled, not passed through as pixels.
    assert resolution.bounding_box.x == pytest.approx(creatinine_native.x * 72 / 93)


def test_registry_ignores_unrecorded_and_foreign_citations() -> None:
    """A claim citing a fact the turn did not extract must not ground (no fabricated provenance)."""
    registry = DocumentFactRegistry()  # empty — nothing recorded this turn
    assert registry.resolve(
        SourceRef(resource_type=DOCUMENT_FACT_RESOURCE_TYPE, resource_id="never#0", field="value")
    ) is None
    # A FHIR citation is not this registry's to resolve — it defers (returns None) to the FetchLog.
    fhir_ref = SourceRef(resource_type="Condition", resource_id="c1", field="x")
    assert registry.resolve(fhir_ref) is None


def test_grounded_document_claim_projects_to_lab_pdf_citation() -> None:
    """End-to-end stamp: a claim citing an extracted fact grounds and projects to a LabPdfCitation.

    This is the seam the sidebar consumes — if the box/document_id don't land on the SourceRef and
    survive to_citation(), click-to-source shows no overlay.
    """
    import json

    report, page_dpi = map_lab_report(json.loads(_LAB_OCR.read_text()))
    registry = DocumentFactRegistry()
    handles = registry.record(
        ExtractedDocument(
            document_id="doc-777", doc_type=DocType.LAB_PDF, report=report, page_dpi=page_dpi
        )
    )
    handle = next(h for h in handles if h.test_name == "Creatinine")
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
    extractor = DocumentExtractor(
        ocr=FixtureOcrBackend(str(_LAB_OCR)), byte_source=FixturePdfByteSource(str(_LAB_PDF))
    )
    deps = GraphDeps(
        fhir=FixtureFhirClient.from_seed(),
        patient_id="1",
        correlation_id="test-cid",
        retriever=StubRetriever(snippets=()),
        extractor=extractor,
        fetched=FetchLog(),
        chunks=ChunkRegistry(),
        documents=DocumentFactRegistry(),
    )

    state = {"extracted": False}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if not state["extracted"]:
            state["extracted"] = True
            # Extract the uploaded report first (document_id is what list_lab_documents would give).
            return ModelResponse(
                parts=[ToolCallPart(tool_name="attach_and_extract", args={"document_id": "doc-1"})]
            )
        # Cite the first extracted fact — Glucose is result ordinal 0, so its id is "doc-1#0".
        output = ExtractorOutput(
            summary="Fasting glucose is high.",
            claims=[
                Claim(
                    text="Fasting glucose was 108 mg/dL (high).",
                    source=SourceRef(
                        resource_type=DOCUMENT_FACT_RESOURCE_TYPE,
                        resource_id="doc-1#0",
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
    assert source.document_id == "doc-1"
    assert source.bounding_box is not None
    assert source.to_citation().source_type is CitationSourceType.LAB_PDF


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


def test_malformed_document_annotation_raises_extraction_error() -> None:
    """A truncated/invalid OCR JSON must raise ExtractionError, not a raw JSONDecodeError.

    attach_and_extract only catches ExtractionError; if the mapping leaked a JSONDecodeError the
    physician's whole /chat turn would 500 instead of degrading to 'no lab facts'.
    """
    bad = {"document_annotation": "{not valid json", "pages": [{"index": 0, "dimensions": {}}]}
    with pytest.raises(ExtractionError):
        map_lab_report(bad)


def test_malformed_table_block_does_not_crash() -> None:
    """A table block missing its coordinate keys must not raise (finding #1 KeyError path).

    Live OCR can return a table block without the flat top_left_x etc.; the mapper must skip it and
    fall back, never crash the turn.
    """
    ocr = _ocr_with(
        [{"test_name": "Glucose", "value": "108", "abnormal_flag": "H"}],
        [{"type": "table", "content": "<table><tr><td>Glucose</td><td>108</td></tr></table>"}],
    )
    report, _ = map_lab_report(ocr)  # must not raise despite the coordinate-less table block
    assert len(report.results) == 1


def test_non_dict_result_entry_is_skipped() -> None:
    """A non-object entry in results is skipped, not crashed on (finding #1 AttributeError path)."""
    ocr = _ocr_with(
        ["garbage", {"test_name": "Sodium", "value": "140", "abnormal_flag": "no"}],
        [{"type": "table", "content": "<table><tr><td>Sodium</td><td>140</td></tr></table>",
          "top_left_x": 10, "top_left_y": 10, "bottom_right_x": 100, "bottom_right_y": 40}],
    )
    report, _ = map_lab_report(ocr)
    assert [r.test_name for r in report.results] == ["Sodium"]


def test_no_table_block_falls_back_to_whole_page_box() -> None:
    """A non-tabular OCR layout must still surface its values, not silently drop them (finding #4).

    Without a fallback the Co-Pilot would tell the physician it found no labs even though the report
    plainly contains them; the fallback highlights the whole page instead of losing the fact.
    """
    ocr = _ocr_with(
        [
            {"test_name": "Glucose", "value": "108", "abnormal_flag": "H"},
            {"test_name": "Creatinine", "value": "1.44", "abnormal_flag": "H"},
        ],
        [{"type": "text", "content": "Glucose 108  Creatinine 1.44",
          "top_left_x": 5, "top_left_y": 5, "bottom_right_x": 50, "bottom_right_y": 20}],
    )
    report, _ = map_lab_report(ocr)
    assert len(report.results) == 2  # values survived despite no table block
    box = report.results[0].citation.bounding_box
    assert box is not None
    # Whole-page fallback: native-pixel box spanning the full page dimensions.
    assert (box.x, box.y, box.width, box.height) == (0, 0, 791, 1023)
