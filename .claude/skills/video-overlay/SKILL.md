---
name: video-overlay
description: Composite a graphic onto video — text, a logo/image, a static transparent PNG, or an animated effect — and place it accurately, including locking it to a moving subject (a face, eye, hand, object). Use whenever the user wants to add/overlay/burn-in/stick something onto footage, place a watermark or label, or position a graphic precisely. Covers transparency (RGBA), animated overlays, timed windows, and coordinate-finding.
---

# Video overlays & compositing

Deterministic compositing of a known asset onto video. (For *generative* edits that
reimagine the footage — recolor objects, add smoke/fire — use `grok-video-edit` instead.)

## Pick the overlay type

| Asset | Command / approach |
|---|---|
| Text label | `overlay-text` (PIL render; mise ffmpeg lacks drawtext) |
| Multiple text labels | `overlay-texts` (one pass, e.g. countdown) |
| Static image / logo | `overlay-image` |
| Static **transparent** graphic | RGBA PNG + ffmpeg `overlay` (alpha is native — no colorkey) |
| **Animated** transparent effect | RGBA **PNG sequence** + ffmpeg (see below) |

```bash
uv run video-agent overlay-text in.mp4 --text "BOOM!" --x center --y center \
    --size 120 --color yellow --start 5.0 --end 7.0 -o out.mp4
uv run video-agent overlay-image in.mp4 --image logo.png --x "W-w-20" --y 20 \
    --scale 200:100 --start 0.0 --end 5.0 -o out.mp4
```
`--x`/`--y`: pixel offset, `center`, or ffmpeg expr (`W-w-10` = 10px from right edge).

## Transparency rules (learned the hard way)

- **Static transparent overlay → use an RGBA PNG directly.** `overlay` handles 8-bit alpha
  natively. No colorkey.
- **Animated transparent overlay → use an RGBA PNG sequence, NOT a GIF.** PIL-saved GIFs
  quantize to a 1-bit palette and `overlay-gif` silently fails to composite their
  transparency. Render frames as `seq/000.png`, `001.png`, … and feed:
  ```bash
  ffmpeg -i video.mp4 -framerate 25 -stream_loop -1 -i "seq/%03d.png" \
    -filter_complex "[1:v]format=rgba[ov];[0:v]format=rgba[bg];[bg][ov]overlay=X:Y,format=yuv420p" \
    -c:v h264_videotoolbox -b:v 8M -c:a copy -shortest out.mp4
  ```
- If using a GIF with `colorkey` anyway: order is **colorkey → scale → format=rgba →
  overlay** (scale converts BGRA→YUV and breaks the key if done first).
- **Debug invisible overlays by removing transparency first** — composite the asset with a
  solid background to confirm geometry/timing work, then fix the alpha separately.

## Timed windows

ffmpeg's `enable='between(t,a,b)'` is unreliable on image/GIF streams with manipulated PTS.
Use the **split-overlay-concat** pattern: split source into before/during/after, apply the
overlay only to the middle segment (no enable clause), then filter_complex concat the three.
`overlay-gif` does this internally when `--start`/`--end` are given.

**Get the WHEN from the transcript, not by scrubbing.** To land a label/callout on a spoken
word or phrase, `transcribe --words` and grep for it → read its start/end → that's your
`--start`/`--end` (or `enable` window). Spatial placement uses the grid/dot-probe below;
temporal placement uses the word timestamps. (Same "the edit is text" discipline as `edl-edit`.)

## Positioning accurately — DON'T iterate-and-eyeball

Guessing coordinates and re-rendering to check is slow and error-prone. Pick by subject:

### Static target (fixed scene element)
Use `position-grid`, but **read the labels, then compute** — don't squint at a thumbnail:
1. `position-grid video.mp4 --at T --spacing 200 -o grid.png` (start at 200; 100 is too
   dense to read when rendered small).
2. Read `grid.png`, find the 200px quadrant containing the target.
3. Crop+zoom that region to read the exact label:
   ```python
   from PIL import Image
   img = Image.open("grid.png"); c = img.crop((x0,y0,x1,y1))
   c.resize((c.width*3, c.height*3), Image.NEAREST).save("grid_zoom.png")
   ```
4. **Compute** overlay top-left = `target_center − overlay_size/2`. Trust the math.
5. Verify once with `frame` on the output.

### Target that's hard to pin by eye, or that moves
Reading a grid once fails when the target is hard to pin to a pixel (blur, low contrast, a
small feature) or when the subject moves between the frame you measured and the clip you're
editing. Use the **dot-probe** method — it works for any target and needs nothing beyond PIL:

1. Extract the target frame full-res with `frame`.
2. Drop a few **colored probe dots** at candidate (x,y) on the *actual* frame, crop tight
   around the target, zoom big (NEAREST), and read which dot lands on it:
   ```python
   from PIL import Image, ImageDraw
   im = Image.open("frame.png").convert("RGB"); d = ImageDraw.Draw(im)
   cands = [(x0, y0, (255,0,0)), (x1, y1, (0,255,0)), (x2, y2, (0,128,255))]
   for (x, y, c) in cands:
       d.ellipse([x-4,y-4,x+4,y+4], fill=c)          # note which color = which (x,y)
   z = im.crop((cx0,cy0,cx1,cy1)); z.resize((z.width*6,z.height*6), Image.NEAREST).save("probe.png")
   ```
   Two or three rounds converge on the pixel — this removes the blur/size ambiguity that pure
   eyeballing suffers from.
3. **Resolve an ambiguous target via an unambiguous neighbor.** When the target itself reads
   poorly but a nearby feature is crisp, measure the easy one and transfer the constraint —
   a clear adjacent landmark fixes the shared row or column of the unclear target.
4. **Compute** overlay top-left = `target_center − overlay_size/2`. Verify with `frame`.

- **A single static position is wrong for a moving target.** If it moves during the overlay
  window, measure it at the window's **start and end** frames, then composite frame-by-frame
  in PIL with the position **linearly interpolated** between them (extract the window's
  frames, paste the asset per frame, re-encode, concat back) — ffmpeg `overlay` can't take a
  time-varying position easily, so a static x/y visibly drifts off the target. If the measured
  drift across the window is small (< ~10px), a static position is fine — measure both ends to
  decide.

## Verify

Spot-check the output with `frame` at the overlay's mid-window timestamp. For timing/motion
correctness on re-encoded sources, dump frames sequentially (`ffmpeg -i out.mp4 -vf fps=10
/tmp/f_%03d.png`) rather than trusting a single seeked frame.
