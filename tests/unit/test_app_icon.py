"""Значок приложения для окон: явный setWindowIcon (дефект Fly 1.8.4, 0.1.7-7)."""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest

from astra_voice.core import paths
from astra_voice.ui import app_icon
from astra_voice.ui.app_icon import app_icon_files, install_app_icon

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from helpers.qt_app import get_qapplication  # noqa: E402

if TYPE_CHECKING:
    from PyQt5.QtWidgets import QApplication

pytestmark = pytest.mark.unit
DATA = Path(__file__).resolve().parents[2] / "data"


@pytest.fixture(scope="module")
def qapp() -> QApplication:
    return get_qapplication()


@pytest.fixture
def window_icon(qapp: QApplication) -> Iterator[None]:
    """Значок приложения — глобальное состояние Qt: возвращаем прежний."""
    from PyQt5.QtGui import QGuiApplication

    previous = QGuiApplication.windowIcon()
    QGuiApplication.setWindowIcon(previous.__class__())
    yield
    QGuiApplication.setWindowIcon(previous)


def _roots(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, data: Path) -> Path:
    """Тема установки — во временной папке; данные — ``data``. Возвращает корень темы."""
    monkeypatch.setattr(paths, "resource_root", lambda: tmp_path / "usr/share/astra-voice")
    monkeypatch.setattr(paths, "data_dir_static", lambda: data)
    return tmp_path / "usr/share/icons/hicolor"


def _png(path: Path, size: int) -> Path:
    from PyQt5.QtGui import QColor, QImage

    path.parent.mkdir(parents=True, exist_ok=True)
    image = QImage(size, size, QImage.Format_ARGB32)
    image.fill(QColor("#3366cc"))
    assert image.save(str(path), "PNG")
    return path


def _same_image(left: object, right: object) -> bool:
    from PyQt5.QtGui import QImage

    assert isinstance(left, QImage) and isinstance(right, QImage)
    # QPixmap хранит альфу premultiplied: сравниваем в том же формате.
    fmt = QImage.Format_ARGB32_Premultiplied
    return bool(left.convertToFormat(fmt) == right.convertToFormat(fmt))


def test_icon_roots_follow_install_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    """.deb: /usr/share/icons/hicolor (packaging/debian/rules); AppImage — то же под $APPDIR."""
    monkeypatch.setattr(paths, "resource_root", lambda: paths.INSTALL_SHARE_DIR)
    assert paths.icon_theme_roots() == (
        Path("/usr/share/icons/hicolor"),
        paths.INSTALL_SHARE_DIR / "data/icons/hicolor",
    )
    appdir = Path("/tmp/.mount_astra/usr/share/astra-voice")
    monkeypatch.setattr(paths, "resource_root", lambda: appdir)
    assert paths.icon_theme_roots()[0] == Path("/tmp/.mount_astra/usr/share/icons/hicolor")


def test_installs_bundled_png_as_window_icon(
    qapp: QApplication, window_icon: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from PyQt5.QtGui import QGuiApplication, QImage

    _roots(monkeypatch, tmp_path, DATA)
    files = app_icon_files()
    sizes = [int(path.parent.parent.name.partition("x")[0]) for path in files]
    assert sizes == sorted(sizes) and sizes[0] == 16 and sizes[-1] == 256
    assert all(path.is_relative_to(DATA / "icons/hicolor") for path in files)

    assert install_app_icon(qapp) is True

    icon = QGuiApplication.windowIcon()
    assert not icon.isNull()
    available = {(size.width(), size.height()) for size in icon.availableSizes()}
    assert available == {(size, size) for size in sizes}
    expected = QImage(str(DATA / "icons/hicolor/48x48/apps/astravoice.png"))
    assert _same_image(icon.pixmap(48, 48).toImage(), expected)


def test_theme_root_wins_and_sizes_are_not_mixed(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    theme = _roots(monkeypatch, tmp_path, DATA)
    small = _png(theme / "32x32/apps/astravoice.png", 32)
    large = _png(theme / "256x256/apps/astravoice.png", 256)

    assert app_icon_files() == (small, large)


def test_ignores_oversized_scalable_and_odd_directories(
    qapp: QApplication, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    theme = _roots(monkeypatch, tmp_path, tmp_path / "no-data")
    kept = _png(theme / "48x48/apps/astravoice.png", 48)
    _png(theme / "512x512/apps/astravoice.png", 512)
    _png(theme / "48x32/apps/astravoice.png", 48)
    _png(theme / "0x0/apps/astravoice.png", 16)
    _png(theme / "scalable/apps/astravoice.png", 48)
    _png(theme / "64x64/apps/other.png", 64)
    (theme / "README").write_text("not a size directory")

    assert app_icon_files() == (kept,)


def test_missing_icon_leaves_app_without_icon_and_logs_without_paths(
    qapp: QApplication,
    window_icon: None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    from PyQt5.QtGui import QGuiApplication

    _roots(monkeypatch, tmp_path, tmp_path / "no-data")
    assert app_icon_files() == ()

    with caplog.at_level(logging.DEBUG, logger=app_icon.__name__):
        assert install_app_icon(qapp) is False

    assert QGuiApplication.windowIcon().isNull()
    assert "Значок приложения не задан" in caplog.text
    assert str(tmp_path) not in caplog.text
    assert all(record.levelno == logging.DEBUG for record in caplog.records)


def test_broken_png_is_skipped_without_exception(
    qapp: QApplication, window_icon: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from PyQt5.QtGui import QGuiApplication

    theme = _roots(monkeypatch, tmp_path, tmp_path / "no-data")
    broken = theme / "48x48/apps/astravoice.png"
    broken.parent.mkdir(parents=True)
    broken.write_bytes(b"not a png")

    assert install_app_icon(qapp) is False
    assert QGuiApplication.windowIcon().isNull()


def test_unreadable_theme_falls_back_to_data(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    theme = _roots(monkeypatch, tmp_path, DATA)
    theme.parent.mkdir(parents=True)
    theme.write_text("file instead of directory")

    files = app_icon_files()
    assert files and all(path.is_relative_to(DATA) for path in files)


def test_load_error_does_not_raise(
    qapp: QApplication, window_icon: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    from PyQt5.QtGui import QGuiApplication

    def fail() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(app_icon, "app_icon", fail)
    assert install_app_icon(qapp) is False
    assert QGuiApplication.windowIcon().isNull()


def test_without_gui_application_nothing_is_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    """Подменённый QApplication (тесты main) — не трогаем QPixmap и не зовём setWindowIcon."""
    loader = Mock()
    monkeypatch.setattr(app_icon, "app_icon", loader)
    fake = Mock()

    assert install_app_icon(fake) is False
    loader.assert_not_called()
    fake.setWindowIcon.assert_not_called()
