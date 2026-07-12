from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from copilot.fhir.models import ResourceIdentity
from copilot.schemas import ChatResponse, Claim, SourceRef

_MISSING = object()


class FhirRecordable(Protocol):
    """A fetched resource that knows its own FHIR identity.

    Every typed FHIR model exposes ``resource_type``/``resource_id`` (all
    :meth:`FetchLog.record_all` needs to log it) plus a ``citation_identity`` naming the specific
    record for the evidence card. Structural typing keeps ``verification`` free of a dependency on
    the concrete model classes — it depends only on the shared ``ResourceIdentity`` value, not on
    ``Problem``/``Medication``/etc. Declared as read-only properties so the frozen Pydantic models
    (whose fields are read-only) satisfy it.
    """

    @property
    def resource_type(self) -> str: ...

    @property
    def resource_id(self) -> str: ...

    @property
    def citation_identity(self) -> ResourceIdentity: ...


@dataclass
class FetchLog:
    """Registry of the typed resources a turn's tools returned, keyed by (type, id).

    Stores the actual parsed objects (not just their ids) so the verification gate can resolve
    a claim's cited field to the real value in the record — the field-level grounding check
    (ARCHITECTURE.md §7). This is what makes grounding deterministic: it compares against the
    fetched data, not the model's say-so.
    """

    _objects: dict[tuple[str, str], Any] = field(default_factory=dict)

    def record(self, resource_type: str, resource_id: str, resource: Any) -> None:
        """Record a resource a tool returned this turn.

        Args:
            resource_type: FHIR resource type, e.g. ``"Patient"``.
            resource_id: FHIR resource logical id.
            resource: The parsed, typed resource object (e.g. ``PatientDemographics``).
        """
        self._objects[(resource_type, resource_id)] = resource

    def record_all(self, resources: FhirRecordable | Sequence[FhirRecordable]) -> None:
        """Record one resource or a list of them, keyed by each resource's own identity.

        Centralizes the "a tool records exactly what it returns" invariant so no tool has to
        spell out the ``(resource_type, resource_id)`` key or loop by hand.

        Args:
            resources: A single fetched resource, or a sequence of them.
        """
        items = resources if isinstance(resources, Sequence) else [resources]
        for resource in items:
            self.record(resource.resource_type, resource.resource_id, resource)

    def resolve(self, ref: SourceRef) -> str | None:
        """Resolve a citation to the real value in the fetched resource.

        Two modes, both deterministic: a ``quote`` citation (free-text note) is grounded when the
        quote appears verbatim in the fetched note's text; otherwise a ``field`` citation
        (structured resource) resolves to that field's value.

        Args:
            ref: The claim's citation.

        Returns:
            The stringified record value (or the matched quote), or None when the claim cannot be
            grounded — the resource was not fetched, neither quote nor field is cited, the field or
            note text is missing, or the quote is not found in the note.
        """
        resource = self._objects.get((ref.resource_type, ref.resource_id))
        if resource is None:
            return None
        if ref.quote is not None:
            return _resolve_quote(resource, ref.quote)
        if ref.field is None:
            return None
        value = getattr(resource, ref.field, _MISSING)
        if value is _MISSING or value is None:
            return None
        return str(value)

    def identify(self, ref: SourceRef) -> ResourceIdentity | None:
        """Return the identity (name + key date) of the resource a citation points at.

        Independent of which field the claim grounds on: it names the *specific* fetched record so
        the card can distinguish, say, the asthma Condition from any other active Condition. Like
        :meth:`resolve`, it reads only the fetched typed object — the identity is code-derived from
        the record, never model-authored.

        Args:
            ref: The claim's citation.

        Returns:
            The record's :class:`ResourceIdentity`, or None when the cited resource was not fetched
            this turn (the same gate ``resolve`` applies).
        """
        resource = self._objects.get((ref.resource_type, ref.resource_id))
        if resource is None:
            return None
        identity: ResourceIdentity = resource.citation_identity
        return identity


def _normalize_ws(text: str) -> str:
    """Collapse runs of whitespace so quote matching tolerates line breaks and reflowing."""
    return " ".join(text.split())


def _resolve_quote(resource: Any, quote: str) -> str | None:
    """Ground a free-text note citation: the quote must appear verbatim in the note's text.

    This keeps note grounding deterministic — the cited span literally exists in the fetched note,
    or the claim is refused. A paraphrase fails and forces the model to quote exactly.

    Args:
        resource: The fetched resource the claim cites (expected to carry a ``text`` note body).
        quote: The verbatim span the claim says supports it.

    Returns:
        The stripped quote when it is a whitespace-normalized substring of the note text; otherwise
        None (the resource has no text, or the quote is not found).
    """
    text = getattr(resource, "text", None)
    if not isinstance(text, str) or not quote.strip():
        return None
    if _normalize_ws(quote) in _normalize_ws(text):
        return quote.strip()
    return None


def resolve_claims(response: ChatResponse, fetched: FetchLog) -> tuple[ChatResponse, list[Claim]]:
    """Ground each claim against the fetched data, stamping the real value into its citation.

    For every claim, resolve its citation to the actual value in the record a tool returned.
    Grounded claims get that value stamped into ``source.value`` — code-populated, never
    model-authored — so a reader can compare the claim to the exact record value. The cited
    record's identity (``label``/``date``/``date_label``) is stamped the same way, so the evidence
    card names the *specific* record (e.g. "Asthma", onset date), not just its type. Claims that
    cannot be resolved are returned as offenders for the gate to reject.

    Args:
        response: The agent's candidate answer.
        fetched: The registry of resources tools returned this turn.

    Returns:
        A tuple of (the response with values stamped onto grounded claims, the offending
        claims that could not be grounded). An empty offender list means every claim is
        grounded and the stamped response is safe to return.
    """
    grounded: list[Claim] = []
    offenders: list[Claim] = []
    for claim in response.claims:
        stamped_source = _stamp(fetched, claim.source)
        stamped_supporting = [_stamp(fetched, ref) for ref in claim.supporting]
        # A claim is grounded only when its primary citation AND every supporting citation resolve —
        # so a statement drawing on an uncited (or merely inferred) record is rejected, not shipped.
        if stamped_source is None or None in stamped_supporting:
            offenders.append(claim)
        else:
            grounded.append(
                claim.model_copy(
                    update={
                        "source": stamped_source,
                        "supporting": [ref for ref in stamped_supporting if ref is not None],
                    }
                )
            )
    return response.model_copy(update={"claims": grounded}), offenders


def _stamp(fetched: FetchLog, ref: SourceRef) -> SourceRef | None:
    """Resolve one citation and stamp the record's real value and identity into it.

    Args:
        fetched: The registry of resources tools returned this turn.
        ref: A single citation (primary or supporting).

    Returns:
        The citation with ``value`` and identity (``label``/``date``/``date_label``) stamped from
        the fetched record, or None when the citation cannot be grounded.
    """
    value = fetched.resolve(ref)
    if value is None:
        return None
    update: dict[str, str | None] = {"value": value}
    identity = fetched.identify(ref)
    if identity is not None:
        update["label"] = identity.label
        update["date"] = identity.date
        update["date_label"] = identity.date_label
    return ref.model_copy(update=update)
