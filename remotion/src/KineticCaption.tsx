// Word-synced kinetic captions: each word pops in on its own timestamp (from the
// `--words` transcript). Transparent background → composite over footage with ffmpeg.
// Feed `words` as [{word, start}] (start = seconds); grep them from the transcript.
import {AbsoluteFill, interpolate, spring, useCurrentFrame, useVideoConfig} from 'remotion';
import {FONT} from './anim';

export type Word = {word: string; start: number};
export type KineticCaptionProps = {words: Word[]};

export const kineticCaptionDefaults: KineticCaptionProps = {
  words: [
    {word: 'every', start: 0.0},
    {word: 'word', start: 0.32},
    {word: 'pops', start: 0.7},
    {word: 'on', start: 1.0},
    {word: 'its', start: 1.2},
    {word: 'beat', start: 1.45},
  ],
};

export const KineticCaption: React.FC<KineticCaptionProps> = ({words}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const t = frame / fps;

  return (
    <AbsoluteFill style={{justifyContent: 'flex-end', alignItems: 'center', paddingBottom: 120}}>
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          justifyContent: 'center',
          gap: '0 18px',
          maxWidth: '82%',
          fontFamily: FONT,
        }}
      >
        {words.map((w, i) => {
          const startF = w.start * fps;
          const pop = spring({frame: frame - startF, fps, config: {damping: 200}});
          const spoken = t >= w.start;
          const scale = interpolate(pop, [0, 1], [0.7, 1]);
          return (
            <span
              key={i}
              style={{
                fontSize: 64,
                fontWeight: 800,
                color: spoken ? 'white' : 'rgba(255,255,255,0.28)',
                opacity: interpolate(pop, [0, 1], [0, 1]),
                transform: `scale(${scale})`,
                textShadow: '0 2px 18px rgba(0,0,0,0.85)',
              }}
            >
              {w.word}
            </span>
          );
        })}
      </div>
    </AbsoluteFill>
  );
};
