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
    LOW_VOLUME_PERCENT,
    RAISE_TARGET_PERCENT,
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
    assert state.problem is None


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


def test_raise_microphone_unmutes_and_sets_target_volume() -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: yes\n"),
            "get-source-volume": (0, "Volume: front-left: 6554 / 10% / -30 dB\n"),
        }
    )
    assert sound.raise_microphone("mic")
    assert runner.calls[-2:] == [
        ["pactl", "set-source-mute", "mic", "0"],
        ["pactl", "set-source-volume", "mic", "50%"],
    ]


def test_raise_microphone_keeps_volume_above_full() -> None:
    """Громкость 130 % не трогаем: только включаем звук."""
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: yes\n"),
            "get-source-volume": (0, "Volume: front-left: 85197 / 130% / 6 dB\n"),
        }
    )
    assert sound.raise_microphone("mic")
    assert not any(call[1] == "set-source-volume" for call in runner.calls)
    assert runner.calls[-1] == ["pactl", "set-source-mute", "mic", "0"]


@pytest.mark.parametrize("percent", [60, 80])
def test_raise_microphone_unmutes_without_lowering_volume(percent: int) -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: yes\n"),
            "get-source-volume": (0, f"Volume: front-left: 1 / {percent}% / -6 dB\n"),
        }
    )
    assert sound.raise_microphone("mic") is True
    assert not any(call[1] == "set-source-volume" for call in runner.calls)
    assert runner.calls[-1] == ["pactl", "set-source-mute", "mic", "0"]


def test_raise_microphone_does_nothing_when_current_volume_unknown() -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: yes\n"),
            "get-source-volume": (1, ""),
        }
    )
    assert sound.raise_microphone("mic") is False
    assert not any(call[1].startswith("set-") for call in runner.calls)


def test_raise_microphone_reports_failure_without_pactl() -> None:
    sound, runner = control(present=("systemctl",))
    assert not sound.raise_microphone("mic")
    assert runner.calls == []


def test_restore_microphone_returns_first_known_volume_and_unmute_state() -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: no\n"),
            "get-source-volume": (0, "Volume: front-left: 16384 / 25% / -20 dB\n"),
        }
    )
    assert sound.raise_microphone("mic")
    assert sound.can_restore_microphone("mic")
    runner.replies["get-source-volume"] = (
        0,
        f"Volume: front-left: 1 / {RAISE_TARGET_PERCENT}% / -8 dB\n",
    )
    assert sound.raise_microphone("mic")
    assert sound.restore_microphone("mic")
    assert runner.calls[-2:] == [
        ["pactl", "set-source-volume", "mic", "16384"],
        ["pactl", "set-source-mute", "mic", "0"],
    ]
    assert not sound.can_restore_microphone("mic")


def test_restore_microphone_returns_original_mute_state() -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: yes\n"),
            "get-source-volume": (0, "Volume: front-left: 52428 / 80% / -6 dB\n"),
        }
    )
    assert sound.raise_microphone("mic")
    assert sound.restore_microphone("mic")
    assert runner.calls[-2:] == [
        ["pactl", "set-source-volume", "mic", "52428"],
        ["pactl", "set-source-mute", "mic", "1"],
    ]


def test_raise_then_restore_preserves_stereo_levels_and_mute() -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: yes\n"),
            "get-source-volume": (
                0,
                "Volume: front-left: 45875 / 70% / -9.29 dB, "
                "front-right: 13107 / 20% / -41.94 dB\n",
            ),
        }
    )
    assert sound.raise_microphone("mic")
    assert not any(call[1] == "set-source-volume" for call in runner.calls)
    assert sound.can_restore_microphone("mic")
    assert sound.restore_microphone("mic")
    assert runner.calls[-2:] == [
        ["pactl", "set-source-volume", "mic", "45875", "13107"],
        ["pactl", "set-source-mute", "mic", "1"],
    ]
    assert not sound.can_restore_microphone("mic")


def test_raise_microphone_without_change_does_not_remember() -> None:
    sound, _ = control(
        replies={
            "get-source-mute": (0, "Mute: no\n"),
            "get-source-volume": (0, "Volume: front-left: 52428 / 80% / -6 dB\n"),
        }
    )
    assert sound.raise_microphone("mic")
    assert not sound.can_restore_microphone("mic")


def test_raise_microphone_remembers_when_only_one_command_succeeds() -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: yes\n"),
            "get-source-volume": (0, "Volume: front-left: 16384 / 25% / -20 dB\n"),
        },
        fails=("set-source-volume",),
    )
    assert not sound.raise_microphone("mic")
    assert sound.can_restore_microphone("mic")
    runner.fails = ("set-source-mute", "set-source-volume")
    sound.forget_microphone_changes()
    assert not sound.raise_microphone("mic")
    assert not sound.can_restore_microphone("mic")


def test_raise_microphone_does_not_remember_successful_noop() -> None:
    sound, _ = control(
        replies={
            "get-source-mute": (0, "Mute: no\n"),
            "get-source-volume": (0, "Volume: front-left: 16384 / 25% / -20 dB\n"),
        },
        fails=("set-source-volume",),
    )
    assert not sound.raise_microphone("mic")
    assert not sound.can_restore_microphone("mic")


def test_failed_restore_keeps_snapshot_for_retry() -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: no\n"),
            "get-source-volume": (0, "Volume: front-left: 16384 / 25% / -20 dB\n"),
        }
    )
    assert sound.raise_microphone("mic")
    runner.fails = ("set-source-mute",)
    assert not sound.restore_microphone("mic")
    assert sound.can_restore_microphone("mic")
    runner.fails = ()
    assert sound.restore_microphone("mic")
    assert not sound.can_restore_microphone("mic")


def test_restore_without_snapshot_does_not_call_pactl() -> None:
    sound, runner = control()
    assert not sound.can_restore_microphone(None)
    assert not sound.restore_microphone(None)
    assert not sound.restore_microphone("mic")
    assert runner.calls == []


def test_default_source_change_prevents_restore_to_another_microphone() -> None:
    sound, runner = control(
        replies={
            "get-default-source": (0, "alsa_input.a\n"),
            "get-source-mute": (0, "Mute: yes\n"),
            "get-source-volume": (0, "Volume: front-left: 13107 / 20% / -20 dB\n"),
        }
    )
    assert sound.raise_microphone(None)
    assert runner.calls[-2:] == [
        ["pactl", "set-source-mute", "alsa_input.a", "0"],
        ["pactl", "set-source-volume", "alsa_input.a", "50%"],
    ]
    assert sound.can_restore_microphone(None)
    runner.replies["get-default-source"] = (0, "alsa_input.b\n")
    before_change = len(runner.calls)
    assert not sound.can_restore_microphone(None)
    assert not sound.restore_microphone(None)
    assert not any(call[1].startswith("set-source-") for call in runner.calls[before_change:])
    assert sound.can_restore_microphone("alsa_input.a")


def test_default_source_restore_targets_resolved_microphone() -> None:
    sound, runner = control(
        replies={
            "get-default-source": (0, "alsa_input.a\n"),
            "get-source-mute": (0, "Mute: yes\n"),
            "get-source-volume": (0, "Volume: front-left: 13107 / 20% / -20 dB\n"),
        }
    )
    assert sound.raise_microphone("")
    assert sound.can_restore_microphone(None)
    assert sound.restore_microphone(None)
    assert runner.calls[-2:] == [
        ["pactl", "set-source-volume", "alsa_input.a", "13107"],
        ["pactl", "set-source-mute", "alsa_input.a", "1"],
    ]
    assert not sound.can_restore_microphone(None)


def test_default_source_manual_change_remembers_resolved_microphone() -> None:
    sound, runner = control(
        replies={
            "get-default-source": (0, "alsa_input.a\n"),
            "get-source-mute": (0, "Mute: no\n"),
            "get-source-volume": (0, "Volume: front-left: 13107 / 20% / -20 dB\n"),
        }
    )
    assert sound.set_microphone_volume(35, None)
    assert runner.calls[-1] == ["pactl", "set-source-volume", "alsa_input.a", "35%"]
    assert sound.can_restore_microphone("alsa_input.a")
    assert sound.restore_microphone(None)
    assert runner.calls[-2:] == [
        ["pactl", "set-source-volume", "alsa_input.a", "13107"],
        ["pactl", "set-source-mute", "alsa_input.a", "0"],
    ]


@pytest.mark.parametrize("answer", [(1, ""), (0, "  \n")])
def test_unresolved_default_source_does_not_remember(answer: tuple[int, str]) -> None:
    sound, runner = control(
        replies={
            "get-default-source": answer,
            "get-source-mute": (0, "Mute: yes\n"),
            "get-source-volume": (0, "Volume: front-left: 13107 / 20% / -20 dB\n"),
        }
    )
    assert sound.raise_microphone(None)
    assert not sound.can_restore_microphone(None)
    assert [call[2] for call in runner.calls if call[1].startswith("set-source-")] == [
        "@DEFAULT_SOURCE@",
        "@DEFAULT_SOURCE@",
    ]


def test_forget_microphone_changes_clears_all_sources() -> None:
    sound, _ = control(
        replies={
            "get-source-mute": (0, "Mute: no\n"),
            "get-source-volume": (0, "Volume: front-left: 16384 / 25% / -20 dB\n"),
            "get-default-source": (0, "default-mic\n"),
        }
    )
    assert sound.raise_microphone("mic")
    assert sound.raise_microphone(None)
    assert sound.can_restore_microphone("mic")
    assert sound.can_restore_microphone(None)
    sound.forget_microphone_changes()
    assert not sound.can_restore_microphone("mic")
    assert not sound.can_restore_microphone(None)


def test_set_microphone_volume_remembers_volume_and_mute_for_restore() -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: yes\n"),
            "get-source-volume": (0, "Volume: front-left: 16384 / 25% / -20 dB\n"),
        }
    )
    assert sound.set_microphone_volume(35, "mic")
    assert runner.calls[-1] == ["pactl", "set-source-volume", "mic", "35%"]
    assert not any(call[1] == "set-source-mute" for call in runner.calls)
    assert sound.can_restore_microphone("mic")
    assert sound.restore_microphone("mic")
    assert runner.calls[-2:] == [
        ["pactl", "set-source-volume", "mic", "16384"],
        ["pactl", "set-source-mute", "mic", "1"],
    ]


@pytest.mark.parametrize(("left_raw", "left_percent"), [(45875, 70), (32768, 50), (32790, 50)])
def test_set_microphone_volume_preserves_first_stereo_baseline(
    left_raw: int, left_percent: int
) -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: no\n"),
            "get-source-volume": (
                0,
                f"Volume: front-left: {left_raw} / {left_percent}% / -9 dB, "
                "front-right: 13107 / 20% / -41.94 dB\n",
            ),
        }
    )
    # Даже если первый канал уже 50 %, второй изменится с 20 до 50 %.
    assert sound.set_microphone_volume(50, "mic")
    assert runner.calls[-1] == ["pactl", "set-source-volume", "mic", "50%"]
    assert sound.can_restore_microphone("mic")
    runner.replies["get-source-volume"] = (
        0,
        "Volume: front-left: 32768 / 50% / -18.06 dB, front-right: 32768 / 50% / -18.06 dB\n",
    )
    assert sound.set_microphone_volume(60, "mic")
    assert sound.restore_microphone("mic")
    assert runner.calls[-2:] == [
        ["pactl", "set-source-volume", "mic", str(left_raw), "13107"],
        ["pactl", "set-source-mute", "mic", "0"],
    ]
    assert not sound.can_restore_microphone("mic")


@pytest.mark.parametrize(("requested", "expected"), [(150, "100%"), (-5, "0%")])
def test_set_microphone_volume_clamps_to_full_range(requested: int, expected: str) -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: no\n"),
            "get-source-volume": (0, "Volume: front-left: 16384 / 25% / -20 dB\n"),
        }
    )
    assert sound.set_microphone_volume(requested, "mic")
    assert runner.calls[-1] == ["pactl", "set-source-volume", "mic", expected]
    assert not any(call[1] == "set-source-mute" for call in runner.calls)


def test_set_microphone_volume_remembers_only_first_change() -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: no\n"),
            "get-source-volume": (0, "Volume: front-left: 16384 / 25% / -20 dB\n"),
        }
    )
    assert sound.set_microphone_volume(40, "mic")
    runner.replies["get-source-volume"] = (0, "Volume: front-left: 26214 / 40% / -15 dB\n")
    assert sound.set_microphone_volume(60, "mic")
    assert sound.restore_microphone("mic")
    assert runner.calls[-2:] == [
        ["pactl", "set-source-volume", "mic", "16384"],
        ["pactl", "set-source-mute", "mic", "0"],
    ]


def test_set_microphone_volume_after_raise_keeps_original_state() -> None:
    sound, runner = control(
        replies={
            "get-source-mute": (0, "Mute: yes\n"),
            "get-source-volume": (0, "Volume: front-left: 16384 / 25% / -20 dB\n"),
        }
    )
    assert sound.raise_microphone("mic")
    runner.replies["get-source-mute"] = (0, "Mute: no\n")
    runner.replies["get-source-volume"] = (
        0,
        f"Volume: front-left: 1 / {RAISE_TARGET_PERCENT}% / -8 dB\n",
    )
    assert sound.set_microphone_volume(60, "mic")
    assert sound.restore_microphone("mic")
    assert runner.calls[-2:] == [
        ["pactl", "set-source-volume", "mic", "16384"],
        ["pactl", "set-source-mute", "mic", "1"],
    ]


def test_set_microphone_volume_failure_does_not_remember() -> None:
    sound, _ = control(
        replies={
            "get-source-mute": (0, "Mute: no\n"),
            "get-source-volume": (0, "Volume: front-left: 16384 / 25% / -20 dB\n"),
        },
        fails=("set-source-volume",),
    )
    assert not sound.set_microphone_volume(35, "mic")
    assert not sound.can_restore_microphone("mic")


def test_set_microphone_volume_without_pactl_runs_nothing() -> None:
    sound, runner = control(present=())
    assert not sound.set_microphone_volume(35, "mic")
    assert runner.calls == []


def test_set_microphone_volume_does_nothing_when_current_volume_unknown() -> None:
    sound, runner = control(
        replies={"get-source-mute": (0, "Mute: yes\n"), "get-source-volume": (1, "")}
    )
    assert not sound.set_microphone_volume(50, "mic")
    assert not any(call[1].startswith("set-") for call in runner.calls)


def test_set_microphone_volume_without_change_does_not_remember() -> None:
    sound, _ = control(
        replies={
            "get-source-mute": (0, "Mute: no\n"),
            "get-source-volume": (0, "Volume: front-left: 16384 / 25% / -20 dB\n"),
        }
    )
    assert sound.set_microphone_volume(25, "mic")
    assert not sound.can_restore_microphone("mic")


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
    run = Mock(
        wraps=FakeRunner(
            replies={
                "get-source-mute": (0, "Mute: yes\n"),
                "get-source-volume": (0, "Volume: front-left: 13107 / 20% / -20 dB\n"),
            },
            fails=("pipewire-pulse.socket",),
        )
    )
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
