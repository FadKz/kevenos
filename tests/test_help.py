"""Role help must be private, role-derived and read-only."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import test_event_access as A
S=A.S

class RoleHelp(unittest.TestCase):
    def setUp(self):
        self.fixture=A.EventAccess('test_guest_cannot_manage_or_read_private_content')
        self.fixture.setUp();self.addCleanup(self.fixture.doCleanups)
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        scope=patch.object(S,'HERE',self.temp.name);scope.start();self.addCleanup(scope.stop)
        self.resources=[{'id':'judge-app-guide','file':'app.pdf','audience':['judge','admin']},
            {'id':'judge-pack-s1','file':'s1.pdf','audience':['judge','admin'],'scenario':'S1'},
            {'id':'judge-pack-s2','file':'s2.pdf','audience':['judge','admin'],'scenario':'S2'}]
        (self.root/'event-guide.json').write_text(json.dumps({'resources':self.resources}))
        roles={role:{'title':role+' help','summary':'Short guidance','steps':[{'title':'First','body':'Do this'}],
            'sections':[],'downloads':[{'id':r['id'],'label':r['id']} for r in self.resources]+[{'id':'unregistered','url':'https://invalid.example','label':'Bad'}]} for role in ('admin','desk','judge','participant','guest')}
        (self.root/'usage-guides.json').write_text(json.dumps({'version':'test','roles':roles}))
        (self.root/'resources').mkdir()
        for r in self.resources:(self.root/'resources'/r['file']).write_bytes(b'%PDF-1.4\nfixture')
        with S.DB:S.DB.execute("INSERT INTO roster(pair,partner,p1,p2) VALUES(1,'Example','Alex','Sam')")

    def test_guide_role_comes_from_session_and_filters_downloads(self):
        self.assertEqual(A.request('GET','/api/help')[0],401)
        cookies={'admin':self.fixture.admin,'guest':self.fixture.login(S.CFG['guest_password'])}
        for role in ('desk','participant'):
            fields={'pair':1,'member':1} if role=='participant' else {}
            item=self.fixture.create(role,**fields);cookies[role]=self.fixture.login(item['password'])
        for role,cookie in cookies.items():
            status,guide,_=A.request('GET','/api/help?role=judge',cookie=cookie)
            self.assertEqual(status,200)
            self.assertEqual(guide['role'],role)
            self.assertEqual(guide['title'],role+' help')
            self.assertEqual(len(guide['steps']),1)
            self.assertNotIn('roles',guide)
            self.assertEqual([x['id'] for x in guide['downloads']],[x['id'] for x in self.resources] if role=='admin' else [])
            if role!='admin':self.assertEqual(A.request('GET','/api/resources/judge-app-guide',cookie=cookie)[0],403)
        before='\n'.join(S.DB.iterdump())
        self.assertEqual(A.request('GET','/api/help',cookie=self.fixture.admin)[0],200)
        self.assertEqual('\n'.join(S.DB.iterdump()),before)

    def test_judge_gets_only_authorized_materials_and_no_state_writes(self):
        issued = self.fixture.create('judge', station=1)
        cookie = self.fixture.login(issued['password'])
        before='\n'.join(S.DB.iterdump())
        status,guide,_=A.request('GET','/api/help?role=admin',cookie=cookie)
        self.assertEqual(status,200)
        self.assertEqual(guide['role'],'judge')
        self.assertEqual([x['id'] for x in guide['downloads']],['judge-app-guide','judge-pack-s1'])
        self.assertTrue(all(x['url']=='/api/resources/'+x['id'] for x in guide['downloads']))
        self.assertEqual('\n'.join(S.DB.iterdump()),before)
        self.assertEqual(A.request('GET','/api/resources/judge-app-guide',cookie=cookie)[0],200)
        self.assertEqual(A.request('GET','/api/resources/judge-pack-s2',cookie=cookie)[0],403)

    def test_unavailable_help_is_retryable_and_does_not_expose_internal_errors(self):
        (self.root/'usage-guides.json').write_text('{')
        status,body,_=A.request('GET','/api/help',cookie=self.fixture.admin)
        self.assertEqual(status,503)
        self.assertEqual(set(body),{'error'})
        self.assertNotIn(self.temp.name,body['error'])

    def test_admin_operating_desk_gets_desk_context_without_granting_role_override(self):
        status,guide,_=A.request('GET','/api/help?context=desk',cookie=self.fixture.admin)
        self.assertEqual(status,200)
        self.assertEqual(guide['role'],'admin')
        self.assertEqual(guide['context'],'desk')
        self.assertEqual(guide['title'],'desk help')
        guest=self.fixture.login(S.CFG['guest_password'])
        self.assertEqual(A.request('GET','/api/help?context=desk',cookie=guest)[0],403)
        self.assertEqual(A.request('GET','/api/help?context=judge',cookie=guest)[0],403)
        self.assertEqual(A.request('GET','/api/help?context=judge',cookie=self.fixture.admin)[0],403)

if __name__=='__main__':unittest.main()
