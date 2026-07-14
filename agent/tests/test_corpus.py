import pytest

from copilot.rag.corpus import CorpusError, iter_corpus_chunks, load_corpus


def test_load_corpus_parses_every_chunk_with_full_provenance() -> None:
    # The corpus is the reproducible-from-repo source of the index (W2_ARCH §6). If a chunk row
    # loses a provenance field, the citation built from it would be incomplete — this fails at
    # load time instead of surfacing as an uncitable snippet at query time.
    chunks = load_corpus()

    assert len(chunks) >= 50  # JOS-52 curated 55; guard against a corpus that silently emptied
    for chunk in chunks:
        assert chunk.chunk_id and chunk.guideline and chunk.source
        assert chunk.section and chunk.source_url and chunk.text


def test_load_corpus_rejects_a_malformed_row(tmp_path) -> None:  # type: ignore[no-untyped-def]
    # Parse-don't-validate at the boundary: a corrupt JSONL line must raise loudly, never be
    # silently skipped into a partial index that looks complete.
    bad = tmp_path / "broken.jsonl"
    bad.write_text('{"chunk_id": "x"}\n', encoding="utf-8")  # missing required fields
    with pytest.raises(CorpusError):
        list(iter_corpus_chunks(tmp_path))
