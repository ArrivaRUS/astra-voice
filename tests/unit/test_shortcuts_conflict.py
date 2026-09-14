"""Подсказки KDE/Fly: регрессии форматов и честные ограничения источников."""

from __future__ import annotations

import logging
import os
import signal
import time
import unicodedata
from collections.abc import Iterator
from pathlib import Path
from types import FrameType

import pytest

from astra_voice.platform import shortcuts_conflict as sc
from astra_voice.platform.session import SessionKind

pytestmark = pytest.mark.unit
FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
KDE = FIXTURES / "kglobalshortcutsrc.sample"
FLY = FIXTURES / "fly-keyshortcutrc.sample"
EMPTY_FLY = FIXTURES / "fly-keyshortcutrc-empty.sample"
RU = FIXTURES / "fly-miscrc-ru.sample"
EN = FIXTURES / "fly-miscrc-en.sample"
MISSING = FIXTURES / "does-not-exist.sample"


@pytest.fixture(autouse=True)
def fixtures_only(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Разрешает только фикстуры и tmp_path, не разыменовывая проверяемую ссылку."""
    read_lines = sc._read_lines

    def guarded(path: Path) -> list[str] | None:
        assert any(
            path == root or path.parent.resolve().is_relative_to(root)
            for root in (FIXTURES, tmp_path)
        )
        return read_lines(path)

    monkeypatch.setattr(sc, "_read_lines", guarded)


@pytest.fixture
def read_deadline() -> Iterator[None]:
    """Прерывает регрессию с FIFO/устройством, чтобы сам тест не завис навсегда."""

    def expired(signum: int, frame: FrameType | None) -> None:
        pytest.fail("Чтение конфигурации заблокировалось более чем на секунду")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, 1.0)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def test_kde_krunner_escaped_tabs() -> None:
    raw = KDE.read_text(encoding="utf-8")
    assert r"Alt+Space\tAlt+F2\tSearch" in raw
    assert "\t" not in raw
    source = sc.KdeShortcutSource(KDE)
    assert source.available
    hits = source.lookup("Alt+F2")
    assert len(hits) == 1
    hit = hits[0]
    assert hit.combo == "Alt+F2"
    assert hit.action == "_launch"
    assert hit.title == "Открыть строку поиска и запуска KRunner"
    assert str(hit) == hit.title
    assert hit.section == "org.kde.krunner.desktop"
    assert hit.section_name == hit.title
    assert hit.source == "KDE"
    assert hit.origin_path == KDE
    assert not hit.is_system_default
    assert hit.authoritative is False
    assert source.lookup("Alt+Space")[0].action == "_launch"
    assert source.lookup("Search")[0].action == "_launch"


def test_kde_real_tabs_and_section_name_fallback() -> None:
    path = FIXTURES / "kglobalshortcutsrc-tabs.sample"
    assert "\t" in path.read_text(encoding="utf-8")
    source = sc.KdeShortcutSource(path)
    assert source.lookup("Meta+Q")[0].section_name == "kwin"
    assert source.lookup("Alt+F4")[0].action == "Window Close"


@pytest.mark.parametrize("combo", ["Ctrl+Space", "ctrl+space", "none", "NONE", "", "Meta+PgUp"])
def test_kde_absent_and_disabled_combinations(combo: str) -> None:
    assert sc.KdeShortcutSource(KDE).lookup(combo) == []


def test_fly_empty_user_file_uses_system_defaults() -> None:
    source = sc.FlyShortcutSource(EMPTY_FLY, FLY, locale_paths=[RU])
    assert source.available
    hit = source.lookup("Alt+F4")[0]
    assert hit.combo == "Alt+F4"
    assert hit.source == "FLY"
    assert hit.action == "FLYWM_CLOSE"
    assert hit.title == "Закрыть окно"
    assert hit.section == hit.section_name == "ShortCutKeys"
    assert hit.origin_path == FLY
    assert hit.is_system_default
    assert hit.authoritative is False


def test_fly_unknown_title_is_only_action_code() -> None:
    source = sc.FlyShortcutSource(FLY, MISSING, locale_paths=[RU])
    hit = source.lookup("Meta+Left")[0]
    assert hit.title is None
    assert str(hit) == hit.action == "FLYWM_SNAP_LEFT"
    assert not hit.is_system_default


def test_fly_user_file_prevents_system_read(monkeypatch: pytest.MonkeyPatch) -> None:
    read_lines = sc._read_lines

    def guarded(path: Path) -> list[str] | None:
        assert path != MISSING
        return read_lines(path)

    monkeypatch.setattr(sc, "_read_lines", guarded)
    source = sc.FlyShortcutSource(FLY, MISSING, locale_paths=[RU])
    assert source.lookup("Alt+Insert")[0].action == "FLYWM_DESKTOP_FOCUS"


def test_fly_missing_user_file_uses_defaults() -> None:
    source = sc.FlyShortcutSource(MISSING, FLY, locale_paths=[RU])
    assert source.lookup("Alt+F4")[0].is_system_default


def test_fly_comments_and_none_modifier() -> None:
    source = sc.FlyShortcutSource(FLY, MISSING, locale_paths=[RU])
    assert source.lookup("Alt+Backspace") == []
    assert source.lookup("Super_L")[0].action == "FLYWM_POPUP_START_MENU"
    assert source.lookup("None|Super_L")[0].combo == "Super_L"
    assert source.lookup("Meta") == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("ctrl+space", "Ctrl+Space"),
        ("shift+alt+control+super+x", "Meta+Ctrl+Alt+Shift+X"),
        ("Mod4|Ctrl|Alt|Shift|x", "Meta+Ctrl+Alt+Shift+X"),
        ("shift|Mod4|less", "Meta+Shift+less"),
        ("None|Super_L", "Super_L"),
        ("alt+f2", "Alt+F2"),
        (" Ctrl + ctrl + SPACE ", "Ctrl+Space"),
    ],
)
def test_normalization(raw: str, expected: str) -> None:
    assert sc.norm_combo(raw) == expected
    assert sc.norm_combo(expected) == expected


@pytest.mark.parametrize("combo", ["alt+space", "Alt|space", "ALT+SPACE", "space+alt"])
def test_kde_and_fly_normalize_symmetrically(combo: str) -> None:
    kde = sc.KdeShortcutSource(KDE)
    fly = sc.FlyShortcutSource(FLY, MISSING, locale_paths=[RU])
    assert kde.lookup(combo)[0].combo == fly.lookup(combo)[0].combo == "Alt+Space"
    assert fly.lookup("Shift+Meta+less")[0].action == "FLYWM_PREV_WALLPAPER"


def test_fly_locale_priority_and_accelerator() -> None:
    russian = sc.FlyShortcutSource(FLY, MISSING, locale_paths=[RU, EN])
    english = sc.FlyShortcutSource(FLY, MISSING, locale_paths=[MISSING, EN])
    assert russian.lookup("Alt+F4")[0].title == "Закрыть окно"
    assert english.lookup("Alt+F4")[0].title == "Close"
    assert english.lookup("Meta+Left")[0].title is None


@pytest.mark.parametrize("locale_variable", ["LC_ALL", "LC_MESSAGES", "LANG"])
def test_fly_full_locale_filename_and_english_fallback(
    locale_variable: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("LC_ALL", "LC_MESSAGES", "LANG"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv(locale_variable, "ru_RU.UTF-8")
    user = FIXTURES / "user"
    system = FIXTURES / "system"
    # Виртуальные пути направляются только на существующие фикстуры.
    mapping = {
        user / "keyshortcutrc": EMPTY_FLY,
        system / "keyshortcutrc": FLY,
        system / "ru_RU.UTF-8.miscrc": RU,
        user / "en.miscrc": EN,
    }
    read_lines = sc._read_lines
    calls: list[Path] = []

    def mapped(path: Path) -> list[str] | None:
        calls.append(path)
        return read_lines(mapping.get(path, MISSING))

    monkeypatch.setattr(sc, "_read_lines", mapped)
    source = sc.FlyShortcutSource(user / "keyshortcutrc", system / "keyshortcutrc")
    assert source.lookup("Alt+F4")[0].title == "Закрыть окно"
    assert calls[2:] == [
        user / "ru_RU.UTF-8.miscrc",
        system / "ru_RU.UTF-8.miscrc",
        user / "en.miscrc",
        system / "en.miscrc",
    ]


def test_unavailable_files_log_debug(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG, logger=sc.__name__):
        sources = [
            sc.KdeShortcutSource(MISSING),
            sc.FlyShortcutSource(MISSING, MISSING, locale_paths=[MISSING]),
        ]
    for source in sources:
        assert not source.available
        assert source.lookup("Alt+F4") == []
    assert "не удалось прочитать" in caplog.text


def test_damaged_lines_are_skipped_and_logged(caplog: pytest.LogCaptureFixture) -> None:
    path = FIXTURES / "shortcuts-malformed.sample"
    with caplog.at_level(logging.DEBUG, logger=sc.__name__):
        kde = sc.KdeShortcutSource(path)
        fly = sc.FlyShortcutSource(path, MISSING, locale_paths=[path])
    assert kde.lookup("Alt+F4")[0].title == "Закрыть окно"
    assert kde.lookup("Alt+F9") == []
    assert kde.lookup("Ctrl+F9") == []
    assert fly.lookup("Alt+F4")[0].title == "Закрыть окно"
    for combo in ("Alt+F1", "Alt+F2", "Alt+F3"):
        assert fly.lookup(combo) == []
    assert "битой кодировкой" in caplog.text
    assert "пропущена строка KDE" in caplog.text
    assert "пропущена строка Fly" in caplog.text
    assert "пропущена строка локали Fly" in caplog.text
    for number in (3, 4, 5, 8, 9, 10, 12):
        assert f"{path}:{number}" in caplog.text
    for raw in path.read_bytes().splitlines():
        if raw.strip():
            assert raw.decode("utf-8", errors="replace") not in caplog.text


def test_missing_locale_and_empty_title_remain_unknown() -> None:
    missing = sc.FlyShortcutSource(FLY, MISSING, locale_paths=[MISSING])
    empty = sc.FlyShortcutSource(
        FLY, MISSING, locale_paths=[FIXTURES / "shortcuts-malformed.sample"]
    )
    assert missing.lookup("Alt+F4")[0].title is None
    assert empty.lookup("Meta+Left")[0].title is None


def test_unreadable_path_is_unavailable() -> None:
    assert not sc.KdeShortcutSource(FIXTURES).available
    assert not sc.FlyShortcutSource(FIXTURES, FIXTURES, locale_paths=[]).available


@pytest.mark.parametrize("kind", [SessionKind.KDE, SessionKind.FLY, SessionKind.OTHER])
def test_factory(kind: SessionKind) -> None:
    source = sc.for_session(
        kind, kde_path=KDE, fly_path=EMPTY_FLY, fly_system_path=FLY, locale_paths=[RU]
    )
    if kind == SessionKind.OTHER:
        assert not source.available
        assert source.lookup("Alt+F4") == []
    else:
        assert source.available
        assert source.lookup("Alt+F4")[0].source == kind.value


def test_other_never_reads_files(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(path: Path) -> list[str] | None:
        pytest.fail(f"OTHER не должен читать {path}")

    monkeypatch.setattr(sc, "_read_lines", forbidden)
    source = sc.for_session(SessionKind.OTHER)
    assert not source.available
    assert source.lookup("Ctrl+Space") == []


@pytest.mark.parametrize("unsafe", ["fifo", "symlink", "oversized", "directory"])
@pytest.mark.parametrize("target", ["kde", "fly_user", "fly_system", "fly_locale"])
def test_t47_unsafe_files_are_unavailable_without_blocking(
    target: str,
    unsafe: str,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    read_deadline: None,
) -> None:
    path = (
        tmp_path
        / {
            "kde": "kglobalshortcutsrc",
            "fly_user": "keyshortcutrc",
            "fly_system": "keyshortcutrc",
            "fly_locale": "ru_RU.UTF-8.miscrc",
        }[target]
    )
    if unsafe == "fifo":
        os.mkfifo(path)
    elif unsafe == "symlink":
        path.symlink_to("/dev/zero")
    elif unsafe == "directory":
        path.mkdir()
    else:
        path.write_bytes(b"x" * (2 * 1024 * 1024))

    with caplog.at_level(logging.DEBUG, logger=sc.__name__):
        started = time.monotonic()
        source = sc.for_session(
            SessionKind.KDE if target == "kde" else SessionKind.FLY,
            kde_path=path if target == "kde" else KDE,
            fly_path=path if target == "fly_user" else EMPTY_FLY if target == "fly_system" else FLY,
            fly_system_path=path if target == "fly_system" else FLY,
            locale_paths=[path] if target == "fly_locale" else [RU],
        )
        elapsed = time.monotonic() - started

    assert elapsed < 0.100
    assert not source.available
    assert source.lookup("Alt+F4") == []
    assert source.lookup("Alt+Space") == []
    reason = "размер превышает 1 МиБ" if unsafe == "oversized" else "не обычный файл"
    assert [record.getMessage() for record in caplog.records] == [
        f"не удалось прочитать {path}: {reason}"
    ]


@pytest.mark.parametrize("size", [1024 * 1024 - 1, 1024 * 1024, 1024 * 1024 + 1])
def test_t47_file_size_boundary(tmp_path: Path, size: int) -> None:
    path = tmp_path / "kglobalshortcutsrc"
    prefix = b"[app]\nlaunch=Alt+F4,,Title\n#"
    path.write_bytes(prefix + b"x" * (size - len(prefix)))
    source = sc.for_session(SessionKind.KDE, kde_path=path)
    assert source.available is (size <= 1024 * 1024)
    assert bool(source.lookup("Alt+F4")) is source.available


@pytest.mark.parametrize("replacement", ["fifo", "symlink", "oversized"])
def test_t47_replacement_between_stat_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, replacement: str, read_deadline: None
) -> None:
    path = tmp_path / "kglobalshortcutsrc"
    path.write_text("[app]\nlaunch=Alt+F4,,Title\n", encoding="utf-8")
    original_stat = Path.stat

    def replace_after_stat(self: Path, *, follow_symlinks: bool = True) -> os.stat_result:
        result = original_stat(self, follow_symlinks=follow_symlinks)
        if self == path:
            path.unlink()
            if replacement == "fifo":
                os.mkfifo(path)
            elif replacement == "symlink":
                path.symlink_to("/dev/zero")
            else:
                path.write_bytes(b"x" * (2 * 1024 * 1024))
        return result

    monkeypatch.setattr(Path, "stat", replace_after_stat)
    started = time.monotonic()
    source = sc.for_session(SessionKind.KDE, kde_path=path)
    assert time.monotonic() - started < 0.100
    assert not source.available
    assert source.lookup("Alt+F4") == []


def test_t47_growth_after_descriptor_stat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "kglobalshortcutsrc"
    path.write_text("[app]\nlaunch=Alt+F4,,Title\n", encoding="utf-8")
    original_fstat = os.fstat
    calls = 0

    def grow_after_stat(fd: int) -> os.stat_result:
        nonlocal calls
        info = original_fstat(fd)
        calls += 1
        if calls == 1:
            with path.open("ab") as fh:
                fh.write(b"x" * (2 * 1024 * 1024))
        else:
            # Дескриптор действительно прочитал не больше 1 МиБ даже после роста файла.
            assert os.lseek(fd, 0, os.SEEK_CUR) == 1024 * 1024
        return info

    monkeypatch.setattr(os, "fstat", grow_after_stat)
    source = sc.for_session(SessionKind.KDE, kde_path=path)
    assert calls == 2
    assert not source.available
    assert source.lookup("Alt+F4") == []


@pytest.mark.parametrize("kind", [SessionKind.KDE, SessionKind.FLY])
@pytest.mark.parametrize("long_label", [False, True])
def test_t47_untrusted_labels_are_cleaned(
    kind: SessionKind, long_label: bool, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    label = "  Чужая  \t\x1b\u202e подпись  "
    if long_label:
        label += "я" * ((10 * 1024 - len(label.encode("utf-8"))) // 2)
        label += "x" * (10 * 1024 - len(label.encode("utf-8")))
        assert len(label.encode("utf-8")) == 10 * 1024
    path = tmp_path / "kglobalshortcutsrc"
    path.write_text(
        f"[app]\n{label}=Alt+F4,,{label}\n_k_friendly_name={label}\n{label}=Alt+F3,,\n",
        encoding="utf-8",
    )
    locale = tmp_path / "ru_RU.UTF-8.miscrc"
    locale.write_text(f'"{label}" "" "" FLYWM_CLOSE\n', encoding="utf-8")
    with caplog.at_level(logging.DEBUG, logger=sc.__name__):
        source = sc.for_session(
            kind, kde_path=path, fly_path=FLY, fly_system_path=MISSING, locale_paths=[locale]
        )
    hit = source.lookup("Alt+F4")[0]
    assert hit.title is not None
    values = [hit.title, hit.action, hit.section_name, str(hit)]
    if kind == SessionKind.KDE:
        fallback = source.lookup("Alt+F3")[0]
        assert fallback.title is None
        values.extend([fallback.action, str(fallback)])
        assert hit.action == hit.title == hit.section_name
    for value in values:
        assert len(value) <= 120
        assert all(unicodedata.category(char) not in {"Cc", "Cf"} for char in value)
        assert value == " ".join(value.split())
    if long_label:
        assert len(hit.title) == 120
        assert hit.title.endswith("…")
    else:
        assert hit.title == "Чужая подпись"
    assert caplog.records == []


def test_t47_logs_contain_only_path_and_line_number(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "kglobalshortcutsrc"
    lines = [b"[app]", b"secret-broken-line", b"secret-invalid-utf8-\xff"]
    path.write_bytes(b"\n".join(lines))
    with caplog.at_level(logging.DEBUG, logger=sc.__name__):
        sc.for_session(SessionKind.KDE, kde_path=path)
    assert [record.getMessage() for record in caplog.records] == [
        f"пропущена строка с битой кодировкой: {path}:3",
        f"пропущена строка KDE: {path}:2",
    ]
    assert all(line.decode("utf-8", errors="replace") not in caplog.text for line in lines)
