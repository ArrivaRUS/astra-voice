"""US-2.6: выгрузка модели по простою и загрузка заново по нажатию.

Настоящие оркестратор, автомат хоткея и мост настроек; воркер, таймеры и время — фейки
из test_runtime. Выгрузка — новый процесс воркера без model.load (RSS как у свежего
воркера); следующее нажатие показывает «Загружаю модель…», а запись начинается, когда
модель готова, если клавиша ещё не отпущена.
"""

from __future__ import annotations

from typing import Any, cast
from unittest.mock import Mock, call

import pytest
from test_command_mouse_runtime import MouseRuntimeRig
from test_runtime import MARKER, FakeTimer, Rig, assert_phase

from astra_voice.core.dictation import PROCESSING_WATCHDOG_MS, DictationPhase
from astra_voice.core.settings import MODEL_UNLOAD_MINUTES, Settings, from_dict
from astra_voice.platform.hotkey import (
    GrabResult,
    HotkeyBackend,
    HotkeyEvent,
    HotkeyManager,
    HotkeyState,
)
from astra_voice.platform.mouse_button import MouseHoldState
from astra_voice.platform.paste import snapshot_target
from astra_voice.ui.bridges import SettingsApply, SettingsBridge
from astra_voice.ui.pill import (
    ERROR_MODEL_LOAD_FAILED,
    ERROR_MODEL_NOT_LOADED,
    ERROR_MODEL_REVOKED,
    ERROR_RECOGNITION_FAILED,
    PillState,
)

pytestmark = pytest.mark.unit
FIVE_MIN_S = 5 * 60.0


class PhysicalHotkeyRig(Rig):
    """События настоящего менеджера; прямой fsm.press не доказывает удержание."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
        backend = Mock(spec=HotkeyBackend)
        backend.grab_combo.return_value = GrabResult("ok", keycode=65, mods=4)
        backend.grab_escape.return_value = GrabResult("ok", keycode=9)
        backend.ungrab_combo.return_value = GrabResult("ok")
        backend.poll_events.return_value = []
        backend.fileno.return_value = 17
        hotkey = HotkeyManager(backend, clock=lambda: self.now)
        super().__init__(monkeypatch, settings, hotkey_factory=lambda: hotkey)
        self.hotkey = cast(Any, hotkey)

    def press(self, now: float) -> None:
        self.hotkey.handle_event(HotkeyEvent("KeyPress", 65, int(now * 1000), mods=4), now)

    def release(self, now: float) -> None:
        self.hotkey.handle_event(HotkeyEvent("KeyRelease", 65, int(now * 1000), mods=4), now)
        self.fire_tail()

    def recognize(self, text: str = MARKER) -> None:
        self.press(self.now)
        self.release(self.now + 1)
        self.event(type="result", text=text)


def make_rig(monkeypatch: pytest.MonkeyPatch, minutes: int = 5) -> PhysicalHotkeyRig:
    rig = PhysicalHotkeyRig(
        monkeypatch, from_dict({"model_dir": "/tmp/model", "model_unload_minutes": minutes})
    )
    rig.runtime.start()
    load_model(rig)
    return rig


def load_model(rig: Rig) -> None:
    rig.event(type="hello")
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert rig.runtime._selfcheck == "ok" and not rig.runtime._loading_model


def sent(rig: Rig) -> list[str]:
    return [str(item.args[0]["type"]) for item in rig.supervisor.send.call_args_list]


def idle_timer(rig: Rig) -> FakeTimer | None:
    return cast(FakeTimer | None, rig.runtime._idle_unload_timer)


def fire_idle(rig: Rig) -> None:
    timer = idle_timer(rig)
    assert timer is not None and timer.active
    timer.fire()


def settle(rig: Rig) -> None:
    """Доводит короткие таймеры фаз (пилюля «Готово», сторож отмены) до IDLE."""
    for timer in list(rig.timers):
        if timer.name == "scheduled" and timer is not idle_timer(rig) and timer.interval < 60_000:
            timer.fire()


def park(rig: Rig) -> None:
    rig.now += FIVE_MIN_S
    fire_idle(rig)
    assert rig.runtime._model_parked
    rig.event(type="hello")


def test_always_keeps_model_and_never_arms_unload(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_rig(monkeypatch, minutes=0)
    assert Settings().model_unload_minutes == 0
    assert idle_timer(rig) is None
    rig.now += 24 * 3600
    rig.recognize()
    for timer in rig.timers:
        timer.fire()
    assert idle_timer(rig) is None
    assert not rig.runtime._model_parked
    rig.supervisor_factory.assert_called_once()
    rig.supervisor.stop.assert_not_called()


def test_work_pending_includes_supervisor_waiting_for_worker_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_rig(monkeypatch)
    try:
        assert not rig.runtime._work_pending()
        rig.supervisor.restarting = True
        assert rig.runtime._work_pending()
        assert not rig.runtime.reserve_appimage_removal()
        assert not rig.runtime._appimage_removal_reserved
        factories = rig.supervisor_factory.call_count
        rig.now += FIVE_MIN_S
        fire_idle(rig)
        assert not rig.runtime._model_parked
        assert rig.supervisor_factory.call_count == factories
        rig.supervisor.stop.assert_not_called()
        rig.supervisor.restarting = False
        assert not rig.runtime._work_pending()
    finally:
        rig.runtime.shutdown()


def test_wake_while_worker_restarts_preserves_parked_model_and_reports_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_rig(monkeypatch)
    park(rig)
    runtime = rig.runtime
    rig.supervisor.restarting = True
    rig.supervisor.send.reset_mock()
    rig.supervisor.stop.reset_mock()
    pending_test = Mock()
    runtime._pending_test = ("fake-mic", pending_test)
    try:
        assert not runtime._wake_model()
        assert runtime._model_parked and not runtime._loading_model
        assert runtime._pending_test is None
        pending_test.assert_called_once()
        update = pending_test.call_args.args[0]
        assert update.state == "error" and "перезапускается" in update.message
        rig.pill.show_state.assert_called_with(PillState.ERROR, text=update.message)
        rig.supervisor.send.assert_not_called()
        rig.supervisor.stop.assert_not_called()
    finally:
        rig.supervisor.restarting = False
        runtime.shutdown()


def test_unload_after_idle_restarts_worker_without_model(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_rig(monkeypatch)
    timer = idle_timer(rig)
    assert timer is not None and timer.interval == FIVE_MIN_S * 1000
    generation = rig.supervisor.generation
    loads = sent(rig).count("model.load")
    rig.pill.show_state.reset_mock()
    rig.now += FIVE_MIN_S - 1
    fire_idle(rig)  # раньше срока проверка лишь переносится на остаток
    assert not rig.runtime._model_parked
    assert idle_timer(rig) is not None and idle_timer(rig).interval == 1000  # type: ignore[union-attr]
    rig.now += 1
    fire_idle(rig)
    assert rig.runtime._model_parked
    rig.supervisor.stop.assert_called_once()
    assert rig.supervisor.generation == generation + 1
    assert rig.supervisor_factory.call_count == 2
    rig.event(type="hello")
    # Новый процесс воркера модель не грузит: память — как у свежего воркера.
    assert sent(rig).count("model.load") == loads
    assert idle_timer(rig) is None
    rig.pill.show_state.assert_not_called()
    # Падение выгруженного воркера и его автоперезапуск тоже не грузят модель.
    rig.supervisor.generation += 1
    rig.event(type="hello")
    assert sent(rig).count("model.load") == loads


@pytest.mark.parametrize("activity", ["dictation", "worker-event", "hotkey"])
def test_any_activity_restarts_idle_countdown(
    monkeypatch: pytest.MonkeyPatch, activity: str
) -> None:
    rig = make_rig(monkeypatch)
    rig.now += 200
    if activity == "dictation":
        rig.recognize()
        rig.now += 1  # recognize отпускает клавишу на секунду позже
    elif activity == "worker-event":
        rig.event(type="measured", vm_hwm_kb=1000)
    else:
        rig.press(rig.now)
        rig.hotkey.fsm.escape(rig.now)
        rig.event(type="cancelled", utterance_id=rig.runtime.orchestrator._utterance_id)
    settle(rig)
    assert_phase(rig.runtime, DictationPhase.IDLE)
    last = rig.now
    rig.now = 10.0 + FIVE_MIN_S + 50  # исходный срок истёк, с активности — нет
    fire_idle(rig)
    assert not rig.runtime._model_parked
    rig.now = last + FIVE_MIN_S
    fire_idle(rig)
    assert rig.runtime._model_parked


def test_work_in_progress_postpones_unload(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_rig(monkeypatch)
    rig.press(rig.now)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    rig.now += FIVE_MIN_S + 10  # долгая запись без событий воркера
    fire_idle(rig)
    assert not rig.runtime._model_parked
    rig.supervisor.stop.assert_not_called()
    rig.release(rig.now)
    rig.event(type="result", text=MARKER)
    rig.now = 10.0 + 2 * FIVE_MIN_S + 20  # срок, взведённый в записи: идёт пилюля «Готово»
    fire_idle(rig)
    assert not rig.runtime._model_parked
    settle(rig)
    finished = rig.now
    rig.now = finished + FIVE_MIN_S - 0.5
    fire_idle(rig)
    assert not rig.runtime._model_parked
    rig.now = finished + FIVE_MIN_S
    fire_idle(rig)
    assert rig.runtime._model_parked


def test_hold_during_reload_starts_recording_when_model_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_rig(monkeypatch)
    park(rig)
    rig.pill.show_state.reset_mock()
    rig.pill.hide.reset_mock()
    loads = sent(rig).count("model.load")
    rig.press(rig.now)
    assert sent(rig).count("model.load") == loads + 1
    assert rig.supervisor.send.call_args.kwargs == {"timeout": 10.0}
    rig.pill.show_state.assert_called_once_with(PillState.LOADING_MODEL)
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert "record.start" not in sent(rig)[-1:]
    rig.now += 1.5
    rig.event(type="model.loaded")
    assert sent(rig)[-1] == "transcribe.file"  # самопроверка нового процесса
    rig.event(type="result", utterance_id="file", text="проверка")
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    assert sent(rig)[-1] == "record.start"
    rig.pill.hide.assert_not_called()
    rig.release(rig.now + 2)
    rig.event(type="result", text=MARKER)
    assert rig.paste.call_args.args[0] == MARKER
    assert not rig.runtime._model_parked
    assert idle_timer(rig) is not None


@pytest.mark.parametrize("gesture", ["tap", "release", "escape"])
def test_released_before_ready_does_not_record(
    monkeypatch: pytest.MonkeyPatch, gesture: str
) -> None:
    rig = make_rig(monkeypatch)
    park(rig)
    rig.press(rig.now)
    if gesture in ("tap", "release"):
        rig.release(rig.now + (0.1 if gesture == "tap" else 0.5))
    else:
        rig.hotkey.fsm.escape(rig.now + 0.5)
        rig.release(rig.now + 0.5)
    assert not rig.runtime._wake_dictation
    assert rig.runtime._loading_model
    assert rig.hotkey.fsm.state is HotkeyState.IDLE
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert "record.start" not in sent(rig)
    assert rig.hotkey.fsm.state is HotkeyState.IDLE
    rig.pill.hide.assert_called()
    # Следующее нажатие — обычная диктовка без загрузки.
    loads = sent(rig).count("model.load")
    rig.recognize()
    assert rig.paste.call_args.args[0] == MARKER
    assert sent(rig).count("model.load") == loads


@pytest.mark.parametrize("held", [False, True])
def test_toggle_wake_only_loads_until_next_press(
    monkeypatch: pytest.MonkeyPatch, held: bool
) -> None:
    rig = PhysicalHotkeyRig(
        monkeypatch,
        from_dict({"model_dir": "/tmp/model", "model_unload_minutes": 15, "hotkey_mode": "toggle"}),
    )
    rig.runtime.start()
    load_model(rig)
    assert idle_timer(rig).interval == 15 * 60 * 1000  # type: ignore[union-attr]
    rig.now += 15 * 60
    fire_idle(rig)
    rig.event(type="hello")
    rig.press(rig.now)
    if not held:
        rig.release(rig.now + 0.1)
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert "record.start" not in sent(rig)
    assert rig.hotkey.fsm.state is HotkeyState.IDLE
    assert not rig.runtime._wake_dictation
    rig.pill.hide.assert_called()
    if held:
        rig.release(rig.now + 1)
    rig.press(rig.now + 2)
    assert_phase(rig.runtime, DictationPhase.RECORDING)


def test_failed_reload_shows_error_and_does_not_record_later(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rig = make_rig(monkeypatch)
    park(rig)
    rig.press(rig.now)
    rig.event(type="error", code="engine-failed", request_type="model.load")
    assert not rig.runtime._loading_model
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert not rig.runtime._wake_dictation


def test_setting_changes_apply_on_the_fly(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_rig(monkeypatch, minutes=0)
    assert idle_timer(rig) is None
    rig.now += 1000
    rig.runtime.apply_model_unload_minutes(5)
    timer = idle_timer(rig)
    assert timer is not None and timer.interval == FIVE_MIN_S * 1000  # отсчёт с момента выбора
    rig.runtime.apply_model_unload_minutes(60)
    assert timer.deleted and idle_timer(rig).interval == 60 * 60 * 1000  # type: ignore[union-attr]
    rig.runtime.apply_model_unload_minutes(0)
    assert idle_timer(rig) is None
    rig.runtime.apply_model_unload_minutes(7)  # вне списка — без изменений
    assert rig.runtime.settings.model_unload_minutes == 0 and idle_timer(rig) is None
    # Выгруженная модель при выборе «Всегда» сразу возвращается в память.
    rig.runtime.apply_model_unload_minutes(5)
    park(rig)
    loads = sent(rig).count("model.load")
    rig.runtime.apply_model_unload_minutes(0)
    assert not rig.runtime._model_parked
    assert sent(rig).count("model.load") == loads + 1
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert idle_timer(rig) is None


def test_microphone_test_while_unloaded_loads_model(monkeypatch: pytest.MonkeyPatch) -> None:
    rig = make_rig(monkeypatch)
    park(rig)
    workers = rig.supervisor_factory.call_count
    loads = sent(rig).count("model.load")
    callback = Mock()
    assert rig.runtime.start_test("", callback)
    assert not rig.runtime._model_parked
    # Модель грузится в уже запущенный выгруженный воркер — второго перезапуска нет.
    assert rig.supervisor_factory.call_count == workers
    rig.supervisor.stop.assert_called_once()  # только выгрузка
    assert sent(rig).count("model.load") == loads + 1
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert sent(rig)[-1] == "record.start"  # проверка микрофона началась


def test_microphone_test_while_unloaded_reports_failed_wake(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from astra_voice.core.dictation import TEST_MODEL_UNAVAILABLE, MicrophoneTestUpdate

    rig = make_rig(monkeypatch)
    park(rig)
    monkeypatch.setattr(rig.runtime, "_resolve_model_request", lambda: None)
    callback = Mock()
    rig.runtime.start_test("", callback)
    assert callback.call_args.args[0] == MicrophoneTestUpdate(
        "error", message=TEST_MODEL_UNAVAILABLE
    )
    assert rig.runtime._model_parked and rig.runtime._pending_test is None


def test_mouse_command_stays_armed_and_press_reloads_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mouse = MouseRuntimeRig(monkeypatch)
    runtime = mouse.runtime
    request: dict[str, Any] = {
        "type": "model.load",
        "id": "model",
        "revision": "revision",
        "dir": "/tmp/trusted",
        "layout": "onnx-asr-gigaam-v3",
        "variant": "gigaam-v3-e2e-rnnt",
        "threads": 2,
        "min_ram_mb": 768,
    }
    monkeypatch.setattr(runtime, "_resolve_model_request", lambda: dict(request))
    runtime.restart_worker(_park=True)
    mouse.rig.event(type="hello")
    retry = runtime._mouse_retry_timer
    assert retry is not None
    cast(FakeTimer, retry).fire()
    assert runtime.command_mouse_status == "ready"  # кнопка захвачена и при выгрузке
    mouse.press()
    assert not runtime._model_parked and runtime._loading_model
    assert mouse.sent("model.load")
    assert_phase(runtime, DictationPhase.IDLE)
    assert mouse.manager.state is MouseHoldState.IDLE
    mouse.rig.pill.show_state.assert_called_with(PillState.LOADING_MODEL)
    retry = runtime._mouse_retry_timer
    assert retry is not None
    cast(FakeTimer, retry).fire()
    assert runtime.command_mouse_status == "suspended"  # пока модель грузится
    mouse.release()
    mouse.rig.event(type="model.loaded")
    mouse.rig.event(type="result", utterance_id="file", text="проверка")
    runtime._tick_hotkey()
    retry = runtime._mouse_retry_timer
    assert retry is not None
    cast(FakeTimer, retry).fire()
    assert runtime.command_mouse_status == "ready"
    mouse.record_mouse()


def test_command_hotkey_press_reloads_model(monkeypatch: pytest.MonkeyPatch) -> None:
    mouse = MouseRuntimeRig(monkeypatch)
    runtime = mouse.runtime
    monkeypatch.setattr(
        runtime,
        "_resolve_model_request",
        lambda: {
            "type": "model.load",
            "id": "model",
            "revision": "revision",
            "dir": "/tmp/trusted",
            "layout": "onnx-asr-gigaam-v3",
            "variant": "gigaam-v3-e2e-rnnt",
            "threads": 2,
            "min_ram_mb": 768,
        },
    )
    runtime.restart_worker(_park=True)
    mouse.rig.event(type="hello")
    assert runtime.command_hotkey is not None
    runtime.command_hotkey.fsm.press(mouse.rig.now)
    assert mouse.sent("model.load") and runtime._loading_model
    assert runtime.command_hotkey.fsm.state is HotkeyState.IDLE
    assert_phase(runtime, DictationPhase.IDLE)


@pytest.mark.parametrize("value", [-1, 1, 30, "5", True, None, 5.0])
def test_invalid_saved_value_keeps_model_in_memory(value: object) -> None:
    settings = from_dict({"model_unload_minutes": value})
    assert settings.model_unload_minutes == 0


@pytest.mark.parametrize("minutes", MODEL_UNLOAD_MINUTES)
def test_saved_choice_is_loaded(minutes: int) -> None:
    assert from_dict({"model_unload_minutes": minutes}).model_unload_minutes == minutes


def test_bridge_saves_choice_applies_it_and_rejects_others() -> None:
    settings = Settings()
    save = Mock()
    apply = Mock(spec=SettingsApply)
    bridge = SettingsBridge(settings, save=save, apply=apply)
    prop = "modelUnloadMinutes"  # стабы Qt описывают свойство методом: зовём сеттер
    setattr(bridge, prop, 15)
    assert settings.model_unload_minutes == 15 and bridge.modelUnloadMinutes == 15
    save.assert_called_once_with(settings)
    apply.model_unload_minutes.assert_called_once_with(15)
    for invalid in (7, -5, True):
        setattr(bridge, prop, invalid)
    assert settings.model_unload_minutes == 15
    assert save.call_count == 1 and apply.model_unload_minutes.call_count == 1


# US-2.7 × US-2.6 (T-278) ------------------------------------------------------------------


def record_ids(rig: Rig) -> list[str]:
    return [
        str(item.args[0]["utterance_id"])
        for item in rig.supervisor.send.call_args_list
        if item.args[0]["type"] == "record.start"
    ]


def test_overlap_queue_blocks_idle_unload_until_last_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Пока в очереди есть фраза, выгрузка по простою не срабатывает; владелец ввода — text."""
    rig = make_rig(monkeypatch)
    runtime = rig.runtime
    rig.press(rig.now)
    rig.release(rig.now + 1)
    rig.now += 2
    rig.press(rig.now)
    first, second = record_ids(rig)
    assert runtime.orchestrator.pending_utterances == 2
    assert runtime._input_owner == "text"
    # Третье нажатие при двух фразах: автомат клавиши возвращается к распознаванию.
    rig.release(rig.now + 1)
    rig.press(rig.now + 2)
    assert rig.hotkey.fsm.state is HotkeyState.PROCESSING
    assert len(record_ids(rig)) == 2
    assert runtime._work_pending()
    rig.now += FIVE_MIN_S + 10
    fire_idle(rig)
    assert not runtime._model_parked
    rig.event(type="result", utterance_id=first, text=MARKER)
    for timer in list(rig.timers):
        # Только пилюля «Готово» первой фразы: сторож распознавания второй не трогаем.
        if timer.name == "scheduled" and timer.active and timer.interval < PROCESSING_WATCHDOG_MS:
            timer.fire()
    assert runtime.orchestrator.pending_utterances == 1
    assert runtime._input_owner == "text" and runtime._work_pending()
    rig.event(type="result", utterance_id=second, text=MARKER)
    settle(rig)
    assert_phase(runtime, DictationPhase.IDLE)
    assert runtime._input_owner is None
    assert rig.hotkey.fsm.state is HotkeyState.IDLE
    assert not runtime._model_parked
    rig.now += FIVE_MIN_S
    fire_idle(rig)
    assert runtime._model_parked


def start_overlap(rig: PhysicalHotkeyRig) -> tuple[str, str]:
    """A отпущена и распознаётся, B нажата и пишется (настоящие автомат и оркестратор)."""
    rig.press(rig.now)
    rig.release(rig.now + 1)
    rig.now += 2
    rig.press(rig.now)
    first, second = record_ids(rig)
    assert rig.runtime.orchestrator.pending_utterances == 2
    return first, second


def fire_phrase_outcome(rig: Rig) -> None:
    """Только пилюля исхода фразы: сторожа распознавания и смены модели не трогаем."""
    for timer in list(rig.timers):
        if timer.name == "scheduled" and timer.active and timer.interval < PROCESSING_WATCHDOG_MS:
            timer.fire()


def test_overlap_queue_defers_model_switch_until_last_phrase(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-278: смена модели при непустой очереди ждёт исхода последней фразы (on_idle);
    обе фразы вставлены прежней моделью, воркер не заменён посреди очереди."""
    rig = make_rig(monkeypatch)
    runtime = rig.runtime
    first, second = start_overlap(rig)
    workers = rig.supervisor_factory.call_count
    runtime.switch_model(min_ram_mb=100, pause=True)
    assert runtime._pending_switch == (100, True)
    rig.release(rig.now + 1)
    rig.event(type="result", utterance_id=first, text=MARKER)
    fire_phrase_outcome(rig)
    assert runtime.orchestrator.pending_utterances == 1
    assert runtime._pending_switch == (100, True), "смена началась посреди очереди"
    assert rig.supervisor_factory.call_count == workers
    rig.event(type="result", utterance_id=second, text=MARKER)
    assert rig.paste.call_count == 2
    replacement = Mock(state="running", generation=2, restarting=False, ready=True)
    rig.supervisor_factory.return_value = replacement
    fire_phrase_outcome(rig)
    assert_phase(runtime, DictationPhase.IDLE)
    assert runtime._pending_switch is None and runtime._switch_paused
    assert rig.supervisor_factory.call_count == workers + 1
    assert runtime.supervisor is replacement
    runtime.shutdown()


def test_quit_with_overlap_queue_cancels_both_without_paste(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T-278: «Выход» при непустой очереди — обе фразы отменены, вставок нет, хоткей и
    индикатор записи освобождены."""
    rig = make_rig(monkeypatch)
    runtime = rig.runtime
    first, second = start_overlap(rig)
    rig.guard.set_recording.reset_mock()
    runtime.shutdown()
    cancelled = [
        item.args[0]["utterance_id"]
        for item in rig.supervisor.send.call_args_list
        if item.args[0]["type"] == "record.cancel"
    ]
    assert {first, second} <= set(cancelled)
    rig.guard.set_recording.assert_called_with(False)
    assert rig.hotkey.fsm.state is HotkeyState.IDLE
    rig.paste.assert_not_called()


# Код-ревью US-2.6: отказы пробуждения, смена модели, доверие мыши, предел и окно ----------


TRUSTED_REQUEST: dict[str, Any] = {
    "type": "model.load",
    "id": "model",
    "revision": "revision",
    "dir": "/tmp/trusted",
    "layout": "onnx-asr-gigaam-v3",
    "variant": "gigaam-v3-e2e-rnnt",
    "threads": 2,
    "min_ram_mb": 768,
}


def fail_wake(rig: Rig, monkeypatch: pytest.MonkeyPatch, failure: str) -> None:
    """Подменяет загрузку так, чтобы пробуждение не началось (синхронный отказ)."""
    from astra_voice.core.model_source import ModelRevoked

    runtime = rig.runtime
    request = runtime._resolve_model_request()
    assert request is not None
    if failure == "send":

        def send(message: dict[str, Any], **kwargs: object) -> None:
            if message["type"] == "model.load":
                raise RuntimeError(MARKER)
            rig.send(message, **kwargs)

        rig.supervisor.send.side_effect = send
    elif failure == "none":
        monkeypatch.setattr(runtime, "_resolve_model_request", lambda: None)
    elif failure == "revoked":

        def revoked() -> dict[str, Any]:
            raise ModelRevoked("revision")

        monkeypatch.setattr(runtime, "_resolve_model_request", revoked)
    else:
        monkeypatch.setattr(runtime, "_resolve_model_request", lambda: {**request, "threads": 0})


@pytest.mark.parametrize(
    ("failure", "text"),
    [
        ("send", ERROR_MODEL_LOAD_FAILED),
        ("none", ERROR_MODEL_NOT_LOADED),
        ("revoked", ERROR_MODEL_REVOKED),
        ("params", ERROR_MODEL_LOAD_FAILED),
    ],
)
def test_failed_wake_keeps_model_unloaded_and_frees_hotkey(
    monkeypatch: pytest.MonkeyPatch, failure: str, text: str
) -> None:
    """MAJOR-1: загрузка не началась — модель остаётся выгруженной, клавиша не залипает,
    следующее нажатие снова грузит модель, а не шлёт запись воркеру без модели."""
    rig = make_rig(monkeypatch)
    park(rig)
    original_send = rig.supervisor.send.side_effect
    original_resolve = rig.runtime._resolve_model_request
    fail_wake(rig, monkeypatch, failure)
    rig.pill.show_state.reset_mock()
    rig.press(rig.now)
    assert rig.hotkey.fsm.state is HotkeyState.IDLE
    assert rig.runtime._model_parked and not rig.runtime._loading_model
    assert not rig.runtime._wake_dictation
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=text)
    rig.release(rig.now + 1)
    assert rig.hotkey.fsm.state is HotkeyState.IDLE
    assert not rig.runtime._work_pending()
    assert "record.start" not in sent(rig)
    # Причина устранена: следующее нажатие грузит модель и пишет фразу.
    rig.supervisor.send.side_effect = original_send
    monkeypatch.setattr(rig.runtime, "_resolve_model_request", original_resolve)
    loads = sent(rig).count("model.load")
    rig.now += 5
    rig.press(rig.now)
    assert sent(rig).count("model.load") == loads + 1
    assert "record.start" not in sent(rig)
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    rig.release(rig.now + 2)
    rig.event(type="result", text=MARKER)
    assert rig.paste.call_args.args[0] == MARKER


def test_failed_wake_twice_in_same_worker_retries_each_press(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Две неудачи пробуждения в одном воркере не запрещают третью попытку нажатием."""
    rig = make_rig(monkeypatch)
    park(rig)
    original_send = rig.supervisor.send.side_effect
    fail_wake(rig, monkeypatch, "send")
    for _ in range(2):
        rig.press(rig.now)
        rig.release(rig.now + 1)
        rig.now += 2
    assert rig.runtime._model_parked and rig.hotkey.fsm.state is HotkeyState.IDLE
    rig.supervisor.send.side_effect = original_send
    loads = sent(rig).count("model.load")
    rig.press(rig.now)
    assert sent(rig).count("model.load") == loads + 1 and rig.runtime._loading_model


def test_worker_load_error_after_wake_unloads_again(monkeypatch: pytest.MonkeyPatch) -> None:
    """Воркер ответил ошибкой загрузки: клавиша свободна, следующее нажатие грузит снова."""
    rig = make_rig(monkeypatch)
    park(rig)
    rig.press(rig.now)
    rig.event(type="error", code="engine-failed", request_type="model.load")
    assert rig.hotkey.fsm.state is HotkeyState.IDLE
    assert rig.runtime._model_parked and not rig.runtime._loading_model
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
    rig.release(rig.now + 1)
    loads = sent(rig).count("model.load")
    rig.press(rig.now + 5)
    assert sent(rig).count("model.load") == loads + 1
    assert "record.start" not in sent(rig)


def test_switch_while_unloaded_uses_pause_not_second_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MINOR-2: смена модели при выгруженной — с паузой, без временного воркера рядом;
    нажатие во время смены не будит прежнюю модель."""
    rig = make_rig(monkeypatch)
    park(rig)
    monkeypatch.setattr(rig.runtime, "can_switch_without_pause", lambda mb: True)
    loads = sent(rig).count("model.load")
    workers = rig.supervisor_factory.call_count
    rig.runtime.switch_model(min_ram_mb=768)
    assert rig.runtime._switch_active() and rig.runtime._switch_paused
    assert rig.runtime._switch_candidate is None
    assert not rig.runtime._model_parked and rig.runtime._loading_model
    assert rig.supervisor_factory.call_count == workers + 1  # воркер заменён, не добавлен
    rig.press(rig.now)
    assert sent(rig).count("model.load") == loads  # прежняя модель не грузится
    rig.event(type="hello")
    assert sent(rig).count("model.load") == loads + 1  # только новая


def test_press_while_unloaded_during_switch_waits_for_new_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Защитная ветка MINOR-2: выгружена и идёт смена — прежнюю не будим; смена
    не удалась — автомат клавиши свободен."""
    rig = make_rig(monkeypatch)
    park(rig)
    rig.runtime._switch_in_progress = True
    loads = sent(rig).count("model.load")
    rig.pill.show_state.reset_mock()
    rig.press(rig.now)
    assert sent(rig).count("model.load") == loads
    assert rig.runtime._model_parked and rig.runtime._wake_dictation
    rig.pill.show_state.assert_called_once_with(PillState.LOADING_MODEL)
    rig.runtime._finish_switch("failed")
    assert rig.hotkey.fsm.state is HotkeyState.IDLE
    assert not rig.runtime._wake_dictation


def test_always_during_appimage_removal_keeps_model_unloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MINOR-3: «Всегда» во время подготовки удаления AppImage не снимает выгрузку
    без загрузки; следующее нажатие грузит модель, а не пишет без неё."""
    rig = make_rig(monkeypatch)
    park(rig)
    assert rig.runtime.reserve_appimage_removal()
    loads = sent(rig).count("model.load")
    rig.runtime.apply_model_unload_minutes(0)
    assert rig.runtime._model_parked and sent(rig).count("model.load") == loads
    rig.runtime.release_appimage_removal()
    rig.press(rig.now)
    assert sent(rig).count("model.load") == loads + 1
    assert "record.start" not in sent(rig)
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert_phase(rig.runtime, DictationPhase.RECORDING)


def parked_mouse(monkeypatch: pytest.MonkeyPatch) -> MouseRuntimeRig:
    mouse = MouseRuntimeRig(monkeypatch)
    monkeypatch.setattr(mouse.runtime, "_resolve_model_request", lambda: dict(TRUSTED_REQUEST))
    mouse.runtime._model_load_request = dict(TRUSTED_REQUEST)
    mouse.runtime.restart_worker(_park=True)
    mouse.rig.event(type="hello")
    retry = mouse.runtime._mouse_retry_timer
    assert retry is not None
    cast(FakeTimer, retry).fire()
    return mouse


@pytest.mark.parametrize("distrust", ["revoked", "revocation-unknown", "manual-dir", "record"])
def test_unloaded_mouse_still_checks_static_model_trust(
    monkeypatch: pytest.MonkeyPatch, distrust: str
) -> None:
    """MINOR-4: при выгруженной модели мышь пропускает только проверку загруженного
    поколения; отзыв, неизвестный список отзыва, ручной каталог и запись — проверяются."""
    mouse = parked_mouse(monkeypatch)
    runtime = mouse.runtime
    assert runtime._model_parked and runtime._mouse_allowed()
    if distrust == "revoked":
        runtime._revoked_check = lambda _id, revision: True
    elif distrust == "revocation-unknown":
        runtime.revocation_unknown = True
    elif distrust == "manual-dir":
        runtime.settings.extra["model_dir"] = "/tmp/manual"
    else:
        mouse.record.state = "broken"
    assert not runtime._mouse_allowed()
    mouse.press()
    assert runtime._model_parked
    assert not mouse.sent("model.load")


@pytest.mark.parametrize("mode", ["ptt", "toggle"])
def test_record_limit_counts_from_real_start_after_wake(
    monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Лимит — от старта: при удержании PTT либо новом нажатии configured toggle."""
    from astra_voice.core.constants import RECORD_LIMIT_S

    rig = PhysicalHotkeyRig(
        monkeypatch,
        from_dict({"model_dir": "/tmp/model", "model_unload_minutes": 5, "hotkey_mode": mode}),
    )
    rig.runtime.start()
    load_model(rig)
    park(rig)
    rig.press(rig.now)
    if mode == "toggle":
        rig.release(rig.now + 0.1)
    rig.now += RECORD_LIMIT_S + 30  # загрузка дольше предела
    rig.hotkey.fsm.tick(rig.now)
    expected = HotkeyState.IDLE if mode == "toggle" else HotkeyState.RECORDING
    assert rig.hotkey.fsm.state is expected
    assert "record.start" not in sent(rig)
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    if mode == "toggle":
        assert "record.start" not in sent(rig)
        rig.press(rig.now)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    started = rig.now
    rig.hotkey.fsm.tick(started + RECORD_LIMIT_S - 1)
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    rig.hotkey.fsm.tick(started + RECORD_LIMIT_S)
    assert rig.hotkey.fsm.state is HotkeyState.PROCESSING  # предел — от старта


def test_fsm_recording_without_physical_press_does_not_autostart(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RECORDING сам по себе не разрешает запись после загрузки."""
    rig = make_rig(monkeypatch)
    park(rig)
    rig.hotkey.fsm.press(rig.now)  # Намеренно нет физического KeyPress.
    assert rig.hotkey.fsm.state is HotkeyState.RECORDING
    assert not rig.hotkey.physical_key_down
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert "record.start" not in sent(rig)
    assert cast(HotkeyState, rig.hotkey.fsm.state) is HotkeyState.IDLE
    assert_phase(rig.runtime, DictationPhase.IDLE)
    rig.pill.hide.assert_called()


def test_target_window_is_fixed_at_press_not_after_load(monkeypatch: pytest.MonkeyPatch) -> None:
    """MINOR-6: фокус ушёл за время загрузки — текст не уходит в новое окно: окно-цель
    — на момент нажатия, вставка идёт штатной веткой «окно сменилось»."""
    rig = make_rig(monkeypatch)
    park(rig)
    rig.press(rig.now)
    rig.x11.target_window.return_value = 9999  # пользователь переключился, пока грузится
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert_phase(rig.runtime, DictationPhase.RECORDING)
    rig.release(rig.now + 2)
    rig.event(type="result", text=MARKER)
    assert rig.paste.call_args.args[1] == rig.target
    # Следующая фраза без выгрузки — окно на её старте.
    rig.now += 5
    rig.recognize()
    assert rig.paste.call_args.args[1] == snapshot_target(rig.x11, 9999)


def test_autostart_failure_keeps_its_error_pill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Пункт 7: синхронный провал автостарта показал ошибку — готовность модели её
    не прячет, автомат клавиши свободен."""
    rig = make_rig(monkeypatch)
    park(rig)

    def send(message: dict[str, Any], **kwargs: object) -> None:
        if message["type"] == "record.start":
            raise RuntimeError(MARKER)
        rig.send(message, **kwargs)

    rig.supervisor.send.side_effect = send
    rig.press(rig.now)
    rig.event(type="model.loaded")
    rig.pill.reset_mock()
    rig.event(type="result", utterance_id="file", text="проверка")
    assert rig.runtime.orchestrator.cleanup_pending
    rig.pill.hide.assert_not_called()
    # Ошибка отправки не подтверждает, что воркер уже освободил захват.
    rig.event(type="record.stopped", utterance_id=rig.runtime.orchestrator._utterance_id)
    assert rig.pill.mock_calls[-1] == call.show_state(
        PillState.ERROR, text=ERROR_RECOGNITION_FAILED
    )
    rig.pill.hide.assert_not_called()
    assert rig.hotkey.fsm.state is HotkeyState.IDLE


def test_autostart_silent_refusal_hides_loading_pill(monkeypatch: pytest.MonkeyPatch) -> None:
    """Молчаливый отказ автостарту (ввод занят) не оставляет «Загружаю модель…»."""
    rig = make_rig(monkeypatch)
    park(rig)
    rig.press(rig.now)
    rig.event(type="model.loaded")
    rig.runtime._input_owner = "keyboard"  # ввод занят другим источником
    rig.pill.reset_mock()
    rig.event(type="result", utterance_id="file", text="проверка")
    rig.runtime._input_owner = None
    assert_phase(rig.runtime, DictationPhase.IDLE)
    assert rig.hotkey.fsm.state is HotkeyState.IDLE
    rig.pill.hide.assert_called()
    assert "record.start" not in sent(rig)


@pytest.mark.parametrize("value", [30, -5, 1])
def test_invalid_admin_policy_value_is_logged(caplog: pytest.LogCaptureFixture, value: int) -> None:
    """Пункт 8: недопустимое значение политики не применяется молча — WARNING в журнал."""
    from astra_voice.core import policy as pol

    settings = Settings(model_unload_minutes=15)
    with caplog.at_level("WARNING", logger="astra_voice.core.policy"):
        result = pol.effective(
            settings, pol.Policy(values={"model_unload_minutes": value}, status=pol.PolicyStatus.OK)
        )
    assert result.model_unload_minutes == 15
    assert any(
        record.levelname == "WARNING" and "model_unload_minutes" in record.getMessage()
        for record in caplog.records
    )
    valid = pol.effective(
        settings, pol.Policy(values={"model_unload_minutes": 60}, status=pol.PolicyStatus.OK)
    )
    assert valid.model_unload_minutes == 60


# Финальный проход ревью US-2.6 ----------------------------------------------------------


def crash_during_wake(rig: PhysicalHotkeyRig) -> None:
    """Нажатие будит модель; воркер падает до её загрузки, новый воркер грузит её сам."""
    park(rig)
    rig.press(rig.now)
    assert rig.runtime._loading_model
    rig.event(type="error", code="worker-crashed")
    rig.supervisor.generation += 1
    loads = sent(rig).count("model.load")
    rig.event(type="hello")
    assert sent(rig).count("model.load") == loads + 1 and rig.runtime._loading_model


def test_load_error_after_crash_during_wake_unloads_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """minor: пробуждение прервал перезапуск воркера, загрузка в новом тоже упала —
    модель снова выгружена, клавиша свободна, следующее нажатие грузит её, а не шлёт
    record.start в воркер без модели."""
    rig = make_rig(monkeypatch)
    crash_during_wake(rig)
    rig.event(type="error", code="engine-failed", request_type="model.load")
    assert rig.hotkey.fsm.state is HotkeyState.IDLE
    assert rig.runtime._model_parked and not rig.runtime._loading_model
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_MODEL_LOAD_FAILED)
    rig.release(rig.now + 1)
    loads = sent(rig).count("model.load")
    rig.press(rig.now + 5)
    assert sent(rig).count("model.load") == loads + 1
    assert "record.start" not in sent(rig)
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert_phase(rig.runtime, DictationPhase.RECORDING)


def test_crash_during_wake_then_load_in_new_worker_keeps_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Загрузка в новом воркере удалась: модель в памяти, следующее нажатие пишет."""
    rig = make_rig(monkeypatch)
    crash_during_wake(rig)
    rig.event(type="model.loaded")
    rig.event(type="result", utterance_id="file", text="проверка")
    assert not rig.runtime._model_parked and rig.runtime._wake_generation is None
    assert rig.hotkey.fsm.state is HotkeyState.IDLE  # падение воркера сбросило нажатие
    rig.release(rig.now + 1)
    rig.press(rig.now + 5)
    assert sent(rig)[-1] == "record.start"
    assert_phase(rig.runtime, DictationPhase.RECORDING)


def test_mouse_press_while_unloaded_in_disallowed_session_keeps_model_unloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nit: как клавиша «Команды», кнопка мыши в сессии, где команды запрещены
    (экран заблокирован), выгруженную модель не будит."""
    from dataclasses import replace

    mouse = parked_mouse(monkeypatch)
    runtime = mouse.runtime
    loads = len(mouse.sent("model.load"))
    mouse.snapshot = replace(mouse.snapshot, locked=True)
    runtime._mouse_pending(mouse.manager.generation)
    assert runtime._model_parked and not runtime._loading_model
    assert len(mouse.sent("model.load")) == loads
    mouse.snapshot = replace(mouse.snapshot, locked=False)
    runtime._mouse_pending(mouse.manager.generation)
    assert not runtime._model_parked and runtime._loading_model
    assert len(mouse.sent("model.load")) == loads + 1


def test_always_while_unloaded_without_model_choice_shows_no_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """nit: «Всегда» при выгруженной модели и пустом выборе модели — не жест нажатия:
    пилюли ошибки нет, модель остаётся выгруженной, нажатие покажет причину."""
    rig = make_rig(monkeypatch)
    park(rig)
    monkeypatch.setattr(rig.runtime, "_resolve_model_request", lambda: None)
    rig.pill.show_state.reset_mock()
    rig.runtime.apply_model_unload_minutes(0)
    assert rig.runtime._model_parked
    assert call(PillState.ERROR, text=ERROR_MODEL_NOT_LOADED) not in (
        rig.pill.show_state.call_args_list
    )
    rig.press(rig.now)
    rig.pill.show_state.assert_called_with(PillState.ERROR, text=ERROR_MODEL_NOT_LOADED)
    assert rig.hotkey.fsm.state is HotkeyState.IDLE


def test_failed_selfcheck_after_wake_then_crash_does_not_unload_or_grab_mouse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N2 (W7): пробуждение закончилось проваленной самопроверкой, затем воркер упал.
    Hello нового воркера не считает пробуждение прерванным: модель не уходит в
    выгруженные мимо провала, кнопка мыши не захватывается и клик не съедается."""
    mouse = parked_mouse(monkeypatch)
    runtime = mouse.runtime
    mouse.press()
    assert runtime._loading_model and runtime._wake_generation is not None
    mouse.release()
    mouse.rig.event(type="model.loaded")
    mouse.rig.event(type="result", utterance_id="file", text="совсем не то")
    assert runtime._selfcheck == "failed"
    mouse.rig.event(type="error", code="worker-crashed")
    mouse.rig.supervisor.generation += 1
    mouse.rig.event(type="hello")
    assert runtime._selfcheck == "failed"
    assert not runtime._model_parked
    assert not runtime._mouse_allowed()
    retry = runtime._mouse_retry_timer
    if retry is not None:
        cast(FakeTimer, retry).fire()
    assert runtime.command_mouse_status != "ready"
    loads = len(mouse.sent("model.load"))
    mouse.press()
    assert runtime._input_owner is None
    assert not mouse.sent("record.start")
    assert len(mouse.sent("model.load")) == loads


def test_mouse_not_allowed_with_failed_selfcheck_even_when_unloaded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N2: выгруженная модель пропускает только незагруженную самопроверку, не проваленную."""
    mouse = parked_mouse(monkeypatch)
    runtime = mouse.runtime
    assert runtime._model_parked and runtime._mouse_allowed()
    runtime._selfcheck = "failed"
    assert not runtime._mouse_allowed()
