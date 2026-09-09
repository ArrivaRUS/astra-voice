#!/usr/bin/env python3
"""Закрывает окно так, как это делает пользователь кнопкой «×»: посылает
менеджеру окон EWMH-сообщение _NET_CLOSE_WINDOW, а тот шлёт клиенту
WM_DELETE_WINDOW. `xdotool windowclose` так НЕ делает — он уничтожает окно
(XDestroyWindow), и приложение о закрытии не узнаёт.

    python3 close_window.py <window-id>
"""

import sys

from Xlib import X, display as xdisplay
from Xlib.protocol import event


def main() -> int:
    wid = int(sys.argv[1], 0)
    d = xdisplay.Display()
    root = d.screen().root
    win = d.create_resource_object("window", wid)
    protocols = win.get_full_property(d.intern_atom("WM_PROTOCOLS"), X.AnyPropertyType)
    names = [d.get_atom_name(a) for a in (protocols.value if protocols else [])]
    print("WM_PROTOCOLS:", ", ".join(names) or "нет")
    msg = event.ClientMessage(
        window=win,
        client_type=d.intern_atom("_NET_CLOSE_WINDOW"),
        data=(32, [X.CurrentTime, 2, 0, 0, 0]),  # 2 = источник «прочее приложение»
    )
    root.send_event(msg, event_mask=X.SubstructureRedirectMask | X.SubstructureNotifyMask)
    d.sync()
    print("_NET_CLOSE_WINDOW отправлен окну", hex(wid))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
