import json
from collections.abc import Iterator
from pathlib import Path

from pydantic import ValidationError

from copilot.rag.models import CorpusChunk

# The curated guideline corpus (JOS-52 output) lives beside this module, versioned in the repo
# so the index is reproducible from the repo alone (W2_ARCHITECTURE.md §6).
CORPUS_DIR = Path(__file__).parent / "corpus"


class CorpusError(RuntimeError):
    """Raised when a corpus JSONL file is missing, malformed, or has an invalid chunk row."""


def iter_corpus_chunks(corpus_dir: Path | None = None) -> Iterator[CorpusChunk]:
    """Parse every ``*.jsonl`` row under ``corpus_dir`` into a validated :class:`CorpusChunk`.

    Malformed rows fail loudly at this boundary (parse, don't validate downstream) so a bad
    chunk can never reach Qdrant or the answer model.

    Args:
        corpus_dir: Directory of ``*.jsonl`` corpus files; defaults to the in-repo corpus.

    Yields:
        Each corpus chunk, in filename-then-file order (deterministic).

    Raises:
        CorpusError: If the directory is absent, a line is not valid JSON, or a row fails
            :class:`CorpusChunk` validation.
    """
    directory = corpus_dir or CORPUS_DIR
    if not directory.is_dir():
        raise CorpusError(f"corpus directory not found: {directory}")
    for path in sorted(directory.glob("*.jsonl")):
        try:
            # utf-8-sig tolerates a leading BOM (which .strip() would not remove, causing a
            # spurious JSON error). A non-UTF-8 file (latin-1/cp1252 smart quotes) or read error
            # is surfaced as CorpusError, honoring this function's documented contract.
            contents = path.read_text(encoding="utf-8-sig")
        except (UnicodeDecodeError, OSError) as exc:
            raise CorpusError(f"{path.name} could not be read as UTF-8") from exc
        for line_no, raw in enumerate(contents.splitlines(), start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError as exc:
                raise CorpusError(f"{path.name}:{line_no} is not valid JSON") from exc
            try:
                yield CorpusChunk.model_validate(data)
            except ValidationError as exc:
                raise CorpusError(f"{path.name}:{line_no} failed chunk validation") from exc


def load_corpus(corpus_dir: Path | None = None) -> list[CorpusChunk]:
    """Load the whole corpus into memory as a list of validated chunks.

    Args:
        corpus_dir: Directory of ``*.jsonl`` corpus files; defaults to the in-repo corpus.

    Returns:
        Every corpus chunk.

    Raises:
        CorpusError: If the corpus is missing or any row is malformed.
    """
    return list(iter_corpus_chunks(corpus_dir))
