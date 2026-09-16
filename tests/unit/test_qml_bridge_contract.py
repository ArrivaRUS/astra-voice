"""Контракт QML и фейков xvfb сверяем с метаобъектами настоящих мостов Qt."""

from __future__ import annotations

import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING, cast

import pytest

if TYPE_CHECKING:
    from PyQt5.QtCore import QMetaObject

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]
QML_FILES = sorted((REPO / "qml/onboarding").glob("*.qml")) + [
    REPO / "qml/sections/General.qml",
    REPO / "qml/Main.qml",
]
CONTEXTS = ("onboarding", "settingsBridge", "appInfo")
IDENTIFIER = r"[A-Za-z_$][\w$]*"
ALIAS = re.compile(
    rf"\breadonly\s+property\s+var\s+(?P<alias>{IDENTIFIER})\s*:\s*\(\s*"
    rf"typeof\s+(?P<context>{'|'.join(CONTEXTS)})\s*!==\s*(?:\"undefined\"|'undefined')"
)


def load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec and spec.loader, f"не удалось загрузить модуль {path}"
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# source уже вызывает without_comments; при разборе QML Qt не импортируется.
qml_static = load_module(
    "_qml_bridge_contract_static", REPO / "tests/unit/test_qml_onboarding_static.py"
)


@dataclass(frozen=True)
class QmlReference:
    path: Path
    line: int
    context: str
    member: str
    # None означает обращение к члену без вызова, 0 — вызов с пустыми скобками.
    argument_count: int | None


def mask_string_contents(text: str) -> str:
    """Прячем содержимое строк, сохраняя кавычки, позиции и номера строк."""
    characters = list(text)
    quote: str | None = None
    escaped = False
    for index, character in enumerate(text):
        if quote is None:
            if character in "\"'":
                quote = character
        elif not escaped and character == quote:
            quote = None
        else:
            if character != "\n":
                characters[index] = " "
            escaped = not escaped and character == "\\"
    return "".join(characters)


def argument_count(text: str, opening: int) -> int:
    """Считаем запятые вне вложенных (), [] и {}; содержимое строк уже скрыто."""
    brackets = {"(": ")", "[": "]", "{": "}"}
    closing = [")"]
    commas = 0
    for index in range(opening + 1, len(text)):
        character = text[index]
        if character in brackets:
            closing.append(brackets[character])
        elif character in ")]}":
            if character != closing.pop():
                raise ValueError(f"несогласованные скобки вызова в позиции {opening}")
            if not closing:
                arguments = text[opening + 1 : index].strip()
                return commas + 1 - int(arguments.endswith(",")) if arguments else 0
        elif character == "," and len(closing) == 1:
            commas += 1
    raise ValueError(f"нет закрывающей скобки вызова в позиции {opening}")


def qml_references(path: Path) -> list[QmlReference]:
    text = cast(str, qml_static.source(path))
    code = mask_string_contents(text)
    aliases = {context: context for context in CONTEXTS}
    aliases.update(
        (match["alias"], match["context"])
        for match in ALIAS.finditer(text)
        if code[match.start()] == "r"
    )
    names = "|".join(re.escape(name) for name in aliases)
    member = re.compile(
        rf"(?<![\w$.])(?:(?:root|window)\s*\.\s*)?"
        rf"(?P<object>{names})\s*\.\s*(?P<member>{IDENTIFIER})"
    )
    references: list[QmlReference] = []
    for match in member.finditer(code):
        following = match.end()
        while following < len(code) and code[following].isspace():
            following += 1
        count = (
            argument_count(code, following)
            if following < len(code) and code[following] == "("
            else None
        )
        references.append(
            QmlReference(
                path=path.relative_to(REPO),
                line=code.count("\n", 0, match.start()) + 1,
                context=aliases[match["object"]],
                member=match["member"],
                argument_count=count,
            )
        )
    return references


@dataclass(frozen=True)
class MetaMethod:
    name: str
    signature: str
    parameter_count: int
    is_signal: bool


@dataclass(frozen=True)
class MetaContract:
    class_name: str
    properties: frozenset[str]
    methods: tuple[MetaMethod, ...]


def meta_contract(meta: QMetaObject, *, own_only: bool = False) -> MetaContract:
    from PyQt5.QtCore import QMetaMethod

    property_start = meta.propertyOffset() if own_only else 0
    method_start = meta.methodOffset() if own_only else 0
    return MetaContract(
        class_name=meta.className(),
        properties=frozenset(
            meta.property(index).name() for index in range(property_start, meta.propertyCount())
        ),
        methods=tuple(
            MetaMethod(
                name=bytes(method.name()).decode(),
                signature=bytes(method.methodSignature()).decode(),
                parameter_count=method.parameterCount(),
                is_signal=method.methodType() == QMetaMethod.Signal,
            )
            for index in range(method_start, meta.methodCount())
            for method in [meta.method(index)]
        ),
    )


@pytest.fixture
def real_contracts(monkeypatch: pytest.MonkeyPatch) -> dict[str, MetaContract]:
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")

    from astra_voice.app import _make_app_info
    from astra_voice.core.policy import PolicyStatus
    from astra_voice.platform.session import SessionKind
    from astra_voice.ui.bridges import OnboardingController, SettingsBridge

    # QApplication и экземпляры мостов не нужны; AppInfo объявлен внутри фабрики.
    app_info = _make_app_info(SessionKind.OTHER, PolicyStatus.ABSENT.value)
    return {
        "onboarding": meta_contract(OnboardingController.staticMetaObject),
        "settingsBridge": meta_contract(SettingsBridge.staticMetaObject),
        "appInfo": meta_contract(app_info.metaObject()),
    }


def test_qml_members_match_real_bridges(real_contracts: dict[str, MetaContract]) -> None:
    failures: list[str] = []
    for path in QML_FILES:
        for reference in qml_references(path):
            contract = real_contracts[reference.context]
            methods = [method for method in contract.methods if method.name == reference.member]
            location = f"{reference.path}:{reference.line}: {reference.context}.{reference.member}"
            if reference.member not in contract.properties and not methods:
                failures.append(f"{location} — нет в {contract.class_name}")
            elif reference.argument_count is not None:
                if not methods:
                    failures.append(f"{location} — нет вызываемого метода в {contract.class_name}")
                    continue
                counts = {method.parameter_count for method in methods}
                if reference.argument_count not in counts:
                    expected = " или ".join(str(count) for count in sorted(counts))
                    failures.append(
                        f"{location} — ожидалось {expected} аргументов, "
                        f"в QML {reference.argument_count}"
                    )
    assert not failures, "QML не соответствует метаобъектам мостов:\n" + "\n".join(failures)


def test_xvfb_fakes_match_real_bridges(real_contracts: dict[str, MetaContract]) -> None:
    fakes = load_module("_qml_bridge_contract_fakes", REPO / "tests/xvfb/test_onboarding.py")
    failures: list[str] = []
    for fake, context in (
        (fakes.FakeOnboarding, "onboarding"),
        (fakes.FakeSettings, "settingsBridge"),
    ):
        declared = meta_contract(fake.staticMetaObject, own_only=True)
        real = real_contracts[context]
        failures.extend(
            f"{declared.class_name}.{name} — нет свойства в {real.class_name}"
            for name in sorted(declared.properties - real.properties)
        )
        real_methods = {method.signature for method in real.methods if not method.is_signal}
        for method in declared.methods:
            # Собственный notify-сигнал фейка не является частью контракта моста.
            if method.is_signal or method.signature in real_methods:
                continue
            alternatives = sorted(
                candidate.signature
                for candidate in real.methods
                if not candidate.is_signal and candidate.name == method.name
            )
            detail = f" (есть {', '.join(alternatives)})" if alternatives else ""
            failures.append(
                f"{declared.class_name}.{method.signature} — нет в {real.class_name}{detail}"
            )
    assert not failures, "Фейки xvfb не соответствуют метаобъектам мостов:\n" + "\n".join(failures)
