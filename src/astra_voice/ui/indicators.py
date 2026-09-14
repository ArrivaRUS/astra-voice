"""Страж видимой индикации О4: остановку записи запрашивает у оркестрации."""

from __future__ import annotations

from collections.abc import Callable
from time import monotonic
from typing import TYPE_CHECKING

from PyQt5.QtCore import QObject, QTimer

from astra_voice.ui import notify

if TYPE_CHECKING:
    from astra_voice.ui.pill import Pill
    from astra_voice.ui.tray import Tray


def indicator_visible(*, pill_visible: bool, tray_registered: bool) -> bool:
    """Виден хотя бы один индикатор; чистая проверка без обращения к Qt."""
    return pill_visible or tray_registered


class IndicatorGuard(QObject):
    """Наблюдает за индикаторами, не владея записью или их состояниями UI."""

    def __init__(
        self,
        pill: Pill,
        tray: Tray,
        *,
        poll_ms: int = 500,
        grace_ms: int = 1000,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self.on_stop_recording: Callable[[], None] | None = None
        self._pill = pill
        self._tray = tray
        self._recording = False
        self._loss_reported = False
        self._grace_seconds = grace_ms / 1000
        self._grace_deadline = 0.0
        self._timer = QTimer(self)
        self._timer.setInterval(poll_ms)
        self._timer.timeout.connect(self.check)

    def set_recording(self, value: bool) -> None:
        """Обновить факт записи и сразу проверить индикацию при её начале."""
        if value and not self._recording:
            self._grace_deadline = monotonic() + self._grace_seconds
        self._recording = value
        if value:
            # Колбэк из check() может синхронно вызвать set_recording(False).
            self.start()
            self.check()
        else:
            self.stop()
            self._loss_reported = False
            self._pill.set_forced(False)

    @property
    def recording(self) -> bool:
        return self._recording

    @property
    def ok(self) -> bool:
        """Есть ли прямо сейчас видимый индикатор, независимо от записи."""
        return indicator_visible(
            pill_visible=self._pill.visible, tray_registered=self._tray.registered
        )

    def check(self) -> bool:
        """Применить О4 во время записи; вне записи только прочитать видимость."""
        if not self._recording:
            return self.ok

        self._pill.set_forced(not self._tray.registered)
        if self.ok:
            self._loss_reported = False
        elif not self._loss_reported and monotonic() >= self._grace_deadline:
            # Грейс даёт пилюле время показаться, не подавляя её принудительный показ.
            # До внешних вызовов: повторный вход не должен дублировать эпизод.
            self._loss_reported = True
            notify.notify_indicators_lost()
            if self.on_stop_recording is not None:
                self.on_stop_recording()
        return self.ok

    def start(self) -> None:
        """Включить периодические проверки, только если запись уже идёт."""
        if self._recording and not self._timer.isActive():
            self._timer.start()

    def stop(self) -> None:
        """Остановить только таймер; факт записи сообщает оркестрация."""
        self._timer.stop()
