"""Spike: verify Mistral OCR schema-mode capabilities against a real fixture (JOS-47 GO/NO-GO).

NOT production code. A throwaway probe (JOS-47) run BEFORE building the extraction workflow.
OCR 4 (`mistral-ocr-4-0`, what `mistral-ocr-latest` resolves to as of 2026-07-14) gives
paragraph/block-level bboxes with 13-way classification (`include_blocks`) and opt-in per-word
confidence (`confidence_scores_granularity`) — verified against the live Mistral docs. The open
question this probes: do the block boxes reach lab-ROW granularity, so each `LabResult` maps to
one box, or only whole-table? Run against a real PDF and read the dumped JSON before trusting the
schema's per-field bbox-required rule.

Run:
    COPILOT_MISTRAL_API_KEY=... .venv/bin/python scripts/ocr_spike.py [pdf_path]

Try it on BOTH the clean (digital) lab PDF and a scanned/degraded one — a digital text PDF may
have zero image regions, which is exactly the "no per-field boxes" failure mode to look for.
"""

import base64
import json
import os
import sys
from pathlib import Path

from pydantic import BaseModel, Field

try:
    # mistralai 2.x is a namespace package: Mistral lives in the `client` subpackage.
    from mistralai.client import Mistral
    from mistralai.extra import response_format_from_pydantic_model
except ImportError as exc:  # API surface may have moved — that itself is a finding.
    sys.exit(f"mistralai import failed ({exc}). Confirm the package + OCR helper path.")

DEFAULT_PDF = (
    Path(__file__).resolve().parents[1]
    / "tests/fixtures/documents/pdfs/sergio-angulo-lab-report.pdf"
)


# Deliberately FLAT probe schemas — not the real LabReport (no bbox/citation) — so we isolate
# "can Mistral extract the fields?" from "what geometry does it return, and at what granularity?".
class LabResultProbe(BaseModel):
    test_name: str = Field(description="Analyte/test name as printed")
    value: str = Field(description="Result value verbatim")
    unit: str | None = Field(default=None, description="Unit if printed")
    reference_range: str | None = Field(default=None, description="Reference range if printed")
    collection_date: str | None = Field(default=None, description="Collection date if printed")
    abnormal_flag: str | None = Field(default=None, description="Abnormal flag if shown")


class LabReportProbe(BaseModel):
    results: list[LabResultProbe] = Field(description="Every lab result on the report")


class MedicationProbe(BaseModel):
    name: str = Field(description="Medication name as printed")
    dose: str | None = Field(default=None, description="Dose/strength if printed")
    frequency: str | None = Field(default=None, description="Frequency if printed")


class AllergyProbe(BaseModel):
    substance: str = Field(description="Allergen as printed")
    reaction: str | None = Field(default=None, description="Reaction if printed")


class FamilyHistoryProbe(BaseModel):
    condition: str = Field(description="Condition as printed")
    relation: str | None = Field(default=None, description="Affected relative if printed")


class IntakeFormProbe(BaseModel):
    full_name: str | None = Field(default=None, description="Patient full name")
    date_of_birth: str | None = Field(default=None, description="Date of birth as printed")
    sex: str | None = Field(default=None, description="Sex/gender as printed")
    address: str | None = Field(default=None, description="Mailing address")
    phone: str | None = Field(default=None, description="Contact phone")
    chief_concern: str | None = Field(default=None, description="Chief concern / reason for visit")
    current_medications: list[MedicationProbe] = Field(default_factory=list)
    allergies: list[AllergyProbe] = Field(default_factory=list)
    family_history: list[FamilyHistoryProbe] = Field(default_factory=list)


def _select_probe(pdf_path: Path) -> tuple[str, type[BaseModel]]:
    """Pick the extraction schema by filename: intake forms vs lab reports."""
    doc_type = "intake_form" if "intake" in pdf_path.name.lower() else "lab_pdf"
    return doc_type, IntakeFormProbe if doc_type == "intake_form" else LabReportProbe


def main() -> None:
    api_key = os.environ.get("COPILOT_MISTRAL_API_KEY") or os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        sys.exit("Set COPILOT_MISTRAL_API_KEY (or MISTRAL_API_KEY) in the environment.")

    pdf_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PDF
    if not pdf_path.exists():
        sys.exit(f"PDF not found: {pdf_path}")

    b64 = base64.b64encode(pdf_path.read_bytes()).decode()
    doc_type, probe = _select_probe(pdf_path)
    print(f"Doc type: {doc_type}  (probe schema: {probe.__name__})")
    client = Mistral(api_key=api_key)

    resp = client.ocr.process(
        model="mistral-ocr-latest",  # alias -> mistral-ocr-4-0 (verified 2026-07-14)
        document={"type": "document_url", "document_url": f"data:application/pdf;base64,{b64}"},
        document_annotation_format=response_format_from_pydantic_model(probe),
        include_blocks=True,  # OCR 4+: paragraph/block bboxes + 13-way classification
        confidence_scores_granularity="word",  # opt-in per-word confidence (off by default)
        table_format="html",  # lab data is tabular
    )
    data = resp.model_dump()

    # Write extraction JSON to the sibling extractions/ dir (repo layout), else next to the PDF.
    ext_dir = pdf_path.parent.parent / "extractions"
    out_dir = ext_dir if ext_dir.is_dir() else pdf_path.parent
    out = out_dir / f"{pdf_path.stem}.ocr.json"
    out.write_text(json.dumps(data, indent=2, default=str))
    print(f"Pages: {len(data.get('pages', []))}")
    print(f"Full response written -> {out}\n")

    print("== 1. FIELD EXTRACTION (document_annotation) — does it hold the lab fields? ==")
    print(data.get("document_annotation") or "(none)")

    print("\n== 2. GEOMETRY — per-page BLOCKS (bbox + classification). Per-ROW or whole-table? ==")
    for i, page in enumerate(data.get("pages", [])):
        blocks = page.get("blocks") or []
        print(f" page {i}: {len(blocks)} block(s)")
        for bl in blocks:
            box = (
                bl.get("top_left_x"),
                bl.get("top_left_y"),
                bl.get("bottom_right_x"),
                bl.get("bottom_right_y"),
            )
            content = (bl.get("content") or "")[:60].replace("\n", " ")
            print(f"   type={bl.get('type')!r:12} bbox={box}  content={content!r}")

    print("\n== 3. CONFIDENCE — per-word/page scores (opt-in via confidence_scores_granularity) ==")
    for i, page in enumerate(data.get("pages", [])):
        scores = page.get("confidence_scores")
        print(f" page {i}: confidence_scores present={scores is not None}")
        if scores:
            print(f"   {json.dumps(scores)[:200]}")

    print(
        "\nGO/NO-GO: (1) fields extracted? (2) do BLOCK boxes reach lab-ROW granularity (one box "
        "per LabResult) or only whole-table? (3) confidence usable per field? If (2) is "
        "whole-table only, the per-field overlay needs a row-mapping step or a rethink."
    )


if __name__ == "__main__":
    main()
