# video-agent

Give Claude Code the ability to **edit video** — cut, transcribe, overlay, transition, and clean audio — frame-accurately with ffmpeg. Everything in the core runs **locally with no API key**. Optional generative VFX (recolor, smoke, restyle) plugs in via cloud models.

A single Python file + CLI. Claude drives it through natural language; you get frame-accurate edits without writing ffmpeg commands.

---

## What it does

**Editing core** — local, deterministic, no API key:

| Task | How |
|------|-----|
| Transcribe + remove filler words | `transcribe` (mlx-whisper) + `speech-segments` for frame-accurate cuts |
| Find a specific moment (face, object, gesture) | `detect` builds labeled frame grids Claude reads visually |
| Trim, concat, extract frames | codec-universal (handles AV1 via PyAV) |
| Burn in text / images / animated overlays | `overlay-text` / `overlay-image` / `overlay-gif` — pure ffmpeg |
| Place an overlay accurately (lock to a face/eye) | `position-grid` + the `video-overlay` skill |
| Transition / crossfade between shots | `splice` + the `video-transitions` skill |
| Download any YouTube video | `yt-dl` via yt-dlp |

**Generative VFX** — optional `vfx` extra, cloud + API key:

| Task | How |
|------|-----|
| Reimagine a region (recolor, add smoke/fire, restyle) | `vfx-edit` chunks the clip, sends to a cloud model, splices back |

---

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and ffmpeg.

```bash
mise install ffmpeg        # or: brew install ffmpeg
git clone https://github.com/agamm/video-agent
cd video-agent
uv sync                    # editing core — no API key needed
```

**Optional** — generative VFX (`vfx-edit`):
```bash
uv sync --extra vfx        # adds the cloud SDK
echo 'XAI_API_KEY=xai-...' >> .env   # get one at https://console.x.ai
```

---

## Commands

```
uv run video-agent info          <video>
uv run video-agent yt-dl         <url> [-o path]
uv run video-agent transcribe    <video> [-o out.txt] [--words]
uv run video-agent detect        <video> --start S --end E -o grids/
uv run video-agent trim          <video> --start S --end E -o out.mp4
uv run video-agent frame         <video> --at T -o frame.png
uv run video-agent concat        a.mp4 b.mp4 ... -o out.mp4
uv run video-agent speech-segments <video>            # speech/silence spans (cut points)
uv run video-agent splice        a.mp4 b.mp4 -o out.mp4
uv run video-agent vfx-edit      <video> --prompt "..." -o out.mp4   # optional: needs `vfx` extra + API key
uv run video-agent overlay-text  <video> --text "..." --x center --y center -o out.mp4
uv run video-agent overlay-image <video> --image logo.png --x "W-w-20" --y 20 -o out.mp4
uv run video-agent overlay-texts <video> --items "3:5:6" "2:6:7" "1:7:8" -o out.mp4
```

**Position values** accept frame numbers (`90`), seconds (`3.0`), or timestamps (`00:01:30`).  
For `detect` step: integers = frame count, floats < 1 = seconds (`--step 0.05` = every 50ms).

---

## How detect works

`detect` samples frames into labeled grid images and saves `mapping.json`. Claude reads the grids visually, notes which cells contain what you're looking for, and converts back to exact timestamps using the mapping.

```bash
# Coarse scan (1fps) to find a region
uv run video-agent detect video.mp4 --start 0.0 --end 120.0 --step 0.04 -o grids/scan/

# Fine scan (every frame, large cells) to find exact boundaries
uv run video-agent detect video.mp4 --start 75.0 --end 85.0 --step 1 --cell-w 384 -o grids/fine/
```

Use `--cell-w 384` (2× default) to make small or distant subjects visible.

---

## Generative VFX (optional)

`vfx-edit` is the one cloud-dependent command. It's **provider-agnostic** — `--backend`
selects the model (Grok today; the registry in `video_agent.py` is where Runway/Veo/etc.
slot in). Requires `uv sync --extra vfx` and that provider's API key. The editing core
above never touches it.

```bash
# Edit a standalone clip
uv run video-agent vfx-edit clip.mp4 --prompt "Make the butterfly a ladybug" -o out.mp4

# Edit a region of a longer video and splice it back in
uv run video-agent vfx-edit clip.mp4 --prompt "Add a cartoon explosion" \
    --splice-into source.mp4 --splice-start 83.0 --splice-end 86.0 \
    -o final.mp4

# (grok-edit remains as an alias of `vfx-edit --backend grok`)
```

**What happens internally:**
1. Clip is downscaled to 720p / 2Mbps (fits gRPC's 4MB limit)
2. Split into ≤8s chunks, each sent to the backend as a base64 data URL (no tunnel needed)
3. Edited chunks are concatenated; original audio is stitched back
4. If `--splice-into` is set, before/after sections are trimmed and everything is reassembled

**AV1 sources** (common from YouTube downloads) are decoded via PyAV/libdav1d — ffmpeg's AV1 decoder is broken on some builds.

### vfx-edit tips

- **Shot boundaries first** — find natural scene cuts in the detect grid and trim each shot separately; don't split chunks across cuts or the second shot gets re-imagined in a different style
- **Boundary precision matters** — use `detect --step 1 --cell-w 384` to find the exact first/last frame of your subject; send only those frames, not surrounding context
- **Chunk size** — 8s chunks balance style drift vs. seam count; shorter = more seams, longer = more drift per chunk
- **Prompts** — describe what to *add/change*, not what to keep; the model ignores negative constraints poorly
- **Audio** — the model outputs video only; the tool restores original audio automatically
- **Generative results vary run-to-run** (character drift); the deterministic editing core does not. See the `grok-video-edit` skill for the full workflow.

---

## Overlay text and images

No AI needed — pure ffmpeg.

```bash
# Countdown before an event
uv run video-agent overlay-texts video.mp4 \
    --items "3:5.0:6.0" "2:6.0:7.0" "1:7.0:8.0" \
    --size 180 --color yellow -o out.mp4

# Watermark (top-right corner)
uv run video-agent overlay-image video.mp4 --image logo.png \
    --x "W-w-20" --y 20 --scale 200:100 -o out.mp4

# Subtitle-style text at a specific time
uv run video-agent overlay-text video.mp4 --text "BOOM!" \
    --x center --y center --size 120 --color white \
    --start 5.0 --end 7.0 -o out.mp4
```

> **Note:** The mise-installed ffmpeg lacks `libfreetype` (`drawtext` unavailable). Text is rendered via PIL to a transparent PNG and composited with ffmpeg's `overlay` filter.

---

## Use with Claude Code

Claude reads `CLAUDE.md` in this repo for detailed usage instructions. Tell it what you want in plain English:

- *"Find every frame in the first 2 minutes where there's a butterfly and change it to a ladybug"*
- *"Download this YouTube video, remove filler words, and normalize the audio"*
- *"Add an explosion effect when the apple hits the ground, then add a 3-2-1 countdown before it"*

Claude will use `detect` to find regions, `trim` to isolate them, overlays/`splice` to assemble, and (with the `vfx` extra) `vfx-edit` to transform a region — splicing everything back with the original audio intact.

---

## Requirements

- **Python** 3.11+
- **ffmpeg** + ffprobe on PATH (`mise install ffmpeg` or `brew install ffmpeg`)
- **macOS** for hardware encoding (`h264_videotoolbox`); set `VCODEC = "libx264"` in `video_agent.py` for Linux
- **Apple Silicon** for `transcribe` (mlx-whisper; downloaded on first use, ~1.5GB)
- **xAI API key** — *only* for the optional `vfx-edit` (`uv sync --extra vfx`); get one at [console.x.ai](https://console.x.ai)

No cloudflared or tunnels needed — `vfx-edit` sends video as base64 inline in the request.

---

## License

MIT
