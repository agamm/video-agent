---
name: reframe-social
description: Reframe a video to a different aspect ratio for social platforms — vertical 9:16 (Reels/Shorts/TikTok), square 1:1, or 4:5 portrait — from horizontal source. Use whenever the user wants to make a video vertical/square, fit it for Instagram/TikTok/YouTube Shorts, repurpose a 16:9 clip for social, or crop/pad to an aspect ratio. Covers crop-vs-pad choice, keeping an off-center subject in frame, and following a moving subject.
---

# Reframe for social

`reframe` converts to a target aspect ratio two ways. Pick by what the footage is:

| Footage | Mode | Why |
|---|---|---|
| Talking head, single subject | `--mode crop` (default) | Full-bleed vertical; subject fills frame |
| Screencast, gameplay, slides, anything where edges matter | `--mode pad` | Keeps the whole frame; blurred-fill background |

```bash
# Vertical 9:16 talking-head (full-bleed crop, subject centered)
uv run video-agent reframe in.mp4 -o reel.mp4 --aspect 9:16

# Vertical with blurred-background fill (nothing cropped)
uv run video-agent reframe in.mp4 -o reel.mp4 --aspect 9:16 --mode pad --width 1080

# Square / portrait
uv run video-agent reframe in.mp4 -o sq.mp4 --aspect 1:1
uv run video-agent reframe in.mp4 -o pt.mp4 --aspect 4:5
```

`--width` sets the final output width (height follows the aspect); omit to keep native
resolution. For 9:16 social, `--width 1080` → 1080×1920, the standard upload size.

## Keeping an off-center subject in frame (crop mode)

Crop defaults to **center** (`--focus 0.5`). If the speaker sits left/right of center, the
default crop clips them. There's **no face tracking** (by design). Set the crop bias by hand:

1. Read where the subject is: `uv run video-agent position-grid in.mp4 --at <t> --spacing 200 -o grid.png`, open it, note the subject's center x.
2. `focus = subject_center_x / frame_width` (0 = hug left edge, 1 = hug right edge).
3. `uv run video-agent reframe in.mp4 -o reel.mp4 --aspect 9:16 --focus 0.62`
4. Verify: `uv run video-agent frame reel.mp4 --at <t> -o check.png` and look.

## Following a subject that MOVES across the shot

One fixed `--focus` can't track motion. Cut the clip at the points where the subject moves,
reframe each piece with its own focus, then concat:

```bash
# subject starts left, walks right around 4s
uv run video-agent trim in.mp4 --start 0.0 --end 4.0 -o a.mp4
uv run video-agent trim in.mp4 --start 4.0 --end 8.0 -o b.mp4
uv run video-agent reframe a.mp4 -o ar.mp4 --aspect 9:16 --focus 0.3
uv run video-agent reframe b.mp4 -o br.mp4 --aspect 9:16 --focus 0.7
uv run video-agent concat ar.mp4 br.mp4 -o reel.mp4
```

Use `detect` over the range to find exactly where the subject crosses, so the cut lands on
the move (not mid-gesture).

## Gotchas

- **Crop loses the edges** — always eyeball a `frame` from the output before shipping; a
  gesture or second person at the edge may be gone.
- **Pad mode never upscales the content** — it scales to *fit* and fills the rest with a
  blurred, zoomed copy. Output canvas can be large without `--width`; pass `--width` to pin
  the size.
- **Caption after reframing, not before** — so caption size/wrapping match the final frame.
  See the `captions` skill.
- **Audio is re-encoded** (aac); video uses `h264_videotoolbox`. After reframe, a further
  join must use filter_complex concat or the `concat` command (it re-encodes audio).
