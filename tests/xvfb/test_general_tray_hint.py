"""Подсказка XEmbed в «Общих»: настоящий мост, две темы, безопасные снимки."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtCore import QObject, Qt, QUrl, pyqtSignal, qInstallMessageHandler
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtTest import QTest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_onboarding import (  # noqa: E402
    FakeTheme,
    ensure_control_in_viewport,
    visual_tree,
)
from test_update_states import add_background, grab  # noqa: E402

from astra_voice.core.settings import Settings
from astra_voice.ui.bridges import SettingsBridge
from astra_voice.ui.icons import install_icon_provider
from astra_voice.ui.tray import Tray
from helpers.qt_app import get_qapplication

pytestmark = pytest.mark.xvfb
REPO = Path(__file__).resolve().parents[2]
HINT = (
    "Во Fly значок может быть скрыт за стрелкой области уведомлений; "
    "при записи пилюля показывается всегда."
)


class FakeTray(QObject):
    xembedUnconfirmedChanged = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.xembed_unconfirmed = False

    def set_unconfirmed(self, value: bool) -> None:
        self.xembed_unconfirmed = value
        self.xembedUnconfirmedChanged.emit()


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_general_tray_hint_live_layout_and_snapshot(tmp_path: Path, dark: bool) -> None:
    app = get_qapplication()
    messages: list[str] = []

    def handler(_mode: Any, _context: Any, message: str) -> None:
        messages.append(message)

    previous = qInstallMessageHandler(handler)
    tray = FakeTray()
    saved = Mock()
    bridge = SettingsBridge(
        Settings(), tray=cast(Tray, tray), save=saved, device_provider=lambda: []
    )
    theme = FakeTheme(dark)
    engine = QQmlApplicationEngine()
    install_icon_provider(engine)
    snapshots = Path(os.environ.get("ASTRA_VOICE_SNAPSHOT_DIR_GENERAL") or tmp_path)
    snapshots.mkdir(parents=True, exist_ok=True)
    theme_name = "dark" if dark else "light"
    try:
        context = engine.rootContext()
        context.setContextProperty("themeSource", theme)
        engine.load(QUrl.fromLocalFile(str(REPO / "qml/Main.qml")))
        assert len(engine.rootObjects()) == 1, messages
        window = engine.rootObjects()[0]
        window.setWidth(1024)
        window.setHeight(588)
        window.setProperty("freezeAnimations", True)
        _background = add_background(engine, window)
        window.show()
        QTest.qWait(80)
        window.setWidth(1024)
        window.setHeight(588)
        QTest.qWait(40)
        items = list(visual_tree(window.contentItem()))
        hint = next(item for item in items if item.objectName() == "trayXembedHint")
        sound_row = next(item for item in items if item.objectName() == "soundCuesRow")
        indicator_row = next(item for item in items if item.property("label") == "Индикатор записи")
        assert not hint.isVisible() and hint.height() == 0
        base_y = sound_row.y()
        context.setContextProperty("settingsBridge", bridge)
        QTest.qWait(40)
        assert not hint.isVisible() and hint.height() == 0
        assert sound_row.y() == base_y
        image = grab(app, window, 1024, 588)
        assert image.save(str(snapshots / f"general-tray-default-{theme_name}.png"), "PNG")

        tray.set_unconfirmed(True)
        QTest.qWait(40)
        assert hint.isVisible() and hint.height() > 0
        assert hint.property("text") == HINT
        assert hint.property("textFormat") == Qt.PlainText
        assert hint.width() > 0 and hint.property("lineCount") >= 1
        assert hint.y() == indicator_row.y() + indicator_row.height()
        assert sound_row.y() == base_y + hint.height()
        assert hint.property("truncated") is False
        body = next(
            item
            for item in items
            if item.metaObject().className() == "QQuickFlickable" and item.isVisible()
        )
        ensure_control_in_viewport(app, body, hint)
        image = grab(app, window, 1024, 588)
        assert image.save(str(snapshots / f"general-tray-xembed-{theme_name}.png"), "PNG")

        tray.set_unconfirmed(False)
        QTest.qWait(40)
        assert not hint.isVisible() and hint.height() == 0
        assert sound_row.y() == base_y
        saved.assert_not_called()
        assert not messages, "\n".join(messages)
    finally:
        for window in engine.rootObjects():
            window.close()
        sip.delete(engine)
        sip.delete(bridge)
        sip.delete(theme)
        sip.delete(tray)
        app.processEvents()
        qInstallMessageHandler(previous)
