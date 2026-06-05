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
# codec:      av1  ← ffmpeg cannot decode; trim/frame/detect use PyAV
```
**Check codec first.** AV1 (from YouTube downloads) can't be decoded by ffmpeg — all
primitives (trim/frame/detect/grok-edit) route through PyAV automatically, but knowing
the codec upfront avoids surprises.

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

**Position values: bare integer = frame number; float or HH:MM:SS = seconds.**
Wrong: `--start 120` (= frame 120 = 5s at 24fps). Correct: `--start 120.0` or `--start 00:02:00`.

**Workflow in Claude Code:**
1. Run detect to build grids
2. Read each grid image with the Read tool
3. Note cell indices matching your criteria
4. Read `grids/mapping.json` → convert cell index → frame number
```json
{"grid_000.png": [0, 3, 6, 9, ...], "grid_001.png": [192, 195, ...]}
```
Cell index 2 in `grid_000.png` = frame 6, and so on.

### trim — extract a time range (codec-universal)
```bash
uv run video-agent trim video.mp4 --start 10.0 --end 30.0 -o clip.mp4
```
Handles AV1 automatically (PyAV → h264_videotoolbox pipe).

### frame — extract a single frame (codec-universal)
```bash
uv run video-agent frame video.mp4 --at 45.5 -o frame.png
```

### concat — join clips (stream copy, no re-encode)
```bash
uv run video-agent concat a.mp4 b.mp4 c.mp4 -o out.mp4
```
All clips must share codec and resolution. Use `trim` first if mixing sources.

### overlay-text — burn text onto video (no AI, deterministic)
```bash
uv run video-agent overlay-text in.mp4 --text "BOOM!" \
    --x center --y center --size 120 --color yellow \
    --start 5.0 --end 7.0 -o out.mp4
```
- `--x` / `--y`: pixel offset or `center` (horizontal/vertical center)
- `--color`: white | yellow | red | black
- `--start` / `--end`: optional seconds to show/hide (omit = always visible)
- Uses PIL for text rendering (mise ffmpeg lacks libfreetype/drawtext)

### overlay-image — composite image onto video (no AI, deterministic)
```bash
uv run video-agent overlay-image in.mp4 --image logo.png \
    --x "W-w-20" --y 20 --scale 200:100 \
    --start 0.0 --end 5.0 -o out.mp4
```
- `--x` / `--y`: pixel offset or ffmpeg expressions (`W-w-10` = 10px from right edge)
- `--scale WxH`: resize image before overlaying (e.g. `320:180`)
- `--start` / `--end`: optional window (omit = always visible)

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

Position values: bare integer = frame number; float or `HH:MM:SS` = seconds.

---

## xAI / Grok video editing

```bash
# Edit a clip standalone
uv run video-agent grok-edit clip.mp4 --prompt "Make the sunglasses lenses solid red" -o out.mp4

# Edit a section of a larger video and splice it back in (PREFERRED)
uv run video-agent grok-edit source.mp4 --prompt "Make the butterfly red and sparkly" \
    --splice-into source.mp4 --splice-start 99.0 --splice-end 112.0 \
    -o final.mp4
```

**Always splice back when editing a section of a longer video.**
The typical flow: detect a region → grok-edit that region with `--splice-into` → done.
Without `--splice-into` you get only the edited clip, not the full video with the edit applied.

Automatically handles: chunking, base64 upload (no tunnel), concat, original audio stitch.
Requires `XAI_API_KEY` in `.env`. Chunks downscaled to 720p/2Mbps (gRPC 4MB limit).
**AV1 sources** handled automatically (PyAV decode → h264 chunks).

**Chunk size** (`GROK_MAX_SECONDS = 5.0` in code): each chunk is edited independently —
Grok reimagines the scene fresh each time. Shorter chunks = more seams/style jumps.
5s is the best balance. Do NOT go below 3s.

**Boundary detection** — before grok-edit, run `detect` with `--step 1 --cell-w 384`
inside the rough window to find frame-exact first/last frames of the subject. Trim
to those exact boundaries so Grok doesn't see irrelevant context.

**Shot boundaries** — split chunks at natural scene cuts, not arbitrary time intervals.
Use the detect grid to identify cuts, then trim each shot separately. A chunk that
spans a shot change will have the second shot reimagined in a different style.

**Character drift** — Grok cannot maintain character consistency between chunks.
The system constraint (`_GROK_CONSTRAINT`) is generic and should stay that way.
Pass character descriptions in the user prompt, not the constraint.

**Audio** — Grok outputs video only. For the final assembly, always rebuild the audio
track from the original source: extract with ffmpeg (`-vn -c:a copy`), then mux onto
the assembled video track. Never rely on PyAV-trimmed audio for sync.

**Prompt tips:**
- Say what to change, not what to keep — Grok ignores negative constraints better than positive ones
- Avoid "shockwave rings", "portal effects" etc. unless you want them — Grok adds them literally
- For color-only changes describe the exact color to replace and target color

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
