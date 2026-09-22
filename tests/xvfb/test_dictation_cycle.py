"""Полный цикл runtime на изолированном X11: WAV, фиктивный IPC, настоящий UI и ввод."""

from __future__ import annotations

import json
import logging
import math
import os
import struct
import sys
import wave
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import PasteTarget, clipboard_owner, paste_target, wait_until  # noqa: E402
from helpers import xapp as xapp  # noqa: E402
from helpers import xdisplay as xdisplay  # noqa: E402

pytestmark = pytest.mark.xvfb

if os.environ.get("DISPLAY", "").strip() in {"", ":0", ":0.0"}:
    pytest.skip(
        "тест требует изолированного X: на :0 живая сессия заказчика", allow_module_level=True
    )
pytest.importorskip("Xlib")
pytest.importorskip("PyQt5.QtQuick", reason="нужен python3-pyqt5.qtquick")

from PyQt5 import sip  # noqa: E402
from PyQt5.QtCore import QEvent, QObject, QTimer  # noqa: E402
from PyQt5.QtQuick import QQuickView  # noqa: E402
from PyQt5.QtWidgets import QSystemTrayIcon  # noqa: E402

from astra_voice.core.dictation import DictationPhase  # noqa: E402
from astra_voice.core.paths import state_dir  # noqa: E402
from astra_voice.core.settings import Settings  # noqa: E402
from astra_voice.platform.hotkey import (  # noqa: E402
    GrabResult,
    HotkeyFsm,
    HotkeyManager,
    HotkeyMode,
    HotkeyState,
)
from astra_voice.platform.session import SessionKind  # noqa: E402
from astra_voice.runtime import DictationRuntime  # noqa: E402
from astra_voice.ui.pill import Pill, PillState  # noqa: E402
from astra_voice.ui.tray import Tray  # noqa: E402
from astra_voice.ui.tray_icons import TrayState  # noqa: E402
from astra_voice.worker.audio import (  # noqa: E402
    CHANNELS,
    CHUNK_BYTES,
    RATE,
    SAMPLE_BYTES,
    WavFileSource,
    dbfs,
    normalize,
)
from astra_voice.worker.supervisor import WorkerSupervisor  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
MARKER = "ГЕЛИОТРОП-7"
ORIGINAL_CLIPBOARD = "Исходный буфер, ёж."


class FakeHotkey:
    """Настоящий автомат без дескриптора X11 и захватов клавиатуры."""

    def __init__(self) -> None:
        self.fsm = HotkeyFsm()
        self.backend = Mock(spec=["ungrab_escape", "close"])
        self.on_state: Callable[[HotkeyState, str], None] | None = None
        self.grabs: list[tuple[str, HotkeyMode]] = []
        self.ungrab_calls = 0

    def fileno(self) -> int:
        return -1

    def grab(self, combo: str, mode: HotkeyMode) -> GrabResult:
        self.grabs.append((combo, mode))
        return GrabResult("ok")

    def ungrab(self) -> None:
        self.ungrab_calls += 1


class WavSupervisor(QObject):
    """Замена процесса: читает WAV, доставляет коррелированные ответы через Qt.

    Часы аудио ускорены: первый блок читается при старте, остаток при остановке.
    Распознавание заменено фиксированной фразой; модель и Pulse не создаются.
    """

    def __init__(self, source: WavFileSource, on_event: Callable[[dict[str, Any]], None]) -> None:
        super().__init__()
        self.source = source
        self.on_event = on_event
        self.generation = 1
        self.state = "new"
        self.recording = False
        self.utterance_id = ""
        self.samples_read = 0
        self.commands: list[dict[str, Any]] = []
        self.events: list[dict[str, Any]] = []
        self.trace: list[str] = []
        self.pending: deque[dict[str, Any]] = deque()
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.pump)

    def start(self) -> None:
        self.state = "running"

    def queue_event(self, kind: str, **fields: object) -> None:
        self.pending.append(
            {
                "type": kind,
                "generation": self.generation,
                "utterance_id": self.utterance_id,
                **fields,
            }
        )
        self.timer.start(0)

    def send(self, message: dict[str, Any], *, timeout: float | None = None) -> dict[str, Any]:
        assert self.state == "running"
        request = {**message, "generation": self.generation}
        self.commands.append(request)
        kind = message["type"]
        self.trace.append(kind)
        if kind == "record.start":
            self.utterance_id = message["utterance_id"]
            self.source.open(message.get("device"))
            self.recording = True
            chunk = self.source.read_chunk()
            assert chunk is not None, "WAV должен содержать звук"
            samples = normalize(chunk)
            self.samples_read += len(samples)
            self.queue_event("audio.ready", device="Тестовый WAV")
            self.queue_event(
                "level",
                peak_dbfs=dbfs(max(abs(sample) for sample in samples) * 32768),
                rms_dbfs=dbfs(math.sqrt(sum(s * s for s in samples) / len(samples)) * 32768),
            )
        elif kind in {"record.stop", "recognize", "record.cancel"}:
            assert message["utterance_id"] == self.utterance_id
            if kind == "record.stop":
                assert self.recording
                while (chunk := self.source.read_chunk()) is not None:
                    self.samples_read += len(normalize(chunk))
                self.recording = False
            elif kind == "recognize":
                assert not self.recording and self.samples_read > 0
                self.queue_event("result", text=MARKER)
            else:
                self.recording = False
                self.source.close()
                self.queue_event("cancelled")
        elif kind == "audio.close":
            self.recording = False
            self.source.close()
            self.queue_event("audio.closed")
        else:
            raise AssertionError(f"Неожиданная команда: {kind}")
        return request

    def pump(self, timeout: float = 0.0) -> None:
        self.timer.stop()
        pending, self.pending = self.pending, deque()
        for event in pending:
            self.events.append(event)
            self.on_event(event)

    def stop(self) -> None:
        self.trace.append("stop")
        self.state = "stopped"
        self.recording = False
        self.timer.stop()
        self.pending.clear()
        self.source.close()


def visual_tree(root: Any) -> Iterator[Any]:
    """Как в test_pill_states: childItems включает делегаты Repeater."""
    yield root
    for child in root.childItems():
        yield from visual_tree(child)


def qml_strings(root: Any) -> list[str]:
    """Проверяем также невидимые Text и строковые свойства всего визуального дерева."""
    values: list[str] = []
    texts = 0
    for item in visual_tree(root):
        meta = item.metaObject()
        if meta.indexOfProperty("elide") >= 0:
            texts += 1
            values.append(str(item.property("text")))
        for index in range(meta.propertyCount()):
            prop = meta.property(index)
            if prop.typeName() == "QString":
                values.append(str(item.property(prop.name())))
    assert texts > 0, "проверка QML Text не должна быть пустой"
    values.extend(str(root.property(name)) for name in ("avState", "label", "levels"))
    return values


@dataclass
class Cycle:
    runtime: DictationRuntime
    worker: WavSupervisor
    hotkey: FakeHotkey
    target: PasteTarget
    clipboard: Any
    states: list[str] = field(default_factory=list)
    ui_strings: list[str] = field(default_factory=list)

    def snapshot(self) -> None:
        root = self.runtime.pill._root
        state = str(root.property("avState"))
        if not self.states or self.states[-1] != state:
            self.states.append(state)
        self.ui_strings.extend(qml_strings(root))
        self.ui_strings.append(str(self.runtime.tray._tray.toolTip()))

    def press(self) -> None:
        self.runtime.orchestrator.on_hotkey_state(HotkeyState.RECORDING, "press")
        wait_until(lambda: any(event["type"] == "level" for event in self.worker.events))
        assert self.runtime.phase == DictationPhase.RECORDING
        assert self.worker.recording and self.worker.source.is_open
        self.snapshot()

    def release(self) -> None:
        self.runtime.orchestrator.on_hotkey_state(HotkeyState.PROCESSING, "release")

    def complete(self) -> None:
        self.press()
        self.release()
        wait_until(lambda: "done" in self.states)
        self.target.expect_text(MARKER)
        wait_until(
            lambda: (
                self.runtime.phase == DictationPhase.IDLE
                and self.runtime.pill.state == PillState.HIDDEN
            )
        )
        self.snapshot()


@pytest.fixture
def cycle(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
    caplog: pytest.LogCaptureFixture,
) -> Iterator[Cycle]:
    # До QApplication, Stats, UI и дочерних помощников: никаких реальных XDG-файлов.
    for name in ("STATE", "CONFIG", "DATA", "CACHE"):
        directory = tmp_path / name.lower()
        directory.mkdir()
        monkeypatch.setenv(f"XDG_{name}_HOME", str(directory))
    runtime_dir = tmp_path / "runtime"
    runtime_dir.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime_dir))
    monkeypatch.setenv("DBUS_SESSION_BUS_ADDRESS", f"unix:path={tmp_path / 'no-session-bus'}")
    monkeypatch.setenv("ASTRA_VOICE_RESOURCES", str(REPO))
    monkeypatch.setenv("QT_QUICK_BACKEND", "software")
    caplog.set_level(logging.DEBUG)
    caplog.set_level(logging.DEBUG, logger="astra_voice")
    for name, logger in list(logging.Logger.manager.loggerDict.items()):
        if name.startswith("astra_voice.") and isinstance(logger, logging.Logger):
            caplog.set_level(logging.DEBUG, logger=name)
            monkeypatch.setattr(logger, "propagate", True)
            monkeypatch.setattr(logger, "disabled", False)

    app = request.getfixturevalue("xapp")
    display = request.getfixturevalue("xdisplay")
    source_path = REPO / "data/test/test-ru-20s.wav"
    if not source_path.is_file():
        source_path = tmp_path / "source.wav"
        # Тот же PCM16 и постоянный сигнал, что в tests/unit/test_audio.py::wav_factory.
        with wave.open(str(source_path), "wb") as wav:
            wav.setparams((CHANNELS, SAMPLE_BYTES, RATE, 0, "NONE", "not compressed"))
            wav.writeframes(struct.pack("<h", 8192) * (CHUNK_BYTES // SAMPLE_BYTES) * 10)
    source = WavFileSource(source_path)
    hotkey = FakeHotkey()

    def supervisor_factory(
        *, on_event: Callable[[dict[str, Any]], None], use_qt: bool
    ) -> WorkerSupervisor:
        assert use_qt
        return cast(WorkerSupervisor, WavSupervisor(source, on_event))

    runtime = DictationRuntime(
        settings=Settings(autostart=False),
        session_kind=SessionKind.KDE,
        hotkey_factory=lambda: cast(HotkeyManager, hotkey),
        supervisor_factory=supervisor_factory,
    )
    worker = cast(WavSupervisor, runtime.supervisor)
    clipboard = app.clipboard()
    view = runtime.pill._view
    menu = runtime.tray._menu
    try:
        assert isinstance(runtime.pill, Pill) and isinstance(runtime.tray, Tray)
        assert isinstance(runtime.tray._tray, QSystemTrayIcon)
        assert view.status() == QQuickView.Ready, [error.toString() for error in view.errors()]
        runtime.start()
        assert runtime.notifier is None
        assert hotkey.grabs == [(runtime.settings.hotkey, HotkeyMode.PTT)]
        with paste_target(tmp_path / "target") as target:
            with clipboard_owner(tmp_path / "clipboard", ORIGINAL_CLIPBOARD):
                wait_until(lambda: clipboard.text() == ORIGINAL_CLIPBOARD)
                target.activate(display)
                rig = Cycle(runtime, worker, hotkey, target, clipboard)
                show_state = runtime.pill.show_state
                set_tray_state = runtime.tray.set_state

                def observe(
                    state: PillState, *, text: str | None = None, level: float | None = None
                ) -> None:
                    show_state(state, text=text, level=level)
                    rig.snapshot()

                def observe_tray(state: TrayState, tooltip: str | None = None) -> None:
                    set_tray_state(state, tooltip)
                    rig.snapshot()

                monkeypatch.setattr(runtime.pill, "show_state", observe)
                monkeypatch.setattr(runtime.tray, "set_state", observe_tray)
                rig.snapshot()
                yield rig
    finally:
        runtime.shutdown()
        if worker.state != "stopped":
            worker.stop()
        source.close()
        clipboard.clear()
        # QMenu не имеет родителя; окно пилюли удаляется через deleteLater.
        sip.delete(menu)
        sip.delete(runtime)
        sip.delete(worker)
        app.sendPostedEvents(None, QEvent.DeferredDelete)
        app.processEvents()


def test_successful_cycle_pastes_and_restores_clipboard(cycle: Cycle) -> None:
    before = cycle.clipboard.mimeData()
    formats = {
        str(fmt): bytes(before.data(fmt)) for fmt in before.formats() if fmt.startswith("text/")
    }
    assert {"text/plain", "text/html"} <= formats.keys()
    cycle.complete()
    assert cycle.states == ["hidden", "listening", "processing", "done", "hidden"]
    assert cycle.clipboard.text() == ORIGINAL_CLIPBOARD
    after = cycle.clipboard.mimeData()
    assert all(bytes(after.data(fmt)) == data for fmt, data in formats.items())
    assert [command["type"] for command in cycle.worker.commands] == [
        "record.start",
        "record.stop",
        "recognize",
    ]
    assert cycle.worker.source.ended and cycle.worker.samples_read > 0
    assert [event["type"] for event in cycle.worker.events] == ["audio.ready", "level", "result"]
    assert all(
        event["generation"] == cycle.worker.generation
        and event["utterance_id"] == cycle.worker.commands[0]["utterance_id"]
        for event in cycle.worker.events
    )


def test_o4_pill_is_visible_while_recording(cycle: Cycle) -> None:
    cycle.press()
    assert cycle.runtime.guard.recording
    assert cycle.runtime.pill.state == PillState.LISTENING
    assert cycle.runtime.pill.visible
    assert cycle.runtime.pill._view.isVisible() and cycle.runtime.pill._view.isExposed()
    assert cycle.runtime.pill._root.isVisible()
    cycle.target.expect_text("")
    assert cycle.worker.recording and cycle.runtime.pill.visible


def test_escape_cancels_without_pasting(cycle: Cycle) -> None:
    cycle.press()
    cycle.runtime.orchestrator.cancel("escape")
    cycle.release()
    wait_until(lambda: "cancelled" in cycle.states)
    # Поздний ответ с правильными идентификаторами также не должен попасть в окно.
    cycle.worker.queue_event("result", text=MARKER)
    wait_until(lambda: not cycle.worker.pending)
    wait_until(lambda: cycle.runtime.phase == DictationPhase.IDLE)
    cycle.target.expect_text("")
    assert cycle.clipboard.text() == ORIGINAL_CLIPBOARD
    assert [command["type"] for command in cycle.worker.commands] == [
        "record.start",
        "record.cancel",
    ]
    assert cycle.worker.commands[1]["utterance_id"] == cycle.worker.commands[0]["utterance_id"]
    assert "cancelled" in cycle.states and "done" not in cycle.states
    assert cycle.runtime.last_text is None and not cycle.worker.recording


def test_t13_marker_absent_from_logs_stats_and_ui(
    cycle: Cycle, caplog: pytest.LogCaptureFixture
) -> None:
    cycle.complete()
    assert cycle.runtime.last_text == MARKER  # Положительный контроль доставки результата.
    cycle.runtime.shutdown()
    cycle.snapshot()
    stats_text = (state_dir() / "stats.json").read_text(encoding="utf-8")
    assert MARKER not in stats_text
    assert MARKER not in json.dumps(json.loads(stats_text), ensure_ascii=False)
    assert any(
        event["type"] == "dictation" and event["result"] == "ok"
        for event in json.loads(stats_text)["events"]
    )
    records = [
        record
        for phase in ("setup", "call")
        for record in caplog.get_records(phase)
        if record.name == "astra_voice" or record.name.startswith("astra_voice.")
    ]
    assert any(record.levelno == logging.DEBUG for record in records)
    assert all(MARKER not in caplog.handler.format(record) for record in records)
    assert cycle.ui_strings and all(MARKER not in value for value in cycle.ui_strings)
    assert MARKER not in cycle.runtime.tray._tray.toolTip()


def test_us84_shutdown_releases_runtime_resources(cycle: Cycle) -> None:
    cycle.press()
    cycle.release()
    # Останавливаем в PROCESSING: активны сторож и таймер хоткея, result ещё в Qt.
    # После отпускания запись живёт ещё RELEASE_TAIL_MS (блок 5 M6) — ждём перехода.
    runtime = cycle.runtime
    wait_until(lambda: runtime.phase == DictationPhase.PROCESSING)
    assert runtime.phase == DictationPhase.PROCESSING
    assert runtime.timers and runtime.tick_timer is not None and runtime.tick_timer.isActive()
    timers = list(runtime.findChildren(QTimer))
    assert any(timer.isActive() for timer in timers)
    runtime.shutdown()
    assert not runtime.timers
    assert all(sip.isdeleted(timer) or not timer.isActive() for timer in timers)
    assert not any(timer.isActive() for timer in runtime.findChildren(QTimer))
    assert not runtime.pill.visible and not runtime.pill._view.isVisible()
    assert cycle.hotkey.ungrab_calls == 1
    cycle.hotkey.backend.ungrab_escape.assert_called_once_with()
    cycle.hotkey.backend.close.assert_called_once_with()
    assert cycle.worker.trace[-2:] == ["audio.close", "stop"]
    assert cycle.worker.state == "stopped" and not cycle.worker.source.is_open
    assert not cycle.worker.timer.isActive() and not cycle.worker.pending
    cycle.target.expect_text("")
