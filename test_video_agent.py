"""Tests for video_agent.py — one file, no conftest."""
import json
import os
import subprocess

import pytest
import video_agent as va


@pytest.fixture(scope="session")
def test_mp4(tmp_path_factory):
    path = tmp_path_factory.mktemp("media") / "test.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi",
         "-i", "testsrc=duration=5:size=320x240:rate=30",
         "-pix_fmt", "yuv420p", str(path)],
        check=True, capture_output=True,
    )
    return str(path)


def test_video_info(test_mp4):
    info = va.video_info(test_mp4)
    assert round(info["fps"]) == 30
    assert (info["width"], info["height"]) == (320, 240)
    assert abs(info["duration"] - 5.0) < 0.2


def test_position_conversions():
    fps = 30.0
    assert va.to_seconds(45, fps) == 1.5
    assert va.to_seconds("1.5", fps) == 1.5
    assert va.to_seconds("00:00:01.5", fps) == 1.5
    assert va.to_frame("1.5", fps) == 45
    assert va.to_frame(45, fps) == 45


def test_detect_builds_grids(test_mp4, tmp_path):
    out = str(tmp_path / "grids")
    mapping = va.detect(test_mp4, 0, 30, out, step=10)
    frames = [f for v in mapping.values() for f in v]
    assert 0 in frames and 20 in frames
    for fname in mapping:
        assert (tmp_path / "grids" / fname).exists()
    assert json.loads((tmp_path / "grids" / "mapping.json").read_text()) == mapping


def test_format_srt_and_vtt():
    segs = [{"start": 0.0, "end": 2.5, "text": " Hello world "},
            {"start": 2.5, "end": 4.0, "text": "second line"}]
    srt = va.format_srt(segs)
    assert "1\n00:00:00,000 --> 00:00:02,500\nHello world" in srt
    assert "2\n00:00:02,500 --> 00:00:04,000\nsecond line" in srt
    vtt = va.format_vtt(segs)
    assert vtt.startswith("WEBVTT")
    assert "00:00:00.000 --> 00:00:02.500" in vtt  # dot separator, no index


def test_parse_subtitles_roundtrip(tmp_path):
    segs = [{"start": 1.0, "end": 2.5, "text": "alpha beta"},
            {"start": 3.0, "end": 4.0, "text": "gamma"}]
    f = tmp_path / "s.srt"
    f.write_text(va.format_srt(segs))
    parsed = va.parse_subtitles(str(f))
    assert [p["text"] for p in parsed] == ["alpha beta", "gamma"]
    assert abs(parsed[0]["start"] - 1.0) < 1e-3 and abs(parsed[0]["end"] - 2.5) < 1e-3


def test_burn_captions_outputs_video(test_mp4, tmp_path):
    out = str(tmp_path / "capped.mp4")
    segs = [{"start": 0.5, "end": 2.0, "text": "first caption here"},
            {"start": 2.0, "end": 4.0, "text": "second caption"}]
    va.burn_captions(test_mp4, out, segs)
    assert os.path.exists(out)
    assert abs(va.video_info(out)["duration"] - va.video_info(test_mp4)["duration"]) < 0.3


def test_reframe_crop_aspect(test_mp4, tmp_path):
    out = str(tmp_path / "vert.mp4")
    va.reframe(test_mp4, out, aspect="9:16", mode="crop")
    info = va.video_info(out)
    assert abs(info["width"] / info["height"] - 9 / 16) < 0.02


def test_reframe_pad_square(test_mp4, tmp_path):
    out = str(tmp_path / "sq.mp4")
    va.reframe(test_mp4, out, aspect="1:1", mode="pad")
    info = va.video_info(out)
    assert info["width"] == info["height"]


def test_xai_client_missing_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    # patch load_dotenv so it doesn't re-read the .env file
    monkeypatch.setattr("video_agent.os.environ.get", lambda k, *a: None if k == "XAI_API_KEY" else os.environ.get(k, *a))
    import os
    with pytest.raises(RuntimeError, match="XAI_API_KEY"):
        va.xai_client()
