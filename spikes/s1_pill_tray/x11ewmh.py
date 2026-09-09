#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S1 · EWMH руками через ctypes + libX11 (прототип для platform/x11.py).

Почему ctypes, а не python3-xlib: `python3-xlib` 0.33 есть в apt Астры
(plan-claude §12), но на этой машине НЕ установлен, а ставить в систему нельзя.
ctypes + libX11.so.6 не требует ни одного нового пакета и даёт ровно те же
XChangeProperty/XSendEvent. Решение о зависимости — за M1 (см. README «Открытые вопросы»).

Что умеет:
  set_window_type_notification()  — _NET_WM_WINDOW_TYPE = _NET_WM_WINDOW_TYPE_NOTIFICATION
  set_state_above_skip()          — _NET_WM_STATE = ABOVE + SKIP_TASKBAR + SKIP_PAGER (до map)
  set_user_time_zero()            — _NET_WM_USER_TIME = 0 (не претендуем на фокус)
  set_fly_no_animation()          — _FLY_WM_WINDOW_MAP_ANIMATION = 0, _FLY_WM_FADE_SHOW = 0
  reassert_above()                — client-message _NET_WM_STATE_ADD после map (урок Cowork)
  active_window() / net_workarea()/ window_geometry()
"""
from __future__ import annotations

import ctypes
import ctypes.util

Window = ctypes.c_ulong
Atom = ctypes.c_ulong

_PropModeReplace = 0
_XA_ATOM = 4
_XA_CARDINAL = 6
_ClientMessage = 33
_SubstructureNotifyMask = 1 << 19
_SubstructureRedirectMask = 1 << 20

_NET_WM_STATE_REMOVE = 0
_NET_WM_STATE_ADD = 1


class _XClientMessageEvent(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_int),
        ("serial", ctypes.c_ulong),
        ("send_event", ctypes.c_int),
        ("display", ctypes.c_void_p),
        ("window", Window),
        ("message_type", Atom),
        ("format", ctypes.c_int),
        ("l", ctypes.c_long * 5),
    ]


class X11:
    def __init__(self, display_name: bytes | None = None):
        path = ctypes.util.find_library("X11") or "libX11.so.6"
        self.lib = ctypes.CDLL(path)
        self.lib.XOpenDisplay.restype = ctypes.c_void_p
        self.lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.dpy = self.lib.XOpenDisplay(display_name)
        if not self.dpy:
            raise RuntimeError("XOpenDisplay не удался (нет $DISPLAY?)")
        self.lib.XInternAtom.restype = Atom
        self.lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        self.lib.XDefaultRootWindow.restype = Window
        self.lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.lib.XChangeProperty.argtypes = [
            ctypes.c_void_p, Window, Atom, Atom, ctypes.c_int, ctypes.c_int,
            ctypes.c_void_p, ctypes.c_int,
        ]
        self.lib.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, Window, Atom, ctypes.c_long, ctypes.c_long, ctypes.c_int,
            Atom, ctypes.POINTER(Atom), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
        ]
        self.lib.XSendEvent.argtypes = [
            ctypes.c_void_p, Window, ctypes.c_int, ctypes.c_long, ctypes.c_void_p,
        ]
        self.lib.XGetGeometry.argtypes = [
            ctypes.c_void_p, Window, ctypes.POINTER(Window),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
        ]
        self.lib.XTranslateCoordinates.argtypes = [
            ctypes.c_void_p, Window, Window, ctypes.c_int, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
            ctypes.POINTER(Window),
        ]
        # ВАЖНО: без argtypes ctypes передаёт указатель Display* как C int (32 бита)
        # и на amd64 обрезает его → SIGSEGV. Подписываем ВСЕ используемые функции.
        self.lib.XFlush.argtypes = [ctypes.c_void_p]
        self.lib.XSync.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.lib.XFree.argtypes = [ctypes.c_void_p]
        self.lib.XGetAtomName.restype = ctypes.c_char_p
        self.lib.XGetAtomName.argtypes = [ctypes.c_void_p, Atom]
        self.lib.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.root = self.lib.XDefaultRootWindow(self.dpy)
        self._atoms: dict[str, int] = {}

    # --- базовое -------------------------------------------------------
    def atom(self, name: str, only_if_exists: bool = False) -> int:
        key = f"{name}:{int(only_if_exists)}"
        if key not in self._atoms:
            self._atoms[key] = int(
                self.lib.XInternAtom(self.dpy, name.encode(), 1 if only_if_exists else 0)
            )
        return self._atoms[key]

    def flush(self) -> None:
        self.lib.XFlush(self.dpy)

    def sync(self) -> None:
        self.lib.XSync(self.dpy, 0)

    def _set32(self, win: int, prop: str, prop_type: int, values: list[int]) -> None:
        arr = (ctypes.c_long * len(values))(*values)
        self.lib.XChangeProperty(
            self.dpy, Window(win), Atom(self.atom(prop)), Atom(prop_type), 32,
            _PropModeReplace, ctypes.cast(arr, ctypes.c_void_p), len(values),
        )

    def get32(self, win: int, prop: str, max_items: int = 32) -> list[int]:
        actual_type = Atom()
        actual_fmt = ctypes.c_int()
        nitems = ctypes.c_ulong()
        after = ctypes.c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        rc = self.lib.XGetWindowProperty(
            self.dpy, Window(win), Atom(self.atom(prop)), 0, max_items, 0, 0,
            ctypes.byref(actual_type), ctypes.byref(actual_fmt), ctypes.byref(nitems),
            ctypes.byref(after), ctypes.byref(data),
        )
        if rc != 0 or not data or actual_fmt.value != 32:
            return []
        longs = ctypes.cast(data, ctypes.POINTER(ctypes.c_long))
        out = [int(longs[i]) for i in range(nitems.value)]
        self.lib.XFree(data)
        return out

    # --- то, что требует S1 --------------------------------------------
    def set_window_type_notification(self, win: int) -> None:
        self._set32(win, "_NET_WM_WINDOW_TYPE", _XA_ATOM,
                    [self.atom("_NET_WM_WINDOW_TYPE_NOTIFICATION")])

    def set_state_above_skip(self, win: int) -> None:
        self._set32(win, "_NET_WM_STATE", _XA_ATOM, [
            self.atom("_NET_WM_STATE_ABOVE"),
            self.atom("_NET_WM_STATE_SKIP_TASKBAR"),
            self.atom("_NET_WM_STATE_SKIP_PAGER"),
        ])

    def set_user_time_zero(self, win: int) -> None:
        """USER_TIME=0 — «окно не претендует на фокус» (EWMH §_NET_WM_USER_TIME)."""
        self._set32(win, "_NET_WM_USER_TIME", _XA_CARDINAL, [0])

    def set_fly_no_animation(self, win: int) -> None:
        """Fly-ветка (plan-claude §13.1): без этого показ во Fly > 100 мс из-за map/fade."""
        self._set32(win, "_FLY_WM_WINDOW_MAP_ANIMATION", _XA_CARDINAL, [0])
        self._set32(win, "_FLY_WM_FADE_SHOW", _XA_CARDINAL, [0])

    def set_fly_corner_radius(self, win: int, radius: int = 18) -> None:
        self._set32(win, "_FLY_WM_WINDOW_CORNER_RADIUS", _XA_CARDINAL, [radius])

    def reassert_above(self, win: int) -> None:
        """После map свойство уже не читается — нужен client-message (урок Astra Cowork)."""
        ev = _XClientMessageEvent()
        ev.type = _ClientMessage
        ev.serial = 0
        ev.send_event = 1
        ev.display = self.dpy
        ev.window = Window(win)
        ev.message_type = Atom(self.atom("_NET_WM_STATE"))
        ev.format = 32
        for atom_name in ("_NET_WM_STATE_ABOVE", "_NET_WM_STATE_SKIP_TASKBAR",
                          "_NET_WM_STATE_SKIP_PAGER"):
            ev.l[0] = _NET_WM_STATE_ADD
            ev.l[1] = self.atom(atom_name)
            ev.l[2] = 0
            ev.l[3] = 1  # source indication: normal application
            ev.l[4] = 0
            buf = (ctypes.c_char * 192)()          # sizeof(XEvent) на amd64
            ctypes.memmove(buf, ctypes.byref(ev), ctypes.sizeof(ev))
            self.lib.XSendEvent(
                self.dpy, Window(self.root), 0,
                ctypes.c_long(_SubstructureNotifyMask | _SubstructureRedirectMask),
                ctypes.cast(buf, ctypes.c_void_p),
            )
        self.flush()

    # --- чтение окружения ----------------------------------------------
    def active_window(self) -> int:
        vals = self.get32(self.root, "_NET_ACTIVE_WINDOW", 1)
        return vals[0] if vals else 0

    def net_workarea(self) -> list[int]:
        return self.get32(self.root, "_NET_WORKAREA", 16)

    def window_geometry(self, win: int) -> tuple[int, int, int, int] | None:
        """(x, y, w, h) в координатах root."""
        if not win:
            return None
        root = Window()
        x = ctypes.c_int()
        y = ctypes.c_int()
        w = ctypes.c_uint()
        h = ctypes.c_uint()
        bw = ctypes.c_uint()
        depth = ctypes.c_uint()
        if not self.lib.XGetGeometry(
            self.dpy, Window(win), ctypes.byref(root), ctypes.byref(x), ctypes.byref(y),
            ctypes.byref(w), ctypes.byref(h), ctypes.byref(bw), ctypes.byref(depth)
        ):
            return None
        rx = ctypes.c_int()
        ry = ctypes.c_int()
        child = Window()
        self.lib.XTranslateCoordinates(
            self.dpy, Window(win), Window(self.root), 0, 0,
            ctypes.byref(rx), ctypes.byref(ry), ctypes.byref(child)
        )
        return (rx.value, ry.value, w.value, h.value)

    def atom_names(self, win: int, prop: str) -> list[str]:
        """Имена атомов свойства — для самопроверки без xprop."""
        out = []
        for a in self.get32(win, prop):
            name = self.lib.XGetAtomName(self.dpy, Atom(a))
            out.append(name.decode() if name else str(a))
        return out
