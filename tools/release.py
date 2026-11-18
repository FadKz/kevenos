#!/usr/bin/env python3
"""Validate a KevenOS version and create deterministic release artifacts."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


SEMVER = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]+)?")


def read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def match_version(path: Path, pattern: str, label: str) -> str:
    match = re.search(pattern, read(path), re.MULTILINE)
    if not match:
        raise ValueError(f"{label} does not declare a release version")
    return match.group(1)


def validate_version(root: Path, tag: str | None = None) -> str:
    root = root.resolve()
    version = str(json.loads(read(root / "package.json")).get("version", ""))
    if not SEMVER.fullmatch(version):
        raise ValueError("package.json version is not valid semantic versioning")

    sources = {
        "package-lock.json": str(
            json.loads(read(root / "package-lock.json")).get("packages", {}).get("", {}).get("version", "")
        ),
        "server.py": match_version(root / "server.py", r'^APP_VERSION\s*=\s*["\']([^"\']+)', "server.py"),
        "Dockerfile": match_version(
            root / "Dockerfile",
            r'org\.opencontainers\.image\.version="([^"]+)"',
            "Dockerfile",
        ),
        "Dockerfile.vercel": match_version(
            root / "Dockerfile.vercel",
            r'org\.opencontainers\.image\.version="([^"]+)"',
            "Dockerfile.vercel",
        ),
        "compose.yaml": match_version(root / "compose.yaml", r"image:\s*kevenos:([^\s]+)", "compose.yaml"),
    }
    mismatches = [f"{name}={value}" for name, value in sources.items() if value != version]
    if mismatches:
        raise ValueError(f"release version {version} does not match " + ", ".join(mismatches))

    if not re.search(rf"^##\s+{re.escape(version)}(?:\s|—|-)", read(root / "CHANGELOG.md"), re.MULTILINE):
        raise ValueError(f"CHANGELOG.md has no section for {version}")

    if tag is not None:
        normalized = tag.removeprefix("v")
        if normalized != version:
            raise ValueError(f"tag {tag} does not match package version {version}")
    return version


def changelog_notes(root: Path, version: str) -> str:
    text = read(root / "CHANGELOG.md")
    match = re.search(
        rf"^##\s+{re.escape(version)}[^\n]*\n(?P<body>.*?)(?=^##\s+|\Z)",
        text,
        re.MULTILINE | re.DOTALL,
    )
    if not match:
        raise ValueError(f"CHANGELOG.md has no section for {version}")
    return f"# KevenOS {version}\n\n{match.group('body').strip()}\n"


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(root: Path, *args: str, stdout=None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=root, check=True, text=stdout is None, stdout=stdout or subprocess.PIPE
    )
    return "" if stdout is not None else result.stdout.strip()


def archive(root: Path, output: Path, prefix: str, paths: list[str] | None = None) -> None:
    args = ["archive", "--format=tar.gz", f"--prefix={prefix}/", f"--output={output}", "HEAD"]
    if paths:
        args.extend(paths)
    git(root, *args)


def build_release(root: Path, output: Path, tag: str | None = None) -> Path:
    root = root.resolve()
    output = output.resolve()
    version = validate_version(root, tag)
    if output.exists() and any(output.iterdir()):
        raise ValueError(f"release output directory is not empty: {output}")
    output.mkdir(parents=True, exist_ok=True)

    prefix = f"kevenos-{version}"
    source = output / f"{prefix}-source.tar.gz"
    website = output / f"{prefix}-website.tar.gz"
    notes = output / "RELEASE_NOTES.md"
    archive(root, source, prefix)
    archive(root, website, prefix, ["website", "brand"])
    notes.write_text(changelog_notes(root, version), encoding="utf-8")

    artifacts = []
    for path in (source, website, notes):
        artifacts.append({"name": path.name, "bytes": path.stat().st_size, "sha256": sha256(path)})
    manifest = {
        "schema": 1,
        "name": "KevenOS",
        "version": version,
        "tag": f"v{version}",
        "commit": git(root, "rev-parse", "HEAD"),
        "artifacts": artifacts,
    }
    manifest_path = output / "release-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    checksum_paths = [source, website, notes, manifest_path]
    checksums = "".join(f"{sha256(path)}  {path.name}\n" for path in checksum_paths)
    (output / "SHA256SUMS").write_text(checksums, encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("validate", "build"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--tag")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    try:
        version = validate_version(args.root, args.tag)
        if args.command == "build":
            path = build_release(args.root, args.output, args.tag)
            print(f"prepared KevenOS {version} in {path}")
        else:
            print(f"KevenOS {version} release metadata is consistent")
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError, json.JSONDecodeError) as exc:
        parser.exit(2, f"release error: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
