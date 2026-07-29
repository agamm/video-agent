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
    uv run video-agent edl edit.json -o out.mp4 [--report] [--draft]
    uv run video-agent tighten <src> -o edit.json        # collapse dead air → an EDL
    uv run video-agent beats <music> [-o beats.txt]      # tempo + beat grid
    uv run video-agent snap edit.json -o snapped.json --to silence|beats
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


def whisper_segments(src: str, model: str = "mlx-community/whisper-large-v3-turbo",
                     clean: bool = False) -> list:
    """Transcribe `src` and return whisper segments (each: {start, end, text, words}).

    Verbatim by default (keeps um/uh) — see the `transcribe` command for the rationale.
    Shared by the transcribe and captions commands.
    """
    import mlx_whisper
    verbatim_kwargs = {} if clean else dict(
        condition_on_previous_text=False,
        initial_prompt="Um, uh, er, hmm...",
    )
    return mlx_whisper.transcribe(
        src, path_or_hf_repo=model, word_timestamps=True, **verbatim_kwargs,
    )["segments"]


def _srt_timestamp(t: float, sep: str = ",") -> str:
    """Seconds → SRT/VTT timestamp HH:MM:SS,mmm (sep=',' SRT, '.' VTT)."""
    if t < 0:
        t = 0.0
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def format_srt(segments: list) -> str:
    """Render whisper segments as a SubRip (.srt) document."""
    blocks = []
    for i, seg in enumerate((s for s in segments if s["text"].strip()), start=1):
        blocks.append(
            f"{i}\n"
            f"{_srt_timestamp(seg['start'])} --> {_srt_timestamp(seg['end'])}\n"
            f"{seg['text'].strip()}"
        )
    return "\n\n".join(blocks) + "\n"


def parse_subtitles(path: str) -> list:
    """Parse a .srt or .vtt file into segments [{start, end, text}, …]."""
    import re
    txt = open(path, encoding="utf-8").read()
    segs = []
    pat = re.compile(
        r"(\d{1,2}:\d{2}:\d{2}[.,]\d{3})\s*-->\s*(\d{1,2}:\d{2}:\d{2}[.,]\d{3})(.*?)"
        r"(?=\n\s*\n|\n\s*\d{1,2}:\d{2}:\d{2}[.,]\d{3}|\Z)",
        re.DOTALL,
    )

    def _sec(ts: str) -> float:
        ts = ts.replace(",", ".")
        h, m, s = ts.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)

    for m in pat.finditer(txt):
        text = " ".join(line.strip() for line in m.group(3).strip().splitlines()
                        if line.strip())
        if text:
            segs.append({"start": _sec(m.group(1)), "end": _sec(m.group(2)), "text": text})
    return segs


def format_vtt(segments: list) -> str:
    """Render whisper segments as a WebVTT (.vtt) document."""
    blocks = ["WEBVTT"]
    for seg in segments:
        if not seg["text"].strip():
            continue
        blocks.append(
            f"{_srt_timestamp(seg['start'], '.')} --> {_srt_timestamp(seg['end'], '.')}\n"
            f"{seg['text'].strip()}"
        )
    return "\n\n".join(blocks) + "\n"


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
        _overlay_timed_pngs(src, pngs, enables, out)
    finally:
        for p in pngs:
            os.unlink(p)


def _overlay_timed_pngs(src: str, pngs: list, enables: list, out: str):
    """Overlay N full-frame RGBA PNGs onto `src`, each gated by an `enable` expr, one pass.

    Shared by overlay_texts and burn_captions. Video is re-encoded (overlay filter); audio
    is stream-copied. With no PNGs it just re-muxes the source.
    """
    if not pngs:
        _run(["ffmpeg", "-y", "-i", src, "-c:v", VCODEC, "-c:a", "copy", out])
        return
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
    _run(["ffmpeg", "-y"] + inputs + [
        "-filter_complex", ";".join(chain),
        "-map", "[vout]", "-map", "0:a?",
        "-c:v", VCODEC, "-c:a", "copy", out,
    ])


def _wrap_lines(draw, text: str, font, max_w: int) -> list:
    """Greedy word-wrap `text` so each line's rendered width ≤ max_w (PIL)."""
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if cur and draw.textlength(trial, font=font) > max_w:
            lines.append(cur)
            cur = word
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines


def burn_captions(src: str, out: str, segments: list, size: int = None,
                  color: str = "white", position: str = "bottom", box: bool = True):
    """Burn subtitle segments into `src` (PIL render → ffmpeg overlay; no drawtext needed).

    segments: list of {start, end, text} (whisper segments work directly). Each caption is
    word-wrapped to ~90% of frame width, centered horizontally, with an optional
    semi-transparent background box for legibility. position: bottom|top|center.
    """
    from PIL import Image, ImageDraw, ImageFont

    info = video_info(src)
    W, H = info["width"], info["height"]
    if size is None:
        size = max(16, round(H * 0.05))          # ~5% of frame height
    font = ImageFont.load_default(size=size)
    r, g, b = {"white": (255, 255, 255), "yellow": (255, 255, 0),
               "red": (255, 0, 0), "black": (0, 0, 0)}.get(color, (255, 255, 255))
    max_w = int(W * 0.9)
    line_h = size + round(size * 0.35)
    pad = round(size * 0.3)

    pngs, enables = [], []
    for seg in segments:
        text = seg["text"].strip()
        if not text:
            continue
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        lines = _wrap_lines(draw, text, font, max_w)
        block_h = line_h * len(lines)
        if position == "top":
            y0 = round(H * 0.06)
        elif position == "center":
            y0 = (H - block_h) // 2
        else:                                    # bottom
            y0 = H - block_h - round(H * 0.08)

        if box:
            widest = max(draw.textlength(ln, font=font) for ln in lines)
            bx0 = int((W - widest) // 2 - pad)
            bx1 = int((W + widest) // 2 + pad)
            draw.rectangle([bx0, y0 - pad, bx1, y0 + block_h + pad // 2],
                           fill=(0, 0, 0, 140))

        for i, line in enumerate(lines):
            lw = draw.textlength(line, font=font)
            lx, ly = (W - lw) // 2, y0 + i * line_h
            draw.text((lx + 2, ly + 2), line, font=font, fill=(0, 0, 0, 200))
            draw.text((lx, ly), line, font=font, fill=(r, g, b, 255))

        f = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        img.save(f.name); f.close()
        pngs.append(f.name)
        enables.append(f"between(t,{seg['start']:.3f},{seg['end']:.3f})")

    try:
        _overlay_timed_pngs(src, pngs, enables, out)
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


def reframe(src: str, out: str, aspect: str = "9:16", mode: str = "crop",
            focus: float = 0.5, width: int = None):
    """Reframe video to a target aspect ratio (e.g. 9:16 reels, 1:1, 4:5).

    mode="crop" (default): crop to fill the target aspect — full-bleed, loses the edges.
      `focus` (0..1) biases the crop along the cut axis: 0=left/top, 0.5=center, 1=right/
      bottom. Use it to keep an off-center speaker in frame (no face tracking — set it from
      a `position-grid` read). To follow a *moving* subject, see the reframe skill.
    mode="pad": fit the whole frame inside the target canvas, filling the gaps with a
      blurred zoom of the footage (the ubiquitous TikTok/IG look) — nothing is cropped.
    width: optional final scale (output width in px; height follows the aspect).
    """
    info = video_info(src)
    W, H = info["width"], info["height"]
    aw, ah = (float(x) for x in aspect.split(":"))
    r = aw / ah                                  # target width / height
    focus = min(max(focus, 0.0), 1.0)

    if mode == "crop":
        if W / H > r:                            # source too wide → crop width
            new_w, new_h = round(H * r), H
        else:                                    # source too tall → crop height
            new_w, new_h = W, round(W / r)
        new_w -= new_w % 2
        new_h -= new_h % 2
        x = round(focus * (W - new_w))
        y = round(focus * (H - new_h))
        vf = f"crop={new_w}:{new_h}:{x}:{y}"
        if width:
            vf += f",scale={width}:-2"
        _run(["ffmpeg", "-y", "-i", src, "-vf", vf,
              "-c:v", VCODEC, "-c:a", "aac", out])
    elif mode == "pad":
        if W / H > r:                            # canvas grows taller
            out_w, out_h = W, round(W / r)
        else:                                    # canvas grows wider
            out_w, out_h = round(H * r), H
        out_w -= out_w % 2
        out_h -= out_h % 2
        if width:
            out_h = round(width * out_h / out_w)
            out_w = width
            out_h -= out_h % 2
        vf = (f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=increase,"
              f"crop={out_w}:{out_h},gblur=sigma=20[bg];"
              f"[0:v]scale={out_w}:{out_h}:force_original_aspect_ratio=decrease[fg];"
              f"[bg][fg]overlay=(W-w)/2:(H-h)/2")
        _run(["ffmpeg", "-y", "-i", src, "-filter_complex", vf,
              "-c:v", VCODEC, "-c:a", "aac", out])
    else:
        raise ValueError(f"unknown reframe mode {mode!r}; use 'crop' or 'pad'")


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
# EDL — the edit is a JSON file (auditable, re-runnable cut list)
# ---------------------------------------------------------------------------

# Normalize every segment to a common geometry so concat never desyncs / errors.
def _edl_vchain(W, H, fps, punch=None, nframes=None):
    """Video filter chain for one EDL segment.

    `punch` is an optional (z_start, z_end) slow push (see `punch` in the EDL schema).
    The pre-scale to 2×  is what keeps zoompan smooth: zoompan rounds its crop origin to
    whole pixels, so pushing directly on a W×H frame visibly stair-steps.
    """
    chain = (f"setpts=PTS-STARTPTS,fps={fps},"
             f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
             f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if punch:
        z0, z1 = punch
        n = max(1, int(nframes or 1))
        chain += (f",scale={W*2}:{H*2},"
                  f"zoompan=z='{z0:.6f}+({z1:.6f}-{z0:.6f})*min(on/{n},1)':d=1:"
                  f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':s={W}x{H}:fps={fps}")
    return chain + ",format=yuv420p"


def _edl_achain(a_s, a_e, seam_fade, fade_in, fade_out):
    """Audio filter chain for one EDL segment, with short fades at the internal seams.

    Butt-splicing two audio segments mid-waveform steps the signal discontinuously and
    clicks. A fade shorter than a syllable (~15 ms) is inaudible as a fade but removes the
    step entirely — so every internal boundary gets one, at no cost in duration.
    """
    dur = a_e - a_s
    parts = [f"atrim={a_s:.3f}:{a_e:.3f}", "asetpts=PTS-STARTPTS"]
    if seam_fade > 0 and dur > 2 * seam_fade:
        if fade_in:
            parts.append(f"afade=t=in:st=0:d={seam_fade:.4f}")
        if fade_out:
            parts.append(f"afade=t=out:st={dur - seam_fade:.4f}:d={seam_fade:.4f}")
    parts.append("aformat=channel_layouts=stereo:sample_rates=48000")
    return ",".join(parts)


def _edl_audio_windows(clips):
    """Resolve each clip's audio in/out from `audio_lead` → [(a_start, a_end), …].

    `audio_lead` is what makes a cut a **split edit**: it moves the audio edit point off
    the picture edit point. +0.4 = this clip's sound arrives 0.4 s before its picture
    (J-cut); -0.4 = the previous clip's sound holds 0.4 s over this picture (L-cut).

    Clip i's audio therefore runs [start_i − lead_i, end_i − lead_{i+1}]. The leads
    telescope, so total audio length still equals total video length even though
    individual segments no longer match — which is exactly why the renderer concatenates
    video and audio as two independent chains.
    """
    n = len(clips)
    leads = []
    for i, c in enumerate(clips):
        lead = float(c.get("audio_lead", 0.0))
        if i == 0 and lead:
            print("  [edl] ignoring audio_lead on clip 0 — nothing precedes it to split against")
            lead = 0.0
        leads.append(lead)
    leads.append(0.0)                      # sentinel: nothing follows the last clip

    windows = []
    for i, c in enumerate(clips):
        a_s = float(c["start"]) - leads[i]
        a_e = float(c["end"]) - leads[i + 1]
        if a_s < 0:
            raise ValueError(
                f"clip {i}: audio_lead {leads[i]}s reaches before the start of {c['src']} "
                f"(audio in would be {a_s:.3f}s). Use a smaller lead or a later in-point.")
        if a_e - a_s < 0.05:
            raise ValueError(
                f"clip {i}: audio_lead leaves only {a_e - a_s:.3f}s of audio "
                f"({a_s:.3f}→{a_e:.3f}). Neighbouring leads are eating the whole clip.")
        windows.append((a_s, a_e))
    return windows


def _edl_quantize(clips, fps):
    """Round every cut point onto the frame grid.

    Video can only cut on a frame boundary; audio can cut anywhere. A cut point that sits
    between frames therefore lands in two slightly different places in the two streams, and
    across a cut list those fractions accumulate into real A/V drift — which is easy to hit
    now that `snap`/`tighten` write times derived from beats and silence rather than from
    round numbers. Quantizing up front makes both timelines agree exactly, for every EDL
    regardless of what produced it.
    """
    def q(t):
        return round(float(t) * fps) / fps       # full precision — rounding for looks here
                                                 # would put the value back off the grid
    out = []
    for c in clips:
        rc = dict(c)
        for k in ("start", "end", "vstart", "vend", "audio_lead"):
            if k in rc:
                rc[k] = q(rc[k])
        out.append(rc)
    return out


def _edl_check_cutaway(i, clip, fps):
    """A `vsrc` cutaway must be the same length as the audio it covers, or the picture
    after it lands off its sound and every later cut inherits the drift."""
    if "vsrc" not in clip:
        return
    v_dur = float(clip.get("vend", clip["end"])) - float(clip.get("vstart", clip["start"]))
    a_dur = float(clip["end"]) - float(clip["start"])
    if abs(v_dur - a_dur) > 1.0 / fps:
        raise ValueError(
            f"clip {i}: vsrc window is {v_dur:.3f}s but the clip covers {a_dur:.3f}s of "
            f"audio. A cutaway swaps the picture only — the two must match (fix vstart/vend).")


def edl_render(edl_path: str, out: str, draft: bool = False):
    """Execute an edit.json (Edit Decision List) → one re-encoded video.

    Schema (see the edl-edit skill):
      {
        "fps": 60, "width": 1920, "height": 1080,   # all optional (defaults shown)
        "grade": "luts/warm.cube",                   # optional LUT (.cube or HALD .png)
        "audio_fix": "loudnorm=I=-14:TP=-1.5:LRA=11",# optional filter chain on the speech bus
        "seam_fade": 0.015,                          # optional click-killing fade at each cut
        "vbitrate": "9M",                            # optional; raise for 4K/screencast text
        "music": {"src": "bed.mp3", "gain_db": -18, "duck": true,
                  "start": 0.0, "fade_in": 0.5, "fade_out": 2.0},   # optional music bed
        "clips": [
          {"src": "a.mp4", "start": 1.89, "end": 60.81,
           "first_words": "Hey everyone", "rationale": "cleanest take, zero ums"},
          # split edit — this clip's sound arrives 0.4s before its picture (J-cut):
          {"src": "b.mp4", "start": 12.0, "end": 20.0, "audio_lead": 0.4,
           "punch": [1.0, 1.06],                      # optional slow push over the clip
           "rationale": "answer starts under the end of the question"},
          # multicam cutaway — audio from src, picture from a second camera:
          {"src": "a.mp4", "start": 70.0, "end": 78.0,
           "vsrc": "roomcam.mp4", "vstart": 141.3, "vend": 149.3,
           "rationale": "cut to wide while the slide is static"}
        ]
      }
    `start`/`end` (and `vstart`/`vend`) are seconds (floats) on the SOURCE timeline; cuts are
    frame-accurate (trim filter, not -ss seeking). `rationale`/`first_words` are documentation
    only — ignored by the renderer, read by humans. After rendering, VERIFY by re-transcribing
    `out`.

    Video and audio are concatenated as **two independent chains**, so `audio_lead` can move
    the sound edit off the picture edit (J/L cuts) without desyncing anything downstream.
    `draft` renders small and cheap for the internal verify loop — never as a deliverable.
    """
    edl = json.load(open(edl_path))
    clips = edl["clips"]
    if not clips:
        raise ValueError("EDL has no clips")
    fps = edl.get("fps", 60)
    W, H = edl.get("width", 1920), edl.get("height", 1080)
    seam_fade = float(edl.get("seam_fade", 0.015))
    # 9M is fine for camera footage; screencasts with small text need more to stay legible.
    vbitrate = str(edl.get("vbitrate", "9M"))
    if draft:
        H = 480
        W = max(2, round(edl.get("width", 1920) * H / edl.get("height", 1080) / 2) * 2)
        vbitrate = "1500k"
        print(f"  [edl] DRAFT render at {W}x{H} — for verification only, not a deliverable")

    # Resolve the audio timeline BEFORE any pre-transcoding: `audio_lead` reaches outside a
    # clip's picture range, so the ranges have to be known before we decide what to extract.
    # Validating here also means errors name the real source, not a temp file.
    n = len(clips)
    clips = _edl_quantize(clips, fps)
    windows = _edl_audio_windows(clips)
    for i, c in enumerate(clips):
        _edl_check_cutaway(i, c, fps)

    # AV1 cannot be decoded by ffmpeg's trim filter on this platform — pre-transcode
    # any AV1 clip segments to temp H.264 files so the filter_complex works cleanly.
    _av1_tmp_dir = None
    _av1_map: dict[tuple, tuple] = {}   # (abs_src, t0, t1) -> (tmp_path, offset)

    def _resolve_av1(src, t0, t1):
        """Return (path, offset) for the range [t0, t1] — `offset` is the source time at
        the returned file's t=0, so caller times map as `t - offset`."""
        nonlocal _av1_tmp_dir
        if video_info(src).get("codec", "").lower() != "av1":
            return src, 0.0
        key = (os.path.abspath(src), round(float(t0), 3), round(float(t1), 3))
        if key not in _av1_map:
            if _av1_tmp_dir is None:
                _av1_tmp_dir = tempfile.mkdtemp(prefix="edl_av1_")
            tmp = os.path.join(_av1_tmp_dir, f"av1_{len(_av1_map)}.mp4")
            print(f"  [edl] pre-transcoding AV1 {t0:.1f}–{t1:.1f}s → {tmp}")
            trim(src, float(t0), float(t1), tmp)
            _av1_map[key] = (tmp, float(t0))
        return _av1_map[key]

    inputs: list[str] = []
    def _idx(path):
        ap = os.path.abspath(path)
        if ap not in inputs:
            inputs.append(ap)
        return inputs.index(ap)

    def _has_audio(path):
        out = _run(["ffprobe", "-v", "error", "-select_streams", "a",
                    "-show_entries", "stream=index", "-of", "csv=p=0", path])
        return bool(out.strip())

    audio_cache: dict[str, bool] = {}
    need_silence = False
    parts = []
    for i, c in enumerate(clips):
        a_s, a_e = windows[i]
        v_s = float(c.get("vstart", c["start"]))
        v_e = float(c.get("vend", c["end"]))

        # For AV1, extract once per clip covering picture AND sound (a J-cut's audio starts
        # before its picture, so extracting only the picture range would cut it off).
        if "vsrc" in c:
            v_src, v_off = _resolve_av1(c["vsrc"], v_s, v_e)
            a_src, a_off = _resolve_av1(c["src"], a_s, a_e)
        else:
            a_src, a_off = _resolve_av1(c["src"], min(a_s, v_s), max(a_e, v_e))
            v_src, v_off = a_src, a_off
        v_s, v_e = v_s - v_off, v_e - v_off
        a_s, a_e = a_s - a_off, a_e - a_off
        vi, ai = _idx(v_src), _idx(a_src)

        punch = c.get("punch")
        if punch is not None:
            punch = (1.0, float(punch)) if isinstance(punch, (int, float)) else \
                    (float(punch[0]), float(punch[1]))
        vchain = _edl_vchain(W, H, fps, punch=punch,
                             nframes=round((v_e - v_s) * fps))
        # Nudge the out-point a quarter-frame inside: `trim` compares t < end in floating
        # point, so a frame sitting exactly on a quantized boundary is sometimes admitted
        # and sometimes not, leaving the picture a frame longer than the sound.
        parts.append(f"[{vi}:v]trim={v_s:.4f}:{v_e - 0.25 / fps:.4f},{vchain}[v{i}]")

        ap = os.path.abspath(a_src)
        if ap not in audio_cache:
            audio_cache[ap] = _has_audio(a_src)
        achain = _edl_achain(a_s, a_e, seam_fade,
                             fade_in=i > 0, fade_out=i < n - 1)
        if audio_cache[ap]:
            parts.append(f"[{ai}:a]{achain}[a{i}]")
        else:  # silent source (e.g. a title card) → synthesize silence of the clip's length
            need_silence = True
            parts.append(f"[__SIL__:a]"
                         + _edl_achain(0.0, a_e - a_s, seam_fade,
                                       fade_in=i > 0, fade_out=i < n - 1)
                         + f"[a{i}]")

    # Two independent concat chains: `audio_lead` makes per-segment A and V lengths differ
    # (they only telescope back to equal *totals*), so a single v=1:a=1 concat would drift.
    graph = ";".join(parts)
    graph += ";" + "".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[vc0]"
    graph += ";" + "".join(f"[a{i}]" for i in range(n)) + f"concat=n={n}:v=0:a=1[ac0]"

    vmap, amap = "[vc0]", "[ac0]"
    if edl.get("grade"):
        lut = edl["grade"]
        if lut.lower().endswith(".png"):        # HALD-CLUT is an image input, not a param
            graph += f";[vc0][{_idx(lut)}:v]haldclut[vc]"
        else:
            graph += f";[vc0]lut3d={lut}[vc]"
        vmap = "[vc]"
    if edl.get("audio_fix"):
        graph += f";[ac0]{edl['audio_fix']}[acf]"; amap = "[acf]"

    music = edl.get("music")
    if music:
        total = sum(e - s for s, e in windows)
        m_start = float(music.get("start", 0.0))
        fin = float(music.get("fade_in", 0.5))
        fout = float(music.get("fade_out", 1.5))
        graph += (f";[__MUS__:a]atrim={m_start:.3f}:{m_start + total:.3f},asetpts=PTS-STARTPTS,"
                  f"volume={float(music.get('gain_db', -18)):.1f}dB,"
                  f"afade=t=in:st=0:d={fin:.3f},"
                  f"afade=t=out:st={max(0.0, total - fout):.3f}:d={fout:.3f},"
                  f"aformat=channel_layouts=stereo:sample_rates=48000[bg]")
        if music.get("duck", True):
            # Sidechain: the speech bus triggers the ducking *and* is mixed back in, so it
            # has to be split — a label can only be consumed once.
            graph += (f";{amap}asplit=2[spk][trig]"
                      f";[bg][trig]sidechaincompress="
                      f"threshold={float(music.get('threshold', 0.03))}:"
                      f"ratio={float(music.get('ratio', 8))}:attack=20:release=300[bgd]"
                      f";[spk][bgd]amix=inputs=2:duration=first:normalize=0[amx]")
        else:
            graph += (f";{amap}anull[spk]"
                      f";[spk][bg]amix=inputs=2:duration=first:normalize=0[amx]")
        amap = "[amx]"

    # Extra inputs come after the files, in this exact order — keep the indices in step.
    sil_idx = len(inputs)
    mus_idx = sil_idx + (1 if need_silence else 0)
    graph = graph.replace("__SIL__", str(sil_idx)).replace("__MUS__", str(mus_idx))

    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(graph + "\n")
        gfile = f.name
    try:
        cmd = ["ffmpeg", "-y"]
        for p in inputs:
            cmd += ["-i", p]
        if need_silence:
            cmd += ["-f", "lavfi", "-i", "anullsrc=channel_layout=stereo:sample_rate=48000"]
        if music:
            # Loop the bed so a short track still covers the whole cut.
            cmd += ["-stream_loop", "-1", "-i", music["src"]]
        cmd += ["-/filter_complex", gfile, "-map", vmap, "-map", amap,
                "-c:v", VCODEC, "-b:v", vbitrate, "-c:a", "aac", "-b:a", "192k",
                "-ar", "48000", "-movflags", "+faststart", out]
        _run(cmd)
    finally:
        os.unlink(gfile)
        if _av1_tmp_dir and os.path.isdir(_av1_tmp_dir):
            import shutil as _shutil
            _shutil.rmtree(_av1_tmp_dir, ignore_errors=True)
    return out


# ---------------------------------------------------------------------------
# Rhythm — pacing, dead air, beats, and snapping cuts to them
#
# The mechanical tools above decide WHERE a cut can land. These decide WHEN it should,
# which is what separates an edit that flows from one that's merely accurate.
# ---------------------------------------------------------------------------

def edl_report(edl_path: str) -> str:
    """Render an EDL's pacing as text — shot lengths, split edits, and rhythm warnings.

    "The edit is text" already applies to the cuts; rhythm is part of the edit. A cut list
    where every shot is the same length reads as monotone however good the picks are, and
    that's invisible in JSON until you lay the durations side by side.
    """
    import statistics

    edl = json.load(open(edl_path))
    clips = edl["clips"]
    durs = [float(c["end"]) - float(c["start"]) for c in clips]
    longest = max(durs)
    total = sum(durs)

    lines = [f"{'#':>3}  {'source':<22} {'in':>8} {'out':>8} {'dur':>7} {'lead':>6}  rhythm"]
    for i, (c, d) in enumerate(zip(clips, durs)):
        lead = float(c.get("audio_lead", 0.0))
        src = os.path.basename(str(c["src"]))
        src = "…" + src[-21:] if len(src) > 22 else src
        lines.append(
            f"{i:>3}  {src:<22} {float(c['start']):>8.2f} {float(c['end']):>8.2f} "
            f"{d:>7.2f} {(f'{lead:+.2f}' if lead else '·'):>6}  "
            + "█" * max(1, round(d / longest * 30)))

    mean = statistics.fmean(durs)
    cv = (statistics.pstdev(durs) / mean) if mean else 0.0
    lines += [
        "",
        f"{len(clips)} clips · {total:.1f}s · mean {mean:.2f}s · "
        f"median {statistics.median(durs):.2f}s · range {min(durs):.2f}–{max(durs):.2f}s · "
        f"variation {cv:.0%}",
        f"longest hold: clip {durs.index(longest)} ({longest:.2f}s)",
    ]

    notes = []
    if len(clips) >= 4 and cv < 0.15:
        notes.append("shot lengths barely vary — a cut list this even reads as monotone; "
                     "vary the holds and let them shorten toward the climax")
    if len(clips) >= 3 and not any(float(c.get("audio_lead", 0.0)) for c in clips):
        notes.append("no split edits — every cut changes picture and sound on the same "
                     "frame, which is the main thing that makes a cut feel abrupt; "
                     "add `audio_lead` on the cuts that should flow")
    if len(clips) >= 3 and durs[-1] < mean * 0.8:
        notes.append("the last shot is shorter than average — endings usually want a hold, "
                     "not the tightest cut in the piece")
    for note in notes:
        lines.append(f"  ⚠ {note}")
    return "\n".join(lines) + "\n"


def tighten(src: str, out: str, target_gap: float = 0.5, min_gap: float = 1.0,
            noise_db: float = -30.0, trim_ends: bool = False) -> dict:
    """Collapse dead air to a target beat and write the result as an EDL (not a video).

    Every silence longer than `min_gap` is shortened to `target_gap` by removing time from
    the **middle** of the pause, so the breath at the end of one sentence and the intake
    before the next both survive — cutting from the edges is what makes tightened speech
    sound clipped and breathless.

    This is the snappiness pass for talking-head footage, and it is deliberately distinct
    from `filler-removal`: the words all stay, only the gaps between them shrink. Output is
    an `edit.json` so the pacing decisions stay reviewable before anything is rendered.
    """
    info = video_info(src)
    dur = info["duration"]
    spans = speech_segments(src, noise_db=noise_db, min_silence=min(0.3, min_gap / 2))

    cuts = []                                     # (start, end) regions to remove
    for s0, s1, kind in spans:
        if kind != "silence" or s1 - s0 <= min_gap:
            continue
        leading = s0 <= 0.01
        trailing = s1 >= dur - 0.01
        if trim_ends and (leading or trailing):
            cuts.append((s0, s1))                 # drop head/tail silence outright
            continue
        half = target_gap / 2
        c0, c1 = s0 + half, s1 - half
        if c1 - c0 > 0.05:
            cuts.append((c0, c1))

    keeps, cursor = [], 0.0
    for c0, c1 in cuts:
        if c0 > cursor + 0.05:
            keeps.append((cursor, c0))
        cursor = c1
    if cursor < dur - 0.05:
        keeps.append((cursor, dur))

    clips = []
    for i, (k0, k1) in enumerate(keeps):
        if i < len(cuts):
            removed = cuts[i][1] - cuts[i][0]
            why = (f"hold, then a {removed:.2f}s pause removed "
                   f"(collapsed to ~{target_gap:.2f}s of breathing room)")
        else:
            why = "final segment — nothing after it to tighten against"
        clips.append({"src": src, "start": round(k0, 3), "end": round(k1, 3),
                      "rationale": why})

    edl = {"fps": round(info["fps"]), "width": info["width"], "height": info["height"],
           "clips": clips}
    with open(out, "w") as f:
        json.dump(edl, f, indent=2)

    removed_total = sum(c1 - c0 for c0, c1 in cuts)
    print(f"tightened {len(cuts)} pause(s): {dur:.1f}s → {dur - removed_total:.1f}s "
          f"(-{removed_total:.1f}s)")
    print(f"wrote {out} — review it, then: uv run video-agent edl {out} -o out.mp4")
    return edl


def _decode_mono(path: str, sr: int = 22050):
    """Decode any media file to a mono float32 numpy array at `sr` Hz."""
    import numpy as np
    p = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-f", "f32le", "-ac", "1",
         "-ar", str(sr), "-"],
        capture_output=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg failed decoding audio:\n{p.stderr.decode().strip()}")
    return np.frombuffer(p.stdout, dtype=np.float32)


def beats(path: str, sr: int = 22050, hop: int = 512, n_fft: int = 1024,
          min_bpm: float = 60.0, max_bpm: float = 200.0) -> dict:
    """Find musical beats and tempo → {"bpm", "beats": [s…], "onsets": [s…]}.

    Spectral-flux onset detection: how much energy *appeared* between one frame and the
    next (rises only — a note starting matters, a note ending doesn't). Tempo comes from
    autocorrelating that envelope, and the beat grid is the best-fitting phase of that
    period, so beats stay on the pulse through passages with no drum hit.

    Uses numpy + scipy, both already present via mlx-whisper — no new dependency.
    """
    import numpy as np
    from scipy.signal import find_peaks

    x = _decode_mono(path, sr)
    if x.size < n_fft * 4:
        raise RuntimeError(f"{path}: too short to find beats in")

    frames = np.lib.stride_tricks.sliding_window_view(x, n_fft)[::hop]
    mag = np.abs(np.fft.rfft(frames * np.hanning(n_fft), axis=1))
    flux = np.maximum(0.0, np.diff(mag, axis=0)).sum(axis=1)
    flux = np.concatenate([[0.0], flux])
    if flux.max() > 0:
        flux /= flux.max()
    fps_env = sr / hop

    # Onsets: local peaks that clear a moving local average (adapts to quiet/loud passages)
    win = max(3, int(fps_env * 0.5)) | 1
    local = np.convolve(flux, np.ones(win) / win, mode="same")
    peaks, _ = find_peaks(flux, height=local + 0.05, distance=max(1, int(fps_env * 0.08)))
    onsets = (peaks / fps_env).tolist()

    # Tempo: autocorrelate the onset envelope over the plausible beat-period range
    env = flux - flux.mean()
    ac = np.correlate(env, env, mode="full")[len(env) - 1:]
    lo = max(1, int(fps_env * 60.0 / max_bpm))
    hi = min(len(ac) - 1, int(fps_env * 60.0 / min_bpm))
    if hi <= lo:
        raise RuntimeError("audio too short to estimate tempo")
    # Autocorrelation peaks just as hard at half and double the real tempo, and it tends to
    # settle on the slow one. Weight by a log-normal prior around 120 BPM (Ellis 2007) so a
    # genuine 125 BPM track doesn't get reported — and cut — at 62.
    lags = np.arange(lo, hi, dtype=float)
    prior = np.exp(-0.5 * (np.log2((60.0 * fps_env / lags) / 120.0) / 0.9) ** 2)
    p = lo + int(np.argmax(ac[lo:hi] * prior))

    # Interpolate the autocorrelation peak to a FRACTIONAL period. A whole-frame period is
    # only accurate to ~23 ms, and that error compounds every beat — over a minute the grid
    # walks right off the music. The sub-frame fit is what keeps late beats on the pulse.
    if 0 < p < len(ac) - 1:
        denom = ac[p - 1] - 2 * ac[p] + ac[p + 1]
        if denom:
            p += float(np.clip(0.5 * (ac[p - 1] - ac[p + 1]) / denom, -0.5, 0.5))
    period_sec = p / fps_env
    bpm = 60.0 / period_sec

    # Phase: where the grid sits inside one beat. Take the energy-weighted circular mean of
    # the onsets modulo the period — averaging angles, not times, so onsets either side of a
    # beat boundary reinforce instead of cancelling.
    duration = len(flux) / fps_env
    if len(onsets):
        ang = 2 * np.pi * (np.asarray(onsets) % period_sec) / period_sec
        phase = float(np.angle((flux[peaks] * np.exp(1j * ang)).sum())) / (2 * np.pi)
        phase = (phase * period_sec) % period_sec
    else:
        phase = 0.0
    grid = (phase + np.arange(0, max(1, int((duration - phase) / period_sec) + 1))
            * period_sec)
    grid = grid[grid <= duration].tolist()

    return {"bpm": round(float(bpm), 2), "beats": [round(t, 3) for t in grid],
            "onsets": [round(t, 3) for t in onsets]}


def _nearest(candidates, t, tolerance):
    """Nearest candidate to `t` within `tolerance`, else None."""
    if not candidates:
        return None
    best = min(candidates, key=lambda c: abs(c - t))
    return best if abs(best - t) <= tolerance else None


def snap_edl(edl_path: str, out: str, to: str = "silence", ref: str = None,
             tolerance: float = 0.35, noise_db: float = -30.0,
             offset: float = 0.0) -> dict:
    """Move an EDL's cut points onto silence edges or musical beats, and report every move.

    Two modes, because the two things live on different timelines:

    - `silence` — each clip's in/out is nudged to the nearest speech/silence boundary in its
      **own source**. This is the snap-into-the-neighbouring-silence step that filler-removal
      and edl-edit both describe by hand; doing it in code makes it consistent and reviewable.
    - `beats` — cut points are moved so they land on the beat in the **output** timeline
      (cumulative running time), which is what "cut on the beat" actually means. `ref` is the
      music file; `offset` is the bed's own in-point (the EDL's `music.start`).

    Cuts that have no candidate within `tolerance` are left exactly where they were — a snap
    is a nudge onto a nearby truth, never a drag onto a distant one.
    """
    edl = json.load(open(edl_path))
    clips = edl["clips"]
    moves = []

    if to == "silence":
        cache = {}
        for i, c in enumerate(clips):
            src = c["src"]
            if src not in cache:
                spans = speech_segments(src, noise_db=noise_db)
                cache[src] = sorted({round(s, 3) for s0, s1, _ in spans for s in (s0, s1)})
            edges = cache[src]
            for key in ("start", "end"):
                t = float(c[key])
                hit = _nearest(edges, t, tolerance)
                if hit is not None and abs(hit - t) > 1e-3:
                    moves.append((i, key, t, hit))
                    c[key] = round(hit, 3)
            if c["end"] - c["start"] < 0.1:
                raise ValueError(f"clip {i}: snapping collapsed it to "
                                 f"{c['end'] - c['start']:.3f}s — lower --tolerance")

    elif to == "beats":
        if not ref:
            raise ValueError("--to beats needs --ref <music file> to find the beat grid")
        grid = [b - offset for b in beats(ref)["beats"]]
        t_out = 0.0
        for i, c in enumerate(clips[:-1]):          # the last out-point ends the piece
            dur = float(c["end"]) - float(c["start"])
            hit = _nearest(grid, t_out + dur, tolerance)
            if hit is not None and hit - t_out > 0.2 and abs(hit - t_out - dur) > 1e-3:
                new_end = round(float(c["start"]) + (hit - t_out), 3)
                moves.append((i, "end", float(c["end"]), new_end))
                c["end"] = new_end
                if "vend" in c:                     # keep a cutaway the same length as its audio
                    c["vend"] = round(float(c["vstart"]) + (hit - t_out), 3)
            t_out += float(c["end"]) - float(c["start"])
    else:
        raise ValueError(f"unknown snap target {to!r}; use 'silence' or 'beats'")

    with open(out, "w") as f:
        json.dump(edl, f, indent=2)

    if moves:
        print(f"snapped {len(moves)} cut point(s) to {to}:")
        for i, key, was, now in moves:
            print(f"  clip {i} {key}: {was:.3f} → {now:.3f}  ({now - was:+.3f}s)")
    else:
        print(f"no cut points were within {tolerance}s of a {to} candidate — nothing moved")
    print(f"wrote {out}")
    return edl


# ---------------------------------------------------------------------------
# Color grading — .cube / HALD LUTs via ffmpeg
# ---------------------------------------------------------------------------

def grade_apply(src: str, lut: str, out: str):
    """Apply a color grade LUT. `.cube` → lut3d; `.png` HALD-CLUT → haldclut."""
    if lut.lower().endswith(".png"):
        vf = f"[0:v][1:v]haldclut"
        _run(["ffmpeg", "-y", "-i", src, "-i", lut, "-filter_complex", vf,
              "-c:v", VCODEC, "-b:v", "9M", "-c:a", "copy", out])
    else:
        _run(["ffmpeg", "-y", "-i", src, "-vf", f"lut3d={lut}",
              "-c:v", VCODEC, "-b:v", "9M", "-c:a", "copy", out])
    return out


def grade_preview(src: str, at, luts: list, out: str):
    """Contact sheet: one frame graded by each candidate LUT, side by side, labelled.

    `luts` may include the literal "none" for the ungraded original. Labels are drawn with
    PIL (this ffmpeg is LGPL — no drawtext), like the captions command.
    """
    from PIL import Image, ImageDraw, ImageFont
    info = video_info(src)
    t = to_seconds(at, info["fps"])
    cw = 640
    cells = []
    with tempfile.TemporaryDirectory() as td:
        for n, lut in enumerate(luts):
            tile = os.path.join(td, f"t{n}.png")
            if lut == "none":
                _run(["ffmpeg", "-y", "-ss", str(t), "-i", src, "-frames:v", "1",
                      "-vf", f"scale={cw}:-2", tile]); label = "original"
            elif lut.lower().endswith(".png"):
                _run(["ffmpeg", "-y", "-ss", str(t), "-i", src, "-i", lut, "-frames:v", "1",
                      "-filter_complex", f"[0:v][1:v]haldclut,scale={cw}:-2", tile])
                label = os.path.basename(lut)
            else:
                _run(["ffmpeg", "-y", "-ss", str(t), "-i", src, "-frames:v", "1",
                      "-vf", f"lut3d={lut},scale={cw}:-2", tile]); label = os.path.basename(lut)
            cells.append((Image.open(tile).convert("RGB").copy(), label))
        ch = cells[0][0].height
        cols = min(3, len(cells)); rows = (len(cells) + cols - 1) // cols
        try:
            font = ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", 26)
        except Exception:
            font = ImageFont.load_default()
        sheet = Image.new("RGB", (cols * cw, rows * (ch + 30)), (15, 15, 15))
        dr = ImageDraw.Draw(sheet)
        for n, (img, label) in enumerate(cells):
            x, y = (n % cols) * cw, (n // cols) * (ch + 30)
            sheet.paste(img, (x, y + 30))
            dr.rectangle([x, y, x + cw, y + 30], fill=(0, 0, 0))
            dr.text((x + 6, y + 3), label, fill=(255, 255, 0), font=font)
        sheet.save(out)
    return out


def grade_gen_lut(eq: str, out: str, level: int = 8):
    """Bake an ffmpeg color-filter chain into a reusable LUT by running an identity
    HALD-CLUT through it. Output `.png` (use via haldclut) — fast, exact, no deps.

    `eq` is any ffmpeg video-filter chain, e.g. "eq=contrast=1.1:saturation=1.2,curves=..."
    """
    if not out.lower().endswith(".png"):
        out = out.rsplit(".", 1)[0] + ".png"
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"haldclutsrc=level={level}",
          "-vf", eq, "-frames:v", "1", out])
    return out


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
    tr.add_argument("--srt", action="store_true", help="output SubRip (.srt) subtitles")
    tr.add_argument("--vtt", action="store_true", help="output WebVTT (.vtt) subtitles")
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

    cap = sub.add_parser("captions", help="burn subtitles into video (auto-transcribe or from --srt)")
    cap.add_argument("src")
    cap.add_argument("-o", "--out", required=True)
    cap.add_argument("--srt", default=None, metavar="FILE",
                     help="use an existing .srt/.vtt file instead of auto-transcribing")
    cap.add_argument("--size", type=int, default=None, help="font size in px (default ~5%% of height)")
    cap.add_argument("--color", default="white", help="white|yellow|red|black")
    cap.add_argument("--position", default="bottom", choices=["bottom", "top", "center"])
    cap.add_argument("--no-box", action="store_true", help="disable the background box behind text")
    cap.add_argument("--model", default="mlx-community/whisper-large-v3-turbo")
    cap.add_argument("--clean", action="store_true", help="drop disfluencies when auto-transcribing")

    rf = sub.add_parser("reframe", help="reframe to a target aspect ratio (9:16, 1:1, 4:5) for social")
    rf.add_argument("src")
    rf.add_argument("-o", "--out", required=True)
    rf.add_argument("--aspect", default="9:16", help="target aspect W:H (default 9:16)")
    rf.add_argument("--mode", default="crop", choices=["crop", "pad"],
                    help="crop=full-bleed (loses edges); pad=fit with blurred-fill background")
    rf.add_argument("--focus", type=float, default=0.5,
                    help="crop bias along the cut axis, 0..1 (0=left/top, 0.5=center, 1=right/bottom)")
    rf.add_argument("--width", type=int, default=None, help="optional final output width in px")

    el = sub.add_parser("edl", help="execute an edit.json (clip list + rationale) → one video")
    el.add_argument("edit", help="path to edit.json (see edl-edit skill for schema)")
    el.add_argument("-o", "--out", required=True)
    el.add_argument("--draft", action="store_true",
                    help="fast 480p render for the internal verify loop (never a deliverable)")
    el.add_argument("--report", action="store_true",
                    help="print the pacing report (shot lengths + rhythm warnings) first")
    el.add_argument("--dry-run", action="store_true",
                    help="with --report: show the pacing and exit without rendering")

    tg = sub.add_parser("tighten",
                        help="collapse dead air to a target beat → writes an edit.json")
    tg.add_argument("src")
    tg.add_argument("-o", "--out", required=True, help="output edit.json")
    tg.add_argument("--target-gap", type=float, default=0.5,
                    help="what a collapsed pause becomes, in seconds (default 0.5)")
    tg.add_argument("--min-gap", type=float, default=1.0,
                    help="only pauses longer than this are touched (default 1.0)")
    tg.add_argument("--noise", type=float, default=-30.0,
                    help="silence threshold in dB (quieter rooms: -35 to -40)")
    tg.add_argument("--trim-ends", action="store_true",
                    help="also drop leading/trailing silence entirely")

    bt = sub.add_parser("beats", help="detect musical beats + tempo (for cutting on the beat)")
    bt.add_argument("src", help="music or video file")
    bt.add_argument("-o", "--out", default=None, help="output file (default: print)")
    bt.add_argument("--onsets", action="store_true",
                    help="list detected onsets instead of the inferred beat grid")

    sn = sub.add_parser("snap", help="move an EDL's cut points onto silence edges or beats")
    sn.add_argument("edit", help="input edit.json")
    sn.add_argument("-o", "--out", required=True, help="output edit.json")
    sn.add_argument("--to", default="silence", choices=["silence", "beats"])
    sn.add_argument("--ref", default=None,
                    help="reference file for --to beats (the music track)")
    sn.add_argument("--tolerance", type=float, default=0.35,
                    help="max distance a cut may be moved, in seconds (default 0.35)")
    sn.add_argument("--noise", type=float, default=-30.0, help="silence threshold in dB")
    sn.add_argument("--offset", type=float, default=0.0,
                    help="music in-point (the EDL's music.start) for --to beats")

    gr = sub.add_parser("grade", help="color-grade via .cube/HALD LUTs (apply | preview | gen-lut)")
    gr.add_argument("action", choices=["apply", "preview", "gen-lut"])
    gr.add_argument("src", nargs="?", help="input video (apply/preview)")
    gr.add_argument("--lut", action="append", default=[],
                    help=".cube or HALD .png LUT (repeatable for preview; 'none'=original)")
    gr.add_argument("--at", default="0.0", help="preview frame position (float secs / HH:MM:SS)")
    gr.add_argument("--eq", help="ffmpeg filter chain to bake into a LUT (gen-lut)")
    gr.add_argument("-o", "--out", required=True)

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
        print("transcribing…", flush=True)
        # Verbatim by default: whisper silently cleans hesitation sounds (um/uh) otherwise,
        # which breaks filler-removal. whisper_segments seeds the decoder to keep them;
        # --clean restores whisper's default behavior.
        segments = whisper_segments(a.src, model=a.model, clean=a.clean)
        if a.srt:
            text = format_srt(segments)
        elif a.vtt:
            text = format_vtt(segments)
        elif a.words:
            lines = [f"{w['start']:.3f}\t{w['end']:.3f}\t{w['word'].strip()}"
                     for seg in segments for w in seg.get("words", [])]
            text = "\n".join(lines) + "\n"
        else:
            lines = []
            for seg in segments:
                m0, s0 = divmod(seg["start"], 60)
                m1, s1 = divmod(seg["end"], 60)
                lines.append(f"[{int(m0):02d}:{s0:05.2f} --> {int(m1):02d}:{s1:05.2f}]  {seg['text'].strip()}")
            text = "\n".join(lines) + "\n"
        if a.out:
            with open(a.out, "w") as f:
                f.write(text)
            print(f"saved {len(segments)} segments to {a.out}")
        else:
            print(text, end="")

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

    elif a.cmd == "captions":
        if a.srt:
            segments = parse_subtitles(a.srt)
            print(f"loaded {len(segments)} caption(s) from {a.srt}", flush=True)
        else:
            print("transcribing…", flush=True)
            segments = whisper_segments(a.src, model=a.model, clean=a.clean)
        burn_captions(a.src, a.out, segments, size=a.size, color=a.color,
                      position=a.position, box=not a.no_box)
        print(a.out)

    elif a.cmd == "reframe":
        reframe(a.src, a.out, aspect=a.aspect, mode=a.mode,
                focus=a.focus, width=a.width)
        print(a.out)

    elif a.cmd == "edl":
        if a.report or a.dry_run:
            print(edl_report(a.edit), end="")
        if not a.dry_run:
            edl_render(a.edit, a.out, draft=a.draft)
            print(a.out)

    elif a.cmd == "tighten":
        tighten(a.src, a.out, target_gap=a.target_gap, min_gap=a.min_gap,
                noise_db=a.noise, trim_ends=a.trim_ends)

    elif a.cmd == "beats":
        b = beats(a.src)
        times = b["onsets"] if a.onsets else b["beats"]
        text = (f"# bpm {b['bpm']}  ({len(b['beats'])} beats, {len(b['onsets'])} onsets)\n"
                + "\n".join(f"{t:.3f}" for t in times) + "\n")
        if a.out:
            with open(a.out, "w") as f:
                f.write(text)
            print(f"bpm {b['bpm']} — wrote {len(times)} times to {a.out}")
        else:
            print(text, end="")

    elif a.cmd == "snap":
        snap_edl(a.edit, a.out, to=a.to, ref=a.ref, tolerance=a.tolerance,
                 noise_db=a.noise, offset=a.offset)

    elif a.cmd == "grade":
        if a.action == "apply":
            if not a.lut:
                p.error("grade apply needs --lut <file>")
            grade_apply(a.src, a.lut[0], a.out)
        elif a.action == "preview":
            grade_preview(a.src, a.at, a.lut or ["none"], a.out)
        elif a.action == "gen-lut":
            if not a.eq:
                p.error("grade gen-lut needs --eq <filter chain>")
            a.out = grade_gen_lut(a.eq, a.out)
        print(a.out)


if __name__ == "__main__":
    main()
