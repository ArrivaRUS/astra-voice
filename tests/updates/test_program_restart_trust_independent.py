"""Independent restart trust chain with genuine disposable OpenPGP signatures.

The only HTTP boundary is forbidden. A harmless signed Python script stands in
for an AppImage runtime; it does not install, open a GUI or access user services.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import subprocess
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.core import paths
from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import HttpClient
from astra_voice.updates.download import FileIdentity, VerifiedDownload
from astra_voice.updates.release import ReleaseMetadata
from astra_voice.updates.restart import PendingRestart, RestartError, RestartPreparer

# Share the existing temporary-key fixture without duplicate mypy module discovery.
_trust = importlib.import_module("updates.test_release_trust_independent")
signing_keys = _trust.signing_keys

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        shutil.which("gpg") is None or shutil.which("gpgv") is None,
        reason="real restart trust requires gpg and gpgv",
    ),
]


class SignedRestart:
    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch, keys: Any) -> None:
        monkeypatch.setenv("XDG_CACHE_HOME", str(root / "cache"))
        self.output = root / "child-result.json"
        self.lock_path = root / "instance.lock"
        self.ipc_path = root / "instance.socket"
        self.probe_fd = -1
        self.policy = Policy()
        self.body = (
            "#!/usr/bin/python3\n"
            "import fcntl,json,os,sys\n"
            f"lock=open({str(self.lock_path)!r},'a')\n"
            "try:\n fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB); free=True\n"
            "except BlockingIOError:\n free=False\n"
            "fd=int(os.environ.get('TEST_RESTART_FD','-1'))\n"
            "try:\n os.fstat(fd); inherited=True\n"
            "except OSError:\n inherited=False\n"
            "report={'argv':sys.argv,'tmp':os.environ['TMPDIR'],"
            "'private_mode':os.stat(os.environ['TMPDIR']).st_mode & 511,"
            "'lock_free':free,'fd_inherited':inherited,"
            f"'ipc_absent':not os.path.exists({str(self.ipc_path)!r}),"
            "'session':os.getsid(0)==os.getpid(),"
            "'bundle_vars':[n for n in os.environ if n.startswith(('ASTRA_VOICE_', 'APPIMAGE'))],"
            "'pythonhome':os.environ.get('PYTHONHOME')}\n"
            f"open({str(self.output)!r},'w').write(json.dumps(report))\n"
        ).encode()
        data = _trust.manifest()
        artifact = data["artifacts"]["appimage"]
        artifact.update(sha256=hashlib.sha256(self.body).hexdigest(), size=len(self.body))
        latest = json.dumps(data).encode()
        sums = (
            f"{hashlib.sha256(latest).hexdigest()}  latest.json\n"
            f"{'a' * 64}  astra-voice_0.2.1_amd64.deb\n"
            f"{artifact['sha256']}  {artifact['name']}\n"
        ).encode()
        self.bundle = root / "complete"
        _trust.write_cache(
            self.bundle,
            {"SHA256SUMS": sums, "SHA256SUMS.asc": keys.sign(sums), "latest.json": latest},
        )
        self.image = self.bundle / artifact["name"]
        self.image.write_bytes(self.body)
        self.image.chmod(0o755)
        client = HttpClient(
            NetworkGate(Settings(offline=True), Policy()), user_agent="test-restart"
        )

        def no_http(*args: Any, **kwargs: Any) -> Any:
            pytest.fail("local restart attempted HTTP")

        monkeypatch.setattr(client, "get_stream", no_http)
        self.verifier = keys.verifier()
        self.metadata = ReleaseMetadata(client, self.verifier, staging_dir=root / "metadata")
        release = self.metadata.validate_local(self.bundle, "v0.2.1", "0.1.1~dev10", "appimage")
        self.download = VerifiedDownload(
            self.image, release, FileIdentity.from_stat(self.image.stat())
        )
        self.preparer = RestartPreparer(
            self.metadata,
            "0.1.1~dev10",
            policy=lambda: self.policy,
            record_path=root / "records" / "attempt.json",
        )

    def prepare(self) -> PendingRestart:
        return self.preparer.prepare(self.download, cancel=threading.Event())


@pytest.fixture
def signed_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, signing_keys: Any
) -> SignedRestart:
    return SignedRestart(tmp_path, monkeypatch, signing_keys)


def test_real_signature_json_hash_and_package_hash_prepare_offline(
    signed_restart: SignedRestart,
) -> None:
    case = signed_restart
    pending = case.prepare()
    assert pending.download == case.download
    assert pending.download.path.read_bytes() == case.body
    record = json.loads(case.preparer.record_path.read_bytes())
    assert set(record) == {"schema", "attempt_id", "target_version", "stage", "error", "bundle_id"}
    assert record["stage"] == "prepared" and record["error"] == ""
    assert "argv" not in record and "env" not in record
    assert case.preparer.record_path.stat().st_mode & 0o777 == 0o600
    assert Path(dict(pending.environment)["TMPDIR"]).stat().st_mode & 0o777 == 0o700
    assert not case.output.exists()


@pytest.mark.parametrize("tamper", ["foreign-signature", "latest", "body", "replacement"])
def test_prepare_rechecks_actual_local_trust_despite_previous_verified_download(
    signed_restart: SignedRestart, signing_keys: Any, tamper: str
) -> None:
    case = signed_restart
    expected = "signature-invalid"
    if tamper == "foreign-signature":
        sums = (case.bundle / "SHA256SUMS").read_bytes()
        (case.bundle / "SHA256SUMS.asc").write_bytes(signing_keys.sign(sums, foreign=True))
    elif tamper == "latest":
        with (case.bundle / "latest.json").open("ab") as stream:
            stream.write(b" ")
        expected = "hash-mismatch"
    elif tamper == "body":
        case.image.write_bytes(b"x" * len(case.body))
        # Preserve a self-consistent caller identity: signed digest still must win.
        case.download = replace(case.download, identity=FileIdentity.from_stat(case.image.stat()))
        expected = "hash-mismatch"
    else:
        replacement = case.bundle / "replacement"
        replacement.write_bytes(case.body)
        replacement.chmod(0o755)
        replacement.replace(case.image)
        expected = "file-changed"
    with pytest.raises(RestartError) as failure:
        case.prepare()
    assert failure.value.code == expected
    assert not case.preparer.record_path.exists() and not case.output.exists()


@pytest.mark.parametrize("action", ["cancel", "policy"])
def test_cancel_or_policy_after_real_gpg_success_cannot_prepare(
    signed_restart: SignedRestart, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    case = signed_restart
    cancel = threading.Event()
    verify = case.verifier.verify_detached

    def after_verify(*args: Any, **kwargs: Any) -> Any:
        verified = verify(*args, **kwargs)
        assert verified.ok
        if action == "cancel":
            cancel.set()
        else:
            case.policy = Policy(values={"updates": "admin"})
        return verified

    monkeypatch.setattr(case.verifier, "verify_detached", after_verify)
    with pytest.raises(RestartError) as failure:
        case.preparer.prepare(case.download, cancel=cancel)
    assert failure.value.code == ("cancelled" if action == "cancel" else "admin")
    assert not case.preparer.record_path.exists() and not case.output.exists()


@pytest.mark.parametrize("tamper", ["body", "replace", "permissions", "policy"])
def test_change_after_real_prepare_prevents_launch(
    signed_restart: SignedRestart, monkeypatch: pytest.MonkeyPatch, tamper: str
) -> None:
    case = signed_restart
    pending = case.prepare()
    if tamper == "body":
        case.image.write_bytes(b"x" * len(case.body))
    elif tamper == "replace":
        replacement = case.bundle / "replacement"
        replacement.write_bytes(case.body)
        replacement.chmod(0o755)
        replacement.replace(case.image)
    elif tamper == "permissions":
        case.image.chmod(0o644)
    else:
        case.policy = Policy(values={"appimage": "deny"})
    launch = Mock()
    monkeypatch.setattr(subprocess, "Popen", launch)
    assert case.preparer.launch(pending, shutdown_complete=True) == 1
    launch.assert_not_called()
    record = json.loads(case.preparer.record_path.read_bytes())
    assert record["stage"] == "launch_failed"
    assert record["error"] == ("appimage-denied" if tamper == "policy" else "file-changed")
    assert not case.output.exists()


def test_tmp_symlink_refused_without_chmod_of_target(signed_restart: SignedRestart) -> None:
    case = signed_restart
    cache = paths.cache_dir_path()
    cache.mkdir(mode=0o700, parents=True)
    target = case.bundle.parent / "foreign-temp"
    target.mkdir(mode=0o705)
    sentinel = target / "untouched"
    sentinel.write_bytes(b"foreign temporary files")
    (cache / "tmp").symlink_to(target, target_is_directory=True)
    with pytest.raises(RestartError, match="path-unsafe"):
        case.prepare()
    assert target.stat().st_mode & 0o777 == 0o705
    assert sentinel.read_bytes() == b"foreign temporary files"
    assert not case.preparer.record_path.exists()


def test_diagnostic_record_never_authorizes_shutdown_or_command(
    signed_restart: SignedRestart, monkeypatch: pytest.MonkeyPatch
) -> None:
    case = signed_restart
    pending = case.prepare()
    record = json.loads(case.preparer.record_path.read_bytes())
    record.update(stage="launch_requested", argv=["/bin/false"], env={"TMPDIR": "/tmp"})
    case.preparer.record_path.write_text(json.dumps(record))
    assert case.preparer.notice("0.1.1", is_appimage=True) is None
    launch = Mock()
    monkeypatch.setattr(subprocess, "Popen", launch)
    assert case.preparer.launch(pending, shutdown_complete=False) == 1
    launch.assert_not_called()
    repaired = json.loads(case.preparer.record_path.read_bytes())
    assert repaired["stage"] == "shutdown_failed" and repaired["error"] == "shutdown-incomplete"
    assert "argv" not in repaired and "env" not in repaired
    assert case.image.read_bytes() == case.body
