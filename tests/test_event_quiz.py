import unittest
from unittest.mock import patch
import event_quiz
from event_quiz import bank, score, result_ready

class QuizRules(unittest.TestCase):
    def test_bank_uses_approved_rules(self):
        items = bank()
        self.assertEqual(len(items), 44)
        self.assertEqual(sum(q['points'] for q in items.values()), 55)
        self.assertEqual(items['PR2']['time'], '11:20')
        self.assertEqual(items['F1']['points'], 0)

    def test_points_available_skip_void_and_pending_prediction(self):
        runs = [
            {'id':'first','status':'closed','points':3,'keys':['B']},
            {'id':'skipped','status':'skipped','points':2,'keys':['A']},
            {'id':'void','status':'void','points':8,'keys':['C']},
            {'id':'pred','status':'closed','points':2,'keys':[]},
            {'id':'pulse','status':'closed','points':0,'keys':[]},
        ]
        self.assertEqual(score(runs, {'first':{'key':'B','elapsed':3}}),
                         {'earned':3,'available':3,'pending':2,'time':3})

    def test_answer_key_release_is_delayed(self):
        run = {'status':'closed','kind':'warmup','keys':['A'],'released':True}
        self.assertFalse(result_ready(run, False))
        self.assertTrue(result_ready(run, True))
        self.assertFalse(result_ready(run | {'status':'open'}, True))

import test_workspace_routes as workspace


class QuizRoutes(unittest.TestCase):
    def setUp(self):
        self.f = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        self.addCleanup(self.f.doCleanups)
        self.f.setUp()
        self.f.post('/api/desk/roster',{'rows':[{'pair':1,'partner':'Example Company','p1':'Test Person'}]},cookie=self.f.admin)
        self.player_password = self.f.issue('participant',pair=1,member=1)
        self.player = self.f.login(self.player_password)
        self.desk = '/api/desk/quiz'
        self.quiz = '/api/participant/quiz'

    def act(self,action,status=200,**fields):
        rev = self.f.get(self.desk,cookie=self.f.admin)[0]['rev']
        return self.f.post(self.desk,{'action':action,'rev':rev,**fields},status=status,cookie=self.f.admin)[0]

    def read(self):
        return self.f.get(self.quiz,cookie=self.player)[0]

    def test_participant_write_reads_large_quiz_only_once(self):
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        run=self.act('send',qid='W2')['runs'][-1]
        read=workspace.S.kv_get
        keys=[]
        def count(key,*args,**kwargs):
            keys.append(key)
            return read(key,*args,**kwargs)
        with patch.object(workspace.S,'kv_get',side_effect=count):
            result,_=self.f.post(self.quiz,{'action':'answer','id':run['id'],'key':'B'},cookie=self.player)
        self.assertEqual(keys.count(event_quiz.KEY),1)
        self.assertTrue(any(row['id']==run['id'] for row in result['history']))

    def test_room_send_releases_and_collapses_on_server_time(self):
        import event_quiz
        import server as s
        from unittest.mock import patch

        self.act('settings', enabled=True)
        self.f.post('/api/desk/program', {'action':'set','index':1}, cookie=self.f.admin)

        def room(names=True):
            key = s.named_view_key() if names else s.anonymous_view_key()
            return self.f.get('/api/public?presenter=1', headers={'X-Key':key})[0]

        with patch.object(event_quiz, 'now', return_value=1000):
            run, = self.act('send', qid='W2', room=True)['runs']
            self.assertEqual(room()['quiz']['id'], run['id'])
            self.assertNotIn('correct', room()['quiz'])
            self.f.post(self.quiz, {'action':'answer','id':run['id'],'key':'B'}, cookie=self.player)
            self.assertEqual(room()['quiz_leaderboard'], [])
        with patch.object(event_quiz, 'now', return_value=1060):
            state = room()
            self.assertEqual(state['quiz']['correct'], ['B'])
            self.assertEqual(state['quiz']['display_until'], 1075)
            self.assertEqual(state['quiz_leaderboard'][0]['earned'], 1)
            self.assertEqual(room(False)['quiz_leaderboard'], state['quiz_leaderboard'])
            self.assertEqual(room(False)['quiz_leaderboard'][0]['name'], 'Test P.')
            self.assertNotIn('Test Person', str(room(False)['quiz_leaderboard']))
            self.assertNotIn('Example Company', str(room(False)['quiz_leaderboard']))
            self.assertEqual(self.read()['score']['earned'], 1)
        with patch.object(event_quiz, 'now', return_value=1075):
            state = room()
            self.assertIsNone(state['quiz'])
            self.assertEqual(state['room']['source'], 'agenda')
            self.assertEqual(state['quiz_leaderboard'][0]['earned'], 1)
            self.act('spotlight')
            self.assertEqual(room()['quiz']['display_until'], 1090)
        with patch.object(event_quiz, 'now', return_value=1090):
            self.assertIsNone(room()['quiz'])

    def test_reset_archives_current_workspace_and_restores_without_broadcast(self):
        self.act('settings',enabled=True)
        self.act('prize',prize='Synthetic prize')
        self.act('edit',qid='W2',changes={'prompt':'Synthetic draft'})
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.f.post('/api/desk/program',{'action':'set','index':1},cookie=self.f.admin)
        before = self.act('send',qid='W2')
        run, = before['runs']
        self.f.post(self.quiz,{'action':'view','id':run['id']},cookie=self.player)
        self.f.post(self.quiz,{'action':'answer','id':run['id'],'key':'B'},cookie=self.player)
        self.act('reset',status=400,confirm='RESET QUIZ',reason='  ')
        cleared = self.act('reset',confirm='RESET QUIZ',reason='Start a new quiz')
        self.assertEqual(cleared['rev'],before['rev']+1)
        self.assertFalse(cleared['enabled'])
        self.assertEqual(cleared['runs'],[])
        self.assertIsNone(cleared['hall'])
        self.assertEqual((cleared['prize'],cleared['analytics']['summary']['joined']),('Synthetic prize',1))
        self.assertEqual(next(q['prompt'] for q in cleared['bank'] if q['id']=='W2'),'Synthetic draft')
        archive, = cleared['reset_archives']
        self.assertEqual(set(archive),{'id','at','reason','run_count','answer_count'})
        self.assertEqual((archive['run_count'],archive['answer_count']), (1,1))
        archive_id = archive['id']
        self.assertEqual(archive_id,cleared['restorable_reset_id'])
        organizer = self.f.login(self.f.issue('desk'))
        projected = self.f.get(self.desk,cookie=organizer,headers={'X-Dashboard':'organizer'})[0]
        self.assertEqual(projected['reset_archives'],[archive])
        self.assertNotIn('Test Person',str(projected['reset_archives']))
        self.f.switch('rehearsal')
        self.assertEqual(self.f.get(self.desk,cookie=self.f.admin)[0]['reset_archives'],[])
        self.act('restore_reset',status=409,archive_id=archive_id,confirm='RESTORE QUIZ',reason='Restore old quiz')
        self.f.switch('live')
        restored = self.act('restore_reset',archive_id=archive_id,confirm='RESTORE QUIZ',reason='Restore old quiz')
        self.assertGreater(restored['rev'],cleared['rev'])
        self.assertFalse(restored['enabled'])
        self.assertIsNone(restored['hall'])
        self.assertIsNone(restored['restorable_reset_id'])
        self.assertEqual([(r['status'],r['responses']) for r in restored['runs']],[('closed',1)])
        self.player = self.f.login(self.player_password)
        self.act('settings',enabled=True)
        self.assertEqual(self.read()['questions'],[])
        self.assertEqual(self.read()['score']['earned'],1)
        self.assertEqual(restored['analytics']['summary']['viewers'],1)

    def test_reset_keeps_three_snapshots_and_refuses_restore_after_new_work(self):
        ids = []
        for _ in range(4):
            state = self.act('reset',confirm='RESET QUIZ',reason='Fresh event round')
            ids.append(state['restorable_reset_id'])
        self.assertEqual([x['id'] for x in state['reset_archives']],ids[:0:-1])
        self.act('restore_reset',status=409,archive_id=ids[-2],confirm='RESTORE QUIZ',reason='Restore older round')
        self.act('edit',qid='W2',changes={'prompt':'New bank work'})
        self.assertIsNone(self.f.get(self.desk,cookie=self.f.admin)[0]['restorable_reset_id'])
        self.act('restore_reset',status=409,archive_id=ids[-1],confirm='RESTORE QUIZ',reason='Restore older round')

    def test_reset_archive_state_and_audit_roll_back_together(self):
        import event_quiz
        self.act('settings',enabled=True)
        s = workspace.S
        state = event_quiz.load(s)
        s.DB.execute("CREATE TEMP TRIGGER quiz_reset_failure BEFORE INSERT ON audit WHEN NEW.action='quiz-reset' "
                     "BEGIN SELECT RAISE(ABORT,'synthetic audit write failure'); END")
        self.act('reset',status=500,confirm='RESET QUIZ',reason='Test rollback')
        self.assertEqual(event_quiz.load(s),state)
        self.assertEqual(event_quiz.reset_archives(s),[])
        self.assertEqual(s.DB.execute("SELECT count(*) FROM audit WHERE action='quiz-reset'").fetchone()[0],0)

    def test_question_reset_discards_points_and_queues_a_fresh_run(self):
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.f.post('/api/desk/program',{'action':'set','index':1},cookie=self.f.admin)
        run, = self.act('send',qid='W2')['runs']
        self.f.post(self.quiz,{'action':'answer','id':run['id'],'key':'B'},cookie=self.player)
        self.act('close',id=run['id'])
        self.assertEqual(self.read()['score']['earned'],1)
        self.act('reset_question',status=400,id=run['id'],confirm='RESET QUIZ',reason='Wrong question was sent')
        reset = self.act('reset_question',id=run['id'],confirm='RESET QUESTION',reason='Wrong question was sent')
        self.assertEqual(reset['runs'][0]['status'],'void')
        self.assertEqual(reset['runs'][0]['responses'],1)
        self.assertEqual(self.read()['score']['earned'],0)
        self.assertTrue(next(q['can_send'] for q in reset['bank'] if q['id']=='W2'))
        old,new = self.act('send',qid='W2')['runs']
        self.assertNotEqual(old['id'],new['id'])
        self.assertEqual(self.read()['questions'][0]['id'],new['id'])

    def test_question_reset_cannot_change_published_champion(self):
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.f.post('/api/desk/program',{'action':'set','index':1},cookie=self.f.admin)
        run, = self.act('send',qid='W2')['runs']
        self.f.post(self.quiz,{'action':'answer','id':run['id'],'key':'B'},cookie=self.player)
        self.act('close',id=run['id'])
        self.f.post('/api/desk/program',{'action':'set','index':9},cookie=self.f.admin)
        published = self.act('champion')
        self.act('hide')
        self.f.post('/api/desk/program',{'action':'set','index':1},cookie=self.f.admin)
        self.act('show_champion',status=409)
        self.act('reset_question',status=409,id=run['id'],confirm='RESET QUESTION',reason='Restart this question')
        self.act('void',status=409,id=run['id'])
        cleared = self.act('reset',confirm='RESET QUIZ',reason='Start a new quiz')
        self.assertIsNone(cleared['release'])
        restored = self.act('restore_reset',archive_id=cleared['restorable_reset_id'],confirm='RESTORE QUIZ',reason='Restore old quiz')
        self.assertEqual(restored['release'],published['release'])
        self.assertFalse(restored['hall_visible'])

    def test_retime_preview_shifts_only_unsent_cues_and_checks_revision(self):
        self.act('settings',enabled=True)
        self.f.post('/api/desk/program',{'action':'set','index':2},cookie=self.f.admin)
        self.act('skip',qid='M2')
        done, = [r for r in self.act('send',qid='M3')['runs'] if r['qid']=='M3']
        self.act('close',id=done['id'])
        before = self.f.get(self.desk,cookie=self.f.admin)[0]
        args = {'qid':'M1','time':'10:13','shift_following':True}
        preview = self.act('retime_preview',**args)
        self.assertEqual(preview['rev'],before['rev'])
        self.assertTrue(preview['agenda_unchanged'])
        self.assertEqual([change['qid'] for change in preview['changes']],['M1','M0','M5','M4','P03'])
        self.assertEqual(preview['changes'][1]['after'],{'phase':'mac','time':'10:17','offset_minutes':7})
        self.assertEqual(self.f.get(self.desk,cookie=self.f.admin)[0]['activity'],before['activity'])
        self.act('retime_preview',status=409,qid='M1',time='23:55',shift_following=True)
        self.act('retime_preview',status=409,qid='PQ1',time='09:49',shift_following=False)
        self.act('retime_preview',status=409,qid='R1a',time='12:45',shift_following=False,phase='break')
        self.act('edit',qid='W2',changes={'cue':'Synthetic change'})
        self.f.post(self.desk,{'action':'retime','rev':preview['rev'],**args},cookie=self.f.admin,status=409)
        after = self.act('retime',**args)
        self.assertEqual([r['id'] for r in after['runs']],[r['id'] for r in before['runs']])
        cues = {q['id']:q['time'] for q in after['bank']}
        self.assertEqual((cues['M1'],cues['M0'],cues['M2'],cues['M3'],cues['PQ1']),('10:13','10:17','10:17','10:18','09:48'))

    def test_scored_resend_after_release_is_unscored_and_preserves_the_original(self):
        import server as s
        import event_quiz
        from unittest.mock import patch
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.f.post('/api/desk/program',{'action':'set','index':1},cookie=self.f.admin)
        with patch.object(event_quiz, 'now', return_value=1000):
            desk = self.act('send',qid='W2',seconds=15)
            run = next(r for r in desk['runs'] if r['qid']=='W2')
            self.assertEqual(run['end']-run['start'],15)
            self.assertIsNone(desk['hall'])
            self.assertEqual(self.act('show',id=run['id'])['hall']['id'],run['id'])
            closed, = self.act('close',id=run['id'])['runs']
            missed, = self.read()['history']
            self.assertEqual((missed['selected'],missed['correct']),(None,['B']))
            self.assertTrue(closed['can_resend'])
            self.f.post(self.quiz,{'action':'answer','id':run['id'],'key':'B'},cookie=self.player,status=409)
            hall = event_quiz.hall(s)
            self.assertEqual(hall['status'],'closed')
            self.assertNotIn('bars',hall)
        with patch.object(event_quiz, 'now', return_value=1016):
            self.assertNotIn('bars',event_quiz.hall(s))
            self.act('show',id=run['id'],results=True)
            self.assertIn('bars',event_quiz.hall(s))
            self.assertEqual(self.read()['questions'],[])
            replay = self.act('resend',id=run['id'],seconds=25)['runs'][-1]
            self.assertEqual((replay['points'], replay['end']), (0,1041))
            self.assertNotEqual(run['id'], replay['id'])

    def test_unscored_pulse_can_be_resent_after_closing(self):
        self.act('settings',enabled=True)
        run, = self.act('send',qid='P01')['runs']
        self.act('close',id=run['id'])
        reopened, = self.act('resend',id=run['id'],seconds=25)['runs']
        self.assertEqual((reopened['id'],reopened['status'],reopened['points']),(run['id'],'open',0))
        self.assertEqual(self.read()['questions'][0]['id'],run['id'])

    def test_explicit_hall_show_and_resend_follow_readiness(self):
        self.act('settings',enabled=True)
        self.f.post('/api/desk/program',{'action':'set','index':1},cookie=self.f.admin)
        run, = self.act('send',qid='W2')['runs']
        self.assertTrue(run['can_show'])
        self.assertTrue(run['can_resend'])
        self.act('close',id=run['id'])
        self.act('hide')
        self.f.post('/api/desk/program',{'action':'set','index':6},cookie=self.f.admin)
        desk = self.f.get(self.desk,cookie=self.f.admin)[0]
        blocked, = desk['runs']
        self.assertFalse(desk['operator']['can_show_hall'])
        self.assertFalse(blocked['can_show'])
        self.assertTrue(blocked['can_resend'])
        for action in ('show',):
            refusal = self.act(action,status=409,id=run['id'])
            self.assertEqual(refusal['error'],blocked[action+'_blocked_reason'])
        self.assertIsNone(self.f.get(self.desk,cookie=self.f.admin)[0]['hall'])

    def test_awards_with_no_published_final_winner_keeps_quiz_controls_available(self):
        import event_quiz
        self.act('settings',enabled=True)
        s = workspace.S
        s.kv_set('final',{'pairs':[1,2,3],'status':'completed','run_id':'synthetic-final','published_rows':[]})
        state = event_quiz.load(s)
        for qid, prediction in [('F1',True),('F2',False)]:
            q = event_quiz.bank()[qid] | {'seconds':15}
            context = event_quiz.operation_context(s,'final',s.program_now())
            context['final']['status'] = 'ready' if prediction else 'completed'
            run = event_quiz.build_run(s,state,q,'vote',0,context)
            run.update(status='closed',released=False)
        s.kv_set(event_quiz.KEY,state)
        self.f.post('/api/desk/program',{'action':'set','index':9},cookie=self.f.admin)
        desk = self.f.get(self.desk,cookie=self.f.admin)[0]
        self.assertFalse(desk['operator']['can_predictions'])
        self.assertEqual(self.act('predictions',status=409)['error'],desk['operator']['predictions_blocked_reason'])
        with unittest.mock.patch.object(event_quiz,'quiet',return_value=True):
            desk = self.f.get(self.desk,cookie=self.f.admin)[0]
            self.assertFalse(desk['operator']['can_people_choice'])
            self.assertEqual(self.act('people_choice',status=409)['error'],desk['operator']['people_choice_blocked_reason'])
        self.assertTrue(self.act('people_choice')['hall_visible'])

    def test_resend_before_release_reaches_only_missing_responses(self):
        import event_quiz
        from unittest.mock import patch
        self.f.post('/api/desk/roster',{'rows':[{'pair':1,'partner':'Example Company','p1':'Test Person','p2':'Second Person'}]},cookie=self.f.admin)
        second = self.f.login(self.f.issue('participant',pair=1,member=2))
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.f.post(self.quiz,{'action':'join'},cookie=second)
        self.f.post('/api/desk/program',{'action':'set','index':1},cookie=self.f.admin)
        with patch.object(event_quiz,'now',return_value=2000):
            run = next(r for r in self.act('send',qid='W2')['runs'] if r['qid']=='W2')
            self.f.post(self.quiz,{'action':'answer','id':run['id'],'key':'B'},cookie=self.player)
        with patch.object(event_quiz,'now',return_value=2005):
            resent = self.act('resend',id=run['id'],seconds=25)['runs'][-1]
            self.assertEqual(resent['end'],2030)
            self.assertEqual(self.read()['questions'],[])
            offered = self.f.get(self.quiz,cookie=second)[0]['questions']
            self.assertEqual([q['id'] for q in offered],[run['id']])
            self.f.post(self.quiz,{'action':'answer','id':run['id'],'key':'A'},cookie=second)
            self.assertEqual(self.f.get(self.desk,cookie=self.f.admin)[0]['runs'][-1]['responses'],2)

    def test_final_prediction_resend_is_limited_to_the_pre_pitch_window(self):
        s = workspace.S
        final = {'pairs':[2,3,4],'status':'ready','run_id':'synthetic-final'}
        s.kv_set('final',final)
        self.act('settings',enabled=True)
        self.f.post('/api/desk/program',{'action':'set','index':8},cookie=self.f.admin)
        run, = self.act('send',qid='F1')['runs']
        self.act('close',id=run['id'])
        self.assertEqual(self.act('resend',id=run['id'])['runs'][0]['status'],'open')
        self.act('close',id=run['id'])
        for status in ('collecting','review','completed'):
            with self.subTest(status=status):
                s.kv_set('final',final | {'status':status,'published_rows':[{'pair':2,'rank':1}]})
                current, = self.f.get(self.desk,cookie=self.f.admin)[0]['runs']
                self.assertFalse(current['can_resend'])
                refusal = self.act('resend',status=409,id=run['id'])
                self.assertEqual(refusal['error'],current['resend_blocked_reason'])

    def test_releasing_waiting_answers_preserves_resolved_scenario_prediction(self):
        from unittest.mock import patch

        s = workspace.S
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.f.post('/api/desk/program',{'action':'set','index':6},cookie=self.f.admin)
        for qid in ('R1a','R3b'):
            run = self.act('send',qid=qid)['runs'][-1]
            self.f.post(self.quiz,{'action':'answer','id':run['id'],'key':(run['keys'] or ['A'])[0]},cookie=self.player)
            self.act('close',id=run['id'])
        self.f.post('/api/desk/program',{'action':'set','index':9},cookie=self.f.admin)
        s.kv_set('reveal','scores')
        with patch.object(s,'leaderboard',return_value=[{'scenario':run['options'][0]['scenario'],'total':1}]):
            waiting,prediction = self.act('predictions')['runs']
        self.assertFalse(waiting['released'])
        self.assertTrue(prediction['released'])
        waiting,prediction = self.act('close_waiting')['runs']
        self.assertTrue(waiting['released'])
        self.assertTrue(prediction['released'])
        result = next(row for row in self.read()['history'] if row['id']==run['id'])
        self.assertEqual((result['result_state'],result['correct']),('ready',['A']))
        self.assertTrue(self.act('champion')['release'])

    def test_peoples_choice_resend_stops_after_awards_release(self):
        s = workspace.S
        s.kv_set('final',{'pairs':[2,3,4],'status':'review','run_id':'synthetic-final'})
        self.act('settings',enabled=True)
        self.f.post('/api/desk/program',{'action':'set','index':8},cookie=self.f.admin)
        run, = self.act('send',qid='F2')['runs']
        self.act('close',id=run['id'])
        self.assertEqual(self.act('resend',id=run['id'])['runs'][0]['status'],'open')
        self.act('close',id=run['id'])
        self.f.post('/api/desk/program',{'action':'set','index':9},cookie=self.f.admin)
        self.act('people_choice')
        self.f.post('/api/desk/program',{'action':'set','index':8},cookie=self.f.admin)
        self.assertFalse(self.f.get(self.desk,cookie=self.f.admin)[0]['runs'][0]['can_resend'])
        self.act('resend',status=409,id=run['id'])

    def test_champion_waits_for_final_prediction_and_peoples_choice(self):
        s = workspace.S
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.f.post('/api/desk/program',{'action':'set','index':1},cookie=self.f.admin)
        scored, = self.act('send',qid='W2')['runs']
        self.f.post(self.quiz,{'action':'answer','id':scored['id'],'key':'B'},cookie=self.player)
        self.act('close',id=scored['id'])
        self.f.post('/api/desk/program',{'action':'set','index':8},cookie=self.f.admin)
        final = {'pairs':[2,3,4],'status':'ready','run_id':'synthetic-final'}
        for qid,status in (('F1','ready'),('F2','review')):
            s.kv_set('final',final | {'status':status})
            run = self.act('send',qid=qid)['runs'][-1]
            self.f.post(self.quiz,{'action':'answer','id':run['id'],'key':'A'},cookie=self.player)
            self.act('close',id=run['id'])
        s.kv_set('final',final | {'status':'completed','published_rows':[{'pair':2,'rank':1}]})
        self.f.post('/api/desk/program',{'action':'set','index':9},cookie=self.f.admin)
        for reveal in ('predictions','people_choice'):
            operator = self.f.get(self.desk,cookie=self.f.admin)[0]['operator']
            self.assertFalse(operator['can_champion'])
            self.assertEqual(self.act('champion',status=409)['error'],operator['champion_blocked_reason'])
            self.act(reveal)
        self.assertTrue(self.act('champion')['release'])

    def test_participant_tracks_own_saved_answer_until_release_and_cancellation(self):
        self.f.post('/api/desk/roster',{'rows':[{'pair':1,'partner':'Example Company','p1':'Test Person','p2':'Other Person'}]},cookie=self.f.admin)
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.f.post('/api/desk/program',{'action':'set','index':1},cookie=self.f.admin)
        run, = self.act('send',qid='W2')['runs']
        saved = self.f.post(self.quiz,{'action':'answer','id':run['id'],'key':'B'},cookie=self.player)[0]
        own, = saved['history']
        self.assertEqual((own['selected'],own['result_state'],own['earned']),('B','awaiting_close',None))
        self.assertNotIn('correct',own)
        self.assertNotIn('explanation',own)
        other = self.f.login(self.f.issue('participant',pair=1,member=2))
        self.f.post(self.quiz,{'action':'join'},cookie=other)
        self.assertEqual(self.f.get(self.quiz,cookie=other)[0]['history'],[])
        self.act('close',id=run['id'])
        ready, = self.read()['history']
        self.assertEqual((ready['result_state'],ready['correct'],ready['earned']),('ready',['B'],1))
        missed, = self.f.get(self.quiz,cookie=other)[0]['history']
        self.assertIsNone(missed['selected'])
        self.assertEqual(missed['earned'],0)
        self.act('void',id=run['id'])
        cancelled, = self.read()['history']
        self.assertEqual(cancelled['result_state'],'cancelled')
        self.assertIsNone(cancelled['earned'])
        self.assertNotIn('correct',cancelled)
        self.assertEqual(self.read()['score']['earned'],0)
        self.assertEqual(self.f.get(self.quiz,cookie=other)[0]['history'],[])

    def test_practice_results_wait_for_released_post_test(self):
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.act('warmup',minutes=5)
        run = self.read()['questions'][0]
        self.f.post(self.quiz,{'action':'answer','id':run['id'],'key':'A'},cookie=self.player)
        pending, = self.read()['history']
        self.assertEqual(pending['result_state'],'awaiting_post_test')
        self.assertNotIn('correct',pending)
        self.act('close_warmup')
        self.assertEqual(self.read()['history'][0]['result_state'],'awaiting_post_test')
        self.f.post('/api/desk/program',{'action':'set','index':7},cookie=self.f.admin)
        self.act('post',minutes=5)
        post = self.read()['questions'][0]
        self.f.post(self.quiz,{'action':'answer','id':post['id'],'key':'A'},cookie=self.player)
        self.assertEqual(next(q for q in self.read()['history'] if q['id']==post['id'])['result_state'],'awaiting_release')
        self.act('close_post')
        ready = next(q for q in self.read()['history'] if q['id']==run['id'])
        self.assertEqual((ready['result_state'],ready['points'],ready['earned']),('ready',0,0))
        self.assertIn('correct',ready)

    def test_early_post_release_finishes_unexpired_practice(self):
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.act('warmup',minutes=15)
        run = self.read()['questions'][0]
        self.f.post(self.quiz,{'action':'answer','id':run['id'],'key':'A'},cookie=self.player)
        self.f.post('/api/desk/program',{'action':'set','index':7},cookie=self.f.admin)
        self.act('post',minutes=5)
        self.act('close_post')
        ready = next(q for q in self.read()['history'] if q['id']==run['id'])
        self.assertEqual((ready['status'],ready['result_state'],ready['earned']),('closed','ready',0))
        self.assertIn('correct',ready)

    def test_individual_quiz_send_and_resend_default_to_sixty_seconds(self):
        import event_quiz
        from unittest.mock import patch
        self.act('settings', enabled=True)
        with patch.object(event_quiz, 'now', return_value=1000):
            run, = self.act('send', qid='W2')['runs']
            self.assertEqual((run['seconds'], run['end']), (60, 1060))
        with patch.object(event_quiz, 'now', return_value=1020):
            resent, = self.act('resend', id=run['id'])['runs']
            self.assertEqual((resent['seconds'], resent['end']), (60, 1080))

    def test_manual_question_stays_open_across_agenda_sections_until_closed(self):
        import event_quiz
        from unittest.mock import patch
        self.act('settings', enabled=True)
        self.f.post(self.quiz, {'action':'join'}, cookie=self.player)
        with patch.object(event_quiz, 'now', return_value=1000):
            run, = self.act('send', qid='W2', seconds=None)['runs']
            self.assertIsNone(run['end'])
        self.f.post('/api/desk/program', {'action':'set','index':2}, cookie=self.f.admin)
        with patch.object(event_quiz, 'now', return_value=10000):
            prompt, = self.read()['questions']
            self.assertEqual(prompt['id'], run['id'])
            self.f.post(self.quiz, {'action':'answer','id':run['id'],'key':'B'}, cookie=self.player)
            closed, = self.act('close', id=run['id'])['runs']
            self.assertEqual(closed['end'], 10000)
            replay = self.act('resend', id=run['id'], seconds=None)['runs'][-1]
            self.assertEqual((replay['points'], replay['status'], replay['end']), (0,'open',None))
            self.assertNotEqual(replay['id'], run['id'])
            self.assertEqual(self.read()['questions'][0]['id'], replay['id'])
            self.assertEqual(self.read()['score']['earned'], 1)

    def test_manual_release_and_off_switch(self):
        self.assertEqual(self.read(),{'enabled':False})
        self.f.get(self.desk,cookie=self.player,status=403)
        self.f.get(self.quiz,headers=self.f.judge(),status=403)
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.f.post('/api/desk/program',{'action':'set','index':1},cookie=self.f.admin)
        self.assertEqual(self.read()['questions'],[])
        self.act('send',qid='W2')
        item = self.read()['questions'][0]
        self.assertNotIn('correct',item)
        answer = {'action':'answer','id':item['id'],'key':'B'}
        self.f.post(self.quiz,answer,cookie=self.player)
        self.f.post(self.quiz,answer,cookie=self.player)
        self.f.post(self.quiz,dict(answer,key='A'),cookie=self.player,status=409)
        self.assertEqual(self.read()['score']['earned'],0)
        self.act('close',id=item['id'])
        self.assertEqual(self.read()['score']['earned'],1)
        self.act('settings',enabled=False)
        self.assertEqual(self.read(),{'enabled':False})
        self.act('settings',enabled=True)
        self.assertEqual(self.read()['score']['earned'],1)
        self.f.switch('rehearsal')
        self.assertFalse(self.f.get(self.desk,cookie=self.f.admin)[0]['enabled'])

    def test_organizer_gets_aggregate_results_and_question_export_only(self):
        organizer = self.f.login(self.f.issue('desk'))
        headers = {'X-Dashboard':'organizer'}
        data, _ = self.f.get(self.desk,cookie=organizer,headers=headers)
        self.assertTrue(data['individual_results_restricted'])
        self.assertEqual(data['standings'],[])
        self.assertNotIn('teams',data['analytics'])
        self.assertNotIn('companies',data['analytics'])
        self.assertIn('questions',data['analytics'])
        self.f.get(self.desk+'/people.csv',cookie=organizer,headers=headers,status=403)
        questions, _ = self.f.get(self.desk+'/questions.csv',cookie=organizer,headers=headers)
        self.assertTrue(questions.startswith(b'question,run,status,responses'))

    def test_champion_waits_for_expired_post_test_to_be_released(self):
        import event_quiz
        from unittest.mock import patch
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.f.post('/api/desk/program',{'action':'set','index':1},cookie=self.f.admin)
        question = next(r for r in self.act('send',qid='W2')['runs'] if r['qid']=='W2')
        self.f.post(self.quiz,{'action':'answer','id':question['id'],'key':'B'},cookie=self.player)
        self.act('close',id=question['id'])
        self.f.post('/api/desk/program',{'action':'set','index':7},cookie=self.f.admin)
        with patch.object(event_quiz,'now',return_value=3000):
            desk = self.act('post',minutes=1)
            question = self.read()['questions'][0]
            key = next(r['keys'][0] for r in desk['runs'] if r['id']==question['id'])
            self.f.post(self.quiz,{'action':'answer','id':question['id'],'key':key},cookie=self.player)
        self.f.post('/api/desk/program',{'action':'set','index':9},cookie=self.f.admin)
        with patch.object(event_quiz,'now',return_value=3061):
            self.assertEqual(self.read()['score']['earned'],1)
            refusal = self.act('champion',status=409)
            self.assertIn('Release or void',refusal['error'])
            self.assertIsNone(self.f.get(self.desk,cookie=self.f.admin)[0]['release'])
            self.act('close_post')
            self.assertEqual(self.act('champion')['release']['rows'][0]['earned'],1+question['points'])

    def test_confirmed_catchup_reaches_paused_team_and_stops_if_judging_resumes(self):
        self.assert_catchup_extension()

    def test_saved_catchup_without_run_marker_can_be_extended(self):
        self.assert_catchup_extension(legacy=True)

    def assert_catchup_extension(self, *, legacy=False):
        import event_quiz
        from unittest.mock import patch
        self.act('settings',enabled=True)
        self.f.post('/api/desk/roster',{'rows':[{'pair':1,'partner':'Example Company','p1':'Test Person'},
                     {'pair':2,'partner':'Other Company','p1':'Other Person'}]},cookie=self.f.admin)
        other = self.f.login(self.f.issue('participant',pair=2,member=1))
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.f.post(self.quiz,{'action':'join'},cookie=other)
        desk = lambda path, body: self.f.post('/api/desk/'+path,body,cookie=self.f.admin)
        desk('mode',{'mode':'self'})
        desk('program',{'action':'set','index':6})
        seat = self.f.get('/api/participant/state',cookie=self.player)[0]['participant']
        desk('clock',{'action':'goto','station':seat['station'],'round':seat['round']})
        judge = self.f.judge(station=seat['station'])
        def clock(action):
            self.f.post('/api/session',{'action':action},headers=judge)
        clock('start')
        run = next(r for r in self.act('send',qid='R1a')['runs'] if r['qid']=='R1a')
        self.assertTrue(self.read()['busy'])
        clock('pause')
        desk('program',{'action':'set','index':7})
        self.assertEqual(self.f.get('/api/participant/state',cookie=self.player)[0]['action']['code'],'paused')
        self.act('catchup',status=409,minutes=2)
        self.assertEqual(self.read()['questions'],[])
        with self.subTest(window='before confirmation'):
            self.assertEqual(self.f.get(self.quiz,cookie=other)[0]['questions'],[])
        with patch.object(event_quiz,'now',return_value=5000):
            self.act('catchup',minutes=1,confirm_missing=True)
            view = self.read()
            self.assertFalse(view['busy'])
            self.assertTrue(view['catchup_allowed'])
            self.assertEqual([q['id'] for q in view['questions']],[run['id']])
            if legacy:
                state = event_quiz.load(workspace.S)
                for stored in state['runs']:
                    stored.pop('catchup',None)
                workspace.S.kv_set(event_quiz.KEY,state)
                self.assertTrue(self.read()['catchup_allowed'])
            latest = self.act('extend',id=run['id'],seconds=15)
            self.assertEqual(next(r['end'] for r in latest['runs'] if r['id']==run['id']),5075)
        answer = {'action':'answer','id':run['id'],'key':run['keys'][0]}
        with patch.object(event_quiz,'now',return_value=5061):
            with self.subTest(window='during extension'):
                self.assertTrue(self.read()['catchup_allowed'])
                self.assertEqual([q['id'] for q in self.read()['questions']],[run['id']])
            self.assertEqual([q['id'] for q in self.f.get(self.quiz,cookie=other)[0]['questions']],[run['id']])
            clock('start')
            self.assertEqual(self.read()['questions'],[])
            self.assertFalse(self.read()['catchup_allowed'])
            self.f.post(self.quiz,answer,cookie=self.player,status=409)
            other_view = self.f.get(self.quiz,cookie=other)[0]
            self.assertFalse(other_view['busy'])
            with self.subTest(window='judging resumes during extension'):
                self.assertEqual(other_view['questions'],[])
                self.f.post(self.quiz,answer,cookie=other,status=409)
            clock('pause')
            self.f.post(self.quiz,answer,cookie=self.player)
        with patch.object(event_quiz,'now',return_value=5076):
            self.assertFalse(self.read()['catchup_allowed'])
            self.assertTrue(self.read()['busy'])
            self.assertEqual(self.f.get(self.quiz,cookie=other)[0]['questions'],[])
            self.f.post(self.quiz,answer,cookie=other,status=409)

    def test_published_question_preserves_original_revision(self):
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.f.post('/api/desk/program',{'action':'set','index':1},cookie=self.f.admin)
        self.act('send',qid='W2')
        original = self.read()['questions'][0]
        self.act('edit',qid='W2',changes={'prompt':'Revised station timer'})
        self.assertEqual(self.read()['questions'][0]['prompt'],original['prompt'])
        self.act('void',id=original['id'])
        self.assertEqual(self.read()['score']['available'],0)
        self.act('send',qid='W2')
        self.assertEqual(self.read()['questions'][0]['prompt'],'Revised station timer')

    def test_participant_gets_only_next_released_question(self):
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        desk = self.act('warmup',minutes=5)
        first, second, *others = [run for run in desk['runs'] if run['kind']=='warmup']
        view = self.read()
        self.assertEqual([run['id'] for run in view['questions']],[first['id']])
        self.assertNotIn(second['prompt'],str(view))
        self.assertNotIn('correct',view['questions'][0])
        self.assertEqual(view['questions'][0]['progress'],{'position':1,'total':5})
        self.f.post(self.quiz,{'action':'answer','id':second['id'],'key':'A'},cookie=self.player,status=409)
        self.f.post(self.quiz,{'action':'answer','id':first['id'],'key':'A'},cookie=self.player)
        next_view = self.read()
        self.assertEqual([run['id'] for run in next_view['questions']],[second['id']])
        self.assertEqual(next_view['questions'][0]['progress'],{'position':2,'total':5})
        self.assertNotIn(others[0]['prompt'],str(next_view))
        self.act('close_warmup')
        self.f.post('/api/desk/program',{'action':'set','index':1},cookie=self.f.admin)
        self.assertEqual(self.read()['questions'],[])
        self.assertNotIn('How many minutes',str(self.read()))
        self.act('send',qid='W2')
        self.assertEqual([run['qid'] for run in self.read()['questions']],['W2'])

    def test_analytics_reports_answers_and_desk_actions_only_to_staff(self):
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        self.f.post('/api/desk/program',{'action':'set','index':1},cookie=self.f.admin)
        self.act('skip',qid='W1')
        live = self.act('send',qid='W2')
        run = next(row for row in live['runs'] if row['qid']=='W2')
        self.f.post(self.quiz,{'action':'answer','id':run['id'],'key':'B'},cookie=self.player)
        result = self.act('close',id=run['id'])
        self.assertEqual(result['analytics']['summary'],{'joined':1,'viewers':1,'respondents':1,'answers':1,'released':1,'skipped':1,'voided':0})
        self.assertEqual(result['analytics']['questions'][run['id']]['correctPercent'],100)
        self.assertEqual(result['analytics']['questions'][run['id']]['completion'],100)
        self.assertEqual([row['action'] for row in result['activity'][:4]],['close','send','skip','settings'])
        self.assertEqual(result['activity'][3]['detail'],'Enabled')
        self.assertNotIn('analytics', self.read())
        self.assertNotIn('activity', self.read())

    def test_analytics_distinguishes_viewing_from_answering(self):
        self.act('settings',enabled=True)
        self.f.post(self.quiz,{'action':'join'},cookie=self.player)
        desk = self.act('warmup',minutes=5)
        first, second, *_ = [run for run in desk['runs'] if run['kind']=='warmup']
        self.f.post(self.quiz,{'action':'view','id':second['id']},cookie=self.player,status=409)
        for _ in range(2):
            self.f.post(self.quiz,{'action':'view','id':first['id']},cookie=self.player)
        data, _ = self.f.get('/api/desk/quiz',cookie=self.f.admin)
        self.assertEqual(data['analytics']['summary']['viewers'],1)
        self.assertEqual(data['analytics']['summary']['respondents'],0)
        self.assertEqual(data['analytics']['questions'][first['id']]['viewers'],1)
        self.assertEqual(data['analytics']['questions'][first['id']]['completion'],0)
        self.assertEqual(data['analytics']['questions'][second['id']]['viewers'],0)
