"""MP4 video toolkit — local, frame-accurate video editing.

Editing core (no API key, no cloud): transcription, cutting, frame search, overlays,
transitions, audio. Optional generative VFX (recolor / smoke / restyle) via pluggable
cloud backends lives behind the `vfx` extra — see `vfx_edit`. For everything else use
ffmpeg directly. See CLAUDE.md.

CLI:
    uv run video-agent info <src>
    uv run video-agent yt-dl <url> [-o path]
    uv run video-agent transcribe <src> [-o transcript.txt]
    uv run video-agent detect <src> --start S --end E -o grids/
    uv run video-agent trim <src> --start S --end E -o out.mp4
    uv run video-agent frame <src> --at T -o frame.png
    uv run video-agent concat a.mp4 b.mp4 ... -o out.mp4
    uv run video-agent vfx-edit <src> --prompt "Make sunglasses red" -o out.mp4   # optional; needs [vfx] extra + API key

Position values: bare integer = frame number; float or HH:MM:SS = seconds.

AV1 note: ffmpeg cannot decode AV1 on this machine. trim/frame/detect/vfx-edit
automatically route AV1 sources through PyAV (libdav1d) for decoding.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile

# H.264 encoder. VideoToolbox = macOS hardware; swap to "libx264" elsewhere.
VCODEC = "h264_videotoolbox"

# Codecs that ffmpeg cannot decode on this machine (AV1 native decoder broken).
_PYAV_DECODE_CODECS = {"av1"}


def xai_client():
    """Return an xai_sdk.Client(), raising clearly if XAI_API_KEY is not set."""
    from dotenv import load_dotenv
    load_dotenv()
    key = os.environ.get("XAI_API_KEY")
    if not key:
        raise RuntimeError(
            "XAI_API_KEY is not set. Add it to .env:\n  XAI_API_KEY=xai-..."
        )
    try:
        from xai_sdk import Client
    except ImportError:
        raise RuntimeError(
            "Generative VFX needs the optional 'vfx' extra:\n"
            "  uv sync --extra vfx   (or: pip install 'video-agent[vfx]')"
        )
    return Client(api_key=key)


def _run(cmd: list[str]) -> str:
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"{cmd[0]} failed:\n{p.stderr.strip()}")
    return p.stdout


# ---------------------------------------------------------------------------
# Video info + position helpers
# ---------------------------------------------------------------------------

def video_info(path: str) -> dict:
    """Return {fps, width, height, duration, nframes, codec} for the first video stream."""
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries",
                "stream=r_frame_rate,width,height,duration,nb_frames,codec_name",
                "-show_entries", "format=duration", "-of", "json", path])
    data = json.loads(out)
    s = data["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    duration = float(s.get("duration") or data["format"]["duration"])
    nframes = (int(s["nb_frames"]) if s.get("nb_frames", "N/A") != "N/A"
               else round(duration * fps))
    return {"fps": fps, "width": int(s["width"]), "height": int(s["height"]),
            "duration": duration, "nframes": nframes,
            "codec": s.get("codec_name", "unknown")}


def speech_segments(path: str, noise_db: float = -30.0,
                    min_silence: float = 0.15) -> list:
    """Detect speech vs silence spans via ffmpeg `silencedetect`.

    Returns a gap-free list of (start, end, kind) over [0, duration], where kind is
    "speech" or "silence". The value is the **inversion**: silencedetect reports only
    silence intervals; this fills the gaps as speech, which is the form you actually want
    for finding an isolated filler ("um" = a lone speech burst between two silences) — far
    more reliable than whisper word timestamps, which jitter ±0.3-0.7s between runs.
    """
    dur = video_info(path)["duration"]
    proc = subprocess.run(
        ["ffmpeg", "-hide_banner", "-i", path, "-af",
         f"silencedetect=noise={noise_db}dB:d={min_silence}", "-f", "null", "-"],
        capture_output=True, text=True)
    silences = []
    start = None
    for line in proc.stderr.splitlines():
        if "silence_start:" in line:
            start = float(line.split("silence_start:")[1].split()[0])
        elif "silence_end:" in line:
            end = float(line.split("silence_end:")[1].split()[0])
            silences.append((max(0.0, start if start is not None else 0.0),
                             min(dur, end)))
            start = None
    if start is not None:                      # file ends mid-silence
        silences.append((max(0.0, start), dur))
    spans = []
    cursor = 0.0
    for s0, s1 in silences:
        if s0 > cursor + 1e-3:
            spans.append((cursor, s0, "speech"))
        spans.append((s0, s1, "silence"))
        cursor = s1
    if cursor < dur - 1e-3:
        spans.append((cursor, dur, "speech"))
    return spans


def to_seconds(value, fps: float) -> float:
    """Frame number or timestamp → seconds. Bare integer = frame number."""
    if isinstance(value, int):
        return value / fps
    s = str(value).strip()
    if ":" in s:
        sec = 0.0
        for part in [float(x) for x in s.split(":")]:
            sec = sec * 60 + part
        return sec
    if s.isdigit():
        return int(s) / fps
    return float(s)


def to_frame(value, fps: float) -> int:
    if isinstance(value, int):
        return value
    s = str(value).strip()
    if s.isdigit() and ":" not in s:
        return int(s)
    return round(to_seconds(value, fps) * fps)


# ---------------------------------------------------------------------------
# PyAV helpers — decode any codec (AV1 via libdav1d) without ffmpeg
# ---------------------------------------------------------------------------

def _extract_frames_python(src: str, wanted: list, cell_w: int, cell_h: int) -> list:
    """Extract specific frame numbers via PyAV (handles AV1/libdav1d).

    Returns a list of PIL Images (or None for frames that couldn't be decoded),
    in the same order as `wanted`.
    """
    import av as _av
    from PIL import Image

    if not wanted:
        return []

    wanted_set = set(wanted)
    last = max(wanted)
    result = {}

    with _av.open(src) as container:
        stream = container.streams.video[0]
        fps = float(stream.average_rate)

        # Seek close to the first wanted frame to avoid decoding from t=0
        first_sec = min(wanted) / fps
        if first_sec > 1.0:
            container.seek(int(first_sec * 1_000_000))

        for frame in container.decode(stream):
            if frame.pts is None:
                continue
            fn = round(float(frame.pts) * float(stream.time_base) * fps)
            if fn in wanted_set:
                img = frame.to_image().convert("RGB")
                result[fn] = img.resize((cell_w, cell_h), Image.LANCZOS)
            if fn > last:
                break

    return [result.get(fn) for fn in wanted]


def _plane_bytes(plane) -> bytes:
    """Return packed plane bytes with line padding stripped."""
    raw = bytes(plane)
    if plane.line_size == plane.width:
        return raw
    return b"".join(raw[r * plane.line_size: r * plane.line_size + plane.width]
                    for r in range(plane.height))


def _pyav_trim(src: str, out: str, ss: float, duration: float):
    """Trim video using PyAV decode → ffmpeg pipe encode (handles AV1/libdav1d).

    PyAV decodes frames and pipes raw YUV420p to ffmpeg's VideoToolbox h264
    encoder. Audio is stream-copied by ffmpeg (no video decode needed for AAC).
    """
    import av as _av

    end_t = ss + duration

    with _av.open(src) as inp:
        stream = inp.streams.video[0]
        fps = float(stream.average_rate)
        w = stream.codec_context.width
        h = stream.codec_context.height

    with tempfile.TemporaryDirectory() as td:
        vid_only = os.path.join(td, "video.mp4")

        ff = subprocess.Popen(
            ["ffmpeg", "-y",
             "-f", "rawvideo", "-pix_fmt", "yuv420p",
             "-s", f"{w}x{h}", "-r", f"{fps:.6f}",
             "-i", "pipe:0",
             "-c:v", VCODEC, "-an", vid_only],
            stdin=subprocess.PIPE, stderr=subprocess.DEVNULL,
        )
        try:
            with _av.open(src) as inp:
                stream = inp.streams.video[0]
                inp.seek(int(ss * 1_000_000))
                for frame in inp.decode(stream):
                    if frame.pts is None:
                        continue
                    t = float(frame.pts * stream.time_base)
                    if t < ss:
                        continue
                    if t > end_t:
                        break
                    yuv = frame.reformat(format="yuv420p")
                    for plane in yuv.planes:
                        ff.stdin.write(_plane_bytes(plane))
        except BrokenPipeError:
            pass
        finally:
            ff.stdin.close()
            ff.wait()

        if ff.returncode not in (0, None):
            raise RuntimeError(f"ffmpeg video encoding failed (rc={ff.returncode})")

        # Extract audio with ffmpeg (safe: doesn't decode the video stream)
        aud = os.path.join(td, "audio.aac")
        has_audio = False
        try:
            _run(["ffmpeg", "-y", "-ss", f"{ss:.3f}", "-i", src,
                  "-t", f"{duration:.3f}", "-vn", "-c:a", "copy", aud])
            has_audio = os.path.exists(aud) and os.path.getsize(aud) > 100
        except RuntimeError:
            pass

        if has_audio:
            _run(["ffmpeg", "-y", "-i", vid_only, "-i", aud,
                  "-c:v", "copy", "-c:a", "copy", "-shortest", out])
        else:
            import shutil
            shutil.copy(vid_only, out)


# ---------------------------------------------------------------------------
# Frame detection — builds grid montages for Claude to inspect visually
# ---------------------------------------------------------------------------

def detect(src: str, start, end, out_dir: str, step=None,
           cols=8, rows=8, cell_w=256) -> dict:
    """Extract frames from [start, end], pack into labeled grid montages, save to out_dir.

    Returns a mapping {grid_filename: [frame_numbers]} and saves mapping.json alongside
    the grids. Open the grid images in Claude Code to identify matching frames, then use
    the mapping to convert cell indices back to frame numbers.

    Automatically uses PyAV for codecs ffmpeg cannot decode (e.g. AV1).
    """
    from PIL import Image, ImageDraw, ImageFont

    info = video_info(src)
    fps = info["fps"]
    if step is None:
        step = max(1, round(0.1 * fps))
    elif isinstance(step, float) and step < 1.0:
        # Treat as seconds — e.g. 0.05 → every ~50ms
        step = max(1, round(step * fps))
    else:
        step = int(step)
    cell_h = max(2, round(cell_w * info["height"] / info["width"] / 2) * 2)
    label_h = 28
    f0, f1 = to_frame(start, fps), to_frame(end, fps)
    wanted = list(range(f0, f1 + 1, step))
    if not wanted:
        return {}

    os.makedirs(out_dir, exist_ok=True)

    frames = []

    # Try ffmpeg first
    if info["codec"] not in _PYAV_DECODE_CODECS:
        with tempfile.TemporaryDirectory() as td:
            try:
                sel = f"between(n\\,{f0}\\,{f1})*not(mod(n-{f0}\\,{step}))"
                _run(["ffmpeg", "-y", "-i", src, "-vf",
                      f"select='{sel}',scale={cell_w}:-2", "-vsync", "0",
                      f"{td}/%06d.png"])
                raw = sorted(f for f in os.listdir(td) if f.endswith(".png"))
                for i, fname in enumerate(raw):
                    if i >= len(wanted):
                        break
                    img = Image.open(os.path.join(td, fname))
                    img.load()  # force into memory before tempdir is removed
                    frames.append((wanted[i], img))
            except RuntimeError:
                frames = []

    # PyAV fallback (AV1 or ffmpeg failure)
    if not frames:
        imgs = _extract_frames_python(src, wanted, cell_w, cell_h)
        frames = [(fn, img) for fn, img in zip(wanted, imgs) if img is not None]

    font = ImageFont.load_default(size=max(12, label_h - 6))
    per = cols * rows
    slot_h = cell_h + label_h
    mapping = {}

    for g, start_i in enumerate(range(0, len(frames), per)):
        chunk = frames[start_i:start_i + per]
        montage = Image.new("RGB", (cols * cell_w, rows * slot_h), (0, 0, 0))
        draw = ImageDraw.Draw(montage)
        for i, (_, img) in enumerate(chunk):
            cx, cy = (i % cols) * cell_w, (i // cols) * slot_h
            label = str(i)
            tb = draw.textbbox((0, 0), label, font=font)
            draw.text((cx + 4, cy + (label_h - (tb[3] - tb[1])) // 2 - tb[1]),
                      label, fill=(255, 255, 255), font=font)
            montage.paste(img.convert("RGB").resize((cell_w, cell_h)), (cx, cy + label_h))
        fname = f"grid_{g:03d}.png"
        montage.save(os.path.join(out_dir, fname))
        mapping[fname] = [fno for fno, _ in chunk]

    with open(os.path.join(out_dir, "mapping.json"), "w") as f:
        json.dump(mapping, f, indent=2)

    return mapping


# ---------------------------------------------------------------------------
# Primitive video operations (codec-universal)
# ---------------------------------------------------------------------------

def trim(src: str, start, end, out: str):
    """Trim video to [start, end]. Routes AV1 through PyAV automatically."""
    info = video_info(src)
    fps = info["fps"]
    ss = to_seconds(start, fps)
    end_s = to_seconds(end, fps)
    dur = end_s - ss
    if info["codec"] in _PYAV_DECODE_CODECS:
        _pyav_trim(src, out, ss, dur)
    else:
        _run(["ffmpeg", "-y", "-ss", f"{ss:.3f}", "-i", src,
              "-t", f"{dur:.3f}", "-c:v", VCODEC, "-c:a", "aac", out])


def extract_frame(src: str, at, out: str):
    """Extract a single frame at position `at` as an image file."""
    info = video_info(src)
    fps = info["fps"]
    t = to_seconds(at, fps)
    if info["codec"] in _PYAV_DECODE_CODECS:
        import av as _av
        with _av.open(src) as container:
            stream = container.streams.video[0]
            container.seek(int(t * 1_000_000))  # microseconds (no stream= → AV_TIME_BASE)
            fps_v = float(stream.average_rate)
            for frame in container.decode(stream):
                if frame.pts is None:
                    continue
                frame_t = float(frame.pts * stream.time_base)
                if frame_t < t - 1.0 / fps_v:
                    continue  # pre-seek keyframe, skip
                frame.to_image().save(out)
                break
    else:
        # Use select filter for frame-accurate extraction.
        # Fast-seek (-ss before -i) snaps to nearest keyframe on B-frame sources (e.g. Grok output),
        # returning the wrong frame. select= decodes to the exact pts.
        _run(["ffmpeg", "-y", "-i", src,
              "-vf", f"select=gte(t\\,{t:.6f})",
              "-vframes", "1", "-vsync", "0", out])


def overlay_text(src: str, out: str, text: str, x: str = "100", y: str = "100",
                 size: int = 72, color: str = "white", start: float = None,
                 end: float = None):
    """Burn text onto video. Renders text via PIL to a transparent PNG, then overlays with ffmpeg.
    x/y accept pixel values or ffmpeg expressions like '(W-w)/2' for centering."""
    from PIL import Image, ImageDraw, ImageFont

    info = video_info(src)
    W, H = info["width"], info["height"]

    # Render text to a transparent image the same size as the video
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default(size=size)

    # Measure text to resolve simple centering expressions
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    cx = (W - tw) // 2 if "w)/2" in x or x == "center" else int(x)
    cy = (H - th) // 2 if "h)/2" in y or y == "center" else int(y)

    # Shadow + text
    draw.text((cx + 3, cy + 3), text, font=font, fill=(0, 0, 0, 180))
    r, g, b = {"white": (255,255,255), "yellow": (255,255,0),
                "red": (255,0,0), "black": (0,0,0)}.get(color, (255,255,255))
    draw.text((cx, cy), text, font=font, fill=(r, g, b, 255))

    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
        img.save(f.name)
        text_png = f.name

    try:
        enable = ""
        if start is not None and end is not None:
            enable = f":enable='between(t,{start},{end})'"
        elif start is not None:
            enable = f":enable='gte(t,{start})'"
        filt = f"[1:v]copy[ov];[0:v][ov]overlay=0:0{enable}"
        _run(["ffmpeg", "-y", "-i", src, "-i", text_png,
              "-filter_complex", filt, "-c:v", VCODEC, "-c:a", "copy", out])
    finally:
        os.unlink(text_png)


def overlay_texts(src: str, out: str,
                  items: list,
                  x: str = "center", y: str = "center",
                  size: int = 72, color: str = "white"):
    """Overlay multiple text labels in a single encode pass.

    items: list of (text, start_sec, end_sec) tuples.
    """
    from PIL import Image, ImageDraw, ImageFont

    info = video_info(src)
    W, H = info["width"], info["height"]
    font = ImageFont.load_default(size=size)
    r, g, b = {"white": (255,255,255), "yellow": (255,255,0),
                "red": (255,0,0), "black": (0,0,0)}.get(color, (255,255,255))

    pngs = []
    enables = []
    for text, t0, t1 in items:
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        bbox = draw.textbbox((0, 0), text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        cx = (W - tw) // 2 if x == "center" else int(x)
        cy = (H - th) // 2 if y == "center" else int(y)
        draw.text((cx + 3, cy + 3), text, font=font, fill=(0, 0, 0, 180))
        draw.text((cx, cy), text, font=font, fill=(r, g, b, 255))
        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        img.save(f.name); f.close()
        pngs.append(f.name)
        enables.append(f"between(t,{t0},{t1})")

    try:
        # Build filter_complex: chain overlays one after another
        inputs = ["-i", src]
        for p in pngs:
            inputs += ["-i", p]

        # [0:v][1:v]overlay=0:0:enable=...[v1]; [v1][2:v]overlay=0:0:enable=...[v2]; ...
        chain = []
        prev = "0:v"
        for i, enable in enumerate(enables):
            label_out = f"v{i+1}" if i < len(enables) - 1 else "vout"
            chain.append(f"[{prev}][{i+1}:v]overlay=0:0:enable='{enable}'[{label_out}]")
            prev = label_out
        filt = ";".join(chain)

        _run(["ffmpeg", "-y"] + inputs + [
            "-filter_complex", filt,
            "-map", "[vout]", "-map", "0:a?",
            "-c:v", VCODEC, "-c:a", "copy", out
        ])
    finally:
        for p in pngs:
            os.unlink(p)


def overlay_image(src: str, image: str, out: str, x: str = "0", y: str = "0",
                  scale: str = None, start: float = None, end: float = None):
    """Composite an image on top of video using ffmpeg overlay filter."""
    scale_filt = f"[1:v]scale={scale}[ov];" if scale else "[1:v]copy[ov];"
    enable = ""
    if start is not None and end is not None:
        enable = f":enable='between(t,{start},{end})'"
    elif start is not None:
        enable = f":enable='gte(t,{start})'"
    filt = f"{scale_filt}[0:v][ov]overlay={x}:{y}{enable}"
    _run(["ffmpeg", "-y", "-i", src, "-i", image,
          "-filter_complex", filt, "-c:v", VCODEC, "-c:a", "copy", out])


def concat(clips: list, out: str):
    """Concatenate clips. Video stream-copied; audio re-encoded to prevent timestamp drift."""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
        for c in clips:
            f.write(f"file '{os.path.abspath(c)}'\n")
        list_file = f.name
    try:
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
              "-c:v", "copy", "-c:a", "aac", "-ar", "44100", out])
    finally:
        os.unlink(list_file)


def splice(clip_a: str, clip_b: str, out: str, crossfade: float = 0.04):
    """Join two clips with a short audio+video crossfade. Avoids hard-cut jump."""
    dur_a = video_info(clip_a)["duration"]
    offset = max(0.0, dur_a - crossfade)
    _run(["ffmpeg", "-y", "-i", clip_a, "-i", clip_b,
          "-filter_complex",
          f"[0:v][1:v]xfade=transition=fade:duration={crossfade:.4f}:offset={offset:.4f}[v];"
          f"[0:a][1:a]acrossfade=d={crossfade:.4f}:c1=tri:c2=tri[a]",
          "-map", "[v]", "-map", "[a]",
          "-c:v", VCODEC, "-b:v", "8M", out])


def overlay_gif(src: str, gif: str, out: str, x: str = "0", y: str = "0",
                scale: str = None, start: float = None, end: float = None):
    """Composite an animated GIF or RGBA PNG sequence (looping) onto video.

    GIF transparency: colorkey is applied BEFORE scale because scale converts
    BGRA→YUV which breaks colorkey color matching. For best results use PNGs
    with real alpha channel (via overlay_png_seq) instead of GIF colorkey.

    start/end: restrict overlay to a time window using split-sparkle-concat
    internally to avoid ffmpeg's unreliable enable= expression on GIF streams.
    """
    import tempfile, os

    def _build_filt(x_, y_, scale_):
        # colorkey BEFORE scale to stay in BGRA pixel format
        ck = "colorkey=0x000000:0.2:0.05"
        if scale_:
            return (f"[1:v]setpts=PTS-STARTPTS,{ck},scale={scale_},format=rgba[ov];"
                    f"[0:v][ov]overlay={x_}:{y_}:format=auto")
        return (f"[1:v]setpts=PTS-STARTPTS,{ck},format=rgba[ov];"
                f"[0:v][ov]overlay={x_}:{y_}:format=auto")

    if start is None and end is None:
        filt = _build_filt(x, y, scale)
        _run(["ffmpeg", "-y", "-i", src, "-ignore_loop", "0", "-i", gif,
              "-filter_complex", filt,
              "-c:v", VCODEC, "-c:a", "copy", "-shortest", out])
        return

    # Timed window: split source into before/during/after, apply overlay only to middle
    info = video_info(src)
    dur = info["duration"]
    t0 = start if start is not None else 0.0
    t1 = end if end is not None else dur

    with tempfile.TemporaryDirectory() as td:
        pa = os.path.join(td, "A.mp4")
        pb_raw = os.path.join(td, "B_raw.mp4")
        pb_ov  = os.path.join(td, "B_ov.mp4")
        pc = os.path.join(td, "C.mp4")

        _run(["ffmpeg", "-y", "-i", src, "-t", f"{t0:.4f}", "-c", "copy", pa])
        _run(["ffmpeg", "-y", "-i", src, "-ss", f"{t0:.4f}", "-t", f"{t1-t0:.4f}",
              "-c:v", VCODEC, "-c:a", "aac", pb_raw])
        _run(["ffmpeg", "-y", "-i", src, "-ss", f"{t1:.4f}",
              "-c:v", VCODEC, "-c:a", "aac", pc])

        filt = _build_filt(x, y, scale)
        _run(["ffmpeg", "-y", "-i", pb_raw, "-ignore_loop", "0", "-i", gif,
              "-filter_complex", filt,
              "-c:v", VCODEC, "-c:a", "copy", "-shortest", pb_ov])

        # Concat A + B_overlay + C
        parts = [p for p in [pa, pb_ov, pc] if os.path.exists(p) and
                 video_info(p)["duration"] > 0.01]
        n = len(parts)
        inputs = []
        for p in parts:
            inputs += ["-i", p]
        streams = "".join(f"[{i}:v][{i}:a]" for i in range(n))
        _run(["ffmpeg", "-y"] + inputs +
             ["-filter_complex", f"{streams}concat=n={n}:v=1:a=1[v][a]",
              "-map", "[v]", "-map", "[a]",
              "-c:v", VCODEC, "-b:v", "8M", "-c:a", "aac", "-ar", "44100", out])


def position_grid(src: str, at, out: str, spacing: int = 100):
    """Extract a frame and draw a pixel-coordinate grid — use to plan overlay positions.

    Every grid intersection is labelled with its (x,y) coordinates so you can read
    positions without tracing back to the edges.  Edge ticks use a slightly larger font.
    Spacing auto-scales the font so labels stay legible at small spacing values.
    """
    from PIL import Image, ImageDraw, ImageFont
    tmp = out + ".raw.png"
    extract_frame(src, at, tmp)
    img = Image.open(tmp).convert("RGB")
    draw = ImageDraw.Draw(img)
    w, h = img.size
    edge_font = ImageFont.load_default(size=max(10, min(16, spacing // 6)))
    inner_font = ImageFont.load_default(size=max(8, min(12, spacing // 8)))

    # Draw grid lines
    for xi in range(0, w + 1, spacing):
        draw.line([(xi, 0), (xi, h)], fill=(0, 0, 0), width=3)
        draw.line([(xi, 0), (xi, h)], fill=(0, 255, 128), width=1)
    for yi in range(0, h + 1, spacing):
        draw.line([(0, yi), (w, yi)], fill=(0, 0, 0), width=3)
        draw.line([(0, yi), (w, yi)], fill=(0, 255, 128), width=1)

    # Edge tick labels (x along top, y along left)
    for xi in range(spacing, w + 1, spacing):
        draw.text((xi + 2, 4), str(xi), fill=(0, 0, 0), font=edge_font)
        draw.text((xi + 1, 3), str(xi), fill=(0, 255, 128), font=edge_font)
    for yi in range(spacing, h + 1, spacing):
        draw.text((4, yi + 2), str(yi), fill=(0, 0, 0), font=edge_font)
        draw.text((3, yi + 1), str(yi), fill=(0, 255, 128), font=edge_font)

    # Intersection labels: every crossing gets "(x,y)" so any interior point is readable
    for xi in range(spacing, w, spacing):
        for yi in range(spacing, h, spacing):
            label = f"{xi},{yi}"
            draw.text((xi + 2, yi + 2), label, fill=(0, 0, 0), font=inner_font)
            draw.text((xi + 1, yi + 1), label, fill=(0, 255, 128), font=inner_font)

    img.save(out)
    os.unlink(tmp)


# ---------------------------------------------------------------------------
# Grok video editing
# ---------------------------------------------------------------------------

GROK_MAX_SECONDS = 8.0  # Balance between drift-per-chunk and seam count

# Constraint prefix appended to every grok-edit prompt to prevent style changes.
_GROK_CONSTRAINT = (
    " Keep ALL other elements in the scene completely unchanged — same characters, "
    "same animation style, same background, same lighting, same colors everywhere "
    "except the specified change. Only modify exactly what is asked."
)


def _start_tunnel(serve_dir: str):
    """Start a local HTTP server + cloudflared tunnel. Returns (tunnel_url, cf_proc, httpd)."""
    import re
    import threading
    from http.server import HTTPServer, SimpleHTTPRequestHandler

    # Pass directory explicitly so os.getcwd() changes don't affect serving.
    class QuietHandler(SimpleHTTPRequestHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=serve_dir, **kwargs)
        def log_message(self, *_): pass

    httpd = HTTPServer(("", 8724), QuietHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    cf = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", "http://localhost:8724"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    import time
    deadline = time.time() + 20
    for line in cf.stdout:
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            return m.group(0), cf, httpd
        if time.time() > deadline:
            break
    cf.terminate()
    raise RuntimeError("cloudflared failed to start — is it installed? (mise install cloudflared)")


def vfx_edit(src: str, prompt: str, out: str, backend: str = "grok", **kwargs) -> str:
    """Provider-agnostic generative VFX edit (reimagines footage: recolor, smoke, restyle…).

    `backend` selects the cloud model provider. Requires the optional `vfx` extra
    (`uv sync --extra vfx`) plus that provider's API key — the editing core never needs it.
    Extra kwargs (splice_into / splice_start / splice_end / reference_images) pass through
    to the backend. Add new providers (runway/veo/…) to the `_VFX_BACKENDS` registry.
    """
    if backend not in _VFX_BACKENDS:
        raise ValueError(
            f"unknown vfx backend {backend!r}; available: {', '.join(_VFX_BACKENDS)}")
    return _VFX_BACKENDS[backend](src, prompt, out, **kwargs)


def grok_edit(src: str, prompt: str, out: str,
              splice_into: str = None, splice_start=None, splice_end=None,
              reference_images: list = None) -> str:
    """Edit a video using Grok (grok-imagine-video). Handles chunking for videos > 8s,
    sends chunks as base64 data URLs (no tunnel required), then concats and re-stitches
    the original audio. Routes AV1 sources through PyAV for chunk splitting.

    splice_into / splice_start / splice_end: when provided, the Grok-edited result is
    spliced back into `splice_into` at [splice_start, splice_end], replacing that section.
    Use this whenever you're editing a region of a larger video — the output will be the
    full video with the edit applied, not just the edited clip.
    """
    import base64
    import datetime
    import requests

    client = xai_client()
    info = video_info(src)
    duration = info["duration"]
    use_pyav = info["codec"] in _PYAV_DECODE_CODECS

    with tempfile.TemporaryDirectory() as td:
        # Split into ≤8s chunks
        chunks = []
        t = 0.0
        i = 0
        while t < duration:
            end = min(t + GROK_MAX_SECONDS, duration)
            chunk = os.path.join(td, f"chunk_{i:03d}.mp4")
            if use_pyav:
                _pyav_trim(src, chunk, t, end - t)
            else:
                _run(["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", src, "-t", f"{end-t:.3f}",
                      "-vf", "scale=1280:720", "-c:v", VCODEC, "-b:v", "2M", "-an", chunk])
            chunks.append(chunk)
            t = end
            i += 1

        print(f"editing {len(chunks)} chunk(s) via Grok…", flush=True)

        edited = []
        for idx, chunk in enumerate(chunks):
            print(f"  chunk {idx+1}/{len(chunks)}…", end=" ", flush=True)
            with open(chunk, "rb") as f:
                data_url = "data:video/mp4;base64," + base64.b64encode(f.read()).decode()
            # Upload reference images as base64 data URLs if provided
            ref_urls = None
            if reference_images:
                ref_urls = []
                for img_path in reference_images:
                    with open(img_path, "rb") as rf:
                        ext = os.path.splitext(img_path)[1].lstrip(".") or "png"
                        ref_urls.append(
                            f"data:image/{ext};base64,"
                            + base64.b64encode(rf.read()).decode()
                        )

            resp = client.video.generate(
                prompt=prompt + _GROK_CONSTRAINT,
                model="grok-imagine-video",
                video_url=data_url,
                resolution="720p",
                reference_image_urls=ref_urls,
                timeout=datetime.timedelta(minutes=8),
                interval=datetime.timedelta(seconds=6),
            )
            out_chunk = os.path.join(td, f"edited_{idx:03d}.mp4")
            open(out_chunk, "wb").write(requests.get(resp.url, timeout=120).content)
            edited.append(out_chunk)
            print("done", flush=True)

        # Concat edited chunks (no audio — Grok output may have AI audio)
        list_file = os.path.join(td, "list.txt")
        with open(list_file, "w") as f:
            for e in edited:
                f.write(f"file '{e}'\n")
        joined = os.path.join(td, "joined.mp4")
        _run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_file,
              "-c", "copy", joined])

        # Stitch original audio back
        _run(["ffmpeg", "-y", "-i", joined, "-i", src,
              "-map", "0:v", "-map", "1:a?",
              "-c:v", "copy", "-c:a", "aac", "-shortest", out])

    # Splice the edited clip back into the source video if requested
    if splice_into and splice_start is not None and splice_end is not None:
        splice_info = video_info(splice_into)
        splice_fps = splice_info["fps"]
        ss = to_seconds(splice_start, splice_fps)
        se = to_seconds(splice_end, splice_fps)
        edited_clip = out
        with tempfile.TemporaryDirectory() as td2:
            before_raw = os.path.join(td2, "before_raw.mp4")
            after_raw = os.path.join(td2, "after_raw.mp4")
            before = os.path.join(td2, "before.mp4")
            after = os.path.join(td2, "after.mp4")
            print(f"splicing back into {splice_into} [{ss:.1f}s–{se:.1f}s]…", flush=True)
            trim(splice_into, 0.0, ss, before_raw)
            trim(splice_into, se, splice_info["duration"], after_raw)
            # Downscale to 720p to match Grok output resolution
            _run(["ffmpeg", "-y", "-i", before_raw, "-vf", "scale=1280:720",
                  "-c:v", VCODEC, "-c:a", "aac", before])
            _run(["ffmpeg", "-y", "-i", after_raw, "-vf", "scale=1280:720",
                  "-c:v", VCODEC, "-c:a", "aac", after])
            concat([before, edited_clip, after], out)

    return out


# Generative VFX backend registry: name -> impl. Add "runway"/"veo"/… here as they land.
_VFX_BACKENDS = {"grok": grok_edit}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    from dotenv import load_dotenv
    load_dotenv()
    p = argparse.ArgumentParser(prog="video-agent", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    dl = sub.add_parser("yt-dl", help="download a YouTube (or any yt-dlp supported) video")
    dl.add_argument("url")
    dl.add_argument("-o", "--out", default=".", help="output path or directory (default: .)")
    dl.add_argument("--quality",
                    default="bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best")

    d = sub.add_parser("detect", help="build grid montages for visual frame detection")
    d.add_argument("src")
    d.add_argument("--start", required=True, help="frame number or timestamp")
    d.add_argument("--end", required=True, help="frame number or timestamp")
    d.add_argument("-o", "--out", required=True, help="directory to write grid images")
    d.add_argument("--step", type=float, default=None,
                   help="frame interval: integer=frame count, float<1=seconds (e.g. 0.05=50ms). Default: 0.1s")
    d.add_argument("--cols", type=int, default=8)
    d.add_argument("--rows", type=int, default=8)
    d.add_argument("--cell-w", type=int, default=256)

    inf = sub.add_parser("info", help="print video metadata (fps, resolution, duration, frames, codec)")
    inf.add_argument("src")

    tr = sub.add_parser("transcribe", help="transcribe audio with timestamps (mlx-whisper)")
    tr.add_argument("src")
    tr.add_argument("-o", "--out", default=None, help="output .txt file (default: print to stdout)")
    tr.add_argument("--words", action="store_true",
                    help="output one word per line with precise timestamps (use for cut-point editing)")
    tr.add_argument("--model", default="mlx-community/whisper-large-v3-turbo",
                    help="mlx-whisper model repo")
    tr.add_argument("--clean", action="store_true",
                    help="let whisper drop disfluencies (um/uh); default is verbatim, keeping them")

    ss = sub.add_parser("speech-segments",
                        help="speech/silence spans via silencedetect (find isolated fillers reliably)")
    ss.add_argument("src")
    ss.add_argument("--noise", type=float, default=-30.0,
                    help="silence threshold in dB (default -30; quieter rooms: -35 to -40)")
    ss.add_argument("--min-silence", type=float, default=0.15,
                    help="minimum silence duration in seconds (default 0.15)")

    ge = sub.add_parser("vfx-edit", aliases=["grok-edit"],
                        help="OPTIONAL generative AI edit (needs `vfx` extra + API key); --splice-into embeds result back in source")
    ge.add_argument("src")
    ge.add_argument("--backend", default="grok", choices=sorted(_VFX_BACKENDS),
                    help="generative VFX provider (default: grok)")
    ge.add_argument("--prompt", required=True, help="edit instruction, e.g. 'make sunglasses red'")
    ge.add_argument("-o", "--out", required=True)
    ge.add_argument("--splice-into", default=None, metavar="SOURCE",
                    help="splice edited clip back into SOURCE at [--splice-start, --splice-end]")
    ge.add_argument("--splice-start", default=None, metavar="T",
                    help="start of replaced section in SOURCE (frame number or timestamp)")
    ge.add_argument("--splice-end", default=None, metavar="T",
                    help="end of replaced section in SOURCE (frame number or timestamp)")
    ge.add_argument("--reference", nargs="+", default=None, metavar="IMG",
                    help="reference image(s) to anchor character/style consistency across chunks")

    tm = sub.add_parser("trim", help="extract a time range (codec-universal, handles AV1)")
    tm.add_argument("src")
    tm.add_argument("--start", required=True, help="frame number or timestamp")
    tm.add_argument("--end", required=True, help="frame number or timestamp")
    tm.add_argument("-o", "--out", required=True)

    fr = sub.add_parser("frame", help="extract a single frame as an image (codec-universal, handles AV1)")
    fr.add_argument("src")
    fr.add_argument("--at", required=True, help="frame number or timestamp")
    fr.add_argument("-o", "--out", required=True, help="output image path (e.g. frame.png)")

    ct = sub.add_parser("concat", help="join clips with stream copy (all clips must share codec/resolution)")
    ct.add_argument("clips", nargs="+", help="input clip paths")
    ct.add_argument("-o", "--out", required=True)

    ot = sub.add_parser("overlay-text", help="burn text onto video")
    ot.add_argument("src")
    ot.add_argument("--text", required=True)
    ot.add_argument("-o", "--out", required=True)
    ot.add_argument("--x", default="100", help="x position or expr e.g. '(w-text_w)/2'")
    ot.add_argument("--y", default="100", help="y position or expr e.g. 'h-100'")
    ot.add_argument("--size", type=int, default=72)
    ot.add_argument("--color", default="white")
    ot.add_argument("--start", type=float, default=None, help="show from this second")
    ot.add_argument("--end",   type=float, default=None, help="hide after this second")

    ots = sub.add_parser("overlay-texts", help="overlay multiple text labels in one pass (e.g. countdown)")
    ots.add_argument("src")
    ots.add_argument("-o", "--out", required=True)
    ots.add_argument("--items", nargs="+", required=True,
                     metavar="TEXT:START:END",
                     help="each item as text:start_sec:end_sec e.g. '3:81:82' '2:82:83' '1:83:84'")
    ots.add_argument("--x", default="center")
    ots.add_argument("--y", default="center")
    ots.add_argument("--size", type=int, default=120)
    ots.add_argument("--color", default="white")

    oi = sub.add_parser("overlay-image", help="composite an image onto video")
    oi.add_argument("src")
    oi.add_argument("--image", required=True, help="image file to overlay (PNG, JPG, …)")
    oi.add_argument("-o", "--out", required=True)
    oi.add_argument("--x", default="0", help="x position or expr e.g. 'W-w-10'")
    oi.add_argument("--y", default="0", help="y position or expr e.g. '10'")
    oi.add_argument("--scale", default=None, help="resize image first e.g. '320:180'")
    oi.add_argument("--start", type=float, default=None)
    oi.add_argument("--end",   type=float, default=None)

    og = sub.add_parser("overlay-gif", help="composite an animated GIF (looping) onto video")
    og.add_argument("src")
    og.add_argument("--gif", required=True, help="animated GIF file")
    og.add_argument("-o", "--out", required=True)
    og.add_argument("--x", default="0")
    og.add_argument("--y", default="0")
    og.add_argument("--scale", default=None, help="resize gif first e.g. '200:200'")
    og.add_argument("--start", type=float, default=None)
    og.add_argument("--end",   type=float, default=None)

    pg = sub.add_parser("position-grid", help="extract frame with pixel coordinate grid for planning overlay positions")
    pg.add_argument("src")
    pg.add_argument("--at", type=float, required=True, help="timestamp in seconds")
    pg.add_argument("-o", "--out", required=True, help="output PNG")
    pg.add_argument("--spacing", type=int, default=100, help="grid spacing in pixels (default 100)")

    sp = sub.add_parser("splice", help="join two clips with audio+video crossfade (no hard cut)")
    sp.add_argument("clip_a")
    sp.add_argument("clip_b")
    sp.add_argument("-o", "--out", required=True)
    sp.add_argument("--crossfade", type=float, default=0.04, help="crossfade duration in seconds (default 0.04)")

    a = p.parse_args()

    if a.cmd == "info":
        info = video_info(a.src)
        print(f"duration:   {info['duration']:.3f}s")
        print(f"fps:        {info['fps']}")
        print(f"resolution: {info['width']}x{info['height']}")
        print(f"frames:     {info['nframes']}")
        codec = info["codec"]
        suffix = "  ← ffmpeg cannot decode; trim/frame/detect use PyAV" if codec in _PYAV_DECODE_CODECS else ""
        print(f"codec:      {codec}{suffix}")

    elif a.cmd == "transcribe":
        import mlx_whisper
        print("transcribing…", flush=True)
        # Verbatim by default: whisper silently cleans hesitation sounds (um/uh) otherwise,
        # which breaks filler-removal. Seed the decoder with only short disfluencies so it
        # keeps them — a broader list (like/so/basically) over-biases toward real words.
        # --clean restores whisper's default behavior.
        verbatim_kwargs = {} if a.clean else dict(
            condition_on_previous_text=False,
            initial_prompt="Um, uh, er, hmm...",
        )
        result = mlx_whisper.transcribe(
            a.src,
            path_or_hf_repo=a.model,
            word_timestamps=True,
            **verbatim_kwargs,
        )
        lines = []
        if a.words:
            for seg in result["segments"]:
                for w in seg.get("words", []):
                    lines.append(f"{w['start']:.3f}\t{w['end']:.3f}\t{w['word'].strip()}")
        else:
            for seg in result["segments"]:
                m0, s0 = divmod(seg["start"], 60)
                m1, s1 = divmod(seg["end"], 60)
                lines.append(f"[{int(m0):02d}:{s0:05.2f} --> {int(m1):02d}:{s1:05.2f}]  {seg['text'].strip()}")
        text = "\n".join(lines)
        if a.out:
            with open(a.out, "w") as f:
                f.write(text + "\n")
            print(f"saved {len(lines)} segments to {a.out}")
        else:
            print(text)

    elif a.cmd == "speech-segments":
        spans = speech_segments(a.src, noise_db=a.noise, min_silence=a.min_silence)
        print(f"# kind\tstart\tend\tdur  (noise={a.noise}dB, min_silence={a.min_silence}s)")
        for s0, s1, kind in spans:
            print(f"{kind}\t{s0:.3f}\t{s1:.3f}\t{s1 - s0:.3f}")

    elif a.cmd == "yt-dl":
        import yt_dlp
        out = a.out if a.out.endswith(".mp4") else a.out.rstrip("/") + "/%(title)s.%(ext)s"
        with yt_dlp.YoutubeDL({"format": a.quality, "outtmpl": out,
                                "merge_output_format": "mp4"}) as ydl:
            info = ydl.extract_info(a.url)
            print(ydl.prepare_filename(info).replace(".webm", ".mp4").replace(".mkv", ".mp4"))

    elif a.cmd == "detect":
        fps = video_info(a.src)["fps"]
        mapping = detect(a.src, a.start, a.end, a.out,
                         step=a.step, cols=a.cols, rows=a.rows, cell_w=a.cell_w)
        total = sum(len(v) for v in mapping.values())
        print(f"saved {len(mapping)} grids ({total} frames) to {a.out}/")
        print(f"mapping: {a.out}/mapping.json")
        for fname, frames in mapping.items():
            times = ", ".join(f"{f}({f/fps:.1f}s)" for f in frames[:4])
            suffix = "..." if len(frames) > 4 else ""
            print(f"  {fname}: {times}{suffix}")

    elif a.cmd in ("vfx-edit", "grok-edit"):
        print(vfx_edit(a.src, a.prompt, a.out, backend=a.backend,
                       splice_into=a.splice_into,
                       splice_start=a.splice_start,
                       splice_end=a.splice_end,
                       reference_images=a.reference))

    elif a.cmd == "trim":
        trim(a.src, a.start, a.end, a.out)
        print(a.out)

    elif a.cmd == "frame":
        extract_frame(a.src, a.at, a.out)
        print(a.out)

    elif a.cmd == "concat":
        concat(a.clips, a.out)
        print(a.out)

    elif a.cmd == "overlay-text":
        overlay_text(a.src, a.out, a.text, x=a.x, y=a.y,
                     size=a.size, color=a.color, start=a.start, end=a.end)
        print(a.out)

    elif a.cmd == "overlay-texts":
        items = []
        for item in a.items:
            parts = item.rsplit(":", 2)
            items.append((parts[0], float(parts[1]), float(parts[2])))
        overlay_texts(a.src, a.out, items, x=a.x, y=a.y, size=a.size, color=a.color)
        print(a.out)

    elif a.cmd == "overlay-image":
        overlay_image(a.src, a.image, a.out, x=a.x, y=a.y,
                      scale=a.scale, start=a.start, end=a.end)
        print(a.out)

    elif a.cmd == "overlay-gif":
        overlay_gif(a.src, a.gif, a.out, x=a.x, y=a.y,
                    scale=a.scale, start=a.start, end=a.end)
        print(a.out)

    elif a.cmd == "position-grid":
        position_grid(a.src, a.at, a.out, spacing=a.spacing)
        print(a.out)

    elif a.cmd == "splice":
        splice(a.clip_a, a.clip_b, a.out, crossfade=a.crossfade)
        print(a.out)


if __name__ == "__main__":
    main()
