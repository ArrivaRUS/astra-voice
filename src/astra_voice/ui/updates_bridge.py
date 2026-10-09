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
import math
import os
import re
import stat
import time
from collections.abc import Callable
from typing import TYPE_CHECKING, Protocol

from PyQt5.QtCore import QObject, QThread, pyqtProperty, pyqtSignal, pyqtSlot

from astra_voice.core.version import __version__
from astra_voice.net.github import RELEASE_URL_PREFIX, parse_semver
from astra_voice.ui.formatting import format_moment, format_size
from astra_voice.updates.download import DownloadStatus, FileIdentity, VerifiedDownload
from astra_voice.updates.release import VerifiedRelease

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
_BULLET = re.compile(r"^[-*+]\s+")
_HEADING = re.compile(r"^#{1,6}\s+")


_BUSY_PHASES = {"metadata", "downloading", "verifying", "installing"}
_PHASE_ORDER = {"metadata": 0, "downloading": 1, "verifying": 2}
_ERROR_TEXT = {
    "download-failed": "Не удалось загрузить обновление. Попробуйте ещё раз.",
    "timeout": "Сервер не ответил. Попробуйте ещё раз.",
    "host-unreachable": "Сервер недоступен. Проверьте подключение.",
    "rate-limited": "Сервер ограничил запросы. Повторите позже.",
    "backoff": "Сервер временно недоступен. Повторите позже.",
    "no-space": "Недостаточно места для обновления.",
    "write-failed": "Не удалось сохранить обновление.",
    "signature-invalid": "Подпись обновления не прошла проверку.",
    "hash-mismatch": "Контрольная сумма обновления не совпала.",
    "size-mismatch": "Размер обновления не совпал.",
    "metadata-invalid": "Данные обновления не прошли проверку.",
    "metadata-too-large": "Данные обновления превышают допустимый размер.",
    "release-mismatch": "Данные относятся к другому выпуску.",
    "path-unsafe": "Папка обновления не прошла проверку.",
    "file-changed": "Файл обновления исчез или изменился. Загрузите его заново.",
    "http-status": "Сервер не отдал файл обновления.",
    "short-read": "Загрузка прервалась. Попробуйте ещё раз.",
    "not-allowed": "Загрузка запрещена политикой сети.",
    "offline": "Включён офлайн-режим.",
    "admin": "Обновление запрещено администратором.",
    "policy": "Обновление запрещено политикой сети.",
    "busy": "Дождитесь завершения текущей операции.",
    "closed": "Загрузка сейчас недоступна.",
    "thread-start": "Не удалось начать загрузку.",
    "clock-behind": "Проверьте дату и время на компьютере.",
    "install-refused": "Установка сейчас недоступна. Откройте папку с обновлением.",
    "install-failed": "Не удалось установить обновление. Откройте папку с файлом.",
    "cancelled": "Установка отменена.",
    "appimage-denied": "Запуск обновления запрещён администратором.",
    "unsupported": "Установка этой сборки недоступна. Откройте папку с файлом.",
    "prepare-failed": "Не удалось подготовить перезапуск. Откройте папку с файлом.",
    "shutdown-incomplete": "Не удалось завершить работу для перезапуска.",
    "launch-failed": "Новая версия не запустилась. Запустите Astra Voice из меню.",
    "launch-unconfirmed": "Запуск новой версии не подтверждён. Запустите Astra Voice из меню.",
    "open-failed": "Не удалось открыть папку с обновлением.",
}
_INSTALL_REFUSALS = {"admin", "appimage-denied", "busy", "unsupported", "policy"}


class DownloadActions(Protocol):
    """Контроллер запускает работу и отменяет её без ожидания в GUI."""

    def start(self, release: VerifiedRelease) -> int: ...

    def cancel(self) -> None: ...


class UpdateActions(Protocol):
    """То, что мост вызывает у проверки обновлений (любой поток)."""

    def check_now(self) -> None: ...

    def refresh(self) -> None: ...

    def skip_version(self, version: str) -> None: ...

    def clear_skip(self) -> None: ...

    def remind_later(self) -> None: ...


def display_notes(text: str) -> list[dict[str, object]]:
    """Текст выпуска для панели — строки ``{"text", "bullet"}``.

    Пункт markdown-списка (``-``/``*``/``+`` и пробел) становится ``bullet``: маркер
    рисует QML отдельно, с висячим отступом. Заголовок (``#`` и пробел) — обычная
    строка без решёток; «Что нового» пропускается — это заголовок самой панели.
    «#123 …» заголовком не считается. Пустые строки не нужны, предел — по строкам.
    """
    lines: list[dict[str, object]] = []
    used = 0
    for raw in text.strip().splitlines():
        line = raw.strip()
        if not line:
            continue
        bullet = _BULLET.match(line) is not None
        if bullet:
            line = _BULLET.sub("", line, count=1)
        elif _HEADING.match(line):
            line = _HEADING.sub("", line, count=1).strip()
            if not line or line.casefold() == "что нового":
                continue
        if used + len(line) > MAX_NOTES_DISPLAY_CHARS:
            rest = MAX_NOTES_DISPLAY_CHARS - used
            if rest > 0:
                # Длинную строку обрезаем по остатку лимита, а не выкидываем целиком.
                lines.append({"text": line[:rest].rstrip() + "…", "bullet": bullet})
            else:
                lines.append({"text": "…", "bullet": False})
            break
        used += len(line)
        lines.append({"text": line, "bullet": bullet})
    return lines


def checked_text(timestamp: float | None, now: float) -> str:
    """«Проверено сегодня в 14:02» / «Проверено 07.09.2026 в 14:02»; нет даты — пусто."""
    moment = format_moment(timestamp, now)
    return f"Проверено {moment}" if moment else ""


class UpdatesBridge(QObject):
    """Строка-статус, панель «Что нового» и ручная проверка для QML."""

    statusChanged = pyqtSignal()
    networkChanged = pyqtSignal()
    downloadChanged = pyqtSignal()

    def __init__(
        self,
        checker: UpdateActions | None = None,
        *,
        refusal: Callable[[NetworkKind], str] | None = None,
        controller: DownloadActions | None = None,
        request_install: Callable[[VerifiedDownload], bool] | None = None,
        install_refusal: Callable[[], str] | None = None,
        open_external: Callable[[str], bool] | None = None,
        clock: Callable[[], float] = time.time,
        current_version: str = __version__,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._current_version = current_version
        self._checker = checker
        self._refusal = refusal
        self._open_external = open_external
        self._clock = clock
        self._controller = controller
        self._request_install = request_install
        self._install_refusal = install_refusal
        self._candidate: VerifiedRelease | None = None
        self._operation_release: VerifiedRelease | None = None
        self._operation_notes: list[dict[str, object]] = []
        self._operation_url = ""
        self._prior_version = ""
        self._install_retryable = False
        self._operation_id = 0
        self._phase = "idle"
        self._received = 0
        self._result: VerifiedDownload | None = None
        self._error = ""
        self._retry_at: float | None = None
        self._cancel_requested = False
        self._cancel_reason = ""
        self._starting = False
        self._pending: list[DownloadStatus] = []
        self._state = "disabled"
        self._version = ""
        self._notes: list[dict[str, object]] = []
        self._release_url = ""
        self._snoozed = False
        self._manual = False
        self._checked_text = ""
        self._check_refusal = ""
        self._network_refusal = ""
        self._scheduled_refusal = ""
        self._read_network()

    # -- Python (GUI-поток) -------------------------------------------------

    def set_status(self, status: UpdateStatus) -> None:
        """Новый снимок проверки; вызывать только в GUI-потоке."""
        self._assert_gui()
        candidate = status.verified_release
        if (
            status.state not in ("available", "skipped")
            or candidate is None
            or candidate.version != status.version
            or candidate.raw_tag != status.raw_tag
        ):
            candidate = None
        candidate_changed = candidate != self._candidate
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
        (
            self._state,
            self._version,
            self._notes,
            self._release_url,
            self._snoozed,
            self._manual,
            self._checked_text,
        ) = values
        self._candidate = candidate
        # Qt subscribers may synchronously start a download: publish only after
        # both the trusted candidate and all display fields form one snapshot.
        if candidate_changed:
            self.downloadChanged.emit()
        if values != current or candidate_changed and self._prior_version:
            self.statusChanged.emit()

    def refresh(self) -> None:
        """Сменились тумблер, офлайн или политика: пересчитать доступность и строку."""
        self.refreshCapabilities()
        if self._checker is not None:
            self._checker.refresh()

    def _read_network(self) -> None:
        check = download = scheduled = ""
        if self._refusal is not None:
            check = self._refusal("check_app_manual")
            download = self._refusal("download")
            scheduled = self._refusal("check_app")
        values = (check, download, scheduled)
        if values != (self._check_refusal, self._network_refusal, self._scheduled_refusal):
            self._check_refusal, self._network_refusal, self._scheduled_refusal = values
            self.networkChanged.emit()
            self.statusChanged.emit()
            self.downloadChanged.emit()
        if self._network_refusal and self._phase in ("metadata", "downloading"):
            self._request_cancel(self._network_refusal)

    # -- свойства ------------------------------------------------------------

    @pyqtProperty(str, notify=statusChanged)
    def state(self) -> str:
        return self._state

    @pyqtProperty(str, constant=True)
    def currentVersion(self) -> str:  # noqa: N802
        """Установленная версия программы — для «Установлена версия 0.2.0.»."""
        return self._current_version

    @pyqtProperty(str, notify=statusChanged)
    def version(self) -> str:
        if self._operation_release is not None:
            return self._operation_release.version
        if self._candidate is not None:
            return self._candidate.version
        return self._prior_version or self._version

    @pyqtProperty("QVariantList", notify=statusChanged)
    def notes(self) -> list[dict[str, object]]:
        return (
            self._operation_notes
            if self._operation_release
            else []
            if self._prior_version and self._candidate is None
            else self._notes
        )

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
        return (
            self._open_external is not None
            and bool(self._shown_url())
            and not self._network_refusal
        )

    @pyqtProperty(str, notify=networkChanged)
    def checkRefusal(self) -> str:  # noqa: N802
        return self._check_refusal

    @pyqtProperty(str, notify=networkChanged)
    def networkRefusal(self) -> str:  # noqa: N802
        return self._network_refusal

    @pyqtProperty(str, notify=networkChanged)
    def restState(self) -> str:  # noqa: N802
        """Что строка показывает, когда 3000 мс «Установлена последняя версия» истекли."""
        return "disabled" if self._refusal is None or self._scheduled_refusal else "idle"

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
        if self._checker is None or self.downloadBusy or parse_semver(self.version) is None:
            return
        self._checker.skip_version(self.version)

    @pyqtSlot()
    def clearSkip(self) -> None:  # noqa: N802
        if self._checker is not None:
            self._checker.clear_skip()

    @pyqtSlot()
    def remindLater(self) -> None:  # noqa: N802
        if self._checker is not None and not self.downloadBusy:
            self._checker.remind_later()

    @pyqtSlot()
    def openReleasePage(self) -> None:  # noqa: N802
        self._read_network()
        url = self._shown_url()
        if not self.releasePageAvailable or not url.startswith(RELEASE_URL_PREFIX):
            return
        try:
            if self._open_external is not None and not self._open_external(url):
                log.warning("Не удалось открыть страницу выпуска")
        except Exception:  # noqa: BLE001 — сбой внешней программы не ломает окно
            log.warning("Не удалось открыть страницу выпуска", exc_info=True)

    def _assert_gui(self) -> None:
        if QThread.currentThread() != self.thread():
            raise RuntimeError("UpdatesBridge requires its GUI thread")

    def _shown_release(self) -> VerifiedRelease | None:
        return self._operation_release or self._candidate

    def _shown_url(self) -> str:
        return self._operation_url if self._operation_release else self._release_url

    def _ready_valid(self) -> bool:
        """Cheap identity/provenance check. Runtime must reverify before applying.

        No content reads, signature verification or hashing belong in the GUI.
        Same-UID hostile concurrent mutation is outside this identity snapshot;
        the apply worker is responsible for its own final trusted snapshot.
        """
        result = self._result
        release = self._operation_release
        if result is None or release is None or result.release != release:
            return False
        path = result.path
        name = (
            f"astra-voice_{release.version}_amd64.deb"
            if release.artifact.track == "deb"
            else f"Astra_Voice-{release.version}-x86_64.AppImage"
        )
        if (
            not path.is_absolute()
            or ".." in path.parts
            or path.name != name
            or release.artifact.name != name
        ):
            return False
        try:
            if any(part.is_symlink() for part in (path, *path.parents)):
                return False
            info = path.lstat()
            parent = path.parent.lstat()
            mode = 0o600 if release.artifact.track == "deb" else 0o755
            return (
                stat.S_ISREG(info.st_mode)
                and info.st_uid == os.getuid()
                and info.st_nlink == 1
                and stat.S_IMODE(info.st_mode) == mode
                and info.st_size == release.artifact.size
                and FileIdentity.from_stat(info) == result.identity
                and stat.S_ISDIR(parent.st_mode)
                and parent.st_uid == os.getuid()
                and stat.S_IMODE(parent.st_mode) == 0o700
            )
        except OSError:
            return False

    def _invalidate_ready(self) -> bool:
        if self._result is not None and not self._ready_valid():
            self._result = None
            self._phase = "error"
            self._error = "file-changed"
            self.downloadChanged.emit()
            return True
        return False

    def set_download_status(self, status: DownloadStatus) -> None:
        """GUI-only delivery, queued by the owner; ignore stale operation IDs."""
        self._assert_gui()
        if self._starting:
            self._pending.append(status)
            return
        if status.operation_id != self._operation_id or self._operation_release is None:
            return
        if self._phase not in ("metadata", "downloading", "verifying"):
            return
        phase = "metadata" if status.phase == "checking" else status.phase
        if phase not in {"metadata", "downloading", "verifying", "ready", "error", "cancelled"}:
            return
        if self._cancel_requested:
            if phase not in {"ready", "error", "cancelled"}:
                return
            self._phase = "cancelled"
            self._error = self._cancel_reason
        elif phase in _PHASE_ORDER:
            if _PHASE_ORDER[phase] < _PHASE_ORDER[self._phase]:
                return
            self._phase = phase
            size = self._operation_release.artifact.size
            self._received = max(self._received, min(size, max(0, status.received)))
        elif phase == "ready":
            self._result = status.result
            if not self._ready_valid():
                self._result = None
                self._phase = "error"
                self._error = "file-changed"
            else:
                self._phase = "ready" + self._operation_release.artifact.track
                self._received = self._operation_release.artifact.size
        else:
            self._phase = phase
            self._error = status.error if status.error in _ERROR_TEXT else "download-failed"
            if phase == "cancelled":
                self._error = ""
            retry = status.retry_at
            self._retry_at = (
                retry if retry is not None and math.isfinite(retry) and retry > 0 else None
            )
        self.downloadChanged.emit()

    def set_install_error(self, code: str, *, retryable: bool = False) -> None:
        """GUI-only failed apply completion; a spawned process is not success."""
        self._assert_gui()
        if self._phase != "installing":
            return
        self._phase = "error"
        self._install_retryable = retryable
        self._error = code if code in _ERROR_TEXT else "install-failed"
        self._invalidate_ready()
        self.downloadChanged.emit()

    def restore_install_error(self, version: str, code: str) -> None:
        """Bounded prior-attempt warning; never opens a window or grants trust."""
        self._assert_gui()
        if self.downloadBusy or self._operation_release is not None:
            return
        self._prior_version = version if len(version) <= 64 and parse_semver(version) else ""
        self._phase = "error"
        self._install_retryable = False
        self._error = code if code in _ERROR_TEXT else "install-failed"
        self.statusChanged.emit()
        self.downloadChanged.emit()

    def _request_cancel(self, reason: str = "") -> None:
        if not self.canCancelDownload:
            return
        self._cancel_requested = True
        self._cancel_reason = reason if reason in _ERROR_TEXT else "policy" if reason else ""
        if self._controller is not None:
            self._controller.cancel()
        self.downloadChanged.emit()

    def _start_download(self, release: VerifiedRelease) -> None:
        self._read_network()
        if self._controller is None or self.downloadBusy or self._network_refusal:
            return
        if self._retry_at is not None and self._clock() < self._retry_at:
            return
        # Capture all panel fields before start: test controllers may deliver inline.
        if release != self._operation_release:
            self._operation_notes = list(self._notes)
            self._operation_url = self._release_url
        self._operation_release = release
        self._prior_version = ""
        self._install_retryable = False
        self._phase = "metadata"
        self._received = 0
        self._result = None
        self._error = ""
        self._retry_at = None
        self._cancel_requested = False
        self._cancel_reason = ""
        self._starting = True
        self._pending = []
        try:
            operation_id = self._controller.start(release)
            if operation_id <= self._operation_id:
                raise ValueError("nonmonotonic operation ID")
            self._operation_id = operation_id
        except Exception as error:  # noqa: BLE001 — stable code, no arbitrary exception text
            code = getattr(error, "code", "download-failed")
            self._phase = "error"
            self._error = code if code in _ERROR_TEXT else "download-failed"
        finally:
            self._starting = False
        pending, self._pending = self._pending, []
        self.statusChanged.emit()
        self.downloadChanged.emit()
        for status in pending:
            self.set_download_status(status)

    @pyqtSlot()
    def refreshCapabilities(self) -> None:  # noqa: N802
        """Owner calls on panel opening, policy/activity changes and file refresh."""
        self._assert_gui()
        self._read_network()
        self._invalidate_ready()
        self.downloadChanged.emit()

    @pyqtProperty(str, notify=downloadChanged)
    def downloadPhase(self) -> str:  # noqa: N802
        if self._phase in ("readydeb", "readyappimage") and not self._ready_valid():
            return "error"
        return self._phase

    @pyqtProperty(bool, notify=downloadChanged)
    def downloadBusy(self) -> bool:  # noqa: N802
        return self._phase in _BUSY_PHASES

    @pyqtProperty(bool, notify=downloadChanged)
    def downloadCancelling(self) -> bool:  # noqa: N802
        return self._cancel_requested and self.downloadBusy

    @pyqtProperty(str, notify=downloadChanged)
    def artifactTrack(self) -> str:  # noqa: N802
        release = self._shown_release()
        return release.artifact.track if release else ""

    @pyqtProperty("qlonglong", notify=downloadChanged)
    def artifactSize(self) -> int:  # noqa: N802
        release = self._shown_release()
        return release.artifact.size if release else 0

    @pyqtProperty(str, notify=downloadChanged)
    def artifactSizeText(self) -> str:  # noqa: N802
        return format_size(self.artifactSize, round_up=True) if self._shown_release() else ""

    @pyqtProperty("qlonglong", notify=downloadChanged)
    def downloadReceived(self) -> int:  # noqa: N802
        return self._received

    @pyqtProperty("qlonglong", notify=downloadChanged)
    def downloadTotal(self) -> int:  # noqa: N802
        return int(self.artifactSize)

    @pyqtProperty(int, notify=downloadChanged)
    def downloadPercent(self) -> int:  # noqa: N802
        return int(min(100, 100 * self._received // self.artifactSize)) if self.artifactSize else 0

    @pyqtProperty(str, notify=downloadChanged)
    def downloadError(self) -> str:  # noqa: N802
        if self._result is not None and not self._ready_valid():
            return "file-changed"
        return self._error

    @pyqtProperty(str, notify=downloadChanged)
    def downloadErrorText(self) -> str:  # noqa: N802
        text = _ERROR_TEXT.get(self.downloadError, "")
        if self._prior_version:
            return f"Предыдущая попытка обновления до версии {self._prior_version}: {text}"
        return text

    @pyqtProperty(float, notify=downloadChanged)
    def downloadRetryAt(self) -> float:  # noqa: N802
        return self._retry_at or 0.0

    @pyqtProperty(str, notify=downloadChanged)
    def downloadRetryText(self) -> str:  # noqa: N802
        if self._retry_at is None or self._retry_at <= self._clock():
            return ""
        moment = format_moment(self._retry_at, self._clock())
        return f"Повторить {moment}" if moment else "Повторите позже"

    @pyqtProperty(bool, notify=downloadChanged)
    def canDownload(self) -> bool:  # noqa: N802
        return (
            self._candidate is not None
            and self._controller is not None
            and not self.downloadBusy
            and not self._network_refusal
            and (self._retry_at is None or self._clock() >= self._retry_at)
        )

    @pyqtProperty(bool, notify=downloadChanged)
    def canRetryDownload(self) -> bool:  # noqa: N802
        return (
            self.downloadPhase in ("error", "cancelled")
            and self._operation_release is not None
            and self._controller is not None
            and not self._network_refusal
            and (self._retry_at is None or self._clock() >= self._retry_at)
        )

    @pyqtProperty(bool, notify=downloadChanged)
    def canCancelDownload(self) -> bool:  # noqa: N802
        return self._phase in _PHASE_ORDER and not self._cancel_requested

    @pyqtProperty(bool, notify=downloadChanged)
    def canOpenFolder(self) -> bool:  # noqa: N802
        return not self.downloadBusy and self._open_external is not None and self._ready_valid()

    @pyqtProperty(str, notify=downloadChanged)
    def folderPath(self) -> str:  # noqa: N802
        return str(self._result.path.parent) if self._ready_valid() and self._result else ""

    @pyqtProperty(str, notify=downloadChanged)
    def adminInstruction(self) -> str:  # noqa: N802
        if self.artifactTrack != "deb" or not self._ready_valid() or self._result is None:
            return ""
        return f"sudo apt install ./{self._result.path.name}"

    @pyqtProperty(str, notify=downloadChanged)
    def installRefusal(self) -> str:  # noqa: N802
        if self._request_install is None or self.artifactTrack != "appimage":
            return "unsupported"
        if self.downloadBusy:
            return "busy"
        try:
            code = self._install_refusal() if self._install_refusal else ""
        except Exception:  # noqa: BLE001
            return "unsupported"
        return code if code in _INSTALL_REFUSALS or not code else "unsupported"

    @pyqtProperty(str, notify=downloadChanged)
    def installRefusalText(self) -> str:  # noqa: N802
        return _ERROR_TEXT.get(self.installRefusal, "")

    @pyqtProperty(bool, notify=downloadChanged)
    def canInstallAndRestart(self) -> bool:  # noqa: N802
        return (
            not self.installRefusal
            and self._ready_valid()
            and (
                self._phase == "readyappimage" or self._phase == "error" and self._install_retryable
            )
        )

    @pyqtProperty(bool, notify=downloadChanged)
    def canSkipVersion(self) -> bool:  # noqa: N802
        return (
            self._checker is not None
            and not self.downloadBusy
            and parse_semver(self.version) is not None
        )

    @pyqtProperty(bool, notify=downloadChanged)
    def canRemindLater(self) -> bool:  # noqa: N802
        return self._checker is not None and not self.downloadBusy

    @pyqtSlot()
    def download(self) -> None:
        self._read_network()
        if self.canDownload and self._candidate is not None:
            self._start_download(self._candidate)

    @pyqtSlot()
    def cancelDownload(self) -> None:  # noqa: N802
        self._request_cancel()

    @pyqtSlot()
    def retryDownload(self) -> None:  # noqa: N802
        self._read_network()
        if self.canRetryDownload and self._operation_release is not None:
            self._start_download(self._operation_release)

    @pyqtSlot()
    def openFolder(self) -> None:  # noqa: N802
        if self._invalidate_ready() or not self.canOpenFolder or self._result is None:
            return
        try:
            if self._open_external is not None and self._open_external(
                self._result.path.parent.as_uri()
            ):
                return
        except Exception:  # noqa: BLE001
            pass
        self._error = "open-failed"
        self.downloadChanged.emit()

    @pyqtSlot()
    def installAndRestart(self) -> None:  # noqa: N802
        if self._invalidate_ready() or not self.canInstallAndRestart or self._result is None:
            return
        # Set before invoking: accepted runtime may synchronously deliver an error.
        self._phase = "installing"
        self._error = ""
        self.downloadChanged.emit()
        try:
            accepted = self._request_install(self._result) if self._request_install else False
        except Exception:  # noqa: BLE001
            self.set_install_error("install-failed")
            return
        if not accepted:
            self.set_install_error("install-refused")
