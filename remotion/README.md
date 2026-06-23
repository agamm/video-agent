# remotion/ — optional animated-graphics layer

Optional sibling project for **animated** motion graphics (kinetic captions, animated
lower-thirds, branded title cards). NOT part of `uv sync` — see the **`remotion-graphics`**
skill for when to use it and the full composite workflow. Heavy: needs Node + a headless
Chromium download on first render, and a **Remotion license for company use** (verify).

## Setup (first time)
```bash
cd remotion && npm install      # pulls Remotion + Chromium (~150–300 MB)
npm run studio                  # optional: live preview at localhost:3000
```

## Render a layer, then composite with ffmpeg
```bash
# transparent overlay (alpha) → ProRes 4444
npx remotion render LowerThird out/lower.mov \
  --codec=prores --prores-profile=4444 --pixel-format=yuva444p10le \
  --props='{"name":"Lior Kolnik","title":"Product · Eve Security","inSec":0.3,"outSec":4.0,"accent":"#5b8cff"}'

# composite over footage (timed window) with ffmpeg (back in repo root)
ffmpeg -y -i footage.mp4 -i remotion/out/lower.mov -filter_complex \
  "[0:v][1:v]overlay=40:H-h-60:enable='between(t,12,16)'" \
  -c:v h264_videotoolbox -b:v 9M -c:a copy out.mp4
```
Review one frame before a full render: `npx remotion still TitleCard still.png --frame=40 --props='{...}'`.

## Compositions (`src/`)
| id | file | use | render |
|---|---|---|---|
| `LowerThird` | LowerThird.tsx | animated name tag | transparent (prores 4444) |
| `KineticCaption` | KineticCaption.tsx | word-by-word captions (feed `--words` timestamps) | transparent |
| `TitleCard` | TitleCard.tsx | full-frame intro/outro card | opaque → concat / EDL clip |
| `FinalEdit` | FinalEdit.tsx | cue-sheet of timed overlays (one layer) | transparent |

`src/anim.ts` = global timing + easing knobs (retune the whole feel in one place). Cue/word
timings come from the `--words` transcript (`frame = round(t*fps)`), never from scrubbing.

## Add a component
Create `src/Foo.tsx` (export the component + a `fooDefaults` props object), register a
`<Composition id="Foo" .../>` in `src/Root.tsx`, render with `npx remotion render Foo ...`.
