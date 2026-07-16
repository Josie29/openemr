"""Backfill a verbatim `anchor_quote` onto each guideline corpus chunk.

The evidence panel's "View source" deep-links the cited passage with a URL text fragment
(`…#:~:text=<anchor>`), which highlights the span in Chrome for both PDF and HTML sources — but
only if the anchor text exists *verbatim* in the source. The curated chunk `text` does not: it is
lightly reworded and reframed (e.g. "GINA assessment of asthma symptom control (Box 2-2A). …"),
so an exact match fails.

This one-time maintenance script derives a guaranteed-matchable anchor per chunk: it fetches each
source (PDF via pdfplumber, HTML by stripping tags), then takes the longest contiguous substring
shared between the curated chunk text and the source text. That span is, by construction, verbatim
in the source — so a text fragment built from it will match. The span is written back as
`anchor_quote`; the curated `text` (embedded, displayed, cited) is left untouched.

Re-run whenever the corpus changes. Idempotent — rewrites `anchor_quote` in place. `--dry-run`
previews the matched/unmatched report without writing.

    python -m scripts.backfill_corpus_anchors --dry-run
    python -m scripts.backfill_corpus_anchors
"""

from __future__ import annotations

import argparse
import difflib
import html
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import httpx
import pdfplumber

# A span shorter than this is too generic to anchor on reliably (it might match the wrong place, or
# nowhere), so the chunk is left without an anchor rather than given a bad one.
_MIN_ANCHOR_CHARS = 24
# Cap the anchor length: a very long fragment is more brittle (any single rendering difference in
# the middle breaks the match), and a sentence or two is plenty to land the reader on the spot.
_MAX_ANCHOR_CHARS = 160

_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 AgentForge-corpus-backfill"
)

# Strip <script>/<style> bodies before tags so their contents never leak into the visible text.
_SCRIPT_STYLE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG = re.compile(r"<[^>]+>")


def _normalize(text: str) -> str:
    """Collapse whitespace to single spaces — the form both chunk and source text are matched in."""
    return re.sub(r"\s+", " ", text).strip()


def _html_to_text(raw: str) -> str:
    """Reduce an HTML document to visible text (crude tag strip; enough for substring matching).

    Args:
        raw: The raw HTML source.

    Returns:
        Whitespace-normalized visible text with entities unescaped.
    """
    return _normalize(html.unescape(_TAG.sub(" ", _SCRIPT_STYLE.sub(" ", raw))))


def _source_segments(url: str) -> list[str]:
    """Fetch a source and return its text as segments to search for the anchor span.

    A PDF yields one segment per page (bounds each substring search to a page, which is fast and
    keeps a match from spanning a page break); an HTML page yields a single whole-document segment.

    Args:
        url: The source URL.

    Returns:
        Normalized text segments; empty if the source can't be fetched or read.

    Raises:
        httpx.HTTPError: If the fetch fails (caller handles).
    """
    with httpx.Client(follow_redirects=True, timeout=60.0, headers={"User-Agent": _UA}) as client:
        response = client.get(url)
        response.raise_for_status()
        body = response.content

    if body[:5] == b"%PDF-":
        segments: list[str] = []
        # pdfplumber needs a file path or file-like; hand it the bytes via a temp buffer.
        import io

        with pdfplumber.open(io.BytesIO(body)) as pdf:
            for page in pdf.pages:
                segments.append(_normalize(page.extract_text() or ""))
        return segments
    return [_html_to_text(body.decode("utf-8", errors="replace"))]


def _trim_to_words(segment: str, start: int, size: int) -> str:
    """Trim a matched span to whole-word boundaries and surrounding whitespace/punctuation.

    A longest-common-substring match can begin or end mid-word (e.g. " SABA reliever…" or
    "…future ris"); a text fragment reads cleaner and matches more reliably on whole words.

    Args:
        segment: The source segment the match came from.
        start: Match start index into ``segment``.
        size: Match length.

    Returns:
        The trimmed verbatim span.
    """
    end = start + size
    if start > 0 and segment[start - 1].isalnum():  # began mid-word — advance past the partial
        while start < end and segment[start].isalnum():
            start += 1
    if end < len(segment) and segment[end].isalnum():  # ended mid-word — retreat past the partial
        while end > start and segment[end - 1].isalnum():
            end -= 1
    return segment[start:end].strip(" .,;:-—")


def _best_anchor(chunk_text: str, segments: list[str]) -> str | None:
    """Return the longest verbatim span shared between the chunk and any source segment.

    Args:
        chunk_text: The curated chunk text.
        segments: The source's text segments (per :func:`_source_segments`).

    Returns:
        The trimmed verbatim anchor span, or None if the best match is shorter than the floor.
    """
    needle = _normalize(chunk_text)
    best = ""
    for segment in segments:
        matcher = difflib.SequenceMatcher(None, needle.lower(), segment.lower(), autojunk=False)
        match = matcher.find_longest_match(0, len(needle), 0, len(segment))
        if match.size > len(best):
            best = _trim_to_words(segment, match.b, match.size)
    if len(best) < _MIN_ANCHOR_CHARS:
        return None
    if len(best) <= _MAX_ANCHOR_CHARS:
        return best
    # Trim the cap back to a whole word: a text-fragment endpoint that ends mid-word ("…beca" of
    # "because") lands on no word boundary and fails to match, so drop the final partial token.
    return best[:_MAX_ANCHOR_CHARS].rsplit(" ", 1)[0]


def _load_jsonl(path: Path) -> list[dict[str, object]]:
    """Load a corpus .jsonl into dicts, preserving each record's field order."""
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _with_anchor(record: dict[str, object], anchor: str) -> dict[str, object]:
    """Return a copy of a chunk with ``anchor_quote`` inserted right after ``text``."""
    rebuilt: dict[str, object] = {}
    for key, value in record.items():
        if key == "anchor_quote":
            continue  # drop any stale value; re-inserted in canonical position below
        rebuilt[key] = value
        if key == "text":
            rebuilt["anchor_quote"] = anchor
    if "anchor_quote" not in rebuilt:  # no text key (shouldn't happen) — append
        rebuilt["anchor_quote"] = anchor
    return rebuilt


def _write_jsonl(path: Path, records: list[dict[str, object]]) -> None:
    """Write records back to a .jsonl file, one compact JSON object per line."""
    path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records))


def main() -> int:
    """Backfill anchors across the corpus and print a matched/unmatched report.

    Returns:
        0 if every chunk got an anchor, 1 if any could not (so a caller can notice gaps),
        2 on a fatal setup error (no corpus found).
    """
    default_corpus = Path(__file__).resolve().parents[1] / "src" / "copilot" / "rag" / "corpus"
    parser = argparse.ArgumentParser(
        description="Backfill verbatim `anchor_quote` onto corpus chunks."
    )
    parser.add_argument("--corpus-dir", type=Path, default=default_corpus)
    parser.add_argument(
        "--dry-run", action="store_true", help="Report anchors without writing the corpus."
    )
    args = parser.parse_args()

    files = sorted(args.corpus_dir.glob("*.jsonl"))
    if not files:
        print(f"no corpus .jsonl files under {args.corpus_dir}", file=sys.stderr)
        return 2

    segments_by_url: dict[str, list[str]] = {}
    total = matched = 0
    unmatched: list[str] = []

    for path in files:
        records = _load_jsonl(path)
        by_url: dict[str, list[dict[str, object]]] = defaultdict(list)
        for record in records:
            by_url[str(record["source_url"])].append(record)

        anchors: dict[int, str] = {}  # id(record) -> anchor, to rewrite in original order
        print(f"\n{path.name}")
        for url, chunks in by_url.items():
            if url not in segments_by_url:
                try:
                    segments_by_url[url] = _source_segments(url)
                except (httpx.HTTPError, OSError, ValueError) as exc:
                    print(f"  ! could not read {url}: {exc}", file=sys.stderr)
                    segments_by_url[url] = []
            segments = segments_by_url[url]
            for chunk in chunks:
                total += 1
                chunk_id = str(chunk["chunk_id"])
                anchor = _best_anchor(str(chunk["text"]), segments) if segments else None
                if anchor is None:
                    unmatched.append(chunk_id)
                    print(f"  ? (no anchor)  {chunk_id}")
                    continue
                matched += 1
                anchors[id(chunk)] = anchor
                print(f"  ✓ {chunk_id}\n      {anchor!r}")

        if anchors and not args.dry_run:
            rewritten = [
                _with_anchor(record, anchors[id(record)]) if id(record) in anchors else record
                for record in records
            ]
            _write_jsonl(path, rewritten)

    verb = "would anchor" if args.dry_run else "anchored"
    print(f"\n{verb} {matched}/{total} chunks", end="")
    print(
        f" — {len(unmatched)} without anchor: {', '.join(unmatched)}"
        if unmatched
        else " — all anchored"
    )
    if not args.dry_run and matched:
        print("wrote anchor_quote into the corpus; re-index with `make index FORCE=1`")
    return 1 if unmatched else 0


if __name__ == "__main__":
    raise SystemExit(main())
