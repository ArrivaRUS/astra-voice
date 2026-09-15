"""Потоковая загрузка с сетевым гейтом и проверкой каждого адреса."""

from __future__ import annotations

import os
import threading
import time
import urllib.request
from collections.abc import Iterator, Mapping
from pathlib import Path
from types import TracebackType
from urllib.parse import urljoin, urlsplit

import requests
from requests.packages.urllib3.exceptions import ReadTimeoutError

from astra_voice.net.gate import NetworkGate, NetworkKind

ALLOWED_HOSTS = (
    "github.com",
    "api.github.com",
    "objects.githubusercontent.com",
    "huggingface.co",
    "hf.co",
)
ALLOWED_SCHEMES = ("https",)
ALLOWED_PORTS = (None, 443)
_SUBDOMAIN_HOSTS = ("huggingface.co", "hf.co")
_REDIRECT_STATUSES = (301, 302, 303, 307, 308)
_SYSTEM_CA_BUNDLE = Path("/etc/ssl/certs/ca-certificates.crt")


class NetworkError(Exception):
    """Ошибка сети со стабильным кодом и сообщением для пользователя."""

    code: str
    message: str

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _host_allowed(host: str) -> bool:
    """Сравнивает метки домена; поддомены разрешены только для двух источников."""
    labels = host.lower().split(".")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789-" for char in label)
        for label in labels
    ):
        return False
    for allowed in ALLOWED_HOSTS:
        allowed_labels = allowed.split(".")
        if labels == allowed_labels:
            return True
        if allowed in _SUBDOMAIN_HOSTS and labels[-len(allowed_labels) :] == allowed_labels:
            return True
    return False


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
    if not _host_allowed(host):
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

    def iter_chunks(self, chunk_size: int = 65536) -> Iterator[bytes]:
        """Читает блоки, проверяя отмену и дедлайн до и после каждого чтения."""
        try:
            if chunk_size <= 0:
                raise ValueError("Размер блока должен быть положительным.")
            chunks = self._response.iter_content(chunk_size=chunk_size)
            while not self._closed:
                _remaining(self._deadline_at, self._cancel)
                try:
                    chunk: bytes = next(chunks)
                except StopIteration:
                    return
                _remaining(self._deadline_at, self._cancel)
                if chunk:
                    yield chunk
        except requests.exceptions.RequestException as error:
            raise _request_error(error, self._host) from None
        finally:
            self.close()

    def close(self) -> None:
        """Закрывает ответ и сессию; повторное закрытие безопасно."""
        if self._closed:
            return
        self._closed = True
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
        try:
            while True:
                host = _validate_url(url)
                proxies = urllib.request.getproxies()
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
                    raise _request_error(error, host) from None
                try:
                    _remaining(deadline_at, cancel)
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
