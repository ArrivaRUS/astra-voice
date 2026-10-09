"""GUI-owned AppImage consent and worker lifecycle; no QML-supplied filesystem paths."""

from __future__ import annotations

import queue
import secrets
import threading
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from PyQt5.QtCore import QLockFile, QObject, QTimer, pyqtProperty, pyqtSignal, pyqtSlot

from astra_voice.core import paths
from astra_voice.platform import autostart, userinstall

LOCK_TIMEOUT = 0.5
BUSY = "Завершите диктовку и дождитесь вставки текста, затем повторите удаление"
PARTIAL = (
    "Действие выполнено не полностью. Регистрация могла измениться, часть копий могла "
    "остаться. Повторите после проверки состояния."
)
UNKNOWN_COPY = (
    "Не удалось определить работающую копию. Завершите Astra Voice и повторите "
    "удаление из исходного файла .AppImage."
)


class Runtime(Protocol):
    def appimage_remove_busy_reason(self) -> str: ...
    def reserve_appimage_removal(self) -> bool: ...
    def release_appimage_removal(self) -> None: ...


class ConsentChanged(userinstall.UserInstallError):
    pass


@dataclass(frozen=True)
class _Reply:
    action: str
    snapshot: userinstall.ManagementSnapshot | None = None
    removed: userinstall.RemoveResult | None = None
    error: str = ""


def _snapshot() -> userinstall.ManagementSnapshot:
    userinstall.validate_remove_paths()
    app = paths.appimage_app_dir()
    lock = userinstall._install_lock(app, LOCK_TIMEOUT) if app.is_dir() else nullcontext()
    with lock:
        return userinstall.management_snapshot()


def _error_text(error: Exception, *, mutation_started: bool) -> str:
    if isinstance(error, ConsentChanged):
        return "Состояние установки изменилось. Проверьте данные и подтвердите действие заново."
    if isinstance(error, userinstall.LockTimeoutError):
        return "Установка программы занята. Дождитесь завершения и повторите действие."
    message = str(error)
    if "Небезопасн" in message or isinstance(error, paths.PathError):
        return (
            "Удаление остановлено: папка программы не прошла проверку безопасности. "
            "Файлы могли остаться. Обратитесь к администратору."
        )
    if "работающую копию" in message:
        return UNKNOWN_COPY
    if "Остановка одной" in message:
        return "Остановка одной из копий не подтверждена. Файлы сохранены; требуется проверка."
    if mutation_started:
        return PARTIAL
    return "Не удалось проверить установку. Повторите проверку."


def _run(
    action: str,
    consent: userinstall.ManagementSnapshot | None,
    own_lock: Path,
    running_key: str | None,
    output: queue.Queue[_Reply],
) -> None:
    """Only immutable input/output and Python/QLockFile; never inspect a QObject."""
    locks: list[QLockFile] = []
    mutation_started = False
    try:
        if action == "refresh":
            output.put(_Reply(action, _snapshot()))
            return
        if consent is None:
            raise ConsentChanged()

        def validate() -> None:
            nonlocal mutation_started
            if userinstall.management_snapshot() != consent:
                raise ConsentChanged()
            mutation_started = True

        def prepare() -> list[Path]:
            directories = paths._runtime_dir_candidates(reading=True)
            userinstall.validate_remove_paths((*directories, own_lock.parent))
            stopped: list[Path] = []
            for directory in directories:
                # This process already holds this lock, including portable sessions.
                if directory / "lock" == own_lock:
                    continue
                try:
                    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
                except PermissionError:
                    if not directory.exists():
                        continue
                    raise
                lock = QLockFile(str(directory / "lock"))
                lock.setStaleLockTime(0)
                if lock.tryLock(0):
                    locks.append(lock)
                    stopped.append(directory)
                elif (
                    lock.error() != QLockFile.LockFailedError
                    or userinstall._read_running_key(directory / userinstall.RUNNING_KEY_NAME)
                    is None
                ):
                    raise userinstall.UserInstallError(UNKNOWN_COPY)
            return stopped

        removed = None
        if action == "unregister":
            userinstall.unregister(validate=validate, lock_timeout=LOCK_TIMEOUT)
        elif action == "remove":
            removed = userinstall.remove_program(
                keep=running_key, prepare=prepare, validate=validate, lock_timeout=LOCK_TIMEOUT
            )
        else:
            raise ValueError("Unsupported operation")
        current = _snapshot()
        registration_remains = current.can_unregister
        # D2 may already have removed the bundle that proved icon ownership.
        # Compare with the consent's known own bytes as well: absence of source
        # bundles in the post-snapshot must not turn an unremoved icon into success.
        before = dict(consent.identities)
        for icon in consent.icons:
            identity = userinstall._management_identity(icon)
            previous = before.get(str(icon), ())
            if (
                identity
                and previous
                and isinstance(identity[-1], bytes)
                and identity[-1] == previous[-1]
            ):
                registration_remains = True
        unexpected_copies = bool(
            removed is not None and set(current.installed).difference(removed.kept)
        )
        output.put(
            _Reply(
                action,
                current,
                removed,
                error=PARTIAL if registration_remains or unexpected_copies else "",
            )
        )
    except Exception as error:
        # A failure can follow unregister/partial cleanup. Reread, never invent rollback.
        try:
            current = _snapshot()
        except Exception:
            current = None
        output.put(
            _Reply(action, current, error=_error_text(error, mutation_started=mutation_started))
        )
    finally:
        for lock in reversed(locks):
            lock.unlock()


class AppImageManagement(QObject):
    """All public methods are GUI-thread-only.

    Owner supplies its held session lock and verified running KEY (portable: None).
    close()/shutdown() closes delivery first and joins at most 3 seconds. False
    means resources are still live: owner MUST preserve the installed copy using
    its existing incomplete-shutdown barrier. Call before runtime/lock teardown.
    worker_threads keeps live threads available to that shutdown barrier.
    """

    changed = pyqtSignal()
    quitRequested = pyqtSignal()

    def __init__(
        self,
        runtime: Runtime,
        *,
        own_lock_path: Path,
        running_key: str | None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._runtime = runtime
        self._own_lock = own_lock_path
        self._running_key = running_key
        self._closed = False
        self._reserved = False
        self._successful_remove = False
        self._worker: threading.Thread | None = None
        self._output: queue.Queue[_Reply] = queue.Queue()
        self._snapshot: userinstall.ManagementSnapshot | None = None
        self._consent: userinstall.ManagementSnapshot | None = None
        self._confirmation_id = ""
        self._confirmation_action = ""
        self._confirmation_message = ""
        self._retry_action = ""
        self._after_refresh = ""
        self._state = "checking"
        self._error = ""
        self._result = ""
        self._busy = ""
        try:
            self._kind = paths.install_kind().value
        except Exception:
            self._kind = "unknown"
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._poll)
        self._timer.start()
        QTimer.singleShot(0, self.refresh)

    @pyqtProperty(str, notify=changed)
    def installKind(self) -> str:
        return self._kind

    @pyqtProperty(str, notify=changed)
    def state(self) -> str:
        return self._state

    @pyqtProperty(str, notify=changed)
    def appPath(self) -> str:
        return str(self._snapshot.app_path) if self._snapshot else ""

    def _available(self) -> bool:
        return (
            not self._closed
            and self._worker is None
            and self._state in ("ready", "absent", "busy")
            and self._snapshot is not None
        )

    @pyqtProperty(bool, notify=changed)
    def canUnregister(self) -> bool:
        return bool(self._available() and self._snapshot and self._snapshot.can_unregister)

    @pyqtProperty(bool, notify=changed)
    def canRemove(self) -> bool:
        return bool(
            self._available() and self._snapshot and self._snapshot.installed and not self._busy
        )

    @pyqtProperty(str, notify=changed)
    def busyReason(self) -> str:
        return self._busy

    @pyqtProperty(str, notify=changed)
    def resultText(self) -> str:
        return self._result

    @pyqtProperty(str, notify=changed)
    def errorText(self) -> str:
        return self._error

    @pyqtProperty(str, notify=changed)
    def confirmationId(self) -> str:
        return self._confirmation_id

    @pyqtProperty(str, notify=changed)
    def confirmationAction(self) -> str:
        return self._confirmation_action

    @pyqtProperty(str, notify=changed)
    def confirmationMessage(self) -> str:
        return self._confirmation_message

    @property
    def worker_threads(self) -> tuple[threading.Thread, ...]:
        return (self._worker,) if self._worker is not None else ()

    def _busy_reason(self) -> str:
        if self._kind == "appimage-installed" and not self._running_key:
            return UNKNOWN_COPY
        try:
            return self._runtime.appimage_remove_busy_reason()
        except Exception:
            return "Не удалось проверить готовность программы. Повторите проверку."

    def _start(self, action: str, consent: userinstall.ManagementSnapshot | None = None) -> None:
        self._state = {"refresh": "checking", "unregister": "unregistering", "remove": "removing"}[
            action
        ]
        self._worker = threading.Thread(
            target=_run,
            args=(action, consent, self._own_lock, self._running_key, self._output),
            name="appimage-management",
            daemon=False,
        )
        try:
            self._worker.start()
        except Exception:
            self._worker = None
            self._release()
            self._state = "error"
            self._error = "Не удалось начать действие. Повторите проверку."
        self.changed.emit()

    def _clear_confirmation(self) -> None:
        self._confirmation_id = self._confirmation_action = self._confirmation_message = ""
        self._consent = None

    @pyqtSlot()
    def refresh(self) -> None:
        if self._closed or self._worker is not None or self._state == "exiting":
            return
        self._clear_confirmation()
        self._error = ""
        if self._kind not in ("appimage-installed", "appimage-portable"):
            self._state = "hidden"
            self.changed.emit()
            return
        self._start("refresh")

    @pyqtSlot(str)
    def requestAction(self, action: str) -> None:
        if action not in ("unregister", "remove") or not self._available():
            return
        # Every dialog starts with fresh facts, not the snapshot last painted by QML.
        self._retry_action = action
        self._after_refresh = action
        self.refresh()

    def _open_confirmation(self, action: str) -> None:
        snapshot = self._snapshot
        self._busy = self._busy_reason()
        if snapshot is None or (action == "remove" and self._busy):
            self._state = "busy"
            return
        if action == "remove" and not snapshot.installed:
            self._state = "absent"
            return
        if action == "unregister" and not snapshot.can_unregister:
            self._result = "Регистрация AppImage уже убрана."
            return
        if snapshot.autostart_owned:
            explanation = (
                "Автозапуск будет переключён на системную версию Astra Voice."
                if snapshot.deb_available
                else "Автозапуск AppImage будет выключен."
            )
        else:
            explanation = "Собственной записи автозапуска AppImage нет. Чужие записи сохранятся."
        if action == "remove":
            message = (
                "Будут удалены установленные AppImage-копии Astra Voice из папки:\n"
                f"{snapshot.app_path}\n\n"
                f"Пункт меню и значки AppImage будут убраны.\n{explanation}\n\n"
                "После подтверждения Astra Voice завершится. Работающая копия будет удалена "
                "при выходе после остановки её процессов.\n\n"
                "Модели, настройки и журналы останутся. Исходный файл .AppImage останется."
                f"\n\nСохранённые данные:\n{snapshot.models_path}\n{snapshot.settings_path}"
            )
        else:
            message = (
                f"Пункт меню и значки AppImage будут убраны.\n{explanation}\n\n"
                f"Копии программы останутся в папке:\n{snapshot.app_path}\n\n"
                "Модели, настройки и журналы останутся. Astra Voice продолжит работу."
            )
        self._consent = snapshot
        self._confirmation_id = secrets.token_urlsafe(24)
        self._confirmation_action = action
        self._confirmation_message = message
        self._state = "confirming"

    @pyqtSlot(str)
    def cancelConfirmation(self, token: str) -> None:
        if not token or token != self._confirmation_id:
            return
        self._clear_confirmation()
        self._ready()
        self.changed.emit()

    @pyqtSlot(str)
    def confirmAction(self, token: str) -> None:
        if self._closed or not token or token != self._confirmation_id or self._worker is not None:
            return
        action, consent = self._confirmation_action, self._consent
        self._clear_confirmation()  # Consume before reservation or any worker creation.
        self._error = self._result = ""
        if action == "remove":
            self._busy = self._busy_reason()
            try:
                reserved = not self._busy and self._runtime.reserve_appimage_removal()
            except Exception:
                reserved = False
            if not reserved:
                self._busy = self._busy or BUSY
                self._state = "busy"
                self.changed.emit()
                return
            self._reserved = True
        try:
            self._start(action, consent)
        except Exception:
            self._worker = None
            self._release()
            self._state = "error"
            self._error = "Не удалось начать действие. Повторите проверку."
            self.changed.emit()

    @pyqtSlot()
    def retry(self) -> None:
        if self._worker is not None or self._closed or self._state == "exiting":
            return
        self._after_refresh = self._retry_action
        self.refresh()

    def _release(self) -> None:
        if self._reserved and not self._successful_remove:
            self._runtime.release_appimage_removal()
            self._reserved = False

    def _ready(self) -> None:
        self._busy = self._busy_reason()
        self._state = "ready" if self._snapshot and self._snapshot.installed else "absent"
        if self._busy and self._snapshot and self._snapshot.installed:
            self._state = "busy"

    @pyqtSlot()
    def _poll(self) -> None:
        if self._closed:
            return
        if self._worker is None:
            if self._state in ("ready", "absent", "busy", "confirming"):
                busy = self._busy_reason()
                if busy != self._busy:
                    self._busy = busy
                    if self._state != "confirming":
                        self._ready()
                    self.changed.emit()
            return
        # A queued result does not prove the Thread has actually finished.
        if self._worker.is_alive():
            return
        self._worker.join()
        self._worker = None
        try:
            reply = self._output.get_nowait()
        except queue.Empty:
            reply = _Reply("refresh", error="Не удалось проверить установку. Повторите проверку.")
        self._snapshot = reply.snapshot
        if reply.error:
            self._after_refresh = ""
            self._release()
            self._state = "error"
            self._error = reply.error
        elif reply.action == "remove":
            self._successful_remove = True
            self._state = "exiting"
            self._result = (
                "Удаление подготовлено. Astra Voice завершает работу. "
                "Модели, настройки и журналы сохранены."
            )
            if reply.removed and any(key != self._running_key for key in reply.removed.kept):
                self._result += " Другие работающие копии будут удалены после их завершения."
            self.changed.emit()
            self.quitRequested.emit()
            return
        else:
            self._ready()
            if reply.action == "unregister":
                self._result = "Регистрация AppImage убрана. Копии программы и данные сохранены."
                if reply.snapshot and reply.snapshot.autostart_owned:
                    if reply.snapshot.autostart_program == autostart.DEB_EXECUTABLE:
                        self._result += " Автозапуск указывает на системную версию."
            action, self._after_refresh = self._after_refresh, ""
            if action:
                self._open_confirmation(action)
        self.changed.emit()

    def close(self) -> bool:
        self._closed = True
        self._timer.stop()
        self._clear_confirmation()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=3.0)
            if worker.is_alive():
                return False
            self._worker = None
            # During ordinary quit the GUI may not have consumed the success reply.
            try:
                reply = self._output.get_nowait()
                self._successful_remove = reply.action == "remove" and not reply.error
            except queue.Empty:
                pass
        self._release()
        return True

    def shutdown(self) -> bool:
        return self.close()
