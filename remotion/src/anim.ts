// Global timing + easing knobs. Change one number to retune the whole feel
// ("make it snappier" = lower the durations). Frames @ the composition fps.
import {Easing} from 'remotion';

export const TIMING = {
  reveal: 14, // frames for a primary element to animate in
  stagger: 4, // frames between staggered child elements
  overlayIn: 12, // lower-third slide/fade in
  overlayOut: 10, // lower-third out
  wordPop: 6, // kinetic-caption per-word pop
};

// Shared ease — a soft "out" curve used everywhere for a consistent house feel.
export const EASE_OUT = Easing.bezier(0.16, 1, 0.3, 1);

export const FONT =
  '-apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Helvetica, Arial, sans-serif';
