import React from 'react';
import { AbsoluteFill, useCurrentFrame, interpolate } from 'remotion';
import { theme } from '../lib/theme';
import { fadeIn, fadeOut, stagger } from '../lib/animation';
import { pipelineStages } from '../lib/data';

const NODES_START = 16;
const NODE_STAGGER = 6;
const DOTS_START = 46;
const DOT_TRAVEL = 150;
const DOT_GAP = 46;
const HEADLINE1_AT = 262;
const HEADLINE2_AT = 312;

const TRACK_LEFT = 130;
const TRACK_RIGHT = 1790;
const TRACK_Y = 430;

export const VisionScene: React.FC = () => {
  const frame = useCurrentFrame();
  const sceneOpacity = fadeIn(frame, 0, 16);
  const n = pipelineStages.length;
  const step = (TRACK_RIGHT - TRACK_LEFT) / (n - 1);

  const pipelineOpacity = fadeOut(frame, HEADLINE1_AT - 14, 16);
  const h1Opacity = fadeIn(frame, HEADLINE1_AT, 16) * fadeOut(frame, HEADLINE2_AT - 12, 14);
  const h2Opacity = fadeIn(frame, HEADLINE2_AT, 16);

  const dotStarts = [0, 1, 2].map((i) => DOTS_START + i * DOT_GAP);

  return (
    <AbsoluteFill style={{ background: theme.colors.bg, alignItems: 'center', justifyContent: 'center', opacity: sceneOpacity }}>
      {pipelineOpacity > 0.01 && (
        <div style={{ position: 'absolute', width: '100%', height: '100%', opacity: pipelineOpacity }}>
          {/* Track line */}
          <div
            style={{
              position: 'absolute',
              left: TRACK_LEFT,
              top: TRACK_Y,
              width: TRACK_RIGHT - TRACK_LEFT,
              height: 2,
              background: theme.colors.borderSoft,
            }}
          />

          {/* Traveling dots representing concurrent applications */}
          {dotStarts.map((start, i) => {
            const x = interpolate(frame, [start, start + DOT_TRAVEL], [TRACK_LEFT, TRACK_RIGHT], {
              extrapolateLeft: 'clamp',
              extrapolateRight: 'clamp',
            });
            const opacity = fadeIn(frame, start, 10) * fadeOut(frame, start + DOT_TRAVEL - 10, 10);
            return (
              <div
                key={i}
                style={{
                  position: 'absolute',
                  left: x - 6,
                  top: TRACK_Y - 6,
                  width: 14,
                  height: 14,
                  borderRadius: '50%',
                  background: theme.colors.accentBright,
                  boxShadow: `0 0 16px ${theme.colors.accent}`,
                  opacity,
                }}
              />
            );
          })}

          {/* Nodes */}
          {pipelineStages.map((stage, i) => {
            const delay = NODES_START + stagger(i, 0, NODE_STAGGER);
            const opacity = fadeIn(frame, delay, 14);
            const x = TRACK_LEFT + step * i;
            return (
              <div
                key={stage}
                style={{
                  position: 'absolute',
                  left: x,
                  top: TRACK_Y,
                  transform: 'translate(-50%, -50%)',
                  opacity,
                  display: 'flex',
                  flexDirection: 'column',
                  alignItems: 'center',
                  gap: 12,
                }}
              >
                <div
                  style={{
                    width: 16,
                    height: 16,
                    borderRadius: '50%',
                    background: theme.colors.surfaceElevated,
                    border: `2px solid ${theme.colors.accentBorder}`,
                  }}
                />
                <div
                  style={{
                    fontFamily: theme.font,
                    fontSize: 14,
                    fontWeight: 700,
                    color: theme.colors.text,
                    textAlign: 'center',
                    width: 160,
                  }}
                >
                  {stage}
                </div>
              </div>
            );
          })}
        </div>
      )}

      {h1Opacity > 0.01 && (
        <div style={{ position: 'absolute', fontFamily: theme.font, fontSize: 62, fontWeight: 700, color: theme.colors.text, opacity: h1Opacity }}>
          &quot;From one application…&quot;
        </div>
      )}
      {h2Opacity > 0.01 && (
        <div style={{ position: 'absolute', fontFamily: theme.font, fontSize: 62, fontWeight: 700, color: theme.colors.text, opacity: h2Opacity, textAlign: 'center' }}>
          &quot;…to your entire job search.&quot;
        </div>
      )}
    </AbsoluteFill>
  );
};
