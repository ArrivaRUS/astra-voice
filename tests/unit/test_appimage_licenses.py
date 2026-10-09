"""Документы лицензий AppImage (R3.6, T-188): collect_licenses.py на синтетическом AppDir."""

from __future__ import annotations

import importlib.util
import sys
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


lic = load("appimage_collect_licenses", ROOT / "packaging" / "appimage" / "collect_licenses.py")
SITE = "opt/python3.11/lib/python3.11/site-packages"


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    appdir = tmp_path / "AppDir"
    site = appdir / SITE
    for dist, files in {
        "numpy-1.24.2": {"LICENSE.txt": "BSD", "LICENSES_bundled.txt": "gfortran"},
        "certifi-2026.7.22": {"licenses/LICENSE": "MPL"},
        "PyQt5-5.15.11": {},
        "onnxruntime-1.24.4": {},
    }.items():
        info = site / f"{dist}.dist-info"
        info.mkdir(parents=True)
        (info / "METADATA").write_text("Name: x\n", "utf-8")
        for rel, text in files.items():
            (info / rel).parent.mkdir(parents=True, exist_ok=True)
            (info / rel).write_text(text, "utf-8")
    (site / "onnxruntime").mkdir()
    (site / "onnxruntime" / "LICENSE").write_text("MIT", "utf-8")
    (site / "onnxruntime" / "ThirdPartyNotices.txt").write_text("notices", "utf-8")
    doc = tmp_path / "debian-doc"
    (doc / "libpython3.11-minimal").mkdir(parents=True)
    (doc / "libpython3.11-minimal" / "copyright").write_text(
        "PSF\n see `/usr/share/common-licenses/GPL-2'.\n", "utf-8"
    )
    (doc / "libpython3.11-stdlib").symlink_to("libpython3.11-minimal")
    return appdir, doc


DEBS = ["libpython3.11-minimal", "libpython3.11-stdlib"]


def test_collects_all_documents(tree: tuple[Path, Path]) -> None:
    appdir, doc = tree
    index = lic.collect(appdir, ROOT, doc, DEBS)
    out = appdir / "usr" / "share" / "doc" / "astra-voice"
    assert (out / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()
    assert (out / "NOTICE").is_file()
    assert (out / "licenses" / "common" / "LGPL-3.0.txt").is_file()
    assert (
        (out / "licenses" / "PyQt5" / "LICENSE")
        .read_text("utf-8")
        .startswith("                    GNU GENERAL PUBLIC LICENSE")
    )
    assert (
        (out / "licenses" / "debian" / "libpython3.11-stdlib" / "copyright")
        .read_text("utf-8")
        .startswith("PSF")
    )
    assert (out / "licenses" / "wheels" / "certifi-2026.7.22" / "licenses" / "LICENSE").is_file()
    assert (out / "licenses" / "wheels" / "onnxruntime-1.24.4" / "ThirdPartyNotices.txt").is_file()
    components = {component for component, _ in index}
    assert {"колесо PyQt5-5.15.11", "колесо numpy-1.24.2", "Debian libpython3.11-stdlib"} <= (
        components
    )
    text = (out / "licenses" / "INDEX.txt").read_text("utf-8")
    assert "колесо PyQt5-5.15.11\tlicenses/PyQt5/LICENSE" in text
    # GCC SRPM теперь закреплены как source, устаревшее предупреждение исчезло.
    assert "# ИСХОДНИКИ НЕ ПРИЛОЖЕНЫ" not in text
    assert "libquadmath (GCC runtime, numpy.libs)" not in text


def test_wheel_without_license_fails(tree: tuple[Path, Path]) -> None:
    appdir, doc = tree
    (appdir / SITE / "mystery-1.0.dist-info").mkdir()
    with pytest.raises(lic.LicenseError, match="колесо mystery: нет файла лицензии"):
        lic.collect(appdir, ROOT, doc, DEBS)


def test_missing_common_text_fails(tree: tuple[Path, Path]) -> None:
    appdir, doc = tree
    (doc / "libpython3.11-minimal" / "copyright").write_text(
        "see /usr/share/common-licenses/GPL-1.\n", "utf-8"
    )
    with pytest.raises(lic.LicenseError, match="ссылается на common-licenses/GPL-1"):
        lic.collect(appdir, ROOT, doc, DEBS)


@pytest.mark.parametrize(
    ("text", "refs"),
    [
        ("see /usr/share/common-licenses/LGPL-2.1.\n", ["LGPL-2.1"]),
        ("in `/usr/share/common-licenses/Apache-2.0'.", ["Apache-2.0"]),
        ("GPL: /usr/share/common-licenses/GPL-3", ["GPL-3"]),
        (
            "(/usr/share/common-licenses/MPL-2.0), /usr/share/common-licenses/GPL-2,",
            ["MPL-2.0", "GPL-2"],
        ),
    ],
)
def test_common_license_references(text: str, refs: list[str]) -> None:
    """Ревью Б: версии через точку, кавычки, точка в конце предложения и конец файла."""
    assert lic._COMMON_REF_RE.findall(text) == refs


def test_debian_copyright_missing_or_escaping(tree: tuple[Path, Path], tmp_path: Path) -> None:
    appdir, doc = tree
    with pytest.raises(lic.LicenseError, match="python3.11-minimal: нет copyright"):
        lic.collect(appdir, ROOT, doc, ["python3.11-minimal"])
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside" / "copyright").write_text("x", "utf-8")
    (doc / "evil").symlink_to("../outside")
    with pytest.raises(lic.LicenseError, match="выходит из"):
        lic.collect(appdir, ROOT, doc, ["evil"])


def test_cli(tree: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    appdir, doc = tree
    args = ["--appdir", str(appdir), "--root", str(ROOT), "--debian-doc", str(doc)]
    assert lic.main([*args, "--deb", "libpython3.11-minimal"]) == 0
    assert "лицензии:" in capsys.readouterr().out
    assert lic.main([*args, "--deb", "nope"]) == 1
