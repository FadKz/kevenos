import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';
import {Player} from '@remotion/player';
import {PrepVideo} from './prep-video';
import {chapters, duration, fps, starts} from './prep-chapters';

function Example() {
  const [answer, setAnswer] = useState('');
  return <fieldset className="prep-example">
    <legend>Try an IT objection</legend>
    <p>Your customer says, “One app only works on Windows.” What could you say?</p>
    <div className="row">
      <button type="button" className="btn ghost" aria-pressed={answer==='check'} onClick={()=>setAnswer('check')}>Ask which app, then offer a compatibility check</button>
      <button type="button" className="btn ghost" aria-pressed={answer==='promise'} onClick={()=>setAnswer('promise')}>Promise all their apps will work on solution device</button>
    </div>
    <p role="status">{answer==='check' ? 'Good. Ask who uses the app and what they rely on. Offer to check the workflow with implementation partner and their IT team in Discovery.'
      : answer==='promise' ? 'Check the app and workflow with the customer’s IT team before making that promise. Try the other response.' : ''}</p>
  </fieldset>;
}

function Walkthrough({openTarget}) {
  const player = useRef(null);
  const [current, setCurrent] = useState(0);
  const [seconds, setSeconds] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [still] = useState(()=>matchMedia('(prefers-reduced-motion: reduce)').matches);
  const chapter = chapters[current];

  useEffect(()=>{
    const handle = player.current;
    const track = ({detail:{frame}}) => {
      const next = starts.findLastIndex(start=>start<=frame);
      setCurrent(Math.max(0,next));
      setSeconds(Math.floor(frame/fps));
    };
    const started = ()=>setPlaying(true);
    const stopped = ()=>setPlaying(false);
    handle.addEventListener('frameupdate',track);
    handle.addEventListener('play',started);
    handle.addEventListener('pause',stopped);
    handle.addEventListener('ended',stopped);
    return ()=>{
      handle.removeEventListener('frameupdate',track);
      handle.removeEventListener('play',started);
      handle.removeEventListener('pause',stopped);
      handle.removeEventListener('ended',stopped);
    };
  },[]);

  function seek(i) {
    player.current.pause();
    player.current.seekTo(starts[i]);
    setCurrent(i);
  }

  const format = time => Math.floor(time/60) + ':' + String(time%60).padStart(2,'0');
  function toggle(event) {
    if (playing) player.current.pause();
    else {
      if (player.current.getCurrentFrame() >= duration-1) player.current.seekTo(0);
      player.current.play(event);
    }
  }

  return <>
    <div className="prep-video-frame">
      <Player ref={player} component={PrepVideo} durationInFrames={duration} fps={fps} compositionWidth={720}
        compositionHeight={470} inputProps={{still}} style={{width:'100%'}}
        initiallyMuted numberOfSharedAudioTags={0} moveToBeginningWhenEnded={false} allowFullscreen={false}
        doubleClickToFullscreen={false} errorFallback={()=><p>Use the chapter notes below if the video cannot play.</p>}/>
    </div>
    <div className="prep-controls">
      <button className="btn ghost" type="button" onClick={toggle}>{playing?'Pause':'Play'}</button>
      <input type="range" aria-label="Video position" min={0} max={duration/fps-1} value={seconds}
        aria-valuetext={format(seconds)} onChange={event=>player.current.seekTo(Number(event.target.value)*fps)}/>
      <span aria-hidden="true">{format(seconds)} / {format(duration/fps)}</span>
    </div>
    <nav className="prep-chapters" aria-label="Video chapters">
      {chapters.map((item,i)=><button className="btn ghost" type="button" key={item.id}
        aria-current={i===current?'step':undefined} onClick={()=>seek(i)}>{item.name}</button>)}
    </nav>
    <section className="prep-chapter-action" aria-label="Put this chapter into practice">
      <p>{chapter.task}</p>
      <button className="btn" type="button" onClick={()=>openTarget(chapter.target)}>{chapter.link}</button>
    </section>
    {chapter.id==='solve' && <Example/>}
    <details className="prep-transcript">
      <summary>Read the full walkthrough</summary>
      <ol>{chapters.map(item=><li key={item.id}><strong>{item.name}</strong>
        {item.beats.map(beat=><p key={beat}>{beat}</p>)}<p>{item.task}</p></li>)}</ol>
    </details>
  </>;
}

window.PitchPrepPlayer = {
  mount(container, openTarget) {
    const root = createRoot(container);
    root.render(<Walkthrough openTarget={openTarget}/>);
    return ()=>root.unmount();
  }
};
