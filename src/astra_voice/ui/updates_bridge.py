"""Мост строки обновлений и раздела «Сеть и обновления» (M7-ядро v0.2).

Состояние приходит от :class:`~astra_voice.updates.checker.UpdateChecker`
через :meth:`UpdatesBridge.set_status`, который вызывается **только в
GUI-потоке**: колбэк проверки работает в своём потоке, и владелец (``app.py``)
передаёт снимок очередью (урок 025). Сам мост в рабочем потоке не создаётся и
не удаляется.

Текст «Что нового» пришёл из сети и считается недоверенным: мост отдаёт его
как простой текст (QML рисует только ``Text.PlainText``), а ссылку не отдаёт
вовсе — страницу выпуска открывает слот и только по адресу с префиксом
наших выпусков.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Protocol

from PyQt5.QtCore import QObject, pyqtProperty, pyqtSignal, pyqtSlot

from astra_voice.net.github import RELEASE_URL_PREFIX, parse_semver

if TYPE_CHECKING:
    from astra_voice.net.gate import NetworkKind
    from astra_voice.updates.checker import UpdateStatus

log = logging.getLogger(__name__)

#: Состояния строки-статуса, которые мост отдаёт QML (подмножество спеки §2.2).
#: ``idle`` — справа только версия: проверка включена и ничего нового нет.
STATES = (
    "idle",
    "disabled",
    "policy-locked",
    "checking",
    "uptodate",
    "available",
    "unavailable",
    "error-net",
    "skipped",
)
#: Сколько символов «Что нового» показывает панель; остальное — на странице выпуска.
MAX_NOTES_DISPLAY_CHARS = 2000
_BULLET = re.compile(r"^\s*[-*+]\s+")
_HEADING = re.compile(r"^\s*#{1,6}\s*")


class UpdateActions(Protocol):
    """То, что мост вызывает у проверки обновлений (любой поток)."""

    def check_now(self) -> None: ...

    def refresh(self) -> None: ...

    def skip_version(self, version: str) -> None: ...

    def clear_skip(self) -> None: ...

    def remind_later(self) -> None: ...


def display_notes(text: str) -> str:
    """Текст выпуска для панели: пункты списка — «•», заголовки без «#», с пределом."""
    lines: list[str] = []
    blank = False
    for raw in text.strip().splitlines():
        line = raw.rstrip()
        if not line.strip():
            if lines and not blank:
                lines.append("")
            blank = True
            continue
        blank = False
        if _BULLET.match(line):
            line = "• " + _BULLET.sub("", line)
        elif _HEADING.match(line):
            line = _HEADING.sub("", line)
        lines.append(line)
    result = "\n".join(lines).strip()
    if len(result) > MAX_NOTES_DISPLAY_CHARS:
        cut = result.rfind("\n", 0, MAX_NOTES_DISPLAY_CHARS)
        result = result[: cut if cut > 0 else MAX_NOTES_DISPLAY_CHARS].rstrip() + "\n…"
    return result


def checked_text(timestamp: float | None, now: float) -> str:
    """«Проверено сегодня в 14:02» / «Проверено 07.09.2026 в 14:02»; нет даты — пусто."""
    if timestamp is None:
        return ""
    moment = datetime.fromtimestamp(timestamp)
    clock = moment.strftime("%H:%M")
    if moment.date() == datetime.fromtimestamp(now).date():
        return f"Проверено сегодня в {clock}"
    return f"Проверено {moment.strftime('%d.%m.%Y')} в {clock}"


class UpdatesBridge(QObject):
    """Строка-статус, панель «Что нового» и ручная проверка для QML."""

    statusChanged = pyqtSignal()
    networkChanged = pyqtSignal()

    def __init__(
        self,
        checker: UpdateActions | None = None,
        *,
        refusal: Callable[[NetworkKind], str] | None = None,
        open_external: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.time,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._checker = checker
        self._refusal = refusal
        self._open_external = open_external
        self._clock = clock
        self._state = "disabled"
        self._version = ""
        self._notes = ""
        self._release_url = ""
        self._snoozed = False
        self._manual = False
        self._checked_text = ""
        self._check_refusal = ""
        self._network_refusal = ""
        self._read_network()

    # -- Python (GUI-поток) -------------------------------------------------

    def set_status(self, status: UpdateStatus) -> None:
        """Новый снимок проверки; вызывать только в GUI-потоке."""
        state = str(status.state)
        if state == "uptodate" and not status.manual:
            state = "idle"
        if state not in STATES:
            log.warning("Неизвестное состояние проверки обновлений")
            state = "unavailable"
        url = status.release_url or ""
        values = (
            state,
            status.version or "",
            display_notes(status.notes or ""),
            url if url.startswith(RELEASE_URL_PREFIX) else "",
            state == "available" and status.reminder_until is not None,
            bool(status.manual),
            checked_text(status.last_success_at, self._clock()),
        )
        current = (
            self._state,
            self._version,
            self._notes,
            self._release_url,
            self._snoozed,
            self._manual,
            self._checked_text,
        )
        if values == current:
            return
        (
            self._state,
            self._version,
            self._notes,
            self._release_url,
            self._snoozed,
            self._manual,
            self._checked_text,
        ) = values
        self.statusChanged.emit()

    def refresh(self) -> None:
        """Сменились тумблер, офлайн или политика: пересчитать доступность и строку."""
        self._read_network()
        if self._checker is not None:
            self._checker.refresh()

    def _read_network(self) -> None:
        check = download = ""
        if self._refusal is not None:
            check = self._refusal("check_app_manual")
            download = self._refusal("download")
        if (check, download) != (self._check_refusal, self._network_refusal):
            self._check_refusal, self._network_refusal = check, download
            self.networkChanged.emit()

    # -- свойства ------------------------------------------------------------

    @pyqtProperty(str, notify=statusChanged)
    def state(self) -> str:
        return self._state

    @pyqtProperty(str, notify=statusChanged)
    def version(self) -> str:
        return self._version

    @pyqtProperty(str, notify=statusChanged)
    def notes(self) -> str:
        return self._notes

    @pyqtProperty(bool, notify=statusChanged)
    def snoozed(self) -> bool:
        return self._snoozed

    @pyqtProperty(bool, notify=statusChanged)
    def manual(self) -> bool:
        return self._manual

    @pyqtProperty(str, notify=statusChanged)
    def checkedText(self) -> str:  # noqa: N802 — имя свойства для QML
        return self._checked_text

    @pyqtProperty(bool, notify=statusChanged)
    def releasePageAvailable(self) -> bool:  # noqa: N802
        return self._open_external is not None and self._release_url != ""

    @pyqtProperty(str, notify=networkChanged)
    def checkRefusal(self) -> str:  # noqa: N802
        return self._check_refusal

    @pyqtProperty(str, notify=networkChanged)
    def networkRefusal(self) -> str:  # noqa: N802
        return self._network_refusal

    @pyqtProperty(bool, notify=networkChanged)
    def canCheckNow(self) -> bool:  # noqa: N802
        return self._checker is not None and self._check_refusal == ""

    # -- слоты ---------------------------------------------------------------

    @pyqtSlot()
    def checkNow(self) -> None:  # noqa: N802
        self._read_network()
        if self._checker is not None and self._check_refusal == "":
            self._checker.check_now()

    @pyqtSlot()
    def skipVersion(self) -> None:  # noqa: N802
        if self._checker is None or parse_semver(self._version) is None:
            return
        self._checker.skip_version(self._version)

    @pyqtSlot()
    def clearSkip(self) -> None:  # noqa: N802
        if self._checker is not None:
            self._checker.clear_skip()

    @pyqtSlot()
    def remindLater(self) -> None:  # noqa: N802
        if self._checker is not None:
            self._checker.remind_later()

    @pyqtSlot()
    def openReleasePage(self) -> None:  # noqa: N802
        if self._open_external is None or not self._release_url.startswith(RELEASE_URL_PREFIX):
            return
        try:
            if not self._open_external(self._release_url):
                log.warning("Не удалось открыть страницу выпуска")
        except Exception:  # noqa: BLE001 — сбой внешней программы не ломает окно
            log.warning("Не удалось открыть страницу выпуска", exc_info=True)
