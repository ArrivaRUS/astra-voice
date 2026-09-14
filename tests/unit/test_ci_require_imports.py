"""Тесты гейта `scripts/ci_require_imports.py`.

Гейт существует ради одного: в CI пропуск теста из-за забытого пакета не должен
выглядеть как успех. Поэтому проверяем и то, что он находит зависимости в
исходниках тестов, и то, что он краснеет на отсутствующем модуле.

Сам файл помечен ниже как исключение из сканирования: `importorskip` здесь —
тестовые фикстуры, а не настоящие зависимости. Без метки гейт требовал бы в CI
несуществующий модуль и валил задачу `unit` (так и случилось на 4a23129).

ci-require-imports: skip-file
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "ci_require_imports", ROOT / "scripts" / "ci_require_imports.py"
)
assert _SPEC and _SPEC.loader
guard = importlib.util.module_from_spec(_SPEC)
sys.modules["ci_require_imports"] = guard
_SPEC.loader.exec_module(guard)


def test_finds_modules_in_test_sources(tmp_path: Path) -> None:
    (tmp_path / "test_a.py").write_text(
        'import pytest\npytest.importorskip("Xlib")\n', encoding="utf-8"
    )
    (tmp_path / "test_b.py").write_text(
        'pytest.importorskip("PyQt5.QtQuick", reason="нужен пакет")\n', encoding="utf-8"
    )
    assert guard.required_modules([tmp_path]) == ["PyQt5.QtQuick", "Xlib"]


def test_missing_module_is_reported() -> None:
    assert guard.missing(["sys", "astra_voice_no_such_module"]) == ["astra_voice_no_such_module"]
    # Подмодуль несуществующего пакета не должен ронять проверку исключением.
    assert guard.missing(["нет_пакета.подмодуль"]) == ["нет_пакета.подмодуль"]


def test_exit_code_red_when_dependency_absent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "test_x.py").write_text(
        'pytest.importorskip("astra_voice_no_such_module")\n', encoding="utf-8"
    )
    assert guard.main([str(tmp_path)]) == 1
    assert "astra_voice_no_such_module" in capsys.readouterr().err


def test_exit_code_green_when_all_present(tmp_path: Path) -> None:
    (tmp_path / "test_y.py").write_text('pytest.importorskip("json")\n', encoding="utf-8")
    assert guard.main([str(tmp_path)]) == 0


def test_real_xvfb_suite_dependencies_are_known() -> None:
    """У каждой зависимости xvfb-тестов есть подсказка «какой пакет ставить»."""
    modules = guard.required_modules([ROOT / "tests" / "xvfb"])
    unknown = [m for m in modules if m not in guard.APT_HINT]
    assert not unknown, f"дополните APT_HINT: {unknown}"


def test_ci_installs_every_xvfb_dependency() -> None:
    """Задача `xvfb` обязана ставить пакеты для всех её зависимостей."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = workflow.split("  xvfb:", 1)[1].split("\n  ci-lint:", 1)[0]
    for module in guard.required_modules([ROOT / "tests" / "xvfb"]):
        package = guard.APT_HINT[module]
        assert package in job, f"{module} требует apt-пакет {package} в задаче xvfb"


def test_guard_ignores_its_own_fixtures() -> None:
    """Регресс: гейт не спотыкается о фикстуры собственного теста (4a23129)."""
    modules = guard.required_modules([ROOT / "tests" / "unit"])
    assert "astra_voice_no_such_module" not in modules
    # …но настоящие зависимости соседних тестов по-прежнему видны.
    assert "numpy" in modules


def test_marker_excludes_only_marked_file(tmp_path: Path) -> None:
    (tmp_path / "test_fixture_holder.py").write_text(
        f'"""{guard.SKIP_FILE_MARKER}"""\npytest.importorskip("не_должен_попасть")\n',
        encoding="utf-8",
    )
    (tmp_path / "test_real.py").write_text('pytest.importorskip("json")\n', encoding="utf-8")
    assert guard.required_modules([tmp_path]) == ["json"]


def test_whole_test_tree_is_green() -> None:
    """Полный прогон гейта по репозиторию — 0 (как в CI)."""
    assert guard.main([str(ROOT / "tests" / "unit"), str(ROOT / "tests" / "xvfb")]) == 0
