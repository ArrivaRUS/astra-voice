"""Генератор темы: детерминированность, идемпотентность и свежесть генерата в репозитории."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "gen_theme.py"

pytestmark = pytest.mark.unit


def load_gen_theme() -> ModuleType:
    spec = importlib.util.spec_from_file_location("gen_theme", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_theme"] = module
    spec.loader.exec_module(module)
    return module


def test_check_passes_on_repo() -> None:
    """`gen_theme.py --check` на чистом репозитории — 0 (иначе генерат не перегенерирован)."""
    assert load_gen_theme().main(["--check"]) == 0


def test_generation_is_idempotent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Две генерации подряд дают побайтово одинаковые файлы."""
    module = load_gen_theme()
    out = tmp_path / "qml"
    monkeypatch.setattr(module, "QML_DIR", out)

    assert module.main([]) == 0
    first = {p.name: p.read_bytes() for p in sorted(out.iterdir())}
    assert set(first) == {"Theme.qml", "PillTheme.qml", "qmldir"}

    assert module.main([]) == 0
    second = {p.name: p.read_bytes() for p in sorted(out.iterdir())}
    assert first == second
    assert module.main(["--check"]) == 0


def test_check_detects_edited_generated_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Правка генерата руками ловится `--check` (выход 1)."""
    module = load_gen_theme()
    out = tmp_path / "qml"
    monkeypatch.setattr(module, "QML_DIR", out)
    module.main([])

    theme = out / "Theme.qml"
    theme.write_text(theme.read_text(encoding="utf-8") + "// правка руками\n", encoding="utf-8")
    assert module.main(["--check"]) == 1


def test_theme_has_no_raw_color_literals_outside_tokens() -> None:
    """Все цвета Theme.qml пришли из tokens.json — сверяем несколько опорных значений."""
    text = (REPO / "qml" / "Theme.qml").read_text(encoding="utf-8")
    assert 'readonly property color bgApp: dark ? "#0B1220" : "#F7F9FC"' in text
    assert 'readonly property color primary: dark ? "#6C93E8" : "#1B3A73"' in text
    assert "readonly property real sidebarW: 184" in text
    assert 'readonly property string fontUi: "PT Root UI"' in text
    assert "pragma Singleton" in text
    assert "НЕ ПРАВИТЬ РУКАМИ" in text.splitlines()[0]


def test_qmldir_lists_singletons_and_components() -> None:
    """qmldir объявляет оба синглтона и все типы верхнего уровня из qml/."""
    lines = (REPO / "qml" / "qmldir").read_text(encoding="utf-8").splitlines()
    assert "singleton Theme 1.0 Theme.qml" in lines
    assert "singleton PillTheme 1.0 PillTheme.qml" in lines
    top_level = {p.stem for p in (REPO / "qml").glob("*.qml")} - {"Theme", "PillTheme"}
    for name in top_level:
        assert f"{name} 1.0 {name}.qml" in lines
