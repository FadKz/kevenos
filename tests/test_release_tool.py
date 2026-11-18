import importlib.util
import json
import subprocess
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def load_release_tool():
    path = ROOT / "tools" / "release.py"
    spec = importlib.util.spec_from_file_location("kevenos_release", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleaseToolTests(unittest.TestCase):
    def make_repo(self, root: Path, *, package_version="1.2.3", app_version="1.2.3"):
        (root / "website").mkdir()
        (root / "brand").mkdir()
        (root / "package.json").write_text(json.dumps({"version": package_version}))
        (root / "package-lock.json").write_text(
            json.dumps({"version": package_version, "packages": {"": {"version": package_version}}})
        )
        (root / "server.py").write_text(f'APP_VERSION = "{app_version}"\n')
        (root / "Dockerfile").write_text(
            f'FROM scratch\nLABEL org.opencontainers.image.version="{package_version}"\n'
        )
        (root / "Dockerfile.vercel").write_text(
            f'FROM scratch\nLABEL org.opencontainers.image.version="{package_version}"\n'
        )
        (root / "compose.yaml").write_text(f"services:\n  app:\n    image: kevenos:{package_version}\n")
        (root / "CHANGELOG.md").write_text(
            f"# Changelog\n\n## {package_version} — 2026-11-18\n\n- Release notes.\n\n## 1.0.0 — 2026-01-01\n\n- Earlier.\n"
        )
        (root / "README.md").write_text("KevenOS\n")
        (root / "website" / "index.html").write_text("<title>KevenOS</title>\n")
        (root / "brand" / "BRAND.md").write_text("# Brand\n")
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=root, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=root, check=True)
        subprocess.run(["git", "add", "."], cwd=root, check=True)
        subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)

    def test_version_validation_accepts_matching_tag_and_sources(self):
        release = load_release_tool()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            self.assertEqual(release.validate_version(root, "v1.2.3"), "1.2.3")

    def test_version_validation_rejects_mismatch(self):
        release = load_release_tool()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root, app_version="1.2.2")
            with self.assertRaisesRegex(ValueError, "server.py"):
                release.validate_version(root, "v1.2.3")

    def test_version_validation_rejects_vercel_image_mismatch(self):
        release = load_release_tool()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.make_repo(root)
            (root / "Dockerfile.vercel").write_text(
                'FROM scratch\nLABEL org.opencontainers.image.version="1.2.2"\n'
            )
            with self.assertRaisesRegex(ValueError, "Dockerfile.vercel"):
                release.validate_version(root, "v1.2.3")

    def test_build_creates_archives_manifest_notes_and_checksums(self):
        release = load_release_tool()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            output = Path(tmp) / "dist"
            root.mkdir()
            self.make_repo(root)
            release.build_release(root, output, "v1.2.3")

            source = output / "kevenos-1.2.3-source.tar.gz"
            website = output / "kevenos-1.2.3-website.tar.gz"
            self.assertTrue(source.is_file())
            self.assertTrue(website.is_file())
            self.assertIn("Release notes.", (output / "RELEASE_NOTES.md").read_text())
            manifest = json.loads((output / "release-manifest.json").read_text())
            self.assertEqual(manifest["version"], "1.2.3")
            self.assertEqual(manifest["schema"], 1)
            self.assertEqual(len(manifest["commit"]), 40)
            self.assertEqual(
                {item["name"] for item in manifest["artifacts"]},
                {source.name, website.name, "RELEASE_NOTES.md"},
            )
            checksums = (output / "SHA256SUMS").read_text()
            self.assertIn(source.name, checksums)
            self.assertIn("release-manifest.json", checksums)
            with tarfile.open(website, "r:gz") as archive:
                names = archive.getnames()
            self.assertIn("kevenos-1.2.3/website/index.html", names)
            self.assertIn("kevenos-1.2.3/brand/BRAND.md", names)

    def test_build_refuses_nonempty_output_directory(self):
        release = load_release_tool()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "repo"
            output = Path(tmp) / "dist"
            root.mkdir()
            output.mkdir()
            (output / "unrelated.txt").write_text("keep")
            self.make_repo(root)
            with self.assertRaisesRegex(ValueError, "not empty"):
                release.build_release(root, output, "v1.2.3")


if __name__ == "__main__":
    unittest.main()
