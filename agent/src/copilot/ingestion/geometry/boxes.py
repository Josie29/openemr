from dataclasses import dataclass
from enum import StrEnum

# BoxEvidence lives beside BoundingBox in schemas because it travels with a box onto Citation.
# Re-exported here (and via geometry/__init__) so locators keep importing it from the geometry
# package — the dependency runs geometry -> schemas, never the reverse.
from copilot.ingestion.schemas import BoundingBox, BoxEvidence


class BoxPrecision(StrEnum):
    """How tightly a located box frames the value it cites.

    "Has a box" is not the same as "is click-to-source". A whole-page rectangle is technically a
    box, but it tells the physician nothing — so precision is declared, and each document type sets
    a floor below which a fact is dropped rather than shipped with a useless highlight.
    """

    EXACT = "exact"  # the value's own merged word span, from the PDF text layer
    ROW_BAND = "row_band"  # the right row of a table, but its full width
    LINE_BAND = "line_band"  # the right text line, but coarse horizontally
    PAGE = "page"  # "somewhere on this page"

    @property
    def rank(self) -> int:
        """Ordinal for floor comparison; higher is tighter."""
        match self:
            case BoxPrecision.PAGE:
                return 0
            case BoxPrecision.LINE_BAND:
                return 1
            case BoxPrecision.ROW_BAND:
                return 2
            case BoxPrecision.EXACT:
                return 3

    def meets(self, floor: "BoxPrecision") -> bool:
        """Whether this precision is at least as tight as ``floor``.

        Args:
            floor: The minimum precision the document type accepts.

        Returns:
            True when this precision is tight enough to ship.
        """
        return self.rank >= floor.rank


class LocatorName(StrEnum):
    """Which strategy produced a box. Recorded on the box and logged, for traces and debugging."""

    ROW_SPAN = "row_span"
    LABEL_SPAN = "label_span"
    TABLE_CELL = "table_cell"
    SECTION_SPAN = "section_span"
    CHECKBOX = "checkbox"
    TABLE_ROW_BAND = "table_row_band"
    LINE_BAND = "line_band"
    PAGE_BOX = "page_box"


class LocateOutcome(StrEnum):
    """What a locator can conclude — three of these are "no box", and they must not behave alike.

    ``NOT_APPLICABLE`` means "this is not my layout, ask the next locator". ``REFUTED`` means "I own
    this field and the page says NO". ``UNDETERMINED`` means "I own this field and cannot tell" —
    the evidence a verdict needs was unavailable (e.g. the checkbox detector could not run). Only
    ``NOT_APPLICABLE`` may fall through: letting a refusal or a non-verdict continue to a coarser
    locator gets the preprinted option text-matched and boxed, laundering the very claim the
    non-answer existed to stop.
    """

    LOCATED = "located"
    NOT_APPLICABLE = "not_applicable"
    REFUTED = "refuted"
    UNDETERMINED = "undetermined"


@dataclass(frozen=True, slots=True)
class LocatedBox:
    """A box on the source page, with everything the caller needs to judge whether to trust it."""

    box: BoundingBox
    precision: BoxPrecision
    evidence: BoxEvidence
    locator: LocatorName


@dataclass(frozen=True, slots=True)
class LocateResult:
    """One locator's conclusion: a box, a deferral to the next locator, or a refusal."""

    outcome: LocateOutcome
    located: LocatedBox | None = None
    reason: str | None = None

    @classmethod
    def located_at(cls, located: LocatedBox) -> "LocateResult":
        """The value was found and boxed."""
        return cls(outcome=LocateOutcome.LOCATED, located=located)

    @classmethod
    def not_applicable(cls) -> "LocateResult":
        """This locator does not handle this layout — the chain should try the next one."""
        return cls(outcome=LocateOutcome.NOT_APPLICABLE)

    @classmethod
    def refuted(cls, reason: str) -> "LocateResult":
        """This locator owns the field and the page contradicts the value — stop the chain.

        Args:
            reason: Why the page refutes it, for the drop log.
        """
        return cls(outcome=LocateOutcome.REFUTED, reason=reason)

    @classmethod
    def undetermined(cls, reason: str) -> "LocateResult":
        """This locator owns the field but the evidence to judge it was unavailable — stop.

        Args:
            reason: Why no verdict was possible, for the drop log.
        """
        return cls(outcome=LocateOutcome.UNDETERMINED, reason=reason)
