"""Текстовые ограничения онбординга: чтение QML без импорта Qt."""

from __future__ import annotations

import re
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
    for name in ("NoteBanner", "CaptureField", "OnboardingModelCard", "LevelMeter", "AvDialog")
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
                "downloadable",
                "downloading",
                "verifying",
                "installing",
                "installed",
                "broken",
                "no-network",
                "no-space",
                "no-ram",
                "cancelled",
            ),
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
