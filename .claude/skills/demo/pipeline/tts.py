"""Text-to-speech seam for the /demo pipeline.

The rest of the pipeline knows only ``synthesize(chunks, out_path, engine)`` — it
never imports a concrete engine. This is the swap seam described in the design spec
(``context/specs/demo-pipeline.md`` §6): Kokoro is the local, free, offline default;
flipping ``DEMO_TTS_ENGINE`` (or the ``engine`` argument) to a cloud engine is the
only change needed for a higher-production-value final.
"""

from __future__ import annotations

import os
import re
from abc import ABC, abstractmethod
from pathlib import Path

import numpy as np
import soundfile as sf

# Silence inserted to pace the narration. Between sentences within a beat, a short
# breath; between beats (paragraphs), a longer beat so section shifts read clearly.
_SILENCE_BETWEEN_SENTENCES_S: float = 0.25
_SILENCE_BETWEEN_BEATS_S: float = 0.5

# Kokoro degrades on very long inputs (its phoneme context is bounded), so beats are
# split into sentences before synthesis and stitched back together with silence.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")


class TtsEngine(ABC):
    """A text-to-speech backend that renders narration beats to a single WAV file."""

    @property
    @abstractmethod
    def sample_rate(self) -> int:
        """Native output sample rate of this engine, in Hz."""

    @abstractmethod
    def _render_text(self, text: str) -> np.ndarray:
        """Render one span of text to a mono float32 waveform at ``sample_rate``.

        Args:
            text: A single sentence or short span of narration.

        Returns:
            A 1-D float32 numpy array of audio samples.
        """

    def synthesize(self, beats: list[str], out_path: Path) -> float:
        """Render narration beats to a single continuous WAV file.

        Beats (paragraph-level spoken chunks) are split into sentences, each rendered
        independently, then concatenated with pacing silence. This keeps each call to
        the underlying engine short enough to avoid quality degradation on long input.

        Args:
            beats: Spoken narration chunks in order, one per paragraph.
            out_path: Destination WAV path.

        Returns:
            Duration of the written audio in seconds.

        Raises:
            ValueError: If ``beats`` contains no renderable text.
        """
        sr = self.sample_rate
        sentence_gap = np.zeros(int(sr * _SILENCE_BETWEEN_SENTENCES_S), dtype=np.float32)
        beat_gap = np.zeros(int(sr * _SILENCE_BETWEEN_BEATS_S), dtype=np.float32)

        segments: list[np.ndarray] = []
        for beat in beats:
            sentences = [s.strip() for s in _SENTENCE_SPLIT.split(beat) if s.strip()]
            for i, sentence in enumerate(sentences):
                segments.append(self._render_text(sentence))
                if i < len(sentences) - 1:
                    segments.append(sentence_gap)
            segments.append(beat_gap)

        if not segments:
            raise ValueError("Narration produced no spoken text to synthesize.")

        waveform = np.concatenate(segments)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(str(out_path), waveform, sr)
        return len(waveform) / sr


class KokoroEngine(TtsEngine):
    """Local Kokoro ONNX text-to-speech. Free, offline, no API key."""

    def __init__(self, model_path: Path, voices_path: Path, voice: str, speed: float) -> None:
        """Load the Kokoro ONNX model.

        Args:
            model_path: Path to ``kokoro-v1.0.onnx``.
            voices_path: Path to ``voices-v1.0.bin``.
            voice: Kokoro voice id (e.g. ``af_sarah``).
            speed: Speech-rate multiplier (1.0 is natural).

        Raises:
            FileNotFoundError: If a model file is missing (run ``setup.sh`` first).
        """
        for path in (model_path, voices_path):
            if not path.exists():
                raise FileNotFoundError(
                    f"Kokoro model file missing: {path}. Run the skill's setup.sh."
                )
        # Imported lazily so preflight/record stages don't pay the onnxruntime import.
        from kokoro_onnx import Kokoro

        self._kokoro = Kokoro(str(model_path), str(voices_path))
        self._voice = voice
        self._speed = speed

    @property
    def sample_rate(self) -> int:
        return 24_000

    def _render_text(self, text: str) -> np.ndarray:
        samples, _ = self._kokoro.create(
            text, voice=self._voice, speed=self._speed, lang="en-us"
        )
        return np.asarray(samples, dtype=np.float32)


def get_engine(
    name: str,
    *,
    kokoro_model: Path,
    kokoro_voices: Path,
    voice: str,
    speed: float,
) -> TtsEngine:
    """Resolve a TTS engine by name (the swap seam).

    Args:
        name: Engine identifier. Only ``kokoro`` ships today; cloud engines slot in
            here without touching any caller.
        kokoro_model: Path to the Kokoro ONNX model (used by the kokoro engine).
        kokoro_voices: Path to the Kokoro voices file.
        voice: Voice id passed to the engine.
        speed: Speech-rate multiplier.

    Returns:
        A ready-to-use :class:`TtsEngine`.

    Raises:
        ValueError: If ``name`` is not a known engine.
    """
    if name == "kokoro":
        return KokoroEngine(kokoro_model, kokoro_voices, voice, speed)
    raise ValueError(
        f"Unknown TTS engine {name!r}. Known engines: kokoro. "
        "Add a cloud engine here to raise production value for graded finals."
    )


# --- Narration parsing -------------------------------------------------------------

_ON_SCREEN_PREFIX = "on screen:"


def parse_narration(markdown: str) -> list[str]:
    """Extract the spoken beats from a ``narration.md`` document.

    The narration file interleaves three kinds of line: markdown headers (structure),
    ``On screen:`` directions (instructions for the browser walkthrough, not spoken),
    and prose (what the voiceover says). Only prose is returned, grouped into beats by
    blank-line-separated paragraphs so the synthesizer can pace between them.

    Args:
        markdown: Raw contents of ``narration.md``.

    Returns:
        Spoken beats in document order. Empty if the file has no prose yet.
    """
    beats: list[str] = []
    current: list[str] = []
    in_code_fence = False

    def flush() -> None:
        if current:
            beats.append(" ".join(current))
            current.clear()

    for raw_line in markdown.splitlines():
        line = raw_line.strip()

        if line.startswith("```"):
            in_code_fence = not in_code_fence
            flush()
            continue
        if in_code_fence:
            continue
        if not line:
            flush()
            continue
        if line.startswith("#"):  # header — structural, not spoken
            flush()
            continue
        if line.lower().startswith(_ON_SCREEN_PREFIX):  # driver direction, not spoken
            continue
        if line.startswith(">"):  # blockquote note, not spoken
            continue
        current.append(line)

    flush()
    return beats
