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


@pytest.fixture(scope="session")
def av_mp4(tmp_path_factory):
    """8s clip WITH an audio track — needed for anything touching the audio timeline."""
    path = tmp_path_factory.mktemp("media") / "av.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=8:size=320x240:rate=30",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=8",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True,
    )
    return str(path)


@pytest.fixture(scope="session")
def music_wav(tmp_path_factory):
    # .wav, not .mp3 — this ffmpeg is LGPL and has no mp3 *encoder* (decoding is fine)
    path = tmp_path_factory.mktemp("media") / "bed.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "sine=frequency=220:duration=3", str(path)],
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


def test_edl_render_concats_clips(test_mp4, tmp_path):
    edit = {"fps": 30, "width": 320, "height": 240, "clips": [
        {"src": test_mp4, "start": 0.0, "end": 2.0, "rationale": "head"},
        {"src": test_mp4, "start": 3.0, "end": 5.0, "rationale": "tail"},
    ]}
    ep = tmp_path / "edit.json"
    ep.write_text(json.dumps(edit))
    out = str(tmp_path / "edl.mp4")
    va.edl_render(str(ep), out)
    assert os.path.exists(out)
    assert abs(va.video_info(out)["duration"] - 4.0) < 0.3  # 2.0 + 2.0


def test_edl_render_multicam_vsrc(test_mp4, tmp_path):
    # vsrc takes picture from a second source while audio stays on src — must not error.
    edit = {"fps": 30, "width": 320, "height": 240, "clips": [
        {"src": test_mp4, "start": 0.0, "end": 2.0,
         "vsrc": test_mp4, "vstart": 2.0, "vend": 4.0, "rationale": "cutaway"},
    ]}
    ep = tmp_path / "e2.json"
    ep.write_text(json.dumps(edit))
    out = str(tmp_path / "edl2.mp4")
    va.edl_render(str(ep), out)
    assert os.path.exists(out)
    assert abs(va.video_info(out)["duration"] - 2.0) < 0.3


def test_edl_audio_windows_split_edit():
    # clip 1 asks for its sound 0.4s early (J-cut): clip 0's audio ends 0.4s sooner,
    # clip 1's audio starts 0.4s sooner. Video boundaries are untouched.
    clips = [{"src": "a", "start": 0.0, "end": 5.0},
             {"src": "a", "start": 10.0, "end": 15.0, "audio_lead": 0.4}]
    assert va._edl_audio_windows(clips) == [(0.0, 4.6), (9.6, 15.0)]
    # total audio length still equals total video length — the leads telescope
    assert sum(e - s for s, e in va._edl_audio_windows(clips)) == 10.0


def test_edl_audio_windows_l_cut_and_guards():
    # negative lead = L-cut: the previous clip's sound holds over the new picture
    clips = [{"src": "a", "start": 0.0, "end": 5.0},
             {"src": "a", "start": 10.0, "end": 15.0, "audio_lead": -0.5}]
    assert va._edl_audio_windows(clips) == [(0.0, 5.5), (10.5, 15.0)]
    # a lead bigger than the clip it eats into is rejected, not silently rendered
    with pytest.raises(ValueError, match="audio_lead"):
        va._edl_audio_windows([{"src": "a", "start": 0.0, "end": 1.0},
                               {"src": "a", "start": 9.0, "end": 10.0, "audio_lead": 2.0}])


def test_edl_check_cutaway_rejects_length_mismatch():
    bad = {"src": "a", "start": 0.0, "end": 8.0,
           "vsrc": "b", "vstart": 0.0, "vend": 5.0}
    with pytest.raises(ValueError, match="cutaway"):
        va._edl_check_cutaway(0, bad, 30)
    ok = dict(bad, vend=8.0)
    va._edl_check_cutaway(0, ok, 30)  # matching lengths: no error


def test_edl_render_split_edit_keeps_duration(av_mp4, tmp_path):
    edit = {"fps": 30, "width": 320, "height": 240, "clips": [
        {"src": av_mp4, "start": 0.0, "end": 3.0, "rationale": "question"},
        {"src": av_mp4, "start": 4.0, "end": 7.0, "audio_lead": 0.5,
         "rationale": "answer starts under the tail of the question"},
    ]}
    ep = tmp_path / "split.json"
    ep.write_text(json.dumps(edit))
    out = str(tmp_path / "split.mp4")
    va.edl_render(str(ep), out)
    assert abs(va.video_info(out)["duration"] - 6.0) < 0.3


def test_edl_render_hald_grade(av_mp4, tmp_path):
    # gen-lut only ever emits HALD .png, so the EDL must accept it (lut3d cannot read PNG)
    lut = va.grade_gen_lut("curves=all='0/0 0.5/0.6 1/1'", str(tmp_path / "look.png"))
    edit = {"fps": 30, "width": 320, "height": 240, "grade": lut,
            "clips": [{"src": av_mp4, "start": 0.0, "end": 2.0, "rationale": "graded"}]}
    ep = tmp_path / "graded.json"
    ep.write_text(json.dumps(edit))
    out = str(tmp_path / "graded_edl.mp4")
    va.edl_render(str(ep), out)
    assert abs(va.video_info(out)["duration"] - 2.0) < 0.3


def test_edl_render_music_bed_and_punch(av_mp4, music_wav, tmp_path):
    edit = {"fps": 30, "width": 320, "height": 240,
            "music": {"src": music_wav, "gain_db": -18, "duck": True, "fade_out": 0.5},
            "clips": [{"src": av_mp4, "start": 0.0, "end": 4.0, "punch": [1.0, 1.08],
                       "rationale": "bed loops to cover, slow push"}]}
    ep = tmp_path / "music.json"
    ep.write_text(json.dumps(edit))
    out = str(tmp_path / "music.mp4")
    va.edl_render(str(ep), out)
    # 3s bed must loop to cover a 4s cut without truncating the video
    assert abs(va.video_info(out)["duration"] - 4.0) < 0.3


def test_edl_render_draft_is_smaller(av_mp4, tmp_path):
    edit = {"fps": 30, "width": 1920, "height": 1080,
            "clips": [{"src": av_mp4, "start": 0.0, "end": 2.0, "rationale": "x"}]}
    ep = tmp_path / "d.json"
    ep.write_text(json.dumps(edit))
    out = str(tmp_path / "draft.mp4")
    va.edl_render(str(ep), out, draft=True)
    assert va.video_info(out)["height"] == 480


def test_edl_quantize_snaps_to_frame_grid():
    clips = [{"src": "a", "start": 0.0, "end": 3.163, "audio_lead": 0.31,
              "vstart": 1.007, "vend": 4.17}]
    q = va._edl_quantize(clips, 24)[0]
    for k in ("start", "end", "vstart", "vend", "audio_lead"):
        assert abs(q[k] * 24 - round(q[k] * 24)) < 1e-6, (k, q[k])
    assert abs(q["end"] - 3.16667) < 1e-3


def test_edl_no_av_drift_on_fractional_cuts(av_mp4, tmp_path):
    # cut points off the frame grid (what snap/tighten produce) used to leave the picture
    # up to a frame longer than the sound per clip — that must not accumulate
    edit = {"fps": 30, "width": 320, "height": 240, "clips": [
        {"src": av_mp4, "start": 0.0, "end": 1.317},
        {"src": av_mp4, "start": 2.041, "end": 3.229, "audio_lead": 0.213},
        {"src": av_mp4, "start": 4.111, "end": 5.887},
        {"src": av_mp4, "start": 6.003, "end": 7.449},
    ]}
    ep = tmp_path / "frac.json"
    ep.write_text(json.dumps(edit))
    out = str(tmp_path / "frac.mp4")
    va.edl_render(str(ep), out)
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,duration",
         "-of", "json", out], capture_output=True, text=True).stdout
    d = {s["codec_type"]: float(s["duration"]) for s in json.loads(probe)["streams"]}
    assert abs(d["video"] - d["audio"]) < 1.0 / 30, d   # under one frame, not per-clip


def test_edl_report_flags_monotone_cut_list(av_mp4, tmp_path):
    edit = {"fps": 30, "clips": [
        {"src": av_mp4, "start": s, "end": s + 2.0, "rationale": "x"}
        for s in (0.0, 2.0, 4.0, 6.0)]}
    ep = tmp_path / "flat.json"
    ep.write_text(json.dumps(edit))
    rep = va.edl_report(str(ep))
    assert "4 clips" in rep and "8.0s" in rep
    assert "monotone" in rep          # every shot is 2.0s
    assert "no split edits" in rep    # no audio_lead anywhere


def test_edl_report_quiet_when_pacing_varies(av_mp4, tmp_path):
    edit = {"fps": 30, "clips": [
        {"src": av_mp4, "start": 0.0, "end": 4.0},
        {"src": av_mp4, "start": 4.0, "end": 5.2, "audio_lead": 0.3},
        {"src": av_mp4, "start": 5.2, "end": 5.8},
        {"src": av_mp4, "start": 6.0, "end": 9.0},
    ]}
    ep = tmp_path / "varied.json"
    ep.write_text(json.dumps(edit))
    rep = va.edl_report(str(ep))
    assert "monotone" not in rep and "no split edits" not in rep


@pytest.fixture(scope="session")
def gappy_mp4(tmp_path_factory):
    """Tone, 3s silence, tone — a 3s pause a pacing pass should collapse."""
    path = tmp_path_factory.mktemp("media") / "gappy.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i", "testsrc=duration=9:size=320x240:rate=30",
         "-f", "lavfi", "-i",
         "aevalsrc='if(lt(t,3)+gt(t,6),0.5*sin(880*2*PI*t),0)':d=9",
         "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(path)],
        check=True, capture_output=True)
    return str(path)


def test_tighten_collapses_the_pause(gappy_mp4, tmp_path):
    out = str(tmp_path / "tight.json")
    edl = va.tighten(gappy_mp4, out, target_gap=0.5, min_gap=1.0)
    kept = sum(c["end"] - c["start"] for c in edl["clips"])
    # the ~3s pause should survive as roughly target_gap, not vanish and not stay full
    assert 6.0 < kept < 7.5, kept
    assert len(edl["clips"]) == 2
    assert json.loads(open(out).read())["clips"] == edl["clips"]


def test_tighten_output_renders(gappy_mp4, tmp_path):
    ej = str(tmp_path / "t.json")
    va.tighten(gappy_mp4, ej, target_gap=0.5, min_gap=1.0)
    out = str(tmp_path / "t.mp4")
    va.edl_render(ej, out, draft=True)
    assert os.path.exists(out)


@pytest.fixture(scope="session")
def click_track(tmp_path_factory):
    """120 BPM click — a beat every 0.5s."""
    path = tmp_path_factory.mktemp("media") / "click.wav"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "aevalsrc='sin(1000*2*PI*t)*exp(-40*mod(t,0.5))':d=12:s=22050", str(path)],
        check=True, capture_output=True)
    return str(path)


def test_beats_finds_the_tempo(click_track):
    b = va.beats(click_track)
    assert abs(b["bpm"] - 120.0) < 6.0, b["bpm"]
    assert len(b["beats"]) > 10
    gaps = [j - i for i, j in zip(b["beats"], b["beats"][1:])]
    assert abs(sum(gaps) / len(gaps) - 0.5) < 0.05


def test_snap_to_beats_moves_cut_onto_the_grid(av_mp4, click_track, tmp_path):
    # clip 0 runs 1.35s; the 120bpm grid has a beat at 1.5s, within tolerance
    edit = {"fps": 30, "clips": [
        {"src": av_mp4, "start": 0.0, "end": 1.35, "rationale": "a"},
        {"src": av_mp4, "start": 2.0, "end": 4.0, "rationale": "b"},
    ]}
    ep = tmp_path / "pre.json"
    ep.write_text(json.dumps(edit))
    out = str(tmp_path / "post.json")
    snapped = va.snap_edl(str(ep), out, to="beats", ref=click_track, tolerance=0.35)
    assert abs(snapped["clips"][0]["end"] - 1.5) < 0.06
    assert snapped["clips"][1]["end"] == 4.0     # last out-point ends the piece, untouched


def test_snap_to_silence_moves_cut_to_a_boundary(gappy_mp4, tmp_path):
    # a cut at 2.9s should pull onto the ~3.0s speech→silence edge
    edit = {"fps": 30, "clips": [{"src": gappy_mp4, "start": 0.0, "end": 2.9}]}
    ep = tmp_path / "s_pre.json"
    ep.write_text(json.dumps(edit))
    out = str(tmp_path / "s_post.json")
    snapped = va.snap_edl(str(ep), out, to="silence", tolerance=0.35)
    assert abs(snapped["clips"][0]["end"] - 3.0) < 0.2, snapped["clips"][0]


def test_snap_leaves_distant_cuts_alone(av_mp4, click_track, tmp_path):
    edit = {"fps": 30, "clips": [{"src": av_mp4, "start": 0.0, "end": 1.25},
                                 {"src": av_mp4, "start": 2.0, "end": 4.0}]}
    ep = tmp_path / "far.json"
    ep.write_text(json.dumps(edit))
    out = str(tmp_path / "far_out.json")
    snapped = va.snap_edl(str(ep), out, to="beats", ref=click_track, tolerance=0.05)
    assert snapped["clips"][0]["end"] == 1.25    # nothing within 0.05s → untouched


def test_grade_gen_and_apply(test_mp4, tmp_path):
    lut = str(tmp_path / "look.png")
    va.grade_gen_lut("curves=all='0/0 0.5/0.6 1/1'", lut)
    assert os.path.exists(lut)
    out = str(tmp_path / "graded.mp4")
    va.grade_apply(test_mp4, lut, out)
    assert os.path.exists(out)
    assert abs(va.video_info(out)["duration"] - va.video_info(test_mp4)["duration"]) < 0.3


def test_xai_client_missing_key(monkeypatch):
    monkeypatch.delenv("XAI_API_KEY", raising=False)
    # patch load_dotenv so it doesn't re-read the .env file
    monkeypatch.setattr("video_agent.os.environ.get", lambda k, *a: None if k == "XAI_API_KEY" else os.environ.get(k, *a))
    import os
    with pytest.raises(RuntimeError, match="XAI_API_KEY"):
        va.xai_client()
