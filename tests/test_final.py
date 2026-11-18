"""Final lifecycle through actual hosted judge, Desk and audience routes."""
import json
import unittest
from unittest.mock import patch

import test_event_access as access

S = access.S


class FinalFlow(unittest.TestCase):
    def setUp(self):
        clock = patch.object(S.time, 'time', return_value=1800000000)
        self.time = clock.start(); self.addCleanup(clock.stop)
        self.fixture = access.EventAccess('test_desk_can_run_live_but_cannot_administer_credentials_or_content')
        self.addCleanup(self.fixture.doCleanups); self.fixture.setUp()
        self.desk = self.fixture.login(self.fixture.create('desk')['password'])
        self.viewer = {'X-Key': S.anonymous_view_key()}
        self.judges = {}
        for station in (1, 7):
            issued = self.fixture.create('judge', station=station, label=f'Test Judge {station}')
            self.judges[station] = {'Cookie': self.fixture.login(issued['password'])}


    def command(self, endpoint, status=200, **body):
        code, reply, _ = access.request('POST', '/api/desk/' + endpoint, body, self.desk)
        self.assertEqual(code, status, reply)
        return reply

    def get(self, endpoint, **kwargs):
        code, reply, _ = access.request('GET', endpoint, **kwargs)
        self.assertEqual(code, 200, reply)
        return reply

    def judge(self, station, pair=None):
        return self.get('/api/state' + (f'?final_pair={pair}' if pair is not None else ''), headers=self.judges[station])

    def start_final(self, pairs=None):
        self.command('final', action='set', pairs=pairs or [1, 7, 2], panel=[1, 7])
        return self.command('final', action='start')

    def submit(self, station, pair=None, ticks=5, run_id=None, status=200, final_rating=None):
        state = self.judge(station)
        final_rating = max(1, min(5, ticks)) if final_rating is None else final_rating
        body = {'kind':'final', 'pair': pair or state['assignment']['pair'],
                'run_id':run_id or state['final'].get('run_id'),
                'ticks':[1]*ticks+[0]*(10-ticks), 'ai':0, 'spoke':1,
                'feedback':'Synthetic feedback', 'notes':'Synthetic evidence', 'final_rating':final_rating}
        code, reply, _ = access.request('POST','/api/submit',body,headers=self.judges[station])
        self.assertEqual(code,status,reply)
        return reply

    def finish_team(self, ticks=5):
        self.command('clock', action='finish')
        for station in self.judges:
            self.submit(station, ticks=ticks)

    def complete_final(self):
        self.start_final()
        self.finish_team(ticks=6)
        self.command('final',action='next')
        self.finish_team(ticks=4)
        self.command('final',action='next')
        self.finish_team(ticks=2)
        return self.command('final',action='stop')

    def test_every_judge_gets_current_finalists_original_scenario(self):
        self.start_final()
        for station in self.judges:
            state = self.judge(station)
            self.assertEqual((state['assignment']['kind'],state['assignment']['pair']),('final',1))
            self.assertEqual(state['scenario'], 'S1')
            self.assertFalse(state['has_next'])
        self.finish_team()
        self.command('final',action='next')
        for station in self.judges:
            self.assertEqual(self.judge(station)['scenario'],'S4')
        self.assertEqual(self.get('/api/public',headers=self.viewer)['final']['case_name'],S.scenario_for(7)['name'])

    def test_final_requires_exactly_three_teams(self):
        self.command('final', status=400, action='set', pairs=[1, 7], panel=[1, 7])
        self.command('final', status=400, action='set', pairs=[1, 2, 7, 8], panel=[1, 7])

    def test_preliminary_rating_breaks_equal_score_at_finalist_cutoff(self):
        rows = [
            {'pair':4, 'total':80, 'wrap_rating':3, 'solve':2, 'discover':2},
            {'pair':9, 'total':80, 'wrap_rating':5, 'solve':2, 'discover':2},
            {'pair':2, 'total':90, 'wrap_rating':2, 'solve':2, 'discover':2},
            {'pair':7, 'total':70, 'wrap_rating':5, 'solve':2, 'discover':2},
        ]
        ranked = S.rank(rows)
        self.assertEqual([row['pair'] for row in ranked[:3]], [2, 9, 4])
        self.assertFalse(ranked[1]['tie'])

    def test_final_uses_one_to_five_panel_ratings(self):
        self.start_final(pairs=[1, 2, 7])
        self.command('clock', action='finish')
        self.submit(1, final_rating=5)
        self.submit(7, final_rating=3)
        self.command('final', action='next')
        self.command('clock', action='finish')
        self.submit(1, final_rating=4)
        self.submit(7, final_rating=4)
        self.command('final', action='next')
        self.command('clock', action='finish')
        self.submit(1, final_rating=1)
        self.submit(7, final_rating=3)
        state = self.command('final', action='stop')
        rows = self.get('/api/desk/scores', cookie=self.desk)['final']
        self.assertEqual([(row['pair'], row['final_rating']) for row in rows], [(1, 4), (2, 4), (7, 2)])
        self.assertEqual(state['unresolved_ties'], [[1, 2]])

    def test_final_rejects_missing_or_out_of_range_rating(self):
        self.start_final(pairs=[1, 2, 7])
        state = self.judge(1)
        body = {'kind':'final', 'pair':1, 'run_id':state['final']['run_id'], 'ticks':[0]*10,
                'ai':0, 'spoke':1, 'feedback':'Feedback', 'notes':'Evidence'}
        code, reply, _ = access.request('POST','/api/submit',body,headers=self.judges[1])
        self.assertEqual(code, 400, reply)
        body['final_rating'] = 6
        code, reply, _ = access.request('POST','/api/submit',body,headers=self.judges[1])
        self.assertEqual(code, 400, reply)

    def test_first_and_later_clocks_start_paused_and_are_shared(self):
        self.start_final()
        self.time.return_value += 20
        state = self.judge(1)
        self.assertEqual((state['clock']['kind'],state['clock']['round_left'],state['clock']['running']),('final',480,False))
        self.assertEqual(state['final']['status'],'ready')
        self.command('clock',action='start')
        self.time.return_value += 20
        shared = self.judge(1)['clock']
        self.assertEqual((shared['round_left'],shared['running']),(460,True))
        self.assertEqual(shared,self.judge(7)['clock'])
        self.assertEqual(shared,self.get('/api/desk/state',cookie=self.desk)['clock'])
        self.assertEqual(shared,self.get('/api/board',headers=self.viewer)['clock'])
        self.assertEqual(shared,self.get('/api/public',headers=self.viewer)['clock'])
        self.command('mode',status=409,mode='desk')
        self.command('clock',action='pause')
        self.time.return_value += 30
        self.assertEqual(self.judge(7)['clock']['round_left'],460)
        self.command('clock',action='start')
        self.time.return_value += 460
        self.assertEqual(self.judge(1)['final']['status'],'collecting')
        self.finish_team()
        self.command('final',action='next')
        self.assertEqual((self.judge(1)['clock']['round_left'],self.judge(1)['clock']['running']),(480,False))
        self.command('clock',status=409,action='next')

    def test_handover_requires_panel_receipts_and_late_ballot_is_recoverable(self):
        self.start_final()
        self.command('clock',action='finish')
        missing = self.command('final',status=409,action='next')
        self.assertEqual(missing['missing'],[1,7])
        self.submit(1)
        missing = self.command('final',status=409,action='next')
        self.assertEqual(missing['missing'],[7])
        self.command('final',status=400,action='next',override_reason='x')
        self.command('final',action='next',override_reason='Judge seven reconnecting; recover their sheet later.')
        stage_clock = self.judge(7)['clock']
        recovered = self.judge(7, pair=1)
        self.assertTrue(recovered['assignment']['recovery'])
        self.assertEqual(recovered['scenario'],'S1')
        self.submit(7,pair=1)
        self.assertEqual(self.judge(7)['clock'],stage_clock)
        rows = self.get('/api/desk/scores',cookie=self.desk)['final']
        first = next(row for row in rows if row['pair']==1)
        self.assertEqual((first['judges'],first['expected'],first['complete']),(2,2,True))
        self.assertIn('final-override',[r['action'] for r in S.DB.execute('SELECT action FROM audit')])

    def test_unvisited_or_other_run_ballots_are_rejected(self):
        self.start_final()
        self.submit(1,pair=7,status=409)
        self.submit(1,run_id='different-final-run',status=409)
        self.assertEqual(len(S.active_sheets('final')),0)

    def test_stop_and_publish_require_complete_reviewed_results(self):
        self.start_final()
        self.command('final',status=409,action='stop')
        self.command('reveal',status=409,mode='winners')
        self.command('final',status=409,action='publish',confirm='PUBLISH')
        self.finish_team(ticks=6)
        self.command('final',action='next')
        self.finish_team(ticks=4)
        self.command('final',action='next')
        self.finish_team(ticks=2)
        state=self.command('final',action='stop')
        self.assertEqual(state['status'],'review')
        self.assertTrue(state['can_publish'])
        published=self.command('final',action='publish',confirm='PUBLISH')
        self.assertEqual(published['status'],'completed')
        self.assertTrue(published['published'])
        for route in ('/api/public','/api/board'):
            output=self.get(route,headers=self.viewer)
            self.assertEqual(output['reveal'],'winners')
            self.assertEqual(output['podium'][0]['pair'],1)
            self.assertEqual(output['scene'],'awards')
            self.assertNotIn('notes',json.dumps(output))
        state=self.judge(1)
        self.assertEqual(state['assignment']['kind'],'final')
        self.assertFalse(state['final']['can_submit'])
        self.assertFalse(state['has_next'])
        self.submit(1,status=409)

    def test_ties_require_panel_decision_and_changes_invalidate_decision(self):
        self.start_final()
        self.finish_team(ticks=5)
        self.command('final',action='next')
        self.finish_team(ticks=5)
        self.command('final',action='next')
        self.finish_team(ticks=2)
        state=self.command('final',action='stop')
        self.assertEqual(state['unresolved_ties'],[[1,7]])
        self.command('final',status=409,action='publish',confirm='PUBLISH')
        self.command('final',action='resolve',order=[7,1,2],reason='The panel agreed team seven gave the stronger customer response.')
        self.submit(1,pair=1,ticks=5)
        self.command('final',status=409,action='publish',confirm='PUBLISH')
        self.command('final',action='resolve',order=[7,1,2],reason='The panel reconfirmed team seven after reviewing the correction.')
        self.command('final',action='publish',confirm='PUBLISH')
        self.assertEqual(self.get('/api/public',headers=self.viewer)['podium'][0]['pair'],7)

    def test_qualifier_reveal_never_substitutes_unfinished_final_scores(self):
        self.command('public',mode='off')
        self.start_final()
        self.submit(1,ticks=10)
        self.command('reveal',mode='top3')
        output=self.get('/api/public',headers=self.viewer)
        self.assertEqual(output['podium'],[])
        self.assertEqual(output['rows'],[])
        self.assertNotIn('total',json.dumps(output['final']))
        self.assertEqual(output['scene'],'final')

    def test_desk_station_monitor_tracks_final_not_previous_round(self):
        self.start_final(pairs=[7,1,2])
        desk=self.get('/api/desk/state',cookie=self.desk)
        first=next(row for row in desk['stations'] if row['station']==1)
        self.assertEqual((first['pair'],first['scenario'],first['clock']['kind']),(7,'S4','final'))
        self.assertFalse(first['submitted'])
        self.submit(1)
        desk=self.get('/api/desk/state',cookie=self.desk)
        first=next(row for row in desk['stations'] if row['station']==1)
        self.assertTrue(first['submitted'])
        self.assertEqual(first['score'],50)

    def test_final_review_does_not_resume_rounds_or_announce_future_finalists(self):
        missing = self.command('clock', status=409, action='goto', round=2, station=1)
        self.command('clock', action='goto', round=2, station=1,
                     override_reason='Skip preliminary scoring for this Final fixture.', review_token=missing['review_token'])
        self.command('clock',action='seek',elapsed=130,station=1)
        prior=S.clock_public(1)
        self.complete_final()
        state=self.judge(1)
        self.assertEqual(state['assignment']['kind'],'final')
        self.assertEqual(state['final']['status'],'review')
        self.assertEqual(S.clock_public(1),prior)
        audience=self.get('/api/public',headers=self.viewer)
        self.assertEqual(audience['scene'],'final')
        self.assertEqual(audience['final']['status'],'review')
        self.assertEqual(len(audience['final']['pairs']),3)

    def test_published_results_cannot_be_edited_without_explicit_reopen(self):
        self.complete_final()
        self.command('final',action='publish',confirm='PUBLISH')
        row=S.active_sheets('final')[0]
        self.command('sheet',status=409,action='void',id=row['id'])
        self.command('final',status=400,action='reopen',reason='')
        self.command('final',action='reopen',reason='Correct a score after documented panel review.')
        self.assertEqual(self.get('/api/public',headers=self.viewer)['reveal'],'hidden')
        self.submit(1,pair=1,ticks=7)
        self.command('final',action='publish',confirm='PUBLISH')
        self.assertEqual(self.get('/api/public',headers=self.viewer)['podium'][0]['total'],5)

    def test_non_panel_judge_cannot_submit_final(self):
        self.command('final',action='set',pairs=[1,2,3],panel=[1])
        self.command('final',action='start')
        state=self.judge(7)
        self.assertFalse(state['final']['eligible'])
        self.submit(7,status=403)

    def test_three_member_csv_and_reports_preserve_every_member(self):
        S.event_config.update(S,{'participant_count':5,'team_size':3,'judge_count':2})
        csv='team,company,p1,p2,p3,coach1,coach2,coach3\n1,Example,Alex,Sam,Third Member,1,1,1\n2,Other,Fourth,Fifth,,0,0,0'
        status,reply,_=access.request('POST','/api/desk/roster',{'csv':csv},self.fixture.admin)
        self.assertEqual(status,200,reply)
        row=S.roster_row(1)
        self.assertEqual([m['name'] for m in row['members']],['Alex','Sam','Third Member'])
        self.assertEqual(row['badge'],4)
        self.assertEqual(len(S.roster_row(2)['members']),2)
        board=self.get('/api/board',headers=self.viewer)
        self.assertEqual((board['configuration']['team_size'],board['configuration']['rounds']),(3,1))
        status,reply,_=access.request('POST','/api/submit',{'kind':'round','pair':1,'ticks':[1],'wrap_rating':3},headers=self.judges[1])
        self.assertEqual(status,200,reply)
        for route in ('export.csv','cards'):
            status,payload,_=access.request('GET','/api/desk/'+route,cookie=self.fixture.admin)
            self.assertEqual(status,200)
            self.assertIn(b'Third Member',payload)
        backup=self.get('/api/desk/backup.json',cookie=self.fixture.admin)
        self.assertIn('roster_members',backup)
        self.assertTrue(any(m['member']==3 and m['name']=='Third Member' for m in backup['roster_members']))
        self.assertIn('team_assignments',backup)
        before=S.roster_row(1)
        status,_,_=access.request('POST','/api/desk/roster',{'csv':'pair,partner,p1\ninvalid,Example,Person'},self.fixture.admin)
        self.assertEqual(status,400)
        self.assertEqual(S.roster_row(1),before)

    def test_prepared_final_does_not_announce_the_first_team_or_case(self):
        self.command('final',action='set',pairs=[7,1,2],panel=[1,7])
        state=self.get('/api/public',headers=self.viewer)['final']
        self.assertEqual(state['pairs'],[])
        self.assertIsNone(state['stage_pair'])
        self.assertIsNone(state['scenario'])
        self.assertEqual(state['case_name'],'')

    def test_final_freezes_case_for_judges_and_gateway_context(self):
        self.start_final(pairs=[7,1,2])
        original=self.judge(1)['scenario_snapshot']
        with patch.dict(S.CONTENT['scenarios'][3],name='Changed after Final preparation'):
            for station in self.judges:
                self.assertEqual(self.judge(station)['scenario_snapshot'],original)
            with patch.object(S,'ai_feedback',return_value='Synthetic response') as generate:
                status,reply,_=access.request('POST','/api/ai/feedback',{'ticks':[1], 'scenario':'S1'},headers=self.judges[1])
                self.assertEqual(status,200,reply)
                data=generate.call_args.args[0]
                self.assertEqual((data['scenario'],data['scenario_snapshot']),('S4',original))
            with patch.object(S,'ai_chat',return_value='Synthetic response') as gateway:
                S.ai_feedback(data)
                self.assertIn(original['name'],gateway.call_args.args[1])
                self.assertNotIn('Changed after Final preparation',gateway.call_args.args[1])
        public=self.get('/api/public',headers=self.viewer)
        self.assertNotIn('scenario_snapshot',json.dumps(public))
        self.assertNotIn('objections',json.dumps(public))

    def test_failed_clock_handover_does_not_record_an_exception(self):
        self.start_final()
        self.submit(1)
        self.command('clock',action='start')
        self.command('final',status=409,action='next',override_reason='Judge device temporarily offline.')
        self.assertEqual(S.kv_get('final')['waivers'],{})
        self.assertEqual(S.DB.execute("SELECT count(*) FROM audit WHERE action='final-override'").fetchone()[0],0)

    def test_legacy_final_migrates_receipts_without_publishing_or_changing_rounds(self):
        self.start_final(pairs=[7,1,2])
        self.submit(1)
        self.submit(7)
        with S.DB:
            S.DB.execute("UPDATE sheets SET final_run='' WHERE kind='final'")
        S.kv_set('final',{'active':False,'pairs':[7,1],'current':0})
        S.kv_set('reveal','winners')
        state=self.get('/api/desk/state',cookie=self.desk)
        self.assertEqual(state['final']['status'],'review')
        self.assertTrue(state['final']['run_id'].startswith('legacy-'))
        self.assertEqual(sum(row['judges'] for row in S.final_board()[0]),0)
        self.assertEqual(state['final']['legacy_ballots_remaining'],2)
        self.assertEqual(len(S.active_sheets('final')),2)
        self.assertTrue(all(row['final_run']==state['final']['run_id'] for row in S.active_sheets('final')))
        self.assertEqual(self.judge(1)['scenario'],'S4')
        self.assertNotIn('podium',self.get('/api/public',headers=self.viewer))
        self.assertEqual(self.get('/api/public',headers=self.viewer)['reveal'],'hidden')

    def test_old_client_cannot_submit_to_migrated_prepared_final(self):
        S.kv_set('final',{'active':False,'pairs':[1,7],'current':0})
        self.get('/api/desk/state',cookie=self.desk)
        self.command('final',action='start')
        status,reply,_=access.request('POST','/api/submit',{'kind':'final','pair':1,'ticks':[1]*10,'scenario':'DEMO'},headers=self.judges[1])
        self.assertEqual(status,409,reply)
        self.assertIn('run',reply['error'].lower())
        self.assertEqual(S.active_sheets('final'),[])
        self.submit(1)

    def test_migrated_wrong_case_ballots_require_original_case_replacements(self):
        self.start_final(pairs=[1,2,3])
        self.submit(1);self.submit(7)
        old_ids=[row['id'] for row in S.active_sheets('final')]
        with S.DB:S.DB.execute("UPDATE sheets SET final_run='',app_version='4.2.1' WHERE kind='final'")
        S.kv_set('final',{'active':False,'pairs':[1],'current':0})
        state=self.get('/api/desk/state',cookie=self.desk)['final']
        self.assertTrue(state['legacy_case_review_required'])
        self.assertEqual(state['legacy_ballots_remaining'],2)
        self.assertIn('original scenario',state['legacy_case_warning'])
        self.assertFalse(state['can_publish'])
        self.assertFalse(state['all_complete'])
        station=self.get('/api/desk/state',cookie=self.desk)['stations'][0]
        self.assertFalse(station['submitted'])
        self.assertTrue(station['needs_rescore'])
        self.assertTrue(next(r for r in state['receipts'] if r['station']==1)['needs_rescore'])
        judge=self.judge(1)
        self.assertIsNone(judge['submitted'])
        self.assertTrue(judge['final']['needs_rescore'])
        self.assertEqual(judge['final']['pending_assignments'],[1])
        self.command('final',status=409,action='publish',confirm='PUBLISH')
        self.command('final',status=409,action='stop',override_reason='One judge offline during correction.')
        self.submit(1,ticks=6)
        state=self.get('/api/desk/state',cookie=self.desk)['final']
        self.assertEqual(state['legacy_ballots_remaining'],1)
        self.assertFalse(state['can_publish'])
        self.submit(7,ticks=6)
        state=self.get('/api/desk/state',cookie=self.desk)['final']
        self.assertFalse(state['legacy_case_review_required'])
        self.assertFalse(self.judge(1)['final']['needs_rescore'])
        # Migrated panel retains all configured judges. Deliberate waiver remains
        # possible only after at least one new, correctly scoped ballot exists.
        self.command('final',action='stop',override_reason='Remaining panel members unavailable after reviewed correction.')
        state=self.command('final',action='publish',confirm='PUBLISH')
        self.assertEqual(state['status'],'completed')
        self.assertEqual(self.get('/api/public',headers=self.viewer)['podium'][0]['total'],5)
        old=list(S.DB.execute('SELECT id,voided FROM sheets WHERE id IN (?,?)',old_ids))
        self.assertEqual(len(old),2)
        self.assertTrue(all(row['voided'] for row in old))


if __name__=='__main__': unittest.main()
