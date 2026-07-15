import base64
from pathlib import Path

import httpx
import pytest

from copilot.fhir.client import FhirError, HttpFhirClient
from copilot.fhir.fixtures import FixtureFhirClient
from copilot.ingestion.extractor import ExtractionError, FhirBinaryByteSource

_LAB_PDF = (
    Path(__file__).parent / "fixtures" / "documents" / "pdfs" / "sergio-angulo-lab-report.pdf"
)


def _client_with(handler: object) -> HttpFhirClient:
    """An HttpFhirClient whose transport is mocked (mirrors test_per_request_token's seam)."""
    client = HttpFhirClient("https://fhir.example/apis/default/fhir", "tok")
    client._client = httpx.AsyncClient(
        base_url="https://fhir.example/apis/default/fhir",
        headers=dict(client._client.headers),
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )
    return client


async def test_http_get_document_bytes_returns_raw_stream() -> None:
    """OpenEMR streams the raw file for GET /Binary/{id}; the client returns those bytes verbatim.

    If this broke, OCR would receive an empty or wrong payload and the lab report would silently
    extract nothing in prod.
    """
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        return httpx.Response(
            200, headers={"content-type": "application/pdf"}, content=b"%PDF-1.7 x"
        )

    client = _client_with(handler)
    data = await client.get_document_bytes("a242fb16-81b3-4b5e-83ae-954b27e42a9a")
    assert data == b"%PDF-1.7 x"
    assert seen["path"].endswith("/Binary/a242fb16-81b3-4b5e-83ae-954b27e42a9a")
    await client.aclose()


async def test_http_get_document_bytes_decodes_fhir_binary_json() -> None:
    """If the server returns a FHIR Binary resource instead of a raw stream, decode its base64 data.

    Keeps the fetch correct whether OpenEMR streams the file or returns a standards Binary resource.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        payload = {"resourceType": "Binary", "contentType": "application/pdf"}
        payload["data"] = base64.b64encode(b"%PDF-json").decode()
        return httpx.Response(200, headers={"content-type": "application/fhir+json"}, json=payload)

    client = _client_with(handler)
    assert await client.get_document_bytes("doc-1") == b"%PDF-json"
    await client.aclose()


async def test_http_get_document_bytes_raises_on_error() -> None:
    """A denied/failed Binary read raises FhirError so extraction degrades, not crashes."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401)

    client = _client_with(handler)
    with pytest.raises(FhirError):
        await client.get_document_bytes("doc-1")
    await client.aclose()


async def test_fixture_client_serves_document_bytes() -> None:
    """The fixture client returns the configured PDF, so the offline path exercises real OCR."""
    client = FixtureFhirClient.from_seed(str(_LAB_PDF))
    data = await client.get_document_bytes("any-id")
    assert data[:5] == b"%PDF-"


async def test_fixture_client_without_pdf_raises() -> None:
    """A fixture client with no PDF configured reports the gap rather than returning empty bytes."""
    client = FixtureFhirClient.from_seed()
    with pytest.raises(FhirError):
        await client.get_document_bytes("any-id")


class _StubBytesClient:
    """Minimal FhirClient stand-in for the byte-source, returning canned bytes or raising."""

    def __init__(self, *, raises: bool = False) -> None:
        self._raises = raises

    async def get_document_bytes(self, document_id: str) -> bytes:
        if self._raises:
            raise FhirError("boom")
        return b"%PDF-stub"


async def test_binary_byte_source_passes_bytes_through() -> None:
    """FhirBinaryByteSource returns exactly what the FHIR client yields for the given id."""
    source = FhirBinaryByteSource(_StubBytesClient())  # type: ignore[arg-type]
    assert await source.fetch("doc-1") == b"%PDF-stub"


async def test_binary_byte_source_wraps_fhir_error_as_extraction_error() -> None:
    """A FHIR failure surfaces as ExtractionError so attach_and_extract degrades to no facts.

    attach_and_extract only catches ExtractionError; a leaked FhirError would crash the whole turn.
    """
    source = FhirBinaryByteSource(_StubBytesClient(raises=True))  # type: ignore[arg-type]
    with pytest.raises(ExtractionError):
        await source.fetch("doc-1")
