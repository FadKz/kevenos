"""A compact prior snapshot preserves notification transitions and recipients."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import event_push

class QuizPushSnapshot(unittest.TestCase):
    def setUp(self):
        self.s=SimpleNamespace(time=SimpleNamespace(time=lambda:10))
        self.run=dict(id='q',kind='event',status='open',released=False,resends=0,
                      end=100,points=1,manual_delivery=True,excluded=[2])
        self.state=dict(enabled=True,runs=[self.run],release=False,
                        members={'a':{'joined':True}},answers={'a':{'q':{'key':'B'}}})
    def effects(self,before,after):
        calls=[]
        def enqueue(_s,kind,**kw):
            recipients=kw.get('recipients')
            calls.append((kind,kw.get('ttl',300),kw.get('stored'),
                          None if recipients is None else [bool(recipients(k,p)) for k,p in [('a',1),('b',2)]]))
        with patch.object(event_push,'enqueue',side_effect=enqueue):
            result=event_push.quiz_changed(self.s,before,after)
        return result,calls
    def test_snapshot_matches_full_prior_state_for_every_transition(self):
        cases=[]
        after=deepcopy(self.state);after['answers']['b']={'q':{'key':'C'}}
        cases.append(('answer',self.state,after,[]))
        before=deepcopy(self.state);before['runs']=[]
        after=deepcopy(self.state);after['answers']={}
        cases.append(('new question',before,after,['question']))
        after=deepcopy(self.state);after['runs'][0]['resends']=1
        cases.append(('resend',self.state,after,['question']))
        after=deepcopy(self.state);after['runs'][0].update(status='closed',released=True)
        cases.append(('timeout release',self.state,after,['results']))
        before=deepcopy(self.state);before['runs'][0].update(kind='warmup',status='closed',released=True)
        before['runs'].append(dict(self.run,id='post',kind='post',status='open',released=True))
        after=deepcopy(before);after['runs'][1]['status']='closed'
        cases.append(('warmup after post closes',before,after,['results']))
        after=deepcopy(self.state);after['release']=True
        cases.append(('publish',self.state,after,['results']))
        after=deepcopy(self.state);after['enabled']=False
        cases.append(('disabled',self.state,after,[]))
        for name,before,after,expected in cases:
            with self.subTest(name=name):
                full=self.effects(before,after)
                compact=self.effects(event_push.quiz_transition_snapshot(before),after)
                self.assertEqual(full,compact)
                self.assertEqual([row[0] for row in compact[1]],expected)
                if name=='new question':self.assertEqual(compact[1][0][3],[True,False])
    def test_snapshot_is_detached_and_excludes_answer_history(self):
        snapshot=event_push.quiz_transition_snapshot(self.state)
        self.run['status']='closed'
        self.state['release']=True
        self.assertEqual(snapshot['runs'][0]['status'],'open')
        self.assertFalse(snapshot['release'])
        self.assertEqual(set(snapshot),{'runs','release'})
        self.assertNotIn('answers',snapshot)
