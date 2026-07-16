import asyncio
import base64
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Protocol

from pydantic import BaseModel, Field

from copilot.config import ExtractorMode, Settings
from copilot.fhir.client import FhirClient, FhirError
from copilot.ingestion.errors import ExtractionError
from copilot.ingestion.geometry.document import DocumentGeometry
from copilot.ingestion.geometry.fields import FieldId, spec_for
from copilot.ingestion.geometry.locators import LocateRequest, LocatorState
from copilot.ingestion.geometry.words import (
    Checkbox,
    Word,
    extract_checkboxes,
    extract_word_boxes,
)
from copilot.ingestion.schemas import (
    AbnormalFlag,
    Allergy,
    BoundingBox,
    Citation,
    CitedText,
    Demographics,
    DocType,
    FamilyHistoryItem,
    IntakeForm,
    LabReport,
    LabResult,
    Medication,
)

logger = logging.getLogger("copilot")

# Re-exported: ExtractionError moved to `errors` so the geometry layer can raise it without
# importing this module, but it is part of this module's established public surface.
__all__ = [
    "DocumentExtractor",
    "ExtractedDocument",
    "ExtractionError",
    "FhirBinaryByteSource",
    "FixtureOcrBackend",
    "FixturePdfByteSource",
    "MistralOcrBackend",
    "build_extractor",
    "map_intake_form",
    "map_lab_report",
]


# --- Mistral OCR schema-mode probes (JOS-47 spike, productionized) -----------------------------
# Deliberately FLAT: the values Mistral extracts into `document_annotation`. Geometry does NOT come
# from here (Mistral returns whole-table blocks, not per-field boxes) — each value is placed by the
# locator chain bound to its field (`geometry.fields`). Kept minimal so the schema-mode request
# stays cheap and robust.
#
# These probes are NOT the ingestion schemas. The probe is what we ASK the model for; the schema in
# `ingestion.schemas` is what we ACCEPT, and it is the contract (W2_ARCHITECTURE §3.1 step 3) — raw
# probe output is mapped and validated into it, never returned directly.


class _LabResultProbe(BaseModel):
    test_name: str = Field(description="Analyte/test name as printed")
    value: str = Field(description="Result value verbatim")
    unit: str | None = Field(default=None, description="Unit if printed")
    reference_range: str | None = Field(default=None, description="Reference range if printed")
    collection_date: str | None = Field(default=None, description="Collection date if printed")
    abnormal_flag: str | None = Field(default=None, description="Abnormal flag if shown")


class _LabReportProbe(BaseModel):
    results: list[_LabResultProbe] = Field(description="Every lab result on the report")


# VERBATIM is not a stylistic preference here — it is what makes a fact citable. Every extracted
# value must be located on the page to earn a bounding box, so a value the model has tidied up
# (reformatting "03 / 14 / 1979" to "1979-03-14") cannot be found, and the fact is dropped rather
# than shown with a box that points at something else. Say "exactly as printed" on every field.
_VERBATIM = "EXACTLY as printed on the form, character for character. Do NOT reformat or normalize."


class _IntakeMedicationProbe(BaseModel):
    name: str = Field(description=f"Medication name {_VERBATIM}")
    dose: str | None = Field(description=f"Dose/strength {_VERBATIM} Null if not given.")
    frequency: str | None = Field(description=f"How often it is taken, {_VERBATIM} Null if absent.")


class _IntakeAllergyProbe(BaseModel):
    substance: str = Field(description=f"Allergen/substance {_VERBATIM}")
    reaction: str | None = Field(description=f"Reaction {_VERBATIM} Null if not given.")


class _IntakeFamilyHistoryProbe(BaseModel):
    condition: str = Field(description=f"Condition {_VERBATIM}")
    relation: str | None = Field(description=f"Affected relative, {_VERBATIM} Null if not given.")


class _IntakeFormProbe(BaseModel):
    """The intake values to read off the form.

    Two rules make this schema work, both learned the hard way:

    **Every field is REQUIRED (nullable, but never defaulted.)** The SDK's schema generator leaves a
    defaulted field out of the JSON schema's ``required`` list, and Mistral then simply omits it
    from ``document_annotation`` — silently, for six of nine fields. A field that may be absent is
    typed ``str | None`` with NO default, so it stays required and comes back as null.

    **Only what the form ASSERTS.** A patient-intake form preprints every option it offers — both
    "Male" and "Female", a whole checklist of conditions — and the tick is what makes one of them
    this patient's answer. The model is told to report only what is marked; the checkbox locator
    then independently verifies the mark and refuses the fact if it is absent.
    """

    full_name: str | None = Field(description=f"Patient full name {_VERBATIM}")
    date_of_birth: str | None = Field(
        description=f"Date of birth {_VERBATIM} If the form prints '03 / 14 / 1979', return that, "
        "NOT an ISO date."
    )
    sex: str | None = Field(
        description="The sex option that is TICKED/marked, exactly as printed beside the mark. "
        "Null if none is marked."
    )
    address: str | None = Field(description=f"Mailing address {_VERBATIM}")
    phone: str | None = Field(description=f"Contact phone {_VERBATIM}")
    chief_concern: str | None = Field(description=f"Reason for the visit {_VERBATIM}")
    current_medications: list[_IntakeMedicationProbe] = Field(
        description="Every current medication listed. Empty if none."
    )
    allergies: list[_IntakeAllergyProbe] = Field(
        description="Every allergy listed. Empty if none."
    )
    family_history: list[_IntakeFamilyHistoryProbe] = Field(
        description="ONLY family-history conditions that are TICKED/marked. Never list an "
        "unmarked condition, even though it is printed on the form. Empty if none are marked.",
    )


def _probe_for(doc_type: DocType) -> type[BaseModel]:
    """The schema-mode probe to request for a document type.

    Args:
        doc_type: Which document schema is being extracted.

    Returns:
        The flat probe model describing what to read off this kind of document.
    """
    match doc_type:
        case DocType.LAB_PDF:
            return _LabReportProbe
        case DocType.INTAKE_FORM:
            return _IntakeFormProbe


# --- OCR backends ------------------------------------------------------------------------------


class OcrBackend(Protocol):
    """A source of a raw OCR response for a document's bytes.

    Two implementations share this protocol: :class:`MistralOcrBackend` calls the live API and
    :class:`FixtureOcrBackend` replays a recorded response, so extraction tests run offline. Both
    return the raw ``resp.model_dump()`` dict that :func:`map_lab_report` maps into a
    :class:`~copilot.ingestion.schemas.LabReport`.
    """

    async def process(self, pdf_bytes: bytes, doc_type: DocType) -> dict[str, Any]:
        """Run OCR over ``pdf_bytes`` and return the raw response dict.

        Raises:
            ExtractionError: If the OCR call fails or the document type is unsupported.
        """
        ...


class MistralOcrBackend:
    """Live Mistral OCR (``mistral-ocr-latest``) in schema mode (JOS-54, W2_ARCHITECTURE §3.1).

    Productionizes the JOS-47 spike call: schema-mode field extraction plus paragraph/block boxes
    (``include_blocks``) and per-word confidence. The synchronous SDK call is run in a worker thread
    so it never blocks the event loop serving other turns.
    """

    def __init__(self, api_key: str) -> None:
        self._api_key = api_key

    async def process(self, pdf_bytes: bytes, doc_type: DocType) -> dict[str, Any]:
        """OCR a document via Mistral schema mode.

        Args:
            pdf_bytes: The raw document bytes.
            doc_type: Which document schema to extract; selects the probe requested.

        Returns:
            The raw ``resp.model_dump()`` dict (``document_annotation`` + ``pages[].blocks``).

        Raises:
            ExtractionError: If the SDK import fails or the OCR call fails.
        """
        try:
            # mistralai 2.x is a namespace package: Mistral lives in the `client` subpackage.
            from mistralai.client import Mistral
            from mistralai.extra import response_format_from_pydantic_model
        except ImportError as exc:
            raise ExtractionError(
                "mistralai is not installed (install the [extraction] extra)"
            ) from exc

        b64 = base64.b64encode(pdf_bytes).decode()
        client = Mistral(api_key=self._api_key)
        try:
            # The SDK call is synchronous/blocking — run it off the event loop.
            resp = await asyncio.to_thread(
                client.ocr.process,
                model="mistral-ocr-latest",  # alias -> mistral-ocr-4-0
                document={
                    "type": "document_url",
                    "document_url": f"data:application/pdf;base64,{b64}",
                },
                document_annotation_format=response_format_from_pydantic_model(
                    _probe_for(doc_type)
                ),
                include_blocks=True,  # OCR 4+: block bboxes (whole-table for tabular data)
                confidence_scores_granularity="word",  # opt-in per-word confidence
                # Both document types carry tables (a lab's results, an intake form's medication
                # and allergy grids), and the block geometry backs the row-band fallback.
                table_format="html",
            )
        except Exception as exc:
            # The SDK raises varied transport/validation errors; treat all as an OCR failure.
            raise ExtractionError("Mistral OCR request failed") from exc
        data: dict[str, Any] = resp.model_dump()
        return data


class FixtureOcrBackend:
    """Replays a recorded Mistral OCR response (``*.ocr.json``) per document type, no API call.

    The extraction counterpart to ``FixtureFhirClient`` / ``FixtureEvidenceRetriever``: it lets the
    graph run the real mapping pipeline over a deterministic response in tests and offline dev.

    Keyed BY DOCUMENT TYPE, and unconfigured types are a loud error rather than a silent fallback.
    A backend that ignored ``doc_type`` and replayed its one recording for every call would hand a
    lab report's response to the intake mapper, which finds no intake fields in it and yields no
    facts — a turn that looks like "this document has nothing in it" rather than a misconfiguration.
    """

    def __init__(self, fixture_paths: Mapping[DocType, str]) -> None:
        self._fixture_paths = {doc_type: Path(path) for doc_type, path in fixture_paths.items()}

    async def process(self, pdf_bytes: bytes, doc_type: DocType) -> dict[str, Any]:
        """Return the recorded OCR response for ``doc_type``, ignoring the input bytes.

        Raises:
            ExtractionError: If no recording is configured for the document type, or the fixture
                file is missing or not valid JSON.
        """
        path = self._fixture_paths.get(doc_type)
        if path is None:
            raise ExtractionError(f"no OCR fixture recorded for {doc_type.value}")
        try:
            data: dict[str, Any] = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ExtractionError(f"could not read OCR fixture {path}") from exc
        return data


# --- Document byte-source ----------------------------------------------------------------------


class DocumentByteSource(Protocol):
    """Where the extractor gets a document's bytes, given its id.

    Production fetches the bytes from OpenEMR by document id over FHIR ``Binary`` (see
    :class:`FhirBinaryByteSource`); tests can serve a committed fixture PDF. Keyed on the same
    document UUID the citation + click-to-source viewer use.
    """

    async def fetch(self, document_id: str) -> bytes:
        """Return the raw bytes for ``document_id``.

        Raises:
            ExtractionError: If the bytes cannot be read.
        """
        ...


class FhirBinaryByteSource:
    """Fetches a document's bytes from OpenEMR via the per-request FHIR client (``GET /Binary``).

    The production byte-source: it rides the same patient-scoped SMART token as every other FHIR
    read, so the bytes are authorized by the open patient's own access rights (the
    ``patient/Binary`` scope). Works with either FHIR client — the fixture client returns a
    committed PDF, so the same path exercises the real OCR pipeline offline.
    """

    def __init__(self, fhir: "FhirClient") -> None:
        self._fhir = fhir

    async def fetch(self, document_id: str) -> bytes:
        """Fetch the document's bytes by id, mapping FHIR failures to ``ExtractionError``.

        Raises:
            ExtractionError: If the Binary read fails or returns no content.
        """
        try:
            return await self._fhir.get_document_bytes(document_id)
        except FhirError as exc:
            raise ExtractionError(f"could not fetch document bytes for {document_id}") from exc


class FixturePdfByteSource:
    """Serves the bytes of a single committed lab PDF regardless of id (test byte-source)."""

    def __init__(self, pdf_path: str) -> None:
        self._pdf_path = Path(pdf_path)

    async def fetch(self, document_id: str) -> bytes:
        """Return the fixture PDF bytes.

        Raises:
            ExtractionError: If the fixture PDF is missing.
        """
        try:
            return self._pdf_path.read_bytes()
        except OSError as exc:
            raise ExtractionError(f"could not read document fixture {self._pdf_path}") from exc


# --- Extraction result + facade ----------------------------------------------------------------


@dataclass(frozen=True)
class ExtractedDocument:
    """One document's strict extraction: its cited facts, boxed for click-to-source.

    ``report`` is the schema the document's TYPE selected — a ``LabReport`` for a lab_pdf, an
    ``IntakeForm`` for an intake_form. Which one it is was decided by the document's OpenEMR
    category, never by the model.

    Every fact's ``bounding_box`` is already in **PDF points** (top-left origin) — the exact space
    the overlay renders in — so nothing downstream converts coordinates (the JOS-57 seam).
    """

    document_id: str
    doc_type: DocType
    report: LabReport | IntakeForm


def _map_report(
    doc_type: DocType, raw: dict[str, Any], pdf_bytes: bytes
) -> LabReport | IntakeForm:
    """Map a raw OCR response into the strict schema the document's type names.

    The single place a document type selects its schema. Exhaustive over ``DocType`` with no default
    branch, so adding a third document type is a type error here rather than a silent fallthrough.

    Args:
        doc_type: The schema to map into, resolved from the document's category.
        raw: The raw OCR response.
        pdf_bytes: The document's bytes, for text-layer geometry.

    Returns:
        The validated report.

    Raises:
        ExtractionError: If the response cannot be mapped.
    """
    # The PDF text layer gives exact word boxes (points); empty for a scanned/image-only PDF, in
    # which case the mapper falls back to its coarse locators.
    words = extract_word_boxes(pdf_bytes)
    match doc_type:
        case DocType.LAB_PDF:
            return map_lab_report(raw, words)
        case DocType.INTAKE_FORM:
            # Checkboxes are read only for a form: a tick is what makes an option a fact, and a
            # lab report has none.
            checkboxes = extract_checkboxes(pdf_bytes)
            return map_intake_form(raw, words, checkboxes)


class DocumentExtractor:
    """OCRs a document's bytes and maps the result into the strict schema its type names.

    The byte-source is supplied per call (not held) so the bytes ride the request's own
    patient-scoped FHIR client; the OCR backend is app-lifetime and stateless.
    """

    def __init__(self, ocr: OcrBackend) -> None:
        self._ocr = ocr

    async def extract(
        self, document_id: str, doc_type: DocType, byte_source: DocumentByteSource
    ) -> ExtractedDocument:
        """Extract one document end-to-end: bytes -> OCR -> the strict schema its type names.

        Args:
            document_id: The source document's FHIR ``DocumentReference`` id (used for citations
                and to fetch the bytes).
            doc_type: The document schema to extract, resolved from its OpenEMR category.
            byte_source: Where to fetch this document's bytes (the per-request FHIR client in prod).

        Returns:
            The parsed :class:`ExtractedDocument`.

        Raises:
            ExtractionError: If the byte fetch, OCR, or mapping fails.
        """
        pdf_bytes = await byte_source.fetch(document_id)
        raw = await self._ocr.process(pdf_bytes, doc_type)
        report = _map_report(doc_type, raw, pdf_bytes)
        return ExtractedDocument(document_id=document_id, doc_type=doc_type, report=report)


def _ocr_fixture_paths(settings: Settings) -> dict[DocType, str]:
    """The recorded OCR response configured for each document type, omitting the unconfigured.

    The DocType -> settings-field mapping lives here rather than on ``Settings`` so ``config`` stays
    a leaf that knows nothing about the ingestion schemas.

    Args:
        settings: Service settings.

    Returns:
        The configured fixture paths by document type; empty when none are set.
    """
    by_type = {
        DocType.LAB_PDF: settings.ocr_fixture_path_lab_pdf,
        DocType.INTAKE_FORM: settings.ocr_fixture_path_intake_form,
    }
    return {doc_type: path for doc_type, path in by_type.items() if path}


def build_extractor(settings: Settings) -> DocumentExtractor | None:
    """Construct the document extractor from settings, or None when extraction is unconfigured.

    Returns None (extraction disabled, the intake-extractor simply reports no document) when the
    selected OCR backend lacks its credential/fixture — so a missing key degrades to "no document
    facts", never a crash. The byte-source is supplied per request (the patient-scoped FHIR client),
    so it is not part of this app-lifetime wiring.

    Args:
        settings: Service settings selecting the extractor mode.

    Returns:
        A wired :class:`DocumentExtractor`, or None when extraction cannot be configured.
    """
    if settings.extractor_mode is ExtractorMode.FIXTURE:
        paths = _ocr_fixture_paths(settings)
        if not paths:
            logger.warning("extractor FIXTURE mode without an OCR fixture; extraction disabled")
            return None
        # A partial map is fine and stays enabled: a deployment that only replays labs extracts
        # labs, and an intake document then fails per-call rather than at startup.
        ocr: OcrBackend = FixtureOcrBackend(paths)
    else:
        if settings.mistral_api_key is None:
            logger.warning("extractor MISTRAL mode without an API key; extraction disabled")
            return None
        ocr = MistralOcrBackend(settings.mistral_api_key)
    return DocumentExtractor(ocr)


# --- OCR values + document geometry -> strict LabReport ----------------------------------------
#
# Mistral OCR gives the field VALUES (`document_annotation`, schema mode) but only whole-table
# geometry. Where each value SITS is resolved by the locator chain bound to the field (see
# `geometry.fields`): the PDF text layer pins it exactly where there is one, and the coarse OCR
# row band or the page stands in where there is not. Every box the chain emits is already in PDF
# points — the space the overlay renders in — because `DocumentGeometry` normalizes once.


def _annotation_dict(ocr: dict[str, Any]) -> dict[str, Any]:
    """Parse the OCR response's ``document_annotation`` into a dict.

    Args:
        ocr: The raw OCR response dict.

    Returns:
        The parsed annotation, or ``{}`` when absent or not an object.

    Raises:
        ExtractionError: If the annotation is a string that is not valid JSON.
    """
    annotation = ocr.get("document_annotation")
    if isinstance(annotation, str):
        try:
            annotation = json.loads(annotation)
        except ValueError as exc:  # JSONDecodeError subclasses ValueError
            raise ExtractionError("OCR document_annotation is not valid JSON") from exc
    return annotation if isinstance(annotation, dict) else {}


def map_lab_report(ocr: dict[str, Any], words: list[Word]) -> LabReport:
    """Map a Mistral OCR response + PDF word boxes into a strict ``LabReport`` (boxes in points).

    Args:
        ocr: The raw OCR response dict (``document_annotation`` + ``pages[].blocks``/``tables``).
        words: The PDF text-layer words (from :func:`extract_word_boxes`); empty for a scanned PDF,
            in which case each value falls back to the coarse OCR row band.

    Returns:
        The strict :class:`LabReport`; every ``LabResult``'s ``bounding_box`` is in PDF points.

    Raises:
        ExtractionError: If the response has no usable page, or ``document_annotation`` is present
            but not valid JSON. Any mapping failure surfaces as this one type so the caller can
            degrade to "no facts" rather than crashing the turn.
    """
    annotation = _annotation_dict(ocr)
    raw_annotation_results = annotation.get("results")
    raw_results = raw_annotation_results if isinstance(raw_annotation_results, list) else []

    geometry = DocumentGeometry.from_parts(ocr, words)
    # The raw page is still needed for per-word confidence, which is OCR metadata, not geometry.
    page: dict[str, Any] = (ocr.get("pages") or [{}])[0]
    if not words and raw_results:
        logger.warning(
            "PDF has no text layer; using the coarse OCR row band for box geometry",
            extra={"result_count": len(raw_results)},
        )

    spec = spec_for(DocType.LAB_PDF, FieldId.LAB_RESULT_VALUE)
    state = LocatorState()
    results: list[LabResult] = []
    for ordinal, raw in enumerate(raw_results):
        if not isinstance(raw, dict):
            continue  # skip a malformed (non-object) result entry rather than crashing the turn
        test_name = str(raw.get("test_name", ""))
        value = str(raw.get("value", "")).strip()
        if not test_name.strip() or not value:
            # A spacer/subtotal/pending row (or a degraded scan) yields a blank name or value,
            # which is not a citable analyte+value fact. Skip it — emitting it would violate the
            # schema's min_length guard and raise a ValidationError that escapes ExtractionError.
            logger.warning(
                "dropping lab row with a blank test name or value",
                extra={"test_name": raw.get("test_name"), "value": raw.get("value")},
            )
            continue
        located = spec.chain.locate(
            LocateRequest(
                value=value,
                anchors=(test_name,),
                ordinal=ordinal,
                total=len(raw_results),
            ),
            geometry,
            state,
        )
        if located is None or not located.precision.meets(spec.floor):
            # No text-layer word, no table geometry, no page box — drop rather than fabricate.
            logger.warning("dropping lab result with no locatable box", extra={"test": test_name})
            continue
        results.append(_build_lab_result(raw, located.box, page))
    return LabReport(results=results)


def map_intake_form(
    ocr: dict[str, Any], words: list[Word], checkboxes: list[Checkbox]
) -> IntakeForm:
    """Map a Mistral OCR response + text-layer evidence into a strict ``IntakeForm``.

    Mirrors :func:`map_lab_report`: schema mode gives the VALUES, the locator chain bound to each
    field decides where they sit. A value that cannot be placed to the intake precision floor — or
    that the page refutes, as an unticked option does — is dropped rather than cited with a box
    that points at something which does not support it.

    Unlike ``LabResult``, ``IntakeForm``'s sub-models do not require a bounding box, so the floor
    is enforced here: this function is the only owner of "every intake fact carries a usable box".

    Args:
        ocr: The raw OCR response dict.
        words: The PDF text-layer words in points.
        checkboxes: The form's tick boxes, which alone assert a checkbox-backed answer.

    Returns:
        The strict :class:`IntakeForm`; every emitted citation's box is in PDF points.

    Raises:
        ExtractionError: If the response has no usable page, or ``document_annotation`` is present
            but not valid JSON.
    """
    annotation = _annotation_dict(ocr)
    geometry = DocumentGeometry.from_parts(ocr, words, checkboxes=checkboxes)
    state = LocatorState()

    def cited(field: FieldId, value: Any) -> CitedText | None:
        text = _clean(value)
        if text is None:
            return None
        citation = _locate(field, text, geometry, state)
        return CitedText(value=text, citation=citation) if citation is not None else None

    demographics = Demographics(
        full_name=cited(FieldId.DEMOGRAPHICS_FULL_NAME, annotation.get("full_name")),
        date_of_birth=cited(FieldId.DEMOGRAPHICS_DATE_OF_BIRTH, annotation.get("date_of_birth")),
        sex=cited(FieldId.DEMOGRAPHICS_SEX, annotation.get("sex")),
        address=cited(FieldId.DEMOGRAPHICS_ADDRESS, annotation.get("address")),
        phone=cited(FieldId.DEMOGRAPHICS_PHONE, annotation.get("phone")),
    )
    return IntakeForm(
        demographics=demographics,
        chief_concern=cited(FieldId.CHIEF_CONCERN, annotation.get("chief_concern")),
        current_medications=_map_medications(annotation, geometry, state),
        allergies=_map_allergies(annotation, geometry, state),
        family_history=_map_family_history(annotation, geometry, state),
    )


def _locate(
    field: FieldId, value: str, geometry: DocumentGeometry, state: LocatorState
) -> Citation | None:
    """Place one intake value on the page and build its citation, or None when it cannot be proven.

    The one place a field selects its locator chain. Returns None — so the caller drops the field —
    when no locator applies, when the page refutes the value, or when the best box misses the
    document type's precision floor.

    Args:
        field: The semantic field being placed.
        value: The verbatim value to box.
        geometry: The document's normalized geometry.
        state: Per-document cursors.

    Returns:
        The fact's :class:`Citation`, or None when it should not be emitted.
    """
    spec = spec_for(DocType.INTAKE_FORM, field)
    located = spec.chain.locate(
        LocateRequest(value=value, anchors=tuple(sorted(spec.labels))), geometry, state
    )
    if located is None:
        logger.warning(
            "dropping intake fact the page does not support",
            extra={"field": field.value},
        )
        return None
    if not located.precision.meets(spec.floor):
        logger.warning(
            "dropping intake fact below the precision floor",
            extra={"field": field.value, "precision": located.precision.value},
        )
        return None
    return Citation(quote_or_value=value, bounding_box=located.box)


def _map_medications(
    annotation: dict[str, Any], geometry: DocumentGeometry, state: LocatorState
) -> list[Medication]:
    """Map the medication rows, dropping any whose name cannot be located on the form."""
    items: list[Medication] = []
    for raw in annotation.get("current_medications") or []:
        if not isinstance(raw, dict):
            continue
        name = _clean(raw.get("name"))
        if name is None:
            continue
        citation = _locate(FieldId.CURRENT_MEDICATIONS, name, geometry, state)
        if citation is None:
            continue
        items.append(
            Medication(
                name=name,
                dose=_clean(raw.get("dose")),
                frequency=_clean(raw.get("frequency")),
                citation=citation,
            )
        )
    return items


def _map_allergies(
    annotation: dict[str, Any], geometry: DocumentGeometry, state: LocatorState
) -> list[Allergy]:
    """Map the allergy rows, dropping any whose substance cannot be located on the form."""
    items: list[Allergy] = []
    for raw in annotation.get("allergies") or []:
        if not isinstance(raw, dict):
            continue
        substance = _clean(raw.get("substance"))
        if substance is None:
            continue
        citation = _locate(FieldId.ALLERGIES, substance, geometry, state)
        if citation is None:
            continue
        items.append(
            Allergy(substance=substance, reaction=_clean(raw.get("reaction")), citation=citation)
        )
    return items


def _map_family_history(
    annotation: dict[str, Any], geometry: DocumentGeometry, state: LocatorState
) -> list[FamilyHistoryItem]:
    """Map the family-history rows.

    The checkbox chain does the real work: a condition the form merely prints, unticked, is refuted
    and never becomes a fact — so a model that over-reads the checklist is corrected by the page.
    """
    items: list[FamilyHistoryItem] = []
    for raw in annotation.get("family_history") or []:
        if not isinstance(raw, dict):
            continue
        condition = _clean(raw.get("condition"))
        if condition is None:
            continue
        citation = _locate(FieldId.FAMILY_HISTORY, condition, geometry, state)
        if citation is None:
            continue
        items.append(
            FamilyHistoryItem(
                condition=condition, relation=_clean(raw.get("relation")), citation=citation
            )
        )
    return items


def _build_lab_result(raw: dict[str, Any], box: BoundingBox, page: dict[str, Any]) -> LabResult:
    """Build one cited ``LabResult`` from a document_annotation row and its located box."""
    value = str(raw.get("value", "")).strip()
    return LabResult(
        test_name=str(raw.get("test_name", "")).strip(),
        value=value,
        unit=_clean(raw.get("unit")),
        reference_range=_clean(raw.get("reference_range")),
        collection_date=_parse_date(raw.get("collection_date")),
        abnormal_flag=_map_abnormal(raw.get("abnormal_flag")),
        citation=Citation(quote_or_value=value, bounding_box=box),
        confidence=_value_confidence(value, page),
    )


def _value_confidence(value: str, page: dict[str, Any]) -> float | None:
    """Per-value confidence: average the OCR word confidences whose text matches the value.

    Falls back to the page's average confidence when no word matches (e.g. multi-token values), and
    to None when the response carries no confidence scores at all.
    """
    scores = page.get("confidence_scores")
    if not isinstance(scores, dict):
        return None
    target = _norm(value)
    words = scores.get("word_confidence_scores")
    if isinstance(words, list) and target:
        matched = [
            float(w["confidence"])
            for w in words
            if isinstance(w, dict)
            and _norm(str(w.get("text", ""))) == target
            and isinstance(w.get("confidence"), (int, float))
        ]
        if matched:
            return sum(matched) / len(matched)
    avg = scores.get("average_page_confidence_score")
    return float(avg) if isinstance(avg, (int, float)) else None


def _map_abnormal(flag: Any) -> AbnormalFlag:
    """Map a printed abnormal flag (``H``/``L``/``A``/blank) to the schema enum."""
    token = str(flag or "").strip().upper()
    if not token or token == "N":
        return AbnormalFlag.NO
    if token.startswith("H"):
        return AbnormalFlag.HIGH
    if token.startswith("L"):
        return AbnormalFlag.LOW
    return AbnormalFlag.YES


def _parse_date(raw: Any) -> date | None:
    """Parse an ISO collection date, or None when absent/unparseable (never infer)."""
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        return date.fromisoformat(raw.strip())
    except ValueError:
        return None


def _clean(raw: Any) -> str | None:
    """Return a stripped non-empty string, or None."""
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def _norm(text: str) -> str:
    """Lowercase and collapse whitespace, for matching an OCR word against a value.

    Deliberately NOT ``geometry.spans.norm``, despite the near-identical name: that one also strips
    surrounding punctuation so a label matches whether or not the form prints a trailing colon.
    Here the text being matched is a lab VALUE, where punctuation is significant — "(H)" is an
    abnormal flag, not "h" — so stripping it would match the wrong OCR word and report that word's
    confidence. Keep them separate.
    """
    return " ".join(str(text).split()).lower()
