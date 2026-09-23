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
QML_FILES = sorted((REPO / "qml").rglob("*.qml"))
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
    """Прячем строки и /* комментарии */, сохраняя позиции и номера строк.

    Кавычки оставляем: пустая строка в вызове тоже считается аргументом.
    """

    def mask(match: re.Match[str]) -> str:
        token = match.group()
        hidden = "".join("\n" if char == "\n" else " " for char in token)
        return token[0] + hidden[1:-1] + token[-1] if token[0] in "\"'" else hidden

    return re.sub(r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|/\*.*?\*/""", mask, text, flags=re.DOTALL)


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


def context_aliases(text: str, code: str) -> dict[str, str]:
    aliases = {context: context for context in CONTEXTS}
    aliases.update(
        (match["alias"], match["context"])
        for match in ALIAS.finditer(text)
        if code[match.start()] == "r"
    )
    return aliases


def qml_references(path: Path) -> list[QmlReference]:
    text = cast(str, qml_static.source(path))
    code = mask_string_contents(text)
    aliases = context_aliases(text, code)
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


def direct_block_contents(code: str, opening: int) -> str:
    """Тело Connections без вложенных блоков; сохраняем смещения и переносы."""
    depth = 1
    characters: list[str] = []
    for character in code[opening + 1 :]:
        if character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                return "".join(characters)
        characters.append(character if depth == 1 or character == "\n" else " ")
    raise ValueError(f"нет закрывающей скобки Connections в позиции {opening}")


def qml_signal_handlers(path: Path) -> list[QmlReference]:
    text = cast(str, qml_static.source(path))
    code = mask_string_contents(text)
    aliases = context_aliases(text, code)
    names = "|".join(re.escape(name) for name in aliases)
    target = re.compile(
        rf"\btarget\s*:\s*(?:(?:root|window)\s*\.\s*)?(?P<object>{names})"
        rf"(?=\s*(?:;|\n|$))"
    )
    handler = re.compile(r"\bfunction\s+on(?P<signal>[A-Z][\w$]*)\s*\(")
    references: list[QmlReference] = []
    for connection in re.finditer(r"\bConnections\s*\{", code):
        offset = connection.end()
        body = direct_block_contents(code, offset - 1)
        target_match = target.search(body)
        if target_match is None:
            # Connections к QML-объектам и null не относятся к мостам Python.
            continue
        for match in handler.finditer(body):
            signal = match["signal"]
            references.append(
                QmlReference(
                    path=path.relative_to(REPO),
                    line=code.count("\n", 0, offset + match.start()) + 1,
                    context=aliases[target_match["object"]],
                    member=signal[0].lower() + signal[1:],
                    argument_count=argument_count(body, match.end() - 1),
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


def signal_contract_error(reference: QmlReference, contract: MetaContract) -> str | None:
    signals = [method for method in contract.methods if method.is_signal]
    matching = [signal for signal in signals if signal.name == reference.member]
    assert reference.argument_count is not None
    if not matching:
        reason = f"нет сигнала в {contract.class_name}"
    elif not any(reference.argument_count <= signal.parameter_count for signal in matching):
        expected = " или ".join(
            str(count) for count in sorted({s.parameter_count for s in matching})
        )
        reason = (
            f"у сигнала {contract.class_name} {expected} параметров, "
            f"у обработчика {reference.argument_count}"
        )
    else:
        return None
    available = ", ".join(sorted(signal.signature for signal in signals)) or "нет"
    return (
        f"{reference.path}:{reference.line}: {reference.context}.{reference.member} — {reason}; "
        f"доступные сигналы {contract.class_name}: {available}"
    )


def test_qml_signal_handlers_match_real_bridges(real_contracts: dict[str, MetaContract]) -> None:
    failures = [
        error
        for path in QML_FILES
        for reference in qml_signal_handlers(path)
        if (error := signal_contract_error(reference, real_contracts[reference.context]))
    ]
    assert not failures, "Обработчики QML не соответствуют сигналам мостов:\n" + "\n".join(failures)


@pytest.mark.parametrize(
    ("target", "context"),
    [
        ("window.info", "appInfo"),
        ("info", "appInfo"),
        ("root.bridge", "onboarding"),
        ("appInfo", "appInfo"),
        ("onboarding", "onboarding"),
        ("settingsBridge", "settingsBridge"),
    ],
)
def test_signal_parser_resolves_targets_and_block_boundaries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, target: str, context: str
) -> None:
    monkeypatch.setattr(sys.modules[__name__], "REPO", tmp_path)
    path = tmp_path / "Nested.qml"
    text = """Item {
    readonly property var info: (typeof appInfo !== "undefined") ? appInfo : null
    readonly property var bridge: (typeof onboarding !== "undefined") ? onboarding : null
    property string decoy: "Connections { target: appInfo; function onFake() {} }"
    // Connections { target: appInfo; function onFake() {} }
    /* Connections { target: appInfo; function onFake() { } */
    Connections {
        function onShowSection(section) {
            if (section) { var text = "} target: onboarding {" }
            function onNested(wrong, extra) {}
        }
        function onDebugChanged() {}
        target: TARGET
    }
    Connections { target: localObject; function onUnrelated(a, b) {} }
    Connections { target: null; function onUnrelated(a, b) {} }
    Connections { target: appInfo; function onShowSection() {} }
}""".replace("TARGET", target)
    path.write_text(text, encoding="utf-8")
    references = qml_signal_handlers(path)
    assert [(ref.context, ref.member, ref.argument_count) for ref in references] == [
        (context, "showSection", 1),
        (context, "debugChanged", 0),
        ("appInfo", "showSection", 0),
    ]
    assert all(ref.path == Path("Nested.qml") for ref in references)
    assert [ref.line for ref in references] == [
        number
        for number, line in enumerate(text.splitlines(), 1)
        if "function onShowSection" in line or "function onDebugChanged" in line
    ]


@pytest.mark.parametrize(
    ("context", "signal", "count", "reason"),
    [
        ("appInfo", "showSection", 0, None),
        ("appInfo", "showSection", 1, None),
        ("appInfo", "showSection", 2, "у обработчика 2"),
        ("appInfo", "showSectionX", 1, "нет сигнала"),
        ("appInfo", "version", 0, "нет сигнала"),
        ("settingsBridge", "retryHotkey", 0, "нет сигнала"),
    ],
)
def test_signal_contract_checks_kind_and_parameter_count(
    real_contracts: dict[str, MetaContract],
    context: str,
    signal: str,
    count: int,
    reason: str | None,
) -> None:
    reference = QmlReference(Path("qml/Example.qml"), 12, context, signal, count)
    contract = real_contracts[context]
    error = signal_contract_error(reference, contract)
    if reason is None:
        assert error is None
    else:
        assert error is not None
        assert f"qml/Example.qml:12: {context}.{signal}" in error
        assert reason in error
        assert f"доступные сигналы {contract.class_name}:" in error
        assert all(method.signature in error for method in contract.methods if method.is_signal)


def test_xvfb_fakes_match_real_bridges(real_contracts: dict[str, MetaContract]) -> None:
    fakes = load_module("_qml_bridge_contract_fakes", REPO / "tests/xvfb/test_onboarding.py")
    failures: list[str] = []
    for fake, context in (
        (fakes.FakeOnboarding, "onboarding"),
        (fakes.FakeSettings, "settingsBridge"),
        (fakes.FakeAppInfo, "appInfo"),
    ):
        declared = meta_contract(fake.staticMetaObject, own_only=True)
        real = real_contracts[context]
        failures.extend(
            f"{declared.class_name}.{name} — нет свойства в {real.class_name}"
            for name in sorted(declared.properties - real.properties)
        )
        if context == "settingsBridge":
            failures.extend(
                f"{real.class_name}.{name} — нет свойства в {declared.class_name}"
                for name in sorted(real.properties - declared.properties - DOC_UNLISTED)
            )
        real_methods = {method.signature for method in real.methods if not method.is_signal}
        if context == "settingsBridge":
            fake_methods = {method.signature for method in declared.methods if not method.is_signal}
            failures.extend(
                f"{real.class_name}.{name} — нет метода в {declared.class_name}"
                for name in sorted(real_methods - fake_methods)
                if name.split("(", 1)[0] not in DOC_UNLISTED and not name.startswith("_")
            )
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


def test_app_info_fake_property_and_signal_signatures() -> None:
    from astra_voice.app import _make_app_info
    from astra_voice.core.policy import PolicyStatus
    from astra_voice.platform.session import SessionKind

    fakes = load_module("_qml_app_info_contract_fakes", REPO / "tests/xvfb/test_onboarding.py")
    real = _make_app_info(SessionKind.OTHER, PolicyStatus.ABSENT.value)
    real_meta = real.metaObject()
    fake_meta = fakes.FakeAppInfo.staticMetaObject
    assert meta_contract(fake_meta, own_only=True).properties == {"version", "sessionKind", "debug"}
    for index in range(fake_meta.propertyOffset(), fake_meta.propertyCount()):
        declared = fake_meta.property(index)
        actual = real_meta.property(real_meta.indexOfProperty(declared.name()))
        assert declared.typeName() == actual.typeName()
        assert declared.isConstant() == actual.isConstant()
        assert declared.isWritable() == actual.isWritable()
        assert declared.notifySignal().methodSignature() == actual.notifySignal().methodSignature()
    declared_methods = meta_contract(fake_meta, own_only=True).methods
    assert {method.signature for method in declared_methods} == {
        "debugChanged()",
        "showSection(QString)",
    }
    assert set(declared_methods) <= set(meta_contract(real_meta, own_only=True).methods)


DOC_PATH = REPO / "docs" / "ui-bridge.md"
#: Типы, которыми документ размечает строки-свойства в таблицах.
DOC_PROPERTY_TYPES = frozenset({"string", "bool", "real", "int", "QVariantList", "QStringList"})
#: Служебные члены QObject: к контракту с QML они не относятся.
DOC_UNLISTED = frozenset(
    {"destroyed", "objectName", "objectNameChanged", "deleteLater", "parent", "children"}
)


def documented_names(text: str) -> tuple[frozenset[str], frozenset[str]]:
    """Имена свойств и слотов, которые документ объявляет частью контракта."""
    properties: set[str] = set()
    slots: set[str] = set()
    for line in text.split("\n"):
        if not line.startswith("| `"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        name = cells[0].strip("`")
        if name.endswith("()"):
            slots.add(name[:-2])
        elif len(cells) > 1 and cells[1].strip("`") in DOC_PROPERTY_TYPES:
            properties.add(name)
    return frozenset(properties), frozenset(slots)


def test_documented_members_exist_in_bridges(real_contracts: dict[str, MetaContract]) -> None:
    """Документ не должен обещать того, чего в мостах нет (§7 ui-bridge.md)."""
    text = DOC_PATH.read_text(encoding="utf-8")
    documented_properties, documented_slots = documented_names(text)
    known_properties = {
        name for contract in real_contracts.values() for name in contract.properties
    }
    known_methods = {
        method.name for contract in real_contracts.values() for method in contract.methods
    }
    # Документ описывает и контекстные свойства окна, и поля записей models[]:
    # проверяем только те имена, которые выглядят как члены мостов.
    missing = {
        name
        for name in documented_properties
        if name not in known_properties and name in known_methods
    }
    assert not missing, f"Свойства из документа отсутствуют в мостах: {sorted(missing)}"
    absent_slots = documented_slots - known_methods
    assert not absent_slots, f"Слоты из документа отсутствуют в мостах: {sorted(absent_slots)}"


def test_bridge_members_are_documented(real_contracts: dict[str, MetaContract]) -> None:
    """Добавленный в мост член обязан попасть в документ — иначе он не контракт."""
    text = DOC_PATH.read_text(encoding="utf-8")
    undocumented: list[str] = []
    for key in ("onboarding", "settingsBridge"):
        contract = real_contracts[key]
        for name in sorted(contract.properties):
            if name in DOC_UNLISTED or name.startswith("_") or f"`{name}`" in text:
                continue
            undocumented.append(f"{key}.{name}")
        for method in contract.methods:
            name = method.name
            if name in DOC_UNLISTED or name.startswith("_") or name.endswith("Changed"):
                continue
            # В таблице слотов имя стоит с аргументами: `installFromPath(path)`.
            if f"`{name}`" in text or f"`{name}(" in text:
                continue
            undocumented.append(f"{key}.{name}()")
    assert not undocumented, "Эти члены мостов не описаны в docs/ui-bridge.md: " + ", ".join(
        sorted(set(undocumented))
    )


def test_model_measurement_card_contract() -> None:
    document = DOC_PATH.read_text(encoding="utf-8")
    section = (REPO / "qml/sections/Models.qml").read_text(encoding="utf-8")
    card = (REPO / "qml/components/OnboardingModelCard.qml").read_text(encoding="utf-8")
    for field in ("ramMb", "ramMeasured", "speedKind", "speedText", "speedValue", "qualityValue"):
        assert f"| `{field}` |" in document
        assert f"entry.{field}" in section
        assert f"root.{field}" in card
