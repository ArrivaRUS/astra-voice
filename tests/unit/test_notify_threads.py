"""notify() из рабочих потоков без Python-QObject вне GUI (уроки 025, 026).

Рабочий поток кладёт уведомление Python-значением в очередь и будит бессмертный
получатель через ``QMetaObject.invokeMethod`` без Python-аргументов. Получатель
создаётся в GUI; после ``shutdown_dispatch()`` поток в очередь событий Qt не постит.
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

from astra_voice.ui import notify as notifications
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.unit

_SCENARIO = Path(__file__).resolve().parents[1] / "helpers" / "notify_exit_lifetime.py"
_WORKER = "ntf-worker"


@pytest.fixture(autouse=True)
def delivered(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[tuple[str, int]]]:
    """Доставка в GUI без D-Bus: заголовок и поток, в котором вызван _submit."""
    get_qapplication()
    notifications.reset_state()
    received: list[tuple[str, int]] = []
    monkeypatch.setattr(
        notifications,
        "_submit",
        lambda notice: received.append((notice.summary, threading.get_ident())),
    )
    yield received
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
    notifications.reset_state()


class _QtTouches:
    """Профилировщик рабочих потоков: всё, чем они касаются Qt."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.qt_calls: list[str] = []
        self.qobject_methods: list[str] = []
        self.threads: set[int] = set()

    def __call__(self, frame: FrameType, event: str, arg: Any) -> None:
        if not threading.current_thread().name.startswith(_WORKER):
            return
        if event == "c_call":
            owner = getattr(arg, "__self__", None)
            qt_owner = isinstance(owner, (sip.simplewrapper, sip.wrappertype))
            if owner is None or owner is sip or qt_owner:
                with self.lock:
                    self.qt_calls.append(getattr(arg, "__name__", repr(arg)))
        elif event == "call":
            with self.lock:
                self.threads.add(threading.get_ident())
                if isinstance(frame.f_locals.get("self"), QObject):
                    self.qobject_methods.append(frame.f_code.co_qualname)


def _run_workers(count: int, *, prefix: str = "t") -> _QtTouches:
    touches = _QtTouches()
    barrier = threading.Barrier(count)
    errors: list[BaseException] = []

    def send(index: int) -> None:
        try:
            barrier.wait(timeout=2)
            notifications.notify(f"{prefix}-{index}", "тело", retry=False)
        except BaseException as exc:  # noqa: BLE001 — ошибку проверит тест
            errors.append(exc)

    threads = [
        threading.Thread(target=send, args=(index,), name=f"{_WORKER}-{index}")
        for index in range(count)
    ]
    threading.setprofile(touches)
    try:
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=3)
    finally:
        threading.setprofile(None)
    assert all(not thread.is_alive() for thread in threads)
    assert not errors
    return touches


def _process_until(predicate: Any) -> None:
    deadline = time.monotonic() + 2
    while not predicate():
        QCoreApplication.processEvents()
        assert time.monotonic() < deadline, "уведомления не дошли до GUI"
        time.sleep(0.001)


def test_thread_notify_delivered_in_gui_without_qobject(delivered: list[tuple[str, int]]) -> None:
    notifications.install_dispatcher()
    gui = threading.get_ident()
    touches = _run_workers(6)
    _process_until(lambda: len(delivered) == 6)
    assert {summary for summary, _ in delivered} == {f"t-{index}" for index in range(6)}
    assert {thread for _, thread in delivered} == {gui}
    assert touches.threads and gui not in touches.threads
    # В рабочем потоке нет ни одного метода QObject (в т. ч. __init__ наследника).
    assert touches.qobject_methods == []
    # Единственное обращение к Qt — пробуждение получателя без Python-аргументов.
    assert set(touches.qt_calls) == {"invokeMethod"}


def test_receiver_is_immortal_gui_object() -> None:
    notifications.install_dispatcher()
    receiver = notifications._receiver
    assert receiver is not None
    notifications.install_dispatcher()
    notifications.reset_state()
    assert notifications._receiver is receiver
    assert receiver.thread() is QCoreApplication.instance().thread()
    assert not sip.ispyowned(receiver)
    assert notifications._gui_ident == threading.get_ident()
    errors: list[BaseException] = []

    def install_in_worker() -> None:
        try:
            notifications.install_dispatcher()
        except RuntimeError as exc:
            errors.append(exc)

    worker = threading.Thread(target=install_in_worker)
    worker.start()
    worker.join(2)
    assert errors, "получатель ставился бы из рабочего потока"
    assert notifications._receiver is receiver


def test_thread_notify_before_install_waits_for_receiver(
    monkeypatch: pytest.MonkeyPatch, delivered: list[tuple[str, int]]
) -> None:
    # Получатель бессмертен: подставляем уже созданный, чтобы тест не плодил объекты.
    notifications.install_dispatcher()
    existing = notifications._receiver
    monkeypatch.setattr(notifications, "_receiver", None)
    monkeypatch.setattr(notifications, "_gui_ident", None)
    monkeypatch.setattr(notifications, "_NotifyReceiver", lambda: existing)
    invoke = Mock()
    monkeypatch.setattr(QMetaObject, "invokeMethod", invoke)
    touches = _run_workers(3, prefix="early")
    QCoreApplication.processEvents()
    assert delivered == []
    invoke.assert_not_called()
    assert touches.qobject_methods == []
    assert notifications._inbox.qsize() == 3
    gui = threading.get_ident()
    notifications.install_dispatcher()
    assert notifications._receiver is existing
    assert sorted(summary for summary, _ in delivered) == [f"early-{index}" for index in range(3)]
    assert {thread for _, thread in delivered} == {gui}


def test_gui_thread_notify_submits_directly(
    monkeypatch: pytest.MonkeyPatch, delivered: list[tuple[str, int]]
) -> None:
    invoke = Mock()
    monkeypatch.setattr(QMetaObject, "invokeMethod", invoke)
    notifications.notify("прямо")
    assert delivered == [("прямо", threading.get_ident())]
    invoke.assert_not_called()


def test_gate_stops_posts_to_qt_after_shutdown(
    monkeypatch: pytest.MonkeyPatch, delivered: list[tuple[str, int]]
) -> None:
    notifications.install_dispatcher()
    invoke = Mock()
    monkeypatch.setattr(QMetaObject, "invokeMethod", invoke)
    running, stop = threading.Event(), threading.Event()
    posted = 0

    def spam() -> None:
        nonlocal posted
        running.set()
        while not stop.is_set():
            notifications.notify("поток", retry=False)
            posted += 1

    worker = threading.Thread(target=spam, name=f"{_WORKER}-spam", daemon=True)
    worker.start()
    assert running.wait(2)
    deadline = time.monotonic() + 2
    while not invoke.call_count:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    notifications.shutdown_dispatch()
    after_close = invoke.call_count
    queued = notifications._inbox.qsize()
    before = posted
    deadline = time.monotonic() + 2
    while posted < before + 1000:
        assert time.monotonic() < deadline
        time.sleep(0.001)
    stop.set()
    worker.join(2)
    assert invoke.call_count == after_close
    # После закрытия уведомления потоков отбрасываются, очередь не растёт.
    assert notifications._inbox.qsize() == queued
    # GUI-поток после закрытия работает как прежде, без пробуждений.
    notifications.notify("из GUI")
    assert delivered[-1] == ("из GUI", threading.get_ident())
    assert invoke.call_count == after_close
    # reset_state() (изоляция тестов) открывает затвор и чистит очередь.
    notifications.reset_state()
    assert notifications._inbox.empty()
    worker = threading.Thread(target=notifications.notify, args=("снова",), name=_WORKER)
    worker.start()
    worker.join(2)
    assert invoke.call_count == after_close + 1


def test_reset_state_drops_stale_thread_notices(
    monkeypatch: pytest.MonkeyPatch, delivered: list[tuple[str, int]]
) -> None:
    notifications.install_dispatcher()
    worker = threading.Thread(target=notifications.notify, args=("до сброса",), name=_WORKER)
    worker.start()
    worker.join(2)
    notifications.reset_state()
    assert notifications._inbox.empty()
    receiver = notifications._receiver
    assert receiver is not None
    # Запоздалое пробуждение до сброса ничего не доставляет; старая эпоха отброшена.
    notice = notifications._Notice("старая эпоха", "", 1, (), 0.0, False)
    notifications._inbox.put((notice, notifications._epoch - 1))
    QCoreApplication.sendPostedEvents(None, QEvent.MetaCall)
    receiver._drain()
    assert delivered == []


def _run_exit_scenario(delay: float) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    root = Path(__file__).resolve().parents[2]
    env["PYTHONPATH"] = os.pathsep.join(
        [str(root / "src"), str(root / "tests"), env.get("PYTHONPATH", "")]
    )
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["ASTRA_VOICE_NOTIFY_EXIT_DELAY"] = f"{delay:.2f}"
    return subprocess.run(
        [sys.executable, str(_SCENARIO), "exit_while_thread_notifies"],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
        check=False,
    )


def test_exit_while_thread_notifies() -> None:
    """Выход, пока поток без передышки шлёт уведомления, не зависает и не падает."""
    for attempt in range(6):
        result = _run_exit_scenario(attempt * 0.02)
        output = f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        assert result.returncode == 0, output
        assert "NOTIFY_OK:exit_while_thread_notifies" in result.stdout, output
        assert "Fatal Python error" not in result.stderr, output
