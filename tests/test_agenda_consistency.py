"""Agenda edits cannot be lost to a stale editor or an old programme timer."""
from copy import deepcopy
import time
import unittest
from unittest.mock import patch
import test_workspace_routes as W
import event_commands as E
S=W.S

class AgendaConsistency(unittest.TestCase):
    setUp=W.WorkspaceRoutes.setUp
    request=W.WorkspaceRoutes.request
    post=W.WorkspaceRoutes.post
    get=W.WorkspaceRoutes.get
    login=W.WorkspaceRoutes.login
    write_roster=W.WorkspaceRoutes.write_roster

    def test_stale_editor_cannot_overwrite_an_agent_edit(self):
        editor,_=self.get('/api/desk/content',cookie=self.admin)
        old=deepcopy(editor['content']);version=editor['versions']['content']
        item=E.read(S,'schedule.items',{}, {})['items'][0]
        E.execute(S,'schedule.update_item',{'id':item['id'],'changes':{'line':'Synthetic latest programme note'}},{'id':'synthetic'})
        for payload in ({'content':old}, {'content':old,'expected_content_version':version}, {'action':'bundled','expected_content_version':version}, {'action':'bundled'}):
            with self.subTest(payload_keys=list(payload)):
                value,_=self.post('/api/desk/content',payload,cookie=self.admin,status=409)
                self.assertEqual(value['code'],'content_changed')
                self.assertEqual(S.CONTENT['agenda'][0]['line'],'Synthetic latest programme note')

    def test_current_editor_can_save_or_restore_old_text_as_new_version(self):
        editor,_=self.get('/api/desk/content',cookie=self.admin)
        version=editor['versions']['content'];old=deepcopy(editor['content'])
        fresh=deepcopy(old);fresh['agenda'][0]['line']='Synthetic edit'
        result,_=self.post('/api/desk/content',{'content':fresh,'expected_content_version':version},cookie=self.admin)
        self.assertEqual(result['version'],version+1)
        result,_=self.post('/api/desk/content',{'content':old,'expected_content_version':version+1},cookie=self.admin)
        self.assertEqual(result['version'],version+2)
        self.post('/api/desk/content',{'action':'bundled','expected_content_version':True},cookie=self.admin,status=409)
        result,_=self.post('/api/desk/content',{'action':'bundled','expected_content_version':version+2},cookie=self.admin)
        self.assertEqual(result['version'],version+3)

    def test_clock_program_uses_selected_scheduled_start_without_writing(self):
        S.CONTENT['agenda']=[dict(S.CONTENT['agenda'][0],time='09:30',min=30),dict(S.CONTENT['agenda'][1],time='10:00',min=30)]
        now=time.mktime((2026,11,13,10,7,0,0,0,-1))
        saved={'i':0,'mode':'clock','since':now-7200,'announce':''}
        S.kv_set('program',saved)
        with patch.object(S.time,'time',return_value=now):
            result=S.program_now()
            self.assertEqual(result['i'],1)
            self.assertEqual(now-result['since'],7*60)
            self.assertEqual(S.kv_get('program'),saved)
            S.CONTENT['agenda'][1]['time']='10:05'
            self.assertEqual(now-S.program_now()['since'],2*60)
        S.kv_set('program',dict(saved,mode='manual'))
        with patch.object(S.time,'time',return_value=now):
            result=S.program_now()
            self.assertEqual(result['since'],saved['since'])
            self.assertEqual(result['i'],0)

if __name__=='__main__':unittest.main()
