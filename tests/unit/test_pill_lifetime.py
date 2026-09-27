"""Дочерний таймер пилюли не переживает её удаление."""

from __future__ import annotations

import os
import time
from unittest.mock import Mock

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt5 import sip  # noqa: E402

from astra_voice.ui import pill as pill_module  # noqa: E402
from astra_voice.ui.pill import STATE_DURATION_MS, Pill, PillState  # noqa: E402
from helpers.qt_app import get_qapplication  # noqa: E402

pytestmark = pytest.mark.unit


def test_expiry_timer_dies_with_pill(monkeypatch: pytest.MonkeyPatch) -> None:
    app = get_qapplication()
    assert app.platformName() == "offscreen"

    properties = {"pillWidth": 172.0, "pillHeight": 36.0}
    root = Mock()
    root.property.side_effect = properties.__getitem__
    view = Mock()
    view.rootObject.return_value = root
    view.winId.return_value = 42
    view.isVisible.return_value = False
    view.width.return_value = 208
    view.height.return_value = 78
    monkeypatch.setattr(pill_module, "X11Display", Mock(return_value=Mock(d=None, root=None)))
    monkeypatch.setattr(pill_module, "QMetaObject", Mock())
    monkeypatch.setattr(pill_module, "install_icon_provider", Mock())

    pill = Pill(view_factory=lambda: view)
    expired: list[int] = []

    def record_expiry(self: Pill, generation: int) -> None:
        expired.append(generation)

    monkeypatch.setattr(Pill, "_expire", record_expiry)
    pill.set_enabled(True)
    pill.show_state(PillState.DONE)
    timer = pill._expiry_timer
    assert timer.isActive()

    sip.delete(pill)
    assert sip.isdeleted(timer) is True

    deadline = time.monotonic() + (STATE_DURATION_MS[PillState.DONE] + 200) / 1000
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    app.processEvents()
    assert expired == []
