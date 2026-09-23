"""Векторы S4 и T-14/T-15: подмены X/буфера, Qt-пробы в отдельном процессе."""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path
from textwrap import dedent
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from astra_voice.platform import paste, session
from astra_voice.platform.paste import PasteMethod, PasteMode, PasteOutcomeKind, PasteRestore

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("ls\n rm -rf ~", "ls  rm -rf ~"),
        ("a\r\nb", "a b"),
        ("\x1b[31mкрасный\x1b[0m", "[31mкрасный[0m"),
        ("оп\x0fерация", "операция"),
        ("хвост\x9b", "хвост"),
        ("Проверка связи, ёж.", "Проверка связи, ёж."),
        ("a\rb", "a b"),
        ("\x1b[31mоп\x0fерация", "[31mоперация"),
        ("", ""),
    ],
)
def test_normalize_vectors(source: str, expected: str) -> None:
    result = paste.normalize(source)
    assert result == expected
    assert all(ord(ch) >= 0x20 and not 0x7F <= ord(ch) <= 0x9F for ch in result)


def test_normalize_all_byte_codes() -> None:
    result = paste.normalize("".join(chr(code) for code in range(0x100)))
    assert all(ord(ch) >= 0x20 and not 0x7F <= ord(ch) <= 0x9F for ch in result)
    assert result == "  " + "".join(chr(code) for code in (*range(32, 127), *range(160, 256)))


@pytest.mark.parametrize(
    ("wm_class", "expected"),
    [
        *[
            (name, "ctrl+shift+v")
            for name in (
                "konsole",
                "fly-term",
                "flyterm",
                "yakuake",
                "alacritty",
                "gnome-terminal-server",
                "xfce4-terminal",
                "KoNsOlE",
            )
        ],
        *[
            (name, "shift+insert")
            for name in (
                "xterm",
                "uxterm",
                "rxvt",
                "urxvt",
                "foo-terminal",
                "XTerm",
                "new-tty",
                "console-x",
            )
        ],
        ("kate", "ctrl+v"),
        (None, "ctrl+v"),
        (("custom", "Konsole"), "ctrl+shift+v"),
        (("custom", "XTerm"), "shift+insert"),
        (("custom", "Foo-Terminal"), "shift+insert"),
        (("kate", "Kate"), "ctrl+v"),
    ],
)
def test_method_for_wm_class(wm_class: str | tuple[str, str] | None, expected: str) -> None:
    result = paste.method_for_wm_class(wm_class)
    assert isinstance(result, PasteMethod)
    assert result.value == expected


class FakeClipboard:
    """Байтовые копии двух буферов и независимые признаки владения."""

    def __init__(self) -> None:
        self.data = {
            False: {"text/plain": b"before", "text/html": b"<b>before</b>", "custom": b"\x00\xff"},
            True: {"text/plain": b"selection", "text/uri-list": b"file:///tmp/example"},
        }
        self.owned = {False: False, True: False}
        self.writes: list[tuple[bool, dict[str, bytes]]] = []
        self.trackers: dict[bool, paste._FetchTracker | None] = {False: None, True: None}

    def snapshot(self, primary: bool) -> dict[str, bytes]:
        return self.data[primary].copy()

    def put(
        self,
        snapshot: dict[str, bytes],
        primary: bool,
        tracker: paste._FetchTracker | None = None,
    ) -> None:
        self.data[primary] = snapshot.copy()
        self.writes.append((primary, snapshot.copy()))
        self.trackers[primary] = tracker
        self.owned[primary] = True

    def owns(self, primary: bool) -> bool:
        return self.owned[primary]

    def clear(self, primary: bool) -> None:
        self.put({}, primary)


@pytest.fixture(autouse=True)
def isolated_session_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Сеанс и запомненный снимок независимы от предыдущего теста."""
    monkeypatch.setattr(paste, "_last_clipboard_snapshot", None)
    monkeypatch.setattr(paste, "_pending", None)
    paste._session_kind.cache_clear()
    yield
    paste._session_kind.cache_clear()


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> tuple[FakeClipboard, Mock, list[int]]:
    """Весь ввод/вывод подменён до вызова цепочки, включая определение сеанса."""
    cb = FakeClipboard()
    x = Mock()
    x.wm_class.return_value = ("kate", "kate")
    x.active_window.return_value = 42
    x.send_combo.return_value = True

    def send_combo(*_args: object) -> bool:
        if x.send_combo.return_value:
            tracker = cb.trackers[False]
            assert tracker is not None
            tracker.mark()
        return bool(x.send_combo.return_value)

    x.send_combo.side_effect = send_combo
    x.grab_keyboard.return_value = True
    x.keyboard_grab_deadline = None
    x.keys_held.return_value = False
    x.root.id = 2
    focus = x.d.get_input_focus.return_value.focus
    focus.id = 42
    focus.get_attributes.return_value.override_redirect = False
    focus.query_tree.return_value.parent = x.root
    delays: list[int] = []
    monkeypatch.setattr(paste, "_Clipboard", lambda: cb)
    monkeypatch.setattr(paste, "X11Display", lambda: x)
    monkeypatch.setattr(paste, "_wait_ms", delays.append)
    monkeypatch.setattr(session, "_from_x11", Mock(side_effect=AssertionError("X запрещён")))
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.delenv("DESKTOP_SESSION", raising=False)
    return cb, x, delays


@pytest.mark.parametrize("pending_exists", [False, True])
@pytest.mark.parametrize(
    "text", ["", "строка\r\nещё\n\x1b\x0f\x7f\x85\x9fконец", "ёж\ud800\nконец"]
)
def test_publish_clipboard_normalizes_without_side_effects(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    text: str,
    pending_exists: bool,
) -> None:
    cb, x, delays = harness
    primary = cb.snapshot(True)
    pending = paste._PendingRestore({"text/plain": b"saved"}, False) if pending_exists else None
    monkeypatch.setattr(paste, "_pending", pending)
    forbidden = Mock(side_effect=AssertionError("чтение буфера, X и ожидания запрещены"))
    for name in ("snapshot", "owns", "clear"):
        monkeypatch.setattr(cb, name, forbidden)
    for name in ("X11Display", "_wait_ms", "_session_kind", "_fly_blacklist_type"):
        monkeypatch.setattr(paste, name, forbidden)

    assert paste.publish_clipboard(text, session_kind=session.SessionKind.KDE) is True

    expected = {
        "text/plain": paste.normalize(text).encode("utf-8", errors="replace"),
        paste.KDE_HINT: b"secret",
    }
    assert cb.writes == [(False, expected)]
    published = cb.data[False]["text/plain"].decode("utf-8")
    assert all(ord(ch) >= 0x20 and not 0x7F <= ord(ch) <= 0x9F for ch in published)
    assert cb.data[True] == primary
    assert paste._last_clipboard_snapshot == expected
    assert paste._last_clipboard_snapshot is not cb.data[False]
    assert paste._pending is pending
    if pending is not None:
        assert not pending.consumed
    assert delays == []
    assert x.mock_calls == []
    forbidden.assert_not_called()


@pytest.mark.parametrize("session_kind", list(session.SessionKind))
@pytest.mark.parametrize("types", ["Fly-Type", "text/plain:x-kde-passwordManagerHint"])
def test_publish_clipboard_mime_and_auto_restore(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    session_kind: session.SessionKind,
    types: str,
) -> None:
    """У59/У49: сеанс из аргумента, безопасный Fly-тип и возврат своей публикации."""
    cb, _, _ = harness
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    theme = tmp_path / "theme.themerc"
    theme.write_text(f'ClipboardManagerTypesBlacklist="{types}"\n', encoding="utf-8")
    monkeypatch.setattr(paste, "FLY_SYSTEM_THEME", theme)
    # Кэш намеренно противоречит аргументу; публикующий путь не должен читать его.
    cached = (
        session.SessionKind.KDE
        if session_kind == session.SessionKind.FLY
        else session.SessionKind.FLY
    )
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", cached.value)
    assert paste._session_kind(PasteMode.AUTO) == cached
    assert paste.publish_clipboard("Из\nтрея\x1b\x0f", session_kind=session_kind) is True
    expected = {"text/plain": "Из трея".encode(), paste.KDE_HINT: b"secret"}
    if session_kind == session.SessionKind.FLY:
        expected["Fly-Type" if types == "Fly-Type" else paste.FLY_FALLBACK_TYPE] = b""
    assert cb.writes == [(False, expected)]
    md = paste.restore_mime(cb.data[False])
    assert md.text() == "Из трея"
    assert paste.snapshot_mime(md) == expected

    outcome = paste.paste_text("Следующая диктовка", 42, PasteMode.AUTO)

    assert outcome.kind == PasteOutcomeKind.PASTED
    assert outcome.restore == PasteRestore.RESTORED
    assert cb.data[False] == expected
    assert cb.writes[-1] == (False, expected)


@pytest.mark.parametrize("failure", ["none", "before-put", "after-put"])
def test_publish_clipboard_never_reissues_foreign_secret(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    cb, _, _ = harness
    foreign = {"text/plain": b"FOREIGN-SECRET", paste.KDE_HINT: b"secret"}
    cb.data[False] = foreign.copy()
    put = cb.put

    def failing_put(snapshot: dict[str, bytes], primary: bool) -> None:
        if failure == "after-put":
            put(snapshot, primary)
        raise RuntimeError("публикация прервана")

    with monkeypatch.context() as patch:
        if failure != "none":
            patch.setattr(cb, "put", failing_put)
        assert paste.publish_clipboard("Из трея", session_kind=session.SessionKind.OTHER) is (
            failure == "none"
        )
    outcome = paste.paste_text("Следующая диктовка", 42, PasteMode.AUTO)
    assert all(snapshot != foreign for _, snapshot in cb.writes)
    if failure == "none":
        assert outcome.restore == PasteRestore.RESTORED
        assert cb.data[False]["text/plain"] == "Из трея".encode()
    else:
        assert outcome.kind == PasteOutcomeKind.REFUSED_SECRET
        assert outcome.restore == PasteRestore.CLEARED_SECRET
        assert cb.data[False] == {}


@pytest.mark.parametrize("operation", ["_Clipboard", "normalize", "_fly_blacklist_type", "put"])
def test_publish_clipboard_errors_preserve_pending_and_privacy(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
) -> None:
    cb, _, _ = harness
    before = cb.snapshot(False)
    pending = paste._PendingRestore(before, False)
    monkeypatch.setattr(paste, "_pending", pending)
    monkeypatch.setattr(paste, "_last_clipboard_snapshot", before)
    private = "ПРИВАТНАЯ-ФРАЗА"
    error = Mock(side_effect=RuntimeError(private))
    monkeypatch.setattr(cb if operation == "put" else paste, operation, error)
    caplog.set_level("DEBUG", logger=paste.__name__)

    assert paste.publish_clipboard(private, session_kind=session.SessionKind.FLY) is False

    error.assert_called_once()
    assert cb.writes == []
    assert paste._last_clipboard_snapshot == before
    assert paste._pending is pending
    assert not pending.consumed
    assert caplog.record_tuples == [
        (paste.__name__, 10, "не удалось опубликовать текст в буфер обмена")
    ]
    assert caplog.records[0].exc_info is None
    assert caplog.records[0].args == ()


@pytest.mark.parametrize("scenario", ["no-app", "wrong-thread"])
def test_publish_clipboard_requires_gui_application(
    monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    """Проверка настоящего адаптера с подменённым Qt, без создания QApplication и дисплея."""
    from PyQt5.QtCore import QThread
    from PyQt5.QtWidgets import QApplication

    app = Mock(spec=QApplication)
    app.thread.return_value = object()
    monkeypatch.setattr(QApplication, "instance", lambda: None if scenario == "no-app" else app)
    monkeypatch.setattr(QThread, "currentThread", lambda: object())
    clipboard = Mock(side_effect=AssertionError("обращение к буферу запрещено"))
    monkeypatch.setattr(QApplication, "clipboard", clipboard)

    assert paste.publish_clipboard("Из трея", session_kind=session.SessionKind.KDE) is False

    clipboard.assert_not_called()
    assert paste._last_clipboard_snapshot is None
    assert not paste.has_pending()


@pytest.mark.parametrize("delay", [50, 100])
@pytest.mark.parametrize("primary", [False, True])
def test_restore_pending_during_auto(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    delay: int,
    primary: bool,
) -> None:
    """Аварийный возврат в обеих паузах; продолжение не повторяет ввод и записи."""
    cb, x, _ = harness
    if primary:
        x.wm_class.return_value = ("xterm", "xterm")
    before = {mode: cb.snapshot(mode) for mode in (False, True)}
    observed: list[bool] = []
    writes_after: list[tuple[bool, dict[str, bytes]]] = []
    forbidden = Mock(side_effect=AssertionError("паузы и X запрещены"))

    def interrupt(ms: int) -> None:
        if ms != delay:
            return
        observed.append(paste.has_pending())
        with monkeypatch.context() as patch:
            patch.setattr(paste, "_wait_ms", forbidden)
            patch.setattr(paste, "X11Display", forbidden)
            patch.setattr(paste, "_session_kind", forbidden)
            for name in ("open", "close", "send_combo", "grab_keyboard", "ungrab_keyboard"):
                patch.setattr(x, name, forbidden)
            observed.extend([paste.restore_pending(), paste.has_pending()])
            writes_after.extend(cb.writes)
            observed.append(paste.restore_pending())

    monkeypatch.setattr(paste, "_wait_ms", interrupt)
    paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert observed == [True, True, False, False]
    assert cb.data == before
    assert cb.writes == writes_after
    assert len(cb.writes) == (4 if primary else 2)
    assert not paste.has_pending()
    forbidden.assert_not_called()
    assert x.send_combo.call_count == (0 if delay == 50 else 1)


@pytest.mark.parametrize("scenario", ["manual", "clipboard-only", "window-changed", "failed"])
def test_restore_pending_keeps_phrase_for_non_pasted(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    cb, x, _ = harness
    observed: list[bool] = []
    mode = PasteMode.CLIPBOARD_ONLY if scenario == "manual" else PasteMode.AUTO
    if scenario == "manual":
        monkeypatch.setattr(cb, "snapshot", Mock(side_effect=AssertionError("снимок запрещён")))
    elif scenario == "clipboard-only":
        x.send_combo.side_effect = RuntimeError("ввод недоступен")
    elif scenario == "window-changed":
        x.send_combo.return_value = False
    else:
        # Публикация произошла, но адаптер сообщил об ошибке.
        put = cb.put

        def failing_put(
            snapshot: dict[str, bytes],
            primary: bool,
            tracker: paste._FetchTracker | None = None,
        ) -> None:
            put(snapshot, primary, tracker)
            raise RuntimeError("публикация прервана")

        monkeypatch.setattr(cb, "put", failing_put)

    def wait(ms: int) -> None:
        if scenario == "manual" or ms == 100:
            observed.extend([paste.has_pending(), paste.restore_pending()])

    monkeypatch.setattr(paste, "_wait_ms", wait)
    outcome = paste.paste_text("фраза", 42, mode)
    assert (
        outcome.kind
        == {
            "manual": PasteOutcomeKind.CLIPBOARD_ONLY,
            "clipboard-only": PasteOutcomeKind.CLIPBOARD_ONLY,
            "window-changed": PasteOutcomeKind.WINDOW_CHANGED,
            "failed": PasteOutcomeKind.FAILED,
        }[scenario]
    )
    assert not any(observed)
    assert not paste.has_pending()
    assert not paste.restore_pending()
    assert cb.data[False] == {"text/plain": "фраза".encode(), paste.KDE_HINT: b"secret"}
    assert len(cb.writes) == 1


@pytest.mark.parametrize("primary", [False, True])
@pytest.mark.parametrize("state", ["secret", "own-secret", "not-owner", "empty"])
def test_restore_pending_uses_existing_restore_rules(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    primary: bool,
    state: str,
) -> None:
    cb, x, _ = harness
    if primary:
        x.wm_class.return_value = ("xterm", "xterm")
    if state in {"secret", "own-secret"}:
        cb.data[primary][paste.KDE_HINT] = b"secret"
        if state == "own-secret":
            monkeypatch.setattr(paste, "_last_clipboard_snapshot", cb.snapshot(False))
            cb.owned[False] = True
    elif state == "empty":
        cb.data[primary] = {}
    saved = cb.snapshot(primary)
    observed: list[bool] = []
    writes_before: list[tuple[bool, dict[str, bytes]]] = []

    def interrupt(ms: int) -> None:
        if ms != 50:
            return
        if state == "not-owner":
            cb.owned[primary] = False
            cb.data[primary] = {"text/plain": b"new-owner"}
            # Проверяем независимость PRIMARY от CLIPBOARD.
            cb.owned[not primary] = False
        writes_before.extend(cb.writes)
        observed.extend([paste.has_pending(), paste.restore_pending()])

    monkeypatch.setattr(paste, "_wait_ms", interrupt)
    paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert observed == [True, state != "not-owner"]
    assert not paste.has_pending()
    assert not paste.restore_pending()
    if state == "not-owner":
        assert cb.data[primary] == {"text/plain": b"new-owner"}
        assert cb.writes == writes_before
    elif state == "secret" or (state == "own-secret" and primary):
        # Отличение своей публикации уже действует только для CLIPBOARD.
        assert cb.data[primary] == {}
        assert (primary, saved) not in cb.writes
    else:
        assert cb.data[primary] == saved


@pytest.mark.parametrize("lost_primary", [False, True])
def test_restore_pending_checks_each_owner(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    lost_primary: bool,
) -> None:
    cb, x, _ = harness
    x.wm_class.return_value = ("xterm", "xterm")
    saved = cb.snapshot(not lost_primary)
    observed: list[bool] = []

    def interrupt(ms: int) -> None:
        if ms == 50:
            cb.owned[lost_primary] = False
            cb.data[lost_primary] = {"text/plain": b"new-owner"}
            observed.append(paste.restore_pending())

    monkeypatch.setattr(paste, "_wait_ms", interrupt)
    paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert observed == [True]
    assert cb.data[lost_primary] == {"text/plain": b"new-owner"}
    assert cb.data[not lost_primary] == saved
    assert len(cb.writes) == 3
    assert cb.writes[-1] == (not lost_primary, saved)


@pytest.mark.parametrize("operation", ["owns", "put", "clear"])
def test_restore_pending_swallows_errors(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    operation: str,
) -> None:
    cb, _, _ = harness
    if operation == "clear":
        cb.data[False][paste.KDE_HINT] = b"secret"
    caplog.set_level("DEBUG", logger=paste.__name__)
    observed: list[bool] = []
    error = Mock(side_effect=RuntimeError("ПРИВАТНАЯ-ФРАЗА"))

    def interrupt(ms: int) -> None:
        if ms == 50:
            with monkeypatch.context() as patch:
                patch.setattr(cb, operation, error)
                observed.extend([paste.restore_pending(), paste.restore_pending()])

    monkeypatch.setattr(paste, "_wait_ms", interrupt)
    paste.paste_text("ПРИВАТНАЯ-ФРАЗА", 42, PasteMode.AUTO)
    assert observed == [False, False]
    error.assert_called_once()
    assert not paste.has_pending()
    assert "не удалось аварийно восстановить" in caplog.text
    assert "ПРИВАТНАЯ-ФРАЗА" not in caplog.text


def test_clipboard_only_without_x(
    harness: tuple[FakeClipboard, Mock, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    cb, x, delays = harness
    factory = Mock(side_effect=AssertionError("X запрещён"))
    monkeypatch.setattr(paste, "X11Display", factory)
    monkeypatch.delenv("XDG_CURRENT_DESKTOP")
    # Проверяется отсутствие даже косвенного подключения через session.detect.
    monkeypatch.setenv("DISPLAY", ":unreachable-paste-unit")
    primary = cb.snapshot(True)
    outcome = paste.paste_text("Проверка\nсвязи, ёж.\x0f", 42, PasteMode.CLIPBOARD_ONLY)
    assert outcome.kind == PasteOutcomeKind.CLIPBOARD_ONLY
    assert outcome.method == PasteMethod.NONE
    assert outcome.wm_class is None
    assert outcome.restore == PasteRestore.KEPT_OURS
    assert outcome.chars == len("Проверка связи, ёж.")
    assert outcome.stripped_controls == 1
    assert outcome.t_ms >= 0
    assert cb.writes[0] == (
        False,
        {
            "text/plain": "Проверка связи, ёж.".encode(),
            paste.KDE_HINT: b"secret",
        },
    )
    assert cb.data[False] == cb.writes[0][1]
    assert cb.data[True] == primary
    assert len(cb.writes) == 1
    assert delays == [50, 100]
    factory.assert_not_called()
    x.send_combo.assert_not_called()
    with pytest.raises(FrozenInstanceError):
        outcome.__setattr__("kind", PasteOutcomeKind.PASTED)


@pytest.mark.parametrize(
    ("wm_class", "mods", "key", "method"),
    [
        ("kate", ["Control_L"], "v", PasteMethod.CTRL_V),
        ("konsole", ["Control_L", "Shift_L"], "v", PasteMethod.CTRL_SHIFT_V),
        ("xterm", ["Shift_L"], "Insert", PasteMethod.SHIFT_INSERT),
    ],
)
def test_chain_and_both_snapshots(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    wm_class: str,
    mods: list[str],
    key: str,
    method: PasteMethod,
) -> None:
    cb, x, delays = harness
    before = {primary: cb.snapshot(primary) for primary in (False, True)}
    x.wm_class.return_value = (wm_class, wm_class)

    def wait(ms: int) -> None:
        delays.append(ms)
        assert cb.data[False] == {"text/plain": b"a b[31m", paste.KDE_HINT: b"secret"}
        if method == PasteMethod.SHIFT_INSERT:
            assert cb.data[True] == cb.data[False]
        if ms == 800:
            x.wm_class.assert_called_once_with(42)
            x.grab_keyboard.assert_not_called()
            x.send_combo.assert_not_called()
        else:
            # Проба захвата идёт на окне фокуса (42) после паузы, прямо перед XTest.
            x.grab_keyboard.assert_called_once_with(42)
            x.ungrab_keyboard.assert_called_once_with()
            x.send_combo.assert_called_once_with(mods, key)

    monkeypatch.setattr(paste, "_wait_ms", wait)
    outcome = paste.PasteFlow(delay_before_ms=800, delay_after_ms=300).run(
        "a\r\nb\x1b[31m\x0f", 42, PasteMode.AUTO
    )
    assert outcome.kind == PasteOutcomeKind.PASTED
    assert outcome.method == method
    assert outcome.restore == PasteRestore.RESTORED
    assert cb.data == before
    assert delays == [800, 300]
    assert not paste.has_pending()
    assert not paste.restore_pending()
    x.close.assert_called_once()


def test_delayed_fetch_restores_after_six_polls(
    harness: tuple[FakeClipboard, Mock, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    cb, x, delays = harness
    before = cb.snapshot(False)
    x.send_combo.side_effect = lambda *_args: True

    def wait(ms: int) -> None:
        delays.append(ms)
        if delays.count(paste.KEYS_POLL_MS) == 6:
            tracker = cb.trackers[False]
            assert tracker is not None
            tracker.mark()

    monkeypatch.setattr(paste, "_wait_ms", wait)
    outcome = paste.paste_text("поздняя фраза", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.PASTED
    assert outcome.restore == PasteRestore.RESTORED
    assert outcome.fetched_ms is not None and outcome.fetched_ms >= 0
    assert cb.data[False] == before
    assert delays == [50] + [paste.KEYS_POLL_MS] * 6 + [100]


def test_unfetched_phrase_stays_in_clipboard(
    harness: tuple[FakeClipboard, Mock, list[int]],
) -> None:
    cb, x, delays = harness
    x.send_combo.side_effect = lambda *_args: True
    outcome = paste.paste_text("фраза для ручной вставки", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.WINDOW_CHANGED
    assert outcome.method == PasteMethod.CTRL_V
    assert outcome.reason == "not-fetched"
    assert outcome.restore == PasteRestore.KEPT_OURS
    assert outcome.fetched_ms is None
    assert cb.data[False]["text/plain"] == "фраза для ручной вставки".encode()
    assert delays == [50] + [paste.KEYS_POLL_MS] * (
        (paste.FETCH_TIMEOUT_MS + paste.KEYS_POLL_MS - 1) // paste.KEYS_POLL_MS
    ) + [100]
    assert not paste.has_pending()
    assert not paste.restore_pending()
    assert cb.data[False]["text/plain"] == "фраза для ручной вставки".encode()


def test_new_clipboard_owner_after_xtest_counts_as_pasted(
    harness: tuple[FakeClipboard, Mock, list[int]],
) -> None:
    cb, x, delays = harness

    def send_combo(*_args: object) -> bool:
        cb.owned[False] = False
        return True

    x.send_combo.side_effect = send_combo
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.PASTED
    assert outcome.restore == PasteRestore.SKIPPED_NOT_OWNER
    assert outcome.reason == ""
    assert outcome.fetched_ms is None
    assert delays == [50, 100]


def test_unfetched_shift_insert_keeps_phrase_in_both_clipboards(
    harness: tuple[FakeClipboard, Mock, list[int]],
) -> None:
    cb, x, _ = harness
    x.wm_class.return_value = ("xterm", "xterm")
    x.send_combo.side_effect = lambda *_args: True
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.WINDOW_CHANGED
    assert outcome.method == PasteMethod.SHIFT_INSERT
    assert outcome.reason == "not-fetched"
    assert outcome.fetched_ms is None
    for primary in (False, True):
        assert cb.data[primary]["text/plain"] == "фраза".encode()


def test_fetch_wait_stops_at_wall_clock_deadline(
    harness: tuple[FakeClipboard, Mock, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    _, x, delays = harness
    x.send_combo.side_effect = lambda *_args: True
    now = [0.0]
    monkeypatch.setattr(paste, "time", SimpleNamespace(monotonic=lambda: now[0]))

    def wait(ms: int) -> None:
        delays.append(ms)
        if ms == paste.KEYS_POLL_MS:
            now[0] += paste.FETCH_TIMEOUT_MS / 1000 + 0.1

    monkeypatch.setattr(paste, "_wait_ms", wait)
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.WINDOW_CHANGED
    assert outcome.reason == "not-fetched"
    assert delays == [50, paste.KEYS_POLL_MS, 100]


def test_restore_pending_during_fetch_wait_stops_polling(
    harness: tuple[FakeClipboard, Mock, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    cb, x, delays = harness
    before = cb.snapshot(False)
    x.send_combo.side_effect = lambda *_args: True

    def wait(ms: int) -> None:
        delays.append(ms)
        if ms == paste.KEYS_POLL_MS:
            assert paste.has_pending()
            assert paste.restore_pending()

    monkeypatch.setattr(paste, "_wait_ms", wait)
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.PASTED
    assert outcome.restore == PasteRestore.RESTORED
    assert cb.data[False] == before
    assert delays == [50, paste.KEYS_POLL_MS, 100]


def test_fetch_before_xtest_does_not_count(
    harness: tuple[FakeClipboard, Mock, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    cb, x, _ = harness
    x.send_combo.side_effect = lambda *_args: True
    original_put = cb.put

    def put(
        snapshot: dict[str, bytes], primary: bool, tracker: paste._FetchTracker | None = None
    ) -> None:
        original_put(snapshot, primary, tracker)
        if tracker is not None:
            tracker.mark()

    monkeypatch.setattr(cb, "put", put)
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.WINDOW_CHANGED
    assert outcome.reason == "not-fetched"
    assert outcome.fetched_ms is None
    assert cb.data[False]["text/plain"] == "фраза".encode()


def test_shift_insert_primary_fetch_counts(harness: tuple[FakeClipboard, Mock, list[int]]) -> None:
    cb, x, _ = harness
    x.wm_class.return_value = ("xterm", "xterm")
    before = {mode: cb.snapshot(mode) for mode in (False, True)}

    def send_combo(*_args: object) -> bool:
        assert cb.trackers[True] is cb.trackers[False]
        tracker = cb.trackers[True]
        assert tracker is not None
        tracker.mark()
        return True

    x.send_combo.side_effect = send_combo
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.PASTED
    assert outcome.method == PasteMethod.SHIFT_INSERT
    assert outcome.fetched_ms is not None
    assert cb.data == before


@pytest.mark.parametrize("fetched", [True, False])
def test_info_log_reports_fetch_without_phrase(
    harness: tuple[FakeClipboard, Mock, list[int]],
    caplog: pytest.LogCaptureFixture,
    fetched: bool,
) -> None:
    _, x, _ = harness
    if not fetched:
        x.send_combo.side_effect = lambda *_args: True
    caplog.set_level(logging.INFO, logger=paste.__name__)
    outcome = paste.paste_text("СЕКРЕТНАЯ-ФРАЗА", 42, PasteMode.AUTO)
    lines = [record.getMessage() for record in caplog.records if record.name == paste.__name__]
    assert len(lines) == 1
    if fetched:
        assert isinstance(outcome.fetched_ms, int)
    else:
        assert outcome.reason == "not-fetched"
        assert "reason=not-fetched" in lines[0]
    assert f"fetched_ms={outcome.fetched_ms if fetched else '-'}" in lines[0]
    assert "СЕКРЕТНАЯ-ФРАЗА" not in lines[0]


def test_tracked_mime_marks_data_request_without_system_clipboard() -> None:
    pytest.importorskip("PyQt5")
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from helpers.qt_app import get_qapplication

    app = get_qapplication()
    assert app is not None
    tracker = paste._FetchTracker()
    md = paste._tracked_mime(
        {
            "text/plain": "фраза".encode(),
            paste.KDE_HINT: b"secret",
            paste.FLY_FALLBACK_TYPE: b"",
        },
        tracker,
    )
    assert tracker.fetched_at is None
    assert bytes(md.data(paste.KDE_HINT)) == b"secret"
    assert tracker.fetched_at is None
    assert bytes(md.data(paste.FLY_FALLBACK_TYPE)) == b""
    assert tracker.fetched_at is None
    assert bytes(md.data("text/plain")) == "фраза".encode()
    assert tracker.fetched_at is not None
    assert type(md) is type(
        paste._tracked_mime({"text/plain": "другая".encode()}, paste._FetchTracker())
    )


@pytest.mark.parametrize("target", [42, None])
def test_focus_guard(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    target: int | None,
) -> None:
    cb, x, _ = harness

    def switch_window(ms: int) -> None:
        if ms == 50:
            x.active_window.return_value = 99 if target else None
            x.d.get_input_focus.return_value.focus.id = 99

    monkeypatch.setattr(paste, "_wait_ms", switch_window)
    outcome = paste.paste_text("фраза", target, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.WINDOW_CHANGED
    assert outcome.method == PasteMethod.NONE
    assert outcome.restore == PasteRestore.KEPT_OURS
    assert cb.data[False] == {"text/plain": "фраза".encode(), paste.KDE_HINT: b"secret"}
    assert len(cb.writes) == 1
    x.send_combo.assert_not_called()


@pytest.mark.parametrize("grabbed", [False, True])
def test_keyboard_probe_failure_keeps_phrase(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    grabbed: bool,
) -> None:
    """Отказ захвата или неснятый захват запрещает XTest после проверки фокуса."""
    cb, x, delays = harness
    x.grab_keyboard.return_value = grabbed
    if grabbed:
        x.keyboard_grab_deadline = 30.0

    def wait(ms: int) -> None:
        delays.append(ms)
        if ms == 50:
            x.grab_keyboard.assert_not_called()
        else:
            x.grab_keyboard.assert_called_once_with(42)
            if grabbed:
                x.ungrab_keyboard.assert_called_once_with()
            else:
                x.ungrab_keyboard.assert_not_called()
        x.send_combo.assert_not_called()

    monkeypatch.setattr(paste, "_wait_ms", wait)
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.WINDOW_CHANGED
    assert outcome.reason == ("grab-stuck" if grabbed else "grab-refused")
    assert outcome.restore == PasteRestore.KEPT_OURS
    assert cb.data[False]["text/plain"] == "фраза".encode()
    assert delays == [50, 100]


@pytest.mark.parametrize("first_mode", [PasteMode.AUTO, PasteMode.CLIPBOARD_ONLY])
def test_own_secret_hint_is_restored_across_flows(
    harness: tuple[FakeClipboard, Mock, list[int]], first_mode: PasteMode
) -> None:
    """Оставленная фраза с нашим hint восстанавливается и после второй, и после третьей вставки."""
    cb, x, _ = harness
    x.grab_keyboard.return_value = False
    first = paste.paste_text("Первая фраза", 42, first_mode)
    assert first.kind == (
        PasteOutcomeKind.WINDOW_CHANGED
        if first_mode == PasteMode.AUTO
        else PasteOutcomeKind.CLIPBOARD_ONLY
    )
    assert first.restore == PasteRestore.KEPT_OURS
    x.send_combo.assert_not_called()
    saved = cb.snapshot(False)
    assert saved == {"text/plain": "Первая фраза".encode(), paste.KDE_HINT: b"secret"}
    assert cb.owns(False)

    x.grab_keyboard.return_value = True
    for text in ("Вторая фраза", "Третья фраза"):
        cb.writes.clear()
        outcome = paste.paste_text(text, 42, PasteMode.AUTO)
        assert outcome.kind == PasteOutcomeKind.PASTED
        assert outcome.restore == PasteRestore.RESTORED
        assert cb.snapshot(False) == saved
        assert cb.writes == [
            (False, {"text/plain": text.encode(), paste.KDE_HINT: b"secret"}),
            (False, saved),
        ]
    assert x.send_combo.call_count == 2


@pytest.mark.parametrize("owned", [False, True])
@pytest.mark.parametrize("change", ["none", "text", "mime"])
def test_secret_hint_requires_ownership_and_matching_snapshot(
    harness: tuple[FakeClipboard, Mock, list[int]], owned: bool, change: str
) -> None:
    """Даже тот же текст чужого владельца или иной MIME своего процесса — чужой секрет."""
    cb, x, _ = harness
    first = paste.paste_text("Первая фраза", 42, PasteMode.CLIPBOARD_ONLY)
    assert first.kind == PasteOutcomeKind.CLIPBOARD_ONLY
    if change == "text":
        cb.data[False]["text/plain"] = "Чужой секрет".encode()
    elif change == "mime":
        cb.data[False]["text/html"] = b"<b>secret</b>"
    cb.owned[False] = owned
    saved = cb.snapshot(False)
    assert saved[paste.KDE_HINT] == b"secret"
    cb.writes.clear()

    outcome = paste.paste_text("Вторая фраза", 42, PasteMode.AUTO)
    ours = owned and change == "none"
    assert outcome.kind == (PasteOutcomeKind.PASTED if ours else PasteOutcomeKind.REFUSED_SECRET)
    assert outcome.restore == (PasteRestore.RESTORED if ours else PasteRestore.CLEARED_SECRET)
    assert cb.snapshot(False) == (saved if ours else {})
    assert cb.writes == [
        (False, {"text/plain": "Вторая фраза".encode(), paste.KDE_HINT: b"secret"}),
        (False, saved if ours else {}),
    ]
    x.send_combo.assert_called_once_with(["Control_L"], "v")


def test_secret_owned_without_previous_publication_is_cleared(
    harness: tuple[FakeClipboard, Mock, list[int]],
) -> None:
    """Одного ownsClipboard без запомненной публикации недостаточно для переиздания secret."""
    cb, _, _ = harness
    cb.data[False][paste.KDE_HINT] = b"secret"
    cb.owned[False] = True
    outcome = paste.paste_text("Наша фраза", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.REFUSED_SECRET
    assert outcome.restore == PasteRestore.CLEARED_SECRET
    assert cb.snapshot(False) == {}
    assert all(data.get("text/plain") != b"before" for _, data in cb.writes)


@pytest.mark.parametrize("primary", [False, True])
@pytest.mark.parametrize("state", ["secret", "empty", "lost-owner"])
def test_restore_rules(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    primary: bool,
    state: str,
) -> None:
    cb, x, _ = harness
    x.wm_class.return_value = ("xterm", "XTerm")
    other = cb.snapshot(not primary)
    if state == "secret":
        cb.data[primary][paste.KDE_HINT] = b"secret"
    elif state == "empty":
        cb.data[primary] = {}

    def lose_owner(ms: int) -> None:
        if state == "lost-owner" and ms == 100:
            cb.data[primary] = {"text/plain": b"new user copy"}
            cb.owned[primary] = False

    monkeypatch.setattr(paste, "_wait_ms", lose_owner)
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert cb.data[not primary] == other
    assert cb.data[primary] == ({"text/plain": b"new user copy"} if state == "lost-owner" else {})
    assert outcome.kind == (
        PasteOutcomeKind.REFUSED_SECRET if state == "secret" else PasteOutcomeKind.PASTED
    )
    assert outcome.restore == (
        PasteRestore.RESTORED
        if primary
        else {
            "secret": PasteRestore.CLEARED_SECRET,
            "empty": PasteRestore.CLEARED_EMPTY,
            "lost-owner": PasteRestore.SKIPPED_NOT_OWNER,
        }[state]
    )
    if state == "secret":
        old_text = b"selection" if primary else b"before"
        assert all(data.get("text/plain") != old_text for _, data in cb.writes)


def test_lost_owner_does_not_clear_new_copy_after_secret(
    harness: tuple[FakeClipboard, Mock, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    cb, _, _ = harness
    cb.data[False][paste.KDE_HINT] = b"secret"

    def change(ms: int) -> None:
        cb.data[False] = {"text/plain": b"new copy"}
        cb.owned[False] = False

    monkeypatch.setattr(paste, "_wait_ms", change)
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.restore == PasteRestore.SKIPPED_NOT_OWNER
    assert cb.data[False] == {"text/plain": b"new copy"}


@pytest.mark.parametrize("mode", [PasteMode.AUTO, PasteMode.CLIPBOARD_ONLY])
def test_secret_not_republished_without_xtest(
    harness: tuple[FakeClipboard, Mock, list[int]], mode: PasteMode
) -> None:
    cb, x, _ = harness
    cb.data[False][paste.KDE_HINT] = b"secret"
    x.active_window.return_value = 99
    x.d.get_input_focus.return_value.focus.id = 99
    outcome = paste.paste_text("фраза", 42, mode)
    assert outcome.kind == (
        PasteOutcomeKind.WINDOW_CHANGED
        if mode == PasteMode.AUTO
        else PasteOutcomeKind.CLIPBOARD_ONLY
    )
    assert outcome.restore == PasteRestore.KEPT_OURS
    assert outcome.method == PasteMethod.NONE
    assert cb.data[False] == {"text/plain": "фраза".encode(), paste.KDE_HINT: b"secret"}
    assert len(cb.writes) == 1
    assert all(data.get("text/plain") != b"before" for _, data in cb.writes)
    x.send_combo.assert_not_called()


@pytest.mark.parametrize("secret", [False, True])
def test_failed_xtest_is_not_reported_as_pasted(
    harness: tuple[FakeClipboard, Mock, list[int]], secret: bool
) -> None:
    cb, x, _ = harness
    if secret:
        cb.data[False][paste.KDE_HINT] = b"secret"
    x.send_combo.return_value = False
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.WINDOW_CHANGED
    assert outcome.method == PasteMethod.NONE
    assert outcome.restore == PasteRestore.KEPT_OURS
    assert cb.data[False]["text/plain"] == "фраза".encode()
    assert all(data.get("text/plain") != b"before" for _, data in cb.writes)


@pytest.mark.parametrize("secret", [False, True])
def test_reentrant_call_keeps_outer_flow(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    secret: bool,
) -> None:
    cb, x, _ = harness
    x.wm_class.return_value = ("xterm", "xterm")
    if secret:
        cb.data[False][paste.KDE_HINT] = b"secret"
    before = {primary: cb.snapshot(primary) for primary in (False, True)}
    completed: list[int] = []

    def interrupted(ms: int) -> None:
        during = {primary: cb.snapshot(primary) for primary in (False, True)}
        writes = cb.writes.copy()
        with monkeypatch.context() as guard:
            factory = Mock(side_effect=AssertionError("повторный вход не трогает Qt/X"))
            guard.setattr(paste, "_Clipboard", factory)
            guard.setattr(paste, "X11Display", factory)
            outcome = paste.paste_text("вторая", 42, PasteMode.AUTO)
            factory.assert_not_called()
        assert outcome.kind == PasteOutcomeKind.BUSY
        assert outcome.method == PasteMethod.NONE
        assert outcome.restore == PasteRestore.KEPT_OURS
        assert outcome.chars == outcome.stripped_controls == 0
        assert "вторая" not in repr(outcome)
        assert cb.data == during
        assert cb.writes == writes
        assert paste._running
        assert paste.has_pending()
        completed.append(ms)

    monkeypatch.setattr(paste, "_wait_ms", interrupted)
    outcome = paste.paste_text("первая", 42, PasteMode.AUTO)
    if secret:
        before[False] = {}
    assert outcome.kind == (PasteOutcomeKind.REFUSED_SECRET if secret else PasteOutcomeKind.PASTED)
    assert cb.data == before
    if secret:
        assert all(data.get("text/plain") != b"before" for _, data in cb.writes)
    assert completed == [50, 100]
    assert not paste.has_pending()
    assert not paste.restore_pending()
    x.close.assert_called_once()
    assert not paste._running


@pytest.mark.parametrize("failure_at", ["before", "after", "send", "close"])
def test_exceptions_do_not_escape_and_primary_is_restored(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure_at: str,
) -> None:
    cb, x, _ = harness
    x.wm_class.return_value = ("xterm", "xterm")
    before = {primary: cb.snapshot(primary) for primary in (False, True)}
    private = "ПРИВАТНАЯ-ФРАЗА"

    def wait(ms: int) -> None:
        if (failure_at, ms) in {("before", 50), ("after", 100)}:
            raise ValueError(private)

    monkeypatch.setattr(paste, "_wait_ms", wait)
    if failure_at in {"send", "close"}:
        getattr(x, "send_combo" if failure_at == "send" else "close").side_effect = ValueError(
            private
        )
    outcome = paste.paste_text(private, 42, PasteMode.AUTO)
    pasted = failure_at in {"after", "close"}
    assert outcome.kind == (PasteOutcomeKind.PASTED if pasted else PasteOutcomeKind.CLIPBOARD_ONLY)
    assert outcome.restore == (PasteRestore.RESTORED if pasted else PasteRestore.KEPT_OURS)
    assert cb.data[True] == before[True]
    assert cb.data[False] == (
        before[False] if pasted else {"text/plain": private.encode(), paste.KDE_HINT: b"secret"}
    )
    assert private not in repr(outcome) + caplog.text
    assert not paste._running
    x.close.assert_called_once()


@pytest.mark.parametrize("operation", ["snapshot", "put", "owns", "clear"])
def test_clipboard_errors_return_outcome(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    cb, x, _ = harness
    x.wm_class.return_value = ("xterm", "xterm")
    primary = cb.snapshot(True)
    if operation == "clear":
        cb.data[False][paste.KDE_HINT] = b"secret"
    original = getattr(cb, operation)

    def fail_clipboard(*args: object) -> object:
        if (args[1] if operation == "put" else args[-1]) is False:
            raise RuntimeError("буфер недоступен")
        return original(*args)

    monkeypatch.setattr(cb, operation, fail_clipboard)
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.FAILED
    assert cb.data[True] == primary
    assert not paste._running
    x.close.assert_called_once()


@pytest.mark.parametrize("state", ["normal", "secret", "empty", "lost-owner"])
@pytest.mark.parametrize("changed", [False, True])
def test_primary_restored_when_clipboard_is_kept(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    changed: bool,
) -> None:
    cb, x, _ = harness
    x.wm_class.return_value = ("xterm", "xterm")
    x.send_combo.return_value = False
    if changed:
        x.active_window.return_value = 99
    if state == "secret":
        cb.data[True][paste.KDE_HINT] = b"secret"
    elif state == "empty":
        cb.data[True] = {}
    expected = cb.snapshot(True) if state == "normal" else {}
    if state == "lost-owner":
        expected = {"text/plain": b"new selection"}

        def lose_primary(ms: int) -> None:
            cb.data[True] = expected.copy()
            cb.owned[True] = False

        monkeypatch.setattr(paste, "_wait_ms", lose_primary)
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.restore == PasteRestore.KEPT_OURS
    assert cb.data[False]["text/plain"] == "фраза".encode()
    assert cb.data[True] == expected
    if state == "secret":
        assert outcome.kind == PasteOutcomeKind.REFUSED_SECRET
        assert all(data.get("text/plain") != b"selection" for _, data in cb.writes)
    else:
        assert outcome.kind == PasteOutcomeKind.WINDOW_CHANGED


@pytest.mark.parametrize(
    "override_redirect", [False, True], ids=["same-class", "override-redirect"]
)
def test_focus_matches_rejects_foreign_focus_with_unchanged_ewmh(
    harness: tuple[FakeClipboard, Mock, list[int]], override_redirect: bool
) -> None:
    """Правило фокуса проверяется напрямую на ответах X, без дисплея и пробы захвата."""
    _, x, _ = harness
    wm_class = ("kate", "kate")
    target = x.d.get_input_focus.return_value.focus
    assert paste._focus_matches(x, 42, wm_class) is True

    rival = Mock(id=99)
    rival.get_attributes.return_value.override_redirect = override_redirect
    # Даже потомок мишени с override-redirect запрещён: проверяется именно этот флаг.
    rival.query_tree.return_value.parent = target if override_redirect else x.root
    assert x.wm_class(int(rival.id)) == x.wm_class(42) == wm_class
    x.d.get_input_focus.return_value.focus = rival

    assert x.active_window() == 42
    assert paste._focus_matches(x, 42, wm_class) is False
    assert x.active_window() == 42


@pytest.mark.parametrize(
    "focus_state",
    [
        "other",
        "child",
        "class-changed",
        "no-class",
        "override",
        "none",
        "pointer",
        "root",
        "cycle",
        "too-deep",
        "query-error",
        "tree-error",
        "closed",
    ],
)
def test_real_focus_guard_with_unchanged_ewmh(
    harness: tuple[FakeClipboard, Mock, list[int]], focus_state: str
) -> None:
    cb, x, _ = harness
    focus = x.d.get_input_focus.return_value.focus
    if focus_state in {"other", "child", "cycle", "too-deep", "tree-error"}:
        focus.id = 99
    if focus_state == "child":
        parent = Mock(id=42)
        parent.get_attributes.return_value.override_redirect = False
        focus.query_tree.return_value.parent = parent
    elif focus_state == "class-changed":
        x.wm_class.side_effect = [("kate", "kate"), ("kscreenlocker", "kscreenlocker")]
    elif focus_state == "no-class":
        x.wm_class.return_value = None
    elif focus_state == "override":
        focus.get_attributes.return_value.override_redirect = True
    elif focus_state in {"none", "pointer", "root"}:
        x.d.get_input_focus.return_value.focus = {"none": 0, "pointer": 1, "root": x.root}[
            focus_state
        ]
    elif focus_state == "cycle":
        focus.query_tree.return_value.parent = focus
    elif focus_state == "too-deep":
        window = focus
        for wid in range(100, 140):
            parent = Mock(id=wid)
            parent.get_attributes.return_value.override_redirect = False
            window.query_tree.return_value.parent = parent
            window = parent
    elif focus_state == "query-error":
        x.d.get_input_focus.side_effect = RuntimeError("фокус недоступен")
    elif focus_state == "tree-error":
        focus.query_tree.side_effect = RuntimeError("окно исчезло")
    elif focus_state == "closed":
        x.d = None
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.kind == (
        PasteOutcomeKind.PASTED if focus_state == "child" else PasteOutcomeKind.WINDOW_CHANGED
    )
    if focus_state == "child":
        x.send_combo.assert_called_once()
    else:
        x.send_combo.assert_not_called()
        assert outcome.restore == PasteRestore.KEPT_OURS
        assert cb.data[False]["text/plain"] == "фраза".encode()


@pytest.mark.parametrize(
    ("text", "removed"),
    [("a\r\nb", 0), ("a\r\n\n\rb", 0), ("a\x00\t\x1b\x7f\x85\x9fb", 6), ("a\r\nb\x0f\x9b", 2)],
)
def test_stripped_controls_counts_only_deleted_controls(
    harness: tuple[FakeClipboard, Mock, list[int]], text: str, removed: int
) -> None:
    outcome = paste.paste_text(text, 42, PasteMode.CLIPBOARD_ONLY)
    assert outcome.stripped_controls == removed
    assert outcome.chars == len(paste.normalize(text))


@pytest.mark.parametrize("first", [PasteMode.AUTO, PasteMode.CLIPBOARD_ONLY])
def test_session_detection_is_cached_without_manual_x(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    first: PasteMode,
) -> None:
    monkeypatch.delenv("XDG_CURRENT_DESKTOP")
    monkeypatch.setenv("DISPLAY", ":unreachable-paste-unit")
    probe = Mock(return_value=session.SessionKind.FLY)
    detect = Mock(wraps=session.detect)
    monkeypatch.setattr(session, "_from_x11", probe)
    monkeypatch.setattr(session, "detect", detect)
    monkeypatch.setattr(paste, "_fly_blacklist_type", lambda **kwargs: "Star Embed Source")
    modes = [first, first, *[mode for mode in PasteMode if mode != first] * 2]
    for mode in modes:
        before = probe.call_count
        paste.paste_text("фраза", 42, mode)
        if mode == PasteMode.CLIPBOARD_ONLY:
            assert probe.call_count == before
    assert detect.call_count == 2
    probe.assert_called_once_with(":unreachable-paste-unit")


@pytest.mark.parametrize("scenario", ["reentrant", "no-app", "wrong-thread"])
def test_qt_process_returns_outcome_without_abort(scenario: str) -> None:
    """Настоящий Qt-слот не должен дать SIGABRT; дочерний процесс не подключается к X."""
    script = dedent("""
        import sys
        import threading
        from unittest.mock import Mock, patch
        from PyQt5.QtCore import QCoreApplication, QTimer
        from PyQt5.QtWidgets import QApplication
        from astra_voice.platform import paste

        scenario = sys.argv[1]
        factory = Mock(side_effect=AssertionError("X запрещён"))
        with patch.object(paste, "X11Display", factory):
            if scenario == "no-app":
                outcome = paste.paste_text("фраза", 42, paste.PasteMode.AUTO)
                assert outcome.kind == paste.PasteOutcomeKind.FAILED
                paste._pending = paste._PendingRestore({"text/plain": b"before"}, False)
                assert paste.has_pending()
                assert paste.restore_pending() is False
                factory.assert_not_called()
            elif scenario == "wrong-thread":
                app = QApplication(["paste-test", "-platform", "offscreen"])
                outcomes = []
                restored = []
                def worker():
                    outcomes.append(paste.paste_text("фраза", 42, paste.PasteMode.AUTO))
                    paste._pending = paste._PendingRestore({"text/plain": b"before"}, False)
                    restored.append(paste.restore_pending())
                thread = threading.Thread(target=worker)
                thread.start()
                thread.join(5)
                assert not thread.is_alive()
                assert len(outcomes) == 1
                assert outcomes[0].kind == paste.PasteOutcomeKind.FAILED
                assert restored == [False]
                factory.assert_not_called()
            else:
                app = QCoreApplication([])
                cb = Mock()
                saved = {"text/plain": b"before"}
                cb.snapshot.return_value = saved.copy()
                cb.owns.return_value = True
                x = Mock()
                x.wm_class.return_value = ("kate", "kate")
                x.active_window.return_value = 42
                x.send_combo.side_effect = lambda *args: (tracker.mark(), True)[1]
                x.grab_keyboard.return_value = True
                x.keyboard_grab_deadline = None
                x.keys_held.return_value = False
                x.root.id = 2
                focus = x.d.get_input_focus.return_value.focus
                focus.id = 42
                focus.get_attributes.return_value.override_redirect = False
                factory.side_effect = None
                factory.return_value = x
                outcomes = []
                writes = []
                tracker = None
                def put(data, primary, fetched=None):
                    global tracker
                    tracker = fetched
                    writes.append(data.copy())
                cb.put.side_effect = put
                def reenter():
                    outcomes.append(paste.paste_text("вторая", 42, paste.PasteMode.AUTO))
                QTimer.singleShot(0, reenter)
                QTimer.singleShot(75, reenter)
                with patch.object(paste, "_Clipboard", return_value=cb) as clipboard_factory:
                    outcome = paste.paste_text("первая", 42, paste.PasteMode.AUTO)
                assert outcome.kind == paste.PasteOutcomeKind.PASTED
                assert outcome.restore == paste.PasteRestore.RESTORED
                assert len(outcomes) == 2
                for nested in outcomes:
                    assert nested.kind == paste.PasteOutcomeKind.BUSY
                    assert nested.method == paste.PasteMethod.NONE
                    assert nested.restore == paste.PasteRestore.KEPT_OURS
                assert writes == [
                    {"text/plain": "первая".encode(), paste.KDE_HINT: b"secret"}, saved]
                clipboard_factory.assert_called_once()
                factory.assert_called_once()
                x.close.assert_called_once()
            assert not paste._running
        print("ok")
    """)
    result = subprocess.run(
        [sys.executable, "-c", script, scenario],
        env={
            **os.environ,
            "DISPLAY": "",
            "QT_QPA_PLATFORM": "offscreen",
            "PYTHONPATH": str(Path(paste.__file__).resolve().parents[2]),
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_wait_excludes_user_input(monkeypatch: pytest.MonkeyPatch) -> None:
    """Флаг обязателен для каждого вложенного ожидания; таймеры остаются разрешены."""
    qt = Mock()
    monkeypatch.setitem(sys.modules, "PyQt5.QtCore", qt)
    for delay in (50, 100):
        paste._wait_ms(delay)
        qt.QEventLoop.return_value.exec_.assert_called_with(qt.QEventLoop.ExcludeUserInputEvents)
        qt.QTimer.return_value.start.assert_called_with(delay)
    assert qt.QEventLoop.return_value.exec_.call_count == 2


def test_snapshot_is_byte_copy() -> None:
    payload = bytearray(b"before")
    md = SimpleNamespace(formats=lambda: ["text/plain"], data=lambda fmt: payload)
    snapshot = paste.snapshot_mime(md)
    payload[:] = b"after"
    assert snapshot == {"text/plain": b"before"}
    assert paste.snapshot_mime(None) == {}


def test_snapshot_limits_nontext_without_losing_text_or_hint(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """1 МиБ включительно; текст и hint любого размера сохраняются, содержимое не в логе."""
    from PyQt5.QtCore import QMimeData

    caplog.set_level("DEBUG", logger=paste.__name__)
    limit = 1024 * 1024
    large = b"PRIVATE-CLIPBOARD-" + b"x" * limit
    preserved = dict.fromkeys(
        ("text/plain", "text/html", "text/uri-list", "UTF8_STRING", paste.KDE_HINT), large
    )
    preserved["text/uri-list"] = b"file:///tmp/" + b"x" * limit + b"\r\n"
    preserved["application/small"] = b"x" * limit
    md = QMimeData()
    for fmt, data in {**preserved, "application/large": large}.items():
        md.setData(fmt, data)
    assert paste.snapshot_mime(md) == preserved
    assert "application/large" in caplog.text
    assert "PRIVATE-CLIPBOARD" not in caplog.text


def test_snapshot_does_not_request_heavy_formats(caplog: pytest.LogCaptureFixture) -> None:
    """Не запускаем ленивую загрузку картинки или объекта LibreOffice в GUI-потоке."""
    formats = [
        "image/png",
        "application/x-qt-image",
        'application/x-openoffice-embed-source-xml;windows_formatname="Star Embed Source (XML)"',
        "application/vnd.oasis.opendocument.text",
        "Star Embed Source",
        "Star Object Descriptor (XML)",
    ]
    caplog.set_level("DEBUG", logger=paste.__name__)
    data = Mock(side_effect=AssertionError("загрузка тяжёлых данных запрещена"))
    assert paste.snapshot_mime(SimpleNamespace(formats=lambda: formats, data=data)) == {}
    data.assert_not_called()
    assert all(fmt in caplog.text for fmt in formats)


@pytest.mark.parametrize("mode", [PasteMode.AUTO, PasteMode.CLIPBOARD_ONLY])
def test_lone_surrogate_is_replaced_without_losing_phrase(
    harness: tuple[FakeClipboard, Mock, list[int]],
    caplog: pytest.LogCaptureFixture,
    mode: PasteMode,
) -> None:
    cb, _, _ = harness
    private = "ПРИВАТНАЯ-ФРАЗА"
    outcome = paste.paste_text(private + "\ud800\nконец", 42, mode)
    assert outcome.kind == (
        PasteOutcomeKind.PASTED if mode == PasteMode.AUTO else PasteOutcomeKind.CLIPBOARD_ONLY
    )
    assert cb.writes[0][1]["text/plain"] == (private + "? конец").encode()
    assert private not in repr(outcome) + caplog.text


@pytest.mark.parametrize("mode", [PasteMode.AUTO, PasteMode.CLIPBOARD_ONLY])
def test_fly_adds_blacklisted_type(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    mode: PasteMode,
) -> None:
    cb, x, _ = harness
    x.wm_class.return_value = ("xterm", "xterm")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "Fly")
    monkeypatch.setattr(paste, "_fly_blacklist_type", lambda **kwargs: "Star Embed Source")
    paste.paste_text("фраза", 42, mode)
    expected = {
        "text/plain": "фраза".encode(),
        paste.KDE_HINT: b"secret",
        "Star Embed Source": b"",
    }
    assert cb.writes[0] == (False, expected)
    if mode == PasteMode.AUTO:
        assert cb.writes[1] == (True, expected)


@pytest.mark.parametrize(
    "types",
    [
        "text/plain:x-kde-passwordManagerHint:x-openoffice-link",
        "text/plain:text/html:x-kde-passwordManagerHint",
    ],
)
def test_fly_blacklist_preserves_phrase_and_hint_in_qmime(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    types: str,
) -> None:
    """T-48/У49: опасные типы не затирают text/plain и hint; есть запасной тип."""
    cb, _, _ = harness
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "Fly")
    monkeypatch.setattr(paste, "FLY_SYSTEM_THEME", tmp_path / "missing.themerc")
    theme = tmp_path / ".fly" / "theme"
    theme.mkdir(parents=True)
    (theme / "current.themerc").write_text(
        f'ClipboardManagerTypesBlacklist="{types}"\n', encoding="utf-8"
    )
    outcome = paste.paste_text("фраза", 42, PasteMode.CLIPBOARD_ONLY)
    assert outcome.kind == PasteOutcomeKind.CLIPBOARD_ONLY
    md = paste.restore_mime(cb.data[False])
    assert md.text() == "фраза"
    assert bytes(md.data("text/plain")) == "фраза".encode()
    assert bytes(md.data(paste.KDE_HINT)) == b"secret"
    assert md.hasFormat("x-openoffice-link")


def test_fly_blacklist_skips_occupied_keys(tmp_path: Path) -> None:
    theme = tmp_path / "theme.themerc"
    theme.write_text(
        'ClipboardManagerTypesBlacklist="custom:text/html:x-openoffice-link"', encoding="utf-8"
    )
    assert paste._fly_blacklist_type((theme,), occupied={"custom": b"data"}) == "x-openoffice-link"


@pytest.mark.parametrize(
    ("user_text", "system_text", "expected"),
    [
        (
            'ClipboardManagerTypesBlacklist=":: First Type:second:"',
            'ClipboardManagerTypesBlacklist="system:"',
            "First Type",
        ),
        (
            ';ClipboardManagerTypesBlacklist="comment:"',
            'ClipboardManagerTypesBlacklist="Star Embed Source:x-openoffice-link:"',
            "Star Embed Source",
        ),
        ('ClipboardManagerTypesBlacklist=":::"', None, "x-openoffice-link"),
        (None, None, "x-openoffice-link"),
    ],
)
def test_fly_theme_priority_and_fallback(
    tmp_path: Path, user_text: str | None, system_text: str | None, expected: str
) -> None:
    paths = (tmp_path / "user.themerc", tmp_path / "system.themerc")
    for path, content in zip(paths, (user_text, system_text), strict=True):
        if content is not None:
            path.write_text(content, encoding="utf-8")
    assert paste._fly_blacklist_type(paths) == expected


def test_no_qt_import_for_pure_functions(monkeypatch: pytest.MonkeyPatch) -> None:
    """Повторный импорт модуля запрещает даже попытку загрузить PyQt5."""
    import builtins
    import importlib.util
    import sys

    original: Callable[..., object] = builtins.__import__

    def guarded(name: str, *args: object, **kwargs: object) -> object:
        assert not name.startswith("PyQt5")
        return original(name, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "__import__", guarded)
        spec = importlib.util.spec_from_file_location("_paste_without_qt", paste.__file__)
        assert spec is not None and spec.loader is not None
        isolated = importlib.util.module_from_spec(spec)
        patch.setitem(sys.modules, spec.name, isolated)
        spec.loader.exec_module(isolated)
        assert isolated.normalize("ёж\n") == "ёж "
        assert isolated.method_for_wm_class("xterm") == "shift+insert"


@pytest.mark.parametrize("held_polls", [0, 1, 3])
def test_keys_held_before_probe_waits_on_qt_loop(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    held_polls: int,
) -> None:
    """Toggle: зажатая клавиша хоткея держит активный захват X до отпускания (X GrabKey).

    Отпускания ждём до пробы захвата, короткими шагами цикла Qt, а не time.sleep.
    """
    cb, x, delays = harness
    x.keys_held.side_effect = [True] * held_polls + [False]
    order: list[str] = []

    def grab(*args: object) -> bool:
        order.append(f"grab{args}")
        return True

    x.grab_keyboard.side_effect = grab

    def wait(ms: int) -> None:
        delays.append(ms)
        order.append(f"wait{ms}")

    monkeypatch.setattr(paste, "_wait_ms", wait)
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.PASTED
    assert outcome.reason == ""
    assert delays == [paste.KEYS_POLL_MS] * held_polls + [50, 100]
    assert order == [f"wait{paste.KEYS_POLL_MS}"] * held_polls + ["wait50", "grab(42,)", "wait100"]
    x.ungrab_keyboard.assert_called_once_with()
    x.send_combo.assert_called_once_with(["Control_L"], "v")


def test_keys_held_beyond_timeout_keeps_phrase(
    harness: tuple[FakeClipboard, Mock, list[int]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Залипшая клавиша: фраза остаётся в буфере, пробы и XTest нет, причина в исходе."""
    cb, x, delays = harness
    x.keys_held.return_value = True
    clock_ms = [100_000]
    monkeypatch.setattr(time, "monotonic", lambda: clock_ms[0] / 1000)

    def wait(ms: int) -> None:
        delays.append(ms)
        clock_ms[0] += ms

    monkeypatch.setattr(paste, "_wait_ms", wait)
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.WINDOW_CHANGED
    assert outcome.reason == "keys-held"
    assert outcome.restore == PasteRestore.KEPT_OURS
    assert cb.data[False]["text/plain"] == "фраза".encode()
    assert delays[-2:] == [50, 100]
    polls = delays[:-2]
    assert set(polls) == {paste.KEYS_POLL_MS}
    assert len(polls) == paste.KEYS_RELEASE_TIMEOUT_MS // paste.KEYS_POLL_MS
    x.grab_keyboard.assert_not_called()
    x.send_combo.assert_not_called()


@pytest.mark.parametrize(
    ("scenario", "kind", "reason"),
    [
        ("pasted", PasteOutcomeKind.PASTED, ""),
        ("manual", PasteOutcomeKind.CLIPBOARD_ONLY, "manual"),
        ("no-target", PasteOutcomeKind.WINDOW_CHANGED, "no-target"),
        ("no-wm-class", PasteOutcomeKind.WINDOW_CHANGED, "no-wm-class"),
        ("active-changed", PasteOutcomeKind.WINDOW_CHANGED, "active-changed"),
        ("focus-unknown", PasteOutcomeKind.WINDOW_CHANGED, "focus-unknown"),
        ("focus-outside", PasteOutcomeKind.WINDOW_CHANGED, "focus-outside"),
        ("focus-override-redirect", PasteOutcomeKind.WINDOW_CHANGED, "focus-override-redirect"),
        ("wm-class-changed", PasteOutcomeKind.WINDOW_CHANGED, "wm-class-changed"),
        ("x-error", PasteOutcomeKind.WINDOW_CHANGED, "x-error"),
        ("x-unavailable", PasteOutcomeKind.WINDOW_CHANGED, "x-unavailable"),
        ("grab-refused", PasteOutcomeKind.WINDOW_CHANGED, "grab-refused"),
        ("grab-stuck", PasteOutcomeKind.WINDOW_CHANGED, "grab-stuck"),
        ("xtest-refused", PasteOutcomeKind.WINDOW_CHANGED, "xtest-refused"),
        ("restored-early", PasteOutcomeKind.WINDOW_CHANGED, "restored-early"),
        ("publish-failed", PasteOutcomeKind.FAILED, "publish-failed"),
    ],
)
def test_outcome_reason_names_the_branch(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    scenario: str,
    kind: PasteOutcomeKind,
    reason: str,
) -> None:
    """Каждая ветка «только буфер» получает код, по которому её видно в журнале."""
    cb, x, _ = harness
    focus = x.d.get_input_focus.return_value.focus
    mode = PasteMode.CLIPBOARD_ONLY if scenario == "manual" else PasteMode.AUTO
    target: int | None = 42
    if scenario == "no-target":
        target = None
    elif scenario == "no-wm-class":
        x.wm_class.return_value = None
    elif scenario == "active-changed":
        x.active_window.return_value = 99
        focus.id = 99
    elif scenario == "focus-unknown":
        x.d.get_input_focus.return_value.focus = 1
    elif scenario == "focus-outside":
        focus.id = 99
        focus.query_tree.return_value.parent = x.root
    elif scenario == "focus-override-redirect":
        focus.get_attributes.return_value.override_redirect = True
    elif scenario == "wm-class-changed":
        x.wm_class.side_effect = [("kate", "kate"), ("kate", "other")]
    elif scenario == "x-error":
        x.d.get_input_focus.side_effect = RuntimeError("ПРИВАТНО")
    elif scenario == "x-unavailable":
        x.d = None
    elif scenario == "grab-refused":
        x.grab_keyboard.return_value = False
    elif scenario == "grab-stuck":
        x.keyboard_grab_deadline = 30.0
    elif scenario == "xtest-refused":
        x.send_combo.return_value = False
    elif scenario == "restored-early":
        monkeypatch.setattr(
            paste, "_wait_ms", lambda ms: paste.restore_pending() if ms == 50 else None
        )
    elif scenario == "publish-failed":
        monkeypatch.setattr(cb, "put", Mock(side_effect=RuntimeError("ПРИВАТНО")))
    with caplog.at_level(logging.DEBUG, logger="astra_voice.platform.paste"):
        outcome = paste.paste_text("ФРАЗА", target, mode)
    assert outcome.kind == kind
    assert outcome.reason == reason
    assert "ФРАЗА" not in caplog.text and "ПРИВАТНО" not in caplog.text
    if kind == PasteOutcomeKind.WINDOW_CHANGED:
        assert f"причина {reason}" in caplog.text


@pytest.mark.parametrize("scenario", ["pasted", "window-changed", "manual", "busy"])
def test_outcome_is_logged_once_without_phrase(
    harness: tuple[FakeClipboard, Mock, list[int]],
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    scenario: str,
) -> None:
    """INFO-строка исхода: вид, метод, восстановление, причина, класс окна — без текста."""
    cb, x, _ = harness
    private = "ПРИВАТНАЯ-ФРАЗА-ЁЖ"
    cb.data[False]["text/plain"] = "ПРЕЖНИЙ-БУФЕР".encode()
    mode = PasteMode.CLIPBOARD_ONLY if scenario == "manual" else PasteMode.AUTO
    if scenario == "window-changed":
        x.send_combo.return_value = False
    if scenario == "busy":
        monkeypatch.setattr(paste, "_running", True)
    with caplog.at_level(logging.INFO, logger="astra_voice.platform.paste"):
        outcome = paste.paste_text(private, 42, mode)
    records = [r for r in caplog.records if r.getMessage().startswith("вставка: ")]
    assert len(records) == 1
    message = records[0].getMessage()
    assert records[0].levelno == logging.INFO
    assert message.startswith(f"вставка: {outcome.kind}")
    assert f"method={outcome.method} restore={outcome.restore}" in message
    assert f"reason={outcome.reason or '-'}" in message
    assert "target=set" in message and f"wm_class={outcome.wm_class}" in message
    assert private not in caplog.text and "ПРЕЖНИЙ-БУФЕР" not in caplog.text
    assert all(private not in str(arg) for arg in (records[0].args or ()))


def test_active_window_hint_ignored_when_focus_stays(
    harness: tuple[FakeClipboard, Mock, list[int]],
) -> None:
    """Fly (журнал заказчика 22.09): _NET_ACTIVE_WINDOW сменился при показе пилюли,
    а фокус ввода остался в Kate — вставка должна пройти."""
    cb, x, _ = harness
    x.active_window.return_value = 99
    outcome = paste.paste_text("фраза", 42, PasteMode.AUTO)
    assert outcome.kind == PasteOutcomeKind.PASTED
    assert outcome.reason == ""
    x.send_combo.assert_called_once()
