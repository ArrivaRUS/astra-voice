"""Подпроцессный сценарий: выход приложения, пока рабочий поток шлёт уведомления.

Урок 026: запоздалый пост рабочего потока в очередь событий Qt во время
``~QApplication`` может взаимно заблокироваться с её разрушением. Здесь поток без
передышки вызывает ``notify()``; как в ``app.main()``, затвор уведомлений
закрывается до разрушения ``QApplication``, а поток продолжает слать. Процесс
обязан завершиться сам, без зависания и падения. D-Bus не трогается: доставка в
GUI подменена счётчиком.
"""

from __future__ import annotations

import faulthandler
import gc
import os
import resource
import sys
import threading
from collections.abc import Callable
from time import monotonic, sleep

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtWidgets import QApplication

from astra_voice.ui import notify

MARKER = "NOTIFY_OK:exit_while_thread_notifies"

# Держим до выхода интерпретатора: QApplication разрушается при его завершении.
_exit_app: QApplication | None = None


def exit_while_thread_notifies() -> None:
    global _exit_app
    _exit_app = QApplication([])
    gui = threading.get_ident()
    delivered: list[int] = []

    def submit(_notice: object) -> None:
        delivered.append(threading.get_ident())

    notify._submit = submit  # type: ignore[assignment]
    notify.install_dispatcher()
    started = threading.Event()

    def spam() -> None:
        started.set()
        index = 0
        while True:
            index += 1
            notify.notify(f"ntf-{index % 7}", retry=False)
            sleep(0.0002)

    thread = threading.Thread(target=spam, name="ntf-exit-spam", daemon=True)
    thread.start()
    assert started.wait(5), "поток уведомлений не запустился"
    # Несколько циклов событий: пробуждения от потока реально доходят до GUI.
    deadline = monotonic() + 0.05
    while monotonic() < deadline or not delivered:
        QCoreApplication.processEvents()
        assert monotonic() < deadline + 5, "уведомления потока не дошли до GUI"
    assert set(delivered) == {gui}, "уведомление доставлено не в GUI-потоке"
    # Как в app.main(): затвор закрыт до разрушения QApplication.
    notify.shutdown_dispatch()
    # Сдвиг выхода относительно постов потока.
    sleep(float(os.environ.get("ASTRA_VOICE_NOTIFY_EXIT_DELAY", "0")))
    faulthandler.dump_traceback_later(10, exit=True)
    _exit_app = None
    gc.collect()
    sleep(0.05)
    assert thread.is_alive()


def main() -> int:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    faulthandler.enable()
    faulthandler.dump_traceback_later(25, exit=True)
    if interval := os.environ.get("ASTRA_VOICE_NOTIFY_SWITCH"):
        sys.setswitchinterval(float(interval))
    scenario = sys.argv[1]
    scenarios: dict[str, Callable[[], None]] = {
        "exit_while_thread_notifies": exit_while_thread_notifies,
    }
    try:
        scenarios[scenario]()
    except BaseException:
        faulthandler.cancel_dump_traceback_later()
        raise
    print(MARKER, flush=True)
    # Сторож остаётся взведённым до конца разрушения QApplication при выходе.
    faulthandler.dump_traceback_later(10, exit=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
