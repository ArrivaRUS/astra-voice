"""Штатные средства звука: ни одна настоящая команда в тестах не запускается."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from functools import partial
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.platform.session import SessionKind
from astra_voice.platform.sound import (
    FULL_VOLUME_PERCENT,
    LOW_VOLUME_PERCENT,
    MicrophoneProblem,
    MicrophoneState,
    SoundControl,
    _spawn,
)

pytestmark = pytest.mark.unit


class FakeRunner:
    """Отвечает по имени команды и запоминает порядок вызовов."""

    def __init__(
        self,
        replies: dict[str, tuple[int, str]] | None = None,
        fails: Sequence[str] = (),
    ) -> None:
        self.replies = replies if replies is not None else {}
        self.fails = tuple(fails)
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs.get("shell") is False
        assert kwargs.get("check") is False
        self.calls.append(list(command))
        if any(part in self.fails for part in command):
            return subprocess.CompletedProcess(list(command), 1, "", "")
        code, out = self.replies.get(command[1], (0, ""))
        return subprocess.CompletedProcess(list(command), code, out, "")


def control(
    *,
    session: SessionKind = SessionKind.KDE,
    present: Sequence[str] = ("pactl", "systemctl", "kcmshell5"),
    replies: dict[str, tuple[int, str]] | None = None,
    fails: Sequence[str] = (),
    spawned: list[list[str]] | None = None,
    spawn_ok: bool = True,
) -> tuple[SoundControl, FakeRunner]:
    runner = FakeRunner(replies, fails)

    def spawn(command: Sequence[str]) -> bool:
        if spawned is not None:
            spawned.append(list(command))
        return spawn_ok

    return (
        SoundControl(
            session=session,
            which=lambda name: f"/usr/bin/{name}" if name in present else None,
            run=runner,
            spawn=spawn,
        ),
        runner,
    )


VOLUME_OUTPUT = "Volume: front-left: 45875 /  70% / -9.29 dB,   front-right: 45875 /  70%\n"


@pytest.mark.parametrize(
    ("muted", "percent", "problem"),
    [
        ("yes", 100, MicrophoneProblem.MUTED),
        ("no", 0, MicrophoneProblem.MUTED),
        ("no", LOW_VOLUME_PERCENT - 1, MicrophoneProblem.TOO_QUIET),
        ("no", LOW_VOLUME_PERCENT, None),
        ("no", 100, None),
        ("no", 130, None),
    ],
)
def test_microphone_state_names_the_reason(muted: str, percent: int, problem: object) -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, f"Mute: {muted}\n"),
            "get-source-volume": (0, f"Volume: front-left: 1 / {percent}% / -9 dB\n"),
        }
    )
    state = sound.microphone_state("alsa_input.pci-0000_00_1f.3")
    assert state.known and state.percent == percent
    assert state.problem is problem
    # Читаем состояние, ничего не меняя и не открывая микрофон.
    assert all(call[1].startswith("get-") for call in runner.calls)


def test_microphone_state_unknown_when_service_is_silent() -> None:
    sound, _ = control(replies={"get-source-mute": (1, "")})
    state = sound.microphone_state()
    assert state == MicrophoneState()
    assert state.problem is None and not state.can_raise


def test_microphone_state_without_pactl_runs_nothing() -> None:
    sound, runner = control(present=())
    assert not sound.has_volume_control
    assert sound.microphone_state("mic") == MicrophoneState()
    assert runner.calls == []


def test_default_source_is_used_without_explicit_choice() -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: no\n"),
            "get-source-volume": (0, VOLUME_OUTPUT),
        }
    )
    sound.microphone_state(None)
    assert [call[-1] for call in runner.calls] == ["@DEFAULT_SOURCE@", "@DEFAULT_SOURCE@"]


def test_raise_microphone_unmutes_and_sets_full_volume() -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: yes\n"),
            "get-source-volume": (0, "Volume: front-left: 1 / 10% / -30 dB\n"),
        }
    )
    assert sound.raise_microphone("mic")
    assert runner.calls[-2:] == [
        ["pactl", "set-source-mute", "mic", "0"],
        ["pactl", "set-source-volume", "mic", f"{FULL_VOLUME_PERCENT}%"],
    ]


def test_raise_microphone_keeps_volume_above_full() -> None:
    """Громкость выше 100 % не трогаем: только включаем звук (PRD F6.7 (б))."""
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: yes\n"),
            "get-source-volume": (0, "Volume: front-left: 1 / 130% / 6 dB\n"),
        }
    )
    assert sound.raise_microphone("mic")
    assert ["pactl", "set-source-volume", "mic", "100%"] not in runner.calls
    assert runner.calls[-1] == ["pactl", "set-source-mute", "mic", "0"]


def test_raise_microphone_reports_failure_without_pactl() -> None:
    sound, runner = control(present=("systemctl",))
    assert not sound.raise_microphone("mic")
    assert runner.calls == []


@pytest.mark.parametrize(
    ("session", "present", "expected"),
    [
        (SessionKind.KDE, ("systemsettings5", "kcmshell5"), ["systemsettings5", "kcm_pulseaudio"]),
        (SessionKind.KDE, ("kcmshell5",), ["kcmshell5", "kcm_pulseaudio"]),
        (SessionKind.FLY, ("systemsettings5", "kcmshell5"), ["kcmshell5", "kcm_pulseaudio"]),
        (SessionKind.FLY, ("pavucontrol",), ["pavucontrol"]),
        (SessionKind.OTHER, ("pavucontrol",), ["pavucontrol"]),
    ],
)
def test_sound_settings_command_depends_on_session(
    session: SessionKind, present: tuple[str, ...], expected: list[str]
) -> None:
    spawned: list[list[str]] = []
    sound, runner = control(session=session, present=present, spawned=spawned)
    assert sound.has_sound_settings
    assert sound.open_sound_settings()
    assert spawned == [expected]
    # Панель открывается отдельным процессом, а не через звуковую службу.
    assert runner.calls == []


def test_sound_settings_absent_opens_nothing() -> None:
    spawned: list[list[str]] = []
    sound, _ = control(present=("pactl",), spawned=spawned)
    assert not sound.has_sound_settings
    assert not sound.open_sound_settings()
    assert spawned == []


def test_restart_sound_service_falls_back_to_second_command() -> None:
    sound, runner = control(fails=("pipewire-pulse.socket",))
    assert sound.restart_sound_service()
    assert runner.calls == [
        ["systemctl", "--user", "restart", "pipewire-pulse.socket", "pipewire-pulse.service"],
        ["systemctl", "--user", "restart", "wireplumber"],
    ]


def test_restart_sound_service_without_systemctl_runs_nothing() -> None:
    sound, runner = control(present=("pactl",))
    assert not sound.has_sound_service
    assert not sound.restart_sound_service()
    assert runner.calls == []


@pytest.fixture
def child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> dict[str, str]:
    """Окружение .deb после bootstrap; все пользовательские каталоги временные."""
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    user = {
        # pytest добавляет эту переменную заново перед телом теста.
        "PYTEST_CURRENT_TEST": f"{request.node.nodeid} (call)",
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "TMPDIR": str(tmp_path / "tmp"),
        "PATH": "/usr/bin:/bin",
        "LANG": "ru_RU.UTF-8",
        "LC_ALL": "ru_RU.UTF-8",
    }
    launched = {
        **user,
        "QT_QUICK_BACKEND": "software",
        "QT_XCB_GL_INTEGRATION": "none",
        "PULSE_CLIENTCONFIG": str(tmp_path / "pulse-client.conf"),
        "ASTRA_VOICE_ORIG_UNSET": "QT_QUICK_BACKEND QT_XCB_GL_INTEGRATION PULSE_CLIENTCONFIG",
    }
    for name, value in launched.items():
        monkeypatch.setenv(name, value)
    return user


@pytest.mark.parametrize("appimage", [False, True])
def test_external_sound_commands_receive_clean_environment(
    child_environment: dict[str, str],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    appimage: bool,
) -> None:
    """§10.4: только pactl получает наш запрет autospawn; обе команды systemctl очищены."""
    if appimage:
        for name in (
            "ASTRA_VOICE_APPIMAGE_DIR",
            "APPIMAGE",
            "APPIMAGE_EXTRACT_AND_RUN",
            "APPDIR",
            "ARGV0",
            "OWD",
        ):
            monkeypatch.setenv(name, "bundle-value")
    before = dict(os.environ)
    run = Mock(wraps=FakeRunner(fails=("pipewire-pulse.socket",)))
    popen = Mock()
    sound = SoundControl(
        which=lambda name: f"/usr/bin/{name}",
        run=run,
        spawn=partial(_spawn, popen=popen),
    )
    assert sound.raise_microphone("mic")
    assert sound.restart_sound_service()
    assert sound.open_sound_settings()
    assert len(run.call_args_list) == 6
    for call in run.call_args_list:
        expected = child_environment.copy()
        if call.args[0][0] == "pactl":
            expected.update(LC_ALL="C", PULSE_CLIENTCONFIG=str(tmp_path / "pulse-client.conf"))
        assert call.kwargs["env"] == expected
    popen.assert_called_once_with(
        ["kcmshell5", "kcm_pulseaudio"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        shell=False,
        env=child_environment,
    )
    assert dict(os.environ) == before
    assert not (tmp_path / "cache").exists()


def test_spawn_oserror_returns_false(child_environment: dict[str, str]) -> None:
    popen = Mock(side_effect=OSError("недоступно"))
    assert _spawn(["pavucontrol"], popen=popen) is False
    popen.assert_called_once()
