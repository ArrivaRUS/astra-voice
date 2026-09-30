"""Штатные средства системы для звука: панель настроек, громкость и служба.

Программа не трогает системные настройки сама: каждая команда здесь выполняется
только по нажатию человека (PRD 0.9 F6.7 (в)). Звуковую службу программа тоже
никогда не поднимает (решение 2026-09-21, `core/audio_env.py`) — перезапуск
доступен отдельным действием в «Отладке».

Меняется только выбранный в программе источник записи: громкость
воспроизведения, источники-«мониторы», профиль и порты карты не затрагиваются.
По кнопке громкость поднимается до целевой громкости (50 %); если громкость
уже выше, её не трогаем, только включаем звук. Громкость можно задать вручную.
Прежнее состояние запоминается на сеанс один раз, до первого изменения,
и возвращается по кнопке «Вернуть как было».

Ни имена устройств ALSA, ни пути, ни имена служб наружу не отдаём: сообщения
для человека собираются выше, из значений `MicrophoneState`.
"""

from __future__ import annotations

import logging
import re
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from astra_voice.core import childenv
from astra_voice.platform.session import SessionKind

log = logging.getLogger(__name__)

#: Ниже этой системной громкости считаем микрофон слишком тихим (PRD A20).
LOW_VOLUME_PERCENT = 30
#: Полная громкость: потолок для признака MicrophoneState.can_raise.
FULL_VOLUME_PERCENT = 100
#: Цель кнопки «Поднять»: выше на ноутбуках с усилением микрофона — перегруз;
#: перегруз хуже тихой записи (заказчик, 30.09).
RAISE_TARGET_PERCENT = 50
#: Имя источника по умолчанию в командах звуковой службы.
DEFAULT_SOURCE = "@DEFAULT_SOURCE@"
#: Команда должна отвечать быстро: человек ждёт результата нажатия.
COMMAND_TIMEOUT_S = 3.0

Runner = Callable[..., "subprocess.CompletedProcess[str]"]
Spawner = Callable[[Sequence[str]], bool]
Which = Callable[[str], str | None]

#: Чем открываем системную панель звука; берём первую найденную команду.
#: Под Fly отдельной панели звука нет — там работает тот же отдельный модуль KDE.
SOUND_PANEL_COMMANDS: dict[SessionKind, tuple[tuple[str, ...], ...]] = {
    SessionKind.KDE: (
        ("systemsettings5", "kcm_pulseaudio"),
        ("systemsettings", "kcm_pulseaudio"),
        ("kcmshell5", "kcm_pulseaudio"),
        ("pavucontrol",),
    ),
    SessionKind.FLY: (
        ("kcmshell5", "kcm_pulseaudio"),
        ("pavucontrol",),
        ("systemsettings5", "kcm_pulseaudio"),
    ),
    SessionKind.OTHER: (
        ("kcmshell5", "kcm_pulseaudio"),
        ("pavucontrol",),
    ),
}

#: Перезапуск звуковой службы — только по нажатию человека в «Отладке».
SOUND_SERVICE_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("systemctl", "--user", "restart", "pipewire-pulse.socket", "pipewire-pulse.service"),
    ("systemctl", "--user", "restart", "wireplumber"),
)

_VOLUME_PERCENT = re.compile(r"(\d{1,4})\s*%")


class MicrophoneProblem(Enum):
    """Причина «вас не слышно» по состоянию источника, без открытия микрофона."""

    MUTED = "muted"
    TOO_QUIET = "too-quiet"


@dataclass(frozen=True)
class MicrophoneState:
    """Состояние выбранного источника записи; known=False — узнать не удалось."""

    known: bool = False
    muted: bool = False
    percent: int = -1

    @property
    def problem(self) -> MicrophoneProblem | None:
        """Приоритет причин: сначала «выключен», затем «слишком тихий»."""
        if not self.known:
            return None
        if self.muted or self.percent == 0:
            return MicrophoneProblem.MUTED
        if 0 <= self.percent < LOW_VOLUME_PERCENT:
            return MicrophoneProblem.TOO_QUIET
        return None

    @property
    def can_raise(self) -> bool:
        """Поднимать есть куда: звук выключен или громкость ниже полной."""
        return self.known and (self.muted or 0 <= self.percent < FULL_VOLUME_PERCENT)


def _spawn(command: Sequence[str], *, popen: Callable[..., object] = subprocess.Popen) -> bool:
    """Запускает окно настроек и сразу отпускает его: GUI не ждёт выхода."""
    try:
        popen(
            list(command),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            shell=False,
            env=childenv.clean_env(),
        )
    except OSError:
        log.warning("Не удалось открыть системные настройки звука")
        return False
    return True


class SoundControl:
    """Читает состояние выбранного микрофона и выполняет действия человека."""

    def __init__(
        self,
        *,
        session: SessionKind = SessionKind.OTHER,
        which: Which = shutil.which,
        run: Runner = subprocess.run,
        spawn: Spawner = _spawn,
    ) -> None:
        self._session = session
        self._which = which
        self._run = run
        self._spawn = spawn
        self._before_change: dict[str, MicrophoneState] = {}

    @property
    def has_volume_control(self) -> bool:
        """Есть ли чем менять громкость; без этого строку настройки не показываем."""
        return self._which("pactl") is not None

    @property
    def has_sound_settings(self) -> bool:
        """Есть ли чем открыть системную панель звука."""
        return self._panel_command() is not None

    @property
    def has_sound_service(self) -> bool:
        """Есть ли чем перезапустить звуковую службу."""
        return self._which("systemctl") is not None

    def _panel_command(self) -> tuple[str, ...] | None:
        candidates = SOUND_PANEL_COMMANDS.get(
            self._session, SOUND_PANEL_COMMANDS[SessionKind.OTHER]
        )
        for command in candidates:
            if self._which(command[0]) is not None:
                return command
        return None

    def open_sound_settings(self) -> bool:
        """Открывает системную панель звука; ничего в системе не меняет."""
        command = self._panel_command()
        if command is None:
            log.info("Системная панель звука не найдена")
            return False
        opened = self._spawn(command)
        log.info("Системные настройки звука: %s", "открыты" if opened else "открыть не удалось")
        return opened

    def _pactl(self, *arguments: str) -> str | None:
        """Возвращает вывод pactl либо None; текст ошибки в журнал не попадает."""
        if not self.has_volume_control:
            return None
        try:
            result = self._run(
                ["pactl", *arguments],
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_S,
                env={**childenv.clean_env(keep_pulse_config=True), "LC_ALL": "C"},
                shell=False,
                check=False,
            )
        except (OSError, subprocess.SubprocessError, UnicodeError):
            log.debug("Звуковая служба не ответила на запрос громкости")
            return None
        if result.returncode != 0:
            log.debug("Звуковая служба отказала в запросе громкости")
            return None
        return str(result.stdout)

    @staticmethod
    def _source(device: str | None) -> str:
        return device if device else DEFAULT_SOURCE

    def microphone_state(self, device: str | None = None) -> MicrophoneState:
        """Читает признак «звук выключен» и громкость БЕЗ открытия микрофона."""
        source = self._source(device)
        muted = self._pactl("get-source-mute", source)
        volume = self._pactl("get-source-volume", source)
        if muted is None or volume is None:
            return MicrophoneState()
        match = _VOLUME_PERCENT.search(volume)
        if match is None:
            log.debug("Не удалось разобрать ответ звуковой службы о громкости")
            return MicrophoneState()
        try:
            percent = int(match.group(1))
        except ValueError:
            return MicrophoneState()
        return MicrophoneState(known=True, muted="yes" in muted.lower(), percent=percent)

    def _remember(self, source: str, state: MicrophoneState) -> None:
        """Сохраняет известное состояние до первого изменения источника за сеанс."""
        if state.known and source not in self._before_change:
            self._before_change[source] = state

    def raise_microphone(self, device: str | None = None) -> bool:
        """Включает звук выбранного источника и поднимает до целевой громкости (50 %)."""
        source = self._source(device)
        state = self.microphone_state(device)
        unmuted = self._pactl("set-source-mute", source, "0") is not None
        raised = True
        if state.percent < RAISE_TARGET_PERCENT:
            raised = (
                self._pactl("set-source-volume", source, f"{RAISE_TARGET_PERCENT}%") is not None
            )
        if (state.muted or state.percent < RAISE_TARGET_PERCENT) and (
            unmuted or (state.percent < RAISE_TARGET_PERCENT and raised)
        ):
            self._remember(source, state)
        log.info(
            "Громкость микрофона по кнопке: звук %s, громкость %s",
            "включён" if unmuted else "включить не удалось",
            "поднята" if raised else "не изменена",
        )
        return unmuted and raised

    def set_microphone_volume(self, percent: int, device: str | None = None) -> bool:
        """Задаёт громкость источника вручную, сохраняя исходное состояние для отката."""
        percent = max(0, min(int(percent), FULL_VOLUME_PERCENT))
        source = self._source(device)
        state = self.microphone_state(device)
        changed = self._pactl("set-source-volume", source, f"{percent}%") is not None
        if changed and state.known and state.percent != percent:
            self._remember(source, state)
        if changed:
            log.info("Громкость микрофона задана вручную: %d %%", percent)
        else:
            log.info("Не удалось задать громкость микрофона вручную: %d %%", percent)
        return changed

    def can_restore_microphone(self, device: str | None = None) -> bool:
        """Есть ли состояние источника до первого изменения громкости."""
        return self._source(device) in self._before_change

    def restore_microphone(self, device: str | None = None) -> bool:
        """Возвращает прежние громкость и выключение звука выбранного источника."""
        source = self._source(device)
        state = self._before_change.get(source)
        if state is None:
            return False
        volume_restored = self._pactl("set-source-volume", source, f"{state.percent}%") is not None
        mute_restored = (
            self._pactl("set-source-mute", source, "1" if state.muted else "0") is not None
        )
        restored = volume_restored and mute_restored
        if restored:
            del self._before_change[source]
        log.info("Громкость микрофона по кнопке возвращена: %s", "да" if restored else "нет")
        return restored

    def forget_microphone_changes(self) -> None:
        """Забывает состояния микрофонов, сохранённые за этот сеанс."""
        self._before_change.clear()

    def restart_sound_service(self) -> bool:
        """Перезапускает звуковую службу пользователя; службу сама не поднимает."""
        if not self.has_sound_service:
            log.info("Перезапуск звуковой службы недоступен")
            return False
        for command in SOUND_SERVICE_COMMANDS:
            try:
                result = self._run(
                    list(command),
                    capture_output=True,
                    text=True,
                    timeout=COMMAND_TIMEOUT_S,
                    env=childenv.clean_env(),
                    shell=False,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError, UnicodeError):
                continue
            if result.returncode == 0:
                log.info("Звуковая служба перезапущена")
                return True
        log.warning("Перезапустить звуковую службу не удалось")
        return False
