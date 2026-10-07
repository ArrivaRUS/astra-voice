"""Independent MN-15/MN-17/MN-22 boundaries, using tiny fake build inputs.

ci-require-imports: skip-file
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def gate() -> ModuleType:
    return _load("independent_bundle_gate", ROOT / "packaging/appimage/check_bundle.py")


@pytest.mark.parametrize("library", ["ssl", "crypto"])
@pytest.mark.parametrize("version", ["1.1", "1.0.0", "4"])
def test_extra_openssl_version_rejected_even_if_deleted(
    tmp_path: Path, gate: ModuleType, library: str, version: str
) -> None:
    appdir = tmp_path / "AppDir"
    appdir.mkdir()
    host = tmp_path / "host"
    host.mkdir()
    files = {"_ssl": str(appdir / "_ssl.so"), "_hashlib": str(appdir / "_hashlib.so")}

    def maps(path: str) -> str:
        return f"7f0000-7f1000 r-xp 00000000 fd:01 12 {path}"

    loaded = [maps(str(host / f"lib{name}.so.3")) for name in ("ssl", "crypto")]
    assert (
        gate.check_openssl(appdir, 3, "host", (3, 0, 17), "\n".join(loaded), files, [str(host)])
        == []
    )
    extra = f"lib{library}.so.{version}"
    loaded.append(maps(str(host / extra) + " (deleted)"))
    problems = gate.check_openssl(
        appdir, 3, "host", (3, 0, 17), "\n".join(loaded), files, [str(host)]
    )
    assert problems
    assert any(extra in problem for problem in problems)


@pytest.mark.parametrize("name", ["QSslSocket", "QNetworkAccessManager", "QNetworkRequest"])
def test_qt_tls_alias_imports_rejected(tmp_path: Path, gate: ModuleType, name: str) -> None:
    source = tmp_path / "future_network.py"
    source.write_text(f"from PyQt5.QtNetwork import {name} as InnocentName\n", encoding="utf-8")
    assert gate.check_qt_tls(tmp_path)


def test_local_qt_ipc_and_tls_comments_are_allowed(tmp_path: Path, gate: ModuleType) -> None:
    (tmp_path / "ipc.py").write_text(
        "from PyQt5.QtNetwork import QLocalSocket, QLocalServer\n"
        "# QSslSocket/QNetworkAccessManager/QNetworkRequest are forbidden.\n"
        "socket = QLocalSocket()\nserver = QLocalServer()\n",
        encoding="utf-8",
    )
    assert gate.check_qt_tls(tmp_path) == []


def test_qt_tls_gate_does_not_accept_unparseable_source(tmp_path: Path, gate: ModuleType) -> None:
    (tmp_path / "future_network.py").write_text("if invalid syntax:\n", encoding="utf-8")
    assert gate.check_qt_tls(tmp_path)


@pytest.mark.parametrize(
    "body",
    [
        "raise RuntimeError('module exists but is broken')\n",
        "raise SystemExit(0)\n",
        "import os\nos._exit(0)\n",
        "import missing_transitive_test_dependency\n",
    ],
    ids=["exception", "system-exit-zero", "process-exit-zero", "transitive-import"],
)
def test_import_gate_rejects_existing_but_unusable_modules(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    guard = _load("independent_import_guard", ROOT / "scripts/ci_require_imports.py")
    (tmp_path / "independent_bad_module.py").write_text(body, encoding="utf-8")
    tests = tmp_path / "tests"
    tests.mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    assert importlib.util.find_spec("independent_bad_module") is not None
    assert guard.main([str(tests), "--also", "independent_bad_module"]) == 1


def test_import_executes_in_child_and_preserves_parent_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    guard = _load("independent_import_guard", ROOT / "scripts/ci_require_imports.py")
    marker = tmp_path / "actually-imported"
    (tmp_path / "independent_good_module.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "os.environ['INDEPENDENT_IMPORT_CHILD_ONLY'] = 'changed'\n"
        f"Path({str(marker)!r}).write_text('import executed')\n",
        encoding="utf-8",
    )
    tests = tmp_path / "tests"
    tests.mkdir()
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.delenv("INDEPENDENT_IMPORT_CHILD_ONLY", raising=False)
    assert guard.main([str(tests), "--also", "independent_good_module"]) == 0
    assert marker.read_text() == "import executed"
    assert "INDEPENDENT_IMPORT_CHILD_ONLY" not in os.environ
    assert "independent_good_module" not in sys.modules


@pytest.mark.parametrize("attack", ["cache-change", "staged-change", "signature-failure"])
def test_build_uses_verified_copies_before_any_foreign_execution(
    tmp_path: Path, attack: str
) -> None:
    """Run the real build CLI, stopping at a harmless fake Python-image shell script."""
    root = tmp_path / "repo"
    here = root / "packaging/appimage"
    (here / "keys").mkdir(parents=True)
    shutil.copyfile(ROOT / "packaging/appimage/build.sh", here / "build.sh")
    (here / "ENABLED").touch()
    (here / "keys/appimage-runtime.gpg").write_bytes(b"test key placeholder")
    (root / "packaging/debian").mkdir()
    (root / "packaging/debian/changelog").write_text("astra-voice (0.2.0) unstable\n")
    (root / "packaging/appimage.lock").touch()
    cache = tmp_path / "cache"
    downloads = cache / "downloads"
    downloads.mkdir(parents=True)
    (cache / "wheels").mkdir()
    record = tmp_path / "record.jsonl"
    image = b'#!/bin/sh\nprintf "EXEC:%s\\n" "$0" >> "$INDEPENDENT_BUILD_RECORD"\nexit 91\n'
    contents = {
        "runtime-x86_64": b"original runtime",
        "runtime-x86_64.sig": b"test signature placeholder",
        "appimagetool-x86_64.AppImage": b"unused tool",
        "python3.11-test.AppImage": image,
    }
    records = []
    for name, data in contents.items():
        (downloads / name).write_bytes(data)
        (downloads / name).chmod(0o755)
        records.append(f"{name} {hashlib.sha256(data).hexdigest()} {len(data)} https://invalid")
    (here / "lockfile.py").write_text(
        "import sys\nargs=sys.argv[2:]\n"
        "if args == ['get','base']: print('python-appimage')\n"
        "elif args == ['get','runtime-key']: print('TESTFPR')\n"
        f"elif args == ['tools']: print({chr(10).join(records)!r})\n",
        encoding="utf-8",
    )
    (here / "debverify.py").write_text(
        "import json, os, stat, sys\nfrom pathlib import Path\n"
        "signature, runtime=map(Path,sys.argv[-2:])\n"
        "entry={'signature':str(signature),'runtime':str(runtime),"
        "'content':runtime.read_bytes().decode(),"
        "'parent_mode':stat.S_IMODE(runtime.parent.stat().st_mode)}\n"
        "with Path(os.environ['INDEPENDENT_BUILD_RECORD']).open('a') as log: "
        "log.write(json.dumps(entry)+'\\n')\n"
        "sys.exit(1 if os.environ['INDEPENDENT_BUILD_ATTACK']=='signature-failure' else 0)\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cp = bin_dir / "cp"
    cp.write_text(
        '#!/bin/sh\n/usr/bin/cp "$@" || exit\n'
        'case "$2" in */downloads/runtime-x86_64)\n'
        '    case "$INDEPENDENT_BUILD_ATTACK" in\n'
        '        cache-change) printf changed > "$2" ;;\n'
        '        staged-change) printf changed > "$3" ;;\n'
        "    esac ;;\nesac\n",
        encoding="ascii",
    )
    cp.chmod(0o755)
    work = tmp_path / "work"
    stale = work / "build/tools"
    stale.mkdir(parents=True)
    (stale / "untrusted-leftover").write_bytes(b"stale build input")
    env = {
        **os.environ,
        "PATH": str(bin_dir) + os.pathsep + os.environ["PATH"],
        "ASTRA_VOICE_APPIMAGE_CACHE": str(cache),
        "ASTRA_VOICE_APPIMAGE_WORK": str(work),
        "ASTRA_VOICE_APPIMAGE_OUT": str(tmp_path / "dist"),
        "ASTRA_VOICE_WHEEL_CACHE": str(tmp_path / "unused-wheels"),
        "SOURCE_DATE_EPOCH": "1790000000",
        "INDEPENDENT_BUILD_RECORD": str(record),
        "INDEPENDENT_BUILD_ATTACK": attack,
    }
    proc = subprocess.run(
        ["bash", str(here / "build.sh")],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
    )
    lines = record.read_text().splitlines() if record.exists() else []
    executions = [line for line in lines if line.startswith("EXEC:")]
    if attack == "staged-change":
        assert proc.returncode == 1, proc.stderr
        assert "копия инструмента" in proc.stderr
        assert executions == []
        assert lines == []  # Bad hash must stop before signature checking too.
    else:
        entry = json.loads(next(line for line in lines if not line.startswith("EXEC:")))
        assert entry["runtime"] == str(work / "build/tools/runtime-x86_64")
        assert entry["signature"] == str(work / "build/tools/runtime-x86_64.sig")
        assert entry["content"] == "original runtime"
        assert entry["parent_mode"] == 0o700
        if attack == "signature-failure":
            assert proc.returncode == 1
            assert executions == []
        else:
            assert proc.returncode == 91, proc.stderr
            assert executions == [f"EXEC:{work}/build/tools/python3.11-test.AppImage"]
            assert (downloads / "runtime-x86_64").read_bytes() == b"changed"
    assert not stale.exists()  # EXIT cleanup removes private copies, including leftovers.
