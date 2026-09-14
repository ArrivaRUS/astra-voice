"""Цепочка S4: настоящий CLIPBOARD, XTest и QPlainTextEdit отдельного процесса."""

from __future__ import annotations

import os
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from astra_voice.platform.paste import (
    KDE_HINT,
    PasteFlow,
    PasteMethod,
    PasteMode,
    PasteOutcome,
    PasteOutcomeKind,
    PasteRestore,
)
from astra_voice.platform.x11 import X11Display

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers import clipboard_owner, paste_target, wait_until  # noqa: E402
from helpers import xapp as xapp  # noqa: E402
from helpers import xdisplay as xdisplay  # noqa: E402

pytestmark = pytest.mark.xvfb

if os.environ.get("DISPLAY", "").strip() in {"", ":0", ":0.0"}:
    pytest.skip(
        "тест требует изолированного X: на :0 живая сессия заказчика", allow_module_level=True
    )
pytest.importorskip("Xlib")


@pytest.fixture
def clipboard(xapp: Any) -> Iterator[Any]:
    """Буфер каждого сценария очищается и при исключении теста."""
    cb = xapp.clipboard()
    try:
        cb.clear()
        yield cb
    finally:
        cb.clear()
        xapp.processEvents()


def text_formats(clipboard: Any) -> dict[str, str]:
    """S4-R8: сравнивать текстовые форматы, не набор и байты всех MIME-типов."""
    mime = clipboard.mimeData()
    if mime is None:
        return {}
    return {
        str(fmt): bytes(mime.data(fmt)).decode("utf-8")
        for fmt in mime.formats()
        if fmt.startswith("text/")
    }


def test_normal_paste_restores_text_twice(
    tmp_path: Path, clipboard: Any, xdisplay: X11Display
) -> None:
    """Повторная операция в том же процессе вставляет ровно фразу и возвращает text/*."""
    with paste_target(tmp_path / "target") as target:
        with clipboard_owner(tmp_path / "clipboard", "Исходный буфер, ёж."):
            wait_until(lambda: clipboard.text() == "Исходный буфер, ёж.")
            original_text = str(clipboard.text())
            original_formats = text_formats(clipboard)
            assert {"text/plain", "text/html"} <= original_formats.keys()
            target.activate(xdisplay)
            flow = PasteFlow()
            for count in (1, 2):
                outcome = flow.run("Диктовка\r\nёж.\x0f", target.window, PasteMode.AUTO)
                assert outcome.kind == PasteOutcomeKind.PASTED
                assert outcome.method == PasteMethod.CTRL_V
                assert outcome.wm_class == ("astra-paste-target", "astra-paste-target")
                assert outcome.restore == PasteRestore.RESTORED
                target.expect_text("Диктовка ёж." * count)
                assert clipboard.text() == original_text
                restored = text_formats(clipboard)
                assert all(restored.get(fmt) == value for fmt, value in original_formats.items())


def test_secret_is_cleared_not_republished(
    tmp_path: Path, clipboard: Any, xdisplay: X11Display
) -> None:
    """T-14/У40: секрет независимого владельца никогда не переиздаётся цепочкой."""
    secret = "СЕКРЕТ-ГЕЛИОТРОП-7"
    with paste_target(tmp_path / "target") as target:
        with clipboard_owner(tmp_path / "clipboard", secret, secret=True):
            wait_until(lambda: clipboard.text() == secret)
            assert bytes(clipboard.mimeData().data(KDE_HINT)) == b"secret"
            observed: list[str] = []

            def changed() -> None:
                observed.append(str(clipboard.text()))

            clipboard.dataChanged.connect(changed)
            try:
                target.activate(xdisplay)
                outcome = PasteFlow().run("Безопасная фраза", target.window, PasteMode.AUTO)
                assert outcome.kind == PasteOutcomeKind.REFUSED_SECRET
                assert outcome.restore == PasteRestore.CLEARED_SECRET
                target.expect_text("Безопасная фраза")
                assert clipboard.text() == ""
                assert not text_formats(clipboard)
                mime = clipboard.mimeData()
                assert mime is None or not bytes(mime.data(KDE_HINT))
                assert "Безопасная фраза" in observed
                assert secret not in observed
            finally:
                clipboard.dataChanged.disconnect(changed)


def test_window_changed_sends_no_xtest(
    tmp_path: Path, clipboard: Any, xdisplay: X11Display
) -> None:
    """T-15/У12: второе поднятое окно запрещает ввод в обе мишени."""
    with paste_target(tmp_path / "first") as first:
        first.activate(xdisplay)
        remembered = xdisplay.active_window()
        assert remembered == first.window
        with paste_target(tmp_path / "second") as second:
            second.activate(xdisplay)
            assert xdisplay.active_window() != remembered
            with patch.object(
                X11Display, "send_combo", autospec=True, side_effect=X11Display.send_combo
            ) as send:
                outcome = PasteFlow().run("Не вводить", remembered, PasteMode.AUTO)
                send.assert_not_called()
            assert outcome.kind == PasteOutcomeKind.WINDOW_CHANGED
            assert outcome.method == PasteMethod.NONE
            assert outcome.restore == PasteRestore.KEPT_OURS
            assert clipboard.text() == "Не вводить"
            first.expect_text("")
            second.expect_text("")


def test_controls_never_reach_target(tmp_path: Path, clipboard: Any, xdisplay: X11Display) -> None:
    """T-15: ни перевода строки, ни C0/C1 в реально полученном тексте."""
    with paste_target(tmp_path / "target") as target:
        target.activate(xdisplay)
        outcome = PasteFlow().run("один\n\x1b[31mд\x0fва", target.window, PasteMode.AUTO)
        assert outcome.kind == PasteOutcomeKind.PASTED
        target.expect_text("один [31mдва")
        assert "\n" not in target.text()
        assert all(ord(ch) >= 0x20 and not 0x7F <= ord(ch) <= 0x9F for ch in target.text())


def test_clipboard_only_has_no_keyboard_input(
    tmp_path: Path, clipboard: Any, xdisplay: X11Display
) -> None:
    """Ручной режим оставляет фразу в буфере для последующей вставки пользователем."""
    from PyQt5.QtCore import QTimer

    with paste_target(tmp_path / "target") as target:
        target.activate(xdisplay)
        clipboard.setText("До вставки")
        observed: list[str] = []
        timer = QTimer()
        timer.setInterval(5)
        timer.timeout.connect(lambda: observed.append(str(clipboard.text())))
        try:
            timer.start()
            with patch.object(
                X11Display, "send_combo", autospec=True, side_effect=X11Display.send_combo
            ) as send:
                outcome = PasteFlow().run("Только\nбуфер", target.window, PasteMode.CLIPBOARD_ONLY)
                send.assert_not_called()
        finally:
            timer.stop()
        assert outcome.kind == PasteOutcomeKind.CLIPBOARD_ONLY
        assert outcome.method == PasteMethod.NONE
        assert "Только буфер" in observed
        assert outcome.restore == PasteRestore.KEPT_OURS
        assert clipboard.text() == "Только буфер"
        target.expect_text("")


@pytest.mark.parametrize("mode", [PasteMode.AUTO, PasteMode.CLIPBOARD_ONLY])
def test_secret_not_republished_when_phrase_is_kept(
    tmp_path: Path, clipboard: Any, xdisplay: X11Display, mode: PasteMode
) -> None:
    """Оба исхода без XTest оставляют фразу, не переиздавая чужой секрет."""
    secret = "ИСХОДНЫЙ-СЕКРЕТ"
    with clipboard_owner(tmp_path / "clipboard", secret, secret=True):
        wait_until(lambda: clipboard.text() == secret)
        observed: list[str] = []

        def changed() -> None:
            observed.append(str(clipboard.text()))

        clipboard.dataChanged.connect(changed)
        try:
            with patch.object(X11Display, "send_combo") as send:
                outcome = PasteFlow().run("Наша фраза", None, mode)
            send.assert_not_called()
            assert outcome.kind == (
                PasteOutcomeKind.WINDOW_CHANGED
                if mode == PasteMode.AUTO
                else PasteOutcomeKind.CLIPBOARD_ONLY
            )
            assert outcome.restore == PasteRestore.KEPT_OURS
            assert clipboard.text() == "Наша фраза"
            assert "Наша фраза" in observed
            assert secret not in observed
        finally:
            clipboard.dataChanged.disconnect(changed)


@pytest.mark.parametrize("focus_kind", ["same-class", "locker"])
def test_focus_changed_with_same_active_window_blocks_xtest(
    tmp_path: Path, clipboard: Any, xdisplay: X11Display, focus_kind: str
) -> None:
    """Чужой фокус или захват запрещает XTest независимо от переходного состояния EWMH.

    KWin возвращает фокус активному окну и недетерминированно снимает _NET_ACTIVE_WINDOW.
    """
    from Xlib import X

    with paste_target(tmp_path / "target") as target:
        target.activate(xdisplay)
        window = xdisplay.root.create_window(
            0,
            0,
            100,
            100,
            0,
            X.CopyFromParent,
            # Попап не становится отдельным управляемым клиентом WM.
            override_redirect=True,
            event_mask=X.KeyPressMask | X.KeyReleaseMask,
        )

        def assert_active_window_allowed() -> None:
            active = xdisplay.active_window()
            assert active != int(window.id), (
                f"Активное окно стало соперником: _NET_ACTIVE_WINDOW={active}, "
                f"мишень={target.window}, соперник={int(window.id)}"
            )
            assert active in (target.window, None), (
                f"Допустимы мишень или пустое EWMH: _NET_ACTIVE_WINDOW={active}, "
                f"мишень={target.window}, соперник={int(window.id)}"
            )

        try:
            name = "kscreenlocker" if focus_kind == "locker" else "astra-paste-target"
            window.set_wm_class(name, name)
            assert int(window.id) != target.window
            if focus_kind == "same-class":
                assert window.get_wm_class() == xdisplay.wm_class(target.window)
            window.map()
            window.set_input_focus(X.RevertToParent, X.CurrentTime)
            xdisplay.d.sync()
            assert_active_window_allowed()
            input_focus: int | None = None

            def rival_has_focus() -> bool:
                nonlocal input_focus
                focus = xdisplay.d.get_input_focus().focus
                input_focus = focus if isinstance(focus, int) else int(focus.id)
                return input_focus == int(window.id)

            try:
                wait_until(rival_has_focus, timeout=0.2)
            except AssertionError:
                assert input_focus == target.window, (
                    f"Фокус не достался сопернику и не вернулся мишени: input_focus={input_focus}, "
                    f"мишень={target.window}, соперник={int(window.id)}"
                )
            # При возврате фокуса доставляем сцену захватом; в успешной ветке сохраняем
            # его тоже: KWin может вернуть фокус уже после последнего XGetInputFocus.
            status = window.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
            assert status == X.GrabSuccess
            assert_active_window_allowed()
            with patch.object(X11Display, "send_combo") as send:
                outcome = PasteFlow().run("Не вводить", target.window, PasteMode.AUTO)
            send.assert_not_called()
            assert outcome.kind == PasteOutcomeKind.WINDOW_CHANGED
            assert outcome.method == PasteMethod.NONE
            assert outcome.restore == PasteRestore.KEPT_OURS
            assert clipboard.text() == "Не вводить"
            target.expect_text("")
            assert_active_window_allowed()
        finally:
            xdisplay.d.ungrab_keyboard(X.CurrentTime)
            window.destroy()
            xdisplay.d.sync()


def test_active_keyboard_grab_without_focus_change_blocks_xtest(
    tmp_path: Path, clipboard: Any, xdisplay: X11Display
) -> None:
    """T-45/У46: grab запрещает XTest; после снятия своя фраза не считается чужим secret."""
    from Xlib import X

    with paste_target(tmp_path / "target") as target, X11Display() as rival:
        assert rival.d is not None
        target.activate(xdisplay)
        wm_class = xdisplay.wm_class(target.window)
        assert wm_class is not None
        clipboard.setText("Прежний буфер")
        window = rival.root.create_window(
            0,
            0,
            100,
            100,
            0,
            X.CopyFromParent,
            override_redirect=True,
            event_mask=X.KeyPressMask | X.KeyReleaseMask,
        )
        try:
            window.map()
            rival.d.sync()
            # Попап намеренно не вызывает set_input_focus.
            assert (
                window.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime)
                == X.GrabSuccess
            )
            rival.d.sync()
            while rival.pending_events():
                rival.next_event()
            assert xdisplay.active_window() == target.window
            assert int(xdisplay.d.get_input_focus().focus.id) == target.window
            assert xdisplay.wm_class(target.window) == wm_class
            with patch.object(X11Display, "send_combo") as send:
                outcome = PasteFlow().run("Наша фраза", target.window, PasteMode.AUTO)
            send.assert_not_called()
            assert outcome.kind == PasteOutcomeKind.WINDOW_CHANGED
            assert outcome.restore == PasteRestore.KEPT_OURS
            assert clipboard.text() == "Наша фраза"
            assert clipboard.ownsClipboard()
            assert bytes(clipboard.mimeData().data(KDE_HINT)) == b"secret"
            assert xdisplay.active_window() == target.window
            assert int(xdisplay.d.get_input_focus().focus.id) == target.window
            assert xdisplay.wm_class(target.window) == wm_class
            rival.d.sync()
            assert rival.pending_events() == 0
            target.expect_text("")

            rival.d.ungrab_keyboard(X.CurrentTime)
            rival.d.sync()
            # Между диктовками никто не копирует: в буфере осталась первая наша фраза.
            outcome = PasteFlow().run("Вторая фраза", target.window, PasteMode.AUTO)
            assert outcome.kind == PasteOutcomeKind.PASTED
            assert outcome.restore == PasteRestore.RESTORED
            target.expect_text("Вторая фраза")
            assert clipboard.text() == "Наша фраза"
            assert bytes(clipboard.mimeData().data(KDE_HINT)) == b"secret"
            rival.d.sync()
            assert rival.pending_events() == 0
        finally:
            rival.d.ungrab_keyboard(X.CurrentTime)
            window.destroy()
            rival.d.sync()


def test_keyboard_probe_preserves_target_focus_and_delivery(
    tmp_path: Path, clipboard: Any, xdisplay: X11Display
) -> None:
    """T-45: проба на корне не требует повторной установки фокуса для доставки Ctrl+V."""
    with paste_target(tmp_path / "target") as target:
        target.activate(xdisplay)
        assert int(xdisplay.d.get_input_focus().focus.id) == target.window
        grabbed = xdisplay.grab_keyboard()
        xdisplay.ungrab_keyboard()
        assert grabbed
        assert xdisplay.keyboard_grab_deadline is None
        assert int(xdisplay.d.get_input_focus().focus.id) == target.window
        assert xdisplay.active_window() == target.window
        clipboard.setText("После пробы")
        assert xdisplay.send_combo(["Control_L"], "v")
        target.expect_text("После пробы")


@pytest.mark.parametrize("secret", [False, True])
def test_reentrant_qt_timer_returns_busy_and_outer_finishes(
    tmp_path: Path, clipboard: Any, xdisplay: X11Display, secret: bool
) -> None:
    """Повторный вход из Qt-слота в обеих паузах не роняет процесс и не меняет буфер."""
    from PyQt5.QtCore import QTimer

    with paste_target(tmp_path / "target") as target:
        with clipboard_owner(tmp_path / "clipboard", "Исходный буфер", secret=secret):
            wait_until(lambda: clipboard.text() == "Исходный буфер")
            target.activate(xdisplay)
            nested: list[tuple[PasteOutcome, str, str]] = []
            observed: list[str] = []

            def changed() -> None:
                observed.append(str(clipboard.text()))

            def reenter() -> None:
                before = str(clipboard.text())
                outcome = PasteFlow().run("Вторая фраза", target.window, PasteMode.AUTO)
                nested.append((outcome, before, str(clipboard.text())))

            timers = [QTimer(), QTimer()]
            clipboard.dataChanged.connect(changed)
            try:
                for timer, delay in zip(timers, (10, 80), strict=True):
                    timer.setSingleShot(True)
                    timer.timeout.connect(reenter)
                    timer.start(delay)
                outcome = PasteFlow().run("Первая фраза", target.window, PasteMode.AUTO)
            finally:
                for timer in timers:
                    timer.stop()
                clipboard.dataChanged.disconnect(changed)
            assert len(nested) == 2
            for inner, before, after in nested:
                assert inner.kind == PasteOutcomeKind.BUSY
                assert inner.method == PasteMethod.NONE
                assert inner.restore == PasteRestore.KEPT_OURS
                assert before == after == "Первая фраза"
            assert outcome.kind == (
                PasteOutcomeKind.REFUSED_SECRET if secret else PasteOutcomeKind.PASTED
            )
            assert outcome.restore == (
                PasteRestore.CLEARED_SECRET if secret else PasteRestore.RESTORED
            )
            assert clipboard.text() == ("" if secret else "Исходный буфер")
            if secret:
                assert "Исходный буфер" not in observed
            assert "Вторая фраза" not in observed
            target.expect_text("Первая фраза")
