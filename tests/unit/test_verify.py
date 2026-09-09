"""Тесты `astra_voice.security.verify` (T-05, T-31).

Ключи для тестов генерируются здесь же, во временном `GNUPGHOME`: закрытая часть
тестового ключа спайка S2 в репозиторий не попадает и в тестах не участвует.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from astra_voice.security.verify import Verifier

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        shutil.which("gpg") is None or shutil.which("gpgv") is None,
        reason="нужны gpg и gpgv",
    ),
]


class _Gpg:
    """Одноразовый `GNUPGHOME` с несколькими ключами."""

    def __init__(self, home: Path) -> None:
        self.home = home
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        argv = [
            "gpg",
            "--batch",
            "--yes",
            "--quiet",
            "--homedir",
            str(self.home),
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            *args,
        ]
        return subprocess.run(argv, capture_output=True, text=True, check=True)

    def gen(self, uid: str) -> str:
        """Создать ed25519-ключ и вернуть отпечаток первичного ключа."""
        self._run("--quick-gen-key", uid, "ed25519", "sign", "never")
        out = self._run("--list-keys", "--with-colons", uid).stdout
        for line in out.splitlines():
            if line.startswith("fpr:"):
                return line.split(":")[9]
        raise AssertionError(f"не нашёл отпечаток для {uid}")

    def add_subkey(self, fpr: str) -> str:
        """Добавить подписывающий подключ и вернуть его отпечаток."""
        self._run("--quick-add-key", fpr, "ed25519", "sign", "never")
        out = self._run("--list-keys", "--with-colons", fpr).stdout
        fprs = [line.split(":")[9] for line in out.splitlines() if line.startswith("fpr:")]
        assert len(fprs) >= 2, out
        return fprs[-1]

    def export(self, uid: str, dest: Path) -> Path:
        # --export печатает двоичные данные: берём байтами, без text=True
        raw = subprocess.run(
            ["gpg", "--homedir", str(self.home), "--export", uid],
            capture_output=True,
            check=True,
        ).stdout
        dest.write_bytes(raw)
        return dest

    def sign(self, data: Path, sig: Path, key: str) -> Path:
        self._run("--detach-sign", "--local-user", key, "--output", str(sig), str(data))
        return sig


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> dict[str, object]:
    root = tmp_path_factory.mktemp("verify")
    gpg = _Gpg(root / "gnupg")

    release_uid = "Astra Voice Test Release <release@test.invalid>"
    foreign_uid = "Someone Else <else@test.invalid>"
    release_fpr = gpg.gen(release_uid)
    foreign_fpr = gpg.gen(foreign_uid)
    subkey_fpr = gpg.add_subkey(release_fpr)

    data = root / "SHA256SUMS"
    data.write_text("payload\n", encoding="utf-8")

    keyring = gpg.export(release_uid, root / "release.gpg")
    keyring_both = root / "both.gpg"
    keyring_both.write_bytes(
        keyring.read_bytes() + gpg.export(foreign_uid, root / "foreign.gpg").read_bytes()
    )

    return {
        "root": root,
        "gpg": gpg,
        "data": data,
        "keyring": keyring,
        "keyring_both": keyring_both,
        "release_fpr": release_fpr,
        "foreign_fpr": foreign_fpr,
        "subkey_fpr": subkey_fpr,
        # «!» — подписать именно этим ключом: иначе gpg возьмёт свежий подключ
        "sig_release": gpg.sign(data, root / "release.sig", release_fpr + "!"),
        "sig_foreign": gpg.sign(data, root / "foreign.sig", foreign_fpr),
        "sig_subkey": gpg.sign(data, root / "subkey.sig", subkey_fpr + "!"),
    }


def _verifier(env: dict[str, object], **kw: object) -> Verifier:
    params: dict[str, object] = {
        "purpose": "release",
        "keyring": env["keyring"],
        "pinned": frozenset({str(env["release_fpr"])}),
        "revoked": frozenset(),
    }
    params.update(kw)
    return Verifier(**params)  # type: ignore[arg-type]


def test_valid_signature_accepted(env: dict[str, object]) -> None:
    res = _verifier(env).verify_detached(Path(str(env["data"])), Path(str(env["sig_release"])))
    assert res.ok, res.reason
    assert res.fingerprint == env["release_fpr"]
    assert res.primary_fingerprint == env["release_fpr"]


def test_subkey_accepted_by_primary_pin(env: dict[str, object]) -> None:
    """Пин первичного ключа принимает подпись его подключа (ротация S1→S2)."""
    res = _verifier(env).verify_detached(Path(str(env["data"])), Path(str(env["sig_subkey"])))
    assert res.ok, res.reason
    assert res.fingerprint == env["subkey_fpr"]
    assert res.primary_fingerprint == env["release_fpr"]


def test_foreign_key_rejected(env: dict[str, object]) -> None:
    """Ключ есть в связке, но не закреплён → отказ (У3)."""
    v = _verifier(env, keyring=env["keyring_both"])
    res = v.verify_detached(Path(str(env["data"])), Path(str(env["sig_foreign"])))
    assert not res.ok
    assert "не закреплён" in res.reason
    assert res.fingerprint == env["foreign_fpr"]


def test_unknown_key_rejected(env: dict[str, object]) -> None:
    """Ключа нет в связке → gpgv отвергает сам."""
    res = _verifier(env).verify_detached(Path(str(env["data"])), Path(str(env["sig_foreign"])))
    assert not res.ok


def test_revoked_key_rejected(env: dict[str, object]) -> None:
    """Отзыв побеждает пин: gpgv отзыв не проверяет, проверяем мы (§4.2)."""
    v = _verifier(env, revoked=frozenset({str(env["release_fpr"])}))
    res = v.verify_detached(Path(str(env["data"])), Path(str(env["sig_release"])))
    assert not res.ok
    assert "отозван" in res.reason


def test_goodsig_without_validsig_rejected(env: dict[str, object], tmp_path: Path) -> None:
    """Подделка статуса: GOODSIG + код 0, но без VALIDSIG → отказ."""
    fake = tmp_path / "fake-gpgv"
    fake.write_text(
        f"#!/bin/sh\necho '[GNUPG:] GOODSIG DEADBEEF {env['release_fpr']}'\nexit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    v = _verifier(env, gpgv_path=fake)
    res = v.verify_detached(Path(str(env["data"])), Path(str(env["sig_release"])))
    assert not res.ok
    assert "VALIDSIG" in res.reason


def test_two_validsig_rejected(env: dict[str, object], tmp_path: Path) -> None:
    """Две подписи в одном файле — неоднозначность, отказ."""
    fpr = str(env["release_fpr"])
    line = f"[GNUPG:] VALIDSIG {fpr} 2026-09-09 0 0 4 0 22 8 00 {fpr}"
    fake = tmp_path / "fake-gpgv-2"
    fake.write_text(f"#!/bin/sh\necho '{line}'\necho '{line}'\nexit 0\n", encoding="utf-8")
    fake.chmod(0o755)
    res = _verifier(env, gpgv_path=fake).verify_detached(
        Path(str(env["data"])), Path(str(env["sig_release"]))
    )
    assert not res.ok
    assert "одна подпись" in res.reason


def test_missing_gpgv_reported(env: dict[str, object], tmp_path: Path) -> None:
    res = _verifier(env, gpgv_path=tmp_path / "нет-такого").verify_detached(
        Path(str(env["data"])), Path(str(env["sig_release"]))
    )
    assert not res.ok
    assert "gpgv" in res.reason


def test_sha256sums_ok(env: dict[str, object], tmp_path: Path) -> None:
    payload = tmp_path / "astra-voice_0.1.0~m1_amd64.deb"
    payload.write_bytes(b"deb-bytes")
    digest = subprocess.run(
        ["sha256sum", str(payload)], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{digest}  {payload.name}\n", encoding="utf-8")
    assert _verifier(env).verify_sha256sums(sums, payload).ok


def test_sha256sums_duplicate_name_rejected(env: dict[str, object], tmp_path: Path) -> None:
    """Дубль имени с разными суммами → отказ (У4)."""
    payload = tmp_path / "pkg.deb"
    payload.write_bytes(b"deb-bytes")
    digest = subprocess.run(
        ["sha256sum", str(payload)], capture_output=True, text=True, check=True
    ).stdout.split()[0]
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{digest}  pkg.deb\n{'0' * 64}  pkg.deb\n", encoding="utf-8")
    res = _verifier(env).verify_sha256sums(sums, payload)
    assert not res.ok
    assert "2 строк" in res.reason


def test_sha256sums_mismatch_rejected(env: dict[str, object], tmp_path: Path) -> None:
    payload = tmp_path / "pkg.deb"
    payload.write_bytes(b"deb-bytes")
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{'0' * 64}  pkg.deb\n", encoding="utf-8")
    res = _verifier(env).verify_sha256sums(sums, payload)
    assert not res.ok
    assert "sha256" in res.reason


def test_sha256sums_path_in_name_rejected(env: dict[str, object], tmp_path: Path) -> None:
    payload = tmp_path / "pkg.deb"
    payload.write_bytes(b"deb-bytes")
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(f"{'0' * 64}  ../pkg.deb\n", encoding="utf-8")
    res = _verifier(env).verify_sha256sums(sums, payload)
    assert not res.ok
    assert "имя с путём" in res.reason


def test_bad_purpose_rejected(env: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        Verifier("whatever", Path(str(env["keyring"])), frozenset(), frozenset())  # type: ignore[arg-type]
