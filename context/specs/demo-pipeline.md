# Design Spec — `/demo` Pipeline: Weekly Demo Video Generator

> Status: agreed design, pre-implementation. Source of truth for the `.claude/skills/demo/`
> skill. An implementation prompt can be derived from it.
>
> Backs the PRD **Demo Video (3–5 min)** submission requirement (`PRD.md` §Submission
> Requirements), due per milestone — MVP (Tue), Early (Thu), Final (Sun) — so the pipeline
> is exercised multiple times per week.

## 1. Goal

A reusable, project-level `/demo` skill that produces a **3–5 minute narrated, subtitled
MP4** showcasing that week's AgentForge Clinical Co-Pilot work — the product running live,
plus the **key decisions** behind it — regenerable by editing a narration file and running
`/demo`, with **no manual video editing**.

**One-liner:** edit `narration.md` → run `/demo` → get a submission-ready `demo.mp4`.

The video is a **narrated argument over B-roll**: narration is the spine (it carries the
"key decisions" the PRD grades), and the live browser walkthrough illustrates it. It is
closer to a voiceover essay than a screen tutorial.

## 2. Scope

### In scope

- Drive a live local OpenEMR + Co-Pilot walkthrough via the **Claude-in-Chrome MCP**
  (agentic — the flow is described in prose per week, not hardcoded).
- Screen-record the walkthrough with local **ffmpeg**.
- Generate voiceover from `narration.md` with local **Kokoro** TTS.
- Time subtitles with local **Whisper**; burn them into the video.
- Assemble everything into a single MP4 with ffmpeg.

### Out of scope (this version)

- Cinematic polish (auto-zoom on clicks, cursor smoothing, transitions). "Clear is enough."
- Precise per-step audio/video sync (see §5 — deliberately chosen against).
- Cloud/paid TTS (Kokoro is default; the engine is swappable — see §6).
- Webcam / talking-head overlay.
- Uploading/submitting the video or the deployed-app URL (done by hand).

## 3. Weekly workflow (what the user does)

Each week, for each submission that needs a video:

1. Edit `context/demos/weekN/narration.md` — the decisions-led script (template in §7).
2. Ensure the OpenEMR stack (and, if the demo shows it, the agent stack) is up.
3. Run `/demo weekN`.
4. Review `context/demos/weekN/demo.mp4`; submit it by hand.

The **skill** (record → TTS → subtitle → assemble machinery) is the reusable asset and
does not change week to week. Only `narration.md` changes.

## 4. Constraints

- **Platform:** macOS. Local toolchain only: ffmpeg, Kokoro, Whisper. No SaaS, no API keys,
  no cloud dependency.
- **Narration is authored before the run** and drives everything; footage supports it.
- **Record-first, narrate-to-footage** — voiceover is generated *after* the drive completes,
  never synced live during the agentic walkthrough.
- **Output:** 1080p H.264 MP4, target 3–5 min (PRD requirement).
- **Layout:** skill at `.claude/skills/demo/`; per-week inputs/outputs under
  `context/demos/weekN/`. Follows the CLAUDE.md rule that our working docs live in
  `/context/`.
- **No new project runtime deps** — the pipeline is host-side tooling, not OpenEMR code, so
  it does not touch composer/npm or the PHP module.

## 5. Architecture — Continuous-track assembly

Chosen over per-step manifest syncing because the agentic drive is non-deterministic
(the path varies run-to-run, which we accepted), and "clear is enough" does not justify the
fragile shared-clock, freeze-frame-padding machinery precise sync would require. Precise
per-step sync is a documented **future upgrade** (§10), not this version.

Pipeline stages:

1. **Pre-flight** — verify, and halt with a clear message on any failure:
   - macOS Screen Recording permission is granted (else recording is a black video).
   - OpenEMR reachable at `http://localhost:8300` (and agent stack, if the week's demo
     uses it).
   - Kokoro + Whisper + ffmpeg installed (first-run setup offered — see §9).
2. **Record** — start ffmpeg screen capture (Chrome window region preferred over full
   screen, to keep other windows/notifications out), then drive the Co-Pilot walkthrough
   via Claude-in-Chrome per `narration.md`. Stop capture when the flow ends. Produces
   `raw.mov` (video only, no audio).
3. **Trim** — ffmpeg silence/idle-gap tightening pass on `raw.mov` to remove long dead
   stretches. Produces `trimmed.mp4`.
4. **Narrate** — Kokoro renders `narration.md` to `voiceover.wav` (one continuous track).
5. **Subtitle** — Whisper transcribes `voiceover.wav` to a timed `subs.srt`.
6. **Assemble** — ffmpeg lays `voiceover.wav` over `trimmed.mp4`, burns `subs.srt`, fits
   the result toward the 3–5 min window, exports `demo.mp4`.

Sync is **loose by design**: the voiceover plays continuously over the trimmed footage; a
beat may land slightly before/after its moment on screen. Acceptable for "clear."

## 6. TTS engine seam

Kokoro is the default, but the TTS call is isolated behind a single function/interface so a
higher-quality engine (ElevenLabs, OpenAI TTS) is a **one-line swap** for a graded final
where production value matters more than the ~$0.25 cost.

```
tts_engine = kokoro   # default: local, free, offline
# swap to elevenlabs / openai for graded finals
```

The seam takes narration text → returns a WAV path. No other stage knows which engine ran.

## 7. `narration.md` template (decisions-led)

Structured so the 3–5 min video carries **key decisions**, not just clicks. Each beat is a
decision or capability, phrased problem → what we built → why:

```markdown
# Week N Demo — <feature/theme>

## Intro (~20s)
<What this week's increment is, in one breath. Name the clinical problem.>

## Beat 1 — <decision or capability>
<Problem it addresses. What we built. Why this approach over alternatives.>
<On screen: what the walkthrough shows here.>

## Beat 2 — ...

## Close (~15s)
<What's next / where this sits in the roadmap.>
```

The `On screen:` lines are the instructions the agentic driver follows; the prose above
them is what Kokoro speaks.

## 8. Success criteria

A single `/demo weekN` run yields `context/demos/weekN/demo.mp4` that:

- Plays end to end; runs **3–5 min**.
- Has intelligible Kokoro voiceover, reasonably (loosely) aligned to on-screen action.
- Has burned-in subtitles matching the voiceover.
- Has no long dead gaps (trim pass did its job).
- Is 1080p H.264 MP4.
- Conveys the week's **key decisions**, not only UI clicks (narration quality — human check).

Simplest proof it works: run it against a throwaway one-beat `narration.md` and confirm a
playable, narrated, subtitled MP4 comes out.

## 9. Edge cases & failure modes

| Condition | Behavior |
|---|---|
| Screen Recording permission missing | Pre-flight detects, halts, tells user to grant it in System Settings. Never produces a silent black video. |
| OpenEMR / agent stack down | Pre-flight halts before recording. |
| Login expired mid-drive | Driver reports the broken step; user can re-run the record stage without redoing setup. |
| Kokoro / Whisper / ffmpeg not installed | First-run setup step installs them (Homebrew/pip); documented in SKILL.md. |
| Voiceover longer than footage | Footage tail holds last frame to cover the remaining narration. |
| Voiceover shorter than footage | Trim/speed the tail so video ends near narration end (stay in 3–5 min). |
| Notifications / other windows leak in | Record the Chrome window region, not full screen; advise enabling Do Not Disturb. |
| Re-run of same week | Outputs are per-week folders; warn before overwriting an existing `demo.mp4`. |

## 10. Future upgrades (explicitly deferred)

- **Manifest-synced assembly** — driver emits `{step-label, timestamp}`, narration beats
  key to labels, per-step segmentation with freeze-frame padding for precise sync.
- **Cinematic polish** — Screen Studio capture for auto-zoom/cursor, with this pipeline
  handling voiceover + subtitles + edit.
- **Higher-quality TTS** — flip the §6 seam to ElevenLabs/OpenAI for graded finals.

## 11. Non-goals recap

Not building: precise sync, cinematic effects, cloud TTS, webcam overlay, auto-submission.
The deliverable is a *clear*, decisions-led, regenerable 3–5 min video — nothing more.
