"""Настройки для QML: немедленное сохранение, откат и живое применение."""

from __future__ import annotations

import errno
import logging
import math
import os
import shutil
import threading
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any, Protocol, cast

from PyQt5.QtCore import QEvent, QObject, Qt, QThread, QUrl, pyqtProperty, pyqtSignal, pyqtSlot
from PyQt5.QtWidgets import QFileDialog

from astra_voice.core import paths
from astra_voice.core.dictation import (
    TEST_FAILED,
    MicrophoneTestUpdate,
    TestCallback,
    level_from_dbfs,
)
from astra_voice.core.model_source import SmokeRunner
from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings, is_valid_combo
from astra_voice.core.settings import save as settings_save
from astra_voice.core.version import __version__
from astra_voice.models.catalog import CatalogEntry, load_builtin
from astra_voice.models.downloader import Downloader, DownloadError, Progress
from astra_voice.models.installer import Installer, InstallResult, SmokeCheck, SmokeResult
from astra_voice.models.store import ModelStore, StoreError
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import HttpClient, NetworkError
from astra_voice.platform.hotkey import DEFAULT_CANDIDATES
from astra_voice.security.verify import Verifier
from astra_voice.worker.audio import AudioDevice, AudioError, list_devices

log = logging.getLogger(__name__)

_SMOKE_FAILURE = "Модель не прошла пробное распознавание на этом компьютере"
_BROKEN_MESSAGE = "Модель не прошла проверку. Попробуйте скачать или установить её заново."
_SELFCHECK_MESSAGE = "Распознавание на этом компьютере не работает. Обратитесь к администратору"
_REVOKED_MESSAGE = (
    "Издатель больше не рекомендует эту версию модели. "
    "Не устанавливайте её — скачайте свежую версию."
)
# После таймаута поток и задание должны оставаться живы до выхода run().
_finishing_model_threads: set[tuple[QThread, _ModelJob | None]] = set()


class ModelPort(Protocol):
    """Модель для онбординга; операции установки выполняются в рабочем потоке."""

    def recommended(self) -> Any | None: ...

    def installed_ok(self) -> bool: ...

    def broken(self) -> bool: ...

    def allowed(self) -> tuple[bool, str]: ...

    def disk_ok(self, size_bytes: int) -> bool: ...

    def ram_ok(self, min_ram_mb: int) -> bool: ...

    def download(
        self, entry: Any, *, progress: Callable[[Progress], None], cancel: threading.Event
    ) -> Path: ...

    def install_from_staging(self, entry: Any) -> Any: ...

    def install_from_path(self, source: Path, entry: Any) -> Any: ...


def make_smoke_check(
    runner: SmokeRunner | None = None, *, cancel: Callable[[], bool] | None = None
) -> SmokeCheck:
    """Адаптирует каталог к model.load; речь и внутренние причины не выходят наружу."""
    smoke = runner if runner is not None else SmokeRunner()

    def check(directory: Path, entry: CatalogEntry) -> SmokeResult:
        request = {
            "type": "model.load",
            "id": entry.id,
            "revision": entry.revision,
            "dir": str(directory),
            "layout": entry.layout,
            "variant": entry.variant,
            "threads": 2,
            "min_ram_mb": entry.min_ram_mb,
        }
        try:
            ok = smoke(request, cancel=cancel).ok
        except Exception:
            log.warning("Не удалось выполнить пробное распознавание", exc_info=True)
            ok = False
        return SmokeResult(ok=ok, reason="" if ok else _SMOKE_FAILURE)

    return check


class ModelService:
    """Собирает проверенный каталог, хранилище и разрешённую явную загрузку."""

    def __init__(self, settings: Settings, policy: Policy) -> None:
        keyring = paths.data_dir_static() / "keys" / "release.gpg"
        self._catalog = load_builtin(Verifier("catalog", keyring=keyring))
        self._store = ModelStore()
        self._gate = NetworkGate(settings, policy)
        ca_bundle = policy.values.get("ca_bundle")
        http = HttpClient(
            self._gate,
            ca_bundle=Path(ca_bundle) if ca_bundle else None,
            user_agent=f"astra-voice/{__version__} (+https://github.com/ArrivaRUS/astra-voice)",
        )
        self._downloader = Downloader(http, self._store)
        self._cancel = threading.Event()
        self._installer = Installer(
            self._store,
            make_smoke_check(cancel=lambda: self._cancel.is_set()),
            catalog=self._catalog,
        )

    def set_cancel(self, cancel: threading.Event) -> None:
        """Связывает пробное распознавание с отменой текущей попытки установки."""
        self._cancel = cancel

    def recommended(self) -> CatalogEntry | None:
        return next(
            (
                entry
                for entry in self._catalog.entries
                if entry.recommended and not self._catalog.is_revoked(entry.id, entry.revision)
            ),
            None,
        )

    def installed_ok(self) -> bool:
        return any(record.state == "ok" for record in self._store.records())

    def broken(self) -> bool:
        return any(record.state == "broken" for record in self._store.records())

    def allowed(self) -> tuple[bool, str]:
        return self._gate.allowed("download")

    def disk_ok(self, size_bytes: int) -> bool:
        return self._store.disk_ok(size_bytes)

    def disk_missing_bytes(self, size_bytes: int) -> int:
        """Дополнительная деталь для UI: дефицит с тем же запасом, что у ModelStore."""
        directory = self._store.root
        while not directory.exists():
            directory = directory.parent
        return max(0, (size_bytes * 6 + 4) // 5 - shutil.disk_usage(directory).free)

    def ram_ok(self, min_ram_mb: int) -> bool:
        return self._store.ram_ok(min_ram_mb)

    def download(
        self, entry: Any, *, progress: Callable[[Progress], None], cancel: threading.Event
    ) -> Path:
        return self._downloader.download(entry, progress=progress, cancel=cancel)

    def install_from_staging(self, entry: Any) -> InstallResult:
        return self._installer.install_from_staging(entry, cancel=lambda: self._cancel.is_set())

    def install_from_path(self, source: Path, entry: Any) -> InstallResult:
        return self._installer.install_from_path(
            source, entry, cancel=lambda: self._cancel.is_set()
        )


def _megabytes(size_bytes: int, *, round_up: bool = False) -> str:
    value = size_bytes / 1_000_000
    return f"{math.ceil(value) if round_up else value:.0f} МБ"


class _ModelJob(QObject):
    """Одна попытка установки. С GUI общается только сигналами и Event отмены."""

    progressed = pyqtSignal(float, float, float)
    staged = pyqtSignal(str)
    finished = pyqtSignal(str, str)

    def __init__(
        self,
        model: ModelPort,
        entry: Any,
        cancel: threading.Event,
        source: Path | None = None,
    ) -> None:
        super().__init__()
        self._model = model
        self._entry = entry
        self._cancel = cancel
        self._source = source
        if isinstance(model, ModelService):
            model.set_cancel(cancel)

    def _progress(self, value: Progress) -> None:
        fraction = value.bytes_done / value.bytes_total if value.bytes_total > 0 else 0.0
        self.progressed.emit(
            max(0.0, min(1.0, fraction)),
            value.speed_bps,
            value.eta_s if value.eta_s is not None else -1.0,
        )

    def _check_cancel(self) -> None:
        if self._cancel.is_set():
            raise DownloadError("cancelled")

    def _execute(self) -> tuple[str, str]:
        self._check_cancel()
        if self._source is None:
            allowed, reason = self._model.allowed()
            if not allowed:
                return "no-network", reason
        if not self._model.disk_ok(self._entry.size_bytes):
            return "no-space", ""
        if self._source is None:
            self.staged.emit("downloading")
            self._model.download(self._entry, progress=self._progress, cancel=self._cancel)
            self._check_cancel()
            self.progressed.emit(1.0, 0.0, -1.0)
        self.staged.emit("verifying")
        self._check_cancel()
        # Installer объединяет проверку файлов, перенос и пробное распознавание
        # в одну операцию; частичные результаты не публикуются.
        self.staged.emit("installing")
        result = (
            self._model.install_from_staging(self._entry)
            if self._source is None
            else self._model.install_from_path(self._source, self._entry)
        )
        if result.state == "ok":
            return "installed", ""
        self._check_cancel()
        if result.reason_code == "selfcheck":
            return "broken", _SELFCHECK_MESSAGE
        if result.reason_code == "revoked":
            return "broken", _REVOKED_MESSAGE
        if result.reason_code == "cancelled":
            return "cancelled", "Загрузка отменена. Можно продолжить скачивание."
        # Сохраняем распознавание отказа по месту; оно могло измениться после установки.
        if result.state == "error" and (
            "недостаточно места" in result.reason.casefold()
            or not self._model.disk_ok(self._entry.size_bytes)
        ):
            return "no-space", ""
        return "broken", _BROKEN_MESSAGE

    @pyqtSlot()
    def run(self) -> None:
        state, reason = "error", "Не удалось установить модель. Попробуйте ещё раз."
        try:
            state, reason = self._execute()
        except (DownloadError, StoreError, NetworkError, OSError) as exc:
            code = getattr(exc, "code", "")
            if self._cancel.is_set() or code == "cancelled":
                state, reason = "cancelled", "Загрузка отменена. Можно продолжить скачивание."
            elif code == "bad-path":
                state, reason = "broken", _BROKEN_MESSAGE
            elif code == "disk-full" or (
                isinstance(exc, OSError) and exc.errno in (errno.ENOSPC, errno.EDQUOT)
            ):
                state, reason = "no-space", ""
            elif isinstance(exc, NetworkError) or code in {
                "no-network",
                "host-unreachable",
                "timeout",
                "bad-status",
            }:
                state = "no-network"
                reason = f"Нет доступа к {self._entry.host}. Можно установить модель из папки."
            else:
                state, reason = "broken", _BROKEN_MESSAGE
        except Exception:
            log.warning("Не удалось выполнить установку модели", exc_info=True)
        finally:
            self.finished.emit(state, reason)


class SettingsApply(Protocol):
    """Живое применение настроек; захват клавиши проверяется до записи."""

    def pill_enabled(self, value: bool) -> None: ...

    def hotkey(self, combo: str, mode: str) -> str:
        """Пустая строка или ok — успех; иначе код отказа захвата."""
        ...

    def device(self, value: str | None) -> None: ...


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
    hotkeyStatusChanged = pyqtSignal()
    saveErrorChanged = pyqtSignal()
    modelSelfcheckChanged = pyqtSignal()
    extraChanged = pyqtSignal(str)

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
        locked: Iterable[str] = (),
        apply: SettingsApply | None = None,
        save: Callable[[Settings], None] = settings_save,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._settings = settings
        self._mirror = mirror
        self._locked = frozenset(locked)
        self._apply = apply
        self._save = save
        self._values = self._read_values()
        self._hotkey_status = "ok"
        self._save_error = ""
        self._model_selfcheck = "idle"

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
            if code not in ("", "ok"):
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
                self.set_hotkey_status("ok")
            elif name == "device":
                self._apply.device(self.device or None)
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


class OnboardingHost(Protocol):
    """Действия приложения, доступные мастеру первого запуска."""

    def begin_capture(self) -> bool: ...

    def end_capture(self) -> None: ...

    def probe(self, combo: str) -> str: ...

    def free_candidates(self, prefer: list[str]) -> list[str]: ...

    def apply_hotkey(self, combo: str, mode: str) -> str: ...

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
    levelChanged = pyqtSignal()
    peakChanged = pyqtSignal()
    testPhraseChanged = pyqtSignal()
    testStateChanged = pyqtSignal()
    testTextChanged = pyqtSignal()
    testDurationChanged = pyqtSignal()
    testMessageChanged = pyqtSignal()

    _MESSAGES = {
        "conflict": "Эта комбинация занята другой программой. Можно оставить её или выбрать другую",
        "duplicate": "Эта комбинация уже назначена",
        "not-grabbed": "Не удалось назначить комбинацию. Выберите другую",
    }

    def __init__(
        self,
        bridge: SettingsBridge,
        *,
        settings: Settings,
        host: OnboardingHost | None = None,
        model: ModelPort | None = None,
        dialog_factory: Callable[[], str] = QFileDialog.getExistingDirectory,
        device_provider: Callable[[], list[AudioDevice]] = list_devices,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._bridge = bridge
        self._settings = settings
        self._host = host
        self._resolved_device = ""
        self._model = model
        self._dialog_factory = dialog_factory
        self._devices = [{"id": "", "name": "Системный по умолчанию"}]
        try:
            self._devices.extend(
                {"id": device.name, "name": device.label} for device in device_provider()
            )
        except (AudioError, OSError):
            log.warning("Не удалось получить список микрофонов. Доступен системный по умолчанию.")
        self._entry = model.recommended() if model is not None else None
        self._model_state = "absent"
        self._model_message = ""
        self._progress_value = 0.0
        self._speed = ""
        self._eta = ""
        self._model_thread: QThread | None = None
        self._model_job: _ModelJob | None = None
        self._model_cancel = threading.Event()
        self._model_result: tuple[str, str] | None = None
        self._shutting_down = False
        self._level = 0.0
        self._peak = ""
        self._test_state = "idle"
        self._test_text = ""
        self._test_duration = ""
        self._test_message = ""
        self._test_epoch = 0
        self._initial_model_state()
        step = settings.extra.get("onboarding_step")
        self._step = step if type(step) is int and 1 <= step <= 5 else 1
        self._capture_state = "idle"
        self._capture_hint: str = ""
        self._pending_combo = ""
        self._free_candidates: list[str] = []
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
        elif key == "onboarding_done":
            self.doneChanged.emit()

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # noqa: N802
        if event.type() in (QEvent.Hide, QEvent.Close):
            if self._capture_state == "capturing":
                self.cancelCapture()
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
        if self._capture_state == "capturing":
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
        if self._model is not None:
            return self._model.installed_ok()
        return self._settings.extra.get("onboarding_model_ready") is True

    def _initial_model_state(self) -> None:
        model, entry = self._model, self._entry
        if model is None or entry is None:
            self._set_model_state("absent")
        elif model.installed_ok():
            self._set_model_state("installed")
        elif model.broken():
            self._set_model_state("broken")
        else:
            allowed, reason = model.allowed()
            if not allowed:
                self._set_model_state("no-network", reason)
            elif not model.disk_ok(entry.size_bytes):
                self._set_model_state("no-space")
            elif not model.ram_ok(entry.min_ram_mb):
                self._set_model_state("no-ram")
            else:
                self._set_model_state("downloadable")

    @pyqtProperty(str, notify=modelStateChanged)
    def modelState(self) -> str:  # noqa: N802
        return self._model_state

    @pyqtProperty(str, notify=modelNameChanged)
    def modelName(self) -> str:  # noqa: N802
        return str(self._entry.name) if self._entry is not None else ""

    @pyqtProperty(str, notify=modelHostChanged)
    def modelHost(self) -> str:  # noqa: N802
        return str(self._entry.host) if self._entry is not None else ""

    @pyqtProperty(int, notify=modelSizeBytesChanged)
    def modelSizeBytes(self) -> int:  # noqa: N802
        return int(self._entry.size_bytes) if self._entry is not None else 0

    @pyqtProperty(str, notify=modelSizeChanged)
    def modelSize(self) -> str:  # noqa: N802
        return _megabytes(self.modelSizeBytes) if self._entry is not None else ""

    @pyqtProperty(str, notify=modelRamChanged)
    def modelRam(self) -> str:  # noqa: N802
        # Каталог хранит МБ, а общий форматтер принимает байты.
        return _megabytes(self._entry.min_ram_mb * 1_000_000) if self._entry is not None else ""

    @pyqtProperty(float, notify=progressChanged)
    def progress(self) -> float:
        return self._progress_value

    @pyqtProperty(str, notify=speedChanged)
    def speed(self) -> str:
        return self._speed

    @pyqtProperty(str, notify=etaChanged)
    def eta(self) -> str:
        return self._eta

    @pyqtProperty(str, notify=modelMessageChanged)
    def modelMessage(self) -> str:  # noqa: N802
        return self._model_message

    def _space_message(self) -> str:
        # Минимальный порт знает только да/нет. Сервис умеет дать точный дефицит;
        # для других реализаций не выдаём весь размер модели за недостающее место.
        missing = getattr(self._model, "disk_missing_bytes", None)
        if missing is not None:
            try:
                count = missing(self.modelSizeBytes)
                if count > 0:
                    size = _megabytes(count, round_up=True)
                    return f"На диске не хватает {size}. Освободите место."
            except (OSError, StoreError):
                log.warning("Не удалось определить, сколько места не хватает", exc_info=True)
        return f"Недостаточно места на диске для модели {self.modelSize}. Освободите место."

    def _set_model_state(self, state: str, reason: str = "") -> None:
        messages = {
            "absent": "Каталог моделей недоступен. Попробуйте открыть приложение ещё раз.",
            "downloadable": f"Модель будет скачана с {self.modelHost}, объём — {self.modelSize}.",
            "downloading": f"Скачиваю модель с {self.modelHost}…",
            "verifying": "Проверяю целостность файлов модели…",
            "installing": "Устанавливаю модель и проверяю пробное распознавание…",
            "installed": "Модель установлена и готова к работе.",
            "broken": _BROKEN_MESSAGE,
            "no-network": f"Нет доступа к {self.modelHost}. Можно установить модель из папки.",
            "no-ram": "Может не хватить оперативной памяти. Можно скачать и проверить модель.",
            "cancelled": "Загрузка отменена. Можно продолжить скачивание.",
        }
        message = reason or (
            self._space_message() if state == "no-space" else messages.get(state, _BROKEN_MESSAGE)
        )
        changed = state != self._model_state
        message_changed = message != self._model_message
        self._model_state, self._model_message = state, message
        if changed:
            self.modelStateChanged.emit()
        if message_changed:
            self.modelMessageChanged.emit()

    @pyqtSlot(float, float, float)
    def _model_progressed(self, fraction: float, speed: float, eta: float) -> None:
        fraction = max(0.0, min(1.0, fraction)) if math.isfinite(fraction) else 0.0
        speed_text = (
            f"{speed / 1_000_000:.1f} МБ/с".replace(".", ",")
            if math.isfinite(speed) and speed > 0
            else ""
        )
        eta_text = f"осталось ~{math.ceil(eta)} с" if math.isfinite(eta) and eta >= 0 else ""
        if fraction != self._progress_value:
            self._progress_value = fraction
            self.progressChanged.emit()
        if speed_text != self._speed:
            self._speed = speed_text
            self.speedChanged.emit()
        if eta_text != self._eta:
            self._eta = eta_text
            self.etaChanged.emit()

    @pyqtSlot(str)
    def _model_staged(self, stage: str) -> None:
        self._set_model_state(stage)

    @pyqtSlot(str, str)
    def _model_finished(self, state: str, reason: str) -> None:
        # finished работы предшествует finished потока. Итог публикуется после
        # выхода потока, поэтому «Повторить» сразу готов к новой попытке.
        self._model_result = (state, reason)

    @pyqtSlot()
    def _model_thread_finished(self) -> None:
        self._model_job = None
        self._model_thread = None
        if self._model_result is not None:
            state, reason = self._model_result
            self._model_result = None
            self._set_model_state("broken" if state == "error" else state, reason)
            self._model_progressed(1.0 if state == "installed" else self.progress, 0.0, -1.0)
            self.canFinishChanged.emit()

    def _start_model_job(self, source: Path | None = None) -> None:
        if (
            self._shutting_down
            or self._model_thread is not None
            or self._model is None
            or self._entry is None
        ):
            return
        self._model_cancel = threading.Event()
        self._model_result = None
        thread = QThread(self)
        job = _ModelJob(self._model, self._entry, self._model_cancel, source)
        self._model_thread, self._model_job = thread, job
        job.moveToThread(thread)
        thread.started.connect(job.run)
        job.progressed.connect(self._model_progressed)
        job.staged.connect(self._model_staged)
        job.finished.connect(self._model_finished)
        job.finished.connect(job.deleteLater)
        # quit() потокобезопасен. Прямой вызов нужен и при shutdown(), когда GUI
        # ждёт wait() и уже не обрабатывает очередь сигналов.
        job.finished.connect(thread.quit, Qt.DirectConnection)
        thread.finished.connect(self._model_thread_finished)
        thread.finished.connect(thread.deleteLater)
        self._model_progressed(0.0, 0.0, -1.0)
        self._set_model_state("downloading" if source is None else "verifying")
        thread.start()

    @pyqtSlot()
    def download(self) -> None:
        if self._shutting_down or self._model_thread is not None:
            return
        if self._model_state in {"no-network", "no-space", "no-ram"}:
            self._initial_model_state()
        if self._model_state in {"downloadable", "no-ram", "cancelled", "broken"}:
            self._start_model_job()

    @pyqtSlot()
    def cancelDownload(self) -> None:  # noqa: N802
        if self._model_thread is not None and self._model_state == "downloading":
            self._model_cancel.set()

    @pyqtSlot()
    def pickInstallPath(self) -> None:  # noqa: N802
        path = self._dialog_factory()
        if path:
            self.installFromPath(path)

    @pyqtSlot(str)
    def installFromPath(self, path: str) -> None:  # noqa: N802
        if not path.strip():
            return
        url = QUrl(path)
        source = url.toLocalFile() if url.isLocalFile() else path
        self._start_model_job(Path(source).expanduser())

    def shutdown(self) -> None:
        """Отменяет загрузку и пробное распознавание; ждёт поток не дольше пяти секунд."""
        self._shutting_down = True
        if self._host is not None:
            try:
                self._host.subscribe_device_resolved(None)
            except Exception:
                log.warning("Не удалось снять подписку на имя открытого микрофона", exc_info=True)
        self._clear_test()
        self._model_cancel.set()
        if self._model_thread is not None:
            self._model_thread.quit()
            if not self._model_thread.wait(5000):
                log.warning(
                    "Установка модели не завершилась за 5 секунд после отмены; "
                    "выход из приложения продолжается"
                )
                thread = self._model_thread
                thread.setParent(None)
                pair = (thread, self._model_job)
                _finishing_model_threads.add(pair)
                # После отсоединения потока обработчик finished может не выполниться,
                # если очередь GUI уже не крутится; тогда реестр держит пару до конца процесса.
                thread.finished.connect(lambda: _finishing_model_threads.discard(pair))
                self._model_job = None
                self._model_thread = None

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
        return self._capture_state

    @pyqtProperty(str, notify=captureMessageChanged)
    def captureMessage(self) -> str:  # noqa: N802
        return self._capture_hint or self._MESSAGES.get(self._capture_state, "")

    @pyqtProperty("QStringList", notify=freeCandidatesChanged)
    def freeCandidates(self) -> list[str]:  # noqa: N802
        return list(self._free_candidates)

    @pyqtProperty(str, notify=pendingComboChanged)
    def pendingCombo(self) -> str:  # noqa: N802
        return self._pending_combo

    def _set_capture_state(self, state: str) -> None:
        if state != self._capture_state:
            old_message = self.captureMessage
            self._capture_state = state
            self._capture_hint = ""
            self.captureStateChanged.emit()
            if old_message != self.captureMessage:
                self.captureMessageChanged.emit()

    def _set_capture_hint(self, hint: str) -> None:
        old_message = self.captureMessage
        self._capture_hint = hint
        if old_message != self.captureMessage:
            self.captureMessageChanged.emit()

    def _set_pending_combo(self, combo: str) -> None:
        if combo != self._pending_combo:
            self._pending_combo = combo
            self.pendingComboChanged.emit()

    def _end_capture(self) -> None:
        if self._host is not None:
            try:
                self._host.end_capture()
            except Exception:
                log.warning("Не удалось завершить захват клавиатуры", exc_info=True)

    @pyqtSlot()
    def beginCapture(self) -> None:  # noqa: N802
        self._set_capture_hint("")
        self._set_pending_combo("")
        try:
            available = self._host is not None and self._host.begin_capture()
        except Exception:
            log.warning("Не удалось начать захват клавиатуры", exc_info=True)
            available = False
        if not available:
            self._end_capture()
        self._set_capture_state("capturing" if available else "not-grabbed")

    @pyqtSlot(str)
    def endCapture(self, combo: str) -> None:  # noqa: N802
        # XGrabKeyboard снимается ДО любого пробника, сохранения или применения.
        self._end_capture()
        self._set_pending_combo(combo)
        if not combo:
            self._set_capture_state("idle")
            return
        if not is_valid_combo(combo):
            self._set_capture_state("capturing")
            self._set_capture_hint("Добавьте к клавише Ctrl, Alt или Win")
            self.refreshCandidates()
            return
        self._set_capture_state("captured")
        try:
            code = self._host.probe(combo) if self._host is not None else "not-grabbed"
        except Exception:
            log.warning("Не удалось проверить сочетание клавиш", exc_info=True)
            code = "not-grabbed"
        if code == "ok":
            self._save_combo()
        else:
            self._set_capture_state(
                {"busy": "conflict", "duplicate": "duplicate"}.get(code, "not-grabbed")
            )

    @pyqtSlot()
    def cancelCapture(self) -> None:  # noqa: N802
        self._end_capture()
        self._set_capture_hint("")
        self._set_pending_combo("")
        self._set_capture_state("idle")

    def _save_combo(self, *, keep: bool = False) -> None:
        if self._host is None:
            self._set_capture_state("not-grabbed")
            return
        self._bridge.setProperty("hotkey", self._pending_combo)
        if self._bridge.hotkey != self._pending_combo:
            self._set_capture_state("not-grabbed")
            return
        try:
            code = self._host.apply_hotkey(self.hotkey, self.hotkeyMode)
        except Exception:
            log.warning("Не удалось применить сочетание клавиш", exc_info=True)
            code = "not-grabbed"
        # «Оставить» принимает занятость: рантайм продолжает автоматический перезахват.
        if code == "ok" or (keep and code == "busy"):
            self._set_capture_state("success")
        else:
            self._set_capture_state(
                {"busy": "conflict", "duplicate": "duplicate"}.get(code, "not-grabbed")
            )

    @pyqtSlot()
    def keepCombo(self) -> None:  # noqa: N802
        if self._capture_state == "conflict" and self._pending_combo:
            self._save_combo(keep=True)

    @pyqtSlot()
    def refreshCandidates(self) -> None:  # noqa: N802
        try:
            candidates = (
                self._host.free_candidates(list(DEFAULT_CANDIDATES))
                if self._host is not None
                else []
            )
        except Exception:
            log.warning("Не удалось найти свободные сочетания клавиш", exc_info=True)
            candidates = []
            self._set_capture_state("not-grabbed")
        if self._host is None:
            self._set_capture_state("not-grabbed")
        if candidates != self._free_candidates:
            self._free_candidates = list(candidates)
            self.freeCandidatesChanged.emit()

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
        if self._capture_state == "capturing":
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
