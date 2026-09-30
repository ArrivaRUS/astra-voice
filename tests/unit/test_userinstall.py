"""T-164: самоустановка AppImage в домашнюю папку (arch/appimage.md §1).

Временный HOME и XDG-каталоги в tmp_path; реальный ~/.local не трогается.
Смоук новой копии — настоящий запуск фиктивного AppRun (sh-скрипт).
"""

from __future__ import annotations

import ctypes
import fcntl
import os
import stat
import threading
from pathlib import Path

import pytest

from astra_voice.core import paths
from astra_voice.platform import autostart, userinstall
from helpers.appimage_bundle import BUILD_ID, KEY, VERSION, make_bundle, tree_snapshot

pytestmark = pytest.mark.unit

KEY2 = "0.3.0-aaaaaaaaaaaa"
KEY3 = "0.4.0~rc1-bbbbbbbbbbbb"
# Настоящий сброс на диск — до подмены в фикстуре synced.
_REAL_SYNC = userinstall._sync_filesystem


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("DATA", "CONFIG", "CACHE", "STATE"):
        monkeypatch.delenv(f"XDG_{name}_HOME", raising=False)
    runtime = tmp_path / "run"
    runtime.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    monkeypatch.delenv(paths.PORTABLE_ENV, raising=False)
    return home


@pytest.fixture(autouse=True)
def synced(monkeypatch: pytest.MonkeyPatch) -> list[Path]:
    """syncfs корневого тома в каждом тесте не нужен: записываем вызовы.

    Настоящий вызов проверяет test_sync_filesystem_real (и тесты AppRun).
    """
    calls: list[Path] = []
    monkeypatch.setattr(userinstall, "_sync_filesystem", calls.append)
    return calls


def _app(home: Path) -> Path:
    return home / ".local" / "share" / "astra-voice" / "app"


def _bundle(tmp_path: Path, key: str = KEY, **kwargs: str) -> Path:
    version, build_id = key.rsplit("-", 1)
    return make_bundle(
        tmp_path / f"appimage_extracted_{build_id}", version=version, build_id=build_id, **kwargs
    )


def _link(app: Path, name: str) -> str:
    return os.readlink(app / name)


def test_fresh_install(tmp_path: Path, home: Path) -> None:
    src = _bundle(tmp_path)
    before = tree_snapshot(src)
    result = userinstall.install_from_dir(src)
    app = _app(home)
    assert result == userinstall.InstallResult(KEY, app / KEY, True, None)
    assert _link(app, "current") == KEY
    assert not os.path.lexists(app / "previous")
    installed = tree_snapshot(app / KEY)
    assert installed.pop(paths.INSTALLED_MARKER)[1] == b""
    assert installed == before
    assert tree_snapshot(src) == before
    assert stat.S_IMODE(app.stat().st_mode) == 0o700
    assert stat.S_IMODE(app.parent.stat().st_mode) == 0o700
    assert sorted(os.listdir(app)) == [".install.lock", KEY, "current"]
    assert paths.appimage_current_apprun().resolve() == app / KEY / "AppRun"


def test_same_key_is_not_copied_again(tmp_path: Path, home: Path) -> None:
    src = _bundle(tmp_path)
    userinstall.install_from_dir(src)
    sentinel = _app(home) / KEY / "usr" / "share" / "astra-voice" / "resource.txt"
    sentinel.write_text("installed copy\n", encoding="ascii")
    result = userinstall.install_from_dir(src)
    assert not result.copied
    assert sentinel.read_text(encoding="ascii") == "installed copy\n"
    assert _link(_app(home), "current") == KEY


def test_upgrade_keeps_previous_and_cleans_older(tmp_path: Path, home: Path) -> None:
    """Две копии (В1): третья установка удаляет самую старую по маске."""
    app = _app(home)
    userinstall.install_from_dir(_bundle(tmp_path))
    second = userinstall.install_from_dir(_bundle(tmp_path, KEY2))
    assert second.previous == KEY
    assert (_link(app, "current"), _link(app, "previous")) == (KEY2, KEY)
    third = userinstall.install_from_dir(_bundle(tmp_path, KEY3))
    assert third.previous == KEY2
    assert (_link(app, "current"), _link(app, "previous")) == (KEY3, KEY2)
    assert not (app / KEY).exists()
    assert sorted(os.listdir(app)) == [".install.lock", KEY2, KEY3, "current", "previous"]


def test_old_file_rolls_back(tmp_path: Path, home: Path) -> None:
    """Запуск старого файла делает его версию текущей без повторного копирования."""
    app = _app(home)
    userinstall.install_from_dir(_bundle(tmp_path))
    userinstall.install_from_dir(_bundle(tmp_path, KEY2))
    result = userinstall.install_from_dir(_bundle(tmp_path))
    assert not result.copied
    assert (_link(app, "current"), _link(app, "previous")) == (KEY, KEY2)


def test_cleanup_touches_only_own_copies(tmp_path: Path, home: Path) -> None:
    """Модели, настройки, чужие имена и ссылки в app/ чистка не трогает."""
    data = home / ".local" / "share" / "astra-voice"
    models = data / "models" / "gigaam"
    models.mkdir(parents=True)
    (models / "model.onnx").write_bytes(b"weights")
    settings = home / ".config" / "astra-voice" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text("{}", encoding="ascii")
    app = data / "app"
    app.mkdir(mode=0o700)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("keep", encoding="ascii")
    (app / "0.0.9-cccccccccccc").mkdir()  # старая копия — удаляется
    (app / ".tmp-0.2.0-1c866c1789f7.x1y2z3").mkdir()  # брошенная распаковка — удаляется
    (app / ".tmp-link-deadbeef").symlink_to(outside)  # брошенная ссылка — удаляется сама
    (app / "0.0.8-dddddddddddd").symlink_to(outside)  # ссылка с именем KEY — не трогаем
    (app / "notes").mkdir()  # чужое имя — не трогаем
    (app / "readme.txt").write_text("mine", encoding="utf-8")
    before_outside = tree_snapshot(outside)
    before_models = tree_snapshot(data / "models")

    userinstall.install_from_dir(_bundle(tmp_path))

    assert sorted(os.listdir(app)) == [
        ".install.lock",
        "0.0.8-dddddddddddd",
        KEY,
        "current",
        "notes",
        "readme.txt",
    ]
    assert tree_snapshot(outside) == before_outside
    assert tree_snapshot(data / "models") == before_models
    assert settings.read_text(encoding="ascii") == "{}"


def test_running_key_protects_copy(tmp_path: Path, home: Path) -> None:
    """Р5: работающая версия не удаляется, даже если уже не current/previous."""
    app = _app(home)
    userinstall.install_from_dir(_bundle(tmp_path))
    userinstall.write_running_key(KEY)
    assert userinstall.read_running_key() == KEY
    userinstall.install_from_dir(_bundle(tmp_path, KEY2))
    userinstall.install_from_dir(_bundle(tmp_path, KEY3))
    assert (app / KEY).is_dir()
    assert (_link(app, "current"), _link(app, "previous")) == (KEY3, KEY2)
    # Значение файла — только строка для сравнения с именами каталогов.
    (paths.runtime_dir() / "running-key").write_text("../../outside\n", encoding="ascii")
    assert userinstall.cleanup() == [KEY]
    assert sorted(p for p in os.listdir(app) if userinstall.is_key(p)) == [KEY2, KEY3]


@pytest.mark.parametrize(
    "kind",
    ["symlink", "fifo", "directory", "foreign", "garbage", "path", "nonascii", "large", "missing"],
)
def test_running_key_rejects_unsafe_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    """T-199: опасная метка отвергается, следующий кандидат всё равно читается."""
    runtime = tmp_path / "runtime"
    fallback = tmp_path / "fallback"
    runtime.mkdir()
    fallback.mkdir()
    monkeypatch.setattr(paths, "existing_runtime_dirs", lambda: (runtime, fallback))
    marker = runtime / "running-key"
    if kind == "symlink":
        target = tmp_path / "valid-key"
        target.write_text(KEY, encoding="ascii")
        marker.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(marker)
        original_open = os.open

        def nonblocking_open(path: Path, flags: int) -> int:
            # Проверка до настоящего open: регрессия не повесит процесс тестов.
            assert flags & os.O_NONBLOCK
            return original_open(path, flags)

        monkeypatch.setattr(os, "open", nonblocking_open)
    elif kind == "directory":
        marker.mkdir()
    elif kind != "missing":
        content = {
            "foreign": KEY.encode("ascii"),
            "garbage": b"not a key",
            "path": b"../../outside",
            "nonascii": KEY.encode("ascii") + b"\xff",
            "large": KEY.encode("ascii") + b" " * userinstall.RUNNING_KEY_LIMIT,
        }[kind]
        marker.write_bytes(content)
        if kind == "foreign":
            foreign_uid = os.getuid() + 1
            monkeypatch.setattr(os, "getuid", lambda: foreign_uid)
    assert userinstall.read_running_key() is None
    assert userinstall.read_running_keys() == frozenset()
    if kind == "foreign":
        return
    (fallback / "running-key").write_text(KEY2 + "\n", encoding="ascii")
    assert userinstall.read_running_key() == KEY2
    assert userinstall.read_running_keys() == frozenset({KEY2})


@pytest.mark.parametrize("size", [len(KEY) + 2, userinstall.RUNNING_KEY_LIMIT])
def test_running_key_accepts_valid_ascii_at_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, size: int
) -> None:
    monkeypatch.setattr(paths, "existing_runtime_dirs", lambda: (tmp_path,))
    (tmp_path / "running-key").write_bytes(
        b"\n" + KEY.encode("ascii") + b" " * (size - len(KEY) - 1)
    )
    assert userinstall.read_running_key() == KEY
    assert userinstall.read_running_keys() == frozenset({KEY})


@pytest.mark.parametrize("operation", ["install", "cleanup", "remove"])
def test_all_running_keys_protect_copies(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch, operation: str
) -> None:
    """T-199: актуальная и устаревшая метки защищают обе копии при любой чистке."""
    monkeypatch.setattr(paths, "FALLBACK_TMP_DIR", tmp_path)
    userinstall.write_running_key(KEY)
    fallback = tmp_path / f"astra-voice-{os.getuid()}"
    fallback.mkdir(mode=0o700)
    (fallback / "running-key").write_text(KEY2 + "\n", encoding="ascii")
    assert userinstall.read_running_key() == KEY
    assert userinstall.read_running_keys() == frozenset({KEY, KEY2})
    assert userinstall.status().running == KEY

    app = _app(home)
    app.mkdir(parents=True)
    obsolete = "0.1.0-cccccccccccc"
    for key in (KEY, KEY2, obsolete):
        (app / key).mkdir()
    if operation == "install":
        userinstall.install_from_dir(_bundle(tmp_path, KEY3))
    else:
        (app / KEY3).mkdir()
        (app / "current").symlink_to(KEY3)
        if operation == "cleanup":
            assert userinstall.cleanup() == [obsolete]
        else:
            result = userinstall.remove_program(keep=KEY3)
            assert result.kept == tuple(sorted((KEY, KEY2, KEY3)))
            assert obsolete in result.removed
    assert (app / KEY).is_dir()
    assert (app / KEY2).is_dir()
    assert (app / KEY3).is_dir()
    assert not (app / obsolete).exists()


def test_not_enough_space_creates_nothing(tmp_path: Path, home: Path) -> None:
    src = _bundle(tmp_path)
    with pytest.raises(userinstall.NotEnoughSpaceError, match="нужно ещё [0-9]+ МБ"):
        userinstall.install_from_dir(src, min_free=1 << 62)
    assert os.listdir(home) == []


def test_smoke_failure_keeps_current(tmp_path: Path, home: Path) -> None:
    app = _app(home)
    userinstall.install_from_dir(_bundle(tmp_path))
    broken = _bundle(tmp_path, KEY2, apprun="#!/bin/sh\necho 'astra-voice 9.9.9'\n")
    with pytest.raises(userinstall.SmokeError, match="не запустилась"):
        userinstall.install_from_dir(broken)
    assert _link(app, "current") == KEY
    assert sorted(os.listdir(app)) == [".install.lock", KEY, "current"]


def test_smoke_nonzero_exit(tmp_path: Path, home: Path) -> None:
    src = _bundle(tmp_path, apprun=f"#!/bin/sh\necho 'astra-voice {VERSION}'\nexit 1\n")
    with pytest.raises(userinstall.SmokeError):
        userinstall.install_from_dir(src)
    assert sorted(os.listdir(_app(home))) == [".install.lock"]


def test_smoke_runs_with_clean_env(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Смоук: без переменных бандла и с TMPDIR внутри cache_dir()."""
    monkeypatch.setenv("APPIMAGE_EXTRACT_AND_RUN", "1")
    monkeypatch.setenv("APPIMAGE", "/tmp/x.AppImage")
    monkeypatch.setenv(paths.APPIMAGE_DIR_ENV, "/tmp/.mount_x")
    apprun = f'#!/bin/sh\nenv > "$TMPDIR/smoke-env"\necho "astra-voice {VERSION}"\n'
    userinstall.install_from_dir(_bundle(tmp_path, apprun=apprun))
    tmp = home / ".cache" / "astra-voice" / "tmp"
    names = {line.split("=", 1)[0] for line in (tmp / "smoke-env").read_text().splitlines()}
    assert not names & {"APPIMAGE_EXTRACT_AND_RUN", "APPIMAGE", paths.APPIMAGE_DIR_ENV}
    assert "HOME" in names


def test_app_dir_symlink_refused(tmp_path: Path, home: Path) -> None:
    data = home / ".local" / "share" / "astra-voice"
    data.mkdir(parents=True, mode=0o700)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (data / "app").symlink_to(elsewhere)
    with pytest.raises(paths.PathError):
        userinstall.install_from_dir(_bundle(tmp_path))
    assert os.listdir(elsewhere) == []


def test_current_not_a_link_refused(tmp_path: Path, home: Path) -> None:
    app = _app(home)
    app.mkdir(parents=True, mode=0o700)
    (app / "current").mkdir()
    with pytest.raises(userinstall.UserInstallError, match="не ссылка"):
        userinstall.install_from_dir(_bundle(tmp_path))
    assert (app / "current").is_dir()


def test_broken_copy_replaced(tmp_path: Path, home: Path) -> None:
    """Недокопированный app/<KEY> без маркера заменяется целой копией."""
    app = _app(home)
    (app / KEY).mkdir(parents=True)
    (app / KEY / "partial").write_bytes(b"x")
    result = userinstall.install_from_dir(_bundle(tmp_path))
    assert result.copied
    assert not (app / KEY / "partial").exists()
    assert (app / KEY / paths.INSTALLED_MARKER).is_file()
    assert not [name for name in os.listdir(app) if name.startswith(".tmp-")]


def test_lock_timeout(tmp_path: Path, home: Path) -> None:
    app = _app(home)
    app.mkdir(parents=True, mode=0o700)
    fd = os.open(app / ".install.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        with pytest.raises(userinstall.LockTimeoutError):
            userinstall.install_from_dir(_bundle(tmp_path), lock_timeout=0.3)
    finally:
        os.close(fd)
    assert sorted(os.listdir(app)) == [".install.lock"]


def test_parallel_install_waits_and_reuses_copy(tmp_path: Path, home: Path) -> None:
    """Вторая установка ждёт flock первой и берёт готовую копию без копирования."""
    first_src = _bundle(tmp_path)
    second_src = make_bundle(tmp_path / "second", version=VERSION, build_id=BUILD_ID)
    in_smoke = threading.Event()
    release = threading.Event()
    results: dict[str, userinstall.InstallResult | BaseException] = {}

    def slow_smoke(copy: Path, info: userinstall.BuildInfo) -> None:
        in_smoke.set()
        assert release.wait(10)

    def run(name: str, src: Path, smoke: userinstall.Smoke | None) -> None:
        try:
            results[name] = userinstall.install_from_dir(src, smoke=smoke, lock_timeout=10)
        except BaseException as exc:  # noqa: BLE001 — передаём в основной поток
            results[name] = exc

    first = threading.Thread(target=run, args=("first", first_src, slow_smoke))
    first.start()
    assert in_smoke.wait(10)
    second = threading.Thread(target=run, args=("second", second_src, None))
    second.start()
    second.join(0.3)
    assert second.is_alive()  # ждёт замок
    release.set()
    first.join(10)
    second.join(10)
    assert isinstance(results["first"], userinstall.InstallResult)
    assert isinstance(results["second"], userinstall.InstallResult)
    assert results["first"].copied
    assert not results["second"].copied


def test_switch_current_directly(tmp_path: Path, home: Path) -> None:
    app = _app(home)
    userinstall.install_from_dir(_bundle(tmp_path))
    userinstall.install_from_dir(_bundle(tmp_path, KEY2))
    assert userinstall.switch_current(KEY) == KEY2
    assert (_link(app, "current"), _link(app, "previous")) == (KEY, KEY2)
    with pytest.raises(ValueError):
        userinstall.switch_current("../evil")


@pytest.mark.parametrize(
    ("name", "ok"),
    [
        ("0.2.0-1c866c1789f7", True),
        ("10.20.30~rc1.2-0123456789ab", True),
        ("0.2.0-1C866C1789F7", False),
        ("0.2.0-1c866c1789f", False),
        ("0.2.0-1c866c1789f7\n", False),
        ("0.2-1c866c1789f7", False),
        ("٠.٢.٠-1c866c1789f7", False),  # цифры не ASCII
        ("../0.2.0-1c866c1789f7", False),
        ("0.2.0~rc/1-1c866c1789f7", False),
        (".tmp-0.2.0-1c866c1789f7", False),
        ("current", False),
    ],
)
def test_key_mask(name: str, ok: bool) -> None:
    assert userinstall.is_key(name) is ok


@pytest.mark.parametrize(
    "content",
    [
        b"VERSION=../x\nBUILD_ID=1c866c1789f7\n",
        b"VERSION=0.2.0\n",
        b"VERSION=0.2.0\nBUILD_ID=1c866c1789f7/..\n",
        b"VERSION=0.2.0\nBUILD_ID=1c866c1789f7\n" + b"#" * 5000,
        "VERSION=0.2.0\nBUILD_ID=1c866c1789f7\n# ё\n".encode(),
    ],
)
def test_bad_build_info_refused(tmp_path: Path, home: Path, content: bytes) -> None:
    src = _bundle(tmp_path)
    (src / ".astra-voice-build").write_bytes(content)
    with pytest.raises(userinstall.UserInstallError, match="сведения о сборке"):
        userinstall.install_from_dir(src)
    assert os.listdir(home) == []


def test_build_info_symlink_refused(tmp_path: Path, home: Path) -> None:
    src = _bundle(tmp_path)
    real = tmp_path / "real-build"
    real.write_bytes((src / ".astra-voice-build").read_bytes())
    (src / ".astra-voice-build").unlink()
    (src / ".astra-voice-build").symlink_to(real)
    with pytest.raises(userinstall.UserInstallError, match="сведения о сборке"):
        userinstall.install_from_dir(src)


def test_service_flags_and_portable_skip(
    tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    src = str(_bundle(tmp_path))
    for flag in sorted(userinstall.SERVICE_FLAGS):
        assert userinstall.selfinstall_main([src, "--hidden", flag]) == userinstall.EXIT_SKIPPED
    portable = {paths.PORTABLE_ENV: "1"}
    assert userinstall.selfinstall_main([src], portable) == userinstall.EXIT_SKIPPED
    assert os.listdir(home) == []
    assert capsys.readouterr().err == ""


def test_selfinstall_main(tmp_path: Path, home: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert userinstall.selfinstall_main([]) == userinstall.EXIT_USAGE
    assert userinstall.selfinstall_main(["relative/dir"]) == userinstall.EXIT_USAGE
    src = _bundle(tmp_path)
    assert userinstall.selfinstall_main([str(src), "--hidden"], {}) == userinstall.EXIT_OK
    assert _link(_app(home), "current") == KEY
    broken = _bundle(tmp_path, KEY2, apprun="#!/bin/sh\nexit 1\n")
    capsys.readouterr()
    assert userinstall.selfinstall_main([str(broken)], {}) == userinstall.EXIT_FAILED
    assert "не запустилась" in capsys.readouterr().err


def test_error_messages_hide_home(tmp_path: Path, home: Path) -> None:
    assert userinstall.tilde(home / ".local" / "x") == "~/.local/x"
    assert userinstall.tilde("/usr/bin/x") == "/usr/bin/x"


def test_check_source_hygiene_creates_nothing(tmp_path: Path) -> None:
    """Гигиена без отказов и новых файлов: решение 30.09, BL-1a, вариант б."""
    before = tree_snapshot(tmp_path)
    userinstall.check_source(tmp_path)
    assert tree_snapshot(tmp_path) == before


def test_source_and_launchers_have_no_t1_reminders() -> None:
    root = Path(__file__).resolve().parents[2]
    files = sorted((root / "src/astra_voice").rglob("*.py"))
    assert files
    files.extend(root / "packaging/appimage" / name for name in ("AppRun", "keylib.sh"))
    marker = "T1-" + "01.10"
    remaining = [
        str(path.relative_to(root)) for path in files if marker in path.read_text(encoding="utf-8")
    ]
    assert not remaining, remaining


@pytest.mark.parametrize("kind", list(paths.InstallKind))
@pytest.mark.parametrize("euid", [0, 1000])
def test_refuse_root_only_in_appimage(
    monkeypatch: pytest.MonkeyPatch, kind: paths.InstallKind, euid: int
) -> None:
    monkeypatch.setattr(paths, "install_kind", lambda: kind)
    monkeypatch.setattr(os, "geteuid", lambda: euid)
    if kind.is_appimage and euid == 0:
        with pytest.raises(userinstall.RootRefusedError) as caught:
            userinstall.refuse_root()
        assert caught.value.exit_code == 3
        assert str(caught.value) == userinstall.ROOT_REFUSED_MESSAGE
    else:
        userinstall.refuse_root()


@pytest.mark.parametrize(
    "kind", [paths.InstallKind.APPIMAGE_PORTABLE, paths.InstallKind.APPIMAGE_INSTALLED]
)
@pytest.mark.parametrize("installed", [False, True])
@pytest.mark.parametrize("operation", ["install", "register", "unregister", "remove"])
def test_root_refusal_changes_no_files(
    tmp_path: Path,
    home: Path,
    monkeypatch: pytest.MonkeyPatch,
    kind: paths.InstallKind,
    installed: bool,
    operation: str,
) -> None:
    """T-179: отказ до копирования, записи меню, автозапуска и удаления."""
    src = make_bundle(tmp_path / "bundle", icons=True)
    monkeypatch.setattr(os, "geteuid", lambda: 1000)
    if installed:
        userinstall.install_from_dir(src)
        userinstall.register()
        startup = home / ".config/autostart/astra-voice.desktop"
        startup.parent.mkdir(parents=True)
        startup.write_bytes(autostart.entry_bytes(str(paths.appimage_current_apprun())))
    monkeypatch.setattr(paths, "install_kind", lambda: kind)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    before = tree_snapshot(tmp_path)
    metadata = {p: (p.lstat().st_mtime_ns, p.lstat().st_ctime_ns) for p in tmp_path.rglob("*")}
    actions = {
        "install": lambda: userinstall.install_from_dir(src),
        "register": userinstall.register,
        "unregister": userinstall.unregister,
        "remove": userinstall.remove_program,
    }
    with pytest.raises(userinstall.RootRefusedError) as caught:
        actions[operation]()
    assert caught.value.exit_code == 3
    assert str(caught.value) == userinstall.ROOT_REFUSED_MESSAGE
    assert tree_snapshot(tmp_path) == before
    assert {p: (p.lstat().st_mtime_ns, p.lstat().st_ctime_ns) for p in metadata} == metadata


@pytest.mark.parametrize(
    "kind", [paths.InstallKind.APPIMAGE_PORTABLE, paths.InstallKind.APPIMAGE_INSTALLED]
)
@pytest.mark.parametrize("flag", ["--hidden", *sorted(userinstall.SERVICE_FLAGS)])
def test_selfinstall_root_refusal_before_service_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    kind: paths.InstallKind,
    flag: str,
) -> None:
    monkeypatch.setattr(paths, "install_kind", lambda: kind)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    before = tree_snapshot(tmp_path)
    assert userinstall.selfinstall_main([str(tmp_path / "missing"), flag], {}) == 3
    assert capsys.readouterr().err == userinstall.ROOT_REFUSED_MESSAGE + "\n"
    assert tree_snapshot(tmp_path) == before


def test_sync_before_smoke_and_rename(tmp_path: Path, home: Path, synced: list[Path]) -> None:
    """P2-1: данные копии сброшены на диск до смоука и до rename в app/<KEY>."""
    app = _app(home)
    order: list[str] = []

    def smoke(copy: Path, info: userinstall.BuildInfo) -> None:
        assert synced == [copy]
        assert copy.parent == app and copy.name.startswith(f".tmp-{KEY}.")
        assert (copy / paths.INSTALLED_MARKER).is_file()
        assert not (app / KEY).exists()
        order.append("smoke")

    userinstall.install_from_dir(_bundle(tmp_path), smoke=smoke)
    assert order == ["smoke"]
    assert len(synced) == 1


def test_sync_filesystem_real(tmp_path: Path) -> None:
    _REAL_SYNC(tmp_path)


class _FakeLibc:
    def __init__(self, result: int) -> None:
        self.result = result
        self.calls = 0

    def syncfs(self, fd: int) -> int:
        self.calls += 1
        return self.result


def test_sync_filesystem_error_and_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = _FakeLibc(-1)
    monkeypatch.setattr(ctypes, "CDLL", lambda name, use_errno: failing)
    with pytest.raises(userinstall.UserInstallError, match="на диск"):
        _REAL_SYNC(tmp_path)
    assert failing.calls == 1

    def no_libc(name: object, use_errno: bool) -> object:
        raise OSError("нет libc")

    synced: list[bool] = []
    monkeypatch.setattr(ctypes, "CDLL", no_libc)
    monkeypatch.setattr(os, "sync", lambda: synced.append(True))
    _REAL_SYNC(tmp_path)
    assert synced == [True]


def test_sync_error_aborts_install(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def failing(path: Path) -> None:
        raise userinstall.UserInstallError("Не удалось записать файлы Astra Voice на диск: EIO")

    monkeypatch.setattr(userinstall, "_sync_filesystem", failing)
    with pytest.raises(userinstall.UserInstallError, match="на диск"):
        userinstall.install_from_dir(_bundle(tmp_path))
    assert sorted(os.listdir(_app(home))) == [".install.lock"]


def test_smoke_timeout(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Зависшая новая копия: смоук обрывается по таймауту, current не меняется."""
    monkeypatch.setattr(userinstall, "SMOKE_TIMEOUT_S", 0.5)
    userinstall.install_from_dir(_bundle(tmp_path))
    hung = _bundle(tmp_path, KEY2, apprun="#!/bin/sh\nexec sleep 30\n")
    with pytest.raises(userinstall.SmokeError, match="не запустилась"):
        userinstall.install_from_dir(hung)
    app = _app(home)
    assert _link(app, "current") == KEY
    assert sorted(os.listdir(app)) == [".install.lock", KEY, "current"]


def test_abandoned_tmp_removed_before_space_check(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-1: брошенная распаковка съела место — уборка под замком, затем установка."""
    app = _app(home)
    app.mkdir(parents=True, mode=0o700)
    abandoned = app / f".tmp-{KEY2}.q1w2e3"
    (abandoned / "usr").mkdir(parents=True)
    (abandoned / "usr" / "big").write_bytes(b"x" * 1024)

    def free(path: Path) -> int:
        return 0 if abandoned.exists() else userinstall.MIN_FREE_BYTES

    monkeypatch.setattr(userinstall, "free_bytes", free)
    result = userinstall.install_from_dir(_bundle(tmp_path))
    assert result.copied
    assert not abandoned.exists()
    assert sorted(os.listdir(app)) == [".install.lock", KEY, "current"]


def test_no_space_even_after_cleanup(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = _app(home)
    app.mkdir(parents=True, mode=0o700)
    (app / ".tmp-link-0000").symlink_to(tmp_path)
    monkeypatch.setattr(userinstall, "free_bytes", lambda path: 10 * userinstall.MIB)
    with pytest.raises(userinstall.NotEnoughSpaceError, match="нужно ещё 340 МБ"):
        userinstall.install_from_dir(_bundle(tmp_path))
    assert sorted(os.listdir(app)) == [".install.lock"]


def test_check_source_runs_before_reading_build_info(
    tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """P3-2: источник проверяется раньше, чем из него что-либо читается."""
    seen: list[str] = []

    def check(src: Path) -> None:
        seen.append("check")
        raise userinstall.UnsafeSourceError("Небезопасная временная папка.")

    monkeypatch.setattr(userinstall, "check_source", check)
    monkeypatch.setattr(userinstall, "read_build_info", lambda src: seen.append("read"))
    with pytest.raises(userinstall.UnsafeSourceError):
        userinstall.install_from_dir(tmp_path / "missing")
    assert seen == ["check"]
    assert userinstall.selfinstall_main([str(tmp_path / "missing")], {}) == 4
    assert os.listdir(home) == []


def test_lock_timeout_default() -> None:
    assert userinstall.LOCK_TIMEOUT_S >= 180
