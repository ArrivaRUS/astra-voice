"""Потоковая загрузка с сетевым гейтом и проверкой каждого адреса."""

from __future__ import annotations

import logging
import os
import socket
import threading
import time
import urllib.request
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import TracebackType
from urllib.parse import urljoin, urlsplit

import requests
from urllib3 import HTTPConnectionPool, HTTPSConnectionPool, ProxyManager
from urllib3.connection import HTTPConnection
from urllib3.exceptions import ReadTimeoutError

from astra_voice.net.gate import NetworkGate, NetworkKind
from astra_voice.net.hosts import ALLOWED_HOSTS, host_allowed

log = logging.getLogger(__name__)

ALLOWED_SCHEMES = ("https",)
ALLOWED_PORTS = (None, 443)
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_SYSTEM_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")
# 128 байт/с с запасом на 30 с чтения: даже медленные каналы имеют большой запас.
# Время обработки блока потребителем не расходует этот запас; быстрый блок его
# пополняет, но не позволяет накопить неограниченный кредит на последующий простой.
_MIN_SPEED_BYTES_PER_SECOND = 128
_MIN_SPEED_GRACE_S = 30.0
_WATCHDOG_INTERVAL_S = 0.05


class NetworkError(Exception):
    """Ошибка сети со стабильным кодом и сообщением для пользователя."""

    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _check_url_text(url: str) -> None:
    """Не допускает неоднозначности при разборе адресов и перенаправлений."""
    if not url or "\\" in url or any(ord(char) <= 32 or ord(char) == 127 for char in url):
        raise NetworkError("not-allowed", "Источник не разрешён: некорректный адрес.")


def _validate_url(url: str) -> str:
    """Проверяет весь адрес и возвращает хост без учётных данных."""
    _check_url_text(url)
    try:
        parts = urlsplit(url)
        host = (parts.hostname or "").encode("idna").decode("ascii").lower()
        port = parts.port
    except (ValueError, UnicodeError):
        raise NetworkError("not-allowed", "Источник не разрешён: некорректный адрес.") from None
    # Сам адрес не попадает в сообщения: даже битый URL может содержать пароль.
    if parts.username is not None or parts.password is not None:
        raise NetworkError("not-allowed", "Источник не разрешён: адрес содержит учётные данные.")
    if parts.scheme not in ALLOWED_SCHEMES:
        raise NetworkError("not-allowed", "Источник не разрешён: требуется защищённое соединение.")
    if port not in ALLOWED_PORTS:
        raise NetworkError("not-allowed", "Источник не разрешён: недопустимый порт.")
    # Передаём текущий список: подмена http.ALLOWED_HOSTS действует и на редиректы.
    if not host_allowed(host, ALLOWED_HOSTS):
        raise NetworkError("not-allowed", "Источник не разрешён: сервер отсутствует в списке.")
    return host


def _remaining(deadline_at: float, cancel: threading.Event) -> float:
    """Проверяет отмену и возвращает остаток общего времени операции."""
    if cancel.is_set():
        raise NetworkError("cancelled", "Загрузка отменена.")
    remaining = deadline_at - time.monotonic()
    if not remaining > 0:
        raise NetworkError("timeout", "Время ожидания загрузки истекло.")
    return remaining


def _verify_bundle(ca_bundle: Path | None) -> str | bool:
    """Выбирает существующий файл сертификатов, сохраняя проверку TLS всегда."""
    if ca_bundle is not None and ca_bundle.is_file():
        return str(ca_bundle)
    if _SYSTEM_CA_BUNDLE.is_file():
        return str(_SYSTEM_CA_BUNDLE)
    environment_bundle = os.environ.get("SSL_CERT_FILE")
    if environment_bundle and Path(environment_bundle).is_file():
        return environment_bundle
    return True


def _request_error(error: requests.exceptions.RequestException, host: str) -> NetworkError:
    """Переводит ошибки транспорта, не раскрывая адреса и пароли из исключений."""
    # При потоковом чтении таймаут обёрнут в ConnectionError, а не в Timeout.
    if isinstance(error, requests.exceptions.Timeout) or any(
        isinstance(arg, ReadTimeoutError) for arg in error.args
    ):
        return NetworkError("timeout", "Время ожидания ответа сервера истекло.")
    if isinstance(
        error,
        (requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema),
    ):
        return NetworkError("not-allowed", "Источник не разрешён: некорректный адрес.")
    return NetworkError("host-unreachable", f"Не удалось связаться с {host}.")


def _shutdown_socket(sock: socket.socket | None) -> None:
    if sock is not None:
        try:
            # Базовый метод не меняет _sslobj у SSLSocket во время TLS-чтения.
            socket.socket.shutdown(sock, socket.SHUT_RDWR)
        except OSError:
            # Сокет мог уже закрыться при EOF или ошибке транспорта.
            pass


class _RequestWatchdog:
    """Прерывает отправку и чтение заголовков, пока StreamResponse ещё не создан."""

    def __init__(self, deadline_at: float, cancel: threading.Event) -> None:
        self._deadline_at = deadline_at
        self._cancel = cancel
        self._connections: list[HTTPConnection] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._triggered = threading.Event()
        self._thread = threading.Thread(target=self._watch, name="http-request-watchdog")
        self._thread.start()

    def register(self, connection: HTTPConnection) -> None:
        original_connect = connection.connect

        def connect() -> None:
            original_connect()
            # Пул регистрирует соединение до появления сокета; сторож уже мог выйти.
            with self._lock:
                if self._triggered.is_set():
                    sock = connection.sock
                    if isinstance(sock, socket.socket):
                        _shutdown_socket(sock)

        connection.connect = connect
        with self._lock:
            self._connections.append(connection)
            if self._triggered.is_set():
                sock = connection.sock
                if isinstance(sock, socket.socket):
                    _shutdown_socket(sock)

    def _watch(self) -> None:
        while not self._stop.wait(_WATCHDOG_INTERVAL_S):
            try:
                _remaining(self._deadline_at, self._cancel)
            except NetworkError:
                with self._lock:
                    self._triggered.set()
                    for connection in self._connections:
                        sock = connection.sock
                        if isinstance(sock, socket.socket):
                            _shutdown_socket(sock)
                return

    def close(self) -> None:
        self._stop.set()
        self._thread.join()


class _RequestAdapter(requests.adapters.HTTPAdapter):
    """Регистрирует соединения до connect/request/getresponse, включая прокси."""

    def __init__(self, watchdog: _RequestWatchdog) -> None:
        self._watchdog = watchdog
        self._registering_classes: dict[type[HTTPConnectionPool], type[HTTPConnectionPool]] = {}
        super().__init__()
        # Меняем словарь только этого менеджера, не глобальный словарь urllib3.
        self.poolmanager.pool_classes_by_scheme = {
            "http": self._registering(HTTPConnectionPool, watchdog),
            "https": self._registering(HTTPSConnectionPool, watchdog),
        }

    def _registering(
        self, cls: type[HTTPConnectionPool], watchdog: _RequestWatchdog
    ) -> type[HTTPConnectionPool]:
        if cls in self._registering_classes:
            return self._registering_classes[cls]

        class RegisteringPool(cls):
            def _new_conn(self) -> HTTPConnection:
                connection: HTTPConnection = super()._new_conn()
                watchdog.register(connection)
                return connection

        self._registering_classes[cls] = RegisteringPool
        # Менеджер может повторно вернуть уже обёрнутый класс.
        self._registering_classes[RegisteringPool] = RegisteringPool
        return RegisteringPool

    def proxy_manager_for(self, proxy: str, **proxy_kwargs: object) -> ProxyManager:
        manager: ProxyManager = super().proxy_manager_for(proxy, **proxy_kwargs)
        manager.pool_classes_by_scheme = {
            scheme: self._registering(cls, self._watchdog)
            for scheme, cls in manager.pool_classes_by_scheme.items()
        }
        return manager


class _Session(requests.Session):
    """Сессия без скрытого чтения тела и подготовки следующего перенаправления."""

    def resolve_redirects(
        self, resp: requests.Response, req: requests.PreparedRequest, **kwargs: object
    ) -> Iterator[requests.Response | requests.PreparedRequest]:
        # Даже allow_redirects=False обычно запускает подготовку response.next:
        # она читает тело и разбирает непроверенный Location внутри библиотеки.
        return iter(())


class StreamResponse:
    """Ответ, владеющий соединением до закрытия или завершения чтения."""

    url: str
    status: int
    headers: Mapping[str, str]

    def __init__(
        self,
        response: requests.Response,
        session: requests.Session,
        *,
        url: str,
        host: str,
        deadline_at: float,
        cancel: threading.Event,
    ) -> None:
        self.url = url
        self.status = response.status_code
        self.headers = response.headers.copy()
        self._response = response
        self._session = session
        self._host = host
        self._deadline_at = deadline_at
        self._cancel = cancel
        self._closed = False
        self._resources_closed = False
        self._lock = threading.Lock()
        self._read_lock = threading.Lock()
        self._stop_watchdog = threading.Event()
        self._error: NetworkError | None = None
        self._speed_budget = _MIN_SPEED_GRACE_S
        self._read_deadline_at: float | None = None
        # При Connection: close urllib3.connection.sock уже может быть None.
        # Сохраняем сокет из файлового объекта до первого чтения и его закрытия.
        fp = getattr(response.raw, "_fp", None)
        raw = getattr(getattr(fp, "fp", None), "raw", None)
        sock = getattr(raw, "_sock", None)
        self._socket = sock if isinstance(sock, socket.socket) else None
        if self._socket is None:
            log.warning("Не удалось получить сокет потока: отмена может срабатывать медленнее.")
        self._watchdog = threading.Thread(target=self._watch, name="http-stream-watchdog")
        self._watchdog.start()

    def _check_active(self) -> bool:
        """Проверяет состояние под self._lock, в том числе после прерванного чтения."""
        if self._error is not None:
            raise self._error
        if self._closed:
            return False
        _remaining(self._deadline_at, self._cancel)
        if self._read_deadline_at is not None and time.monotonic() >= self._read_deadline_at:
            raise NetworkError("timeout", "Слишком низкая скорость загрузки.")
        return True

    def _shutdown_socket(self) -> None:
        """Прерывает recv под self._lock, не закрывая читаемый BufferedReader."""
        _shutdown_socket(self._socket)

    def _watch(self) -> None:
        """Следит за остановкой даже внутри requests/urllib3 read(amt)."""
        while not self._stop_watchdog.wait(_WATCHDOG_INTERVAL_S):
            with self._lock:
                try:
                    if not self._check_active():
                        return
                except NetworkError as error:
                    self._error = error
                    self._shutdown_socket()
                    return

    def iter_chunks(self, chunk_size: int = 65536, *, limit: int | None = None) -> Iterator[bytes]:
        """Отдаёт блоки до chunk_size, ограничивая суммарное чтение до limit байт."""
        try:
            if chunk_size <= 0:
                raise ValueError("Размер блока должен быть положительным.")
            if limit is not None and limit < 0:
                raise ValueError("Лимит чтения должен быть неотрицательным.")
            # Читаем запрошенными блоками: сторож прерывает read(amt) через сокет,
            # поэтому отзывчивость отмены и дедлайна не зависит от размера блока.
            read_size = chunk_size
            chunks = self._response.iter_content(chunk_size=read_size)
            # Закрытие старого генератора urllib3 прерывает chunked-ответ.
            # Держим итераторы живыми до завершения чтения ограниченного хвоста.
            iterators = [chunks]
            while True:
                if limit is not None:
                    if limit <= 0:
                        return
                    if limit < read_size:
                        # Размер хвоста задаём до чтения, не обрезаем уже прочитанное.
                        # При коротких chunked-блоках повторно уменьшаем размер
                        # минимум вдвое: сохраняем не более bit_length + 1 итераторов.
                        read_size = (
                            limit if len(iterators) == 1 else min(limit, max(1, read_size // 2))
                        )
                        chunks = self._response.iter_content(chunk_size=read_size)
                        iterators.append(chunks)
                with self._read_lock:
                    with self._lock:
                        if not self._check_active():
                            return
                        read_deadline = time.monotonic() + self._speed_budget
                        self._read_deadline_at = read_deadline
                    try:
                        chunk: bytes = next(chunks, b"")
                    finally:
                        with self._lock:
                            try:
                                active = self._check_active()
                            finally:
                                self._speed_budget = read_deadline - time.monotonic()
                                self._read_deadline_at = None
                    if not active:
                        return
                    with self._lock:
                        self._speed_budget = min(
                            _MIN_SPEED_GRACE_S,
                            self._speed_budget + len(chunk) / _MIN_SPEED_BYTES_PER_SECOND,
                        )
                if not chunk:
                    return
                if limit is not None:
                    limit -= len(chunk)
                yield chunk
        except requests.exceptions.RequestException as error:
            with self._lock:
                if not self._check_active():
                    return
            raise _request_error(error, self._host) from None
        finally:
            self.close()

    def close(self) -> None:
        """Прерывает чтение, дожидается сторожа и закрывает ресурсы ровно один раз."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._stop_watchdog.set()
                self._shutdown_socket()
        self._watchdog.join()
        # response.close() ждёт блокировку BufferedReader. Вызываем его только
        # после выхода из чтения, уже разбуженного через shutdown сокета.
        with self._read_lock:
            if self._resources_closed:
                return
            self._resources_closed = True
            try:
                self._response.close()
            finally:
                self._session.close()

    def __enter__(self) -> StreamResponse:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()


class HttpClient:
    """Выполняет только разрешённые загрузки без пользовательских учётных данных."""

    def __init__(
        self, gate: NetworkGate, *, ca_bundle: Path | None = None, user_agent: str
    ) -> None:
        self._gate = gate
        self._ca_bundle = ca_bundle
        self._user_agent = user_agent

    def get_stream(
        self,
        url: str,
        *,
        range_from: int | None = None,
        deadline_s: float,
        cancel: threading.Event,
        kind: NetworkKind = "download",
    ) -> StreamResponse:
        """Проверяет гейт, открывает поток и вручную проходит до пяти перенаправлений."""
        started_at = time.monotonic()
        allowed, reason = self._gate.allowed(kind)
        if not allowed:
            raise NetworkError("no-network", reason)
        deadline_at = started_at + deadline_s
        _remaining(deadline_at, cancel)
        _validate_url(url)
        verify = _verify_bundle(self._ca_bundle)
        session = _Session()
        session.trust_env = False
        session.headers.clear()
        redirects = 0
        watchdog: _RequestWatchdog | None = None
        try:
            watchdog = _RequestWatchdog(deadline_at, cancel)
            for scheme in ("http://", "https://"):
                session.mount(scheme, _RequestAdapter(watchdog))
            while True:
                host = _validate_url(url)
                proxies = urllib.request.getproxies()
                # trust_env отключён: учитываем исключения явно для каждого адреса.
                if requests.utils.should_bypass_proxies(url, no_proxy=proxies.get("no")):
                    proxies = {}
                headers = {
                    "User-Agent": self._user_agent,
                    "Accept-Encoding": "identity",
                    "Connection": "close",
                }
                if range_from is not None:
                    headers["Range"] = f"bytes={range_from}-"
                # Включая тот же origin: полученные от сервера cookies не отправляются.
                session.cookies.clear()
                remaining = _remaining(deadline_at, cancel)
                try:
                    response = session.get(
                        url,
                        headers=headers,
                        proxies=proxies,
                        verify=verify,
                        timeout=(min(3.0, remaining), min(30.0, remaining)),
                        stream=True,
                        allow_redirects=False,
                    )
                except requests.exceptions.RequestException as error:
                    # shutdown прерывает транспорт, но причина — отмена/дедлайн.
                    try:
                        _remaining(deadline_at, cancel)
                    except NetworkError as stopped:
                        raise stopped from None
                    raise _request_error(error, host) from None
                try:
                    _remaining(deadline_at, cancel)
                    # iter_content распаковывает до учёта бюджета. Отказываем по
                    # заголовкам, включая редиректы, и закрываем без чтения тела.
                    if response.headers.get("Content-Encoding", "").strip().lower() not in (
                        "",
                        "identity",
                    ):
                        raise NetworkError("not-allowed", "Сжатие ответа сервера не разрешено.")
                    if response.status_code in _REDIRECT_STATUSES:
                        if redirects >= 5:
                            raise NetworkError("not-allowed", "Слишком много перенаправлений.")
                        location = response.headers.get("Location", "")
                        _check_url_text(location)
                        try:
                            destination = urlsplit(location)
                            if (destination.scheme or location.startswith("//")) and not (
                                destination.netloc
                            ):
                                raise ValueError
                            target = urljoin(url, location)
                        except (ValueError, UnicodeError):
                            raise NetworkError(
                                "not-allowed", "Источник не разрешён: некорректное перенаправление."
                            ) from None
                        _validate_url(target)
                        _remaining(deadline_at, cancel)
                        url = target
                        redirects += 1
                    elif response.status_code >= 400:
                        raise NetworkError(
                            "bad-status",
                            f"Сервер {host} отклонил запрос (код {response.status_code}).",
                        )
                    else:
                        return StreamResponse(
                            response,
                            session,
                            url=url,
                            host=host,
                            deadline_at=deadline_at,
                            cancel=cancel,
                        )
                except BaseException:
                    response.close()
                    raise
                response.close()
        except BaseException:
            session.close()
            raise
        finally:
            if watchdog is not None:
                watchdog.close()
