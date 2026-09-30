"""T-172/T-176 (часть без запуска): окружение внешних дочерних процессов."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from astra_voice.core import childenv, paths

pytestmark = pytest.mark.unit

RUNTIME_ENV = {
    "APPIMAGE": "/home/u/Загрузки/Astra_Voice-0.2.0-x86_64.AppImage",
    "APPIMAGE_EXTRACT_AND_RUN": "1",
    "APPIMAGE_SILENT_INSTALL": "1",
    "APPDIR": "/tmp/.mount_abc",
    "ARGV0": "./Astra_Voice.AppImage",
    "OWD": "/home/u",
    "ASTRA_VOICE_APPIMAGE_DIR": "/tmp/.mount_abc",
    "ASTRA_VOICE_RESOURCES": "/tmp/.mount_abc/usr/share/astra-voice",
    "ASTRA_VOICE_RENDER": "gl",
}
USER_ENV = {
    "HOME": "/home/u",
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "LANG": "ru_RU.UTF-8",
    "DISPLAY": ":0",
    "TMPDIR": "/tmp",
    # Не тронуты нами — остаются как есть.
    "QT_QPA_PLATFORM_PLUGIN_PATH": "/opt/user/platforms",
}


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    cache = tmp_path / "cache"
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    return cache


def _launched(user: dict[str, str]) -> dict[str, str]:
    """Окружение процесса после AppRun и bootstrap (как они его меняют)."""
    env = {**RUNTIME_ENV, **user}
    childenv.save_originals(childenv.APPRUN_MANAGED, env)
    for name in childenv.APPRUN_MANAGED:
        env.pop(name, None)
    env["SSL_CERT_FILE"] = "/etc/ssl/certs/ca-certificates.crt"
    childenv.save_originals(childenv.BOOTSTRAP_MANAGED, env)
    env["QT_QUICK_CONTROLS_STYLE"] = "Default"
    env.setdefault("QT_QUICK_BACKEND", "software")
    env.setdefault("QT_XCB_GL_INTEGRATION", "none")
    env["PULSE_CLIENTCONFIG"] = "/home/u/.cache/astra-voice/pulse-client.conf"
    return env


def test_runtime_removed_originals_restored() -> None:
    user = {
        **USER_ENV,
        "SSL_CERT_FILE": "/etc/corp/ca.pem",
        "PYTHONPATH": "/home/u/lib",
        "QT_QPA_PLATFORMTHEME": "kde",
        "QT_QUICK_CONTROLS_STYLE": "Fly",
        "QT_QUICK_BACKEND": "rhi",
    }
    env = childenv.clean_env(_launched(user))
    assert env == user


def test_absent_originals_removed() -> None:
    """Чего у пользователя не было, то детям и не передаётся."""
    env = childenv.clean_env(_launched(USER_ENV))
    assert env == USER_ENV


def test_keep_pulse_config_for_pactl() -> None:
    launched = _launched({**USER_ENV, "PULSE_CLIENTCONFIG": "/home/u/my.conf"})
    assert childenv.clean_env(launched)["PULSE_CLIENTCONFIG"] == "/home/u/my.conf"
    kept = childenv.clean_env(launched, keep_pulse_config=True)
    assert kept["PULSE_CLIENTCONFIG"] == "/home/u/.cache/astra-voice/pulse-client.conf"


@pytest.mark.parametrize("keep_pulse_config", [False, True])
def test_deb_unmodified_environment_is_identity(tmp_path: Path, keep_pulse_config: bool) -> None:
    """Без переменных запуска программы окружение .deb сохраняется целиком (§10.4)."""
    user = {
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "PATH": "/usr/bin:/bin",
        "LANG": "ru_RU.UTF-8",
        "LC_ALL": "ru_RU.UTF-8",
        "TMPDIR": str(tmp_path / "tmp"),
        **{name: f"user-{name}" for name in childenv.RESTORABLE},
    }
    before = user.copy()
    result = childenv.clean_env(user, keep_pulse_config=keep_pulse_config)
    assert result == before
    assert result is not user
    assert user == before
    assert {**result, "LC_ALL": "C"} == {**before, "LC_ALL": "C"}


def test_deb_bootstrap_environment_restored(tmp_path: Path) -> None:
    """pactl сохраняет запрет autospawn, программы пользователя — исходное окружение."""
    user = {"HOME": str(tmp_path), "TMPDIR": str(tmp_path / "tmp")}
    launched = {
        **user,
        "QT_QUICK_BACKEND": "software",
        "QT_XCB_GL_INTEGRATION": "none",
        "PULSE_CLIENTCONFIG": str(tmp_path / "pulse-client.conf"),
        "ASTRA_VOICE_ORIG_UNSET": "QT_QUICK_BACKEND QT_XCB_GL_INTEGRATION PULSE_CLIENTCONFIG",
    }
    assert childenv.clean_env(launched) == user
    assert childenv.clean_env(launched, keep_pulse_config=True) == {
        **user,
        "PULSE_CLIENTCONFIG": launched["PULSE_CLIENTCONFIG"],
    }


def test_save_is_first_write_only() -> None:
    env = {"SSL_CERT_FILE": "/corp.pem"}
    childenv.save_originals(["SSL_CERT_FILE", "PYTHONHOME"], env)
    env["SSL_CERT_FILE"] = "/bundle.pem"
    env["PYTHONHOME"] = "/bundle"
    childenv.save_originals(["SSL_CERT_FILE", "PYTHONHOME"], env)
    assert env["ASTRA_VOICE_ORIG_SSL_CERT_FILE"] == "/corp.pem"
    assert env["ASTRA_VOICE_ORIG_UNSET"] == "PYTHONHOME"
    assert childenv.clean_env(env) == {"SSL_CERT_FILE": "/corp.pem"}


def test_save_rejects_unknown_names() -> None:
    with pytest.raises(ValueError):
        childenv.save_originals(["HOME"], {})


def test_unknown_orig_names_ignored() -> None:
    """В окружении могли оказаться чужие ASTRA_VOICE_ORIG_*: восстанавливаем только свои."""
    env = childenv.clean_env(
        {
            "ASTRA_VOICE_ORIG_LD_LIBRARY_PATH": "/evil",
            "ASTRA_VOICE_ORIG_UNSET": "HOME",
            "HOME": "/h",
        }
    )
    assert env == {"HOME": "/h"}


def test_private_tmp_only_on_request(isolated_cache: Path) -> None:
    assert childenv.clean_env({"TMPDIR": "/tmp"}) == {"TMPDIR": "/tmp"}
    assert not isolated_cache.exists()
    env = childenv.clean_env({"TMPDIR": "/tmp"}, private_tmp=True)
    tmp = Path(env["TMPDIR"])
    assert tmp == isolated_cache / "astra-voice" / "tmp"
    info = tmp.lstat()
    assert stat.S_ISDIR(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o700
    assert info.st_uid == os.getuid()


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
        childenv.clean_env({}, private_tmp=True)
