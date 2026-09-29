"""T-172: внешние ссылки с очищенным окружением, без настоящего xdg-open."""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path
from unittest.mock import Mock

import pytest
from PyQt5.QtCore import QUrl

from astra_voice.platform.external import open_external

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def child_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> dict[str, str]:
    """Окружение .deb после bootstrap; все пользовательские каталоги временные."""
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    user = {
        # pytest добавляет эту переменную заново перед телом теста.
        "PYTEST_CURRENT_TEST": f"{request.node.nodeid} (call)",
        "HOME": str(tmp_path),
        "XDG_DATA_HOME": str(tmp_path / "data"),
        "XDG_CONFIG_HOME": str(tmp_path / "config"),
        "XDG_RUNTIME_DIR": str(tmp_path / "runtime"),
        "XDG_CACHE_HOME": str(tmp_path / "cache"),
        "TMPDIR": str(tmp_path / "tmp"),
        "PATH": "/usr/bin:/bin",
        "LANG": "ru_RU.UTF-8",
        "LC_ALL": "ru_RU.UTF-8",
    }
    launched = {
        **user,
        "QT_QUICK_BACKEND": "software",
        "QT_XCB_GL_INTEGRATION": "none",
        "PULSE_CLIENTCONFIG": str(tmp_path / "pulse-client.conf"),
        "ASTRA_VOICE_ORIG_UNSET": "QT_QUICK_BACKEND QT_XCB_GL_INTEGRATION PULSE_CLIENTCONFIG",
    }
    for name, value in launched.items():
        monkeypatch.setenv(name, value)
    return user


@pytest.mark.parametrize(
    "url",
    ["file:///tmp/model", "https://example.org/a?q=b", "http://example.org", "HTTPS://example.org"],
)
def test_allowed_urls(url: str, child_environment: dict[str, str]) -> None:
    popen = Mock()
    assert open_external(url, popen=popen) is True
    popen.assert_called_once_with(
        ["xdg-open", url],
        shell=False,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        env=child_environment,
    )


@pytest.mark.parametrize(
    "url",
    [
        "",
        "-https://example.org/secret",
        "--help",
        "mailto:secret@example.org",
        "ftp://example.org/secret",
        "javascript:secret",
        "/tmp/secret",
        "https:secret",
        "file:/tmp/secret",
        " https://example.org/secret",
    ],
)
def test_forbidden_urls_do_not_spawn(url: str, caplog: pytest.LogCaptureFixture) -> None:
    popen = Mock()
    assert open_external(url, popen=popen) is False
    popen.assert_not_called()
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert "secret" not in caplog.text
    if url:
        assert url not in caplog.text


def test_appimage_environment_is_removed(
    child_environment: dict[str, str], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    for name in (
        "ASTRA_VOICE_APPIMAGE_DIR",
        "ASTRA_VOICE_SECRET",
        "APPIMAGE",
        "APPIMAGE_EXTRACT_AND_RUN",
        "APPIMAGE_SILENT_INSTALL",
        "APPDIR",
        "ARGV0",
        "OWD",
    ):
        monkeypatch.setenv(name, "bundle-value")
    before = dict(os.environ)
    popen = Mock()
    assert open_external("https://example.org", popen=popen)
    assert popen.call_args.kwargs["env"] == child_environment
    assert dict(os.environ) == before
    assert not (tmp_path / "cache").exists()


def test_oserror_is_private_failure(caplog: pytest.LogCaptureFixture) -> None:
    url = "file:///tmp/secret"
    popen = Mock(side_effect=OSError(f"cannot open {url}"))
    assert open_external(url, popen=popen) is False
    popen.assert_called_once()
    assert len(caplog.records) == 1
    assert caplog.records[0].levelno == logging.WARNING
    assert caplog.records[0].exc_info is None
    assert "secret" not in caplog.text


def test_local_url_encodes_cyrillic_and_spaces() -> None:
    url = QUrl.fromLocalFile("/tmp/а б").toString(QUrl.FullyEncoded)
    assert url == "file:///tmp/%D0%B0%20%D0%B1"
    popen = Mock()
    assert open_external(url, popen=popen)
    assert popen.call_args.args == (["xdg-open", url],)
