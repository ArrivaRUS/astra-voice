"""MN-14/15/17: pinned keys, host OpenSSL, Qt TLS and staged build tools."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "packaging/appimage"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate = _load("build_hardening_bundle", HERE / "check_bundle.py")
lockfile = _load("build_hardening_lock", HERE / "lockfile.py")
KEYS = [
    (
        "appimage-runtime.gpg",
        "43ac3cad320541ec2e849ebb68347ddb36b7cce1b4a03774ff7b39de79a251b3",
        "570C77ACEA40C0F1B758902CBF96CCA56490F695",
    ),
    (
        "debian-archive-bookworm-automatic.gpg",
        "59dbde1397f8edc4e4aa24829ba36f9583ea5b4480091c34b89dad9e56360a19",
        "B8B80B5B623EAB6AD8775C45B7C5D7D6350947F8",
    ),
]


@pytest.mark.parametrize(("name", "sha", "fingerprint"), KEYS)
def test_keyring_bytes_are_pinned(name: str, sha: str, fingerprint: str) -> None:
    assert hashlib.sha256((HERE / "keys" / name).read_bytes()).hexdigest() == sha


@pytest.mark.skipif(shutil.which("gpg") is None, reason="нужен gpg")
@pytest.mark.parametrize(("name", "sha", "fingerprint"), KEYS)
def test_real_key_fingerprint_matches_real_lock(
    tmp_path: Path, name: str, sha: str, fingerprint: str
) -> None:
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    result = subprocess.run(
        [
            "gpg",
            "--homedir",
            str(home),
            "--batch",
            "--with-colons",
            "--show-keys",
            str(HERE / "keys" / name),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    )
    records = [line.split(":") for line in result.stdout.splitlines()]
    assert [r[9] for r in records if r[0] == "fpr"][0] == fingerprint
    assert len([r for r in records if r[0] == "pub"]) == 1
    lock = lockfile.load(ROOT / "packaging/appimage.lock")
    if name == "appimage-runtime.gpg":
        assert lock.runtime_key == fingerprint
    else:
        archive = next(a for a in lock.archives if a.id == "debian-bookworm")
        assert archive.signer == fingerprint
        assert ROOT / archive.keyring == HERE / "keys" / name
        expiry = int(next(r for r in records if r[0] == "pub")[6])
        assert datetime.fromtimestamp(expiry, UTC).date() == date(2031, 1, 19)


def test_debian_key_repin_reminder() -> None:
    assert datetime.now(UTC).date() < date(2030, 10, 1), (
        "Перепинить ключ Debian до истечения 2031-01-19; сверить ключ и lock."
    )


@pytest.mark.parametrize("extra", ["libssl.so.1.1", "libcrypto.so.1.1", "libssl.so.4"])
def test_loaded_additional_openssl_major_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, extra: str
) -> None:
    libs = ["libssl.so.3", "libcrypto.so.3", extra]
    monkeypatch.setattr(gate, "_soname", lambda path: None)
    maps = "\n".join(f"0-1 r--p 0 00:00 1 /lib/{name}" for name in libs)
    files = {"_ssl": str(tmp_path / "ssl.so"), "_hashlib": str(tmp_path / "hash.so")}
    problems = gate.check_openssl(tmp_path, 3, "host", (3, 0, 0), maps, files)
    assert problems == [f"{extra}: загружена лишняя версия OpenSSL, ожидалась 3"]


@pytest.mark.parametrize(
    "source",
    [
        "from PyQt5.QtNetwork import QSslSocket as Socket",
        "from PyQt5 import QtNetwork\nQtNetwork.QSslSocket()",
        "from PyQt5.QtNetwork import QNetworkAccessManager as Manager",
        "from PyQt5.QtNetwork import QNetworkRequest",
    ],
)
def test_qt_tls_code_is_rejected(tmp_path: Path, source: str) -> None:
    (tmp_path / "network.py").write_text(source, encoding="utf-8")
    assert gate.check_qt_tls(tmp_path)


def test_qt_local_ipc_and_comments_are_allowed(tmp_path: Path) -> None:
    (tmp_path / "ipc.py").write_text(
        "from PyQt5.QtNetwork import QLocalSocket, QLocalServer\n"
        '# QSslSocket is not allowed\nmessage = "QNetworkRequest"\n',
        encoding="utf-8",
    )
    assert gate.check_qt_tls(tmp_path) == []
    assert gate.check_qt_tls(ROOT / "src") == []


def _function(name: str) -> str:
    text = (HERE / "build.sh").read_text(encoding="utf-8")
    match = re.search(rf"^{name}\(\) \{{\n.*?^\}}", text, re.MULTILINE | re.DOTALL)
    assert match
    return match[0]


@pytest.mark.parametrize("tamper_copy", [False, True])
def test_tools_verified_and_used_from_build_copy(tmp_path: Path, tamper_copy: bool) -> None:
    cache = tmp_path / "cache"
    cache.mkdir()
    build = tmp_path / "build"
    build.mkdir()
    fake_here = tmp_path / "scripts"
    fake_here.mkdir()
    keyring = tmp_path / "key.gpg"
    keyring.touch()
    tool_data = {
        "appimagetool-x86_64.AppImage": b"#!/bin/sh\nprintf 'verified-tool'\n",
        "runtime-x86_64": b"verified-runtime",
        "runtime-x86_64.sig": b"signature",
    }
    for name, content in tool_data.items():
        (cache / name).write_bytes(content)
    pins = tmp_path / "pins"
    pins.write_text(
        "".join(
            f"{name} {hashlib.sha256(content).hexdigest()} {len(content)} https://invalid/{name}\n"
            for name, content in tool_data.items()
        ),
        encoding="ascii",
    )
    # The verifier records exactly which bytes it sees, then changes shared cache inputs.
    (fake_here / "debverify.py").write_text(
        "import json, os, pathlib, sys\n"
        "paths = [pathlib.Path(p) for p in sys.argv[-2:]]\n"
        "pathlib.Path(os.environ['REPORT']).write_text(json.dumps([str(p) for p in paths]))\n"
        "assert [p.read_bytes() for p in paths] == [b'signature', b'verified-runtime']\n"
        "cache = pathlib.Path(os.environ['CACHE'])\n"
        "(cache/'appimagetool-x86_64.AppImage').write_bytes(b'bad cache tool')\n"
        "(cache/'runtime-x86_64').write_bytes(b'bad cache runtime')\n",
        encoding="utf-8",
    )
    script = "\n".join(
        [
            "set -euo pipefail",
            'die() { echo "$*" >&2; exit 1; }',
            'lockq() { if [ "$1" = tools ]; then cat "$PINS"; else echo FINGERPRINT; fi; }',
            _function("file_ok"),
            _function("verify_runtime"),
            _function("stage_tools"),
        ]
    )
    if tamper_copy:
        script += '\ncp() { command cp "$@"; printf bad > "${@: -1}"; }\n'
    script += '\nstage_tools\n"$TOOL"\ncat "$RUNTIME"\n'
    env = dict(
        os.environ,
        DOWNLOADS=str(cache),
        CACHE=str(cache),
        BUILD=str(build),
        HERE=str(fake_here),
        RUNTIME_KEYRING=str(keyring),
        PINS=str(pins),
        REPORT=str(tmp_path / "report"),
    )
    result = subprocess.run(
        ["bash", "-c", script], env=env, capture_output=True, text=True, check=False, timeout=15
    )
    if tamper_copy:
        assert result.returncode == 1
        assert "копия инструмента не совпала с lock" in result.stderr
        assert not (tmp_path / "report").exists()
    else:
        assert result.returncode == 0, result.stderr
        assert result.stdout == "verified-toolverified-runtime"
        assert json.loads((tmp_path / "report").read_text()) == [
            str(build / "tools/runtime-x86_64.sig"),
            str(build / "tools/runtime-x86_64"),
        ]
        assert (build / "tools").stat().st_mode & 0o777 == 0o700
