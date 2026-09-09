"""Точка входа GUI: single-instance, порядок старта, загрузка QML.

Порядок (синтез-план §5.5): блокировка → политика → настройки → тема → QML.
``QLockFile`` берётся **первым действием** (решение О5); проигравший процесс
отправляет строку ``show`` по ``QLocalSocket`` и выходит с кодом 0.
Сервер принимает единственную команду ``show`` (требование У17).
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path
from typing import Any

from astra_voice.core import policy as policy_mod
from astra_voice.core import settings as settings_mod
from astra_voice.core.logging import setup_logging
from astra_voice.core.paths import ipc_socket_path, lock_path, qml_dir
from astra_voice.core.version import __version__
from astra_voice.platform.session import SessionKind, detect

log = logging.getLogger(__name__)

APP_NAME = "astra-voice"
ORGANIZATION_DOMAIN = "io.github.arrivarus"
DESKTOP_FILE_NAME = "astra-voice"
SHOW_COMMAND = b"show"
CONNECT_TIMEOUT_MS = 2000
LOCK_TIMEOUT_MS = 100


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog=APP_NAME, add_help=True)
    parser.add_argument("--version", action="store_true", help="напечатать версию и выйти")
    parser.add_argument("--hidden", action="store_true", help="запуск без окна, только в трее")
    parser.add_argument("--show", action="store_true", help="показать окно работающей копии")
    parser.add_argument("--debug", action="store_true", help="подробный журнал")
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def _send_show() -> int:
    """Просит уже работающую копию показать окно. Всегда возвращает 0."""
    from PyQt5.QtCore import QCoreApplication
    from PyQt5.QtNetwork import QLocalSocket

    if QCoreApplication.instance() is None:
        QCoreApplication([APP_NAME])
    socket = QLocalSocket()
    socket.connectToServer(str(ipc_socket_path()))
    if not socket.waitForConnected(CONNECT_TIMEOUT_MS):
        sys.stderr.write("astra-voice уже запущен, но не отвечает на сокете\n")
        return 0
    socket.write(SHOW_COMMAND + b"\n")
    socket.flush()
    socket.waitForBytesWritten(CONNECT_TIMEOUT_MS)
    socket.disconnectFromServer()
    return 0


class ShowServer:
    """``QLocalServer``, принимающий единственную команду ``show``."""

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
        line = bytes(connection.readLine()).strip()
        if line == SHOW_COMMAND:
            log.info("получена команда show")
            self._on_show()
        else:
            log.warning("отброшена неизвестная команда на сокете (%d байт)", len(line))
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


def _load_qml(app_info: Any) -> Any | None:
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
    engine.rootContext().setContextProperty("appInfo", app_info)
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
    label.resize(900, 620)
    return label


def _show(target: Any) -> None:
    """Показывает окно (корневой объект QML или запасной виджет)."""
    if target is None:
        return
    root = target
    root_objects = getattr(target, "rootObjects", None)
    if callable(root_objects):
        objects = root_objects()
        if not objects:
            return
        root = objects[0]
    if hasattr(root, "setVisible"):
        root.setVisible(True)
    if hasattr(root, "raise_"):
        root.raise_()
    if hasattr(root, "requestActivate"):
        root.requestActivate()


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
    settings = policy_mod.effective(settings_mod.load(), policy)
    log.info(
        "старт: версия=%s сеанс=%s политика=%s",
        __version__,
        session_kind.value,
        policy.status.value,
    )
    if settings.hotkey_mode not in settings_mod.HOTKEY_MODES:  # защита от политики
        settings.hotkey_mode = "ptt"

    from PyQt5.QtWidgets import QApplication

    app = QApplication(sys.argv[:1])
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName("Astra Voice")
    app.setApplicationVersion(__version__)
    app.setOrganizationDomain(ORGANIZATION_DOMAIN)
    app.setDesktopFileName(DESKTOP_FILE_NAME)
    # В M1 трея ещё нет: закрытие окна должно завершать процесс.
    # M4 (трей) переключит это на False.
    app.setQuitOnLastWindowClosed(True)

    app_info = _make_app_info(session_kind, policy.status.value)
    shell = _load_qml(app_info) or _fallback_widget()
    server = ShowServer(lambda: _show(shell))
    if not args.hidden:
        _show(shell)

    try:
        return int(app.exec_())
    finally:
        server.close()
        lock.unlock()
