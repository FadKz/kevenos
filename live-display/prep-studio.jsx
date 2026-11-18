import React from 'react';
import {Composition, registerRoot} from 'remotion';
import {PrepVideo} from './prep-video';
import {duration, fps} from './prep-chapters';

registerRoot(() => <Composition id="PitchPrep" component={PrepVideo} durationInFrames={duration} fps={fps}
  width={720} height={470}/>);
