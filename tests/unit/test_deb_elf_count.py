"""Гейт сборки: сколько собственных ELF уносит `.deb` и под какую glibc они собраны.

Число ELF — не любопытство, а цифра, которую заказчик подписывает по ГОСТ в
контуре с ЗПС (v1.1) и которая стоит в `packaging/INSTALL-ADMIN.md` и в SBOM.
Ожидание жёсткое: 3 `.so` onnxruntime при сборке с vendor, 0 при `--no-vendor`
(`arch/spikes/S3.md` §1.3, `docs/plans.md` M1 «Stop-and-Fix»).

Тест ничего не собирает: он проверяет пакет, который уже лежит в `dist/`.
Нет пакета — пропуск (так локальный `pytest -m unit` не требует сборки);
в CI job `deb` пакет есть, и гейт работает.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
DIST = ROOT / "dist"
ELF_AUDIT = ROOT / "tools" / "elf-audit"
#: С vendor — ровно три `.so` onnxruntime; без vendor — ни одного.
EXPECT_WITH_VENDOR = 3
EXPECT_WITHOUT_VENDOR = 0


def _deb() -> Path:
    debs = sorted(DIST.glob("astra-voice_*_amd64.deb"))
    if not debs:
        pytest.skip("нет собранного пакета в dist/ (packaging/build-deb.sh)")
    return debs[-1]


def _listing(deb: Path) -> list[str]:
    out = subprocess.run(
        ["dpkg-deb", "-c", str(deb)], capture_output=True, text=True, check=True
    ).stdout
    return [line.split(None, 5)[-1] for line in out.splitlines() if line.strip()]


@pytest.fixture(scope="module")
def deb() -> Path:
    if shutil.which("dpkg-deb") is None:
        pytest.skip("нужен dpkg-deb")
    return _deb()


def test_elf_count_matches_expectation(deb: Path) -> None:
    if shutil.which("objdump") is None:
        pytest.skip("нужен objdump (binutils)")
    has_vendor = any("/usr/lib/astra-voice/vendor/onnxruntime/" in p for p in _listing(deb))
    expect = EXPECT_WITH_VENDOR if has_vendor else EXPECT_WITHOUT_VENDOR
    proc = subprocess.run(
        ["python3", str(ELF_AUDIT), "--strict", "--expect", str(expect), str(deb)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert f"ELF найдено: {expect}" in proc.stdout


def test_layout_is_as_packaged(deb: Path) -> None:
    """bootstrap.py лежит РЯДОМ с пакетом astra_voice, а не внутри (И7)."""
    paths = set(_listing(deb))
    assert "./usr/lib/astra-voice/bootstrap.py" in paths
    assert "./usr/lib/astra-voice/astra_voice/bootstrap.py" not in paths
    assert "./usr/bin/astra-voice" in paths
    assert "./usr/share/applications/astra-voice.desktop" in paths
    assert "./usr/share/astra-voice/data/keys/release.gpg" in paths


def test_staging_dir_is_root_only(deb: Path) -> None:
    """`/var/lib/astra-voice/staging` — root:root 0700 (T1, подготовка к M8)."""
    out = subprocess.run(
        ["dpkg-deb", "-c", str(deb)], capture_output=True, text=True, check=True
    ).stdout
    rows = [ln for ln in out.splitlines() if ln.endswith("./var/lib/astra-voice/staging/")]
    assert rows, "пакет не создаёт /var/lib/astra-voice/staging"
    mode, owner = rows[0].split()[0], rows[0].split()[1]
    assert mode == "drwx------", f"права staging: {mode}"
    assert owner == "root/root", f"владелец staging: {owner}"
