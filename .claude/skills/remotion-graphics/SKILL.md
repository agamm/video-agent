---
name: remotion-graphics
description: Create ANIMATED motion graphics — kinetic word-by-word captions, animated lower-thirds/name tags, branded intro/outro title cards, data-driven graphics (count-ups, growing bars) — as React/Remotion components, rendered to a transparent layer and composited over the footage with ffmpeg. OPTIONAL/heavy (needs Node + a headless-Chromium download, and a Remotion license for company use). Use only when an edit needs genuine motion design that PIL/ffmpeg overlays can't do; for a static label, watermark, or simple fade, use the video-overlay skill instead.
---

# Animated graphics with Remotion (optional)

Our `video-overlay`/`captions` path renders **static** PNGs composited by ffmpeg. When a job
needs real motion design — eased entrances, staggered reveals, **word-synced kinetic
captions**, animated lower-thirds, branded title cards, count-ups — author the graphic as a
**Remotion** React component (every word/color/beat is a prop, animated over `frame`), render
it to a **transparent layer**, then composite with our existing ffmpeg `overlay`. ffmpeg still
owns the cut, audio, grade, and final encode; Remotion only authors the moving graphic.

## ⚠ Before using — it's optional and heavy
- Needs **Node + npm** and a Remotion project (the `remotion/` scaffold in this repo). First
  render downloads a **headless Chromium (~150–300 MB)**. This is a bigger dependency than the
  whole Python core — like the `grok-video-edit` vfx extra, it's **off by default**.
- **Licensing:** Remotion is free for individuals/small teams but **companies above a small
  headcount need a paid seat license** — verify before using under a company. (No API key, but
  a real license gate.)
- **Render cost:** frame-by-frame Chrome rendering; render the overlay at overlay
  size/duration only (seconds of a lower-third), never the whole video, to keep it fast.
- If the graphic is static or a simple fade → **use `video-overlay`, not this.**

## Default pattern: transparent layer → ffmpeg composite

1. Author/parameterize the component (see scaffold in `remotion/src/`). Pass content + timing
   as `inputProps` JSON — no hard-coded text.
2. Render **only the graphic** on alpha:
   ```bash
   cd remotion && npm install        # first time only (pulls Remotion + Chromium)
   npx remotion render LowerThird out/lower.mov \
       --codec=prores --prores-profile=4444 --pixel-format=yuva444p10le \
       --props='{"name":"Lior Kolnik","title":"Product, Eve Security","inSec":0.3,"outSec":4.0}'
   # (or a transparent WebM: --codec=vp8 --pixel-format=yuva420p)
   ```
3. Composite over the footage with our normal overlay (timed window), keeping audio:
   ```bash
   ffmpeg -y -i footage.mp4 -i remotion/out/lower.mov -filter_complex \
     "[0:v][1:v]overlay=40:H-h-60:enable='between(t,12,16)'" \
     -c:v h264_videotoolbox -b:v 9M -c:a copy out.mp4
   ```
   This is exactly the RGBA-sequence compositing in `video-overlay` — Remotion just produced a
   richer, animated layer. For a full-frame animated **title/outro card** (no transparency),
   render opaque and `concat` it between clips (or drop it into an `edl-edit` EDL as a clip).

## Word-synced timing comes from the transcript (cue sheet)

Don't eyeball when a word/overlay lands — **grep the `--words` transcript** for the phrase,
read its timestamp, convert to a frame (`frame = round(t * fps)`), and put it in the cue sheet.
The scaffold's `FinalEdit.tsx` holds `CUES = [{id, at, dur}]`; e.g. an emphasis lands on the
word "right" at 4.92s → `at: Math.round(4.92*fps)`. Kinetic captions read the same word
timestamps to pop one word per beat. (See `editor`'s "the edit is text" convention.)

## Tuning knobs + reviewing your own work

- Global feel lives in `remotion/src/anim.ts` — a few `TIMING` constants (reveal, stagger,
  overlayIn/Out) + a shared easing. "Make it snappier" = change one number, re-render.
- **Review before a full pass**: `npx remotion still <Comp> still.png --frame=N --props=...`
  renders one frame — read it (like our frame-extraction checks), adjust, then do the full
  `render`. Iterate stills, not full renders.

## When to reach for it (vs not)
- **Use Remotion**: kinetic/karaoke captions, animated lower-thirds, branded animated intro/
  outro cards, count-ups/charts, anything with eased motion.
- **Use `video-overlay`/`captions` instead**: a static logo/label/watermark, burned-in plain
  captions, a simple fade-in PNG — cheaper, no Node/Chromium, no license question.

See `remotion/README.md` for the scaffold layout and adding a new component.
