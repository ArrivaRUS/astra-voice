"""T-172/T-176 (часть без запуска): окружение внешних дочерних процессов."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from astra_voice.core import childenv, paths

pytestmark = pytest.mark.unit

BUNDLE_ENV = {
    "APPIMAGE": "/home/u/Загрузки/Astra_Voice-0.2.0-x86_64.AppImage",
    "APPDIR": "/tmp/.mount_abc",
    "ARGV0": "./Astra_Voice.AppImage",
    "OWD": "/home/u",
    "APPIMAGE_EXTRACT_AND_RUN": "1",
    "ASTRA_VOICE_APPIMAGE_DIR": "/tmp/.mount_abc",
    "ASTRA_VOICE_RESOURCES": "/tmp/.mount_abc/usr/share/astra-voice",
    "ASTRA_VOICE_RENDER": "gl",
    "QT_PLUGIN_PATH": "/tmp/.mount_abc/plugins",
    "QT_QPA_PLATFORM_PLUGIN_PATH": "/tmp/.mount_abc/platforms",
    "QML2_IMPORT_PATH": "/tmp/.mount_abc/qml",
    "QML_IMPORT_PATH": "/tmp/.mount_abc/qml",
    "QT_QUICK_BACKEND": "software",
    "QT_QUICK_CONTROLS_STYLE": "Default",
    "QT_XCB_GL_INTEGRATION": "none",
}
USER_ENV = {
    "HOME": "/home/u",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "ru_RU.UTF-8",
    "DISPLAY": ":0",
    "XDG_RUNTIME_DIR": "/run/user/1000",
    "SSL_CERT_FILE": "/etc/ssl/certs/ca-certificates.crt",
    "QT_QPA_PLATFORM": "xcb",
    "TMPDIR": "/tmp",
}


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    return cache


def test_bundle_variables_removed_user_kept(isolated_cache: Path) -> None:
    env = childenv.clean_env({**BUNDLE_ENV, **USER_ENV})
    assert not set(BUNDLE_ENV) & set(env)
    expected = {**USER_ENV, "TMPDIR": str(isolated_cache / "astra-voice" / "tmp")}
    assert env == expected


def test_private_tmp_is_private(isolated_cache: Path) -> None:
    env = childenv.clean_env({"TMPDIR": "/tmp"})
    tmp = Path(env["TMPDIR"])
    assert tmp == isolated_cache / "astra-voice" / "tmp"
    info = tmp.lstat()
    assert stat.S_ISDIR(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o700
    assert info.st_uid == os.getuid()


def test_without_private_tmp_keeps_user_tmpdir(isolated_cache: Path) -> None:
    env = childenv.clean_env({**BUNDLE_ENV, **USER_ENV}, private_tmp=False)
    assert env == USER_ENV
    assert not isolated_cache.exists()


def test_default_source_is_process_env_and_not_modified(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APPIMAGE_EXTRACT_AND_RUN", "1")
    monkeypatch.setenv("ASTRA_VOICE_APPIMAGE_DIR", "/tmp/.mount_x")
    monkeypatch.setenv("LANG", "ru_RU.UTF-8")
    env = childenv.clean_env()
    assert "APPIMAGE_EXTRACT_AND_RUN" not in env
    assert "ASTRA_VOICE_APPIMAGE_DIR" not in env
    assert env["LANG"] == "ru_RU.UTF-8"
    assert os.environ["APPIMAGE_EXTRACT_AND_RUN"] == "1"


def test_symlinked_tmp_refused(isolated_cache: Path, tmp_path: Path) -> None:
    """Подложенная ссылка вместо приватного каталога — отказ, а не чужой TMPDIR."""
    (isolated_cache / "astra-voice").mkdir(parents=True, mode=0o700)
    (tmp_path / "elsewhere").mkdir()
    (isolated_cache / "astra-voice" / "tmp").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(paths.PathError):
        childenv.clean_env({})
