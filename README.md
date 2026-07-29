<h1 align="center">video-agent</h1>

<p align="center"><strong>Give Claude Code the ability to actually edit video.</strong></p>

<p align="center">
  <img alt="Python 3.11+" src="https://img.shields.io/badge/python-3.11%2B-blue">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="Local first" src="https://img.shields.io/badge/core-local%20%C2%B7%20no%20API%20key-brightgreen">
  <img alt="ffmpeg" src="https://img.shields.io/badge/powered%20by-ffmpeg-orange">
</p>

<p align="center">
  <img src="docs/demo.gif" alt="A 30-second trailer cut automatically from a 10-minute source video" width="700">
</p>

<p align="center">
  <em>Not a mockup — 12s of a 30s trailer Claude cut from a 10-minute source, 18 shots on the music bed's beat.<br>
  <a href="examples/bbb-trailer.json">Read the edit list it wrote, cut by cut →</a></em>
</p>

Point Claude Code at raw footage and say *"cut this into a 30-second trailer."* It watches the
video, transcribes it, writes a cut list with a **written reason for every cut**, renders it,
then re-watches its own output to check the result.

**Fully local, no API key** — 21 frame-accurate ffmpeg commands. Generative VFX is one
optional cloud command; everything else runs on your machine.

---

## Why not just call ffmpeg?

An LLM with a shell already can, and still makes bad videos — knowing the command isn't the
hard part. Two things are:

**Time that doesn't lie.** Video cuts only on frame boundaries, audio cuts anywhere, so a naive
filtergraph splits picture from sound by up to a frame per cut and drifts visibly over 40 of
them. Every command quantizes to the frame grid, and `transcribe` (*what* was said, ±0.3s
jitter) stays separate from `speech-segments` (*where* sound actually starts, frame-accurate)
so cuts land on the second one.

**Skills that carry the craft.** [`.claude/skills/`](.claude/skills/) holds 12 procedures
Claude loads exactly when they're relevant — where a cut belongs relative to a breath, how long
to hold a shot, why cutting picture and sound on the same frame reads as robotic. An accurate
cut and a good cut are different things.

---

## Quickstart

```bash
mise install ffmpeg        # or: brew install ffmpeg
git clone https://github.com/agamm/video-agent && cd video-agent
uv sync                    # editing core — no API key needed
```

Open Claude Code in the repo and talk to it:

> *"Cut this into a 30-second trailer with music and captions."*
>
> *"Remove every um and uh from this talking head, then normalize the audio."*
>
> *"Make this vertical for Reels, keeping the speaker in frame the whole time."*

Claude reads [`CLAUDE.md`](CLAUDE.md) and invokes whichever skill fits. Every command also
works standalone.

---

## What it produces

A reviewable **edit decision list** — every clip carries why it's there and why it's that long,
so you can argue with the edit before rendering and diff it after:

```jsonc
{
  "fps": 24, "width": 1920, "height": 1080,
  "music": { "src": "bed.wav", "gain_db": -1.0 },
  "clips": [
    { "src": "source.mp4", "start": 5.4, "end": 8.4,
      "rationale": "ACT 1 / establishing. Longest shot in the cut (3.0s) so the trailer
                    opens calm and wide. Sets the world that gets wrecked." },
    { "src": "source.mp4", "start": 44.9, "end": 47.4, "punch": [1.0, 1.05],
      "rationale": "Hero reveal. Slow 5% push draws the eye in as the face resolves." }
  ]
}
```

Render it — and let the edit critique its own pacing first:

```bash
uv run video-agent edl examples/bbb-trailer.json -o out.mp4 --report --dry-run
```

```
  #  source                       in      out     dur   lead  rhythm
  0  Big Buck Bunny.mp4         5.40     8.40    3.00      ·  ██████████████████████████████
  1  Big Buck Bunny.mp4        12.40    14.98    2.58      ·  ██████████████████████████
 ...
 11  Big Buck Bunny.mp4       420.90   421.61    0.71      ·  ███████

18 clips · 30.0s · mean 1.67s · median 1.46s · range 0.71–3.00s · variation 40%
  ⚠ no split edits — every cut changes picture and sound on the same frame, which is the
    main thing that makes a cut feel abrupt; add `audio_lead` on the cuts that should flow
```

That warning is the point. The cut is *correct* — frame-accurate, on the beat — and would
still watch robotically. `--report` catches the three things that make an accurate edit feel
wrong: uniform shot length, cuts that move picture and sound together, and full-length pauses.

---

## Skills

| Skill | What it knows |
|---|---|
| **`editor`** | The director — reads the footage, picks a genre treatment (montage, doc, tutorial, vlog, trailer, explainer), composes everything below into a plan. **Start here.** |
| **`edl-edit`** | Multi-clip assembly as auditable JSON — schema, rationale discipline, multicam cutaways, verifying by re-transcribing the render |
| **`cutting-rhythm`** | Why a correct edit still watches badly: J/L split edits, pause length, shot variety, cutting on the beat |
| **`filler-removal`** | Cutting um/uh so it sounds like they were never said |
| **`captions`** | Burned-in subtitles or `.srt`/`.vtt` export, styling, the verbatim-vs-clean gotcha |
| **`audio-edit`** | Loudness normalization, denoising, music beds ducked under speech |
| **`color-grade`** | Applying `.cube`/HALD LUTs, baking a look, rendering candidates to choose from |
| **`video-overlay`** | Text, logos and graphics — including locking one to a moving face or eye |
| **`video-transitions`** | Wipes, slides, dissolves, pixelize, circle open/close via `xfade` |
| **`reframe-social`** | 9:16 / 1:1 / 4:5 — crop vs. pad, following an off-center subject |
| **`remotion-graphics`** | *Optional.* Real motion design (kinetic captions, lower-thirds) via React → composite |
| **`grok-video-edit`** | *Optional.* Generative edits — recolor, add smoke/fire, restyle |

---

## Commands

Positions accept **frames** (`90`), **seconds** (`3.0`), or **timestamps** (`00:01:30`).
A bare integer is a frame, not a second — `--start 120` means frame 120.

```bash
# understand the footage
uv run video-agent info       video.mp4
uv run video-agent transcribe video.mp4 [--words] [--clean] [--srt|--vtt] [-o out.txt]
uv run video-agent speech-segments video.mp4          # speech/silence spans = cut points
uv run video-agent detect     video.mp4 --start 0.0 --end 60.0 -o grids/
uv run video-agent position-grid video.mp4 --at 14.2 --spacing 200 -o grid.png
uv run video-agent beats      music.mp3 [--onsets] [-o beats.txt]

# cut and assemble
uv run video-agent yt-dl      "https://youtube.com/watch?v=..." -o inputs/
uv run video-agent trim       video.mp4 --start 10.0 --end 30.0 -o clip.mp4
uv run video-agent frame      video.mp4 --at 45.5 -o frame.png
uv run video-agent concat     a.mp4 b.mp4 c.mp4 -o out.mp4
uv run video-agent splice     a.mp4 b.mp4 -o out.mp4  # audio+video crossfade
uv run video-agent edl        edit.json -o out.mp4 [--report] [--dry-run] [--draft]
uv run video-agent tighten    talk.mp4 -o edit.json --target-gap 0.5 --min-gap 1.0
uv run video-agent snap       edit.json -o snapped.json --to silence|beats [--ref music.mp3]

# finish
uv run video-agent captions   video.mp4 -o out.mp4 --clean        # or --srt subs.srt
uv run video-agent reframe    video.mp4 -o reel.mp4 --aspect 9:16 [--mode crop|pad] [--focus 0.4]
uv run video-agent grade apply   in.mp4 --lut luts/warm.png -o out.mp4
uv run video-agent grade preview in.mp4 --at 45.0 --lut none --lut a.cube -o looks.png
uv run video-agent overlay-text  in.mp4 --text "BOOM!" --x center --y center -o out.mp4
uv run video-agent overlay-texts in.mp4 --items "3:5:6" "2:6:7" "1:7:8" -o out.mp4
uv run video-agent overlay-image in.mp4 --image logo.png --x "W-w-20" --y 20 -o out.mp4
uv run video-agent overlay-gif   in.mp4 --gif fx.gif --x 100 --y 100 -o out.mp4
```

**Visual search:** Claude can't scrub a timeline, so `detect` turns a time range into labeled
grid montages plus a `mapping.json` converting any cell back to an exact frame. Go coarse to
find the region, then `--step 1 --cell-w 384` to nail the frame. It decodes sequentially, so it
never hits the keyframe-seek error that makes single-frame extraction lie on B-frame footage.

**As a library:** `import video_agent as va` → `va.video_info()`, `va.trim()`, `va.detect()`,
`va.extract_frame()`, `va.concat()`, `va.to_seconds()`, `va.to_frame()`.

---

## Optional extras

Both opt-in; the editing core never touches them.

**Generative VFX** — reimagine footage ffmpeg can't. Downscales, splits into ≤8s chunks, sends
each as an inline base64 data URL, reassembles, restores the original audio. `--backend` is
provider-agnostic (Grok today). Results vary run-to-run — prefer the deterministic commands
when they can do the job.

```bash
uv sync --extra vfx && echo 'XAI_API_KEY=xai-...' >> .env   # console.x.ai
uv run video-agent vfx-edit clip.mp4 --prompt "Make the butterfly a ladybug" -o out.mp4
uv run video-agent vfx-edit clip.mp4 --prompt "Add a cartoon explosion" \
    --splice-into source.mp4 --splice-start 83.0 --splice-end 86.0 -o final.mp4
```

**Animated motion graphics** — React/Remotion → transparent layer → ffmpeg composite. Heavy
(Node + Chromium download, Remotion company license for commercial use). Scaffold in
[`remotion/`](remotion/); for a static label use `video-overlay` instead.

---

## Requirements

| | |
|---|---|
| **Python** | 3.11+ with [uv](https://docs.astral.sh/uv/) |
| **ffmpeg** | `ffmpeg` + `ffprobe` on PATH |
| **macOS** | for hardware encoding — set `VCODEC = "libx264"` in `video_agent.py` on Linux |
| **Apple Silicon** | for `transcribe` (mlx-whisper; ~1.5GB model on first use) |
| **API key** | *only* for the optional `vfx-edit` |

Two build notes: this ffmpeg is LGPL, so `eq` and `drawtext` are unavailable — text renders via
PIL, grading goes through curves/LUTs. And AV1 sources (common from YouTube) decode via
PyAV/libdav1d, because ffmpeg's AV1 decoder is broken on many builds.

---

## Privacy

Your footage stays on your machine. `inputs/`, `outputs/`, `grids/`, every media extension, and
transcript sidecars (`.srt`/`.vtt` contain everything said on camera) are gitignored by
default. The only network calls the core makes are `yt-dl` fetching a URL you gave it and the
one-time Whisper model download.

---

MIT — see [LICENSE](LICENSE). Demo cut from
[Big Buck Bunny](https://peach.blender.org/), © 2008 Blender Foundation,
[CC BY 3.0](https://creativecommons.org/licenses/by/3.0/) — a public source, so you can
reproduce that trailer from [`examples/bbb-trailer.json`](examples/bbb-trailer.json) yourself.
