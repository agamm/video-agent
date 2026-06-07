# video-agent

**Refresh this file whenever `video_agent.py` changes.**

## Skills — deep procedures live here, not in this file

Workflows and ffmpeg techniques with non-obvious gotchas are documented as skills (in
`.claude/skills/`) so the procedure + its gotchas surface at the moment they're relevant.
A technique that's just a one-line ffmpeg invocation lives in a skill, **not** a CLI command
— commands are reserved for operations needing real code. Invoke the matching skill when:

- **`filler-removal`** — removing um/uh/filler words from speech.
- **`grok-video-edit`** — generative AI edits that reimagine footage (recolor, add smoke/fire/
  glow, restyle). OPTIONAL: needs the `vfx` extra (`uv sync --extra vfx`) + an API key; the
  rest of the toolkit is local and key-free. Results vary run-to-run — prefer deterministic
  commands/skills when they can do the job.
- **`video-overlay`** — compositing text/image/animated graphics onto video, and placing
  them accurately (including locking to a face/eye/moving subject).
- **`video-transitions`** — joining two clips with a visible transition (wipe/slide/dissolve/
  pixelize/circle/fade-to-black) via ffmpeg `xfade`.
- **`audio-edit`** — normalizing loudness, denoising hiss/hum, or mixing background music
  (with ducking under speech).

This file keeps the always-needed command reference, ffmpeg cheatsheet, and universal
pitfalls.

## Setup
```bash
uv sync                       # editing core — local, no API key
# ffmpeg required: mise install ffmpeg
# OPTIONAL generative VFX (vfx-edit): uv sync --extra vfx  +  XAI_API_KEY in .env
```

---

## Our CLI tools (command reference)

Position values everywhere: **bare integer = frame number; float or `HH:MM:SS` = seconds.**
Wrong: `--start 120` (= frame 120). Correct: `--start 120.0` or `--start 00:02:00`.

```bash
# info — metadata (CHECK CODEC FIRST: AV1 can't be ffmpeg-decoded; primitives route via PyAV)
uv run video-agent info video.mp4
#   duration / fps / resolution / frames / codec

# transcribe — speech to text (mlx-whisper, Apple Silicon; model ~1.5GB on first run)
uv run video-agent transcribe video.mp4 [-o out.txt]      # [MM:SS.ss --> MM:SS.ss] text
uv run video-agent transcribe video.mp4 --words -o w.txt  # start_sec  end_sec  word
#   USE FOR: *what* was said — find a filler by name, read content, subtitles. Word times
#   jitter ±0.3-0.7s (and shift between runs) → NEVER cut on them; pair with speech-segments
#   (WHERE) to place the actual cut. Verbatim by default — keeps um/uh hesitations (seeds the
#   decoder with a short filler prompt); --clean drops disfluencies. See filler-removal skill.

# speech-segments — speech/silence spans via silencedetect (inverts silence → speech)
uv run video-agent speech-segments video.mp4   # kind  start  end  dur  (tab-separated)
#   USE FOR: *where* sound starts/stops (frame-accurate) — cut points & safe crossfade spots.
#   --noise -30 (dB threshold; quieter rooms -35..-40), --min-silence 0.15 (s).
#   An isolated filler ("um") = a lone speech burst between two silences; trust this burst's
#   boundaries for the cut, not the jittery whisper word timestamp.
#   Together: transcribe = WHAT (which word to cut) · speech-segments = WHERE (the exact edge).

# yt-dl — download (yt-dlp backend)
uv run video-agent yt-dl "https://youtube.com/watch?v=..." -o inputs/

# detect — grid montages for visual frame search → grids/grid_NNN.png + mapping.json
uv run video-agent detect video.mp4 --start 0.0 --end 60.0 -o grids/
#   mapping.json: {"grid_000.png":[0,3,6,...]} → cell index N maps to that frame number.
#   For Grok boundary-finding use --step 1 --cell-w 384.
#   ALWAYS use detect (NOT repeated `frame` calls) to scan/search ANY time range or find a
#   gesture/wink/cut. It reads sequentially (frame-accurate, no seeking) and builds the
#   montage for you in one pass — never hand-roll a montage from many `frame` extractions.
#   Coarse-to-fine: a wide grid to locate the region, then --step 1 to nail exact frames.
#   Once detect locates the moment, it's fine to re-extract that frame full-res with `frame`
#   to zoom in for detail (grid cells are downscaled). detect = scan; frame = inspect.

# trim — extract a time range (codec-universal, handles AV1)
uv run video-agent trim video.mp4 --start 10.0 --end 30.0 -o clip.mp4

# frame — extract ONE known frame (codec-universal, frame-accurate)
uv run video-agent frame video.mp4 --at 45.5 -o frame.png
#   Use only when you already know the exact timestamp. To SCAN a range, use detect instead.

# concat — join clips (stream copy; all clips must share codec+resolution)
uv run video-agent concat a.mp4 b.mp4 c.mp4 -o out.mp4
#   Do NOT use after a re-encoded/Grok/overlay segment — use filter_complex concat instead.

# overlay-text / overlay-texts — burn text (PIL; mise ffmpeg lacks drawtext)
uv run video-agent overlay-text in.mp4 --text "BOOM!" --x center --y center \
    --size 120 --color yellow --start 5.0 --end 7.0 -o out.mp4
#   --color: white|yellow|red|black ; --start/--end optional (omit = always visible)

# overlay-image / overlay-gif — composite image or animated GIF
uv run video-agent overlay-image in.mp4 --image logo.png --x "W-w-20" --y 20 \
    --scale 200:100 --start 0.0 --end 5.0 -o out.mp4
#   For transparency / animated effects see the video-overlay skill (use RGBA PNG seq, not GIF).

# position-grid — frame with labelled (x,y) coordinate grid for planning overlays
uv run video-agent position-grid video.mp4 --at 14.2 --spacing 200 -o grid.png
#   Start at spacing 200 (100 is too dense to read). See video-overlay skill for the
#   crop-zoom + compute-don't-eyeball positioning workflow.

# vfx-edit — OPTIONAL generative AI edit (needs `vfx` extra + API key; see grok-video-edit
#   skill; always trim the target region first). --backend grok (default); grok-edit = alias.
uv run video-agent vfx-edit clip.mp4 --prompt "Make the lenses solid red" -o out.mp4

# splice — join two clips with audio+video crossfade (no hard cut)
uv run video-agent splice a.mp4 b.mp4 -o out.mp4
```

---

## Python library
```python
import video_agent as va

va.video_info("video.mp4")     # → {fps, width, height, duration, nframes, codec}
va.to_seconds(90, fps=30)      # → 3.0  (frame → seconds)
va.to_seconds("00:01:30", 30)  # → 90.0
va.to_frame(3.0, fps=30)       # → 90
va.detect(src, start, end, out_dir, step=None, cols=8, rows=8, cell_w=192)
va.trim(src, start, end, out)
va.extract_frame(src, at, out)
va.concat(clips, out)
```

---

## ffmpeg — use directly for all editing

macOS encoder: `h264_videotoolbox`. Linux: `libx264`.

```bash
# Info
ffprobe -v error -show_entries stream=width,height,r_frame_rate,duration -of json video.mp4

# Trim (keep 10s–30s)
ffmpeg -ss 10 -i in.mp4 -t 20 -c:v h264_videotoolbox -c:a aac out.mp4

# Cut out a section (remove 10s–30s)
ffmpeg -i in.mp4 -t 10 -c:v h264_videotoolbox -c:a aac part1.mp4
ffmpeg -ss 30 -i in.mp4 -c:v h264_videotoolbox -c:a aac part2.mp4
printf "file 'part1.mp4'\nfile 'part2.mp4'\n" > /tmp/list.txt
ffmpeg -f concat -safe 0 -i /tmp/list.txt -c copy out.mp4

# Concat clips (stream copy — only when all share codec and start on keyframes)
printf "file 'a.mp4'\nfile 'b.mp4'\nfile 'c.mp4'\n" > /tmp/list.txt
ffmpeg -f concat -safe 0 -i /tmp/list.txt -c copy out.mp4

# Concat after any re-encode/filter (re-encodes — avoids freeze frames + drift)
ffmpeg -i a.mp4 -i b.mp4 \
  -filter_complex "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]" \
  -map "[v]" -map "[a]" -c:v h264_videotoolbox -b:v 8M -c:a aac -ar 44100 out.mp4

# Extract a frame (by frame number — accurate)
ffmpeg -i in.mp4 -vf "select=eq(n\,90)" -vsync 0 -frames:v 1 frame.png
# Extract a frame (by timestamp — accurate; -ss AFTER -i, not before)
ffmpeg -i in.mp4 -vf "select=gte(t\,3.07)" -vframes 1 -vsync 0 frame.png

# Picture-in-picture
ffmpeg -i main.mp4 -i pip.mp4 \
  -filter_complex "[1:v]scale=320:180[ov];[0:v][ov]overlay=W-w-10:10:enable='between(t,5,15)'" \
  -c:v h264_videotoolbox out.mp4

# Insert video full-screen at 30s
ffmpeg -i main.mp4 -t 30 -c:v h264_videotoolbox -c:a aac before.mp4
ffmpeg -ss 30 -i main.mp4 -c:v h264_videotoolbox -c:a aac after.mp4
printf "file 'before.mp4'\nfile 'insert.mp4'\nfile 'after.mp4'\n" > /tmp/list.txt
ffmpeg -f concat -safe 0 -i /tmp/list.txt -c copy out.mp4

# Normalize audio
ffmpeg -i in.mp4 -af loudnorm=I=-16:TP=-1.5:LRA=11 -c:v copy out.mp4

# Speed change (2x)
ffmpeg -i in.mp4 -filter:v "setpts=0.5*PTS" -filter:a "atempo=2.0" out.mp4

# Crop (1080x1080 from center of 1920x1080)
ffmpeg -i in.mp4 -vf "crop=1080:1080:420:0" -c:v h264_videotoolbox out.mp4

# Resize
ffmpeg -i in.mp4 -vf "scale=1280:720" -c:v h264_videotoolbox out.mp4

# Mute / replace audio
ffmpeg -i in.mp4 -an -c:v copy out.mp4
ffmpeg -i in.mp4 -i audio.mp3 -map 0:v -map 1:a -c:v copy -shortest out.mp4

# Color grade
ffmpeg -i in.mp4 -vf "eq=brightness=0.05:contrast=1.1:saturation=1.2" -c:v h264_videotoolbox out.mp4
```

---

## Universal pitfalls (apply to every task)

### Frame extraction accuracy
**Fast seeking gives wrong frames on B-frame sources.** `ffmpeg -ss T -i src` (seek before
input) snaps to the nearest keyframe — 1–3s off on H.264 B-frame video (Grok output, OBS
recordings). The extracted frame is NOT what plays at T.

`extract_frame` and `position-grid` use `select=gte(t,T)` and are frame-accurate. In raw
ffmpeg, put `-ss` **after** `-i`, or use the select filter:
```bash
ffmpeg -i in.mp4 -vf "select=gte(t\,3.07)" -vframes 1 -vsync 0 frame.png
```
**Symptom of bad seek:** extracted frame shows the wrong moment (subject in a different
position). Cross-check with `detect` (reads sequentially, no seeking) if a frame looks off.

### Audio / video assembly
- **Stream-copy concat fails silently at non-keyframe boundaries.** After any re-encoded
  segment (Grok, splice, overlay), stream-copy concat may freeze at the seam. Use
  filter_complex concat (re-encodes) for the final join when any input went through a filter.
- **Re-encode audio in concat to prevent drift.** `-c copy` accumulates per-clip timestamp
  offsets across many clips. Always `-c:a aac -ar 44100` in the final concat.
- **Verify timing by dumping frames sequentially.** `ffmpeg -i out.mp4 -vf fps=10
  /tmp/f_%03d.png` gives reliable frame-to-timestamp correspondence — better than a single
  seeked frame for auditing cut/overlay timing on re-encoded sources.
