---
name: filler-removal
description: Remove filler words and disfluencies (um, uh, like, you know, basically) from a talking-head video so it sounds like they were never said. Use whenever the user wants to clean up speech, cut filler/hesitations, tighten a voiceover, or remove "um"s and "uh"s. Covers verbatim transcription, precise cutting rules, and the iterate-until-clean preview loop.
---

# Filler-word removal

Goal: cut filler words so the result sounds natural — no clipped consonants, no abrupt
audio jumps, no real speech lost. This is an **iterative** process; do not stop at the
first pass.

## Step 0 (CRITICAL): get a verbatim transcript

mlx-whisper **silently cleans disfluencies** by default — it drops most "um/uh" from the
transcript, so there's nothing to cut. This was the root cause of a past failure where
fillers survived the edit.

`transcribe` is now **verbatim by default**: it seeds the decoder with a short hesitation
prompt (`Um, uh, er, hmm...`) and `condition_on_previous_text=False`, so um/uh are kept.
Just run:

```bash
uv run video-agent transcribe video.mp4 --words -o words.txt   # start_sec  end_sec  word
```

Do **not** pass `--clean` here — that restores whisper's filler-dropping behavior, the
opposite of what you want.

The seed prompt is deliberately narrow (only short hesitations) to avoid over-transcribing
real words like "like"/"so". If the user reports fillers that still aren't in the list,
listen to the audio span directly to confirm what was actually said before concluding
nothing is there.

## Step 0.5 (CRITICAL): cross-check word boundaries against silence

**Whisper word timestamps are jittery and unreliable for the actual cut** — they can be
off by 0.3–0.7s, and re-running transcription gives *different* positions for the same
word. Cutting on the raw word timestamp landed a real edit in the wrong place (the filler
survived; a chunk of silence got removed instead). Always verify the boundary before
cutting:

```bash
uv run video-agent speech-segments video.mp4   # speech/silence spans, original timeline
```

A filler like an isolated "um" almost always sits as its **own short speech burst between
two silences** — that burst's start/end are the truth, not whisper's number. Map each
whisper filler to its burst and cut the burst, not the timestamp.

This also reveals the natural pacing: if the filler sits inside a long pause, removing only
the burst leaves a draggy hole. Match the speaker's own sentence-boundary pauses (read them
off the silence map — typically 0.4–0.7s) by collapsing the surrounding dead air to one
natural beat. "Seems like I never said it" means seamless flow, not a silent gap where the
word was.

(`speech-segments` wraps ffmpeg `silencedetect` and inverts it to speech spans. Raw form,
if you need to tune the threshold: `ffmpeg -i in.mp4 -af silencedetect=noise=-30dB:d=0.15
-f null -` and read `silence_start`/`silence_end` from stderr.)

## Cutting rules (follow exactly)

- Cut from **word_start − 0.05s** to **word_end + 0.15s** (50ms lead-in, 150ms trail) —
  the trail prevents clipping the consonant that follows the filler.
- Never cut closer than 0.1s to a non-filler word on either side.
- If two fillers are within 0.3s of each other, merge them into one cut.
- Common fillers: um, uh, er, like, you know, I mean, so, basically, literally, right, well.
  Only cut these as *disfluencies* — "like" and "so" are often real words; check context.

## Iteration loop — do not stop until clean

1. List all filler words with their exact start/end from the verbatim transcript.
2. Build the keep-segments (everything between the cuts) and concat with **filter_complex
   concat** (re-encodes — avoids freeze frames at non-keyframe seams):
   ```bash
   ffmpeg -i a.mp4 -i b.mp4 -i c.mp4 \
     -filter_complex "[0:v][0:a][1:v][1:a][2:v][2:a]concat=n=3:v=1:a=1[v][a]" \
     -map "[v]" -map "[a]" -c:v h264_videotoolbox -b:v 8M -c:a aac -ar 44100 out.mp4
   ```
3. For each cut, extract a **3-second splice preview** around the join:
   ```bash
   ffmpeg -i out.mp4 -ss <join_time-1.5> -t 3 -c:v h264_videotoolbox -c:a aac preview_cut_N.mp4
   ```
4. Read/watch each preview. Adjust and redo the offending cut:
   - Abrupt audio jump → increase trail padding by 0.05s
   - Missing word start → decrease lead-in by 0.05s
   - Sounds natural → approve
5. After concat, **re-transcribe the output** (verbatim again) and confirm no non-filler
   speech was removed — and **grep the re-transcript for `um`/`uh`** to confirm the fillers are
   actually gone (the strongest, content-level check that the cuts landed right).
6. If real speech is missing → restore that cut from the original and iterate.
7. Done when: every preview sounds natural AND the re-transcription matches intent.

## After the fillers are gone: pacing

Removing um/uh leaves the *gaps* where they were. If the result still drags, that's a pacing
problem, not a filler problem — run `tighten` (collapses long pauses to a natural beat,
writing an EDL) and add split edits. Both live in the `cutting-rhythm` skill. Order matters:
filler-removal first, then tighten, or you'll tighten around words you're about to cut.

## Reference

- Word-timestamp output format: `start_sec  end_sec  word` (tab-separated, one per line).
- `-ss` must come **after** `-i` for accurate cut points on B-frame sources (see CLAUDE.md
  frame-extraction pitfall).
- See `video-overlay` and `grok-video-edit` skills if the same video also needs effects.
