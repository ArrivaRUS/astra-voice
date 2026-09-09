"""Точка входа GUI: single-instance, порядок старта, загрузка QML.

Порядок (синтез-план §5.5): блокировка → политика → настройки → **тема** → QML.
``QLockFile`` берётся **первым действием** (решение О5); проигравший процесс
отправляет строку ``show`` по ``QLocalSocket`` и выходит с кодом 0.
Сервер принимает единственную команду ``show`` — с необязательной меткой
времени X: ``show`` либо ``show <ts>`` (требование У17, всё иное — отбой).
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
from pathlib import Path
from typing import Any

from astra_voice.core import policy as policy_mod
from astra_voice.core import settings as settings_mod
from astra_voice.core.logging import setup_logging
from astra_voice.core.paths import ipc_socket_path, lock_path, qml_dir, settings_path
from astra_voice.core.version import __version__
from astra_voice.platform.session import SessionKind, detect

log = logging.getLogger(__name__)

APP_NAME = "astra-voice"
ORGANIZATION_DOMAIN = "io.github.arrivarus"
DESKTOP_FILE_NAME = "astra-voice"
SHOW_COMMAND = b"show"
CONNECT_TIMEOUT_MS = 2000
LOCK_TIMEOUT_MS = 100
SIGNAL_POLL_MS = 200

# Продуктово окно настроек — окно трей-приложения: закрытие прячет его, процесс
# продолжает работать (хоткей и запись живут в трее). Трей появляется в M4, а до
# него скрытое окно превратилось бы в ловушку: процесс-призрак ловит `show`, и
# приложение перестаёт открываться. Поэтому в M1 закрытие завершает процесс.
CLOSE_TO_TRAY = False  # M4: True


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=APP_NAME, add_help=True)
    parser.add_argument("--version", action="store_true", help="напечатать версию и выйти")
    parser.add_argument("--hidden", action="store_true", help="запуск без окна, только в трее")
    parser.add_argument("--show", action="store_true", help="показать окно работающей копии")
    parser.add_argument("--debug", action="store_true", help="подробный журнал")
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def parse_command(line: bytes) -> int | None:
    """Разбирает строку протокола.

    ``show`` → 0, ``show <ts>`` → метка времени, всё остальное → ``None``
    (соединение закрывается). Метка времени — целое без знака, лишние поля
    и мусор недопустимы.
    """
    parts = line.strip().split()
    if not parts or parts[0] != SHOW_COMMAND:
        return None
    if len(parts) == 1:
        return 0
    if len(parts) > 2:
        return None
    try:
        timestamp = int(parts[1])
    except ValueError:
        return None
    return timestamp if timestamp >= 0 else None


def _send_show() -> int:
    """Просит уже работающую копию показать окно. Всегда возвращает 0."""
    from PyQt5.QtCore import QCoreApplication
    from PyQt5.QtNetwork import QLocalSocket

    from astra_voice.platform.x11 import server_timestamp

    if QCoreApplication.instance() is None:
        QCoreApplication([APP_NAME])
    socket = QLocalSocket()
    socket.connectToServer(str(ipc_socket_path()))
    if not socket.waitForConnected(CONNECT_TIMEOUT_MS):
        sys.stderr.write("astra-voice уже запущен, но не отвечает на сокете\n")
        return 0
    timestamp = server_timestamp()
    payload = SHOW_COMMAND if timestamp <= 0 else SHOW_COMMAND + b" " + str(timestamp).encode()
    socket.write(payload + b"\n")
    socket.flush()
    socket.waitForBytesWritten(CONNECT_TIMEOUT_MS)
    socket.disconnectFromServer()
    return 0


class ShowServer:
    """``QLocalServer``, принимающий единственную команду ``show [<ts>]``."""

    def __init__(self, on_show: Any) -> None:
        from PyQt5.QtNetwork import QLocalServer

        self._on_show = on_show
        self._connections: list[Any] = []
        path = str(ipc_socket_path())
        QLocalServer.removeServer(path)
        self._server = QLocalServer()
        self._server.setSocketOptions(QLocalServer.UserAccessOption)
        if not self._server.listen(path):
            log.warning("не удалось открыть сокет %s: %s", path, self._server.errorString())
        self._server.newConnection.connect(self._on_new_connection)

    def _on_new_connection(self) -> None:
        connection = self._server.nextPendingConnection()
        if connection is None:
            return
        self._connections.append(connection)
        connection.readyRead.connect(lambda: self._on_ready_read(connection))
        connection.disconnected.connect(lambda: self._drop(connection))

    def _drop(self, connection: Any) -> None:
        if connection in self._connections:
            self._connections.remove(connection)
        connection.deleteLater()

    def _on_ready_read(self, connection: Any) -> None:
        line = bytes(connection.readLine())
        timestamp = parse_command(line)
        if timestamp is None:
            log.warning("отброшена неизвестная команда на сокете (%d байт)", len(line.strip()))
        else:
            log.info("получена команда show (метка времени X: %s)", timestamp or "нет")
            self._on_show(timestamp)
        connection.disconnectFromServer()
        connection.close()

    def close(self) -> None:
        self._server.close()


def _make_app_info(session_kind: SessionKind, policy_status: str) -> Any:
    from PyQt5.QtCore import QObject, pyqtProperty

    class AppInfo(QObject):
        """Данные приложения для QML (версия, сеанс, статус политики)."""

        @pyqtProperty(str, constant=True)
        def version(self) -> str:
            return __version__

        @pyqtProperty(str, constant=True)
        def sessionKind(self) -> str:  # noqa: N802 — имя свойства для QML
            return session_kind.value

        @pyqtProperty(str, constant=True)
        def policyStatus(self) -> str:  # noqa: N802 — имя свойства для QML
            return policy_status

    return AppInfo()


def make_theme_source(session_kind: SessionKind) -> Any:
    """Источник темы под вид сеанса: Fly — `paletterc`, остальные — `kdeglobals`."""
    from astra_voice.core.theme import FlyThemeSource, KdeThemeSource

    if session_kind is SessionKind.FLY:
        return FlyThemeSource()
    return KdeThemeSource()


def _make_theme_bridge(session_kind: SessionKind) -> Any | None:
    """Мост темы для QML или ``None``, если тему собрать не удалось."""
    try:
        from astra_voice.ui.theme_bridge import ThemeBridge

        source = make_theme_source(session_kind)
        bridge = ThemeBridge(source)
    except Exception as exc:  # noqa: BLE001 — без темы окно всё равно должно открыться
        log.warning("источник темы недоступен (%s), QML возьмёт светлую тему", exc)
        return None
    log.info("тема сессии: dark=%s accent=%s", source.dark, source.accent)
    return bridge


def _load_qml(app_info: Any, theme_bridge: Any | None) -> Any | None:
    """Загружает ``qml/Main.qml``; ``None``, если QML недоступен."""
    main_qml = qml_dir() / "Main.qml"
    if not main_qml.is_file():
        log.warning("%s не найден, показываю заглушку", main_qml)
        return None
    try:
        from PyQt5.QtCore import QUrl
        from PyQt5.QtQml import QQmlApplicationEngine
    except ImportError as exc:
        log.warning("модуль QML недоступен (%s), показываю заглушку", exc)
        return None
    engine = QQmlApplicationEngine()
    context = engine.rootContext()
    context.setContextProperty("appInfo", app_info)
    # themeSource обязан быть виден ДО load(): Theme.qml читает его в биндинге
    # `dark` при создании корневого объекта. Theme.qml — `pragma Singleton`, а
    # синглтоны не видят контекстных свойств, поэтому объект кладётся ещё и в
    # глобальный объект JS — оттуда его достаёт любой контекст, включая синглтон.
    context.setContextProperty("themeSource", theme_bridge)
    if theme_bridge is not None:
        from PyQt5.QtQml import QQmlEngine

        QQmlEngine.setObjectOwnership(theme_bridge, QQmlEngine.CppOwnership)
        engine.globalObject().setProperty("themeSource", engine.newQObject(theme_bridge))
    engine.load(QUrl.fromLocalFile(str(main_qml)))
    if not engine.rootObjects():
        log.error("QML не загрузился, показываю заглушку")
        return None
    return engine


def _fallback_widget() -> Any:
    from PyQt5.QtCore import Qt
    from PyQt5.QtWidgets import QLabel

    label = QLabel(f"Astra Voice {__version__} — QML недоступен")
    label.setAlignment(Qt.AlignCenter)
    label.setWindowTitle("Astra Voice")
    label.resize(900, 588)
    return label


def _root_window(target: Any) -> Any:
    """Корневое окно: объект QML из движка или сам виджет-заглушка."""
    if target is None:
        return None
    root_objects = getattr(target, "rootObjects", None)
    if callable(root_objects):
        objects = root_objects()
        return objects[0] if objects else None
    return target


def _show(target: Any, timestamp: int = 0) -> None:
    """Показывает окно: разворачивает из свёрнутого, поднимает, активирует.

    ``setVisible(True)`` не снимает состояние «свёрнуто», поэтому нужен
    ``showNormal()``. Метка времени X (если пришла от второго экземпляра)
    ставится окну до активации — иначе KWin отделается «требует внимания».
    """
    root = _root_window(target)
    if root is None:
        return
    # Сначала развернуть: у свёрнутого окна нативный идентификатор может быть
    # уже недействителен, и правка свойства дала бы BadWindow.
    if hasattr(root, "showNormal"):
        root.showNormal()
    elif hasattr(root, "setVisible"):
        root.setVisible(True)
    if timestamp > 0:
        window_id = 0
        try:
            window_id = int(root.winId())
        except Exception:  # noqa: BLE001 — у окна может не быть winId
            window_id = 0
        if window_id:
            from astra_voice.platform.x11 import set_user_time

            set_user_time(window_id, timestamp)
    if hasattr(root, "raise_"):
        root.raise_()
    if hasattr(root, "requestActivate"):
        root.requestActivate()
    elif hasattr(root, "activateWindow"):
        root.activateWindow()


def _wire_close(app: Any, shell: Any) -> Any | None:
    """Закрытие окна завершает процесс, пока нет трея (см. ``CLOSE_TO_TRAY``).

    ``quitOnLastWindowClosed`` считает только окна-виджеты, поэтому закрытие
    окна QML само по себе процесс не завершает. Сигнал ``QQuickWindow.closing``
    в PyQt5 недоступен (тип аргумента `QQuickCloseEvent*` не поддержан), так что
    ловим событие закрытия фильтром — он одинаково работает и для заглушки.
    """
    if CLOSE_TO_TRAY:
        return None
    root = _root_window(shell)
    if root is None:
        return None

    from PyQt5.QtCore import QEvent, QObject

    class CloseWatcher(QObject):
        def eventFilter(self, obj: Any, event: Any) -> bool:  # noqa: N802 — метод Qt
            if event.type() == QEvent.Close:
                log.info("окно закрыто — завершаю процесс (CLOSE_TO_TRAY=False)")
                app.quit()
            return False

    watcher = CloseWatcher(root)
    root.installEventFilter(watcher)
    return watcher


def _install_signal_handlers(app: Any) -> Any:
    """SIGTERM/SIGINT завершают цикл событий, чтобы отработала уборка в ``finally``.

    Обработчики Python выполняются только между байт-кодами, а Qt держит поток в
    C++-цикле: пробуждаем интерпретатор таймером.
    """
    from PyQt5.QtCore import QTimer

    def handler(signum: int, _frame: Any) -> None:
        log.info("получен сигнал %s, завершаюсь", signal.Signals(signum).name)
        app.quit()

    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(signum, handler)
        except (OSError, ValueError):  # noqa: PERF203 — не главный поток
            log.debug("обработчик сигнала %s не установлен", signum)
    timer = QTimer()
    timer.start(SIGNAL_POLL_MS)
    timer.timeout.connect(lambda: None)
    return timer


def _ensure_settings_file(settings: settings_mod.Settings) -> None:
    """Первый запуск: кладём на диск значения по умолчанию (0600)."""
    path = settings_path()
    if path.exists():
        return
    try:
        settings_mod.save(settings, path)
    except OSError as exc:  # noqa: BLE001 — без файла настроек работать можно
        log.warning("не удалось создать %s: %s", path, exc)
    else:
        log.info("создан %s со значениями по умолчанию", path)


def _cleanup(server: Any, lock: Any) -> None:
    """Убирает сокет и снимает блокировку. Вызывается ровно один раз."""
    try:
        if server is not None:
            server.close()
    finally:
        ipc = ipc_socket_path()
        try:
            ipc.unlink(missing_ok=True)
        except OSError as exc:  # noqa: BLE001
            log.debug("не удалось убрать %s: %s", ipc, exc)
        lock.unlock()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.version:
        sys.stdout.write(f"{APP_NAME} {__version__}\n")
        return 0

    from PyQt5.QtCore import QLockFile

    lock_file = Path(lock_path())
    lock = QLockFile(str(lock_file))
    if not lock.tryLock(LOCK_TIMEOUT_MS):
        return _send_show()

    session_kind = detect()
    setup_logging(session_kind.value, debug=args.debug)
    policy = policy_mod.load()
    stored = settings_mod.load()
    settings = policy_mod.effective(stored, policy)
    log.info(
        "старт: версия=%s сеанс=%s политика=%s",
        __version__,
        session_kind.value,
        policy.status.value,
    )
    if settings.hotkey_mode not in settings_mod.HOTKEY_MODES:  # защита от политики
        settings.hotkey_mode = "ptt"
    # На диск кладутся настройки пользователя, а не результат наложения политики.
    _ensure_settings_file(stored)

    from PyQt5.QtWidgets import QApplication

    # argv[0] определяет instance-часть WM_CLASS: должно быть «astra-voice»,
    # а не имя скрипта («bootstrap.py»).
    app = QApplication([APP_NAME])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName("Astra Voice")
    app.setApplicationVersion(__version__)
    app.setOrganizationDomain(ORGANIZATION_DOMAIN)
    app.setDesktopFileName(DESKTOP_FILE_NAME)
    app.setQuitOnLastWindowClosed(not CLOSE_TO_TRAY)

    app_info = _make_app_info(session_kind, policy.status.value)
    theme_bridge = _make_theme_bridge(session_kind)
    shell = _load_qml(app_info, theme_bridge) or _fallback_widget()
    if theme_bridge is not None:
        theme_bridge.source.start()  # слежение за темой — после загрузки QML

    close_watcher = _wire_close(app, shell)  # держим ссылку на фильтр
    server = ShowServer(lambda timestamp: _show(shell, timestamp))
    timer = _install_signal_handlers(app)
    if not args.hidden:
        _show(shell)

    try:
        return int(app.exec_())
    finally:
        del close_watcher
        timer.stop()
        if theme_bridge is not None:
            theme_bridge.source.stop()
        _cleanup(server, lock)
