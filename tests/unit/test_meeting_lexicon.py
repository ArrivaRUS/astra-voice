"""F18.12(в): public text only; source comments and Python docstrings excluded."""

from __future__ import annotations

import ast
import io
import re
import tokenize
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN = re.compile("перехват", re.IGNORECASE)
CYRILLIC = re.compile("[А-Яа-яЁё]")
QML_COMMENTS = re.compile(
    r"""(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*')|(?P<comment>//[^\n]*|/\*[\s\S]*?\*/)"""
)


def qml_text(source: str) -> str:
    return QML_COMMENTS.sub(
        lambda match: (
            match["string"] if match["string"] is not None else "\n" * match[0].count("\n") + " "
        ),
        source,
    )


def python_texts(source: str) -> list[tuple[int, str]]:
    # Tokenization preserves '#' in strings and handles both pre-3.12 and PEP701
    # f-strings. Parse the comment-free stream, then omit actual docstring nodes.
    source = tokenize.untokenize(
        token
        for token in tokenize.generate_tokens(io.StringIO(source).readline)
        if token.type != tokenize.COMMENT
    )
    tree = ast.parse(source)
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            if (
                node.body
                and isinstance(node.body[0], ast.Expr)
                and isinstance(node.body[0].value, ast.Constant)
                and isinstance(node.body[0].value.value, str)
            ):
                docs.add(node.body[0].value)
    return [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node not in docs
        and CYRILLIC.search(node.value)
    ]


def public_paths(root: Path) -> set[Path]:
    files = {
        root / name
        for name in (
            "README.md",
            "docs/PRIVACY.md",
            "docs/INSTALL-ADMIN.md",
            "CHANGELOG.md",
            "packaging/make_control.py",
        )
        if (root / name).is_file()
    }
    for pattern in (
        "qml/**/*.qml",
        "src/astra_voice/**/*.py",
        "src/**/*.ts",
        "src/astra_voice/i18n*",
        "docs/admin/**",
        "docs/release-notes*",
        "packaging/debian/*",
    ):
        for path in root.glob(pattern):
            if path.is_file():
                files.add(path)
            elif path.is_dir():
                files.update(p for p in path.rglob("*") if p.is_file())
    return files


def violations(path: Path) -> list[tuple[int, str]]:
    source = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        texts = python_texts(source)
    else:
        if path.suffix == ".qml":
            source = qml_text(source)
        texts = list(enumerate(source.splitlines(), 1))
    return [(line, text) for line, text in texts if FORBIDDEN.search(text)]


def test_public_meeting_lexicon() -> None:
    found = [
        f"{path.relative_to(ROOT)}:{line}: {text}"
        for path in sorted(public_paths(ROOT))
        for line, text in violations(path)
    ]
    assert not found, "\n".join(found)


def test_lexicon_scanner_excludes_comments_and_docstrings_but_keeps_literals() -> None:
    source = '''"""Перехватывать: внутреннее описание."""
# перехват
class A:
    """перехват"""
    def f(self):
        """перехват"""
        return "ПЕРЕХВАТЫВАЕТ", f"Перехватили {1}", "\\u043fерехват"
'''
    texts = python_texts(source)
    assert len(texts) == 3
    assert all(FORBIDDEN.search(text) for _, text in texts)
    assert qml_text('// перехват\ntext: "https://host/ПЕРЕХВАТ" // перехват\n/* перехват */') == (
        ' \ntext: "https://host/ПЕРЕХВАТ"  \n '
    )
    assert not FORBIDDEN.search(qml_text("// перехват\n/* ПЕРЕХВАТ */"))


def test_lexicon_scope_includes_public_docs_and_excludes_internal_docs(tmp_path: Path) -> None:
    public = (
        "docs/admin/nested/example.md",
        "docs/release-notes/v1.md",
        "src/astra_voice/i18n/ru.ts",
        "packaging/debian/control",
        "README.md",
    )
    internal = (
        "docs/plans.md",
        "docs/test-plan.md",
        "docs/threat-model.md",
        "PRD.md",
        "arch/note.md",
        "decisions/log.md",
        ".patches/INDEX.md",
        "spikes/readme.md",
    )
    for name in (*public, *internal):
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("перехват", encoding="utf-8")
    assert public_paths(tmp_path) == {tmp_path / name for name in public}
