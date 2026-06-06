---
name: audio-edit
description: Clean up or mix a video's audio track — normalize loudness, reduce background noise/hiss, or add background music (optionally ducked under speech). Use whenever the user wants to fix quiet/loud/inconsistent audio, remove hiss/hum, or lay music under a talking-head/voiceover. For removing um/uh filler words use filler-removal instead; for replacing the audio entirely use plain ffmpeg map.
---

# Audio editing (ffmpeg)

Loudness normalize, denoise, and background-music mixing. These are plain ffmpeg
one-liners — no CLI command wraps them. Every recipe **stream-copies the video**
(`-c:v copy`, fast + lossless) and re-encodes only the audio to `aac`.

Pick the operation:
- Audio too quiet / loud / inconsistent across clips → **normalize**.
- Background hiss / hum / fan noise → **denoise**.
- Lay a music bed under narration → **add music** (use `--duck`-style sidechain so music
  drops under speech).
- Cutting um/uh hesitations → use the `filler-removal` skill, not this one.

## Normalize loudness

```bash
ffmpeg -y -i in.mp4 -af loudnorm=I=-16:TP=-1.5:LRA=11 -c:v copy -c:a aac out.mp4
```
`-16` LUFS / `-1.5` dBTP is a good general/web target. The single pass above is fine for
most clips. For **precise** targets (or batch consistency), do two-pass: run once with
`loudnorm=...:print_format=json`, read the measured values, then pass them back as
`measured_I`/`measured_TP`/`measured_LRA`/`measured_thresh` on a second run.

## Denoise (hiss / hum)

```bash
ffmpeg -y -i in.mp4 -af afftdn=nr=12 -c:v copy -c:a aac out.mp4
```
`afftdn` is an FFT denoiser — no extra deps. `nr` is noise reduction in dB (10–20 typical;
higher = more aggressive but more artifacts/underwater sound — start at 12 and listen).
Combine with normalize in one pass: `-af "afftdn=nr=12,loudnorm=I=-16:TP=-1.5:LRA=11"`.

## Add background music

Plain mix (music sits at a fixed level under the existing audio):
```bash
ffmpeg -y -i in.mp4 -i bg.mp3 -filter_complex \
  "[1:a]volume=-18dB[bg];[0:a][bg]amix=inputs=2:duration=first[a]" \
  -map 0:v -map "[a]" -c:v copy -c:a aac -shortest out.mp4
```

Music **ducked under speech** (music automatically drops when someone talks) — usually what
you want for narration:
```bash
ffmpeg -y -i in.mp4 -i bg.mp3 -filter_complex \
  "[1:a]volume=-18dB[bg];\
   [bg][0:a]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[bgd];\
   [0:a][bgd]amix=inputs=2:duration=first[a]" \
  -map 0:v -map "[a]" -c:v copy -c:a aac -shortest out.mp4
```
The sidechain compresses the music (`[bg]`) using the original speech (`[0:a]`) as the
trigger: louder speech → more music attenuation. Tune `ratio` (duck depth), `attack`
(how fast it ducks, ms), `release` (how fast music returns, ms).

## Gotchas

- **Always `-shortest`** when adding music, or a longer music track extends the clip past
  the video and everything downstream desyncs.
- **`amix` can lower overall level** — if the result sounds quiet, append
  `,loudnorm=I=-16:TP=-1.5:LRA=11` after the `amix` output, or raise the speech with
  `volume` before mixing.
- Re-encode audio to **aac** (`-c:a aac`); don't `-c:a copy` after a filter.
- `duration=first` ties output length to the first `amix` input (the speech) — keep speech
  first in the `amix` chain.
