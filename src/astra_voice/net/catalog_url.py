"""Общая проверка адреса подписанного каталога без загрузки транспорта."""

from __future__ import annotations

import re
from urllib.parse import urlsplit


def _catalog_urls(catalog_url: str, schemes: tuple[str, ...]) -> tuple[str, str]:
    if (
        not catalog_url
        or not catalog_url.isascii()
        or any(ord(char) <= 32 or ord(char) == 127 or char in "\\%?#" for char in catalog_url)
    ):
        raise ValueError("Неверный адрес каталога")
    parsed = urlsplit(catalog_url)
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname) is None
        or any(part in (".", "..") for part in parsed.path.split("/"))
        or parsed.port is not None
        and not 0 < parsed.port <= 65535
    ):
        raise ValueError("Неверный адрес каталога")
    return catalog_url, catalog_url + ".sig"


def catalog_urls(catalog_url: str) -> tuple[str, str]:
    """Production URL: HTTPS, без credentials, параметров и обхода пути."""
    return _catalog_urls(catalog_url, ("https",))
