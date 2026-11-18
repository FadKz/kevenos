import importlib.util
import stat
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_entrypoint():
    path = ROOT / "tools" / "vercel_entrypoint.py"
    spec = importlib.util.spec_from_file_location("vercel_entrypoint", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class VercelEntrypointTests(unittest.TestCase):
    def test_preview_uses_commit_specific_schema(self):
        entrypoint = load_entrypoint()
        environment = dict.fromkeys(entrypoint.SECRET_NAMES, 'synthetic-value')
        environment.update(VERCEL_ENV='preview', VERCEL_GIT_COMMIT_SHA='a' * 40,
                           KEVENOS_DB_SCHEMA_PREFIX='production_prefix')
        with tempfile.TemporaryDirectory() as folder:
            entrypoint.prepare(environment, Path(folder))
        self.assertEqual(environment['KEVENOS_DB_SCHEMA_PREFIX'], 'kevenos_preview_' + 'a' * 24)

    def test_preview_without_commit_fails_before_secret_files(self):
        entrypoint = load_entrypoint()
        environment = dict.fromkeys(entrypoint.SECRET_NAMES, 'synthetic-value')
        environment['VERCEL_ENV'] = 'preview'
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, 'VERCEL_GIT_COMMIT_SHA'):
                entrypoint.prepare(environment, Path(folder))
            self.assertFalse((Path(folder) / 'secrets').exists())

    def test_prepares_disposable_hosted_rehearsal_without_leaving_secrets_in_environment(self):
        entrypoint = load_entrypoint()
        environment = {
            "PORT": "4321",
            "VERCEL_PROJECT_PRODUCTION_URL": "kevenos-demo.example.vercel.app",
            "KEVENOS_PASSWORD": "owner-password-for-demo",
            "KEVENOS_GUEST_PASSWORD": "guest-password-for-demo",
            "KEVENOS_VIEW_KEY": "demo-view-key",
            "KEVENOS_SESSION_SECRET": "demo-session-secret",
        }

        with tempfile.TemporaryDirectory() as folder:
            command = entrypoint.prepare(environment, Path(folder))
            secret_dir = Path(environment["KEVENOS_SECRETS_DIR"])

            self.assertEqual(environment["KEVENOS_HOSTED"], "1")
            self.assertEqual(environment["KEVENOS_PORT"], "4321")
            self.assertEqual(environment["KEVENOS_DATA"], str(Path(folder) / "data"))
            self.assertEqual(environment["KEVENOS_PUBLIC_URL"], "https://kevenos-demo.example.vercel.app")
            self.assertEqual(environment["KEVENOS_DEMO_ROLE_PASSWORDS"], "1")
            self.assertEqual(environment["KEVENOS_STABLE_DEMO_CONTEXT"], "1")
            self.assertEqual(command[-1], "--rehearsal")
            self.assertTrue(command[-2].endswith("database_guard.py"))

            expected = {
                "KEVENOS_PASSWORD": "owner-password-for-demo",
                "KEVENOS_GUEST_PASSWORD": "guest-password-for-demo",
                "KEVENOS_VIEW_KEY": "demo-view-key",
                "KEVENOS_SESSION_SECRET": "demo-session-secret",
            }
            for name, value in expected.items():
                self.assertNotIn(name, environment)
                path = secret_dir / name
                self.assertEqual(path.read_text(encoding="utf-8"), value)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_missing_demo_secret_fails_before_writing_files(self):
        entrypoint = load_entrypoint()
        environment = {
            "KEVENOS_PASSWORD": "owner-password-for-demo",
            "KEVENOS_GUEST_PASSWORD": "guest-password-for-demo",
            "KEVENOS_VIEW_KEY": "demo-view-key",
        }

        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaisesRegex(RuntimeError, "KEVENOS_SESSION_SECRET"):
                entrypoint.prepare(environment, Path(folder))
            self.assertFalse((Path(folder) / "secrets").exists())


if __name__ == "__main__":
    unittest.main()
