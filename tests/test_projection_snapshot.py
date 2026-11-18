"""Programme snapshots must not mix revisions during a concurrent Desk change."""
import threading
import unittest
from unittest.mock import patch
import test_workspace_routes as fixture
S=fixture.S

class ProjectionSnapshot(unittest.TestCase):
    setUp=fixture.WorkspaceRoutes.setUp
    request=fixture.WorkspaceRoutes.request
    post=fixture.WorkspaceRoutes.post
    get=fixture.WorkspaceRoutes.get
    login=fixture.WorkspaceRoutes.login
    write_roster=fixture.WorkspaceRoutes.write_roster

    def assert_snapshot(self, project, index):
        S.kv_set('program',{'i':0,'mode':'manual','scene':'auto','since':S.time.time()})
        entered=threading.Event();done=threading.Event()
        original=S.current_room
        writer=None
        def room(*args,**kwargs):
            nonlocal writer
            def change():
                entered.set()
                S.kv_set('program',{'i':1,'mode':'manual','scene':'auto','since':S.time.time()})
                done.set()
            writer=threading.Thread(target=change)
            writer.start(); self.assertTrue(entered.wait(1))
            done.wait(.1)
            return original(*args,**kwargs)
        try:
            with patch.object(S,'current_room',room): result=project()
        finally:
            if writer: writer.join(2)
        self.assertTrue(done.is_set())
        self.assertEqual(result['room']['title'],S.agenda()[result['program'][index]]['title'])

    def test_public_snapshot_does_not_mix_programme_revisions(self):
        self.assert_snapshot(S.public_state,'index')

    def test_desk_snapshot_does_not_mix_programme_revisions(self):
        self.assert_snapshot(S.desk_state,'i')

    def test_desk_external_probe_does_not_acquire_new_event_lock(self):
        observations=[]
        with patch.object(S,'gateway_ready',side_effect=lambda:observations.append(S.LOCK._is_owned()) or False):
            S.desk_state()
        self.assertEqual(observations,[False])

if __name__=='__main__':unittest.main()
