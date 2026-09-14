"""Подсказки о владельцах комбинаций из настроек KDE и Fly, только чтение.

Это неполные сведения: KDE и kglobalaccel не видят захваты обычных X-клиентов.
На машине заказчика 2026-09-09 Handy держал Ctrl+Space, которую KDE считал
свободной. Занятость определяет живой XGrabKey-пробник в platform/hotkey.py;
здесь authoritative=False, а пустой lookup не означает свободную комбинацию.

Парсер KDE перенесён из spikes/s4_hotkey_paste/shortcuts_conflict.py.
Активные комбинации разделяются экранированной табуляцией (слэш и t) либо TAB;
none означает снятую комбинацию. Значения по умолчанию KDE не считаются активными.
Fly при пустом пользовательском файле использует системные значения по умолчанию.
"""

from __future__ import annotations

import logging
import os
import re
import stat
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal, Protocol

from astra_voice.platform.session import SessionKind

log = logging.getLogger(__name__)

KDE_PATH = "~/.config/kglobalshortcutsrc"
FLY_PATH = "~/.fly/keyshortcutrc"
FLY_SYSTEM_PATH = "/usr/share/fly-wm/keyshortcutrc"

# docs/threat-model.md, У48: не снимать лимит и проверку обычного файла — GUI зависнет.
MAX_CONFIG_BYTES: Final = 1024 * 1024
IS_REGULAR_FILE: Final = stat.S_ISREG
MAX_CONFLICT_LABEL: Final = 120


def _clean_conflict_label(value: str) -> str:
    """Очищает чужую подпись по правилу T-41/У48, сохраняя предел вместе с многоточием."""
    normalized = " ".join(value.split())
    cleaned = "".join(char for char in normalized if unicodedata.category(char) not in {"Cc", "Cf"})
    cleaned = " ".join(cleaned.split())
    if len(cleaned) > MAX_CONFLICT_LABEL:
        cleaned = cleaned[: MAX_CONFLICT_LABEL - 1].rstrip() + "…"
    return cleaned


@dataclass(frozen=True)
class ConflictHit:
    """Запись настроек; title=None означает отсутствие известной подписи."""

    combo: str
    source: Literal["KDE", "FLY"]
    action: str
    title: str | None
    section: str
    section_name: str
    origin_path: Path
    is_system_default: bool
    authoritative: Literal[False] = field(default=False, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "action", _clean_conflict_label(self.action))
        if self.title is not None:
            object.__setattr__(self, "title", _clean_conflict_label(self.title) or None)
        object.__setattr__(self, "section_name", _clean_conflict_label(self.section_name))

    def __str__(self) -> str:
        return self.title if self.title is not None else self.action


class ShortcutConflictSource(Protocol):
    """Источник подсказок; available означает доступность файла, не полноту сведений."""

    @property
    def available(self) -> bool: ...

    def lookup(self, combo: str) -> list[ConflictHit]: ...


def norm_combo(s: str) -> str:
    """Нормализация из спайка: ctrl+space → Ctrl+Space; Mod4 → Meta для Fly."""
    order = ["Meta", "Ctrl", "Alt", "Shift"]
    aliases = {
        "control": "Ctrl",
        "ctrl": "Ctrl",
        "alt": "Alt",
        "shift": "Shift",
        "meta": "Meta",
        "super": "Meta",
        "win": "Meta",
        "mod4": "Meta",
    }
    keys = {
        k.lower(): k
        for k in (
            "Space",
            "Tab",
            "Return",
            "Enter",
            "Escape",
            "Backspace",
            "Delete",
            "Insert",
            "Home",
            "End",
            "Left",
            "Right",
            "Up",
            "Down",
            "PageUp",
            "PageDown",
            "Search",
            "Print",
            "Pause",
            "Super_L",
            "Super_R",
            "less",
        )
    }
    parts = [p.strip() for p in s.replace("|", "+").split("+") if p.strip()]
    mods, key = [], []
    for p in parts:
        if p.lower() == "none":
            continue
        a = aliases.get(p.lower())
        if a:
            if a not in mods:
                mods.append(a)
        else:
            if len(p) == 1 or re.fullmatch(r"f\d+", p, re.IGNORECASE):
                key.append(p.upper())
            else:
                key.append(keys.get(p.lower(), p))
    mods.sort(key=lambda m: order.index(m))
    return "+".join(mods + key)


class _UnsafeConfigError(Exception):
    """Небезопасный файл запрещает использовать источник, включая запасные файлы."""


def _check_config_file(path: Path, info: os.stat_result) -> None:
    """У48: проверяет путь и открытый дескриптор до и после ограниченного чтения."""
    if not IS_REGULAR_FILE(info.st_mode):
        log.debug("не удалось прочитать %s: не обычный файл", path)
        raise _UnsafeConfigError
    if info.st_size > MAX_CONFIG_BYTES:
        log.debug("не удалось прочитать %s: размер превышает 1 МиБ", path)
        raise _UnsafeConfigError


def _read_lines(path: Path) -> list[str] | None:
    """Ограничивает чтение обычными файлами; пропускает строки с повреждённым UTF-8."""
    try:
        _check_config_file(path, path.stat(follow_symlinks=False))
        # У48: подмена после stat не должна открыть symlink или заблокировать FIFO.
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            _check_config_file(path, os.fstat(fd))
            with os.fdopen(fd, "rb", closefd=False) as fh:
                data = fh.read(MAX_CONFIG_BYTES)
            _check_config_file(path, os.fstat(fd))
        finally:
            os.close(fd)
    except FileNotFoundError:
        log.debug("не удалось прочитать %s: файл отсутствует", path)
        return None
    except OSError as exc:
        log.debug("не удалось прочитать %s: ошибка ОС %s", path, exc.errno)
        raise _UnsafeConfigError from None
    lines = []
    for number, raw in enumerate(data.split(b"\n"), 1):
        try:
            lines.append(raw.decode("utf-8").strip())
        except UnicodeError:
            log.debug("пропущена строка с битой кодировкой: %s:%d", path, number)
            lines.append("")  # Сохраняем номера следующих строк исходного файла.
    return lines


class _Source:
    """Общий поиск по нормализованной комбинации в снимке файла."""

    def __init__(self) -> None:
        self._hits: list[ConflictHit] = []
        self._available = False

    @property
    def available(self) -> bool:
        return self._available

    def lookup(self, combo: str) -> list[ConflictHit]:
        want = norm_combo(combo)
        return [hit for hit in self._hits if hit.combo == want]


class KdeShortcutSource(_Source):
    """Читает активные назначения KDE, включая имя раздела после действий."""

    def __init__(self, path: str | Path = KDE_PATH) -> None:
        super().__init__()
        origin = Path(path).expanduser()
        try:
            lines = _read_lines(origin)
        except _UnsafeConfigError:
            return
        if lines is None:
            return
        self._available = True
        section = ""
        friendly: dict[str, str] = {}
        pending: list[tuple[str, str, list[str], str | None]] = []
        for number, line in enumerate(lines, 1):
            if not line or line.startswith("#"):
                continue
            m = re.match(r"^\[(.+)\]$", line)
            if m:
                section = m.group(1)
                continue
            if "=" not in line:
                log.debug("пропущена строка KDE: %s:%d", origin, number)
                continue
            action, value = line.split("=", 1)
            action = action.strip()
            if action == "_k_friendly_name":
                friendly[section] = value
                continue
            fields = value.split(",", 2)
            if not section or not action or len(fields) < 3:
                log.debug("пропущена строка KDE: %s:%d", origin, number)
                continue
            active = fields[0]
            title = fields[2].strip() or None
            # ГРАБЛИ: KConfig хранит слэш и t, а не настоящий TAB.
            # Сохраняем оба варианта из проверенного парсера спайка.
            combos = [
                norm_combo(c)
                for c in re.split(r"\\t|\t", active)
                if c.strip() and c.strip().lower() != "none"
            ]
            pending.append((section, action, combos, title))
        for section, action, combos, title in pending:
            for combo in combos:
                if combo:
                    self._hits.append(
                        ConflictHit(
                            combo,
                            "KDE",
                            action,
                            title,
                            section,
                            friendly.get(section, section),
                            origin,
                            False,
                        )
                    )


def _fly_rows(path: Path) -> tuple[bool, list[tuple[str, str, str]]]:
    """Читает назначения Fly; комментарии с точкой с запятой не являются действиями."""
    lines = _read_lines(path)
    rows: list[tuple[str, str, str]] = []
    if lines is None:
        return False, rows
    section = ""
    for number, line in enumerate(lines, 1):
        if not line or line.startswith((";", "#")):
            continue
        m = re.fullmatch(r"\[(.+)\]", line)
        if m:
            section = m.group(1)
            continue
        if section != "ShortCutKeys" or "=" not in line:
            log.debug("пропущена строка Fly: %s:%d", path, number)
            continue
        combo, action = (p.strip() for p in line.split("=", 1))
        parts = [p.strip() for p in combo.split("|")]
        modifiers = {"none", "mod4", "meta", "super", "win", "ctrl", "control", "alt", "shift"}
        if (
            not re.fullmatch(r"FLYWM_\w+", action)
            or not parts[-1]
            or any(p.lower() not in modifiers for p in parts[:-1])
            or not norm_combo(combo)
        ):
            log.debug("пропущена строка Fly: %s:%d", path, number)
            continue
        rows.append((section, norm_combo(combo), action))
    return True, rows


def _fly_titles(paths: Sequence[Path]) -> dict[str, str]:
    """Первый найденный перевод имеет приоритет; неизвестные названия не сочиняем."""
    titles: dict[str, str] = {}
    pattern = re.compile(r'^"((?:\\.|[^"\\])*)"\s+"[^"]*"\s+"[^"]*"\s+(FLYWM_\w+)$')
    for path in paths:
        for number, line in enumerate(_read_lines(path) or [], 1):
            if not line or line.startswith((";", "#")):
                continue
            match = pattern.fullmatch(line)
            if match is None:
                log.debug("пропущена строка локали Fly: %s:%d", path, number)
                continue
            title = match.group(1).replace(r"\&", "").strip()
            if title:
                titles.setdefault(match.group(2), title)
    return titles


class FlyShortcutSource(_Source):
    """Пользовательские назначения Fly либо системный запасной файл.

    Локаль: LC_ALL → LC_MESSAGES → LANG → en; имя сохраняется целиком,
    например ru_RU.UTF-8.miscrc. Сначала пользовательский перевод, затем системный,
    затем en.miscrc в том же порядке. Каталоги берутся рядом с keyshortcutrc.
    locale_paths позволяет явно задать упорядоченный список файлов переводов.
    """

    def __init__(
        self,
        path: str | Path = FLY_PATH,
        system_path: str | Path = FLY_SYSTEM_PATH,
        *,
        locale_name: str | None = None,
        locale_paths: Sequence[str | Path] | None = None,
    ) -> None:
        super().__init__()
        user_path = Path(path).expanduser()
        default_path = Path(system_path).expanduser()
        try:
            available, rows = _fly_rows(user_path)
            origin, is_default = user_path, False
            if not rows:
                available, rows = _fly_rows(default_path)
                origin, is_default = default_path, True
        except _UnsafeConfigError:
            return
        if not rows:
            self._available = available
            return
        if locale_paths is None:
            name = locale_name or next(
                (os.environ[k] for k in ("LC_ALL", "LC_MESSAGES", "LANG") if os.environ.get(k)),
                "en",
            )
            # Имя локали — имя файла, а не путь к произвольному каталогу.
            if "/" in name or name in (".", ".."):
                log.debug("неподходящее имя локали Fly: %r", name)
                name = "en"
            paths = [
                directory / f"{language}.miscrc"
                for language in dict.fromkeys((name, "en"))
                for directory in (user_path.parent, default_path.parent)
            ]
        else:
            paths = [Path(p).expanduser() for p in locale_paths]
        try:
            titles = _fly_titles(paths)
        except _UnsafeConfigError:
            return
        self._available = available
        self._hits = [
            ConflictHit(
                combo, "FLY", action, titles.get(action), section, section, origin, is_default
            )
            for section, combo, action in rows
        ]


def for_session(
    kind: SessionKind,
    *,
    kde_path: str | Path = KDE_PATH,
    fly_path: str | Path = FLY_PATH,
    fly_system_path: str | Path = FLY_SYSTEM_PATH,
    locale_name: str | None = None,
    locale_paths: Sequence[str | Path] | None = None,
) -> ShortcutConflictSource:
    """Выбирает источник по сеансу; OTHER ничего не читает."""
    if kind == SessionKind.KDE:
        return KdeShortcutSource(kde_path)
    if kind == SessionKind.FLY:
        return FlyShortcutSource(
            fly_path, fly_system_path, locale_name=locale_name, locale_paths=locale_paths
        )
    return _Source()
