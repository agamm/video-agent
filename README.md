# video-agent

Give Claude Code the ability to edit videos.

Five commands that handle what ffmpeg can't do alone — download, transcribe, find specific moments, and AI-edit footage. Claude drives all of it through natural language.

---

## What you can do

**Find any moment in a video**
```
detect a character's face and change their eye color with Grok
find every time the speaker says "um" and remove it
locate the product close-up and overlay a logo
```

**Edit with AI**
```
make the sunglasses solid red in every shot
change the background to a beach
```

**Transcribe and cut**
```
transcribe the interview, find all the filler words, clean them up iteratively
```

**Download + edit any YouTube video**

---

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), and ffmpeg.

```bash
# ffmpeg (if not already installed)
mise install ffmpeg   # or: brew install ffmpeg

# clone and install
git clone https://github.com/yourname/video-agent
cd video-agent
uv sync
```

Set your xAI key in `.env` (needed for `grok-edit`):
```
XAI_API_KEY=xai-...
```

---

## Commands

```bash
uv run video-agent info <video>
uv run video-agent yt-dl <url> [-o path]
uv run video-agent transcribe <video> [-o out.txt] [--words]
uv run video-agent detect <video> --start S --end E -o grids/
uv run video-agent grok-edit <video> --prompt "..." -o out.mp4
```

| Command | What it does |
|---|---|
| `info` | Print fps, resolution, duration, frame count |
| `yt-dl` | Download any YouTube (or Vimeo, Twitter, etc.) video as MP4 |
| `transcribe` | Speech-to-text with timestamps. `--words` gives per-word timing for precise cuts |
| `detect` | Sample frames into labeled grid images — Claude reads them to find matching moments |
| `grok-edit` | AI video edit via xAI Grok. Handles chunking, tunnel, and audio stitch automatically |

Position values accept frame numbers (`90`) or timestamps (`3.0`, `00:01:30`).

---

## Use with Claude Code

Tell Claude what you want in plain English. Claude uses these commands as building blocks alongside ffmpeg for everything else (trim, cut, concat, overlay, normalize, color grade, speed, etc.).

Example prompts:
- *"Download this YouTube video and remove all the ums and uhs"*
- *"Find every frame where the speaker is looking down at the camera and make their sunglasses red"*
- *"Trim the video to the first 2 minutes, normalize the audio, and export at 1080p"*

Claude reads the `CLAUDE.md` in this repo to know exactly how to use each tool.

---

## How detect works

`detect` extracts frames, packs them into labeled grid images, and saves them alongside a `mapping.json`. Claude reads the grid images visually, notes which cells match, and maps them back to exact frame numbers — no separate API call needed.

```bash
uv run video-agent detect interview.mp4 --start 0 --end 300 -o grids/
# → grids/grid_000.png, grid_001.png, ... + mapping.json
```

---

## Requirements

- macOS (uses `h264_videotoolbox`; change `VCODEC` in `video_agent.py` to `libx264` on Linux)
- ffmpeg + ffprobe on PATH
- `cloudflared` on PATH for `grok-edit` (`mise install cloudflared`)
- Apple Silicon for `transcribe` (mlx-whisper)
