---
name: video-transitions
description: Join two clips with a visible transition effect (wipe, slide, dissolve, pixelize, circle open/close, fade-to-black, etc.) using ffmpeg xfade. Use whenever the user wants a transition between shots/scenes, a scene change with an effect, or anything beyond a hard cut. For a barely-visible seam between adjacent cuts use the splice command instead; for generative effects on the footage itself use grok-video-edit.
---

# Video transitions (ffmpeg xfade)

Join two clips with a deliberate, visible transition. This is plain ffmpeg `xfade` — no
CLI command wraps it (it's a one-liner; only unique-code operations get commands). Use the
recipe below directly.

Decide which tool first:
- **Visible transition between two shots** (wipe, dissolve, pixelize…) → this skill, `xfade`.
- **A cut that feels abrupt but shouldn't have a visible effect** → you want a **split edit**,
  not a transition: stagger the sound and picture with `audio_lead` in the EDL. Reaching for a
  dissolve to soften a choppy dialogue cut is the classic wrong fix — see `cutting-rhythm`.
- **Seamless join, no visible effect** (hide a hard cut) → the existing `splice` command
  (`uv run video-agent splice a.mp4 b.mp4 -o out.mp4`) — a fixed 0.04s crossfade.
- **Effect on the footage itself** (smoke, recolor, restyle) → `grok-video-edit` skill.

## Recipe

`xfade` overlaps the end of clip A with the start of clip B. The `offset` is where the
transition **begins** in the combined timeline = `duration_of_A − transition_duration`.
Total output duration = `dur_a + dur_b − duration`.

```bash
# 1. Get clip A's real duration (offset depends on it — never guess)
uv run video-agent info a.mp4        # read the duration line, e.g. 4.000s

# 2. offset = dur_a - duration   (e.g. 4.000 - 0.6 = 3.4)
ffmpeg -y -i a.mp4 -i b.mp4 -filter_complex \
  "[0:v][1:v]xfade=transition=wipeleft:duration=0.6:offset=3.4[v];\
   [0:a][1:a]acrossfade=d=0.6:c1=tri:c2=tri[a]" \
  -map "[v]" -map "[a]" -c:v h264_videotoolbox -b:v 8M -c:a aac -ar 44100 out.mp4
```

Drop the `[0:a][1:a]acrossfade…[a]` segment and the `-map "[a]"` if either clip has no
audio (otherwise ffmpeg errors on the missing stream).

## Useful transition kinds

`fade`, `fadeblack`, `fadewhite`, `dissolve`, `pixelize`,
`wipeleft` / `wiperight` / `wipeup` / `wipedown`,
`slideleft` / `slideright` / `slideup` / `slidedown`,
`circleopen` / `circleclose`, `radial`, `smoothleft` / `smoothright`,
`diagtl` / `diagtr` / `diagbl` / `diagbr`, `squeezeh` / `squeezev`.

Full list: `ffmpeg -h filter=xfade`.

## Gotchas

- **Both inputs MUST share resolution + fps + pixel format**, or `xfade` errors / produces
  garbage. Normalize clip B first if they differ:
  ```bash
  ffmpeg -y -i b.mp4 -vf "scale=1920:1080,fps=30,format=yuv420p" -c:v h264_videotoolbox b_norm.mp4
  ```
  Check both with `uv run video-agent info` before running.
- **`xfade` re-encodes** — always pass `-c:v h264_videotoolbox` (macOS). After it, any
  further join must use filter_complex concat, not stream-copy concat (non-keyframe seam).
- **`offset` is computed from A's real duration** — read it from `info`, don't assume the
  nominal length (trimmed clips are often a few frames off).
- **`duration` is the overlap length**, typically 0.3–1.0s. Longer than the shorter clip
  will fail.
- Verify the seam visually: `uv run video-agent detect out.mp4 --start <offset-0.3>
  --end <offset+0.3> --step 1` and eyeball the grid.
