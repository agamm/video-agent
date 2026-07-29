---
name: cutting-rhythm
description: Make an edit feel flowing, natural and snappy rather than merely accurate — split edits (J/L cuts), where to place a cut relative to speech and motion, how long to hold a shot, collapsing dead air, and cutting on the beat. Use whenever an edit is technically correct but feels choppy, abrupt, draggy, monotone or "AI-edited", when the user asks to tighten/pace/smooth a cut, or before finalizing any multi-clip edit. Complements edl-edit (which is the mechanics of the cut list) with the timing decisions that make it watchable.
---

# Cutting rhythm — why an accurate edit still feels wrong

Frame-accurate cuts in the right places still watch badly if the *timing* is wrong. Three
levers do almost all the work, and they map onto three concrete things you can set:

| Symptom | Lever | Where |
|---|---|---|
| Choppy, abrupt, "every cut is a slam" | **split edits** — move the sound edit off the picture edit | `audio_lead` per clip |
| Draggy, dead, lots of air | **pause length** — collapse gaps to a beat | `tighten` command |
| Monotone, mechanical, nothing lands | **shot length** — vary the holds, accelerate to the climax | clip durations |

Run `edl --report` on any cut list and it will tell you which of the three you're missing.

## 1. Split edits (J/L cuts) — the single biggest flow win

A cut where picture and sound change on the **same frame** is a butt cut. It announces
itself. Real edits stagger the two so the viewer's ear carries them across the visual
change and the cut disappears.

- **J-cut** — the *next* clip's sound arrives before its picture. You hear the answer
  starting under the end of the question. Use on almost every dialogue/interview cut.
- **L-cut** — the *current* clip's sound continues over the next picture. You keep hearing
  the speaker while you cut to what they're describing. Use for cutaways, B-roll, reactions.

One field expresses both — positive leads, negative lags:

```jsonc
{"src": "b.mp4", "start": 12.0, "end": 20.0, "audio_lead":  0.4}   // J-cut into this clip
{"src": "c.mp4", "start": 30.0, "end": 38.0, "audio_lead": -0.5}   // L-cut out of the previous
```

**Typical amounts**: 0.2–0.5 s for conversational cuts, 0.5–1.0 s for a documentary cutaway,
up to ~2 s when laying B-roll under continuing narration. Below ~0.15 s it's inaudible —
you've paid for nothing.

**Where to use it**: cuts *within* a continuous thought. Do **not** split a cut that's meant
to be a hard break — a scene change, a chapter boundary, or the button at the end of a
trailer wants the slam.

The renderer keeps video and audio on independent concat chains, so a lead never desyncs
anything downstream — the leads telescope back to matching totals.

## 2. Where the cut actually goes

- **Cut in the silence, not on the word.** Get the *what* from `transcribe --words` and the
  *where* from `speech-segments`; the silence boundary is the truth (whisper word times
  jitter ±0.3–0.7 s). `snap --to silence` does this for a whole EDL at once.
- **Leave the breath.** The intake before a sentence belongs to that sentence. Cutting it
  off is the classic "why does this sound rushed and airless" mistake.
- **Cut on motion, not around it.** A cut placed while a subject is moving (a gesture
  mid-swing, a head turn, a step) hides itself; a cut between two still moments exposes
  itself. Never cut on the *peak* of a gesture — cut just after it starts or just before it
  lands.
- **Match the outgoing and incoming energy.** Cutting from a wide static shot to a fast
  handheld one reads as a mistake unless the audio motivates it.

## 3. Pause grammar — natural, not dead

"Tighten this up" almost never means cut content; it means shorten the gaps.

```bash
uv run video-agent tighten talk.mp4 -o edit.json --target-gap 0.5 --min-gap 1.0
uv run video-agent edl edit.json -o out.mp4
```

`tighten` removes time from the **middle** of each long pause, so the breath at the end of
one sentence and the intake before the next both survive. Guidance for `--target-gap`:

| Feel | target-gap | min-gap |
|---|---|---|
| Snappy / social / explainer | 0.25–0.35 | 0.6 |
| Normal talking head, vlog, tutorial | 0.4–0.6 | 1.0 |
| Documentary, workshop — let it breathe | 0.7–1.0 | 1.5 |

**Do not tighten uniformly.** A pause *after a punchline or a reveal* is doing work —
it's the beat the viewer laughs or thinks in. Widen those back out by hand in the EDL after
running `tighten`. Uniform pacing is exactly what makes an edit sound machine-made.

This is distinct from `filler-removal`: that cuts spoken um/uh; this leaves every word and
shortens the space between them. Run filler-removal first, then tighten.

## 4. Shot length — the snap

- **Vary the holds.** If `edl --report` says variation is under ~15%, the cut list is
  monotone no matter how good the picks are. Aim for a mix of short and long.
- **Accelerate toward the climax.** Trailers/montages want shot lengths that *shorten* as
  they build — e.g. 3.0, 2.6, 2.0, 1.5, 1.0, then the payoff.
- **Hold the last shot.** Endings want air. The final shot should be at or above the mean,
  never the tightest cut in the piece.
- **Hold after a hit.** A punchline, reveal or big visual needs a beat before the next cut,
  or it doesn't land.

Trailer structure that works: **hook** (≤2 s, in the first two seconds) → **build**
(lengthening context shots) → **turn** (the problem/conflict) → **climax** (fastest cutting)
→ **button** (one held shot + title).

## 5. Cutting on the beat

```bash
uv run video-agent beats inputs/music.mp3                       # bpm + beat grid
uv run video-agent snap edit.json -o snapped.json \
    --to beats --ref inputs/music.mp3 --tolerance 0.4
```

`snap --to beats` moves each cut so it lands on a beat in the **output** timeline (cumulative
running time) — which is what "cut on the beat" means; snapping source timestamps to beats is
meaningless. Cuts with no beat inside `--tolerance` are left alone.

If the music bed starts partway into the track, pass its in-point as `--offset` (the same
value as the EDL's `music.start`), or the grid is shifted.

Tempo is reported with a prior around 120 BPM to avoid the classic half-tempo lock. If the
reported BPM looks like half or double what you expect, the grid is still musically valid
(it's a metrical level) — just coarser or finer than intended.

## 6. The review loop

```bash
uv run video-agent edl edit.json -o /dev/null --report --dry-run   # pacing, no render
uv run video-agent edl edit.json -o draft.mp4 --draft              # fast 480p check
uv run video-agent edl edit.json -o final.mp4                      # deliverable
```

`--report` prints every shot length as a bar plus warnings for the three failure modes above.
Read it **before** rendering — it's free, and it catches monotony that's invisible in JSON.
`--draft` is for your own verification only; per the no-partial-previews rule the user sees
one finished file.

## Gotchas

- **`audio_lead` needs material to reach into.** A J-cut on a clip that starts at 0.2 s in
  its source has only 0.2 s to borrow — the renderer errors rather than silently shortening.
- **No `audio_lead` on the first clip** — nothing precedes it. It's ignored with a warning.
- **A cutaway (`vsrc`) must be the same length as the audio it covers**, or picture and sound
  come apart for the rest of the edit. The renderer now rejects a mismatch.
- **Cut points are quantized to the frame grid** by the renderer, so times produced by
  `snap`/`tighten` (which are not round numbers) can't drift picture against sound.
- **Order**: filler-removal → tighten → structure/split edits → beats snap → everything else.
  Tightening after you've set split edits invalidates the leads.
