// Full-frame branded intro/outro card (opaque). Render it and `concat` between
// clips, or drop it into an edl-edit EDL as a clip. Title + subtitle reveal with
// a staggered fade-up; background is solid so no transparency needed.
import {AbsoluteFill, interpolate, useCurrentFrame, useVideoConfig} from 'remotion';
import {TIMING, EASE_OUT, FONT} from './anim';

export type TitleCardProps = {
  eyebrow: string;
  title: string;
  subtitle: string;
  bg: string;
  accent: string;
};

export const titleCardDefaults: TitleCardProps = {
  eyebrow: 'VIBE CODING CYBERSECURITY WORKSHOP',
  title: "A working app\nisn't a secure app.",
  subtitle: 'Practical security for teams building with AI.',
  bg: '#070a12',
  accent: '#5b8cff',
};

const Line: React.FC<{delay: number; children: React.ReactNode; style?: React.CSSProperties}> = ({
  delay,
  children,
  style,
}) => {
  const frame = useCurrentFrame();
  const v = interpolate(frame, [delay, delay + TIMING.reveal], [0, 1], {
    extrapolateLeft: 'clamp',
    extrapolateRight: 'clamp',
    easing: EASE_OUT,
  });
  return (
    <div style={{...style, opacity: v, transform: `translateY(${interpolate(v, [0, 1], [18, 0])}px)`}}>
      {children}
    </div>
  );
};

export const TitleCard: React.FC<TitleCardProps> = ({eyebrow, title, subtitle, bg, accent}) => {
  useVideoConfig();
  return (
    <AbsoluteFill
      style={{
        background: bg,
        justifyContent: 'center',
        alignItems: 'center',
        textAlign: 'center',
        fontFamily: FONT,
        color: 'white',
      }}
    >
      <Line delay={0} style={{color: accent, letterSpacing: 4, fontSize: 24, fontWeight: 700}}>
        {eyebrow}
      </Line>
      <Line
        delay={TIMING.stagger}
        style={{fontSize: 96, fontWeight: 800, lineHeight: 1.05, margin: '18px 0', whiteSpace: 'pre-line'}}
      >
        {title}
      </Line>
      <Line delay={TIMING.stagger * 2} style={{fontSize: 30, opacity: 0.75}}>
        {subtitle}
      </Line>
    </AbsoluteFill>
  );
};
