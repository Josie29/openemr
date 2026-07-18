from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from copilot.ingestion.geometry.boxes import BoxPrecision
from copilot.ingestion.geometry.locators import (
    CheckboxLocator,
    LabelSpanLocator,
    LineBandLocator,
    LocatorChain,
    PageBoxLocator,
    RowSpanLocator,
    SectionSpanLocator,
    TableRowBandLocator,
    ValueLocator,
)
from copilot.ingestion.schemas import DocType


class FieldId(StrEnum):
    """Semantic identity of an extracted field — the key a locator chain is bound to.

    A StrEnum rather than a bare path string so a typo is a NameError at import, not a chain that
    silently never fires. The value doubles as the citation's ``field_or_chunk_id``, which is the
    one place the path is genuinely a wire value.
    """

    LAB_RESULT_VALUE = "lab.result.value"
    LAB_RESULT_TEST_NAME = "lab.result.test_name"
    LAB_RESULT_LOINC = "lab.result.loinc"
    LAB_RESULT_UNIT = "lab.result.unit"
    LAB_RESULT_REFERENCE_RANGE = "lab.result.reference_range"
    DEMOGRAPHICS_FULL_NAME = "demographics.full_name"
    DEMOGRAPHICS_DATE_OF_BIRTH = "demographics.date_of_birth"
    DEMOGRAPHICS_SEX = "demographics.sex"
    DEMOGRAPHICS_ADDRESS = "demographics.address"
    DEMOGRAPHICS_PHONE = "demographics.phone"
    CHIEF_CONCERN = "chief_concern"
    CURRENT_MEDICATIONS = "current_medications[]"
    CURRENT_MEDICATIONS_DOSE = "current_medications[].dose"
    CURRENT_MEDICATIONS_FREQUENCY = "current_medications[].frequency"
    ALLERGIES = "allergies[]"
    ALLERGIES_REACTION = "allergies[].reaction"
    FAMILY_HISTORY = "family_history[]"
    FAMILY_HISTORY_RELATION = "family_history[].relation"


@dataclass(frozen=True, slots=True)
class FieldSpec:
    """How one semantic field is found on a page, and how good its box must be to ship.

    ``labels`` is the seam that makes a new form layout **data, not code**: the same field is
    introduced by different wording on different forms, so a spec collects every known wording and
    matching is normalized (case- and punctuation-insensitive). Supporting another form should mean
    adding an alias here; only a genuinely new *idiom* warrants a new locator.

    The wordings are an ordered tuple purely so the value handed to a locator is stable run to run;
    which one matches does not depend on their order (a locator collects every match and picks by
    position on the page, not by the order it was asked).

    ``chain`` is ordered and layout-agnostic: the first locator that applies wins, so one spec can
    serve a form that renders the field in a table and another that renders it as a label:value
    pair, without either form being named anywhere.
    """

    field: FieldId
    labels: tuple[str, ...]
    chain: LocatorChain
    floor: BoxPrecision


def _chain(*locators: ValueLocator) -> LocatorChain:
    """Build a chain from locators listed in priority order."""
    return LocatorChain(locators=tuple(locators))


# SECONDARY fields (a dose, a reference range) are exact-or-nothing. A coarse fallback box is worse
# than none for them: the whole point is to prove THIS cell, and a row-wide band claims to prove a
# cell it does not. Missing the floor costs only the box — extractor._secondary keeps the parent
# fact and cites the value without one, which the UI shows as unverified.
_SECONDARY_FLOOR = BoxPrecision.EXACT


# The lab chain, in the order lab extraction has always tried: the exact text-layer join first, the
# coarse OCR row band when the PDF has no text layer, and the page as a last resort.
#
# The lab floor is PAGE — i.e. anything locatable ships. That is the long-standing behaviour and is
# preserved deliberately: raising it would start dropping facts that are extracted today, which is a
# product decision, not a refactor.
LAB_SPECS: Mapping[FieldId, FieldSpec] = MappingProxyType(
    {
        FieldId.LAB_RESULT_VALUE: FieldSpec(
            field=FieldId.LAB_RESULT_VALUE,
            # A lab result is anchored by its own printed test name, which varies per report, so the
            # anchor is supplied per fact rather than drawn from a fixed alias set.
            labels=(),
            chain=_chain(
                RowSpanLocator(anchor_region=(0.0, 200.0), max_span_words=1),
                TableRowBandLocator(),
                PageBoxLocator(),
            ),
            floor=BoxPrecision.PAGE,
        ),
        # The test name and its LOINC identify WHICH test a value belongs to. The name was already
        # being located — it is the anchor the value's own locator scans for — and the box was then
        # thrown away, even though the sidebar shows the name as fact and a name bound to the wrong
        # row is how a value gets attributed to the wrong test. The LOINC is what write-back stamps
        # as `procedure_result.result_code`, so a wrong one files the result under the wrong test.
        # Both are anchored on the name, which is the row's left-most token.
        FieldId.LAB_RESULT_TEST_NAME: FieldSpec(
            field=FieldId.LAB_RESULT_TEST_NAME,
            labels=(),
            chain=_chain(
                RowSpanLocator(
                    anchor_region=(0.0, 200.0),
                    max_span_words=5,
                    cursor_key="lab.test_name",
                    include_anchor=True,
                )
            ),
            floor=_SECONDARY_FLOOR,
        ),
        FieldId.LAB_RESULT_LOINC: FieldSpec(
            field=FieldId.LAB_RESULT_LOINC,
            labels=(),
            chain=_chain(
                RowSpanLocator(
                    anchor_region=(0.0, 200.0), max_span_words=2, cursor_key="lab.loinc"
                )
            ),
            floor=_SECONDARY_FLOOR,
        ),
        # The two qualifiers around the value, anchored on the row's test name like the value is.
        # LabDetail stamps both onto the sidebar as system-authored fact — a wrong reference range
        # flips a normal value to abnormal — so each must be checkable in its own column rather than
        # transported as unlocatable text. `abnormal_flag` is deliberately NOT located: it is a
        # single character ("H"/"L") matching everywhere on the page, and what it asserts derives
        # from the value and the range, which are both boxed here.
        FieldId.LAB_RESULT_REFERENCE_RANGE: FieldSpec(
            field=FieldId.LAB_RESULT_REFERENCE_RANGE,
            labels=(),
            chain=_chain(
                RowSpanLocator(
                    anchor_region=(0.0, 200.0),
                    max_span_words=3,
                    cursor_key="lab.reference_range",
                )
            ),
            floor=_SECONDARY_FLOOR,
        ),
        FieldId.LAB_RESULT_UNIT: FieldSpec(
            field=FieldId.LAB_RESULT_UNIT,
            labels=(),
            chain=_chain(
                RowSpanLocator(
                    anchor_region=(0.0, 200.0), max_span_words=2, cursor_key="lab.unit"
                )
            ),
            floor=_SECONDARY_FLOOR,
        ),
    }
)

# The intake floor is LINE_BAND: a whole-page rectangle is not click-to-source, and unlike a lab
# report an intake form always has a locatable line to fall back to. A fact that cannot even be
# placed on a line is dropped rather than cited with a useless highlight.
_INTAKE_FLOOR = BoxPrecision.LINE_BAND

# Every chain below is ordered and LAYOUT-AGNOSTIC — no form is named anywhere. The two committed
# fixtures are deliberately disjoint (one uses tables + checkboxes and stacks values BELOW their
# labels; the other has neither and prints values to the RIGHT), and one spec set must extract both.
# That is what stops these specs overfitting to whichever form was in front of us.
INTAKE_SPECS: Mapping[FieldId, FieldSpec] = MappingProxyType(
    {
        FieldId.DEMOGRAPHICS_FULL_NAME: FieldSpec(
            field=FieldId.DEMOGRAPHICS_FULL_NAME,
            labels=("patient name (last, first)", "full name", "patient name", "name"),
            chain=_chain(LabelSpanLocator(), LineBandLocator()),
            floor=_INTAKE_FLOOR,
        ),
        FieldId.DEMOGRAPHICS_DATE_OF_BIRTH: FieldSpec(
            field=FieldId.DEMOGRAPHICS_DATE_OF_BIRTH,
            labels=("date of birth", "dob", "birth date"),
            chain=_chain(LabelSpanLocator(), LineBandLocator()),
            floor=_INTAKE_FLOOR,
        ),
        FieldId.DEMOGRAPHICS_SEX: FieldSpec(
            field=FieldId.DEMOGRAPHICS_SEX,
            labels=("sex", "gender", "sex assigned at birth"),
            # Checkbox FIRST: where the form offers ticked options, the tick is the only thing that
            # asserts an answer, and an unticked option must be refused rather than fall through to
            # a text match on its preprinted label. Where a form has no boxes, this defers and the
            # value is read as printed text — which is why evidence is a property of the box that
            # was found, never a rule attached to the field.
            chain=_chain(CheckboxLocator(), LabelSpanLocator(), LineBandLocator()),
            floor=_INTAKE_FLOOR,
        ),
        FieldId.DEMOGRAPHICS_ADDRESS: FieldSpec(
            field=FieldId.DEMOGRAPHICS_ADDRESS,
            labels=("home address", "address", "mailing address", "street address"),
            chain=_chain(LabelSpanLocator(), LineBandLocator()),
            floor=_INTAKE_FLOOR,
        ),
        FieldId.DEMOGRAPHICS_PHONE: FieldSpec(
            field=FieldId.DEMOGRAPHICS_PHONE,
            labels=("phone", "contact phone", "home phone", "mobile", "cell"),
            chain=_chain(LabelSpanLocator(), LineBandLocator()),
            floor=_INTAKE_FLOOR,
        ),
        FieldId.CHIEF_CONCERN: FieldSpec(
            field=FieldId.CHIEF_CONCERN,
            labels=(
                "reason for today's visit",
                "reason for visit",
                "chief concern",
                "chief complaint",
            ),
            # Free text under a heading: there is no label beside the value, so the heading scopes
            # the search instead. The span limit is raised well past the other fields' because this
            # value is a handwritten paragraph — the fixture's runs to 43 words across several
            # lines, and a limit below its length silently drops the field rather than boxing it.
            chain=_chain(SectionSpanLocator(max_span_words=90), LineBandLocator()),
            floor=_INTAKE_FLOOR,
        ),
        FieldId.ALLERGIES: FieldSpec(
            field=FieldId.ALLERGIES,
            labels=("allergies", "allergy / substance", "allergy", "known allergies"),
            chain=_chain(SectionSpanLocator(), LineBandLocator()),
            floor=_INTAKE_FLOOR,
        ),
        FieldId.FAMILY_HISTORY: FieldSpec(
            field=FieldId.FAMILY_HISTORY,
            labels=("family history", "family medical history"),
            # Same reasoning as sex: a family-history checklist preprints every condition, so only
            # the tick distinguishes "the patient has this" from "the form asked about this".
            chain=_chain(CheckboxLocator(), SectionSpanLocator(), LineBandLocator()),
            floor=_INTAKE_FLOOR,
        ),
        # SECONDARY, anchored on the entry each qualifies — the allergen, the condition — so a
        # reaction is read from ITS allergy's row and not from the next one. Anchoring is what makes
        # one spec serve both fixtures: v1 renders these as table rows, v2 as "Peanuts — hives" on a
        # line, and in each the qualifier sits after its anchor on the same row.
        FieldId.ALLERGIES_REACTION: FieldSpec(
            field=FieldId.ALLERGIES_REACTION,
            labels=("reaction", "reaction / severity"),
            chain=_chain(
                RowSpanLocator(
                    anchor_region=(0.0, 300.0), max_span_words=8, cursor_key="allergy.reaction"
                )
            ),
            floor=_SECONDARY_FLOOR,
        ),
        FieldId.FAMILY_HISTORY_RELATION: FieldSpec(
            field=FieldId.FAMILY_HISTORY_RELATION,
            labels=("relation", "relative", "who"),
            chain=_chain(
                RowSpanLocator(
                    anchor_region=(0.0, 300.0), max_span_words=6, cursor_key="family.relation"
                )
            ),
            floor=_SECONDARY_FLOOR,
        ),
    }
)

# Floor is LINE_BAND like intake: a printed medication row is always locatable, a whole-page box is
# not click-to-source. Reuses the section+line chain the intake medication table used (each drug on
# its own row under a medication heading).
_MEDICATION_LIST_FLOOR = BoxPrecision.LINE_BAND

MEDICATION_LIST_SPECS: Mapping[FieldId, FieldSpec] = MappingProxyType(
    {
        FieldId.CURRENT_MEDICATIONS: FieldSpec(
            field=FieldId.CURRENT_MEDICATIONS,
            labels=(
                "active medication list",
                "medication list",
                "current medications",
                "medications",
                "medication",
            ),
            chain=_chain(SectionSpanLocator(), LineBandLocator()),
            floor=_MEDICATION_LIST_FLOOR,
        ),
        # Dose and frequency are SECONDARY: each qualifies a medication whose name is already
        # located, so both are anchored on that name and searched within its row — the same
        # anchor-then-match shape the lab value uses against its test name. They are the values a
        # transcription error is most dangerous in ("500 mg" vs "5000 mg"), so they must be
        # checkable on the page rather than transported as unlocatable text.
        FieldId.CURRENT_MEDICATIONS_DOSE: FieldSpec(
            field=FieldId.CURRENT_MEDICATIONS_DOSE,
            labels=("dose / strength", "dose", "strength", "sig"),
            chain=_chain(
                RowSpanLocator(
                    anchor_region=(0.0, 300.0), max_span_words=6, cursor_key="medication.dose"
                )
            ),
            floor=_SECONDARY_FLOOR,
        ),
        FieldId.CURRENT_MEDICATIONS_FREQUENCY: FieldSpec(
            field=FieldId.CURRENT_MEDICATIONS_FREQUENCY,
            labels=("frequency", "how often", "directions"),
            chain=_chain(
                RowSpanLocator(
                    anchor_region=(0.0, 300.0), max_span_words=8, cursor_key="medication.frequency"
                )
            ),
            floor=_SECONDARY_FLOOR,
        ),
    }
)

_SPECS_BY_DOC_TYPE: Mapping[DocType, Mapping[FieldId, FieldSpec]] = MappingProxyType(
    {
        DocType.LAB_PDF: LAB_SPECS,
        DocType.INTAKE_FORM: INTAKE_SPECS,
        DocType.MEDICATION_LIST: MEDICATION_LIST_SPECS,
    }
)


def spec_for(doc_type: DocType, field: FieldId) -> FieldSpec:
    """The locator chain and precision floor bound to one field of one document type.

    Args:
        doc_type: Which document schema is being extracted.
        field: The semantic field being placed.

    Returns:
        The :class:`FieldSpec` for that field.

    Raises:
        KeyError: If the document type has no spec for the field — a programming error, since the
            specs and the mapper are written together.
    """
    return _SPECS_BY_DOC_TYPE[doc_type][field]
