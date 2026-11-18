import React from 'react';
import {AbsoluteFill, Sequence, interpolate, useCurrentFrame} from 'remotion';
import {chapters, fps, starts} from './prep-chapters';

const steps = ['Discover','Solution at Work','Solve','Close'];
const times = ['2 min','3 min','3 min','2 min'];
const mint = '#8ae9d9';
const blue = '#b1d5ff';

function WorkspaceExample({live}) {
  return <div style={{background:'#0c2036',border:'1px solid #446484',borderRadius:12,padding:12}}>
    <div style={{display:'flex',gap:9,marginBottom:10,fontSize:17,fontWeight:700}}>
      {['Today','Agenda','My Pitch','Results','Resources'].map(tab=><span key={tab} style={{padding:'3px 7px',
        borderRadius:6,background:tab===(live?'Today':'My Pitch')?'#254f68':'transparent',
        color:tab===(live?'Today':'My Pitch')?mint:blue}}>{tab}</span>)}
    </div>
    <div style={{display:'flex',alignItems:'center',gap:12,justifyContent:'space-between'}}>
      <div style={{display:'grid',gap:3}}>
        <strong style={{fontSize:22}}>{live?'Your station and round':'Team worksheet · Discover'}</strong>
        <span style={{fontSize:17,color:blue}}>{live?'Open your speaking cues when called':'Speaker and speaking cues'}</span>
      </div>
      <strong style={{fontSize:19,color:'#0d283e',background:mint,borderRadius:7,padding:'6px 10px'}}>
        {live?'Open Pitch Mode':'Save for the team'}
      </strong>
    </div>
  </div>;
}

function ChapterScene({chapter, index, still}) {
  const frame = useCurrentFrame();
  const half = chapter.seconds * fps / 2;
  const second = frame >= half;
  const beatFrame = second ? frame - half : frame;
  const ease = {extrapolateLeft:'clamp',extrapolateRight:'clamp'};
  const rise = still ? 0 : interpolate(beatFrame,[0,13],[11,0],ease);
  const labels = chapter.labels;
  return <AbsoluteFill style={{background:'linear-gradient(155deg,#142f4d,#0b1c31 75%)',color:'#f3f8ff',
      padding:'28px 34px 22px',fontFamily:'system-ui,sans-serif',display:'flex',flexDirection:'column',gap:16}}>
    <div style={{display:'flex',alignItems:'center',justifyContent:'space-between',color:mint,fontSize:23,fontWeight:750}}>
      <span>{chapter.name}</span><span style={{fontVariantNumeric:'tabular-nums',color:blue}}>{index + 1} / {chapters.length}</span>
    </div>
    <div style={{fontSize:47,lineHeight:1.03,fontWeight:790,letterSpacing:-1.5}}>{chapter.title}</div>
    {chapter.id==='workspace' ? <WorkspaceExample live={second}/> :
    <div style={{display:'grid',gridTemplateColumns:'repeat('+labels.length+',1fr)',gap:8}}>
      {labels.map((label,i)=>{
        const current = i === (second ? labels.length-1 : 0);
        return <div key={label} style={{padding:'11px 6px',textAlign:'center',fontSize:21,lineHeight:1.12,fontWeight:700,
          borderRadius:11,border:'1px solid '+(current ? mint : '#446484'),background:current?'#21465b':'#132d4a',
          color:current?'#c8fff5':'#e2edfa'}}>{label}</div>;
      })}
    </div>}
    <div data-prep-beat style={{fontSize:30,lineHeight:1.18,fontWeight:560,transform:'translateY('+rise+'px)'}}>{chapter.beats[Number(second)]}</div>
    <div data-prep-timing style={{display:'grid',gridTemplateColumns:'repeat(4,1fr)',gap:7,marginTop:'auto'}}>
      {steps.map((label,i)=><div key={label} style={{borderTop:'4px solid '+(chapter.step===i?mint:'#446484'),
        color:chapter.step===i?mint:blue,paddingTop:6,fontSize:17,fontWeight:650}}>{label} · {times[i]}</div>)}
    </div>
  </AbsoluteFill>;
}

export function PrepVideo({still = false}) {
  return chapters.map((chapter,i)=><Sequence key={chapter.id} name={chapter.name} from={starts[i]} durationInFrames={chapter.seconds*fps}>
    <ChapterScene chapter={chapter} index={i} still={still}/>
  </Sequence>);
}
