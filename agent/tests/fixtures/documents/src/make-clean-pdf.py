#!/usr/bin/env python3
"""Render a clean document fixture from its HTML source to PDF via headless Chrome.

The committed fixture PDFs were originally produced this way by hand (their Producer metadata reads
"Skia/PDF", Chrome's PDF writer), but the step itself was never committed — so regenerating a
fixture meant reconstructing the invocation from scratch and hoping the geometry matched. It has to
match: the OCR recordings and the `goldens/*.geometry.json` boxes are all in the page's coordinate
space, so a render that shifts the layout silently invalidates every recorded box.

`make-scanned-variant.py` is the sibling step: it rasterizes the same HTML through Chrome and bakes
in scan artifacts. This one is the clean, text-layer-bearing original.

Dependencies (host build step only — not a runtime dep): Google Chrome.

Usage:
    python make-clean-pdf.py <source.html> <out.pdf>
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"


def render(html: Path, out_pdf: Path) -> None:
    """Render an HTML source file to PDF with headless Chrome.

    The page's own ``@page { size: Letter; margin: 0 }`` rule owns the paper geometry, so no margin
    flags are passed here — Chrome would otherwise add its default 0.4in margin on top and shift
    every box on the page.

    Args:
        html: The HTML source to render.
        out_pdf: Destination PDF path.

    Raises:
        RuntimeError: If Chrome is missing, or exits without producing the PDF.
    """
    if not Path(CHROME).exists():
        raise RuntimeError(f"Google Chrome not found at {CHROME}")

    result = subprocess.run(
        [
            CHROME,
            "--headless",
            "--disable-gpu",
            "--no-pdf-header-footer",
            f"--print-to-pdf={out_pdf.resolve()}",
            html.resolve().as_uri(),
        ],
        check=False,
        capture_output=True,
    )
    if not out_pdf.exists():
        raise RuntimeError(
            f"Chrome did not emit a PDF (exit {result.returncode}): "
            f"{result.stderr.decode('utf-8', 'replace')[:500]}"
        )


def main() -> int:
    """Parse arguments and render the requested fixture.

    Returns:
        Process exit status: 0 on success, 1 when rendering failed.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("html", type=Path, help="HTML source to render.")
    parser.add_argument("out_pdf", type=Path, help="Destination PDF.")
    args = parser.parse_args()

    try:
        render(args.html, args.out_pdf)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    size = args.out_pdf.stat().st_size
    print(f"wrote {args.out_pdf} ({size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
