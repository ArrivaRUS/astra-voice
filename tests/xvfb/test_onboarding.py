"""Пять шагов и настройки в двух темах: Qt5, состояния мостов и детерминированные PNG.

Каталог снимков задаётся ASTRA_VOICE_SNAPSHOT_DIR_ONBOARDING
или по умолчанию design/refs/impl/onboarding.
Все записи тестов, включая временные PNG, ограничены SNAPSHOTS. Публикация
через os.replace выполняется в teardown только после успеха всего модуля.
"""

from __future__ import annotations

import copy
import os
import re
import sys
import tempfile
from collections.abc import Callable, Iterator
from decimal import Decimal
from functools import partial
from hashlib import sha256
from pathlib import Path
from typing import Any

import pytest
from PyQt5 import sip
from PyQt5.QtCore import (
    QBuffer,
    QByteArray,
    QIODevice,
    QMetaObject,
    QObject,
    QPoint,
    QPointF,
    Qt,
    QUrl,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
    qInstallMessageHandler,
)
from PyQt5.QtGui import QColor, QGuiApplication, QImage
from PyQt5.QtQml import QQmlApplicationEngine, QQmlComponent
from PyQt5.QtQuick import QQuickView, QQuickWindow
from PyQt5.QtTest import QTest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from astra_voice.ui.icons import install_icon_provider
from helpers.qt_app import get_qapplication  # noqa: E402

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]
# Промежуточные прогоны разработки не трогают эталоны репозитория.
SNAPSHOTS = REPO / Path(
    os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR_ONBOARDING") or "design/refs/impl/onboarding"
)
WIDTH, HEIGHT = 1024, 620


def theme_number(name: str) -> float:
    source = (REPO / "qml/Theme.qml").read_text()
    match = re.search(rf"readonly property (?:real|int) {name}: ([0-9.]+)", source)
    assert match is not None, name
    return float(match[1])


def theme_color(name: str, dark: bool) -> QColor:
    source = (REPO / "qml/Theme.qml").read_text()
    match = re.search(rf"readonly property color {name}: ([^\n]+)", source)
    assert match is not None, name
    value = match[1]
    if re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", value):
        return theme_color(value, dark)
    colors = re.fullmatch(r'dark \? "(#[0-9A-Fa-f]+)" : "(#[0-9A-Fa-f]+)"', value)
    assert colors is not None, (name, value)
    return QColor(colors[1 if dark else 2])


MODEL_STATES = (
    "available",
    "new",
    "low-ram",
    "not-recommended",
    "no-network",
    "offline-user",
    "policy",
    "no-benchmark",
    "queued",
    "downloading",
    "verifying",
    "paused-no-space",
    "sha-failed",
    "installed",
    "switching",
    "custom",
    "broken",
    "corrupted",
    "update-available",
    "updating",
    "update-failed",
    "removed-from-catalog",
    "failed",
    "no-space",
)
CAPTURE_STATES = (
    "idle",
    "capturing",
    "captured",
    "success",
    "conflict",
    "duplicate",
    "not-grabbed",
)

os.environ["QT_QUICK_CONTROLS_STYLE"] = "Default"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("QT_QUICK_BACKEND", "software")


class FakeTheme(QObject):
    changed = pyqtSignal()

    def __init__(self, dark: bool) -> None:
        super().__init__()
        self._dark = dark

    @pyqtProperty(bool, notify=changed)
    def dark(self) -> bool:
        return self._dark


class FakeAppInfo(QObject):
    """Только используемый Main.qml контракт настоящего AppInfo из _make_app_info."""

    debugChanged = pyqtSignal()
    showSection = pyqtSignal(str, arguments=["section"])

    @pyqtProperty(str, constant=True)
    def version(self) -> str:
        return "0.1.0"

    @pyqtProperty(str, constant=True)
    def sessionKind(self) -> str:  # noqa: N802 — имя свойства для QML
        return "OTHER"

    @pyqtProperty(bool, notify=debugChanged)
    def debug(self) -> bool:
        return True


class FakeOnboarding(QObject):
    """Полный изменяемый контракт; слоты записывают вызовы без побочных действий."""

    changed = pyqtSignal()
    deviceResolvedChanged = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.toggled_model_ids: list[str] = []
        self.cancelled_model_ids: list[str] = []
        self.dequeued_model_ids: list[str] = []
        self.retried_model_ids: list[str] = []
        self._step: int = 1
        self._policyLocked: bool = False
        self._policyLockedText: str = "Задано администратором"
        self._models: list[dict[str, Any]] = [
            {
                "id": "gigaam-v3-rnnt",
                "name": "GigaAM v3 RNN-T",
                "description": "Русская диктовка с пунктуацией — по умолчанию",
                "host": "huggingface.co",
                "recommended": True,
                "sizeBytes": 226431968,
                "sizeText": "226 МБ",
                "ramText": "768 МБ",
                "ramMb": 768,
                "ramMeasured": False,
                "selected": False,
                "badge": "",
                "state": "available",
                "message": "",
                "hint": "",
                "hintKind": "",
                "memoryShortage": False,
                "canSwitchWithPause": False,
                "canReinstall": True,
                "progress": 0.0,
                "vendor": "Сбер (GigaChat Team)",
                "vendorShort": "Сбер",
                "domestic": True,
                "updateAvailable": False,
                "tags": ["Только русский", "с пунктуацией", "MIT", "отечественная"],
                "metrics": [
                    {
                        "kind": "quality",
                        "label": "Точность",
                        "text": "92,4 %",
                        "fill": 0.924,
                        "hasData": True,
                        "level": "good",
                        "measured": False,
                    },
                    {
                        "kind": "speed",
                        "label": "Скорость",
                        "text": "42,5× быстрее речи",
                        "fill": 0.51,
                        "hasData": True,
                        "level": "good",
                        "measured": False,
                    },
                ],
            },
        ]
        self._selectionSummary: str = ""
        self._downloadCounter: str = ""
        self._selectionFits: bool = True
        self._selectionMessage: str = ""
        self._canContinueFromModel: bool = False
        self._modelReady: bool = True
        self._downloadState: str = "idle"
        self._downloadProgress: float = 0.0
        self._downloadTitle: str = ""
        self._downloadSource: str = ""
        self._downloadDetail: str = ""
        self._freeSpaceText: str = "свободно на диске 42,1 ГБ"
        self._speed: str = "5,2 МБ/с"
        self._eta: str = "осталось ~3 мин"
        self._hotkey: str = "Ctrl + Space"
        self._hotkeyMode: str = "ptt"
        self._captureState: str = "idle"
        self._captureMessage: str = ""
        self._pendingCombo: str = ""
        self._freeCandidates: list[str] = []
        self._devices: list[dict[str, str]] = [
            {"id": "", "name": "Системный по умолчанию"},
            {"id": "builtin", "name": "Встроенный микрофон"},
        ]
        self._device: str = ""
        self._deviceResolved: str = "Встроенный микрофон"
        self._level: float = 1.0
        self._levelState: str = "listening"
        self._levelMessage: str = ""
        self._peak: str = "−18 дБ"
        self._testDuration: str = "0,31 с"
        self._testPhrase: str = "Сегодня хороший день для прогулки."
        self._testText: str = "Проверка связи, раз, два, три."
        self._testState: str = "done"
        self._testMessage: str = ""
        self._canFinish: bool = True
        self._language: str = "ru"
        self._checkAppUpdates: bool = False
        self._checkModelUpdates: bool = False

    def _get_step(self) -> int:
        return self._step

    def _set_step(self, value: int) -> None:
        self._step = value
        self.changed.emit()

    step = pyqtProperty(int, _get_step, _set_step, notify=changed)

    def _get_policyLocked(self) -> bool:
        return self._policyLocked

    def _set_policyLocked(self, value: bool) -> None:
        self._policyLocked = value
        self.changed.emit()

    policyLocked = pyqtProperty(bool, _get_policyLocked, _set_policyLocked, notify=changed)

    def _get_policyLockedText(self) -> str:
        return self._policyLockedText

    def _set_policyLockedText(self, value: str) -> None:
        self._policyLockedText = value
        self.changed.emit()

    policyLockedText = pyqtProperty(
        str, _get_policyLockedText, _set_policyLockedText, notify=changed
    )

    def _get_models(self) -> list[dict[str, Any]]:
        return self._models

    def _set_models(self, value: list[dict[str, Any]]) -> None:
        self._models = value
        self.changed.emit()

    models = pyqtProperty("QVariantList", _get_models, _set_models, notify=changed)

    def _get_selectionSummary(self) -> str:
        return self._selectionSummary

    def _set_selectionSummary(self, value: str) -> None:
        self._selectionSummary = value
        self.changed.emit()

    selectionSummary = pyqtProperty(
        str, _get_selectionSummary, _set_selectionSummary, notify=changed
    )

    @pyqtProperty(str, notify=changed)
    def selectionLine(self) -> str:  # noqa: N802
        if self._selectionSummary:
            return self._selectionSummary
        if self.downloadState == "no-space" and any(
            row["state"] == "paused-no-space" for row in self.models
        ):
            return "Загрузка на паузе"
        if any(row["state"] in {"queued", "downloading", "verifying"} for row in self.models):
            return "Идёт загрузка"
        return "Пока ничего не выбрано"

    def _get_selectionFits(self) -> bool:
        return self._selectionFits

    def _set_selectionFits(self, value: bool) -> None:
        self._selectionFits = value
        self.changed.emit()

    selectionFits = pyqtProperty(bool, _get_selectionFits, _set_selectionFits, notify=changed)

    def _get_selectionMessage(self) -> str:
        return self._selectionMessage

    def _set_selectionMessage(self, value: str) -> None:
        self._selectionMessage = value
        self.changed.emit()

    selectionMessage = pyqtProperty(
        str, _get_selectionMessage, _set_selectionMessage, notify=changed
    )

    def _get_canContinueFromModel(self) -> bool:
        return self._canContinueFromModel

    def _set_canContinueFromModel(self, value: bool) -> None:
        self._canContinueFromModel = value
        self.changed.emit()

    canContinueFromModel = pyqtProperty(
        bool, _get_canContinueFromModel, _set_canContinueFromModel, notify=changed
    )

    def _get_modelReady(self) -> bool:
        return self._modelReady

    def _set_modelReady(self, value: bool) -> None:
        self._modelReady = value
        # В настоящем мосте canFinish вычисляется из modelReady.
        self._canFinish = value
        self.changed.emit()

    modelReady = pyqtProperty(bool, _get_modelReady, _set_modelReady, notify=changed)

    def _get_totalSteps(self) -> int:
        return 5

    totalSteps = pyqtProperty(int, _get_totalSteps, notify=changed)

    def _get_downloadState(self) -> str:
        return self._downloadState

    def _set_downloadState(self, value: str) -> None:
        self._downloadState = value
        self.changed.emit()

    downloadState = pyqtProperty(str, _get_downloadState, _set_downloadState, notify=changed)

    def _get_downloadProgress(self) -> float:
        return self._downloadProgress

    def _set_downloadProgress(self, value: float) -> None:
        self._downloadProgress = value
        self.changed.emit()

    downloadProgress = pyqtProperty(
        float, _get_downloadProgress, _set_downloadProgress, notify=changed
    )

    def _get_downloadTitle(self) -> str:
        return self._downloadTitle

    def _set_downloadTitle(self, value: str) -> None:
        self._downloadTitle = value
        self.changed.emit()

    downloadTitle = pyqtProperty(str, _get_downloadTitle, _set_downloadTitle, notify=changed)

    def _get_downloadCounter(self) -> str:
        return self._downloadCounter

    def _set_downloadCounter(self, value: str) -> None:
        self._downloadCounter = value
        self.changed.emit()

    downloadCounter = pyqtProperty(str, _get_downloadCounter, _set_downloadCounter, notify=changed)

    def _get_downloadSource(self) -> str:
        return self._downloadSource

    def _set_downloadSource(self, value: str) -> None:
        self._downloadSource = value
        self.changed.emit()

    downloadSource = pyqtProperty(str, _get_downloadSource, _set_downloadSource, notify=changed)

    def _get_downloadDetail(self) -> str:
        return self._downloadDetail

    def _set_downloadDetail(self, value: str) -> None:
        self._downloadDetail = value
        self.changed.emit()

    downloadDetail = pyqtProperty(str, _get_downloadDetail, _set_downloadDetail, notify=changed)

    def _get_freeSpaceText(self) -> str:
        return self._freeSpaceText

    def _set_freeSpaceText(self, value: str) -> None:
        self._freeSpaceText = value
        self.changed.emit()

    freeSpaceText = pyqtProperty(str, _get_freeSpaceText, _set_freeSpaceText, notify=changed)

    def _get_speed(self) -> str:
        return self._speed

    def _set_speed(self, value: str) -> None:
        self._speed = value
        self.changed.emit()

    speed = pyqtProperty(str, _get_speed, _set_speed, notify=changed)

    def _get_eta(self) -> str:
        return self._eta

    def _set_eta(self, value: str) -> None:
        self._eta = value
        self.changed.emit()

    eta = pyqtProperty(str, _get_eta, _set_eta, notify=changed)

    def _get_hotkey(self) -> str:
        return self._hotkey

    def _set_hotkey(self, value: str) -> None:
        self._hotkey = value
        self.changed.emit()

    hotkey = pyqtProperty(str, _get_hotkey, _set_hotkey, notify=changed)

    def _get_hotkeyMode(self) -> str:
        return self._hotkeyMode

    def _set_hotkeyMode(self, value: str) -> None:
        self._hotkeyMode = value
        self.changed.emit()

    hotkeyMode = pyqtProperty(str, _get_hotkeyMode, _set_hotkeyMode, notify=changed)

    def _get_captureState(self) -> str:
        return self._captureState

    def _set_captureState(self, value: str) -> None:
        self._captureState = value
        self.changed.emit()

    captureState = pyqtProperty(str, _get_captureState, _set_captureState, notify=changed)

    def _get_captureMessage(self) -> str:
        return self._captureMessage

    def _set_captureMessage(self, value: str) -> None:
        self._captureMessage = value
        self.changed.emit()

    captureMessage = pyqtProperty(str, _get_captureMessage, _set_captureMessage, notify=changed)

    def _get_pendingCombo(self) -> str:
        return self._pendingCombo

    def _set_pendingCombo(self, value: str) -> None:
        self._pendingCombo = value
        self.changed.emit()

    pendingCombo = pyqtProperty(str, _get_pendingCombo, _set_pendingCombo, notify=changed)

    def _get_freeCandidates(self) -> list[str]:
        return self._freeCandidates

    def _set_freeCandidates(self, value: list[str]) -> None:
        self._freeCandidates = value
        self.changed.emit()

    freeCandidates = pyqtProperty(
        "QStringList", _get_freeCandidates, _set_freeCandidates, notify=changed
    )

    def _get_devices(self) -> list[dict[str, str]]:
        return self._devices

    def _set_devices(self, value: list[dict[str, str]]) -> None:
        self._devices = value
        self.changed.emit()

    devices = pyqtProperty("QVariantList", _get_devices, _set_devices, notify=changed)

    def _get_device(self) -> str:
        return self._device

    def _set_device(self, value: str) -> None:
        self._device = value
        self.changed.emit()

    device = pyqtProperty(str, _get_device, _set_device, notify=changed)

    @pyqtProperty(str, notify=deviceResolvedChanged)
    def deviceResolved(self) -> str:  # noqa: N802 — имя свойства для QML
        return self._deviceResolved

    def _get_level(self) -> float:
        return self._level

    def _set_level(self, value: float) -> None:
        self._level = value
        self.changed.emit()

    level = pyqtProperty(float, _get_level, _set_level, notify=changed)

    def _get_levelState(self) -> str:
        return self._levelState

    def _set_levelState(self, value: str) -> None:
        self._levelState = value
        self.changed.emit()

    levelState = pyqtProperty(str, _get_levelState, _set_levelState, notify=changed)

    def _get_levelMessage(self) -> str:
        return self._levelMessage

    def _set_levelMessage(self, value: str) -> None:
        self._levelMessage = value
        self.changed.emit()

    levelMessage = pyqtProperty(str, _get_levelMessage, _set_levelMessage, notify=changed)

    def _get_peak(self) -> str:
        return self._peak

    def _set_peak(self, value: str) -> None:
        self._peak = value
        self.changed.emit()

    peak = pyqtProperty(str, _get_peak, _set_peak, notify=changed)

    def _get_testDuration(self) -> str:
        return self._testDuration

    def _set_testDuration(self, value: str) -> None:
        self._testDuration = value
        self.changed.emit()

    testDuration = pyqtProperty(str, _get_testDuration, _set_testDuration, notify=changed)

    def _get_testPhrase(self) -> str:
        return self._testPhrase

    def _set_testPhrase(self, value: str) -> None:
        self._testPhrase = value
        self.changed.emit()

    testPhrase = pyqtProperty(str, _get_testPhrase, _set_testPhrase, notify=changed)

    def _get_testText(self) -> str:
        return self._testText

    def _set_testText(self, value: str) -> None:
        self._testText = value
        self.changed.emit()

    testText = pyqtProperty(str, _get_testText, _set_testText, notify=changed)

    def _get_testState(self) -> str:
        return self._testState

    def _set_testState(self, value: str) -> None:
        self._testState = value
        self.changed.emit()

    testState = pyqtProperty(str, _get_testState, _set_testState, notify=changed)

    def _get_testMessage(self) -> str:
        return self._testMessage

    def _set_testMessage(self, value: str) -> None:
        self._testMessage = value
        self.changed.emit()

    testMessage = pyqtProperty(str, _get_testMessage, _set_testMessage, notify=changed)

    def _get_canFinish(self) -> bool:
        return self._canFinish

    def _set_canFinish(self, value: bool) -> None:
        self._canFinish = value
        self.changed.emit()

    canFinish = pyqtProperty(bool, _get_canFinish, _set_canFinish, notify=changed)

    def _get_language(self) -> str:
        return self._language

    def _set_language(self, value: str) -> None:
        self._language = value
        self.changed.emit()

    language = pyqtProperty(str, _get_language, _set_language, notify=changed)

    def _get_checkAppUpdates(self) -> bool:
        return self._checkAppUpdates

    def _set_checkAppUpdates(self, value: bool) -> None:
        self._checkAppUpdates = value
        self.changed.emit()

    checkAppUpdates = pyqtProperty(bool, _get_checkAppUpdates, _set_checkAppUpdates, notify=changed)

    def _get_checkModelUpdates(self) -> bool:
        return self._checkModelUpdates

    def _set_checkModelUpdates(self, value: bool) -> None:
        self._checkModelUpdates = value
        self.changed.emit()

    checkModelUpdates = pyqtProperty(
        bool, _get_checkModelUpdates, _set_checkModelUpdates, notify=changed
    )

    @pyqtSlot()
    def next(self) -> None:
        self.calls.append("next")

    @pyqtSlot()
    def back(self) -> None:
        self.calls.append("back")

    @pyqtSlot()
    def skip(self) -> None:
        self.calls.append("skip")

    @pyqtSlot(str)
    def toggleModel(self, model_id: str) -> None:
        self.calls.append("toggleModel")
        self.toggled_model_ids.append(model_id)

    @pyqtSlot()
    def startSelectedDownloads(self) -> None:
        self.calls.append("startSelectedDownloads")

    @pyqtSlot(str)
    def retryModel(self, model_id: str) -> None:
        self.calls.append("retryModel")
        self.retried_model_ids.append(model_id)

    @pyqtSlot(str)
    def cancelModel(self, model_id: str) -> None:
        self.calls.append("cancelModel")
        self.cancelled_model_ids.append(model_id)

    @pyqtSlot(str)
    def dequeueModel(self, model_id: str) -> None:
        self.calls.append("dequeueModel")
        self.dequeued_model_ids.append(model_id)

    @pyqtSlot()
    def cancelDownloads(self) -> None:
        self.calls.append("cancelDownloads")

    @pyqtSlot(str)
    def installFromPath(self, path: str) -> None:
        self.calls.append("installFromPath")

    @pyqtSlot()
    def pickInstallPath(self) -> None:
        self.calls.append("pickInstallPath")

    @pyqtSlot()
    def beginCapture(self) -> None:
        self.calls.append("beginCapture")

    @pyqtSlot(str)
    def endCapture(self, combo: str) -> None:
        self.calls.append("endCapture")

    @pyqtSlot()
    def cancelCapture(self) -> None:
        self.calls.append("cancelCapture")

    @pyqtSlot()
    def keepCombo(self) -> None:
        self.calls.append("keepCombo")

    @pyqtSlot()
    def refreshCandidates(self) -> None:
        self.calls.append("refreshCandidates")

    @pyqtSlot()
    def startLevelMonitor(self) -> None:
        self.calls.append("startLevelMonitor")

    @pyqtSlot()
    def stopLevelMonitor(self) -> None:
        self.calls.append("stopLevelMonitor")

    @pyqtSlot()
    def startTest(self) -> None:
        self.calls.append("startTest")

    @pyqtSlot()
    def stopTest(self) -> None:
        self.calls.append("stopTest")

    @pyqtSlot()
    def openModelsFolder(self) -> None:
        self.calls.append("openModelsFolder")

    @pyqtSlot()
    def finish(self) -> None:
        self.calls.append("finish")


class FakeSettings(QObject):
    """Изменяемый контракт настроек без системных побочных действий."""

    changed = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[str] = []
        self.toggled_model_ids: list[str] = []
        self.cancelled_model_ids: list[str] = []
        self.dequeued_model_ids: list[str] = []
        self.retried_model_ids: list[str] = []
        self._hotkey: str = "Ctrl + Space"
        self._hotkeyMode: str = "ptt"
        self._captureState: str = "idle"
        self._captureMessage: str = ""
        self._pendingCombo: str = ""
        self._freeCandidates: list[str] = []
        self._microphoneVolume: int = 80
        self._microphoneMuted: bool = False
        self._canRaiseMicrophone: bool = True
        self._devices: list[dict[str, str]] = [
            {"id": "", "name": "Системный по умолчанию"},
            {"id": "alsa_input.mic", "name": "Микрофон гарнитуры"},
        ]
        self._pillEnabled: bool = True
        self._language: str = "ru"
        self._checkAppUpdates: bool = False
        self._checkModelUpdates: bool = False
        self._autostart: bool = True
        self._device: str = "Системный по умолчанию"
        self._hotkeyStatus: str = "ok"
        self._lockedSettings: list[str] = []
        self._saveError: str = ""
        self._modelSelfcheck: str = "idle"
        self._activeModelName: str = "GigaAM v3 RNN-T"
        self._activeModelSize: str = "226 МБ"
        self._activeModelState: str = "ok"
        self._activeModelMessage: str = ""
        self._canReinstall: bool = True
        self._canInstall: bool = False
        self._revocationUnknown: bool = False
        self._models: list[dict[str, Any]] = [
            {
                "id": "gigaam-v3-rnnt",
                "name": "GigaAM v3 RNN-T",
                "description": "Русская диктовка с пунктуацией — по умолчанию",
                "host": "huggingface.co",
                "recommended": True,
                "sizeBytes": 226431968,
                "sizeText": "226 МБ",
                "ramText": "768 МБ",
                "ramMb": 768,
                "ramMeasured": False,
                "selected": True,
                "badge": "active",
                "state": "installed",
                "message": "",
                "hint": "",
                "hintKind": "",
                "memoryShortage": False,
                "canSwitchWithPause": False,
                "canReinstall": True,
                "progress": 0.0,
                "vendor": "Сбер (GigaChat Team)",
                "vendorShort": "Сбер",
                "domestic": True,
                "updateAvailable": False,
                "tags": ["Только русский", "с пунктуацией", "MIT", "отечественная"],
                "metrics": [
                    {
                        "kind": "quality",
                        "label": "Точность",
                        "text": "92,4 %",
                        "fill": 0.924,
                        "hasData": True,
                        "level": "good",
                        "measured": False,
                    },
                    {
                        "kind": "speed",
                        "label": "Скорость",
                        "text": "42,5× быстрее речи",
                        "fill": 0.51,
                        "hasData": True,
                        "level": "good",
                        "measured": False,
                    },
                ],
            },
            {
                "id": "t-one",
                "name": "T-one",
                "description": "Русская, лёгкая, без пунктуации; вес только fp32",
                "host": "huggingface.co",
                "recommended": False,
                "sizeBytes": 144_200_000,
                "sizeText": "144 МБ",
                "ramText": "300 МБ",
                "ramMb": 300,
                "ramMeasured": False,
                "selected": False,
                "badge": "",
                "state": "available",
                "message": "",
                "hint": "",
                "hintKind": "",
                "memoryShortage": False,
                "canSwitchWithPause": False,
                "canReinstall": True,
                "progress": 0.0,
                "vendor": "Т-Банк",
                "vendorShort": "Т-Банк",
                "domestic": True,
                "updateAvailable": False,
                "tags": [
                    "Только русский",
                    "без пунктуации",
                    "Apache-2.0",
                    "отечественная",
                ],
                "metrics": [
                    {
                        "kind": "quality",
                        "label": "Точность",
                        "text": "93,4 %",
                        "fill": 0.9343,
                        "hasData": True,
                        "level": "good",
                        "measured": False,
                    },
                    {
                        "kind": "speed",
                        "label": "Скорость",
                        "text": "26,3× быстрее речи",
                        "fill": 0.31,
                        "hasData": True,
                        "level": "good",
                        "measured": False,
                    },
                ],
            },
        ]
        self._installedSummary: str = "Установлено 1 из 12 · 226 МБ на диске"
        self._installedCount: int = 1
        self._selectionSummary: str = ""
        self._downloadCounter: str = ""
        self._selectionFits: bool = True
        self._selectionMessage: str = ""
        self._freeSpaceText: str = "свободно на диске 42,1 ГБ"
        self._downloadState: str = "idle"
        self._downloadProgress: float = 0.0
        self._downloadTitle: str = ""
        self._downloadSource: str = ""
        self._downloadDetail: str = ""
        self._speed: str = ""
        self._eta: str = ""

    def _get_hotkey(self) -> str:
        return self._hotkey

    def _set_hotkey(self, value: str) -> None:
        self._hotkey = value
        self.changed.emit()

    hotkey = pyqtProperty(str, _get_hotkey, _set_hotkey, notify=changed)

    def _get_hotkeyMode(self) -> str:
        return self._hotkeyMode

    def _set_hotkeyMode(self, value: str) -> None:
        self._hotkeyMode = value
        self.changed.emit()

    hotkeyMode = pyqtProperty(str, _get_hotkeyMode, _set_hotkeyMode, notify=changed)

    def _get_captureState(self) -> str:
        return self._captureState

    def _set_captureState(self, value: str) -> None:
        self._captureState = value
        self.changed.emit()

    captureState = pyqtProperty(str, _get_captureState, _set_captureState, notify=changed)

    def _get_captureMessage(self) -> str:
        return self._captureMessage

    captureMessage = pyqtProperty(str, _get_captureMessage, notify=changed)

    def _get_pendingCombo(self) -> str:
        return self._pendingCombo

    pendingCombo = pyqtProperty(str, _get_pendingCombo, notify=changed)

    def _get_freeCandidates(self) -> list[str]:
        return self._freeCandidates

    freeCandidates = pyqtProperty("QStringList", _get_freeCandidates, notify=changed)

    def _get_pillEnabled(self) -> bool:
        return self._pillEnabled

    def _set_pillEnabled(self, value: bool) -> None:
        self._pillEnabled = value
        self.changed.emit()

    pillEnabled = pyqtProperty(bool, _get_pillEnabled, _set_pillEnabled, notify=changed)

    def _get_language(self) -> str:
        return self._language

    def _set_language(self, value: str) -> None:
        self._language = value
        self.changed.emit()

    language = pyqtProperty(str, _get_language, _set_language, notify=changed)

    def _get_checkAppUpdates(self) -> bool:
        return self._checkAppUpdates

    def _set_checkAppUpdates(self, value: bool) -> None:
        self._checkAppUpdates = value
        self.changed.emit()

    checkAppUpdates = pyqtProperty(bool, _get_checkAppUpdates, _set_checkAppUpdates, notify=changed)

    def _get_checkModelUpdates(self) -> bool:
        return self._checkModelUpdates

    def _set_checkModelUpdates(self, value: bool) -> None:
        self._checkModelUpdates = value
        self.changed.emit()

    checkModelUpdates = pyqtProperty(
        bool, _get_checkModelUpdates, _set_checkModelUpdates, notify=changed
    )

    def _get_autostart(self) -> bool:
        return self._autostart

    def _set_autostart(self, value: bool) -> None:
        self._autostart = value
        self.changed.emit()

    autostart = pyqtProperty(bool, _get_autostart, _set_autostart, notify=changed)

    def _get_device(self) -> str:
        return self._device

    def _set_device(self, value: str) -> None:
        self._device = value
        self.changed.emit()

    device = pyqtProperty(str, _get_device, _set_device, notify=changed)

    def _get_hotkeyStatus(self) -> str:
        return self._hotkeyStatus

    def _set_hotkeyStatus(self, value: str) -> None:
        self._hotkeyStatus = value
        self.changed.emit()

    hotkeyStatus = pyqtProperty(str, _get_hotkeyStatus, _set_hotkeyStatus, notify=changed)

    def _get_lockedSettings(self) -> list[str]:
        return self._lockedSettings

    def _set_lockedSettings(self, value: list[str]) -> None:
        self._lockedSettings = value
        self.changed.emit()

    lockedSettings = pyqtProperty(
        "QStringList", _get_lockedSettings, _set_lockedSettings, notify=changed
    )

    def _get_saveError(self) -> str:
        return self._saveError

    saveError = pyqtProperty(str, _get_saveError, notify=changed)

    def _get_modelSelfcheck(self) -> str:
        return self._modelSelfcheck

    modelSelfcheck = pyqtProperty(str, _get_modelSelfcheck, notify=changed)

    def _get_activeModelName(self) -> str:
        return self._activeModelName

    def _set_activeModelName(self, value: str) -> None:
        self._activeModelName = value
        self.changed.emit()

    activeModelName = pyqtProperty(str, _get_activeModelName, _set_activeModelName, notify=changed)

    def _get_activeModelSize(self) -> str:
        return self._activeModelSize

    def _set_activeModelSize(self, value: str) -> None:
        self._activeModelSize = value
        self.changed.emit()

    activeModelSize = pyqtProperty(str, _get_activeModelSize, _set_activeModelSize, notify=changed)

    def _get_activeModelState(self) -> str:
        return self._activeModelState

    def _set_activeModelState(self, value: str) -> None:
        self._activeModelState = value
        self.changed.emit()

    activeModelState = pyqtProperty(
        str, _get_activeModelState, _set_activeModelState, notify=changed
    )

    def _get_activeModelMessage(self) -> str:
        return self._activeModelMessage

    def _set_activeModelMessage(self, value: str) -> None:
        self._activeModelMessage = value
        self.changed.emit()

    activeModelMessage = pyqtProperty(
        str, _get_activeModelMessage, _set_activeModelMessage, notify=changed
    )

    def _get_canReinstall(self) -> bool:
        return self._canReinstall

    def _set_canReinstall(self, value: bool) -> None:
        self._canReinstall = value
        self.changed.emit()

    canReinstall = pyqtProperty(bool, _get_canReinstall, _set_canReinstall, notify=changed)

    def _get_canInstall(self) -> bool:
        return self._canInstall

    def _set_canInstall(self, value: bool) -> None:
        self._canInstall = value
        self.changed.emit()

    canInstall = pyqtProperty(bool, _get_canInstall, _set_canInstall, notify=changed)

    def _get_models(self) -> list[dict[str, Any]]:
        return self._models

    def _set_models(self, value: list[dict[str, Any]]) -> None:
        self._models = value
        self.changed.emit()

    models = pyqtProperty("QVariantList", _get_models, _set_models, notify=changed)

    def _get_revocationUnknown(self) -> bool:
        return self._revocationUnknown

    def _set_revocationUnknown(self, value: bool) -> None:
        self._revocationUnknown = value
        self.changed.emit()

    revocationUnknown = pyqtProperty(
        bool, _get_revocationUnknown, _set_revocationUnknown, notify=changed
    )

    def _get_selectionSummary(self) -> str:
        return self._selectionSummary

    def _set_selectionSummary(self, value: str) -> None:
        self._selectionSummary = value
        self.changed.emit()

    selectionSummary = pyqtProperty(
        str, _get_selectionSummary, _set_selectionSummary, notify=changed
    )

    @pyqtProperty(str, notify=changed)
    def selectionLine(self) -> str:  # noqa: N802
        if self._selectionSummary:
            return self._selectionSummary
        if self.downloadState == "no-space" and any(
            row["state"] == "paused-no-space" for row in self.models
        ):
            return "Загрузка на паузе"
        if any(row["state"] in {"queued", "downloading", "verifying"} for row in self.models):
            return "Идёт загрузка"
        return "Пока ничего не выбрано"

    def _get_selectionFits(self) -> bool:
        return self._selectionFits

    def _set_selectionFits(self, value: bool) -> None:
        self._selectionFits = value
        self.changed.emit()

    selectionFits = pyqtProperty(bool, _get_selectionFits, _set_selectionFits, notify=changed)

    def _get_selectionMessage(self) -> str:
        return self._selectionMessage

    def _set_selectionMessage(self, value: str) -> None:
        self._selectionMessage = value
        self.changed.emit()

    selectionMessage = pyqtProperty(
        str, _get_selectionMessage, _set_selectionMessage, notify=changed
    )

    def _get_freeSpaceText(self) -> str:
        return self._freeSpaceText

    def _set_freeSpaceText(self, value: str) -> None:
        self._freeSpaceText = value
        self.changed.emit()

    freeSpaceText = pyqtProperty(str, _get_freeSpaceText, _set_freeSpaceText, notify=changed)

    def _get_installedSummary(self) -> str:
        return self._installedSummary

    def _set_installedSummary(self, value: str) -> None:
        self._installedSummary = value
        self.changed.emit()

    installedSummary = pyqtProperty(
        str, _get_installedSummary, _set_installedSummary, notify=changed
    )

    def _get_installedCount(self) -> int:
        return self._installedCount

    def _set_installedCount(self, value: int) -> None:
        self._installedCount = value
        self.changed.emit()

    installedCount = pyqtProperty(int, _get_installedCount, _set_installedCount, notify=changed)

    def _get_downloadState(self) -> str:
        return self._downloadState

    def _set_downloadState(self, value: str) -> None:
        self._downloadState = value
        self.changed.emit()

    downloadState = pyqtProperty(str, _get_downloadState, _set_downloadState, notify=changed)

    def _get_downloadProgress(self) -> float:
        return self._downloadProgress

    def _set_downloadProgress(self, value: float) -> None:
        self._downloadProgress = value
        self.changed.emit()

    downloadProgress = pyqtProperty(
        float, _get_downloadProgress, _set_downloadProgress, notify=changed
    )

    def _get_downloadTitle(self) -> str:
        return self._downloadTitle

    def _set_downloadTitle(self, value: str) -> None:
        self._downloadTitle = value
        self.changed.emit()

    downloadTitle = pyqtProperty(str, _get_downloadTitle, _set_downloadTitle, notify=changed)

    def _get_downloadCounter(self) -> str:
        return self._downloadCounter

    def _set_downloadCounter(self, value: str) -> None:
        self._downloadCounter = value
        self.changed.emit()

    downloadCounter = pyqtProperty(str, _get_downloadCounter, _set_downloadCounter, notify=changed)

    def _get_downloadSource(self) -> str:
        return self._downloadSource

    def _set_downloadSource(self, value: str) -> None:
        self._downloadSource = value
        self.changed.emit()

    downloadSource = pyqtProperty(str, _get_downloadSource, _set_downloadSource, notify=changed)

    def _get_downloadDetail(self) -> str:
        return self._downloadDetail

    def _set_downloadDetail(self, value: str) -> None:
        self._downloadDetail = value
        self.changed.emit()

    downloadDetail = pyqtProperty(str, _get_downloadDetail, _set_downloadDetail, notify=changed)

    def _get_speed(self) -> str:
        return self._speed

    def _set_speed(self, value: str) -> None:
        self._speed = value
        self.changed.emit()

    speed = pyqtProperty(str, _get_speed, _set_speed, notify=changed)

    def _get_eta(self) -> str:
        return self._eta

    def _set_eta(self, value: str) -> None:
        self._eta = value
        self.changed.emit()

    eta = pyqtProperty(str, _get_eta, _set_eta, notify=changed)

    @pyqtSlot(str)
    def makeModelCurrent(self, model_id: str) -> None:
        self.calls.append("makeModelCurrent")

    @pyqtSlot(str)
    def switchModelWithPause(self, model_id: str) -> None:
        self.calls.append("switchModelWithPause")

    @pyqtSlot(str)
    def reinstallModel(self, model_id: str) -> None:
        self.calls.append("reinstallModel")

    @pyqtSlot(str)
    def removeModel(self, model_id: str) -> None:
        self.calls.append("removeModel")

    @pyqtSlot(str)
    def updateModel(self, model_id: str) -> None:
        self.calls.append("updateModel")

    @pyqtSlot()
    def reinstallActiveModel(self) -> None:
        self.calls.append("reinstallActiveModel")

    @pyqtSlot()
    def installRecommendedModel(self) -> None:
        self.calls.append("installRecommendedModel")

    # Микрофон и звук (блок 5 M6) — без системных вызовов.
    def _get_microphoneVolume(self) -> int:
        return self._microphoneVolume

    def _set_microphoneVolume(self, value: int) -> None:
        self._microphoneVolume = value
        self.changed.emit()

    microphoneVolume = pyqtProperty(
        int, _get_microphoneVolume, _set_microphoneVolume, notify=changed
    )

    def _get_microphoneMuted(self) -> bool:
        return self._microphoneMuted

    def _set_microphoneMuted(self, value: bool) -> None:
        self._microphoneMuted = value
        self.changed.emit()

    microphoneMuted = pyqtProperty(bool, _get_microphoneMuted, _set_microphoneMuted, notify=changed)

    def _get_canRaiseMicrophone(self) -> bool:
        return self._canRaiseMicrophone

    def _set_canRaiseMicrophone(self, value: bool) -> None:
        self._canRaiseMicrophone = value
        self.changed.emit()

    canRaiseMicrophone = pyqtProperty(
        bool, _get_canRaiseMicrophone, _set_canRaiseMicrophone, notify=changed
    )

    def _get_canOpenSoundSettings(self) -> bool:
        return True

    canOpenSoundSettings = pyqtProperty(bool, _get_canOpenSoundSettings, notify=changed)

    def _get_canRestartSoundService(self) -> bool:
        return True

    canRestartSoundService = pyqtProperty(bool, _get_canRestartSoundService, notify=changed)

    def _get_devices(self) -> list[dict[str, str]]:
        return self._devices

    def _set_devices(self, value: list[dict[str, str]]) -> None:
        self._devices = value
        self.changed.emit()

    devices = pyqtProperty("QVariantList", _get_devices, _set_devices, notify=changed)

    @pyqtSlot()
    def refreshMicrophone(self) -> None:
        self.calls.append("refreshMicrophone")

    @pyqtSlot()
    def raiseMicrophoneVolume(self) -> None:
        self.calls.append("raiseMicrophoneVolume")

    @pyqtSlot()
    def openSoundSettings(self) -> None:
        self.calls.append("openSoundSettings")

    @pyqtSlot()
    def restartSoundService(self) -> None:
        self.calls.append("restartSoundService")

    @pyqtSlot()
    def refreshDevices(self) -> None:
        self.calls.append("refreshDevices")

    @pyqtSlot()
    def beginCapture(self) -> None:
        self.calls.append("beginCapture")
        self.captureState = "capturing"

    @pyqtSlot(str)
    def endCapture(self, combo: str) -> None:
        self.calls.append("endCapture")
        self.captureState = "captured" if combo else "idle"

    @pyqtSlot()
    def cancelCapture(self) -> None:
        self.calls.append("cancelCapture")
        self.captureState = "idle"

    @pyqtSlot()
    def keepCombo(self) -> None:
        self.calls.append("keepCombo")

    @pyqtSlot()
    def refreshCandidates(self) -> None:
        self.calls.append("refreshCandidates")

    @pyqtSlot()
    def cancelDownloads(self) -> None:
        self.calls.append("cancelDownloads")

    @pyqtSlot(str)
    def cancelModel(self, model_id: str) -> None:
        self.calls.append("cancelModel")
        self.cancelled_model_ids.append(model_id)

    @pyqtSlot(str)
    def dequeueModel(self, model_id: str) -> None:
        self.calls.append("dequeueModel")
        self.dequeued_model_ids.append(model_id)

    @pyqtSlot()
    def openModelsFolder(self) -> None:
        self.calls.append("openModelsFolder")

    @pyqtSlot(str)
    def toggleModel(self, model_id: str) -> None:
        self.toggled_model_ids.append(model_id)

    @pyqtSlot()
    def startSelectedDownloads(self) -> None:
        self.calls.append("startSelectedDownloads")

    @pyqtSlot(str)
    def retryModel(self, model_id: str) -> None:
        self.calls.append("retryModel")
        self.retried_model_ids.append(model_id)

    @pyqtSlot()
    def pickInstallPath(self) -> None:
        self.calls.append("pickInstallPath")

    @pyqtSlot(str, result=bool)
    def is_locked(self, name: str) -> bool:
        return False

    @pyqtSlot()
    def retryHotkey(self) -> None:
        pass


def configure_onboarding(step: int, **properties: Any) -> FakeOnboarding:
    fake = FakeOnboarding()
    fake.step = step
    for name, value in properties.items():
        setattr(fake, name, value)
    return fake


def configure_selected_model() -> FakeOnboarding:
    fake = configure_onboarding(
        2, selectionSummary="Будет скачано 226 МБ", canContinueFromModel=True
    )
    fake.models = [{**fake.models[0], "selected": True}, *fake.models[1:]]
    return fake


def configure_mic() -> FakeOnboarding:
    fake = configure_onboarding(4)
    fake.models = [{**fake.models[0], "badge": "active", "state": "installed", "selected": True}]
    return fake


SNAPSHOT_SETUPS: dict[str, Callable[[], FakeOnboarding | FakeSettings]] = {
    "01-welcome": partial(configure_onboarding, 1),
    "02-model": partial(configure_onboarding, 2),
    "02-model-selected": configure_selected_model,
    "03-hotkey": partial(configure_onboarding, 3),
    "03-hotkey-progress": partial(
        configure_onboarding,
        3,
        downloadState="downloading",
        downloadProgress=0.43,
        downloadTitle="Загружается GigaAM v3 RNN-T",
        speed="5,2 МБ/с",
        eta="осталось ~3 мин",
    ),
    "04-mic": configure_mic,
    "04-mic-downloading": partial(
        configure_onboarding,
        4,
        modelReady=False,
        downloadState="verifying",
        downloadTitle="Проверяю модель…",
        testState="idle",
        testText="",
        testDuration="",
    ),
    "05-done": partial(configure_onboarding, 5),
    "05-done-waiting": partial(
        configure_onboarding,
        5,
        modelReady=False,
        canFinish=False,
        downloadState="downloading",
        downloadProgress=0.43,
        downloadTitle="Загружается GigaAM v3 RNN-T",
        speed="5,2 МБ/с",
        eta="осталось ~3 мин",
    ),
    "06-settings-general": FakeSettings,
    "07-settings-models": FakeSettings,
    "08-settings-about": FakeSettings,
}
#: Снимок окна настроек делается в этом разделе; остальные — «Общие».
SNAPSHOT_SECTIONS = {"07-settings-models": "models", "08-settings-about": "about"}
CASES = [(name, dark) for name in SNAPSHOT_SETUPS for dark in (False, True)]


@pytest.fixture(scope="session")
def onboarding_app() -> Any:
    return get_qapplication()


def visual_tree(root: Any) -> Iterator[Any]:
    """childItems включает делегаты Repeater, отсутствующие в QObject.children()."""
    yield root
    for child in root.childItems():
        yield from visual_tree(child)


def visible_texts(root: Any) -> set[str]:
    return {
        item.property("text")
        for item in visual_tree(root)
        if item.isVisible() and isinstance(item.property("text"), str)
    }


def visible_button(root: Any, text: str) -> Any:
    buttons = [
        item
        for item in visual_tree(root)
        if item.isVisible()
        and item.property("text") == text
        and item.metaObject().indexOfSignal(b"clicked()") >= 0
    ]
    assert len(buttons) == 1, f"ожидалась одна кнопка «{text}», найдено {len(buttons)}"
    return buttons[0]


def click_item(item: Any) -> None:
    assert item.isVisible()
    position = item.mapToScene(QPointF(item.width() / 2, item.height() / 2))
    QTest.mouseClick(item.window(), Qt.LeftButton, Qt.NoModifier, position.toPoint())


def settings_sidebar(window: Any) -> Any:
    return next(
        item
        for item in visual_tree(window.contentItem())
        if item.metaObject().indexOfProperty("debugCurrent") >= 0
    )


def section_descriptions(window: Any) -> list[dict[str, str]]:
    return list(settings_sidebar(window).property("sections").toVariant())


def select_section(app: Any, window: Any, section: str) -> dict[str, str]:
    """Кликает по пункту бокового меню и ждёт, пока раздел проявится."""
    sidebar = settings_sidebar(window)
    descriptions = section_descriptions(window)
    keys = [description["key"] for description in descriptions]
    index = keys.index(section)
    items = [
        item
        for item in visual_tree(sidebar)
        if item.isVisible() and item.property("title") == descriptions[index]["title"]
    ]
    assert len(items) == 1, f"ожидался один пункт меню «{descriptions[index]['title']}»"
    click_item(items[0])
    app.processEvents()
    assert sidebar.property("currentIndex") == index
    # Переход между разделами — плавное появление (motion.duration.enter = 160).
    QTest.qWait(220)
    app.processEvents()
    return descriptions[index]


def microphone_card(root: Any) -> Any:
    group = next(
        item for item in visual_tree(root) if item.property("title") == "Проверка микрофона"
    )
    cards = [
        item for item in visual_tree(group) if item.metaObject().indexOfProperty("rowData") >= 0
    ]
    assert len(cards) == 1
    return cards[0]


def snapshot_name(name: str, dark: bool) -> str:
    return f"{name}-{'dark' if dark else 'light'}.png"


def assert_frame(image: QImage) -> None:
    assert not image.isNull(), "пустой снимок"
    dpr = image.devicePixelRatio()
    expected = (round(WIDTH * dpr), round(HEIGHT * dpr))
    assert (image.width(), image.height()) == expected, (
        f"размер {(image.width(), image.height())}, ожидался {expected}, DPR={dpr}"
    )
    first = image.pixel(0, 0)
    assert any(
        image.pixel(x, y) != first for y in range(image.height()) for x in range(image.width())
    ), "снимок залит одним цветом"


def settle_pointer(app: Any, window: Any) -> None:
    """Уводит указатель в угол, где нет ни одного элемента с наведением.

    Под Xvfb настоящий курсор стоит в центре экрана, то есть внутри окна:
    карточка или кнопка под ним подсвечивается наведением, а момент, когда X
    доставит это событие окну, снимку не подвластен — два кадра одного и того
    же экрана расходятся побайтно. Синтетическое перемещение задаёт последнюю
    известную позицию мыши явно и одинаково для всех кадров.
    """
    QTest.mouseMove(window, QPoint(1, HEIGHT - 2))
    app.processEvents()
    QTest.qWait(20)
    app.processEvents()


def grab_frame(app: Any, window: QQuickWindow, case: str) -> QImage:
    """Общий захват QQuickView и ApplicationWindow с повторами для Qt5/offscreen."""
    grab = None
    for attempt in range(1, 21):
        if attempt > 1:
            QTest.qWait(25)
            app.processEvents()
        image = window.grabWindow()
        if image.isNull():
            # Qt5/offscreen может не реализовывать grabWindow даже после show.
            if grab is None:
                grab = window.contentItem().grabToImage()
            if grab is not None:
                image = grab.image()
                if not image.isNull():
                    grab = None
        if not image.isNull():
            dpr = image.devicePixelRatio()
            if (image.width(), image.height()) == (
                round(WIDTH * dpr),
                round(HEIGHT * dpr),
            ):
                break
    else:
        pytest.fail(
            f"{case}: снимок не готов после {attempt} попыток; "
            f"размер {(image.width(), image.height())}, DPR={image.devicePixelRatio()}"
        )
    assert_frame(image)
    return image


def render_onboarding(
    app: Any,
    fake: FakeOnboarding,
    dark: bool,
    *,
    extra_wait_ms: int = 0,
    inspect: Callable[[Any], None] | None = None,
) -> tuple[QImage, list[str]]:
    """Снимает настоящее окно, перехватывая Qt вплоть до удаления окна и мостов."""
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    view = QQuickView()
    install_icon_provider(view.engine())
    theme = FakeTheme(dark)
    root: Any = None
    try:
        view.rootContext().setContextProperty("onboarding", fake)
        view.rootContext().setContextProperty("themeSource", theme)
        view.setResizeMode(QQuickView.SizeRootObjectToView)
        view.resize(WIDTH, HEIGHT)
        view.setSource(QUrl.fromLocalFile(str(REPO / "qml/onboarding/Onboarding.qml")))
        assert view.status() == QQuickView.Ready, [error.toString() for error in view.errors()]
        root = view.rootObject()
        assert root is not None
        assert root.setProperty("freezeAnimations", True), (
            "в QML нет свойства freezeAnimations — заморозка анимаций не сработала"
        )
        assert root.property("freezeAnimations") is True
        view.show()
        QTest.qWait(180 + extra_wait_ms)
        app.processEvents()
        assert view.status() == QQuickView.Ready, [error.toString() for error in view.errors()]
        assert root.property("step") == fake.step
        assert any(
            item.isVisible() and item.property("text") == f"Шаг {fake.step} из 5"
            for item in visual_tree(root)
        ), f"не показан шаг {fake.step}"
        assert view.isVisible() and view.isExposed()

        settle_pointer(app, view)
        image = grab_frame(app, view, f"step={fake.step}, dark={dark}")
        if inspect is not None:
            inspect(root)
            app.processEvents()
        return image, messages
    finally:
        # Контекстные объекты живут дольше движка; сообщения деструкторов тоже учитываются.
        try:
            sip.delete(view)
            sip.delete(theme)
            app.processEvents()
        finally:
            root = None
            qInstallMessageHandler(previous)


def render_settings(
    app: Any,
    dark: bool,
    *,
    fake: FakeSettings | None = None,
    section: str = "general",
    extra_wait_ms: int = 0,
    inspect: Callable[[Any], None] | None = None,
) -> tuple[QImage, list[str]]:
    """Загружает настоящий Main.qml без appInfo и открывает нужный раздел."""
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    theme = FakeTheme(dark)
    settings = fake if fake is not None else FakeSettings()
    roots: list[Any] = []
    window: Any = None
    background_component: Any = None
    background: Any = None
    try:
        engine.rootContext().setContextProperty("themeSource", theme)
        engine.rootContext().setContextProperty("showOnboarding", False)
        engine.rootContext().setContextProperty("settingsBridge", settings)
        engine.load(QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
        roots = engine.rootObjects()
        assert len(roots) == 1, messages
        window = roots[0]
        assert isinstance(window, QQuickWindow)
        window.setWidth(WIDTH)
        window.setHeight(HEIGHT)
        assert window.setProperty("freezeAnimations", True), (
            "в Main.qml нет свойства freezeAnimations — заморозка анимаций не сработала"
        )
        assert window.property("freezeAnimations") is True
        # Qt5/software grabToImage не включает цвет очистки QQuickWindow.
        # Повторяем его под содержимым, чтобы запасной захват сохранил фон окна.
        background_component = QQmlComponent(engine)
        background_component.setData(
            b"import QtQuick 2.15; Rectangle { anchors.fill: parent; z: -1 }", QUrl()
        )
        background = background_component.create()
        assert background is not None, [error.toString() for error in background_component.errors()]
        background.setProperty("color", window.color())
        background.setParent(window.contentItem())
        background.setParentItem(window.contentItem())
        window.show()
        QTest.qWait(180 + extra_wait_ms)
        app.processEvents()
        assert window.isVisible() and window.isExposed()
        assert (window.width(), window.height()) == (WIDTH, HEIGHT)
        assert window.property("onboardingVisible") is False
        description = (
            section_descriptions(window)[0]
            if section == "general"
            else select_section(app, window, section)
        )
        assert description["key"] == section
        # Шапка берёт заголовок и подзаголовок из описания раздела (§1.4).
        texts = visible_texts(window.contentItem())
        assert {description["title"], description["subtitle"]} <= texts, (
            f"не показан раздел «{description['title']}»"
        )
        settle_pointer(app, window)
        image = grab_frame(app, window, snapshot_name(f"settings-{section}", dark))
        if inspect is not None:
            inspect(window)
            app.processEvents()
        return image, messages
    finally:
        # Движок владеет окном; контекстные объекты удаляются после него.
        try:
            sip.delete(engine)
            if fake is None:
                sip.delete(settings)
            sip.delete(theme)
            app.processEvents()
        finally:
            window = None
            background = None
            background_component = None
            roots.clear()
            qInstallMessageHandler(previous)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_section_frame_has_no_card_icons_after_models_scroll(
    onboarding_app: Any, dark: bool
) -> None:
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    theme = FakeTheme(dark)
    settings = FakeSettings()
    settings.models = [
        *settings.models,
        *[
            {**copy.deepcopy(settings.models[1]), "id": f"extra-{i}", "name": f"T-one {i}"}
            for i in range(10)
        ],
    ]
    app = onboarding_app
    try:
        engine.rootContext().setContextProperty("themeSource", theme)
        engine.rootContext().setContextProperty("showOnboarding", False)
        engine.rootContext().setContextProperty("settingsBridge", settings)
        engine.load(QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
        roots = engine.rootObjects()
        assert len(roots) == 1, messages
        window = roots[0]
        assert isinstance(window, QQuickWindow)
        window_height = 588
        window.setWidth(1024)
        window.setHeight(window_height)
        assert window.setProperty("freezeAnimations", True)
        window.show()
        QTest.qWait(250)
        app.processEvents()
        assert window.isVisible() and window.isExposed()
        body = next(
            item
            for item in visual_tree(window.contentItem())
            if item.metaObject().className() == "QQuickFlickable"
        )
        top = round(body.mapToScene(QPointF(0, 0)).y())
        bottom = top + round(body.height())

        def capture() -> QImage:
            QTest.qWait(80)
            app.processEvents()
            # QQuickWindow.grabWindow() рисует кадр заново и прячет дефект:
            # он остаётся только в уже нарисованном буфере окна.
            screen = QGuiApplication.primaryScreen()
            assert screen is not None
            image = screen.grabWindow(window.winId()).toImage()
            if image.isNull():
                message = "Qt/offscreen не отдал буфер окна"
                if (
                    os.environ.get("QT_QPA_PLATFORM") == "xcb"
                    or os.environ.get("ASTRA_VOICE_REQUIRE_QT") == "1"
                ):
                    pytest.fail(message)
                pytest.skip(message)
            first = image.pixel(0, 0)
            if not any(
                image.pixel(x, y) != first
                for y in range(image.height())
                for x in range(image.width())
            ):
                message = "Qt/offscreen отдал одноцветный буфер окна"
                if (
                    os.environ.get("QT_QPA_PLATFORM") == "xcb"
                    or os.environ.get("ASTRA_VOICE_REQUIRE_QT") == "1"
                ):
                    pytest.fail(message)
                pytest.skip(message)
            return image

        def settle_local_pointer() -> None:
            QTest.mouseMove(window, QPoint(1, window.height() - 2))
            app.processEvents()
            QTest.qWait(20)
            app.processEvents()

        def strip(image: QImage, y0: int, y1: int) -> QImage:
            dpr = image.devicePixelRatio()
            start, end = round(y0 * dpr), round(y1 * dpr)
            assert 0 <= start < end <= image.height(), (start, end, image.height())
            return image.copy(0, start, image.width(), end - start)

        def assert_same(actual: QImage, expected: QImage, label: str) -> None:
            assert actual.size() == expected.size()
            changed = sum(
                actual.pixel(x, y) != expected.pixel(x, y)
                for y in range(actual.height())
                for x in range(actual.width())
            )
            assert actual == expected, f"{label}: {changed} отличающихся пикселей"

        settle_local_pointer()
        clean = capture()
        select_section(app, window, "models")
        models_top = capture()
        maximum = max(float(body.property("contentHeight")) - body.height(), 0.0)
        assert maximum > 0, "раздел моделей не прокручивается"
        for y in [*range(0, int(maximum), 12), maximum]:
            body.setProperty("contentY", y)
            QTest.qWait(16)
            app.processEvents()
        models_scrolled = capture()
        select_section(app, window, "general")
        settle_local_pointer()
        after = capture()

        assert_same(strip(after, 0, top), strip(clean, 0, top), "шапка после прокрутки")
        assert_same(
            strip(after, bottom, window.height()),
            strip(clean, bottom, window.height()),
            "строка состояния после прокрутки",
        )
        assert_same(
            strip(models_scrolled, 0, top),
            strip(models_top, 0, top),
            "шапка раздела моделей после прокрутки",
        )
        assert_no_messages(messages, "раздел после прокрутки моделей")
    finally:
        try:
            sip.delete(engine)
            sip.delete(settings)
            sip.delete(theme)
            app.processEvents()
        finally:
            qInstallMessageHandler(previous)


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_menu_and_show_section_switch_real_sections(onboarding_app: Any, dark: bool) -> None:
    """Каждый пункт меню открывает свой раздел; из Python переход делает showSection."""
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    app_info = FakeAppInfo()
    theme = FakeTheme(dark)
    settings = FakeSettings()
    try:
        engine.rootContext().setContextProperty("appInfo", app_info)
        engine.rootContext().setContextProperty("themeSource", theme)
        engine.rootContext().setContextProperty("showOnboarding", False)
        engine.rootContext().setContextProperty("settingsBridge", settings)
        engine.load(QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
        roots = engine.rootObjects()
        assert len(roots) == 1, messages
        window = roots[0]
        assert isinstance(window, QQuickWindow)
        window.setWidth(WIDTH)
        window.setHeight(HEIGHT)
        window.show()
        QTest.qWait(180)
        onboarding_app.processEvents()
        sidebar = settings_sidebar(window)
        descriptions = section_descriptions(window)
        assert [description["key"] for description in descriptions] == [
            "general",
            "models",
            "output",
            "network",
            "advanced",
            "about",
        ]
        assert window.property("sectionIndices").toVariant() == {
            **{description["key"]: index for index, description in enumerate(descriptions)},
            "debug": len(descriptions),
        }
        subtitles = {description["subtitle"] for description in descriptions}
        assert len(subtitles) == len(descriptions), "подзаголовки разделов не различаются"

        assert sidebar.property("currentIndex") == 0
        for index, description in enumerate(descriptions):
            assert select_section(onboarding_app, window, description["key"]) == description
            assert sidebar.property("currentIndex") == index
            texts = visible_texts(window.contentItem())
            # Заголовок и подзаголовок шапки — из описания раздела; чужих нет.
            assert {description["title"], description["subtitle"]} <= texts
            assert not (subtitles - {description["subtitle"]}) & texts

        def show_section(section: str, expected_index: int) -> set[str]:
            window.hide()
            assert not window.isVisible()
            app_info.showSection.emit(section)
            onboarding_app.processEvents()
            assert window.isVisible()
            assert sidebar.property("currentIndex") == expected_index
            QTest.qWait(220)
            onboarding_app.processEvents()
            return visible_texts(window.contentItem())

        texts = show_section("models", 1)
        assert {"Модели", "Какая модель распознаёт речь", "Скачать выбранное"} <= texts
        # Неизвестный ключ выделение не меняет.
        assert show_section("unknown", 1) == texts

        texts = show_section("general", 0)
        assert {"Общие", "Диктовка, индикация и запуск", "Горячая клавиша"} <= texts
        assert "Модель распознавания" not in texts

        texts = show_section("debug", len(descriptions))
        assert sidebar.property("debugCurrent") is True
        assert {
            "Отладка",
            "Скрытый раздел: Ctrl + Shift + D",
            "Здесь будут сведения для поддержки",
        } <= texts
        assert not subtitles & texts
    finally:
        try:
            sip.delete(engine)
            sip.delete(app_info)
            sip.delete(settings)
            sip.delete(theme)
            onboarding_app.processEvents()
        finally:
            qInstallMessageHandler(previous)

    # Qt/offscreen не реализует raise(); все остальные сообщения остаются ошибками.
    assert_no_messages(
        [message for message in messages if message != "This plugin does not support raise()"],
        "showSection",
    )


def render_case(
    app: Any, name: str, dark: bool, *, extra_wait_ms: int = 0
) -> tuple[QImage, list[str]]:
    fake = SNAPSHOT_SETUPS[name]()
    if isinstance(fake, FakeSettings):
        return render_settings(
            app,
            dark,
            fake=fake,
            section=SNAPSHOT_SECTIONS.get(name, "general"),
            extra_wait_ms=extra_wait_ms,
        )
    return render_onboarding(app, fake, dark, extra_wait_ms=extra_wait_ms)


def assert_no_messages(messages: list[str], case: str) -> None:
    assert not messages, f"{case}: сообщения Qt\n" + "\n".join(messages)


def png_bytes(image: QImage) -> bytes:
    """Кодирует второй рендер в памяти, не публикуя и не записывая его на диск."""
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.WriteOnly)
    try:
        assert image.save(buffer, "PNG"), "не удалось закодировать PNG"
    finally:
        buffer.close()
    return bytes(data)


def assert_saved_snapshot(path: Path, original: QImage) -> None:
    assert path.is_file(), f"отсутствует PNG: {path}"
    image = QImage(str(path))
    assert not image.isNull(), f"повреждён PNG: {path}"
    # PNG может не сохранять DPR; сравниваем физические размеры с исходным кадром.
    assert (image.width(), image.height()) == (original.width(), original.height()), (
        f"{path}: неверный размер PNG"
    )


@pytest.fixture(scope="module", autouse=True)
def rendered_steps(
    onboarding_app: Any, request: pytest.FixtureRequest
) -> Iterator[dict[tuple[str, bool], tuple[QImage, list[str], Path]]]:
    """Готовит кадры всех случаев; публикует их только после успеха тестов модуля."""
    failures_before = request.session.testsfailed
    SNAPSHOTS.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".onboarding-", dir=SNAPSHOTS) as temporary:
        staging = Path(temporary)
        rendered: dict[tuple[str, bool], tuple[QImage, list[str], Path]] = {}
        for name, dark in CASES:
            image, messages = render_case(onboarding_app, name, dark)
            filename = snapshot_name(name, dark)
            assert_no_messages(messages, filename)
            path = staging / filename
            assert image.save(str(path), "PNG"), f"не удалось сохранить {path}"
            assert_saved_snapshot(path, image)
            rendered[name, dark] = image, messages, path
        yield rendered
        if request.session.testsfailed == failures_before:
            for image, messages, path in rendered.values():
                assert_no_messages(messages, path.name)
                assert_saved_snapshot(path, image)
            for _, _, path in rendered.values():
                os.replace(path, SNAPSHOTS / path.name)
            for image, _, path in rendered.values():
                assert_saved_snapshot(SNAPSHOTS / path.name, image)
            for step in range(1, 6):
                for theme in ("light", "dark"):
                    (SNAPSHOTS / f"step{step}-{theme}.png").unlink(missing_ok=True)


@pytest.mark.parametrize("name", SNAPSHOT_SETUPS)
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_steps_render_without_warnings(
    name: str,
    dark: bool,
    rendered_steps: dict[tuple[str, bool], tuple[QImage, list[str], Path]],
) -> None:
    image, messages, _ = rendered_steps[name, dark]
    assert_no_messages(messages, snapshot_name(name, dark))
    assert_frame(image)


def test_step_snapshots_are_saved(
    rendered_steps: dict[tuple[str, bool], tuple[QImage, list[str], Path]],
) -> None:
    """Проверяет подготовленные PNG; атомарная публикация отложена до teardown."""
    assert set(rendered_steps) == set(CASES)
    expected = {snapshot_name(name, dark) for name, dark in CASES}
    assert len(expected) == 24
    paths = [path for _, _, path in rendered_steps.values()]
    assert {path.name for path in paths[0].parent.glob("*.png")} == expected
    for image, messages, path in rendered_steps.values():
        assert_no_messages(messages, path.name)
        assert_saved_snapshot(path, image)


@pytest.mark.parametrize("name", SNAPSHOT_SETUPS)
@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_snapshots_are_deterministic(
    name: str,
    dark: bool,
    onboarding_app: Any,
    rendered_steps: dict[tuple[str, bool], tuple[QImage, list[str], Path]],
) -> None:
    _, _, first = rendered_steps[name, dark]
    second, messages = render_case(onboarding_app, name, dark, extra_wait_ms=250)
    assert_no_messages(messages, first.name)
    first_hash = sha256(first.read_bytes()).hexdigest()
    second_hash = sha256(png_bytes(second)).hexdigest()
    assert first_hash == second_hash, (
        f"{first.name}: PNG зависит от времени съёмки: {first_hash} != {second_hash}"
    )


@pytest.mark.parametrize("state", MODEL_STATES)
def test_model_card_states_render(state: str, onboarding_app: Any) -> None:
    fake = FakeOnboarding()
    fake.step = 2
    installed = state in {
        "installed",
        "switching",
        "custom",
        "broken",
        "corrupted",
        "update-available",
        "updating",
        "update-failed",
        "removed-from-catalog",
    }
    fake.models = [
        {
            **fake.models[0],
            "state": state,
            "badge": "installed" if installed else "",
            "message": (
                "Не хватает места на диске"
                if state == "paused-no-space"
                else "Не удалось загрузить модель — сервер не отвечает"
                if state == "failed"
                else ""
            ),
        },
        *fake.models[1:],
    ]

    def inspect(root: Any) -> None:
        card = next(item for item in visual_tree(root) if item.property("modelId"))
        expected = (
            "blocked"
            if state in {"no-space", "no-network", "offline-user", "policy"}
            else "locked"
            if installed or state in {"queued", "downloading", "verifying", "paused-no-space"}
            else "off"
        )
        assert card.property("selectionMark") == expected
        alert = next(item for item in visual_tree(card) if item.objectName() == "messageAlert")
        message = next(item for item in visual_tree(card) if item.objectName() == "messageText")
        error_states = {
            "failed",
            "sha-failed",
            "no-space",
            "paused-no-space",
            "broken",
            "corrupted",
            "update-failed",
        }
        neutral_states = {"no-network", "offline-user", "policy"}
        hint_states = {"low-ram", "not-recommended", "no-benchmark"}
        assert alert.isVisible() == (state in error_states)
        assert message.isVisible() == (state in error_states | neutral_states)
        assert card.property("showHint") == (state in hint_states)
        if state in error_states:
            assert alert.property("name") == "alert"
            assert message.property("color") == alert.property("color")
        if state in error_states | neutral_states:
            assert message.property("text") in visible_texts(card)
        if state in {"failed", "paused-no-space"}:
            assert message.property("text") == fake.models[0]["message"]
        if state == "paused-no-space":
            assert card.property("highlighted") is True
            assert card.property("statusLabel") == ""
        expected_buttons = {
            "queued": {"Отмена"},
            "downloading": {"Отмена"},
            "paused-no-space": {"Повторить", "Отмена"},
            "failed": {"Повторить"},
            "sha-failed": set(),
            "no-space": {"Открыть папку моделей"},
        }.get(state, set())
        buttons = {
            item.property("text")
            for item in visual_tree(card)
            if item.isVisible() and item.metaObject().indexOfSignal(b"clicked()") >= 0
        }
        assert buttons == expected_buttons
        if state == "sha-failed":
            assert card.property("detailsActionAvailable") is False
        if state in {"updating", "update-failed"}:
            assert card.property("updateActionsAvailable") is False

    _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    assert_no_messages(messages, f"models[0].state={state}")


@pytest.mark.parametrize("screen", ["onboarding", "models"])
@pytest.mark.parametrize(
    ("state", "button", "call", "ids_attribute"),
    [
        ("queued", "Отмена", "dequeueModel", "dequeued_model_ids"),
        ("downloading", "Отмена", "cancelModel", "cancelled_model_ids"),
        ("paused-no-space", "Повторить", "retryModel", "retried_model_ids"),
        ("failed", "Повторить", "retryModel", "retried_model_ids"),
    ],
)
def test_download_card_actions_target_model(
    onboarding_app: Any, screen: str, state: str, button: str, call: str, ids_attribute: str
) -> None:
    fake = FakeOnboarding() if screen == "onboarding" else FakeSettings()
    if isinstance(fake, FakeOnboarding):
        fake.step = 2
    model_id = "target-download-model"
    fake.models = [
        {
            **fake.models[0],
            "id": model_id,
            "badge": "",
            "state": state,
            "message": "Не хватает места на диске" if state == "paused-no-space" else "",
        }
    ]

    def inspect(root: Any) -> None:
        card = next(item for item in visual_tree(root) if item.property("modelId") == model_id)
        QMetaObject.invokeMethod(visible_button(card, button), "clicked", Qt.DirectConnection)
        assert fake.calls[-1] == call
        assert getattr(fake, ids_attribute) == [model_id]
        assert "cancelDownloads" not in fake.calls

    if screen == "onboarding":
        _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    else:
        _, messages = render_settings(
            onboarding_app,
            False,
            fake=fake,
            section="models",
            inspect=lambda window: inspect(window.contentItem()),
        )
    assert_no_messages(messages, f"{screen} {state} action")


@pytest.mark.parametrize("screen", ["onboarding", "models"])
@pytest.mark.parametrize(
    ("state", "flag", "button"),
    [
        ("queued", "canDequeue", "Отмена"),
        ("downloading", "canCancel", "Отмена"),
        ("paused-no-space", "canCancel", "Отмена"),
        ("paused-no-space", "canRetry", "Повторить"),
        ("failed", "canRetry", "Повторить"),
    ],
)
def test_download_card_bridge_can_hide_action(
    onboarding_app: Any, screen: str, state: str, flag: str, button: str
) -> None:
    fake = FakeOnboarding() if screen == "onboarding" else FakeSettings()
    if isinstance(fake, FakeOnboarding):
        fake.step = 2
    fake.models = [
        {
            **fake.models[0],
            "badge": "",
            "state": state,
            flag: False,
        }
    ]

    def inspect(root: Any) -> None:
        card = next(item for item in visual_tree(root) if item.property("modelId"))
        assert button not in visible_texts(card)

    if screen == "onboarding":
        _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    else:
        _, messages = render_settings(
            onboarding_app,
            False,
            fake=fake,
            section="models",
            inspect=lambda window: inspect(window.contentItem()),
        )
    assert_no_messages(messages, f"{screen} {state} {flag}")


@pytest.mark.parametrize("screen", ["onboarding", "models"])
def test_available_card_stays_selectable_while_downloading(
    onboarding_app: Any, screen: str
) -> None:
    fake = FakeOnboarding() if screen == "onboarding" else FakeSettings()
    if isinstance(fake, FakeOnboarding):
        fake.step = 2
    fake.downloadState = "downloading"
    fake.models = [{**fake.models[0], "badge": "", "state": "available", "selected": True}]
    fake.selectionSummary = "Будет скачано 226 МБ"

    def inspect(root: Any) -> None:
        card = next(item for item in visual_tree(root) if item.property("modelId"))
        assert card.property("selectionAvailable") is True
        click_item(card)
        assert fake.toggled_model_ids == [fake.models[0]["id"]]
        if screen == "models":
            button = visible_button(root, "Скачать выбранное")
            assert button.isEnabled()
            QMetaObject.invokeMethod(button, "clicked", Qt.DirectConnection)
            assert fake.calls[-1] == "startSelectedDownloads"

    if screen == "onboarding":
        _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    else:
        _, messages = render_settings(
            onboarding_app,
            False,
            fake=fake,
            section="models",
            inspect=lambda window: inspect(window.contentItem()),
        )
    assert_no_messages(messages, f"{screen} selectable during download")


@pytest.mark.parametrize("screen", ["onboarding", "models"])
def test_download_strip_shows_source(onboarding_app: Any, screen: str) -> None:
    fake = FakeOnboarding() if screen == "onboarding" else FakeSettings()
    if isinstance(fake, FakeOnboarding):
        fake.step = 2
    fake.downloadState = "downloading"
    fake.downloadTitle = "Загружаю GigaAM v3 RNN-T"
    fake.downloadSource = "Скачиваю с huggingface.co"
    fake.speed = "5,2 МБ/с"
    fake.eta = "осталось ~3 мин"

    def inspect(root: Any) -> None:
        strip = next(
            item
            for item in visual_tree(root)
            if item.metaObject().indexOfProperty("downloadState") >= 0
        )
        assert strip.property("sourceText") == fake.downloadSource
        assert strip.property("tail") == ("Скачиваю с huggingface.co · 5,2 МБ/с · осталось ~3 мин")
        assert strip.property("tail") in visible_texts(strip)

    if screen == "onboarding":
        _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    else:
        _, messages = render_settings(
            onboarding_app,
            False,
            fake=fake,
            section="models",
            inspect=lambda window: inspect(window.contentItem()),
        )
    assert_no_messages(messages, f"{screen} download source")


def test_download_card_state_frames(onboarding_app: Any) -> None:
    from astra_voice.ui.model_downloads import _FAILURE_MESSAGES, _SOURCE_TEXT

    destination = os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR_M6DL")
    if not destination:
        pytest.skip("ASTRA_VOICE_SNAPSHOT_DIR_M6DL не задан")
    snapshots = Path(destination)
    snapshots.mkdir(parents=True, exist_ok=True)
    states = ("queued", "downloading", "downloading-calc", "verifying", "paused-no-space", "failed")
    paused_message = "Не хватает места — освободите 172 МБ"
    for state in states:
        for screen in ("onboarding",) if state == "downloading-calc" else ("onboarding", "models"):
            fake = FakeOnboarding() if screen == "onboarding" else FakeSettings()
            if isinstance(fake, FakeOnboarding):
                fake.step = 2
                fake.canContinueFromModel = state != "failed"
            t_one = copy.deepcopy(FakeSettings().models[1])
            t_one.update(
                id="t-one-fp32",
                description="Русская диктовка от Т-Банка. Знаки препинания не ставит",
                sizeBytes=144_195_192,
                ramText="267 МБ",
                ramMb=267,
                tags=["Только русский", "Apache-2.0", "отечественная"],
            )
            t_one["metrics"][1].update(text="", fill=0.0, hasData=False)
            fake.models = [
                {
                    **fake.models[0],
                    "badge": "",
                    "selected": state != "failed",
                    "state": "downloading" if state == "downloading-calc" else state,
                    "message": paused_message
                    if state == "paused-no-space"
                    else _FAILURE_MESSAGES["timeout"]
                    if state == "failed"
                    else "",
                    "progress": 0.42 if state in {"downloading", "downloading-calc"} else 0.0,
                    "canCancel": state in {"downloading", "downloading-calc", "paused-no-space"},
                    "canRetry": state in {"paused-no-space", "failed"},
                    "canDequeue": state == "queued",
                }
            ]
            if state == "queued":
                fake.models.append(
                    {
                        **t_one,
                        "selected": True,
                        "state": "queued",
                        "message": "",
                        "canDequeue": True,
                    }
                )
                fake.models[0] = {**fake.models[0], "state": "downloading", "canCancel": True}
            fake.downloadState = (
                "no-space"
                if state == "paused-no-space"
                else "downloading"
                if state in {"queued", "downloading-calc"}
                else state
                if state in {"downloading", "verifying", "failed"}
                else "idle"
            )
            fake.downloadTitle = {
                "queued": "Загружается GigaAM v3 RNN-T",
                "downloading": "Загружается GigaAM v3 RNN-T",
                "downloading-calc": "Загружается GigaAM v3 RNN-T",
                "verifying": "Проверяю модель…",
                "paused-no-space": "Не хватает места на диске",
                "failed": "Не удалось загрузить модель",
            }[state]
            fake.downloadCounter = "1 из 2" if state == "queued" else ""
            fake.downloadProgress = 0.42 if state in {"downloading", "downloading-calc"} else 0.0
            fake.downloadSource = (
                _SOURCE_TEXT["hf"] if state in {"downloading", "downloading-calc", "queued"} else ""
            )
            fake.downloadDetail = "нужно ещё 172 МБ" if state == "paused-no-space" else ""
            fake.freeSpaceText = (
                "свободно на диске 100 МБ"
                if state == "paused-no-space"
                else "свободно на диске 42,1 ГБ"
            )
            fake.speed = "5,2 МБ/с" if state == "downloading" else ""
            fake.eta = "осталось ~3 мин" if state == "downloading" else ""

            def inspect(
                root: Any, state: str = state, screen: str = screen, fake: Any = fake
            ) -> None:
                cards = [
                    item
                    for item in visual_tree(root)
                    if item.isVisible() and item.property("modelId")
                ]
                card = cards[0]
                summary = next(
                    item for item in visual_tree(root) if item.objectName() == "selectionSummary"
                )
                assert summary.property("text") == (
                    "Пока ничего не выбрано"
                    if state == "failed"
                    else "Загрузка на паузе"
                    if state == "paused-no-space"
                    else "Идёт загрузка"
                )
                if state == "paused-no-space":
                    assert (
                        sum(
                            item.property("text").count(fake.freeSpaceText)
                            for item in visual_tree(summary.parentItem())
                            if item.isVisible() and isinstance(item.property("text"), str)
                        )
                        == 1
                    )
                assert card.property("selected") is (state != "failed")
                if state in {
                    "queued",
                    "downloading",
                    "downloading-calc",
                    "verifying",
                    "paused-no-space",
                }:
                    badge = next(
                        item for item in visual_tree(card) if item.property("text") == "Рекомендуем"
                    )
                    assert badge.property("color") == theme_color("bgSurface", False)
                action = (
                    "Повторить"
                    if state == "failed"
                    else "Отмена"
                    if state in {"queued", "downloading", "downloading-calc", "paused-no-space"}
                    else "Отмена недоступна"
                )
                item = next(
                    item
                    for item in visual_tree(card)
                    if item.isVisible() and item.property("text") == action
                )
                right = item.mapToScene(QPointF(item.width(), 0)).x()
                card_right = card.mapToScene(QPointF(card.width(), 0)).x()
                actions = next(
                    item for item in visual_tree(card) if item.objectName() == "footerActions"
                )
                actions_right = actions.mapToScene(QPointF(actions.width(), 0)).x()
                assert 11 <= card_right - right <= 15, (
                    state,
                    screen,
                    card_right - right,
                    card_right - actions_right,
                    actions.width(),
                    item.width(),
                )
                if state == "verifying":
                    assert item.property("color") == theme_color("fgMuted", False)
                strip = next(
                    item
                    for item in visual_tree(root)
                    if item.property("downloadState") == fake.downloadState
                    and item.property("title") == fake.downloadTitle
                )
                counter = next(
                    item for item in visual_tree(strip) if item.objectName() == "downloadCounter"
                )
                detail = next(
                    item for item in visual_tree(strip) if item.objectName() == "downloadDetail"
                )
                assert counter.isVisible() == (state == "queued")
                assert detail.isVisible() == (state == "paused-no-space")
                if state == "queued":
                    assert counter.property("text") == "1 из 2"
                    assert {"GigaAM v3 RNN-T", "T-one"} <= visible_texts(root)
                    title = next(
                        item
                        for item in visual_tree(strip)
                        if item.property("text") == fake.downloadTitle
                    )
                    gap = (
                        counter.mapToScene(QPointF(0, 0)).x()
                        - title.mapToScene(QPointF(title.width(), 0)).x()
                    )
                    assert 9 <= gap <= 11
                if state == "paused-no-space":
                    assert detail.property("text") == "нужно ещё 172 МБ"
                    assert paused_message in visible_texts(card)
                    assert fake.freeSpaceText in visible_texts(root)
                    title = next(
                        item
                        for item in visual_tree(strip)
                        if item.property("text") == fake.downloadTitle
                    )
                    gap = (
                        detail.mapToScene(QPointF(0, 0)).x()
                        - title.mapToScene(QPointF(title.width(), 0)).x()
                    )
                    assert 9 <= gap <= 11
                    button = visible_button(strip, "Открыть папку моделей")
                    assert (
                        detail.mapToScene(QPointF(detail.width(), 0)).x()
                        < button.mapToScene(QPointF(0, 0)).x()
                    )
                if state == "downloading-calc":
                    assert strip.property("tail") == "Скачиваю с huggingface.co · считаю…"
                    assert strip.property("tail") in visible_texts(strip)
                if screen == "onboarding" and state != "failed":
                    assert visible_button(root, "Продолжить").isEnabled()
                    assert not any(
                        text.startswith("Выберите хотя бы одну модель")
                        for text in visible_texts(root)
                    )

            if screen == "onboarding":
                image, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
            else:

                def inspect_settings(
                    window: Any, callback: Callable[[Any], None] = inspect
                ) -> None:
                    callback(window.contentItem())

                image, messages = render_settings(
                    onboarding_app,
                    False,
                    fake=fake,
                    section="models",
                    inspect=inspect_settings,
                )
            filename = f"m6dl-{screen}-{state}.png"
            assert_no_messages(messages, filename)
            path = snapshots / filename
            assert image.save(str(path), "PNG"), filename
            assert_saved_snapshot(path, image)


@pytest.mark.parametrize(
    "message",
    [
        "Не удалось удалить модель. Попробуйте ещё раз.",
        "Модель установлена. Сделать её рабочей не удалось — попробуйте переустановить.",
        "Не удалось загрузить модель. Рабочая модель не изменилась.",
        "Модель не прошла проверку. Попробуйте скачать или установить её заново.",
        "Издатель больше не рекомендует эту версию модели.",
    ],
)
def test_healthy_card_message_has_alert(onboarding_app: Any, message: str) -> None:
    fake = FakeOnboarding()
    fake.step = 2
    fake.models = [{**fake.models[0], "message": message}, *fake.models[1:]]

    def inspect(root: Any) -> None:
        card = next(item for item in visual_tree(root) if item.property("modelId"))
        alert = next(item for item in visual_tree(card) if item.objectName() == "messageAlert")
        text = next(item for item in visual_tree(card) if item.objectName() == "messageText")
        assert alert.isVisible() and alert.property("name") == "alert"
        assert text.isVisible() and text.property("text") == message
        assert text.property("color") == alert.property("color")

    _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    assert_no_messages(messages, "healthy card error message")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_rounded_metric_levels_render(onboarding_app: Any, dark: bool) -> None:
    from astra_voice.ui.formatting import _shown
    from astra_voice.ui.model_downloads import _level

    quality_level = _level("quality", _shown(Decimal(100) - Decimal("8.04")))
    speed_level = _level("speed", _shown(19.96))
    assert quality_level == speed_level == "good"
    fake = FakeOnboarding()
    fake.step = 2
    metrics = [dict(row) for row in fake.models[0]["metrics"]]
    metrics[0]["level"] = quality_level
    metrics[1]["level"] = speed_level
    fake.models = [{**fake.models[0], "metrics": metrics}]

    def inspect(root: Any) -> None:
        card = next(item for item in visual_tree(root) if item.property("modelId"))
        fills = [
            item
            for item in visual_tree(card)
            if item.objectName() == "metricFill" and item.isVisible()
        ]
        assert len(fills) == 2
        assert all(
            fill.property("color") == theme_color("modelCardMetricFillGood", dark) for fill in fills
        )

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "rounded metric levels")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_metric_labels_fit_their_field(onboarding_app: Any, dark: bool) -> None:
    fake = FakeOnboarding()
    fake.step = 2

    def inspect(root: Any) -> None:
        card = next(item for item in visual_tree(root) if item.property("modelId"))
        for caption in ("Точность", "Скорость"):
            labels = [
                item
                for item in visual_tree(card)
                if item.isVisible() and item.property("text") == caption
            ]
            assert len(labels) == 1, caption
            label = labels[0]
            assert label.width() == theme_number("modelCardMetricLabelW")
            assert label.property("horizontalAlignment") == Qt.AlignRight
            assert label.property("font").pixelSize() == theme_number("fontMetricSize")
            content_width = float(label.property("contentWidth"))
            implicit_width = float(label.implicitWidth())
            assert max(content_width, implicit_width) - label.width() <= 2, (
                caption,
                content_width,
                implicit_width,
                label.width(),
            )

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "metric labels")


def test_compact_cards_fit_at_default_size(onboarding_app: Any, monkeypatch: Any) -> None:
    col_max = theme_number("sizeContentColMax")
    window_w = theme_number("sizeWindowW")
    sidebar_w = theme_number("sidebarW")
    body_inset = window_w - theme_number("onboardingStep2ContentW")

    def assert_cards(root: Any, count: int) -> None:
        surface = root.contentItem() if isinstance(root, QQuickWindow) else root
        cards = [
            item for item in visual_tree(surface) if item.isVisible() and item.property("modelId")
        ]
        assert len(cards) >= count
        for card in cards[:count]:
            left = card.mapToScene(QPointF(0, 0)).x()
            top = card.mapToScene(QPointF(0, 0)).y()
            assert top >= 0
            assert card.width() == min(col_max, WIDTH - body_inset)
            expected_left = (
                sidebar_w + (WIDTH - sidebar_w - card.width()) / 2
                if isinstance(root, QQuickWindow)
                else (WIDTH - card.width()) / 2
            )
            assert left == expected_left
            assert top + card.height() <= HEIGHT - 36, (top, card.height())

    onboarding = FakeOnboarding()
    onboarding.step = 2
    onboarding.models = [{**onboarding.models[0], "id": f"card-{index}"} for index in range(4)]
    image, messages = render_onboarding(
        onboarding_app, onboarding, False, inspect=lambda root: assert_cards(root, 4)
    )
    assert_frame(image)
    assert_no_messages(messages, "four model cards")

    settings = FakeSettings()
    settings.models = [
        *settings.models,
        {**settings.models[-1], "id": "third-model", "name": "Третья модель"},
    ]
    image, messages = render_settings(
        onboarding_app,
        False,
        fake=settings,
        section="models",
        inspect=lambda root: assert_cards(root, 3),
    )
    assert_frame(image)
    assert_no_messages(messages, "three model cards")

    monkeypatch.setattr(sys.modules[__name__], "WIDTH", 900)
    monkeypatch.setattr(sys.modules[__name__], "HEIGHT", 588)
    _, messages = render_onboarding(
        onboarding_app, onboarding, False, inspect=lambda root: assert_cards(root, 4)
    )
    assert_no_messages(messages, "four model cards at minimum width")
    _, messages = render_settings(
        onboarding_app,
        False,
        fake=settings,
        section="models",
        inspect=lambda root: assert_cards(root, 3),
    )
    assert_no_messages(messages, "three model cards at minimum width")

    monkeypatch.setattr(sys.modules[__name__], "WIDTH", 1200)
    monkeypatch.setattr(sys.modules[__name__], "HEIGHT", 620)
    _, messages = render_onboarding(
        onboarding_app, onboarding, False, inspect=lambda root: assert_cards(root, 4)
    )
    assert_no_messages(messages, "centered model cards in wide wizard")
    _, messages = render_settings(
        onboarding_app,
        False,
        fake=settings,
        section="models",
        inspect=lambda root: assert_cards(root, 3),
    )
    assert_no_messages(messages, "centered model cards in wide settings")


@pytest.mark.parametrize("window_width", [1024, 900])
@pytest.mark.parametrize("step", [1, 2, 3, 4, 5])
def test_onboarding_step_width_and_position(
    onboarding_app: Any, monkeypatch: Any, window_width: int, step: int
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "WIDTH", window_width)
    fake = FakeOnboarding()
    fake.step = step

    def inspect(root: Any) -> None:
        loader = next(item for item in visual_tree(root) if item.objectName() == "stepLoader")
        body_inset = theme_number("sizeWindowW") - theme_number("onboardingStep2ContentW")
        expected_width = (
            min(theme_number("onboardingStep2ContentW"), window_width - body_inset)
            if step == 2
            else 580
        )
        assert loader.width() == expected_width, (step, window_width, loader.width())
        assert loader.mapToScene(QPointF(0, 0)).x() == (window_width - expected_width) / 2
        if step == 2:
            assert "Пока ничего не выбрано" in visible_texts(root)
            assert "свободно на диске 42,1 ГБ" in visible_texts(root)

    _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    assert_no_messages(messages, f"step {step} at {window_width}")


def test_settings_card_action_edge_and_group_gap(onboarding_app: Any) -> None:
    fake = FakeSettings()

    def inspect(window: Any) -> None:
        root = window.contentItem()
        active = next(
            item
            for item in visual_tree(root)
            if item.isVisible() and item.property("modelId") == fake.models[0]["id"]
        )
        remove = visible_button(active, "Удалить")
        right = remove.mapToScene(QPointF(remove.width(), 0)).x()
        caption = next(
            item for item in visual_tree(root) if item.objectName() == "availableCaption"
        )
        card_bottom = active.mapToScene(QPointF(0, active.height())).y()
        caption_top = caption.mapToScene(QPointF(0, 0)).y()
        assert right == 988, right
        assert caption_top - card_bottom == theme_number("spaceGroupTopGap"), (
            caption_top,
            card_bottom,
        )

    _, messages = render_settings(
        onboarding_app, False, fake=fake, section="models", inspect=inspect
    )
    assert_no_messages(messages, "model card geometry")


@pytest.mark.parametrize("screen", ["models", "onboarding"])
def test_model_card_footer_aligns_with_name(onboarding_app: Any, screen: str) -> None:
    assert WIDTH == 1024

    def inspect(root: Any) -> None:
        surface = root.contentItem() if isinstance(root, QQuickWindow) else root
        cards = [
            item for item in visual_tree(surface) if item.isVisible() and item.property("modelId")
        ]
        assert cards
        for card in cards:
            name = next(item for item in visual_tree(card) if item.objectName() == "modelName")
            facts = next(item for item in visual_tree(card) if item.objectName() == "footerFacts")
            name_x = name.mapToScene(QPointF(0, 0)).x()
            facts_x = facts.mapToScene(QPointF(0, 0)).x()
            assert facts_x == name_x, (card.property("modelId"), facts_x, name_x)
            if screen == "models":
                assert facts_x == 249, (card.property("modelId"), facts_x)

    if screen == "models":
        _, messages = render_settings(
            onboarding_app, False, fake=FakeSettings(), section="models", inspect=inspect
        )
    else:
        fake = FakeOnboarding()
        fake.step = 2
        fake.models = [fake.models[0], {**fake.models[0], "id": "failed", "state": "failed"}]
        _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    assert_no_messages(messages, f"model card footer alignment: {screen}")


def test_settings_model_card_heights_and_action_edges(onboarding_app: Any) -> None:
    assert WIDTH == 1024
    fake = FakeSettings()
    models = fake.models
    fake.models = [
        models[0],
        {**models[0], "id": "second-installed", "badge": "installed", "selected": False},
        *models[1:],
    ]

    def inspect(window: Any) -> None:
        cards = {
            item.property("modelId"): item
            for item in visual_tree(window.contentItem())
            if item.isVisible() and item.property("modelId")
        }
        expected_heights = {
            models[0]["id"]: 92,
            "second-installed": 105,
            models[1]["id"]: 82,
        }
        heights = {model_id: card.height() for model_id, card in cards.items()}
        assert set(cards) == set(expected_heights), heights
        for model_id, expected_height in expected_heights.items():
            card = cards[model_id]
            assert card.width() == 796, f"{model_id}: width={card.width()}, heights={heights}"
            assert card.height() == expected_height, f"{model_id}: heights={heights}"

        for model_id, expected_buttons in (
            (models[0]["id"], {"Удалить"}),
            ("second-installed", {"Сделать рабочей", "Удалить"}),
            (models[1]["id"], set()),
        ):
            card = cards[model_id]
            buttons = {
                item.property("text")
                for item in visual_tree(card)
                if item.isVisible() and item.metaObject().indexOfSignal(b"clicked()") >= 0
            }
            assert buttons == expected_buttons, f"{model_id}: {buttons}, heights={heights}"

        visible_button(cards["second-installed"], "Сделать рабочей")
        for model_id in (models[0]["id"], "second-installed"):
            remove = visible_button(cards[model_id], "Удалить")
            right = remove.mapToScene(QPointF(remove.width(), 0)).x()
            assert right == 988, f"{model_id}: right={right}, heights={heights}"

    _, messages = render_settings(
        onboarding_app, False, fake=fake, section="models", inspect=inspect
    )
    assert_no_messages(messages, "model card heights and action edges")


def test_models_first_card_vertical_alignment(onboarding_app: Any) -> None:
    fake = FakeSettings()
    geometry: list[tuple[int, int, int, int, int, int]] = []

    def inspect(window: Any) -> None:
        root = window.contentItem()
        chip = next(item for item in visual_tree(root) if item.objectName() == "allLanguagesChip")
        caption = next(
            item for item in visual_tree(root) if item.objectName() == "installedCaption"
        )
        card = next(
            item
            for item in visual_tree(root)
            if item.isVisible() and item.property("modelId") == fake.models[0]["id"]
        )
        chip_top = chip.mapToScene(QPointF(0, 0)).y()
        chip_bottom = chip.mapToScene(QPointF(0, chip.height())).y()
        caption_point = caption.mapToScene(QPointF(0, 0))
        caption_bottom = caption.mapToScene(QPointF(0, caption.height())).y()
        card_top = card.mapToScene(QPointF(0, 0)).y()
        geometry.append(
            (
                int(chip_top),
                int(chip_bottom),
                int(caption_point.x()),
                int(caption_point.y()),
                int(caption_bottom),
                int(card_top),
            )
        )

    _, messages = render_settings(
        onboarding_app, False, fake=fake, section="models", inspect=inspect
    )
    assert_no_messages(messages, "models first card position")
    _, chip_bottom, _, caption_top, _, card_top = geometry[0]
    assert caption_top - chip_bottom == theme_number("modelCardFirstGroupTopGap"), geometry[0]
    assert 123 <= card_top <= 126, geometry[0]


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("screen", ["onboarding", "models"])
def test_model_summary_uses_two_type_sizes(onboarding_app: Any, screen: str, dark: bool) -> None:
    def inspect(root: Any) -> None:
        surface = root.contentItem() if isinstance(root, QQuickWindow) else root
        summary = next(
            item for item in visual_tree(surface) if item.objectName() == "selectionSummary"
        )
        free = next(
            item for item in visual_tree(surface) if item.objectName() == "selectionMessage"
        )
        assert summary.property("text") == "Пока ничего не выбрано"
        assert free.property("text") == "свободно на диске 42,1 ГБ"
        assert summary.property("font").pixelSize() == theme_number("onboardingSummaryLineSize")
        assert free.property("font").pixelSize() == theme_number("onboardingSummaryLineFreeSize")

    if screen == "onboarding":
        fake = FakeOnboarding()
        fake.step = 2
        _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    else:
        _, messages = render_settings(
            onboarding_app, dark, fake=FakeSettings(), section="models", inspect=inspect
        )
    assert_no_messages(messages, f"summary type sizes: {screen}, dark={dark}")


def test_dark_filter_border_blends_with_chip_fill(onboarding_app: Any) -> None:
    border_point: list[tuple[int, int]] = []

    def inspect(window: Any) -> None:
        chip = next(
            item
            for item in visual_tree(window.contentItem())
            if item.objectName() == "domesticChip"
        )
        point = chip.mapToScene(QPointF(0, chip.height() / 2))
        border_point.append((int(point.x()), int(point.y())))

    image, messages = render_settings(
        onboarding_app, True, fake=FakeSettings(), section="models", inspect=inspect
    )
    assert_no_messages(messages, "dark filter chip border")
    assert len(border_point) == 1
    color = image.pixelColor(*border_point[0])
    assert all(
        abs(channel - target) <= 1
        for channel, target in zip(
            (color.red(), color.green(), color.blue()), (44, 52, 69), strict=True
        )
    ), (border_point[0], color.name())


def test_card_with_action_is_105_high_at_narrow_width(
    onboarding_app: Any, monkeypatch: Any
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "WIDTH", 900)
    fake = FakeOnboarding()
    fake.step = 2
    fake.models = [{**fake.models[0], "state": "failed"}]

    def inspect(root: Any) -> None:
        card = next(item for item in visual_tree(root) if item.property("modelId"))
        assert card.width() == 672
        top = next(item for item in visual_tree(card) if item.objectName() == "cardTop")
        footer = next(item for item in visual_tree(card) if item.objectName() == "cardFooter")
        facts = next(item for item in visual_tree(card) if item.objectName() == "footerFacts")
        actions = next(item for item in visual_tree(card) if item.objectName() == "footerActions")
        assert card.height() == 105, (
            f"card={card.height()} top={top.height()} footer=({footer.y()},{footer.height()}) "
            f"facts={facts.height()} actions={actions.height()}"
        )
        mark = next(item for item in visual_tree(card) if item.objectName() == "selectionBox")
        assert mark.isVisible()
        assert card.property("selectBorderWidth") == theme_number("modelCardSelectBorder") == 1

    _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    assert_no_messages(messages, "narrow card with action")


@pytest.mark.parametrize("state", CAPTURE_STATES)
def test_capture_states_render(state: str, onboarding_app: Any) -> None:
    fake = FakeOnboarding()
    fake.step = 3
    fake.captureState = state
    _, messages = render_onboarding(onboarding_app, fake, False)
    assert_no_messages(messages, f"captureState={state}")


@pytest.mark.parametrize(
    ("key", "modifiers", "accepted_modifiers"),
    [
        (Qt.Key_Space, Qt.NoModifier, Qt.ControlModifier),
        (Qt.Key_A, Qt.NoModifier, Qt.ControlModifier),
        (Qt.Key_5, Qt.NoModifier, Qt.ControlModifier),
        (Qt.Key_F5, Qt.NoModifier, Qt.ControlModifier),
        (Qt.Key_A, Qt.ShiftModifier, Qt.ControlModifier | Qt.ShiftModifier),
        (Qt.Key_Space, Qt.ShiftModifier, Qt.AltModifier),
        (Qt.Key_F5, Qt.ShiftModifier, Qt.MetaModifier),
    ],
    ids=["space", "letter", "digit", "f5", "shift-letter", "shift-space", "shift-f5"],
)
def test_capture_requires_ctrl_alt_or_win(
    key: Any, modifiers: Any, accepted_modifiers: Any, onboarding_app: Any
) -> None:
    fake = FakeOnboarding()
    fake.step = 3
    fake.captureState = "capturing"

    def inspect(root: Any) -> None:
        view = root.window()
        assert isinstance(view, QQuickView)
        capture = next(item for item in visual_tree(root) if item.property("captureActive"))
        assert capture.hasActiveFocus()
        QTest.keyClick(view, key, modifiers)
        onboarding_app.processEvents()
        assert "endCapture" not in fake.calls
        assert fake.captureState == capture.property("state7") == "capturing"
        hint = next(
            item
            for item in visual_tree(root)
            if item.property("text") == "Удерживайте Ctrl, Alt или Win и нажмите клавишу"
        )
        assert hint.isVisible() and hint.height() > 0

        QTest.keyClick(view, key, accepted_modifiers)
        onboarding_app.processEvents()
        assert fake.calls == ["endCapture"]
        assert hint.property("text") == ""
        assert not hint.isVisible() and hint.height() == 0

    _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    assert_no_messages(messages, "capture requires Ctrl/Alt/Win")


def test_capture_ignores_other_keys_and_clears_hint_on_exit(onboarding_app: Any) -> None:
    fake = FakeOnboarding()
    fake.step = 3
    fake.captureState = "capturing"

    def inspect(root: Any) -> None:
        view = root.window()
        assert isinstance(view, QQuickView)
        capture = next(item for item in visual_tree(root) if item.property("captureActive"))
        assert capture.hasActiveFocus()
        for key in (Qt.Key_Control, Qt.Key_Shift, Qt.Key_Alt, Qt.Key_Meta):
            QTest.keyClick(view, key, Qt.NoModifier)
            onboarding_app.processEvents()
            assert fake.calls == []
            assert capture.property("captureHint") == ""
            assert capture.property("state7") == "capturing"

        QTest.keyClick(view, Qt.Key_Comma, Qt.NoModifier)
        assert capture.property("captureHint") == (
            "Эта клавиша не поддерживается. Выберите букву, цифру, пробел или F1–F12"
        )
        assert fake.calls == []
        QTest.keyClick(view, Qt.Key_1, Qt.KeypadModifier | Qt.ControlModifier)
        assert capture.property("captureHint") == (
            "Эта клавиша не поддерживается. Выберите букву, цифру, пробел или F1–F12"
        )
        assert fake.calls == []

        QTest.keyClick(view, Qt.Key_Space, Qt.NoModifier)
        assert capture.property("captureHint") != ""
        assert fake.calls == []
        QTest.keyClick(view, Qt.Key_Escape, Qt.NoModifier)
        assert fake.calls == ["cancelCapture"]
        # Фейк только записывает вызов; переход состояния задаём как ответ моста.
        fake.captureState = "idle"
        onboarding_app.processEvents()
        assert capture.property("captureActive") is False
        assert capture.property("captureHint") == ""
        fake.captureState = "capturing"
        onboarding_app.processEvents()
        assert capture.property("captureActive") is True
        assert capture.property("captureHint") == ""

    _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    assert_no_messages(messages, "capture ignored keys and exit")


def test_capture_caps_lock_is_silent(onboarding_app: Any) -> None:
    fake = FakeOnboarding()
    fake.step = 3
    fake.captureState = "capturing"

    def inspect(root: Any) -> None:
        view = root.window()
        capture = next(item for item in visual_tree(root) if item.property("captureActive"))
        assert capture.hasActiveFocus()
        QTest.keyClick(view, Qt.Key_CapsLock, Qt.NoModifier)
        onboarding_app.processEvents()
        assert capture.property("captureHint") == ""
        assert capture.property("state7") == "capturing"
        assert fake.calls == []

    _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    assert_no_messages(messages, "Caps Lock")


def test_settings_change_hotkey_shows_capture_field(onboarding_app: Any) -> None:
    fake = FakeSettings()

    def inspect(window: Any) -> None:
        body = next(
            item
            for item in visual_tree(window.contentItem())
            if item.metaObject().className() == "QQuickFlickable" and item.isVisible()
        )
        assert body.property("contentHeight") <= body.height()
        button = visible_button(window.contentItem(), "Изменить")
        QMetaObject.invokeMethod(button, "clicked", Qt.DirectConnection)
        onboarding_app.processEvents()
        assert "beginCapture" in fake.calls
        capture = next(
            item
            for item in visual_tree(window.contentItem())
            if item.property("state7") == "capturing" and item.isVisible()
        )
        QTest.keyClick(window, Qt.Key_D, Qt.ControlModifier | Qt.AltModifier)
        onboarding_app.processEvents()
        assert "endCapture" in fake.calls
        assert fake.captureState == capture.property("state7") == "captured"
        fake.captureState = "conflict"
        onboarding_app.processEvents()
        assert capture.hasActiveFocus()
        QTest.keyClick(window, Qt.Key_Escape, Qt.NoModifier)
        onboarding_app.processEvents()
        assert fake.calls[-1] == "cancelCapture"
        assert fake.captureState == capture.property("state7") == "idle"

    _, messages = render_settings(onboarding_app, False, fake=fake, inspect=inspect)
    assert_no_messages(messages, "settings capture")


def test_policy_locked_step1(onboarding_app: Any) -> None:
    fake = FakeOnboarding()
    fake.policyLocked = True

    def inspect(root: Any) -> None:
        assert any(
            item.isVisible() and item.property("text") == "Задано администратором"
            for item in visual_tree(root)
        ), "нет видимой подписи «Задано администратором»"

    _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    assert_no_messages(messages, "policyLocked=True")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("model_ready", [False, True], ids=["waiting", "ready"])
def test_mic_test_button_starts_and_stops_including_processing(
    onboarding_app: Any, dark: bool, model_ready: bool
) -> None:
    fake = FakeOnboarding()
    fake.step = 4
    fake.modelReady = model_ready

    def inspect(root: Any) -> None:
        for state in ("idle", "preparing", "recording", "processing", "done", "error"):
            fake.testState = state
            onboarding_app.processEvents()
            testing = state in ("preparing", "recording", "processing")
            text = "Остановить" if testing else "Тестовая диктовка"
            buttons = [
                item
                for item in visual_tree(root)
                if item.isVisible()
                and item.property("text") == text
                and item.metaObject().indexOfSignal(b"clicked()") >= 0
            ]
            assert len(buttons) == 1, state
            assert buttons[0].isEnabled() == (model_ready or testing), state
            fake.calls.clear()
            if buttons[0].isEnabled():
                QMetaObject.invokeMethod(buttons[0], "clicked", Qt.DirectConnection)
                assert fake.calls == ["stopTest" if testing else "startTest"], state
            else:
                click_item(buttons[0])
                onboarding_app.processEvents()
                assert fake.calls == [], state

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "microphone test button")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_mic_card_with_resolved_default_device_is_57px(onboarding_app: Any, dark: bool) -> None:
    fake = FakeOnboarding()
    fake.step = 4

    def inspect(root: Any) -> None:
        row = next(item for item in visual_tree(root) if item.property("label") == "Микрофон")
        assert row.property("sub") == "Встроенный микрофон"
        assert row.height() == 55
        card = microphone_card(root)
        assert row in list(visual_tree(card))
        assert card.height() == 57

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "resolved default microphone card height")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_mic_explanation_only_shows_resolved_default_device(
    onboarding_app: Any, dark: bool
) -> None:
    fake = FakeOnboarding()
    fake.step = 4
    builtin = "Встроенный микрофон"
    system_default = "Системный по умолчанию"

    def inspect(root: Any) -> None:
        row = next(item for item in visual_tree(root) if item.property("label") == "Микрофон")
        selector = next(
            item
            for item in visual_tree(row)
            if item.metaObject().indexOfProperty("currentText") >= 0
        )
        for device, resolved, selected, explanation in (
            ("", builtin, system_default, builtin),
            ("builtin", builtin, builtin, ""),
            ("", "", system_default, ""),
            ("", builtin, system_default, builtin),
        ):
            fake.device = device
            fake._deviceResolved = resolved
            fake.deviceResolvedChanged.emit()
            onboarding_app.processEvents()
            assert row.property("sub") == explanation
            assert selector.property("currentText") == selected
            texts = [
                item.property("text")
                for item in visual_tree(root)
                if item.isVisible() and isinstance(item.property("text"), str)
            ]
            assert texts.count(system_default) == int(device == "")
            assert texts.count(builtin) == int(bool(explanation) or device == "builtin")
            assert any(
                item.isVisible() and item.property("text") == selected
                for item in visual_tree(selector)
            )

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "resolved microphone explanation")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("default_index", [0, 1, None], ids=["first", "second", "absent"])
def test_mic_missing_device_displays_fallback_without_writing_bridge(
    onboarding_app: Any, dark: bool, default_index: int | None
) -> None:
    fake = FakeOnboarding()
    fake.step = 4
    devices = [{"id": "builtin", "name": "Встроенный микрофон"}]
    if default_index is not None:
        devices.insert(default_index, {"id": "", "name": "Системный по умолчанию"})
    fake.devices = devices
    fake.device = "ghost-id"
    explanation = "Выбранный раньше микрофон не найден — включён системный по умолчанию"

    def inspect(root: Any) -> None:
        selector = next(
            item
            for item in visual_tree(root)
            if item.isVisible() and item.metaObject().indexOfProperty("currentText") >= 0
        )
        expected_index = default_index if default_index is not None else 0
        assert selector.property("currentIndex") == expected_index
        assert selector.property("currentText") == devices[expected_index]["name"]
        assert any(
            item.isVisible() and item.property("text") == devices[expected_index]["name"]
            for item in visual_tree(selector)
        )
        assert any(
            item.isVisible() and item.property("text") == explanation for item in visual_tree(root)
        )
        assert fake.device == "ghost-id"
        assert fake.calls == ["startLevelMonitor"]
        row = next(item for item in visual_tree(root) if item.property("label") == "Микрофон")
        assert row.property("sub") == ""

        fake.device = "builtin"
        onboarding_app.processEvents()
        assert selector.property("currentText") == "Встроенный микрофон"
        assert not any(
            item.isVisible() and item.property("text") == explanation for item in visual_tree(root)
        )
        QTest.qWait(25)  # Дожидаемся кадра с обновлённой геометрией Column.
        assert row.property("sub") == ""
        assert row.height() == 48
        card = microphone_card(root)
        assert row in list(visual_tree(card))
        assert card.height() == 50
        assert fake.calls == ["startLevelMonitor"]

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "missing microphone")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_mic_silence_is_reported_while_listening(onboarding_app: Any, dark: bool) -> None:
    fake = FakeOnboarding()
    fake.step = 4
    fake.level = 0
    fake.testText = ""
    fake.levelMessage = "Не удалось открыть микрофон"
    silence = "Пока тишина"
    warning = "Микрофон молчит"
    retry = "Проверить ещё раз"

    def inspect(root: Any) -> None:
        for state, level in (
            ("idle", 0),
            ("listening", 0),
            ("listening", 0.02),
            ("listening", 0.021),
            ("listening", 0.5),
            ("error", 0),
            ("idle", 0),
        ):
            fake.levelState = state
            fake.level = level
            onboarding_app.processEvents()
            texts = visible_texts(root)
            assert (warning in texts) == (state == "listening" and level <= 0.02), (state, level)
            # В idle пояснение означает, что мост сам остановил проверку по
            # общему потолку (ИБ, У62): показываем его и даём повторить.
            assert (fake.levelMessage in texts) == (state in ("error", "idle")), (state, level)
            assert (retry in texts) == (state == "idle"), (state, level)
            if state == "listening":
                assert (silence in texts) == (level <= 0.02), level
                assert ("Слышим вас" in texts) == (level > 0.02), level
            elif state == "error":
                assert silence not in texts
                assert "Слышим вас" not in texts

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "microphone silence")


def test_finish_calls_bridge(onboarding_app: Any) -> None:
    """Вызывает настоящий сигнал clicked() кнопки «Готово» через метаобъект Qt5."""
    fake = FakeOnboarding()
    fake.step = 5
    fake.canFinish = True

    def inspect(root: Any) -> None:
        buttons = [
            item
            for item in visual_tree(root)
            if item.property("text") == "Готово"
            and item.metaObject().indexOfSignal(b"clicked()") >= 0
            and item.isVisible()
        ]
        assert len(buttons) == 1, f"ожидалась одна кнопка «Готово», найдено {len(buttons)}"
        assert buttons[0].isEnabled()
        assert fake.calls == []
        QMetaObject.invokeMethod(buttons[0], "clicked", Qt.DirectConnection)
        assert fake.calls == ["finish"]

    _, messages = render_onboarding(onboarding_app, fake, False, inspect=inspect)
    assert_no_messages(messages, "finish")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_model_continue_requires_selection(onboarding_app: Any, dark: bool) -> None:
    fake = configure_onboarding(2, canContinueFromModel=False)
    hint = "Выберите хотя бы одну модель — без неё диктовка не работает"

    def inspect(root: Any) -> None:
        button = visible_button(root, "Продолжить")
        for allowed in (False, True, False):
            fake.canContinueFromModel = allowed
            onboarding_app.processEvents()
            assert button.isEnabled() == allowed
            assert (hint in visible_texts(root)) == (not allowed)
            if not allowed:
                click_item(button)
                onboarding_app.processEvents()
                assert fake.calls == []

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "model selection gates continuation")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_model_card_click_toggles_entry(onboarding_app: Any, dark: bool) -> None:
    fake = configure_onboarding(2)
    # Отличающийся от каталожного id ловит захардкоженный аргумент слота.
    model_id = "test-selectable-model"
    fake.models = [{**fake.models[0], "id": model_id}]

    def inspect(root: Any) -> None:
        cards = [item for item in visual_tree(root) if item.property("modelId") == model_id]
        assert len(cards) == 1
        assert cards[0].isEnabled()
        assert fake.calls == []
        click_item(cards[0])
        onboarding_app.processEvents()
        assert fake.calls == ["toggleModel"]
        assert fake.toggled_model_ids == [model_id]

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "model card click")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_model_continue_starts_downloads_before_next(onboarding_app: Any, dark: bool) -> None:
    fake = configure_selected_model()

    def inspect(root: Any) -> None:
        button = visible_button(root, "Продолжить")
        assert button.isEnabled()
        assert fake.calls == []
        QMetaObject.invokeMethod(button, "clicked", Qt.DirectConnection)
        assert fake.calls == ["startSelectedDownloads", "next"]

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "download before next")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_download_strip_visibility_across_steps(onboarding_app: Any, dark: bool) -> None:
    fake = configure_onboarding(
        1, downloadProgress=0.43, downloadTitle="Загружается GigaAM v3 RNN-T"
    )

    def inspect(root: Any) -> None:
        strips = [
            item
            for item in visual_tree(root)
            if item.metaObject().indexOfProperty("downloadState") >= 0
        ]
        assert len(strips) == 1
        strip = strips[0]
        for state in ("downloading", "idle"):
            fake.downloadState = state
            for step in (1, 2, 3, 4, 5, 1):
                fake.step = step
                QTest.qWait(25)
                onboarding_app.processEvents()
                assert root.property("step") == step
                visible = state == "downloading" and step >= 2
                assert strip.isVisible() == visible, (state, step)
                assert strip.property("downloadState") == (state if step >= 2 else "idle")
                assert (fake.downloadTitle in visible_texts(root)) == visible, (state, step)

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "download strip across steps")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("model_ready", [False, True], ids=["waiting", "ready"])
def test_mic_level_monitor_follows_step_visibility(
    onboarding_app: Any, dark: bool, model_ready: bool
) -> None:
    fake = configure_onboarding(4, modelReady=model_ready)

    def inspect(root: Any) -> None:
        calls_after_render = fake.calls.copy()
        assert calls_after_render == ["startLevelMonitor"]
        fake.step = 3
        QTest.qWait(25)
        onboarding_app.processEvents()
        assert root.property("step") == 3
        assert fake.calls == [*calls_after_render, "stopLevelMonitor"]
        fake.step = 4
        QTest.qWait(25)
        onboarding_app.processEvents()
        assert root.property("step") == 4
        assert fake.calls == [*calls_after_render, "stopLevelMonitor", "startLevelMonitor"]

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "level monitor lifecycle")
    assert fake.calls == [
        "startLevelMonitor",
        "stopLevelMonitor",
        "startLevelMonitor",
        "stopLevelMonitor",
    ]


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_mic_dictation_requires_ready_model(onboarding_app: Any, dark: bool) -> None:
    fake = configure_onboarding(4, modelReady=False, testState="idle")
    waiting = "Будет доступно после установки модели"
    ready = "Скажите фразу — покажем, что распознали."

    def inspect(root: Any) -> None:
        button = visible_button(root, "Тестовая диктовка")
        for model_ready in (False, True, False):
            fake.modelReady = model_ready
            onboarding_app.processEvents()
            assert button.isEnabled() == model_ready
            texts = visible_texts(root)
            assert (waiting in texts) == (not model_ready)
            assert (ready in texts) == model_ready
            assert fake.calls == ["startLevelMonitor"]

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "dictation model readiness")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_done_updates_when_model_becomes_ready(onboarding_app: Any, dark: bool) -> None:
    fake = configure_onboarding(5, modelReady=False, canFinish=False)

    def inspect(root: Any) -> None:
        button = visible_button(root, "Готово")
        for model_ready in (False, True, False):
            fake.modelReady = model_ready
            onboarding_app.processEvents()
            texts = visible_texts(root)
            assert ("Почти всё" in texts) == (not model_ready)
            assert ("Комбинация уже назначена: Ctrl + Space" in texts) == (not model_ready)
            assert ("Всё готово" in texts) == model_ready
            assert ("Готово: зажмите Ctrl + Space и говорите" in texts) == model_ready
            assert button.isEnabled() == model_ready
            assert fake.calls == []

    _, messages = render_onboarding(onboarding_app, fake, dark, inspect=inspect)
    assert_no_messages(messages, "done follows model readiness without slot calls")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_settings_models_section_calls_bridge(onboarding_app: Any, dark: bool) -> None:
    """Карточка, «Скачать выбранное» и установка из папки зовут мост, а не молчат."""
    fake = FakeSettings()
    fake.models = [{**fake.models[0], "badge": "", "state": "available", "selected": True}]
    fake.selectionSummary = "Будет скачано 226 МБ"

    def inspect(window: Any) -> None:
        root = window.contentItem()
        cards = [
            item
            for item in visual_tree(root)
            if item.isVisible() and item.property("modelId") == fake.models[0]["id"]
        ]
        assert len(cards) == 1, f"ожидалась одна карточка, найдено {len(cards)}"
        # Раздел «Общие» грузится первым и при открытии читает список микрофонов
        # и состояние громкости — это не вызовы раздела «Модели».
        assert fake.calls == ["refreshDevices", "refreshMicrophone", "cancelCapture"]
        fake.calls.clear()
        assert fake.toggled_model_ids == []
        click_item(cards[0])
        assert fake.toggled_model_ids == [fake.models[0]["id"]]

        download = visible_button(root, "Скачать выбранное")
        assert download.isEnabled()
        QMetaObject.invokeMethod(download, "clicked", Qt.DirectConnection)
        assert fake.calls == ["startSelectedDownloads"]

        install = visible_button(root, "Установить из файла или папки…")
        assert install.isEnabled()
        QMetaObject.invokeMethod(install, "clicked", Qt.DirectConnection)
        assert fake.calls == ["startSelectedDownloads", "pickInstallPath"]

    _, messages = render_settings(
        onboarding_app, dark, fake=fake, section="models", inspect=inspect
    )
    assert_no_messages(messages, "models section calls bridge")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_settings_model_removal_requires_confirmation(onboarding_app: Any, dark: bool) -> None:
    """Удаление установленной модели проходит через оконный диалог."""
    fake = FakeSettings()
    fake.models = [{**fake.models[0], "badge": "installed", "selected": False}]

    def inspect(window: Any) -> None:
        root = window.contentItem()
        card = next(
            item
            for item in visual_tree(root)
            if item.isVisible() and item.property("modelId") == fake.models[0]["id"]
        )
        assert set(fake.calls) <= {"refreshDevices", "refreshMicrophone", "cancelCapture"}
        fake.calls.clear()

        def open_dialog() -> Any:
            remove = visible_button(card, "Удалить")
            assert remove.isEnabled()
            QMetaObject.invokeMethod(remove, "clicked", Qt.DirectConnection)
            onboarding_app.processEvents()
            assert fake.calls == []
            dialogs = [
                item
                for item in window.findChildren(QObject)
                if item.property("heading") == "Удалить GigaAM v3 RNN-T?"
            ]
            assert len(dialogs) == 1
            dialog = dialogs[0]
            assert dialog.property("visible") is True
            assert dialog.property("modal") is True
            assert (
                "С диска будет удалено 226 МБ из папки моделей. Настройки и статистика останутся."
            ) in visible_texts(root)
            assert "Скачать модель заново можно в любой момент." in visible_texts(root)
            assert dialog.property("note") == "Скачать модель заново можно в любой момент."
            assert dialog.property("iconName") == "trash"
            return dialog

        dialog = open_dialog()
        cancel = visible_button(dialog.property("footer"), "Отмена")
        QMetaObject.invokeMethod(cancel, "clicked", Qt.DirectConnection)
        onboarding_app.processEvents()
        assert fake.calls == []
        assert dialog.property("visible") is False

        dialog = open_dialog()
        confirm = visible_button(dialog.property("footer"), "Удалить")
        QMetaObject.invokeMethod(confirm, "clicked", Qt.DirectConnection)
        onboarding_app.processEvents()
        assert fake.calls == ["removeModel"]
        assert dialog.property("visible") is False

    _, messages = render_settings(
        onboarding_app, dark, fake=fake, section="models", inspect=inspect
    )
    assert_no_messages(messages, "model removal confirmation")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_broken_installed_card_keeps_badge_and_message(onboarding_app: Any, dark: bool) -> None:
    fake = FakeSettings()
    fake.models = [
        {
            **fake.models[0],
            "badge": "installed",
            "state": "broken",
            "message": "Файлы модели не читаются — переустановите",
        }
    ]

    def inspect(window: Any) -> None:
        card = next(
            item
            for item in visual_tree(window.contentItem())
            if item.isVisible() and item.property("modelId") == fake.models[0]["id"]
        )
        texts = visible_texts(card)
        assert "Файлы модели не читаются — переустановите" in texts
        assert "Установлена" in texts
        assert visible_button(card, "Переустановить").isEnabled()
        assert visible_button(card, "Удалить").isEnabled()

    _, messages = render_settings(
        onboarding_app, dark, fake=fake, section="models", inspect=inspect
    )
    assert_no_messages(messages, "broken installed model")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_active_model_removal_is_disabled(onboarding_app: Any, dark: bool) -> None:
    fake = FakeSettings()
    fake.models = [{**fake.models[0], "badge": "active", "state": "installed"}]

    def inspect(window: Any) -> None:
        root = window.contentItem()
        card = next(
            item
            for item in visual_tree(root)
            if item.isVisible() and item.property("modelId") == fake.models[0]["id"]
        )
        fake.calls.clear()
        remove = visible_button(card, "Удалить")
        assert not remove.isEnabled()
        assert not any(
            item.property("visible") is True and item.property("modal") is True
            for item in window.findChildren(QObject)
            if item.property("heading")
        )
        assert fake.calls == []

    _, messages = render_settings(
        onboarding_app, dark, fake=fake, section="models", inspect=inspect
    )
    assert_no_messages(messages, "active model removal")


def test_model_dialog_survives_section_reloading(onboarding_app: Any) -> None:
    """Открытый popup закрывается до уничтожения раздела при каждом переходе."""
    fake = FakeSettings()
    fake.models = [{**fake.models[0], "badge": "installed", "selected": False}]
    heading = "Удалить GigaAM v3 RNN-T?"

    def inspect(window: Any) -> None:
        sidebar = settings_sidebar(window)
        for _ in range(30):
            root = window.contentItem()
            card = next(
                item
                for item in visual_tree(root)
                if item.isVisible() and item.property("modelId") == fake.models[0]["id"]
            )
            QMetaObject.invokeMethod(
                visible_button(card, "Удалить"), "clicked", Qt.DirectConnection
            )
            onboarding_app.processEvents()
            dialogs = [
                item
                for item in window.findChildren(QObject)
                if item.property("heading") == heading and item.property("visible") is True
            ]
            assert len(dialogs) == 1
            # Модальный popup блокирует клик; смену source запускает сам сайдбар.
            assert sidebar.setProperty("currentIndex", 0)
            onboarding_app.processEvents()
            assert sidebar.property("currentIndex") == 0
            assert sidebar.setProperty("currentIndex", 1)
            onboarding_app.processEvents()
            assert sidebar.property("currentIndex") == 1

    _, messages = render_settings(
        onboarding_app, False, fake=fake, section="models", inspect=inspect
    )
    assert_no_messages(messages, "removal dialog section reload")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_long_removal_message_wraps_in_dialog(onboarding_app: Any, dark: bool) -> None:
    fake = FakeSettings()
    long_name = "Очень длинное название модели для проверки переноса текста в диалоге " * 5
    long_size = "226 МБ " * 40
    fake.models = [
        {
            **fake.models[0],
            "name": long_name,
            "sizeText": long_size,
            "badge": "installed",
            "state": "installed",
        }
    ]

    def inspect(window: Any) -> None:
        root = window.contentItem()
        card = next(
            item
            for item in visual_tree(root)
            if item.isVisible() and item.property("modelId") == fake.models[0]["id"]
        )
        QMetaObject.invokeMethod(visible_button(card, "Удалить"), "clicked", Qt.DirectConnection)
        onboarding_app.processEvents()
        dialog = next(
            item
            for item in window.findChildren(QObject)
            if item.property("heading") == f"Удалить {long_name}?"
        )
        body = dialog.property("contentItem")
        message = next(
            item
            for item in visual_tree(body)
            if item.property("text") == dialog.property("message")
        )
        assert message.property("paintedHeight") > 3 * message.property("font").pixelSize()
        assert body.property("height") >= message.property("y") + message.property("paintedHeight")
        assert long_size in dialog.property("message")

    _, messages = render_settings(
        onboarding_app, dark, fake=fake, section="models", inspect=inspect
    )
    assert_no_messages(messages, "long model name removal dialog")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_settings_models_survive_long_texts(onboarding_app: Any, dark: bool) -> None:
    """Длинные подписи каталога не должны зацикливать расчёт высоты карточки.

    Текст с переносом делает высоту зависимой от ширины, ширину — от раскладки,
    и строка уходит в Binding loop. На живом сеансе это видно сразу, а в снимке —
    нет, поэтому длины здесь заведомо больше, чем помещается.
    """
    fake = FakeSettings()
    fake.models = [
        {
            **fake.models[0],
            "name": "GigaAM v3 RNN-T с очень длинным названием записи каталога",
            "description": "Очень длинное описание записи каталога, которое заведомо "
            "не помещается в одну строку карточки ни при какой ширине окна",
            "sizeText": "1 234,5 МБ",
            "ramText": "4 096 МБ",
            "badge": "",
            "state": "failed",
            "message": "Не удалось загрузить модель: источник ответил ошибкой, "
            "попробуйте позже или поставьте модель из папки",
        }
    ]
    fake.selectionMessage = "На диске не хватает 1 234,5 МБ. Освободите место."
    fake.selectionFits = False
    fake.selectionSummary = "Будет скачано 1 234,5 МБ"

    def inspect(window: Any) -> None:
        texts = visible_texts(window.contentItem())
        assert any(text.startswith("GigaAM v3 RNN-T с очень длинным") for text in texts)
        assert not visible_button(window.contentItem(), "Скачать выбранное").isEnabled()

    _, messages = render_settings(
        onboarding_app, dark, fake=fake, section="models", inspect=inspect
    )
    assert_no_messages(messages, "long catalog texts")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_settings_models_show_active_model(onboarding_app: Any, dark: bool) -> None:
    """Рабочая модель помечена бейджем и не предлагает себя перекачать."""
    fake = FakeSettings()

    def inspect(window: Any) -> None:
        texts = visible_texts(window.contentItem())
        assert "Установлена и активна" in texts
        assert fake.models[0]["name"] in texts
        assert {"Переустановить", "Установить", "Повторить", "Отмена"} & texts == set()
        assert not visible_button(window.contentItem(), "Скачать выбранное").isEnabled()

    _, messages = render_settings(
        onboarding_app, dark, fake=fake, section="models", inspect=inspect
    )
    assert_no_messages(messages, "active model card")


@pytest.mark.parametrize("unknown", [False, True], ids=["snapshot", "unknown"])
def test_models_empty_catalog_revocation_warning(onboarding_app: Any, unknown: bool) -> None:
    fake = FakeSettings()
    available_models = fake.models
    fake.models = []
    fake.revocationUnknown = unknown
    warning = (
        "Проверить, не отозвана ли текущая версия модели, сейчас нельзя — "
        "программа работает с той моделью, что была выбрана раньше."
    )

    def inspect(window: Any) -> None:
        texts = visible_texts(window.contentItem())
        assert "Список моделей недоступен. Модель можно поставить из файла или папки." in texts
        assert (warning in texts) is unknown
        note = next(
            item
            for item in visual_tree(window.contentItem())
            if item.objectName() == "revocationUnknownNote"
        )
        assert note.isVisible() is unknown
        fake.models = available_models
        onboarding_app.processEvents()
        assert not note.isVisible()
        assert warning not in visible_texts(window.contentItem())

    _, messages = render_settings(
        onboarding_app, False, fake=fake, section="models", inspect=inspect
    )
    assert_no_messages(messages, "empty catalog revocation warning")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_settings_hide_debug_section_without_flag(onboarding_app: Any, dark: bool) -> None:
    """Без appInfo.debug «Отладки» нет ни в меню, ни в области контента."""

    def inspect(window: Any) -> None:
        assert settings_sidebar(window).property("debugVisible") is False
        texts = visible_texts(window.contentItem())
        assert "Отладка (Ctrl+Shift+D)" not in texts
        assert "Здесь будут сведения для поддержки" not in texts

    _, messages = render_settings(onboarding_app, dark, inspect=inspect)
    assert_no_messages(messages, "debug hidden without flag")


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_settings_about_shows_version_and_privacy(onboarding_app: Any, dark: bool) -> None:
    """Раздел «О программе» без appInfo показывает запасную версию и две строки."""

    def inspect(window: Any) -> None:
        texts = visible_texts(window.contentItem())
        assert {
            "Astra Voice",
            "0.1.0",
            "Программа не выходит в сеть без вашего действия",
            "Исходный код открыт",
        } <= texts

    _, messages = render_settings(onboarding_app, dark, section="about", inspect=inspect)
    assert_no_messages(messages, "about section")
