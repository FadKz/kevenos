"""Organizer exports contain aggregate metrics and spreadsheet-safe text only."""
import csv
import io
import unittest
from unittest.mock import patch
import engagement_catalog as catalog
import test_workspace_routes as workspace


class Export(unittest.TestCase):
    def test_export_is_scoped_aggregate_and_safe_for_spreadsheets(self):
        fixture = workspace.WorkspaceRoutes('test_event_configuration_is_admin_only_and_context_bound')
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        service = workspace.S
        identity = {'role':'organizer','credential_id':'operator'}
        payload = {'title':'=HYPERLINK("unsafe")','kind':'poll','questions':[
            {'type':'single','prompt':'+formula','options':['@option','Safe']}]}
        draft = catalog.execute(service, 'save', payload, expected_revision=0, request_id='1'*32, identity=identity)
        catalog.execute(service, 'publish', {'activity_id':draft['id']}, expected_revision=1, request_id='2'*32, identity=identity)
        run = catalog.execute(service, 'open_run', {'activity_id':draft['id'],'version':1}, expected_revision=2, request_id='3'*32, identity=identity)
        run = catalog.execute(service, 'open_question', {'run_id':run['id'],'question_id':run['snapshot']['questions'][0]['id'],'duration':None},
                              expected_revision=3, request_id='4'*32, identity=identity)
        for i in range(5):
            catalog.answer(service, run['id'], run['delivery']['id'], 0,
                           identity={'role':'participant','credential_id':f'private-{i}'})
        participant = fixture.login(fixture.issue('participant', pair=1, member=1))
        with patch.dict('os.environ', {'KEVENOS_ENGAGEMENT_V2':'1'}):
            path = '/api/desk/engagement/analytics.csv'
            fixture.get(path, cookie=participant, status=403)
            raw, headers = fixture.get(path, cookie=fixture.admin)
            self.assertIn('attachment', headers['content-disposition'][0])
            rows = list(csv.DictReader(io.StringIO(raw.decode('utf-8-sig'))))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]['activity'], "'=HYPERLINK(\"unsafe\")")
            self.assertEqual(rows[0]['question'], "'+formula")
            self.assertEqual(rows[0]['option_counts'], "'@option: 5; Safe: 0")
            self.assertEqual(rows[0]['views'], 'not tracked')
            self.assertNotIn('participant', raw.decode('utf-8').lower())
