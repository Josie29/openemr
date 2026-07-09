"""Subtitle rendering for the /demo pipeline.

The available ffmpeg build is a minimal bottle without libass or libfreetype, so it
cannot render text (no ``subtitles``/``drawtext`` filters). Since the pipeline generates
the SRT itself (from the voiceover, via Whisper), captions are rendered to transparent
PNG strips with Pillow and composited with ffmpeg's ``overlay`` filter — which keeps the
whole pipeline on the already-installed ffmpeg with no external dependencies.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# macOS system font; always present. Overridable if the skill is ported off macOS.
_FONT_PATH = "/System/Library/Fonts/Supplemental/Arial.ttf"
_FONT_SIZE = 34
_LINE_SPACING = 8
_PAD_X = 28  # horizontal padding inside the caption box
_PAD_Y = 14  # vertical padding inside the caption box
_BOX_ALPHA = 165  # semi-transparent black background for legibility over any footage

_SRT_TIME = re.compile(
    r"(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})"
)


@dataclass(frozen=True)
class Caption:
    """One subtitle cue: text shown for the window [start, end) in seconds."""

    start: float
    end: float
    text: str


def _to_seconds(h: str, m: str, s: str, ms: str) -> float:
    """Convert SRT timestamp components to seconds."""
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000.0


def parse_srt(path: Path) -> list[Caption]:
    """Parse an SRT file into caption cues.

    Args:
        path: Path to a ``.srt`` file.

    Returns:
        Cues in order. Empty if the file has no timed entries.
    """
    captions: list[Caption] = []
    blocks = re.split(r"\n\s*\n", path.read_text(encoding="utf-8").strip())
    for block in blocks:
        lines = [ln for ln in block.splitlines() if ln.strip()]
        time_line = next((ln for ln in lines if _SRT_TIME.search(ln)), None)
        if time_line is None:
            continue
        m = _SRT_TIME.search(time_line)
        assert m is not None  # guarded by the search above
        start = _to_seconds(*m.group(1, 2, 3, 4))
        end = _to_seconds(*m.group(5, 6, 7, 8))
        idx = lines.index(time_line)
        text = " ".join(ln.strip() for ln in lines[idx + 1 :]).strip()
        if text:
            captions.append(Caption(start, end, text))
    return captions


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Word-wrap text to fit ``max_width`` pixels for the given font.

    Args:
        text: Caption text.
        font: The loaded truetype font.
        max_width: Maximum line width in pixels.

    Returns:
        Wrapped lines.
    """
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        if font.getbbox(trial)[2] <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_caption_strips(
    captions: list[Caption], video_width: int, out_dir: Path
) -> list[tuple[Path, Caption]]:
    """Render each caption to a transparent PNG strip sized to the video width.

    Each strip is a full-width, bottom-anchored caption band (transparent elsewhere) so
    it can be overlaid at x=0,y=0 and gated by an ``enable`` time window in ffmpeg.

    Args:
        captions: Cues to render.
        video_width: Output video width in pixels (strips match it).
        out_dir: Directory for the generated PNGs.

    Returns:
        (png path, caption) pairs in cue order.
    """
    font = ImageFont.truetype(_FONT_PATH, _FONT_SIZE)
    line_h = font.getbbox("Ay")[3] + _LINE_SPACING
    max_text_width = video_width - 4 * _PAD_X  # leave side margins
    strips_dir = out_dir / "captions"
    strips_dir.mkdir(parents=True, exist_ok=True)

    rendered: list[tuple[Path, Caption]] = []
    for i, cap in enumerate(captions):
        lines = _wrap(cap.text, font, max_text_width)
        box_h = len(lines) * line_h + 2 * _PAD_Y
        # Full-width transparent canvas; caption band sits at the bottom.
        strip_h = box_h + 40  # bottom margin baked in so overlay can sit at y=0
        img = Image.new("RGBA", (video_width, strip_h), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Centered box just wide enough for the widest line.
        box_w = max(font.getbbox(ln)[2] for ln in lines) + 2 * _PAD_X
        box_x0 = (video_width - box_w) // 2
        draw.rounded_rectangle(
            [box_x0, 0, box_x0 + box_w, box_h], radius=10, fill=(0, 0, 0, _BOX_ALPHA)
        )
        for j, line in enumerate(lines):
            lw = font.getbbox(line)[2]
            draw.text(
                ((video_width - lw) // 2, _PAD_Y + j * line_h),
                line, font=font, fill=(255, 255, 255, 255),
            )

        png = strips_dir / f"cue_{i:03d}.png"
        img.save(png)
        rendered.append((png, cap))
    return rendered
