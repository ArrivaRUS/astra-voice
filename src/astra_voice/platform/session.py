"""Определение вида сеанса рабочего стола (решение A-04).

Сначала переменные окружения, затем — свойства корневого окна X11 через
``python3-xlib``. Без Xlib или без ``DISPLAY`` работают только переменные.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from enum import StrEnum
from typing import Any

log = logging.getLogger(__name__)


class SessionKind(StrEnum):
    KDE = "KDE"
    FLY = "FLY"
    OTHER = "OTHER"


def _from_env(env: Mapping[str, str]) -> SessionKind | None:
    current = env.get("XDG_CURRENT_DESKTOP", "")
    parts = [p.strip().lower() for p in current.replace(";", ":").split(":") if p.strip()]
    if any("fly" in p for p in parts):
        return SessionKind.FLY
    if any(p == "kde" or p.startswith("kde") for p in parts):
        return SessionKind.KDE
    if env.get("DESKTOP_SESSION", "").strip().lower() == "fly":
        return SessionKind.FLY
    return None


def _x11_property(conn: Any, window: Any, name: str) -> Any:
    """Read any type: a present wrong-type value must not look absent."""
    atom = conn.intern_atom(name, only_if_exists=True)
    return window.get_full_property(atom, 0) if atom else None  # AnyPropertyType


def _x11_uint32(prop: Any, property_type: int) -> int:
    """One positive WINDOW/CARDINAL; bool and values outside X11's range fail."""
    if (
        prop is None
        or getattr(prop, "format", None) != 32
        or getattr(prop, "property_type", None) != property_type
    ):
        raise ValueError("invalid X11 integer property")
    values: Any = getattr(prop, "value", None)
    try:
        if len(values) != 1:
            raise ValueError("invalid X11 integer count")
        value: object = values[0]
    except (TypeError, IndexError, KeyError):
        raise ValueError("invalid X11 integer value") from None
    if not isinstance(value, int) or isinstance(value, bool) or not 0 < value <= 0xFFFFFFFF:
        raise ValueError("invalid X11 integer value")
    return value


def _x11_text(prop: Any, property_type: int, encoding: str) -> str:
    if (
        not property_type
        or getattr(prop, "format", None) != 8
        or getattr(prop, "property_type", None) != property_type
        or not isinstance(getattr(prop, "value", None), bytes)
    ):
        raise ValueError("invalid X11 text property")
    raw: bytes = prop.value
    return raw.decode(encoding)


def _ewmh_wm(conn: Any, root: Any) -> tuple[Any, str | None]:
    """Validate a live EWMH self-link, then read its name with strict precedence.

    Fly 1.8.4 may expose WM_NAME only on root. That fallback is allowed only
    when BOTH names on the check window are absent, never malformed or wrong.
    """
    check = "_NET_SUPPORTING_WM_CHECK"
    window_id = _x11_uint32(_x11_property(conn, root, check), 33)  # WINDOW
    window = conn.create_resource_object("window", window_id)
    if _x11_uint32(_x11_property(conn, window, check), 33) != window_id:
        raise ValueError("invalid EWMH self-link")
    prop = _x11_property(conn, window, "_NET_WM_NAME")
    if prop is not None:
        utf8 = conn.intern_atom("UTF8_STRING", only_if_exists=True)
        return window, _x11_text(prop, utf8, "utf-8")
    prop = _x11_property(conn, window, "WM_NAME")
    if prop is None:
        prop = _x11_property(conn, root, "WM_NAME")
    return window, _x11_text(prop, 31, "latin-1") if prop is not None else None  # STRING


def _from_x11(display_name: str) -> SessionKind | None:
    """FLY_SESSION marker or a typed, self-linked EWMH window's exact Fly name."""
    try:
        from Xlib import display as xdisplay
    except Exception:  # noqa: BLE001 — python3-xlib может отсутствовать
        return None
    conn = None
    try:
        conn = xdisplay.Display(display_name)
        root = conn.screen().root
        prop = _x11_property(conn, root, "FLY_SESSION")
        if prop is not None:
            try:
                if _x11_text(prop, 31, "latin-1") == "fly":
                    return SessionKind.FLY
            except ValueError:
                pass  # This marker is only a hint; EWMH can independently identify the WM.
        _, name = _ewmh_wm(conn, root)
        if name == "fly-wm":
            return SessionKind.FLY
        if name is not None and "kwin" in name.lower():
            return SessionKind.KDE
        return None
    except Exception:  # noqa: BLE001 — X может быть недоступен; не журналируем его данные
        log.debug("не удалось прочитать свойства WM X11")
        return None
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def detect(env: Mapping[str, str] | None = None) -> SessionKind:
    """Вид сеанса: env → атомы X11 → ``OTHER``."""
    environ = os.environ if env is None else env
    kind = _from_env(environ)
    if kind is not None:
        return kind
    display = environ.get("DISPLAY", "").strip()
    if display:
        kind = _from_x11(display)
        if kind is not None:
            return kind
    return SessionKind.OTHER
