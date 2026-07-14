import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from copilot.retrieval import EvidenceSnippet

# The in-repo curated corpus lives beside this module (JOS-52). Resolving it relative to the package
# keeps loading reproducible from the repo alone (the backup/recovery requirement) — no env var, no
# external store.
_CORPUS_DIR = Path(__file__).parent / "rag" / "corpus"

# Publisher/organization acronyms that should stay uppercase when a title is synthesized from a
# source slug (the corpus carries no human title field). Anything not listed is title-cased.
_ACRONYMS = frozenset(
    {
        "uspstf", "ada", "aha", "acc", "accaha", "kdigo", "gold", "gina", "acr",
        "aafp", "chest", "nice", "cdc", "nih", "kdoqi", "escardio", "esc", "who",
    }
)


def _synthesize_title(source: str) -> str:
    """Build a human-recognizable guideline title from a source slug.

    The corpus has no title field — only a source slug like ``uspstf-t2dm-2021``. This renders it
    readable for the evidence card by uppercasing known organization acronyms and title-casing the
    rest (``uspstf-t2dm-2021`` -> ``USPSTF T2DM 2021``). Purely cosmetic; the citation's stable
    identity is ``source_id`` (the slug), not this string.

    Args:
        source: The guideline source slug.

    Returns:
        A readable title. Falls back to the raw slug if it has no usable parts.
    """
    parts = [p for p in source.split("-") if p]
    if not parts:
        return source
    return " ".join(_render_part(p) for p in parts)


def _render_part(part: str) -> str:
    """Render one slug token: uppercase known acronyms and any token carrying a digit (e.g. a year
    or a coded condition like ``t2dm``); title-case the rest."""
    if part.lower() in _ACRONYMS or any(char.isdigit() for char in part):
        return part.upper()
    return part.title()


def _snippet_from_row(row: dict[str, Any], source_file: Path) -> EvidenceSnippet:
    """Parse one corpus JSONL row into an :class:`EvidenceSnippet` (parse-don't-validate).

    Args:
        row: A decoded JSONL object with the corpus fields
            (``chunk_id``/``guideline``/``source``/``source_url``/``section``/``date``/``text``).
        source_file: The file the row came from, named in errors for debuggability.

    Returns:
        The typed snippet, with ``title`` synthesized from the source slug.

    Raises:
        ValueError: If a required field (``chunk_id``/``source``/``text``) is missing or empty.
    """
    for required in ("chunk_id", "source", "text"):
        if not isinstance(row.get(required), str) or not row[required].strip():
            raise ValueError(f"corpus row in {source_file.name} missing required '{required}'")
    return EvidenceSnippet(
        chunk_id=row["chunk_id"],
        source_id=row["source"],
        title=_synthesize_title(row["source"]),
        section=row.get("section") or None,
        text=row["text"],
        source_url=row.get("source_url") or None,
        date=row.get("date") or None,
    )


def load_corpus(corpus_dir: Path | None = None) -> list[EvidenceSnippet]:
    """Load every guideline chunk from the in-repo corpus JSONL files into typed snippets.

    Reads all ``*.jsonl`` files under ``corpus_dir`` (the packaged corpus by default), one JSON
    object per line, mapping the corpus fields onto :class:`EvidenceSnippet` and synthesizing a
    readable ``title`` per source. Ordering is stable (files sorted, then line order) so a retriever
    built from it is deterministic.

    Args:
        corpus_dir: Directory of ``*.jsonl`` corpus files; defaults to the packaged corpus.

    Returns:
        Every chunk as an :class:`EvidenceSnippet`.

    Raises:
        FileNotFoundError: If ``corpus_dir`` does not exist.
        ValueError: If a line is not valid JSON or a required field is missing (the file is named).
    """
    directory = corpus_dir or _CORPUS_DIR
    if not directory.is_dir():
        raise FileNotFoundError(f"corpus directory not found: {directory}")
    snippets: list[EvidenceSnippet] = []
    for path in sorted(directory.glob("*.jsonl")):
        for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON in {path.name} line {line_no}") from exc
            snippets.append(_snippet_from_row(row, path))
    return snippets


@dataclass
class CorpusRetriever:
    """A :class:`~copilot.retrieval.Retriever` over the in-repo corpus with a lexical ranker.

    An honest stand-in for JOS-53's hybrid pipeline (FastEmbed dense+sparse -> Qdrant Fusion.RRF ->
    Cohere rerank): it ranks the *real* curated corpus by simple case-insensitive token overlap, so
    the evidence-retriever grounds on genuine guideline text with real citations today, and the
    Qdrant-backed retriever swaps in behind the same ``Retriever`` protocol with no worker change.
    """

    snippets: Sequence[EvidenceSnippet] = field(default_factory=tuple)

    @classmethod
    def from_corpus(cls, corpus_dir: Path | None = None) -> "CorpusRetriever":
        """Build a retriever from the on-disk corpus.

        Args:
            corpus_dir: Directory of corpus JSONL files; defaults to the packaged corpus.

        Returns:
            A retriever loaded with every corpus chunk.
        """
        return cls(snippets=load_corpus(corpus_dir))

    async def retrieve(self, query: str, *, limit: int) -> list[EvidenceSnippet]:
        """Return the top snippets for a query by lexical token overlap, best first.

        Args:
            query: The retrieval query (the physician's information need, reformulated).
            limit: The maximum number of snippets to return.

        Returns:
            Up to ``limit`` snippets with non-zero overlap, each carrying its overlap ``score``;
            empty when nothing overlaps (the caller then reports "no evidence" rather than guess).
        """
        terms = _terms(query)
        scored: list[tuple[int, int, EvidenceSnippet]] = []
        for index, snippet in enumerate(self.snippets):
            haystack = _terms(f"{snippet.text} {snippet.title} {snippet.section or ''}")
            overlap = len(terms & haystack)
            if overlap:
                # -index is a stable tiebreaker so equal-score results keep corpus order.
                ranked = snippet.model_copy(update={"score": float(overlap)})
                scored.append((overlap, -index, ranked))
        scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [snippet for _, _, snippet in scored[:limit]]


def _terms(text: str) -> set[str]:
    """Lowercase content words (length > 3) of ``text``, for lexical overlap scoring."""
    return {word for word in text.lower().split() if len(word) > 3}
