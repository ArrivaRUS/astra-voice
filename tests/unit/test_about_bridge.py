"""Мост раздела «О программе» (M9-а v0.2): данные, открытие файлов, статистика."""

from __future__ import annotations

import sys
import types
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from PyQt5.QtCore import QUrl

from astra_voice.core import paths
from astra_voice.core.stats import Summary
from astra_voice.ui import about_bridge
from astra_voice.ui.about_bridge import (
    DOC_LICENSE,
    DOC_NOTICE,
    DOC_PRIVACY,
    INSTALL_DOC_DIR,
    SYSTEM_GPL3,
    AboutBridge,
    display_path,
    doc_paths,
    folder_size,
    moment_text,
    plural,
    stats_text,
)
from astra_voice.updates.state import SourceState

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]


def summary(count: int, p50: float | None = None, p95: float | None = None) -> Summary:
    return {
        "dictations": count,
        "p50_ms": p50,
        "p95_ms": p95,
        "total_p50_ms": None,
        "total_p95_ms": None,
        "results": {"ok": count, "empty": 0, "cancelled": 0},
        "result_shares": {},
        "mic_errors": 0,
    }


class FakeStats:
    def __init__(self, value: Summary, *, fail_clear: bool = False) -> None:
        self.value = value
        self.cleared = 0
        self.fail_clear = fail_clear

    def summary(self) -> Summary:
        return self.value

    def clear(self) -> None:
        if self.fail_clear:
            raise OSError("диск только для чтения")
        self.cleared += 1
        self.value = summary(0)


def make(tmp_path: Path, **kwargs: Any) -> tuple[AboutBridge, list[str]]:
    opened: list[str] = []

    def opener(url: QUrl) -> bool:
        opened.append(url.toLocalFile())
        return True

    docs = tmp_path / "docs"
    docs.mkdir(exist_ok=True)
    for name in ("LICENSE", "NOTICE", "PRIVACY.md"):
        (docs / name).write_text("x", encoding="utf-8")
    settings_dir = tmp_path / "config"
    models_dir = tmp_path / "models"
    logs_dir = tmp_path / "logs"
    options: dict[str, Any] = {
        "open_url": opener,
        "documents": {
            DOC_LICENSE: docs / "LICENSE",
            DOC_NOTICE: docs / "NOTICE",
            DOC_PRIVACY: docs / "PRIVACY.md",
        },
        "settings_dir": settings_dir,
        "models_dir": models_dir,
        "logs_dir": logs_dir,
        "update_state": None,
        "installed": False,
    }
    options.update(kwargs)
    return AboutBridge(**options), opened


def test_doc_paths_installed_and_source(tmp_path: Path) -> None:
    installed = doc_paths(paths.INSTALL_SHARE_DIR)
    assert installed == {
        DOC_LICENSE: SYSTEM_GPL3,
        DOC_NOTICE: INSTALL_DOC_DIR / "NOTICE",
        DOC_PRIVACY: INSTALL_DOC_DIR / "PRIVACY.md",
    }
    source = doc_paths(REPO)
    assert source == {
        DOC_LICENSE: REPO / "LICENSE",
        DOC_NOTICE: REPO / "NOTICE",
        DOC_PRIVACY: REPO / "docs" / "PRIVACY.md",
    }
    assert all(path.is_file() for path in source.values())


def test_debian_rules_install_documents_where_bridge_looks() -> None:
    """NOTICE и PRIVACY.md пакет кладёт туда, где их ищет мост."""
    rules = (REPO / "packaging/debian/rules").read_text(encoding="utf-8")
    assert f"DOCDIR   := $(PKG){INSTALL_DOC_DIR}" in rules
    assert "for f in NOTICE docs/PRIVACY.md" in rules


def test_build_date_from_generated_version(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("astra_voice._version")
    module.__version__ = "0.2.0"  # type: ignore[attr-defined]
    module.__build_date__ = "2026-10-01"  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "astra_voice._version", module)
    assert about_bridge.build_date() == "01.10.2026"
    module.__build_date__ = "мусор"  # type: ignore[attr-defined]
    assert about_bridge.build_date() == ""
    del module.__build_date__
    assert about_bridge.build_date() == ""


def test_build_date_absent_without_generated_file(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "astra_voice._version", None)
    assert about_bridge.build_date() == ""


def test_build_script_writes_build_date() -> None:
    script = (REPO / "packaging/build-deb.sh").read_text(encoding="utf-8")
    assert 'BUILD_DATE="$(date -u -d "@$SOURCE_DATE_EPOCH" +%Y-%m-%d)"' in script
    assert '__build_date__ = "$BUILD_DATE"' in script


def test_display_path_shortens_home(tmp_path: Path) -> None:
    assert display_path(tmp_path / ".config" / "astra-voice", tmp_path) == "~/.config/astra-voice"
    assert display_path(Path("/var/lib/x"), tmp_path) == "/var/lib/x"


@pytest.mark.parametrize(
    ("count", "word"),
    [
        (1, "диктовка"),
        (2, "диктовки"),
        (4, "диктовки"),
        (5, "диктовок"),
        (11, "диктовок"),
        (14, "диктовок"),
        (21, "диктовка"),
        (22, "диктовки"),
        (111, "диктовок"),
        (148, "диктовок"),
    ],
)
def test_plural(count: int, word: str) -> None:
    assert plural(count, "диктовка", "диктовки", "диктовок") == word


def test_stats_text() -> None:
    assert stats_text(summary(0)) == ""
    assert stats_text(summary(3)) == "3 диктовки"
    assert (
        stats_text(summary(148, 380.0, 612.0))
        == "обычно 0,38 с, в худших случаях 0,61 с · 148 диктовок"
    )


def test_moment_text() -> None:
    now = datetime(2026, 9, 29, 18, 0).timestamp()
    assert moment_text(None, now) == ""
    assert moment_text(datetime(2026, 9, 29, 14, 5).timestamp(), now) == "сегодня в 14:05"
    assert moment_text(datetime(2026, 9, 28, 9, 7).timestamp(), now) == "28.09.2026 в 09:07"


def test_folder_size_skips_symlinks(tmp_path: Path) -> None:
    (tmp_path / "a").write_bytes(b"x" * 1000)
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b").write_bytes(b"y" * 24)
    outside = tmp_path.parent / f"{tmp_path.name}-outside"
    outside.mkdir()
    (outside / "big").write_bytes(b"z" * 5000)
    (tmp_path / "link").symlink_to(outside, target_is_directory=True)
    # Ссылка на каталог не разворачивается: чужие 5000 байт не считаются.
    assert folder_size(tmp_path) == 1024


def test_bridge_constants(tmp_path: Path) -> None:
    bridge, _ = make(tmp_path)
    assert bridge.version
    assert bridge.installKind == "source"
    assert bridge.pythonVersion.count(".") == 2
    assert bridge.pyqtVersion
    assert bridge.settingsPath == str(tmp_path / "config")
    assert bridge.statsAvailable is False
    assert bridge.statsCount == 0 and bridge.statsText == ""
    installed, _ = make(tmp_path, installed=True)
    assert installed.installKind == "deb"


def test_package_version_missing() -> None:
    assert about_bridge.package_version("astra-voice-нет-такого-пакета") == ""


def test_documents_and_folders_open_only_when_present(tmp_path: Path) -> None:
    bridge, opened = make(tmp_path)
    assert bridge.licenseAvailable and bridge.noticeAvailable and bridge.privacyAvailable
    assert not bridge.settingsFolderAvailable and not bridge.logsFolderAvailable
    assert bridge.modelsSize == ""
    bridge.openSettingsFolder()
    bridge.openLogsFolder()
    assert opened == []
    bridge.openLicense()
    bridge.openNotice()
    bridge.openPrivacy()
    docs = tmp_path / "docs"
    assert opened == [str(docs / "LICENSE"), str(docs / "NOTICE"), str(docs / "PRIVACY.md")]

    (tmp_path / "config").mkdir()
    (tmp_path / "logs").mkdir()
    (tmp_path / "models").mkdir()
    (tmp_path / "models" / "m.onnx").write_bytes(b"x" * 2048)
    (docs / "NOTICE").unlink()
    changes: list[int] = []
    bridge.infoChanged.connect(lambda: changes.append(1))
    bridge.refresh()
    assert changes == [1]
    assert bridge.settingsFolderAvailable and bridge.logsFolderAvailable
    assert bridge.modelsFolderAvailable and bridge.modelsSize
    assert not bridge.noticeAvailable
    opened.clear()
    bridge.openNotice()
    bridge.openSettingsFolder()
    bridge.openLogsFolder()
    assert opened == [str(tmp_path / "config"), str(tmp_path / "logs")]
    bridge.refresh()
    assert changes == [1], "без перемен сигнал не повторяется"


def test_open_failure_does_not_raise(tmp_path: Path) -> None:
    def broken(_url: QUrl) -> bool:
        raise RuntimeError("нет программы для открытия")

    bridge, _ = make(tmp_path, open_url=broken)
    bridge.openLicense()


def test_update_dates(tmp_path: Path) -> None:
    now = datetime(2026, 9, 29, 18, 0).timestamp()
    state = SourceState(
        last_attempt_at=datetime(2026, 9, 29, 14, 5).timestamp(),
        last_success_at=datetime(2026, 9, 28, 10, 12).timestamp(),
    )
    current = {"state": SourceState()}
    bridge, _ = make(tmp_path, update_state=lambda: current["state"], clock=lambda: now)
    assert bridge.lastAttemptText == bridge.lastSuccessText == ""
    changes: list[int] = []
    bridge.updatesChanged.connect(lambda: changes.append(1))
    current["state"] = state
    bridge.refresh_updates()
    assert changes == [1]
    assert bridge.lastAttemptText == "сегодня в 14:05"
    assert bridge.lastSuccessText == "28.09.2026 в 10:12"


def test_update_dates_failure_is_quiet(tmp_path: Path) -> None:
    def broken() -> SourceState:
        raise OSError("нет каталога состояния")

    bridge, _ = make(tmp_path, update_state=broken)
    assert bridge.lastAttemptText == bridge.lastSuccessText == ""


def test_update_dates_from_real_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from astra_voice.updates.state import StateStore

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    moment = datetime(2026, 9, 1, 8, 30).timestamp()
    StateStore().set("app", SourceState(last_attempt_at=moment, last_success_at=None))
    bridge, _ = make(tmp_path, update_state=about_bridge._read_update_state)
    assert bridge.lastAttemptText == "01.09.2026 в 08:30"
    assert bridge.lastSuccessText == ""


def test_stats_and_clear(tmp_path: Path) -> None:
    stats = FakeStats(summary(148, 380.0, 612.0))
    bridge, _ = make(tmp_path, stats=stats)
    assert bridge.statsAvailable
    assert bridge.statsCount == 148
    assert bridge.statsText == "обычно 0,38 с, в худших случаях 0,61 с · 148 диктовок"
    changes: list[int] = []
    bridge.statsChanged.connect(lambda: changes.append(1))
    bridge.clearStats()
    assert stats.cleared == 1
    assert changes == [1]
    assert bridge.statsCount == 0 and bridge.statsText == ""


def test_stats_clear_failure_keeps_numbers(tmp_path: Path) -> None:
    stats = FakeStats(summary(2), fail_clear=True)
    bridge, _ = make(tmp_path, stats=stats)
    bridge.clearStats()
    assert bridge.statsCount == 2


def test_clear_without_stats_is_noop(tmp_path: Path) -> None:
    bridge, _ = make(tmp_path)
    bridge.clearStats()
    assert bridge.statsCount == 0


def test_real_stats_clear(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from astra_voice.core.stats import Stats

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    stats = Stats()
    stats.append("dictation", model_id="m", t_ms=400.0, cold=False, result="ok")
    bridge, _ = make(tmp_path, stats=stats)
    assert bridge.statsCount == 1
    bridge.clearStats()
    assert bridge.statsCount == 0
    assert Stats().events() == []


def test_paths_helpers_do_not_create(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    assert paths.config_dir_path() == tmp_path / "cfg" / "astra-voice"
    assert paths.data_dir_path() == tmp_path / "data" / "astra-voice"
    assert paths.log_dir_path() == tmp_path / "data" / "astra-voice" / "logs"
    assert paths.model_store_dir_path() == tmp_path / "data" / "astra-voice" / "models"
    assert not (tmp_path / "cfg").exists() and not (tmp_path / "data").exists()
    assert paths.model_store_dir() == paths.model_store_dir_path()
