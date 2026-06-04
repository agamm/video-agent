#!/usr/bin/env python3
"""Start a local HTTP server + cloudflared tunnel for Grok video editing.

Usage:
    uv run python tunnel.py                    # serves current directory
    uv run python tunnel.py path/to/file.mp4   # copies file to serve dir, prints URL
    uv run python tunnel.py path/to/dir/       # serves that directory
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

PORT = 8723


def start_server(directory: str) -> HTTPServer:
    os.chdir(directory)

    class QuietHandler(SimpleHTTPRequestHandler):
        def log_message(self, *args):
            pass

    server = HTTPServer(("", PORT), QuietHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def get_tunnel_url(timeout=15):
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    deadline = time.time() + timeout
    for line in proc.stdout:
        if time.time() > deadline:
            break
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            return m.group(0), proc
    raise RuntimeError("cloudflared didn't print a URL within timeout")


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("-h", "--help"):
        print(__doc__); sys.exit(0)

    arg = sys.argv[1] if len(sys.argv) > 1 else "."
    path = Path(arg).expanduser().resolve()

    serve_dir = tempfile.mkdtemp(prefix="tunnel_serve_")

    if path.is_file():
        dest = Path(serve_dir) / path.name
        shutil.copy2(path, dest)
        filename = path.name
    elif path.is_dir():
        serve_dir = str(path)
        filename = None
    else:
        print(f"error: {arg!r} is not a file or directory", file=sys.stderr)
        sys.exit(1)

    print(f"serving: {serve_dir}", flush=True)
    start_server(serve_dir)

    print("starting cloudflared tunnel...", flush=True)
    tunnel_url, proc = get_tunnel_url()

    print(f"\n  tunnel: {tunnel_url}")
    if filename:
        print(f"  file:   {tunnel_url}/{filename}")
        print(f"\n  use in Grok: video_url=\"{tunnel_url}/{filename}\"")
    else:
        print(f"\n  use in Grok: video_url=\"{tunnel_url}/<filename>\"")

    print("\nCtrl+C to stop.\n")
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        print("\ntunnel stopped.")


if __name__ == "__main__":
    main()
