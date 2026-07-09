"""CLI for the /demo weekly-demo-video pipeline.

Subcommands orchestrated by ``SKILL.md``:

    preflight <label>       Verify toolchain/models/permission, scaffold narration.md.
    record <label> start    Begin detached screen capture (returns immediately).
    record <label> stop     Stop capture and finalize the raw recording.
    build <label>           Narrate (TTS) + subtitle (Whisper) + assemble demo.mp4.

Design: record-first, narrate-to-footage, continuous-track assembly. See
``context/specs/demo-pipeline.md``. The video is fit to the voiceover length (narration
is the spine): shorter footage holds its last frame; longer footage is trimmed.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import captions
import tts

# --- Configuration -----------------------------------------------------------------

CACHE_DIR = Path.home() / ".cache" / "demo-skill"
MODELS_DIR = CACHE_DIR / "models"
KOKORO_MODEL = MODELS_DIR / "kokoro-v1.0.onnx"
KOKORO_VOICES = MODELS_DIR / "voices-v1.0.bin"
WHISPER_MODEL = MODELS_DIR / "ggml-base.en.bin"

# Overridable via environment so the skill is portable when promoted to a global skill.
OUTPUT_ROOT = Path(os.environ.get("DEMO_OUTPUT_ROOT", Path.cwd() / "context" / "demos"))
OPENEMR_URL = os.environ.get("DEMO_OPENEMR_URL", "http://localhost:8300")
TTS_ENGINE = os.environ.get("DEMO_TTS_ENGINE", "kokoro")
TTS_VOICE = os.environ.get("DEMO_VOICE", "af_sarah")
TTS_SPEED = float(os.environ.get("DEMO_SPEED", "1.0"))

OUTPUT_HEIGHT = 1080  # 1080p H.264 output (PRD submission requirement)
TARGET_MIN_S = 180  # 3 minutes
TARGET_MAX_S = 300  # 5 minutes

# A freshly scaffolded narration.md carries this sentinel; presence means "unedited".
_TEMPLATE_SENTINEL = "<!-- DEMO-TEMPLATE-UNEDITED -->"


class PipelineError(RuntimeError):
    """A blocking pipeline failure with a user-actionable message."""


# --- Small process helpers ---------------------------------------------------------


def _run(cmd: list[str], *, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing output; raise :class:`PipelineError` on failure.

    Args:
        cmd: Argument vector.
        cwd: Working directory for the child process.

    Returns:
        The completed process (stdout/stderr captured as text).

    Raises:
        PipelineError: If the command exits non-zero.
    """
    proc = subprocess.run(
        cmd, cwd=cwd, capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()[-8:]
        raise PipelineError(
            f"Command failed ({proc.returncode}): {' '.join(cmd[:3])} ...\n"
            + "\n".join(tail)
        )
    return proc


def _ffprobe_duration(path: Path) -> float:
    """Return the duration of a media file in seconds via ffprobe.

    Args:
        path: Media file to probe.

    Returns:
        Duration in seconds.

    Raises:
        PipelineError: If ffprobe cannot read a duration.
    """
    proc = _run(
        [
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(path),
        ]
    )
    try:
        return float(proc.stdout.strip())
    except ValueError as exc:
        raise PipelineError(f"Could not read duration of {path}") from exc


def _ffprobe_dimensions(path: Path) -> tuple[int, int]:
    """Return (width, height) in pixels of a video file via ffprobe.

    Args:
        path: Video file to probe.

    Returns:
        (width, height) in pixels.

    Raises:
        PipelineError: If dimensions cannot be read.
    """
    proc = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height",
        "-of", "csv=s=x:p=0", str(path),
    ])
    try:
        w, h = proc.stdout.strip().split("x")
        return int(w), int(h)
    except ValueError as exc:
        raise PipelineError(f"Could not read dimensions of {path}") from exc


def detect_screen_device() -> str:
    """Find the avfoundation device index for the primary display capture.

    avfoundation device indices are not stable across machines or sessions (cameras,
    virtual devices shift them), so the primary screen is detected fresh each run.
    Override with the ``DEMO_SCREEN_DEVICE`` environment variable.

    Returns:
        The avfoundation video device index (as a string) for "Capture screen 0".

    Raises:
        PipelineError: If no screen-capture device is found.
    """
    override = os.environ.get("DEMO_SCREEN_DEVICE")
    if override:
        return override

    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True, check=False,
    )
    # ffmpeg lists devices on stderr and exits non-zero — that is expected here.
    match = re.search(r"\[(\d+)\]\s+Capture screen 0", proc.stderr)
    if not match:
        raise PipelineError(
            "No screen-capture device found. On macOS this almost always means the "
            "terminal/host app lacks Screen Recording permission: grant it under "
            "System Settings > Privacy & Security > Screen Recording, then fully quit "
            "and reopen the app (the permission only takes effect after a restart). "
            "Advanced override: set DEMO_SCREEN_DEVICE to the avfoundation index."
        )
    return match.group(1)


# --- Paths per label ---------------------------------------------------------------


_GITIGNORE_BODY = """\
# Generated demo artifacts — narration.md is the source of truth and stays tracked.
*.mov
*.mp4
*.wav
*.srt
*.png
captions/
.record.*
.permcheck.mp4
"""


def _ensure_output_gitignore() -> None:
    """Write a .gitignore in the demos output root so generated media stays out of git."""
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    gitignore = OUTPUT_ROOT / ".gitignore"
    if not gitignore.exists():
        gitignore.write_text(_GITIGNORE_BODY, encoding="utf-8")


def _label_dir(label: str) -> Path:
    """Return (creating) the working directory for a demo label."""
    d = OUTPUT_ROOT / label
    d.mkdir(parents=True, exist_ok=True)
    return d


# --- preflight ---------------------------------------------------------------------


def _scaffold_narration(label_dir: Path) -> bool:
    """Create narration.md from the template if absent.

    Args:
        label_dir: The demo's working directory.

    Returns:
        True if a fresh template was written (caller should tell the user to edit it).
    """
    target = label_dir / "narration.md"
    if target.exists():
        return False
    template = Path(__file__).resolve().parent.parent / "templates" / "narration.md"
    shutil.copyfile(template, target)
    return True


def _narration_is_unedited(label_dir: Path) -> bool:
    """Return True if narration.md is still the unedited scaffold."""
    target = label_dir / "narration.md"
    return target.exists() and _TEMPLATE_SENTINEL in target.read_text(encoding="utf-8")


def _check_screen_permission(label_dir: Path, device: str) -> str | None:
    """Probe whether screen capture yields real (non-black) frames.

    Captures a brief clip and runs blackdetect. An all-black clip usually means macOS
    Screen Recording permission has not been granted to the host application.

    Args:
        label_dir: Scratch directory for the probe clip.
        device: avfoundation screen device index.

    Returns:
        A warning string if the capture looks black, else None.
    """
    probe = label_dir / ".permcheck.mp4"
    try:
        _run([
            "ffmpeg", "-y", "-hide_banner", "-f", "avfoundation",
            "-framerate", "10", "-t", "0.6", "-i", f"{device}:none",
            "-pix_fmt", "yuv420p", str(probe),
        ])
    except PipelineError:
        return (
            "Screen capture test failed to run. Grant Screen Recording permission to "
            "your terminal app in System Settings > Privacy & Security."
        )
    black = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", str(probe),
         "-vf", "blackdetect=d=0.5:pix_th=0.10", "-f", "null", "-"],
        capture_output=True, text=True, check=False,
    )
    probe.unlink(missing_ok=True)
    if "black_start:0" in black.stderr:
        return (
            "Recording looks all-black — likely missing Screen Recording permission "
            "(System Settings > Privacy & Security > Screen Recording). Ignore if your "
            "screen is genuinely dark right now."
        )
    return None


def cmd_preflight(label: str) -> int:
    """Verify the pipeline can run and scaffold this label's narration file.

    Args:
        label: Demo label (e.g. ``mvp``, ``final``).

    Returns:
        Process exit code (0 ready, 1 blocked).
    """
    label_dir = _label_dir(label)
    _ensure_output_gitignore()
    blockers: list[str] = []
    warnings: list[str] = []

    for tool in ("ffmpeg", "ffprobe", "whisper-cli"):
        if shutil.which(tool) is None:
            blockers.append(f"`{tool}` not on PATH — run the skill's setup.sh.")

    for model in (KOKORO_MODEL, KOKORO_VOICES, WHISPER_MODEL):
        if not model.exists():
            blockers.append(f"Model file missing: {model} — run setup.sh.")

    try:
        import kokoro_onnx  # noqa: F401
    except ImportError:
        blockers.append("kokoro-onnx not importable — run setup.sh in the skill venv.")

    device: str | None = None
    if shutil.which("ffmpeg"):
        try:
            device = detect_screen_device()
        except PipelineError as exc:
            blockers.append(str(exc))

    # OpenEMR reachability (warning, not blocker — some demos may target another host).
    reachable = subprocess.run(
        ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-m", "4", OPENEMR_URL],
        capture_output=True, text=True, check=False,
    )
    if reachable.stdout.strip()[:1] not in {"2", "3"}:
        warnings.append(f"{OPENEMR_URL} not reachable — is the OpenEMR stack up?")

    if device and not blockers:
        perm = _check_screen_permission(label_dir, device)
        if perm:
            warnings.append(perm)

    fresh = _scaffold_narration(label_dir)
    if fresh:
        warnings.append(
            f"Scaffolded {label_dir / 'narration.md'} — edit it before recording."
        )
    elif _narration_is_unedited(label_dir):
        warnings.append(
            f"{label_dir / 'narration.md'} is still the unedited template — fill it in."
        )

    print(f"Preflight for '{label}' (output dir: {label_dir})")
    if device:
        print(f"  screen device: {device}")
    for w in warnings:
        print(f"  [warn] {w}")
    for b in blockers:
        print(f"  [BLOCK] {b}")
    if blockers:
        print("Preflight: BLOCKED")
        return 1
    print("Preflight: READY")
    return 0


# --- record ------------------------------------------------------------------------


def _pid_file(label_dir: Path) -> Path:
    return label_dir / ".record.pid"


def cmd_record_start(label: str) -> int:
    """Start a detached ffmpeg screen recording that survives this process exiting.

    Args:
        label: Demo label.

    Returns:
        Process exit code.
    """
    label_dir = _label_dir(label)
    pid_file = _pid_file(label_dir)
    if pid_file.exists():
        raise PipelineError(
            f"A recording is already active for '{label}' (pid file {pid_file}). "
            "Stop it first with: record stop."
        )

    device = detect_screen_device()
    raw = label_dir / "raw.mov"
    log = label_dir / ".record.log"

    cmd = [
        "ffmpeg", "-y", "-hide_banner",
        "-f", "avfoundation", "-capture_cursor", "1", "-framerate", "30",
        "-i", f"{device}:none",
        "-c:v", "libx264", "-preset", "ultrafast", "-crf", "18",
        "-pix_fmt", "yuv420p", str(raw),
    ]
    with log.open("w", encoding="utf-8") as log_fh:
        # Detached: new session, no stdin, so it keeps recording after this returns.
        proc = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=log_fh, stderr=log_fh,
            start_new_session=True,
        )
    pid_file.write_text(str(proc.pid), encoding="utf-8")
    print(f"Recording started (pid {proc.pid}) -> {raw}")
    print("Drive the browser walkthrough now, then run: record stop.")
    return 0


def cmd_record_stop(label: str) -> int:
    """Stop the detached recording via SIGINT so ffmpeg finalizes the file.

    Args:
        label: Demo label.

    Returns:
        Process exit code.

    Raises:
        PipelineError: If no active recording, or the output is unusable.
    """
    label_dir = _label_dir(label)
    pid_file = _pid_file(label_dir)
    if not pid_file.exists():
        raise PipelineError(f"No active recording for '{label}'.")

    pid = int(pid_file.read_text(encoding="utf-8").strip())
    try:
        # SIGINT (not KILL) so ffmpeg writes the container trailer and the file is valid.
        os.kill(pid, signal.SIGINT)
    except ProcessLookupError:
        pass  # already gone; fall through to validation

    for _ in range(100):  # up to ~10s for ffmpeg to flush and exit
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    pid_file.unlink(missing_ok=True)

    raw = label_dir / "raw.mov"
    if not raw.exists() or raw.stat().st_size < 1_000:
        raise PipelineError(f"Recording {raw} is missing or empty — capture failed.")
    # Validate via ffprobe, not byte size: a legitimately dark screen compresses tiny.
    duration = _ffprobe_duration(raw)
    if duration < 0.5:
        raise PipelineError(f"Recording {raw} is too short ({duration:.2f}s) — capture failed.")
    print(f"Recording stopped: {raw} ({duration:.1f}s)")
    return 0


# --- build -------------------------------------------------------------------------


def _synthesize_voiceover(label_dir: Path) -> tuple[Path, float]:
    """Render narration.md to a voiceover WAV via the configured TTS engine.

    Args:
        label_dir: The demo's working directory.

    Returns:
        (voiceover wav path, duration in seconds).

    Raises:
        PipelineError: If narration.md is missing/empty or still the template.
    """
    narration = label_dir / "narration.md"
    if not narration.exists():
        raise PipelineError(f"{narration} not found — run preflight first.")
    if _TEMPLATE_SENTINEL in narration.read_text(encoding="utf-8"):
        raise PipelineError(f"{narration} is still the unedited template — fill it in.")

    beats = tts.parse_narration(narration.read_text(encoding="utf-8"))
    if not beats:
        raise PipelineError(f"{narration} has no spoken prose — nothing to narrate.")

    engine = tts.get_engine(
        TTS_ENGINE,
        kokoro_model=KOKORO_MODEL,
        kokoro_voices=KOKORO_VOICES,
        voice=TTS_VOICE,
        speed=TTS_SPEED,
    )
    out = label_dir / "voiceover.wav"
    duration = engine.synthesize(beats, out)
    print(f"  voiceover: {out.name} ({duration:.1f}s, {len(beats)} beats, {TTS_ENGINE})")
    return out, duration


def _generate_subtitles(label_dir: Path, voiceover: Path) -> Path:
    """Transcribe the voiceover to a timed SRT with whisper.cpp.

    whisper.cpp requires 16 kHz mono input, so the voiceover is downsampled first.

    Args:
        label_dir: The demo's working directory.
        voiceover: The synthesized voiceover WAV.

    Returns:
        Path to the generated ``subs.srt``.
    """
    wav16 = label_dir / "voiceover.16k.wav"
    _run(["ffmpeg", "-y", "-hide_banner", "-i", str(voiceover),
          "-ar", "16000", "-ac", "1", str(wav16)])
    # whisper-cli writes "<-of>.srt".
    _run(["whisper-cli", "-m", str(WHISPER_MODEL), "-f", str(wav16),
          "-l", "en", "-osrt", "-of", str(label_dir / "subs"), "-np"])
    wav16.unlink(missing_ok=True)
    srt = label_dir / "subs.srt"
    if not srt.exists():
        raise PipelineError("Whisper did not produce subs.srt.")
    print(f"  subtitles: {srt.name}")
    return srt


def _assemble(label_dir: Path, audio_dur: float) -> Path:
    """Assemble the final MP4: fit footage to voiceover, scale, overlay captions, mux.

    Continuous-track model: the video is fit to the voiceover length — shorter footage
    holds its last frame; longer footage is trimmed. Captions are Pillow-rendered PNG
    strips composited via ffmpeg ``overlay`` (this ffmpeg has no text filters). Run from
    ``label_dir`` so caption inputs can be referenced relatively.

    Args:
        label_dir: The demo's working directory (used as ffmpeg cwd).
        audio_dur: Voiceover duration in seconds (the target output length).

    Returns:
        Path to the final ``demo.mp4``.
    """
    raw = label_dir / "raw.mov"
    video_dur = _ffprobe_duration(raw)
    src_w, src_h = _ffprobe_dimensions(raw)
    out_w = round(src_w * OUTPUT_HEIGHT / src_h)
    out_w += out_w % 2  # libx264 needs even dimensions

    srt = label_dir / "subs.srt"
    caps = captions.parse_srt(srt) if srt.exists() else []
    strips = captions.render_caption_strips(caps, out_w, label_dir) if caps else []

    # Inputs: 0=footage, 1=voiceover, then one looped PNG per caption cue.
    inputs = ["-i", "raw.mov", "-i", "voiceover.wav"]
    for png, _ in strips:
        inputs += ["-loop", "1", "-i", str(png.relative_to(label_dir))]

    base_ops: list[str] = []
    if video_dur < audio_dur:  # hold last frame to reach the narration length
        base_ops.append(f"tpad=stop_mode=clone:stop_duration={audio_dur - video_dur:.3f}")
    base_ops.append(f"scale=-2:{OUTPUT_HEIGHT}")
    graph = [f"[0:v]{','.join(base_ops)}[base]"]

    prev = "base"
    for i, (_, cap) in enumerate(strips):
        out_label = f"o{i}"
        # y=H-h anchors each strip flush to the frame bottom; enable gates its window.
        graph.append(
            f"[{prev}][{i + 2}:v]overlay=x=0:y=H-h:"
            f"enable='between(t,{cap.start:.3f},{cap.end:.3f})'[{out_label}]"
        )
        prev = out_label

    out = label_dir / "demo.mp4"
    _run(
        [
            "ffmpeg", "-y", "-hide_banner", *inputs,
            "-filter_complex", ";".join(graph),
            "-map", f"[{prev}]", "-map", "1:a",
            "-t", f"{audio_dur:.3f}",  # trims footage longer than the voiceover
            "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart", "demo.mp4",
        ],
        cwd=label_dir,
    )
    print(f"  assembled: {out} ({len(strips)} captions)")
    return out


def cmd_build(label: str) -> int:
    """Run the full narrate -> subtitle -> assemble build for a recorded demo.

    Args:
        label: Demo label.

    Returns:
        Process exit code.

    Raises:
        PipelineError: If the recording is missing or any stage fails.
    """
    label_dir = _label_dir(label)
    raw = label_dir / "raw.mov"
    if not raw.exists():
        raise PipelineError(f"No recording at {raw} — record the walkthrough first.")

    print(f"Building demo '{label}':")
    voiceover, audio_dur = _synthesize_voiceover(label_dir)
    _generate_subtitles(label_dir, voiceover)
    final = _assemble(label_dir, audio_dur)

    final_dur = _ffprobe_duration(final)
    print(f"Done: {final} ({final_dur:.0f}s)")
    if not (TARGET_MIN_S <= final_dur <= TARGET_MAX_S):
        print(
            f"  [note] {final_dur:.0f}s is outside the 3-5 min target — "
            "adjust narration.md length."
        )
    return 0


# --- CLI ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """Entry point: dispatch a subcommand.

    Args:
        argv: Argument vector (defaults to ``sys.argv[1:]``).

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(prog="demo", description="Weekly demo-video pipeline.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_pre = sub.add_parser("preflight", help="Verify tooling and scaffold narration.md.")
    p_pre.add_argument("label")

    p_rec = sub.add_parser("record", help="Start/stop screen recording.")
    p_rec.add_argument("label")
    p_rec.add_argument("action", choices=["start", "stop"])

    p_build = sub.add_parser("build", help="Narrate, subtitle, and assemble demo.mp4.")
    p_build.add_argument("label")

    args = parser.parse_args(argv)

    try:
        if args.command == "preflight":
            return cmd_preflight(args.label)
        if args.command == "record":
            return cmd_record_start(args.label) if args.action == "start" \
                else cmd_record_stop(args.label)
        if args.command == "build":
            return cmd_build(args.label)
    except PipelineError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
