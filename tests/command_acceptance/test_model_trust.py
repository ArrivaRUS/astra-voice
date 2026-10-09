"""Trust gate tied to captured worker generation and actual ModelRecord/Settings types."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from astra_voice.core.settings import from_dict
from astra_voice.models.store import ModelRecord
from astra_voice.runtime import DictationRuntime

pytestmark = pytest.mark.unit


class Rig:
    """Only data ports; never construct/start runtime, transport, hotkey or microphone."""

    def __init__(self, directory: Path) -> None:
        self.record: ModelRecord | None = ModelRecord(
            "catalog-model", "revision-1", directory, "flat", "rnnt", 123
        )
        request = {"id": "catalog-model", "revision": "revision-1", "dir": str(directory)}
        self.runtime = SimpleNamespace(
            settings=from_dict({}),
            _model_load_request=request,
            _model_load_generation=7,
            _command_capture_model=(7, dict(request)),
            supervisor=SimpleNamespace(generation=7),
            model_store=SimpleNamespace(current=lambda: self.record),
            revocation_unknown=False,
            _revoked_check=lambda model_id, revision: False,
        )

    def trusted(self) -> bool:
        return DictationRuntime._command_model_trusted(cast(Any, self.runtime))


def test_verified_store_model_does_not_depend_on_remaining_in_current_catalog(
    tmp_path: Path,
) -> None:
    rig = Rig(tmp_path)
    assert rig.trusted()  # no catalog lookup; installed signed revision is the source of truth


@pytest.mark.parametrize("raw", ["/tmp/manual-model", "", False])
def test_any_manual_model_override_is_untrusted(tmp_path: Path, raw: object) -> None:
    rig = Rig(tmp_path)
    rig.runtime.settings = from_dict({"model_dir": raw})
    assert not rig.trusted()


@pytest.mark.parametrize("case", ["restarted", "load_generation", "request_changed", "no_capture"])
def test_current_loaded_and_captured_generation_and_identity_must_match(
    tmp_path: Path, case: str
) -> None:
    rig = Rig(tmp_path)
    if case == "restarted":
        rig.runtime.supervisor.generation = 8
    elif case == "load_generation":
        rig.runtime._model_load_generation = 6
    elif case == "request_changed":
        rig.runtime._model_load_request = {
            **rig.runtime._model_load_request,
            "revision": "revision-2",
        }
    else:
        rig.runtime._command_capture_model = None
    assert not rig.trusted()


@pytest.mark.parametrize(
    "changes",
    [
        {"state": "broken"},
        {"recheck": True},
        {"metadata_ok": False},
        {"id": "another-model"},
        {"revision": "another-revision"},
    ],
)
def test_store_record_must_be_verified_and_match_load_request(
    tmp_path: Path, changes: dict[str, Any]
) -> None:
    rig = Rig(tmp_path)
    assert rig.record is not None
    rig.record = replace(rig.record, **changes)
    assert not rig.trusted()


def test_store_directory_must_match_and_realpath_alias_is_allowed(tmp_path: Path) -> None:
    actual = tmp_path / "actual"
    actual.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(actual, target_is_directory=True)
    rig = Rig(actual)
    rig.runtime._model_load_request["dir"] = str(alias)
    rig.runtime._command_capture_model = (7, dict(rig.runtime._model_load_request))
    assert rig.trusted()
    assert rig.record is not None
    rig.record = replace(rig.record, dir=tmp_path / "different")
    assert not rig.trusted()


@pytest.mark.parametrize("case", ["unknown", "revoked", "no_checker", "no_store", "no_record"])
def test_missing_trust_evidence_fails_closed(tmp_path: Path, case: str) -> None:
    rig = Rig(tmp_path)
    if case == "unknown":
        rig.runtime.revocation_unknown = True
    elif case == "revoked":
        rig.runtime._revoked_check = lambda model_id, revision: True
    elif case == "no_checker":
        rig.runtime._revoked_check = None
    elif case == "no_store":
        rig.runtime.model_store = None
    else:
        rig.record = None
    assert not rig.trusted()


def test_store_failure_cannot_open_trust_gate(tmp_path: Path) -> None:
    rig = Rig(tmp_path)

    def unreadable() -> None:
        raise RuntimeError("PRIVATE_MODEL_RECORD_SENTENCE")

    rig.runtime.model_store.current = unreadable
    assert not rig.trusted()
