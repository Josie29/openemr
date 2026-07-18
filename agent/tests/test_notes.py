import base64

import httpx
from fastapi.testclient import TestClient

from copilot.config import Settings
from copilot.fhir.fixtures import FixtureFhirClient
from copilot.fhir.models import NoteContent
from copilot.graph.outputs import ExtractorOutput
from copilot.graph.routing import Route
from copilot.main import create_app
from copilot.schemas import ChatResponse, Claim, SourceRef
from copilot.verification import FetchLog
from graph_script import override_graph, route_model, worker_model

# A verbatim span of the seed note on encounter enc-2025-11 (see patient-1-reyes.bundle.json).
_NOTE_QUOTE = "Metformin 500 mg continued; tolerating well, no GI upset reported."


def test_note_decodes_base64_text_and_links_to_its_encounter() -> None:
    # Guards note parsing: OpenEMR emits note prose as base64 text/plain in content[].attachment,
    # linked to an encounter via context.encounter. If decoding/linking breaks, the free-text tool
    # returns empty text and grounding can never match a quote.
    resource = {
        "resourceType": "DocumentReference",
        "id": "n1",
        "content": [
            {
                "attachment": {
                    "contentType": "text/plain",
                    "data": base64.b64encode(b"Hello note.").decode(),
                }
            }
        ],
        "context": {"encounter": [{"reference": "Encounter/e1"}]},
    }
    note = NoteContent.from_fhir(resource)

    assert note.text == "Hello note."
    assert note.encounter_id == "e1"


def test_note_with_no_attachment_data_yields_none_text() -> None:
    # Guards the data-absent variant OpenEMR emits for an empty note — must not crash, text is None.
    note = NoteContent.from_fhir(
        {
            "resourceType": "DocumentReference",
            "id": "n2",
            "content": [{"attachment": {"contentType": "text/plain"}}],
        }
    )
    assert note.text is None


async def test_fixture_returns_only_the_requested_encounters_note(
    seed_client: FixtureFhirClient,
) -> None:
    # Guards the client-side encounter filter (FHIR has no `encounter` search param): the note is
    # returned for its own encounter and NOT for a different one.
    notes = await seed_client.get_encounter_note("1", "enc-2025-11")
    assert len(notes) == 1
    assert "no GI upset reported" in (notes[0].text or "")
    assert await seed_client.get_encounter_note("1", "enc-2026-06") == []


def test_quote_grounding_matches_verbatim_and_rejects_absent(
    seed_client: FixtureFhirClient,
) -> None:
    # Guards the deterministic quote check: a verbatim span (whitespace-normalized) grounds and is
    # stamped; a paraphrase that isn't in the note does NOT ground. This is what keeps free-text
    # citations as trustworthy as structured ones without an LLM judge.
    note = NoteContent(resource_id="n1", text="Line one.\n   Metformin  continued  today.")
    log = FetchLog()
    log.record(note.resource_type, note.resource_id, note)

    good = SourceRef(
        resource_type="DocumentReference", resource_id="n1", quote="Metformin continued today."
    )
    resolution = log.resolve(good)
    assert resolution is not None and resolution.value == "Metformin continued today."

    fabricated = SourceRef(
        resource_type="DocumentReference", resource_id="n1", quote="Metformin stopped."
    )
    assert log.resolve(fabricated) is None


def _post(client: TestClient, message: str) -> httpx.Response:
    # Starlette's TestClient returns its vendored httpx Response (not importable here).
    return client.post("/chat", json={"patient_id": "1", "message": message})  # type: ignore[return-value]


def test_uc3_answer_from_a_note_grounds_on_a_verbatim_quote(settings: Settings) -> None:
    # THE free-text test: the extractor reads a note and the answer carries a claim whose quote is
    # verbatim from that note. It must pass the gate with the quote stamped as the verified value —
    # proving notes are citable and quote-grounded end to end through the graph.
    note_claim = Claim(
        text="At the 2025-11 visit, metformin was continued and tolerated well.",
        source=SourceRef(
            resource_type="DocumentReference", resource_id="note-2025-11", quote=_NOTE_QUOTE
        ),
    )
    app = create_app(settings)
    with override_graph(
        app.state.graph,
        router=route_model([Route.EXTRACT_INTAKE, Route.ANSWER]),
        extractor=worker_model(
            [("get_patient_summary", {}), ("get_encounter_note", {"encounter_id": "enc-2025-11"})],
            ExtractorOutput(summary="Metformin continued.", claims=[note_claim]),
        ),
        answerer=worker_model(
            [], ChatResponse(summary="The note says metformin was continued.", claims=[note_claim])
        ),
    ):
        response = _post(TestClient(app), "Why is she still on metformin — what did the note say?")

    assert response.status_code == 200
    assert response.json()["claims"][0]["source"]["value"] == _NOTE_QUOTE


def test_a_fabricated_note_quote_is_refused(settings: Settings) -> None:
    # Guards the safety property for free text: a claim quoting text that is NOT in the note (a
    # plausible-sounding fabrication) must be refused, not shipped — the extractor's quote check
    # fails and the turn degrades to a refusal.
    fabricated = ExtractorOutput(
        summary="Metformin stopped.",
        claims=[
            Claim(
                text="Metformin was discontinued due to side effects.",
                source=SourceRef(
                    resource_type="DocumentReference",
                    resource_id="note-2025-11",
                    quote="Metformin discontinued due to intolerable side effects.",
                ),
            )
        ],
    )
    app = create_app(settings)
    with override_graph(
        app.state.graph,
        router=route_model([Route.EXTRACT_INTAKE]),
        extractor=worker_model(
            [("get_encounter_note", {"encounter_id": "enc-2025-11"})], fabricated
        ),
    ):
        response = _post(TestClient(app), "Did she stop metformin?")

    assert response.status_code == 200
    body = response.json()
    assert body["claims"] == []
    assert "attribute" in body["summary"].lower()
