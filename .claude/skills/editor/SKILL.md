---
name: editor
description: Turn raw footage into a finished edit in a specific genre/format — montage, documentary, tutorial, news, workshop, vlog, trailer, explainer. Use whenever the user wants the video "edited into a X", asks "what style should this be / what would make a good X", or hands over raw footage without spelling out every cut. This is the director: it understands the footage first, picks a treatment, then composes the other skills (filler-removal, captions, reframe-social, video-transitions, video-overlay, audio-edit, grok-video-edit) to execute it.
---

# Editor — understand the footage, then edit it to a style

This is a **director/orchestrator** skill, not a single ffmpeg move. It does three things in
order: (1) **understand** what the footage is, (2) **pick a treatment** (genre/format), and
(3) **compose the other skills** into a plan and execute it. Every "signature move" below is
just a call into an existing skill or CLI command — this skill decides *which*, in *what
order*, and *why*.

## Is there a video-understanding API key?

**No separate one — and you don't need it.** `XAI_API_KEY` drives Grok's *generative editing*
only (`grok-video-edit`). The understanding model here is **you (Claude) — you are
multimodal**. Sample the footage with the local, key-free tools and read it yourself. (Grok's
vision chat *could* caption frames if you were ever running fully headless with no ability to
read images, but that's the fallback, not the path — reading the frames directly is better and
free.)

## Step 1 — Understand the footage (local, no key)

Gather three signals and form one paragraph "read" of the source before deciding anything:

1. **Metadata** — `uv run video-agent info src.mp4` → duration, fps, resolution, codec.
   (AV1 is fine; primitives route through PyAV automatically. Note raw length — it sets how
   aggressively you cut.)
2. **WHAT is said** — `uv run video-agent transcribe src.mp4 -o /tmp/t.txt` (add `--clean`
   unless you'll also be removing filler). Read it for: topic, **structure** (a narrative
   arc? numbered steps? Q&A? a single pitch?), tone, energy, named people/titles, and the
   punchiest lines (these become montage/trailer beats and pull-quotes).
3. **What it LOOKS like** — `uv run video-agent detect src.mp4 --start 0.0 --end <dur> -o /tmp/grid/`
   then Read the grid PNGs. Classify: single talking head · screen-recording/slides ·
   multi-scene + b-roll · action/motion · existing on-screen text. This drives crop-vs-pad,
   transition choice, and whether there's visual variety to cut on.
   - Per the **detect sanity check** habit: before trusting a moment, re-extract a couple of
     full-res `frame`s at ±0.2s to confirm what's actually there (grid cells are downscaled).

**Read output** (state it back to the user briefly): content · structure · visual type ·
pace · raw length · audio quality. This justifies the treatment you pick next.

## Step 2 — Pick the treatment

If the user named a style, use it. If not, recommend one from the read and **confirm before a
long render** (`AskUserQuestion`) — inferring wrong is cheap to ask, expensive to redo. Natural
fits:

| Source read | Likely style |
|---|---|
| One person pitching/telling a story, good lines | Documentary · Trailer · Explainer-short |
| Step-by-step, screen/slides | Tutorial · Workshop |
| Lots of motion, varied shots, music-friendly | Montage · Vlog |
| Authoritative single subject, factual | News |

## Step 3 — The recipes (each = a composition of other skills)

**Universal order of operations** — do not reorder; getting it wrong forces re-renders:

> **content cuts** (trim · filler-removal · highlight-select) → **structure** (concat ·
> transitions) → **reframe** → **audio** (normalize · music bed) → **overlays/captions LAST**
> → **single final encode**.

Why last-things-last: captions must be burned at final resolution (font size/wrapping follow
the output frame), and audio music-bed ducking needs the final speech track. Re-encode any
join *after a filtered segment* with `filter_complex concat` (CLAUDE.md: stream-copy concat
freezes at non-keyframe seams; re-encode audio to kill drift).

### Montage — fast, kinetic, music-driven (~20–60s)
- **Select beats**: from the transcript pull the punchiest 4–10 lines; from `detect` pull
  high-motion / expressive frames. `trim` each beat short (1–3s).
- **Join** with hard cuts, or quick `video-transitions` (slide/pixelize) for energy.
- **Music bed** under everything (`audio-edit`); if any speech is kept, duck it. Cut on the
  beat where you can.
- Optional **speed ramps** (`setpts=0.5*PTS` + `atempo`), punch-in `reframe --mode crop`, and
  big kinetic `overlay-text` hits.

### Documentary — narrative, slower, cinematic
- **Keep the arc**; clean disfluencies with `filler-removal` (don't gut content).
- **Crossfades/dissolves** between sections (`video-transitions` xfade, or `splice` for a soft
  seam).
- **Lower-thirds**: name + title via `video-overlay`; section/chapter title cards.
- **Music bed ducked** under voice + `loudnorm` (`audio-edit`). Optional cinematic grade
  (`eq=contrast=1.05:saturation=0.95`). Clean `captions` optional.

### Tutorial — clarity first
- `filler-removal` (tight) and **speed up dead air**. Keep screen content readable: reframe
  with `--mode pad` (never crop UI off).
- **Step/section title cards** (`overlay-text`, numbered). **Zoom/punch-in** on the region
  that matters — use `position-grid` to find it, then crop + scale there.
- **Clean captions** (`captions --clean`) + `loudnorm`.

### News — authoritative, factual
- Tight filler cut, `loudnorm`, neutral grade. Standard **16:9**.
- **Lower-third** name/title + a headline **chyron** (`video-overlay`). Intro/outro card.
- Clean `captions`.

### Workshop — long-form teaching, keep most content
- **Light** filler trim only (don't lose substance). Minimal reframe (`pad` for slides).
- **Chapter/section cards** (`overlay-text`); consider exporting an `.srt` (`transcribe --srt`)
  as chapter source. `loudnorm`.
- Accessibility `captions --clean` — for a very long video, **split → caption each part →
  concat** (captions skill gotcha: one encode pass, but huge segment counts bloat the command).

### Other styles — same method, just a different composition
The point of this skill is the *method* (read → map → compose), so new genres are easy:
- **Vlog** — personable: jump-cuts, light music, `captions`, occasional `overlay-text` asides.
- **Trailer** — dramatic: music-driven, escalating fast cuts, big `overlay-text`, `xfade`
  fade-to-black, hard ending. Hook in the first 2s.
- **Explainer-short** — `reframe 9:16`, hook line as `overlay-text` in first 2s, `captions`
  (clean, karaoke-style if time allows), ruthless tightening to <60s.

## Step 4 — Assemble and deliver ONE finished video

- Final join: `filter_complex concat` with `-c:a aac -ar 44100` (re-encode) after any
  filtered/Grok/overlay segment.
- Write the result to `outputs/`.
- **Show only the finished video — never a partial render.** Build the whole pipeline through
  to the last encode, then present the single final file. (No half-painted previews; the
  per-cut preview loop inside `filler-removal` is internal verification, not a deliverable.)

## Gotchas

- **Order of operations** (Step 3 banner) is the #1 source of wasted renders — caption before
  reframe = wrong font size; music duck before final speech = wrong levels.
- **transcribe verbatim vs `--clean`**: `filler-removal` needs verbatim (um/uh kept);
  captions and every other style want `--clean`. If a recipe both removes filler *and*
  captions, transcribe verbatim for the cut, then `--clean` for the captions.
- **Don't mis-cut for the genre**: a montage that isn't tight drags; a documentary/workshop
  cut too hard loses its point. The raw length + structure from Step 1 sets the budget.
- **Confirm an inferred style before a long render** — `AskUserQuestion`, then commit.
- **Generative looks are optional and non-deterministic** — only reach for `grok-video-edit`
  when a style needs a reimagined look (recolor, smoke, restyle) that ffmpeg/overlays can't do;
  prefer the deterministic skills.
