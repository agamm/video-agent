"""MP4 video toolkit — frame detection, Grok editing, transcription, and YouTube download.

For all other editing use ffmpeg directly. See CLAUDE.md.

CLI:
    uv run video-agent info <src>
    uv run video-agent yt-dl <url> [-o path]
    uv run video-agent transcribe <src> [-o transcript.txt]
    uv run video-agent detect <src> --start S --end E -o grids/
    uv run video-agent grok-edit <src> --prompt "Make sunglasses red" -o out.mp4

Position values: bare integer = frame number; float/timestamp = seconds.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile

# H.264 encoder. VideoToolbox = macOS hardware; swap to "libx264" elsewhere.
VCODEC = "h264_videotoolbox"


def xai_client():
    """Return an xai_sdk.Client(), raising clearly if XAI_API_KEY is not set."""
    from dotenv import load_dotenv
    load_dotenv()
    key = os.environ.get("XAI_API_KEY")
    if not key:
        raise RuntimeError(
            "XAI_API_KEY is not set. Add it to .env:\n  XAI_API_KEY=xai-..."
        )
    from xai_sdk import Client
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
    """Return {fps, width, height, duration, nframes} for the first video stream."""
    out = _run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                "-show_entries", "stream=r_frame_rate,width,height,duration,nb_frames",
                "-show_entries", "format=duration", "-of", "json", path])
    data = json.loads(out)
    s = data["streams"][0]
    num, den = s["r_frame_rate"].split("/")
    fps = float(num) / float(den)
    duration = float(s.get("duration") or data["format"]["duration"])
    nframes = (int(s["nb_frames"]) if s.get("nb_frames", "N/A") != "N/A"
               else round(duration * fps))
    return {"fps": fps, "width": int(s["width"]), "height": int(s["height"]),
            "duration": duration, "nframes": nframes}


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
# Frame detection — builds grid montages for Claude to inspect visually
# ---------------------------------------------------------------------------

def detect(src: str, start, end, out_dir: str, step=None,
           cols=8, rows=8, cell_w=192) -> dict:
    """Extract frames from [start, end], pack into labeled grid montages, save to out_dir.

    Returns a mapping {grid_filename: [frame_numbers]} and saves mapping.json alongside
    the grids. Open the grid images in Claude Code to identify matching frames, then use
    the mapping to convert cell indices back to frame numbers.
    """
    from PIL import Image, ImageDraw, ImageFont

    info = video_info(src)
    fps = info["fps"]
    if step is None:
        step = max(1, round(0.1 * fps))
    cell_h = max(2, round(cell_w * info["height"] / info["width"] / 2) * 2)
    label_h = 28
    f0, f1 = to_frame(start, fps), to_frame(end, fps)
    wanted = list(range(f0, f1 + 1, step))
    if not wanted:
        return {}

    os.makedirs(out_dir, exist_ok=True)

    with tempfile.TemporaryDirectory() as td:
        sel = f"between(n\\,{f0}\\,{f1})*not(mod(n-{f0}\\,{step}))"
        _run(["ffmpeg", "-y", "-i", src, "-vf",
              f"select='{sel}',scale={cell_w}:-2", "-vsync", "0", f"{td}/%06d.png"])
        raw = sorted(f for f in os.listdir(td) if f.endswith(".png"))
        frames = [(wanted[i], Image.open(os.path.join(td, f)))
                  for i, f in enumerate(raw) if i < len(wanted)]

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
# Grok video editing
# ---------------------------------------------------------------------------

GROK_MAX_SECONDS = 8.0  # Undocumented limit; empirically found to be 8.7s from API error messages


def _start_tunnel(serve_dir: str):
    """Start a local HTTP server + cloudflared tunnel. Returns (tunnel_url, cf_proc, httpd)."""
    import re
    import threading
    from http.server import HTTPServer, SimpleHTTPRequestHandler

    orig = os.getcwd()
    os.chdir(serve_dir)

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *_): pass

    httpd = HTTPServer(("", 8724), QuietHandler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    os.chdir(orig)

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


def grok_edit(src: str, prompt: str, out: str) -> str:
    """Edit a video using Grok (grok-imagine-video). Handles chunking for videos > 8s,
    auto-starts a local tunnel so Grok can fetch the clips, then concats and re-stitches
    the original audio."""
    import datetime
    import requests

    client = xai_client()
    info = video_info(src)
    duration = info["duration"]

    with tempfile.TemporaryDirectory() as td:
        # Split into ≤8s chunks
        chunks = []
        t = 0.0
        i = 0
        while t < duration:
            end = min(t + GROK_MAX_SECONDS, duration)
            chunk = os.path.join(td, f"chunk_{i:03d}.mp4")
            _run(["ffmpeg", "-y", "-ss", f"{t:.3f}", "-i", src, "-t", f"{end-t:.3f}",
                  "-c:v", VCODEC, "-an", chunk])
            chunks.append(chunk)
            t = end
            i += 1

        print(f"editing {len(chunks)} chunk(s) via Grok…", flush=True)

        # Start tunnel — serve the temp dir
        tunnel_url, cf, httpd = _start_tunnel(td)
        print(f"tunnel: {tunnel_url}", flush=True)

        edited = []
        try:
            for idx, chunk in enumerate(chunks):
                name = os.path.basename(chunk)
                url = f"{tunnel_url}/{name}"
                print(f"  chunk {idx+1}/{len(chunks)}…", end=" ", flush=True)
                resp = client.video.generate(
                    prompt=prompt,
                    model="grok-imagine-video",
                    video_url=url,
                    resolution="720p",
                    timeout=datetime.timedelta(minutes=8),
                    interval=datetime.timedelta(seconds=6),
                )
                out_chunk = os.path.join(td, f"edited_{idx:03d}.mp4")
                open(out_chunk, "wb").write(requests.get(resp.url, timeout=120).content)
                edited.append(out_chunk)
                print("done", flush=True)
        finally:
            cf.terminate()
            httpd.shutdown()

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
    d.add_argument("--step", type=int, default=None,
                   help="sample every Nth frame (default: every 0.1s)")
    d.add_argument("--cols", type=int, default=8)
    d.add_argument("--rows", type=int, default=8)
    d.add_argument("--cell-w", type=int, default=192)

    inf = sub.add_parser("info", help="print video metadata (fps, resolution, duration, frames)")
    inf.add_argument("src")

    tr = sub.add_parser("transcribe", help="transcribe audio with timestamps (mlx-whisper)")
    tr.add_argument("src")
    tr.add_argument("-o", "--out", default=None, help="output .txt file (default: print to stdout)")
    tr.add_argument("--words", action="store_true",
                    help="output one word per line with precise timestamps (use for cut-point editing)")
    tr.add_argument("--model", default="mlx-community/whisper-large-v3-turbo",
                    help="mlx-whisper model repo")

    ge = sub.add_parser("grok-edit", help="edit video with Grok AI (auto-handles chunking + tunnel)")
    ge.add_argument("src")
    ge.add_argument("--prompt", required=True, help="edit instruction, e.g. 'make sunglasses red'")
    ge.add_argument("-o", "--out", required=True)

    a = p.parse_args()

    if a.cmd == "info":
        info = video_info(a.src)
        print(f"duration:   {info['duration']:.3f}s")
        print(f"fps:        {info['fps']}")
        print(f"resolution: {info['width']}x{info['height']}")
        print(f"frames:     {info['nframes']}")

    elif a.cmd == "transcribe":
        import mlx_whisper
        print("transcribing…", flush=True)
        result = mlx_whisper.transcribe(
            a.src,
            path_or_hf_repo=a.model,
            word_timestamps=True,
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

    elif a.cmd == "grok-edit":
        print(grok_edit(a.src, a.prompt, a.out))


if __name__ == "__main__":
    main()
