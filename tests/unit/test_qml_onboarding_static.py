"""Текстовые ограничения онбординга: чтение QML без импорта Qt."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit
REPO = Path(__file__).resolve().parents[2]
ONBOARDING = REPO / "qml/onboarding"
EXPECTED_FILES = (
    "Onboarding.qml",
    "Step1Network.qml",
    "Step2Model.qml",
    "Step3Hotkey.qml",
    "Step4Mic.qml",
    "Step5Done.qml",
)
ONBOARDING_FILES = sorted(ONBOARDING.glob("*.qml"))
COMPONENTS = [
    REPO / "qml/components" / f"{name}.qml"
    for name in (
        "NoteBanner",
        "CaptureField",
        "OnboardingModelCard",
        "DownloadStrip",
        "LevelMeter",
        "AvDialog",
    )
]
UI_FILES = ONBOARDING_FILES + COMPONENTS
MAIN = REPO / "qml/Main.qml"
GENERAL = REPO / "qml/sections/General.qml"
QSTR = re.compile(r'\bqsTr\s*\(\s*"((?:\\.|[^"\\])*)"', re.DOTALL)
STRINGS_OR_COMMENT = re.compile(r""""(?:\\.|[^"\\\n])*"|'(?:\\.|[^'\\\n])*'|//[^\n]*""")
FORBIDDEN = (
    "/usr",
    "/etc",
    "~/",
    "alsa",
    "pulseaudio",
    "pipewire",
    "wireplumber",
    ".service",
    "systemctl",
    "sof-",
    "hw:",
    "plughw",
)


def without_comments(text: str) -> str:
    """Убирает строки // и хвосты // вне одинарных/двойных строк с экранированием.

    Это построчное приближение, не парсер QML/JS: блоки /* ... */, шаблонные строки и
    литералы регулярных выражений специально не разбираются. Переносы сохранены.
    """
    return STRINGS_OR_COMMENT.sub(
        lambda match: "" if match.group().startswith("//") else match.group(), text
    )


def source(path: Path) -> str:
    return without_comments(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("name", EXPECTED_FILES)
def test_onboarding_files_exist_and_are_not_empty(name: str) -> None:
    path = ONBOARDING / name
    assert path.is_file(), f"{path}: файл отсутствует"
    assert path.read_text(encoding="utf-8").strip(), f"{path}: пустой файл"


@pytest.mark.parametrize("path", UI_FILES, ids=lambda path: path.name)
def test_user_text_has_no_technical_words(path: Path) -> None:
    failures = [
        f"{path}: {literal!r} содержит {word!r}"
        for literal in QSTR.findall(source(path))
        for word in FORBIDDEN
        if word.casefold() in literal.casefold()
    ]
    assert not failures, "\n".join(failures)


@pytest.mark.parametrize("path", sorted(ONBOARDING.glob("Step*.qml")), ids=lambda path: path.name)
def test_steps_declare_bar_contract(path: Path) -> None:
    text = source(path)
    for declaration in (r"\bproperty\s+string\s+barHint\b", r"\bproperty\s+bool\s+skipEnabled\b"):
        assert re.search(declaration, text), f"{path}: нет объявления {declaration}"


@pytest.mark.parametrize(
    ("path", "identifier"),
    [(path, "onboarding") for path in UI_FILES]
    + [(MAIN, "settingsBridge"), (GENERAL, "settingsBridge"), (MAIN, "showOnboarding")],
    ids=[f"{path.name}-onboarding" for path in UI_FILES]
    + ["Main-settingsBridge", "General-settingsBridge", "Main-showOnboarding"],
)
def test_context_identifiers_are_guarded(path: Path, identifier: str) -> None:
    guard = f'typeof {identifier} !== "undefined"'
    failures = [
        f"{path}:{number}: {line.strip()}"
        for number, line in enumerate(source(path).splitlines(), start=1)
        if re.search(rf"\b{re.escape(identifier)}\b", line) and guard not in line
    ]
    assert not failures, f"нет защиты {guard}:\n" + "\n".join(failures)


@pytest.mark.parametrize("path", UI_FILES + [MAIN, GENERAL], ids=lambda path: path.name)
def test_colors_come_from_theme(path: Path) -> None:
    failures = [
        f"{path}:{number}: {line.strip()}"
        for number, line in enumerate(source(path).splitlines(), start=1)
        if re.search(r"#[0-9A-Fa-f]{3,8}|\bQt\.rgba\s*\(", line)
    ]
    assert not failures, "цветовые литералы:\n" + "\n".join(failures)


def test_onboarding_step_labels() -> None:
    path = ONBOARDING / "Onboarding.qml"
    text = source(path)
    for label in ("Шаг %1 из %2", "Сеть", "Модель", "Горячая клавиша", "Микрофон", "Готово"):
        assert label in text, f"{path}: нет подписи {label!r}"


@pytest.mark.parametrize(
    ("component", "states"),
    [
        (
            "CaptureField",
            ("idle", "capturing", "captured", "success", "conflict", "duplicate", "not-grabbed"),
        ),
        (
            "OnboardingModelCard",
            (
                "available",
                "queued",
                "downloading",
                "verifying",
                "installed",
                "failed",
                "no-space",
            ),
        ),
        (
            "DownloadStrip",
            ("idle", "downloading", "verifying", "done", "failed", "no-space"),
        ),
    ],
)
def test_component_state_names(component: str, states: tuple[str, ...]) -> None:
    path = REPO / "qml/components" / f"{component}.qml"
    text = source(path)
    for state in states:
        assert f'"{state}"' in text or f"'{state}'" in text, f"{path}: нет состояния {state!r}"


def test_finish_button_mentions_can_finish() -> None:
    path = ONBOARDING / "Onboarding.qml"
    text = source(path)
    assert "Готово" in text and re.search(r"\bcanFinish\b", text), (
        f"{path}: для кнопки «Готово» нужен canFinish"
    )


# ИБ-12: разбираем границы объектов и полные выражения, включая переносы и .arg().
# Строки (в том числе URL и скобки внутри них) — неделимые токены; комментарии удалены.
QML_TOKEN = re.compile(
    r"""//[^\n]*|/\*.*?\*/|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|"""
    r"`(?:\\.|[^`\\])*`|[A-Za-z_$][\w$]*|[^\s]",
    re.DOTALL,
)
QML_STRING = re.compile(r""""(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'""", re.DOTALL)


@dataclass(frozen=True)
class QmlText:
    line: int
    bindings: dict[str, tuple[str, ...]]


def qml_text_items(text: str) -> list[QmlText]:
    """Text/Label с собственными привязками, без свойств вложенных объектов.

    Это ограниченный лексический разбор QML, без исполнения JavaScript. Неизвестные
    выражения считаем динамическими: исключение допускается только для целого
    строкового литерала или qsTr с единственным строковым аргументом.
    """
    tokens = [
        match for match in QML_TOKEN.finditer(text) if not match.group().startswith(("//", "/*"))
    ]
    values = [token.group() for token in tokens]
    closing: dict[int, int] = {}
    stack: list[int] = []
    pairs = {"}": "{", ")": "(", "]": "["}
    for index, value in enumerate(values):
        if value in ("{", "(", "["):
            stack.append(index)
        elif value in pairs:
            assert stack and values[stack[-1]] == pairs[value], (
                f"несогласованные скобки QML в позиции {tokens[index].start()}"
            )
            closing[stack.pop()] = index
    assert not stack, "незакрытые скобки QML"

    items: list[QmlText] = []
    for index, value in enumerate(values[:-1]):
        if value not in ("Text", "Label") or values[index + 1] != "{":
            continue
        end = closing[index + 1]
        # Сохраняем только верхний уровень; вложенные выражения остаются в срезах.
        direct: list[int] = []
        cursor = index + 2
        while cursor < end:
            direct.append(cursor)
            cursor = closing.get(cursor, cursor) + 1

        bindings: dict[str, tuple[str, ...]] = {}
        for position, start in enumerate(direct[:-1]):
            if values[start] not in ("text", "textFormat") or values[start + 1] != ":":
                continue
            if start > index + 2 and values[start - 1] == ".":
                continue
            finish = end
            for following in direct[position + 2 :]:
                if values[following] == ";":
                    finish = following
                    break
                # Следующий член объекта на новой строке: свойство, функция или ребёнок.
                line_start = text.rfind("\n", 0, tokens[following].start()) + 1
                line_prefix = text[line_start : tokens[following].start()]
                if not line_prefix.strip() and re.match(
                    r"(?:[\w$.]+\s*:|[\w.]+\s*\{|(?:readonly\s+)?property\b|"
                    r"(?:function|signal|component)\b)",
                    text[tokens[following].start() :],
                ):
                    finish = following
                    break
            bindings[values[start]] = tuple(values[start + 2 : finish])
        items.append(QmlText(text.count("\n", 0, tokens[index].start()) + 1, bindings))
    return items


def constant_qml_text(expression: tuple[str, ...]) -> bool:
    if len(expression) == 1:
        return QML_STRING.fullmatch(expression[0]) is not None
    return (
        len(expression) == 4
        and expression[:2] == ("qsTr", "(")
        and QML_STRING.fullmatch(expression[2]) is not None
        and expression[3] == ")"
    )


def unsafe_qml_text_items(text: str) -> list[QmlText]:
    # Text без локального text тоже защищаем: inline-базы (FooterText) получают
    # внешнюю строку в месте использования, а формат наследуется от базы.
    return [
        item
        for item in qml_text_items(text)
        if not constant_qml_text(item.bindings.get("text", ()))
        and item.bindings.get("textFormat") != ("Text", ".", "PlainText")
    ]


def test_ib12_all_dynamic_qml_text_is_plain_text() -> None:
    paths = sorted((REPO / "qml").rglob("*.qml"))
    assert paths, "не найдено ни одного qml/**/*.qml"
    failures = [
        f"{path.relative_to(REPO)}:{item.line}: text требует textFormat: Text.PlainText"
        for path in paths
        for item in unsafe_qml_text_items(path.read_text(encoding="utf-8"))
    ]
    assert not failures, "ИБ-12: возможна загрузка HTML в обход NetworkGate:\n" + "\n".join(
        failures
    )


@pytest.mark.parametrize(
    "expression",
    [
        "root.sub",
        "control.displayText",
        "parent.modelData",
        'qsTr("%1").arg(bridge.deviceResolved)',
        'qsTr("%1")\n    .arg(model.name)',
        '"Модель: "\n    + model.name',
        'root.ready ? qsTr("Готово") : bridge.message',
        '{ if (root.ready) { return model.name; } return ""; }',
        "`Модель: ${model.name}`",
    ],
)
@pytest.mark.parametrize("kind", ["Text", "Label", "Controls.Label"])
def test_ib12_parser_rejects_dynamic_bindings(expression: str, kind: str) -> None:
    body = f"{kind} {{\n    text: {expression}\n    color: theme.fg\n}}"
    assert len(unsafe_qml_text_items(body)) == 1
    protected = body.replace("color:", "textFormat: Text.PlainText\n    color:")
    assert not unsafe_qml_text_items(protected)


@pytest.mark.parametrize("expression", ['"literal"', "'literal'", 'qsTr("Микрофон")'])
def test_ib12_parser_allows_only_constant_text(expression: str) -> None:
    assert not unsafe_qml_text_items(f"Text {{ text: {expression}; color: theme.fg }}")


def test_ib12_inline_text_base_requires_plain_text() -> None:
    body = "component FooterText: Text { color: theme.fg }"
    assert len(unsafe_qml_text_items(body)) == 1
    protected = body.replace("color:", "textFormat: Text.PlainText; color:")
    assert not unsafe_qml_text_items(protected)


def test_ib12_parser_ignores_comments_strings_and_nested_formats() -> None:
    text = """Item {
    // Text { text: bridge.name }
    /* Label { text: model.name } */
    property string decoy: "Text { text: bridge.name }"
    Text {
        text: root.sub // textFormat: Text.PlainText
        /* textFormat: Text.PlainText */
        Text { text: "http://example.test/}\\\"{"; textFormat: Text.PlainText }
    }
    Label { textFormat: Text.AutoText; text: model.name }
    Text { text: qsTr("%1").arg(model.name); textFormat: Text.RichText }
}"""
    assert [item.line for item in unsafe_qml_text_items(text)] == [5, 10, 11]
    assert len(qml_text_items(text)) == 4
