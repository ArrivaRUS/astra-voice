"""Тонкий AppRun (packaging/appimage/AppRun): режимы, самоустановка, флаги без установки.

Настоящий AppRun в фиктивном AppDir; вместо бандлового Python — sh-заглушка:
команда ``selfinstall`` уходит в настоящий bootstrap.py (userinstall), команда
``app`` только записывает вызов в журнал — GUI не запускается. HOME и XDG — в tmp_path.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from astra_voice.core import childenv, paths, policy
from astra_voice.platform import userinstall
from conftest import REAL_GETEUID
from helpers.appimage_bundle import KEY, VERSION, id_shim, make_bundle, tree_snapshot

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
APPRUN = REPO_ROOT / "packaging" / "appimage" / "AppRun"
KEYLIB = APPRUN.with_name("keylib.sh")
KEY_CASES = REPO_ROOT / "tests" / "fixtures" / "appimage" / "keys.txt"
DEV_BOOTSTRAP = REPO_ROOT / "src" / "astra_voice" / "bootstrap.py"

FAKE_PYTHON = """#!/bin/sh
[ "$1" = -I ] && shift
boot=$1
shift
cmd=$1
shift
printf '%s|%s|HERE=%s|EAR=%s|PP=%s\\n' "$cmd" "$*" "${ASTRA_VOICE_APPIMAGE_DIR:-}" \\
    "${APPIMAGE_EXTRACT_AND_RUN:-}" "${PYTHONPATH:-}" >> "$APPRUN_TEST_LOG"
case $cmd in
    selfinstall)
        if [ -n "${APPRUN_TEST_SELFINSTALL_RC:-}" ]; then
            exit "$APPRUN_TEST_SELFINSTALL_RC"
        fi
        exec "@REAL_PY@" -I "$boot" selfinstall "$@" ;;
    app)
        env > "$APPRUN_TEST_LOG.env"
        for arg do
            if [ "$arg" = --version ]; then
                echo "astra-voice ${APPRUN_TEST_VERSION:-@VERSION@}"
            fi
        done
        exit 0 ;;
esac
exit 99
"""


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir()
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    return id_shim(
        tmp_path,
        {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "XDG_RUNTIME_DIR": str(runtime),
            "APPRUN_TEST_LOG": str(tmp_path / "calls.log"),
            "LANG": "C.UTF-8",
        },
    )


def _bundle(root: Path) -> Path:
    bundle = make_bundle(root, apprun=APPRUN.read_text(encoding="utf-8"))
    shutil.copyfile(KEYLIB, bundle / "keylib.sh")
    python = bundle / "opt" / "python3.11" / "bin" / "python3.11"
    python.write_text(
        FAKE_PYTHON.replace("@REAL_PY@", sys.executable).replace("@VERSION@", VERSION),
        encoding="utf-8",
    )
    python.chmod(0o755)
    boot = bundle / "usr" / "lib" / "astra-voice" / "bootstrap.py"
    boot.unlink()
    # -I не читает sitecustomize из PYTHONPATH: подмена нужна в самом потомке.
    boot.write_text(
        "import os, runpy\n"
        "os.geteuid = lambda: 1000\n"
        f"runpy.run_path({str(DEV_BOOTSTRAP)!r}, run_name='__main__')\n",
        encoding="utf-8",
    )
    return bundle


def _run(
    launcher: Path, args: list[str], env: dict[str, str], shell: str | None = None
) -> subprocess.CompletedProcess[str]:
    command = [shell, str(launcher)] if shell else [str(launcher)]
    return subprocess.run(
        [*command, *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )


def _calls(env: dict[str, str]) -> list[str]:
    log = Path(env["APPRUN_TEST_LOG"])
    return log.read_text(encoding="utf-8").splitlines() if log.exists() else []


def _app(env: dict[str, str]) -> Path:
    return Path(env["HOME"]) / ".local" / "share" / "astra-voice" / "app"


SHELLS = [None] + (["dash"] if shutil.which("dash") else [])


@pytest.mark.parametrize("shell", SHELLS)
def test_extract_and_run_installs_and_starts_copy(
    tmp_path: Path, env: dict[str, str], shell: str | None
) -> None:
    """Режим В: самоустановка → exec установленной копии → распаковка удалена."""
    extracted = _bundle(tmp_path / "tmp" / "appimage_extracted_0123abcd")
    env = {**env, "APPIMAGE_EXTRACT_AND_RUN": "1", "PYTHONPATH": "/evil"}
    proc = _run(extracted / "AppRun", ["--hidden"], env, shell)
    assert proc.returncode == 0, proc.stderr
    app = _app(env)
    target = app / KEY
    assert os.readlink(app / "current") == KEY
    assert not extracted.exists()
    calls = _calls(env)
    assert calls[0] == f"selfinstall|{extracted} --hidden|HERE=|EAR=|PP="
    # Смоук новой копии до переключения current — из временного каталога рядом.
    assert re.fullmatch(
        rf"app\|--version\|HERE={re.escape(str(app))}/\.tmp-{re.escape(KEY)}\.[^|]+\|EAR=\|PP=",
        calls[1],
    )
    assert calls[2:] == [f"app|--register --hidden|HERE={target}|EAR=|PP="]


def test_second_run_takes_fast_path(tmp_path: Path, env: dict[str, str]) -> None:
    first = _bundle(tmp_path / "a" / "appimage_extracted_1")
    assert _run(first / "AppRun", [], env).returncode == 0
    Path(env["APPRUN_TEST_LOG"]).unlink()
    second = _bundle(tmp_path / "b" / "appimage_extracted_2")
    proc = _run(second / "AppRun", ["--hidden"], env)
    assert proc.returncode == 0, proc.stderr
    assert _calls(env) == [f"app|--register --hidden|HERE={_app(env) / KEY}|EAR=|PP="]
    assert not second.exists()


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("state", ["missing", "directory"])
def test_incomplete_keylib_requires_reinstallation(
    tmp_path: Path, env: dict[str, str], shell: str | None, state: str
) -> None:
    target = _bundle(_app(env) / KEY)
    (target / paths.INSTALLED_MARKER).touch()
    (_app(env) / "current").symlink_to(KEY)
    info = userinstall.read_build_info(target)
    assert userinstall.installed_ok(target, info)
    (target / "keylib.sh").unlink()
    if state == "directory":
        (target / "keylib.sh").mkdir()
    assert not userinstall.installed_ok(target, info)
    extracted = _bundle(tmp_path / "appimage_extracted_repair")
    proc = _run(extracted / "AppRun", [], env, shell)
    assert proc.returncode == 0, proc.stderr
    assert _calls(env)[0] == f"selfinstall|{extracted}|HERE=|EAR=|PP="
    assert userinstall.installed_ok(target, info)
    assert (target / "keylib.sh").read_bytes() == KEYLIB.read_bytes()
    assert not extracted.exists()


def test_installed_copy_runs_in_place(tmp_path: Path, env: dict[str, str]) -> None:
    """Режим А: меню и автозапуск идут через app/current/AppRun — без установки."""
    assert _run(_bundle(tmp_path / "appimage_extracted_1") / "AppRun", [], env).returncode == 0
    Path(env["APPRUN_TEST_LOG"]).unlink()
    proc = _run(_app(env) / "current" / "AppRun", ["--hidden"], env)
    assert proc.returncode == 0, proc.stderr
    assert _calls(env) == [f"app|--hidden|HERE={_app(env) / KEY}|EAR=|PP="]
    status = _run(_app(env) / "current" / "AppRun", ["--selfinstall-status"], env)
    assert status.stdout.splitlines()[0] == "MODE=А"


@pytest.mark.parametrize("flag", sorted(userinstall.SERVICE_FLAGS - {"--selfinstall-status"}))
def test_service_flags_do_not_install(tmp_path: Path, env: dict[str, str], flag: str) -> None:
    extracted = _bundle(tmp_path / "appimage_extracted_1")
    proc = _run(extracted / "AppRun", [flag], env)
    assert proc.returncode == 0, proc.stderr
    assert not (Path(env["HOME"]) / ".local").exists()
    assert extracted.exists()
    assert _calls(env) == [f"app|{flag}|HERE={extracted}|EAR=|PP="]


def test_portable_env_does_not_install(tmp_path: Path, env: dict[str, str]) -> None:
    extracted = _bundle(tmp_path / "appimage_extracted_1")
    proc = _run(extracted / "AppRun", ["--hidden"], {**env, "ASTRA_VOICE_PORTABLE": "1"})
    assert proc.returncode == 0, proc.stderr
    assert not (Path(env["HOME"]) / ".local").exists()
    assert _calls(env) == [f"app|--hidden|HERE={extracted}|EAR=|PP="]


def test_manual_extraction_runs_without_install(tmp_path: Path, env: dict[str, str]) -> None:
    """Режим Г (squashfs-root): обычный запуск, домашняя папка не меняется."""
    root = _bundle(tmp_path / "squashfs-root")
    status = _run(root / "AppRun", ["--selfinstall-status"], env)
    assert status.stdout.splitlines() == [
        "MODE=Г",
        f"HERE={root}",
        f"KEY={KEY}",
        f"ROOT={_app(env)}",
    ]
    proc = _run(root / "AppRun", ["--hidden"], env)
    assert proc.returncode == 0, proc.stderr
    assert not (Path(env["HOME"]) / ".local").exists()
    assert _calls(env) == [f"app|--hidden|HERE={root}|EAR=|PP="]


def test_failed_install_in_extract_mode_exits(tmp_path: Path, env: dict[str, str]) -> None:
    """Режим В: сломанная сборка не становится current, из распаковки не запускаемся."""
    extracted = _bundle(tmp_path / "appimage_extracted_1")
    proc = _run(extracted / "AppRun", ["--hidden"], {**env, "APPRUN_TEST_VERSION": "9.9.9"})
    assert proc.returncode == userinstall.EXIT_FAILED
    assert "не запустилась" in proc.stderr
    assert not os.path.lexists(_app(env) / "current")
    assert extracted.exists()
    assert not [call for call in _calls(env) if call.startswith("app|--hidden")]


def test_xdg_data_home_matches_python(tmp_path: Path, env: dict[str, str]) -> None:
    data = tmp_path / "data"
    extracted = _bundle(tmp_path / "appimage_extracted_1")
    env = {**env, "XDG_DATA_HOME": str(data)}
    assert _run(extracted / "AppRun", [], env).returncode == 0
    assert os.readlink(data / "astra-voice" / "app" / "current") == KEY
    assert not (Path(env["HOME"]) / ".local").exists()


def test_corrupt_build_info(tmp_path: Path, env: dict[str, str]) -> None:
    root = _bundle(tmp_path / "appimage_extracted_1")
    (root / ".astra-voice-build").write_text("VERSION=0.2.0/..\nBUILD_ID=x\n", encoding="ascii")
    proc = _run(root / "AppRun", [], env)
    assert proc.returncode == 1
    assert "Повреждены сведения о сборке" in proc.stderr
    assert _calls(env) == []


def test_service_flags_match_python() -> None:
    """Один список служебных флагов в AppRun и userinstall.SERVICE_FLAGS."""
    text = APPRUN.read_text(encoding="utf-8")
    match = re.search(r"^\s*(--version[^)]*)\) return 0 ;;$", text, re.MULTILINE)
    assert match is not None
    flags = {flag.strip() for flag in match.group(1).split("|")}
    assert flags == userinstall.SERVICE_FLAGS


def test_apprun_uses_keylib() -> None:
    """MN-1: AppRun подключает общую проверку вместо собственной заглушки."""
    text = APPRUN.read_text(encoding="utf-8")
    assert "T1-01.10" not in text
    assert '. "$HERE/keylib.sh"' in text
    assert re.search(r"\bkey_ok\s*\(\s*\)", text) is None


def _key_cases() -> list[tuple[bool, str]]:
    cases = []
    for line in KEY_CASES.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        verdict, literal = line.split("\t", 1)
        assert verdict in {"ok", "bad"}
        name = ast.literal_eval(literal)
        assert isinstance(name, str)
        cases.append((verdict == "ok", name))
    assert cases
    return cases


@pytest.mark.parametrize(("expected", "name"), _key_cases())
@pytest.mark.parametrize("shell", SHELLS)
def test_t181_key_grammar(expected: bool, name: str, shell: str | None) -> None:
    """T-181: общие примеры для Python и настоящей POSIX sh."""
    proc = subprocess.run(
        [shell or "sh", "-c", '. "$1"; key_ok "$2"', "sh", str(KEYLIB), name],
        capture_output=True,
        text=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode in (0, 1), proc.stderr
    assert proc.stdout == proc.stderr == ""
    assert (
        (paths.APPIMAGE_KEY_RE.fullmatch(name) is not None)
        == paths.is_appimage_key(name)
        == userinstall.is_key(name)
        == expected
        == (proc.returncode == 0)
    )
    assert userinstall.KEY_RE is paths.APPIMAGE_KEY_RE


def test_t181_key_directory_symlink_rejected(tmp_path: Path) -> None:
    """Правильное имя не разрешает установке и чистке следовать по ссылке."""
    bundle = _bundle(tmp_path / "outside")
    (bundle / paths.INSTALLED_MARKER).touch()
    info = userinstall.read_build_info(bundle)
    assert userinstall.installed_ok(bundle, info)
    app = tmp_path / "app"
    app.mkdir()
    target = app / KEY
    target.symlink_to(bundle, target_is_directory=True)
    before = tree_snapshot(tmp_path)
    assert paths.is_appimage_key(target.name)
    assert userinstall.is_key(target.name)
    assert not userinstall.installed_ok(target, info)
    assert userinstall._cleanup(app, ()) == []
    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("state", ["missing", "unreadable", "directory"])
def test_unavailable_keylib_refused(tmp_path: Path, env: dict[str, str], state: str) -> None:
    if state == "unreadable" and REAL_GETEUID() == 0:
        pytest.skip("root читает файл даже после chmod 0")
    root = _bundle(tmp_path / "appimage_extracted_1")
    keylib = root / "keylib.sh"
    if state == "unreadable":
        keylib.chmod(0)
    else:
        keylib.unlink()
        if state == "directory":
            keylib.mkdir()
    proc = _run(root / "AppRun", [], env, "sh")
    assert proc.returncode == 1
    assert proc.stderr == "Повреждены сведения о сборке Astra Voice.\n"
    assert proc.stdout == ""
    assert _calls(env) == []


@pytest.mark.parametrize("shell", SHELLS)
def test_failed_id_refused_before_launch(
    tmp_path: Path, env: dict[str, str], shell: str | None
) -> None:
    bundle = _bundle(tmp_path / "appimage_extracted_user")
    (tmp_path / "bin/id").write_text("#!/bin/sh\nexit 1\n", encoding="ascii")
    before = tree_snapshot(tmp_path)
    proc = _run(bundle / "AppRun", [], env, shell)
    assert proc.returncode == 1
    assert proc.stderr == "Не удалось определить пользователя. Запуск остановлен.\n"
    assert proc.stdout == ""
    assert _calls(env) == []
    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("colon", [False, True])
@pytest.mark.parametrize("args", [[], ["--version"], ["--uninstall"], ["--selfinstall-status"]])
def test_root_refused_before_cleanup_or_launch(
    tmp_path: Path, env: dict[str, str], colon: bool, args: list[str]
) -> None:
    """T-179: отказ root сохраняет даже распаковку с двоеточием в пути."""
    bundle = _bundle(tmp_path / ("a:b" if colon else "tmp") / "appimage_extracted_root")
    env = id_shim(tmp_path, env, 0)
    # Любая внешняя команда после id означает, что ранний отказ был обойдён.
    for name in ("dirname", "readlink", "rm", "sed", "awk"):
        command = tmp_path / "bin" / name
        command.write_text('#!/bin/sh\nprintf "%s\\n" unexpected >&2\nexit 99\n')
        command.chmod(0o755)
    before = tree_snapshot(tmp_path)
    proc = _run(bundle / "AppRun", args, env, "sh")
    assert proc.returncode == 3
    assert proc.stderr == userinstall.ROOT_REFUSED_MESSAGE + "\n"
    assert proc.stdout == ""
    assert bundle.is_dir()
    assert _calls(env) == []
    assert tree_snapshot(tmp_path) == before


@pytest.mark.parametrize("colon", [False, True])
def test_nonroot_shim_keeps_existing_behavior(
    tmp_path: Path, env: dict[str, str], colon: bool
) -> None:
    bundle = _bundle(tmp_path / ("a:b" if colon else "tmp") / "appimage_extracted_user")
    env = id_shim(tmp_path, env)
    proc = _run(bundle / "AppRun", ["--version"], env, "sh")
    if colon:
        assert proc.returncode == 1
        assert "двоеточие «:»" in proc.stderr
        assert not bundle.exists()
        assert _calls(env) == []
    else:
        assert proc.returncode == 0
        assert proc.stderr == ""
        assert proc.stdout == f"astra-voice {VERSION}\n"
        assert bundle.is_dir()
        assert _calls(env) == [f"app|--version|HERE={bundle}|EAR=|PP="]
    assert not _app(env).exists()


def _mount(tmp_path: Path, bundle: Path) -> Path:
    """Фикстура /proc/self/mountinfo: бандл смонтирован через FUSE, как runtime AppImage."""
    mountinfo = tmp_path / "mountinfo"
    mountinfo.write_text(
        "22 1 8:2 / / rw,relatime shared:1 - ext4 /dev/sda2 rw\n"
        f"311 22 0:61 / {bundle} ro,nosuid,nodev,relatime shared:170 - "
        "fuse.Astra_Voice-0.2.0-x86_64.AppImage Astra_Voice-0.2.0-x86_64.AppImage "
        "ro,user_id=1000,group_id=1000\n",
        encoding="ascii",
    )
    return mountinfo


def _fuse_env(tmp_path: Path, env: dict[str, str], bundle: Path) -> dict[str, str]:
    return {
        **env,
        "APPIMAGE": str(tmp_path / "Astra_Voice-0.2.0-x86_64.AppImage"),
        "ASTRA_VOICE_TEST_MOUNTINFO": str(_mount(tmp_path, bundle)),
    }


def test_fuse_mode_detected(tmp_path: Path, env: dict[str, str]) -> None:
    bundle = _bundle(tmp_path / ".mount_Ab12Cd")
    status = _run(bundle / "AppRun", ["--selfinstall-status"], _fuse_env(tmp_path, env, bundle))
    assert status.stdout.splitlines()[0] == "MODE=Б"
    # Без $APPIMAGE тот же каталог — режим Г.
    plain = {**_fuse_env(tmp_path, env, bundle)}
    del plain["APPIMAGE"]
    assert _run(bundle / "AppRun", ["--selfinstall-status"], plain).stdout.startswith("MODE=Г")


@pytest.mark.parametrize("shell", SHELLS)
def test_fuse_mode_installs_and_starts_copy(
    tmp_path: Path, env: dict[str, str], shell: str | None
) -> None:
    bundle = _bundle(tmp_path / ".mount_Ab12Cd")
    proc = _run(bundle / "AppRun", ["--hidden"], _fuse_env(tmp_path, env, bundle), shell)
    assert proc.returncode == 0, proc.stderr
    assert os.readlink(_app(env) / "current") == KEY
    assert bundle.exists()  # монтирование не удаляется
    assert _calls(env)[-1] == f"app|--register --hidden|HERE={_app(env) / KEY}|EAR=|PP="


@pytest.mark.parametrize("shell", SHELLS)
def test_fuse_mode_falls_back_to_mount_on_failure(
    tmp_path: Path, env: dict[str, str], shell: str | None
) -> None:
    """Режим Б: обычная ошибка установки (здесь смоук) — работа из монтирования."""
    bundle = _bundle(tmp_path / ".mount_Ab12Cd")
    fuse = {**_fuse_env(tmp_path, env, bundle), "APPRUN_TEST_VERSION": "9.9.9"}
    proc = _run(bundle / "AppRun", ["--hidden"], fuse, shell)
    assert proc.returncode == 0, proc.stderr
    assert "работает без установки" in proc.stderr
    assert not os.path.lexists(_app(env) / "current")
    assert _calls(env)[-1] == f"app|--hidden|HERE={bundle}|EAR=|PP="


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("code", [userinstall.EXIT_ROOT, userinstall.EXIT_UNSAFE_SOURCE])
@pytest.mark.parametrize("mode_dir", [".mount_Ab12Cd", "appimage_extracted_1"])
def test_refusals_stop_in_any_mode(
    tmp_path: Path, env: dict[str, str], shell: str | None, code: int, mode_dir: str
) -> None:
    """P2-2: отказ от root (3) и небезопасный источник (4) не дают работать без установки."""
    bundle = _bundle(tmp_path / mode_dir)
    run_env = {**_fuse_env(tmp_path, env, bundle), "APPRUN_TEST_SELFINSTALL_RC": str(code)}
    proc = _run(bundle / "AppRun", ["--hidden"], run_env, shell)
    assert proc.returncode == code
    assert "работает без установки" not in proc.stderr
    assert [call.split("|")[0] for call in _calls(env)] == ["selfinstall"]
    assert bundle.exists()


def test_originals_saved_once_and_restorable(tmp_path: Path, env: dict[str, str]) -> None:
    """P3-5: исходные значения переживают вложенный AppRun; clean_env их возвращает."""
    extracted = _bundle(tmp_path / "appimage_extracted_1")
    run_env = {
        **env,
        "SSL_CERT_FILE": "/etc/corp/ca.pem",
        "PYTHONPATH": "/home/u/lib",
        "QT_QPA_PLATFORMTHEME": "kde",
    }
    assert _run(extracted / "AppRun", ["--hidden"], run_env).returncode == 0
    lines = Path(env["APPRUN_TEST_LOG"] + ".env").read_text(encoding="utf-8").splitlines()
    final = dict(line.split("=", 1) for line in lines if "=" in line)
    assert final["SSL_CERT_FILE"] != "/etc/corp/ca.pem"  # бандлу — хостовый набор CA
    assert "PYTHONPATH" not in final and "QT_QPA_PLATFORMTHEME" not in final
    assert final["ASTRA_VOICE_ORIG_SSL_CERT_FILE"] == "/etc/corp/ca.pem"
    assert final["ASTRA_VOICE_ORIG_PYTHONPATH"] == "/home/u/lib"
    assert "PYTHONHOME" in final["ASTRA_VOICE_ORIG_UNSET"].split()
    child = childenv.clean_env(final)
    assert child["SSL_CERT_FILE"] == "/etc/corp/ca.pem"
    assert child["PYTHONPATH"] == "/home/u/lib"
    assert child["QT_QPA_PLATFORMTHEME"] == "kde"
    assert "PYTHONHOME" not in child
    assert not [name for name in child if name.startswith(("ASTRA_VOICE_", "APPIMAGE"))]


def test_qt_paths_point_into_bundle_even_with_cyrillic_path(
    tmp_path: Path, env: dict[str, str]
) -> None:
    """R3: qt.conf PyQt5 ломается на не-ASCII пути — AppRun задаёт пути Qt явно, от своей копии."""
    bundle = _bundle(tmp_path / "Мои программы" / "Astra Voice")
    run_env = {**env, "ASTRA_VOICE_PORTABLE": "1", "QT_PLUGIN_PATH": "/home/u/qtplugins"}
    assert _run(bundle / "AppRun", ["--hidden"], run_env).returncode == 0
    lines = Path(env["APPRUN_TEST_LOG"] + ".env").read_text(encoding="utf-8").splitlines()
    final = dict(line.split("=", 1) for line in lines if "=" in line)
    qt5 = bundle / "opt" / "python3.11" / "lib" / "python3.11" / "site-packages" / "PyQt5" / "Qt5"
    assert final["QT_PLUGIN_PATH"] == str(qt5 / "plugins")
    assert final["QML2_IMPORT_PATH"] == str(qt5 / "qml")
    child = childenv.clean_env(final)
    assert child["QT_PLUGIN_PATH"] == "/home/u/qtplugins"
    assert "QML2_IMPORT_PATH" not in child


def _final_env(env: dict[str, str]) -> dict[str, str]:
    lines = Path(env["APPRUN_TEST_LOG"] + ".env").read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def test_empty_managed_variable_restored_as_empty(tmp_path: Path, env: dict[str, str]) -> None:
    """Пустая, но заданная переменная — не то же, что отсутствующая: детям — снова пустая."""
    bundle = _bundle(tmp_path / "Astra Voice")
    run_env = {**env, "ASTRA_VOICE_PORTABLE": "1", "QT_PLUGIN_PATH": ""}
    assert _run(bundle / "AppRun", ["--hidden"], run_env).returncode == 0
    final = _final_env(env)
    assert final["QT_PLUGIN_PATH"].endswith("/PyQt5/Qt5/plugins")
    assert final["ASTRA_VOICE_ORIG_QT_PLUGIN_PATH"] == ""
    assert childenv.clean_env(final)["QT_PLUGIN_PATH"] == ""


@pytest.mark.parametrize("shell", SHELLS)
def test_colon_in_path_refused(tmp_path: Path, env: dict[str, str], shell: str | None) -> None:
    """«:» ломает списки путей Qt — отказ с понятной ошибкой до любого запуска."""
    bundle = _bundle(tmp_path / "a:b")
    proc = _run(bundle / "AppRun", ["--hidden"], {**env, "ASTRA_VOICE_PORTABLE": "1"}, shell)
    assert proc.returncode == 1
    assert "двоеточие «:»" in proc.stderr
    assert _calls(env) == []


@pytest.mark.parametrize(
    ("where", "locale_env", "expected"),
    [
        ("Мои программы", {"LC_ALL": "C"}, {"LC_ALL": "C.UTF-8"}),
        ("Мои программы", {"LANG": "C.UTF-8"}, {"LANG": "C.UTF-8"}),
        ("programs", {"LC_ALL": "C"}, {"LC_ALL": "C"}),
        # Ревью: язык сообщений не трогаем — только кодировка (LC_CTYPE).
        (
            "Мои программы",
            {"LANG": "ru_RU.UTF-8", "LC_CTYPE": "C"},
            {"LANG": "ru_RU.UTF-8", "LC_CTYPE": "C.UTF-8"},
        ),
        ("Мои программы", {"LANG": "C"}, {"LANG": "C", "LC_CTYPE": "C.UTF-8"}),
    ],
)
def test_cyrillic_path_with_single_byte_locale(
    tmp_path: Path,
    env: dict[str, str],
    where: str,
    locale_env: dict[str, str],
    expected: dict[str, str],
) -> None:
    """Qt 5 зацикливается на кириллице в пути при однобайтовой кодировке: процессу — UTF-8
    (LC_ALL, если он задан, иначе только LC_CTYPE), детям — исходные значения."""
    bundle = _bundle(tmp_path / where / "Astra Voice")
    run_env = {k: v for k, v in env.items() if k != "LANG"}
    run_env.update(locale_env, ASTRA_VOICE_PORTABLE="1")
    assert _run(bundle / "AppRun", ["--hidden"], run_env).returncode == 0
    final = _final_env(env)
    names = ("LC_ALL", "LC_CTYPE", "LANG")
    assert {n: final[n] for n in names if n in final} == expected
    child = childenv.clean_env(final)
    assert {n: child[n] for n in names if n in child} == locale_env


def test_colon_refusal_drops_own_extraction(tmp_path: Path, env: dict[str, str]) -> None:
    """Ревью, nit: отказ при «:» в режиме распаковки убирает свою распаковку, чужое — нет."""
    bundle = _bundle(tmp_path / "a:b" / "appimage_extracted_0123abcd")
    proc = _run(bundle / "AppRun", ["--hidden"], env)
    assert proc.returncode == 1
    assert "двоеточие «:»" in proc.stderr
    assert not bundle.exists()
    foreign = _bundle(tmp_path / "c:d" / "Astra Voice")
    assert _run(foreign / "AppRun", ["--hidden"], env).returncode == 1
    assert foreign.exists()


def test_managed_list_matches_python() -> None:
    text = APPRUN.read_text(encoding="utf-8")
    match = re.search(r"^MANAGED='([^']*)'$", text, re.MULTILINE)
    assert match is not None
    assert tuple(match.group(1).split()) == childenv.APPRUN_MANAGED


@pytest.mark.parametrize("value", [" /data", "/data ", "relative", ""])
def test_xdg_data_home_same_as_python(
    tmp_path: Path, env: dict[str, str], monkeypatch: pytest.MonkeyPatch, value: str
) -> None:
    """P3-3: AppRun и paths.appimage_app_dir() видят один и тот же каталог app/."""
    root = _bundle(tmp_path / "squashfs-root")
    status = _run(root / "AppRun", ["--selfinstall-status"], {**env, "XDG_DATA_HOME": value})
    monkeypatch.setenv("HOME", env["HOME"])
    monkeypatch.setenv("XDG_DATA_HOME", value)
    assert f"ROOT={paths.appimage_app_dir()}" in status.stdout.splitlines()


def _policy_boot(bundle: Path, config: Path, system: Path) -> None:
    """Тестовый bootstrap бандла: путь политики, «системная версия» и трек внедряем в код.

    Через окружение Python политику не подменить (``-I``, arch/appimage.md §5);
    ``system`` — подменный лаунчер .deb, настоящий /usr/bin/astra-voice не запускается.
    """
    boot = bundle / "usr" / "lib" / "astra-voice" / "bootstrap.py"
    boot.unlink()
    boot.write_text(
        "import os, sys\n"
        "os.geteuid = lambda: 1000\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})\n"
        "from pathlib import Path\n"
        "from astra_voice import bootstrap\n"
        "from astra_voice.core import paths, policy\n"
        f"policy.POLICY_PATH = Path({str(config)!r})\n"
        f"paths.SYSTEM_EXECUTABLE = Path({str(system)!r})\n"
        "paths.install_kind = lambda: paths.InstallKind.APPIMAGE_PORTABLE\n"
        "raise SystemExit(bootstrap.main())\n",
        encoding="utf-8",
    )


@pytest.mark.parametrize("script", [APPRUN, KEYLIB])
def test_apprun_does_not_parse_policy(script: Path) -> None:
    """P2-2: одна точка правды для appimage=deny — core/policy.py, в AppRun разбора нет."""
    code = "\n".join(
        line
        for line in script.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#")
    )
    assert "policy" not in code.lower()
    assert "appimage[" not in code and "grep" not in code
    if script == KEYLIB:
        assert re.search(r"\b(?:sed|awk|expr)\b", code) is None


@pytest.mark.parametrize("shell", SHELLS)
@pytest.mark.parametrize("value", ["deny", "DENY", " no ", "false", "0", "off"])
def test_policy_denial_stops_before_copy(
    tmp_path: Path, env: dict[str, str], shell: str | None, value: str
) -> None:
    """T-180: режим В — отказ Python до создания app/ (arch/appimage.md §5), пакета нет."""
    extracted = _bundle(tmp_path / "appimage_extracted_1")
    config = tmp_path / "policy.conf"
    config.write_text(f"[astra-voice]\nappimage = {value}\n", encoding="utf-8")
    _policy_boot(extracted, config, tmp_path / "no-deb" / "astra-voice")
    proc = _run(extracted / "AppRun", [], env, shell)
    assert proc.returncode == 3
    assert proc.stderr == policy.APPIMAGE_DENIED_MESSAGE + "\n"
    assert not _app(env).exists()
    assert [call.split("|")[0] for call in _calls(env)] == ["selfinstall"]
    assert extracted.exists()
    status = _run(extracted / "AppRun", ["--selfinstall-status"], env, shell)
    assert status.returncode == 0
    assert status.stdout.startswith("MODE=В\n")


@pytest.mark.parametrize("value", ["allow", "deny-extra", "profile-secure"])
def test_policy_allows_install(tmp_path: Path, env: dict[str, str], value: str) -> None:
    extracted = _bundle(tmp_path / "appimage_extracted_1")
    config = tmp_path / "policy.conf"
    text = "profile = secure" if value == "profile-secure" else f"appimage = {value}"
    config.write_text(f"[astra-voice]\n{text}\n", encoding="utf-8")
    _policy_boot(extracted, config, tmp_path / "no-deb" / "astra-voice")
    proc = _run(extracted / "AppRun", ["--hidden"], env)
    assert proc.returncode == 0, proc.stderr
    assert os.readlink(_app(env) / "current") == KEY
    assert not extracted.exists()
    assert _calls(env)[-1] == f"app|--register --hidden|HERE={_app(env) / KEY}|EAR=|PP="


# Таблица ревью P2-2: то, что прежний grep в AppRun понимал иначе, чем Python.
POLICY_TABLE = {
    "no-section": b"appimage = deny\n",
    "other-section": b"[astra-voice]\nlanguage = ru\n[other]\nappimage = deny\n",
    "broken-line": "[astra-voice]\nappimage = deny\n[сломано\n".encode(),
    "bad-bool": b"[astra-voice]\nappimage = deny\nautostart = maybe\n",
    "bom": b"\xef\xbb\xbf[astra-voice]\nappimage = deny\n",
    "plain-deny": b"[astra-voice]\nappimage = deny\n",
}


@pytest.mark.parametrize("case", sorted(POLICY_TABLE))
def test_policy_table_matches_python(tmp_path: Path, env: dict[str, str], case: str) -> None:
    """P2-2: итог запуска AppRun по каждому файлу совпадает с разбором core/policy.py."""
    config = tmp_path / "policy.conf"
    config.write_bytes(POLICY_TABLE[case])
    denied = policy.appimage_denied(policy.load(config))
    assert denied is (case == "plain-deny")  # сверка таблицы с сегодняшним разбором
    extracted = _bundle(tmp_path / "appimage_extracted_1")
    _policy_boot(extracted, config, tmp_path / "no-deb" / "astra-voice")
    proc = _run(extracted / "AppRun", ["--hidden"], env)
    if denied:
        assert proc.returncode == 3
        assert proc.stderr == policy.APPIMAGE_DENIED_MESSAGE + "\n"
        assert not _app(env).exists()
    else:
        assert proc.returncode == 0, proc.stderr
        assert os.readlink(_app(env) / "current") == KEY


@pytest.mark.parametrize("mode_dir", [".mount_Ab12Cd", "appimage_extracted_1"])
def test_python_policy_denial_stops_without_fallback(
    tmp_path: Path, env: dict[str, str], mode_dir: str
) -> None:
    """Код 3 реального bootstrap selfinstall не разрешает запуск из монтирования."""
    bundle = _bundle(tmp_path / mode_dir)
    config = tmp_path / "policy.conf"
    config.write_text("[astra-voice]\nappimage = deny\n", encoding="utf-8")
    _policy_boot(bundle, config, tmp_path / "no-deb" / "astra-voice")
    proc = _run(bundle / "AppRun", ["--hidden"], _fuse_env(tmp_path, env, bundle))
    assert proc.returncode == 3
    assert proc.stderr == policy.APPIMAGE_DENIED_MESSAGE + "\n"
    assert [call.split("|")[0] for call in _calls(env)] == ["selfinstall"]
    assert not _app(env).exists()
    assert bundle.exists()


@pytest.mark.parametrize("mode_dir", [".mount_Ab12Cd", "appimage_extracted_1"])
def test_policy_denial_with_system_version_hands_over_to_app(
    tmp_path: Path, env: dict[str, str], mode_dir: str
) -> None:
    """P2-1: при стоящем .deb selfinstall ничего не копирует и отдаёт запуск команде app."""
    bundle = _bundle(tmp_path / mode_dir)
    config = tmp_path / "policy.conf"
    config.write_text("[astra-voice]\nappimage = deny\n", encoding="utf-8")
    system = tmp_path / "deb" / "astra-voice"
    system.parent.mkdir()
    system.write_text("#!/bin/sh\nexit 0\n", encoding="ascii")
    system.chmod(0o755)
    _policy_boot(bundle, config, system)
    proc = _run(bundle / "AppRun", ["--hidden"], _fuse_env(tmp_path, env, bundle))
    assert proc.returncode == 0, proc.stderr
    assert "работает без установки" not in proc.stderr
    assert _calls(env) == [
        f"selfinstall|{bundle} --hidden|HERE=|EAR=|PP=",
        f"app|--hidden|HERE={bundle}|EAR=|PP=",
    ]
    assert not _app(env).exists()
    # Режим В: распаковку в $TMPDIR убираем после работы app (ревью P3); монтирование — не наше.
    assert bundle.exists() is (mode_dir == ".mount_Ab12Cd")


def test_extract_mode_handoff_keeps_app_exit_code(tmp_path: Path, env: dict[str, str]) -> None:
    """Режим В, код 10: app — дочерним процессом, его код выхода — код AppRun."""
    extracted = _bundle(tmp_path / "appimage_extracted_1")
    python = extracted / "opt" / "python3.11" / "bin" / "python3.11"
    python.write_text(
        python.read_text(encoding="utf-8").replace(
            "        exit 0 ;;\nesac", "        exit 5 ;;\nesac"
        ),
        encoding="utf-8",
    )
    proc = _run(extracted / "AppRun", ["--hidden"], {**env, "APPRUN_TEST_SELFINSTALL_RC": "10"})
    assert proc.returncode == 5
    assert [call.split("|")[0] for call in _calls(env)] == ["selfinstall", "app"]
    assert not extracted.exists()
