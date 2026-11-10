"""E13.M7: pinned two-wheel layout, exact ELF gate and licensing/SBOM coverage."""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import stat
import struct
import subprocess
import sys
import zipfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
WHEELS = Path.home() / ".cache/astra-voice-dev/wheels"
NAMES = {
    "onnxruntime/capi/libonnxruntime.so.1.24.4",
    "onnxruntime/capi/libonnxruntime_providers_shared.so",
    "onnxruntime/capi/onnxruntime_pybind11_state.cpython-311-x86_64-linux-gnu.so",
    "sherpa_onnx/lib/_sherpa_onnx.cpython-311-x86_64-linux-gnu.so",
    "sherpa_onnx/lib/libonnxruntime.so",
    "sherpa_onnx/lib/libsherpa-onnx-c-api.so",
    "sherpa_onnx/lib/libsherpa-onnx-cxx-api.so",
    "sherpa_onnx.libs/libasound-fb5348bf.so.2.0.0",
}


def load(name: str, rel: str) -> ModuleType:
    loader = SourceFileLoader(name, str(ROOT / rel))
    spec = importlib.util.spec_from_loader(name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


audit = load("sherpa_elf_audit", "tools/elf-audit")
deps = load("sherpa_deps_audit", "tools/deps-audit")
sbom = load("sherpa_sbom", "scripts/sbom.py")
lockfile = load("sherpa_lockfile", "packaging/appimage/lockfile.py")
licenses = load("sherpa_licenses", "packaging/appimage/collect_licenses.py")
bundle = load("sherpa_bundle", "packaging/appimage/check_bundle.py")


@pytest.fixture
def elf_tree(tmp_path: Path) -> Path:
    root = tmp_path / "deb"
    for name in NAMES:
        path = root / audit.DEB_VENDOR / name
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile("/bin/true", path)
    return root


def test_exact_names_not_just_count(elf_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert audit.VENDOR_ELFS == NAMES
    assert audit.main(["--strict", str(elf_tree)]) == 0
    path = elf_tree / audit.DEB_VENDOR / "sherpa_onnx/lib/libonnxruntime.so"
    path.rename(path.with_name("unexpected.so"))
    assert audit.main(["--strict", str(elf_tree)]) == 1
    err = capsys.readouterr().err
    assert "отсутствует ELF: sherpa_onnx/lib/libonnxruntime.so" in err
    assert "лишний ELF: sherpa_onnx/lib/unexpected.so" in err


def test_missing_and_extra_elf_fail(elf_tree: Path) -> None:
    path = elf_tree / "extra"
    shutil.copyfile("/bin/true", path)
    assert audit.main(["--strict", str(elf_tree)]) == 1
    path.unlink()
    (elf_tree / audit.DEB_VENDOR / "sherpa_onnx/lib/libonnxruntime.so").unlink()
    assert audit.main(["--strict", str(elf_tree)]) == 1


def test_appimage_checks_names_even_when_total_is_correct(elf_tree: Path, tmp_path: Path) -> None:
    site = tmp_path / "AppDir" / audit.APPIMAGE_SITE
    site.parent.mkdir(parents=True)
    shutil.move(elf_tree / audit.DEB_VENDOR, site)
    appdir = tmp_path / "AppDir"
    shutil.copyfile("/bin/true", appdir / "python")
    assert audit.main(["--strict", "--expect", "9", str(appdir)]) == 0
    path = site / "sherpa_onnx.libs/libasound-fb5348bf.so.2.0.0"
    path.rename(path.with_name("wrong.so"))
    assert audit.main(["--strict", "--expect", "9", str(appdir)]) == 1


@pytest.mark.parametrize("provider", ["cuda", "tensorrt", "azure"])
def test_cpu_only_even_for_non_elf_provider(tmp_path: Path, provider: str) -> None:
    (tmp_path / f"libonnxruntime_providers_{provider}.so").touch()
    assert audit.main(["--strict", "--expect", "0", str(tmp_path)]) == 1
    assert bundle.check_forbidden(tmp_path, "host")


@pytest.mark.skipif(shutil.which("cc") is None, reason="requires compiler")
@pytest.mark.parametrize(
    ("flags", "error"),
    [(["-Wl,-rpath,/tmp/unsafe"], "RPATH/RUNPATH"), (["-Wl,-z,execstack"], "исполняемый стек")],
)
def test_unsafe_elf_flags(
    tmp_path: Path, flags: list[str], error: str, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "source.c"
    source.write_text("int f(void) { return 0; }\n")
    subprocess.run(
        ["cc", "-shared", "-fPIC", *flags, str(source), "-o", str(tmp_path / "a.so")],
        check=True,
        capture_output=True,
    )
    assert audit.main(["--strict", "--expect", "1", str(tmp_path)]) == 1
    assert error in capsys.readouterr().err


def metadata(vendor: Path, name: str, version: str, requires: str = "") -> None:
    path = vendor / f"{name.replace('-', '_')}-{version}.dist-info/METADATA"
    path.parent.mkdir(parents=True)
    path.write_text(f"Name: {name}\nVersion: {version}\n{requires}")


def test_vendor_dependency_checked_without_apt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, version in deps._vendor_pins().items():
        metadata(
            tmp_path,
            name,
            version,
            "Requires-Dist: sherpa-onnx-core==1.13.8\n" if name == "sherpa-onnx" else "",
        )

    def no_apt(_name: str) -> None:
        pytest.fail("vendored dependency must not be mapped to apt")

    monkeypatch.setattr(deps, "_apt_version", no_apt)
    assert deps.main(["--strict", "--vendor", str(tmp_path)]) == 0
    core = next(tmp_path.glob("sherpa_onnx_core-*.dist-info/METADATA"))
    core.write_text("Name: sherpa_onnx_core\nVersion: 1.13.7\n")
    assert deps.main(["--strict", "--vendor", str(tmp_path)]) == 1
    core.unlink()
    assert deps.main(["--strict", "--vendor", str(tmp_path)]) == 1


def test_all_dependency_constraints_are_checked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, version in deps._vendor_pins().items():
        metadata(tmp_path, name, version)
    metadata(
        tmp_path,
        "extra",
        "1",
        "Requires-Dist: sherpa_onnx_core==1.13.8\nRequires-Dist: sherpa-onnx-core==9.0\n",
    )
    assert deps.main(["--strict", "--vendor", str(tmp_path)]) == 1


def test_sbom_keeps_both_python_hashes_and_licenses() -> None:
    wheels = {c["name"]: c for c in sbom._wheel_components()}
    lock = lockfile.load(ROOT / "packaging/appimage.lock")
    assert lock.expect_elf == 103
    for req in lock.requirements:
        if req.name in {"sherpa-onnx", "sherpa-onnx-core"}:
            comp = wheels[req.name]
            assert comp["version"] == req.version == "1.13.8"
            assert {h["content"] for h in comp["hashes"]} == set(req.hashes)
            assert comp["licenses"] == [{"expression": "Apache-2.0"}]
    assert len(wheels["sherpa-onnx"]["hashes"]) == 2


def test_appimage_license_collector_accepts_core_without_license(tmp_path: Path) -> None:
    site = tmp_path / licenses.SITE
    metadata(site, "sherpa_onnx_core", "1.13.8")
    licenses.collect(tmp_path, ROOT, None, [])
    doc = tmp_path / licenses.DOC
    index = (doc / "licenses/INDEX.txt").read_text()
    assert "sherpa_onnx_core-1.13.8" in index
    assert "sherpa-libasound" not in index  # index uses human-readable component name
    assert "alsa-lib" in index and "LGPL-2.1-or-later" in index
    assert (doc / "licenses/sherpa-onnx/ONNX-Runtime-LICENSE").is_file()
    assert (doc / "licenses/common/LGPL-2.1.txt").is_file()


def test_notice_covers_bundled_and_downloaded_components() -> None:
    notice = (ROOT / "NOTICE").read_text()
    assert "sherpa-onnx (Apache-2.0, k2-fsa) — не используется" not in notice
    for name in [
        "sherpa-onnx-core 1.13.8",
        "ONNX Runtime 1.28.2",
        "libasound",
        "LGPL-2.1-or-later",
        "pyannote-segmentation-3.0",
        "3D-Speaker CAM++",
    ]:
        assert name in notice


def sherpa_wheels() -> list[Path]:
    files = [
        *WHEELS.glob("sherpa_onnx-1.13.8-cp311-*.whl"),
        *WHEELS.glob("sherpa_onnx_core-1.13.8-*.whl"),
    ]
    if len(files) != 2:
        pytest.skip("pinned cp311 sherpa wheels not cached")
    return files


def test_cached_wheels_have_exact_five_safe_elf(tmp_path: Path) -> None:
    hashes = {
        h["content"]
        for c in sbom._wheel_components()
        if c["name"].startswith("sherpa-")
        for h in c["hashes"]
    }
    for wheel in sherpa_wheels():
        assert hashlib.sha256(wheel.read_bytes()).hexdigest() in hashes
        with zipfile.ZipFile(wheel) as z:
            z.extractall(tmp_path)
    elves = audit._collect(tmp_path)
    assert {e.path.removeprefix("/") for e in elves} == {
        n for n in NAMES if n.startswith("sherpa_")
    }
    assert audit.main(["--strict", "--expect", "5", str(tmp_path)]) == 0


def test_appimage_sbom_assigns_nested_runtime_and_alsa(tmp_path: Path) -> None:
    site = tmp_path / sbom.SITE
    for wheel in sherpa_wheels():
        with zipfile.ZipFile(wheel) as z:
            z.extractall(site)
    full = lockfile.load(ROOT / "packaging/appimage.lock")
    full.requirements = [r for r in full.requirements if r.name.startswith("sherpa-")]
    origin: dict[str, str] = {}
    components = sbom._wheel_components_r3(tmp_path, full, origin)
    assert all(c["licenses"] == [{"expression": "Apache-2.0"}] for c in components)
    nested: dict[str, Any] = {c["name"]: c for p in components for c in p["components"]}
    assert nested["libonnxruntime.so"]["version"] == "1.28.2"
    assert nested["libonnxruntime.so"]["licenses"] == [{"expression": "MIT"}]
    alsa = nested["libasound-fb5348bf.so.2.0.0"]
    assert alsa["licenses"] == [{"expression": "LGPL-2.1-or-later"}]
    assert {p["name"]: p["value"] for p in alsa["properties"]}[
        "astra-voice:sources"
    ] == "not-attached"
    assert len(origin) == 5


def test_deb_build_uses_offline_hashes_without_writing_source(tmp_path: Path) -> None:
    packaging = tmp_path / "packaging"
    (packaging / "debian").mkdir(parents=True)
    shutil.copyfile(ROOT / "packaging/build-deb.sh", packaging / "build-deb.sh")
    for rel in ["wheels.lock", "debian/changelog"]:
        shutil.copyfile(ROOT / "packaging" / rel, packaging / rel)
    source = tmp_path / "src/astra_voice/_version.py"
    source.parent.mkdir(parents=True)
    source.write_text("unchanged source")
    bins = tmp_path / "bin"
    bins.mkdir()
    python = bins / "python3"
    python.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$ARG_LOG"\nexit 42\n')
    python.chmod(0o755)
    cache = tmp_path / "wheels"
    cache.mkdir()
    import os

    env = {
        **os.environ,
        "PATH": f"{bins}:{os.environ['PATH']}",
        "ASTRA_VOICE_WHEEL_CACHE": str(cache),
        "ARG_LOG": str(tmp_path / "args"),
    }
    proc = subprocess.run(
        ["bash", str(packaging / "build-deb.sh"), "--host"], env=env, capture_output=True, text=True
    )
    assert proc.returncode == 42, proc.stdout + proc.stderr
    assert source.read_text() == "unchanged source"
    assert (tmp_path / ".build/_version.py").is_file()
    args = (tmp_path / "args").read_text().splitlines()
    assert {"--no-index", "--no-deps", "--require-hashes"} <= set(args)


def test_debian_copyright_covers_embedded_components() -> None:
    text = (ROOT / "packaging/debian/copyright").read_text()
    assert (
        "sherpa_onnx/lib/libonnxruntime.so\nCopyright: Microsoft Corporation\nLicense: MIT" in text
    )
    assert (
        "sherpa_onnx.libs/*\nCopyright: ALSA project contributors\nLicense: LGPL-2.1-or-later"
        in text
    )
    assert "License: Apache-2.0\n" in text


@pytest.mark.parametrize("private_licenses", [False, True])
def test_deb_install_target_keeps_both_wheels_and_license_texts(
    tmp_path: Path, private_licenses: bool
) -> None:
    """Run the real install recipe on a minimal fixture, without reading product code."""
    project = tmp_path / "project"
    files = [
        "src/astra_voice/__init__.py",
        "src/astra_voice/bootstrap.py",
        "scripts/astra-voice",
        "data/astra-voice.desktop",
        "data/icons/hicolor/32x32/icon.png",
        "docs/admin/meetings.md",
        "docs/admin/templates/fixture.md",
        "docs/PRIVACY.md",
        "NOTICE",
    ]
    for rel in files:
        p = project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("fixture\n")
    (project / "packaging/debian").mkdir(parents=True)
    shutil.copyfile(ROOT / "packaging/debian/rules", project / "packaging/debian/rules")
    shutil.copytree(ROOT / "packaging/appimage/licenses", project / "packaging/appimage/licenses")
    if private_licenses:
        source_licenses = project / "packaging/appimage/licenses"
        for path in source_licenses.rglob("*"):
            path.chmod(0o700 if path.is_dir() else 0o600)
    vendor = tmp_path / "vendor"
    for wheel in sherpa_wheels():
        with zipfile.ZipFile(wheel) as z:
            z.extractall(vendor)
    generated = tmp_path / "_version.py"
    generated.write_text('__version__ = "fixture"\n')
    pkg = tmp_path / "pkg"
    proc = subprocess.run(
        [
            "make",
            "-f",
            "packaging/debian/rules",
            "override_dh_auto_install",
            f"PKG={pkg}",
            f"ASTRA_VOICE_VENDOR={vendor}",
            f"ASTRA_VOICE_VERSION_FILE={generated}",
        ],
        cwd=project,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    target = pkg / "usr/lib/astra-voice/vendor"
    for rel in NAMES:
        if rel.startswith("sherpa_"):
            assert (target / rel).read_bytes() == (vendor / rel).read_bytes()
    assert (
        pkg / "usr/lib/astra-voice/astra_voice/_version.py"
    ).read_bytes() == generated.read_bytes()
    assert not (project / "src/astra_voice/_version.py").exists()
    doc = pkg / "usr/share/doc/astra-voice/licenses"
    assert (doc / "common/LGPL-2.1.txt").is_file()
    assert (doc / "sherpa-onnx/ONNX-Runtime-LICENSE").is_file()

    for path in [doc, *doc.rglob("*")]:
        assert stat.S_IMODE(path.stat().st_mode) == (0o755 if path.is_dir() else 0o644)


@pytest.mark.skipif(shutil.which("cc") is None, reason="requires compiler")
@pytest.mark.parametrize("layout", ["deb", "appimage"])
@pytest.mark.parametrize("old_dtags", [False, True], ids=["RUNPATH", "RPATH"])
@pytest.mark.parametrize(
    "rpath",
    [
        "$ORIGIN/../../../../../../../../tmp",
        "${ORIGIN}/../../../../../../../../tmp",
        "/tmp",
        "",
        "$ORIGIN:",
        "$ORIGIN/$LIB",
        "$ORIGIN/$PLATFORM",
        "$LIB",
        "$PLATFORM",
    ],
)
def test_round2_rpath_cannot_escape_vendor(
    elf_tree: Path,
    tmp_path: Path,
    layout: str,
    old_dtags: bool,
    rpath: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    vendor = elf_tree / audit.DEB_VENDOR
    if layout == "appimage":
        site = elf_tree / audit.APPIMAGE_SITE
        site.parent.mkdir(parents=True)
        shutil.move(vendor, site)
        vendor = site
    source = tmp_path / "rpath.c"
    source.write_text("int f(void) { return 0; }\n")
    elf = vendor / "sherpa_onnx/lib/libonnxruntime.so"
    flags = ["-Wl,--disable-new-dtags"] if old_dtags else []
    subprocess.run(
        ["cc", "-shared", "-fPIC", *flags, f"-Wl,-rpath,{rpath}", str(source), "-o", str(elf)],
        check=True,
        capture_output=True,
    )
    assert audit.main(["--strict", str(elf_tree)]) == 1
    assert "RPATH/RUNPATH" in capsys.readouterr().err


def test_round2_missing_gnu_stack_fails(elf_tree: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = elf_tree / audit.DEB_VENDOR / "sherpa_onnx/lib/libonnxruntime.so"
    data = bytearray(path.read_bytes())
    phoff = struct.unpack_from("<Q", data, 32)[0]
    phentsize, phnum = struct.unpack_from("<HH", data, 54)
    removed = 0
    for i in range(phnum):
        offset = phoff + i * phentsize
        if struct.unpack_from("<I", data, offset)[0] == 0x6474E551:
            struct.pack_into("<I", data, offset, 0)
            removed += 1
    assert removed == 1
    path.write_bytes(data)
    assert audit.main(["--strict", str(elf_tree)]) == 1
    assert "GNU_STACK" in capsys.readouterr().err


@pytest.mark.parametrize("layout", ["deb", "appimage"])
@pytest.mark.parametrize("target", ["external", "unlisted", "chain", "cycle"])
def test_round2_vendor_symlinks_require_listed_internal_target(
    elf_tree: Path, layout: str, target: str, capsys: pytest.CaptureFixture[str]
) -> None:
    vendor = elf_tree / audit.DEB_VENDOR
    if layout == "appimage":
        site = elf_tree / audit.APPIMAGE_SITE
        site.parent.mkdir(parents=True)
        shutil.move(vendor, site)
        vendor = site
    link = vendor / "alias.so"
    if target == "external":
        link.symlink_to("/bin/true")
    elif target == "unlisted":
        (vendor / "unlisted.txt").write_text("not an inventoried library")
        link.symlink_to("unlisted.txt")
    elif target == "chain":
        (vendor / "chain.so").symlink_to("/bin/true")
        link.symlink_to("chain.so")
    else:
        link.symlink_to("alias.so")
    assert audit.main(["--strict", str(elf_tree)]) == 1
    assert "ссылк" in capsys.readouterr().err
    if layout == "appimage":
        assert bundle.check_symlinks(elf_tree)


@pytest.mark.parametrize("provider", ["CUDA", "TensorRT", "AZURE"])
@pytest.mark.parametrize("symlink", [False, True])
def test_round2_provider_names_ignore_case(tmp_path: Path, provider: str, symlink: bool) -> None:
    path = tmp_path / f"libonnxruntime_providers_{provider}.so"
    if symlink:
        path.symlink_to("/bin/true")
    else:
        path.touch()
    assert audit.main(["--strict", "--expect", "0", str(tmp_path)]) == 1
    assert bundle.check_forbidden(tmp_path, "host")


@pytest.mark.parametrize("target", ["external-vendor", "unlisted", "cycle"])
def test_round2_bundle_vendor_symlink_inventory(tmp_path: Path, target: str) -> None:
    site = tmp_path / audit.APPIMAGE_SITE
    site.mkdir(parents=True)
    link = site / "alias.so"
    if target == "external-vendor":
        (site.parent / "outside.so").touch()
        link.symlink_to("../outside.so")
    elif target == "unlisted":
        (site / "unlisted.txt").touch()
        link.symlink_to("unlisted.txt")
    else:
        link.symlink_to("alias.so")
    assert bundle.check_symlinks(tmp_path)


@pytest.mark.parametrize("provider", ["CUDA", "TensorRT", "AZURE"])
@pytest.mark.parametrize("symlink", [False, True])
def test_round2_bundle_forbids_mixed_case_providers(
    tmp_path: Path, provider: str, symlink: bool
) -> None:
    path = tmp_path / f"libonnxruntime_providers_{provider}.so"
    if symlink:
        path.symlink_to("/bin/true")
    else:
        path.touch()
    assert bundle.check_forbidden(tmp_path, "host")


def test_round2_internal_alias_to_listed_elf_is_allowed(elf_tree: Path) -> None:
    assert bundle.VENDOR_ELFS == audit.VENDOR_ELFS
    site = elf_tree / audit.APPIMAGE_SITE
    site.parent.mkdir(parents=True)
    shutil.move(elf_tree / audit.DEB_VENDOR, site)
    (site / "alias.so").symlink_to("sherpa_onnx/lib/libonnxruntime.so")
    assert audit.main(["--strict", str(elf_tree)]) == 0
    assert bundle.check_symlinks(elf_tree) == []
