"""Рабочие потоки загрузки моделей без Python-QObject (уроки 025, 026).

Задания установки и перепроверки живут в ``threading.Thread``: Qt рабочий поток
трогает только через ``QMetaObject.invokeMethod`` бессмертного получателя, всё
остальное — Python-значения в очереди канала. После ``shutdown()`` затвор канала
закрыт, и в очередь событий Qt поток больше не постит.
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import FrameType
from typing import Any
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QCoreApplication, QEvent, QMetaObject, QObject

from astra_voice.models.installer import InstallResult
from astra_voice.ui import model_downloads
from astra_voice.ui.model_downloads import ModelDownloads, _JobChannel
from helpers.model_rig import FakeModelPort
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit

_SCENARIO = Path(__file__).resolve().parents[1] / "helpers" / "model_job_lifetime.py"
_GUI_SLOTS = (
    "_model_progressed",
    "_model_staged",
    "_model_sourced",
    "_model_finished",
    "_model_thread_finished",
)


@pytest.fixture(autouse=True)
def qapp() -> Iterator[None]:
    get_qapplication()
    yield
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)


def _wait_idle(downloads: ModelDownloads) -> None:
    deadline = time.monotonic() + 2
    while downloads._model_thread is not None:
        QCoreApplication.processEvents()
        assert time.monotonic() < deadline, "Задание не завершилось"
        time.sleep(0.001)


class _QtTouches:
    """Профилировщик рабочих потоков заданий: всё, чем они касаются Qt."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.qt_calls: list[str] = []
        self.qobject_methods: list[str] = []
        self.threads: set[int] = set()

    def __call__(self, frame: FrameType, event: str, arg: Any) -> None:
        if threading.current_thread().name != model_downloads._JOB_THREAD_NAME:
            return
        if event == "c_call":
            owner = getattr(arg, "__self__", None)
            # Методы объектов Qt привязаны к sip-обёртке; статические методы классов
            # Qt (как QMetaObject.invokeMethod) — ни к чему: у обычных встроенных
            # функций __self__ — их модуль.
            qt_owner = isinstance(owner, (sip.simplewrapper, sip.wrappertype))
            if owner is None or owner is sip or qt_owner:
                with self.lock:
                    self.qt_calls.append(getattr(arg, "__name__", repr(arg)))
        elif event == "call":
            with self.lock:
                self.threads.add(threading.get_ident())
                # Python-метод QObject (в т. ч. __init__ наследника) в рабочем потоке.
                if isinstance(frame.f_locals.get("self"), QObject):
                    self.qobject_methods.append(frame.f_code.co_qualname)


@pytest.mark.parametrize("scenario", ["download", "local", "recheck"])
def test_worker_touches_qt_only_through_wakeup(
    monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    port = FakeModelPort()
    if scenario == "recheck":
        monkeypatch.setattr(port, "recheck_entries", lambda: (port.entry,))
        monkeypatch.setattr(port, "verify_files", lambda entry: (True, ""))
        monkeypatch.setattr(port, "smoke", lambda entry: (True, ""))
        monkeypatch.setattr(port, "mark_ok", lambda model_id, revision: None)
    downloads = ModelDownloads(port)
    gui = threading.get_ident()
    slot_threads: dict[str, set[int]] = {name: set() for name in _GUI_SLOTS}
    for name in _GUI_SLOTS:
        original = getattr(downloads, name)

        def record(*args: Any, _name: str = name, _original: Any = original) -> Any:
            slot_threads[_name].add(threading.get_ident())
            return _original(*args)

        monkeypatch.setattr(downloads, name, record)
    touches = _QtTouches()
    threading.setprofile(touches)
    try:
        if scenario == "download":
            downloads.download()
        elif scenario == "local":
            downloads.installFromPath("/fake/model")
        else:
            downloads.start_recheck()
        thread = downloads._model_thread
        assert isinstance(thread, threading.Thread) and thread.daemon
        assert not isinstance(downloads._model_job, QObject)
        _wait_idle(downloads)
    finally:
        threading.setprofile(None)
        downloads.shutdown()
    assert touches.threads and gui not in touches.threads
    assert touches.qobject_methods == []
    # Единственное обращение к Qt — пробуждение получателя без Python-аргументов.
    assert touches.qt_calls and set(touches.qt_calls) == {"invokeMethod"}
    assert slot_threads["_model_finished"] == slot_threads["_model_thread_finished"] == {gui}
    assert set().union(*slot_threads.values()) == {gui}
    if scenario == "download":
        assert port.download_thread in touches.threads
        assert slot_threads["_model_progressed"] == {gui}
    if scenario != "recheck":
        assert downloads.modelState == "installed"


def test_waker_is_immortal_gui_object() -> None:
    downloads = ModelDownloads(FakeModelPort())
    channel = _JobChannel(downloads)
    waker = model_downloads._job_waker
    assert waker is not None and channel.waker is waker
    assert _JobChannel(downloads).waker is waker
    assert waker.thread() is QCoreApplication.instance().thread()
    assert not sip.ispyowned(waker)
    errors: list[BaseException] = []

    def create_in_worker() -> None:
        global_waker, model_downloads._job_waker = model_downloads._job_waker, None
        try:
            _JobChannel(downloads)
        except RuntimeError as exc:
            errors.append(exc)
        finally:
            model_downloads._job_waker = global_waker

    worker = threading.Thread(target=create_in_worker)
    worker.start()
    worker.join(2)
    assert errors, "получатель создался бы в рабочем потоке"
    downloads.shutdown()


def test_gate_stops_posts_to_qt_after_close(monkeypatch: pytest.MonkeyPatch) -> None:
    downloads = ModelDownloads(FakeModelPort())
    channel = _JobChannel(downloads)
    invoke = Mock()
    monkeypatch.setattr(QMetaObject, "invokeMethod", invoke)
    running, stop = threading.Event(), threading.Event()
    posted = 0

    def spam() -> None:
        nonlocal posted
        running.set()
        while not stop.is_set():
            channel.post("progress", (0.5,))
            posted += 1

    worker = threading.Thread(target=spam, name=model_downloads._JOB_THREAD_NAME, daemon=True)
    worker.start()
    assert running.wait(2)
    deadline = time.monotonic() + 2
    while not invoke.call_count:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    channel.close()
    after_close = invoke.call_count
    before = posted
    deadline = time.monotonic() + 2
    while posted < before + 1000:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    stop.set()
    worker.join(2)
    assert invoke.call_count == after_close
    # Значения по-прежнему копятся в канале: GUI разберёт их сам после join.
    assert channel.events.qsize() == posted
    # Прочий GUI-канал (владелец ещё не закрыт) пробуждения не лишается.
    other = _JobChannel(downloads)
    other.post("progress", (0.1,))
    assert invoke.call_count == after_close + 1
    downloads.shutdown()


def test_stale_channel_messages_are_ignored() -> None:
    port = FakeModelPort()
    port.block = True
    downloads = ModelDownloads(port)
    stale = _JobChannel(downloads)
    stale.post("finished", ("installed", ""))
    stale.post("exit", ())
    downloads.download()
    assert downloads._model_channel is not stale
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
    assert downloads._model_result is None
    assert downloads._model_thread is not None
    downloads.shutdown()
    assert downloads._model_thread is None
    assert downloads.modelState == "cancelled"


def test_shutdown_during_blocked_install_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Остановка, пока задание висит: ожидание ограничено, итог — без пробуждений Qt."""
    port = FakeModelPort()
    release, started = threading.Event(), threading.Event()

    def install(entry: Any) -> InstallResult:
        started.set()
        assert release.wait(10), "Тест не освободил установку"
        return InstallResult("ok")

    monkeypatch.setattr(port, "install_from_staging", install)
    monkeypatch.setattr(model_downloads, "_SHUTDOWN_JOIN_S", 0.2)
    downloads = ModelDownloads(port)
    downloads.download()
    thread, channel = downloads._model_thread, downloads._model_channel
    assert thread is not None and channel is not None
    try:
        assert started.wait(2)
        before = time.monotonic()
        downloads.shutdown()
        assert time.monotonic() - before < 1.0
        assert channel.closed and thread.is_alive()
        assert downloads._model_thread is None and downloads._model_channel is None
        invoke = Mock()
        monkeypatch.setattr(QMetaObject, "invokeMethod", invoke)
    finally:
        release.set()
        thread.join(2)
    assert not thread.is_alive()
    invoke.assert_not_called()
    assert [kind for kind, _ in list(channel.events.queue)][-2:] == ["finished", "exit"]


def _run_exit_scenario(delay: float, *, switch: str = "") -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root / "src"), str(root / "tests"), env.get("PYTHONPATH", "")]
    )
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["ASTRA_VOICE_MODEL_EXIT_DELAY"] = f"{delay:.2f}"
    env["ASTRA_VOICE_MODEL_SWITCH"] = switch
    return subprocess.run(
        [sys.executable, str(_SCENARIO), "exit_while_download_blocked"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )


def assert_exit_scenario(result: subprocess.CompletedProcess[str]) -> None:
    output = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert result.returncode == 0, output
    assert "MODEL_JOB_OK:exit_while_download_blocked" in result.stdout, output
    assert "Fatal Python error" not in result.stderr, output


def test_exit_while_download_blocked() -> None:
    """Выход, пока поток висит в запросе и сообщает прогресс, не зависает и не падает.

    20 повторов — в tests/stress/test_model_install_stress.py.
    """
    for attempt in range(6):
        assert_exit_scenario(_run_exit_scenario(attempt * 0.02))


def test_shutdown_closes_gate_even_if_cancel_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    port = FakeModelPort()
    port.block = True
    downloads = ModelDownloads(port)
    downloads.download()
    thread, channel = downloads._model_thread, downloads._model_channel
    assert thread is not None and channel is not None
    monkeypatch.setattr(downloads, "_cancel_queue", Mock(side_effect=RuntimeError("сбой")))
    with pytest.raises(RuntimeError):
        downloads.shutdown()
    # Затвор закрыт, хотя отмена очереди упала: поток не постит в Qt до ~QApplication.
    assert channel.closed
    downloads._model_cancel.set()
    thread.join(2)
    assert not thread.is_alive()


@pytest.mark.parametrize("scenario", ["download", "recheck"])
def test_thread_start_failure_resets_and_queue_continues(
    monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    port = FakeModelPort()
    if scenario == "recheck":
        monkeypatch.setattr(port, "recheck_entries", lambda: (port.entry,))
        monkeypatch.setattr(port, "verify_files", lambda entry: (True, ""))
        monkeypatch.setattr(port, "smoke", lambda entry: (True, ""))
        monkeypatch.setattr(port, "mark_ok", lambda model_id, revision: None)
    downloads = ModelDownloads(port)
    original_start = threading.Thread.start

    def start(self: threading.Thread) -> None:
        if self.name == model_downloads._JOB_THREAD_NAME:
            raise RuntimeError("can't start new thread")
        original_start(self)

    monkeypatch.setattr(threading.Thread, "start", start)
    try:
        before = downloads.models[0]
        if scenario == "download":
            downloads.download()
        else:
            downloads.start_recheck()
        assert downloads._model_thread is None
        assert downloads._model_job is None and downloads._model_channel is None
        assert not downloads._queue_running and not downloads._rechecking
        if scenario == "download":
            assert downloads.models[0]["state"] == "failed"
            assert downloads.downloadState == "failed"
        else:
            assert downloads.models[0] == before
            assert downloads.downloadState == "idle"
        monkeypatch.setattr(threading.Thread, "start", original_start)
        # Следующая попытка запускается как обычно.
        if scenario == "download":
            downloads.retryModel(port.entry.id)
            _wait_idle(downloads)
            assert downloads.modelState == "installed"
    finally:
        downloads.shutdown()


@pytest.mark.parametrize("stage", ("verify_files", "smoke"))
def test_recheck_job_reports_finished_even_on_base_exception(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    """Итог перепроверки уходит в finally, как у _ModelJob: без него GUI узнаёт об
    исходе только по признаку выхода потока, а не от самого задания."""

    class Abort(BaseException):
        pass

    port = FakeModelPort()
    monkeypatch.setattr(port, "verify_files", lambda entry: (True, ""))
    monkeypatch.setattr(port, "smoke", lambda entry: (True, ""))
    monkeypatch.setattr(port, stage, Mock(side_effect=Abort()))
    events: list[tuple[str, tuple[Any, ...]]] = []
    job = model_downloads._RecheckJob(
        port, port.entry, threading.Event(), lambda kind, args: events.append((kind, args))
    )
    with pytest.raises(Abort):
        job.run()
    assert events == [("finished", ("cancelled", ""))]
