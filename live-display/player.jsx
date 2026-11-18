import React, {memo, useEffect, useRef} from 'react';
import {createRoot} from 'react-dom/client';
import {Player} from '@remotion/player';
import {AbsoluteFill, interpolate, useCurrentFrame} from 'remotion';

const labels = {info:'Event agenda', prep:'Team preparation', pitch:'Preliminary pitches', scores:'Preliminary leaderboard',
  final:'The Final', awards:'Awards', message:'Event update', quiz:'Event quiz', audience:'Audience question'};
const stations = {waiting:'Ready', pitch:'Pitching', feedback:'Feedback', change:'Changeover', scored:'Scored'};
const name = team => team?.partner || (team?.pair ? 'Team ' + team.pair : '');
const time = seconds => {
  const rounded = Math.max(0, Math.ceil(seconds));
  return Math.floor(rounded / 60) + ':' + String(rounded % 60).padStart(2, '0');
};
const eventTime = new Intl.DateTimeFormat('en-GB', {timeZone:'UTC', hour:'2-digit', minute:'2-digit'});

function Entrance({children, delay = 0, reduced}) {
  const frame = useCurrentFrame();
  const ease = t => 1 - (1 - t) ** 3;
  const position = interpolate(frame, [delay, delay + 14], [16, 0], {easing:ease, extrapolateLeft:'clamp', extrapolateRight:'clamp'});
  return <div style={{transform: reduced ? undefined : 'translateY(' + position + 'px)'}}>{children}</div>;
}

function Clock({label, value, left, duration, caption}) {
  return <div className="hall-card hall-timer"><div className="hall-label">{label}</div><strong>{value}</strong>
    {caption && <p>{caption}</p>}
    {duration > 0 && <progress max={duration} value={Math.max(0, Math.min(left, duration))} aria-label={label}/>}
  </div>;
}

function Agenda({program}) {
  const items = program.items.slice(program.index, program.index + 4);
  return <div className="hall-card"><h3>On the agenda</h3><ol className="hall-agenda">
    {items.map((item, i) => <li key={program.index+i} aria-current={i === 0 ? 'step' : undefined}>
      <time>{item.time}</time><span>{item.title}</span>
    </li>)}
  </ol></div>;
}

function Current({current, pitch, steps, reduced}) {
  const todo = (current.do?.length ? current.do : [current.line]).filter(Boolean).slice(0, pitch ? 2 : 3);
  return <div className="hall-current">
    <Entrance reduced={reduced}><h2>{current.title}</h2></Entrance>
    <ol>{todo.map((item,i) => <li key={i}>{item}</li>)}</ol>
    {pitch && <div className="hall-steps">{steps.slice(0,4).map((step,i) =>
      <div key={i}><strong>{step.name}</strong><span>{step.minutes} min</span></div>
    )}</div>}
  </div>;
}

function Progress({data}) {
  const total = data.pairs || 0;
  const scored = data.scored || 0;
  return <div className="hall-card hall-progress"><h3>Pitch progress</h3>
    <strong>{scored}<small> / {total} teams scored</small></strong>
    <progress max={total || 1} value={Math.min(scored,total)} aria-label="Teams scored"/>
    {data.stations.length <= 12 && <div className="hall-stations" aria-label="Station status">
      {data.stations.map(station => <div key={station.station} data-state={station.state}>
        <b>{station.station}</b><span>{stations[station.state] || 'Idle'}</span>
      </div>)}
    </div>}
  </div>;
}

function Scores({data, names, page, top, rotate, reduced}) {
  const total = data.rows.length;
  const pages = Math.max(1, Math.ceil(total / top));
  const index = rotate ? page % pages : 0;
  const rows = data.rows.slice(index * top, (index + 1) * top);
  return <div className="hall-scores">
    <header><h2>Preliminary leaderboard</h2><p>{data.scored} / {data.pairs} teams scored{rotate && pages > 1 ? ' · Page ' + (index + 1) + ' of ' + pages : ''}</p></header>
    {data.off || !total ? <div className="hall-empty">{data.off ? 'Scores will appear when the Desk releases them.' : 'Waiting for the first scores.'}</div> :
      <ol>{rows.map((team,i) => <li key={team.pair}>
        <Entrance delay={2 + i * 2} reduced={reduced}><div className="hall-score-row" data-rank={team.rank}>
          <strong className="hall-rank">{team.rank}</strong>
          <div className="hall-team"><strong>{name(team)}</strong><span>{names ? KEV.members(team) || 'Team ' + team.pair : 'Team ' + team.pair}</span></div>
          <strong className="hall-points">{team.total}<small> pts</small></strong>
        </div></Entrance>
      </li>)}</ol>}
  </div>;
}

function Final({data, elapsed, names}) {
  const final = data.final || {};
  const teams = final.pairs || [];
  const status = KEV.finalStatus(final);
  const active = ['ready','pitch','collecting'].includes(status) ? teams[final.current] : null;
  const clock = data.clock;
  const left = Math.max(0, (clock.round_left || 0) - (clock.running ? elapsed : 0));
  return <div className="hall-grid">
    <div className="hall-current">
      {active && <div className="hall-label">Finalist {final.current + 1} of {teams.length}</div>}
      <h2>{active ? name(active) : status === 'review' ? 'Judges are confirming results' : 'Finalists take the stage'}</h2>
      {active ? <p>{(names && KEV.members(active)) || 'Team ' + active.pair}{final.case_name ? ' · ' + final.case_name : ''}</p> :
        <p>{status === 'review' ? 'Results will appear when the Desk publishes them.' : 'Each team returns to its original customer scenario.'}</p>}
      {active && (status === 'collecting' ? <p>Please stay on stage for feedback.</p> :
        <Clock label={clock.running ? 'Pitch time remaining' : clock.started_any ? 'Pitch paused' : 'Ready to pitch'}
          value={time(left)} left={left} duration={480}/>)}
    </div>
    <div className="hall-card"><h3>Stage order</h3><ol className="hall-finalists">
      {teams.map((team,i) => <li key={team.pair} aria-current={active && i === final.current ? 'step' : undefined}>
        <b>{i+1}</b><div><strong>{name(team)}</strong><span>{team.completed ? 'Complete' : active && i === final.current ? 'On stage' : 'Team ' + team.pair}</span></div>
      </li>)}
    </ol>{!teams.length && <p>To be announced.</p>}</div>
  </div>;
}

function Awards({data, reduced}) {
  const podium = (data.podium || []).filter(team => [1,2,3].includes(team.rank));
  const winners = data.reveal === 'winners';
  if (!podium.length) return <div className="hall-message"><h2>Awards</h2><p>The winners will be announced shortly.</p></div>;
  return <div className="hall-awards"><h2>{winners ? 'Congratulations' : 'Preliminary top three'}</h2>
    <div className="hall-podium">{[2,1,3].map(rank => {
      const team = podium.find(item => item.rank === rank);
      if (!team) return null;
      return <Entrance key={rank} delay={rank === 1 ? 0 : 5} reduced={reduced}><div className="hall-card" data-rank={rank}>
        <div className="hall-label">{['','1st place','2nd place','3rd place'][rank]}</div>
        <strong>{name(team)}</strong><p>Team {team.pair}</p>
        {Number.isFinite(team.total) && <b>{team.total}<small>{winners ? ' / 5' : ' pts'}</small></b>}
      </div></Entrance>;
    })}</div>
  </div>;
}

function QuizBoard({rows, wide=false}) {
  if (!rows) return null;
  return <section className={'hall-quiz-board'+(wide?' hall-quiz-board-wide':'')} aria-label="Quiz leaderboard">
    <h3>Quiz leaderboard</h3><p>Standings so far</p>
    {rows.length ? <ol>{rows.slice(0, wide?10:5).map((row,i)=><li key={i}>
      <b>{row.rank}</b><span>{row.name}</span><strong>{row.earned} {row.earned===1?'pt':'pts'}</strong>
    </li>)}</ol> : <p>Points will appear after a scored question closes.</p>}
  </section>;
}

function QuizScene({quiz,now,reduced,rows}) {
  if (!quiz) return null;
  if (quiz.kind==='spotlight') return <QuizBoard rows={quiz.rows} wide/>;
  if (quiz.kind==='question') {
    const denominator=quiz.bars?.reduce((sum,row)=>sum+row.count,0)||1;
    const content = <div className="hall-quiz">
      <div className="hall-quiz-title"><h2>{quiz.prompt}</h2>
        <strong>{quiz.status==='open'?(quiz.end===null?'Open':time(quiz.end-now)):quiz.results_pending?'Results pending':quiz.bars?'Results':'Closed'}</strong></div>
      <ul>{(quiz.bars||quiz.options).map((option,i)=><li key={option.key} data-correct={quiz.correct?.includes(option.key)}>
        <Entrance delay={i*2} reduced={reduced}><div className="hall-quiz-option"><b>{option.key}</b><span>{option.text}</span>
          {quiz.correct?.includes(option.key) && <small className="hall-correct">Correct</small>}
          {quiz.bars && <strong>{option.count}</strong>}</div>
        {quiz.bars && <progress max={denominator} value={option.count} aria-label={option.text}/>}</Entrance>
      </li>)}</ul>
      {quiz.explanation && <p>{quiz.explanation}</p>}
    </div>;
    return quiz.bars && quiz.display_until && rows
      ? <div className="hall-quiz-results">{content}<QuizBoard rows={rows}/></div> : content;
  }
  return <div className="hall-quiz">
    <h2>{quiz.kind==='champion'?(quiz.tied?'Quiz champions':'Quiz champion'):'Quiz top five'}</h2>
    {quiz.kind==='champion' && quiz.prize && <p className="hall-quiz-prize">{quiz.prize}</p>}
    {quiz.learning?.matched>0 && <p>Learning gain · {quiz.learning.before}% to {quiz.learning.after}% on matched answers</p>}
    <ol>{quiz.rows.map((row,i)=><li key={i}><Entrance delay={i*2} reduced={reduced}><div className="hall-quiz-option">
      <b>{row.rank}</b><span>{row.name}</span><strong>{row.earned} pts</strong>
    </div></Entrance></li>)}</ol>
  </div>;
}

function AudienceScene({question,now,reduced}) {
  const options = question.options || [];
  const bars = question.counts && options.map(option => ({...option, count:question.counts[option.key] || 0}));
  const quiz = {...question, kind:'question', status:question.status === 'live' ? 'open' : 'closed', options, bars};
  return <><QuizScene quiz={quiz} now={now} reduced={reduced}/>
    {question.status === 'live' ? <p>Respond in Messages on your phone.</p> :
      question.count !== undefined && <p>{question.count} responses</p>}</>;
}

const Scene = memo(function Scene({data, scene, now, elapsed, stale, top, page, rotate, names, reduced, header, dir}) {
  const program = data.program;
  const current = program.items[program.index] || {};
  const next = program.items[program.index + 1];
  const plan = (current.min || 0) * 60;
  const remaining = plan && Number.isFinite(program.since) ? plan - Math.max(0, now - program.since) : null;
  const clock = data.clock || {};
  const pitchLeft = Math.max(0, (clock.phase_left || 0) - (clock.running ? elapsed : 0));
  const question = scene === 'quiz' || scene === 'audience';
  const board = !question && Array.isArray(data.quiz_leaderboard);
  return <AbsoluteFill className={'hall'+(question ? ' hall-quiz-fullscreen' : '')+(board?' hall-has-quiz-board':'')} dir={dir}>
    {header && !question && <header className="hall-header"><h1>{data.event}</h1>
      <div>{data.rehearsal && <span className="hall-rehearsal">Rehearsal</span>}<time>{eventTime.format(new Date(now*1000))}</time></div>
    </header>}
    <div className="hall-eyebrow">{labels[scene]}{question && data.rehearsal && <span className="hall-rehearsal">Rehearsal</span>}{stale && <span role="status">Connection lost · display paused</span>}</div>
    {data.program.announce && <div className="hall-announcement">{data.program.announce}</div>}
    <div className="hall-content"><div className="hall-programme">
      {scene === 'quiz' && <QuizScene quiz={data.quiz} now={now} reduced={reduced} rows={data.quiz_leaderboard}/>}
      {scene === 'audience' && <AudienceScene question={data.audience} now={now} reduced={reduced}/>}
      {scene === 'info' && <div className="hall-grid"><Current current={current} reduced={reduced}/><Agenda program={program}/></div>}
      {scene === 'prep' && <div className="hall-grid"><Current current={current} reduced={reduced}/>
        <Clock label={remaining < 0 ? 'Preparation running over' : 'Briefing and preparation time left'}
          value={remaining === null ? '—' : (remaining < 0 ? '+' : '') + time(Math.abs(remaining))}
          left={remaining || 0} duration={plan} caption="Follow the host for the call to pitch."/></div>}
      {scene === 'pitch' && <div className="hall-grid"><Current current={current} pitch steps={data.steps} reduced={reduced}/>
        <div className="hall-stack">{data.mode === 'desk' && clock.kind !== 'final' &&
          <Clock label={'Round ' + clock.round + ' · ' + (!clock.started_any ? 'Get ready' :
            clock.running ? ({pitch:'Pitch',feedback:'Feedback',change:'Changeover',over:'Time is up'}[clock.phase] || 'Pitch') : 'Paused')}
            value={clock.started_any ? time(pitchLeft) : '—'}/>}<Progress data={data}/></div></div>}
      {scene === 'scores' && (data.off ? <div className="hall-message"><h2>{current.title}</h2>
        <p>{current.line || 'Scores will appear when the Desk releases them.'}</p></div> :
        <Scores {...{data, names, page, top, rotate, reduced}}/>)}
      {scene === 'final' && <Final {...{data, elapsed, names}}/>}
      {scene === 'awards' && <Awards {...{data,reduced}}/>}
      {scene === 'message' && <div className="hall-message"><h2>{current.title}</h2><p>{current.line || current.do?.[0]}</p></div>}
    </div>{board && <QuizBoard rows={data.quiz_leaderboard}/>}</div>
    {!question && <footer className="hall-footer">
      <div className="hall-track" aria-label="Programme progress">{program.items.map((item,i) =>
        <i key={i} data-stage={i === program.index ? 'now' : i < program.index ? 'done' : 'later'}/>)}</div>
      <div><span>{current.time} · {current.title}</span>{next && <span><b>Up next</b> · {next.time} · {next.title}</span>}</div>
    </footer>}
  </AbsoluteFill>;
});

function Display({props, animationKey}) {
  const player = useRef(null);
  useEffect(() => {
    const ref = player.current;
    if (props.reduced) ref.seekTo(29);
    else { ref.seekTo(0); ref.play(); }
  }, [animationKey, props.reduced]);
  return <Player ref={player} component={Scene} inputProps={props} durationInFrames={30} fps={30}
    compositionWidth={1920} compositionHeight={1080} controls={false} style={{width:'100%', height:'100%'}}/>;
}

class PresenterBoundary extends React.Component {
  state = {failed:false};
  static getDerivedStateFromError() { return {failed:true}; }
  componentDidCatch() { this.props.onFailure?.(); }
  render() { return this.state.failed ? null : this.props.children; }
}

let root;
window.PitchHall = {
  show(props) {
    const mount = document.getElementById('hall-display');
    root ||= createRoot(mount);
    const animationKey = JSON.stringify([props.scene, props.data.program.index, props.scene === 'scores' ?
      [props.page, props.data.off, props.data.rows.map(row => [row.pair,row.rank,row.total])] :
      [props.data.reveal, props.data.final?.current, props.data.final?.status, props.data.quiz?.id, props.data.quiz?.kind, props.data.audience?.id]]);
    root.render(<PresenterBoundary onFailure={props.onFailure}><Display props={props} animationKey={animationKey}/></PresenterBoundary>);
    mount.hidden = false;
  },
  clear() {
    if (root) {root.unmount(); root = null;}
    document.getElementById('hall-display').hidden = true;
  }
};
