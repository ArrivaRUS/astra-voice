"""Проверки публичной связки настоящими временными GPG-ключами."""

from __future__ import annotations

import os
import runpy
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import cast

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
Check = Callable[[Path, frozenset[str], frozenset[str], str | None], tuple[bool, list[str]]]


@pytest.fixture(scope="module")
def key(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[Path, str, str]]:
    if any(shutil.which(name) is None for name in ("gpg", "gpgconf")):
        pytest.skip("нет gpg или gpgconf")
    root = tmp_path_factory.mktemp("check-keyring")
    home = root / "gnupg"
    home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}
    try:
        generated = subprocess.run(
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
        if generated.returncode:
            pytest.skip("генерация GPG-ключа невозможна")
        listing = subprocess.run(
            ["gpg", "--batch", "--with-colons", "--list-keys"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
        primary = next(
            line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
        )
        added = subprocess.run(
            [
                "gpg",
                "--batch",
                "--pinentry-mode",
                "loopback",
                "--passphrase",
                "",
                "--quick-add-key",
                primary,
                "ed25519",
                "sign",
                "never",
            ],
            env=env,
            capture_output=True,
            timeout=30,
            check=False,
        )
        if added.returncode:
            pytest.skip("генерация GPG-подключа невозможна")
        listing = subprocess.run(
            ["gpg", "--batch", "--with-colons", "--list-keys"],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        ).stdout
        fingerprints = [
            line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
        ]
        exported = root / "release.gpg"
        with exported.open("wb") as output:
            subprocess.run(
                ["gpg", "--batch", "--export"], env=env, stdout=output, check=True, timeout=30
            )
        yield exported, fingerprints[0], fingerprints[1]
    finally:
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=env, check=False, timeout=10)


@pytest.fixture
def check() -> Check:
    return cast(Check, runpy.run_path(str(ROOT / "scripts/check_keyring.py"))["check"])


def test_pinned_and_unpinned(key: tuple[Path, str, str], check: Check) -> None:
    path, primary, _ = key
    assert check(path, frozenset({primary}), frozenset(), None)[0]
    ok, messages = check(path, frozenset(), frozenset(), None)
    assert not ok and primary in " ".join(messages)


def test_revoked_primary_and_subkey(key: tuple[Path, str, str], check: Check) -> None:
    path, primary, subkey = key
    for revoked in (primary, subkey):
        ok, messages = check(path, frozenset({primary}), frozenset({revoked}), None)
        assert not ok and revoked in " ".join(messages)


@pytest.mark.parametrize("content", [b"garbage", b""])
def test_invalid_keyring(tmp_path: Path, check: Check, content: bytes) -> None:
    path = tmp_path / "bad.gpg"
    path.write_bytes(content)
    assert not check(path, frozenset(), frozenset(), None)[0]


def test_empty_gpg_listing_has_no_primary(tmp_path: Path, check: Check) -> None:
    fake = tmp_path / "gpg"
    fake.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    ok, messages = check(tmp_path / "release.gpg", frozenset(), frozenset(), str(fake))
    assert not ok
    assert "нет первичного ключа" in " ".join(messages)


def test_subkey_only_listing_has_no_primary(tmp_path: Path, check: Check) -> None:
    fake = tmp_path / "gpg"
    fingerprint = "A" * 40
    fake.write_text(
        f"#!/bin/sh\nprintf '%s\\n' 'sub:::::::::' 'fpr:::::::::{fingerprint}:'\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    ok, messages = check(tmp_path / "release.gpg", frozenset(), frozenset(), str(fake))
    assert not ok
    assert "нет первичного ключа" in " ".join(messages)


def test_gpg_failure(tmp_path: Path, key: tuple[Path, str, str], check: Check) -> None:
    fake = tmp_path / "gpg"
    fake.write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    fake.chmod(0o755)
    assert not check(key[0], frozenset({key[1]}), frozenset(), str(fake))[0]


def test_gpg_missing(
    monkeypatch: pytest.MonkeyPatch, key: tuple[Path, str, str], check: Check
) -> None:
    # runpy создаёт отдельное пространство имён, но импортированный модуль shutil общий.
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    assert not check(key[0], frozenset({key[1]}), frozenset(), None)[0]


def test_cli(tmp_path: Path, key: tuple[Path, str, str]) -> None:
    script = tmp_path / "scripts" / "check_keyring.py"
    script.parent.mkdir()
    shutil.copy2(ROOT / "scripts/check_keyring.py", script)
    security = tmp_path / "src" / "astra_voice" / "security"
    security.mkdir(parents=True)
    (security.parent / "__init__.py").write_text("", encoding="utf-8")
    (security / "__init__.py").write_text("", encoding="utf-8")
    verify = security / "verify.py"
    verify.write_text(
        f"PINNED_FINGERPRINTS = frozenset({{{key[1]!r}}})\nREVOKED_FINGERPRINTS = frozenset()\n",
        encoding="utf-8",
    )
    assert subprocess.run([sys.executable, str(script), str(key[0])], check=False).returncode == 0
    verify.write_text(
        'PINNED_FINGERPRINTS = frozenset({"0" * 40})\nREVOKED_FINGERPRINTS = frozenset()\n',
        encoding="utf-8",
    )
    assert subprocess.run([sys.executable, str(script), str(key[0])], check=False).returncode == 1


def test_cli_usage() -> None:
    assert (
        subprocess.run(
            [sys.executable, str(ROOT / "scripts/check_keyring.py")],
            capture_output=True,
            check=False,
        ).returncode
        == 2
    )
