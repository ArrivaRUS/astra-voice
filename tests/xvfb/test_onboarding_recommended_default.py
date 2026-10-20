"""Recommended default through the real wizard bridge and rendered QML check mark.

Only the model service is scripted; selection, SettingsBridge, the wizard controller
and Onboarding.qml are production code. No downloads or snapshot files are needed.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, cast
from unittest.mock import Mock

import pytest
from PyQt5 import sip
from PyQt5.QtTest import QTest
from test_onboarding import (
    assert_no_messages,
    click_item,
    render_onboarding,
    visible_button,
    visual_tree,
)
from test_onboarding import (
    onboarding_app as onboarding_app,
)

from astra_voice.core.settings import Settings
from astra_voice.ui.bridges import OnboardingController, SettingsBridge
from astra_voice.ui.model_downloads import ModelDownloads
from helpers.scripted_model_port import FIRST, SECOND, ScriptedModelPort

pytestmark = pytest.mark.xvfb


class WizardModelPort(ScriptedModelPort):
    """Recommendation is deliberately second in the catalogue; network is controllable."""

    def __init__(self, refusal: str = "") -> None:
        super().__init__((SECOND, FIRST))
        self.network_refusal = refusal

    def recommended(self) -> Any:
        return FIRST

    def allowed(self) -> tuple[bool, str]:
        return not self.network_refusal, self.network_refusal

    def refusal(self) -> str:
        return self.network_refusal


@contextmanager
def wizard_bridge(
    port: WizardModelPort, *, step: int = 2
) -> Iterator[tuple[OnboardingController, ModelDownloads, Mock]]:
    settings = Settings(extra={"onboarding_language_set": True, "onboarding_step": step})
    save = Mock()
    downloads = ModelDownloads(port, settings=settings)
    bridge = SettingsBridge(settings, downloads=downloads, save=save, device_provider=lambda: [])
    controller = None
    try:
        # Opening settings and reading its cards must not choose anything.
        assert not any(row["selected"] for row in bridge.models)
        controller = OnboardingController(
            bridge, settings=settings, downloads=downloads, device_provider=lambda: []
        )
        yield controller, downloads, save
    finally:
        if controller is not None:
            controller.shutdown()
            sip.delete(controller)
        downloads.shutdown()
        sip.delete(bridge)
        sip.delete(downloads)


def model_card(root: Any, model_id: str) -> Any:
    cards = [item for item in visual_tree(root) if item.property("modelId") == model_id]
    assert len(cards) == 1, model_id
    return cards[0]


def assert_check_mark(root: Any, model_id: str, checked: bool, *, mark: str | None = None) -> None:
    """The production card paints its checkbox rather than using a Qt CheckBox."""
    card = model_card(root, model_id)
    assert card.property("selected") is checked
    assert card.property("selectionMark") == (
        mark if mark is not None else "on" if checked else "off"
    )
    boxes = [item for item in visual_tree(card) if item.objectName() == "selectionBox"]
    assert len(boxes) == 1
    checks = [item for item in visual_tree(boxes[0]) if item.property("name") == "check"]
    assert len(checks) == 1
    assert boxes[0].isVisible()
    assert checks[0].isVisible() is checked


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize("initial_step", [1, 2], ids=["first-visit", "resumed-step2"])
def test_recommended_default_reaches_real_step2_check_mark(
    onboarding_app: Any, dark: bool, initial_step: int
) -> None:
    port = WizardModelPort()
    with wizard_bridge(port, step=initial_step) as (controller, downloads, save):
        assert {row["id"] for row in controller.models if row["selected"]} == (
            {FIRST.id} if initial_step == 2 else set()
        )
        assert controller.canContinueFromModel is (initial_step == 2)
        save.assert_not_called()

        def inspect(root: Any) -> None:
            if initial_step == 1:
                click_item(visible_button(root, "Продолжить"))
                QTest.qWait(200)
                onboarding_app.processEvents()
            assert root.property("step") == 2
            assert_check_mark(root, FIRST.id, True)
            assert_check_mark(root, SECOND.id, False)
            assert visible_button(root, "Продолжить").isEnabled()
            # Reads and model refresh notifications must preserve the default.
            downloads.measurements_changed()
            onboarding_app.processEvents()
            assert_check_mark(root, FIRST.id, True)

        _, messages = render_onboarding(
            onboarding_app, cast(Any, controller), dark, inspect=inspect
        )
        assert_no_messages(messages, "real wizard recommended default")
        assert port.download_calls == []


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
def test_click_deselects_and_refresh_navigation_and_show_preserve_choice(
    onboarding_app: Any, dark: bool
) -> None:
    port = WizardModelPort()
    with wizard_bridge(port) as (controller, downloads, _save):

        def inspect(root: Any) -> None:
            controller.attach_window(root.window())
            assert_check_mark(root, FIRST.id, True)
            click_item(model_card(root, FIRST.id))
            onboarding_app.processEvents()
            assert_check_mark(root, FIRST.id, False)
            assert controller.canContinueFromModel is False
            assert not visible_button(root, "Продолжить").isEnabled()

            downloads.measurements_changed()
            downloads.network_changed()
            onboarding_app.processEvents()
            assert_check_mark(root, FIRST.id, False)

            controller.back()
            onboarding_app.processEvents()
            assert root.property("step") == 1
            controller.next()
            QTest.qWait(200)
            onboarding_app.processEvents()
            assert root.property("step") == 2
            assert_check_mark(root, FIRST.id, False)

            root.window().hide()
            onboarding_app.processEvents()
            root.window().show()
            QTest.qWait(50)
            onboarding_app.processEvents()
            assert_check_mark(root, FIRST.id, False)
            assert not visible_button(root, "Продолжить").isEnabled()

            click_item(model_card(root, FIRST.id))
            onboarding_app.processEvents()
            assert_check_mark(root, FIRST.id, True)
            assert visible_button(root, "Продолжить").isEnabled()

        _, messages = render_onboarding(
            onboarding_app, cast(Any, controller), dark, inspect=inspect
        )
        assert_no_messages(messages, "real wizard deselection survives refresh/re-entry/show")
        assert port.download_calls == []


@pytest.mark.parametrize("blocked", ["offline", "policy", "revoked", "installed"])
def test_unavailable_recommendation_is_not_checked_or_replaced(
    onboarding_app: Any, blocked: str
) -> None:
    port = WizardModelPort(blocked if blocked in {"offline", "policy"} else "")
    if blocked == "revoked":
        port.revoked.add(FIRST.id)
    elif blocked == "installed":
        port.records[(FIRST.id, FIRST.revision)] = "ok"
    with wizard_bridge(port) as (controller, _downloads, save):
        assert not any(row["selected"] for row in controller.models)
        save.assert_not_called()

        def inspect(root: Any) -> None:
            assert model_card(root, FIRST.id).property("selected") is False
            assert_check_mark(
                root, SECOND.id, False, mark="blocked" if blocked == "offline" else "off"
            )
            if blocked != "installed":
                assert controller.canContinueFromModel is False
                assert not visible_button(root, "Продолжить").isEnabled()

        _, messages = render_onboarding(
            onboarding_app, cast(Any, controller), False, inspect=inspect
        )
        assert_no_messages(messages, f"real wizard blocked recommendation: {blocked}")
        assert port.download_calls == []
