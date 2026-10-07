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


def test_if_exists_skips_absent_dir(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Каталог из --if-exists, которого ещё нет (ветка не влита), не ошибка; есть — сканируется."""
    unit = tmp_path / "unit"
    unit.mkdir()
    (unit / "test_a.py").write_text('pytest.importorskip("json")\n', encoding="utf-8")
    absent = tmp_path / "updates"
    assert guard.main([str(unit), "--if-exists", str(absent)]) == 0
    assert "каталога ещё нет — пропуск" in capsys.readouterr().out
    net = tmp_path / "net"
    net.mkdir()
    (net / "test_n.py").write_text('pytest.importorskip("no_such_module_xyz")\n', "utf-8")
    assert guard.main([str(unit), "--if-exists", str(net), str(absent)]) == 1


def test_absent_positional_path_is_error(tmp_path: Path) -> None:
    """Опечатка в обязательном пути не должна молча выключать гейт."""
    assert guard.main([str(tmp_path / "typo")]) == 2


@pytest.mark.parametrize(
    "body",
    [
        "raise RuntimeError('broken dependency')",
        "import nonexistent_nested_dependency",
        "raise SystemExit(0)",
    ],
)
def test_module_found_but_import_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    (tmp_path / "broken_dependency.py").write_text(body, encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert importlib.util.find_spec("broken_dependency") is not None
    errors = guard.import_errors(["broken_dependency"])
    assert "broken_dependency" in errors
    assert "код 1" in errors["broken_dependency"]
    assert "broken_dependency" not in sys.modules


def test_import_timeout_is_controlled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "slow_dependency.py").write_text("import time; time.sleep(30)", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(guard, "IMPORT_TIMEOUT", 0.1)
    errors = guard.import_errors(["slow_dependency", "sys"])
    assert list(errors) == ["slow_dependency"]
    assert "не завершился" in errors["slow_dependency"]


def test_import_failure_is_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "bad_module.py").write_text("raise ValueError('library ABI mismatch')", "utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    assert guard.main([str(tmp_path), "--also", "bad_module"]) == 1
    assert "ValueError: library ABI mismatch" in capsys.readouterr().err
