"""Определение вида сеанса рабочего стола (решение A-04).

Сначала переменные окружения, затем — свойства корневого окна X11 через
``python3-xlib``. Без Xlib или без ``DISPLAY`` работают только переменные.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping
from enum import StrEnum

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


def _from_x11(display_name: str) -> SessionKind | None:
    """Атомы корневого окна: ``_FLY_WM_PID`` → Fly, ``_NET_WM_NAME`` = KWin → KDE."""
    try:
        from Xlib import X
        from Xlib import display as xdisplay
    except Exception:  # noqa: BLE001 — python3-xlib может отсутствовать
        return None
    conn = None
    try:
        conn = xdisplay.Display(display_name)
        root = conn.screen().root
        fly_atom = conn.intern_atom("_FLY_WM_PID", True)
        if fly_atom and root.get_full_property(fly_atom, X.AnyPropertyType) is not None:
            return SessionKind.FLY
        check_atom = conn.intern_atom("_NET_SUPPORTING_WM_CHECK", True)
        if not check_atom:
            return None
        prop = root.get_full_property(check_atom, X.AnyPropertyType)
        if prop is None or not len(prop.value):
            return None
        window = conn.create_resource_object("window", prop.value[0])
        name_atom = conn.intern_atom("_NET_WM_NAME", True)
        name_prop = window.get_full_property(name_atom, X.AnyPropertyType) if name_atom else None
        raw = getattr(name_prop, "value", b"") if name_prop is not None else b""
        name = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
        low = name.lower()
        if "kwin" in low:
            return SessionKind.KDE
        if "fly" in low:
            return SessionKind.FLY
        return None
    except Exception as exc:  # noqa: BLE001 — X может быть недоступен
        log.debug("не удалось прочитать атомы X11: %s", exc)
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
