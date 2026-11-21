"""Fixed copy, passive inputs and deployment contracts for W1."""

from __future__ import annotations

import ast
import os
import re
import shlex
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from test_qml_onboarding_static import constant_qml_text, qml_text_items

from astra_voice.core import paths
from astra_voice.ui.meeting_texts import W1_CALL_TEXTS, W1_REMAINING, W1_TEXTS

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
QML = ROOT / "qml"
OFFER = QML / "MeetingOffer.qml"
CONTROL = QML / "components/MeetingOfferControl.qml"
WINDOW = ROOT / "src/astra_voice/ui/meeting_offer_window.py"


def test_u13_fixed_offer_copy_matches_spec_and_qml_has_no_display_literals() -> None:
    spec = (ROOT / "design/meetings/spec.md").read_text().split("### 4.3 Предложение W1")[1]
    quoted = re.findall("«([^»]+)»", spec.split("### 4.4")[0])
    header, dion, browser, body, addition, start, refuse, remaining = [
        phrase.replace("\n", " ") for phrase in quoted
    ]
    assert W1_CALL_TEXTS == {
        "call-app": {"title": dion, "body": body},
        "call-browser": {"title": browser, "body": body + addition},
    }
    assert W1_TEXTS == {"header": header, "start": start, "refuse": refuse, "close": "Закрыть"}
    assert W1_REMAINING.format(seconds=42) == remaining
    for file in (OFFER, CONTROL):
        source = file.read_text()
        for item in qml_text_items(source):
            assert not constant_qml_text(item.bindings.get("text", ())), (file, item.line)
            assert item.bindings["textFormat"] == ("Text", ".", "PlainText")
        # Includes uses of the inline OfferText component and attached Accessible properties.
        for expr in re.findall(
            r"^\s*(?:text|Accessible\.(?:name|description)):\s*(.+)$", source, re.M
        ):
            assert not constant_qml_text((expr.strip(),))
            assert not re.search(r"""['"][^'"]+['"]""", expr)


def test_q3_controls_share_one_pressed_action_and_avoid_keyboard_and_theme_colors() -> None:
    offer, control = (path.read_text() for path in (OFFER, CONTROL))
    assert re.search(r"Accessible\.role:\s*Accessible\.AlertMessage", offer)
    assert re.search(r"Accessible\.role:\s*Accessible\.Button", control)
    assert re.search(r"Accessible\.name:\s*control\.label", control)
    assert re.search(r"MouseArea\s*\{", control)
    (press,) = re.findall(r"^\s*onPressed:\s*(.+)$", control, re.M)
    (accessible,) = re.findall(r"^\s*Accessible\.onPressAction:\s*(.+)$", control, re.M)
    assert press == "control.pressed(true)"
    assert accessible == "control.pressed(false)"
    assert set(re.findall(r'onPressed: root\.pressed\("([^"]+)", mouse\)', offer)) == {
        "start",
        "refuse",
        "close",
        "open_meetings",
    }
    assert not re.search(
        r"\b(?:Keys\.|activeFocusOnTab|focusPolicy|focus:\s*true|QtQuick\.Shapes"
        r"|(?:AbstractButton|Button|ToolButton|ToolTip|Timer)\s*\{)",
        offer + control,
    )
    colors = re.findall(r"^\s*(?:(?:border\.)?color|\w+Color):\s*(.+)", offer + control, re.M)
    assert colors and all(not re.search(r"\bTheme\.", expr) for expr in colors)
    assert "Qt.PointingHandCursor" in control
    assert not re.search(r"pill_enabled|indicator", offer + WINDOW.read_text())


def _notification_calls(source: str) -> list[str]:
    found = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "notify_"
        ):
            found.append(node.name)
        if isinstance(node, ast.Call) and (node.args or node.keywords):
            if isinstance(node.func, (ast.Name, ast.Attribute)):
                name = ast.unparse(node.func).rsplit(".", 1)[-1]
                if name == "notify" or name.startswith("notify_"):
                    found.append(name)
    return found


def test_u15_window_uses_only_its_local_input_paths_and_no_notifications() -> None:
    source = WINDOW.read_text()
    assert not _notification_calls(source)
    assert _notification_calls("def notify_bad():\n    notify('recording')") == [
        "notify_bad",
        "notify",
    ]
    senders: list[tuple[Path, str]] = []
    for path in sorted((ROOT / "src/astra_voice").rglob("*.py")):
        contents = path.read_text()
        if "press_offer" not in contents and "dismiss_offer" not in contents:
            continue
        senders.extend(
            (path, node.func.attr)
            for node in ast.walk(ast.parse(contents))
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"press_offer", "dismiss_offer"}
        )
    assert sorted(senders) == [(WINDOW, "dismiss_offer"), (WINDOW, "press_offer")]
    assert "pyqtSlot" not in source
    assert "setContextProperty" not in source


@pytest.mark.parametrize("package", ["appimage", "deb"])
def test_p1_p2_installed_resource_checks_cover_w1(
    package: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    resources = tmp_path / "usr/share/astra-voice"
    offer_resources = ("qml/MeetingOffer.qml", "qml/components/MeetingOfferControl.qml")
    for relative in (
        *offer_resources,
        "qml/Pill.qml",
        "qml/Main.qml",
        "data/vad/silero_vad.onnx",
        "data/catalog.json",
        "data/catalog.json.sig",
        "data/catalog.schema.json",
        "data/smoke/smoke-ru.wav",
        "docs/NOTICE",
        "docs/PRIVACY.md",
        "LICENSE",
    ):
        file = resources / relative
        file.parent.mkdir(parents=True, exist_ok=True)
        file.touch()
    shutil.copytree(ROOT / "data/icons/hicolor", resources.parent / "icons/hicolor")

    def check_appimage(expected_code: int) -> str:
        script = ROOT / "packaging/appimage/check_resources.py"
        program = compile(script.read_text(), str(script), "exec")
        with monkeypatch.context() as patch:
            patch.setenv(paths.RESOURCES_ENV, str(resources))
            patch.setattr(paths, "bundle_root", lambda: tmp_path)
            patch.setattr(sys, "argv", [str(script), str(ROOT / "src")])
            patch.setattr(sys, "path", sys.path.copy())
            with pytest.raises(SystemExit) as exited:
                exec(program, {"__name__": "__main__"})
            assert exited.value.code == expected_code
        return capsys.readouterr().err

    if package == "deb":
        # Stage the resource layout and resolve the license inside the fixture,
        # using doc_paths' local-document mode instead of the host's GPL-3 file.
        (resources / "src/astra_voice").mkdir(parents=True)
        (resources / "NOTICE").touch()
        lib = tmp_path / "usr/lib/astra-voice"
        lib.mkdir(parents=True)
        (lib / "astra_voice").symlink_to(ROOT / "src/astra_voice", target_is_directory=True)
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        python = bin_dir / "python3"
        python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
        python.chmod(0o700)
        script = ROOT / "packaging/smoke-installed.sh"
        source = script.read_text()
        phase_start = source.index('say "смоук ресурсов:')
        phase_end = source.index('\nsay "смоук документов:', phase_start)
        shell = (
            source.split("\nROOTDIR=", 1)[0]
            + '\nLIB="$1"\nexport ASTRA_VOICE_RESOURCES="$2"\n'
            + source[phase_start:phase_end]
        )

        def check(expected_code: int) -> str:
            result = subprocess.run(
                ["/bin/sh", "-c", shell, str(script), str(lib), str(resources)],
                cwd=tmp_path,
                env={**os.environ, "PATH": str(bin_dir)},
                capture_output=True,
                text=True,
                timeout=7,
                check=False,
            )
            assert (result.returncode == 0) == (expected_code == 0), result.stderr
            return result.stderr

    else:
        check = check_appimage

    assert check(0) == ""
    for relative in offer_resources:
        file = resources / relative
        file.unlink()
        assert f"Нет файла ресурса: {file}" in check(1).splitlines()
        file.touch()
    assert check(0) == ""


QML_MODULE_DIR = {
    "QtQuick": "QtQuick.2",
    "QtQuick.Controls": "QtQuick/Controls.2",
    "QtQuick.Layouts": "QtQuick/Layouts",
    "QtQuick.Window": "QtQuick/Window.2",
    "QtQml": "QtQml",
}
QML_IMPORT = re.compile(r'^\s*import\s+("[^"]+"|[\w.]+)', re.M)


def _modules(source: str) -> Iterator[str]:
    for name in QML_IMPORT.findall(source):
        if not name.startswith('"'):
            assert name in QML_MODULE_DIR, f"unsupported AppImage QML import: {name}"
            yield QML_MODULE_DIR[name]


def _qml_dependencies(entry: Path) -> dict[Path, set[str]]:
    pending = [entry]
    visited: dict[Path, set[str]] = {}
    while pending:
        path = pending.pop()
        if path in visited:
            continue
        source = path.read_text()
        visited[path] = set(_modules(source))
        directories = [path.parent]
        for name in QML_IMPORT.findall(source):
            if name.startswith('"'):
                directory = (path.parent / name.strip('"')).resolve()
                assert directory.is_relative_to(QML) and directory.is_dir()
                directories.append(directory)
        # Local types and singletons referenced by this file, not every QML in the directory.
        types = set(re.findall(r"\b([A-Z]\w*)\s*\{", source))
        types.update(re.findall(r"\b([A-Z]\w*)\s*\.\s*[a-z]\w*", source))
        for name in types:
            pending.extend(
                candidate
                for directory in directories
                if (candidate := directory / f"{name}.qml").is_file()
            )
    return visited


def test_p3_transitive_qml_imports_match_appimage_keep_list() -> None:
    script = (ROOT / "packaging/appimage/build.sh").read_text()
    keep = set(re.findall(r'^mv "\$QML/([^"/]+)" "\$QML_KEEP/"$', script, re.M))
    (quick,) = re.findall(
        r"^for module in (.+); do$",
        script[script.index("QML_KEEP=") :].split('rm -rf "$QML"')[0],
        re.M,
    )
    keep.update(f"QtQuick/{module}" for module in quick.split())
    deps = _qml_dependencies(OFFER)
    assert {
        OFFER,
        CONTROL,
        QML / "components/Icon.qml",
        QML / "components/OverlaySurface.qml",
        QML / "PillTheme.qml",
        QML / "Theme.qml",
    } <= deps.keys()
    assert "QtQuick/Window.2" in deps[QML / "components/Icon.qml"]
    assert all(modules <= keep for modules in deps.values()), deps
    with pytest.raises(AssertionError, match="QtQuick.Shapes"):
        list(_modules("import QtQuick 2.15\nimport QtQuick.Shapes 1.15"))
