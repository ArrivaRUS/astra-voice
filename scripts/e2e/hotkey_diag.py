#!/usr/bin/env python3
"""Диагностика хоткея для dictate50.sh: почему нажатие не дошло до диктовки.

Работает на отдельном соединении X11 через системный python3-xlib, без
astra_voice. Печатает только имена клавиш, коды и статусы — без текста.

Команды:
  app-pid                         pid своего процесса Astra Voice по точному argv
                                  из /proc/*/cmdline (0 — найден, 1 — нет)
                                  только пакетная программа
                                  /usr/lib/astra-voice/bootstrap.py;
                                  запуск из исходников (venv) не видит
  offset                          размер журнала приложения (-1 — журнала нет)
  wait-start --offset N --timeout S
                                  ждёт «record.start отправлен» после смещения N:
                                  0 — есть, 1 — нет за S секунд, 3 — журнала нет
  hotkey-lines --offset N         строки журнала «хоткей:» после смещения N
  probe-grab СОЧЕТАНИЕ            XGrabKey на всех масках блокировок:
                                  BadAccess — захват занят (вероятно, Astra Voice),
                                  успех — потерян (проба сразу снята)
  probe-keyboard                  XGrabKeyboard на своём окне: AlreadyGrabbed —
                                  клавиатуру держит чужой клиент
  keymap СОЧЕТАНИЕ                query_keymap: зажатые модификаторы и клавиша хоткея
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

START_MARKER = "record.start отправлен"
HOTKEY_MARKER = "хоткей:"
LOG_NAME = "astra-voice.log"
# argv пакетного приложения; допускается хвост --hidden (автозагрузка).
APP_ARGV = ["/usr/bin/python3", "-I", "/usr/lib/astra-voice/bootstrap.py", "app"]
MODIFIER_NAMES = (
    "Control_L",
    "Control_R",
    "Shift_L",
    "Shift_R",
    "Alt_L",
    "Alt_R",
    "Meta_L",
    "Meta_R",
    "Super_L",
    "Super_R",
    "ISO_Level3_Shift",
)
GRAB_STATUS = {
    0: "успех — активного захвата клавиатуры ни у кого нет (проба сразу снята)",
    1: "AlreadyGrabbed — клавиатуру держит чужой клиент",
    2: "GrabInvalidTime",
    3: "GrabNotViewable — окно пробы не стало видимым",
    4: "GrabFrozen — клавиатура заморожена чужим захватом",
}


def log_path() -> Path:
    """Как ``paths.log_dir()``: ``$XDG_DATA_HOME/astra-voice/logs``.

    Если приложение запущено с другим окружением, путь задаёт ``ASTRA_VOICE_E2E_LOG``.
    """
    override = os.environ.get("ASTRA_VOICE_E2E_LOG", "").strip()
    if override:
        return Path(override)
    raw = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(raw) if raw.startswith("/") else Path.home() / ".local" / "share"
    return base / "astra-voice" / "logs" / LOG_NAME


def read_after(offset: int) -> str | None:
    """Текст журнала после смещения; после ротации — хвост .1 и весь новый файл."""
    path = log_path()
    try:
        size = path.stat().st_size
    except OSError:
        return None
    chunks: list[bytes] = []
    if size < offset:
        rotated = path.with_name(LOG_NAME + ".1")
        try:
            with rotated.open("rb") as source:
                source.seek(offset)
                chunks.append(source.read())
        except OSError:
            pass
        offset = 0
    try:
        with path.open("rb") as source:
            source.seek(offset)
            chunks.append(source.read())
    except OSError:
        return None
    return b"".join(chunks).decode("utf-8", errors="replace")


def app_pids(proc: Path = Path("/proc"), uid: int | None = None) -> list[int]:
    """Процессы текущего пользователя с argv ровно APP_ARGV (или APP_ARGV + --hidden)."""
    uid = os.getuid() if uid is None else uid
    found: list[int] = []
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            if entry.stat().st_uid != uid:
                continue
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        argv = [part.decode("utf-8", errors="replace") for part in raw.split(b"\0")]
        if argv and argv[-1] == "":
            argv.pop()
        if argv in (APP_ARGV, [*APP_ARGV, "--hidden"]):
            found.append(int(entry.name))
    return sorted(found)


def cmd_app_pid(_args: argparse.Namespace) -> int:
    pids = app_pids()
    for pid in pids:
        print(pid)
    return 0 if pids else 1


def cmd_offset(_args: argparse.Namespace) -> int:
    try:
        print(log_path().stat().st_size)
    except OSError:
        print(-1)
    return 0


def cmd_wait_start(args: argparse.Namespace) -> int:
    if args.offset < 0:
        return 3
    deadline = time.monotonic() + args.timeout
    while True:
        text = read_after(args.offset)
        if text is None:
            return 3
        if START_MARKER in text:
            return 0
        if time.monotonic() >= deadline:
            return 1
        time.sleep(0.05)


def cmd_hotkey_lines(args: argparse.Namespace) -> int:
    text = read_after(max(args.offset, 0)) or ""
    lines = [line for line in text.splitlines() if HOTKEY_MARKER in line]
    print(f"Строки журнала «{HOTKEY_MARKER}» после нажатия: {len(lines)}")
    for line in lines[-20:]:
        print(f"  {line}")
    return 0


def open_display() -> Any:
    from Xlib import display

    return display.Display()


def parse_combo(conn: Any, combo: str) -> tuple[int, int, str]:
    """xdotool-сочетание (ctrl+space) → (keycode, маска модификаторов, keysym)."""
    from Xlib import XK, X

    masks = {
        "ctrl": X.ControlMask,
        "control": X.ControlMask,
        "shift": X.ShiftMask,
        "alt": X.Mod1Mask,
        "meta": X.Mod1Mask,
        "super": X.Mod4Mask,
        "win": X.Mod4Mask,
    }
    mods = 0
    key = ""
    for part in combo.split("+"):
        if part.lower() in masks:
            mods |= masks[part.lower()]
        else:
            key = part
    keysym = XK.string_to_keysym(key)
    keycode = int(conn.keysym_to_keycode(keysym)) if keysym else 0
    if not keycode:
        raise SystemExit(f"Клавиши «{key}» нет в текущей раскладке.")
    return keycode, mods, key


def lock_masks(conn: Any) -> list[int]:
    """Маски Caps/Num/Scroll Lock по текущей модификаторной карте."""
    from Xlib import XK

    wanted = {
        XK.string_to_keysym(name) for name in ("Caps_Lock", "Shift_Lock", "Num_Lock", "Scroll_Lock")
    }
    found: dict[int, None] = {}
    for index, codes in enumerate(conn.get_modifier_mapping()):
        for code in codes:
            if code and set(conn.get_keyboard_mapping(int(code), 1)[0]) & wanted:
                found[1 << index] = None
    return list(found)


def cmd_probe_grab(args: argparse.Namespace) -> int:
    from Xlib import X, error

    conn = open_display()
    busy: list[int] = []
    free: list[int] = []
    try:
        root = conn.screen().root
        keycode, mods, key = parse_combo(conn, args.combo)
        variants = [mods]
        for lock in lock_masks(conn):
            variants = list(dict.fromkeys(variants + [value | lock for value in variants]))
        for mask in variants:
            catcher = error.CatchError(error.BadAccess)
            root.grab_key(keycode, mask, True, X.GrabModeAsync, X.GrabModeAsync, onerror=catcher)
            conn.sync()
            if catcher.get_error() is not None:
                busy.append(mask)
            else:
                # Проба не должна перехватывать клавишу: снимаем сразу.
                free.append(mask)
                root.ungrab_key(keycode, mask)
                conn.sync()
    finally:
        conn.close()
    label = f"{args.combo} (keycode={keycode}, mods={mods:#x}, клавиша {key})"
    if not free:
        print(
            f"(а) XGrabKey {label}: BadAccess на всех {len(variants)} масках — "
            "захват занят (вероятно, Astra Voice)."
        )
    elif not busy:
        print(
            f"(а) XGrabKey {label}: успех на всех {len(variants)} масках — "
            "захват ПОТЕРЯН (проба сразу снята)."
        )
    else:
        print(
            f"(а) XGrabKey {label}: захват занят (вероятно, Astra Voice) на масках "
            f"{[hex(m) for m in busy]}, "
            f"потерян на {[hex(m) for m in free]} (проба сразу снята)."
        )
    return 0


def cmd_probe_keyboard(_args: argparse.Namespace) -> int:
    from Xlib import X

    conn = open_display()
    root = conn.screen().root
    # Как X11Display.probe_window: InputOnly 1×1 вне экрана, без оконного менеджера.
    window = root.create_window(
        -1,
        -1,
        1,
        1,
        0,
        0,
        window_class=X.InputOnly,
        visual=X.CopyFromParent,
        override_redirect=True,
    )
    window.map()
    conn.sync()
    try:
        status = int(window.grab_keyboard(False, X.GrabModeAsync, X.GrabModeAsync, X.CurrentTime))
        if status == X.GrabSuccess:
            conn.ungrab_keyboard(X.CurrentTime)
            conn.sync()
        print(f"(б) XGrabKeyboard после отпускания: {GRAB_STATUS.get(status, status)}.")
    finally:
        window.destroy()
        conn.sync()
        conn.close()
    return 0


def cmd_keymap(args: argparse.Namespace) -> int:
    from Xlib import XK

    conn = open_display()
    try:
        keycode, _mods, key = parse_combo(conn, args.combo)
        names = {int(conn.keysym_to_keycode(XK.string_to_keysym(n))): n for n in MODIFIER_NAMES}
        names.pop(0, None)
        names[keycode] = key
        keymap = conn.query_keymap()
    finally:
        conn.close()
    held = [code for code in range(8, 256) if keymap[code // 8] & (1 << (code % 8))]
    known = sorted({names[code] for code in held if code in names})
    others = len([code for code in held if code not in names])
    if known:
        print(f"(в) query_keymap: зажаты {', '.join(known)}; прочих клавиш: {others}.")
    else:
        print(f"(в) query_keymap: залипших модификаторов и {key} нет; прочих клавиш: {others}.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser(
        "app-pid",
        help=(
            "ищет только пакетную программу /usr/lib/astra-voice/bootstrap.py; "
            "запуск из исходников (venv) не видит"
        ),
    ).set_defaults(run=cmd_app_pid)
    commands.add_parser("offset").set_defaults(run=cmd_offset)
    wait = commands.add_parser("wait-start")
    wait.add_argument("--offset", type=int, required=True)
    wait.add_argument("--timeout", type=float, required=True)
    wait.set_defaults(run=cmd_wait_start)
    lines = commands.add_parser("hotkey-lines")
    lines.add_argument("--offset", type=int, required=True)
    lines.set_defaults(run=cmd_hotkey_lines)
    for name, run in (("probe-grab", cmd_probe_grab), ("keymap", cmd_keymap)):
        command = commands.add_parser(name)
        command.add_argument("combo")
        command.set_defaults(run=run)
    commands.add_parser("probe-keyboard").set_defaults(run=cmd_probe_keyboard)
    args = parser.parse_args(argv)
    try:
        return int(args.run(args))
    except Exception as exc:  # диагностика не должна ронять прогон трассировкой
        print(f"Диагностика {args.command} не удалась: {type(exc).__name__}", file=sys.stderr)
        return 4


if __name__ == "__main__":
    sys.exit(main())
