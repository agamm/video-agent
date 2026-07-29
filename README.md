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
  <em>Not a mockup — 12 seconds of a 30s trailer Claude cut from a 10-minute source.<br>
  18 shots, every cut from the turn onward landing on the music bed's beat.<br>
  <a href="examples/bbb-trailer.json">Read the edit list it wrote, cut by cut →</a></em>
</p>

Point Claude Code at raw footage and say *"cut this into a 30-second trailer."* It watches the
video, transcribes it, picks a structure, writes a cut list with a **written reason for every
single cut**, renders it, then re-watches its own output to check the result.

The editing core is **fully local and needs no API key** — 21 frame-accurate ffmpeg commands.
Generative VFX is one optional cloud command; everything else runs on your machine.

---

## Why this is not "an ffmpeg wrapper"

An LLM with a shell can already call ffmpeg. It still makes bad videos, because knowing the
command is not the hard part. This repo ships the two things that are:

**1. Primitives that don't lie about time.** Video only cuts on frame boundaries; audio cuts
anywhere. A naive filtergraph splits picture from sound by up to a frame per cut, and across
40 cuts that becomes visible drift. Every command here quantizes to the frame grid, and
`transcribe` (*what* was said, ±0.3s jitter) is deliberately separate from `speech-segments`
(*where* sound actually starts, frame-accurate) so cuts land on the second one.

**2. Skills that carry the craft.** [`.claude/skills/`](.claude/skills/) holds 12 procedures
Claude loads exactly when they become relevant — not just which flag to pass, but *where a cut
belongs relative to a breath*, why every cut changing picture and sound on the same frame reads
as robotic, and how long to hold a shot. An accurate cut and a good cut are different things.

---

## Quickstart

```bash
mise install ffmpeg                        # or: brew install ffmpeg
git clone https://github.com/agamm/video-agent
cd video-agent
uv sync                                    # editing core — no API key needed
```

Then open Claude Code in the repo and talk to it:

> *"Download this YouTube video, cut it into a 30-second trailer with music, and add captions."*
>
> *"Remove every um and uh from this talking head, then normalize the audio."*
>
> *"Find the moment the butterfly lands and put a label on it that follows the wing."*
>
> *"Make this vertical for Reels, keeping the speaker in frame the whole time."*

Claude reads [`CLAUDE.md`](CLAUDE.md) for the command reference, then invokes whichever skill
matches the ask. Or drive the CLI yourself — every command works standalone.

---

## What the agent actually produces

Not a throwaway filtergraph — a reviewable **edit decision list**. Every clip carries the
reasoning for why it's there and why it's that long, so you can argue with the edit before
rendering it, and diff it after:

```jsonc
{
  "fps": 24, "width": 1920, "height": 1080,
  "music": { "src": "outputs/bed.wav", "gain_db": -1.0, "duck": false },
  "clips": [
    {
      "src": "inputs/source.mp4", "start": 5.4, "end": 8.4,
      "rationale": "ACT 1 / establishing. Pastel dawn meadow — the longest shot in the cut
                    (3.0s) so the trailer opens calm and wide. Sets the world that gets wrecked."
    },
    {
      "src": "inputs/source.mp4", "start": 44.9, "end": 47.4,
      "punch": [1.0, 1.05],
      "rationale": "ACT 1 / hero reveal. Slow 5% push draws the eye into the shadow as the
                    face resolves."
    }
  ]
}
```

Then render it — and have the edit critique its own pacing before you commit to a render:

```bash
uv run video-agent edl examples/bbb-trailer.json -o out.mp4 --report --dry-run
```

```
  #  source                       in      out     dur   lead  rhythm
  0  Big Buck Bunny.mp4         5.40     8.40    3.00      ·  ██████████████████████████████
  1  Big Buck Bunny.mp4        12.40    14.98    2.58      ·  ██████████████████████████
  2  Big Buck Bunny.mp4        44.90    47.40    2.50      ·  █████████████████████████
  3  Big Buck Bunny.mp4        61.00    63.21    2.21      ·  ██████████████████████
 ...
 10  Big Buck Bunny.mp4       394.90   395.65    0.75      ·  ████████
 11  Big Buck Bunny.mp4       420.90   421.61    0.71      ·  ███████
 12  Big Buck Bunny.mp4       448.95   449.66    0.71      ·  ███████

18 clips · 30.0s · mean 1.67s · median 1.46s · range 0.71–3.00s · variation 40%
longest hold: clip 0 (3.00s)
  ⚠ no split edits — every cut changes picture and sound on the same frame, which is the
    main thing that makes a cut feel abrupt; add `audio_lead` on the cuts that should flow
```

That warning is the whole point. The cut above is *correct* — every in/out is frame-accurate
and on the beat — and it would still watch slightly robotically, because nothing tells you
that except an editor's eye. `--report` catches the three things that make an accurate edit
feel wrong: every shot the same length, every cut changing picture and sound on the same
frame, and pauses left at full length. One re-encode then covers the cuts, the grade, and the
music bed together.

---

## Skills

Deep procedures live in [`.claude/skills/`](.claude/skills/), so the gotchas surface at the
moment they matter instead of sitting in a wiki nobody opens.

| Skill | What it knows |
|---|---|
| **`editor`** | The director. Understands raw footage, picks a genre treatment (montage, documentary, tutorial, news, vlog, trailer, explainer), and composes every skill below into an ordered plan. **Start here.** |
| **`edl-edit`** | Multi-clip assembly as auditable JSON — schema, rationale discipline, multicam cutaways, verifying by re-transcribing the render |
| **`cutting-rhythm`** | Why a correct edit still watches badly: J/L split edits, pause length, shot-length variety, cutting on the beat |
| **`filler-removal`** | Cutting um/uh so it sounds like they were never said — the verbatim-transcription trick and the iterate-until-clean loop |
| **`captions`** | Burned-in subtitles or `.srt`/`.vtt` export, styling, and the verbatim-vs-clean gotcha |
| **`audio-edit`** | Loudness normalization, denoising hiss/hum, music beds with ducking under speech |
| **`color-grade`** | Applying `.cube`/HALD LUTs, baking a look, rendering candidate looks to choose from |
| **`video-overlay`** | Compositing text/logos/animated graphics — including locking one to a moving face or eye |
| **`video-transitions`** | Wipes, slides, dissolves, pixelize, circle open/close via `xfade` |
| **`reframe-social`** | 9:16 / 1:1 / 4:5 for Reels, Shorts, TikTok — crop vs. pad, following an off-center subject |
| **`remotion-graphics`** | *Optional.* Real motion design (kinetic captions, animated lower-thirds) via React → transparent layer → composite |
| **`grok-video-edit`** | *Optional.* Generative edits that reimagine footage — recolor, add smoke/fire, restyle |

---

## Command reference

Position values accept **frame numbers** (`90`), **seconds** (`3.0`), or **timestamps**
(`00:01:30`). A bare integer is a frame, not a second — `--start 120` means frame 120.

#### Understand the footage
```bash
uv run video-agent info      video.mp4                            # fps, resolution, duration, codec
uv run video-agent transcribe video.mp4 [--words] [--clean] [--srt|--vtt] [-o out.txt]
uv run video-agent speech-segments video.mp4                      # speech/silence spans = cut points
uv run video-agent detect    video.mp4 --start 0.0 --end 60.0 -o grids/
uv run video-agent position-grid video.mp4 --at 14.2 --spacing 200 -o grid.png
uv run video-agent beats     music.mp3 [--onsets] [-o beats.txt]  # tempo + beat grid
```

#### Cut and assemble
```bash
uv run video-agent yt-dl     "https://youtube.com/watch?v=..." -o inputs/
uv run video-agent trim      video.mp4 --start 10.0 --end 30.0 -o clip.mp4
uv run video-agent frame     video.mp4 --at 45.5 -o frame.png
uv run video-agent concat    a.mp4 b.mp4 c.mp4 -o out.mp4
uv run video-agent splice    a.mp4 b.mp4 -o out.mp4               # audio+video crossfade
uv run video-agent edl       edit.json -o out.mp4 [--report] [--dry-run] [--draft]
uv run video-agent tighten   talk.mp4 -o edit.json --target-gap 0.5 --min-gap 1.0
uv run video-agent snap      edit.json -o snapped.json --to silence|beats [--ref music.mp3]
```

#### Finish
```bash
uv run video-agent captions  video.mp4 -o out.mp4 --clean         # or --srt subs.srt
uv run video-agent reframe   video.mp4 -o reel.mp4 --aspect 9:16 [--mode crop|pad] [--focus 0.4]
uv run video-agent grade apply   in.mp4 --lut luts/warm.png -o out.mp4
uv run video-agent grade preview in.mp4 --at 45.0 --lut none --lut a.cube -o looks.png
uv run video-agent overlay-text  in.mp4 --text "BOOM!" --x center --y center -o out.mp4
uv run video-agent overlay-texts in.mp4 --items "3:5:6" "2:6:7" "1:7:8" -o out.mp4
uv run video-agent overlay-image in.mp4 --image logo.png --x "W-w-20" --y 20 -o out.mp4
uv run video-agent overlay-gif   in.mp4 --gif fx.gif --x 100 --y 100 -o out.mp4
```

Also importable as a library: `import video_agent as va` → `va.video_info()`, `va.trim()`,
`va.detect()`, `va.extract_frame()`, `va.concat()`, `va.to_seconds()`, `va.to_frame()`.

---

## How visual search works

Claude can't scrub a timeline, so `detect` turns a time range into labeled grid montages plus a
`mapping.json` that converts any cell back to an exact frame number. Claude reads the grids,
says which cells contain what you asked for, and gets a timestamp back. It reads frames
sequentially, so it never suffers the keyframe-seek error that makes single-frame extraction
lie on B-frame footage.

```bash
uv run video-agent detect video.mp4 --start 0.0 --end 120.0 -o grids/scan/   # coarse: find the region
uv run video-agent detect video.mp4 --start 75.0 --end 85.0 \
    --step 1 --cell-w 384 -o grids/fine/                                     # fine: nail the frame
```

Coarse-to-fine beats one huge scan. `--cell-w 384` (2× default) makes small or distant subjects
legible.

---

## Optional extras

Both are opt-in; the editing core never touches them.

**Generative VFX** — reimagine footage that ffmpeg can't ([`grok-video-edit`](.claude/skills/grok-video-edit/SKILL.md)):

```bash
uv sync --extra vfx
echo 'XAI_API_KEY=xai-...' >> .env        # get one at https://console.x.ai

uv run video-agent vfx-edit clip.mp4 --prompt "Make the butterfly a ladybug" -o out.mp4
uv run video-agent vfx-edit clip.mp4 --prompt "Add a cartoon explosion" \
    --splice-into source.mp4 --splice-start 83.0 --splice-end 86.0 -o final.mp4
```

It downscales to fit the request limit, splits into ≤8s chunks, sends each as an inline base64
data URL (no tunnel needed), reassembles, and restores the original audio. `--backend` is
provider-agnostic — Grok today, with a registry in `video_agent.py` where Runway/Veo slot in.
Results vary run-to-run; prefer the deterministic commands whenever they can do the job.

**Animated motion graphics** — React/Remotion → transparent layer → ffmpeg composite. Heavy
(Node + a Chromium download, and a Remotion company license for commercial use). Scaffold in
[`remotion/`](remotion/); use it only for genuine motion design, otherwise `video-overlay`.

---

## Requirements

| | |
|---|---|
| **Python** | 3.11+ with [uv](https://docs.astral.sh/uv/) |
| **ffmpeg** | `ffmpeg` + `ffprobe` on PATH |
| **macOS** | for hardware encoding (`h264_videotoolbox`) — set `VCODEC = "libx264"` in `video_agent.py` on Linux |
| **Apple Silicon** | for `transcribe` (mlx-whisper; ~1.5GB model downloaded on first use) |
| **API key** | *only* for the optional `vfx-edit` |

Two build notes worth knowing: this ffmpeg is LGPL, so the `eq` and `drawtext` filters are
unavailable — text is rendered through PIL and composited, and grading goes through
curves/LUTs. And AV1 sources (common from YouTube) are decoded via PyAV/libdav1d, because
ffmpeg's AV1 decoder is broken on many builds.

---

## Privacy

Your footage stays on your machine. `inputs/`, `outputs/`, `grids/`, every media extension, and
transcript sidecars (`.srt`/`.vtt` — they contain everything said on camera) are gitignored by
default, so forking this repo can't leak your source material. The only network calls the core
makes are `yt-dl` fetching a URL you gave it and the one-time Whisper model download.

---

## Credits

The demo above was cut from **[Big Buck Bunny](https://peach.blender.org/)** — © 2008 Blender
Foundation, licensed [CC BY 3.0](https://creativecommons.org/licenses/by/3.0/). It's a great
test source precisely because it's public: you can download it and reproduce the trailer from
[`examples/bbb-trailer.json`](examples/bbb-trailer.json) yourself.

## License

MIT — see [LICENSE](LICENSE).
