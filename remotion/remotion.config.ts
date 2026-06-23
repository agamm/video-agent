// Remotion config — default to transparent-friendly PNG image format so overlay
// renders keep their alpha channel. See .claude/skills/remotion-graphics.
import {Config} from '@remotion/cli/config';

Config.setVideoImageFormat('png'); // preserve alpha for overlay layers
Config.setOverwriteOutput(true);
