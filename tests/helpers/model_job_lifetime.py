"""Подпроцессный сценарий: выход приложения, пока рабочий поток загрузки висит в запросе.

Урок 026: запоздалый пост рабочего потока в очередь событий Qt во время
``~QApplication`` может взаимно заблокироваться с её разрушением. Здесь поток
установки модели «висит в запросе»: отмену не слышит и без передышки сообщает
прогресс. ``shutdown()`` ждёт его недолго и сдаётся, затем процесс выходит с
живым потоком. Процесс обязан завершиться сам, без зависания и падения.
"""

from __future__ import annotations

import faulthandler
import gc
import os
import resource
import sys
import threading
from collections.abc import Callable
from pathlib import Path
from time import monotonic, sleep
from typing import Any

from PyQt5.QtCore import QCoreApplication
from PyQt5.QtWidgets import QApplication

from astra_voice.models.catalog import CatalogEntry
from astra_voice.models.downloader import Progress
from astra_voice.ui import model_downloads
from astra_voice.ui.model_downloads import ModelDownloads

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from test_bridges import FakeModelPort  # noqa: E402

MARKER = "MODEL_JOB_OK:exit_while_download_blocked"

# Держим до выхода интерпретатора: QApplication разрушается при его завершении.
_exit_app: QApplication | None = None
_exit_downloads: ModelDownloads | None = None


class HangingPort(FakeModelPort):
    """Загрузка «висит в запросе»: отмену не слышит, прогресс идёт непрерывно."""

    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()

    def download(
        self,
        entry: CatalogEntry,
        *,
        progress: Callable[[Progress], None],
        cancel: threading.Event,
        source: Callable[[str], None] | None = None,
    ) -> Any:
        self.started.set()
        done = 0
        while True:
            done = (done + 1) % entry.size_bytes
            progress(Progress(done, entry.size_bytes, 0, None, 1, 1))
            sleep(0.0002)


def exit_while_download_blocked() -> None:
    global _exit_app, _exit_downloads
    _exit_app = QApplication([])
    port = HangingPort()
    downloads = ModelDownloads(port)
    _exit_downloads = downloads
    downloads.download()
    thread = downloads._model_thread
    assert thread is not None
    assert port.started.wait(5), "рабочий поток не начал загрузку"
    # Несколько циклов событий: пробуждения от потока реально доходят до GUI.
    deadline = monotonic() + 0.05
    while monotonic() < deadline:
        QCoreApplication.processEvents()
    # Ожидание короче «запроса»: поток гарантированно переживает shutdown().
    model_downloads._SHUTDOWN_JOIN_S = 0.1
    downloads.shutdown()
    assert thread.is_alive(), "поток не висел в запросе"
    # Сдвиг выхода относительно постов потока.
    sleep(float(os.environ.get("ASTRA_VOICE_MODEL_EXIT_DELAY", "0")))
    # Как в app.main(): QApplication разрушается сразу после shutdown(), пока
    # поток ещё жив и сообщает прогресс, — под сторожем.
    faulthandler.dump_traceback_later(10, exit=True)
    _exit_app = None
    gc.collect()
    sleep(0.05)
    assert thread.is_alive()


def main() -> int:
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    faulthandler.enable()
    faulthandler.dump_traceback_later(25, exit=True)
    if interval := os.environ.get("ASTRA_VOICE_MODEL_SWITCH"):
        sys.setswitchinterval(float(interval))
    scenario = sys.argv[1]
    scenarios: dict[str, Callable[[], None]] = {
        "exit_while_download_blocked": exit_while_download_blocked,
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
