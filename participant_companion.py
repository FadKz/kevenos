"""Own-team next actions and explicitly released results for participants.

This projection never serializes a score sheet, judge notes, or another
participant's identity. The caller supplies only the session's assignment.
"""


def _action(participant: dict, code: str, title: str, instruction: str,
            detail: str = '', clock: dict | None = None) -> dict:
    return {'code': code, 'title': title, 'instruction': instruction,
            'detail': detail, 'station': participant['station'],
            'round': participant['round'], 'clock': clock}


def _round_result(submitted: bool) -> dict:
    return {'status': 'under_review' if submitted else 'awaiting',
            'label': 'Your round result is under review.' if submitted else 'Awaiting your round result.',
            'total': None, 'rank': None, 'kind': 'round'}


def _final_result(s, participant: dict, final: dict) -> dict:
    pair = participant['pair']
    finished = final['status'] in ('review', 'completed') or (
        pair in final.get('visited', []) and
        (pair != s.final_event.current_pair(final) or final['status'] == 'collecting'))
    result = {'status': 'under_review' if finished else 'awaiting',
              'label': 'Your Final result is under review.' if finished else 'Awaiting your Final result.',
              'total': None, 'rank': None, 'kind': 'final'}
    # Final release is a frozen, reviewed snapshot, never the live tally.
    if final['status'] == 'completed' and s.kv_get('reveal', 'hidden') == 'winners':
        row = next((row for row in final.get('published_rows', []) if row['pair'] == pair), None)
        if row:
            result.update(status='released', label='Your published Final result.',
                          total=row['total'], rank=row['rank'])
    return result


def _final_action(s, participant: dict, final: dict, qualified: bool) -> dict:
    action = lambda *args, **kwargs: _action(participant, *args, **kwargs)
    status = final['status']
    if status == 'completed' and s.kv_get('reveal', 'hidden') == 'winners':
        return action('final_complete', 'The Final is complete',
                      'Celebrate the teams and follow the Event Desk’s next instruction.',
                      'Your published result is shown below.' if qualified else 'The Final results have been published.')
    if status in ('review', 'completed'):
        return action('final_review', 'Final results are being reviewed',
                      'Stay nearby and wait for the Event Desk to announce the results.',
                      'Scores appear only after the results are released.')
    if not qualified:
        return action('final_watch', 'Watch the Final',
                      'Take a seat and support the finalist teams.',
                      'You do not have a pitch in this Final. Follow the current programme and the Event Desk’s guidance.')
    current_pair = s.final_event.current_pair(final)
    if participant['pair'] != current_pair:
        if participant['pair'] in final.get('visited', []):
            return action('final_submitted', 'Your Final pitch is finished',
                          'Take a seat and wait for the remaining teams and results.',
                          'The panel’s scores stay private until the Event Desk publishes the results.')
        return action('final_upcoming', 'Get ready for your Final pitch',
                      'Stay nearby with your team and wait to be called to the stage.',
                      'Use your original customer scenario. Your Final pitch lasts 8 minutes.')
    clock = s.final_clock_public(final)
    if status == 'collecting' or clock['ended']:
        return action('final_feedback', 'Listen to the panel’s feedback',
                      'Your Final pitch has ended. Stop pitching and follow the panel’s guidance.',
                      'Wait for the Event Desk before leaving the stage.', clock)
    if clock['running']:
        return action('final_pitch', 'Your Final pitch is live',
                      'Pitch your original scenario to the panel. Make sure every team member speaks.',
                      'Stop when the pitch clock ends or the Event Desk asks you to stop.', clock)
    if clock['started_any']:
        return action('final_paused', 'Your Final pitch is paused',
                      'Pause and follow the Event Desk’s guidance. Wait for the clock to resume.', clock=clock)
    return action('final_ready', 'Your team is next on stage',
                  'Join your team on stage when the Event Desk calls you. Wait for the start signal.',
                  'Use your original customer scenario. You have 8 minutes.', clock)


def _round_action(s, participant: dict, program: dict, scene: str, submitted: bool) -> dict:
    action = lambda *args, **kwargs: _action(participant, *args, **kwargs)
    station, own_round = participant['station'], participant['round']
    clock = s.clock_public(station)
    slot = f'Your assignment is station {station}, round {own_round}.'
    same_slot = clock['round'] == own_round and s.pair_for(own_round, station) == participant['pair']
    # Calibration has its own judge assignment, even though it shares clocks.
    calibration = bool((s.kv_get('calib') or {}).get('active'))
    if program['scene'] != 'pitch' and not (same_slot and not calibration and
            clock['started_any'] and not clock['ended']):
        instruction = program['instructions'][0] if program['instructions'] else 'Follow the current programme and the Event Desk’s instructions.'
        return action('program', program['title'], instruction, slot)
    if same_slot and not calibration:
        if clock['ended'] or clock['phase'] in ('change', 'over'):
            return action('submitted' if submitted else 'finished', 'Your pitch is finished',
                          'Return to your table when the judge directs you. You have one preliminary pitch.',
                          'Wait for your released result and the Final announcement.', clock)
        if clock['running'] or clock['started_any']:
            if not clock['running']:
                return action('paused', 'Your session is paused',
                              'Pause and follow your judge’s guidance. Wait for the clock to resume.', slot, clock)
            if clock['phase'] == 'feedback':
                return action('feedback', 'Listen to your judge’s feedback',
                              'Stop pitching and listen. Stay at your station until the judge directs you to leave.',
                              'After feedback, return to your table and wait for results.', clock)
            step = s.STEP_NAMES[min(clock['step'], len(s.STEP_NAMES) - 1)]
            return action('pitch', 'Your pitch is live',
                          f'You are in the {step} step. Follow your judge and make sure every team member speaks.',
                          'The 10-minute pitch is followed by 3 minutes of feedback.', clock)
    # A receipt may arrive early; it must never end an active pitch or feedback.
    if submitted:
        return action('submitted', 'Your round sheet has been received',
                      'Stay with your team and follow the current programme.',
                      'Wait for released results and the Final announcement. You have one preliminary pitch.')
    if not calibration and clock['round'] > own_round:
        return action('check_desk', 'Check with the Event Desk',
                      'Your station is set to a later round and no result is recorded for your team. Ask the Event Desk what to do.',
                      slot)
    if calibration or (scene != 'pitch' and not clock['running'] and not clock['started_any']):
        instruction = program['instructions'][0] if program['instructions'] else 'Follow the current programme and the Event Desk’s instructions.'
        return action('program', program['title'], instruction,
                      slot + ' Stay with your team; wait until your team is called.')
    if same_slot:
        return action('ready', 'Get ready at your assigned station',
                      f'Go to station {station} when your team is called. Wait for your judge’s start signal.',
                      f'Your pitch is in round {own_round}; the clock has not started.', clock)
    return action('upcoming', 'Your pitch is coming up',
                  f'Stay with your team at your table. Go to station {station} when round {own_round} and your team are called.',
                  f'Your station is currently set to round {clock["round"]}.')


def state(s, participant: dict) -> dict:
    """Called with the request workspace guard and server lock held."""
    pr = s.program_now()
    agenda = s.agenda()
    current = agenda[pr['i']] if agenda else {}
    next_item = agenda[pr['i'] + 1] if pr['i'] + 1 < len(agenda) else {}
    legacy_rotation = 'One bell: stop pitching. Two bells: move to your next station.'
    instructions = [
        'One bell: stop pitching. When directed, make way for the next team and return to your table.'
        if instruction == legacy_rotation else instruction
        for instruction in current.get('do') or []
    ]
    program = {'title': current.get('title') or 'Follow the Event Desk',
               'line': current.get('line', ''), 'instructions': instructions,
               'scene': current.get('scene', 'info'),
               'next_title': next_item.get('title', ''), 'index': pr['i'],
               'items': [{'title': item.get('title', ''), 'time': item.get('time', ''),
                          'detail': item.get('line', ''), 'min': item.get('min', 0)}
                         for item in agenda]}
    final = s.final_event.get(s)
    visible = s.final_event.engaged(final)
    qualified = participant['pair'] in final.get('pairs', []) if visible else None
    safe_final = s.final_event.public_view(s, final, names=False) | {'qualified': qualified}
    # Existence is enough for the action; no sheet contents cross this boundary.
    submitted = bool(s.DB.execute(
        "SELECT 1 FROM sheets WHERE kind='round' AND pair=? AND voided=0 LIMIT 1",
        (participant['pair'],)).fetchone())
    result = _final_result(s, participant, final) if qualified else _round_result(submitted)
    # A published Final keeps its result, but the next agenda section owns Today.
    show_programme = (visible and final['status'] == 'completed' and
                      s.kv_get('reveal', 'hidden') == 'winners' and
                      program['scene'] in {'awards', 'message'})
    if show_programme:
        action = _action(participant, 'program', program['title'],
                         instructions[0] if instructions else 'Follow the current programme and the Event Desk’s instructions.')
    elif visible:
        action = _final_action(s, participant, final, qualified)
    else:
        action = _round_action(s, participant, program, s.display_scene(final, s.kv_get('reveal', 'hidden')), submitted)
    text = pr.get('announce', '')
    announcement = {'text': text, 'updated_at': pr.get('announce_updated_at')} if text.strip() else None
    from event_push import MESSAGES_KEY
    messages = s.kv_get(MESSAGES_KEY) or []
    return {'participant': participant, 'messages': messages, 'now': s.time.time(), 'rehearsal': bool(s.CFG.get('rehearsal')),
            'content_version': s.CONTENT.get('content_version', 1),
            'program': program, 'action': action, 'result': result,
            'announcement': announcement, 'final': safe_final}
