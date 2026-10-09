"""Download bridge scenarios: operation races, separate gates and local file identity."""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from threading import Thread
from unittest.mock import Mock

import pytest
from PyQt5.QtTest import QSignalSpy

from astra_voice.ui.updates_bridge import UpdatesBridge
from astra_voice.updates.checker import UpdateStatus
from astra_voice.updates.download import (
    DownloadError,
    DownloadStatus,
    FileIdentity,
    VerifiedDownload,
)
from astra_voice.updates.release import Track, VerifiedArtifact, VerifiedRelease

pytestmark = pytest.mark.unit


def release(track: Track = "deb", version: str = "1.2.3", size: int = 7) -> VerifiedRelease:
    name = (
        f"astra-voice_{version}_amd64.deb"
        if track == "deb"
        else f"Astra_Voice-{version}-x86_64.AppImage"
    )
    base = "https://github.com/ArrivaRUS/astra-voice/releases/"
    return VerifiedRelease(
        version,
        "v" + version,
        "2026-10-09T12:00:00Z",
        "1.8",
        base + "tag/v" + version,
        VerifiedArtifact(track, name, "a" * 64, size, base + "download/v" + version + "/" + name),
        b"sums",
        b"sig",
        b"latest",
    )


def found(value: VerifiedRelease, notes: str = "- Описание") -> UpdateStatus:
    return UpdateStatus(
        "available",
        value.version,
        notes,
        value.release_url,
        raw_tag=value.raw_tag,
        verified_release=value,
    )


class Controller:
    def __init__(self) -> None:
        self.started: list[VerifiedRelease] = []
        self.cancelled = 0
        self.inline: Callable[[int], None] | None = None
        self.error: DownloadError | None = None

    def start(self, value: VerifiedRelease) -> int:
        if self.error:
            raise self.error
        self.started.append(value)
        operation_id = len(self.started)
        if self.inline:
            self.inline(operation_id)
        return operation_id

    def cancel(self) -> None:
        self.cancelled += 1


def bridge_with(
    value: VerifiedRelease | None = None,
    *,
    refusals: dict[str, str] | None = None,
    install: Mock | None = None,
    install_refusal: Callable[[], str] | None = None,
    now: list[float] | None = None,
) -> tuple[UpdatesBridge, Controller, Mock, Mock]:
    controller = Controller()
    checker = Mock(spec=["check_now", "refresh", "skip_version", "clear_skip", "remind_later"])
    opener = Mock(return_value=True)
    table = refusals if refusals is not None else {}
    current_time = now if now is not None else [1000.0]
    bridge = UpdatesBridge(
        checker,
        controller=controller,
        open_external=opener,
        refusal=lambda kind: table.get(kind, ""),
        clock=lambda: current_time[0],
        request_install=install,
        install_refusal=install_refusal,
    )
    if value:
        bridge.set_status(found(value))
    return bridge, controller, checker, opener


def ready_file(tmp_path: Path, value: VerifiedRelease) -> VerifiedDownload:
    folder = tmp_path / "complete"
    folder.mkdir(mode=0o700)
    path = folder / value.artifact.name
    path.write_bytes(b"package")
    path.chmod(0o600 if value.artifact.track == "deb" else 0o755)
    return VerifiedDownload(path, value, FileIdentity.from_stat(path.lstat()))


def publish_ready(bridge: UpdatesBridge, result: VerifiedDownload, operation_id: int = 1) -> None:
    bridge.set_download_status(DownloadStatus(operation_id, "ready", result=result))


def test_only_explicit_download_uses_trusted_candidate() -> None:
    value = release()
    bridge, controller, _, _ = bridge_with()
    bridge.set_status(UpdateStatus("available", value.version, raw_tag=value.raw_tag))
    assert not bridge.canDownload
    bridge.download()
    assert controller.started == []
    bridge.set_status(found(value))
    assert bridge.canDownload
    assert controller.started == []
    bridge.download()
    assert controller.started == [value]
    assert bridge.downloadPhase == "metadata"


@pytest.mark.parametrize("change", ["version", "raw_tag", "state"])
def test_discovery_identity_mismatch_never_offers_download(change: str) -> None:
    bridge, controller, _, _ = bridge_with()
    status = found(release())
    if change == "version":
        status = replace(status, version="9.0.0")
    elif change == "raw_tag":
        status = replace(status, raw_tag="v9.0.0")
    else:
        status = replace(status, state="unavailable")
    bridge.set_status(status)
    bridge.download()
    assert not bridge.canDownload
    assert controller.started == []


def test_same_panel_fields_can_gain_trust_and_emit_download_signal() -> None:
    bridge, _, _, _ = bridge_with()
    value = release()
    status = found(value)
    bridge.set_status(replace(status, verified_release=None))
    spy = QSignalSpy(bridge.downloadChanged)
    bridge.set_status(status)
    assert bridge.canDownload
    assert len(spy) == 1


def test_operation_pins_notes_version_size_and_page_across_checker_changes() -> None:
    old = release()
    bridge, controller, checker, opener = bridge_with(old)
    bridge.download()
    bridge.set_status(found(release(version="2.0.0", size=90), "- <b>Новое</b>"))
    bridge.checkNow()
    bridge.skipVersion()
    bridge.remindLater()
    bridge.clearSkip()
    assert bridge.state == "available"
    assert bridge.version == old.version
    assert bridge.notes == [{"text": "Описание", "bullet": True}]
    assert bridge.artifactSize == old.artifact.size
    assert not bridge.canSkipVersion and not bridge.canRemindLater
    checker.check_now.assert_called_once()
    checker.clear_skip.assert_called_once()
    checker.skip_version.assert_not_called()
    checker.remind_later.assert_not_called()
    bridge.openReleasePage()
    opener.assert_called_once_with(old.release_url)
    bridge.download()
    assert controller.started == [old]


def test_progress_uses_signed_size_and_cannot_regress_or_become_ready_at_100() -> None:
    bridge, _, _, _ = bridge_with(release(size=2 << 30))
    bridge.download()
    bridge.set_download_status(DownloadStatus(1, "downloading", 1 << 30, 3))
    assert bridge.downloadPercent == 50
    assert bridge.property("artifactSize") == 2 << 30
    assert bridge.property("downloadTotal") == 2 << 30
    bridge.set_download_status(DownloadStatus(1, "downloading", 2 << 30, 3))
    assert bridge.downloadPercent == 100
    assert bridge.downloadPhase == "downloading"
    bridge.set_download_status(DownloadStatus(1, "verifying", 2 << 30))
    bridge.set_download_status(DownloadStatus(1, "checking", 0))
    assert bridge.downloadPhase == "verifying"
    assert bridge.downloadReceived == 2 << 30
    assert not bridge.canOpenFolder


def test_inline_controller_callback_is_delivered_after_start_id(tmp_path: Path) -> None:
    value = release()
    result = ready_file(tmp_path, value)
    bridge, controller, _, _ = bridge_with(value)
    controller.inline = lambda number: publish_ready(bridge, result, number)
    bridge.download()
    assert bridge.downloadPhase == "readydeb"
    assert bridge.canOpenFolder


def test_cancel_waits_for_worker_and_late_ready_cannot_resurrect_file(tmp_path: Path) -> None:
    value = release()
    bridge, controller, _, _ = bridge_with(value)
    bridge.download()
    bridge.cancelDownload()
    bridge.cancelDownload()
    bridge.retryDownload()
    assert controller.cancelled == 1
    assert len(controller.started) == 1
    assert bridge.downloadBusy and bridge.downloadCancelling
    assert not bridge.canCancelDownload
    bridge.set_download_status(DownloadStatus(1, "downloading", 4))
    assert bridge.downloadReceived == 0
    publish_ready(bridge, ready_file(tmp_path, value))
    assert bridge.downloadPhase == "cancelled"
    assert not bridge.canOpenFolder
    bridge.retryDownload()
    bridge.set_download_status(DownloadStatus(1, "error", error="hash-mismatch"))
    bridge.set_download_status(DownloadStatus(1, "ready"))
    assert len(controller.started) == 2
    assert bridge.downloadPhase == "metadata"
    bridge.set_download_status(DownloadStatus(2, "downloading", 2))
    assert bridge.downloadReceived == 2


def test_terminal_ready_ignores_late_progress_and_duplicate_failure(tmp_path: Path) -> None:
    value = release()
    bridge, _, _, _ = bridge_with(value)
    bridge.download()
    publish_ready(bridge, ready_file(tmp_path, value))
    bridge.set_download_status(DownloadStatus(1, "downloading", 1))
    bridge.set_download_status(DownloadStatus(1, "error", error="hash-mismatch"))
    assert bridge.downloadPhase == "readydeb"


@pytest.mark.parametrize("phase", ["checking", "downloading", "verifying"])
def test_offline_cancels_only_phases_needing_network(phase: str) -> None:
    table: dict[str, str] = {}
    bridge, controller, _, _ = bridge_with(release(), refusals=table)
    bridge.download()
    bridge.set_download_status(DownloadStatus(1, phase))  # type: ignore[arg-type]
    table["download"] = "offline"
    bridge.refreshCapabilities()
    assert controller.cancelled == (0 if phase == "verifying" else 1)
    table.clear()
    bridge.refreshCapabilities()
    assert len(controller.started) == 1


def test_manual_and_download_gate_are_separate_and_auto_off_does_not_cancel() -> None:
    table = {"check_app": "auto-disabled", "check_app_manual": "", "download": "admin"}
    bridge, controller, checker, _ = bridge_with(release(), refusals=table)
    bridge.checkNow()
    bridge.download()
    checker.check_now.assert_called_once()
    assert controller.started == []
    table["download"] = ""
    bridge.download()
    bridge.refreshCapabilities()
    assert controller.cancelled == 0


def test_release_page_rechecks_network_at_click() -> None:
    table: dict[str, str] = {}
    bridge, _, _, opener = bridge_with(release(), refusals=table)
    table["download"] = "offline"
    bridge.openReleasePage()
    assert not bridge.releasePageAvailable
    opener.assert_not_called()


@pytest.mark.parametrize("track", ["deb", "appimage"])
def test_ready_offline_folder_uses_only_injected_uri_opener(tmp_path: Path, track: Track) -> None:
    value = release(track)
    result = ready_file(tmp_path, value)
    table: dict[str, str] = {}
    bridge, _, _, opener = bridge_with(value, refusals=table)
    bridge.download()
    publish_ready(bridge, result)
    table["download"] = "offline"
    bridge.refreshCapabilities()
    assert bridge.downloadPhase == "ready" + track
    assert bridge.canOpenFolder
    bridge.openFolder()
    opener.assert_called_once_with(result.path.parent.as_uri())
    if track == "deb":
        assert bridge.adminInstruction == "sudo apt install ./astra-voice_1.2.3_amd64.deb"
    else:
        assert bridge.adminInstruction == ""
        assert not bridge.canInstallAndRestart
        assert bridge.installRefusal == "unsupported"


@pytest.mark.parametrize(
    "mutation",
    ["delete", "content", "replace", "chmod", "hardlink", "symlink", "parent-mode", "timestamps"],
)
def test_changed_ready_file_disables_folder_and_install_before_action(
    tmp_path: Path, mutation: str
) -> None:
    value = release("appimage")
    result = ready_file(tmp_path, value)
    install = Mock(return_value=True)
    bridge, _, _, opener = bridge_with(value, install=install)
    bridge.download()
    publish_ready(bridge, result)
    assert bridge.canInstallAndRestart
    path = result.path
    if mutation == "delete":
        path.unlink()
    elif mutation == "content":
        path.write_bytes(b"changed content")
    elif mutation == "replace":
        other = path.parent / "replacement"
        other.write_bytes(b"package")
        other.chmod(0o755)
        other.replace(path)
    elif mutation == "chmod":
        path.chmod(0o644)
    elif mutation == "hardlink":
        os.link(path, path.parent / "alias")
    elif mutation == "symlink":
        path.unlink()
        path.symlink_to(tmp_path / "foreign")
    elif mutation == "timestamps":
        os.utime(path, ns=(result.identity.mtime_ns, result.identity.mtime_ns + 1000000000))
    else:
        path.parent.chmod(0o755)
    assert bridge.downloadPhase == "error"
    assert bridge.downloadError == "file-changed"
    assert not bridge.canOpenFolder and not bridge.canInstallAndRestart
    assert bridge.folderPath == ""
    bridge.openFolder()
    bridge.installAndRestart()
    opener.assert_not_called()
    install.assert_not_called()
    assert bridge.canRetryDownload


@pytest.mark.parametrize("forgery", ["release", "basename", "identity", "relative", "ancestor"])
def test_ready_callback_requires_matching_release_and_safe_provenance(
    tmp_path: Path, forgery: str
) -> None:
    value = release()
    result = ready_file(tmp_path, value)
    if forgery == "release":
        result = replace(result, release=release(version="2.0.0"))
    elif forgery == "basename":
        result = replace(result, path=result.path.parent / "unknown.deb")
    elif forgery == "identity":
        result = replace(result, identity=replace(result.identity, ino=0))
    elif forgery == "relative":
        result = replace(result, path=Path("complete") / result.path.name)
    else:
        alias = tmp_path / "alias"
        alias.symlink_to(result.path.parent, target_is_directory=True)
        result = replace(result, path=alias / result.path.name)
    bridge, _, _, opener = bridge_with(value)
    bridge.download()
    publish_ready(bridge, result)
    assert bridge.downloadPhase == "error"
    assert not bridge.canOpenFolder
    bridge.openFolder()
    opener.assert_not_called()


@pytest.mark.parametrize("code", ["admin", "appimage-denied", "busy", "policy", "<b>evil</b>"])
def test_install_refusal_is_independent_of_network_and_closed(tmp_path: Path, code: str) -> None:
    value = release("appimage")
    install = Mock(return_value=True)
    bridge, _, _, _ = bridge_with(value, install=install, install_refusal=lambda: code)
    bridge.download()
    publish_ready(bridge, ready_file(tmp_path, value))
    assert bridge.installRefusal == ("unsupported" if code.startswith("<") else code)
    assert not bridge.canInstallAndRestart
    assert bridge.canOpenFolder
    bridge.installAndRestart()
    install.assert_not_called()
    assert bridge.downloadPhase == "readyappimage"


def test_install_acceptance_means_installing_not_installed(tmp_path: Path) -> None:
    value = release("appimage")
    install = Mock(return_value=True)
    table: dict[str, str] = {}
    bridge, _, _, _ = bridge_with(value, install=install, refusals=table)
    result = ready_file(tmp_path, value)
    bridge.download()
    publish_ready(bridge, result)
    table["download"] = "offline"
    bridge.refreshCapabilities()
    assert bridge.canInstallAndRestart
    bridge.installAndRestart()
    install.assert_called_once_with(result)
    assert bridge.downloadPhase == "installing"
    assert bridge.downloadBusy
    bridge.installAndRestart()
    assert install.call_count == 1
    bridge.set_install_error("attacker path secret")
    assert bridge.downloadPhase == "error"
    assert bridge.downloadError == "install-failed"
    assert bridge.canOpenFolder


def test_synchronous_install_preparation_error_is_not_overwritten(tmp_path: Path) -> None:
    value = release("appimage")
    install = Mock()
    bridge, _, _, _ = bridge_with(value, install=install)
    bridge.download()
    publish_ready(bridge, ready_file(tmp_path, value))

    def fail(_: VerifiedDownload) -> bool:
        bridge.set_install_error("no-space")
        return True

    install.side_effect = fail
    bridge.installAndRestart()
    assert bridge.downloadPhase == "error"
    assert bridge.downloadError == "no-space"


@pytest.mark.parametrize("outcome", [False, OSError("secret path")])
def test_install_rejection_or_exception_stays_error_with_local_fallback(
    tmp_path: Path, outcome: object
) -> None:
    value = release("appimage")
    install = Mock(return_value=outcome)
    if isinstance(outcome, Exception):
        install.side_effect = outcome
    bridge, _, _, _ = bridge_with(value, install=install)
    bridge.download()
    publish_ready(bridge, ready_file(tmp_path, value))
    bridge.installAndRestart()
    assert bridge.downloadPhase == "error"
    assert bridge.downloadError in ("install-refused", "install-failed")
    assert bridge.canOpenFolder
    assert "secret" not in bridge.downloadErrorText


def test_error_is_closed_and_rate_limit_blocks_retry_until_window() -> None:
    now = [1000.0]
    bridge, controller, _, _ = bridge_with(release(), now=now)
    bridge.download()
    bridge.set_download_status(DownloadStatus(1, "error", error="rate-limited", retry_at=1060))
    assert bridge.downloadRetryAt == 1060
    assert bridge.downloadRetryText
    assert not bridge.canRetryDownload
    bridge.retryDownload()
    assert len(controller.started) == 1
    now[0] = 1060
    assert bridge.canRetryDownload
    bridge.retryDownload()
    assert len(controller.started) == 2
    bridge.set_download_status(
        DownloadStatus(2, "error", error="<img>secret", retry_at=float("nan"))
    )
    assert bridge.downloadError == "download-failed"
    assert bridge.downloadRetryAt == 0
    assert "secret" not in bridge.downloadErrorText


def test_retry_keeps_original_release_after_new_discovery() -> None:
    value = release()
    bridge, controller, _, _ = bridge_with(value)
    bridge.download()
    bridge.set_download_status(DownloadStatus(1, "error", error="timeout"))
    bridge.set_status(found(release(version="2.0.0"), "- Новое"))
    bridge.retryDownload()
    assert controller.started == [value, value]
    assert bridge.notes == [{"text": "Описание", "bullet": True}]


def test_start_failure_becomes_safe_error_and_can_retry() -> None:
    bridge, controller, _, _ = bridge_with(release())
    controller.error = DownloadError("thread-start")
    bridge.download()
    assert bridge.downloadPhase == "error"
    assert bridge.downloadError == "thread-start"
    assert bridge.canRetryDownload


def test_python_status_setters_require_gui_thread() -> None:
    bridge, _, _, _ = bridge_with(release())
    failures: list[Exception] = []

    def worker() -> None:
        callbacks: tuple[Callable[[], None], ...] = (
            lambda: bridge.set_download_status(DownloadStatus(1, "ready")),
            lambda: bridge.set_install_error("no-space"),
            lambda: bridge.set_status(found(release())),
        )
        for callback in callbacks:
            try:
                callback()
            except Exception as error:
                failures.append(error)

    thread = Thread(target=worker)
    thread.start()
    thread.join(timeout=1)
    assert not thread.is_alive()
    assert len(failures) == 3
    assert all(isinstance(error, RuntimeError) for error in failures)


def test_failed_install_needs_explicit_retryable_permission(tmp_path: Path) -> None:
    value = release("appimage")
    install = Mock(return_value=True)
    bridge, _, _, _ = bridge_with(value, install=install)
    bridge.download()
    publish_ready(bridge, ready_file(tmp_path, value))
    bridge.installAndRestart()
    bridge.set_install_error("prepare-failed")
    assert not bridge.canInstallAndRestart
    assert bridge.canOpenFolder and bridge.canRetryDownload
    bridge.installAndRestart()
    assert install.call_count == 1


def test_runtime_can_allow_explicit_local_retry(tmp_path: Path) -> None:
    value = release("appimage")
    install = Mock(return_value=True)
    bridge, controller, _, _ = bridge_with(value, install=install)
    bridge.download()
    publish_ready(bridge, ready_file(tmp_path, value))
    bridge.installAndRestart()
    bridge.set_install_error("busy", retryable=True)
    assert bridge.canInstallAndRestart
    bridge.installAndRestart()
    assert install.call_count == 2
    assert len(controller.started) == 1


def test_restored_prior_warning_grants_no_file_trust_or_startup_action() -> None:
    bridge, controller, checker, opener = bridge_with()
    bridge.restore_install_error("1.2.3", "launch-unconfirmed")
    assert bridge.downloadPhase == "error"
    assert bridge.version == "1.2.3"
    assert bridge.downloadError == "launch-unconfirmed"
    assert bridge.notes == []
    assert not bridge.canOpenFolder and not bridge.canInstallAndRestart
    assert not bridge.canRetryDownload
    assert controller.started == []
    checker.check_now.assert_not_called()
    opener.assert_not_called()
    bridge.set_status(found(release(version="2.0.0")))
    assert bridge.version == "2.0.0"
    assert bridge.downloadErrorText.startswith("Предыдущая попытка обновления до версии 1.2.3:")
    bridge.download()
    assert bridge.version == "2.0.0"
    assert bridge.downloadPhase == "metadata"


def test_restored_warning_is_bounded_plaintext_and_cannot_replace_active_operation() -> None:
    bridge, _, _, _ = bridge_with()
    bridge.restore_install_error("<b>evil</b>" * 30, "<img>secret")
    assert bridge.version == ""
    assert bridge.downloadError == "install-failed"
    bridge.set_status(found(release()))
    bridge.download()
    bridge.restore_install_error("9.0.0", "launch-failed")
    assert bridge.downloadPhase == "metadata"
    assert bridge.version == "1.2.3"


@pytest.mark.parametrize("outcome", [False, OSError("secret file path")])
def test_folder_open_failure_keeps_verified_file_without_raw_error(
    tmp_path: Path, outcome: object
) -> None:
    value = release()
    bridge, _, _, opener = bridge_with(value)
    bridge.download()
    publish_ready(bridge, ready_file(tmp_path, value))
    if isinstance(outcome, Exception):
        opener.side_effect = outcome
    else:
        opener.return_value = outcome
    bridge.openFolder()
    assert bridge.downloadError == "open-failed"
    assert bridge.downloadPhase == "readydeb"
    assert bridge.canOpenFolder
    assert "secret" not in bridge.downloadErrorText


def test_install_refusal_is_rechecked_at_click(tmp_path: Path) -> None:
    value = release("appimage")
    code = [""]
    install = Mock(return_value=True)
    bridge, _, _, _ = bridge_with(value, install=install, install_refusal=lambda: code[0])
    bridge.download()
    publish_ready(bridge, ready_file(tmp_path, value))
    assert bridge.canInstallAndRestart
    code[0] = "busy"
    bridge.installAndRestart()
    install.assert_not_called()
    assert bridge.downloadPhase == "readyappimage"


@pytest.mark.parametrize("track", ["deb", "appimage"])
def test_prior_warning_and_fresh_candidate_form_consistent_signalled_snapshot(track: Track) -> None:
    install = Mock(return_value=True)
    bridge, controller, checker, opener = bridge_with(install=install)
    prior = "1.4.2"
    candidate = release(track, "1.5.0", size=123)
    bridge.restore_install_error(prior, "launch-unconfirmed")
    assert bridge.artifactSize == 0 and bridge.artifactSizeText == ""
    assert not bridge.canDownload and not bridge.canRetryDownload
    observed: list[tuple[str, list[dict[str, object]], int, str, str, bool, bool]] = []

    def observe() -> None:
        observed.append(
            (
                bridge.version,
                bridge.notes,
                bridge.artifactSize,
                bridge.downloadPhase,
                bridge.downloadErrorText,
                bridge.canDownload,
                bridge.canRetryDownload,
            )
        )

    bridge.downloadChanged.connect(observe)
    bridge.statusChanged.connect(observe)
    bridge.set_status(found(candidate, "- Изменения 1.5.0"))
    assert observed
    for version, notes, size, phase, error, can_download, can_retry in observed:
        assert version == "1.5.0"
        assert notes == [{"text": "Изменения 1.5.0", "bullet": True}]
        assert size == 123
        assert phase == "error"
        assert error.startswith("Предыдущая попытка обновления до версии 1.4.2:")
        assert can_download and not can_retry
    assert not bridge.canOpenFolder and not bridge.canInstallAndRestart
    assert bridge.folderPath == "" and bridge.adminInstruction == ""
    assert controller.started == []
    install.assert_not_called()
    opener.assert_not_called()
    checker.check_now.assert_not_called()
    bridge.downloadChanged.disconnect(observe)
    bridge.statusChanged.disconnect(observe)
    bridge.download()
    assert controller.started == [candidate]
    assert bridge.version == "1.5.0"
    assert bridge.notes == [{"text": "Изменения 1.5.0", "bullet": True}]
    assert bridge.downloadPhase == "metadata"
    assert bridge.downloadError == "" and bridge.downloadErrorText == ""
    assert not bridge.canOpenFolder and not bridge.canInstallAndRestart
    install.assert_not_called()
    opener.assert_not_called()


def test_prior_warning_survives_untrusted_discovery_without_candidate_authority() -> None:
    bridge, controller, _, _ = bridge_with()
    bridge.restore_install_error("1.4.2", "launch-unconfirmed")
    bridge.set_status(UpdateStatus("available", "1.5.0", "- Untrusted", raw_tag="v1.5.0"))
    assert bridge.version == "1.4.2"
    assert bridge.notes == []
    assert bridge.artifactSize == 0 and bridge.artifactSizeText == ""
    assert bridge.downloadErrorText.startswith("Предыдущая попытка обновления до версии 1.4.2:")
    assert bridge.downloadPhase == "error"
    assert not bridge.canDownload and not bridge.canRetryDownload
    bridge.download()
    assert controller.started == []


def test_prior_candidate_only_change_notifies_display_fields_after_complete_snapshot() -> None:
    bridge, _, _, _ = bridge_with()
    candidate = release(version="1.5.0")
    bridge.restore_install_error("1.4.2", "launch-unconfirmed")
    status = found(candidate, "- Описание 1.5.0")
    bridge.set_status(replace(status, verified_release=None))
    assert bridge.version == "1.4.2" and bridge.notes == []
    observed: list[tuple[str, list[dict[str, object]], int]] = []

    def observe() -> None:
        observed.append((bridge.version, bridge.notes, bridge.artifactSize))

    bridge.statusChanged.connect(observe)
    bridge.set_status(status)
    assert observed == [("1.5.0", [{"text": "Описание 1.5.0", "bullet": True}], 7)]
    assert bridge.downloadPhase == "error"
    assert bridge.canDownload and not bridge.canRetryDownload
