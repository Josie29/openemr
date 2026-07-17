import logging

import pytest

from copilot.fhir.models import LabObservation
from copilot.main import _MAX_LAB_SERIES, _build_lab_series
from copilot.verification import FetchLog


def _obs(
    resource_id: str,
    code: str | None,
    *,
    value: float | None = 1.0,
    date: str | None = "2026-06-03T00:00:00+00:00",
    display: str = "Hemoglobin",
    unit: str | None = "g/dL",
    status: str = "final",
) -> LabObservation:
    return LabObservation(
        resource_id=resource_id,
        code=code,
        display=display,
        value=value,
        unit=unit,
        effective_date=date,
        status=status,
    )


def _log(*observations: LabObservation) -> FetchLog:
    fetched = FetchLog()
    fetched.record_all(list(observations))
    return fetched


def test_two_dated_points_become_one_series_oldest_first() -> None:
    """A physician asking about a lab over time must get the draws in chronological order.

    Out-of-order points would draw a rising marker as falling.
    """
    series = _build_lab_series(
        _log(
            _obs("o2", "718-7", value=16.1, date="2026-06-03T00:00:00+00:00"),
            _obs("o1", "718-7", value=13.3, date="2021-05-05T00:00:00+00:00"),
        )
    )

    assert len(series) == 1
    assert series[0]["code"] == "718-7"
    assert [p["value"] for p in series[0]["points"]] == [13.3, 16.1]
    assert [p["observation_id"] for p in series[0]["points"]] == ["o1", "o2"]


def test_single_point_analyte_is_not_a_trend() -> None:
    """One draw is not a trend and must not render — the user's explicit rule.

    Without this a lone value would draw a degenerate one-point 'line'.
    """
    series = _build_lab_series(_log(_obs("o1", "718-7")))

    assert series == []


def test_valueless_and_undated_points_are_dropped_never_coerced() -> None:
    """A point needs both a number and a date to be plotted; the rest are dropped, not faked.

    OpenEMR records questionnaire scores with no value and can omit a collection date; coercing
    either (None->0, undated->today) would fabricate a clinical data point.
    """
    series = _build_lab_series(
        _log(
            _obs("keep1", "718-7", value=13.3, date="2021-05-05T00:00:00+00:00"),
            _obs("keep2", "718-7", value=16.1, date="2026-06-03T00:00:00+00:00"),
            _obs("noval", "718-7", value=None, date="2024-01-01T00:00:00+00:00"),
            _obs("nodate", "718-7", value=14.0, date=None),
        )
    )

    assert len(series[0]["points"]) == 2
    assert {p["observation_id"] for p in series[0]["points"]} == {"keep1", "keep2"}


def test_distinct_loinc_codes_become_distinct_series() -> None:
    """Each analyte is its own chart, grouped by LOINC — never merged by similar display name.

    This is what stops the three 'Platelet...' codes plotting fL beside 10*3/uL on one axis.
    """
    series = _build_lab_series(
        _log(
            _obs("h1", "718-7", display="Hemoglobin", value=13.3, date="2021-05-05T00:00:00+00:00"),
            _obs("h2", "718-7", display="Hemoglobin", value=16.1, date="2026-06-03T00:00:00+00:00"),
            _obs(
                "p1",
                "777-3",
                display="Platelets",
                unit="10*3/uL",
                value=300.0,
                date="2021-05-05T00:00:00+00:00",
            ),
            _obs(
                "p2",
                "777-3",
                display="Platelets",
                unit="10*3/uL",
                value=177.0,
                date="2026-06-03T00:00:00+00:00",
            ),
        )
    )

    assert {s["code"] for s in series} == {"718-7", "777-3"}
    assert {s["unit"] for s in series} == {"g/dL", "10*3/uL"}


def test_preliminary_status_is_carried_through() -> None:
    """A persisted-but-unconfirmed point reaches the sidebar as 'preliminary' so it renders amber.

    If status were lost, a model-derived value would be shown as a clinician-confirmed result.
    """
    series = _build_lab_series(
        _log(
            _obs("f", "718-7", value=13.3, date="2021-05-05T00:00:00+00:00", status="final"),
            _obs("p", "718-7", value=16.1, date="2026-06-03T00:00:00+00:00", status="preliminary"),
        )
    )

    statuses = {p["observation_id"]: p["status"] for p in series[0]["points"]}
    assert statuses == {"f": "final", "p": "preliminary"}


def test_non_observation_records_are_ignored() -> None:
    """The projection reads only lab Observations; other fetched resources never enter a chart."""

    class _NotAnObservation:
        resource_type = "Observation"  # same tag, wrong type — must still be skipped
        resource_id = "x"

    fetched = FetchLog()
    fetched.record("Observation", "x", _NotAnObservation())
    fetched.record_all(
        [
            _obs("o1", "718-7", value=13.3, date="2021-05-05T00:00:00+00:00"),
            _obs("o2", "718-7", value=16.1, date="2026-06-03T00:00:00+00:00"),
        ]
    )

    series = _build_lab_series(fetched)

    assert len(series) == 1
    assert {p["observation_id"] for p in series[0]["points"]} == {"o1", "o2"}


def test_series_are_capped_and_the_drop_is_logged(caplog: pytest.LogCaptureFixture) -> None:
    """A broad fetch must not wall the sidebar; truncation is bounded and never silent.

    Silent truncation would read as 'these are all the trends' when it isn't.
    """
    observations: list[LabObservation] = []
    for i in range(_MAX_LAB_SERIES + 3):
        code = f"loinc-{i}"
        observations.append(_obs(f"a{i}", code, value=1.0, date="2021-01-01T00:00:00+00:00"))
        observations.append(_obs(f"b{i}", code, value=2.0, date="2026-01-01T00:00:00+00:00"))

    with caplog.at_level(logging.INFO, logger="copilot"):
        series = _build_lab_series(_log(*observations))

    assert len(series) == _MAX_LAB_SERIES
    assert any("lab_series truncated" in record.message for record in caplog.records)
