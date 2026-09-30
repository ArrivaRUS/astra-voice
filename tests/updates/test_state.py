"""`updates/state.py`: раздельные даты и выбор пользователя в update-state.json."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from astra_voice.updates.state import SourceState, StateStore

pytestmark = pytest.mark.unit


def test_roundtrip_private_file(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "state" / "update-state.json")
    assert store.get("app") == SourceState()
    state = SourceState(
        last_attempt_at=2.0, last_success_at=1.0, skipped_version="0.2.1", remind_until=3.0
    )
    store.set("app", state)
    assert StateStore(store.path).get("app") == state
    assert stat.S_IMODE(os.stat(store.path).st_mode) == 0o600
    assert [p.name for p in store.path.parent.iterdir()] == ["update-state.json"]


@pytest.mark.parametrize(
    "raw",
    [
        b"",
        b"{",
        b"[]",
        b'{"schema_version": 2, "sources": {}}',
        b'{"schema_version": 1, "sources": {"hf": {}}}',
        b'{"schema_version": 1, "sources": {"app": {"last_attempt_at": "1"}}}',
        json.dumps(
            {
                "schema_version": 1,
                "sources": {
                    "app": {
                        "last_attempt_at": float("nan"),
                        "last_success_at": None,
                        "skipped_version": None,
                        "remind_until": None,
                    }
                },
            }
        ).encode(),
        json.dumps(
            {
                "schema_version": 1,
                "sources": {
                    "app": {
                        "last_attempt_at": None,
                        "last_success_at": None,
                        "skipped_version": "../x",
                        "remind_until": None,
                    }
                },
            }
        ).encode(),
        b"x" * (64 * 1024 + 1),
    ],
)
def test_bad_file_is_empty_state(tmp_path: Path, raw: bytes) -> None:
    path = tmp_path / "update-state.json"
    path.write_bytes(raw)
    assert StateStore(path).get("app") == SourceState()


def test_symlink_not_followed(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    store = StateStore(tmp_path / "update-state.json")
    store.set("app", SourceState(last_attempt_at=1.0))
    store.path.rename(target)
    store.path.symlink_to(target)
    assert store.get("app") == SourceState()


def test_invalid_state_rejected(tmp_path: Path) -> None:
    store = StateStore(tmp_path / "update-state.json")
    with pytest.raises(ValueError):
        store.set("hf", SourceState())
    with pytest.raises(ValueError):
        store.set("app", SourceState(skipped_version="latest"))
    with pytest.raises(ValueError):
        store.set("app", SourceState(last_attempt_at=-1.0))
    assert not store.path.exists()
