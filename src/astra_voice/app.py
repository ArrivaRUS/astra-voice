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
import os
import signal
import sys
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from astra_voice.core import paths
from astra_voice.core import policy as policy_mod
from astra_voice.core import settings as settings_mod
from astra_voice.core.logging import setup_logging
from astra_voice.core.model_request import build_model_load
from astra_voice.core.paths import ipc_socket_path, lock_path, qml_dir, settings_path
from astra_voice.core.version import __version__
from astra_voice.models.store import ModelStore, StoreError
from astra_voice.platform.session import SessionKind, detect

if TYPE_CHECKING:
    from astra_voice.core.dictation import LevelCallback, TestCallback
    from astra_voice.runtime import DictationRuntime

log = logging.getLogger(__name__)

APP_NAME = "astra-voice"
ORGANIZATION_DOMAIN = "io.github.arrivarus"
DESKTOP_FILE_NAME = "astra-voice"
SHOW_COMMAND = b"show"
CONNECT_TIMEOUT_MS = 2000
LOCK_TIMEOUT_MS = 100
SIGNAL_POLL_MS = 200

# Программный рендер (bootstrap ставит QT_QUICK_BACKEND=software и
# QT_XCB_GL_INTEGRATION=none ради 65 МБ RSS) заставляет Qt дважды пожаловаться на
# отсутствие GL. Это ожидаемое следствие нашего же выбора, а не проблема —
# обе строки уходят в журнал на уровне debug и не сорят в stderr.
QT_EXPECTED_MESSAGES = (
    "Cannot create platform OpenGL context",
    "fallback to QtQuick software backend",
)
_qt_message_handler = None  # ссылку держим сами: Qt хранит только указатель

# При доступном трее закрытие окна прячет его, процесс продолжает жить в трее
# (хоткей и диктовка остаются доступны). Без трея закрытие завершает процесс.
CLOSE_TO_TRAY = True


def _install_qt_message_handler() -> None:
    """Уводит сообщения Qt из stderr в наш журнал (фильтр `text` действует и на них)."""
    global _qt_message_handler
    from PyQt5.QtCore import QtMsgType, qInstallMessageHandler

    qt_log = logging.getLogger("qt")
    levels = {
        QtMsgType.QtDebugMsg: logging.DEBUG,
        QtMsgType.QtInfoMsg: logging.INFO,
        QtMsgType.QtWarningMsg: logging.WARNING,
        QtMsgType.QtCriticalMsg: logging.ERROR,
        QtMsgType.QtFatalMsg: logging.CRITICAL,
    }

    def handler(mode: Any, context: Any, message: str) -> None:
        text = str(message).strip()
        if not text:
            return
        if any(expected in text for expected in QT_EXPECTED_MESSAGES):
            qt_log.debug("%s (ожидаемо при программном рендере)", text)
            return
        qt_log.log(levels.get(mode, logging.INFO), "%s", text)

    _qt_message_handler = handler
    qInstallMessageHandler(handler)


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    def threads_count(value: str) -> int:
        """Отклоняет неверное число потоков с понятным пояснением."""
        try:
            count = int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(
                "число потоков должно быть целым и не меньше 1"
            ) from None
        if count < 1:
            raise argparse.ArgumentTypeError("число потоков должно быть целым и не меньше 1")
        return count

    parser = argparse.ArgumentParser(prog=APP_NAME, add_help=True)
    parser.add_argument("--version", action="store_true", help="напечатать версию и выйти")
    parser.add_argument("--stats", action="store_true", help="напечатать статистику и выйти")
    parser.add_argument("--hidden", action="store_true", help="запуск без окна, только в трее")
    parser.add_argument("--show", action="store_true", help="показать окно работающей копии")
    parser.add_argument("--debug", action="store_true", help="подробный журнал")
    parser.add_argument(
        "--debug-transcribe",
        metavar="WAV",
        help="расшифровать файл и выйти; модель из настроек или --model-dir",
    )
    parser.add_argument(
        "--model-dir",
        metavar="PATH",
        help="каталог ревизии модели для расшифровки, вместо настройки",
    )
    parser.add_argument(
        "--variant", metavar="NAME", help="вариант модели для расшифровки, вместо настройки"
    )
    parser.add_argument(
        "--threads",
        metavar="N",
        type=threads_count,
        help="число потоков инференса: целое от 1, вместо настройки (по умолчанию 2)",
    )
    return parser.parse_args(sys.argv[1:] if argv is None else argv)


def _debug_model_request(
    settings: dict[str, Any], args: argparse.Namespace, store_dir: Path
) -> dict[str, Any]:
    """Передаёт аргументы отладочного запуска построителю запроса."""
    return build_model_load(
        settings,
        model_dir=args.model_dir,
        variant=args.variant,
        threads=args.threads,
        store_dir=store_dir,
    )


def _debug_transcribe(path: str, args: argparse.Namespace) -> int:
    """Расшифровывает WAV без окна; текст передаёт исключительно в stdout."""
    from PyQt5.QtCore import QCoreApplication

    from astra_voice.worker import ipc
    from astra_voice.worker.supervisor import Message, WorkerSupervisor

    app = QCoreApplication.instance() or QCoreApplication([APP_NAME])
    events: deque[Message] = deque()
    supervisor = WorkerSupervisor(on_event=events.append, use_qt=False)

    def fail(code: int, message: str) -> int:
        """Показывает безопасное пояснение без содержимого ответа воркера."""
        sys.stderr.write(message + "\n")
        return code

    def wait_for(kind: str, timeout: float) -> Message:
        """Ждёт нужное событие или ошибку, ограничивая также запуск воркера."""
        deadline = time.monotonic() + timeout
        while True:
            while events:
                event = events.popleft()
                if event["type"] in (kind, "error", "cancelled"):
                    return event
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return ipc.error("timeout", "Истекло время ожидания.")
            supervisor.pump(min(remaining, 0.05))
            app.processEvents()

    try:
        setup_logging(debug=args.debug)
        wav = Path(path).expanduser().resolve()
        if not wav.is_file():
            return fail(1, "Звуковой файл не найден или недоступен.")
        settings = policy_mod.effective(settings_mod.load(), policy_mod.load()).to_dict()

        try:
            request = _debug_model_request(settings, args, paths.model_store_dir())
        except ValueError:
            return fail(
                2, "Модель не настроена. Укажите --model-dir или модель и ревизию в настройках."
            )
        request["dir"] = str(Path(request["dir"]).expanduser().resolve())
        try:
            ipc.encode(request)
            if request["threads"] < 1 or request["min_ram_mb"] < 1:
                raise ValueError
        except (ipc.FrameError, ValueError):
            return fail(2, "Параметры модели заданы неверно. Проверьте настройки модели.")

        supervisor.start()
        for kind, command, timeout in (
            ("hello", None, 10.0),
            ("model.loaded", request, 10.0),
            ("result", {"type": "transcribe.file", "path": str(wav)}, 120.0),
        ):
            if command is not None:
                supervisor.send(command, timeout=timeout)
            event = wait_for(kind, timeout)
            if event["type"] != kind:
                # Не журналируем ответ целиком: даже ошибка может содержать диктовку.
                log.debug("Отладочная расшифровка: ожидание %s завершилось ошибкой", kind)
                if event.get("code") in ("timeout", "load-timeout"):
                    return fail(1, "Время ожидания распознавания истекло. Попробуйте ещё раз.")
                if kind == "model.loaded" and event.get("code") not in (
                    "worker-start",
                    "worker-crashed",
                    "restart-limit",
                ):
                    return fail(2, "Не удалось загрузить модель. Проверьте её файлы и настройки.")
                if event.get("code") in ("engine-unavailable", "no-model"):
                    return fail(2, "Движок распознавания недоступен. Проверьте установку модели.")
                return fail(1, "Не удалось расшифровать файл. Проверьте запись и попробуйте снова.")
            if kind == "hello" and event["runtime"]["onnxruntime"] is None:
                return fail(2, "Движок распознавания не установлен или недоступен.")
            if kind == "result":
                sys.stdout.write(f"{event['text']}\nt_ms={int(event['t_ms'])}\n")
        return 0
    except (TypeError, ValueError, ipc.FrameError):
        return fail(2, "Параметры модели заданы неверно. Проверьте настройки модели.")
    except (OSError, RuntimeError):
        return fail(1, "Не удалось выполнить расшифровку. Проверьте доступ к файлам.")
    finally:
        supervisor.stop()


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


# Ключи разделов — общий контракт Python → QML для showSection(QString).
SECTION_GENERAL = "general"
SECTION_MODELS = "models"
SECTION_OUTPUT = "output"
SECTION_NETWORK = "network"
SECTION_ADVANCED = "advanced"
SECTION_ABOUT = "about"
SECTION_DEBUG = "debug"


def _make_app_info(session_kind: SessionKind, policy_status: str, *, debug: bool = False) -> Any:
    from PyQt5.QtCore import QObject, pyqtProperty, pyqtSignal

    class AppInfo(QObject):
        """Данные приложения и запросы перехода между разделами QML."""

        debugChanged = pyqtSignal()
        showSection = pyqtSignal(str, arguments=["section"])

        def __init__(self) -> None:
            super().__init__()
            self._debug = debug or os.environ.get("ASTRA_VOICE_DEBUG", "").strip().lower() in (
                "1",
                "true",
                "yes",
            )

        @pyqtProperty(bool, notify=debugChanged)
        def debug(self) -> bool:
            return self._debug

        def show_section(self, name: str) -> None:
            """Жест пользователя открывает отладку до запроса перехода в неё."""
            if name == SECTION_DEBUG and not self._debug:
                self._debug = True
                self.debugChanged.emit()
            self.showSection.emit(name)

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
    context.setContextProperty("showOnboarding", False)
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


def _set_context_property(shell: Any, name: str, obj: Any) -> None:
    """Добавляет объект в контекст QML; для виджета-заглушки ничего не делает."""
    root_context = getattr(shell, "rootContext", None)
    if callable(root_context):
        root_context().setContextProperty(name, obj)


class _RuntimeSettingsApply:
    """Адаптирует apply_*; runtime.hotkey уже занят менеджером клавиш."""

    def __init__(self, runtime: DictationRuntime) -> None:
        self._runtime = runtime

    def pill_enabled(self, value: bool) -> None:
        self._runtime.apply_pill_enabled(value)

    def hotkey(self, combo: str, mode: str) -> str:
        return self._runtime.apply_hotkey(combo, mode)

    def device(self, value: str | None) -> None:
        self._runtime.apply_device(value)


class _RuntimeOnboardingHost:
    """Живой пробник и действия окна для мастера первого запуска."""

    def __init__(self, runtime: DictationRuntime, shell: Any) -> None:
        self._runtime = runtime
        self._shell = shell

    def begin_capture(self) -> bool:
        return self._runtime.begin_hotkey_capture()

    def end_capture(self) -> None:
        self._runtime.end_hotkey_capture()

    def probe(self, combo: str) -> str:
        return self._runtime.hotkey.probe(combo).code

    def free_candidates(self, prefer: list[str]) -> list[str]:
        return self._runtime.hotkey.free_candidates(prefer)

    def apply_hotkey(self, combo: str, mode: str) -> str:
        return self._runtime.apply_hotkey(combo, mode)

    def start_level_monitor(self, device: str, callback: LevelCallback) -> bool:
        return self._runtime.start_level_monitor(device, callback)

    def stop_level_monitor(self) -> None:
        self._runtime.stop_level_monitor()

    def start_test(self, device: str, callback: TestCallback) -> bool:
        return self._runtime.start_test(device, callback)

    def subscribe_device_resolved(self, callback: Callable[[str], None] | None) -> str:
        return self._runtime.subscribe_device_resolved(callback)

    def stop_test(self) -> None:
        self._runtime.stop_test()

    def cancel_test(self) -> None:
        self._runtime.cancel_test()

    def reload_model(self) -> None:
        self._runtime.reload_model()

    def notify_ready(self, combo: str) -> None:
        from astra_voice.ui import notify

        notify.notify_onboarding_ready(combo)

    def hide_window(self) -> None:
        root = _root_window(self._shell)
        if root is not None:
            root.hide()


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


def _wire_close(
    app: Any, shell: Any, is_tray_ready: Callable[[], bool] | None = None
) -> Any | None:
    """Закрытие прячет окно в трей либо завершает процесс (см. ``CLOSE_TO_TRAY``).

    ``quitOnLastWindowClosed`` считает только окна-виджеты, поэтому закрытие
    окна QML само по себе процесс не завершает. Сигнал ``QQuickWindow.closing``
    в PyQt5 недоступен (тип аргумента `QQuickCloseEvent*` не поддержан), так что
    ловим событие закрытия фильтром — он одинаково работает и для заглушки.
    """
    root = _root_window(shell)
    if root is None:
        return None

    from PyQt5.QtCore import QEvent, QObject

    class CloseWatcher(QObject):
        def eventFilter(self, obj: Any, event: Any) -> bool:  # noqa: N802 — метод Qt
            if event.type() == QEvent.Close:
                if CLOSE_TO_TRAY:
                    try:
                        tray_ready = is_tray_ready is not None and is_tray_ready()
                    except Exception:  # noqa: BLE001 — ошибка проверки не должна скрыть окно
                        tray_ready = False
                    if tray_ready:
                        event.ignore()
                        root.hide()
                        return True
                    log.warning("Трей недоступен — закрытие окна завершает приложение")
                else:
                    log.info("Окно закрыто — завершаю приложение")
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
    if args.stats:
        from astra_voice.core.stats import Stats

        stats = Stats()
        if not stats.events():
            sys.stdout.write("Пока нет данных.\n")
            return 0
        summary = stats.summary()
        sys.stdout.write(f"Диктовок: {summary['dictations']}\n")
        p50, p95 = summary["p50_ms"], summary["p95_ms"]
        if p50 is None or p95 is None:
            sys.stdout.write("Скорость: пока нет данных.\n")
        else:
            sys.stdout.write(f"Скорость: обычно {p50:.0f} мс, в худших случаях {p95:.0f} мс\n")
        results = summary["results"]
        sys.stdout.write(
            f"Успешно: {results['ok']}, пусто: {results['empty']}, "
            f"отменено: {results['cancelled']}\n"
            f"Ошибок микрофона: {summary['mic_errors']}\n"
        )
        return 0
    if args.debug_transcribe is not None:
        return _debug_transcribe(args.debug_transcribe, args)

    from PyQt5.QtCore import QLockFile

    lock_file = Path(lock_path())
    lock = QLockFile(str(lock_file))
    if not lock.tryLock(LOCK_TIMEOUT_MS):
        return _send_show()

    session_kind = detect()
    setup_logging(session_kind.value, debug=args.debug)
    _install_qt_message_handler()  # до создания QApplication: GL-жалобы идут оттуда
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

    app_info = _make_app_info(session_kind, policy.status.value, debug=args.debug)
    theme_bridge = _make_theme_bridge(session_kind)
    shell = _load_qml(app_info, theme_bridge) or _fallback_widget()
    if theme_bridge is not None:
        theme_bridge.source.start()  # слежение за темой — после загрузки QML

    runtime: DictationRuntime | None = None
    settings_bridge = None  # держим Python-обёртку живой до выхода из main
    onboarding = None
    downloads = None
    # Проверяем текущее состояние: диктовка и трей запускаются позже фильтра.
    close_watcher = _wire_close(  # держим ссылку на фильтр
        app, shell, is_tray_ready=lambda: runtime is not None and runtime.tray.registered
    )
    server = ShowServer(lambda timestamp: _show(shell, timestamp))
    timer = _install_signal_handlers(app)
    if not args.hidden:
        _show(shell)

    try:
        runtime_ready = False
        try:
            from astra_voice.runtime import DictationRuntime
            from astra_voice.ui import notify

            def show_details() -> None:
                _show(shell)
                app_info.show_section(SECTION_DEBUG)

            def show_general() -> None:
                _show(shell)
                app_info.show_section(SECTION_GENERAL)

            model_store: ModelStore | None = None
            try:
                model_store = ModelStore()
            except (StoreError, OSError):
                log.warning(
                    "Не удалось открыть хранилище моделей, приложение продолжит работу без него",
                    exc_info=True,
                )
            runtime = DictationRuntime(
                settings=settings, session_kind=session_kind, model_store=model_store
            )
            runtime.on_quit_requested = app.quit
            runtime.on_show_requested = lambda: _show(shell)
            runtime.tray.on_settings = lambda: _show(shell)
            runtime.tray.on_about = lambda: _show(shell)
            runtime.pill.on_details_clicked = show_details
            runtime.start()
            # start() регистрирует общий показ окна для всех действий уведомлений.
            notify.set_action_handler(notify.ACTION_SHOW_DETAILS, show_details)
            notify.set_action_handler(notify.ACTION_CHOOSE_MICROPHONE, show_general)
            notify.set_action_handler(notify.ACTION_CHOOSE_HOTKEY, show_general)
            runtime_ready = True
        except Exception:  # noqa: BLE001 — без диктовки окно должно продолжать работать
            log.warning(
                "Не удалось запустить диктовку, приложение продолжит работу без неё", exc_info=True
            )
        from PyQt5.QtQml import QQmlEngine

        from astra_voice.ui.bridges import (
            ModelDownloads,
            ModelService,
            OnboardingController,
            SettingsBridge,
        )

        model = None
        try:
            model = ModelService(settings, policy)
        except Exception:  # noqa: BLE001 — каталог не должен мешать запуску окна
            log.warning("Не удалось подготовить каталог моделей, настройка продолжится без него")
        downloads = ModelDownloads(model)
        downloads.start_recheck()

        settings_bridge = SettingsBridge(
            stored,
            mirror=settings,
            downloads=downloads,
            apply=_RuntimeSettingsApply(runtime) if runtime_ready and runtime is not None else None,
            locked=policy.locked_keys,
        )
        QQmlEngine.setObjectOwnership(settings_bridge, QQmlEngine.CppOwnership)
        _set_context_property(shell, "settingsBridge", settings_bridge)
        show_onboarding = stored.extra.get("onboarding_done") is not True
        if show_onboarding:
            onboarding = OnboardingController(
                settings_bridge,
                settings=stored,
                downloads=downloads,
                status_sink=runtime.tray.set_download_status
                if runtime_ready and runtime is not None
                else None,
                host=_RuntimeOnboardingHost(runtime, shell)
                if runtime_ready and runtime is not None
                else None,
            )
            QQmlEngine.setObjectOwnership(onboarding, QQmlEngine.CppOwnership)
            _set_context_property(shell, "onboarding", onboarding)
            root = _root_window(shell)
            if root is not None:
                root.installEventFilter(onboarding)
            onboarding.doneChanged.connect(
                lambda: _set_context_property(shell, "showOnboarding", not onboarding.done)
            )
        elif runtime_ready and runtime is not None:
            # Переустановка из настроек видна в трее и после первого запуска.
            def update_download_status() -> None:
                if downloads is not None and runtime is not None:
                    runtime.tray.set_download_status(downloads.status_text())

            downloads.downloadTitleChanged.connect(update_download_status)
            downloads.downloadProgressChanged.connect(update_download_status)
            downloads.downloadStateChanged.connect(update_download_status)
        _set_context_property(shell, "showOnboarding", show_onboarding)
        return int(app.exec_())
    finally:
        # US-8.4: выход из трея и SIGTERM/SIGINT вызывают app.quit() и приходят
        # сюда. Сначала освобождаем воркер и захваты клавиш, затем lock/ipc и UI.
        if onboarding is not None:
            try:
                onboarding.shutdown()
            except Exception:  # noqa: BLE001 — остальные ресурсы тоже нужно освободить
                log.warning("Не удалось завершить установку модели")
        if downloads is not None:
            try:
                downloads.shutdown()
            except Exception:  # noqa: BLE001 — остальные ресурсы тоже нужно освободить
                log.warning("Не удалось завершить очередь установки моделей")
        if runtime is not None:
            try:
                runtime.shutdown()
            except Exception:  # noqa: BLE001 — ошибка диктовки не должна оставить lock/ipc
                log.warning("Не удалось завершить диктовку")
        del close_watcher
        timer.stop()
        if theme_bridge is not None:
            theme_bridge.source.stop()
        _cleanup(server, lock)
