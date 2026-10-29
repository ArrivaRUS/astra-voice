"""Shared cache/private-file helper compatibility (E13 review item 3)."""

from __future__ import annotations

import errno
import os
from pathlib import Path
from unittest.mock import Mock

import pytest

from astra_voice.net.update_cache import CacheEntry, UpdateCache, read_private, write_private

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_read_private_rejects_type_before_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, kind: str
) -> None:
    target = tmp_path / "target"
    target.write_bytes(b"unchanged")
    path = tmp_path / "entry"
    if kind == "symlink":
        path.symlink_to(target)
    else:
        os.mkfifo(path, mode=0o600)
    open_spy = Mock(wraps=os.open)
    monkeypatch.setattr(os, "open", open_spy)
    with pytest.raises(ValueError, match="тип файла"):
        read_private(path, 100)
    open_spy.assert_not_called()
    assert target.read_bytes() == b"unchanged"


def test_read_private_symlink_swap_returns_legacy_value_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "entry"
    path.write_bytes(b"data")
    monkeypatch.setattr(os, "open", Mock(side_effect=OSError(errno.ELOOP, "PRIVATE_MARKER")))
    with pytest.raises(ValueError, match="^тип файла$"):
        read_private(path, 100)


def test_read_private_short_reads_and_bounds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "entry"
    write_private(path, b"abcdef")
    original = os.read
    monkeypatch.setattr(os, "read", lambda fd, size: original(fd, min(size, 2)))
    assert read_private(path, 6) == b"abcdef"
    with pytest.raises(ValueError, match="размер"):
        read_private(path, 5)


def test_update_cache_still_roundtrips(tmp_path: Path) -> None:
    cache = UpdateCache(tmp_path / "cache.json")
    entry = CacheEntry(etag='"abc"', body="body", stored_at=12)
    cache.set("test", entry)
    assert cache.get("test") == entry
