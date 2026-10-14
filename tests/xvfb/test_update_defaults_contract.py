"""0.1.5 real component contracts isolated from unrelated Main.qml dialogs.

The existing full-window and independent xcb tests retain their strict assertions.
This harness renders the production Network, About and Step1Network unchanged.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from PyQt5 import sip
from PyQt5.QtCore import Qt, QUrl
from PyQt5.QtQml import QQmlApplicationEngine
from PyQt5.QtTest import QTest

from astra_voice.core import settings as settings_mod
from astra_voice.ui.bridges import OnboardingController, SettingsBridge

states: Any = importlib.import_module("test_update_states")
pytestmark = pytest.mark.xvfb
HARNESS = Path(__file__).resolve().parents[1] / "fixtures/qml/update_defaults_harness.qml"


@pytest.fixture(scope="module")
def app() -> Any:
    app = states.get_qapplication()
    states.onboarding.register_ui_fonts()
    return app


@pytest.fixture
def component(app: Any, request: pytest.FixtureRequest) -> Iterator[Any]:
    engine = QQmlApplicationEngine()
    states.install_icon_provider(engine)
    mode = getattr(request, "param", {}).get("mode", "ordinary")
    changes: dict[str, Any] = {}
    values: dict[str, Any] = {"state": "idle"}
    if mode == "offline":
        changes["_offline"] = True
        values.update(networkRefusal="offline", checkRefusal="offline", canCheckNow=False)
    elif mode == "policy":
        changes.update(
            _lockedSettings=["check_app_updates", "check_model_updates"],
            _checkAppUpdates=False,
            _checkModelUpdates=False,
        )
        values.update(networkRefusal="admin", checkRefusal="admin", canCheckNow=False)
    if getattr(request, "param", {}).get("available"):
        values.update(states.AVAILABLE)
    changes.update(getattr(request, "param", {}).get("settings", {}))
    values.update(getattr(request, "param", {}).get("updates", {}))
    settings = states.make_settings(changes)
    updates = states.FakeUpdates(**values)
    onboarding = states.onboarding.FakeOnboarding()
    theme = states.onboarding.FakeTheme(False)
    messages, previous = states.collect_messages()
    context = engine.rootContext()
    for name, obj in (
        ("settingsBridge", settings),
        ("updatesBridge", updates),
        ("onboarding", onboarding),
        ("themeSource", theme),
    ):
        context.setContextProperty(name, obj)
    engine.load(QUrl.fromLocalFile(str(HARNESS)))
    assert len(engine.rootObjects()) == 1, messages
    window = engine.rootObjects()[0]
    QTest.qWait(50)
    try:
        yield window, updates, settings, onboarding, context
    finally:
        sip.delete(engine)
        for obj in (settings, updates, onboarding, theme):
            sip.delete(obj)
        app.processEvents()
        states.qInstallMessageHandler(previous)
        assert not messages, "\n".join(messages)


@pytest.mark.parametrize(
    "automatic,expected",
    [(True, "Автоматически раз в сутки"), (False, "Только вручную")],
)
def test_about_with_only_settings_reports_known_update_mode(
    app: Any, component: Any, automatic: bool, expected: str
) -> None:
    window, updates, settings, _, context = component
    previous_automatic = settings.checkAppUpdates
    previous_view = window.property("activeView")
    try:
        settings.checkAppUpdates = automatic
        context.setContextProperty("updatesBridge", None)
        window.setProperty("activeView", "about")
        app.processEvents()
        about = states.find_object(window, "aboutContractSection")
        assert about.property("about") is None
        assert about.property("updates") is None
        assert about.updatesLine() == expected + " · нет сведений"
        assert expected + " · нет сведений" in states.onboarding.visible_texts(about)
    finally:
        settings.checkAppUpdates = previous_automatic
        context.setContextProperty("updatesBridge", updates)
        window.setProperty("activeView", previous_view)
        app.processEvents()


@pytest.mark.parametrize("updates_present", [False, True])
def test_about_without_settings_or_refusal_reports_unknown_update_mode(
    app: Any, component: Any, updates_present: bool
) -> None:
    window, updates, settings, _, context = component
    previous_view = window.property("activeView")
    try:
        context.setContextProperty("settingsBridge", None)
        if not updates_present:
            context.setContextProperty("updatesBridge", None)
        window.setProperty("activeView", "about")
        app.processEvents()
        about = states.find_object(window, "aboutContractSection")
        assert about.updatesLine() == "Нет сведений"
        assert "Нет сведений" in states.onboarding.visible_texts(about)
    finally:
        context.setContextProperty("settingsBridge", settings)
        context.setContextProperty("updatesBridge", updates)
        window.setProperty("activeView", previous_view)
        app.processEvents()


def test_compact_states_keep_action_inside_panel_and_wrap_at_narrow_width(
    app: Any, component: Any
) -> None:
    window, updates, _, _, _ = component
    panel = states.find_object(window, "updatePanel")
    button = states.find_object(window, "checkNowButton")
    for state, title in (
        ("idle", "Установлена версия"),
        ("checking", "Проверяю обновления…"),
        ("uptodate", "Установлена последняя версия"),
        ("unavailable", "Источник обновлений недоступен"),
        ("error-net", "Не удалось проверить сведения об обновлении"),
        ("skipped", "Версия"),
    ):
        updates.update(state=state, manual=True, version="0.2.1")
        QTest.qWait(30)
        assert panel.isVisible() and button.isVisible()
        assert button.property("enabled") is (state != "checking")
        assert states.find_object(window, "updatePanelTitle").property("text") == title
        parent = button.parentItem()
        while parent is not None and parent is not panel:
            parent = parent.parentItem()
        assert parent is panel
    updates.update(state="idle", manual=False)
    QTest.qWait(30)
    app.processEvents()
    assert panel.height() == 74
    window.setWidth(400)
    QTest.qWait(30)
    heading = states.find_object(window, "updatePanelHeading")
    assert button.x() == 0
    assert button.y() >= heading.y() + heading.height() + 12
    assert button.width() <= panel.width() - 32
    assert updates.calls == []


@pytest.mark.parametrize("reason", ["offline", "policy", "admin"])
def test_compact_refusal_explains_disabled_action(component: Any, reason: str) -> None:
    window, updates, _, _, _ = component
    updates.update(checkRefusal=reason, canCheckNow=False)
    QTest.qWait(30)
    button = states.find_object(window, "checkNowButton")
    refusal = states.find_object(window, "updateCheckRefusal")
    assert button.isVisible() and button.property("enabled") is False
    assert refusal.isVisible()
    assert refusal.property("text") == (
        "Проверить сейчас нельзя: включён офлайн-режим."
        if reason == "offline"
        else "Проверить сейчас нельзя: задано администратором."
    )
    assert refusal.property("textFormat") == 0
    assert updates.calls == []


def test_missing_bridge_keeps_disabled_action_and_reason(component: Any) -> None:
    window, updates, _, _, context = component
    context.setContextProperty("updatesBridge", None)
    QTest.qWait(30)
    panel = states.find_object(window, "updatePanel")
    assert panel.isVisible()
    assert "Обновление программы" in states.onboarding.visible_texts(panel)
    assert "Проверка пока недоступна" in states.onboarding.visible_texts(panel)
    button = states.find_object(window, "checkNowButton")
    assert button.isVisible() and button.property("enabled") is False
    assert updates.calls == []


@pytest.mark.parametrize(
    "name,field",
    [
        ("checkAppUpdatesToggle", "checkAppUpdates"),
        ("checkModelUpdatesToggle", "checkModelUpdates"),
    ],
)
@pytest.mark.parametrize(
    "component,mode",
    [({"mode": mode}, mode) for mode in ("ordinary", "offline", "policy")],
    indirect=["component"],
)
def test_both_update_preferences_are_visible_and_preserve_user_choice(
    app: Any, component: Any, name: str, field: str, mode: str
) -> None:
    window, updates, settings, _, _ = component
    app.processEvents()
    button = states.find_object(window, name)
    assert button.isVisible() and button.property("enabled") is (mode == "ordinary")
    before = settings.property(field)
    states.onboarding.click_item(button)
    app.processEvents()
    assert settings.property(field) is (not before if mode == "ordinary" else before)
    assert updates.calls == []


@pytest.mark.parametrize("activation", ["mouse", "return", "space"])
def test_check_heading_keeps_focus_and_keyboard_ring_through_retained_result(
    app: Any, component: Any, activation: str
) -> None:
    window, updates, _, _, _ = component
    button = states.find_object(window, "checkNowButton")
    window.requestActivate()
    if activation == "mouse":
        states.onboarding.click_item(button)
    else:
        button.forceActiveFocus(Qt.TabFocusReason)
        QTest.keyClick(window, Qt.Key_Return if activation == "return" else Qt.Key_Space)
    updates.update(state="checking", manual=True)
    QTest.qWait(30)
    heading = states.find_object(window, "updatePanelHeading")
    ring = states.find_object(window, "updateHeadingFocusRing")
    assert heading.hasActiveFocus()
    assert ring.isVisible() is (activation != "mouse")
    updates.update(state="uptodate", manual=True, checkedText="Проверено сегодня в 14:02")
    QTest.qWait(3200)
    assert heading.hasActiveFocus()
    assert ring.isVisible() is (activation != "mouse")
    assert "Установлена последняя версия" in states.onboarding.visible_texts(
        states.find_object(window, "updatePanel")
    )
    QTest.keyClick(window, Qt.Key_Return)
    app.processEvents()
    assert updates.calls == ["checkNow"]


@pytest.mark.parametrize("dark", [False, True], ids=["light", "dark"])
@pytest.mark.parametrize(
    "mode,title",
    [
        ("on", "В сеть программа ходит только проверить обновления"),
        ("off", "Пока оба переключателя выключены, программа не выходит в сеть"),
        ("policy", "Проверку обновлений отключил администратор"),
    ],
)
def test_onboarding_step1_disclosure_and_snapshots(
    app: Any, component: Any, dark: bool, mode: str, title: str
) -> None:
    window, _, settings, onboarding, context = component
    theme = states.onboarding.FakeTheme(dark)
    context.setContextProperty("themeSource", theme)
    window.setProperty("activeView", "onboarding")
    window.setWidth(900)
    window.setHeight(588)
    onboarding.checkAppUpdates = onboarding.checkModelUpdates = mode == "on"
    onboarding.policyLocked = mode == "policy"
    settings.lockedSettings = (
        ["check_app_updates", "check_model_updates"] if mode == "policy" else []
    )
    try:
        QTest.qWait(50)
        texts = states.onboarding.visible_texts(window.contentItem())
        assert title in texts
        if mode == "policy":
            assert "Задано администратором" in texts
        else:
            assert "Раз в сутки, github.com. Скачивание — только по вашему нажатию." in texts
            assert "huggingface.co. Скачивание — только по вашему нажатию." in texts
        image = states.grab(app, window, 900, 588)
        path = states.save_snapshot(
            image, f"onboarding-015-{mode}-{'dark' if dark else 'light'}.png"
        )
        states.onboarding.assert_saved_snapshot(path, image)
    finally:
        context.setContextProperty("themeSource", None)
        sip.delete(theme)


def test_deb_defaults_reach_step1_and_about(app: Any, component: Any, tmp_path: Path) -> None:
    window, _, _, _, context = component
    stored = settings_mod.load(tmp_path / "settings.json", appimage=False)
    bridge = SettingsBridge(stored, save=lambda _settings: None)
    controller = OnboardingController(bridge, settings=stored, device_provider=lambda: [])
    context.setContextProperty("settingsBridge", bridge)
    context.setContextProperty("onboarding", controller)
    try:
        window.setProperty("activeView", "onboarding")
        QTest.qWait(50)
        texts = states.onboarding.visible_texts(window.contentItem())
        assert "Пока оба переключателя выключены, программа не выходит в сеть" in texts
        rows = [
            item
            for item in states.onboarding.visual_tree(window.contentItem())
            if item.isVisible()
            and item.property("label")
            in ("Проверять обновления утилиты", "Проверять обновления моделей")
        ]
        assert len(rows) == 2
        for row in rows:
            assert row.property("toggle").property("checked") is False
            assert row.property("toggle").property("locked") is False
        assert stored.check_app_updates is stored.check_model_updates is False
        window.setProperty("activeView", "about")
        QTest.qWait(30)
        texts = states.onboarding.visible_texts(window.contentItem())
        assert "Только вручную · нет сведений" in texts
    finally:
        context.setContextProperty("onboarding", None)
        context.setContextProperty("settingsBridge", None)
        sip.delete(controller)
        sip.delete(bridge)
        app.processEvents()


@pytest.mark.parametrize(
    "locked_field,editable_label,locked_label,host",
    [
        (
            "check_app_updates",
            "Проверять обновления моделей",
            "Проверять обновления утилиты",
            "github.com",
        ),
        (
            "check_model_updates",
            "Проверять обновления утилиты",
            "Проверять обновления моделей",
            "huggingface.co",
        ),
    ],
)
@pytest.mark.parametrize("policy_value", [False, True])
def test_step1_single_policy_lock_allows_other_preference_to_be_disabled(
    app: Any,
    component: Any,
    locked_field: str,
    editable_label: str,
    locked_label: str,
    host: str,
    policy_value: bool,
) -> None:
    window, _, settings, onboarding, _ = component
    window.setProperty("activeView", "onboarding")
    settings.lockedSettings = [locked_field]
    onboarding.policyLocked = True
    locked_preference = (
        "checkAppUpdates" if locked_field == "check_app_updates" else "checkModelUpdates"
    )
    setattr(onboarding, locked_preference, policy_value)
    app.processEvents()
    rows = {
        control.property("label"): control
        for control in states.onboarding.visual_tree(window.contentItem())
        if control.isVisible() and control.property("label") in (editable_label, locked_label)
    }
    editable_row, locked_row = rows[editable_label], rows[locked_label]
    assert not editable_row.property("locked") and editable_row.property("rowEnabled")
    assert locked_row.property("locked") and not locked_row.property("rowEnabled")
    expected_sub = (
        ("Раз в сутки, " if locked_field == "check_app_updates" else "")
        + host
        + ". Скачивание — только по вашему нажатию."
        if policy_value
        else "Администратор отключил проверку обновлений на этом компьютере."
    )
    assert locked_row.property("sub") == expected_sub
    states.onboarding.click_item(editable_row.property("toggle"))
    app.processEvents()
    editable_preference = (
        "checkModelUpdates" if locked_field == "check_app_updates" else "checkAppUpdates"
    )
    assert getattr(onboarding, editable_preference) is False
    assert getattr(onboarding, locked_preference) is policy_value
    texts = states.onboarding.visible_texts(window.contentItem())
    expected_banner = (
        "В сеть программа ходит только проверить обновления"
        if policy_value
        else "Пока оба переключателя выключены, программа не выходит в сеть"
    )
    assert expected_banner in texts
    assert "Проверку обновлений отключил администратор" not in texts


@pytest.mark.parametrize(
    "component,banner",
    [
        ({"mode": "offline", "available": True}, "Включён офлайн-режим"),
        ({"mode": "policy", "available": True}, "Обновления ограничены администратором"),
    ],
    indirect=["component"],
    ids=["offline", "admin"],
)
def test_found_release_keeps_its_banner_without_compact_check_refusal(
    component: Any, banner: str
) -> None:
    window, updates, _, _, _ = component
    panel = states.find_object(window, "updatePanel")
    texts = states.onboarding.visible_texts(panel)
    assert "Доступна версия" in texts
    assert banner in texts
    assert not states.find_object(window, "updateCheckRefusal").isVisible()
    assert not states.find_object(window, "checkNowButton").isVisible()
    assert not any("Проверить сейчас нельзя:" in text for text in texts)
    assert updates.calls == []


@pytest.mark.parametrize(
    "component,prefix",
    [
        ({}, "Автоматически раз в сутки"),
        ({"settings": {"_checkAppUpdates": False}}, "Только вручную"),
        (
            {
                "settings": {"_checkAppUpdates": False, "_lockedSettings": ["check_app_updates"]},
                "updates": {"checkRefusal": "policy"},
            },
            "Отключена администратором",
        ),
        (
            {"settings": {"_checkAppUpdates": True, "_lockedSettings": ["check_app_updates"]}},
            "Автоматически раз в сутки",
        ),
        ({"mode": "offline"}, "Отключена: офлайн-режим"),
    ],
    indirect=["component"],
    ids=["automatic", "manual", "locked-false", "locked-true", "offline"],
)
def test_about_has_no_check_action_and_reports_state_without_about_bridge(
    component: Any, prefix: str
) -> None:
    window, updates, _, _, _ = component
    window.setProperty("activeView", "about")
    QTest.qWait(30)
    about = states.find_object(window, "aboutContractSection")
    controls = list(states.onboarding.visual_tree(about))
    texts = states.onboarding.visible_texts(about)
    assert about.isVisible()
    assert not any(control.objectName() == "aboutCheckUpdates" for control in controls)
    assert "Проверить обновления" not in texts
    assert "Обновления…" not in texts
    row = next(
        control for control in controls if control.property("label") == "Проверка обновлений"
    )
    assert row.property("sub") == prefix + " · нет сведений"
    assert prefix + " · нет сведений" in texts
    privacy = next(
        control
        for control in controls
        if control.property("label") == "Приватность и сетевые хосты"
    )
    assert privacy.property("sub") == (
        "Звук и текст не покидают компьютер и не сохраняются на диск. "
        "Сеть нужна только для скачивания моделей с huggingface.co "
        "(или с зеркала администратора) и проверки новых версий на github.com — "
        "по кнопке и раз в сутки, если автопроверка включена "
        "(в «Сеть и обновления»; в AppImage включена по умолчанию)."
    )
    assert updates.calls == []
