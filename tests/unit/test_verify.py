"""Тесты `astra_voice.security.verify` (T-05, T-31).

Ключи для тестов генерируются здесь же, во временном `GNUPGHOME`: закрытая часть
тестового ключа спайка S2 в репозиторий не попадает и в тестах не участвует.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import Mock, call

import pytest

from astra_voice.security import verify
from astra_voice.security.verify import HashCancelledError, Verifier, sha256_file

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


def test_relative_signature_paths_accepted(
    env: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(Path(str(env["root"])))
    verifier = _verifier(env, keyring=Path("release.gpg"))
    result = verifier.verify_detached(Path("SHA256SUMS"), Path("release.sig"))
    assert result.ok, result.reason


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


def test_sha256_file_cancel_on_second_block(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payload = tmp_path / "payload"
    data = b"abcdefgh"
    payload.write_bytes(data)
    assert sha256_file(payload) == hashlib.sha256(data).hexdigest()

    positions: list[int] = []
    with payload.open("rb") as reader:
        read = Mock(wraps=reader.read)
        monkeypatch.setattr(reader, "read", read)
        monkeypatch.setattr(verify, "open", Mock(return_value=reader), raising=False)

        def cancel() -> bool:
            positions.append(reader.tell())
            return reader.tell() == 4

        with pytest.raises(HashCancelledError):
            sha256_file(payload, chunk=2, cancel=cancel)

    assert positions == [0, 2, 4]
    assert read.call_args_list == [call(2), call(2)]
    assert positions[-1] < len(data)
    assert reader.closed


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


# -- T-116 (У92): часы компьютера раньше даты создания ключа --------------------

_FUTURE_S = 400 * 86400


@pytest.fixture(scope="module")
def future(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, object]]:
    """Ключ и подпись «из будущего»: `--faked-system-time` во временном GNUPGHOME."""
    root = tmp_path_factory.mktemp("future")
    gpg = _Gpg(root / "gnupg")
    faked = f"{int(time.time()) + _FUTURE_S}!"
    uid = "Astra Voice Future Key <future@test.invalid>"
    gpg._run("--faked-system-time", faked, "--quick-gen-key", uid, "ed25519", "sign", "never")
    out = gpg._run("--list-keys", "--with-colons", uid).stdout
    fpr = next(line.split(":")[9] for line in out.splitlines() if line.startswith("fpr:"))
    data = root / "catalog.json"
    data.write_text("{}\n", encoding="utf-8")
    sig = root / "catalog.json.sig"
    gpg._run(
        "--faked-system-time",
        faked,
        "--detach-sign",
        "--local-user",
        fpr + "!",
        "--output",
        str(sig),
        str(data),
    )
    yield {
        "root": root,
        "fpr": fpr,
        "data": data,
        "sig": sig,
        "keyring": gpg.export(uid, root / "future.gpg"),
    }
    # Агент, поднятый gpg для временного каталога, не должен пережить тесты.
    subprocess.run(
        ["gpgconf", "--homedir", str(gpg.home), "--kill", "gpg-agent"],
        capture_output=True,
        check=False,
    )


def _spy_argv(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []
    original = subprocess.run

    def spy(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(list(argv))
        return original(argv, **kwargs)  # type: ignore[call-overload,no-any-return]

    monkeypatch.setattr(subprocess, "run", spy)
    return calls


def test_key_from_future_reports_clock_behind(
    future: dict[str, object], monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _spy_argv(monkeypatch)
    verifier = Verifier(
        "catalog", keyring=Path(str(future["keyring"])), pinned=frozenset({str(future["fpr"])})
    )
    res = verifier.verify_detached(Path(str(future["data"])), Path(str(future["sig"])))
    assert not res.ok
    assert res.code == verify.CLOCK_BEHIND
    assert "часы отстают" in res.reason
    assert any(line.startswith("ERRSIG ") for line in res.status)
    assert len(calls) == 1
    assert calls[0][0] == str(verify.GPGV_PATH)
    assert "--ignore-time-conflict" not in calls[0]
    assert not any(arg.startswith("--ignore") or "faked" in arg for arg in calls[0])


def test_future_signature_is_compared_with_clock(future: dict[str, object]) -> None:
    """Если часы проверяльщика впереди подписи, причина — обычный отказ gpgv."""
    verifier = Verifier(
        "catalog",
        keyring=Path(str(future["keyring"])),
        pinned=frozenset({str(future["fpr"])}),
        clock=lambda: time.time() + 2 * _FUTURE_S,
    )
    res = verifier.verify_detached(Path(str(future["data"])), Path(str(future["sig"])))
    assert not res.ok
    assert res.code == ""
    assert "часы" not in res.reason


def test_unknown_future_key_is_not_clock_behind(
    env: dict[str, object], future: dict[str, object]
) -> None:
    """Ключа нет в связке (ERRSIG с кодом 9): дата подписи о часах не говорит."""
    res = _verifier(env).verify_detached(Path(str(future["data"])), Path(str(future["sig"])))
    assert not res.ok
    assert res.code == ""


@pytest.mark.parametrize(
    ("line", "clock_behind"),
    [
        ("[GNUPG:] ERRSIG 99806982213FFEC7 22 8 00 {future} 6 {fpr}", True),
        ("[GNUPG:] ERRSIG 99806982213FFEC7 22 8 00 {future} 6", True),
        ("[GNUPG:] ERRSIG 99806982213FFEC7 22 8 00 {past} 6 {fpr}", False),
        ("[GNUPG:] ERRSIG 99806982213FFEC7 22 8 00 {future} 9 -", False),
        ("[GNUPG:] ERRSIG 99806982213FFEC7 22 8 00 {future} 4 -", False),
        ("[GNUPG:] ERRSIG 99806982213FFEC7 22 8 00 20271103T094325 6 {fpr}", False),
        ("[GNUPG:] ERRSIG 99806982213FFEC7 22 8 00", False),
        ("[GNUPG:] BADSIG 99806982213FFEC7 {future}", False),
    ],
)
def test_errsig_status_parsing(
    env: dict[str, object], tmp_path: Path, line: str, clock_behind: bool
) -> None:
    now = int(time.time())
    text = line.format(future=now + 86400, past=now - 86400, fpr="A" * 40)
    fake = tmp_path / "fake-gpgv-errsig"
    fake.write_text(f"#!/bin/sh\necho '{text}'\nexit 2\n", encoding="utf-8")
    fake.chmod(0o755)
    res = _verifier(env, gpgv_path=fake).verify_detached(
        Path(str(env["data"])), Path(str(env["sig_release"]))
    )
    assert not res.ok
    assert (res.code == verify.CLOCK_BEHIND) is clock_behind


def test_errsig_with_zero_exit_is_not_accepted(env: dict[str, object], tmp_path: Path) -> None:
    fake = tmp_path / "fake-gpgv-errsig-0"
    fake.write_text(
        f"#!/bin/sh\necho '[GNUPG:] ERRSIG 1 22 8 00 {int(time.time()) + 86400} 6'\nexit 0\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    res = _verifier(env, gpgv_path=fake).verify_detached(
        Path(str(env["data"])), Path(str(env["sig_release"]))
    )
    assert not res.ok and "VALIDSIG" in res.reason


def test_load_builtin_passes_clock_behind(future: dict[str, object], tmp_path: Path) -> None:
    """Встроенный каталог: причина «часы отстают» доходит до CatalogError."""
    from astra_voice.models.catalog import CatalogError, load_builtin

    (tmp_path / "catalog.json").write_bytes(Path(str(future["data"])).read_bytes())
    (tmp_path / "catalog.json.sig").write_bytes(Path(str(future["sig"])).read_bytes())
    verifier = Verifier(
        "catalog", keyring=Path(str(future["keyring"])), pinned=frozenset({str(future["fpr"])})
    )
    with pytest.raises(CatalogError) as error:
        load_builtin(verifier, root=tmp_path)
    assert error.value.code == "clock-behind"
    assert "часы компьютера отстают" in error.value.message
