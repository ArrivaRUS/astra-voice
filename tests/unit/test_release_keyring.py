"""Публичная релизная связка, срок подключа и выбор подписанта каталога."""

from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest

from astra_voice.security import PINNED_FINGERPRINTS
from astra_voice.security.verify import Verifier

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
GPG_AVAILABLE = pytest.mark.skipif(
    shutil.which("gpg") is None or shutil.which("gpgv") is None,
    reason="нужны gpg и gpgv",
)


def _home(path: Path) -> Path:
    path.mkdir(mode=0o700)
    os.chmod(path, 0o700)
    return path


def _gpg(home: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--homedir",
            str(home),
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            *args,
        ],
        capture_output=True,
        text=True,
        check=True,
    )


@GPG_AVAILABLE
def test_release_keyring_contains_only_pinned_public_key(tmp_path: Path) -> None:
    home = _home(tmp_path / "gnupg")
    keyring = ROOT / "data/keys/release.gpg"
    packets = _gpg(home, "--list-packets", str(keyring)).stdout.lower()
    assert "secret key packet" not in packets
    assert "secret sub key packet" not in packets
    assert packets.count("public key packet") == 1

    records = _gpg(
        home, "--with-colons", "--import-options", "show-only", "--import", str(keyring)
    ).stdout.splitlines()
    primaries = [line.split(":") for line in records if line.startswith("pub:")]
    subs = [line.split(":") for line in records if line.startswith("sub:")]
    fprs = [line.split(":")[9] for line in records if line.startswith("fpr:")]
    assert len(primaries) == 1
    assert len(fprs) == len(subs) + 1
    assert fprs[0] in PINNED_FINGERPRINTS
    assert "".join(char for char in primaries[0][11] if char.islower()) == "c"
    assert subs
    assert all("".join(char for char in sub[11] if char.islower()) == "s" for sub in subs)


@GPG_AVAILABLE
def test_verifier_accepts_signature_from_expired_subkey(tmp_path: Path) -> None:
    home = _home(tmp_path / "gnupg")
    uid = "Astra Voice Expired Subkey <expired@test.invalid>"
    created = "20200101T000000"
    signed = "20200101T120000"
    _gpg(home, "--faked-system-time", created, "--quick-gen-key", uid, "ed25519", "cert", "1d")
    primary = next(
        line.split(":")[9]
        for line in _gpg(home, "--with-colons", "--list-keys", uid).stdout.splitlines()
        if line.startswith("fpr:")
    )
    _gpg(
        home,
        "--faked-system-time",
        created,
        "--quick-add-key",
        primary,
        "ed25519",
        "sign",
        "1d",
    )
    fprs = [
        line.split(":")[9]
        for line in _gpg(
            home, "--faked-system-time", created, "--with-colons", "--list-keys", uid
        ).stdout.splitlines()
        if line.startswith("fpr:")
    ]
    assert len(fprs) == 2
    data = tmp_path / "SHA256SUMS"
    sig = tmp_path / "SHA256SUMS.asc"
    data.write_text("payload\n", encoding="utf-8")
    _gpg(
        home,
        "--faked-system-time",
        signed,
        "--detach-sign",
        "--local-user",
        fprs[1] + "!",
        "--output",
        str(sig),
        str(data),
    )
    keyring = tmp_path / "release.gpg"
    keyring.write_bytes(
        subprocess.run(
            ["gpg", "--batch", "--homedir", str(home), "--export", primary],
            capture_output=True,
            check=True,
        ).stdout
    )
    result = Verifier("release", keyring, pinned=frozenset({primary})).verify_detached(data, sig)
    assert result.ok, result.reason
    assert result.fingerprint == fprs[1]
    assert result.primary_fingerprint == primary
    assert any(line.startswith("EXPKEYSIG") for line in result.status)


def _build_manifest_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "build_manifest", ROOT / "scripts/build_manifest.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_build_manifest_sign_pins_exact_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _build_manifest_module()
    key = "7602A029F0E34CD2344A9CDA657ED04689FF4D79"
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        check = kwargs["check"]
        assert check is False
        calls.append(argv)
        if argv[0] == "gpgv":
            assert kwargs["capture_output"] is True
            assert kwargs["text"] is True
            return SimpleNamespace(returncode=0, stdout=f"[GNUPG:] VALIDSIG {key} metadata\n")
        assert argv[0] == "gpg"
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    module._sign(tmp_path, key)
    module._sign(tmp_path, key + "!")
    signing = [argv for argv in calls if argv[0] == "gpg"]
    verifying = [argv for argv in calls if argv[0] == "gpgv"]
    assert len(signing) == len(verifying) == 2
    assert [argv[argv.index("--local-user") + 1] for argv in signing] == [key + "!"] * 2
    for argv in verifying:
        assert argv[argv.index("--status-fd") + 1] == "1"
        assert argv[argv.index("--keyring") + 1] == str((ROOT / "data/keys/release.gpg").resolve())


@pytest.mark.parametrize(
    ("stdout", "returncode", "error"),
    [
        ("[GNUPG:] VALIDSIG 9053EE0C0085683CE6C7042EDC2E593225D80428\n", 0, "не тем ключом"),
        ("", 1, "gpgv не подтвердил подпись"),
    ],
)
def test_build_manifest_rejects_unverified_signer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    stdout: str,
    returncode: int,
    error: str,
) -> None:
    module = _build_manifest_module()
    key = "7602A029F0E34CD2344A9CDA657ED04689FF4D79"

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        if argv[0] == "gpgv":
            return SimpleNamespace(returncode=returncode, stdout=stdout)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(module.BuildError, match=error):
        module._sign(tmp_path, key)


def test_build_manifest_rejects_short_key_before_signing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = _build_manifest_module()
    calls: list[list[str]] = []

    def fake_run(argv: list[str], **kwargs: object) -> SimpleNamespace:
        calls.append(argv)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    with pytest.raises(
        module.BuildError, match=r"--key должен быть отпечатком подключа \(40 hex\)"
    ):
        module._sign(tmp_path, "ABC")
    assert calls == []
