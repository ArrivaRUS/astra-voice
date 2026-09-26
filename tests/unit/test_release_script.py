"""Проверки release.sh выполняются только во временных Git-репозиториях."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def git(repo: Path, env: dict[str, str], *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, env=env, check=True, capture_output=True)


@pytest.fixture(scope="module")
def generated_key(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, str]]:
    if any(shutil.which(name) is None for name in ("gpg", "gpgconf")):
        pytest.skip("нет gpg или gpgconf")
    root = tmp_path_factory.mktemp("release-key")
    home = root / "gnupg"
    home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}
    try:
        result = subprocess.run(
            [
                "gpg",
                "--batch",
                "--pinentry-mode",
                "loopback",
                "--passphrase",
                "",
                "--quick-gen-key",
                "Test <t@example.invalid>",
                "ed25519",
                "sign",
                "never",
            ],
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if result.returncode:
            pytest.skip("генерация GPG-ключа невозможна")
        listing = subprocess.run(
            ["gpg", "--batch", "--with-colons", "--list-keys"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
        fingerprint = next(
            line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
        )
        keyring = root / "release.gpg"
        with keyring.open("wb") as output:
            subprocess.run(
                ["gpg", "--batch", "--export"], env=env, stdout=output, check=True, timeout=30
            )
        yield keyring, fingerprint
    finally:
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=env, check=False, timeout=10)


@pytest.fixture
def repo(tmp_path: Path, generated_key: tuple[Path, str]) -> tuple[Path, dict[str, str]]:
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
    shutil.copy2(ROOT / "scripts/check_keyring.py", work / "scripts/check_keyring.py")
    security = work / "src/astra_voice/security"
    security.mkdir(parents=True)
    (security.parent / "__init__.py").write_text("", encoding="utf-8")
    (security / "__init__.py").write_text("", encoding="utf-8")
    (security / "verify.py").write_text(
        f"PINNED_FINGERPRINTS = frozenset({{{generated_key[1]!r}}})\n"
        "REVOKED_FINGERPRINTS = frozenset()\n",
        encoding="utf-8",
    )
    (work / "packaging/debian").mkdir(parents=True)
    (work / "packaging/debian/changelog").write_text(
        "astra-voice (0.1.0) unstable; urgency=medium\n\n"
        " -- Test <t@example.invalid>  Wed, 30 Sep 2026 12:00:00 +0000\n",
        encoding="utf-8",
    )
    (work / "docs").mkdir()
    (work / "docs/INSTALL-ADMIN.md").write_text("# Test\n", encoding="utf-8")
    (work / "data/keys").mkdir(parents=True)
    shutil.copy2(generated_key[0], work / "data/keys/release.gpg")
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
    assert "git push origin refs/tags/v0.1.0" in result.stdout
    assert "ПРЕДУПРЕЖДЕНИЕ: CI НЕ проверен (--skip-ci-check)" in result.stdout
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


def test_gpg_failure(repo: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    tools = tmp_path / "tools"
    tools.mkdir()
    fake_gpg = tools / "gpg"
    fake_gpg.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
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
    assert "связка ключей в белом списке" in result.stdout


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
    assert "связка ключей в белом списке" in result.stdout


def test_unpinned_keyring(repo: tuple[Path, dict[str, str]]) -> None:
    work, env = repo
    verify = work / "src/astra_voice/security/verify.py"
    verify.write_text(
        'PINNED_FINGERPRINTS = frozenset({"0" * 40})\nREVOKED_FINGERPRINTS = frozenset()\n',
        encoding="utf-8",
    )
    git(work, env, "add", ".")
    git(work, env, "commit", "-m", "wrong pin")
    git(work, env, "push", "origin", "main")
    git(work, env, "fetch", "origin")
    result = run(repo, "v0.1.0", "--skip-ci-check")
    assert result.returncode == 1
    assert "первичный ключ не закреплён" in result.stderr


def test_garbage_keyring(repo: tuple[Path, dict[str, str]]) -> None:
    work, env = repo
    (work / "data/keys/release.gpg").write_bytes(b"garbage")
    git(work, env, "add", ".")
    git(work, env, "commit", "-m", "garbage key")
    git(work, env, "push", "origin", "main")
    git(work, env, "fetch", "origin")
    result = run(repo, "v0.1.0", "--skip-ci-check")
    assert result.returncode == 1
    assert "gpg не смог прочитать связку" in result.stderr


def test_status_failure(repo: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    work, env = repo
    tools = tmp_path / "tools"
    tools.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    fake = tools / "git"
    fake.write_text(
        f'#!/bin/sh\nif [ "$1" = status ]; then exit 2; fi\nexec {real_git} "$@"\n',
        encoding="utf-8",
    )
    fake.chmod(0o755)
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
    assert "не удалось проверить состояние рабочего дерева" in result.stdout


def test_tag_failure(repo: tuple[Path, dict[str, str]], tmp_path: Path) -> None:
    work, env = repo
    tools = tmp_path / "tools"
    tools.mkdir()
    real_git = shutil.which("git")
    assert real_git is not None
    fake = tools / "git"
    fake.write_text(
        f'#!/bin/sh\nif [ "$1" = tag ]; then exit 2; fi\nexec {real_git} "$@"\n', encoding="utf-8"
    )
    fake.chmod(0o755)
    result = subprocess.run(
        ["bash", "scripts/release.sh", "v0.1.0", "--push", "--skip-ci-check"],
        cwd=work,
        env={**env, "PATH": f"{tools}:{env['PATH']}"},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "не удалось создать тег" in result.stderr


def test_push_failure(repo: tuple[Path, dict[str, str]]) -> None:
    work, env = repo
    bare = Path(
        subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    )
    hook = bare / "hooks/pre-receive"
    hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    hook.chmod(0o755)
    result = run(repo, "v0.1.0", "--push", "--skip-ci-check")
    assert result.returncode == 1
    assert "git tag -d v0.1.0" in result.stderr
    assert (
        subprocess.run(
            ["git", "tag", "-l", "v0.1.0"],
            cwd=work,
            env=env,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        == "v0.1.0"
    )
