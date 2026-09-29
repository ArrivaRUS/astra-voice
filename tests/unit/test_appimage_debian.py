"""Debian-входы AppImage («Ревизия 3» R3.3, T-185): записи lock и цепочка происхождения.

Архив синтетический: тестовый ключ во временном `GNUPGHOME`, свой `InRelease` (clearsign),
`Packages.xz`/`Sources.xz`, `.deb` из `dpkg-deb -b`. Сеть и `~/.gnupg` не используются.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import lzma
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
HERE = ROOT / "packaging" / "appimage"


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# debverify импортирует соседний модуль как `lockfile` (так он работает из build.sh).
lockfile = load("lockfile", HERE / "lockfile.py")
debverify = load("appimage_debverify", HERE / "debverify.py")

SHA = "a" * 64
VERSION = "3.11.2-6+deb12u8"
Q = VERSION.replace("+", "%2B")
BASE_URL = "https://snapshot.invalid/archive/debian/20260929T000000Z"
HEAD = f"""# base: debian12
# openssl-origin: host
# openssl-major: 3
# build-python: 3.11
# build-pip: 23.0.1
# expect-elf: 100
# max-glibc: 2.36
# runtime-key: 570C77ACEA40C0F1B758902CBF96CCA56490F695
# host-lib: libssl.so.3 libssl3
# host-lib: libcrypto.so.3 libssl3
# tool: runtime-x86_64 {SHA} 1 https://example.invalid/runtime-x86_64
# tool: runtime-x86_64.sig {SHA} 2 https://example.invalid/runtime-x86_64.sig
# tool: appimagetool-x86_64.AppImage {SHA} 3 https://example.invalid/appimagetool
numpy==1.24.2 \\
    --hash=sha256:{SHA}
"""


def rec(kind: str, data: dict[str, Any]) -> str:
    return f"# {kind}: " + json.dumps(data, ensure_ascii=False, separators=(",", ":"))


def records(**override: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Записи Debian для lock; sha/size — заглушки. В `override` значение None удаляет поле."""
    items: list[tuple[str, dict[str, Any]]] = [
        (
            "archive",
            {
                "id": "debian-bookworm",
                "keyring": "packaging/appimage/keys/test-archive.gpg",
                "signer": "B8B80B5B623EAB6AD8775C45B7C5D7D6350947F8",
                "release": f"{BASE_URL}/dists/bookworm/InRelease",
                "sha256": SHA,
                "size": 10,
                "codename": "bookworm",
                "suite": "oldstable",
            },
        ),
        (
            "index",
            {
                "id": "main-amd64",
                "archive": "debian-bookworm",
                "kind": "Packages",
                "file": "main/binary-amd64/Packages.xz",
                "size": 10,
                "sha256": SHA,
                "url": f"{BASE_URL}/dists/bookworm/main/binary-amd64/Packages.xz",
            },
        ),
        (
            "index",
            {
                "id": "main-sources",
                "archive": "debian-bookworm",
                "kind": "Sources",
                "file": "main/source/Sources.xz",
                "size": 10,
                "sha256": SHA,
                "url": f"{BASE_URL}/dists/bookworm/main/source/Sources.xz",
            },
        ),
    ]
    for package in lockfile.PYTHON_DEBS:
        items.append(
            (
                "deb",
                {
                    "package": package,
                    "version": VERSION,
                    "arch": "amd64",
                    "file": f"{package}_{VERSION}_amd64.deb",
                    "size": 10,
                    "sha256": SHA,
                    "url": f"{BASE_URL}/pool/main/p/python3.11/{package}_{Q}_amd64.deb",
                    "index": "main-amd64",
                    "source": "python3.11",
                    "source_version": VERSION,
                    "dsc": "py-dsc",
                },
            )
        )
    items += [
        (
            "source",
            {
                "id": "py-dsc",
                "file": f"python3.11_{VERSION}.dsc",
                "size": 10,
                "sha256": SHA,
                "url": f"{BASE_URL}/pool/main/p/python3.11/python3.11_{Q}.dsc",
                "index": "main-sources",
            },
        ),
        (
            "source",
            {
                "id": "py-orig",
                "file": "python3.11_3.11.2.orig.tar.gz",
                "size": 10,
                "sha256": SHA,
                "url": f"{BASE_URL}/pool/main/p/python3.11/python3.11_3.11.2.orig.tar.gz",
                "dsc": "py-dsc",
            },
        ),
    ]
    result = []
    for kind, data in items:
        key = data.get("id") or data.get("package")
        merged = {**data, **override.get(str(key), {})}
        result.append((kind, {k: v for k, v in merged.items() if v is not None}))
    return result


def lock_text(items: list[tuple[str, dict[str, Any]]], head: str = HEAD, extra: str = "") -> str:
    return head + "\n".join(rec(kind, data) for kind, data in items) + "\n" + extra


# --- разбор lock ---------------------------------------------------------------


def test_debian_lock_parses() -> None:
    lock = lockfile.parse(lock_text(records()))
    assert lock.base == "debian12"
    assert lock.openssl_major == 3
    assert [deb.package for deb in lock.debs] == list(lockfile.PYTHON_DEBS)
    assert lock.debs[0].cache_path == f"debs/python3.11-minimal_{VERSION}_amd64.deb"
    assert lock.indexes[0].cache_path == "metadata/debian-bookworm/main/binary-amd64/Packages.xz"
    assert lock.archives[0].cache_path == "metadata/debian-bookworm/InRelease"
    assert lock.source("py-orig").dsc == "py-dsc"
    paths = [item[0] for item in lock.fetch_list()]
    assert paths[:3] == [
        "downloads/runtime-x86_64",
        "downloads/runtime-x86_64.sig",
        "downloads/appimagetool-x86_64.AppImage",
    ]
    assert "sources/python3.11_3.11.2.orig.tar.gz" in paths
    assert len(paths) == 3 + 1 + 2 + 3 + 2


@pytest.mark.parametrize(
    ("override", "message"),
    [
        (
            {
                "libpython3.11-stdlib": {
                    "version": "3.11.2-6+deb12u7",
                    "file": "libpython3.11-stdlib_3.11.2-6+deb12u7_amd64.deb",
                    "url": f"{BASE_URL}/libpython3.11-stdlib_3.11.2-6+deb12u7_amd64.deb",
                }
            },
            "разных версий",
        ),
        (
            {
                "python3.11-minimal": {
                    "arch": "i386",
                    "file": f"python3.11-minimal_{VERSION}_i386.deb",
                    "url": f"{BASE_URL}/python3.11-minimal_{VERSION}_i386.deb",
                }
            },
            "архитектура i386",
        ),
        ({"python3.11-minimal": {"file": "other.deb"}}, "ожидался python3.11-minimal_"),
        ({"python3.11-minimal": {"url": f"{BASE_URL}/pool/other.deb"}}, "не оканчивается на"),
        (
            {
                "python3.11-minimal": {
                    "url": "http://x.invalid/" + f"python3.11-minimal_{Q}_amd64.deb"
                }
            },
            "не https",
        ),
        ({"python3.11-minimal": {"index": "main-sources"}}, "не Packages"),
        ({"python3.11-minimal": {"index": "nope"}}, "нет записи index с id nope"),
        ({"python3.11-minimal": {"dsc": "py-orig"}}, "ожидался python3.11_"),
        ({"python3.11-minimal": {"size": "10"}}, "size — положительное целое"),
        ({"python3.11-minimal": {"size": 0}}, "size — положительное целое"),
        ({"python3.11-minimal": {"sha256": "A" * 64}}, "неверный sha256"),
        ({"python3.11-minimal": {"extra": "x"}}, "неизвестные поля extra"),
        (
            {"debian-bookworm": {"signer": "b8b80b5b623eab6ad8775c45b7c5d7d6350947f8"}},
            "signer — 40 hex",
        ),
        ({"debian-bookworm": {"keyring": "../keys/x.gpg"}}, "недопустимый путь"),
        ({"debian-bookworm": {"keyring": "/usr/share/keyrings/x.gpg"}}, "недопустимый путь"),
        ({"debian-bookworm": {"keyring": "tests/x.gpg"}}, "keyring вне packaging/appimage/keys/"),
        ({"main-amd64": {"archive": "nope"}}, "нет записи archive с id nope"),
        ({"main-amd64": {"kind": "Contents"}}, "kind — Packages или Sources"),
        (
            {
                "main-amd64": {
                    "file": "main/binary-amd64/Packages.gz",
                    "url": f"{BASE_URL}/main/binary-amd64/Packages.gz",
                }
            },
            "не Packages\\[\\.xz\\]",
        ),
        ({"py-dsc": {"index": "main-amd64"}}, "не Sources"),
        ({"py-orig": {"index": "main-sources"}}, "index и dsc одновременно"),
        ({"py-orig": {"index": "main-sources", "dsc": None}}, "только .dsc"),
        ({"py-orig": {"dsc": "py-orig"}}, "не .dsc"),
    ],
)
def test_bad_debian_records_rejected(override: dict[str, dict[str, Any]], message: str) -> None:
    with pytest.raises(lockfile.LockError, match=message):
        lockfile.parse(lock_text(records(**override)))


def test_debian_base_needs_all_three_packages() -> None:
    items = [item for item in records() if item[1].get("package") != "libpython3.11-stdlib"]
    with pytest.raises(lockfile.LockError, match="нет пакетов libpython3.11-stdlib"):
        lockfile.parse(lock_text(items))


def test_unknown_debian_package_rejected() -> None:
    items = records()
    extra = dict(items[3][1])
    extra.update(
        package="zlib1g",
        file=f"zlib1g_{VERSION}_amd64.deb",
        url=f"{BASE_URL}/zlib1g_{VERSION}_amd64.deb",
    )
    with pytest.raises(lockfile.LockError, match="не умеет раскладывать пакеты zlib1g"):
        lockfile.parse(lock_text([*items, ("deb", extra)]))


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("# deb: [1, 2]\n", "запись — не объект JSON"),
        ('# deb: {"package": }\n', "неверный JSON"),
        ("# deb: python3.11-minimal 3.11\n", "неверный JSON"),
        ("# deb:\n", "неверная запись"),
        ("# TODO-PIN\n", "неверная строка TODO-PIN"),
    ],
)
def test_malformed_record_lines(text: str, message: str) -> None:
    with pytest.raises(lockfile.LockError, match=message):
        lockfile.parse(lock_text(records(), extra=text))


def test_duplicates_rejected() -> None:
    items = records()
    with pytest.raises(lockfile.LockError, match="повторяется id записи archive"):
        lockfile.parse(lock_text([*items, items[0]]))
    with pytest.raises(lockfile.LockError, match="пакет Debian указан дважды"):
        lockfile.parse(lock_text([*items, items[3]]))


def test_base_and_tools_must_agree() -> None:
    python_tool = (
        f"# tool: python3.11.16-cp311-cp311-manylinux_2_28_x86_64.AppImage {SHA} 4 "
        "https://example.invalid/py\n"
    )
    with pytest.raises(lockfile.LockError, match="Python AppImage в lock не нужен"):
        lockfile.parse(lock_text(records(), extra=python_tool))
    with pytest.raises(lockfile.LockError, match="неизвестные инструменты: linuxdeploy"):
        lockfile.parse(
            lock_text(records(), extra=f"# tool: linuxdeploy {SHA} 5 https://example.invalid/ld\n")
        )
    host_to_bundled = HEAD.replace("openssl-origin: host", "openssl-origin: bundled")
    with pytest.raises(lockfile.LockError, match="openssl-origin должен быть host"):
        lockfile.parse(lock_text(records(), head=host_to_bundled))
    legacy = (
        HEAD.replace("base: debian12", "base: python-appimage").replace(
            "openssl-origin: host", "openssl-origin: bundled"
        )
        + python_tool
    )
    with pytest.raises(lockfile.LockError, match="только при base: debian12"):
        lockfile.parse(lock_text(records(), head=legacy))
    with pytest.raises(lockfile.LockError, match="неверное значение base"):
        lockfile.parse(lock_text(records(), head=HEAD.replace("debian12", "debian13")))


def test_build_tool_pins_required_for_debian12() -> None:
    lock = lockfile.parse(lock_text(records()))
    assert (lock.directives["build-python"], lock.directives["build-pip"]) == ("3.11", "23.0.1")
    with pytest.raises(lockfile.LockError, match="нет директив build-pip"):
        lockfile.parse(lock_text(records(), head=HEAD.replace("# build-pip: 23.0.1\n", "")))
    with pytest.raises(lockfile.LockError, match="неверное значение build-python"):
        lockfile.parse(lock_text(records(), head=HEAD.replace("3.11\n", "3\n", 1)))


def test_host_lib_manifest() -> None:
    lock = lockfile.parse(lock_text(records()))
    assert lock.host_libs == {"libssl.so.3": "libssl3", "libcrypto.so.3": "libssl3"}
    no_crypto = HEAD.replace("# host-lib: libcrypto.so.3 libssl3\n", "")
    with pytest.raises(lockfile.LockError, match="нет host-lib: libcrypto.so.3"):
        lockfile.parse(lock_text(records(), head=no_crypto))
    with pytest.raises(lockfile.LockError, match="host-lib libssl.so.3 повторяется"):
        lockfile.parse(lock_text(records(), extra="# host-lib: libssl.so.3 libssl3\n"))
    with pytest.raises(lockfile.LockError, match="неверная строка host-lib"):
        lockfile.parse(lock_text(records(), extra="# host-lib: libz.so.1\n"))


def test_todo_pin_passes_format_but_is_listed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    items = [item for item in records() if item[1].get("package") != "libpython3.11-stdlib"]
    text = lock_text(items, extra="# TODO-PIN: deb libpython3.11-stdlib — нужна сеть\n")
    lock = lockfile.parse(text)
    assert lock.todo_pin == ["deb libpython3.11-stdlib — нужна сеть"]
    path = tmp_path / "appimage.lock"
    path.write_text(text, encoding="utf-8")
    assert lockfile.main([str(path), "todo-pin"]) == 0
    assert capsys.readouterr().out == "deb libpython3.11-stdlib — нужна сеть\n"
    assert lockfile.main([str(path), "get", "base"]) == 0
    assert capsys.readouterr().out == "debian12\n"
    assert lockfile.main([str(path), "debs"]) == 0
    assert capsys.readouterr().out.splitlines()[0] == (
        f"python3.11-minimal debs/python3.11-minimal_{VERSION}_amd64.deb"
    )


# --- синтетический архив и цепочка происхождения ------------------------------------

needs_tools = pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("gpg", "gpgv", "dpkg-deb")),
    reason="нужны gpg, gpgv и dpkg-deb",
)


class Gpg:
    """Одноразовый `GNUPGHOME`; `~/.gnupg` не трогается."""

    def __init__(self, home: Path) -> None:
        self.home = home
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)

    def run(self, *args: str) -> subprocess.CompletedProcess[bytes]:
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
        return subprocess.run(argv, capture_output=True, check=True)

    def gen(self, uid: str) -> str:
        self.run("--quick-gen-key", uid, "ed25519", "sign", "never")
        out = self.run("--list-keys", "--with-colons", uid).stdout.decode()
        return next(line.split(":")[9] for line in out.splitlines() if line.startswith("fpr:"))

    def export(self, uid: str, dest: Path) -> Path:
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.run("--export", uid).stdout)
        return dest

    def clearsign(self, text: str, dest: Path, key: str) -> None:
        src = dest.with_suffix(".plain")
        src.write_text(text, encoding="utf-8")
        self.run("--clearsign", "--local-user", key + "!", "--output", str(dest), str(src))
        src.unlink()


def digest(path: Path) -> tuple[str, int]:
    data = path.read_bytes()
    return hashlib.sha256(data).hexdigest(), len(data)


def build_deb(dest: Path, package: str, version: str, source: str | None = "python3.11") -> Path:
    tree = dest.parent / f"tree-{package}-{version}"
    (tree / "DEBIAN").mkdir(parents=True)
    (tree / "usr" / "share" / "doc" / package).mkdir(parents=True)
    (tree / "usr" / "share" / "doc" / package / "copyright").write_text("test\n", "utf-8")
    control = (
        f"Package: {package}\nVersion: {version}\nArchitecture: amd64\n"
        "Maintainer: Test <test@test.invalid>\nDescription: test\n"
    )
    if source:
        control = control.replace("Architecture", f"Source: {source}\nArchitecture")
    (tree / "DEBIAN" / "control").write_text(control, "utf-8")
    subprocess.run(
        ["dpkg-deb", "--root-owner-group", "-Zxz", "-b", str(tree), str(dest)],
        capture_output=True,
        check=True,
    )
    shutil.rmtree(tree)
    return dest


@dataclass
class Archive:
    """Синтетический «Debian»: репозиторий-корень с ключом, кэш и lock к ним."""

    root: Path
    cache: Path
    gpg: Gpg
    signer: str
    foreign: str
    items: list[tuple[str, dict[str, Any]]]
    packages: str
    release_extra: str = ""

    def item(self, key: str) -> dict[str, Any]:
        return next(d for _, d in self.items if key in (d.get("id"), d.get("package")))

    def write_lock(self) -> Any:
        return lockfile.parse(lock_text(self.items))

    def resign(
        self, key: str | None = None, packages: str | None = None, sources: bytes | None = None
    ) -> None:
        """Пересобрать индексы и InRelease; обновить sha/size в lock (как честный перепин)."""
        meta = self.cache / "metadata" / "debian-bookworm"
        pk = meta / "main" / "binary-amd64" / "Packages.xz"
        pk.write_bytes(lzma.compress((packages or self.packages).encode(), format=lzma.FORMAT_XZ))
        src = meta / "main" / "source" / "Sources.xz"
        if sources is not None:
            src.write_bytes(sources)
        lines = []
        for path, name in ((pk, "main/binary-amd64/Packages.xz"), (src, "main/source/Sources.xz")):
            sha, size = digest(path)
            lines.append(f" {sha} {size} {name}")
            ident = "main-amd64" if "binary" in name else "main-sources"
            self.item(ident).update(sha256=sha, size=size)
        release = (
            "Origin: Test\nSuite: oldstable\nCodename: bookworm\nMD5Sum:\n 0 0 ignored\nSHA256:\n"
            + "\n".join(lines)
            + "\n"
            + self.release_extra
        )
        inrelease = meta / "InRelease"
        self.gpg.clearsign(release, inrelease, key or self.signer)
        sha, size = digest(inrelease)
        self.item("debian-bookworm").update(sha256=sha, size=size)


def packages_text(debs: dict[str, Path], version: str = VERSION, **field: str) -> str:
    stanzas = []
    for package, path in debs.items():
        sha, size = digest(path)
        stanza = {
            "Package": package,
            "Source": "python3.11",
            "Version": version,
            "Architecture": "amd64",
            "Filename": f"pool/main/p/python3.11/{path.name}",
            "Size": str(size),
            "SHA256": sha,
            "Description": "test\n multi-line continuation",
        }
        stanza.update(field)
        stanzas.append("\n".join(f"{k}: {v}" for k, v in stanza.items()))
    return "Package: other\nVersion: 1\nArchitecture: amd64\n\n" + "\n\n".join(stanzas) + "\n"


@pytest.fixture(scope="module")
def gpg_keys(tmp_path_factory: pytest.TempPathFactory) -> tuple[Gpg, str, str]:
    gpg = Gpg(tmp_path_factory.mktemp("gnupg-home") / "home")
    return gpg, gpg.gen("Test Archive <archive@test.invalid>"), gpg.gen("Foreign <f@test.invalid>")


@pytest.fixture
def archive(tmp_path: Path, gpg_keys: tuple[Gpg, str, str]) -> Archive:
    if any(shutil.which(tool) is None for tool in ("gpg", "gpgv", "dpkg-deb")):
        pytest.skip("нужны gpg, gpgv и dpkg-deb")
    gpg, signer, foreign = gpg_keys
    root, cache = tmp_path / "repo", tmp_path / "cache"
    gpg.export(signer, root / "packaging" / "appimage" / "keys" / "test-archive.gpg")
    (cache / "debs").mkdir(parents=True)
    (cache / "sources").mkdir()
    (cache / "metadata" / "debian-bookworm" / "main" / "binary-amd64").mkdir(parents=True)
    (cache / "metadata" / "debian-bookworm" / "main" / "source").mkdir(parents=True)
    debs = {
        package: build_deb(cache / "debs" / f"{package}_{VERSION}_amd64.deb", package, VERSION)
        for package in lockfile.PYTHON_DEBS
    }
    orig = cache / "sources" / "python3.11_3.11.2.orig.tar.gz"
    orig.write_bytes(b"orig tarball")
    orig_sha, orig_size = digest(orig)
    dsc = cache / "sources" / f"python3.11_{VERSION}.dsc"
    gpg.clearsign(
        f"Format: 3.0 (quilt)\nSource: python3.11\nVersion: {VERSION}\nChecksums-Sha256:\n"
        f" {orig_sha} {orig_size} {orig.name}\n",
        dsc,
        foreign,
    )
    dsc_sha, dsc_size = digest(dsc)
    sources = lzma.compress(
        (
            f"Package: python3.11\nVersion: {VERSION}\nChecksums-Sha256:\n"
            f" {dsc_sha} {dsc_size} {dsc.name}\n {orig_sha} {orig_size} {orig.name}\n"
        ).encode(),
        format=lzma.FORMAT_XZ,
    )
    items = records()
    result = Archive(root, cache, gpg, signer, foreign, items, packages_text(debs))
    result.item("debian-bookworm")["signer"] = signer
    for package, path in debs.items():
        sha, size = digest(path)
        result.item(package).update(sha256=sha, size=size)
    result.item("py-dsc").update(sha256=dsc_sha, size=dsc_size)
    result.item("py-orig").update(sha256=orig_sha, size=orig_size)
    result.resign(sources=sources)
    return result


def verify(archive: Archive, what: str = "debs") -> list[Path]:
    verifier = debverify.Verifier(archive.write_lock(), archive.cache, archive.root)
    paths: list[Path] = verifier.verify_debs() if what == "debs" else verifier.verify_sources()
    return paths


@needs_tools
def test_chain_accepts_synthetic_archive(archive: Archive) -> None:
    paths = verify(archive)
    assert [p.name for p in paths] == [f"{pkg}_{VERSION}_amd64.deb" for pkg in lockfile.PYTHON_DEBS]
    assert len(verify(archive, "sources")) == 2


@needs_tools
def test_cli_verifies_debs(archive: Archive, tmp_path: Path) -> None:
    lock = tmp_path / "appimage.lock"
    lock.write_text(lock_text(archive.items), encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(HERE / "debverify.py"),
            "--lock",
            str(lock),
            "--cache",
            str(archive.cache),
            "--root",
            str(archive.root),
            "debs",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "debverify debs: 3 файлов" in proc.stdout
    (archive.cache / "debs" / f"python3.11-minimal_{VERSION}_amd64.deb").write_bytes(b"x")
    proc = subprocess.run(
        [
            sys.executable,
            str(HERE / "debverify.py"),
            "--lock",
            str(lock),
            "--cache",
            str(archive.cache),
            "--root",
            str(archive.root),
            "debs",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "ОШИБКА: происхождение: deb python3.11-minimal: размер 1" in proc.stderr


@needs_tools
def test_warm_cache_tampered_deb_rejected(archive: Archive) -> None:
    """Попадание в кэш ничего не доказывает: подмена файла ловится и без --fetch."""
    path = archive.cache / "debs" / f"libpython3.11-stdlib_{VERSION}_amd64.deb"
    data = bytearray(path.read_bytes())
    data[-1] ^= 1
    path.write_bytes(bytes(data))
    with pytest.raises(debverify.VerifyError, match="deb libpython3.11-stdlib: sha256"):
        verify(archive)


@needs_tools
def test_missing_file_asks_for_fetch(archive: Archive) -> None:
    (archive.cache / "debs" / f"python3.11-minimal_{VERSION}_amd64.deb").unlink()
    with pytest.raises(debverify.VerifyError, match="нужен build.sh --fetch"):
        verify(archive)


@needs_tools
def test_lock_sha_differs_from_index(archive: Archive) -> None:
    archive.item("python3.11-minimal")["sha256"] = SHA
    with pytest.raises(debverify.VerifyError, match="python3.11-minimal: в индексе [0-9a-f]{64}"):
        verify(archive)


@needs_tools
def test_foreign_signature_rejected(archive: Archive) -> None:
    archive.resign(key=archive.foreign)
    with pytest.raises(debverify.VerifyError, match=f"нет подписи ключом {archive.signer}"):
        verify(archive)


@needs_tools
def test_tampered_inrelease_bad_signature(archive: Archive) -> None:
    """Текст InRelease изменён после подписи, lock «перепинен» на новый файл — BADSIG."""
    path = archive.cache / "metadata" / "debian-bookworm" / "InRelease"
    path.write_text(path.read_text("utf-8").replace("Origin: Test", "Origin: Evil"), "utf-8")
    sha, size = digest(path)
    archive.item("debian-bookworm").update(sha256=sha, size=size)
    with pytest.raises(debverify.VerifyError, match="плохая подпись \\(BADSIG\\)"):
        verify(archive)


@needs_tools
def test_inrelease_not_matching_lock(archive: Archive) -> None:
    archive.item("debian-bookworm")["sha256"] = SHA
    with pytest.raises(debverify.VerifyError, match="InRelease debian-bookworm: sha256"):
        verify(archive)


@needs_tools
def test_index_not_listed_in_inrelease(archive: Archive) -> None:
    archive.item("main-amd64")["sha256"] = SHA
    with pytest.raises(debverify.VerifyError, match="index main-amd64: в InRelease"):
        verify(archive)


@needs_tools
def test_index_file_swapped(archive: Archive) -> None:
    path = archive.cache / "metadata" / "debian-bookworm" / "main" / "binary-amd64" / "Packages.xz"
    path.write_bytes(lzma.compress(b"Package: x\n", format=lzma.FORMAT_XZ))
    with pytest.raises(debverify.VerifyError, match="index main-amd64: (размер|sha256)"):
        verify(archive)


@needs_tools
def test_missing_keyring(archive: Archive) -> None:
    (archive.root / "packaging" / "appimage" / "keys" / "test-archive.gpg").unlink()
    with pytest.raises(debverify.VerifyError, match="нет ключа архива"):
        verify(archive)


@needs_tools
def test_version_missing_from_index(archive: Archive) -> None:
    debs = {p: archive.cache / "debs" / f"{p}_{VERSION}_amd64.deb" for p in lockfile.PYTHON_DEBS}
    archive.resign(packages=packages_text(debs, version="3.11.2-6+deb12u9"))
    with pytest.raises(debverify.VerifyError, match="0 строф 3.11.2-6\\+deb12u8 amd64"):
        verify(archive)


@needs_tools
def test_source_mapping_mismatch(archive: Archive) -> None:
    debs = {p: archive.cache / "debs" / f"{p}_{VERSION}_amd64.deb" for p in lockfile.PYTHON_DEBS}
    archive.resign(packages=packages_text(debs, Source="python3.11 (3.11.2-6+deb12u7)"))
    with pytest.raises(debverify.VerifyError, match="исходник в индексе"):
        verify(archive)


@needs_tools
def test_control_of_deb_must_match(archive: Archive) -> None:
    """Индекс и lock согласны, но внутри .deb другая версия — отказ по control."""
    path = archive.cache / "debs" / f"python3.11-minimal_{VERSION}_amd64.deb"
    build_deb(path.with_name("x.deb"), "python3.11-minimal", "3.11.2-6+deb12u1").replace(path)
    debs = {p: archive.cache / "debs" / f"{p}_{VERSION}_amd64.deb" for p in lockfile.PYTHON_DEBS}
    archive.resign(packages=packages_text(debs))
    sha, size = digest(path)
    archive.item("python3.11-minimal").update(sha256=sha, size=size)
    with pytest.raises(debverify.VerifyError, match="control пакета"):
        verify(archive)


@needs_tools
def test_filename_in_index_must_match(archive: Archive) -> None:
    debs = {p: archive.cache / "debs" / f"{p}_{VERSION}_amd64.deb" for p in lockfile.PYTHON_DEBS}
    archive.resign(packages=packages_text(debs, Filename="pool/main/p/python3.11/evil.deb"))
    with pytest.raises(debverify.VerifyError, match="в индексе файл pool/main/p/python3.11/evil"):
        verify(archive)


@needs_tools
def test_todo_pin_blocks_verification(archive: Archive) -> None:
    lock = lockfile.parse(lock_text(archive.items, extra="# TODO-PIN: deb zlib1g\n"))
    with pytest.raises(debverify.VerifyError, match="TODO-PIN"):
        debverify.Verifier(lock, archive.cache, archive.root).verify_debs()


@needs_tools
def test_sources_incomplete_or_tampered(archive: Archive) -> None:
    orig = archive.cache / "sources" / "python3.11_3.11.2.orig.tar.gz"
    items = [item for item in archive.items if item[1].get("id") != "py-orig"]
    lock = lockfile.parse(lock_text(items))
    with pytest.raises(debverify.VerifyError, match="в lock нет архивов python3.11_3.11.2.orig"):
        debverify.Verifier(lock, archive.cache, archive.root).verify_sources()
    orig.write_bytes(b"other tarball")
    sha, size = digest(orig)
    archive.item("py-orig").update(sha256=sha, size=size)
    with pytest.raises(debverify.VerifyError, match="source py-orig: в py-dsc"):
        verify(archive, "sources")


def test_release_and_deb822_parsers() -> None:
    text = (
        "Origin: Debian\nMD5Sum:\n 0123 5 main/x\nSHA256:\n"
        f" {SHA} 7 main/binary-amd64/Packages.xz\n {'b' * 64} 9 main/source/Sources.xz\n"
        "Acquire-By-Hash: yes\n"
    )
    assert debverify.release_sha256(text) == {
        "main/binary-amd64/Packages.xz": (SHA, 7),
        "main/source/Sources.xz": ("b" * 64, 9),
    }
    with pytest.raises(debverify.VerifyError, match="нет раздела SHA256"):
        debverify.release_sha256("Origin: Debian\n")
    with pytest.raises(debverify.VerifyError, match="неверная строка SHA256"):
        debverify.release_sha256("SHA256:\n broken line\n")
    stanza = {"Package": "libfoo1", "Version": "1.0+b1", "Source": "foo (1.0)"}
    assert debverify.source_of(stanza) == ("foo", "1.0")
    assert debverify.source_of({"Package": "foo", "Version": "2"}) == ("foo", "2")
    assert debverify.source_of({"Package": "a", "Version": "2", "Source": "bar"}) == ("bar", "2")
    signed = (
        "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nSource: x\n- -dash\n"
        "-----BEGIN PGP SIGNATURE-----\nabc\n-----END PGP SIGNATURE-----\n"
    )
    assert debverify.strip_pgp(signed) == "Source: x\n-dash"


# --- SBOM базы debian12 (R3.4) --------------------------------------------------------------

ELF = b"\x7fELF" + bytes(60)
SITE = "opt/python3.11/lib/python3.11/site-packages"
SBOM_SOURCES = "\n".join(
    rec("source", {"id": ident, "file": file, "size": 1, "sha256": SHA, "url": url})
    for ident, file, url in (
        ("qtbase-5.15.19", "c1.tar.gz", "https://github.com/qt/qtbase/archive/c1.tar.gz"),
        ("type2-runtime-src", "c2.tar.gz", "https://github.com/AppImage/x/archive/c2.tar.gz"),
        ("libfuse-3.15.0", "fuse.tar.xz", "https://github.com/libfuse/libfuse/fuse.tar.xz"),
    )
)


def _sbom_tree(tmp_path: Path) -> tuple[Path, Path, Path]:
    """AppDir, кэш и lock: два .deb интерпретатора, колёса numpy и PyQt5-Qt5 с RECORD."""
    cache = tmp_path / "cache"
    (cache / "debs").mkdir(parents=True)
    appdir = tmp_path / "AppDir"
    trees = {
        "python3.11-minimal": {"usr/bin/python3.11": ELF},
        "libpython3.11-minimal": {"usr/lib/python3.11/os.py": b"#\n"},
        "libpython3.11-stdlib": {
            "usr/lib/python3.11/lib-dynload/_ssl.cpython-311-x86_64-linux-gnu.so": ELF,
            "usr/lib/python3.11/lib-dynload/readline.cpython-311-x86_64-linux-gnu.so": ELF,
        },
    }
    override: dict[str, dict[str, Any]] = {}
    for package, files in trees.items():
        tree = tmp_path / f"tree-{package}"
        (tree / "DEBIAN").mkdir(parents=True)
        (tree / "DEBIAN" / "control").write_text(
            f"Package: {package}\nVersion: {VERSION}\nArchitecture: amd64\n"
            "Maintainer: T <t@test.invalid>\nDescription: t\n",
            "utf-8",
        )
        for rel, data in files.items():
            (tree / rel).parent.mkdir(parents=True, exist_ok=True)
            (tree / rel).write_bytes(data)
            if "readline" in rel:
                continue  # урезано сборкой
            target = appdir / (
                "opt/python3.11/bin/python3.11"
                if rel == "usr/bin/python3.11"
                else "opt/python3.11/" + rel.removeprefix("usr/")
            )
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        deb = cache / "debs" / f"{package}_{VERSION}_amd64.deb"
        subprocess.run(
            ["dpkg-deb", "--root-owner-group", "-b", str(tree), str(deb)],
            capture_output=True,
            check=True,
        )
        sha, size = digest(deb)
        override[package] = {"sha256": sha, "size": size}
    site = appdir / SITE
    wheels: dict[str, dict[str, bytes | None]] = {
        "numpy-1.24.2": {
            "numpy/core/_multiarray.cpython-311-x86_64-linux-gnu.so": ELF,
            "numpy.libs/libgfortran-040039e1.so.5.0.0": ELF,
            "numpy/__init__.py": b"",
        },
        "pyqt5_qt5-5.15.19": {
            "PyQt5/Qt5/lib/libQt5Core.so.5": ELF,
            "PyQt5/Qt5/lib/libicuuc.so.56": ELF,
            "PyQt5/Qt5/plugins/platforms/libqxcb.so": ELF,
            "PyQt5/Qt5/lib/libQt5Designer.so.5": None,  # в RECORD, но вырезано сборкой
        },
    }
    for dist, content in wheels.items():
        info = site / f"{dist}.dist-info"
        info.mkdir(parents=True)
        rows = []
        for rel, blob in content.items():
            rows.append(f"{rel},sha256=x,1")
            if blob is not None:
                (site / rel).parent.mkdir(parents=True, exist_ok=True)
                (site / rel).write_bytes(blob)
        (info / "RECORD").write_text("\n".join(rows) + "\n", "utf-8")
    (appdir / ".astra-voice-build").write_text("VERSION=0.2.0\nBUILD_ID=0123456789ab\n", "ascii")
    head = HEAD.replace(
        f"numpy==1.24.2 \\\n    --hash=sha256:{SHA}\n",
        f"numpy==1.24.2 \\\n    --hash=sha256:{SHA}\n"
        f"PyQt5-Qt5==5.15.19 \\\n    --hash=sha256:{SHA}\n",
    ).replace(
        "# host-lib: libcrypto.so.3 libssl3",
        "# host-lib: libcrypto.so.3 libssl3\n# host-lib: libz.so.1 zlib1g",
    )
    lock = tmp_path / "appimage.lock"
    lock.write_text(lock_text(records(**override), head=head, extra=SBOM_SOURCES + "\n"), "utf-8")
    return appdir, cache, lock


def _sbom(appdir: Path, cache: Path, lock: Path, out: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts" / "sbom.py"),
            "--appdir",
            str(appdir),
            "--lock",
            str(lock),
            "--cache",
            str(cache),
            "--out",
            str(out),
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={"PATH": "/usr/bin:/bin", "SOURCE_DATE_EPOCH": "1790000000"},
    )


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="нужен dpkg-deb")
def test_sbom_debian12(tmp_path: Path) -> None:
    appdir, cache, lock = _sbom_tree(tmp_path)
    out = tmp_path / "sbom.json"
    proc = _sbom(appdir, cache, lock, out)
    assert proc.returncode == 0, proc.stderr
    bom = json.loads(out.read_text("utf-8"))
    comps = {c["bom-ref"]: c for c in bom["components"]}
    stdlib = comps[f"pkg:deb/debian/libpython3.11-stdlib@{VERSION}?arch=amd64"]
    props = {p["name"]: p["value"] for p in stdlib["properties"]}
    assert props["astra-voice:files-retained"] == "1"  # readline урезан
    assert props["astra-voice:source-package"] == f"python3.11 {VERSION}"
    assert (
        stdlib["hashes"][0]["content"]
        == digest(cache / "debs" / f"libpython3.11-stdlib_{VERSION}_amd64.deb")[0]
    )
    ssl_host = comps["host:libssl3"]
    host_props = {p["name"]: p["value"] for p in ssl_host["properties"]}
    assert ssl_host["name"] == "openssl"
    assert host_props["astra-voice:origin"] == "host"
    assert host_props["astra-voice:not-bundled"] == "true"
    assert host_props["astra-voice:sonames"] == "libcrypto.so.3 libssl.so.3"
    assert "host:zlib1g" in comps
    runtime = next(c for c in bom["components"] if c["name"] == "type2-runtime")
    assert runtime["scope"] == "required"
    assert [p["name"] for p in runtime["components"]][:2] == ["libfuse", "squashfuse"]
    assert comps["tool:appimagetool-x86_64.AppImage"]["scope"] == "excluded"
    qt = next(
        c for c in comps["pkg:pypi/PyQt5-Qt5@5.15.19"]["components"] if c["name"] == "qt5-qtbase"
    )
    assert qt["licenses"] == [{"expression": "LGPL-3.0-only"}]
    assert qt["externalReferences"][0]["url"].endswith("qtbase/archive/c1.tar.gz")
    numpy_libs = comps["pkg:pypi/numpy@1.24.2"]["components"]
    assert numpy_libs[0]["licenses"] == [{"expression": "GPL-3.0-or-later WITH GCC-exception-3.1"}]
    origins = {
        c["name"]: next(p["value"] for p in c["properties"] if p["name"] == "astra-voice:from")
        for c in bom["components"]
        if c["bom-ref"].startswith("file:")
    }
    assert origins["/opt/python3.11/bin/python3.11"].startswith("pkg:deb/debian/python3.11-min")
    assert origins[f"/{SITE}/PyQt5/Qt5/lib/libicuuc.so.56"] == "pkg:generic/icu@56"
    assert origins[f"/{SITE}/numpy.libs/libgfortran-040039e1.so.5.0.0"] == "pkg:pypi/numpy@1.24.2"
    assert len(origins) == 7


@pytest.mark.skipif(shutil.which("dpkg-deb") is None, reason="нужен dpkg-deb")
def test_sbom_debian12_refuses_unattributed_elf(tmp_path: Path) -> None:
    appdir, cache, lock = _sbom_tree(tmp_path)
    (appdir / "usr" / "lib").mkdir(parents=True)
    (appdir / "usr" / "lib" / "libstray.so.1").write_bytes(ELF)
    proc = _sbom(appdir, cache, lock, tmp_path / "sbom.json")
    assert proc.returncode == 1
    assert "ELF без происхождения: /usr/lib/libstray.so.1" in proc.stderr
    (appdir / "usr" / "lib" / "libstray.so.1").unlink()
    qt_extra = appdir / SITE / "PyQt5" / "Qt5" / "lib" / "libQt5Designer.so.5"
    qt_extra.write_bytes(ELF)
    proc = _sbom(appdir, cache, lock, tmp_path / "sbom.json")
    assert proc.returncode == 1
    assert "ELF Qt без модуля в карте sbom.py: PyQt5/Qt5/lib/libQt5Designer.so.5" in proc.stderr
    assert not (tmp_path / "sbom.json").exists()


# --- ревью P3-1/P3-4: структура clearsign и белый список статусов gpgv ------------------


def _repin_inrelease(archive: Archive, data: bytes) -> None:
    path = archive.cache / "metadata" / "debian-bookworm" / "InRelease"
    path.write_bytes(data)
    sha, size = digest(path)
    archive.item("debian-bookworm").update(sha256=sha, size=size)


@needs_tools
@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda signed: signed.split(b"\n\n", 1)[1].split(b"-----BEGIN PGP SIGNATURE")[0],
            "не clearsigned-файл",
        ),
        (lambda signed: signed + signed, "данные после подписи"),
        (lambda signed: signed + b"Codename: evil\n", "данные после подписи"),
        (
            lambda signed: signed.replace(
                b"\nSHA256:\n", b"\n-----BEGIN PGP SIGNED MESSAGE-----\nSHA256:\n"
            ),
            "неэкранированная строка",
        ),
        (lambda signed: signed.split(b"-----END PGP SIGNATURE-----")[0], "блок подписи не закрыт"),
    ],
)
def test_inrelease_structure_rejected(archive: Archive, mutate: Any, message: str) -> None:
    signed = (archive.cache / "metadata" / "debian-bookworm" / "InRelease").read_bytes()
    _repin_inrelease(archive, mutate(signed))
    with pytest.raises(debverify.VerifyError, match=message):
        verify(archive)


@needs_tools
def test_expired_key_rejected(archive: Archive, tmp_path: Path) -> None:
    """Ключ, истёкший до проверки (подпись сделана в прошлом) — EXPKEYSIG, отказ."""
    gpg = Gpg(tmp_path / "gnupg-expired")
    past = ["--faked-system-time", "20200101T000000!"]
    gpg.run(*past, "--quick-gen-key", "Expired <e@test.invalid>", "ed25519", "sign", "1d")
    out = gpg.run("--list-keys", "--with-colons").stdout.decode()
    fpr = next(line.split(":")[9] for line in out.splitlines() if line.startswith("fpr:"))
    gpg.export(fpr, archive.root / "packaging" / "appimage" / "keys" / "test-archive.gpg")
    inrelease = archive.cache / "metadata" / "debian-bookworm" / "InRelease"
    body = debverify.strip_pgp(inrelease.read_text("utf-8")) + "\n"
    plain = tmp_path / "Release"
    plain.write_text(body, "utf-8")
    gpg.run(*past, "--clearsign", "--local-user", fpr + "!", "--output", str(inrelease), str(plain))
    _repin_inrelease(archive, inrelease.read_bytes())
    archive.item("debian-bookworm")["signer"] = fpr
    with pytest.raises(debverify.VerifyError, match="плохая подпись \\(.*EXPKEYSIG"):
        verify(archive)


def _fake_gpgv(tmp_path: Path, statuses: list[str]) -> str:
    script = tmp_path / "fake-gpgv"
    lines = "\n".join(f"echo '[GNUPG:] {s}'" for s in statuses)
    script.write_text(
        "#!/bin/sh\n"
        'while [ $# -gt 1 ]; do [ "$1" = --output ] && out=$2; shift; done\n'
        'sed -n "/^$/,/^-----BEGIN PGP SIGNATURE/p" "$1" > "$out"\n' + lines + "\n",
        "utf-8",
    )
    script.chmod(0o755)
    return str(script)


@needs_tools
@pytest.mark.parametrize(
    ("statuses", "message"),
    [
        (
            ["PLAINTEXT 74 0", "GOODSIG X u", "VALIDSIG {s} 1 2 0 4 0 1 8 01 {s}", "ERROR x 1"],
            "плохая подпись \\(ERROR\\)",
        ),
        (
            ["PLAINTEXT 74 0", "GOODSIG X u", "VALIDSIG {s} 1 2 0 4 0 1 8 01 {s}", "BADARMOR 1"],
            "BADARMOR",
        ),
        (["GOODSIG X u", "VALIDSIG {s} 1 2 0 4 0 1 8 01 {s}"], "ровно одна подпись"),
        (
            ["PLAINTEXT 74 0", "GOODSIG X u", "GOODSIG Y u", "VALIDSIG {s} 1 2 0 4 0 1 8 01 {s}"],
            "ровно одна подпись",
        ),
        (
            ["PLAINTEXT 74 0", "GOODSIG X u", "VALIDSIG A 1 2 0 4 0 1 8 01 AAAA"],
            "нет подписи ключом",
        ),
    ],
)
def test_gpgv_status_whitelist(
    archive: Archive, tmp_path: Path, statuses: list[str], message: str
) -> None:
    gpgv = _fake_gpgv(tmp_path, [s.format(s=archive.signer) for s in statuses])
    verifier = debverify.Verifier(archive.write_lock(), archive.cache, archive.root, gpgv=gpgv)
    with pytest.raises(debverify.VerifyError, match=message):
        verifier.verify_debs()


@needs_tools
@pytest.mark.parametrize(("field", "value"), [("codename", "trixie"), ("suite", "stable")])
def test_codename_and_suite_must_match_lock(archive: Archive, field: str, value: str) -> None:
    """Ревью P3-2: подписанный InRelease другого выпуска Debian не подходит."""
    archive.item("debian-bookworm")[field] = value
    with pytest.raises(debverify.VerifyError, match="Codename/Suite"):
        verify(archive)


@needs_tools
def test_deb_dir_copy_is_what_gets_verified(archive: Archive, tmp_path: Path) -> None:
    """Ревью P3-3: сборка проверяет свою копию .deb; подмена копии ловится, кэш не при чём."""
    copies = tmp_path / "debs"
    copies.mkdir()
    for path in (archive.cache / "debs").glob("*.deb"):
        shutil.copy2(path, copies / path.name)
    lock = archive.write_lock()
    paths = debverify.Verifier(lock, archive.cache, archive.root, deb_dir=copies).verify_debs()
    assert all(path.parent == copies for path in paths)
    (copies / f"python3.11-minimal_{VERSION}_amd64.deb").write_bytes(b"swapped")
    with pytest.raises(debverify.VerifyError, match="deb python3.11-minimal: размер"):
        debverify.Verifier(lock, archive.cache, archive.root, deb_dir=copies).verify_debs()
