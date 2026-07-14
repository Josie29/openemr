#!/usr/bin/env python3
"""Generate a degraded "scanned" variant of a clean document fixture.

Renders the clean HTML source to a high-DPI raster via headless Chrome, then bakes in
photocopier/scan artifacts and re-wraps the result as a PDF. Two intensity profiles:

  light  — legible-but-imperfect. Skew, warm grayscale, blur, sensor noise, JPEG. Proves the
           extractor's *happy path*: it degrades gracefully and still returns good fields.
  heavy  — failure-path fixture. Low resolution + heavy blur/noise + faded contrast + aggressive
           JPEG, PLUS localized damage (toner streaks, a coffee-ring stain, a fold crease, a dark
           scanner edge) positioned over the renal "money" values and the prior column. Specific
           fields become genuinely unreadable so the extractor's confidence gate must surface
           uncertainty (low-confidence / missing / refusal) instead of confidently emitting garbage.
           This is what the eval set's safe_refusal / confidence-gate cases need.

Dependencies (host build step only — not a runtime dep): Pillow, numpy, and Google Chrome.
Deterministic: the noise RNG is seeded, so re-running reproduces the same PDF.

Usage:
    python make-scanned-variant.py [--profile light|heavy] <clean.html> <out.pdf> [preview.png]
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image, ImageEnhance, ImageFilter

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"

# 8.5x11in at 96 CSS px/in = 816x1056 CSS px; a 2x device scale yields a 1632x2112 source raster.
CAPTURE_W, CAPTURE_H = 816, 1056
CAPTURE_SCALE = 2
PAPER = (247, 244, 236)  # warm off-white the page sits on

PROFILES: dict[str, dict[str, Any]] = {
    "light": {
        "width": 1275,        # ~150 DPI on Letter
        "dpi": 150.0,
        "skew_deg": -0.6,
        "desaturate": 0.85,
        "illum": 0.10,
        "black_lift": 0,      # ink stays dark
        "contrast": 0.88,
        "brightness": 1.04,
        "blur": 0.7,
        "noise_sigma": 6.0,
        "pepper_p": 0.0008,
        "salt_p": 0.0,
        "jpeg_q": 72,
        "artifacts": [],
    },
    "heavy": {
        "width": 1000,        # ~118 DPI — low-res but the bulk of the page still reads
        "dpi": 118.0,
        "skew_deg": -2.2,
        "desaturate": 1.0,    # full grayscale photocopy
        "illum": 0.20,        # uneven lamp
        "black_lift": 22,     # faded toner — blacks become dark gray, still legible
        "contrast": 0.74,
        "brightness": 1.05,
        "blur": 1.1,
        "noise_sigma": 13.0,
        "pepper_p": 0.003,
        "salt_p": 0.0015,
        "jpeg_q": 40,
        # Localized damage does the "make it fail" work — the global settings stay readable so the
        # extractor still returns structure. Illegibility is confined to the
        # stain/streak/crease/edge.
        "artifacts": ["toner_streaks", "coffee_stain", "fold_crease", "edge_shadow"],
    },
}


def render_png(html: Path, out_png: Path) -> None:
    """Rasterize the clean HTML to PNG with headless Chrome.

    Args:
        html: Path to the clean document HTML source.
        out_png: Where to write the captured PNG.

    Raises:
        RuntimeError: If Chrome does not produce the screenshot.
    """
    subprocess.run(
        [
            CHROME, "--headless", "--disable-gpu", "--hide-scrollbars",
            f"--window-size={CAPTURE_W},{CAPTURE_H}",
            f"--force-device-scale-factor={CAPTURE_SCALE}",
            f"--screenshot={out_png}", html.resolve().as_uri(),
        ],
        check=True, capture_output=True,
    )
    if not out_png.exists():
        raise RuntimeError("Chrome did not emit a screenshot")


def illumination(h: int, w: int, strength: float) -> np.ndarray:
    """Smooth multiplicative brightness field (uneven copier lamp / page lift).

    Args:
        h: Image height in pixels.
        w: Image width in pixels.
        strength: Peak darkening fraction toward the lower-right corner.

    Returns:
        An ``(h, w, 1)`` float field to multiply against pixels.
    """
    ys = np.linspace(-1.0, 1.0, h)[:, None]
    xs = np.linspace(-1.0, 1.0, w)[None, :]
    field = 1.0 - strength * ((xs + 0.6) ** 2 + (ys + 0.4) ** 2) / 4.0
    field = np.clip(field + 0.02, 1.0 - strength * 1.4, 1.03)
    return field[:, :, None]


def toner_streaks(arr: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Vertical toner smears (black) and a dropout streak (white) across the full page height.

    Placed to cross the Result/Flag columns and the Prior column so those values partially vanish.
    """
    h, w, _ = arr.shape
    x = int(w * 0.44)          # a single gray smear over the Result/Flag columns
    bw = int(rng.integers(4, 7))
    arr[:, x:x + bw, :] *= 0.4
    x = int(w * 0.90)          # dropout (missing toner) over the Prior column
    bw = int(rng.integers(3, 6))
    arr[:, x:x + bw, :] = arr[:, x:x + bw, :] * 0.2 + 255 * 0.8
    return arr


def coffee_stain(arr: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Translucent brown ring stain over the CMP renal rows (creatinine / eGFR / K)."""
    h, w, _ = arr.shape
    cy, cx = int(h * 0.42), int(w * 0.5)
    r = min(h, w) * 0.17
    ys = np.arange(h)[:, None]
    xs = np.arange(w)[None, :]
    dist = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2) / r
    ring = np.exp(-((dist - 1.0) ** 2) / (2 * 0.11 ** 2))       # dark rim where liquid pooled
    fill = np.clip(1.0 - dist, 0, 1) * 0.45                     # lighter tint inside
    alpha = np.clip(ring * 0.6 + fill, 0, 0.72)[:, :, None]
    brown = np.array([120, 84, 52], dtype=np.float32)
    stained: np.ndarray = arr * (1 - alpha) + brown * alpha
    return stained


def fold_crease(arr: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """A horizontal fold: bright highlight line with shadowed edges across the results table."""
    h, w, _ = arr.shape
    y = int(h * 0.58)
    for dy in range(-7, 8):
        yy = y + dy
        if 0 <= yy < h:
            arr[yy, :, :] *= 1.0 - 0.42 * np.exp(-(dy ** 2) / (2 * 3.2 ** 2))
    if 0 <= y < h:
        arr[y, :, :] = np.clip(arr[y, :, :] + 55, 0, 255)
    return arr


def edge_shadow(arr: np.ndarray, rng: np.random.Generator) -> np.ndarray:
    """Darkening gradient + thin black scanner border down the right edge.

    (Page not flat on glass.)
    """
    h, w, _ = arr.shape
    band = int(w * 0.11)
    grad = np.linspace(1.0, 0.32, band)[None, :, None]
    arr[:, w - band:, :] *= grad
    b = max(2, int(w * 0.015))
    arr[:, w - b:, :] *= 0.05
    return arr


ARTIFACTS = {
    "toner_streaks": toner_streaks,
    "coffee_stain": coffee_stain,
    "fold_crease": fold_crease,
    "edge_shadow": edge_shadow,
}


def degrade(src_png: Path, out_pdf: Path, preview: Path | None, p: dict[str, Any]) -> None:
    """Apply the scan-artifact pipeline for profile ``p`` and write the PDF (+ optional preview).

    Args:
        src_png: The clean high-DPI capture.
        out_pdf: Destination PDF path.
        preview: Optional PNG path for visual inspection.
        p: A profile dict from ``PROFILES``.
    """
    rng = np.random.default_rng(20260708)  # seed = collection date, for reproducibility

    src = Image.open(src_png).convert("RGB")
    tw = p["width"]
    th = round(tw * src.height / src.width)
    img = src.resize((tw, th), Image.Resampling.LANCZOS)

    # Feed skew, filling exposed corners with the warm paper color.
    img = img.rotate(
        p["skew_deg"], resample=Image.Resampling.BICUBIC, expand=False, fillcolor=PAPER
    )

    # Desaturate toward grayscale, retaining an optional faint warm cast.
    gray = img.convert("L").convert("RGB")
    img = Image.blend(img, gray, p["desaturate"])

    arr = np.asarray(img, dtype=np.float32)
    arr *= illumination(th, tw, p["illum"])

    tint = np.array(PAPER, dtype=np.float32) / 255.0
    arr *= 0.5 + 0.5 * tint
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")

    img = ImageEnhance.Contrast(img).enhance(p["contrast"])
    img = ImageEnhance.Brightness(img).enhance(p["brightness"])
    img = img.filter(ImageFilter.GaussianBlur(radius=p["blur"]))

    # Faded-toner black lift: compress the dynamic range from below so ink is mid-gray, not black.
    arr = np.asarray(img, dtype=np.float32)
    if p["black_lift"]:
        arr = arr * (1.0 - p["black_lift"] / 255.0) + p["black_lift"]

    # Localized damage (heavy profile).
    for name in p["artifacts"]:
        arr = ARTIFACTS[name](arr, rng)

    # Sensor noise + salt/pepper.
    arr += rng.normal(0.0, p["noise_sigma"], arr.shape).astype(np.float32)
    hh, ww = arr.shape[:2]
    if p["pepper_p"]:
        arr[rng.random((hh, ww)) < p["pepper_p"]] *= 0.4
    if p["salt_p"]:
        arr[rng.random((hh, ww)) < p["salt_p"]] = 255
    img = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8), "RGB")

    # JPEG round-trip bakes in blocky compression artifacts.
    jpg = out_pdf.with_suffix(".scanjpg.jpg")
    img.save(jpg, "JPEG", quality=p["jpeg_q"])
    img = Image.open(jpg).convert("RGB")

    img.save(out_pdf, "PDF", resolution=p["dpi"])
    if preview is not None:
        img.save(preview, "PNG")
    jpg.unlink(missing_ok=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Generate a degraded scanned fixture from clean HTML.")
    ap.add_argument("--profile", choices=sorted(PROFILES), default="light")
    ap.add_argument("html", type=Path)
    ap.add_argument("out_pdf", type=Path)
    ap.add_argument("preview", type=Path, nargs="?", default=None)
    args = ap.parse_args()

    tmp_png = args.out_pdf.with_suffix(".clean.png")
    render_png(args.html, tmp_png)
    degrade(tmp_png, args.out_pdf, args.preview, PROFILES[args.profile])
    tmp_png.unlink(missing_ok=True)
    print(f"wrote {args.out_pdf} (profile={args.profile})")


if __name__ == "__main__":
    main()
