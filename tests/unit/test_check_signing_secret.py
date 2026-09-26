"""Проверки секрета подписи настоящими временными GPG-ключами."""

from __future__ import annotations

import os
import runpy
import shutil
import subprocess
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
CheckSecret = Callable[
    [Path, Path, frozenset[str], frozenset[str], str | None], tuple[str | None, list[str]]
]
CheckSignature = Callable[[Path, Path, Path, str, frozenset[str], frozenset[str]], list[str]]


def gpg(home: Path, *args: str, input_data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["gpg", "--batch", "--no-tty", "--pinentry-mode", "loopback", "--passphrase", "", *args],
        env={**os.environ, "GNUPGHOME": str(home), "LC_ALL": "C"},
        input=input_data,
        capture_output=True,
        timeout=30,
        check=True,
    )
    return result.stdout


def fingerprints(home: Path) -> list[str]:
    return [
        line.split(":")[9]
        for line in gpg(home, "--with-colons", "--list-keys").decode().splitlines()
        if line.startswith("fpr:")
    ]


@dataclass(frozen=True)
class Keys:
    home: Path
    keyring: Path
    master: str
    s1: str
    s2: str
    m2: str
    m2_subkey: str
    encryption: str


@pytest.fixture(scope="module")
def keys(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Keys]:
    if any(shutil.which(name) is None for name in ("gpg", "gpgconf")):
        pytest.skip("нет gpg или gpgconf")
    root = tmp_path_factory.mktemp("signing-secret")
    home = root / "gnupg"
    home.mkdir(mode=0o700)
    try:
        gpg(home, "--quick-gen-key", "Master <m@example.invalid>", "ed25519", "cert", "never")
        master = fingerprints(home)[0]
        gpg(home, "--quick-add-key", master, "ed25519", "sign", "never")
        s1 = fingerprints(home)[1]
        gpg(home, "--quick-add-key", master, "ed25519", "sign", "never")
        s2 = fingerprints(home)[2]
        gpg(home, "--quick-add-key", master, "cv25519", "encr", "never")
        encryption = fingerprints(home)[3]
        keyring = root / "release.gpg"
        keyring.write_bytes(gpg(home, "--export", master))
        gpg(home, "--quick-gen-key", "Other <o@example.invalid>", "ed25519", "cert", "never")
        m2 = fingerprints(home)[4]
        gpg(home, "--quick-add-key", m2, "ed25519", "sign", "never")
        m2_subkey = fingerprints(home)[5]
        yield Keys(home, keyring, master, s1, s2, m2, m2_subkey, encryption)
    finally:
        subprocess.run(
            ["gpgconf", "--kill", "gpg-agent"],
            env={**os.environ, "GNUPGHOME": str(home)},
            check=False,
            timeout=10,
        )


@contextmanager
def imported(tmp_path: Path, secret: bytes) -> Iterator[Path]:
    home = tmp_path / "imported"
    home.mkdir(mode=0o700)
    try:
        if secret:
            gpg(home, "--import", input_data=secret)
        yield home
    finally:
        subprocess.run(
            ["gpgconf", "--kill", "gpg-agent"],
            env={**os.environ, "GNUPGHOME": str(home)},
            check=False,
            timeout=10,
        )


@pytest.fixture
def api() -> dict[str, object]:
    return runpy.run_path(str(ROOT / "scripts/check_signing_secret.py"))


def check(api: dict[str, object]) -> CheckSecret:
    return cast(CheckSecret, api["check_secret"])


def secret_for(keys: Keys, fingerprint: str | None = None) -> bytes:
    return gpg(
        keys.home,
        "--export-secret-subkeys" if fingerprint is not None else "--export-secret-keys",
        f"{fingerprint}!" if fingerprint is not None else keys.master,
    )


def test_reference(keys: Keys, api: dict[str, object], tmp_path: Path) -> None:
    with imported(tmp_path, secret_for(keys, keys.s1)) as home:
        assert check(api)(home, keys.keyring, frozenset({keys.master}), frozenset(), None) == (
            keys.s1,
            [],
        )


def test_full_master_rejected(keys: Keys, api: dict[str, object], tmp_path: Path) -> None:
    with imported(tmp_path, secret_for(keys)) as home:
        fingerprint, errors = check(api)(
            home, keys.keyring, frozenset({keys.master}), frozenset(), None
        )
        assert fingerprint is None
        assert "заглушкой" in " ".join(errors)


def test_two_secret_subkeys_rejected(keys: Keys, api: dict[str, object], tmp_path: Path) -> None:
    with imported(tmp_path, gpg(keys.home, "--export-secret-subkeys", keys.master)) as home:
        fingerprint, errors = check(api)(
            home, keys.keyring, frozenset({keys.master}), frozenset(), None
        )
        assert fingerprint is None
        assert "ровно один секретный ssb" in " ".join(errors)


def test_foreign_subkey_rejected(keys: Keys, api: dict[str, object], tmp_path: Path) -> None:
    with imported(tmp_path, secret_for(keys, keys.m2_subkey)) as home:
        fingerprint, errors = check(api)(
            home, keys.keyring, frozenset({keys.master, keys.m2}), frozenset(), None
        )
        assert fingerprint is None
        assert "отсутствует в публичной связке" in " ".join(errors)


def test_primary_unpinned(keys: Keys, api: dict[str, object], tmp_path: Path) -> None:
    with imported(tmp_path, secret_for(keys, keys.s1)) as home:
        fingerprint, errors = check(api)(home, keys.keyring, frozenset(), frozenset(), None)
        assert fingerprint is None
        assert "первичный ключ не закреплён" in " ".join(errors)


def test_primary_revoked(keys: Keys, api: dict[str, object], tmp_path: Path) -> None:
    with imported(tmp_path, secret_for(keys, keys.s1)) as home:
        fingerprint, errors = check(api)(
            home, keys.keyring, frozenset({keys.master}), frozenset({keys.master}), None
        )
        assert fingerprint is None
        assert "первичный ключ отозван" in " ".join(errors)


def test_subkey_revoked(keys: Keys, api: dict[str, object], tmp_path: Path) -> None:
    with imported(tmp_path, secret_for(keys, keys.s1)) as home:
        fingerprint, errors = check(api)(
            home, keys.keyring, frozenset({keys.master}), frozenset({keys.s1}), None
        )
        assert fingerprint is None
        assert "секретный подключ отозван" in " ".join(errors)


def test_expired_signing_subkey(api: dict[str, object], tmp_path: Path) -> None:
    home = tmp_path / "expired-gnupg"
    home.mkdir(mode=0o700)
    try:
        # Создаём ключи в 2020 году: мастер бессрочный, подключ истёк в 2021 году.
        fake_time = ("--faked-system-time", "20200101T000000!")
        gpg(
            home,
            *fake_time,
            "--quick-gen-key",
            "Expired <e@example.invalid>",
            "ed25519",
            "cert",
            "never",
        )
        master = fingerprints(home)[0]
        gpg(home, *fake_time, "--quick-add-key", master, "ed25519", "sign", "1y")
        signing = fingerprints(home)[1]
        keyring = tmp_path / "expired-release.gpg"
        keyring.write_bytes(gpg(home, "--export", master))
        secret = gpg(home, "--export-secret-subkeys", f"{signing}!")
        with imported(tmp_path, secret) as imported_home:
            fingerprint, errors = check(api)(
                imported_home, keyring, frozenset({master}), frozenset(), None
            )
            assert fingerprint is None
            assert "секретный подключ отозван или истёк" in " ".join(errors)
    finally:
        subprocess.run(
            ["gpgconf", "--kill", "gpg-agent"],
            env={**os.environ, "GNUPGHOME": str(home)},
            check=False,
            timeout=10,
        )


def test_revoked_primary_validity(api: dict[str, object], tmp_path: Path) -> None:
    home = tmp_path / "revoked-gnupg"
    home.mkdir(mode=0o700)
    try:
        gpg(home, "--quick-gen-key", "Revoked <r@example.invalid>", "ed25519", "cert", "never")
        master = fingerprints(home)[0]
        gpg(home, "--quick-add-key", master, "ed25519", "sign", "never")
        signing = fingerprints(home)[1]
        revocation = home / "openpgp-revocs.d" / f"{master}.rev"
        certificate = revocation.read_text(encoding="utf-8").replace(
            ":-----BEGIN PGP PUBLIC KEY BLOCK-----",
            "-----BEGIN PGP PUBLIC KEY BLOCK-----",
            1,
        )
        gpg(home, "--import", input_data=certificate.encode())
        keyring = tmp_path / "revoked-release.gpg"
        keyring.write_bytes(gpg(home, "--export", master))
        secret = gpg(home, "--export-secret-subkeys", f"{signing}!")
        with imported(tmp_path, secret) as imported_home:
            fingerprint, errors = check(api)(
                imported_home, keyring, frozenset({master}), frozenset(), None
            )
            assert fingerprint is None
            assert "первичный ключ отозван или истёк" in " ".join(errors)
    finally:
        subprocess.run(
            ["gpgconf", "--kill", "gpg-agent"],
            env={**os.environ, "GNUPGHOME": str(home)},
            check=False,
            timeout=10,
        )


def test_non_signing_subkey_rejected(keys: Keys, api: dict[str, object], tmp_path: Path) -> None:
    with imported(tmp_path, secret_for(keys, keys.encryption)) as home:
        fingerprint, errors = check(api)(
            home, keys.keyring, frozenset({keys.master}), frozenset(), None
        )
        assert fingerprint is None
        assert "не имеет возможности подписи" in " ".join(errors)


def test_empty_home_rejected(keys: Keys, api: dict[str, object], tmp_path: Path) -> None:
    with imported(tmp_path, b"") as home:
        fingerprint, errors = check(api)(
            home, keys.keyring, frozenset({keys.master}), frozenset(), None
        )
        assert fingerprint is None
        assert "ровно один sec" in " ".join(errors)


@pytest.fixture
def signed(keys: Keys, tmp_path: Path) -> Iterator[tuple[Path, Path]]:
    with imported(tmp_path, secret_for(keys, keys.s1)) as home:
        data = tmp_path / "SHA256SUMS"
        sig = tmp_path / "SHA256SUMS.asc"
        data.write_text("test\n", encoding="utf-8")
        gpg(
            home,
            "--local-user",
            f"{keys.s1}!",
            "--detach-sign",
            "--armor",
            "--output",
            str(sig),
            str(data),
        )
        yield sig, data


def test_signature_valid(keys: Keys, api: dict[str, object], signed: tuple[Path, Path]) -> None:
    verify = cast(CheckSignature, api["check_signature"])
    sig, data = signed
    assert verify(keys.keyring, sig, data, keys.s1, frozenset({keys.master}), frozenset()) == []


def test_signature_tampered_data(
    keys: Keys, api: dict[str, object], signed: tuple[Path, Path]
) -> None:
    verify = cast(CheckSignature, api["check_signature"])
    sig, data = signed
    with data.open("ab") as output:
        output.write(b"x")
    errors = verify(keys.keyring, sig, data, keys.s1, frozenset({keys.master}), frozenset())
    assert "подпись не прошла проверку" in " ".join(errors)


def test_signature_other_subkey(
    keys: Keys, api: dict[str, object], signed: tuple[Path, Path]
) -> None:
    verify = cast(CheckSignature, api["check_signature"])
    sig, data = signed
    assert "другим подключом" in " ".join(
        verify(keys.keyring, sig, data, keys.s2, frozenset({keys.master}), frozenset())
    )


def test_signature_unpinned_master(
    keys: Keys, api: dict[str, object], signed: tuple[Path, Path]
) -> None:
    verify = cast(CheckSignature, api["check_signature"])
    sig, data = signed
    assert "не закреплён" in " ".join(
        verify(keys.keyring, sig, data, keys.s1, frozenset(), frozenset())
    )


def test_main_output(
    keys: Keys, api: dict[str, object], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    main = cast(Callable[..., int], api["main"])
    with imported(tmp_path, secret_for(keys, keys.s1)) as home:
        argv = ["secret", "--homedir", str(home), "--keyring", str(keys.keyring)]
        assert main(argv, frozenset({keys.master}), frozenset()) == 0
        out = capsys.readouterr()
        assert out.out == keys.s1 + "\n"
        assert out.err == ""
        assert main(argv, frozenset(), frozenset()) == 1
        out = capsys.readouterr()
        assert out.out == ""
        assert "ОШИБКА: первичный ключ не закреплён" in out.err
