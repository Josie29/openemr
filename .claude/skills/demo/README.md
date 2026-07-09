# /demo — weekly demo video pipeline

Generates a narrated, subtitled 3–5 min MP4 for a project submission by driving a live
browser walkthrough, recording the screen, and assembling a local voiceover + subtitles.
Fully local and free: ffmpeg + Kokoro (TTS) + Whisper (subtitles). No cloud, no API keys.

Design contract: [`context/specs/demo-pipeline.md`](../../../context/specs/demo-pipeline.md).

## First-time setup

```bash
bash .claude/skills/demo/setup.sh
```

Installs `ffmpeg` + `whisper-cpp` (Homebrew), a dedicated Python venv with
`kokoro-onnx`/`soundfile`/`pillow`, and downloads the Kokoro + Whisper model files to
`~/.cache/demo-skill/`. Idempotent — safe to re-run.

**macOS Screen Recording permission (required, one-time):** the pipeline captures the
screen via ffmpeg avfoundation, which needs Screen Recording permission for the **app
hosting Claude Code** — i.e. your editor/terminal (e.g. **Visual Studio Code**, iTerm,
Terminal), **not** the standalone Claude desktop app. Grant it under **System Settings →
Privacy & Security → Screen Recording**, then **fully quit and reopen** that app (it only
takes effect after a restart). Without it, avfoundation won't even list the screen and
`preflight` will block — `preflight` auto-detects and names the exact app to grant.

## Usage

Invoke the skill: `/demo <label>` (e.g. `/demo mvp`, `/demo final`, `/demo week3-early`).
The skill orchestrates the CLI below; see [`SKILL.md`](SKILL.md) for the exact flow.

Manual invocation of the CLI (what the skill runs under the hood):

```bash
PY=~/.cache/demo-skill/venv/bin/python
CLI=.claude/skills/demo/pipeline/demo.py

$PY $CLI preflight <label>       # verify tooling/permission, scaffold narration.md
$PY $CLI record  <label> start   # begin detached screen capture
#   ... drive the browser walkthrough (the skill does this via the Chrome MCP) ...
$PY $CLI record  <label> stop    # finalize raw.mov
$PY $CLI build   <label>         # voiceover + subtitles + assemble demo.mp4
```

Outputs land in `./context/demos/<label>/` (override with `DEMO_OUTPUT_ROOT`):
`narration.md` (the script you edit) and `demo.mp4` (the deliverable). Generated media is
git-ignored; `narration.md` is the tracked source of truth.

## The narration file

`narration.md` is the spine. Prose is **spoken**; lines starting with `On screen:` are the
**walkthrough instructions** (not spoken); headers are structure (not spoken). Lead with
decisions (problem → what you built → why), not a click-by-click tour. Aim ~450–650 words
for a 3–5 min video. The video is fit to the voiceover length: shorter footage holds its
last frame, longer footage is trimmed.

## Iterating

- Changed only the words? Edit `narration.md` and re-run `build` — no re-record needed.
- Changed the on-screen flow? Re-record (`record start` → drive → `record stop`) then `build`.

## Configuration (environment variables)

| Var | Default | Purpose |
|-----|---------|---------|
| `DEMO_OUTPUT_ROOT` | `./context/demos` | Where per-label folders are written. |
| `DEMO_OPENEMR_URL` | `http://localhost:8300` | Reachability check target in preflight. |
| `DEMO_TTS_ENGINE` | `kokoro` | TTS engine (swap seam — add cloud engines in `pipeline/tts.py`). |
| `DEMO_VOICE` | `af_sarah` | Kokoro voice id (e.g. `af_heart`, `am_michael`). |
| `DEMO_SPEED` | `1.0` | Speech-rate multiplier. |
| `DEMO_SCREEN_DEVICE` | auto-detected | avfoundation screen index override. |

## Promoting to a global skill

The pipeline is project-agnostic (only `narration.md` is per-project). To reuse it across
projects, move `.claude/skills/demo/` to `~/.claude/skills/demo/`; the venv/models already
live in `~/.cache/demo-skill/`, and outputs follow the caller's cwd. No code change needed.

## Known limitations

- macOS only (avfoundation capture, system font path).
- ffmpeg from Homebrew ships without libass/libfreetype, so captions are Pillow-rendered
  PNG strips composited via `overlay` rather than a `subtitles` filter.
- Loose audio/video sync by design (continuous-track model). Precise per-step sync is a
  documented future upgrade in the spec.
