"""Мост раздела «О программе» (M9-а v0.2, PRD F13).

Сведения для ИБ-службы и поддержки: версии, дата сборки, документы лицензий и
приватности, каталоги данных, локальная статистика и даты проверок обновлений.

Файлы и папки открываются тем же способом, что и папка моделей
(:meth:`ModelDownloads.open_models_folder`): ``QUrl.fromLocalFile`` по
известному локальному пути, только если он существует. Произвольных адресов
мост не принимает — все пути вычисляются здесь же.

Мост создаётся и живёт в GUI-потоке (урок 025); статистика однопоточная и
тоже трогается только отсюда. Распознанный текст мост не видит: в статистике
его нет по построению (PRD §10).
"""

from __future__ import annotations

import importlib
import logging
import os
import platform
import time
from collections.abc import Callable, Mapping
from datetime import datetime
from importlib import metadata
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from PyQt5.QtCore import PYQT_VERSION_STR, QObject, QUrl, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt5.QtGui import QDesktopServices

from astra_voice.core import paths
from astra_voice.core.version import __version__
from astra_voice.ui.formatting import format_size

if TYPE_CHECKING:
    from astra_voice.core.stats import Summary
    from astra_voice.updates.state import SourceState

log = logging.getLogger(__name__)

#: Где пакет кладёт документы (packaging/debian/rules, DOCDIR).
INSTALL_DOC_DIR = Path("/usr/share/doc/astra-voice")
#: Текст GPL-3 в установленной системе: LICENSE в пакет не кладётся, лицензия
#: та же (debian/copyright ссылается на этот файл).
SYSTEM_GPL3 = Path("/usr/share/common-licenses/GPL-3")

DOC_LICENSE = "license"
DOC_NOTICE = "notice"
DOC_PRIVACY = "privacy"


class StatsPort(Protocol):
    """Часть :class:`~astra_voice.core.stats.Stats`, нужная разделу."""

    def summary(self) -> Summary: ...

    def clear(self) -> None: ...


def doc_paths(resources: Path | None = None) -> dict[str, Path]:
    """Документы программы: в пакете — /usr/share/doc, в исходниках — корень репо."""
    root = paths.resource_root() if resources is None else resources
    if root == paths.INSTALL_SHARE_DIR:
        return {
            DOC_LICENSE: SYSTEM_GPL3,
            DOC_NOTICE: INSTALL_DOC_DIR / "NOTICE",
            DOC_PRIVACY: INSTALL_DOC_DIR / "PRIVACY.md",
        }
    return {
        DOC_LICENSE: root / "LICENSE",
        DOC_NOTICE: root / "NOTICE",
        DOC_PRIVACY: root / "docs" / "PRIVACY.md",
    }


def package_version(name: str) -> str:
    """Версия пакета по метаданным (без импорта самого модуля); нет — пусто."""
    try:
        return metadata.version(name)
    except (metadata.PackageNotFoundError, ValueError, OSError):
        return ""


def build_date() -> str:
    """Дата сборки из сгенерированного ``_version.py`` как «01.09.2026»; нет — пусто."""
    try:
        raw = getattr(importlib.import_module("astra_voice._version"), "__build_date__", "")
    except Exception:  # noqa: BLE001 — файла нет в дереве разработки
        return ""
    try:
        return datetime.strptime(str(raw), "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return ""


def display_path(path: Path, home: Path | None = None) -> str:
    """Путь для показа: домашний каталог сокращается до «~»."""
    base = Path.home() if home is None else home
    try:
        return "~/" + path.relative_to(base).as_posix()
    except ValueError:
        return str(path)


def folder_size(path: Path) -> int:
    """Сумма размеров файлов каталога; ссылки не разворачиваются, ошибки пропускаются."""
    total = 0
    for directory, _dirs, files in os.walk(path, followlinks=False):
        for name in files:
            try:
                info = os.lstat(os.path.join(directory, name))
            except OSError:
                continue
            total += info.st_size
    return total


def plural(count: int, one: str, few: str, many: str) -> str:
    """Русское согласование: 1 диктовка, 2 диктовки, 5 диктовок."""
    tail = count % 100
    if 11 <= tail <= 14:
        return many
    last = count % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _seconds(value_ms: float) -> str:
    return f"{value_ms / 1000:.2f}".replace(".", ",") + " с"


def stats_text(summary: Summary) -> str:
    """«обычно 0,38 с, в худших случаях 0,61 с · 148 диктовок»; диктовок нет — пусто."""
    count = int(summary["dictations"])
    if count <= 0:
        return ""
    parts: list[str] = []
    p50, p95 = summary["p50_ms"], summary["p95_ms"]
    if p50 is not None and p95 is not None:
        parts.append(f"обычно {_seconds(p50)}, в худших случаях {_seconds(p95)}")
    parts.append(f"{count} {plural(count, 'диктовка', 'диктовки', 'диктовок')}")
    return " · ".join(parts)


def moment_text(timestamp: float | None, now: float) -> str:
    """«сегодня в 14:05» / «07.09.2026 в 14:05»; нет даты — пусто."""
    if timestamp is None:
        return ""
    moment = datetime.fromtimestamp(timestamp)
    clock = moment.strftime("%H:%M")
    if moment.date() == datetime.fromtimestamp(now).date():
        return f"сегодня в {clock}"
    return f"{moment.strftime('%d.%m.%Y')} в {clock}"


def _read_update_state() -> SourceState:
    from astra_voice.updates.state import StateStore

    return StateStore().get("app")


class AboutBridge(QObject):
    """Свойства и действия раздела «О программе»; всё — только в GUI-потоке."""

    infoChanged = pyqtSignal()
    statsChanged = pyqtSignal()
    updatesChanged = pyqtSignal()

    def __init__(
        self,
        *,
        stats: StatsPort | None = None,
        update_state: Callable[[], SourceState] | None = _read_update_state,
        open_url: Callable[[QUrl], bool] | None = None,
        documents: Mapping[str, Path] | None = None,
        settings_dir: Path | None = None,
        models_dir: Path | None = None,
        logs_dir: Path | None = None,
        installed: bool | None = None,
        clock: Callable[[], float] = time.time,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._stats = stats
        self._update_state = update_state
        self._open_url = open_url if open_url is not None else QDesktopServices.openUrl
        self._documents = dict(documents) if documents is not None else doc_paths()
        self._settings_dir = settings_dir if settings_dir is not None else paths.config_dir_path()
        self._models_dir = models_dir if models_dir is not None else paths.model_store_dir_path()
        self._logs_dir = logs_dir if logs_dir is not None else paths.log_dir_path()
        self._installed = (
            installed if installed is not None else paths.resource_root() == paths.INSTALL_SHARE_DIR
        )
        self._clock = clock
        self._build_date = build_date()
        self._python = platform.python_version()
        self._onnxruntime = package_version("onnxruntime")
        self._onnx_asr = package_version("onnx-asr")
        self._available: dict[str, bool] = {}
        self._models_size = ""
        self._last_attempt = ""
        self._last_success = ""
        self._stats_count = 0
        self._stats_text = ""
        self.refresh()

    # ── версии ─────────────────────────────────────────────────────────────
    @pyqtProperty(str, constant=True)
    def version(self) -> str:
        return __version__

    @pyqtProperty(str, constant=True)
    def buildDate(self) -> str:  # noqa: N802 — имя свойства для QML
        return self._build_date

    @pyqtProperty(str, constant=True)
    def installKind(self) -> str:  # noqa: N802
        return "deb" if self._installed else "source"

    @pyqtProperty(str, constant=True)
    def pythonVersion(self) -> str:  # noqa: N802
        return self._python

    @pyqtProperty(str, constant=True)
    def pyqtVersion(self) -> str:  # noqa: N802
        return str(PYQT_VERSION_STR)

    @pyqtProperty(str, constant=True)
    def onnxruntimeVersion(self) -> str:  # noqa: N802
        return self._onnxruntime

    @pyqtProperty(str, constant=True)
    def onnxAsrVersion(self) -> str:  # noqa: N802
        return self._onnx_asr

    # ── документы ──────────────────────────────────────────────────────────
    def _document(self, key: str) -> Path | None:
        path = self._documents.get(key)
        return path if path is not None and path.is_file() else None

    @pyqtProperty(bool, notify=infoChanged)
    def licenseAvailable(self) -> bool:  # noqa: N802
        return self._available.get(DOC_LICENSE, False)

    @pyqtProperty(bool, notify=infoChanged)
    def noticeAvailable(self) -> bool:  # noqa: N802
        return self._available.get(DOC_NOTICE, False)

    @pyqtProperty(bool, notify=infoChanged)
    def privacyAvailable(self) -> bool:  # noqa: N802
        return self._available.get(DOC_PRIVACY, False)

    @pyqtSlot()
    def openLicense(self) -> None:  # noqa: N802
        self._open(self._document(DOC_LICENSE))

    @pyqtSlot()
    def openNotice(self) -> None:  # noqa: N802
        self._open(self._document(DOC_NOTICE))

    @pyqtSlot()
    def openPrivacy(self) -> None:  # noqa: N802
        self._open(self._document(DOC_PRIVACY))

    # ── данные на диске ────────────────────────────────────────────────────
    @pyqtProperty(str, constant=True)
    def settingsPath(self) -> str:  # noqa: N802
        return display_path(self._settings_dir)

    @pyqtProperty(str, constant=True)
    def modelsPath(self) -> str:  # noqa: N802
        return display_path(self._models_dir)

    @pyqtProperty(str, constant=True)
    def logsPath(self) -> str:  # noqa: N802
        return display_path(self._logs_dir)

    @pyqtProperty(bool, notify=infoChanged)
    def settingsFolderAvailable(self) -> bool:  # noqa: N802
        return self._available.get("settings", False)

    @pyqtProperty(bool, notify=infoChanged)
    def modelsFolderAvailable(self) -> bool:  # noqa: N802
        return self._available.get("models", False)

    @pyqtProperty(bool, notify=infoChanged)
    def logsFolderAvailable(self) -> bool:  # noqa: N802
        return self._available.get("logs", False)

    @pyqtProperty(str, notify=infoChanged)
    def modelsSize(self) -> str:  # noqa: N802
        return self._models_size

    @pyqtSlot()
    def openSettingsFolder(self) -> None:  # noqa: N802
        self._open(self._settings_dir if self._settings_dir.is_dir() else None)

    @pyqtSlot()
    def openLogsFolder(self) -> None:  # noqa: N802
        self._open(self._logs_dir if self._logs_dir.is_dir() else None)

    # ── проверки обновлений ────────────────────────────────────────────────
    @pyqtProperty(str, notify=updatesChanged)
    def lastAttemptText(self) -> str:  # noqa: N802
        return self._last_attempt

    @pyqtProperty(str, notify=updatesChanged)
    def lastSuccessText(self) -> str:  # noqa: N802
        return self._last_success

    # ── статистика ─────────────────────────────────────────────────────────
    @pyqtProperty(bool, constant=True)
    def statsAvailable(self) -> bool:  # noqa: N802
        return self._stats is not None

    @pyqtProperty(int, notify=statsChanged)
    def statsCount(self) -> int:  # noqa: N802
        return self._stats_count

    @pyqtProperty(str, notify=statsChanged)
    def statsText(self) -> str:  # noqa: N802
        return self._stats_text

    @pyqtSlot()
    def clearStats(self) -> None:  # noqa: N802
        if self._stats is None:
            return
        try:
            self._stats.clear()
        except OSError:
            log.warning("Не удалось очистить статистику")
        self._refresh_stats()

    # ── обновление снимка ──────────────────────────────────────────────────
    @pyqtSlot()
    def refresh(self) -> None:
        """Перечитывает то, что меняется во время работы: папки, статистику, даты."""
        folders = {
            DOC_LICENSE: self._document(DOC_LICENSE) is not None,
            DOC_NOTICE: self._document(DOC_NOTICE) is not None,
            DOC_PRIVACY: self._document(DOC_PRIVACY) is not None,
            "settings": self._settings_dir.is_dir(),
            "models": self._models_dir.is_dir(),
            "logs": self._logs_dir.is_dir(),
        }
        size = ""
        if folders["models"]:
            try:
                size = format_size(folder_size(self._models_dir))
            except OSError:
                log.debug("Не удалось посчитать размер каталога моделей")
        if folders != self._available or size != self._models_size:
            self._available = folders
            self._models_size = size
            self.infoChanged.emit()
        self.refresh_updates()
        self._refresh_stats()

    def refresh_updates(self) -> None:
        """Даты последней попытки и последнего успеха проверки обновлений (У41)."""
        attempt = success = ""
        if self._update_state is not None:
            try:
                state = self._update_state()
            except Exception:  # noqa: BLE001 — раздел должен открыться и без этих дат
                log.debug("Состояние проверок обновлений недоступно")
            else:
                now = self._clock()
                attempt = moment_text(state.last_attempt_at, now)
                success = moment_text(state.last_success_at, now)
        if (attempt, success) != (self._last_attempt, self._last_success):
            self._last_attempt, self._last_success = attempt, success
            self.updatesChanged.emit()

    def _refresh_stats(self) -> None:
        count, text = 0, ""
        if self._stats is not None:
            try:
                summary = self._stats.summary()
            except (OSError, ValueError):
                log.debug("Статистика недоступна")
            else:
                count = int(summary["dictations"])
                text = stats_text(summary)
        if (count, text) != (self._stats_count, self._stats_text):
            self._stats_count, self._stats_text = count, text
            self.statsChanged.emit()

    def _open(self, path: Path | None) -> None:
        if path is None:
            log.debug("Файл или папка для открытия недоступны")
            return
        try:
            self._open_url(QUrl.fromLocalFile(str(path)))
        except Exception:  # noqa: BLE001 — сбой внешней программы не роняет окно
            log.debug("Не удалось открыть файл или папку")
