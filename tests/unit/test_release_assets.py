"""scripts/release_assets.sh на фейковом каталоге выпуска (arch/appimage.md §9.3; T-168)."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
VERSION = "0.2.0"
DEB = f"astra-voice_{VERSION}_amd64.deb"
IMAGE = f"Astra_Voice-{VERSION}-x86_64.AppImage"
SHA = "a" * 64
LOCK = f"""# expect-elf: 157
# max-glibc: 2.28
# runtime-key: 570C77ACEA40C0F1B758902CBF96CCA56490F695
# tool: runtime-x86_64 {SHA} 1 https://example.invalid/runtime-x86_64
# tool: runtime-x86_64.sig {SHA} 2 https://example.invalid/runtime-x86_64.sig
# tool: appimagetool-x86_64.AppImage {SHA} 3 https://example.invalid/appimagetool
# tool: python3.11.16-cp311-cp311-manylinux_2_28_x86_64.AppImage {SHA} 4 https://example.invalid/py
numpy==1.24.2 \\
    --hash=sha256:{SHA}
"""
TODO_LINE = "# TODO-HASH: jsonschema==4.10.3 py3-none-any\n"


def make_repo(tmp_path: Path, *, enabled: bool) -> Path:
    repo = tmp_path / "repo"
    for name in ("release_assets.sh", "release_build.sh", "release_latest_json.py"):
        target = repo / "scripts" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / "scripts" / name, target)
    changelog = repo / "packaging" / "debian" / "changelog"
    changelog.parent.mkdir(parents=True)
    changelog.write_text(
        f"astra-voice ({VERSION}) unstable; urgency=medium\n\n"
        " -- Test <t@example.invalid>  Thu, 15 Oct 2026 12:00:00 +0000\n",
        encoding="utf-8",
    )
    (repo / "docs").mkdir()
    (repo / "docs" / "INSTALL-ADMIN.md").write_text("# admin\n", encoding="utf-8")
    (repo / "docs" / "SECURITY.md").write_text("# security\n", encoding="utf-8")
    (repo / "data" / "keys").mkdir(parents=True)
    (repo / "data" / "keys" / "release.gpg").write_bytes(b"keyring")
    if enabled:
        flag = repo / "packaging" / "appimage" / "ENABLED"
        flag.parent.mkdir(parents=True)
        flag.write_text("# флаг\n", encoding="utf-8")
        shutil.copy2(ROOT / "packaging/appimage/lockfile.py", flag.parent / "lockfile.py")
        (repo / "packaging" / "appimage.lock").write_text(LOCK, encoding="utf-8")
    dist = repo / "dist"
    dist.mkdir()
    (dist / DEB).write_bytes(b"deb bytes")
    (dist / "sbom.cdx.json").write_text("{}\n", encoding="utf-8")
    if enabled:
        (dist / IMAGE).write_bytes(b"\x7fELF\x02\x01\x01\x00AI\x02" + b"\0" * 100)
        (dist / "sbom-appimage.cdx.json").write_text("{}\n", encoding="utf-8")
    return repo


def run(
    repo: Path, script: str = "release_assets.sh", **extra_env: str
) -> subprocess.CompletedProcess[str]:
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in ("GPG_SIGNING_KEY", "GITHUB_REF_NAME")
    }
    env.update(extra_env)
    return subprocess.run(
        ["bash", f"scripts/{script}", "dist"]
        if script == "release_assets.sh"
        else ["bash", f"scripts/{script}"],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def sums(dist: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in (dist / "SHA256SUMS").read_text(encoding="utf-8").splitlines():
        digest, name = line.split("  ", 1)
        result[name] = digest
    return result


def test_with_appimage(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, enabled=True)
    dist = repo / "dist"
    result = run(repo, GITHUB_REF_NAME=f"v{VERSION}")
    assert result.returncode == 0, result.stdout + result.stderr
    listed = sums(dist)
    assert set(listed) == {
        DEB,
        "sbom.cdx.json",
        IMAGE,
        "sbom-appimage.cdx.json",
        "INSTALL-ADMIN.md",
        "SECURITY.md",
        "release.gpg",
        "latest.json",
    }
    for name, digest in listed.items():
        assert hashlib.sha256((dist / name).read_bytes()).hexdigest() == digest
    latest = json.loads((dist / "latest.json").read_text(encoding="utf-8"))
    assert latest["schema"] == 2
    assert latest["version"] == VERSION
    assert latest["release_url"].endswith(f"/releases/tag/v{VERSION}")
    assert latest["artifacts"]["appimage"] == {
        "name": IMAGE,
        "sha256": listed[IMAGE],
        "size": (dist / IMAGE).stat().st_size,
    }
    assert latest["artifacts"]["deb"]["name"] == DEB
    assets = (dist / "assets.txt").read_text(encoding="utf-8").splitlines()
    assert assets == [f"dist/{name}" for name in listed] + [
        "dist/SHA256SUMS",
        "dist/SHA256SUMS.asc",
    ]
    assert not (dist / "SHA256SUMS.asc").exists()


def test_without_appimage(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, enabled=False)
    result = run(repo)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "AppImage выключен" in result.stdout
    latest = json.loads((repo / "dist" / "latest.json").read_text(encoding="utf-8"))
    assert set(latest["artifacts"]) == {"deb"}
    assert IMAGE not in sums(repo / "dist")


def test_refuses_signing_secret(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, enabled=True)
    result = run(repo, GPG_SIGNING_KEY="secret")
    assert result.returncode == 1
    assert "секрет подписи" in result.stderr
    assert not (repo / "dist" / "SHA256SUMS").exists()


def test_enabled_requires_appimage(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, enabled=True)
    (repo / "dist" / IMAGE).unlink()
    result = run(repo)
    assert result.returncode == 1
    assert f"нет артефакта {repo / 'dist' / IMAGE}" in result.stderr


def test_stray_artifact_rejected(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, enabled=False)
    (repo / "dist" / IMAGE).write_bytes(b"stale")
    result = run(repo)
    assert result.returncode == 1
    assert f"посторонний артефакт в {repo / 'dist'}: {IMAGE}" in result.stderr


def test_tag_must_match_changelog(tmp_path: Path) -> None:
    repo = make_repo(tmp_path, enabled=False)
    result = run(repo, GITHUB_REF_NAME="v0.2.1")
    assert result.returncode == 1
    assert "не совпадает с версией changelog" in result.stderr


@pytest.mark.parametrize("script", ["release_assets.sh", "release_build.sh"])
def test_enabled_refuses_unpinned_wheels(tmp_path: Path, script: str) -> None:
    """P2-2: при ENABLED lock с TODO-HASH не выпускается — переменные не помогают."""
    repo = make_repo(tmp_path, enabled=True)
    with (repo / "packaging" / "appimage.lock").open("a", encoding="utf-8") as lock:
        lock.write(TODO_LINE)
    result = run(repo, script, ASTRA_VOICE_APPIMAGE_ALLOW_TODO_HASH="1")
    assert result.returncode == 1
    assert "колёса без хэша (TODO-HASH): jsonschema==4.10.3" in result.stderr
    assert not (repo / "dist" / "SHA256SUMS").exists()
