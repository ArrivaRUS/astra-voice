"""Restart preparation and launch boundaries; fake metadata/launcher, no GUI/network."""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.core import paths
from astra_voice.core.policy import Policy, PolicyStatus
from astra_voice.updates.download import FileIdentity, VerifiedDownload
from astra_voice.updates.release import VerifiedArtifact, VerifiedRelease
from astra_voice.updates.restart import (
    RestartController,
    RestartError,
    RestartPreparer,
    install_refusal,
)

pytestmark = pytest.mark.unit


class Rig:
    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.cache = tmp_path / "cache"
        monkeypatch.setenv("XDG_CACHE_HOME", str(self.cache))
        bundle = tmp_path / "bundle"
        bundle.mkdir(mode=0o700)
        self.body = b"harmless image bytes"
        artifact = VerifiedArtifact(
            "appimage",
            "Astra_Voice-0.2.0-x86_64.AppImage",
            hashlib.sha256(self.body).hexdigest(),
            len(self.body),
            "unused",
        )
        release = VerifiedRelease("0.2.0", "v0.2.0", "", "", "", artifact, b"sums", b"sig", b"json")
        image = bundle / artifact.name
        image.write_bytes(self.body)
        image.chmod(0o755)
        self.result = VerifiedDownload(image, release, FileIdentity.from_stat(image.stat()))
        self.metadata = Mock()
        self.metadata.validate_local.return_value = release
        self.policy = Policy()
        self.preparer = RestartPreparer(
            self.metadata,
            "0.1.1",
            policy=lambda: self.policy,
            record_path=tmp_path / "records" / "attempt.json",
        )
        self.popen = Mock()
        monkeypatch.setattr("astra_voice.updates.restart.subprocess.Popen", self.popen)

    def prepare(self) -> Any:
        return self.preparer.prepare(self.result, cancel=threading.Event())


def test_prepare_revalidates_and_launch_only_after_barrier(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    pending = rig.prepare()
    rig.metadata.validate_local.assert_called_once()
    rig.popen.assert_not_called()
    assert dict(pending.environment)["TMPDIR"] == str(paths.cache_dir_path() / "tmp")
    assert rig.preparer.record_path.stat().st_mode & 0o777 == 0o600
    assert rig.preparer.launch(pending, shutdown_complete=False) == 1
    rig.popen.assert_not_called()
    notice = rig.preparer.notice("0.1.1", is_appimage=True)
    assert notice is not None and notice.error == "shutdown-incomplete"
    assert rig.preparer.launch(pending, shutdown_complete=True) == 0
    args, kwargs = rig.popen.call_args
    assert args == ([str(rig.result.path), "--appimage-extract-and-run"],)
    assert kwargs["close_fds"] and kwargs["start_new_session"]
    data = json.loads(rig.preparer.record_path.read_bytes())
    assert set(data) == {"schema", "attempt_id", "target_version", "stage", "error", "bundle_id"}
    assert data["stage"] == "launch_requested"
    notice = rig.preparer.notice("0.1.1", is_appimage=True)
    assert notice is not None and notice.error == "launch-unconfirmed"
    assert rig.preparer.notice("0.2.0", is_appimage=False) is not None
    assert rig.preparer.notice("0.2.0", is_appimage=True) is None
    assert not rig.preparer.record_path.exists()


@pytest.mark.parametrize("fault", ["identity", "hash", "metadata", "symlink", "track", "cancel"])
def test_preparation_refuses_without_launcher(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    cancel = threading.Event()
    expected = "file-changed"
    if fault == "identity":
        rig.result.path.write_bytes(b"changed")
    elif fault == "hash":
        rig.result.path.write_bytes(b"x" * len(rig.body))
        rig.result = replace(rig.result, identity=FileIdentity.from_stat(rig.result.path.stat()))
        expected = "hash-mismatch"
    elif fault == "metadata":
        rig.metadata.validate_local.return_value = replace(rig.result.release, version="0.3.0")
        expected = "metadata-invalid"
    elif fault == "symlink":
        original = rig.result.path.with_suffix(".old")
        rig.result.path.rename(original)
        rig.result.path.symlink_to(original)
        expected = "path-unsafe"
    elif fault == "track":
        rig.result = replace(
            rig.result,
            release=replace(
                rig.result.release, artifact=replace(rig.result.release.artifact, track="deb")
            ),
        )
        expected = "unsupported"
    else:
        cancel.set()
        expected = "cancelled"
    with pytest.raises(RestartError, match=expected):
        rig.preparer.prepare(rig.result, cancel=cancel)
    rig.popen.assert_not_called()


@pytest.mark.parametrize("fault", ["changed", "policy", "popen"])
def test_launch_rechecks_and_records_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    pending = rig.prepare()
    if fault == "changed":
        rig.result.path.write_bytes(b"changed")
    elif fault == "policy":
        rig.policy = Policy(values={"updates": "admin"})
    else:
        rig.popen.side_effect = OSError("do not disclose path/environment")
    assert rig.preparer.launch(pending, shutdown_complete=True) == 1
    assert json.loads(rig.preparer.record_path.read_bytes())["stage"] == "launch_failed"
    if fault != "popen":
        rig.popen.assert_not_called()


def test_xdg_symlink_rejected_before_clean_env_chmod(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    foreign = tmp_path / "foreign"
    foreign.mkdir(mode=0o755)
    (foreign / "astra-voice").mkdir(mode=0o755)
    rig.cache.symlink_to(foreign, target_is_directory=True)
    before = foreign.stat().st_mode, (foreign / "astra-voice").stat().st_mode
    with pytest.raises(RestartError, match="path-unsafe"):
        rig.prepare()
    assert before == (foreign.stat().st_mode, (foreign / "astra-voice").stat().st_mode)
    assert not (foreign / "astra-voice" / "tmp").exists()


@pytest.mark.parametrize("fault", ["symlink", "oversize", "invalid", "foreign-mode"])
def test_untrusted_record_never_launches_or_mutates_foreign(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fault: str
) -> None:
    rig = Rig(tmp_path, monkeypatch)
    record = rig.preparer.record_path
    record.parent.mkdir(mode=0o700)
    data = b"x" * 4097 if fault == "oversize" else b'{"argv":["evil"],"schema":1}'
    target = record.with_suffix(".target") if fault == "symlink" else record
    target.write_bytes(data)
    target.chmod(0o644 if fault == "foreign-mode" else 0o600)
    if fault == "symlink":
        record.symlink_to(target)
    assert rig.preparer.notice("0.1.1", is_appimage=True) is None
    assert target.read_bytes() == data
    rig.popen.assert_not_called()


@pytest.mark.parametrize(
    "values,status,expected",
    [
        ({"offline": True}, PolicyStatus.ABSENT, ""),
        ({"updates": "admin"}, PolicyStatus.ABSENT, "admin"),
        ({"appimage": "deny"}, PolicyStatus.ABSENT, "appimage-denied"),
        ({}, PolicyStatus.INVALID, "policy"),
    ],
)
def test_local_policy(values: dict[str, Any], status: PolicyStatus, expected: str) -> None:
    assert install_refusal(Policy(status=status, values=values)) == expected


def test_controller_close_retains_worker_and_suppresses_callback() -> None:
    entered = threading.Event()
    release = threading.Event()
    preparer = Mock()

    def prepare(*args: Any, **kwargs: Any) -> None:
        entered.set()
        release.wait(2)

    preparer.prepare.side_effect = prepare
    callback = Mock()
    controller = RestartController(preparer, on_prepared=callback)
    assert controller.start(Mock())
    assert entered.wait(1)
    assert not controller.start(Mock())
    assert not controller.close(0)
    assert len(controller.worker_threads) == 1
    release.set()
    assert controller.close(1)
    assert not controller.worker_threads
    callback.assert_not_called()
    assert not controller.start(Mock())
