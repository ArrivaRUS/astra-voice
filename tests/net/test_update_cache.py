"""Кэш проверок устойчив к повреждённым и чужим файлам."""

import os
import threading
from pathlib import Path

import pytest

from astra_voice.net.update_cache import CacheEntry, UpdateCache

pytestmark = pytest.mark.unit


def test_atomic_private_write(tmp_path: Path) -> None:
    path = tmp_path / "update-cache.json"
    cache = UpdateCache(path)
    cache.set("app:github", CacheEntry(etag='"abc"', body="данные", stored_at=1.0))
    assert cache.get("app:github").body == "данные"
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(tmp_path.iterdir()) == [path]
    cache.set("app:github", CacheEntry(etag='"abc"', body="x" * (256 * 1024 + 1)))
    assert cache.get("app:github").body is None
    assert cache.get("app:github").etag is None


@pytest.mark.parametrize("bad", [b"{", b"x" * (1024 * 1024 + 1)])
def test_bad_file(tmp_path: Path, bad: bytes) -> None:
    path = tmp_path / "update-cache.json"
    path.write_bytes(bad)
    assert UpdateCache(path).get("app:github") == CacheEntry()


def test_symlink(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("{}")
    link = tmp_path / "update-cache.json"
    os.symlink(target, link)
    assert UpdateCache(link).get("app:github") == CacheEntry()


def test_invalid_types(tmp_path: Path) -> None:
    path = tmp_path / "update-cache.json"
    path.write_text(
        '{"key":{"etag":17,"body":"ok","stored_at":1,'
        '"rate_limited_until":null,"backoff_until":null,"failures":0}}'
    )
    assert UpdateCache(path).get("key") == CacheEntry()


def test_invalid_url_type(tmp_path: Path) -> None:
    cache = UpdateCache(tmp_path / "cache.json")
    cache.set("key", CacheEntry(url="https://example.com"))
    cache.path.write_text(cache.path.read_text().replace('"url":"https://example.com"', '"url":7'))
    assert cache.get("key") == CacheEntry()


def test_old_entry_without_url(tmp_path: Path) -> None:
    path = tmp_path / "cache.json"
    path.write_text(
        '{"key":{"etag":"\\"old\\"","body":"old","stored_at":1,'
        '"rate_limited_until":null,"backoff_until":null,"failures":0}}'
    )
    assert UpdateCache(path).get("key") == CacheEntry()


def test_parallel_keys(tmp_path: Path) -> None:
    cache = UpdateCache(tmp_path / "cache.json")
    barrier = threading.Barrier(3)

    def write(key: str) -> None:
        barrier.wait()
        for number in range(50):
            cache.set(key, CacheEntry(body=str(number), url=f"https://{key}.example"))

    threads = [threading.Thread(target=write, args=(key,)) for key in ("a", "b")]
    for thread in threads:
        thread.start()
    barrier.wait()
    for thread in threads:
        thread.join()
    assert cache.get("a").body == "49"
    assert cache.get("b").body == "49"
