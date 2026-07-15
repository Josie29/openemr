import base64
from datetime import date
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


def _codeable_text(concept: Any) -> str | None:
    """Render a FHIR ``CodeableConcept`` to a display string.

    Prefers ``text``, then the first coding's ``display``, then its ``code``. This is the
    always-present human label a claim can cite even when a resource carries no structured code
    (the med/problem text-fallback reality from ``deployment-strategy.md``).

    Args:
        concept: A FHIR ``CodeableConcept`` (dict), or anything.

    Returns:
        A display string, or None when nothing usable is present.
    """
    if not isinstance(concept, dict):
        return None
    text = concept.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()
    codings = concept.get("coding")
    if isinstance(codings, list):
        for coding in codings:
            if not isinstance(coding, dict):
                continue
            display = coding.get("display")
            if isinstance(display, str) and display.strip():
                return display.strip()
            code = coding.get("code")
            if isinstance(code, str) and code.strip():
                return code.strip()
    return None


def _first_coding(concept: Any) -> tuple[str | None, str | None]:
    """Extract ``(code, system)`` from the first coding of a FHIR ``CodeableConcept``.

    Args:
        concept: A FHIR ``CodeableConcept`` (dict), or anything.

    Returns:
        A ``(code, system)`` tuple; either element is None when absent.
    """
    if not isinstance(concept, dict):
        return None, None
    codings = concept.get("coding")
    if not isinstance(codings, list):
        return None, None
    for coding in codings:
        if isinstance(coding, dict) and isinstance(coding.get("code"), str):
            system = coding.get("system") if isinstance(coding.get("system"), str) else None
            return coding["code"], system
    return None, None


def _status_code(status: Any) -> str | None:
    """Pull the code out of a FHIR status ``CodeableConcept`` (e.g. clinicalStatus).

    Args:
        status: A FHIR ``CodeableConcept`` carrying a status code, or anything.

    Returns:
        The status code string (e.g. ``"active"``), or None.
    """
    code, _ = _first_coding(status)
    return code


def bundle_resources(bundle: Any, resource_type: str) -> list[dict[str, Any]]:
    """Extract the resources of one type from a FHIR searchset/collection ``Bundle``.

    A FHIR search (``GET /Condition?patient=X``) returns a ``Bundle`` whose ``entry`` list wraps
    each matching resource. A bare single resource is tolerated too (returned as a one-item list
    when it matches), so fixtures may hold either shape.

    Args:
        bundle: The parsed FHIR ``Bundle`` (or a single resource dict).
        resource_type: The FHIR resource type to keep, e.g. ``"Condition"``.

    Returns:
        The matching resource dicts, in bundle order.
    """
    if not isinstance(bundle, dict):
        return []
    if bundle.get("resourceType") == resource_type:
        return [bundle]
    entries = bundle.get("entry")
    if not isinstance(entries, list):
        return []
    resources: list[dict[str, Any]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        resource = entry.get("resource")
        if isinstance(resource, dict) and resource.get("resourceType") == resource_type:
            resources.append(resource)
    return resources


def _require_id(resource: dict[str, Any], resource_type: str) -> str:
    """Return a FHIR resource's logical id, validating type and presence.

    Args:
        resource: The parsed FHIR resource.
        resource_type: The expected ``resourceType``.

    Returns:
        The resource's string ``id``.

    Raises:
        ValueError: If the resource is the wrong type or lacks a string ``id``.
    """
    actual = resource.get("resourceType")
    if actual != resource_type:
        raise ValueError(f"expected a {resource_type} resource, got {actual!r}")
    resource_id = resource.get("id")
    if not isinstance(resource_id, str) or not resource_id:
        raise ValueError(f"{resource_type} resource is missing a string 'id'")
    return resource_id


class ResourceIdentity(BaseModel):
    """The human-recognizable identity of a fetched resource, for a citation's provenance chip.

    Derived by code from the fetched typed record (never model-authored, same trust rule as a
    citation's stamped ``value``), this is what lets a reader tie a proof card to the *specific*
    record it cites — the display name plus the record's key date — rather than only its resource
    type and one field. Every typed model exposes its own via ``citation_identity``; the
    verification gate stamps it onto the citation and the sidebar renders it on the evidence card.
    """

    model_config = ConfigDict(frozen=True)

    label: str | None = Field(
        default=None, description="Human-recognizable record name, e.g. 'Asthma'"
    )
    date: str | None = Field(default=None, description="The record's key date (ISO), if any")
    date_label: str | None = Field(
        default=None, description="What `date` means for this record, e.g. 'Onset'"
    )


class PatientDemographics(BaseModel):
    """Typed projection of a FHIR R4 ``Patient`` resource (ARCHITECTURE.md §4 tool table).

    Carries its own ``resource_type``/``resource_id`` so a claim can cite it verbatim from the
    tool output — the grounding gate resolves ``(resource_type, resource_id, field)`` against the
    fetched object (ARCHITECTURE.md §7). Produced by parsing the raw FHIR resource at the boundary
    (parse-don't-validate), so downstream code works with a value that guarantees its own validity.
    """

    model_config = ConfigDict(frozen=True)

    resource_type: str = Field(
        default="Patient", description="FHIR resource type, always 'Patient'"
    )
    resource_id: str = Field(description="FHIR Patient.id")
    full_name: str | None = Field(default=None, description="Preferred human name, rendered")
    birth_date: date | None = Field(default=None, description="Patient.birthDate")
    gender: str | None = Field(default=None, description="Patient.gender")

    @classmethod
    def from_fhir(cls, resource: dict[str, Any]) -> "PatientDemographics":
        """Parse a FHIR R4 ``Patient`` resource dict into typed demographics.

        Args:
            resource: A FHIR ``Patient`` resource as returned by the FHIR API (parsed JSON).

        Returns:
            The typed ``PatientDemographics`` projection.

        Raises:
            ValueError: If ``resource`` is not a ``Patient`` resource or lacks an ``id``.
        """
        resource_id = _require_id(resource, "Patient")
        birth_date_raw = resource.get("birthDate")
        birth_date = date.fromisoformat(birth_date_raw) if isinstance(birth_date_raw, str) else None
        return cls(
            resource_id=resource_id,
            full_name=_render_name(resource.get("name")),
            birth_date=birth_date,
            gender=resource.get("gender"),
        )

    @property
    def citation_identity(self) -> ResourceIdentity:
        """The patient's name — a Patient carries no clinically-relevant 'record date'."""
        return ResourceIdentity(label=self.full_name)


class Problem(BaseModel):
    """Typed projection of a FHIR R4 ``Condition`` — a problem-list entry (UC-1, UC-2, UC-4)."""

    model_config = ConfigDict(frozen=True)

    resource_type: str = Field(
        default="Condition", description="FHIR resource type, always 'Condition'"
    )
    resource_id: str = Field(description="FHIR Condition.id")
    display: str | None = Field(
        default=None, description="Problem name (code.text or coding.display)"
    )
    code: str | None = Field(default=None, description="SNOMED (or other) code; may be absent")
    code_system: str | None = Field(default=None, description="Coding system URI for `code`")
    clinical_status: str | None = Field(default=None, description="active | inactive | resolved")
    onset_date: str | None = Field(
        default=None, description="Onset date (onsetDateTime or period start)"
    )

    @classmethod
    def from_fhir(cls, resource: dict[str, Any]) -> "Problem":
        """Parse a FHIR ``Condition`` resource into a typed problem-list entry.

        Args:
            resource: A FHIR ``Condition`` resource (parsed JSON).

        Returns:
            The typed ``Problem``.

        Raises:
            ValueError: If ``resource`` is not a ``Condition`` or lacks an ``id``.
        """
        resource_id = _require_id(resource, "Condition")
        code, system = _first_coding(resource.get("code"))
        onset = resource.get("onsetDateTime")
        if onset is None and isinstance(resource.get("onsetPeriod"), dict):
            onset = resource["onsetPeriod"].get("start")
        return cls(
            resource_id=resource_id,
            display=_codeable_text(resource.get("code")),
            code=code,
            code_system=system,
            clinical_status=_status_code(resource.get("clinicalStatus")),
            onset_date=onset if isinstance(onset, str) else None,
        )

    @property
    def citation_identity(self) -> ResourceIdentity:
        """The problem name and its onset date — what identifies this problem-list entry."""
        return ResourceIdentity(label=self.display, date=self.onset_date, date_label="Onset")


class Medication(BaseModel):
    """Typed projection of a FHIR R4 ``MedicationRequest`` (UC-1–UC-4).

    ``name`` is always present (``medicationCodeableConcept.text`` or a coding display).
    ``rxnorm_code`` is nullable — list-originated meds lack a structured code and fall back to name
    matching
    (``deployment-strategy.md``). Deduplication across the ``prescriptions``/``lists`` FHIR union is
    applied by :func:`dedup_medications`, not here.
    """

    model_config = ConfigDict(frozen=True)

    resource_type: str = Field(
        default="MedicationRequest", description="FHIR resource type, always 'MedicationRequest'"
    )
    resource_id: str = Field(description="FHIR MedicationRequest.id")
    name: str | None = Field(default=None, description="Drug name (always present when known)")
    rxnorm_code: str | None = Field(
        default=None, description="RxNorm code, or None for text-only meds"
    )
    status: str | None = Field(
        default=None, description="active | stopped | completed | on-hold ..."
    )
    authored_on: str | None = Field(default=None, description="MedicationRequest.authoredOn")

    @classmethod
    def from_fhir(cls, resource: dict[str, Any]) -> "Medication":
        """Parse a FHIR ``MedicationRequest`` resource into a typed medication entry.

        Args:
            resource: A FHIR ``MedicationRequest`` resource (parsed JSON).

        Returns:
            The typed ``Medication``.

        Raises:
            ValueError: If ``resource`` is not a ``MedicationRequest`` or lacks an ``id``.
        """
        resource_id = _require_id(resource, "MedicationRequest")
        concept = resource.get("medicationCodeableConcept")
        code, system = _first_coding(concept)
        # Only treat the code as RxNorm when the coding system says so; otherwise leave it null so
        # the tool falls back to name matching rather than mislabelling an arbitrary code as RxNorm.
        is_rxnorm = isinstance(system, str) and "rxnorm" in system.lower()
        return cls(
            resource_id=resource_id,
            name=_codeable_text(concept),
            rxnorm_code=code if is_rxnorm else None,
            status=resource.get("status") if isinstance(resource.get("status"), str) else None,
            authored_on=resource.get("authoredOn")
            if isinstance(resource.get("authoredOn"), str)
            else None,
        )

    @property
    def citation_identity(self) -> ResourceIdentity:
        """The drug name and when it was authored — what identifies this prescription."""
        return ResourceIdentity(label=self.name, date=self.authored_on, date_label="Authored")


class Allergy(BaseModel):
    """Typed projection of a FHIR R4 ``AllergyIntolerance`` (UC-1, UC-4)."""

    model_config = ConfigDict(frozen=True)

    resource_type: str = Field(
        default="AllergyIntolerance", description="FHIR resource type, always 'AllergyIntolerance'"
    )
    resource_id: str = Field(description="FHIR AllergyIntolerance.id")
    substance: str | None = Field(
        default=None, description="Allergen (code.text or coding.display)"
    )
    criticality: str | None = Field(default=None, description="low | high | unable-to-assess")
    clinical_status: str | None = Field(default=None, description="active | inactive | resolved")
    reactions: str | None = Field(default=None, description="Reaction manifestations, comma-joined")

    @classmethod
    def from_fhir(cls, resource: dict[str, Any]) -> "Allergy":
        """Parse a FHIR ``AllergyIntolerance`` resource into a typed allergy entry.

        Args:
            resource: A FHIR ``AllergyIntolerance`` resource (parsed JSON).

        Returns:
            The typed ``Allergy``.

        Raises:
            ValueError: If ``resource`` is not an ``AllergyIntolerance`` or lacks an ``id``.
        """
        resource_id = _require_id(resource, "AllergyIntolerance")
        return cls(
            resource_id=resource_id,
            substance=_codeable_text(resource.get("code")),
            criticality=resource.get("criticality")
            if isinstance(resource.get("criticality"), str)
            else None,
            clinical_status=_status_code(resource.get("clinicalStatus")),
            reactions=_render_reactions(resource.get("reaction")),
        )

    @property
    def citation_identity(self) -> ResourceIdentity:
        """The allergen substance — an AllergyIntolerance carries no single defining date."""
        return ResourceIdentity(label=self.substance)


class Encounter(BaseModel):
    """Typed projection of a FHIR R4 ``Encounter`` (UC-1, UC-2, UC-3).

    Bounded, structured metadata only — date, type, reason, status. Note *bodies* (free-text
    narrative) are deliberately not read here; that is the separate free-text tool decision
    recorded in ``context/decisions/agent-workflow.md``.
    """

    model_config = ConfigDict(frozen=True)

    resource_type: str = Field(
        default="Encounter", description="FHIR resource type, always 'Encounter'"
    )
    resource_id: str = Field(description="FHIR Encounter.id")
    type: str | None = Field(default=None, description="Encounter type/class display")
    reason: str | None = Field(default=None, description="Reason for the visit, if coded")
    start_date: str | None = Field(default=None, description="Encounter.period.start")
    end_date: str | None = Field(default=None, description="Encounter.period.end")
    status: str | None = Field(default=None, description="planned | in-progress | finished ...")

    @classmethod
    def from_fhir(cls, resource: dict[str, Any]) -> "Encounter":
        """Parse a FHIR ``Encounter`` resource into a typed encounter entry.

        Args:
            resource: A FHIR ``Encounter`` resource (parsed JSON).

        Returns:
            The typed ``Encounter``.

        Raises:
            ValueError: If ``resource`` is not an ``Encounter`` or lacks an ``id``.
        """
        resource_id = _require_id(resource, "Encounter")
        period_raw = resource.get("period")
        period: dict[str, Any] = period_raw if isinstance(period_raw, dict) else {}
        types = resource.get("type")
        type_display = _codeable_text(types[0]) if isinstance(types, list) and types else None
        if type_display is None:
            # Fall back to the encounter class coding (Encounter.class is a single Coding).
            type_display = _codeable_text({"coding": [resource.get("class")]})
        reasons = resource.get("reasonCode")
        reason = _codeable_text(reasons[0]) if isinstance(reasons, list) and reasons else None
        return cls(
            resource_id=resource_id,
            type=type_display,
            reason=reason,
            start_date=period.get("start") if isinstance(period.get("start"), str) else None,
            end_date=period.get("end") if isinstance(period.get("end"), str) else None,
            status=resource.get("status") if isinstance(resource.get("status"), str) else None,
        )

    @property
    def citation_identity(self) -> ResourceIdentity:
        """The visit's real category and its date. OpenEMR hardcodes FHIR ``Encounter.type`` to a
        generic 'Encounter for check up' for every visit, so it cannot identify a specific one; the
        true category ('Emergency room admission', 'General examination') rides in the reason.
        Prefer that, falling back to the type only when no reason is recorded."""
        return ResourceIdentity(
            label=self.reason or self.type, date=self.start_date, date_label="Date"
        )


def _encounter_ref_id(resource: dict[str, Any]) -> str | None:
    """Extract the referenced encounter id from a DocumentReference's ``context.encounter``.

    Args:
        resource: A FHIR ``DocumentReference`` resource.

    Returns:
        The encounter logical id (``"enc-1"`` from ``"Encounter/enc-1"``), or None.
    """
    context = resource.get("context")
    if not isinstance(context, dict):
        return None
    encounters = context.get("encounter")
    if not isinstance(encounters, list) or not encounters:
        return None
    first = encounters[0]
    reference = first.get("reference") if isinstance(first, dict) else None
    if not isinstance(reference, str) or "/" not in reference:
        return None
    return reference.rsplit("/", 1)[-1]


def _decode_note_text(content: Any) -> str | None:
    """Decode the inline plain-text note body from a DocumentReference ``content`` list.

    OpenEMR emits a clinical note as base64-encoded ``text/plain`` in
    ``content[].attachment.data`` (no ``url``/Binary) — verified against the FHIR service code.
    Returns None when no decodable ``text/plain`` attachment is present (e.g. the data-absent
    variant OpenEMR emits when the note body is empty).

    Args:
        content: The ``DocumentReference.content`` value (a list of content dicts), or anything.

    Returns:
        The decoded note text, or None when absent/undecodable.
    """
    if not isinstance(content, list):
        return None
    for item in content:
        if not isinstance(item, dict):
            continue
        attachment = item.get("attachment")
        if not isinstance(attachment, dict) or attachment.get("contentType") != "text/plain":
            continue
        data = attachment.get("data")
        if not isinstance(data, str) or not data:
            continue
        try:
            return base64.b64decode(data, validate=True).decode("utf-8", errors="replace")
        except ValueError:
            # binascii.Error (malformed base64) subclasses ValueError.
            return None
    return None


class NoteContent(BaseModel):
    """Typed projection of a FHIR R4 ``DocumentReference`` clinical note — free text (UC-3).

    The one tool that reads *narrative* rather than coded fields: ``text`` is the decoded note body.
    A claim citing a note must carry a verbatim ``quote`` that the grounding gate checks is a
    substring of this text (ARCHITECTURE.md §7) — the same deterministic guarantee as structured
    fields, applied to prose.
    """

    model_config = ConfigDict(frozen=True)

    resource_type: str = Field(
        default="DocumentReference", description="FHIR resource type, always 'DocumentReference'"
    )
    resource_id: str = Field(description="FHIR DocumentReference.id")
    encounter_id: str | None = Field(default=None, description="Encounter this note belongs to")
    date: str | None = Field(default=None, description="DocumentReference.date")
    type_display: str | None = Field(default=None, description="Note type (LOINC display)")
    status: str | None = Field(default=None, description="current | entered-in-error ...")
    text: str | None = Field(default=None, description="Decoded free-text note body")

    @classmethod
    def from_fhir(cls, resource: dict[str, Any]) -> "NoteContent":
        """Parse a FHIR ``DocumentReference`` clinical note into a typed value.

        Args:
            resource: A FHIR ``DocumentReference`` resource (parsed JSON).

        Returns:
            The typed ``NoteContent``.

        Raises:
            ValueError: If ``resource`` is not a ``DocumentReference`` or lacks an ``id``.
        """
        resource_id = _require_id(resource, "DocumentReference")
        return cls(
            resource_id=resource_id,
            encounter_id=_encounter_ref_id(resource),
            date=resource.get("date") if isinstance(resource.get("date"), str) else None,
            type_display=_codeable_text(resource.get("type")),
            status=resource.get("status") if isinstance(resource.get("status"), str) else None,
            text=_decode_note_text(resource.get("content")),
        )

    @property
    def citation_identity(self) -> ResourceIdentity:
        """The note type and date. The quote grounds the claim; this just names which note it is."""
        return ResourceIdentity(label=self.type_display, date=self.date, date_label="Date")


def _uploaded_document_attachment(resource: dict[str, Any]) -> dict[str, Any] | None:
    """Return a DocumentReference's binary attachment (PDF/Binary), or None for a text note.

    An uploaded lab document carries an ``application/pdf`` attachment (or a ``url``/Binary
    reference); an OpenEMR clinical note instead carries inline ``text/plain`` (read by
    ``get_encounter_note``). This is what separates the two DocumentReference kinds without relying
    on the exact OpenEMR category token.

    Args:
        resource: A FHIR ``DocumentReference`` resource.

    Returns:
        The binary attachment dict, or None when the resource is a text note / has no binary data.
    """
    content = resource.get("content")
    if not isinstance(content, list):
        return None
    for item in content:
        attachment = item.get("attachment") if isinstance(item, dict) else None
        if not isinstance(attachment, dict):
            continue
        content_type = attachment.get("contentType")
        has_url = isinstance(attachment.get("url"), str) and attachment.get("url")
        if content_type == "application/pdf" or (content_type != "text/plain" and has_url):
            return attachment
    return None


def _is_lab_document(resource: dict[str, Any]) -> bool:
    """Whether a DocumentReference is a lab report, by its category (or type) naming a lab.

    OpenEMR maps the uploaded document's category to ``DocumentReference.category`` (a list of
    CodeableConcept); a lab report carries the text ``"Lab Report"``. ``DocumentReference.type`` is
    typically the ``UNK`` NullFlavor for uploaded files (verified against live FHIR), so category is
    the reliable signal, with type as a fallback. Matched case-insensitively on any category
    text/coding so ``"Laboratory"``/``"Labs"`` variants also pass — and, critically, a non-lab
    uploaded PDF (a referral, intake form, or insurance card) is excluded so it is not OCR'd through
    the lab schema.

    Args:
        resource: A FHIR ``DocumentReference`` resource.

    Returns:
        True when a category (or type) CodeableConcept names a lab document.
    """
    categories = resource.get("category")
    concepts: list[Any] = list(categories) if isinstance(categories, list) else []
    concepts.append(resource.get("type"))
    for concept in concepts:
        if not isinstance(concept, dict):
            continue
        text = concept.get("text")
        if isinstance(text, str) and "lab" in text.lower():
            return True
        codings = concept.get("coding")
        if not isinstance(codings, list):
            continue
        for coding in codings:
            if not isinstance(coding, dict):
                continue
            for field in ("display", "code"):
                value = coding.get(field)
                if isinstance(value, str) and "lab" in value.lower():
                    return True
    return False


class LabDocumentSummary(BaseModel):
    """A discovered uploaded lab document (a ``DocumentReference`` categorized as a lab report).

    Returned by ``get_lab_documents`` so the intake-extractor can find the id of a patient's
    uploaded lab report and hand it to ``attach_and_extract``. A document qualifies only when it has
    a binary (PDF/Binary) attachment AND its category names a lab — so a non-lab uploaded PDF is not
    listed and OCR'd through the lab schema. This is metadata only — the document is OCR'd from its
    bytes, not read from FHIR (the SMART token cannot read the Binary; see the seam spec), so no
    ``citation_identity`` is needed here.
    """

    model_config = ConfigDict(frozen=True)

    resource_type: str = Field(
        default="DocumentReference", description="FHIR resource type, always 'DocumentReference'"
    )
    resource_id: str = Field(description="FHIR DocumentReference.id — pass to attach_and_extract")
    title: str | None = Field(default=None, description="Document title/filename, if any")
    date: str | None = Field(default=None, description="DocumentReference.date")

    @classmethod
    def try_from_fhir(cls, resource: dict[str, Any]) -> "LabDocumentSummary | None":
        """Parse a ``DocumentReference`` into a summary, or None if it is not an uploaded document.

        Args:
            resource: A FHIR ``DocumentReference`` resource (parsed JSON).

        Returns:
            The typed summary for an uploaded lab (PDF/Binary + lab category) document, or None for
            a text note, a non-lab uploaded document, or a resource lacking a logical id.
        """
        resource_id = resource.get("id")
        if not isinstance(resource_id, str) or not resource_id:
            return None
        attachment = _uploaded_document_attachment(resource)
        if attachment is None:
            return None
        if not _is_lab_document(resource):
            return None
        title = attachment.get("title")
        if not (isinstance(title, str) and title.strip()):
            title = _codeable_text(resource.get("type")) or resource.get("description")
        return cls(
            resource_id=resource_id,
            title=title if isinstance(title, str) and title.strip() else None,
            date=resource.get("date") if isinstance(resource.get("date"), str) else None,
        )


def dedup_medications(medications: list[Medication]) -> list[Medication]:
    """Collapse duplicate meds from the FHIR prescriptions/lists UNION (deployment-strategy.md).

    A med recorded in both tables without the internal link surfaces as two ``MedicationRequest``
    resources — one RxNorm-coded, one text-only. We key on the drug *name* (the list branch has no
    code to match on, per the audit) and keep the coded variant when present so downstream
    cross-referencing (UC-4) has the RxNorm code where one exists.

    Args:
        medications: Parsed medications, possibly containing name-duplicates.

    Returns:
        Deduplicated medications, preserving first-seen order and preferring coded entries.
    """
    by_name: dict[str, Medication] = {}
    order: list[str] = []
    for med in medications:
        # Meds without a name can't be de-duplicated safely — keep each as-is under its unique id.
        key = med.name.strip().lower() if med.name else f"\0{med.resource_id}"
        existing = by_name.get(key)
        if existing is None:
            by_name[key] = med
            order.append(key)
        elif existing.rxnorm_code is None and med.rxnorm_code is not None:
            # Prefer the coded variant over the text-only one for the same drug.
            by_name[key] = med
    return [by_name[key] for key in order]


def _render_reactions(reactions: Any) -> str | None:
    """Comma-join the manifestation displays across a FHIR ``AllergyIntolerance.reaction`` list.

    Args:
        reactions: The ``reaction`` value (a list of reaction dicts), or anything.

    Returns:
        A comma-joined manifestation string, or None when none is present.
    """
    if not isinstance(reactions, list):
        return None
    labels: list[str] = []
    for reaction in reactions:
        if not isinstance(reaction, dict):
            continue
        manifestations = reaction.get("manifestation")
        if not isinstance(manifestations, list):
            continue
        for manifestation in manifestations:
            label = _codeable_text(manifestation)
            if label:
                labels.append(label)
    return ", ".join(labels) if labels else None


def _render_name(names: Any) -> str | None:
    """Render a FHIR ``HumanName`` list into a single display string.

    Prefers an ``official`` use, then the first entry. Returns None when no usable name is
    present — a data-quality gap the agent must state plainly rather than paper over.

    Args:
        names: The ``Patient.name`` value (a list of ``HumanName`` dicts, or anything).

    Returns:
        A rendered name, or None if none is available.
    """
    if not isinstance(names, list) or not names:
        return None

    chosen = next((n for n in names if isinstance(n, dict) and n.get("use") == "official"), None)
    chosen = chosen or next((n for n in names if isinstance(n, dict)), None)
    if chosen is None:
        return None

    text = chosen.get("text")
    if isinstance(text, str) and text.strip():
        return text.strip()

    given = chosen.get("given")
    given_part = " ".join(g for g in given if isinstance(g, str)) if isinstance(given, list) else ""
    family = chosen.get("family") if isinstance(chosen.get("family"), str) else ""
    rendered = f"{given_part} {family}".strip()
    return rendered or None
