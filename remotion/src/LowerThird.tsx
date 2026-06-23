// Animated lower-third / name tag. Renders on a TRANSPARENT background so ffmpeg
// can overlay it onto footage (see remotion-graphics skill). Slides + fades in,
// holds, fades out — timing from anim.ts.
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {TIMING, EASE_OUT, FONT} from './anim';

export type LowerThirdProps = {
  name: string;
  title: string;
  inSec: number; // when it animates in (seconds)
  outSec: number; // when it animates out (seconds)
  accent: string;
};

export const lowerThirdDefaults: LowerThirdProps = {
  name: 'Speaker Name',
  title: 'Role · Company',
  inSec: 0.3,
  outSec: 4.0,
  accent: '#5b8cff',
};

export const LowerThird: React.FC<LowerThirdProps> = ({name, title, inSec, outSec, accent}) => {
  const frame = useCurrentFrame();
  const {fps} = useVideoConfig();
  const inF = inSec * fps;
  const outF = outSec * fps;

  const enter = interpolate(frame, [inF, inF + TIMING.overlayIn], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: EASE_OUT,
  });
  const exit = interpolate(frame, [outF - TIMING.overlayOut, outF], [1, 0], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: EASE_OUT,
  });
  const vis = Math.min(enter, exit);
  const x = interpolate(vis, [0, 1], [-40, 0]);

  return (
    <AbsoluteFill style={{justifyContent: 'flex-end', alignItems: 'flex-start'}}>
      <div
        style={{
          margin: '0 0 90px 70px',
          padding: '18px 28px',
          background: 'rgba(10,12,20,0.78)',
          borderLeft: `5px solid ${accent}`,
          borderRadius: 8,
          opacity: vis,
          transform: `translateX(${x}px)`,
          fontFamily: FONT,
          color: 'white',
        }}
      >
        <div style={{fontSize: 46, fontWeight: 700, lineHeight: 1.1}}>{name}</div>
        <div style={{fontSize: 28, opacity: 0.8, marginTop: 4}}>{title}</div>
      </div>
    </AbsoluteFill>
  );
};
