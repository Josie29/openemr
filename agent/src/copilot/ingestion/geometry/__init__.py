from copilot.ingestion.geometry.boxes import (
    BoxEvidence,
    BoxPrecision,
    LocatedBox,
    LocateOutcome,
    LocateResult,
    LocatorName,
)
from copilot.ingestion.geometry.spans import first_token, match_span, merge_and_pad, norm
from copilot.ingestion.geometry.words import Word, extract_word_boxes

__all__ = [
    "BoxEvidence",
    "BoxPrecision",
    "LocateOutcome",
    "LocateResult",
    "LocatedBox",
    "LocatorName",
    "Word",
    "extract_word_boxes",
    "first_token",
    "match_span",
    "merge_and_pad",
    "norm",
]
