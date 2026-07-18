from typing import Any

from copilot.fhir.fixtures import FixtureFhirClient
from copilot.fhir.models import LabObservation


def _observation(
    resource_id: str,
    code: str,
    display: str,
    *,
    effective: str | None = "2026-06-03T00:00:00+00:00",
    value: float | None = 1.0,
    unit: str = "fL",
    category: str = "laboratory",
) -> dict[str, Any]:
    """An Observation shaped like OpenEMR's live FHIR projection of a `procedure_result` row."""
    resource: dict[str, Any] = {
        "resourceType": "Observation",
        "id": resource_id,
        "status": "final",
        "category": [
            {
                "coding": [
                    {
                        "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                        "code": category,
                    }
                ]
            }
        ],
        "code": {"coding": [{"system": "http://loinc.org", "code": code, "display": display}]},
        "subject": {"reference": "Patient/1", "type": "Patient"},
    }
    if effective is not None:
        resource["effectiveDateTime"] = effective
    if value is not None:
        resource["valueQuantity"] = {
            "value": value,
            "unit": unit,
            "system": "http://unitsofmeasure.org",
            "code": unit,
        }
    return resource


def _client(*observations: dict[str, Any]) -> FixtureFhirClient:
    return FixtureFhirClient(
        {
            "1": {
                "Patient": {"resourceType": "Patient", "id": "1"},
                "Observation": list(observations),
            }
        }
    )


async def test_code_filter_isolates_an_analyte_from_its_namesakes() -> None:
    """Filtering by LOINC must return ONE analyte, never its similarly-named siblings.

    Sergio's record really does carry three distinct LOINCs whose display names all begin
    "Platelet": 777-3 (count, 10*3/uL), 32207-3 (distribution width, fL) and 32623-1 (mean
    volume, fL). If this ever matched on the name instead of the code, a trend would merge
    three different analytes and plot ~9 fL beside ~300 10*3/uL as one series — a wrong
    clinical reading, not a cosmetic bug.
    """
    client = _client(
        _observation(
            "p-count", "777-3", "Platelets [#/volume] in Blood", value=300.0, unit="10*3/uL"
        ),
        _observation("p-width", "32207-3", "Platelet distribution width", value=9.4),
        _observation("p-volume", "32623-1", "Platelet [Entitic mean volume]", value=9.8),
    )

    platelet_counts = await client.get_lab_observations("1", code="777-3")

    assert [o.resource_id for o in platelet_counts] == ["p-count"]
    assert platelet_counts[0].unit == "10*3/uL"


async def test_observations_are_returned_oldest_first() -> None:
    """A trend reads left-to-right in time, and the FHIR Bundle's order is not guaranteed.

    If this ordering broke, a chart would draw the series out of sequence and a rising
    marker could read as falling.
    """
    client = _client(
        _observation("recent", "787-2", "MCV", effective="2026-06-03T00:00:00+00:00", value=90.6),
        _observation("older", "787-2", "MCV", effective="2021-05-05T00:00:00+00:00", value=82.8),
    )

    series = await client.get_lab_observations("1", code="787-2")

    assert [o.resource_id for o in series] == ["older", "recent"]


async def test_undated_observation_never_sorts_last() -> None:
    """An undated result must not occupy the newest slot.

    OpenEMR permits a result with no effectiveDateTime. Sorting it last would present a
    result of unknown age as the patient's current value.
    """
    client = _client(
        _observation("dated", "787-2", "MCV", effective="2021-05-05T00:00:00+00:00", value=82.8),
        _observation("undated", "787-2", "MCV", effective=None, value=99.9),
    )

    series = await client.get_lab_observations("1", code="787-2")

    assert series[-1].resource_id == "dated"


async def test_observation_without_a_value_is_kept_with_value_none() -> None:
    """A lab Observation carrying no value[x] must parse, not raise, and expose value=None.

    OpenEMR records scored questionnaires (PHQ-9, some GAD-7) as laboratory Observations with
    no value at all — 8 of Sergio's 48. Raising here would fail every lab read for the patient;
    inventing a number would fabricate a clinical value.
    """
    client = _client(_observation("phq", "89204-2", "PHQ-9 total score", value=None))

    results = await client.get_lab_observations("1")

    assert len(results) == 1
    assert results[0].value is None
    assert results[0].display == "PHQ-9 total score"


async def test_non_laboratory_observations_are_excluded() -> None:
    """Vitals must never enter a lab trend.

    OpenEMR serves vitals, social history and labs all as Observation; only the category
    separates them. Without the filter, a body-temperature reading could be charted as a lab.
    """
    client = _client(
        _observation("lab", "787-2", "MCV", value=90.6),
        _observation("vital", "8310-5", "Body temperature", value=37.0, category="vital-signs"),
    )

    results = await client.get_lab_observations("1")

    assert [o.resource_id for o in results] == ["lab"]


def test_quantity_value_of_zero_is_preserved() -> None:
    """A result of exactly 0 must stay 0, not collapse to None.

    A falsy-check instead of a None-check would silently drop a genuine zero result and the
    point would vanish from the trend.
    """
    observation = LabObservation.from_fhir(_observation("z", "787-2", "MCV", value=0.0))

    assert observation.value == 0.0


async def test_seed_bundle_serves_sergios_real_lab_series() -> None:
    """The bundled fixture must carry a real multi-point series so fixture mode can demo a trend.

    Fixture mode is the no-OpenEMR path; if the seed lost its Observations, the agent would
    answer "no labs on file" for the demo patient with no test noticing.
    """
    client = FixtureFhirClient.from_seed()

    mcv = await client.get_lab_observations("23", code="787-2")

    assert len(mcv) == 2
    assert [o.effective_date[:10] for o in mcv if o.effective_date] == ["2021-05-05", "2026-06-03"]
    assert all(o.unit == "fL" for o in mcv)
