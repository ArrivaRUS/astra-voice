"""В M2–M4 пакет был неработоспособен: импорты dev-дерева оставались зелёными."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from conftest import import_names, import_prefixes

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[2]


def test_imports_exist_in_deb_layout() -> None:
    """Импорты исходников не должны ссылаться на вынесенные из пакета модули."""
    rules = (ROOT / "packaging/debian/rules").read_text(encoding="utf-8")
    target = re.search(
        r"^override_dh_auto_install:[^\n]*\n(?P<recipe>(?:\t[^\n]*\n|\n|#[^\n]*\n)*)",
        rules,
        re.MULTILINE,
    )
    assert target is not None, "в packaging/debian/rules нет override_dh_auto_install"
    package_dir = "$(LIBDIR)/astra_voice/"
    moved = {
        source.removeprefix(package_dir)
        for source, destination in re.findall(
            r'\bmv\s+["\']?(\$\(LIBDIR\)/astra_voice/[^\s"\';]+)["\']?'
            r'\s+["\']?([^\s"\';]+)',
            target["recipe"],
        )
        if not destination.startswith(package_dir)
        and destination.rstrip("/") != package_dir.rstrip("/")
    }

    package_path = ROOT / "src/astra_voice"
    modules: dict[str, Path] = {}
    imports: dict[Path, set[str]] = {}
    for path in sorted(package_path.rglob("*.py")):
        parts = path.relative_to(package_path.parent).with_suffix("").parts
        is_package = parts[-1] == "__init__"
        module = ".".join(parts[:-1] if is_package else parts)
        package = module if is_package else module.rpartition(".")[0]
        modules[module] = path
        imports[path] = {
            name
            for name in import_prefixes(import_names(path.read_text(encoding="utf-8"), package))
            if name.startswith("astra_voice.")
        }

    missing = {
        module
        for module, path in modules.items()
        if path.relative_to(package_path).as_posix() in moved
    }
    failures = [
        f"{path.relative_to(ROOT)}: {name} отсутствует в установленном пакете"
        for path, names in imports.items()
        for name in sorted(names & modules.keys() & missing)
    ]
    assert not failures, "\n".join(failures)
