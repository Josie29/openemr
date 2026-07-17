import pytest
from pydantic import BaseModel

from copilot.ingestion import extractor


def _probe_models() -> list[type[BaseModel]]:
    """Every probe model the extractor sends to Mistral in schema mode.

    Returns:
        The private ``_*Probe`` BaseModel subclasses declared in ``copilot.ingestion.extractor``.
    """
    return [
        obj
        for name, obj in vars(extractor).items()
        if name.startswith("_")
        and name.endswith("Probe")
        and isinstance(obj, type)
        and issubclass(obj, BaseModel)
    ]


def test_every_probe_model_is_discovered() -> None:
    # Guards the guard: if the probes are renamed or moved, the discovery below would silently match
    # nothing and the real test would vacuously pass on an empty list.
    assert len(_probe_models()) >= 6


@pytest.mark.parametrize("model", _probe_models(), ids=lambda m: m.__name__)
def test_probe_fields_are_required_even_when_nullable(model: type[BaseModel]) -> None:
    # THE rule this pipeline has now learned twice, at real cost, and never enforced until now:
    # a Pydantic field with a default is omitted from the JSON schema's "required" list, and Mistral
    # schema mode then silently drops it from document_annotation. Not an error — the key is simply
    # absent, every downstream .get() returns None, and the extraction looks like a document that
    # did not print those values.
    #
    # It cost six of nine fields on the intake form (JOS-80). It then cost the lab report unit
    # (27 of 28 rows), collection_date (28 of 28) and the abnormal rows' reference_range the moment
    # JOS-87 added a seventh field to _LabResultProbe -- silently deleting the columns that prove a
    # lab value is abnormal, while every test of that PR passed.
    #
    # A field that may be absent is typed `X | None` with NO default: still required, comes back an
    # explicit null. This test fails the next time someone adds a convenient `default=None`.
    schema = model.model_json_schema()
    required = set(schema.get("required", []))
    missing = sorted(set(schema["properties"]) - required)
    assert not missing, (
        f"{model.__name__}: {missing} are optional in the JSON schema, so Mistral will drop them "
        "from document_annotation. Remove the default — use `X | None` with no default to keep a "
        "nullable field required."
    )
