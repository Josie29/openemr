from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from copilot.fhir.models import ResourceIdentity
from copilot.ingestion.schemas import BoundingBox, DocType, LabDetail
from copilot.schemas import ChatResponse, Claim, SourceRef

_MISSING = object()


@dataclass(frozen=True)
class Resolution:
    """What resolving a citation against its source yields: the verified value and record identity.

    Resolving and identifying a citation are the same lookup, so one ``resolve`` returns both — the
    gate stamps the grounded ``value`` and the record ``identity`` (name + key date) from this. The
    wire :class:`~copilot.schemas.Citation` is a further *pure* projection of the stamped
    ``SourceRef`` (see :meth:`~copilot.schemas.SourceRef.to_citation`), so it is derived at the
    response boundary rather than carried here.

    A document-extraction resolver (JOS-54) additionally returns the click-to-source overlay
    provenance — the source ``document_id``, ``doc_type``, ``page``, and ``bounding_box`` (**in PDF
    points**) — which :func:`_stamp` copies onto the ``SourceRef``. FHIR/guideline resolvers leave
    these None. A *lab* fact additionally returns ``lab_detail`` (JOS-88), the analyte metadata the
    sidebar's lab table renders; every other fact leaves it None.
    """

    value: str
    identity: ResourceIdentity | None
    document_id: str | None = None
    doc_type: DocType | None = None
    page: int | None = None
    bounding_box: BoundingBox | None = None
    lab_detail: LabDetail | None = None


class CitationResolver(Protocol):
    """A source of ground truth a claim's citation can be resolved against.

    The grounding gate (:func:`resolve_claims`) depends only on this single ``resolve`` method, not
    on where the evidence came from. :class:`FetchLog` is the FHIR-record implementation (the Week-1
    gate); the Week-2 guideline-evidence registry implements the same protocol, so the one gate
    attaches unchanged to each worker and the final answer (the "crown jewel survives the port"
    requirement — ``context/decisions/agent-framework-week2.md``). A composite resolver over
    several sources lets the final answer ground claims that draw on FHIR records *and* guideline
    chunks in a single pass.
    """

    def resolve(self, ref: SourceRef) -> Resolution | None:
        """Resolve a citation to its verified value and identity, or None when it cannot ground."""
        ...


@dataclass
class CompositeResolver:
    """A :class:`CitationResolver` over several sources, tried in order until one grounds.

    The final answer draws on both FHIR records (via :class:`FetchLog`) and guideline chunks (via
    the evidence registry), so its gate resolves each citation against whichever source owns it.
    Each sub-resolver already returns None for a citation it does not recognise (a FHIR log ignores
    a guideline chunk and vice versa), so first-non-None is an unambiguous dispatch.
    """

    resolvers: tuple[CitationResolver, ...]

    def resolve(self, ref: SourceRef) -> Resolution | None:
        """Resolve against the first sub-resolver that grounds the citation, else None."""
        for resolver in self.resolvers:
            resolution = resolver.resolve(ref)
            if resolution is not None:
                return resolution
        return None


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

    def records_of_type(self, resource_type: str) -> list[Any]:
        """Return every recorded resource of one FHIR type, in insertion order.

        Lets a payload projection (e.g. the lab trend chart) read what a turn fetched without
        reaching into the private store or knowing the ids.

        Args:
            resource_type: FHIR resource type to filter by, e.g. ``"Observation"``.

        Returns:
            The recorded objects whose key type matches; empty when none were fetched.
        """
        return [obj for (rtype, _), obj in self._objects.items() if rtype == resource_type]

    def resolve(self, ref: SourceRef) -> Resolution | None:
        """Resolve a citation to the real value and identity of the fetched resource.

        Two grounding modes, both deterministic: a ``quote`` citation (free-text note) is grounded
        when the quote appears verbatim in the fetched note's text; otherwise a ``field`` citation
        (structured resource) resolves to that field's value. The record's identity (name + key
        date) is read from the same fetched object — code-derived, never model-authored.

        Args:
            ref: The claim's citation.

        Returns:
            The :class:`Resolution` (value + identity), or None when the claim cannot be grounded —
            the resource was not fetched, neither quote nor field is cited, the field or note text
            is missing, or the quote is not found in the note.
        """
        resource = self._objects.get((ref.resource_type, ref.resource_id))
        if resource is None:
            return None
        if ref.quote is not None:
            value = _resolve_quote(resource, ref.quote)
        elif ref.field is not None:
            raw = getattr(resource, ref.field, _MISSING)
            value = None if raw is _MISSING or raw is None else str(raw)
        else:
            value = None
        if value is None:
            return None
        identity: ResourceIdentity = resource.citation_identity
        return Resolution(value=value, identity=identity)


def _normalize_for_match(text: str) -> str:
    """Normalize text for quote matching: collapse whitespace and fold case.

    Collapsing runs of whitespace lets line breaks and reflowing not defeat an otherwise-verbatim
    quote. Case-folding lets a span the model lifted from mid-sentence — and so capitalized as a
    standalone quote (``"The diagnosis..."`` for the chunk's ``"...the diagnosis..."``) — still
    ground. Word order and the words themselves are preserved, so a genuine paraphrase still fails
    and forces the model to quote exactly.
    """
    return " ".join(text.casefold().split())


def quote_in_text(quote: str, text: str | None) -> str | None:
    """Ground a verbatim quote against a body of text: the quote must appear in it.

    The deterministic substring check both the FHIR note gate and the guideline-evidence gate
    share, so quote grounding behaves identically for a clinical note and a retrieved guideline
    chunk. Matching is whitespace-normalized and case-insensitive (see :func:`_normalize_for_match`)
    so reflowing and the capitalization of a lifted mid-sentence span don't defeat an
    otherwise-verbatim quote; a paraphrase still fails and forces the model to quote exactly.

    Args:
        quote: The verbatim span the claim says supports it.
        text: The body of text the quote must appear in (a note body, a chunk), or None.

    Returns:
        The stripped quote when it is a whitespace- and case-normalized substring of ``text``;
        otherwise None (``text`` is absent, or the quote is not found).
    """
    if not isinstance(text, str) or not quote.strip():
        return None
    if _normalize_for_match(quote) in _normalize_for_match(text):
        return quote.strip()
    return None


def _resolve_quote(resource: Any, quote: str) -> str | None:
    """Ground a free-text note citation: the quote must appear verbatim in the note's text.

    This keeps note grounding deterministic — the cited span literally exists in the fetched note,
    or the claim is refused.

    Args:
        resource: The fetched resource the claim cites (expected to carry a ``text`` note body).
        quote: The verbatim span the claim says supports it.

    Returns:
        The stripped quote when it grounds against the resource's ``text``; otherwise None.
    """
    return quote_in_text(quote, getattr(resource, "text", None))


def resolve_claims(
    response: ChatResponse, resolver: CitationResolver
) -> tuple[ChatResponse, list[Claim]]:
    """Ground each claim against a citation source, stamping the real value into its citation.

    For every claim, resolve its citation to the actual value in the source it draws on (a FHIR
    record via :class:`FetchLog`, a guideline chunk via the evidence registry, or either via a
    composite). Grounded claims get that value stamped into ``source.value`` — code-populated,
    never model-authored — so a reader can compare the claim to the exact source value. The cited
    record's identity (``label``/``date``/``date_label``) is stamped the same way, so the evidence
    card names the *specific* record (e.g. "Asthma", onset date), not just its type. Claims that
    cannot be resolved are returned as offenders for the gate to reject.

    Args:
        response: The agent's candidate answer.
        resolver: The source of ground truth to resolve each citation against.

    Returns:
        A tuple of (the response with values stamped onto grounded claims, the offending
        claims that could not be grounded). An empty offender list means every claim is
        grounded and the stamped response is safe to return.
    """
    grounded, offenders = ground_claims(response.claims, resolver)
    return response.model_copy(update={"claims": grounded}), offenders


def ground_claims(
    claims: Sequence[Claim], resolver: CitationResolver
) -> tuple[list[Claim], list[Claim]]:
    """Split a list of claims into (grounded, offenders), stamping the grounded ones.

    The core of the gate, decoupled from any particular answer model so it grounds the claims of a
    worker's output (``ExtractorOutput``, ``RetrieverOutput``) exactly as it grounds the final
    ``ChatResponse``. A claim is grounded only when its primary citation *and* every supporting
    citation resolve — so a statement drawing on an uncited (or merely inferred) source is
    rejected, not shipped — and each grounded claim carries the source's real value and identity,
    stamped by code.

    Args:
        claims: The candidate claims to ground.
        resolver: The source of ground truth to resolve each citation against.

    Returns:
        A ``(grounded, offenders)`` pair. An empty offender list means every claim grounded.
    """
    grounded: list[Claim] = []
    offenders: list[Claim] = []
    for claim in claims:
        stamped_source = _stamp(resolver, claim.source)
        stamped_supporting = [_stamp(resolver, ref) for ref in claim.supporting]
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
    return grounded, offenders


def _stamp(resolver: CitationResolver, ref: SourceRef) -> SourceRef | None:
    """Resolve one citation and stamp the source's real value and identity into it.

    Args:
        resolver: The source of ground truth to resolve the citation against.
        ref: A single citation (primary or supporting).

    Returns:
        The citation with ``value`` and identity (``label``/``date``/``date_label``) stamped from
        the resolved source, or None when the citation cannot be grounded.
    """
    resolution = resolver.resolve(ref)
    if resolution is None:
        return None
    update: dict[str, Any] = {"value": resolution.value}
    if resolution.identity is not None:
        update["label"] = resolution.identity.label
        update["date"] = resolution.identity.date
        update["date_label"] = resolution.identity.date_label
    # Click-to-source overlay provenance (JOS-54/57) is ALWAYS system-set from the resolution —
    # written unconditionally (None for a non-document fact) so anything the model authored here is
    # stripped, never trusted. `doc_type` is the load-bearing one: `to_citation` routes a citation
    # to its document arm on `doc_type is not None`, so a model-authored doc_type would make a plain
    # FHIR fact serialize as a document citation — and draw whatever box it also invented onto a
    # source scan the fact was never read from.
    update["document_id"] = resolution.document_id
    update["doc_type"] = resolution.doc_type
    update["page"] = resolution.page
    update["bounding_box"] = resolution.bounding_box
    # `lab_detail` rides the same unconditional rule and needs it just as badly. The sidebar renders
    # reference_range/abnormal_flag as system-stamped fact, so a model-authored range would make a
    # normal value read as abnormal — or hide an abnormal one — under a UI that says the cells came
    # off the page. Written for EVERY fact (None for a non-lab one), so anything the model authored
    # here is stripped, never trusted. Do NOT guard this with
    # `if resolution.lab_detail is not None`:
    # that reopens exactly this hole for any fact the resolver doesn't supply a detail for.
    update["lab_detail"] = resolution.lab_detail
    return ref.model_copy(update=update)
