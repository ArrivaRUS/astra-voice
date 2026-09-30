"""Бутстрап: диспетчер команд, sys.path, обе раскладки, любой cwd."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astra_voice import bootstrap
from astra_voice.core import audio_env, paths, policy
from astra_voice.platform import userinstall
from helpers.appimage_bundle import KEY, make_bundle

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DEV_BOOTSTRAP = REPO_ROOT / "src" / "astra_voice" / "bootstrap.py"


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in ("CACHE", "CONFIG", "DATA", "STATE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path / "runtime"))


def _run(script: Path, args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    # Подмена pytest не наследуется процессом; root-скрипт ниже сам ставит 0.
    runner = (
        "import os, runpy, sys; os.geteuid = lambda: 1000; "
        "sys.argv = sys.argv[1:]; runpy.run_path(sys.argv[0], run_name='__main__')"
    )
    return subprocess.run(
        [sys.executable, "-I", "-c", runner, str(script), *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _installed_layout(tmp_path: Path) -> Path:
    """Раскладка /usr/lib/astra-voice: bootstrap.py рядом с каталогом astra_voice."""
    root = tmp_path / "usr-lib-astra-voice"
    shutil.copytree(
        REPO_ROOT / "src" / "astra_voice",
        root / "astra_voice",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    shutil.copy2(DEV_BOOTSTRAP, root / "bootstrap.py")
    return root


def test_dev_layout_dispatches_worker_without_fd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ASTRA_VOICE_IPC_FD", raising=False)
    proc = _run(DEV_BOOTSTRAP, ["worker"], cwd=tmp_path)
    assert proc.returncode == 2
    assert "astra-voice worker: требуется числовой IPC fd" in proc.stderr


def test_worker_bootstrap_sets_pulse_clientconfig(tmp_path: Path) -> None:
    script = tmp_path / "check_bootstrap.py"
    script.write_text(
        "import os, runpy\n"
        "os.geteuid = lambda: 1000\n"
        "from pathlib import Path\n"
        f"bootstrap = runpy.run_path({str(DEV_BOOTSTRAP)!r})\n"
        "assert bootstrap['main'](['worker']) == 2\n"
        "assert os.environ['PULSE_SERVER'] == 'unix:/nonexistent'\n"
        "path = Path(os.environ['PULSE_CLIENTCONFIG'])\n"
        "print(path)\n"
        "print(path.read_text(encoding='utf-8'))\n",
        encoding="utf-8",
    )
    env = dict(os.environ)
    env.pop("PULSE_CLIENTCONFIG", None)
    env.pop("ASTRA_VOICE_IPC_FD", None)
    env["PULSE_SERVER"] = "unix:/nonexistent"
    proc = subprocess.run(
        [sys.executable, "-I", str(script)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    filename, content = proc.stdout.split("\n", 1)
    path = Path(filename)
    assert path.is_absolute() and path.is_relative_to(tmp_path)
    assert path == tmp_path / "cache" / "astra-voice" / "pulse-client.conf"
    assert "autospawn = no" in content


def test_installed_layout_dispatches_helper_stub(tmp_path: Path) -> None:
    root = _installed_layout(tmp_path)
    proc = _run(root / "bootstrap.py", ["helper"], cwd=tmp_path)
    assert proc.returncode == 2
    assert "not implemented" in proc.stderr


def test_unknown_command_and_no_args(tmp_path: Path) -> None:
    for args in ([], ["nonsense"]):
        proc = _run(DEV_BOOTSTRAP, args, cwd=tmp_path)
        assert proc.returncode == 2
        assert "usage" in proc.stderr


@pytest.mark.parametrize(
    "kind", [paths.InstallKind.APPIMAGE_PORTABLE, paths.InstallKind.APPIMAGE_INSTALLED]
)
@pytest.mark.parametrize(
    "args",
    [
        [],
        ["--version"],
        ["--uninstall"],
        ["app", "--version"],
        ["app", "--uninstall"],
        ["selfinstall", "/missing"],
        ["worker"],
        ["helper"],
    ],
)
def test_root_refused_before_any_entry(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    policy_entries: dict[str, Mock],
    kind: paths.InstallKind,
    args: list[str],
) -> None:
    """T-179: даже служебная или неверная команда не обходит отказ."""
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(paths, "install_kind", lambda: kind)
    gate = Mock(side_effect=AssertionError("политика до отказа root"))
    monkeypatch.setattr(bootstrap, "_appimage_policy_gate", gate)
    assert bootstrap.main(args) == 3
    assert capsys.readouterr().err == userinstall.ROOT_REFUSED_MESSAGE + "\n"
    gate.assert_not_called()
    for entry in policy_entries.values():
        entry.assert_not_called()
    assert isinstance(audio_env.deny_pulse_autospawn, Mock)
    audio_env.deny_pulse_autospawn.assert_not_called()
    assert "QT_QUICK_CONTROLS_STYLE" not in os.environ


@pytest.mark.parametrize("kind", [paths.InstallKind.DEB, paths.InstallKind.SOURCE])
def test_root_outside_bundle_keeps_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    policy_entries: dict[str, Mock],
    kind: paths.InstallKind,
) -> None:
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(paths, "install_kind", lambda: kind)
    assert bootstrap.main(["--version"]) == 2
    assert capsys.readouterr().err == bootstrap.USAGE
    assert bootstrap.main(["app", "--version"]) == 17
    policy_entries["app"].assert_called_once_with(["--version"])
    assert capsys.readouterr().err == ""


def test_root_refusal_does_not_import_qt(tmp_path: Path) -> None:
    """Проверяем чистый интерпретатор: Qt не загружен даже косвенно."""
    script = tmp_path / "root_bootstrap.py"
    script.write_text(
        "import os, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT / 'src')!r})\n"
        "from astra_voice import bootstrap\n"
        "from astra_voice.core import paths\n"
        "os.geteuid = lambda: 0\n"
        "paths.install_kind = lambda: paths.InstallKind.APPIMAGE_PORTABLE\n"
        "result = bootstrap.main(['--version'])\n"
        "assert not any(n == 'PyQt5' or n.startswith('PyQt5.') for n in sys.modules)\n"
        "raise SystemExit(result)\n",
        encoding="utf-8",
    )
    proc = _run(script, [], tmp_path)
    assert proc.returncode == 3
    assert proc.stdout == ""
    assert proc.stderr == userinstall.ROOT_REFUSED_MESSAGE + "\n"


def test_setup_sys_path_installed_prefers_vendor(tmp_path: Path) -> None:
    root = tmp_path / "usr-lib-astra-voice"
    (root / "astra_voice").mkdir(parents=True)
    (root / "vendor").mkdir()
    saved = list(sys.path)
    try:
        bootstrap._setup_sys_path(root)
        assert sys.path[0] == str(root / "vendor")
        assert sys.path[1] == str(root)
    finally:
        sys.path[:] = saved


def test_setup_sys_path_dev_uses_parent(tmp_path: Path) -> None:
    here = tmp_path / "src" / "astra_voice"
    here.mkdir(parents=True)
    saved = list(sys.path)
    try:
        bootstrap._setup_sys_path(here)
        assert sys.path[0] == str(tmp_path / "src")
        assert str(here / "vendor") not in sys.path
    finally:
        sys.path[:] = saved


# ── выбор рендера Qt Quick (RSS: 167 740 → 102 580 кБ, spikes/m1_live/rss.md) ──


def _clear_render_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(bootstrap.RENDER_ENV, raising=False)
    for name in bootstrap.SOFTWARE_RENDER_ENV:
        monkeypatch.delenv(name, raising=False)


def test_render_defaults_to_software(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_render_env(monkeypatch)
    bootstrap._setup_render_env()
    assert os.environ["QT_QUICK_BACKEND"] == "software"
    assert os.environ["QT_XCB_GL_INTEGRATION"] == "none"


@pytest.mark.parametrize("value", ["gl", "GL", " gl "])
def test_render_gl_sets_nothing(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    _clear_render_env(monkeypatch)
    monkeypatch.setenv(bootstrap.RENDER_ENV, value)
    bootstrap._setup_render_env()
    assert "QT_QUICK_BACKEND" not in os.environ
    assert "QT_XCB_GL_INTEGRATION" not in os.environ


def test_render_unknown_value_falls_back_to_software(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_render_env(monkeypatch)
    monkeypatch.setenv(bootstrap.RENDER_ENV, "чепуха")
    bootstrap._setup_render_env()
    assert os.environ["QT_QUICK_BACKEND"] == "software"


def test_render_keeps_user_values(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_render_env(monkeypatch)
    monkeypatch.setenv("QT_QUICK_BACKEND", "openvg")
    bootstrap._setup_render_env()
    assert os.environ["QT_QUICK_BACKEND"] == "openvg"  # выбор пользователя не перебит
    assert os.environ["QT_XCB_GL_INTEGRATION"] == "none"


def test_app_command_sets_render_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_render_env(monkeypatch)
    assert bootstrap.main(["app", "--version"]) == 0
    assert os.environ["QT_QUICK_BACKEND"] == "software"
    assert os.environ["QT_QUICK_CONTROLS_STYLE"] == "Default"


def test_worker_command_leaves_render_env_alone(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_render_env(monkeypatch)
    monkeypatch.delenv("QT_QUICK_CONTROLS_STYLE", raising=False)
    assert bootstrap.main(["worker"]) == 2
    assert "QT_QUICK_BACKEND" not in os.environ
    assert "QT_XCB_GL_INTEGRATION" not in os.environ
    assert "QT_QUICK_CONTROLS_STYLE" not in os.environ


def test_selfinstall_command_installs_bundle(tmp_path: Path) -> None:
    """T-170 (unit-часть): bootstrap selfinstall ставит бандл без Qt и GUI."""
    bundle = make_bundle(tmp_path / "appimage_extracted_1c866c1789f7")
    proc = _run(DEV_BOOTSTRAP, ["selfinstall", str(bundle), "--hidden"], cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    app = tmp_path / "data" / "astra-voice" / "app"
    assert os.readlink(app / "current") == KEY
    assert (app / KEY / ".installed-ok").is_file()


def test_selfinstall_service_flag_creates_nothing(tmp_path: Path) -> None:
    bundle = make_bundle(tmp_path / "bundle")
    proc = _run(DEV_BOOTSTRAP, ["selfinstall", str(bundle), "--version"], cwd=tmp_path)
    assert proc.returncode == 10
    assert not (tmp_path / "data").exists()


def test_selfinstall_without_source_is_usage(tmp_path: Path) -> None:
    proc = _run(DEV_BOOTSTRAP, ["selfinstall"], cwd=tmp_path)
    assert proc.returncode == 2
    assert "usage" in proc.stderr


@pytest.fixture
def policy_entries(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Mock]:
    """Подменяем точки входа и подготовку процессов: без Qt, GUI и установки."""
    monkeypatch.setattr(policy, "POLICY_PATH", tmp_path / "policy.conf")
    entries = {name: Mock(return_value=17) for name in bootstrap.COMMANDS}
    for name, module in (
        ("app", "astra_voice.app"),
        ("worker", "astra_voice.worker.main"),
        ("helper", "astra_voice.helper.main"),
    ):
        monkeypatch.setitem(sys.modules, module, SimpleNamespace(main=entries[name]))
    monkeypatch.setattr(userinstall, "selfinstall_main", entries["selfinstall"])
    for name in ("_setup_sys_path", "_harden", "_setup_render_env"):
        monkeypatch.setattr(bootstrap, name, Mock())
    monkeypatch.setattr(audio_env, "deny_pulse_autospawn", Mock())
    # Системной версии нет; exec в процессе pytest недопустим в любом случае.
    monkeypatch.setattr(paths, "SYSTEM_EXECUTABLE", tmp_path / "no-deb" / "astra-voice")
    monkeypatch.setattr(os, "execve", Mock(side_effect=AssertionError("execve в тесте")))
    for name in (*bootstrap.SOFTWARE_RENDER_ENV, "QT_QUICK_CONTROLS_STYLE"):
        monkeypatch.delenv(name, raising=False)
    return entries


@pytest.mark.parametrize("kind", list(paths.InstallKind))
@pytest.mark.parametrize("args", [["app"], ["selfinstall", "/x"]])
def test_appimage_policy_denial_before_entry(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    policy_entries: dict[str, Mock],
    kind: paths.InstallKind,
    args: list[str],
) -> None:
    """T-180: запрет действует только в бандле, до подготовки и точек входа."""
    policy.POLICY_PATH.write_text("[astra-voice]\nappimage = deny\n", encoding="utf-8")
    monkeypatch.setattr(paths, "install_kind", lambda: kind)
    load = Mock(wraps=policy.load)
    monkeypatch.setattr(policy, "load", load)

    assert bootstrap.main(args) == (3 if kind.is_appimage else 17)
    if kind.is_appimage:
        assert capsys.readouterr().err == (
            "Администратор запретил эту версию программы на компьютере. "
            "Используйте системную версию.\n"
        )
        load.assert_called_once_with()
        for entry in policy_entries.values():
            entry.assert_not_called()
        assert isinstance(audio_env.deny_pulse_autospawn, Mock)
        audio_env.deny_pulse_autospawn.assert_not_called()
        assert "QT_QUICK_CONTROLS_STYLE" not in os.environ
    else:
        assert capsys.readouterr().err == ""
        load.assert_not_called()
        policy_entries[args[0]].assert_called_once_with(args[1:])


@pytest.mark.parametrize(
    "kind", [paths.InstallKind.APPIMAGE_PORTABLE, paths.InstallKind.APPIMAGE_INSTALLED]
)
@pytest.mark.parametrize("args", [["app"], ["selfinstall", "/x"]])
@pytest.mark.parametrize("text", ["[сломано\n", "[astra-voice]\nprofile = secure\n"])
def test_appimage_invalid_or_secure_policy_allows_entry(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    policy_entries: dict[str, Mock],
    kind: paths.InstallKind,
    args: list[str],
    text: str,
) -> None:
    policy.POLICY_PATH.write_text(text, encoding="utf-8")
    monkeypatch.setattr(paths, "install_kind", lambda: kind)
    assert bootstrap.main(args) == 17
    policy_entries[args[0]].assert_called_once_with(args[1:])
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize("command", ["worker", "helper"])
def test_internal_commands_do_not_check_appimage_policy(
    monkeypatch: pytest.MonkeyPatch, policy_entries: dict[str, Mock], command: str
) -> None:
    policy.POLICY_PATH.write_text("[astra-voice]\nappimage = deny\n", encoding="utf-8")
    kind = Mock(return_value=paths.InstallKind.APPIMAGE_PORTABLE)
    load = Mock(wraps=policy.load)
    monkeypatch.setattr(paths, "install_kind", kind)
    monkeypatch.setattr(policy, "load", load)
    assert bootstrap.main([command]) == 17
    policy_entries[command].assert_called_once_with([])
    kind.assert_not_called()
    load.assert_not_called()


def test_python_policy_path_ignores_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, policy_entries: dict[str, Mock]
) -> None:
    policy.POLICY_PATH.write_text("[astra-voice]\nappimage = deny\n", encoding="utf-8")
    other = tmp_path / "allow.conf"
    other.write_text("[astra-voice]\nappimage = allow\n", encoding="utf-8")
    monkeypatch.setenv("ASTRA_VOICE_POLICY_FILE", str(other))
    monkeypatch.setenv("POLICY_PATH", str(other))
    monkeypatch.setattr(paths, "install_kind", lambda: paths.InstallKind.APPIMAGE_PORTABLE)
    assert bootstrap.main(["app"]) == 3
    policy_entries["app"].assert_not_called()
