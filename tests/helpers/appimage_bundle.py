"""Фиктивный AppDir для юнит-тестов трека AppImage (без реального бандла)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import overload

VERSION = "0.2.0"
BUILD_ID = "1c866c1789f7"
KEY = f"{VERSION}-{BUILD_ID}"

APPRUN_VERSION = f"""#!/bin/sh
echo "astra-voice {VERSION}"
"""


def id_shim(tmp_path: Path, env: dict[str, str], uid: int = 1000) -> dict[str, str]:
    """AppRun видит заданный uid; неожиданные вызовы id завершаются ошибкой."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    script = bin_dir / "id"
    script.write_text(
        f'#!/bin/sh\n[ "$#" = 1 ] && [ "$1" = -u ] || exit 99\nprintf "%s\\n" {uid}\n',
        encoding="ascii",
    )
    script.chmod(0o755)
    search_path = env["PATH"].split(os.pathsep)
    if str(bin_dir) not in search_path:
        search_path.insert(0, str(bin_dir))
    return {**env, "PATH": os.pathsep.join(search_path)}


@overload
def make_bundle(
    root: Path,
    *,
    version: str = VERSION,
    build_id: str = BUILD_ID,
    apprun: str | None = None,
) -> Path: ...


@overload
def make_bundle(
    root: Path,
    *,
    version: str = VERSION,
    build_id: str = BUILD_ID,
    apprun: str | None = None,
    icons: bool,
) -> Path: ...


def make_bundle(
    root: Path,
    *,
    version: str = VERSION,
    build_id: str = BUILD_ID,
    apprun: str | None = None,
    icons: bool = False,
) -> Path:
    """Создаёт минимальную раскладку AppDir, которую принимает ``userinstall``.

    ``AppRun`` по умолчанию печатает ``astra-voice <версия>`` — как настоящий
    ``AppRun --version``; интерпретатор и ``bootstrap.py`` — пустые заглушки.
    """
    root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        Path(__file__).resolve().parents[2] / "packaging/appimage/keylib.sh", root / "keylib.sh"
    )
    (root / ".astra-voice-build").write_text(
        f"VERSION={version}\nBUILD_ID={build_id}\n", encoding="ascii"
    )
    launcher = root / "AppRun"
    launcher.write_text(
        apprun if apprun is not None else APPRUN_VERSION.replace(VERSION, version),
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    python = root / "opt" / "python3.11" / "bin" / "python3.11"
    python.parent.mkdir(parents=True, exist_ok=True)
    python.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    python.chmod(0o755)
    lib = root / "usr" / "lib" / "astra-voice"
    lib.mkdir(parents=True, exist_ok=True)
    (lib / "bootstrap.py").write_text("# stub\n", encoding="ascii")
    share = root / "usr" / "share" / "astra-voice"
    share.mkdir(parents=True, exist_ok=True)
    (share / "resource.txt").write_text("data\n", encoding="ascii")
    if icons:
        for name in (
            "48x48/apps/astravoice.png",
            "scalable/apps/astravoice.svg",
            "22x22/status/astravoice-tray-idle.svg",
        ):
            icon = root / "usr/share/icons/hicolor" / name
            icon.parent.mkdir(parents=True, exist_ok=True)
            icon.write_bytes(f"icon: {name}\n".encode())
    return root


def module_path(bundle: Path) -> Path:
    """Где лежал бы ``astra_voice/core/paths.py`` внутри бандла."""
    return bundle / "usr" / "lib" / "astra-voice" / "astra_voice" / "core" / "paths.py"


def tree_snapshot(root: Path) -> dict[str, tuple[int, bytes | str]]:
    """Содержимое дерева без следования по ссылкам: путь → (режим, байты | цель ссылки)."""
    result: dict[str, tuple[int, bytes | str]] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            info = path.lstat()
            rel = str(path.relative_to(root))
            if path.is_symlink():
                result[rel] = (info.st_mode, os.readlink(path))
            elif path.is_file():
                result[rel] = (info.st_mode, path.read_bytes())
            else:
                result[rel] = (info.st_mode, b"")
    return result
