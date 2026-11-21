"""The agent guide must describe the interface the code actually provides."""
import argparse
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import agent_api
import agent_mcp

ROOT = Path(__file__).resolve().parents[1]
GUIDE = (ROOT / 'docs' / 'AGENT-GUIDE.md').read_text(encoding='utf-8')
SOURCES = '\n'.join(path.read_text(encoding='utf-8') for path in ROOT.glob('*.py'))


class AgentGuide(unittest.TestCase):
    def test_every_setting_named_in_the_guide_is_read_by_the_code(self):
        named = set(re.findall(r'KEVENOS_[A-Z_]+', GUIDE))
        self.assertIn('KEVENOS_AGENT_PRINCIPALS_FILE', named)
        unknown = sorted(name for name in named if name not in SOURCES)
        self.assertEqual(unknown, [])

    def test_the_http_api_address_matches_the_server(self):
        self.assertIn(agent_api.PREFIX.rstrip('/'), GUIDE)
        self.assertNotIn('/api/agent/v1', GUIDE)
        for endpoint in ('status', 'capabilities', 'read', 'prepare', 'execute', 'receipts', 'guide', 'openapi.json'):
            self.assertIn('`%s`' % endpoint, GUIDE, endpoint)

    def test_the_remote_mcp_address_is_the_one_the_application_serves(self):
        self.assertTrue(callable(agent_api.proxy_mcp), 'the application forwards /mcp on its main port')
        self.assertIn('${KEVENOS_PUBLIC_URL}/mcp', GUIDE)
        self.assertNotIn('not part of the main application port', GUIDE)

    def test_every_command_line_option_shown_for_the_mcp_server_exists(self):
        parser_options = set()
        original = argparse.ArgumentParser.parse_args

        def capture(parser, *args, **kwargs):
            parser_options.update(option for action in parser._actions for option in action.option_strings)
            raise SystemExit(0)
        with patch.object(argparse.ArgumentParser, 'parse_args', capture), self.assertRaises(SystemExit):
            agent_mcp.main()
        self.assertIs(argparse.ArgumentParser.parse_args, original)
        shown = set(re.findall(r'agent_mcp\.py[^\n`]*', GUIDE))
        options = {option for line in shown for option in re.findall(r'--[a-z][a-z-]*', line)}
        self.assertTrue(shown, 'the guide shows how to start the MCP server')
        self.assertEqual(sorted(options - parser_options), [])

    def test_the_principals_file_fields_are_documented(self):
        for field in ('id', 'label', 'token_sha256', 'scopes', 'workspaces', 'expires_at', 'revoked', 'realm', 'persona', 'audience'):
            self.assertIn('"%s"' % field, GUIDE, field)
        catalog = agent_api._catalog()
        scopes = {scope for group in ('resources', 'commands') for entry in catalog[group] for scope in agent_api._scopes(entry)}
        listed = set(re.findall(r'`([a-z]+:[a-z]+)`', GUIDE))
        self.assertEqual(sorted(scopes ^ listed), [], 'a scope the guide lists but the server does not know disables every agent')


if __name__ == '__main__':
    unittest.main()
