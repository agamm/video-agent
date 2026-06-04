# video-agent

**Refresh this file whenever `video_agent.py` changes.**

## Setup
```bash
uv sync
# ffmpeg required: mise install ffmpeg
# XAI_API_KEY in .env
```

---

## Our CLI tools

### info — video metadata
```bash
uv run video-agent info video.mp4
# duration:   424.433s
# fps:        30.0
# resolution: 1920x1080
# frames:     12733
```

### transcribe — speech to text with timestamps
```bash
uv run video-agent transcribe video.mp4              # print to stdout
uv run video-agent transcribe video.mp4 -o out.txt   # save to file
```
Output format: `[MM:SS.ss --> MM:SS.ss]  text` per segment.
Uses mlx-whisper (Apple Silicon, fast). Model downloads on first run (~1.5GB).

**Workflow — remove filler words (iterate until perfect):**

```bash
# Step 1: get word-level timestamps (essential — segment timestamps are too imprecise)
uv run video-agent transcribe video.mp4 --words -o words.txt
# output: start_sec  end_sec  word  (one per line, tab-separated)
```

**Cutting rules (follow exactly):**
- Cut from **word_start - 0.05s** to **word_end + 0.15s** (50ms lead-in, 150ms trail)
  — the trail prevents clipping the consonant that follows the filler
- Never cut closer than 0.1s to a non-filler word on either side
- If two fillers are within 0.3s of each other, merge them into one cut

**Iteration loop — do not stop until clean:**
1. Find all filler words (um, uh, like, you know, basically, literally) in words.txt
2. For each cut: extract a **3-second splice preview** around the join point:
   ```bash
   ffmpeg -ss <join_time - 1.5> -i draft.mp4 -t 3 preview_cut_N.mp4
   ```
3. Read each preview clip (use Read tool or open it) — listen/watch for:
   - Abrupt audio jump → increase trail padding by 0.05s, redo that cut
   - Missing word start → decrease lead-in by 0.05s, redo that cut
   - Sounds natural → mark cut as approved
4. Only approved cuts go into the final concat
5. After concat, **re-transcribe the output** and check no non-filler speech was removed
6. If any real speech is missing → restore that cut from the original, iterate again
7. Done when: all previews sound natural AND re-transcription matches intent

### yt-dl — download video
```bash
uv run video-agent yt-dl "https://youtube.com/watch?v=..." -o inputs/
uv run video-agent yt-dl "https://youtube.com/watch?v=..." -o inputs/clip.mp4
```

### detect — build grid montages for visual frame search
```bash
uv run video-agent detect video.mp4 --start 0 --end 60 -o grids/
uv run video-agent detect video.mp4 --start 00:01:00 --end 00:02:00 -o grids/
```
Outputs: `grids/grid_000.png`, `grid_001.png`, ... + `mapping.json`

**Workflow in Claude Code:**
1. Run detect to build grids
2. Read each grid image with the Read tool
3. Note cell indices matching your criteria
4. Read `grids/mapping.json` → convert cell index → frame number
```json
{"grid_000.png": [0, 3, 6, 9, ...], "grid_001.png": [192, 195, ...]}
```
Cell index 2 in `grid_000.png` = frame 6, and so on.

---

## Python library
```python
import video_agent as va

va.video_info("video.mp4")     # → {fps, width, height, duration, nframes}
va.to_seconds(90, fps=30)      # → 3.0  (frame → seconds)
va.to_seconds("00:01:30", 30)  # → 90.0
va.to_frame(3.0, fps=30)       # → 90
va.detect(src, start, end, out_dir, step=None, cols=8, rows=8, cell_w=192)
```

Position values: bare integer = frame number; float or `HH:MM:SS` = seconds.

---

## xAI / Grok video editing

```bash
uv run video-agent grok-edit clip.mp4 --prompt "Make the sunglasses lenses solid red" -o out.mp4
```

Automatically handles: chunking (Grok max 8.7s), cloudflared tunnel, parallel edits, concat, original audio stitch. Requires `XAI_API_KEY` in `.env` and `cloudflared` on PATH (`mise install cloudflared`).

---

## ffmpeg — use directly for all editing

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

# Concat clips
printf "file 'a.mp4'\nfile 'b.mp4'\nfile 'c.mp4'\n" > /tmp/list.txt
ffmpeg -f concat -safe 0 -i /tmp/list.txt -c copy out.mp4

# Extract a frame
ffmpeg -i in.mp4 -vf "select=eq(n\,90)" -vsync 0 -frames:v 1 frame.png

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

# Mute
ffmpeg -i in.mp4 -an -c:v copy out.mp4

# Replace audio
ffmpeg -i in.mp4 -i audio.mp3 -map 0:v -map 1:a -c:v copy -shortest out.mp4

# Color grade
ffmpeg -i in.mp4 -vf "eq=brightness=0.05:contrast=1.1:saturation=1.2" -c:v h264_videotoolbox out.mp4

# Transcribe (Apple Silicon)
uv run --with mlx-whisper python3 -c "
import mlx_whisper
r = mlx_whisper.transcribe('video.mp4', path_or_hf_repo='mlx-community/whisper-large-v3-turbo', word_timestamps=True)
for s in r['segments']: print(f\"[{s['start']:.2f}s] {s['text'].strip()}\")
"
```

macOS encoder: `h264_videotoolbox`. Linux: `libx264`.
