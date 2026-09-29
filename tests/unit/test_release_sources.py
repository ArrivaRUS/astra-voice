"""Архив исходников выпуска AppImage (R3.6): сборка из lock и кэша, потоковая проверка."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

import pytest

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(shutil.which("git") is None, reason="нужен git"),
]
ROOT = Path(__file__).resolve().parents[2]


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


rs = load("release_sources_under_test", ROOT / "scripts" / "release_sources.py")
SHA = "a" * 64


def _lock(sources: dict[str, bytes]) -> str:
    lines = [
        "# base: python-appimage",
        "# openssl-origin: bundled",
        "# openssl-major: 1",
        "# expect-elf: 1",
        "# max-glibc: 2.28",
        "# runtime-key: 570C77ACEA40C0F1B758902CBF96CCA56490F695",
        f"# tool: runtime-x86_64 {SHA} 1 https://example.invalid/runtime-x86_64",
        f"# tool: runtime-x86_64.sig {SHA} 2 https://example.invalid/runtime-x86_64.sig",
        f"# tool: appimagetool-x86_64.AppImage {SHA} 3 https://example.invalid/appimagetool",
        f"# tool: python3.11.16-cp311-cp311-manylinux_2_28_x86_64.AppImage {SHA} 4 "
        "https://example.invalid/py",
    ]
    for ident, data in sources.items():
        record = {
            "id": ident,
            "file": f"{ident}.tar.gz",
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
            "url": f"https://example.invalid/{ident}.tar.gz",
        }
        lines.append("# source: " + json.dumps(record, separators=(",", ":")))
    return "\n".join(lines) + "\nnumpy==1.24.2 \\\n    --hash=sha256:" + SHA + "\n"


@pytest.fixture
def repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    root = tmp_path / "repo"
    (root / "packaging" / "appimage").mkdir(parents=True)
    for name in ("lockfile.py", "debverify.py"):
        shutil.copy2(ROOT / "packaging" / "appimage" / name, root / "packaging" / "appimage")
    sources = {"qtbase-5.15.19": b"qt source", "libfuse-3.15.0": b"fuse source"}
    lock = root / "packaging" / "appimage.lock"
    lock.write_text(_lock(sources), "utf-8")
    (root / "README.md").write_text("код\n", "utf-8")
    cache = tmp_path / "cache"
    (cache / "sources").mkdir(parents=True)
    for ident, data in sources.items():
        (cache / "sources" / f"{ident}.tar.gz").write_bytes(data)
    env = ["-c", "user.name=t", "-c", "user.email=t@test.invalid"]
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True)
    subprocess.run(["git", *env, "-C", str(root), "commit", "-qm", "init"], check=True)
    return root, cache, lock


def test_build_is_complete_reproducible_and_checked(
    repo: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    root, cache, lock = repo
    out = tmp_path / "dist" / "astra-voice-0.2.0-sources.tar.xz"
    sha = rs.build(lock, cache, root, "0.2.0", out, 1790000000)
    assert rs.check(out) == (sha, 4)
    again = tmp_path / "again.tar.xz"
    assert rs.build(lock, cache, root, "0.2.0", again, 1790000000) == sha
    with tarfile.open(out) as tar:
        names = tar.getnames()
        manifest = tar.extractfile(names[0]).read().decode()  # type: ignore[union-attr]
        member = tar.getmember(names[1])
    assert names[0] == "astra-voice-0.2.0-sources/MANIFEST.txt"
    assert "astra-voice-0.2.0-sources/upstream/qtbase-5.15.19/qtbase-5.15.19.tar.gz" in names
    assert "https://example.invalid/libfuse-3.15.0.tar.gz" in manifest
    assert (member.uid, member.mtime, member.mode) == (0, 1790000000, 0o644)
    assert rs.check(out, top="astra-voice-0.2.0-sources")[1] == 4
    with pytest.raises(rs.SourcesError, match="вне каталога astra-voice-0.3.0-sources"):
        rs.check(out, top="astra-voice-0.3.0-sources")
    with tarfile.open(out) as tar:
        readme = tar.extractfile("astra-voice-0.2.0-sources/README.txt").read().decode()  # type: ignore[union-attr]
    assert "qtbase-5.15.19: https://example.invalid/qtbase-5.15.19.tar.gz" in readme
    code = next(n for n in names if "/astra-voice/" in n)
    with tarfile.open(out) as tar:
        inner = tarfile.open(fileobj=tar.extractfile(code))
        assert "astra-voice-0.2.0/packaging/appimage.lock" in inner.getnames()


def test_build_refuses_dirty_tree_and_changed_cache(
    repo: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    root, cache, lock = repo
    out = tmp_path / "s.tar.xz"
    (root / "README.md").write_text("правка\n", "utf-8")
    with pytest.raises(rs.SourcesError, match="рабочее дерево не чистое"):
        rs.build(lock, cache, root, "0.2.0", out, 0)
    subprocess.run(["git", "-C", str(root), "checkout", "-q", "README.md"], check=True)
    (cache / "sources" / "qtbase-5.15.19.tar.gz").write_bytes(b"other")
    with pytest.raises(rs.SourcesError, match="исходники не прошли проверку"):
        rs.build(lock, cache, root, "0.2.0", out, 0)
    assert not out.exists()


def _archive(path: Path, members: Sequence[tuple[str, bytes | None]]) -> Path:
    with tarfile.open(path, "w:xz") as tar:
        for name, data in members:
            info = tarfile.TarInfo(name)
            if data is None:
                info.type = tarfile.SYMTYPE
                info.linkname = "/etc/passwd"
                tar.addfile(info)
            else:
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
    return path


def _manifest(files: dict[str, bytes]) -> bytes:
    return "".join(
        f"{hashlib.sha256(d).hexdigest()}\t{len(d)}\t{p}\thttps://x.invalid\n"
        for p, d in files.items()
    ).encode()


GOOD = {"README.txt": b"r", "astra-voice/a.tar": b"code", "upstream/q/q.tar.gz": b"q"}


@pytest.mark.parametrize(
    ("members", "message"),
    [
        ([("t/README.txt", b"r")], "первым в архиве должен идти MANIFEST.txt"),
        ([("t/MANIFEST.txt", _manifest(GOOD)), ("t/../x", b"r")], "недопустимый путь"),
        ([("t/MANIFEST.txt", _manifest(GOOD)), ("t/README.txt", None)], "не обычный файл"),
        ([("t/MANIFEST.txt", _manifest(GOOD)), ("u/README.txt", b"r")], "вне каталога t"),
        ([("t/MANIFEST.txt", _manifest(GOOD)), ("t/README.txt", b"R")], "не совпал с манифестом"),
        ([("t/MANIFEST.txt", _manifest(GOOD)), ("t/extra", b"x")], "файла нет в манифесте"),
        ([("t/MANIFEST.txt", _manifest(GOOD)), ("t/README.txt", b"r")], "нет файлов из манифеста"),
        ([("t/MANIFEST.txt", b"broken line\n")], "неверная строка манифеста"),
        (
            [("t/MANIFEST.txt", _manifest({"README.txt": b"r"})), ("t/README.txt", b"r")],
            "нет README.txt или исходников программы",
        ),
    ],
)
def test_check_rejects(
    tmp_path: Path, members: list[tuple[str, bytes | None]], message: str
) -> None:
    path = _archive(tmp_path / "a.tar.xz", members)
    with pytest.raises(rs.SourcesError, match=message):
        rs.check(path)


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        (b"\xff\xfe\n", "MANIFEST.txt не в UTF-8"),
        (f"{SHA}\t\u00b2\tREADME.txt\tx\n".encode(), "неверная строка манифеста"),
        (f"{'A' * 64}\t1\tREADME.txt\tx\n".encode(), "неверная строка манифеста"),
    ],
)
def test_check_rejects_malformed_manifest(tmp_path: Path, manifest: bytes, message: str) -> None:
    """Ревью А: неверные байты и «²» в манифесте — SourcesError, а не трейсбек."""
    path = _archive(tmp_path / "a.tar.xz", [("t/MANIFEST.txt", manifest), ("t/README.txt", b"r")])
    with pytest.raises(rs.SourcesError, match=message):
        rs.check(path)


def test_check_rejects_hardlink_and_huge_header(tmp_path: Path) -> None:
    good = [("t/MANIFEST.txt", _manifest(GOOD))] + [(f"t/{p}", d) for p, d in GOOD.items()]
    path = tmp_path / "hard.tar.xz"
    with tarfile.open(path, "w:xz") as tar:
        for name, data in good:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        link = tarfile.TarInfo("t/link")
        link.type = tarfile.LNKTYPE
        link.linkname = "t/README.txt"
        tar.addfile(link)
    with pytest.raises(rs.SourcesError, match="не обычный файл: t/link"):
        rs.check(path)
    huge = tmp_path / "huge.tar.xz"
    with tarfile.open(huge, "w:xz", format=tarfile.PAX_FORMAT) as tar:
        info = tarfile.TarInfo("t/MANIFEST.txt")
        info.pax_headers = {"comment": "x" * (100 << 10)}
        manifest = _manifest(GOOD)
        info.size = len(manifest)
        tar.addfile(info, io.BytesIO(manifest))
    with pytest.raises(rs.SourcesError, match="служебный заголовок tar"):
        rs.check(huge)


def test_check_limits_and_symlink(tmp_path: Path) -> None:
    good = [("t/MANIFEST.txt", _manifest(GOOD))] + [(f"t/{p}", d) for p, d in GOOD.items()]
    path = _archive(tmp_path / "a.tar.xz", good)
    assert rs.check(path)[1] == 3
    with pytest.raises(rs.SourcesError, match="больше предела"):
        rs.check(path, limit=10)
    link = tmp_path / "link.tar.xz"
    link.symlink_to(path)
    with pytest.raises(rs.SourcesError, match="не обычный файл"):
        rs.check(link)
    broken = tmp_path / "broken.tar.xz"
    broken.write_bytes(path.read_bytes()[:-40])
    with pytest.raises(rs.SourcesError, match="повреждён"):
        rs.check(broken)
