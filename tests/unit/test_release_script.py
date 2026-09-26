"""Проверки release.sh выполняются только во временных Git-репозиториях."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def git(repo: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    work = tmp_path / "work"
    work.mkdir()
    env = {**os.environ, "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": "/dev/null"}
    git(work, env, "init", "-b", "main")
    for name, value in (
        ("user.name", "Test"),
        ("user.email", "t@example.invalid"),
        ("commit.gpgsign", "false"),
        ("tag.gpgsign", "false"),
    ):
        git(work, env, "config", name, value)
    (work / "scripts").mkdir()
    shutil.copy2(ROOT / "scripts/release.sh", work / "scripts/release.sh")
    (work / "packaging/debian").mkdir(parents=True)
    (work / "packaging/debian/changelog").write_text(
        "astra-voice (0.1.0) unstable; urgency=medium\n\n"
        " -- Test <t@example.invalid>  Wed, 30 Sep 2026 12:00:00 +0000\n",
        encoding="utf-8",
    )
    (work / "docs").mkdir()
    (work / "docs/INSTALL-ADMIN.md").write_text("# Test\n", encoding="utf-8")
    (work / "data/keys").mkdir(parents=True)
    (work / "data/keys/release.gpg").write_bytes(b"not a keyring")
    git(work, env, "add", ".")
    git(work, env, "commit", "-m", "test")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "init", "--bare", str(bare)], env=env, check=True, capture_output=True)
    git(work, env, "remote", "add", "origin", str(bare))
    git(work, env, "push", "-u", "origin", "main")
    git(work, env, "fetch", "origin")
    return work, env


def run(repo: tuple[Path, dict[str, str]], *args: str) -> subprocess.CompletedProcess[str]:
    work, env = repo
    return subprocess.run(
        ["bash", "scripts/release.sh", *args],
        cwd=work,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )


def test_dry_run(repo: tuple[Path, dict[str, str]]) -> None:
    work, env = repo
    result = run(repo, "v0.1.0", "--skip-ci-check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "git tag -a v0.1.0" in result.stdout
    assert "git push origin v0.1.0" in result.stdout
    assert "dry-run: ничего не создано" in result.stdout
    for asset in (
        "astra-voice_*.deb",
        "sbom.cdx.json",
        "SHA256SUMS",
        "SHA256SUMS.asc",
        "INSTALL-ADMIN.md (из docs/INSTALL-ADMIN.md)",
        "latest.json",
        "release.gpg (из data/keys/release.gpg)",
    ):
        assert asset in result.stdout
    assert (
        subprocess.run(
            ["git", "tag", "-l", "v0.1.0"],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == ""
    )


def test_dirty_tree(repo: tuple[Path, dict[str, str]]) -> None:
    work, _ = repo
    (work / "new.txt").write_text("dirty", encoding="utf-8")
    assert run(repo, "v0.1.0", "--skip-ci-check").returncode == 1


def test_missing_admin(repo: tuple[Path, dict[str, str]]) -> None:
    work, _ = repo
    (work / "docs/INSTALL-ADMIN.md").unlink()
    result = run(repo, "v0.1.0", "--skip-ci-check")
    assert result.returncode == 1
    assert "docs/INSTALL-ADMIN.md отсутствует или не отслеживается Git" in result.stdout


def test_untracked_keyring(repo: tuple[Path, dict[str, str]]) -> None:
    work, env = repo
    git(work, env, "rm", "--cached", "data/keys/release.gpg")
    result = run(repo, "v0.1.0", "--skip-ci-check")
    assert result.returncode == 1
    assert "data/keys/release.gpg отсутствует или не отслеживается Git" in result.stdout


def test_spike_keyring(repo: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_gpg = tools / "gpg"
    fake_gpg.write_text(
        "#!/bin/sh\nprintf '%s\\n' 'fpr:::::::::CB951AD794407972A0B1A5BBAFA87398C4953A71:'\n",
        encoding="utf-8",
    )
    fake_gpg.chmod(0o755)
    work, env = repo
    result = subprocess.run(
        ["bash", "scripts/release.sh", "v0.1.0", "--skip-ci-check"],
        cwd=work,
        env={**env, "PATH": f"{tools}:{env['PATH']}"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "в связке тестовый ключ" in result.stdout


def test_changelog_suffix(repo: tuple[Path, dict[str, str]]) -> None:
    work, _ = repo
    path = work / "packaging/debian/changelog"
    path.write_text(
        path.read_text(encoding="utf-8").replace("0.1.0", "0.1.0~m6.17"), encoding="utf-8"
    )
    result = run(repo, "v0.1.0", "--skip-ci-check")
    assert result.returncode == 1
    assert "dch -v 0.1.0 -D unstable" in result.stdout


def test_wrong_branch(repo: tuple[Path, dict[str, str]]) -> None:
    work, env = repo
    git(work, env, "switch", "-c", "other")
    assert run(repo, "v0.1.0", "--skip-ci-check").returncode == 1


def test_existing_tag(repo: tuple[Path, dict[str, str]]) -> None:
    work, env = repo
    git(work, env, "tag", "v0.1.0")
    assert run(repo, "v0.1.0", "--skip-ci-check").returncode == 1


@pytest.mark.parametrize("args", [("0.1.0",), ("v0.1.0", "--dry-run", "--push")])
def test_usage(repo: tuple[Path, dict[str, str]], args: tuple[str, ...]) -> None:
    assert run(repo, *args).returncode == 2


def test_missing_gh(repo: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("bash", "git", "sed", "python3", "head"):
        target = shutil.which(name)
        assert target is not None
        (tools / name).symlink_to(target)
    work, env = repo
    result = subprocess.run(
        [str(tools / "bash"), "scripts/release.sh", "v0.1.0"],
        cwd=work,
        env={**env, "PATH": str(tools)},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "--skip-ci-check" in result.stdout
    assert "gpg отсутствует; проверка тестового отпечатка пропущена" in result.stdout
