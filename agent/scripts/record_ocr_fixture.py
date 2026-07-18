import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from copilot.ingestion.errors import ExtractionError  # noqa: E402
from copilot.ingestion.extractor import (  # noqa: E402
    MistralOcrBackend,
    map_intake_form,
    map_lab_report,
    map_medication_list,
)
from copilot.ingestion.geometry.document import DocumentGeometry  # noqa: E402
from copilot.ingestion.schemas import (  # noqa: E402
    DocType,
    IntakeForm,
    LabReport,
    MedicationList,
)

_AGENT_ROOT = Path(__file__).resolve().parents[1]
_DOCUMENTS_DIR = _AGENT_ROOT / "tests" / "fixtures" / "documents"
_PDF_DIR = _DOCUMENTS_DIR / "pdfs"
_EXTRACTIONS_DIR = _DOCUMENTS_DIR / "extractions"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Record a Mistral OCR response as a committed test fixture, then replay the real "
            "mapper over it to prove the recording is usable. Calls the SAME production OCR "
            "backend the service uses, so a fixture can never drift from the request production "
            "actually makes. Exits non-zero if the recording does not map to any facts."
        )
    )
    parser.add_argument(
        "--pdf", required=True, type=Path, help="Source PDF to OCR."
    )
    parser.add_argument(
        "--doc-type",
        required=True,
        type=DocType,
        choices=list(DocType),
        help=(
            "Which schema to extract. REQUIRED and explicit: the document's type decides the "
            "schema, and inferring it from the filename is exactly the heuristic the ingestion "
            "contract forbids."
        ),
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write here instead of the fixtures dir. For one-off inspection only.",
    )
    parser.add_argument(
        "--allow-external",
        action="store_true",
        help="Permit a PDF outside the committed fixtures dir (see the PHI warning).",
    )
    return parser.parse_args()


def _check_source(pdf: Path, allow_external: bool) -> None:
    """Refuse to record a document that is not a committed synthetic fixture.

    The recorded response embeds the document's text — for an intake form that is a name, date of
    birth, address, and phone. Fixtures are committed to git, so recording a real document would
    publish PHI into the repository's history. Synthetic fixtures live in one directory; anything
    else has to be opted into explicitly.

    Args:
        pdf: The source PDF.
        allow_external: Whether the caller has explicitly accepted the risk.

    Raises:
        SystemExit: If the PDF is outside the fixtures dir and the override was not given.
    """
    try:
        pdf.resolve().relative_to(_PDF_DIR.resolve())
    except ValueError:
        if not allow_external:
            raise SystemExit(
                f"refusing to record {pdf}: it is outside {_PDF_DIR}.\n"
                "The recorded response embeds the document's text and is committed to git — for "
                "an intake form that means name, DOB, address, and phone. Pass --allow-external "
                "only for synthetic documents."
            ) from None
        print(f"WARNING: recording a PDF outside the fixtures dir: {pdf}", file=sys.stderr)
        print("WARNING: its text will be written into a file intended for git.", file=sys.stderr)


def _report_rows(report: LabReport | IntakeForm | MedicationList) -> list[tuple[str, str, str]]:
    """Flatten a mapped report into (field, value, box) rows for the replay table."""
    rows: list[tuple[str, str, str]] = []

    def _box(citation: Any) -> str:
        box = citation.bounding_box
        if box is None:
            return "-"
        return f"p{box.page} {box.x:.0f},{box.y:.0f} {box.width:.0f}x{box.height:.0f}"

    match report:
        case LabReport():
            for result in report.results:
                rows.append((result.test_name, result.value, _box(result.citation)))
        case IntakeForm():
            for name, cited in (
                ("demographics.full_name", report.demographics.full_name),
                ("demographics.date_of_birth", report.demographics.date_of_birth),
                ("demographics.sex", report.demographics.sex),
                ("demographics.address", report.demographics.address),
                ("demographics.phone", report.demographics.phone),
                ("chief_concern", report.chief_concern),
            ):
                if cited is not None:
                    rows.append((name, cited.value, _box(cited.citation)))
            for allergy in report.allergies:
                rows.append(("allergies[]", allergy.substance, _box(allergy.citation)))
            for item in report.family_history:
                rows.append(("family_history[]", item.condition, _box(item.citation)))
        case MedicationList():
            for medication in report.medications:
                rows.append(("current_medications[]", medication.name, _box(medication.citation)))
    return rows


def _replay(raw: dict[str, Any], pdf_bytes: bytes, doc_type: DocType) -> list[tuple[str, str, str]]:
    """Map the recorded response with the real mapper, so a useless recording never gets committed.

    Args:
        raw: The recorded OCR response.
        pdf_bytes: The source PDF's bytes, for text-layer geometry.
        doc_type: Which schema to map.

    Returns:
        The per-fact rows the mapper produced.

    Raises:
        SystemExit: If the response maps to no facts — the recording is not worth committing.
    """
    try:
        geometry = DocumentGeometry.from_document(pdf_bytes, raw)
        match doc_type:
            case DocType.LAB_PDF:
                report: LabReport | IntakeForm | MedicationList = map_lab_report(raw, geometry)
            case DocType.INTAKE_FORM:
                report = map_intake_form(raw, geometry)
            case DocType.MEDICATION_LIST:
                report = map_medication_list(raw, geometry)
    except ExtractionError as exc:
        raise SystemExit(f"the recording does not map: {exc}") from exc
    rows = _report_rows(report)
    if not rows:
        raise SystemExit(
            "the recording mapped to ZERO facts — not committing it.\n"
            "Either the probe did not match the document, or no value could be located on the "
            "page. Fix that before recording a golden; every downstream test trusts this file."
        )
    return rows


def main() -> None:
    args = _parse_args()
    if not args.pdf.is_file():
        raise SystemExit(f"no such PDF: {args.pdf}")
    _check_source(args.pdf, args.allow_external)

    api_key = os.environ.get("COPILOT_MISTRAL_API_KEY") or os.environ.get("MISTRAL_API_KEY")
    if not api_key:
        raise SystemExit("set COPILOT_MISTRAL_API_KEY (or MISTRAL_API_KEY) to record a fixture")

    pdf_bytes = args.pdf.read_bytes()
    print(f"OCR {args.pdf.name} as {args.doc_type.value} (live Mistral call)...")
    raw = asyncio.run(MistralOcrBackend(api_key).process(pdf_bytes, args.doc_type))

    rows = _replay(raw, pdf_bytes, args.doc_type)
    print(f"\nmapped {len(rows)} facts:")
    for field, value, box in rows:
        print(f"  {field:32} {value[:38]:40} {box}")

    out = args.out or (_EXTRACTIONS_DIR / f"{args.pdf.stem}.ocr.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    print(f"\nwrote {out}")


if __name__ == "__main__":
    main()
