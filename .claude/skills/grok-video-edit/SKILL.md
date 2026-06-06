---
name: grok-video-edit
description: Apply AI-generated visual edits to video using xAI / Grok — change object colors, add effects (smoke, fire, sparkles), restyle scenes, or alter what's in a clip ("make the sunglasses red", "add smoke from the gun", "make it sparkly"). Use whenever an edit can't be done deterministically with ffmpeg/overlays and needs generative reimagining of the footage. Covers trimming, chunking, audio mux, and seamless reassembly.
---

# Grok (xAI) video editing

Use Grok for edits that require *generative* changes to the footage itself — recoloring
objects, adding physical effects (smoke, fire, glow), restyling. For compositing a known
asset (logo, text, PNG/animated graphic) onto video, use the `video-overlay` skill instead
— it's deterministic and cheaper.

Requires `XAI_API_KEY` in `.env`.

## Golden rule: always trim the target region first

`grok-edit full_video.mp4` chunks the **entire** video even with `--splice-into`. Never
send the full video. Workflow:

1. **Find exact boundaries.** Run `detect` with `--step 1 --cell-w 384` inside the rough
   window to locate the frame-exact first/last frames of the subject. Trim to those exact
   boundaries so Grok doesn't reimagine irrelevant context.
   ```bash
   uv run video-agent trim source.mp4 --start X --end Y -o segment.mp4
   ```
2. **Edit the segment:**
   ```bash
   uv run video-agent grok-edit segment.mp4 --prompt "..." -o edited.mp4
   ```
3. **Mux original audio back** (Grok outputs video only, and drops ~0.2–0.3s of frames):
   ```bash
   ffmpeg -i edited.mp4 -i segment.mp4 -map 0:v -map 1:a -c copy -shortest out.mp4
   ```
   **Always `-shortest`** — without it the audio outlasts the shorter video track and
   everything downstream desyncs.
4. **Final join with filter_complex concat** (re-encodes — kills keyframe freeze + drift):
   ```bash
   ffmpeg -i a.mp4 -i b.mp4 \
     -filter_complex "[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v][a]" \
     -map "[v]" -map "[a]" -c:v h264_videotoolbox -b:v 8M -c:a aac -ar 44100 out.mp4
   ```
   Do **NOT** use stream-copy `concat` after a Grok-edited clip — non-keyframe seams cause
   freeze frames.

The `--splice-into` flag automates chunking, base64 upload, concat, and audio stitch, but
still only use it on an already-trimmed region.

## Chunking & seams

- `GROK_MAX_SECONDS = 8.0` (in code): each chunk is edited **independently** — Grok
  reimagines the scene fresh per chunk. Shorter chunks = more seams/style jumps. Default 8s.
  Do NOT go below 3s.
- **Split chunks at natural scene cuts, not arbitrary intervals.** Use the detect grid to
  find cuts, trim each shot separately. A chunk spanning a shot change will have the second
  shot restyled differently.
- Chunks are downscaled to 720p/2Mbps (gRPC 4MB limit). AV1 sources handled automatically
  (PyAV decode → h264 chunks).
- **Grok re-encodes at ~24fps regardless of source fps**, and the output is a few frames
  shorter than the input. On a high-fps source (e.g. 120fps) the edited segment will *not*
  match the surrounding clips. **Normalize fps in the final concat** — add `fps=<orig>` to
  each input in the filter_complex (`[1:v]fps=120,setsar=1,...`), or the seam will stutter /
  the durations won't line up. Never stream-copy a Grok segment back into a higher-fps clip.

## Effects with aftermath — don't pop at the seam

If an added effect leaves **visual aftermath that outlives the action it came from**, it
will still be on screen at the trim boundary. A hard cut back to the untouched clip makes it
**vanish in one frame** — a jarring pop. Two fixes:

- **Extend the edited region** to cover the aftermath so the effect resolves *within* Grok's
  footage. Trade-off: a longer region means Grok reimagines more frames (more chance of
  subject/character drift), so only extend as far as needed.
- **Dissolve the seam** with a short `xfade` (≈0.25–0.4s) so the effect fades instead of
  popping. Because both sides are the same subject in nearly the same pose, the dissolve
  reads as the effect clearing, not as a transition.
- **You can xfade freely wherever the audio is silent.** xfade shortens the video by the
  overlap, which desyncs muxed audio — but only if speech crosses the seam. When the effect
  lands in a silent stretch, dissolve as much as you like and re-mux the original audio with
  `-shortest`. Use `speech-segments` (filler-removal skill) to see which stretches are silent.

## Character / consistency drift

Grok cannot maintain character consistency between chunks. The system constraint
(`_GROK_CONSTRAINT`) is generic — keep it that way. Pass character/subject descriptions in
the **user prompt**, not the constraint.

## Prompt tips

- Say what to **change**, not what to keep — Grok handles positive instructions better than
  negative constraints.
- Avoid incidental words like "shockwave rings", "portal effects" unless you want them —
  Grok adds them literally.
- For color changes, name the exact source color to replace and the exact target color.
- For added effects, describe **direction and physics**, not just the effect — where it
  originates, which way it travels, how it ends (e.g. rises then drifts, radiates outward,
  settles). Grok renders the motion you specify and invents motion you leave unsaid.

## Parallelize independent edits

If a video needs multiple separate Grok edits (e.g. two different moments), trim each region
and run the `grok-edit` calls in **background** simultaneously — they're independent network
jobs. Mux audio and assemble once both complete.

## Audio

Grok outputs video only. For final assembly always rebuild audio from the **original**
source (extract with `-vn -c:a copy`, then mux). Never rely on PyAV-trimmed audio for sync.
