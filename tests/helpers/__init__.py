"""Мишени S4 и управление их временем жизни на изолированном X11."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from astra_voice.platform.x11 import X11Display


def wait_until(condition: Callable[[], bool], *, timeout: float = 5.0) -> None:
    """Ждать условие, обслуживая запросы CLIPBOARD в GUI-потоке."""
    deadline = time.monotonic() + timeout
    while True:
        qt_widgets = sys.modules.get("PyQt5.QtWidgets")
        app = qt_widgets.QApplication.instance() if qt_widgets is not None else None
        if app is not None:
            app.processEvents()
        if condition():
            return
        assert time.monotonic() < deadline, "истёк срок ожидания мишени X11"
        time.sleep(0.005)


@pytest.fixture(scope="session")
def xapp() -> Any:
    """Один QApplication с настоящим xcb, независимо от offscreen у QML-тестов."""
    from PyQt5.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["astra-xvfb-tests", "-platform", "xcb"])
    assert isinstance(app, QApplication)
    assert app.platformName() == "xcb", "запустите xvfb-тесты отдельно от offscreen-тестов"
    app.setQuitOnLastWindowClosed(False)
    return app


@pytest.fixture
def xdisplay() -> Iterator[X11Display]:
    """Закрыть соединение даже при ошибке и вернуть EWMH-свойство стенда."""
    from Xlib import Xatom

    x = X11Display()
    try:
        assert x.open(), "не удалось открыть изолированный DISPLAY"
        atom = x.d.intern_atom("_NET_ACTIVE_WINDOW")
        previous = x.root.get_full_property(atom, Xatom.WINDOW)
        try:
            yield x
        finally:
            if previous is None:
                x.root.delete_property(atom)
            else:
                x.root.change_property(atom, Xatom.WINDOW, 32, previous.value)
            x.d.sync()
    finally:
        x.ungrab_keyboard()
        x.close()


@contextmanager
def helper_process(script: str, directory: Path, *args: str) -> Iterator[subprocess.Popen[bytes]]:
    """Запуск без оболочки; terminate/kill и wait обязательны при любом выходе."""
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "xcb",
        "QT_QPA_PLATFORMTHEME": "",
        "QT_ACCESSIBILITY": "0",
        "DBUS_SESSION_BUS_ADDRESS": f"unix:path={directory / 'no-session-bus'}",
    }
    with (directory / f"{script}.log").open("wb") as log:
        process = subprocess.Popen(
            [sys.executable, str(Path(__file__).with_name(script)), *args],
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            yield process
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)


@dataclass
class PasteTarget:
    """Настоящее окно; содержимое читается только из периодического дампа."""

    process: subprocess.Popen[bytes]
    output: Path
    ready: Path
    window: int

    def dumps(self) -> int:
        assert self.process.poll() is None, "мишень завершилась; см. paste_target.py.log"
        return int(json.loads(self.ready.read_text(encoding="utf-8"))["dumps"])

    def text(self) -> str:
        return self.output.read_text(encoding="utf-8")

    def expect_text(self, expected: str) -> None:
        """Даже пустоту проверять по новому дампу, а не по стартовому файлу."""
        before = self.dumps()
        wait_until(lambda: self.dumps() > before and self.text() == expected)
        assert self.text() == expected

    def activate(self, x: X11Display) -> None:
        """Дождаться активного окна и фокуса; без WM стенд сам публикует EWMH."""
        from Xlib import X, Xatom
        from Xlib.protocol import event

        window = x.d.create_resource_object("window", self.window)
        atom = x.d.intern_atom("_NET_ACTIVE_WINDOW")
        wm = x.root.get_full_property(x.d.intern_atom("_NET_SUPPORTING_WM_CHECK"), Xatom.WINDOW)
        if wm is None:
            window.configure(stack_mode=X.Above)
            window.set_input_focus(X.RevertToParent, X.CurrentTime)
            x.root.change_property(atom, Xatom.WINDOW, 32, [self.window])
        else:
            x.root.send_event(
                event.ClientMessage(
                    window=self.window,
                    client_type=atom,
                    data=(32, [2, X.CurrentTime, 0, 0, 0]),
                ),
                event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask,
            )
        x.d.sync()
        active: int | None = None
        focus: int | None = None

        def activated() -> bool:
            nonlocal active, focus
            active = x.active_window()
            observed = x.d.get_input_focus().focus
            focus = observed if isinstance(observed, int) else int(observed.id)
            return active == self.window and focus == self.window

        try:
            wait_until(activated, timeout=2.0)
        except AssertionError as exc:
            raise AssertionError(
                f"мишень X11 {self.window} не активировалась за 2 с: "
                f"_NET_ACTIVE_WINDOW={active}, input_focus={focus}"
            ) from exc


@contextmanager
def paste_target(directory: Path) -> Iterator[PasteTarget]:
    """Публикация winId и первого дампа означает готовность окна."""
    directory.mkdir(exist_ok=True)
    output, ready = directory / "text.txt", directory / "ready.json"
    with helper_process(
        "paste_target.py", directory, "--out", str(output), "--ready", str(ready)
    ) as process:

        def started() -> bool:
            assert process.poll() is None, "мишень не запустилась; см. paste_target.py.log"
            return ready.exists()

        wait_until(started)
        window = int(json.loads(ready.read_text(encoding="utf-8"))["window"])
        yield PasteTarget(process, output, ready, window)


@contextmanager
def clipboard_owner(directory: Path, text: str, *, secret: bool = False) -> Iterator[None]:
    """Отдельный клиент остаётся владельцем до вставки, затем не переиздаёт буфер."""
    directory.mkdir(exist_ok=True)
    ready = directory / "ready"
    args = ["--text", text, "--html", f"<b>{text}</b>", "--ready", str(ready)]
    if secret:
        args.append("--secret")
    with helper_process("clip_owner.py", directory, *args) as process:

        def started() -> bool:
            assert process.poll() is None, "владелец не запустился; см. clip_owner.py.log"
            return ready.exists()

        wait_until(started)
        yield
