import {Composition} from 'remotion';
import {LowerThird, lowerThirdDefaults} from './LowerThird';
import {KineticCaption, kineticCaptionDefaults} from './KineticCaption';
import {TitleCard, titleCardDefaults} from './TitleCard';
import {FinalEdit, finalEditDefaults} from './FinalEdit';

const FPS = 60;
const W = 1920;
const H = 1080;

// Each composition is rendered with: npx remotion render <id> out.mov --props='{...}'
// Override durationInFrames at render time with --frames or by passing props.
export const RemotionRoot: React.FC = () => (
  <>
    {/* Transparent overlay layers (render --codec=prores --prores-profile=4444) */}
    <Composition
      id="LowerThird"
      component={LowerThird}
      durationInFrames={5 * FPS}
      fps={FPS}
      width={W}
      height={H}
      defaultProps={lowerThirdDefaults}
    />
    <Composition
      id="KineticCaption"
      component={KineticCaption}
      durationInFrames={4 * FPS}
      fps={FPS}
      width={W}
      height={H}
      defaultProps={kineticCaptionDefaults}
    />
    {/* Opaque full-frame card (concat between clips, or use as an EDL clip) */}
    <Composition
      id="TitleCard"
      component={TitleCard}
      durationInFrames={4 * FPS}
      fps={FPS}
      width={W}
      height={H}
      defaultProps={titleCardDefaults}
    />
    {/* Cue-sheet driven multi-overlay layer (timings grepped from the transcript) */}
    <Composition
      id="FinalEdit"
      component={FinalEdit}
      durationInFrames={20 * FPS}
      fps={FPS}
      width={W}
      height={H}
      defaultProps={finalEditDefaults}
    />
  </>
);
