"""Мелкие операции X11 через ``python3-xlib``.

Нужны для активации окна: KWin применяет защиту от кражи фокуса и в ответ на
``requestActivate()`` без свежего ``_NET_WM_USER_TIME`` лишь подсвечивает окно
(``_NET_WM_STATE_DEMANDS_ATTENTION``). Второй экземпляр берёт метку времени у
X-сервера и передаёт её первому, первый ставит её окну перед активацией.

``PyQt5.QtX11Extras`` в Astra не установлен, поэтому всё делается на Xlib.
Каждая функция безопасна: без Xlib, без ``DISPLAY`` или при любой ошибке
возвращает нейтральный результат, а не исключение.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

TIMESTAMP_ATOM = "_ASTRA_VOICE_TIMESTAMP"
USER_TIME_ATOM = "_NET_WM_USER_TIME"
_WAIT_S = 1.0
_POLL_S = 0.005


def server_timestamp() -> int:
    """Текущее время X-сервера в миллисекундах или 0.

    Штатный приём: нулевая правка свойства собственного окна рождает
    ``PropertyNotify``, в котором сервер проставляет своё время.
    """
    try:
        from Xlib import X, Xatom
        from Xlib import display as xdisplay
    except Exception:  # noqa: BLE001 — python3-xlib может отсутствовать
        return 0
    conn = None
    try:
        conn = xdisplay.Display()
        conn.set_error_handler(lambda *_: None)
        window = conn.screen().root.create_window(
            0, 0, 1, 1, 0, X.CopyFromParent, event_mask=X.PropertyChangeMask
        )
        atom = conn.intern_atom(TIMESTAMP_ATOM)
        window.change_property(atom, Xatom.STRING, 8, b"")
        conn.flush()
        deadline = time.monotonic() + _WAIT_S
        while time.monotonic() < deadline:
            for _ in range(conn.pending_events()):
                event = conn.next_event()
                if event.type == X.PropertyNotify and event.window.id == window.id:
                    window.destroy()
                    return int(event.time)
            time.sleep(_POLL_S)
        window.destroy()
        return 0
    except Exception as exc:  # noqa: BLE001 — X может быть недоступен
        log.debug("не удалось получить метку времени X: %s", exc)
        return 0
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass


def set_user_time(window_id: int, timestamp: int) -> bool:
    """Ставит окну ``_NET_WM_USER_TIME``. ``True``, если получилось."""
    if not window_id or timestamp <= 0:
        return False
    try:
        from Xlib import Xatom
        from Xlib import display as xdisplay
    except Exception:  # noqa: BLE001
        return False
    conn = None
    try:
        conn = xdisplay.Display()
        # Асинхронные ошибки X (окно могло исчезнуть) не должны сорить в stderr:
        # gate CI требует пустой stderr приложения.
        conn.set_error_handler(lambda *_: None)
        window = conn.create_resource_object("window", int(window_id))
        atom = conn.intern_atom(USER_TIME_ATOM)
        window.change_property(atom, Xatom.CARDINAL, 32, [int(timestamp)])
        conn.sync()
        return True
    except Exception as exc:  # noqa: BLE001
        log.debug("не удалось выставить %s: %s", USER_TIME_ATOM, exc)
        return False
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
