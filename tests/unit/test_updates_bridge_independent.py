"""Independent user-action/race contracts of the updates GUI boundary.

Only queued immutable values and actual private files cross this boundary.
No production downloader, verifier, launcher or network operation is permitted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from astra_voice.ui.updates_bridge import UpdatesBridge
from astra_voice.updates.checker import UpdateStatus
from astra_voice.updates.download import DownloadStatus, FileIdentity, Phase, VerifiedDownload
from astra_voice.updates.release import Track, VerifiedArtifact, VerifiedRelease

pytestmark = pytest.mark.unit


def offered(track: Track = "appimage", version: str = "1.4.2") -> VerifiedRelease:
    name = (
        f"Astra_Voice-{version}-x86_64.AppImage"
        if track == "appimage"
        else f"astra-voice_{version}_amd64.deb"
    )
    repository = "https://github.com/ArrivaRUS/astra-voice/releases/"
    return VerifiedRelease(
        version,
        "v" + version,
        "2026-10-09T12:00:00Z",
        "1.8",
        repository + "tag/v" + version,
        VerifiedArtifact(
            track, name, "a" * 64, 8, repository + "download/v" + version + "/" + name
        ),
        b"only-worker-validates-sums",
        b"only-worker-validates-signature",
        b"only-worker-validates-json",
    )


def status(value: VerifiedRelease, notes: str) -> UpdateStatus:
    return UpdateStatus(
        "available",
        value.version,
        notes,
        value.release_url,
        raw_tag=value.raw_tag,
        verified_release=value,
    )


def bundle(root: Path, value: VerifiedRelease) -> VerifiedDownload:
    folder = root / value.version
    folder.mkdir(mode=0o700)
    path = folder / value.artifact.name
    path.write_bytes(b"12345678")
    path.chmod(0o755 if value.artifact.track == "appimage" else 0o600)
    return VerifiedDownload(path, value, FileIdentity.from_stat(path.stat()))


class UIHarness:
    def __init__(self, value: VerifiedRelease) -> None:
        self.starts: list[VerifiedRelease] = []
        self.cancels = 0
        self.gates: dict[str, str] = {}
        self.install_gate = ""
        self.checker = Mock(
            spec=["check_now", "refresh", "skip_version", "clear_skip", "remind_later"]
        )
        self.opened = Mock(return_value=True)
        self.installed = Mock(return_value=True)
        self.bridge = UpdatesBridge(
            self.checker,
            controller=self,
            request_install=self.installed,
            install_refusal=lambda: self.install_gate,
            open_external=self.opened,
            refusal=lambda kind: self.gates.get(kind, ""),
        )
        self.bridge.set_status(status(value, "- Исходные сведения <img src='https://invalid/x'>"))

    def start(self, value: VerifiedRelease) -> int:
        self.starts.append(value)
        return len(self.starts)

    def cancel(self) -> None:
        self.cancels += 1

    def ready(self, result: VerifiedDownload, operation: int = 1) -> None:
        self.bridge.set_download_status(DownloadStatus(operation, "ready", result=result))


@pytest.mark.parametrize("terminal", ["ready", "error", "cancelled"])
def test_cancel_new_check_retry_and_stale_terminal_cannot_rebind_operation(
    tmp_path: Path, terminal: Phase
) -> None:
    first, newest = offered(), offered(version="1.5.0")
    case = UIHarness(first)
    bridge = case.bridge
    first_notes = list(bridge.notes)
    old_ready = bundle(tmp_path, first)
    bridge.download()
    bridge.cancelDownload()
    bridge.set_status(status(newest, "- Новая версия с другими сведениями"))
    bridge.set_download_status(DownloadStatus(1, terminal, result=old_ready, error="timeout"))
    assert bridge.downloadPhase == "cancelled"
    assert not bridge.canOpenFolder and not bridge.canInstallAndRestart
    assert case.installed.call_count == 0
    bridge.retryDownload()
    assert case.starts == [first, first]
    assert bridge.version == first.version and bridge.notes == first_notes
    stale_phases: tuple[Phase, ...] = ("downloading", "verifying", "ready", "error", "cancelled")
    for phase in stale_phases:
        bridge.set_download_status(
            DownloadStatus(1, phase, result=old_ready, error="timeout", received=8)
        )
        assert bridge.downloadPhase == "metadata"
        assert bridge.downloadReceived == 0
    case.ready(old_ready, operation=2)
    assert bridge.downloadPhase == "readyappimage"
    assert bridge.version == first.version and bridge.notes == first_notes
    bridge.openReleasePage()
    case.opened.assert_called_once_with(first.release_url)
    assert case.installed.call_count == 0


@pytest.mark.parametrize("network_phase", ["metadata", "downloading"])
@pytest.mark.parametrize("refusal", ["offline", "policy"])
def test_gate_change_cancel_then_unlock_requires_manual_retry(
    tmp_path: Path, network_phase: str, refusal: str
) -> None:
    value = offered("deb")
    case = UIHarness(value)
    bridge = case.bridge
    bridge.download()
    if network_phase == "downloading":
        bridge.set_download_status(DownloadStatus(1, "downloading", received=4))
    case.gates.update(download=refusal, check_app_manual=refusal)
    bridge.refreshCapabilities()
    assert case.cancels == 1 and bridge.downloadCancelling
    case.gates.clear()
    bridge.refreshCapabilities()
    assert len(case.starts) == 1
    case.ready(bundle(tmp_path, value))
    assert bridge.downloadPhase == "cancelled"
    assert not bridge.canOpenFolder
    assert len(case.starts) == 1
    bridge.retryDownload()
    assert case.starts == [value, value]


@pytest.mark.parametrize("track", ["deb", "appimage"])
def test_offline_ready_stays_local_but_current_install_policy_is_authoritative(
    tmp_path: Path, track: Track
) -> None:
    value = offered(track)
    case = UIHarness(value)
    bridge = case.bridge
    bridge.download()
    verified = bundle(tmp_path, value)
    case.ready(verified)
    case.gates.update(download="offline", check_app_manual="offline", check_app="offline")
    case.install_gate = "admin"
    bridge.refreshCapabilities()
    assert not bridge.canCheckNow and not bridge.canDownload and not bridge.releasePageAvailable
    assert bridge.canOpenFolder
    assert bridge.downloadPhase == "ready" + track
    bridge.checkNow()
    bridge.download()
    bridge.openReleasePage()
    bridge.installAndRestart()
    case.checker.check_now.assert_not_called()
    case.installed.assert_not_called()
    bridge.openFolder()
    case.opened.assert_called_once_with(verified.path.parent.as_uri())
    case.install_gate = ""
    bridge.refreshCapabilities()
    assert len(case.starts) == 1 and case.installed.call_count == 0
    assert bridge.canInstallAndRestart == (track == "appimage")
    if track == "appimage":
        bridge.installAndRestart()
        case.installed.assert_called_once_with(verified)
        assert bridge.downloadPhase == "installing"


@pytest.mark.parametrize("retryable", [False, True])
@pytest.mark.parametrize("file_change", ["unchanged", "missing", "swapped"])
def test_failed_install_retry_needs_explicit_action_authorization_and_original_file(
    tmp_path: Path, retryable: bool, file_change: str
) -> None:
    value = offered()
    case = UIHarness(value)
    bridge = case.bridge
    bridge.download()
    verified = bundle(tmp_path, value)
    case.ready(verified)
    bridge.installAndRestart()
    bridge.set_install_error("prepare-failed", retryable=retryable)
    bridge.set_status(status(offered(version="1.5.0"), "- Новый выпуск"))
    bridge.refreshCapabilities()
    assert case.installed.call_count == 1 and bridge.downloadPhase == "error"
    if file_change == "missing":
        verified.path.unlink()
    elif file_change == "swapped":
        replacement = verified.path.parent / "replacement"
        replacement.write_bytes(b"12345678")
        replacement.chmod(0o755)
        replacement.replace(verified.path)
    bridge.installAndRestart()
    allowed = retryable and file_change == "unchanged"
    assert case.installed.call_count == (2 if allowed else 1)
    assert bridge.downloadPhase == ("installing" if allowed else "error")
    if file_change != "unchanged":
        assert bridge.downloadError == "file-changed"
        assert not bridge.canOpenFolder and not bridge.canInstallAndRestart


def test_gui_properties_and_slots_never_do_hash_gpg_http_or_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = offered()
    verified = bundle(tmp_path, value)
    case = UIHarness(value)
    bridge = case.bridge

    def forbidden(*args: Any, **kwargs: Any) -> Any:
        pytest.fail("GUI boundary performed HTTP, cryptographic verification or process launch")

    for target in (
        "astra_voice.net.http.HttpClient.get_stream",
        "astra_voice.updates.release.ReleaseMetadata.validate_local",
        "astra_voice.security.verify.Verifier.verify_detached",
        "astra_voice.security.verify.Verifier.verify_sha256sums",
        "astra_voice.updates.download.ReleaseDownloader.download",
        "hashlib.sha256",
        "subprocess.Popen",
    ):
        monkeypatch.setattr(target, forbidden)
    bridge.checkNow()
    bridge.openReleasePage()
    bridge.download()
    bridge.download()
    bridge.set_download_status(DownloadStatus(1, "verifying", received=8))
    assert not bridge.canOpenFolder and not bridge.canInstallAndRestart
    case.ready(verified)
    for _ in range(3):
        bridge.refreshCapabilities()
        assert bridge.folderPath and bridge.artifactSize == 8
        assert bridge.downloadPhase == "readyappimage" and bridge.canInstallAndRestart
        bridge.openFolder()
    bridge.installAndRestart()
    assert case.starts == [value]
    case.installed.assert_called_once_with(verified)


def test_candidate_changed_signal_never_exposes_new_release_with_old_notes() -> None:
    old, new = offered(), offered(version="1.5.0")
    case = UIHarness(old)
    bridge = case.bridge
    observed: list[tuple[str, list[dict[str, object]]]] = []

    def start_when_offer_changes() -> None:
        if bridge.canDownload and not case.starts:
            bridge.download()
            observed.append((bridge.version, list(bridge.notes)))

    bridge.downloadChanged.connect(start_when_offer_changes)
    bridge.set_status(status(new, "- Новые сведения"))
    assert case.starts == [new]
    assert observed == [(new.version, [{"text": "Новые сведения", "bullet": True}])]
