"""Нагрузочный прогон установки модели: ловит взаимную блокировку GIL ↔ Qt.

Вне unit-гейта и CI. Запуск (минуты):
    env QT_QPA_PLATFORM=offscreen pytest -m stress tests/stress
Длительность — ASTRA_VOICE_STRESS_SECONDS (по умолчанию 180).

Раньше задание установки было QObject в рабочем QThread и удалялось там
(deleteLater в QThreadPrivate::finish): ~QObject брал GIL под мьютексом сигналов
Qt, а GUI под GIL ждал тот же мьютекс при уничтожении другого QObject — процесс
замирал (урок 025). Теперь задания — threading.Thread без Python-QObject (урок
026), а прогон сторожит, что класс зависания не вернулся. Частое переключение
потоков и частая сборка мусора поднимают вероятность пересечения; сторож
faulthandler на зависшей итерации печатает стеки всех потоков и завершает
процесс, чтобы прогон не висел молча.
"""

from __future__ import annotations

import faulthandler
import gc
import itertools
import os
import sys
import time
from collections.abc import Iterator
from functools import partial
from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtTest import QSignalSpy

from astra_voice.models.installer import InstallResult, ReasonCode

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
from test_model_job_threads import _run_exit_scenario, assert_exit_scenario  # noqa: E402

from helpers.model_rig import model_rig_session  # noqa: E402
from helpers.qt_app import get_qapplication  # noqa: E402

pytestmark = pytest.mark.stress

_WATCHDOG_S = 15
_REASON_CODES: tuple[ReasonCode, ...] = ("selfcheck", "checksum", "layout")


@pytest.fixture
def stress_runtime(request: pytest.FixtureRequest) -> Iterator[None]:
    if "stress" not in (request.config.option.markexpr or ""):
        pytest.skip("только по явному -m stress")
    get_qapplication()
    interval, threshold = sys.getswitchinterval(), gc.get_threshold()
    sys.setswitchinterval(0.05)
    gc.set_threshold(10)
    try:
        yield
    finally:
        faulthandler.cancel_dump_traceback_later()
        sys.setswitchinterval(interval)
        gc.set_threshold(*threshold)


def _capture_messages(controller: Any, displayed: list[str]) -> None:
    displayed.extend((controller.modelMessage, controller.downloadTitle))
    displayed.extend(card["message"] for card in controller.models)


def test_model_install_cycle_does_not_deadlock(stress_runtime: None) -> None:
    duration = float(os.environ.get("ASTRA_VOICE_STRESS_SECONDS", "180"))
    params = itertools.cycle(
        itertools.product((False, True), _REASON_CODES, ("SECRET /path/service", ""))
    )
    deadline = time.monotonic() + duration
    iterations = 0
    while time.monotonic() < deadline:
        faulthandler.dump_traceback_later(_WATCHDOG_S, exit=True)
        local, reason_code, reason = next(params)
        fixture = model_rig_session()
        port, create = next(fixture)
        port.result = InstallResult("broken", reason, reason_code=reason_code)
        controller = create()
        displayed: list[str] = []
        # Замыкание на сигналах замыкает цикл ссылок: контроллер уходит не по
        # счётчику ссылок, а сборщиком мусора в случайный момент, в том числе
        # пока рабочий поток следующей итерации ещё сообщает о ходе установки.
        capture = partial(_capture_messages, controller, displayed)
        for signal in (
            controller.modelMessageChanged,
            controller.modelsChanged,
            controller.downloadTitleChanged,
        ):
            signal.connect(capture)
        done = QSignalSpy(controller.canFinishChanged)
        if local:
            controller.installFromPath("/fake/model")
        else:
            controller.download()
        assert done.wait(1000), f"итерация {iterations}: установка не завершилась"
        assert controller.modelState == "broken"
        with pytest.raises(StopIteration):
            next(fixture)
        iterations += 1
    faulthandler.cancel_dump_traceback_later()
    print(f"stress: {iterations} итераций за {duration:.0f} с, зависаний нет")
    assert iterations > 0


def test_model_exit_while_download_blocked(request: pytest.FixtureRequest) -> None:
    """Выход, пока поток загрузки висит в запросе: 20 повторов (в unit — 6)."""
    if "stress" not in (request.config.option.markexpr or ""):
        pytest.skip("только по явному -m stress")
    for attempt in range(20):
        # Выход в разные моменты относительно постов потока; половина — с частым
        # переключением потоков.
        switch = "1e-5" if attempt % 2 else ""
        assert_exit_scenario(_run_exit_scenario((attempt // 2) * 0.01, switch=switch))
