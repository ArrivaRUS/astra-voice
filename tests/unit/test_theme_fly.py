"""US-4.7, ревизия 20 — тема Fly (T-290…T-297). Тесты написаны ДО кода, 21.10.

Источник 1 — `~/.fly/paletterc` `[Variables]`: `ColorScheme` — путь к файлу `.colors`,
`BackgroundColor` — запасной фон, `PrimaryColor` — акцент. Источник 2 —
`~/.config/fly-admin-themerc` `[UiSettings] ColorScheme=<имя>` → поиск файла схемы по
`Name`/`Name[ru]` в каталоге схем. Тёмность — только по светлоте `[Colors:Window]
BackgroundNormal` (luma < 128), слово «Темная» в имени признаком не служит.

Контракт конструктора (решение Юрки 21.10, тесты его фиксируют)::

    FlyThemeSource(
        path=None,                 # paletterc, по умолчанию fly_palette_path()
        *,
        admin_themerc=None,        # по умолчанию fly_admin_themerc_path()
        schemes_dir=None,          # по умолчанию /usr/share/color-schemes (поиск по имени)
        user_schemes_dir=None,     # по умолчанию ~/.local/share/color-schemes
    )

Фикстуры — копии форматов схем ноутбука ALSE 21.10 (AstraDark/AstraLight/
AstraProximaAdmin — hex, BreezeDark/ZionReversed1 — r,g,b, FlyDefault — без
ForegroundNormal). В каждой схеме `[Colors:Button]` идёт раньше `[Colors:Window]` и
имеет противоположную светлоту: чтение «первого BackgroundNormal» тест поймает.
Системные пути не читаются: каталоги схем — во `tmp_path`.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from astra_voice.core import theme as theme_mod
from astra_voice.core.theme import FlyThemeSource

pytestmark = pytest.mark.unit

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PRIMARY = "#118bcc"
SECRET_NAME = "Секретная схема 42"


def scheme(
    name: str,
    window_bg: str,
    *,
    name_ru: str | None = None,
    window_fg: str | None = None,
    selection_bg: str = "#2FB3FA",
    button_bg: str | None = None,
) -> str:
    """Файл `.colors` в формате KDE/Astra: секции по алфавиту, как в поставке."""
    lines = ["[ColorEffects:Disabled]", "Color=56,56,56", ""]
    lines += ["[Colors:Button]", f"BackgroundNormal={button_bg or window_bg}", ""]
    lines += ["[Colors:Selection]", f"BackgroundNormal={selection_bg}", ""]
    lines += ["[Colors:Window]", f"BackgroundNormal={window_bg}"]
    if window_fg is not None:
        lines.append(f"ForegroundNormal={window_fg}")
    lines += ["", "[General]", "ColorScheme=" + name.replace(" ", ""), f"Name={name}"]
    lines.append("Name[ar]=اسم")
    if name_ru is not None:
        lines.append(f"Name[ru]={name_ru}")
    return "\n".join(lines) + "\n"


# Копии форматов /usr/share/color-schemes ноутбука ALSE (21.10).
SCHEMES: dict[str, str] = {
    "AstraDark.colors": scheme(
        "Astra Proxima Dark",
        "#262626",
        name_ru="Astra Proxima Темная",
        window_fg="#F7F7F7",
        button_bg="#FFFFFF",
    ),
    "AstraLight.colors": scheme(
        "Astra Proxima Light",
        "#FFFFFF",
        name_ru="Astra Proxima Светлая",
        window_fg="#262626",
        selection_bg="#118BCC",
        button_bg="#262626",
    ),
    "AstraProximaAdmin.colors": scheme(
        "Astra Proxima Service Admin",
        "#262626",
        name_ru="Astra Proxima Служебная (Admin)",
        window_fg="#F7F7F7",
        button_bg="#FFFFFF",
    ),
    "BreezeDark.colors": scheme(
        "Breeze Dark", "42,46,50", window_fg="252,252,252", button_bg="239,240,241"
    ),
    "FlyDefault.colors": scheme("Fly blue", "#dfdfdf", name_ru="Fly синяя", button_bg="#101010"),
    "ZionReversed1.colors": scheme(
        "Zion (Reversed)",
        "16,16,16",
        name_ru="Черная",
        window_fg="255,255,255",
        button_bg="250,250,250",
    ),
    # Рядом лежат *.css — не схемы; совпадение имени в них ничего не значит.
    "AstraProximaDark-default.css": "/* Name=Astra Proxima Dark Css */\n"
    "[Colors:Window]\nBackgroundNormal=#000000\n[General]\nName=Astra Proxima Dark Css\n",
}


@dataclass
class FlyHome:
    """Раскладка файлов Fly во временном каталоге."""

    home: Path
    system: Path  # подмена /usr/share/color-schemes
    user: Path  # подмена ~/.local/share/color-schemes

    @property
    def paletterc(self) -> Path:
        return self.home / ".fly" / "paletterc"

    @property
    def admin(self) -> Path:
        return self.home / ".config" / "fly-admin-themerc"

    def write_paletterc(
        self,
        *,
        color_scheme: Path | str | None = None,
        background: str | None = None,
        primary: str | None = PRIMARY,
    ) -> None:
        lines = ["[Variables]"]
        if background is not None:
            lines.append(f"BackgroundColor={background}")
        if color_scheme is not None:
            lines.append(f"ColorScheme={color_scheme}")
        if primary is not None:
            lines.append(f"PrimaryColor={primary}")
        atomic_write(self.paletterc, "\n".join(lines) + "\n")

    def write_admin(self, name: str) -> None:
        atomic_write(
            self.admin, f"[General]\nFoo=bar\n\n[UiSettings]\nColorScheme={name}\nIconTheme=x\n"
        )

    def source(self) -> FlyThemeSource:
        return FlyThemeSource(
            self.paletterc,
            admin_themerc=self.admin,
            schemes_dir=self.system,
            user_schemes_dir=self.user,
        )


def atomic_write(path: Path, text: str | bytes) -> None:
    """Как пишут настройки Fly/KDE: временный файл + rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(text, bytes):
        tmp.write_bytes(text)
    else:
        tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


@pytest.fixture
def fly(tmp_path: Path) -> FlyHome:
    layout = FlyHome(
        home=tmp_path / "home",
        system=tmp_path / "usr" / "share" / "color-schemes",
        user=tmp_path / "home" / ".local" / "share" / "color-schemes",
    )
    (layout.home / ".fly").mkdir(parents=True)
    (layout.home / ".config").mkdir(parents=True)
    layout.system.mkdir(parents=True)
    layout.user.mkdir(parents=True)
    for name, text in SCHEMES.items():
        (layout.system / name).write_text(text, encoding="utf-8")
    return layout


def theme_warnings(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and r.name.startswith("astra_voice")
    ]


# ── T-290 · источник 1: paletterc → путь к файлу схемы ─────────────────────


@pytest.mark.parametrize(
    ("file", "dark", "bg", "fg"),
    [
        ("AstraDark.colors", True, "#262626", "#F7F7F7"),
        ("AstraLight.colors", False, "#FFFFFF", "#262626"),
        ("AstraProximaAdmin.colors", True, "#262626", "#F7F7F7"),
        ("BreezeDark.colors", True, "#2A2E32", "#FCFCFC"),  # формат r,g,b
        ("ZionReversed1.colors", True, "#101010", "#FFFFFF"),
        ("FlyDefault.colors", False, "#DFDFDF", None),  # без ForegroundNormal
    ],
)
def test_t290_paletterc_scheme_path_decides_by_window_background(
    fly: FlyHome, file: str, dark: bool, bg: str, fg: str | None
) -> None:
    fly.write_paletterc(color_scheme=fly.system / file, background="#808080")
    source = fly.source()
    assert source.dark is dark
    assert source.window_bg == bg
    assert source.text == fg
    assert source.accent == "#118BCC"  # PrimaryColor из paletterc


def test_t290_ru_laptop_paletterc_astra_light(fly: FlyHome) -> None:
    """Дословно файл ноутбука ALSE 21.10 (путь подменён на временный каталог)."""
    atomic_write(
        fly.paletterc,
        "[Variables]\nBackgroundColor=#ffffff\n"
        f"ColorScheme={fly.system / 'AstraLight.colors'}\nPrimaryColor=#118bcc\n",
    )
    source = fly.source()
    assert (source.dark, source.accent, source.window_bg, source.text) == (
        False,
        "#118BCC",
        "#FFFFFF",
        "#262626",
    )


def test_t290_accent_falls_back_to_scheme_selection(fly: FlyHome) -> None:
    fly.write_paletterc(color_scheme=fly.system / "AstraDark.colors", primary=None)
    source = fly.source()
    assert source.dark is True
    assert source.accent == "#2FB3FA"  # [Colors:Selection] BackgroundNormal схемы


def test_t290_user_schemes_dir_is_allowed(fly: FlyHome) -> None:
    (fly.user / "Mine.colors").write_text(scheme("Mine", "#0E1729"), encoding="utf-8")
    fly.write_paletterc(color_scheme=fly.user / "Mine.colors", background="#ffffff")
    source = fly.source()
    assert source.dark is True
    assert source.window_bg == "#0E1729"


# ── T-291 · запасной фон BackgroundColor из paletterc ──────────────────────


@pytest.mark.parametrize(
    ("background", "dark"),
    [("#262626", True), ("#ffffff", False), ("#7e7e7e", True), ("#828282", False)],
)
def test_t291_missing_scheme_file_falls_back_to_background_color(
    fly: FlyHome, background: str, dark: bool
) -> None:
    """Около порога luma 128: #7e7e7e (126) → тёмная, #828282 (130) → светлая.

    Ровно 128 не берём: 0.299+0.587+0.114 в float даёт 127.999…, граница не детерминирована.
    """
    fly.write_paletterc(color_scheme=fly.system / "НетТакой.colors", background=background)
    source = fly.source()
    assert source.dark is dark
    assert source.window_bg == background.upper()
    assert source.accent == "#118BCC"


def test_t291_background_color_without_color_scheme_key(fly: FlyHome) -> None:
    fly.write_paletterc(background="#262626")
    source = fly.source()
    assert source.dark is True
    assert source.window_bg == "#262626"


# ── T-292 · источник 2: fly-admin-themerc → поиск схемы по имени ───────────


@pytest.mark.parametrize(
    ("name", "dark", "bg"),
    [
        ("Astra Proxima Темная", True, "#262626"),  # Name[ru], рабочий ноутбук 18.10
        ("Astra Proxima Светлая", False, "#FFFFFF"),  # Name[ru]
        ("Astra Proxima Служебная (Admin)", True, "#262626"),  # Name[ru] со скобками
        ("Astra Proxima Dark", True, "#262626"),  # Name
        ("Breeze Dark", True, "#2A2E32"),  # Name, r,g,b
        ("Черная", True, "#101010"),  # Name[ru], r,g,b
        ("Fly синяя", False, "#DFDFDF"),
        ("  astra proxima ТЕМНАЯ  ", True, "#262626"),  # регистр и пробелы по краям
    ],
)
def test_t292_admin_themerc_finds_scheme_by_name(
    fly: FlyHome, name: str, dark: bool, bg: str
) -> None:
    fly.write_admin(name)
    source = fly.source()
    assert source.dark is dark
    assert source.window_bg == bg


def test_t292_word_temnaya_in_name_is_not_a_sign(fly: FlyHome) -> None:
    """Факт 18.10: «Темная» в имени ничего не решает — решает фон окна схемы."""
    (fly.system / "Trap.colors").write_text(
        scheme("Trap", "#FAFAFA", name_ru="Ловушка Темная", button_bg="#000000"),
        encoding="utf-8",
    )
    fly.write_admin("Ловушка Темная")
    source = fly.source()
    assert source.dark is False
    assert source.window_bg == "#FAFAFA"


def test_t292_paletterc_wins_over_admin_themerc(fly: FlyHome) -> None:
    fly.write_paletterc(color_scheme=fly.system / "AstraLight.colors", background="#ffffff")
    fly.write_admin("Astra Proxima Темная")
    source = fly.source()
    assert source.dark is False
    assert source.window_bg == "#FFFFFF"


def test_t292_paletterc_without_keys_uses_admin_themerc(fly: FlyHome) -> None:
    atomic_write(fly.paletterc, "[Variables]\nPrimaryColor=#118bcc\n")
    fly.write_admin("Astra Proxima Темная")
    source = fly.source()
    assert source.dark is True
    assert source.window_bg == "#262626"


def test_t292_unresolved_paletterc_scheme_without_background_uses_admin(fly: FlyHome) -> None:
    """paletterc не дал ответа (файла схемы нет, BackgroundColor нет) → источник 2."""
    fly.write_paletterc(color_scheme=fly.system / "НетТакой.colors")
    fly.write_admin("Breeze Dark")
    source = fly.source()
    assert source.dark is True
    assert source.window_bg == "#2A2E32"


def test_t292_symlinked_scheme_inside_schemes_dir_is_followed(fly: FlyHome) -> None:
    real = fly.system / "real"
    real.mkdir()
    (real / "Linked.colors").write_text(
        scheme("Linked Dark", "#141414", name_ru="Связанная"), encoding="utf-8"
    )
    (fly.system / "Linked.colors").symlink_to(real / "Linked.colors")
    fly.write_admin("Связанная")
    source = fly.source()
    assert source.dark is True
    assert source.window_bg == "#141414"


# ── T-293 · ничего не нашли → светлая + одна строка WARNING без содержимого ─


def test_t293_nothing_at_all_is_light_with_one_warning(
    fly: FlyHome, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    source = fly.source()
    assert (source.dark, source.accent, source.window_bg, source.text) == (
        False,
        None,
        None,
        None,
    )
    assert len(theme_warnings(caplog)) == 1


@pytest.mark.parametrize(
    "name",
    [
        SECRET_NAME,  # такой схемы нет
        "Astra Proxima",  # не префикс: только точное совпадение
        "Astra Proxima Dark Css",  # имя есть только в .css — не схема
    ],
)
def test_t293_unknown_scheme_name_is_light_with_one_warning_without_content(
    fly: FlyHome, caplog: pytest.LogCaptureFixture, name: str
) -> None:
    caplog.set_level(logging.WARNING)
    fly.write_admin(name)
    source = fly.source()
    assert (source.dark, source.accent, source.window_bg, source.text) == (
        False,
        None,
        None,
        None,
    )
    warnings = theme_warnings(caplog)
    assert len(warnings) == 1
    message = warnings[0].getMessage()
    assert name.strip() not in message, "содержимое fly-admin-themerc попало в журнал"
    assert "UiSettings" not in message


def test_t293_admin_themerc_without_key_is_light_with_one_warning(
    fly: FlyHome, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    atomic_write(fly.admin, "[UiSettings]\nIconTheme=x\n")
    source = fly.source()
    assert source.dark is False
    assert source.window_bg is None
    assert len(theme_warnings(caplog)) == 1


# ── T-294 · большой или битый файл схемы = «не читается» ───────────────────

BIG_LIGHT = scheme("Big", "#FFFFFF", name_ru="Большая") + (
    "# padding\n" * (300 * 1024 // 10)
)  # > 256 КБ, валидный INI со светлым фоном
BINARY_LIGHT = (
    b"\x00\x01\xff\xfe\x00" * 200
    + b"\n[Colors:Window]\nBackgroundNormal=#FFFFFF\n[General]\nName=Binary\n"
)
CSS_TEXT = ".ColorScheme-Text { color: #FFFFFF; }\nbody { background: #FFFFFF; }\n"


@pytest.mark.parametrize(
    "content", [BIG_LIGHT, BINARY_LIGHT, CSS_TEXT], ids=["over-256k", "binary", "not-ini"]
)
def test_t294_unreadable_scheme_from_paletterc_falls_back_to_background(
    fly: FlyHome, content: str | bytes
) -> None:
    """Схема светлая, запасной фон тёмный: тёмный итог доказывает, что файл отвергнут."""
    path = fly.system / "Bad.colors"
    atomic_write(path, content)
    assert len(BIG_LIGHT.encode()) > 256 * 1024
    fly.write_paletterc(color_scheme=path, background="#262626")
    source = fly.source()
    assert source.dark is True
    assert source.window_bg == "#262626"


def test_t294_oversized_scheme_found_by_name_is_not_used(
    fly: FlyHome, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    big_dark = scheme("Big Dark", "#000000", name_ru="Большая тёмная") + "# p\n" * (300 * 1024 // 4)
    atomic_write(fly.system / "BigDark.colors", big_dark)
    fly.write_admin("Большая тёмная")
    source = fly.source()
    assert source.dark is False
    assert source.window_bg is None
    assert len(theme_warnings(caplog)) == 1


# ── T-295 · пути вне допустимых каталогов ──────────────────────────────────


@pytest.mark.parametrize("root_kind", ["system", "user"])
@pytest.mark.parametrize("redirected", [True, False], ids=["symlink-root", "real-root"])
def test_t295_scheme_root_cannot_be_redirected_outside_its_parent(
    fly: FlyHome,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    root_kind: str,
    redirected: bool,
) -> None:
    caplog.set_level(logging.WARNING)
    root = tmp_path / "configured-parent" / "color-schemes"
    root.parent.mkdir()
    target = tmp_path / "outside-parent" / "private-schemes" if redirected else root
    target.mkdir(parents=True)
    if redirected:
        root.symlink_to(target, target_is_directory=True)
    filename = "PrivateScheme.colors"
    (target / filename).write_text(scheme(SECRET_NAME, "#FFFFFF"), encoding="utf-8")
    fly.write_paletterc(color_scheme=root / filename, background="#262626")
    source = FlyThemeSource(
        fly.paletterc,
        admin_themerc=fly.admin,
        schemes_dir=root if root_kind == "system" else fly.system,
        user_schemes_dir=root if root_kind == "user" else fly.user,
    )
    assert (source.dark, source.window_bg) == (
        (True, "#262626") if redirected else (False, "#FFFFFF")
    )
    assert source.reload() is False
    warnings = theme_warnings(caplog)
    assert len(warnings) == (1 if redirected else 0)
    for warning in warnings:
        message = warning.getMessage()
        for private in (str(tmp_path), str(root), str(target), filename, SECRET_NAME):
            assert private not in message, "WARNING раскрыл путь или имя схемы"


def test_t295_paletterc_path_outside_allowed_dirs_is_rejected(
    fly: FlyHome, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "Evil.colors").write_text(scheme("Evil", "#FFFFFF"), encoding="utf-8")
    fly.write_paletterc(color_scheme=elsewhere / "Evil.colors", background="#262626")
    source = fly.source()
    assert source.dark is True, "схема вне каталогов схем прочитана"
    assert source.window_bg == "#262626"
    warnings = theme_warnings(caplog)
    assert warnings, "отказ по пути должен оставить WARNING"
    assert all("Evil" not in w.getMessage() for w in warnings)


def test_t295_dotdot_escape_from_schemes_dir_is_rejected(fly: FlyHome, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "Evil.colors").write_text(scheme("Evil", "#FFFFFF"), encoding="utf-8")
    sneaky = f"{fly.system}/../../../elsewhere/Evil.colors"
    assert Path(sneaky).resolve() == (elsewhere / "Evil.colors").resolve()
    fly.write_paletterc(color_scheme=sneaky, background="#262626")
    source = fly.source()
    assert source.dark is True
    assert source.window_bg == "#262626"


def test_t295_symlink_in_allowed_dir_pointing_outside_is_rejected(
    fly: FlyHome, tmp_path: Path
) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "Evil.colors").write_text(scheme("Evil", "#FFFFFF"), encoding="utf-8")
    (fly.user / "Evil.colors").symlink_to(elsewhere / "Evil.colors")
    fly.write_paletterc(color_scheme=fly.user / "Evil.colors", background="#262626")
    source = fly.source()
    assert source.dark is True
    assert source.window_bg == "#262626"


def test_t295_name_search_does_not_follow_symlink_outside_schemes_dir(
    fly: FlyHome, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "Evil.colors").write_text(
        scheme("Evil", "#000000", name_ru="Чужая"), encoding="utf-8"
    )
    (fly.system / "Evil.colors").symlink_to(elsewhere / "Evil.colors")
    fly.write_admin("Чужая")
    source = fly.source()
    assert source.dark is False
    assert source.window_bg is None
    assert len(theme_warnings(caplog)) == 1


def test_t295_name_search_ignores_user_schemes_dir(fly: FlyHome) -> None:
    """Поиск по имени — только в системном каталоге схем."""
    (fly.user / "Mine.colors").write_text(
        scheme("Mine", "#000000", name_ru="Моя"), encoding="utf-8"
    )
    fly.write_admin("Моя")
    source = fly.source()
    assert source.dark is False
    assert source.window_bg is None


# ── T-296 · пути по умолчанию ──────────────────────────────────────────────


def test_t296_admin_themerc_default_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/tmp/не-важно")
    assert theme_mod.fly_admin_themerc_path() == Path.home() / ".config" / "fly-admin-themerc"


def test_t296_defaults_read_home_files_and_user_schemes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    user = tmp_path / ".local" / "share" / "color-schemes"
    user.mkdir(parents=True)
    (user / "Mine.colors").write_text(scheme("Mine", "#0E1729"), encoding="utf-8")
    atomic_write(
        tmp_path / ".fly" / "paletterc",
        f"[Variables]\nBackgroundColor=#ffffff\nColorScheme={user / 'Mine.colors'}\n",
    )
    source = FlyThemeSource()
    assert source.path == tmp_path / ".fly" / "paletterc"
    assert source.dark is True


def test_t296_default_admin_themerc_under_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path))
    system = tmp_path / "schemes"
    system.mkdir()
    (system / "AstraDark.colors").write_text(SCHEMES["AstraDark.colors"], encoding="utf-8")
    atomic_write(
        tmp_path / ".config" / "fly-admin-themerc", "[UiSettings]\nColorScheme=Astra Proxima Dark\n"
    )
    source = FlyThemeSource(schemes_dir=system)
    assert source.dark is True


# ── T-297 · наблюдатель: смена paletterc / fly-admin-themerc / файла схемы ─


class Waiter:
    """Ждёт уведомление источника не дольше бюджета истории (≤ 2 с)."""

    def __init__(self, source: FlyThemeSource) -> None:
        from PyQt5.QtCore import QEventLoop

        self.source = source
        self.seen: list[bool] = []
        self._loop: QEventLoop | None = None
        source.subscribe(self._on_change)

    def _on_change(self) -> None:
        self.seen.append(self.source.dark)
        if self._loop is not None:
            self._loop.quit()

    def run(self, action: Callable[[], None], timeout_ms: int = 2000) -> list[bool]:
        from PyQt5.QtCore import QEventLoop, QTimer

        before = len(self.seen)
        loop = QEventLoop()
        self._loop = loop
        QTimer.singleShot(timeout_ms, loop.quit)
        action()
        if len(self.seen) == before:
            loop.exec_()
        self._loop = None
        return self.seen[before:]


@pytest.fixture
def watched(fly: FlyHome) -> FlyHome:
    from helpers.qt_app import get_qapplication

    get_qapplication()
    return fly


def run_watched(fly: FlyHome, steps: list[tuple[Callable[[], None], bool]]) -> None:
    source = fly.source()
    source.start()
    try:
        waiter = Waiter(source)
        for index, (action, expected) in enumerate(steps):
            seen = waiter.run(action)
            assert seen, f"шаг {index}: смена файла не долетела до подписчика за 2 с"
            assert seen[-1] is expected, f"шаг {index}: тема {seen[-1]}, ожидалась {expected}"
            assert source.dark is expected
    finally:
        source.stop()


def test_t297_paletterc_switch_rereads_and_follows_new_scheme_file(watched: FlyHome) -> None:
    fly = watched
    fly.write_paletterc(color_scheme=fly.system / "AstraLight.colors", background="#ffffff")
    fresh = fly.user / "Fresh.colors"
    fresh.write_text(scheme("Fresh", "#141414"), encoding="utf-8")
    run_watched(
        fly,
        [
            # Fly «Применить»: paletterc переписан на другую схему.
            (lambda: fly.write_paletterc(color_scheme=fresh, background="#262626"), True),
            # Теперь под наблюдением НОВЫЙ файл схемы: его правка тоже долетает.
            (lambda: atomic_write(fresh, scheme("Fresh", "#F0F0F0")), False),
        ],
    )


def test_t297_scheme_file_edit_in_place_is_noticed(watched: FlyHome) -> None:
    fly = watched
    mine = fly.user / "Mine.colors"
    mine.write_text(scheme("Mine", "#FFFFFF"), encoding="utf-8")
    fly.write_paletterc(color_scheme=mine, background="#ffffff")

    def edit_in_place() -> None:
        mine.write_text(scheme("Mine", "#101010"), encoding="utf-8")

    run_watched(fly, [(edit_in_place, True)])


def test_t297_scheme_file_appearing_later_is_picked_up(watched: FlyHome) -> None:
    fly = watched
    later = fly.user / "Later.colors"
    fly.write_paletterc(color_scheme=later, background="#ffffff")
    run_watched(fly, [(lambda: atomic_write(later, scheme("Later", "#101010")), True)])


def test_t297_admin_themerc_change_rereads(watched: FlyHome) -> None:
    fly = watched
    fly.write_admin("Astra Proxima Светлая")
    run_watched(
        fly,
        [
            (lambda: fly.write_admin("Astra Proxima Темная"), True),
            (lambda: fly.write_admin("Fly синяя"), False),
        ],
    )


def test_t297_admin_themerc_created_after_start(watched: FlyHome) -> None:
    fly = watched
    run_watched(fly, [(lambda: fly.write_admin("Черная"), True)])


def test_t297_paletterc_created_after_start(watched: FlyHome) -> None:
    fly = watched
    run_watched(
        fly,
        [(lambda: fly.write_paletterc(color_scheme=fly.system / "AstraDark.colors"), True)],
    )
