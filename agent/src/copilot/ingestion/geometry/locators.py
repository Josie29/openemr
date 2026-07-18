import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol

from copilot.ingestion.geometry.boxes import (
    BoxPrecision,
    LocatedBox,
    LocateOutcome,
    LocateResult,
    LocatorName,
)
from copilot.ingestion.geometry.document import DocumentGeometry
from copilot.ingestion.geometry.spans import first_token, match_span, merge_and_pad, norm
from copilot.ingestion.geometry.words import Checkbox, Word
from copilot.ingestion.schemas import BoundingBox, BoxEvidence

logger = logging.getLogger("copilot")

# Two words belong to the same visual row when their top edges are within this many points.
_ROW_TOLERANCE = 3.0
# How far below a section heading its content may run before the next heading is assumed.
_SECTION_MAX_DEPTH = 130.0


class Direction(StrEnum):
    """Where a form prints a value relative to its label.

    Not cosmetic: one committed fixture stacks the value BELOW the label (left-aligned under it),
    another prints it to the RIGHT on the same line. A locator that assumed either would extract
    nothing from the other.
    """

    RIGHT = "right"
    BELOW = "below"


@dataclass(frozen=True, slots=True)
class LocateRequest:
    """One fact to place on the page: what to box, and what anchors it."""

    value: str
    anchors: tuple[str, ...]  # label/test-name wordings that may introduce the value
    ordinal: int = 0  # position among sibling facts, for band fallbacks
    total: int = 1


@dataclass
class LocatorState:
    """Per-document, per-mapping-run cursors — the only mutable object in the geometry layer.

    Keyed rather than global: a lab table is walked strictly top-to-bottom by one cursor, but an
    intake form's fields are located out of document order, so one shared cursor would strand every
    field printed above the previous one. Each locator advances only its own key, forward-only.

    Owned by the mapper, not by :class:`DocumentGeometry` — the geometry is frozen and reusable, the
    walk over it is not. That split is what lets one document be mapped twice without contamination.
    """

    _cursors: dict[str, int] = field(default_factory=dict)

    def cursor(self, key: str) -> int:
        """The current index for ``key``, or 0 when it has not been walked yet."""
        return self._cursors.get(key, 0)

    def advance(self, key: str, index: int) -> None:
        """Move ``key``'s cursor past ``index``. Forward-only; never rewinds."""
        self._cursors[key] = max(self._cursors.get(key, 0), index)


class ValueLocator(Protocol):
    """One strategy for placing a value on the page.

    Locators are ordered into a :class:`LocatorChain` per field and tried until one returns a box,
    so a document type is never hardcoded into the geometry: a lab table, a label:value form, and a
    checkbox grid are three locators over the same evidence, not three code paths.

    Implementations share the span matcher in ``spans`` rather than each re-deriving "find the
    words, merge them, pad the box".
    """

    @property
    def name(self) -> LocatorName:
        """Which strategy this is; stamped on the box for traces."""
        ...

    def locate(
        self, request: LocateRequest, doc: DocumentGeometry, state: LocatorState
    ) -> LocateResult:
        """Place ``request.value`` on the page, defer to the next locator, or refute it.

        Args:
            request: The fact to box, its anchors, and its ordinal among siblings.
            doc: The document's normalized geometry (PDF points throughout).
            state: Per-document cursors; advanced on a hit.

        Returns:
            LOCATED with a box, NOT_APPLICABLE to defer to the next locator, or REFUTED to stop the
            chain and drop the fact.
        """
        ...


@dataclass(frozen=True, slots=True)
class RowSpanLocator:
    """Boxes a value on the text-layer row its anchor introduces — the lab-table join.

    The anchor (a test name) is the row's left-most token, so a value repeated elsewhere on the page
    — in the "prior result" column, or in the interpretive narrative below the table — cannot be
    mistaken for the row's own result. ``anchor_region`` bounds where an anchor may start, which is
    what excludes those narrative mentions; it is per-instance, not a module constant, because the
    left-hand-column assumption is a property of *this layout*, not of geometry.
    """

    anchor_region: tuple[float, float] = (0.0, 200.0)
    row_tolerance: float = _ROW_TOLERANCE
    max_span_words: int = 1
    # Cursors are keyed so independent walks do not strand each other (see LocatorState). Two fields
    # read from the SAME table — a medication's dose and its frequency — are two such walks: sharing
    # one key would leave the second scanning from past the row the first consumed. Defaults to the
    # locator name, so the single-field lab walk is unchanged.
    cursor_key: str | None = None

    @property
    def name(self) -> LocatorName:
        return LocatorName.ROW_SPAN

    @property
    def _key(self) -> str:
        """The cursor key this instance walks."""
        return self.cursor_key or self.name.value

    def locate(
        self, request: LocateRequest, doc: DocumentGeometry, state: LocatorState
    ) -> LocateResult:
        """Scan forward for the anchor, then box the value on that row."""
        anchor_key = first_token(request.anchors[0]) if request.anchors else ""
        if not anchor_key or not norm(request.value) or not doc.words:
            return LocateResult.not_applicable()
        left, right = self.anchor_region
        cursor = state.cursor(self._key)
        for index in range(cursor, len(doc.words)):
            anchor = doc.words[index]
            if not (left <= anchor.x0 <= right) or norm(anchor.text) != anchor_key:
                continue
            row = _row_after(doc, anchor, self.row_tolerance)
            span = match_span(row, request.value, self.max_span_words)
            if span is None:
                continue  # anchor matched but its value is not on this row — keep scanning
            state.advance(self._key, index + 1)
            return LocateResult.located_at(
                LocatedBox(
                    box=merge_and_pad(span),
                    precision=BoxPrecision.EXACT,
                    evidence=BoxEvidence.PRINTED_VALUE,
                    locator=self.name,
                )
            )
        return LocateResult.not_applicable()


@dataclass(frozen=True, slots=True)
class TableRowBandLocator:
    """Bands the OCR table block by row count to place a value when the text layer cannot.

    The scanned-document fallback: with no text layer there are no word boxes to match, but the OCR
    still reports the table block and its rows, so the value's row can be placed even though its
    column cannot. Coarse by construction — a full-width band — hence ``ROW_BAND``.
    """

    @property
    def name(self) -> LocatorName:
        return LocatorName.TABLE_ROW_BAND

    def locate(
        self, request: LocateRequest, doc: DocumentGeometry, state: LocatorState
    ) -> LocateResult:
        """Place the value's row within the table block, by name if matched, else by ordinal."""
        table = doc.table_on(_first_page(doc))
        if table is None or table.box is None:
            return LocateResult.not_applicable()
        total_rows = len(table.rows) or 1
        anchor = request.anchors[0] if request.anchors else ""
        row_index = table.row_index_of(anchor)
        if row_index is None:
            # Name not matched (the annotation reworded it): estimate by ordinal across the table.
            denom = request.total or 1
            row_index = min(total_rows - 1, round((request.ordinal + 0.5) / denom * total_rows))
        row_height = table.box.height / total_rows
        return LocateResult.located_at(
            LocatedBox(
                box=BoundingBox(
                    page=table.box.page,
                    x=table.box.x,
                    y=table.box.y + row_index * row_height,
                    width=max(table.box.width, 1.0),
                    height=max(row_height, 1.0),
                ),
                precision=BoxPrecision.ROW_BAND,
                evidence=BoxEvidence.PRINTED_VALUE,
                locator=self.name,
            )
        )


@dataclass(frozen=True, slots=True)
class PageBoxLocator:
    """Boxes the whole page — the last resort, labelled honestly as ``PAGE``.

    Kept because dropping a readable fact for want of geometry is worse than citing it without a
    usable highlight; a precision floor is what decides which document types will accept it.
    """

    @property
    def name(self) -> LocatorName:
        return LocatorName.PAGE_BOX

    def locate(
        self, request: LocateRequest, doc: DocumentGeometry, state: LocatorState
    ) -> LocateResult:
        """Return the page's own box, or defer when the page has no usable dimensions."""
        dims = doc.page(_first_page(doc))
        if dims is None:
            return LocateResult.not_applicable()
        return LocateResult.located_at(
            LocatedBox(
                box=dims.box,
                precision=BoxPrecision.PAGE,
                evidence=BoxEvidence.PRINTED_VALUE,
                locator=self.name,
            )
        )


@dataclass(frozen=True, slots=True)
class LabelSpanLocator:
    """Boxes a value introduced by a printed label — the label:value idiom.

    ``directions`` is ordered and both are needed: forms disagree about where the answer goes. One
    prints the value to the RIGHT of its label on the same line; another stacks it BELOW,
    left-aligned under the label. Trying right-then-below lets one field spec serve both without
    either layout being named.

    Multi-word by design: an address is seven tokens, a date five ("03 / 14 / 1979"), so the value's
    words are merged into one box.
    """

    directions: tuple[Direction, ...] = (Direction.RIGHT, Direction.BELOW)
    max_gap: float = 26.0
    row_tolerance: float = _ROW_TOLERANCE
    max_span_words: int = 14

    @property
    def name(self) -> LocatorName:
        return LocatorName.LABEL_SPAN

    def locate(
        self, request: LocateRequest, doc: DocumentGeometry, state: LocatorState
    ) -> LocateResult:
        """Find a label the field is known by, then box the value beside or beneath it."""
        if not doc.words or not norm(request.value):
            return LocateResult.not_applicable()
        for anchor_span in _label_spans(doc, request.anchors):
            for direction in self.directions:
                candidates = _words_near(
                    doc, anchor_span, direction, self.max_gap, self.row_tolerance
                )
                span = match_span(candidates, request.value, self.max_span_words)
                if span is not None:
                    return LocateResult.located_at(
                        LocatedBox(
                            box=merge_and_pad(span),
                            precision=BoxPrecision.EXACT,
                            evidence=BoxEvidence.PRINTED_VALUE,
                            locator=self.name,
                        )
                    )
        return LocateResult.not_applicable()


@dataclass(frozen=True, slots=True)
class SectionSpanLocator:
    """Boxes a value anywhere under a section heading — the free-text/list idiom.

    Some forms drop the per-field labels entirely and print a heading ("Current Medications") over
    a list of lines, or a paragraph. There is no label to anchor on, so the heading scopes the
    search instead: everything between it and the next heading is fair game, which is narrow enough
    to keep a value from matching an identical string elsewhere on the page.
    """

    max_span_words: int = 24

    @property
    def name(self) -> LocatorName:
        return LocatorName.SECTION_SPAN

    def locate(
        self, request: LocateRequest, doc: DocumentGeometry, state: LocatorState
    ) -> LocateResult:
        """Find the field's section heading, then box the value within that section."""
        if not doc.words or not norm(request.value):
            return LocateResult.not_applicable()
        for anchor_span in _label_spans(doc, request.anchors):
            candidates = _words_below(doc, anchor_span, _SECTION_MAX_DEPTH)
            span = match_span(candidates, request.value, self.max_span_words)
            if span is not None:
                return LocateResult.located_at(
                    LocatedBox(
                        box=merge_and_pad(span),
                        precision=BoxPrecision.EXACT,
                        evidence=BoxEvidence.PRINTED_VALUE,
                        locator=self.name,
                    )
                )
        return LocateResult.not_applicable()


@dataclass(frozen=True, slots=True)
class CheckboxLocator:
    """Boxes a ticked option — and REFUTES an unticked one.

    The only locator that can say NO, and the reason the chain needs a third outcome. Every other
    locator answers "where is this text?"; this one answers "did the patient actually assert this?"
    — a different question, because the option's text is preprinted either way.

    Three outcomes, each load-bearing:
      - the value names a TICKED option  -> LOCATED, boxed over the mark AND its label, so a
        physician clicking through sees the tick that proves it.
      - the value names an UNTICKED option -> REFUTED. Not "no box": a refusal, which stops the
        chain. Falling through would let a coarser locator box the preprinted text and hand back a
        citation for a fact the form denies.
      - the value names no option here (or the form has no boxes at all, as a linear one does)
        -> NOT_APPLICABLE, deferring to the text locators.
    """

    max_label_gap: float = 95.0
    row_tolerance: float = 6.0
    max_span_words: int = 8

    @property
    def name(self) -> LocatorName:
        return LocatorName.CHECKBOX

    def locate(
        self, request: LocateRequest, doc: DocumentGeometry, state: LocatorState
    ) -> LocateResult:
        """Match the value against each box's option label; the mark decides the outcome."""
        if not doc.checkboxes_available:
            # The detector could not run, so we cannot tell whether this form ticks its options.
            # Deferring here is what let the chain text-match the PREPRINTED option and box it.
            return LocateResult.undetermined("checkbox evidence unavailable for this document")
        if not doc.checkboxes or not norm(request.value):
            return LocateResult.not_applicable()
        for checkbox in doc.checkboxes:
            label = _checkbox_label_words(doc, checkbox, self.max_label_gap, self.row_tolerance)
            span = match_span(label, request.value, self.max_span_words)
            if span is None:
                continue
            if not checkbox.ticked:
                return LocateResult.refuted(
                    f"'{request.value}' is printed on the form but its box is not ticked"
                )
            return LocateResult.located_at(
                LocatedBox(
                    box=merge_and_pad((_checkbox_as_word(checkbox), *span)),
                    precision=BoxPrecision.EXACT,
                    evidence=BoxEvidence.CHECKED_MARK,
                    locator=self.name,
                )
            )
        return LocateResult.not_applicable()


@dataclass(frozen=True, slots=True)
class LineBandLocator:
    """Bands the text line a value sits on — the coarse fallback for a form.

    A form has no table to band, so when the value cannot be pinned exactly this places its line:
    right vertically, coarse horizontally. Exists so a scanned or awkwardly-typeset form degrades
    to an approximate highlight instead of dropping every fact, which a strict precision floor
    would otherwise do.
    """

    @property
    def name(self) -> LocatorName:
        return LocatorName.LINE_BAND

    def locate(
        self, request: LocateRequest, doc: DocumentGeometry, state: LocatorState
    ) -> LocateResult:
        """Band the line holding the value's first word, or defer when it is not on the page."""
        span = match_span(doc.words, request.value, max_span_words=24)
        if span is None:
            return LocateResult.not_applicable()
        anchor = span[0]
        line = [
            word
            for word in doc.words
            if word.page == anchor.page and abs(word.top - anchor.top) <= _ROW_TOLERANCE
        ]
        return LocateResult.located_at(
            LocatedBox(
                box=merge_and_pad(line or list(span)),
                precision=BoxPrecision.LINE_BAND,
                evidence=BoxEvidence.PRINTED_VALUE,
                locator=self.name,
            )
        )


@dataclass(frozen=True, slots=True)
class LocatorChain:
    """An ordered set of locators, tried until one places the value or one refutes it."""

    locators: tuple[ValueLocator, ...]

    def locate(
        self, request: LocateRequest, doc: DocumentGeometry, state: LocatorState
    ) -> LocatedBox | None:
        """Run the chain.

        Args:
            request: The fact to box.
            doc: The document's normalized geometry.
            state: Per-document cursors.

        Returns:
            The first box produced, or None when no locator applies **or** one refuted the value.
            A refusal stops the chain: it means the page contradicts the fact, so falling through
            to a coarser locator would box something that does not support it.
        """
        for locator in self.locators:
            result = locator.locate(request, doc, state)
            match result.outcome:
                case LocateOutcome.LOCATED:
                    return result.located
                case LocateOutcome.REFUTED:
                    logger.warning(
                        "locator refuted a value; dropping the fact",
                        extra={"locator": locator.name.value, "reason": result.reason},
                    )
                    return None
                case LocateOutcome.UNDETERMINED:
                    logger.warning(
                        "locator could not judge a value; dropping the fact rather than "
                        "falling through to a coarser box",
                        extra={"locator": locator.name.value, "reason": result.reason},
                    )
                    return None
                case LocateOutcome.NOT_APPLICABLE:
                    continue
        return None


def _first_page(doc: DocumentGeometry) -> int:
    """The 1-based number of the document's first page.

    Lab extraction has always read page one only; multi-page support is tracked separately
    (JOS-72), so this keeps the existing scope explicit rather than silently implied.
    """
    return doc.pages[0].page if doc.pages else 1


def _label_spans(doc: DocumentGeometry, anchors: tuple[str, ...]) -> list[tuple[Word, ...]]:
    """Every place one of ``anchors`` is printed, as the words spelling it out.

    Anchors are matched as spans, not tokens, because a label is usually several words ("Patient
    Name (Last, First)"). All matches are returned, in reading order, so a caller can try the next
    one when a label appears more than once.

    Args:
        doc: The document's geometry.
        anchors: The wordings this field may be introduced by.

    Returns:
        The matching label spans, in reading order.
    """
    found: list[tuple[Word, ...]] = []
    for anchor in anchors:
        target = norm(anchor)
        if not target:
            continue
        width = len(target.split())
        for start in range(len(doc.words)):
            span = tuple(doc.words[start : start + width])
            if len(span) < width or span[-1].page != span[0].page:
                continue
            if norm(" ".join(word.text for word in span)) == target:
                found.append(span)
    return sorted(found, key=lambda span: (span[0].page, span[0].top, span[0].x0))


def _words_near(
    doc: DocumentGeometry,
    label: tuple[Word, ...],
    direction: Direction,
    max_gap: float,
    row_tolerance: float,
) -> list[Word]:
    """The words a label's value could be made of, in the given direction.

    Args:
        doc: The document's geometry.
        label: The label's words.
        direction: RIGHT (same line, after the label) or BELOW (next line, x-aligned under it).
        max_gap: How far the value may sit from the label, in points.
        row_tolerance: How far two words' tops may differ and still share a line.

    Returns:
        The candidate words in reading order.
    """
    page = label[0].page
    right = max(word.x1 for word in label)
    top = min(word.top for word in label)
    bottom = max(word.bottom for word in label)
    # The region is scoped generously and `match_span` does the disambiguating. Narrowing per word
    # (say, "within max_gap of the label's left edge") would truncate a multi-word value: the second
    # word of a name already sits ~38pt out, so the span would be cut to its first word.
    match direction:
        case Direction.RIGHT:
            candidates = [
                word
                for word in doc.words
                if word.page == page
                and abs(word.top - top) <= row_tolerance
                and word.x0 >= right
            ]
            return sorted(candidates, key=lambda word: word.x0)
        case Direction.BELOW:
            candidates = [
                word for word in doc.words if word.page == page and 0 < word.top - bottom <= max_gap
            ]
            return sorted(candidates, key=lambda word: (word.top, word.x0))


def _words_below(doc: DocumentGeometry, heading: tuple[Word, ...], depth: float) -> list[Word]:
    """The words in the region a section heading introduces, in reading order."""
    page = heading[0].page
    bottom = max(word.bottom for word in heading)
    candidates = [
        word for word in doc.words if word.page == page and 0 < word.top - bottom <= depth
    ]
    return sorted(candidates, key=lambda word: (word.top, word.x0))


def _checkbox_as_word(checkbox: Checkbox) -> Word:
    """Adapt a checkbox to a ``Word`` so it can be merged into the box it helps prove."""
    return Word(
        text="",
        x0=checkbox.x0,
        top=checkbox.top,
        x1=checkbox.x1,
        bottom=checkbox.bottom,
        page=checkbox.page,
    )


def _checkbox_label_words(
    doc: DocumentGeometry, checkbox: Checkbox, max_gap: float, row_tolerance: float
) -> list[Word]:
    """The option text printed beside a checkbox, in reading order.

    Words overlapping the box are included, not just those clear of it: a tick drawn tight against
    its label is merged into one token by the text layer ("✕Male"), so requiring the label to start
    after the box would lose exactly the ticked options. Leading mark glyphs are stripped from the
    text so such a token still matches the plain option name.

    Args:
        doc: The document's geometry.
        checkbox: The box whose label is wanted.
        max_gap: How far right of the box the label may run, in points.
        row_tolerance: Vertical slack when deciding the label shares the box's line.

    Returns:
        The label's candidate words, left to right.
    """
    centre = (checkbox.top + checkbox.bottom) / 2
    candidates = [
        word
        for word in doc.words
        if word.page == checkbox.page
        and abs((word.top + word.bottom) / 2 - centre) <= row_tolerance
        and word.x1 > checkbox.x0
        and word.x0 - checkbox.x1 <= max_gap
    ]
    return [_strip_marks(word) for word in sorted(candidates, key=lambda word: word.x0)]


def _strip_marks(word: Word) -> Word:
    """Drop leading tick glyphs from a word the text layer merged with its checkbox."""
    text = word.text.lstrip("✕✗✘☒✓✔")
    return word if text == word.text else Word(
        text=text, x0=word.x0, top=word.top, x1=word.x1, bottom=word.bottom, page=word.page
    )


def _row_after(doc: DocumentGeometry, anchor: Word, tolerance: float) -> list[Word]:
    """Words on the anchor's row and to its right, ordered left-to-right.

    Sorted by x rather than taken in reading order: reading order sorts by top then x, so within the
    row tolerance two words can come back in an order that is not left-to-right. The result column
    precedes the "prior result" column, so left-most-first is what makes a value that equals its own
    prior draw still box the current result.

    Args:
        doc: The document's geometry.
        anchor: The word introducing the row.
        tolerance: How far two words' top edges may differ and still share a row, in points.

    Returns:
        The row's words right of the anchor, left-to-right.
    """
    return sorted(
        (
            word
            for word in doc.words
            if word.page == anchor.page
            and abs(word.top - anchor.top) <= tolerance
            and word.x0 > anchor.x1
        ),
        key=lambda word: word.x0,
    )
