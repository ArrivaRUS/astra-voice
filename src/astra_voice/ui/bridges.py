"""Настройки для QML: немедленное сохранение, откат и живое применение."""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable, Iterable
from typing import Any, Protocol, cast

from PyQt5.QtCore import (
    QEvent,
    QObject,
    Qt,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
)
from PyQt5.QtGui import QDesktopServices
from PyQt5.QtWidgets import QFileDialog

from astra_voice.core.dictation import (
    LEVEL_FAILED,
    TEST_FAILED,
    LevelCallback,
    MicrophoneLevelUpdate,
    MicrophoneTestUpdate,
    TestCallback,
    level_from_dbfs,
)
from astra_voice.core.settings import Settings, is_valid_combo
from astra_voice.core.settings import save as settings_save
from astra_voice.platform.sound import MicrophoneState
from astra_voice.ui import notify
from astra_voice.ui.formatting import (
    clean_display_name,
    format_size,
)
from astra_voice.ui.hotkey_capture import CaptureHost, HotkeyCapture
from astra_voice.ui.model_downloads import (
    ModelDownloads,
    ModelPort,
)
from astra_voice.worker.audio import AudioDevice, AudioError, list_devices

log = logging.getLogger(__name__)

_SMOKE_FAILURE = "Модель не прошла пробное распознавание на этом компьютере"
_BROKEN_MESSAGE = "Модель не прошла проверку. Попробуйте скачать или установить её заново."
_SELFCHECK_MESSAGE = "Распознавание на этом компьютере не работает. Обратитесь к администратору"
_ENGINE_FAILED_MESSAGE = "Не удалось запустить распознавание. Попробуйте переустановить модель"
_REVOKED_MESSAGE = (
    "Издатель больше не рекомендует эту версию модели. "
    "Не устанавливайте её — скачайте свежую версию."
)
_CURRENT_FAILED_MESSAGE = (
    "Модель установлена. Сделать её рабочей не удалось — попробуйте переустановить."
)
_SAFE_MODEL_MESSAGES = frozenset(
    {
        _BROKEN_MESSAGE,
        _SELFCHECK_MESSAGE,
        _ENGINE_FAILED_MESSAGE,
        _REVOKED_MESSAGE,
        _CURRENT_FAILED_MESSAGE,
        "Не удалось установить модель. Попробуйте ещё раз.",
        "Модель нужно переустановить.",
        "В папке есть лишние файлы. Оставьте только файлы выбранной модели.",
        "Файлы в папке не подходят для выбранной модели.",
        "В папке не хватает файлов модели. Получите их заново.",
        "Файлы модели повреждены: контрольная сумма не совпала. Получите их заново.",
        "Не удалось проверить правила администратора: работа без сети",
        "Задано администратором: работа без сети",
        "Включена работа без сети",
    }
)


class SettingsApply(Protocol):
    """Живое применение настроек; захват клавиши проверяется до записи."""

    def pill_enabled(self, value: bool) -> None: ...

    def hotkey(self, combo: str, mode: str) -> str:
        """Пустая строка или ok — успех; иначе код отказа захвата."""
        ...

    def device(self, value: str | None) -> None: ...

    def microphone_state(self) -> MicrophoneState:
        """Состояние выбранного источника записи; микрофон при этом не открывается."""
        ...

    def raise_microphone_volume(self) -> bool: ...

    def open_sound_settings(self) -> bool: ...

    def restart_sound_service(self) -> bool: ...

    @property
    def has_volume_control(self) -> bool: ...

    @property
    def has_sound_settings(self) -> bool: ...

    @property
    def has_sound_service(self) -> bool: ...


_DEFAULT_DEVICE = {"id": "", "name": "Системный по умолчанию"}


class SettingsBridge(QObject):
    """Публикует настройки и ошибки записи; каждый сеттер сразу пишет файл."""

    hotkeyChanged = pyqtSignal()
    hotkeyModeChanged = pyqtSignal()
    pillEnabledChanged = pyqtSignal()
    languageChanged = pyqtSignal()
    checkAppUpdatesChanged = pyqtSignal()
    checkModelUpdatesChanged = pyqtSignal()
    autostartChanged = pyqtSignal()
    deviceChanged = pyqtSignal()
    devicesChanged = pyqtSignal()
    hotkeyStatusChanged = pyqtSignal()
    captureStateChanged = pyqtSignal()
    captureMessageChanged = pyqtSignal()
    pendingComboChanged = pyqtSignal()
    freeCandidatesChanged = pyqtSignal()
    saveErrorChanged = pyqtSignal()
    modelSelfcheckChanged = pyqtSignal()
    microphoneChanged = pyqtSignal()
    extraChanged = pyqtSignal(str)

    activeModelChanged = pyqtSignal()
    activeModelStateChanged = pyqtSignal()
    modelsChanged = pyqtSignal()
    revocationUnknownChanged = pyqtSignal()
    selectionChanged = pyqtSignal()
    freeSpaceTextChanged = pyqtSignal()
    downloadStateChanged = pyqtSignal()
    downloadProgressChanged = pyqtSignal()
    downloadTitleChanged = pyqtSignal()
    downloadSourceChanged = pyqtSignal()
    downloadDetailChanged = pyqtSignal()
    speedChanged = pyqtSignal()
    etaChanged = pyqtSignal()

    _FIELDS = {
        "hotkey": "hotkey",
        "hotkeyMode": "hotkey_mode",
        "pillEnabled": "pill_enabled",
        "language": "language",
        "checkAppUpdates": "check_app_updates",
        "checkModelUpdates": "check_model_updates",
        "autostart": "autostart",
        "device": "device",
    }

    def __init__(
        self,
        settings: Settings,
        *,
        mirror: Settings | None = None,
        downloads: ModelDownloads | None = None,
        locked: Iterable[str] = (),
        apply: SettingsApply | None = None,
        capture_host: CaptureHost | None = None,
        capture: HotkeyCapture | None = None,
        save: Callable[[Settings], None] = settings_save,
        open_url: Callable[[QUrl], bool] | None = None,
        dialog_factory: Callable[[], str] = QFileDialog.getExistingDirectory,
        device_provider: Callable[[], list[AudioDevice]] = list_devices,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._mirror = mirror
        # Список микрофонов читается по запросу экрана (refreshDevices), а не при
        # старте: звуковая служба может подняться позже программы, а устройства —
        # подключаться позже (2026-09-22: в «Общих» нельзя было выбрать микрофон).
        self._device_provider = device_provider
        self._devices: list[dict[str, str]] = [_DEFAULT_DEVICE.copy()]
        self._locked = frozenset(locked)
        self._apply = apply
        self._capture = capture if capture is not None else HotkeyCapture(capture_host, self)
        for name in ("captureState", "captureMessage", "pendingCombo", "freeCandidates"):
            getattr(self._capture, name + "Changed").connect(getattr(self, name + "Changed"))
        self._save = save
        self._values = self._read_values()
        self._hotkey_status = "ok"
        self._save_error = ""
        self._model_selfcheck = "idle"
        self._microphone = MicrophoneState()
        self._downloads = downloads
        self._open_url = open_url if open_url is not None else QDesktopServices.openUrl
        self._dialog_factory = dialog_factory
        if downloads is not None:
            for name in (
                "downloadState",
                "downloadProgress",
                "downloadTitle",
                "downloadSource",
                "speed",
                "eta",
            ):
                getattr(downloads, name + "Changed").connect(getattr(self, name + "Changed"))
            downloads.downloadDetailChanged.connect(self.downloadDetailChanged)
            downloads.modelsChanged.connect(self.modelsChanged)
            downloads.revocationUnknownChanged.connect(self.revocationUnknownChanged)
            downloads.selectionChanged.connect(self.selectionChanged)
            downloads.freeSpaceTextChanged.connect(self.freeSpaceTextChanged)
            downloads.modelsChanged.connect(self.activeModelChanged)
            downloads.modelsChanged.connect(self.activeModelStateChanged)
            downloads.modelReadyChanged.connect(self.activeModelChanged)
            downloads.modelReadyChanged.connect(self.activeModelStateChanged)
            downloads.downloadStateChanged.connect(self.activeModelStateChanged)

    @property
    def capture(self) -> HotkeyCapture:
        return self._capture

    @pyqtProperty(str, notify=activeModelChanged)
    def activeModelName(self) -> str:  # noqa: N802
        entry = self._downloads.active_entry() if self._downloads is not None else None
        if entry is not None and getattr(entry, "removed_from_catalog", False):
            return "Установленная модель"
        return (
            clean_display_name(str(getattr(entry, "name", entry.id))) if entry is not None else ""
        )

    @pyqtProperty(str, notify=activeModelChanged)
    def activeModelSize(self) -> str:  # noqa: N802
        entry = self._downloads.active_entry() if self._downloads is not None else None
        return format_size(entry.size_bytes) if entry is not None else ""

    @pyqtProperty(str, notify=activeModelStateChanged)
    def activeModelState(self) -> str:  # noqa: N802
        return self._downloads.active_state() if self._downloads is not None else "none"

    @pyqtProperty(str, notify=activeModelStateChanged)
    def activeModelMessage(self) -> str:  # noqa: N802
        return {
            "broken": "Файлы модели повреждены. Переустановите модель",
            "failed": "Не удалось загрузить модель",
            "no-space": "Не хватает места на диске",
            "paused-no-space": "Не хватает места на диске",
            "catalog-unavailable": "Не удалось проверить список моделей",
        }.get(self.activeModelState, "")

    @pyqtProperty(bool, notify=activeModelStateChanged)
    def canInstall(self) -> bool:  # noqa: N802
        return self._downloads is not None and self._downloads.can_install()

    # ── раздел «Модели»: те же данные очереди, что видит мастер (§3.6) ──────
    @pyqtProperty("QVariantList", notify=modelsChanged)
    def models(self) -> list[dict[str, Any]]:
        return self._downloads.models if self._downloads is not None else []

    @pyqtProperty(bool, notify=revocationUnknownChanged)
    def revocationUnknown(self) -> bool:  # noqa: N802
        return self._downloads.revocationUnknown if self._downloads is not None else False

    @pyqtProperty(str, notify=selectionChanged)
    def selectionSummary(self) -> str:  # noqa: N802
        return self._downloads.selectionSummary if self._downloads is not None else ""

    @pyqtProperty(bool, notify=selectionChanged)
    def selectionFits(self) -> bool:  # noqa: N802
        return self._downloads.selectionFits if self._downloads is not None else True

    @pyqtProperty(str, notify=selectionChanged)
    def selectionMessage(self) -> str:  # noqa: N802
        return self._downloads.selectionMessage if self._downloads is not None else ""

    @pyqtProperty(str, notify=freeSpaceTextChanged)
    def freeSpaceText(self) -> str:  # noqa: N802
        return self._downloads.freeSpaceText if self._downloads is not None else ""

    @pyqtProperty(str, notify=modelsChanged)
    def installedSummary(self) -> str:  # noqa: N802
        return self._downloads.installedSummary if self._downloads is not None else ""

    @pyqtProperty(int, notify=modelsChanged)
    def installedCount(self) -> int:  # noqa: N802
        return self._downloads.installedCount if self._downloads is not None else 0

    @pyqtSlot(str)
    def makeModelCurrent(self, model_id: str) -> None:  # noqa: N802
        if self._downloads is not None:
            self._downloads.makeModelCurrent(model_id)

    @pyqtSlot(str)
    def switchModelWithPause(self, model_id: str) -> None:  # noqa: N802
        if self._downloads is not None:
            self._downloads.switchModelWithPause(model_id)

    @pyqtSlot(str)
    def removeModel(self, model_id: str) -> None:  # noqa: N802
        if self._downloads is not None:
            self._downloads.removeModel(model_id)

    @pyqtSlot(str)
    def reinstallModel(self, model_id: str) -> None:  # noqa: N802
        if self._downloads is not None:
            self._downloads.reinstallModel(model_id)

    @pyqtSlot(str)
    def updateModel(self, model_id: str) -> None:  # noqa: N802
        if self._downloads is not None:
            self._downloads.updateModel(model_id)

    @pyqtSlot(str)
    def toggleModel(self, model_id: str) -> None:  # noqa: N802
        if self._downloads is not None:
            self._downloads.toggleModel(model_id)

    @pyqtSlot()
    def startSelectedDownloads(self) -> None:  # noqa: N802
        if self._downloads is not None:
            self._downloads.startSelectedDownloads()

    @pyqtSlot(str)
    def retryModel(self, model_id: str) -> None:  # noqa: N802
        if self._downloads is not None:
            self._downloads.retryModel(model_id)

    @pyqtSlot(str)
    def cancelModel(self, model_id: str) -> None:  # noqa: N802
        if self._downloads is not None:
            self._downloads.cancelModel(model_id)

    @pyqtSlot(str)
    def dequeueModel(self, model_id: str) -> None:  # noqa: N802
        if self._downloads is not None:
            self._downloads.dequeueModel(model_id)

    @pyqtSlot()
    def pickInstallPath(self) -> None:  # noqa: N802
        if self._downloads is None:
            log.debug("Хранилище моделей недоступно для установки из папки")
            return
        path = self._dialog_factory()
        if path:
            self._downloads.installFromPath(path)

    @pyqtSlot()
    def installRecommendedModel(self) -> None:  # noqa: N802
        if self._downloads is not None:
            self._downloads.install_recommended()

    @pyqtSlot()
    def openModelsFolder(self) -> None:  # noqa: N802
        if self._downloads is None:
            log.debug("Хранилище моделей недоступно для открытия")
            return
        self._downloads.open_models_folder(self._open_url)

    @pyqtProperty(bool, notify=activeModelStateChanged)
    def canReinstall(self) -> bool:  # noqa: N802
        return self._downloads is not None and self._downloads.can_reinstall()

    @pyqtSlot()
    def reinstallActiveModel(self) -> None:  # noqa: N802
        if self._downloads is not None and self.canReinstall:
            self._downloads.reinstall_active()

    @pyqtSlot()
    def cancelDownloads(self) -> None:  # noqa: N802
        if self._downloads is not None:
            self._downloads.cancelDownloads()

    @pyqtProperty(str, notify=downloadStateChanged)
    def downloadState(self) -> str:  # noqa: N802
        return self._downloads.downloadState if self._downloads is not None else ""

    @pyqtProperty(float, notify=downloadProgressChanged)
    def downloadProgress(self) -> float:  # noqa: N802
        return self._downloads.downloadProgress if self._downloads is not None else 0.0

    @pyqtProperty(str, notify=downloadTitleChanged)
    def downloadTitle(self) -> str:  # noqa: N802
        return self._downloads.downloadTitle if self._downloads is not None else ""

    @pyqtProperty(str, notify=downloadSourceChanged)
    def downloadSource(self) -> str:  # noqa: N802
        return self._downloads.downloadSource if self._downloads is not None else ""

    @pyqtProperty(str, notify=downloadDetailChanged)
    def downloadDetail(self) -> str:  # noqa: N802
        return self._downloads.downloadDetail if self._downloads is not None else ""

    @pyqtProperty(str, notify=speedChanged)
    def speed(self) -> str:  # noqa: N802
        return self._downloads.speed if self._downloads is not None else ""

    @pyqtProperty(str, notify=etaChanged)
    def eta(self) -> str:  # noqa: N802
        return self._downloads.eta if self._downloads is not None else ""

    def _read_values(self) -> dict[str, str | bool]:
        settings = self._mirror if self._mirror is not None else self._settings
        return {
            name: (settings.extra.get("device") or "")
            if name == "device"
            else getattr(settings, field)
            for name, field in self._FIELDS.items()
        }

    def set_extra(self, key: str, value: object) -> bool:
        """Сохраняет дополнительную настройку; зеркало меняется только после записи."""
        present = key in self._settings.extra
        old = self._settings.extra.get(key)
        self._settings.extra[key] = value
        try:
            self._save(self._settings)
        except OSError:
            if present:
                self._settings.extra[key] = old
            else:
                self._settings.extra.pop(key, None)
            self._save_error = "Не удалось сохранить настройки"
            log.warning("Не удалось сохранить настройки")
            self.saveErrorChanged.emit()
            return False
        if self._mirror is not None:
            self._mirror.extra[key] = value
        if self._save_error:
            self._save_error = ""
            self.saveErrorChanged.emit()
        self.extraChanged.emit(key)
        return True

    def _set_value(self, name: str, value: str | bool) -> None:
        field = self._FIELDS[name]
        if self.is_locked(field):
            log.info("%s: настройка задана администратором", field)
            getattr(self, name + "Changed").emit()
            return
        if self._values[name] == value:
            return
        if (
            (name == "hotkeyMode" and value not in ("ptt", "toggle"))
            or (name == "language" and value not in ("ru", "en"))
            or (name == "hotkey" and not is_valid_combo(cast(str, value)))
        ):
            log.warning("Недопустимое значение настройки %s", name)
            if name == "hotkey":
                self.hotkeyChanged.emit()
            return
        if name == "hotkeyMode" and not is_valid_combo(self.hotkey):
            log.warning("hotkey=%r недопустим, смена режима отменена", self.hotkey)
            self.hotkeyModeChanged.emit()
            return
        device_present = "device" in self._settings.extra
        old = (
            self._settings.extra.get("device")
            if name == "device"
            else getattr(self._settings, field)
        )
        hotkey_apply = self._apply if name in ("hotkey", "hotkeyMode") else None
        if hotkey_apply is not None:
            combo = cast(str, value) if name == "hotkey" else self.hotkey
            mode = cast(str, value) if name == "hotkeyMode" else self.hotkeyMode
            code = hotkey_apply.hotkey(combo, mode)
            if code not in ("", "ok") and not (
                name == "hotkey" and self._capture.keep_busy and code == "busy"
            ):
                hotkey_apply.hotkey(self.hotkey, self.hotkeyMode)
                # apply_hotkey может менять тот же Settings, что сохраняет мост.
                setattr(self._settings, field, old)
                self.set_hotkey_status(code)
                getattr(self, name + "Changed").emit()
                return
        if name == "device":
            self._settings.extra["device"] = value or None
        else:
            setattr(self._settings, field, value)
        try:
            self._save(self._settings)
        except OSError:
            if hotkey_apply is not None:
                self.set_hotkey_status(hotkey_apply.hotkey(self.hotkey, self.hotkeyMode) or "ok")
            if name == "device":
                if device_present:
                    self._settings.extra["device"] = old
                else:
                    self._settings.extra.pop("device", None)
            else:
                setattr(self._settings, field, old)
            self._save_error = "Не удалось сохранить настройки"
            log.warning("Не удалось сохранить настройки")
            getattr(self, name + "Changed").emit()
            self.saveErrorChanged.emit()
            return
        # Остальные настройки применяются только после успешной записи.
        if self._mirror is not None:
            if name == "device":
                self._mirror.extra["device"] = value or None
            else:
                setattr(self._mirror, field, value)
        self._values[name] = value
        if self._save_error:
            self._save_error = ""
            self.saveErrorChanged.emit()
        if self._apply is not None:
            if name == "pillEnabled":
                self._apply.pill_enabled(self.pillEnabled)
            elif hotkey_apply is not None:
                self.set_hotkey_status(
                    "busy"
                    if name == "hotkey" and self._capture.keep_busy and code == "busy"
                    else "ok"
                )
            elif name == "device":
                self._apply.device(self.device or None)
                self.refreshMicrophone()
        getattr(self, name + "Changed").emit()

    @pyqtProperty("QStringList", constant=True)
    def lockedSettings(self) -> list[str]:  # noqa: N802 — имя свойства для QML
        return sorted(self._locked)

    @pyqtSlot(str, result=bool)
    def is_locked(self, name: str) -> bool:
        return name in self._locked

    # Qt виден mypy как Any: декоратор setter не распознаётся как часть свойства.
    @pyqtProperty(str, notify=hotkeyChanged)
    def hotkey(self) -> str:
        return cast(str, self._values["hotkey"])

    @hotkey.setter  # type: ignore[no-redef]
    def hotkey(self, value: str) -> None:
        self._set_value("hotkey", value)

    @pyqtProperty(str, notify=captureStateChanged)
    def captureState(self) -> str:  # noqa: N802
        return self._capture.state

    @pyqtProperty(str, notify=captureMessageChanged)
    def captureMessage(self) -> str:  # noqa: N802
        return self._capture.message

    @pyqtProperty(str, notify=pendingComboChanged)
    def pendingCombo(self) -> str:  # noqa: N802
        return self._capture.pending_combo

    @pyqtProperty("QStringList", notify=freeCandidatesChanged)
    def freeCandidates(self) -> list[str]:  # noqa: N802
        return list(self._capture.free_candidates)

    def save_capture_combo(self, combo: str, keep: bool) -> str:
        """Применяет и сохраняет комбинацию общим путём для обоих экранов."""
        if self._apply is None:
            return "not-grabbed"
        # «Оставить» учитывается в _set_value через общий HotkeyCapture.keep_busy.
        self._set_value("hotkey", combo)
        if self.saveError:
            return "not-grabbed"
        if self.hotkey != combo:
            return self.hotkeyStatus if self.hotkeyStatus != "ok" else "not-grabbed"
        return "ok"

    @pyqtSlot()
    def beginCapture(self) -> None:  # noqa: N802
        self._capture.begin(self.save_capture_combo)

    @pyqtSlot(str)
    def endCapture(self, combo: str) -> None:  # noqa: N802
        self._capture.end(combo, self.save_capture_combo)

    @pyqtSlot()
    def cancelCapture(self) -> None:  # noqa: N802
        self._capture.cancel()

    @pyqtSlot()
    def keepCombo(self) -> None:  # noqa: N802
        self._capture.keep()

    @pyqtSlot()
    def refreshCandidates(self) -> None:  # noqa: N802
        self._capture.refresh_candidates()

    @pyqtProperty(str, notify=hotkeyModeChanged)
    def hotkeyMode(self) -> str:  # noqa: N802 — имя свойства для QML
        return cast(str, self._values["hotkeyMode"])

    @hotkeyMode.setter  # type: ignore[no-redef]
    def hotkeyMode(self, value: str) -> None:  # noqa: N802
        self._set_value("hotkeyMode", value)

    @pyqtProperty(bool, notify=pillEnabledChanged)
    def pillEnabled(self) -> bool:  # noqa: N802
        return cast(bool, self._values["pillEnabled"])

    @pillEnabled.setter  # type: ignore[no-redef]
    def pillEnabled(self, value: bool) -> None:  # noqa: N802
        self._set_value("pillEnabled", value)

    @pyqtProperty(str, notify=languageChanged)
    def language(self) -> str:
        return cast(str, self._values["language"])

    @language.setter  # type: ignore[no-redef]
    def language(self, value: str) -> None:
        self._set_value("language", value)

    @pyqtProperty(bool, notify=checkAppUpdatesChanged)
    def checkAppUpdates(self) -> bool:  # noqa: N802
        return cast(bool, self._values["checkAppUpdates"])

    @checkAppUpdates.setter  # type: ignore[no-redef]
    def checkAppUpdates(self, value: bool) -> None:  # noqa: N802
        self._set_value("checkAppUpdates", value)

    @pyqtProperty(bool, notify=checkModelUpdatesChanged)
    def checkModelUpdates(self) -> bool:  # noqa: N802
        return cast(bool, self._values["checkModelUpdates"])

    @checkModelUpdates.setter  # type: ignore[no-redef]
    def checkModelUpdates(self, value: bool) -> None:  # noqa: N802
        self._set_value("checkModelUpdates", value)

    @pyqtProperty(bool, notify=autostartChanged)
    def autostart(self) -> bool:
        return cast(bool, self._values["autostart"])

    @autostart.setter  # type: ignore[no-redef]
    def autostart(self, value: bool) -> None:
        self._set_value("autostart", value)

    @pyqtProperty(str, notify=deviceChanged)
    def device(self) -> str:
        return cast(str, self._values["device"])

    @device.setter  # type: ignore[no-redef]
    def device(self, value: str) -> None:
        self._set_value("device", value)

    @pyqtProperty("QVariantList", notify=devicesChanged)
    def devices(self) -> list[dict[str, str]]:
        """Микрофоны для выбора: первый — системный по умолчанию, далее из списка службы."""
        return [dict(device) for device in self._devices]

    @pyqtSlot()
    def refreshDevices(self) -> None:  # noqa: N802
        """Перечитывает список микрофонов (только список, микрофон не открывается)."""
        devices = [_DEFAULT_DEVICE.copy()]
        try:
            devices.extend(
                {"id": device.name, "name": device.label} for device in self._device_provider()
            )
        except (AudioError, OSError):
            log.warning("Не удалось получить список микрофонов. Доступен системный по умолчанию.")
        # Число, не имена: имена устройств в журнал не пишем.
        log.info("Список микрофонов: найдено %d", len(devices) - 1)
        if devices != self._devices:
            self._devices = devices
            self.devicesChanged.emit()

    @pyqtProperty(int, notify=microphoneChanged)
    def microphoneVolume(self) -> int:  # noqa: N802
        """Системная громкость выбранного микрофона в процентах; −1 — неизвестна."""
        return self._microphone.percent if self._microphone.known else -1

    @pyqtProperty(bool, notify=microphoneChanged)
    def microphoneMuted(self) -> bool:  # noqa: N802
        return self._microphone.known and self._microphone.muted

    @pyqtProperty(bool, notify=microphoneChanged)
    def canRaiseMicrophone(self) -> bool:  # noqa: N802
        """Есть чем и куда поднимать громкость; иначе строку в «Общих» не показываем."""
        return self._apply is not None and self._apply.has_volume_control

    @pyqtProperty(bool, notify=microphoneChanged)
    def canOpenSoundSettings(self) -> bool:  # noqa: N802
        return self._apply is not None and self._apply.has_sound_settings

    @pyqtProperty(bool, notify=microphoneChanged)
    def canRestartSoundService(self) -> bool:  # noqa: N802
        return self._apply is not None and self._apply.has_sound_service

    @pyqtSlot()
    def refreshMicrophone(self) -> None:  # noqa: N802
        """Перечитывает состояние источника: при открытии раздела и смене микрофона."""
        state = MicrophoneState()
        if self._apply is not None and self._apply.has_volume_control:
            try:
                state = self._apply.microphone_state()
            except Exception:
                log.warning("Не удалось узнать громкость микрофона")
        if state != self._microphone:
            self._microphone = state
            self.microphoneChanged.emit()

    @pyqtSlot()
    def raiseMicrophoneVolume(self) -> None:  # noqa: N802
        """Нажатие «Поднять»: звук включается, громкость выставляется на 100 %."""
        if self._apply is not None:
            try:
                self._apply.raise_microphone_volume()
            except Exception:
                log.warning("Не удалось поднять громкость микрофона")
        self.refreshMicrophone()

    @pyqtSlot()
    def openSoundSettings(self) -> None:  # noqa: N802
        """Нажатие «Открыть настройки звука»: системная панель, ничего не меняем."""
        if self._apply is not None:
            try:
                self._apply.open_sound_settings()
            except Exception:
                log.warning("Не удалось открыть системные настройки звука")

    @pyqtSlot()
    def restartSoundService(self) -> None:  # noqa: N802
        """Нажатие в «Отладке»: перезапуск звуковой службы сеанса."""
        if self._apply is not None:
            try:
                self._apply.restart_sound_service()
            except Exception:
                log.warning("Не удалось перезапустить звуковую службу")
        self.refreshMicrophone()

    @pyqtProperty(str, notify=hotkeyStatusChanged)
    def hotkeyStatus(self) -> str:  # noqa: N802
        return self._hotkey_status

    @pyqtProperty(str, notify=saveErrorChanged)
    def saveError(self) -> str:  # noqa: N802
        return self._save_error

    @pyqtProperty(str, notify=modelSelfcheckChanged)
    def modelSelfcheck(self) -> str:  # noqa: N802
        return self._model_selfcheck

    def set_hotkey_status(self, code: str) -> None:
        if self._hotkey_status != code:
            self._hotkey_status = code
            self.hotkeyStatusChanged.emit()

    def set_model_selfcheck(self, state: str) -> None:
        if self._model_selfcheck != state:
            self._model_selfcheck = state
            self.modelSelfcheckChanged.emit()

    @pyqtSlot()
    def retryHotkey(self) -> None:  # noqa: N802
        """Внутренний повтор для рантайма и тестов.

        Кнопки «Повторить» в интерфейсе нет (решение заказчика 15.09.2026).
        """
        if self._apply is not None:
            self.set_hotkey_status(self._apply.hotkey(self.hotkey, self.hotkeyMode))

    def reload(self) -> None:
        """Перечитывает Settings, уведомляя только об изменённых свойствах."""
        values = self._read_values()
        old, self._values = self._values, values
        for name, value in values.items():
            if value != old[name]:
                getattr(self, name + "Changed").emit()


class OnboardingHost(CaptureHost, Protocol):
    """Действия приложения, доступные мастеру первого запуска."""

    def begin_capture(self) -> bool: ...

    def end_capture(self) -> None: ...

    def probe(self, combo: str) -> str: ...

    def free_candidates(self, prefer: list[str]) -> list[str]: ...

    def start_level_monitor(self, device: str, callback: LevelCallback) -> bool: ...

    def stop_level_monitor(self) -> None: ...

    def start_test(self, device: str, callback: TestCallback) -> bool: ...

    def subscribe_device_resolved(self, callback: Callable[[str], None] | None) -> str: ...

    def stop_test(self) -> None: ...

    def cancel_test(self) -> None: ...

    def reload_model(self) -> None: ...

    def notify_ready(self, combo: str) -> None: ...

    def hide_window(self) -> None: ...


class OnboardingController(QObject):
    """Пять шагов онбординга; все записи проходят через SettingsBridge."""

    stepChanged = pyqtSignal()
    canFinishChanged = pyqtSignal()
    doneChanged = pyqtSignal()
    languageChanged = pyqtSignal()
    checkAppUpdatesChanged = pyqtSignal()
    checkModelUpdatesChanged = pyqtSignal()
    policyLockedChanged = pyqtSignal()
    hotkeyChanged = pyqtSignal()
    hotkeyModeChanged = pyqtSignal()
    captureStateChanged = pyqtSignal()
    captureMessageChanged = pyqtSignal()
    freeCandidatesChanged = pyqtSignal()
    pendingComboChanged = pyqtSignal()
    modelsChanged = pyqtSignal()
    selectionChanged = pyqtSignal()
    freeSpaceTextChanged = pyqtSignal()
    downloadStateChanged = pyqtSignal()
    downloadProgressChanged = pyqtSignal()
    downloadTitleChanged = pyqtSignal()
    downloadSourceChanged = pyqtSignal()
    downloadDetailChanged = pyqtSignal()
    modelReadyChanged = pyqtSignal()
    modelStateChanged = pyqtSignal()
    modelNameChanged = pyqtSignal()
    modelHostChanged = pyqtSignal()
    modelSizeBytesChanged = pyqtSignal()
    modelSizeChanged = pyqtSignal()
    modelRamChanged = pyqtSignal()
    progressChanged = pyqtSignal()
    speedChanged = pyqtSignal()
    etaChanged = pyqtSignal()
    modelMessageChanged = pyqtSignal()
    devicesChanged = pyqtSignal()
    deviceChanged = pyqtSignal()
    deviceResolvedChanged = pyqtSignal()
    levelStateChanged = pyqtSignal()
    levelMessageChanged = pyqtSignal()
    levelChanged = pyqtSignal()
    peakChanged = pyqtSignal()
    testPhraseChanged = pyqtSignal()
    testStateChanged = pyqtSignal()
    testTextChanged = pyqtSignal()
    testDurationChanged = pyqtSignal()
    testMessageChanged = pyqtSignal()

    def __init__(
        self,
        bridge: SettingsBridge,
        *,
        settings: Settings,
        host: OnboardingHost | None = None,
        capture: HotkeyCapture | None = None,
        model: ModelPort | None = None,
        downloads: ModelDownloads | None = None,
        status_sink: Callable[[str], None] | None = None,
        dialog_factory: Callable[[], str] = QFileDialog.getExistingDirectory,
        open_url: Callable[[QUrl], bool] | None = None,
        device_provider: Callable[[], list[AudioDevice]] = list_devices,
        clock: Callable[[], float] = time.monotonic,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._settings = settings
        self._host = host
        self._resolved_device = ""
        self._dialog_factory = dialog_factory
        self._open_url = open_url if open_url is not None else QDesktopServices.openUrl
        self._devices = [_DEFAULT_DEVICE.copy()]
        try:
            self._devices.extend(
                {"id": device.name, "name": device.label} for device in device_provider()
            )
        except (AudioError, OSError):
            log.warning("Не удалось получить список микрофонов. Доступен системный по умолчанию.")
        self._owns_downloads = downloads is None
        self._downloads = downloads if downloads is not None else ModelDownloads(model, clock=clock)
        self._shutting_down = False
        self._window: QObject | None = None
        self._window_visible: bool | None = None
        self._status_sink = status_sink
        for name in (
            "modelsChanged",
            "selectionChanged",
            "freeSpaceTextChanged",
            "downloadStateChanged",
            "downloadProgressChanged",
            "downloadTitleChanged",
            "downloadSourceChanged",
            "downloadDetailChanged",
            "modelReadyChanged",
            "modelStateChanged",
            "modelNameChanged",
            "modelHostChanged",
            "modelSizeBytesChanged",
            "modelSizeChanged",
            "modelRamChanged",
            "progressChanged",
            "speedChanged",
            "etaChanged",
            "modelMessageChanged",
        ):
            getattr(self._downloads, name).connect(getattr(self, name))
        self._downloads.modelReadyChanged.connect(self.canFinishChanged)
        self._downloads.queueFinished.connect(self._queue_finished)
        self.downloadTitleChanged.connect(self._publish_download_status)
        self.downloadProgressChanged.connect(self._publish_download_status)
        self.downloadStateChanged.connect(self._publish_download_status)
        self._publish_download_status()
        self._level_state = "idle"
        self._level_message = ""
        self._level_epoch = 0
        self._level_running = False
        self._level_stopping = False
        self._test_after_level = False
        self._level = 0.0
        self._peak = ""
        self._test_state = "idle"
        self._test_text = ""
        self._test_duration = ""
        self._test_message = ""
        self._test_epoch = 0
        step = settings.extra.get("onboarding_step")
        self._step = step if type(step) is int and 1 <= step <= 5 else 1
        self._capture = capture if capture is not None else bridge.capture
        # События окна принимает один фильтр; мастер очищает только свою пробу.
        self._capture.windowEvent.connect(self.eventFilter, Qt.DirectConnection)
        if host is not None:
            self._capture.host = host
        for name in ("captureState", "captureMessage", "pendingCombo", "freeCandidates"):
            getattr(self._capture, name + "Changed").connect(getattr(self, name + "Changed"))
        for name in (
            "language",
            "checkAppUpdates",
            "checkModelUpdates",
            "hotkey",
            "hotkeyMode",
            "device",
        ):
            getattr(bridge, name + "Changed").connect(getattr(self, name + "Changed").emit)
        bridge.extraChanged.connect(self._extra_changed)
        self.deviceChanged.connect(self._clear_test)
        if host is not None:
            try:
                self._resolved_device = host.subscribe_device_resolved(self._device_resolved)
            except Exception:
                log.warning("Не удалось подписаться на имя открытого микрофона", exc_info=True)
        if "onboarding_language_set" not in settings.extra:
            self.setProperty(
                "language", "ru" if os.environ.get("LANG", "").startswith("ru") else "en"
            )

    def _extra_changed(self, key: str) -> None:
        if key == "onboarding_model_ready":
            self.canFinishChanged.emit()
            self.modelReadyChanged.emit()
            self.selectionChanged.emit()
        elif key == "onboarding_done":
            self.doneChanged.emit()

    def _queue_finished(self, installed: bool) -> None:
        if installed and self._window_visible is not True and not self._shutting_down:
            try:
                notify.notify_model_installed()
            except Exception:
                log.warning("Не удалось показать уведомление об установке модели")

    def _publish_download_status(self) -> None:
        if self._status_sink is None:
            return
        try:
            self._status_sink(self._downloads.status_text())
        except Exception:
            log.warning("Не удалось обновить состояние загрузки в трее")

    def attach_window(self, window: QObject) -> None:
        """Подключает корневое окно и читает видимость до первого события Show."""
        self._window = window
        self._capture.attach_window(window)
        visible = getattr(window, "isVisible", None)
        self._window_visible = bool(visible()) if callable(visible) else None

    @staticmethod
    def _window_minimized(window: QObject) -> bool:
        state = getattr(window, "windowState", None)
        return callable(state) and bool(state() & Qt.WindowMinimized)

    def _window_allows_level(self) -> bool:
        if self._window is None:
            return self._window_visible is not False
        visible = getattr(self._window, "isVisible", None)
        shown = bool(visible()) if callable(visible) else self._window_visible is not False
        return shown and not self._window_minimized(self._window)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() == QEvent.Show:
            self._window_visible = True
        minimized = event.type() == QEvent.WindowStateChange and self._window_minimized(obj)
        if event.type() == QEvent.WindowStateChange and not minimized:
            visible = getattr(obj, "isVisible", None)
            if callable(visible):
                self._window_visible = bool(visible())
        if event.type() in (QEvent.Hide, QEvent.Close) or minimized:
            self._window_visible = False
            self._clear_test()
        return bool(super().eventFilter(obj, event))

    @pyqtProperty(int, notify=stepChanged)
    def step(self) -> int:
        return self._step

    @pyqtProperty(int, constant=True)
    def totalSteps(self) -> int:  # noqa: N802
        return 5

    def _go(self, step: int) -> None:
        if step == self._step or not 1 <= step <= 5:
            return
        if not self._bridge.set_extra("onboarding_step", step):
            return
        if self.captureState == "capturing":
            self.cancelCapture()
        self._clear_test()
        self._step = step
        self.stepChanged.emit()

    @pyqtSlot()
    def next(self) -> None:
        self._go(self._step + 1)

    @pyqtSlot()
    def back(self) -> None:
        self._go(self._step - 1)

    @pyqtSlot()
    def skip(self) -> None:
        self.next()

    @pyqtProperty(bool, notify=canFinishChanged)
    def canFinish(self) -> bool:  # noqa: N802
        return bool(self.modelReady)

    @pyqtProperty(bool, notify=modelReadyChanged)
    def modelReady(self) -> bool:  # noqa: N802
        if self._downloads.model is not None:
            return bool(self._downloads.model.installed_ok())
        return self._settings.extra.get("onboarding_model_ready") is True

    @pyqtProperty("QVariantList", notify=modelsChanged)
    def models(self) -> list[dict[str, Any]]:
        return self._downloads.models

    @pyqtProperty(str, notify=freeSpaceTextChanged)
    def freeSpaceText(self) -> str:  # noqa: N802
        return self._downloads.freeSpaceText

    @pyqtSlot()
    def openModelsFolder(self) -> None:  # noqa: N802
        self._downloads.open_models_folder(self._open_url)

    @pyqtProperty(str, notify=selectionChanged)
    def selectionSummary(self) -> str:  # noqa: N802
        return self._downloads.selectionSummary

    @pyqtProperty(bool, notify=selectionChanged)
    def selectionFits(self) -> bool:  # noqa: N802
        return self._downloads.selectionFits

    @pyqtProperty(str, notify=selectionChanged)
    def selectionMessage(self) -> str:  # noqa: N802
        return self._downloads.selectionMessage

    @pyqtProperty(bool, notify=selectionChanged)
    def canContinueFromModel(self) -> bool:  # noqa: N802
        return bool(
            (self._downloads.canContinueFromModel or self.modelReady) and self.selectionFits
        )

    @pyqtProperty(str, notify=downloadStateChanged)
    def downloadState(self) -> str:  # noqa: N802
        return self._downloads.downloadState

    @pyqtProperty(float, notify=downloadProgressChanged)
    def downloadProgress(self) -> float:  # noqa: N802
        return self._downloads.downloadProgress

    @pyqtProperty(str, notify=downloadTitleChanged)
    def downloadTitle(self) -> str:  # noqa: N802
        return self._downloads.downloadTitle

    @pyqtProperty(str, notify=downloadSourceChanged)
    def downloadSource(self) -> str:  # noqa: N802
        return self._downloads.downloadSource

    @pyqtProperty(str, notify=downloadDetailChanged)
    def downloadDetail(self) -> str:  # noqa: N802
        return self._downloads.downloadDetail

    @pyqtSlot(str)
    def toggleModel(self, model_id: str) -> None:  # noqa: N802
        self._downloads.toggleModel(model_id)

    @pyqtSlot()
    def startSelectedDownloads(self) -> None:  # noqa: N802
        self._downloads.startSelectedDownloads()

    @pyqtSlot(str)
    def retryModel(self, model_id: str) -> None:  # noqa: N802
        self._downloads.retryModel(model_id)

    @pyqtSlot(str)
    def cancelModel(self, model_id: str) -> None:  # noqa: N802
        self._downloads.cancelModel(model_id)

    @pyqtSlot(str)
    def dequeueModel(self, model_id: str) -> None:  # noqa: N802
        self._downloads.dequeueModel(model_id)

    @pyqtSlot()
    def cancelDownloads(self) -> None:  # noqa: N802
        self._downloads.cancelDownloads()

    @pyqtProperty(str, notify=modelStateChanged)
    def modelState(self) -> str:  # noqa: N802
        return self._downloads.modelState

    @pyqtProperty(str, notify=modelNameChanged)
    def modelName(self) -> str:  # noqa: N802
        return self._downloads.modelName

    @pyqtProperty(str, notify=modelHostChanged)
    def modelHost(self) -> str:  # noqa: N802
        return self._downloads.modelHost

    @pyqtProperty(int, notify=modelSizeBytesChanged)
    def modelSizeBytes(self) -> int:  # noqa: N802
        return self._downloads.modelSizeBytes

    @pyqtProperty(str, notify=modelSizeChanged)
    def modelSize(self) -> str:  # noqa: N802
        return self._downloads.modelSize

    @pyqtProperty(str, notify=modelRamChanged)
    def modelRam(self) -> str:  # noqa: N802
        # Каталог хранит МБ, а общий форматтер принимает байты.
        return self._downloads.modelRam

    @pyqtProperty(float, notify=progressChanged)
    def progress(self) -> float:
        return self._downloads.progress

    @pyqtProperty(str, notify=speedChanged)
    def speed(self) -> str:
        return self._downloads.speed

    @pyqtProperty(str, notify=etaChanged)
    def eta(self) -> str:
        return self._downloads.eta

    @pyqtProperty(str, notify=modelMessageChanged)
    def modelMessage(self) -> str:  # noqa: N802
        return self._downloads.modelMessage

    def _set_model_state(self, state: str, reason: str = "") -> None:
        self._downloads._set_model_state(state, reason)

    @pyqtSlot(float, float, float)
    def _model_progressed(self, fraction: float, speed: float, eta: float) -> None:
        self._downloads._model_progressed(fraction, speed, eta)

    @pyqtSlot(str)
    def _model_staged(self, stage: str) -> None:
        self._downloads._model_staged(stage)

    @pyqtSlot()
    def download(self) -> None:
        self._downloads.download()

    @pyqtSlot()
    def cancelDownload(self) -> None:  # noqa: N802
        self._downloads.cancelDownload()

    @pyqtSlot()
    def pickInstallPath(self) -> None:  # noqa: N802
        path = self._dialog_factory()
        if path:
            self.installFromPath(path)

    @pyqtSlot(str)
    def installFromPath(self, path: str) -> None:  # noqa: N802
        self._downloads.installFromPath(path)

    def shutdown(self) -> None:
        """Останавливает микрофон и собственную очередь; общую завершает приложение."""
        self._shutting_down = True
        if self._host is not None:
            try:
                self._host.subscribe_device_resolved(None)
            except Exception:
                log.warning("Не удалось снять подписку на имя открытого микрофона", exc_info=True)
        self._clear_test()
        if self._owns_downloads:
            self._downloads.shutdown()

    @pyqtProperty("QVariantList", notify=devicesChanged)
    def devices(self) -> list[dict[str, str]]:
        return [dict(device) for device in self._devices]

    @pyqtProperty(str, notify=deviceChanged)
    def device(self) -> str:
        return cast(str, self._bridge.device)

    @device.setter  # type: ignore[no-redef]
    def device(self, value: str) -> None:
        self._bridge.device = value

    @pyqtProperty(str, notify=deviceResolvedChanged)
    def deviceResolved(self) -> str:  # noqa: N802
        return self._resolved_device

    def _device_resolved(self, name: str) -> None:
        if self._shutting_down or name == self._resolved_device:
            return
        self._resolved_device = name
        self.deviceResolvedChanged.emit()

    @pyqtProperty(str, notify=levelStateChanged)
    def levelState(self) -> str:  # noqa: N802
        return self._level_state

    @pyqtProperty(str, notify=levelMessageChanged)
    def levelMessage(self) -> str:  # noqa: N802
        return self._level_message

    def _level_updated(self, update: MicrophoneLevelUpdate) -> None:
        old = (self._level_state, self._level_message, self._level, self._peak)
        self._level_state = update.state
        self._level_message = update.message
        if update.state == "listening" and update.peak_dbfs is not None:
            self._level = level_from_dbfs(update.peak_dbfs)
            if math.isfinite(update.peak_dbfs):
                self._peak = f"{update.peak_dbfs:.0f} дБ".replace("-", "−")
        elif update.state != "listening":
            self._level = 0.0
        new = (self._level_state, self._level_message, self._level, self._peak)
        for name, before, after in zip(
            ("levelState", "levelMessage", "level", "peak"), old, new, strict=True
        ):
            if before != after:
                getattr(self, name + "Changed").emit()

    def _level_host_failed(self) -> None:
        self._test_after_level = False
        self._level_epoch += 1
        self._level_running = False
        self._level_stopping = False
        if self._host is not None:
            try:
                self._host.stop_level_monitor()
            except Exception:
                pass
        self._level_updated(MicrophoneLevelUpdate("error", message=LEVEL_FAILED))

    @pyqtSlot()
    def startLevelMonitor(self) -> None:  # noqa: N802
        if (
            self._host is None
            or self._step != 4
            or self._shutting_down
            or not self._window_allows_level()
            or self.done
            or self._level_running
            or self._test_state in ("preparing", "recording", "processing")
        ):
            return
        self._level_epoch += 1
        epoch = self._level_epoch
        self._level_running = True
        self._level_stopping = False

        def receive(update: MicrophoneLevelUpdate) -> None:
            if epoch != self._level_epoch or self._shutting_down or self._step != 4:
                return
            if self._level_stopping and update.state == "listening":
                return
            if update.state in ("idle", "error"):
                self._level_running = False
                self._level_stopping = False
                self._level_epoch += 1
            self._level_updated(update)
            pending = self._test_after_level
            if update.state in ("idle", "error"):
                self._test_after_level = False
            if update.state == "idle" and pending:
                self.startTest()

        try:
            accepted = self._host.start_level_monitor(self.device, receive)
            if not accepted and self._level_running:
                self._level_host_failed()
        except Exception:
            self._level_host_failed()

    def _stop_level(self) -> None:
        if self._host is None or not self._level_running or self._level_stopping:
            return
        self._level_stopping = True
        try:
            self._host.stop_level_monitor()
        except Exception:
            self._level_host_failed()

    @pyqtSlot()
    def stopLevelMonitor(self) -> None:  # noqa: N802
        self._test_after_level = False
        self._stop_level()

    @pyqtProperty(float, notify=levelChanged)
    def level(self) -> float:
        return self._level

    @pyqtProperty(str, notify=peakChanged)
    def peak(self) -> str:
        return self._peak

    @pyqtProperty(str, notify=testPhraseChanged)
    def testPhrase(self) -> str:  # noqa: N802
        return "Сегодня хороший день для прогулки."

    @pyqtProperty(str, notify=testStateChanged)
    def testState(self) -> str:  # noqa: N802
        return self._test_state

    @pyqtProperty(str, notify=testTextChanged)
    def testText(self) -> str:  # noqa: N802
        return self._test_text if self._step == 4 and not self.done else ""

    @pyqtProperty(str, notify=testDurationChanged)
    def testDuration(self) -> str:  # noqa: N802
        return self._test_duration

    @pyqtProperty(str, notify=testMessageChanged)
    def testMessage(self) -> str:  # noqa: N802
        return self._test_message

    def _test_updated(self, update: MicrophoneTestUpdate) -> None:
        """Единственный получатель речи проверки: память шага 4, без журналирования."""
        old = (
            self._test_state,
            self._test_text,
            self._test_duration,
            self._test_message,
            self._level,
            self._peak,
        )
        self._test_state = update.state
        self._test_text = update.text if update.state == "done" and self._step == 4 else ""
        self._test_duration = (
            f"{update.duration_s:.2f} с".replace(".", ",")
            if update.state == "done" and update.duration_s is not None
            else ""
        )
        self._test_message = update.message if update.state in ("error", "preparing") else ""
        if update.state == "recording" and update.peak_dbfs is not None:
            self._level = level_from_dbfs(update.peak_dbfs)
            if math.isfinite(update.peak_dbfs):
                self._peak = f"{update.peak_dbfs:.0f} дБ".replace("-", "−")
        elif update.state != "recording":
            self._level = 0.0
        new = (
            self._test_state,
            self._test_text,
            self._test_duration,
            self._test_message,
            self._level,
            self._peak,
        )
        for name, before, after in zip(
            ("testState", "testText", "testDuration", "testMessage", "level", "peak"),
            old,
            new,
            strict=True,
        ):
            if before != after:
                getattr(self, name + "Changed").emit()

    def _clear_test(self) -> None:
        self.stopLevelMonitor()
        # Уход со шага/закрытие запрещает receive, в том числе поздний idle.
        # Сбрасываем экран здесь и отсекаем уведомления предыдущего измерения.
        self._level_epoch += 1
        self._level_running = False
        self._level_stopping = False
        self._level_updated(MicrophoneLevelUpdate("idle"))
        self._test_epoch += 1
        self._test_updated(MicrophoneTestUpdate("idle"))
        if self._peak:
            self._peak = ""
            self.peakChanged.emit()
        if self._host is not None:
            try:
                self._host.cancel_test()
            except Exception:
                # Даже текст исключения может содержать речь — не журналируем его.
                pass

    @pyqtSlot()
    def startTest(self) -> None:  # noqa: N802
        if (
            self._host is None
            or self._step != 4
            or self._shutting_down
            or self.done
            or self._test_state in ("preparing", "recording", "processing")
        ):
            return
        if not self.modelReady:
            message = "Будет доступно после установки модели"
            if self._test_message != message:
                self._test_message = message
                self.testMessageChanged.emit()
            return
        if self._level_running:
            self._test_after_level = True
            self._stop_level()
            return
        self._clear_test()
        epoch = self._test_epoch

        def receive(update: MicrophoneTestUpdate) -> None:
            if epoch != self._test_epoch or self._step != 4 or self._shutting_down:
                return
            if update.state in ("done", "error", "idle"):
                self._test_epoch += 1
            self._test_updated(update)

        try:
            self._host.start_test(self.device, receive)
        except Exception:
            try:
                self._host.cancel_test()
            except Exception:
                # Даже текст исключения может содержать речь — не журналируем его.
                pass
            receive(MicrophoneTestUpdate("error", message=TEST_FAILED))

    @pyqtSlot()
    def stopTest(self) -> None:  # noqa: N802
        if self._host is None or self._test_state not in ("preparing", "recording", "processing"):
            return
        try:
            self._host.stop_test()
        except Exception:
            self._clear_test()
            self._test_updated(MicrophoneTestUpdate("error", message=TEST_FAILED))

    @pyqtProperty(str, notify=languageChanged)
    def language(self) -> str:
        return cast(str, self._bridge.language)

    @language.setter  # type: ignore[no-redef]
    def language(self, value: str) -> None:
        if value not in ("ru", "en") or self._bridge.is_locked("language"):
            return
        self._bridge.language = value
        if self._bridge.language == value:
            self._bridge.set_extra("onboarding_language_set", True)

    @pyqtProperty(bool, notify=checkAppUpdatesChanged)
    def checkAppUpdates(self) -> bool:  # noqa: N802
        return cast(bool, self._bridge.checkAppUpdates)

    @checkAppUpdates.setter  # type: ignore[no-redef]
    def checkAppUpdates(self, value: bool) -> None:  # noqa: N802
        self._bridge.checkAppUpdates = value

    @pyqtProperty(bool, notify=checkModelUpdatesChanged)
    def checkModelUpdates(self) -> bool:  # noqa: N802
        return cast(bool, self._bridge.checkModelUpdates)

    @checkModelUpdates.setter  # type: ignore[no-redef]
    def checkModelUpdates(self, value: bool) -> None:  # noqa: N802
        self._bridge.checkModelUpdates = value

    @pyqtProperty(bool, notify=policyLockedChanged)
    def policyLocked(self) -> bool:  # noqa: N802
        return bool(
            self._bridge.is_locked("check_app_updates")
            or self._bridge.is_locked("check_model_updates")
        )

    @pyqtProperty(str, notify=policyLockedChanged)
    def policyLockedText(self) -> str:  # noqa: N802
        return "Задано администратором" if self.policyLocked else ""

    @pyqtProperty(str, notify=hotkeyChanged)
    def hotkey(self) -> str:
        return cast(str, self._bridge.hotkey)

    @hotkey.setter  # type: ignore[no-redef]
    def hotkey(self, value: str) -> None:
        self._bridge.hotkey = value

    @pyqtProperty(str, notify=hotkeyModeChanged)
    def hotkeyMode(self) -> str:  # noqa: N802
        return cast(str, self._bridge.hotkeyMode)

    @hotkeyMode.setter  # type: ignore[no-redef]
    def hotkeyMode(self, value: str) -> None:  # noqa: N802
        self._bridge.hotkeyMode = value

    @pyqtProperty(str, notify=captureStateChanged)
    def captureState(self) -> str:  # noqa: N802
        return self._capture.state

    @pyqtProperty(str, notify=captureMessageChanged)
    def captureMessage(self) -> str:  # noqa: N802
        return self._capture.message

    @pyqtProperty("QStringList", notify=freeCandidatesChanged)
    def freeCandidates(self) -> list[str]:  # noqa: N802
        return list(self._capture.free_candidates)

    @pyqtProperty(str, notify=pendingComboChanged)
    def pendingCombo(self) -> str:  # noqa: N802
        return self._capture.pending_combo

    @pyqtSlot()
    def beginCapture(self) -> None:  # noqa: N802
        self._capture.begin(self._bridge.save_capture_combo)

    @pyqtSlot(str)
    def endCapture(self, combo: str) -> None:  # noqa: N802
        self._capture.end(combo, self._bridge.save_capture_combo)

    @pyqtSlot()
    def cancelCapture(self) -> None:  # noqa: N802
        self._capture.cancel()

    @pyqtSlot()
    def keepCombo(self) -> None:  # noqa: N802
        self._capture.keep()

    @pyqtSlot()
    def refreshCandidates(self) -> None:  # noqa: N802
        self._capture.refresh_candidates()

    @pyqtProperty(bool, notify=doneChanged)
    def done(self) -> bool:
        return self._settings.extra.get("onboarding_done") is True

    @pyqtSlot()
    def finish(self) -> None:
        if not self.canFinish or self.done:
            return
        if not self._bridge.set_extra("onboarding_done", True):
            return
        self._clear_test()
        if self.captureState == "capturing":
            self.cancelCapture()
        if self._host is not None:
            try:
                self._host.reload_model()
            except Exception:
                log.warning("Не удалось перезагрузить модель после онбординга", exc_info=True)
            try:
                self._host.notify_ready(self.hotkey)
            finally:
                self._host.hide_window()
