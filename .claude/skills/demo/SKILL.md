---
name: demo
description: >-
  Produce a narrated, subtitled 3-5 min demo video for a project submission. Drives a
  live browser walkthrough via the Claude-in-Chrome MCP, screen-records it with ffmpeg,
  generates a local Kokoro voiceover from a narration file, times subtitles with Whisper,
  and assembles a submission-ready MP4. Use when the user asks to record, build, or
  regenerate a demo / submission video. Takes a free-form label (e.g. mvp, final, week3).
---

# /demo — weekly demo video pipeline

Design contract: `context/specs/demo-pipeline.md`. Model: **record-first,
narrate-to-footage, continuous-track** — narration is the spine; footage supports it.

Paths (this skill is self-contained; when promoted to a global skill only the venv path
below stays the same):

- Runner: `~/.cache/demo-skill/venv/bin/python`
- CLI: `<this-skill-dir>/pipeline/demo.py`
- Per-label output: `./context/demos/<label>/` (override with `DEMO_OUTPUT_ROOT`)

Define a shell alias for brevity in the steps below:
`DEMO="~/.cache/demo-skill/venv/bin/python <this-skill-dir>/pipeline/demo.py"`

## Steps

1. **Resolve the label.** From the user's request (e.g. `mvp`, `final`, `week3-early`).
   If none given, ask for one short label.

2. **Ensure setup (once).** If `~/.cache/demo-skill/venv` does not exist, run
   `bash <this-skill-dir>/setup.sh` and wait for it to finish. It is idempotent.

3. **Preflight.** Run `$DEMO preflight <label>`. It scaffolds
   `context/demos/<label>/narration.md` on first use and reports blockers/warnings.
   - If it reports **BLOCKED**, fix the blockers (usually: run setup.sh, or grant macOS
     Screen Recording permission) and re-run.
   - If `narration.md` was just scaffolded or is still the unedited template, **STOP
     here.** Tell the user to fill in `context/demos/<label>/narration.md` (prose is
     spoken; `On screen:` lines are the walkthrough script). Do not record until it is
     written.

4. **Confirm the stage is clean.** Before recording, tell the user to: put the OpenEMR
   browser window **fullscreen on the primary display**, enable **Do Not Disturb**, and
   close noisy windows — the whole primary screen is captured. Wait for their go-ahead.

5. **Start recording.** Run `$DEMO record <label> start`. It returns immediately; ffmpeg
   keeps capturing in the background.

6. **Drive the walkthrough.** Using the **Claude-in-Chrome MCP**, perform the flow
   described by the `On screen:` lines in `narration.md`, in order. Move deliberately and
   pause briefly on each meaningful state (loose sync — the voiceover is laid over the
   whole thing afterward, so exact timing is not required). Keep the total drive close to
   the intended narration length.

7. **Stop recording.** Run `$DEMO record <label> stop`. It finalizes `raw.mov`.

8. **Build.** Run `$DEMO build <label>`. This synthesizes the voiceover (Kokoro),
   generates subtitles (Whisper), and assembles `context/demos/<label>/demo.mp4`, fitting
   the footage to the voiceover length. It notes if the result falls outside 3-5 min.

9. **Deliver.** Report the path and surface the file to the user (SendUserFile) so they
   can review it. If they want changes, they edit `narration.md` and you re-run `build`
   (no re-record needed) — or re-record from step 4 if the on-screen flow changed.

## Notes

- Re-running `build` alone regenerates voiceover + subtitles + final cut from the
  existing recording — cheap. Only re-record when the on-screen flow itself changed.
- Higher-quality voice for a graded final: set `DEMO_TTS_ENGINE` once a cloud engine is
  added to `pipeline/tts.py` (the seam is already there). Default is local Kokoro.
- Override the spoken voice with `DEMO_VOICE` (e.g. `af_heart`, `am_michael`) and pace
  with `DEMO_SPEED`.
