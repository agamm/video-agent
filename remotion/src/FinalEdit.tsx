// Cue sheet: place each overlay at a frame derived from the transcript. This is the
// "the edit is text" pattern — grep the `--words` transcript for the phrase, read its
// start time, set `atSec`. Renders a single transparent layer with all timed overlays;
// ffmpeg overlays it over the whole footage in one pass.
import {AbsoluteFill, Sequence, useVideoConfig} from 'remotion';
import {LowerThird, LowerThirdProps} from './LowerThird';

type Cue =
  | {type: 'lowerThird'; atSec: number; durSec: number; props: Omit<LowerThirdProps, 'inSec' | 'outSec'>};

export type FinalEditProps = {cues: Cue[]};

export const finalEditDefaults: FinalEditProps = {
  cues: [
    // e.g. the word "Lior" lands at 12.4s in the transcript → show his name tag there
    {type: 'lowerThird', atSec: 12.4, durSec: 4, props: {name: 'Lior Kolnik', title: 'Product · Eve Security', accent: '#5b8cff'}},
    {type: 'lowerThird', atSec: 31.0, durSec: 4, props: {name: 'Agam More', title: 'AI×CRE · ex-Palo Alto / 8200', accent: '#5b8cff'}},
  ],
};

export const FinalEdit: React.FC<FinalEditProps> = ({cues}) => {
  const {fps} = useVideoConfig();
  return (
    <AbsoluteFill>
      {cues.map((c, i) => (
        <Sequence key={i} from={Math.round(c.atSec * fps)} durationInFrames={Math.round(c.durSec * fps)}>
          {c.type === 'lowerThird' && <LowerThird {...c.props} inSec={0} outSec={c.durSec} />}
        </Sequence>
      ))}
    </AbsoluteFill>
  );
};
