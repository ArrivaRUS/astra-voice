"""Гейт состава бандла, «Ревизия 3» (R3.4; T-184, T-186): OpenSSL с хоста, запрещённые
файлы, ссылки, разрешение DT_NEEDED. Настоящего бандла нет — ELF собираются здесь же
(минимальные ELF64 с PT_DYNAMIC), карта памяти процесса подаётся текстом."""

from __future__ import annotations

import importlib.util
import os
import shutil
import struct
import subprocess
import sys
import types
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


gate = load("appimage_check_bundle_r3", ROOT / "packaging" / "appimage" / "check_bundle.py")


def make_elf(
    path: Path,
    needed: tuple[str, ...] = (),
    rpath: str | None = None,
    runpath: str | None = None,
    e_type: int = 3,
) -> Path:
    """Минимальный ELF64 LE: один PT_LOAD (vaddr = смещение) и PT_DYNAMIC."""
    strtab = b"\0"

    def add(text: str) -> int:
        nonlocal strtab
        offset = len(strtab)
        strtab += text.encode() + b"\0"
        return offset

    entries = [(1, add(name)) for name in needed]
    if rpath is not None:
        entries.append((15, add(rpath)))
    if runpath is not None:
        entries.append((29, add(runpath)))
    strtab_off = 64 + 2 * 56
    dyn_off = (strtab_off + len(strtab) + 7) // 8 * 8
    entries += [(5, strtab_off), (10, len(strtab)), (0, 0)]
    total = dyn_off + 16 * len(entries)
    ident = b"\x7fELF" + bytes([2, 1, 1]) + bytes(9)
    header = ident + struct.pack("<HHIQQQIHHHHHH", e_type, 62, 1, 0, 64, 0, 0, 64, 56, 2, 64, 0, 0)
    load_ph = struct.pack("<IIQQQQQQ", 1, 5, 0, 0, 0, total, total, 0x1000)
    dyn_ph = struct.pack("<IIQQQQQQ", 2, 6, dyn_off, dyn_off, dyn_off, total - dyn_off, 0, 8)
    body = header + load_ph + dyn_ph + strtab
    body += bytes(dyn_off - len(body)) + b"".join(struct.pack("<qQ", t, v) for t, v in entries)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return path


# --- разбор ELF ---------------------------------------------------------------------


def test_read_dynamic_synthetic(tmp_path: Path) -> None:
    elf = make_elf(tmp_path / "a.so", ("libc.so.6", "libfoo.so.1"), runpath="$ORIGIN/lib:/x")
    info = gate.read_dynamic(elf)
    assert info == gate.Dynamic(("libc.so.6", "libfoo.so.1"), (), ("$ORIGIN/lib", "/x"))
    elf = make_elf(tmp_path / "b.so", ("libz.so.1",), rpath="$ORIGIN")
    assert gate.read_dynamic(elf) == gate.Dynamic(("libz.so.1",), ("$ORIGIN",), ())
    assert gate.read_dynamic(make_elf(tmp_path / "c.o", ("x",), e_type=1)) == gate.Dynamic(())
    text = tmp_path / "t.py"
    text.write_text("print()\n", "utf-8")
    assert gate.read_dynamic(text) is None
    broken = tmp_path / "broken.so"
    broken.write_bytes(b"\x7fELF" + bytes([1, 1]) + bytes(58))
    with pytest.raises(gate.ElfError, match="не ELF64"):
        gate.read_dynamic(broken)


@pytest.mark.skipif(shutil.which("readelf") is None, reason="нет readelf")
def test_read_dynamic_matches_readelf_on_host_binary() -> None:
    binary = Path(os.path.realpath(sys.executable))
    info = gate.read_dynamic(binary)
    assert info is not None
    out = subprocess.run(["readelf", "-d", str(binary)], capture_output=True, text=True).stdout
    needed = [line.split("[", 1)[1].rstrip("]") for line in out.splitlines() if "(NEEDED)" in line]
    assert list(info.needed) == needed


# --- DT_NEEDED (T-186) ----------------------------------------------------------------


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    appdir, host = tmp_path / "AppDir", tmp_path / "hostlib"
    appdir.mkdir()
    host.mkdir()
    (host / "libc.so.6").write_bytes(b"")
    return appdir, host


def needed(appdir: Path, host: Path, sonames: tuple[str, ...] = ("libc.so.6",)) -> list[str]:
    count, problems = gate.check_needed(appdir, {s: "pkg" for s in sonames}, [str(host)])
    assert count > 0
    return list(problems)


def test_needed_resolves_inside_and_on_host(tree: tuple[Path, Path]) -> None:
    appdir, host = tree
    make_elf(appdir / "ext.so", ("libfoo.so.1", "libc.so.6"), runpath="$ORIGIN/lib")
    make_elf(appdir / "lib" / "libfoo.so.1", ("libc.so.6",), runpath="$ORIGIN")
    assert needed(appdir, host) == []


def test_dangling_and_missing_host_library(tree: tuple[Path, Path]) -> None:
    appdir, host = tree
    make_elf(appdir / "ext.so", ("libreadline.so.8", "libssl.so.3", "libc.so.6"))
    problems = needed(appdir, host, ("libc.so.6", "libssl.so.3", "libunused.so.1"))
    assert problems == [
        "ext.so: висячий DT_NEEDED libreadline.so.8",
        "host-lib libssl.so.3 не найдена на машине проверки",
        "host-lib libunused.so.1 не найдена на машине проверки",
        "host-lib libunused.so.1 не нужна ни одному ELF — убрать из lock",
    ]


def test_rpath_is_inherited_runpath_is_not(tree: tuple[Path, Path]) -> None:
    """Как numpy.libs: libopenblas без путей находит libgfortran по RPATH загрузчика."""
    appdir, host = tree
    make_elf(appdir / "pkg" / "ext.so", ("liba.so",), rpath="$ORIGIN/../libs")
    make_elf(appdir / "libs" / "liba.so", ("libb.so",))
    make_elf(appdir / "libs" / "libb.so", ("libc.so.6",))
    assert needed(appdir, host) == []
    make_elf(appdir / "pkg" / "ext.so", ("liba.so",), runpath="$ORIGIN/../libs")
    assert needed(appdir, host) == [
        "host-lib libc.so.6 не нужна ни одному ELF — убрать из lock",
        "libs/liba.so: висячий DT_NEEDED libb.so",
    ]


def test_runpath_outside_appdir_and_unknown_token(tree: tuple[Path, Path]) -> None:
    appdir, host = tree
    make_elf(host / "libevil.so.1")
    make_elf(appdir / "ext.so", ("libevil.so.1",), runpath=str(host))
    make_elf(appdir / "other.so", ("libc.so.6",), runpath="$LIB/x")
    assert needed(appdir, host) == [
        f"ext.so: libevil.so.1 найден вне AppDir: {host / 'libevil.so.1'}",
        "host-lib libc.so.6 не нужна ни одному ELF — убрать из lock",
        "other.so: неподдерживаемая подстановка в RPATH/RUNPATH",
    ]


def test_read_host_libs(tmp_path: Path) -> None:
    path = tmp_path / "host-libs.txt"
    path.write_text("libz.so.1 zlib1g\nlibssl.so.3 libssl3\n", "utf-8")
    assert gate.read_host_libs(path) == {"libz.so.1": "zlib1g", "libssl.so.3": "libssl3"}
    path.write_text("libz.so.1\n", "utf-8")
    with pytest.raises(ValueError, match="неверная строка"):
        gate.read_host_libs(path)
    path.write_text("", "utf-8")
    with pytest.raises(ValueError, match="пустой манифест"):
        gate.read_host_libs(path)


# --- запрещённые файлы и ссылки --------------------------------------------------------


def test_forbidden_files(tmp_path: Path) -> None:
    dynload = tmp_path / "opt" / "python3.11" / "lib" / "python3.11" / "lib-dynload"
    dynload.mkdir(parents=True)
    for name in (
        "readline.cpython-311-x86_64-linux-gnu.so",
        "_dbm.cpython-311-x86_64-linux-gnu.so",
        "_gdbm.cpython-311-x86_64-linux-gnu.so",
        "_ssl.cpython-311-x86_64-linux-gnu.so",
        "_sqlite3.cpython-311-x86_64-linux-gnu.so",
    ):
        (dynload / name).write_bytes(b"")
    lib = tmp_path / "usr" / "lib"
    lib.mkdir(parents=True)
    for name in ("libssl.so.3", "libcrypto.so.1.1", "libdb-5.3.so", "libffi.so.8"):
        (lib / name).write_bytes(b"")
    host = [p.split(": ", 1)[1].rsplit("/", 1)[1] for p in gate.check_forbidden(tmp_path, "host")]
    assert sorted(host) == sorted(
        [
            "readline.cpython-311-x86_64-linux-gnu.so",
            "_dbm.cpython-311-x86_64-linux-gnu.so",
            "_gdbm.cpython-311-x86_64-linux-gnu.so",
            "libssl.so.3",
            "libcrypto.so.1.1",
            "libdb-5.3.so",
        ]
    )
    bundled = gate.check_forbidden(tmp_path, "bundled")
    assert not any("libssl" in p or "libcrypto" in p for p in bundled)
    assert len(bundled) == 4


def test_symlinks(tmp_path: Path) -> None:
    lib = tmp_path / "AppDir" / "opt" / "python3.11" / "lib"
    (lib / "python3.11" / "site-packages").mkdir(parents=True)
    (lib / "python3").mkdir()
    (lib / "python3" / "dist-packages").symlink_to("../python3.11/site-packages")
    appdir = tmp_path / "AppDir"
    assert gate.check_symlinks(appdir) == []
    (lib / "python3.11" / "sitecustomize.py").symlink_to("/etc/python3.11/sitecustomize.py")
    (lib / "python3.11" / "escape").symlink_to("../../../../../outside")
    assert gate.check_symlinks(appdir) == [
        "ссылка opt/python3.11/lib/python3.11/escape -> ../../../../../outside выходит из AppDir",
        "абсолютная ссылка opt/python3.11/lib/python3.11/sitecustomize.py -> "
        "/etc/python3.11/sitecustomize.py",
    ]


# --- OpenSSL (T-184) --------------------------------------------------------------------


def maps_line(path: str) -> str:
    return f"7f0000000000-7f0000001000 r-xp 00000000 fd:01 123 {path}"


HOST_MAPS = "\n".join(
    [
        "55d000000000-55d000001000 r--p 00000000 fd:01 1 /tmp/AppDir/opt/python3.11/bin/python3.11",
        maps_line("/usr/lib/x86_64-linux-gnu/libcrypto.so.3"),
        maps_line("/usr/lib/x86_64-linux-gnu/libssl.so.3"),
        maps_line("/usr/lib/x86_64-linux-gnu/libssl.so.3"),
        "7ffd00000000-7ffd00001000 rw-p 00000000 00:00 0 [stack]",
    ]
)


@pytest.fixture
def ssl_appdir(tmp_path: Path) -> tuple[Path, dict[str, str | None]]:
    appdir = tmp_path / "AppDir"
    dynload = appdir / "opt" / "python3.11" / "lib" / "python3.11" / "lib-dynload"
    dynload.mkdir(parents=True)
    files: dict[str, str | None] = {}
    for name in ("_ssl", "_hashlib"):
        path = dynload / f"{name}.cpython-311-x86_64-linux-gnu.so"
        path.write_bytes(b"")
        files[name] = str(path)
    return appdir, files


def test_openssl_from_host_accepted(ssl_appdir: tuple[Path, dict[str, str | None]]) -> None:
    appdir, files = ssl_appdir
    assert gate.loaded_openssl(HOST_MAPS) == {
        "libcrypto.so.3": {"/usr/lib/x86_64-linux-gnu/libcrypto.so.3"},
        "libssl.so.3": {"/usr/lib/x86_64-linux-gnu/libssl.so.3"},
    }
    assert gate.check_openssl(appdir, 3, "host", (3, 0, 17, 0, 0), HOST_MAPS, files) == []


def test_openssl_violations(ssl_appdir: tuple[Path, dict[str, str | None]]) -> None:
    appdir, files = ssl_appdir
    old = gate.check_openssl(appdir, 3, "host", (1, 1, 1, 11, 15), HOST_MAPS, files)
    assert old == ["OpenSSL (1, 1, 1, 11, 15): ожидалась старшая версия 3"]
    bundled = "\n".join(
        [
            maps_line(f"{appdir}/usr/lib/libssl.so.3"),
            maps_line("/opt/weird/libcrypto.so.3"),
        ]
    )
    problems = gate.check_openssl(appdir, 3, "host", (3, 0, 0), bundled, files)
    assert problems == [
        "libcrypto.so.3 загружена не из системного каталога: /opt/weird/libcrypto.so.3",
        f"libssl.so.3 загружена из бандла: {appdir}/usr/lib/libssl.so.3",
    ]
    missing = gate.check_openssl(appdir, 3, "host", (3, 0, 0), maps_line("/lib/libssl.so.3"), files)
    assert missing == ["libcrypto.so.3 не загружена в процесс (нет в /proc/self/maps)"]
    foreign = {**files, "_ssl": "/usr/lib/python3.11/lib-dynload/_ssl.so", "_hashlib": None}
    problems = gate.check_openssl(appdir, 3, "host", (3, 0, 0), HOST_MAPS, foreign)
    assert problems == [
        "_ssl: модуль не из бандла: /usr/lib/python3.11/lib-dynload/_ssl.so",
        "_hashlib: модуль не из бандла: None",
    ]


def test_openssl_bundled_rollback(ssl_appdir: tuple[Path, dict[str, str | None]]) -> None:
    """Откат (г): OpenSSL 1.1 из бандла; хостовая копия — нарушение."""
    appdir, files = ssl_appdir
    inside = maps_line(f"{appdir}/usr/lib/libssl.so.1.1")
    assert gate.check_openssl(appdir, 1, "bundled", (1, 1, 1), inside, files) == []
    outside = maps_line("/usr/lib/x86_64-linux-gnu/libssl.so.1.1")
    assert gate.check_openssl(appdir, 1, "bundled", (1, 1, 1), outside, files) == [
        "libssl.so.1.1 загружена не из бандла: /usr/lib/x86_64-linux-gnu/libssl.so.1.1"
    ]


# --- префиксы и sitecustomize ------------------------------------------------------------


def test_prefixes_and_sitecustomize(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    appdir = tmp_path / "AppDir"
    prefix = appdir / "opt" / "python3.11"
    prefix.mkdir(parents=True)
    for name in ("prefix", "exec_prefix", "base_prefix", "base_exec_prefix"):
        monkeypatch.setattr(sys, name, str(prefix))
    monkeypatch.delitem(sys.modules, "sitecustomize", raising=False)
    monkeypatch.delitem(sys.modules, "usercustomize", raising=False)
    assert gate.check_prefixes(appdir) == []
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    host_hook = types.ModuleType("sitecustomize")
    host_hook.__file__ = "/etc/python3.11/sitecustomize.py"
    monkeypatch.setitem(sys.modules, "sitecustomize", host_hook)
    assert gate.check_prefixes(appdir) == [
        "sys.base_prefix вне бандла: /usr",
        "sitecustomize подключён не из бандла: /etc/python3.11/sitecustomize.py",
    ]
