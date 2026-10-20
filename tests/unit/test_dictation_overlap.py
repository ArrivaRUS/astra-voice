"""US-2.7: запись новой фразы, пока предыдущая распознаётся (тесты до кода, T-260…T-279).

Given идёт распознавание фразы A, When начинаю запись B, Then запись принята,
обе вставки приходят по порядку (stories.md, US-2.7).

Тесты написаны ДО реализации и на коде 0.1.6 красные по одной причине: нажатие во
время распознавания не начинает запись B (``HotkeyFsm.press`` игнорирует нажатие
в PROCESSING, а ``DictationOrchestrator.on_hotkey_state`` отвергает его по фазе).
Тесты с пометкой «guard» зелёные уже сейчас и должны остаться зелёными после
реализации. Тесты с пометкой «Q<n>» зависят от решения по вопросу n из отчёта
tester (рекомендация зашита в ожидание; другое решение — правка теста, не кода).

Проверяемый контракт (без новых импортов, чтобы файл собирался на старом коде):

* Очередь фраз в ``DictationOrchestrator``: у каждой фразы свои ``utterance_id``,
  поколение воркера, окно-цель (окно фокуса при старте ЭТОЙ фразы), замеры,
  повтор BUSY и сторож PROCESSING. Записывается не больше одной фразы; всего
  незавершённых фраз не больше двух (предлагаемая константа
  ``MAX_PENDING_UTTERANCES = 2``, совпадает с очередью воркера: одно задание
  выполняется, одно ждёт). Третье нажатие игнорируется и НЕ отменяет A и B.
* Доставка строго FIFO по порядку нажатий: результат B ждёт, пока A не дойдёт до
  исхода (вставлено / буфер / пусто / ошибка / отмена). Отмена или ошибка одной
  фразы не трогает другую; смена поколения (перезапуск воркера, смена модели)
  завершает обе одним сообщением.
* ``phase`` агрегирован: IDLE/FINISHING только при пустой очереди (на этом держатся
  отложенная смена модели ``on_idle``, выгрузка US-2.6, удаление AppImage, проверка
  микрофона); RECORDING, пока хоть одна фраза пишется.
* Хоткей: нажатие в PROCESSING начинает новую запись (предлагается разрешить
  ``press`` из PROCESSING в ``HotkeyFsm``; ``hotkey_done``/``hotkey_cancel``
  оркестратор зовёт только при пустой очереди, иначе исход A сбросил бы FSM
  записи B). Escape отменяет только самую новую фразу.
* Индикация (О4): пока пишется B, пилюля и трей показывают запись, а
  ``set_recording(False)`` не вызывается исходами A.
* Q1: вставка A не выполняется, пока пишется B (в PTT клавиша хоткея зажата —
  ``paste`` отдал бы ``keys-held`` и текст A ушёл бы только в буфер); A
  вставляется сразу после отпускания B, не дожидаясь результата B.
* Командный режим перекрытия не допускает (guard).
* Текст фраз не попадает в журнал и статистику.

Запуск: ``QT_QPA_PLATFORM=offscreen pytest tests/unit/test_dictation_overlap.py``.
"""

from __future__ import annotations

import logging
from typing import Any, cast

import pytest
from test_dictation import Rig

from astra_voice.core.command_mode import CommandMode, SessionSnapshot
from astra_voice.core.dictation import (
    BUSY_RETRY_MS,
    CANCEL_TIMEOUT_MS,
    PROCESSING_WATCHDOG_MS,
    TEST_BUSY,
    DictationPhase,
    MicrophoneTestUpdate,
)
from astra_voice.platform.cowork import DeliveryResult
from astra_voice.platform.hotkey import HotkeyMode, HotkeyState
from astra_voice.platform.paste import PasteOutcomeKind
from astra_voice.ui.pill import (
    CLIPBOARD_WINDOW_CHANGED,
    ERROR_MICROPHONE_CHANGED,
    ERROR_RECOGNITION_FAILED,
    ERROR_RECOGNITION_RESTARTED,
    STATE_DURATION_MS,
    PillState,
)
from astra_voice.ui.tray_icons import TrayState

pytestmark = pytest.mark.unit

A_TEXT = "ГЕЛИОТРОП-А первая фраза"
B_TEXT = "ГЕЛИОТРОП-Б вторая фраза"
LISTENING_STATES = (PillState.LISTENING, PillState.LISTENING_SILENT, PillState.LIMIT)
OVERLAP_MISSING = "US-2.7: нажатие во время распознавания A не начало запись B"
A, B, C = 0, 1, 2


class OverlapRig(Rig):
    """Rig из test_dictation с адресацией событий по номеру фразы."""

    @property
    def uids(self) -> list[str]:
        return [str(m["utterance_id"]) for m, _ in self.sent if m["type"] == "record.start"]

    def sent_ids(self, kind: str) -> list[str]:
        return [str(m.get("utterance_id")) for m, _ in self.sent if m["type"] == kind]

    def event_for(self, index: int, kind: str, **fields: object) -> None:
        self.event(kind, utterance_id=self.uids[index], **fields)

    def result_for(self, index: int, text: str) -> None:
        self.now += 0.25
        self.event_for(index, "result", text=text)

    def texts(self) -> list[str]:
        return [text for text, _, _ in self.pasted]

    def active(self, delay: int) -> list[Any]:
        return [t for t in self.timers if t.delay == delay and not t.cancelled and not t.fired]


@pytest.fixture
def rig() -> OverlapRig:
    return OverlapRig()


def phase_of(rig: OverlapRig) -> DictationPhase:
    """Читает фазу заново после событий, без сужения типа mypy."""
    return rig.core.phase


def begin_overlap(rig: OverlapRig, *, release_b: bool = False) -> None:
    """A записана и распознаётся; B нажата (и при release_b — отпущена)."""
    rig.start()
    rig.stop()
    assert phase_of(rig) == DictationPhase.PROCESSING
    assert rig.sent_ids("recognize") == [rig.uids[A]]
    rig.start()
    assert len(rig.uids) == 2, OVERLAP_MISSING
    if release_b:
        rig.stop()
        assert rig.sent_ids("recognize") == [rig.uids[A], rig.uids[B]]


def fire_finishing_timers(rig: OverlapRig) -> None:
    """Срабатывают все таймеры возврата в IDLE (длительности завершающих пилюль)."""
    durations = set(STATE_DURATION_MS.values())
    for timer in [t for t in rig.timers if t.delay in durations]:
        if not timer.cancelled and not timer.fired:
            timer.fire()


# T-260 ---------------------------------------------------------------------------------


def test_press_during_recognition_starts_second_recording(rig: OverlapRig) -> None:
    """T-260: B принята: свой id, A не отменена, индикация записи включена."""
    begin_overlap(rig)
    a_uid, b_uid = rig.uids
    assert a_uid != b_uid
    assert "record.cancel" not in rig.commands()
    assert phase_of(rig) == DictationPhase.RECORDING
    assert rig.recording is True
    assert rig.pill.calls[-1][0] == PillState.LISTENING
    assert rig.tray.state == TrayState.LISTENING
    rig.assert_hotkey_state(HotkeyState.RECORDING)
    # Уровень B доходит до пилюли; поздний уровень A (чужой id) игнорируется.
    rig.event_for(B, "level", peak_dbfs=-30.0)
    assert rig.pill.calls[-1] == (PillState.LISTENING, None, 0.5)
    calls = len(rig.pill.calls)
    rig.event_for(A, "level", peak_dbfs=-6.0)
    assert len(rig.pill.calls) == calls


# T-261 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("arrival", ["a-first", "b-first"])
def test_pastes_follow_press_order(rig: OverlapRig, arrival: str) -> None:
    """T-261: вставки по порядку нажатий, даже если результат B пришёл раньше A."""
    begin_overlap(rig, release_b=True)
    if arrival == "b-first":
        rig.result_for(B, B_TEXT)
        assert rig.pasted == [], "результат B вставлен раньше, чем A дошла до исхода"
        rig.result_for(A, A_TEXT)
    else:
        rig.result_for(A, A_TEXT)
        assert rig.texts() == [A_TEXT]
        rig.result_for(B, B_TEXT)
    assert rig.texts() == [A_TEXT, B_TEXT]
    assert rig.core.last_text == B_TEXT and rig.tray.has_last_text
    assert [e["result"] for e in rig.stats.events if e["type"] == "dictation"] == ["ok", "ok"]
    assert phase_of(rig) == DictationPhase.FINISHING
    rig.assert_hotkey_state(HotkeyState.IDLE)


def test_second_result_during_first_paste_is_not_dropped(rig: OverlapRig) -> None:
    """T-261: результат B, пришедший во вложенном цикле вставки A, вставляется после A."""
    begin_overlap(rig, release_b=True)
    rig.during_paste = lambda: rig.result_for(B, B_TEXT)
    rig.result_for(A, A_TEXT)
    rig.during_paste = None
    assert rig.texts() == [A_TEXT, B_TEXT]


def test_busy_retry_of_first_keeps_order(rig: OverlapRig) -> None:
    """T-261: повтор BUSY фразы A не пропускает B вперёд."""
    begin_overlap(rig, release_b=True)
    rig.outcomes.append(PasteOutcomeKind.BUSY)
    rig.result_for(A, A_TEXT)
    rig.result_for(B, B_TEXT)
    assert rig.texts() == [A_TEXT], "B вставлена, пока A ждёт повтора BUSY"
    (retry,) = rig.active(BUSY_RETRY_MS)
    retry.fire()
    assert rig.texts() == [A_TEXT, A_TEXT, B_TEXT]


# T-262 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("b_state", ["recording", "processing"])
def test_escape_cancels_only_newest_phrase(rig: OverlapRig, b_state: str) -> None:
    """T-262: Esc отменяет B; A распознаётся и вставляется."""
    begin_overlap(rig, release_b=b_state == "processing")
    a_uid, b_uid = rig.uids
    rig.fsm.escape(rig.now)
    assert rig.sent_ids("record.cancel") == [b_uid]
    assert rig.recording == (b_state == "recording")
    rig.event_for(B, "cancelled")
    rig.result_for(A, A_TEXT)
    assert rig.texts() == [A_TEXT]
    assert a_uid not in rig.sent_ids("record.cancel")
    results = sorted(str(e["result"]) for e in rig.stats.events if e["type"] == "dictation")
    assert results == ["cancelled", "ok"]


def test_escape_after_first_delivered_cancels_second(rig: OverlapRig) -> None:
    """T-262: A вставлена, B распознаётся — хоткей не сброшен исходом A, Esc отменяет B."""
    begin_overlap(rig, release_b=True)
    rig.result_for(A, A_TEXT)
    assert rig.fsm.state is not HotkeyState.IDLE, "исход A сбросил хоткей фразы B"
    rig.fsm.escape(rig.now)
    assert rig.sent_ids("record.cancel") == [rig.uids[B]]


# T-263 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("a_outcome", ["error", "empty", "watchdog"])
@pytest.mark.parametrize("b_state", ["recording", "processing"])
def test_first_failure_does_not_lose_second(rig: OverlapRig, a_outcome: str, b_state: str) -> None:
    """T-263: ошибка/пусто/сторож A не отменяет B; B вставляется."""
    begin_overlap(rig, release_b=b_state == "processing")
    b_uid = rig.uids[B]
    if a_outcome == "error":
        rig.event_for(A, "error", code="frame-too-large", message="x")
    elif a_outcome == "empty":
        rig.result_for(A, "   ")
    else:
        watchdogs = rig.active(PROCESSING_WATCHDOG_MS)
        assert watchdogs, "у A нет сторожа PROCESSING"
        watchdogs[0].fire()
        assert rig.sent_ids("record.cancel") == [rig.uids[A]]
    assert b_uid not in rig.sent_ids("record.cancel")
    if b_state == "recording":
        assert phase_of(rig) == DictationPhase.RECORDING and rig.recording
        rig.assert_hotkey_state(HotkeyState.RECORDING)
        rig.stop()
    else:
        assert rig.active(PROCESSING_WATCHDOG_MS), "исход A снял сторож PROCESSING фразы B"
    rig.result_for(B, B_TEXT)
    assert rig.texts() == [B_TEXT]
    assert [e["result"] for e in rig.stats.events if e["type"] == "dictation"][-1] == "ok"


# T-264 ---------------------------------------------------------------------------------


def test_each_phrase_pastes_into_its_own_start_window(rig: OverlapRig) -> None:
    """T-264: окно-цель — окно фокуса при старте КАЖДОЙ фразы (Q2)."""
    rig.window = 42
    rig.start()
    rig.stop()
    rig.window = 77
    rig.start()
    assert len(rig.uids) == 2, OVERLAP_MISSING
    rig.stop()
    rig.window = 99
    rig.result_for(A, A_TEXT)
    rig.result_for(B, B_TEXT)
    assert [(text, target) for text, target, _ in rig.pasted] == [(A_TEXT, 42), (B_TEXT, 77)]


# T-265 ---------------------------------------------------------------------------------


def test_third_press_is_ignored_without_cancelling_queue(rig: OverlapRig) -> None:
    """T-265: не больше двух незавершённых фраз; третье нажатие не трогает A и B."""
    begin_overlap(rig, release_b=True)
    rig.start()
    assert len(rig.uids) == 2, "третья запись при двух незавершённых фразах"
    assert "record.cancel" not in rig.commands(), "отказ третьему нажатию отменил очередь"
    rig.result_for(A, A_TEXT)
    # A завершена: в полёте только B — новое нажатие снова принимается.
    rig.start()
    assert len(rig.uids) == 3
    rig.stop()
    rig.result_for(B, B_TEXT)
    rig.result_for(C, "третья")
    assert rig.texts() == [A_TEXT, B_TEXT, "третья"]


# T-266 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("b_state", ["recording", "processing"])
def test_worker_restart_ends_whole_queue_once(rig: OverlapRig, b_state: str) -> None:
    """T-266: смена поколения (крах/смена модели) — одна ошибка на обе фразы, без вставок."""
    begin_overlap(rig, release_b=b_state == "processing")
    rig.worker_generation = 2
    rig.event_for(A, "audio.ready")
    restarted = [c for c in rig.pill.calls if c[1] == ERROR_RECOGNITION_RESTARTED]
    assert len(restarted) == 1
    assert not rig.recording
    assert phase_of(rig) == DictationPhase.FINISHING
    rig.assert_hotkey_state(HotkeyState.IDLE)
    # Поздние результаты старого поколения не воскрешают очередь.
    for index, text in ((A, A_TEXT), (B, B_TEXT)):
        rig.core.on_worker_event(
            {"type": "result", "generation": 1, "utterance_id": rig.uids[index], "text": text}
        )
    assert rig.pasted == []
    fire_finishing_timers(rig)
    assert phase_of(rig) == DictationPhase.IDLE
    rig.idle.assert_called_once_with()


def test_shutdown_cancels_every_queued_phrase(rig: OverlapRig) -> None:
    """T-266: выход при непустой очереди — отмена обеих фраз, индикаторы погашены."""
    begin_overlap(rig)
    a_uid, b_uid = rig.uids
    rig.core.shutdown()
    assert {a_uid, b_uid} <= set(rig.sent_ids("record.cancel"))
    assert not rig.recording and rig.pill.hidden
    rig.result_for(A, A_TEXT)
    rig.result_for(B, B_TEXT)
    assert rig.pasted == []


# T-267 ---------------------------------------------------------------------------------


def test_phase_and_on_idle_wait_for_whole_queue(rig: OverlapRig) -> None:
    """T-267: смена модели/выгрузка ждут конца очереди — on_idle один раз в конце."""
    begin_overlap(rig, release_b=True)
    rig.result_for(A, A_TEXT)
    fire_finishing_timers(rig)
    assert phase_of(rig) not in (DictationPhase.IDLE, DictationPhase.FINISHING)
    rig.idle.assert_not_called()
    rig.result_for(B, B_TEXT)
    fire_finishing_timers(rig)
    assert phase_of(rig) == DictationPhase.IDLE
    rig.idle.assert_called_once_with()


def test_microphone_test_refused_while_phrase_recognizes(rig: OverlapRig) -> None:
    """T-267 guard: проверка микрофона/мастер не вклиниваются в распознавание."""
    rig.start()
    rig.stop()
    updates: list[MicrophoneTestUpdate] = []
    assert rig.core.start_test("fake-device", updates.append) is False
    assert updates == [MicrophoneTestUpdate("error", message=TEST_BUSY)]
    assert rig.commands().count("record.start") == 1


def test_microphone_test_refused_during_overlap(rig: OverlapRig) -> None:
    """T-267: и при двух фразах в полёте проверка микрофона отказывает."""
    begin_overlap(rig, release_b=True)
    rig.result_for(A, A_TEXT)
    updates: list[MicrophoneTestUpdate] = []
    assert rig.core.start_test("fake-device", updates.append) is False
    assert updates == [MicrophoneTestUpdate("error", message=TEST_BUSY)]


# T-268 ---------------------------------------------------------------------------------


def test_command_press_during_text_recognition_is_ignored(rig: OverlapRig) -> None:
    """T-268 guard: «Команда» не перекрывает распознавание текста (Q4)."""
    rig.start()
    rig.stop()
    rig.core.on_hotkey_state(HotkeyState.RECORDING, "press", mode="command")
    assert rig.commands().count("record.start") == 1
    assert rig.core.mode == "text"


def test_text_press_during_command_recognition_is_ignored(rig: OverlapRig) -> None:
    """T-268 guard: текст не перекрывает распознавание команды (Q4)."""

    class Client:
        def submit(self, text: str, callback: Any) -> None:
            callback(DeliveryResult("delivered", "none"))

        def cancel(self) -> None:
            pass

    rig.core.command_mode = CommandMode(
        client=Client(),
        session=lambda: SessionSnapshot(known=True, locked=False),
        model_trusted=lambda: True,
        publish=lambda text: True,
        present=lambda feedback: None,
    )
    rig.core.on_hotkey_state(HotkeyState.RECORDING, "press", mode="command")
    rig.core.on_hotkey_state(HotkeyState.PROCESSING, "limit", mode="command")
    assert phase_of(rig) == DictationPhase.PROCESSING
    rig.core.on_hotkey_state(HotkeyState.RECORDING, "press")
    assert rig.commands().count("record.start") == 1
    assert "record.cancel" not in rig.commands()
    assert rig.core.mode == "command"


def test_command_press_during_overlap_is_ignored(rig: OverlapRig) -> None:
    """T-268: при A в распознавании и B в записи «Команда» не начинает третью запись."""
    begin_overlap(rig)
    rig.core.on_hotkey_state(HotkeyState.RECORDING, "press", mode="command")
    assert len(rig.uids) == 2
    assert rig.core.mode == "text"


# T-269 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("a_outcome", ["result", "error"])
def test_recording_indication_survives_first_outcome(rig: OverlapRig, a_outcome: str) -> None:
    """T-269: пока пишется B, исход A не гасит запись в пилюле, трее и страже О4."""
    begin_overlap(rig)
    mark = len(rig.trace)
    if a_outcome == "result":
        rig.result_for(A, A_TEXT)
    else:
        rig.event_for(A, "error", code="frame-too-large", message="x")
    assert rig.recording is True
    assert ("recording", False) not in rig.trace[mark:]
    assert rig.pill.calls[-1][0] in LISTENING_STATES
    assert rig.tray.state == TrayState.LISTENING
    rig.assert_hotkey_state(HotkeyState.RECORDING)
    assert rig.uids[B] not in rig.sent_ids("record.cancel")


def test_first_error_is_reported_after_second_recording(rig: OverlapRig) -> None:
    """T-269 (Q3): ошибку A не прячем навсегда — она видна после записи B."""
    begin_overlap(rig)
    rig.event_for(A, "error", code="frame-too-large", message="x")
    release = len(rig.pill.calls)
    rig.stop()
    rig.result_for(B, B_TEXT)
    shown = [
        c for c in rig.pill.calls[release:] if c[:2] == (PillState.ERROR, ERROR_RECOGNITION_FAILED)
    ]
    assert shown, "ошибка распознавания A так и не показана"
    assert rig.texts() == [B_TEXT]


# T-270 ---------------------------------------------------------------------------------


def test_overlap_keeps_text_out_of_logs_and_stats(
    rig: OverlapRig, caplog: pytest.LogCaptureFixture
) -> None:
    """T-270: текст обеих фраз не попадает ни в журнал, ни в статистику."""
    caplog.set_level(logging.DEBUG)
    begin_overlap(rig, release_b=True)
    rig.result_for(B, B_TEXT)
    rig.result_for(A, A_TEXT)
    assert rig.texts() == [A_TEXT, B_TEXT]
    for marker in ("ГЕЛИОТРОП", A_TEXT, B_TEXT):
        assert marker not in caplog.text
        assert all(marker not in str(v) for e in rig.stats.events for v in e.values())


# T-271 (Q1) ----------------------------------------------------------------------------


def test_first_paste_waits_until_second_recording_ends(rig: OverlapRig) -> None:
    """T-271 (Q1): A не вставляется, пока зажата клавиша B; после отпускания — сразу."""
    begin_overlap(rig)
    rig.result_for(A, A_TEXT)
    assert rig.pasted == [], "вставка A во время записи B (клавиша зажата → keys-held)"
    rig.stop()
    assert rig.texts() == [A_TEXT], "A ждёт результата B вместо вставки после отпускания"
    rig.result_for(B, B_TEXT)
    assert rig.texts() == [A_TEXT, B_TEXT]


# T-272 (Q6) ----------------------------------------------------------------------------


def test_press_during_first_paste_starts_second_after_paste(rig: OverlapRig) -> None:
    """T-272 (Q6): нажатие во время вставки A не теряется — B стартует после вставки."""
    rig.start()
    rig.stop()
    rig.during_paste = rig.start
    rig.result_for(A, A_TEXT)
    rig.during_paste = None
    assert rig.texts() == [A_TEXT]
    assert len(rig.uids) == 2, "нажатие во время вставки A потеряно"
    assert phase_of(rig) == DictationPhase.RECORDING and rig.recording
    rig.assert_hotkey_state(HotkeyState.RECORDING)


# T-273 (Q7) ----------------------------------------------------------------------------


def test_indicator_loss_stops_recording_but_keeps_recorded_phrase(rig: OverlapRig) -> None:
    """T-273 (Q7): потеря индикаторов О4 останавливает запись B; записанная A вставляется."""
    begin_overlap(rig)
    rig.core.on_indicators_lost()
    assert rig.sent_ids("record.cancel") == [rig.uids[B]]
    assert rig.recording
    rig.event_for(B, "cancelled")
    rig.result_for(A, A_TEXT)
    assert rig.texts() == [A_TEXT]


# Дополнения разработчика к US-2.7 (решения владельца 2026-10-15) ------------------------


def test_short_tap_during_recognition_cancels_only_its_recording(rig: OverlapRig) -> None:
    """Открытый риск PTT: касание < 0,3 с поверх распознавания не оставляет запись открытой.

    Запись B отменяется (без toggle и без «Отменено» поверх распознавания A), хоткей
    возвращается к A: Escape по-прежнему отменяет A, done после A — IDLE.
    """
    rig.start()
    rig.stop()
    rig.start()
    a_uid, b_uid = rig.uids
    rig.now += 0.1
    rig.fsm.release(rig.now)
    assert rig.sent_ids("record.cancel") == [b_uid]
    assert rig.recording
    rig.assert_hotkey_state(HotkeyState.PROCESSING)
    rig.event_for(B, "cancelled")
    assert rig.pill.calls[-1][0] == PillState.PROCESSING
    assert rig.tray.state == TrayState.PROCESSING
    assert phase_of(rig) == DictationPhase.PROCESSING
    rig.result_for(A, A_TEXT)
    assert rig.texts() == [A_TEXT]
    assert a_uid not in rig.sent_ids("record.cancel")
    rig.assert_hotkey_state(HotkeyState.IDLE)


def test_held_press_during_recognition_is_a_normal_phrase(rig: OverlapRig) -> None:
    """Удержание ≥ 0,3 с поверх распознавания — обычная фраза PTT, без отмены."""
    begin_overlap(rig)
    rig.now += 0.5
    rig.fsm.release(rig.now)
    rig.fire_tail()
    assert rig.sent_ids("recognize") == rig.uids
    assert "record.cancel" not in rig.commands()


def test_queued_phrase_failed_by_engine_shows_error_not_cancelled(rig: OverlapRig) -> None:
    """Решение 5: B, ждавшая очереди и не распознанная из-за ошибки A, — ошибка, не «Отменено»."""
    begin_overlap(rig, release_b=True)
    rig.event_for(A, "error", code="engine-failed")
    # Ошибка адресована A; B продолжает ждать.
    assert rig.active(PROCESSING_WATCHDOG_MS), "ошибка A сняла сторож B"
    rig.event_for(B, "error", code="engine-failed", request_type="recognize")
    assert (PillState.ERROR, ERROR_RECOGNITION_FAILED, None) in rig.pill.calls
    assert all(call[0] != PillState.CANCELLED for call in rig.pill.calls)
    assert [e["result"] for e in rig.stats.events if e["type"] == "dictation"] == []
    assert rig.pasted == []
    assert phase_of(rig) == DictationPhase.FINISHING
    rig.assert_hotkey_state(HotkeyState.IDLE)


def test_escape_on_recognized_waiting_phrase_cancels_locally(rig: OverlapRig) -> None:
    """B распознана и ждёт A: воркеру отменять нечего — record.cancel не шлём."""
    begin_overlap(rig, release_b=True)
    rig.result_for(B, B_TEXT)
    rig.fsm.escape(rig.now)
    assert "record.cancel" not in rig.commands()
    rig.result_for(A, A_TEXT)
    assert rig.texts() == [A_TEXT]
    results = [e["result"] for e in rig.stats.events if e["type"] == "dictation"]
    assert results == ["cancelled", "ok"]
    assert not rig.restart_worker.called


def test_third_press_is_logged_without_text(
    rig: OverlapRig, caplog: pytest.LogCaptureFixture
) -> None:
    """Решение 7: отказ третьему нажатию — в журнале, без текста фраз; хоткей у B."""
    caplog.set_level(logging.DEBUG)
    begin_overlap(rig, release_b=True)
    rig.result_for(B, B_TEXT)
    rig.start()
    assert len(rig.uids) == 2
    rig.assert_hotkey_state(HotkeyState.PROCESSING)
    assert any("нажатие проигнорировано" in r.getMessage() for r in caplog.records)
    rig.result_for(A, A_TEXT)
    assert rig.texts() == [A_TEXT, B_TEXT]
    assert "ГЕЛИОТРОП" not in caplog.text


def test_queue_limit_matches_worker_queue() -> None:
    """Очередь GUI = очередь воркера: одно задание выполняется и одно ждёт."""
    from astra_voice.core.dictation import MAX_PENDING_UTTERANCES

    assert MAX_PENDING_UTTERANCES == 2


def test_first_pastes_after_toggle_stop_of_second() -> None:
    """Решение 1 в режиме toggle: A вставляется после остановки записи B вторым нажатием."""
    toggle = OverlapRig(hotkey_mode=HotkeyMode.TOGGLE)
    toggle.start()
    toggle.stop()
    toggle.start()
    assert len(toggle.uids) == 2, OVERLAP_MISSING
    toggle.result_for(A, A_TEXT)
    assert toggle.pasted == []
    toggle.stop()
    assert toggle.texts() == [A_TEXT]
    toggle.result_for(B, B_TEXT)
    assert toggle.texts() == [A_TEXT, B_TEXT]
    toggle.assert_hotkey_state(HotkeyState.IDLE)


def test_pill_moves_between_phrases_without_hide(rig: OverlapRig) -> None:
    """T-277 (уровень оркестратора): processing → listening → processing без hide между ними.

    Пилюля не скрывается на стыке фраз, а индикатор записи включается заново только
    после того, как пилюля уже показывает «Слушаю» (страж О4 не видит потери индикации).
    """
    rig.start()
    rig.stop()
    mark = len(rig.trace)
    rig.start()
    rig.stop()
    rig.result_for(A, A_TEXT)
    rig.result_for(B, B_TEXT)
    window = rig.trace[mark:]
    assert ("hide",) not in window
    shown = [entry[1] for entry in window if entry[0] == "pill"]
    assert shown[:2] == [PillState.LISTENING, PillState.PROCESSING]
    assert shown[-1] == PillState.DONE
    on = window.index(("recording", True))
    assert ("pill", PillState.LISTENING, None, None) in window[:on]


@pytest.mark.parametrize("confirm", ["cancelled", "timeout"])
def test_ready_first_pastes_after_second_is_cancelled(rig: OverlapRig, confirm: str) -> None:
    """A распознана, пока пишется B; Escape отменяет B — A вставляется после отмены B."""
    begin_overlap(rig)
    rig.result_for(A, A_TEXT)
    assert rig.pasted == []
    rig.fsm.escape(rig.now)
    if confirm == "cancelled":
        rig.event_for(B, "cancelled")
    else:
        (timeout,) = rig.active(CANCEL_TIMEOUT_MS)
        timeout.fire()
    assert rig.texts() == [A_TEXT]
    assert [e["result"] for e in rig.stats.events if e["type"] == "dictation"] == [
        "cancelled",
        "ok",
    ]


# Код-ревью US-2.7 (46d3afd): регресс-тесты по пробам p1–p7 и nit маршрутизации ---------


@pytest.mark.parametrize("hotkey_mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
def test_press_within_release_tail_keeps_recorded_phrase(hotkey_mode: HotkeyMode) -> None:
    """MAJOR-1 (p1/p1b): нажатие в первые RELEASE_TAIL_MS после отпускания A (дребезг,
    быстрый повтор) не отменяет записанную A: A распознаётся и вставляется."""
    rig = OverlapRig(hotkey_mode=hotkey_mode)
    rig.start()
    rig.stop(tail=False)
    rig.assert_hotkey_state(HotkeyState.PROCESSING)
    rig.now += 0.01
    rig.start()
    assert "record.cancel" not in rig.commands(), "нажатие в хвосте отменило A"
    assert len(rig.uids) == 1
    rig.assert_hotkey_state(HotkeyState.PROCESSING)
    if hotkey_mode == HotkeyMode.PTT:
        rig.now += 0.03
        rig.fsm.release(rig.now)
    rig.fire_tail()
    assert rig.sent_ids("recognize") == [rig.uids[A]]
    rig.result_for(A, A_TEXT)
    assert rig.texts() == [A_TEXT]
    assert "record.cancel" not in rig.commands()
    rig.assert_hotkey_state(HotkeyState.IDLE)
    assert not rig.recording
    # Следующее нажатие — обычная новая фраза.
    rig.start()
    assert len(rig.uids) == 2


def test_deferred_first_error_does_not_resurface_on_later_phrase(rig: OverlapRig) -> None:
    """p2: ошибка A, отложенная на запись B, не всплывает на фразе C через минуту, если B
    закончилась без распознавания (короткая запись до смены микрофона)."""
    begin_overlap(rig)
    rig.event_for(A, "error", code="engine-failed")
    rig.core.on_worker_event(
        {
            "type": "audio.device.changed",
            "generation": rig.worker_generation,
            "utterance_id": rig.uids[B],
            "kind": "switched",
            "label": "USB",
            "audio_ms": 100,
        }
    )
    assert rig.pill.calls[-1][0] == PillState.LISTENING
    assert rig.recording
    rig.event_for(B, "cancelled")
    assert rig.pill.calls[-1][:2] == (PillState.ERROR, ERROR_MICROPHONE_CHANGED)
    fire_finishing_timers(rig)
    assert phase_of(rig) == DictationPhase.IDLE
    rig.now += 60
    mark = len(rig.pill.calls)
    rig.start()
    rig.stop()
    later = rig.pill.calls[mark:]
    assert (PillState.ERROR, ERROR_RECOGNITION_FAILED, None) not in later
    assert later[-1][0] == PillState.PROCESSING


def test_failing_ui_port_during_paste_queue_does_not_strand_phrase(
    rig: OverlapRig, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """p7: исключение порта UI при разборе отложенного нажатия не оставляет A во вставке:
    A доходит до исхода, очередь не застревает, фаза возвращается в IDLE."""
    caplog.set_level(logging.DEBUG)
    rig.start()
    rig.stop()

    def press() -> None:
        rig.now += 0.05
        rig.fsm.press(rig.now)

    original = rig.tray.set_state

    def failing(state: TrayState, tooltip: str | None = None) -> None:
        if state == TrayState.LISTENING:
            raise RuntimeError(A_TEXT)
        original(state, tooltip)

    rig.during_paste = press
    monkeypatch.setattr(rig.tray, "set_state", failing)
    rig.result_for(A, A_TEXT)
    rig.during_paste = None
    monkeypatch.setattr(rig.tray, "set_state", original)
    assert rig.texts() == [A_TEXT]
    assert [e["result"] for e in rig.stats.events if e["type"] == "dictation"] == ["ok"]
    assert rig.core.pending_utterances == 1, "A осталась в очереди после сбоя порта"
    # Сбой порта гасится на месте (О4): B пишется с включённым индикатором записи.
    assert any("не удалось обновить трей" in r.getMessage() for r in caplog.records)
    assert rig.recording, "B пишется без индикатора записи"
    assert "ГЕЛИОТРОП" not in caplog.text
    fire_finishing_timers(rig)
    rig.stop()
    rig.result_for(B, B_TEXT)
    assert rig.texts() == [A_TEXT, B_TEXT]
    fire_finishing_timers(rig)
    assert phase_of(rig) == DictationPhase.IDLE
    rig.idle.assert_called_once_with()


@pytest.mark.parametrize("hotkey_mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
def test_second_recording_failure_returns_hotkey_to_queue(hotkey_mode: HotkeyMode) -> None:
    """p4: B падает при записи (audio-failed), пока A распознаётся, — автомат клавиши
    возвращается к A, и следующее нажатие начинает новую фразу, а не теряется."""
    rig = OverlapRig(hotkey_mode=hotkey_mode)
    rig.start()
    rig.stop()
    rig.start()
    assert len(rig.uids) == 2, OVERLAP_MISSING
    rig.event_for(B, "error", code="audio-failed")
    rig.assert_hotkey_state(HotkeyState.PROCESSING)
    assert rig.recording
    assert rig.uids[A] not in rig.sent_ids("record.cancel")
    assert rig.core.cleanup_pending
    rig.event_for(B, "cancelled")
    rig.now += 1
    if hotkey_mode == HotkeyMode.PTT:
        rig.fsm.release(rig.now)
        rig.assert_hotkey_state(HotkeyState.PROCESSING)
    rig.start()
    assert len(rig.uids) == 3, "нажатие после сбоя B потеряно"
    rig.stop()
    rig.result_for(A, A_TEXT)
    rig.result_for(C, "третья")
    assert rig.texts() == [A_TEXT, "третья"]
    rig.assert_hotkey_state(HotkeyState.IDLE)


def test_press_during_first_paste_with_second_in_flight_starts_new_phrase(
    rig: OverlapRig,
) -> None:
    """p6: нажатие во время вставки A, пока B распознаётся, — не «третье»: вставленная A
    завершена до разбора очереди (решение 6), нажатие начинает фразу C."""
    begin_overlap(rig, release_b=True)

    def press() -> None:
        rig.now += 0.05
        rig.fsm.press(rig.now)

    rig.during_paste = press
    rig.result_for(A, A_TEXT)
    rig.during_paste = None
    assert rig.texts() == [A_TEXT]
    assert len(rig.uids) == 3, "нажатие во время вставки A отклонено как третье"
    assert rig.refusals == 0
    assert rig.core.pending_utterances == 2
    assert phase_of(rig) == DictationPhase.RECORDING and rig.recording
    rig.assert_hotkey_state(HotkeyState.RECORDING)
    rig.result_for(B, B_TEXT)
    assert rig.texts() == [A_TEXT], "B вставлена, пока пишется C"
    rig.stop()
    assert rig.texts() == [A_TEXT, B_TEXT]
    rig.result_for(C, "третья")
    assert rig.texts() == [A_TEXT, B_TEXT, "третья"]
    rig.assert_hotkey_state(HotkeyState.IDLE)


def test_press_during_busy_paste_with_second_in_flight_is_third(rig: OverlapRig) -> None:
    """p6, граница: A не вставлена (BUSY, ждёт повтора) — нажатие остаётся третьим."""
    begin_overlap(rig, release_b=True)
    rig.outcomes.append(PasteOutcomeKind.BUSY)

    def press() -> None:
        rig.now += 0.05
        rig.fsm.press(rig.now)

    rig.during_paste = press
    rig.result_for(A, A_TEXT)
    rig.during_paste = None
    assert len(rig.uids) == 2
    assert "record.cancel" not in rig.commands()
    rig.assert_hotkey_state(HotkeyState.PROCESSING)


@pytest.mark.parametrize("b_outcome", [PasteOutcomeKind.PASTED, PasteOutcomeKind.WINDOW_CHANGED])
def test_first_in_clipboard_then_second_delivered(
    rig: OverlapRig, b_outcome: PasteOutcomeKind
) -> None:
    """T-276 (уровень оркестратора): A ушла в буфер (окно сменилось), затем B.

    Каждая фраза сообщает свой исход: «в буфере» у A видно, хотя B ещё в работе
    (решение 3). Если и B ушла в буфер, буфер и «последний текст» — B: текст A
    остаётся только в буфере до вставки B (возврат снимка буфера — xvfb, T-276).
    """
    begin_overlap(rig, release_b=True)
    rig.outcomes.extend([PasteOutcomeKind.WINDOW_CHANGED, b_outcome])
    rig.result_for(A, A_TEXT)
    clipboard = (PillState.CLIPBOARD_ONLY, CLIPBOARD_WINDOW_CHANGED, None)
    assert rig.pill.calls[-1] == clipboard
    assert rig.core.last_text == A_TEXT
    rig.result_for(B, B_TEXT)
    assert rig.texts() == [A_TEXT, B_TEXT]
    assert rig.core.last_text == B_TEXT
    notices = [c for c in rig.pill.calls if c == clipboard]
    expected = 2 if b_outcome == PasteOutcomeKind.WINDOW_CHANGED else 1
    assert len(notices) == expected
    if b_outcome == PasteOutcomeKind.PASTED:
        assert rig.pill.calls[-1][0] == PillState.DONE
    assert [e["result"] for e in rig.stats.events if e["type"] == "dictation"] == ["ok", "ok"]


@pytest.mark.parametrize("b_state", ["recording", "processing"])
def test_reply_error_without_phrase_id_fails_no_phrase(rig: OverlapRig, b_state: str) -> None:
    """nit _route: ошибка ответа на запрос не по фразе (замер памяти после «Готово» A,
    загрузка модели) не назначается распознаваемой фразе и не обрывает пишущуюся."""
    begin_overlap(rig, release_b=b_state == "processing")
    for code, response in (("measure-failed", "measured"), ("timeout", "model.loaded")):
        rig.core.on_worker_event(
            {
                "type": "error",
                "generation": rig.worker_generation,
                "code": code,
                "response_type": response,
            }
        )
    assert all(call[0] != PillState.ERROR for call in rig.pill.calls)
    assert "record.cancel" not in rig.commands()
    assert rig.core.pending_utterances == 2
    if b_state == "recording":
        assert rig.recording
        rig.stop()
    rig.result_for(A, A_TEXT)
    rig.result_for(B, B_TEXT)
    assert rig.texts() == [A_TEXT, B_TEXT]


def test_restart_reply_error_still_ends_queue(rig: OverlapRig) -> None:
    """nit _route, граница: перезапуск воркера в ответе на чужой запрос — по-прежнему
    одна ошибка на всю очередь."""
    begin_overlap(rig, release_b=True)
    rig.core.on_worker_event(
        {
            "type": "error",
            "generation": rig.worker_generation,
            "code": "worker-crashed",
            "response_type": "measured",
        }
    )
    restarted = [c for c in rig.pill.calls if c[1] == ERROR_RECOGNITION_RESTARTED]
    assert len(restarted) == 1
    assert rig.core.pending_utterances == 0


# Финальный проход ревью (minor-1, minor-2) ----------------------------------------------


def test_press_while_second_cancel_awaits_worker_keeps_escape_for_first(
    rig: OverlapRig,
) -> None:
    """minor-1: потеря индикаторов отменила B; нажатие до подтверждения отмены B не
    сбрасывает автомат клавиши в IDLE — Esc по-прежнему отменяет распознаваемую A."""
    begin_overlap(rig)
    rig.core.on_indicators_lost()
    rig.assert_hotkey_state(HotkeyState.PROCESSING)
    rig.now += 0.2
    rig.fsm.press(rig.now)
    rig.assert_hotkey_state(HotkeyState.PROCESSING)
    assert rig.uids[A] not in rig.sent_ids("record.cancel")
    assert len(rig.uids) == 2, "нажатие во время отмены B начало новую запись"
    rig.event_for(B, "cancelled")
    rig.now += 0.1
    rig.fsm.escape(rig.now)
    assert rig.uids[A] in rig.sent_ids("record.cancel"), "Esc для A потерян"
    rig.event_for(A, "cancelled")
    fire_finishing_timers(rig)
    assert rig.texts() == []
    assert phase_of(rig) == DictationPhase.IDLE
    rig.assert_hotkey_state(HotkeyState.IDLE)


def failing_ui_ports(rig: OverlapRig, *, pill: bool = True) -> tuple[Any, Any]:
    """Порт трея (и пилюли) бросает с текстом речи; возвращает исходные методы."""
    show_state, set_state = rig.core._pill.show_state, rig.core._tray.set_state

    def failing_pill(state: PillState, *args: object, **kwargs: object) -> None:
        raise RuntimeError(B_TEXT)

    def failing_tray(state: TrayState, tooltip: str | None = None) -> None:
        raise RuntimeError(B_TEXT)

    if pill:
        rig.core._pill.show_state = failing_pill  # type: ignore[method-assign]
    rig.core._tray.set_state = failing_tray  # type: ignore[method-assign]
    return show_state, set_state


def restore_ui_ports(rig: OverlapRig, ports: tuple[Any, Any]) -> None:
    rig.core._pill.show_state, rig.core._tray.set_state = ports  # type: ignore[method-assign]


def test_failing_ui_ports_on_single_phrase_cancel_recording(
    rig: OverlapRig, caplog: pytest.LogCaptureFixture
) -> None:
    """N1 (О4): пилюля и трей бросают после record.start — запись втихую не идёт:
    фраза отменяется, страж активен до ACK, текст исключения не в журнале."""
    caplog.set_level(logging.DEBUG)
    ports = failing_ui_ports(rig)
    rig.start()
    restore_ui_ports(rig, ports)
    assert rig.sent_ids("record.cancel") == [rig.uids[A]]
    assert rig.recording, "страж остаётся активным до ACK"
    rig.event_for(A, "cancelled")
    fire_finishing_timers(rig)
    assert rig.texts() == []
    assert phase_of(rig) == DictationPhase.IDLE
    assert "ГЕЛИОТРОП" not in caplog.text


def test_icon_only_mode_with_failing_tray_cancels_recording(rig: OverlapRig) -> None:
    """N1 (О4): режим «только значок» (пилюля не видна) и трей не обновился —
    значок зарегистрирован, но не показывает запись; фраза отменяется."""
    rig.pill.visible = False
    ports = failing_ui_ports(rig, pill=False)
    rig.start()
    restore_ui_ports(rig, ports)
    assert rig.sent_ids("record.cancel") == [rig.uids[A]]
    assert rig.recording


def test_failing_tray_with_visible_pill_keeps_recording(rig: OverlapRig) -> None:
    """N1 (О4): трей не обновился, но пилюля «Слушаю» видна — запись идёт."""
    ports = failing_ui_ports(rig, pill=False)
    rig.start()
    restore_ui_ports(rig, ports)
    assert "record.cancel" not in rig.commands()
    assert phase_of(rig) == DictationPhase.RECORDING
    assert rig.recording
    assert rig.pill.calls[-1][0] == PillState.LISTENING


def test_failing_ui_ports_when_second_starts_cancel_only_second(rig: OverlapRig) -> None:
    """minor-2 + N1 (О4): порты пилюли и трея бросают при старте B после record.start —
    B не пишется втихую: отменяется только она (путь Q7), распознаваемая A вставляется."""
    rig.start()
    rig.stop()
    ports = failing_ui_ports(rig)
    rig.start()
    restore_ui_ports(rig, ports)
    assert len(rig.uids) == 2, OVERLAP_MISSING
    assert rig.sent_ids("record.cancel") == [rig.uids[B]]
    assert rig.recording, "страж B остаётся активным до ACK"
    rig.event_for(B, "cancelled")
    rig.result_for(A, A_TEXT)
    assert rig.texts() == [A_TEXT]


@pytest.mark.parametrize("timeout_first", [False, True])
def test_global_error_never_selects_an_utterance(rig: OverlapRig, timeout_first: bool) -> None:
    """ИБ-30: ошибка без id не угадывает A и не завершает запись B."""
    begin_overlap(rig)
    if timeout_first:
        rig.event_for(A, "error", code="timeout")
        assert rig.sent_ids("record.cancel") == [rig.uids[A]]
    before = list(rig.trace)
    rig.core.on_worker_event({"type": "error", "generation": 1, "code": "engine-failed"})
    assert rig.trace == before
    assert rig.core.phase is DictationPhase.RECORDING
    assert rig.recording
    rig.assert_hotkey_state(HotkeyState.RECORDING)
    assert rig.uids[B] not in rig.sent_ids("record.cancel")


class WorkerWireRig(OverlapRig):
    """Real WorkerState, deterministic jobs and separately delivered capture ACKs.

    Capture models the physical ownership boundary; actual AudioCapture threading
    and close failure behavior are covered by the worker tests.
    """

    def __init__(self) -> None:
        from concurrent.futures import Future
        from types import SimpleNamespace

        from test_worker_state import load_message

        from astra_voice.worker.state import WorkerState
        from fakes import FakeCancelToken, FakeEngine

        super().__init__()
        self.ack_stop = False
        self.wire: list[dict[str, Any]] = []
        self.jobs: list[Any] = []
        self.capture_uid: str | None = None
        self.capture_open = False
        self.engine = FakeEngine(text=B_TEXT)

        def submit(call: Any, *args: Any) -> Future[Any]:
            future: Future[Any] = Future()
            self.jobs.append(lambda: future.set_result(call(*args)))
            return future

        def start(uid: str, device: str | None, **kwargs: Any) -> None:
            assert not self.capture_open
            self.capture_uid, self.capture_open = uid, True

        def close(uid: str | None = None) -> None:
            if self.capture_open and (uid is None or uid == self.capture_uid):
                self.capture_open = False
                self.wire.append({"type": "record.stopped", "utterance_id": self.capture_uid})

        capture = SimpleNamespace(
            start=start,
            request_stop=lambda **kwargs: None,
            stop=close,
            wait_stopped=close,
            close=close,
        )
        self.worker = WorkerState(
            engine_factory=lambda layout: self.engine,
            cancel_factory=FakeCancelToken,
            audio_factory=list,
            executor=cast(Any, SimpleNamespace(submit=submit)),
            on_event=self.wire.append,
            capture=cast(Any, capture),
        )
        assert self.worker.handle(load_message())[0]["type"] == "model.loaded"
        self.during_send = self.to_worker

    def to_worker(self, message: dict[str, Any]) -> None:
        for response in self.worker.handle(message):
            if response["type"] == "error":
                response = {
                    **response,
                    "utterance_id": message.get("utterance_id"),
                    "request_type": message["type"],
                }
            self.wire.append(response)

    def deliver(self, kind: str) -> dict[str, Any]:
        matches = [i for i, event in enumerate(self.wire) if event["type"] == kind]
        assert matches, f"worker did not deliver {kind}: {[e['type'] for e in self.wire]}"
        index = matches[0]
        event = self.wire.pop(index)
        self.core.on_worker_event({**event, "generation": self.worker_generation})
        return event

    def run_job(self) -> None:
        self.jobs.pop(0)()

    def finish_b(self) -> None:
        from array import array

        uid = self.uids[-1]
        before = len(self.worker.buffers[uid])
        assert self.worker.on_samples(uid, array("f", [0.4] * 8000))
        assert len(self.worker.buffers[uid]) == before + 8000
        assert self.capture_open and self.recording
        self.stop()
        self.deliver("record.stopped")
        self.run_job()
        self.deliver("result")
        assert self.texts()[-1] == B_TEXT
        assert "audio.close" not in self.commands()
        self.restart_worker.assert_not_called()
        self.worker.close()


@pytest.mark.parametrize("boundary", ["cold-stop", "limit", "device-change"])
@pytest.mark.parametrize("ack", ["record.stopped", "bad-state"])
def test_worker_late_close_after_result_never_interrupts_next_capture(
    boundary: str,
    ack: str,
) -> None:
    """MAJOR-1: real unknown cancel replies bad-state, never synthetic cancelled."""
    from array import array

    rig = WorkerWireRig()
    rig.start()
    if boundary == "cold-stop":
        # No PCM and no record.started: opening was cancelled before capture began.
        rig.stop()
    else:
        if boundary == "limit":
            rig.worker.feed_audio(rig.uids[A], [0.4] * (120 * 16000))
            rig.deliver("record.limit")
        else:
            rig.worker.on_samples(rig.uids[A], array("f", [0.4] * 16000))
            rig.worker.on_device_change(rig.uids[A], "switched", "USB")
            rig.deliver("audio.device.changed")
    rig.run_job()
    rig.deliver("result")
    if boundary != "cold-stop":
        # Real capture eventually closes after request_stop, even without a GUI cancel.
        assert rig.worker._capture is not None
        rig.worker._capture.stop()
    assert any(e.get("code") == "bad-state" for e in rig.wire)
    assert not any(e["type"] == "cancelled" for e in rig.wire)
    if ack == "record.stopped":
        rig.deliver("record.stopped")
    else:
        reply = rig.deliver("error")
        assert reply["code"] == "bad-state" and reply["request_type"] == "record.cancel"
    rig.now += 0.5
    # Auto-stop can leave the physical hotkey down; release it before the next press.
    rig.fsm.release(rig.now)
    rig.start()
    assert len(rig.uids) == 2
    if ack == "record.stopped":
        rig.deliver("error")
    else:
        rig.deliver("record.stopped")
    for timer in list(rig.timers):
        if not timer.fired and not timer.cancelled and timer.delay != PROCESSING_WATCHDOG_MS:
            timer.fire()
    rig.finish_b()


@pytest.mark.parametrize("code", ["audio-silent", "audio-busy", "audio-failed"])
def test_worker_capture_error_bad_state_allows_next_press_after_half_second(code: str) -> None:
    rig = WorkerWireRig()
    rig.start()
    rig.worker.on_error(rig.uids[A], code, "capture failed")
    rig.deliver("error")
    reply = rig.deliver("error")
    assert reply["code"] == "bad-state" and reply["request_type"] == "record.cancel"
    assert not any(e["type"] == "cancelled" for e in rig.wire)
    rig.now += 0.5
    rig.fsm.release(rig.now)
    rig.start()
    assert len(rig.uids) == 2
    rig.deliver("record.stopped")  # delayed A boundary while B physically records
    for timer in list(rig.timers):
        if not timer.fired and not timer.cancelled:
            timer.fire()
    rig.finish_b()


@pytest.mark.parametrize("test_mode", [False, True])
def test_cancel_guard_stays_active_until_physical_capture_ack(test_mode: bool) -> None:
    rig = OverlapRig()
    if test_mode:
        rig.core.start_test("", lambda update: None)
        rig.event_for(A, "error", code="audio-failed")
    else:
        rig.start()
        rig.fsm.escape(rig.now)
    assert rig.recording and rig.tray.state == TrayState.LISTENING
    if not test_mode:
        assert rig.pill.calls[-1][0] == PillState.LISTENING
    rig.event_for(A, "record.stopped")
    assert not rig.recording and not rig.core.cleanup_pending
    assert "audio.close" not in rig.commands()


def test_capture_ack_does_not_acknowledge_active_recognition_cancel() -> None:
    rig = OverlapRig()
    rig.ack_stop = False
    rig.start()
    rig.stop()
    rig.fsm.escape(rig.now)
    rig.event_for(A, "record.stopped")
    assert not rig.recording
    rig.start()
    assert len(rig.uids) == 1
    assert rig.core.cleanup_pending
    rig.event_for(A, "cancelled")
    assert not rig.core.cleanup_pending
    rig.start()
    assert len(rig.uids) == 2


def test_cancel_timeout_with_failing_finish_ui_keeps_restart_armed() -> None:
    from astra_voice.core.dictation import CANCEL_RESTART_MS

    rig = OverlapRig()
    rig.start()
    rig.fsm.escape(rig.now)
    ports = failing_ui_ports(rig)
    rig.timer(CANCEL_TIMEOUT_MS).fire()
    assert rig.commands()[-1] == "audio.close"
    assert rig.core.cleanup_pending and rig.recording
    rig.timer(CANCEL_RESTART_MS).fire()
    rig.restart_worker.assert_called_once_with()
    assert not rig.core.cleanup_pending and not rig.recording
    restore_ui_ports(rig, ports)


@pytest.mark.parametrize("outcome", [PasteOutcomeKind.PASTED, PasteOutcomeKind.WINDOW_CHANGED])
@pytest.mark.parametrize("target_kind", ["same", "other-tab", "other-window"])
def test_pasted_first_preserves_each_queued_target_snapshot(
    outcome: PasteOutcomeKind,
    target_kind: str,
) -> None:
    from astra_voice.platform.paste import PasteTarget

    rig = OverlapRig()
    original = PasteTarget(42, b"initial")
    queued = {
        "same": original,
        "other-tab": PasteTarget(42, b"different-tab"),
        "other-window": PasteTarget(43, b"initial"),
    }[target_kind]
    rig.window = original
    rig.start()
    rig.stop()
    rig.window = queued
    rig.start()
    rig.stop()
    rig.outcomes.append(outcome)
    rig.result_for(A, A_TEXT)
    rig.result_for(B, B_TEXT)
    assert rig.pasted[1][1] is queued


def test_own_kate_title_change_sends_second_phrase_to_clipboard(
    caplog: pytest.LogCaptureFixture,
) -> None:
    import hashlib

    from astra_voice.platform.paste import PasteMode, PasteOutcome, PasteTarget

    rig = OverlapRig()
    title = "file.txt — Kate"
    updated_title = "file.txt * — Kate"
    delivered: list[str] = []

    def snapshot(target: PasteTarget) -> PasteTarget:
        return PasteTarget(target.window, hashlib.sha256(title.encode()).digest())

    def checked_paste(text: str, target: Any, mode: PasteMode) -> PasteOutcome:
        nonlocal title
        assert isinstance(target, PasteTarget)
        # Paste boundary checks the current title exactly as PasteFlow does.
        if target.title_digest != snapshot(target).title_digest:
            rig.outcomes.append(PasteOutcomeKind.WINDOW_CHANGED)
        else:
            delivered.append(text)
            title = updated_title
        return rig.paste(text, target, mode)

    rig.core._paste = checked_paste
    rig.window = snapshot(PasteTarget(42, None))
    begin_overlap(rig, release_b=True)
    rig.result_for(A, A_TEXT)
    rig.result_for(B, B_TEXT)
    assert delivered == [A_TEXT]
    assert rig.pill.calls[-1][:2] == (PillState.CLIPBOARD_ONLY, CLIPBOARD_WINDOW_CHANGED)
    assert rig.stats.events[-1]["result"] == "ok"
    assert "file.txt" not in caplog.text
    assert hashlib.sha256(updated_title.encode()).hexdigest() not in caplog.text


@pytest.mark.parametrize("mode", [HotkeyMode.PTT, HotkeyMode.TOGGLE])
def test_capture_notice_callback_start_preserves_new_capture_guard(
    mode: HotkeyMode,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from test_t2_overlap_replay import CaptureReplay

    replay = CaptureReplay(mode, monkeypatch)
    first = replay.start()
    assert first is not None
    replay.event("record.started", first)
    replay.advance(0.5)
    replay.event("audio.device.changed", first, kind="switched", audio_ms=1000)
    replay.stop()
    original = replay.pill.show_state
    second: list[str | None] = []

    def show_and_start(state: PillState, **fields: Any) -> None:
        original(state, **fields)
        if state is PillState.ERROR and not second:
            second.append(replay.start())

    monkeypatch.setattr(replay.pill, "show_state", show_and_start)
    replay.physical_stop(first, acknowledge=True)
    assert len(second) == 1 and second[0] is not None
    assert replay.worker.captures == {second[0]}
    replay.assert_o4()
    assert replay.rig.commands().count("record.start") == 2
    assert "audio.close" not in replay.rig.commands()


# T-283 (Fly 1.8.4, журнал 20.10) -------------------------------------------------------
# Нажатие B во время вставки A: решение 1 проверяется только до входа во вставку, а
# PasteFlow ждёт отпускания клавиш 300 мс и сдаётся с ``keys-held`` (или захват
# клавиатуры отказан — ``grab-refused``). XTest в этих ветках не отправлялся, поэтому A
# не завершается, а ждёт отпускания B, как в решении 1. ``not-fetched`` и
# ``xtest-refused`` — Ctrl+V мог уйти: повтора нет, A остаётся в буфере.

UNSENT_PASTE_REASONS = ("keys-held", "grab-refused")
SENT_PASTE_REASONS = ("not-fetched", "xtest-refused")


def press_during_failed_first_paste(rig: OverlapRig, reason: str) -> None:
    """A распознана; во время её вставки нажата B, вставка A не подтверждена (reason)."""
    rig.start()
    rig.stop()
    rig.during_paste = rig.start
    rig.outcomes.append(PasteOutcomeKind.WINDOW_CHANGED)
    rig.paste_reason = reason
    rig.result_for(A, A_TEXT)
    rig.during_paste = None
    rig.paste_reason = ""
    assert len(rig.uids) == 2, "нажатие во время вставки A потеряно"
    assert phase_of(rig) == DictationPhase.RECORDING and rig.recording


def dictation_results(rig: OverlapRig) -> list[str]:
    return [str(e["result"]) for e in rig.stats.events if e["type"] == "dictation"]


def clipboard_notices(rig: OverlapRig) -> int:
    return sum(1 for call in rig.pill.calls if call[0] == PillState.CLIPBOARD_ONLY)


@pytest.mark.parametrize("reason", UNSENT_PASTE_REASONS)
def test_unsent_first_paste_waits_for_second_release_and_retries(
    rig: OverlapRig, reason: str
) -> None:
    """T-283: XTest A не уходил, пишется B → A ждёт отпускания B и вставляется один раз."""
    press_during_failed_first_paste(rig, reason)
    assert rig.texts() == [A_TEXT]
    assert dictation_results(rig) == [], "A завершена исходом неотправленной вставки"
    assert clipboard_notices(rig) == 0, "A показана «в буфере», хотя ждёт отпускания B"
    assert rig.pill.calls[-1][0] in LISTENING_STATES
    rig.assert_hotkey_state(HotkeyState.RECORDING)
    rig.stop()
    assert rig.texts() == [A_TEXT, A_TEXT], "A не вставлена повторно после отпускания B"
    assert rig.core.last_text == A_TEXT
    assert dictation_results(rig) == ["ok"]
    rig.result_for(B, B_TEXT)
    assert rig.texts() == [A_TEXT, A_TEXT, B_TEXT]
    assert dictation_results(rig) == ["ok", "ok"]
    assert clipboard_notices(rig) == 0
    assert rig.core.last_text == B_TEXT
    assert rig.pill.calls[-1][0] == PillState.DONE
    rig.assert_hotkey_state(HotkeyState.IDLE)


@pytest.mark.parametrize("reason", UNSENT_PASTE_REASONS)
def test_unsent_first_paste_retries_after_second_is_cancelled(rig: OverlapRig, reason: str) -> None:
    """T-283: ждущая A не застревает, если B отменили Esc: A вставляется, B — нет."""
    press_during_failed_first_paste(rig, reason)
    rig.fsm.escape(rig.now)
    assert rig.sent_ids("record.cancel") == [rig.uids[B]]
    rig.event_for(B, "cancelled")
    assert rig.texts() == [A_TEXT, A_TEXT]
    assert sorted(dictation_results(rig)) == ["cancelled", "ok"]
    assert clipboard_notices(rig) == 0


@pytest.mark.parametrize("reason", SENT_PASTE_REASONS)
def test_possibly_sent_first_paste_is_final_during_second_recording(
    rig: OverlapRig, reason: str
) -> None:
    """T-283, контроль: Ctrl+V мог уйти (not-fetched / xtest-refused) — повтора нет."""
    press_during_failed_first_paste(rig, reason)
    assert dictation_results(rig) == ["ok"]
    rig.stop()
    rig.result_for(B, B_TEXT)
    assert rig.texts() == [A_TEXT, B_TEXT], "A вставлена повторно, хотя Ctrl+V мог уйти"
    assert dictation_results(rig) == ["ok", "ok"]


def test_keys_held_without_second_recording_stays_final(rig: OverlapRig) -> None:
    """T-283, контроль: ``keys-held`` без пишущейся фразы — исход окончательный (буфер)."""
    rig.start()
    rig.stop()
    rig.outcomes.append(PasteOutcomeKind.WINDOW_CHANGED)
    rig.paste_reason = "keys-held"
    rig.result_for(A, A_TEXT)
    rig.paste_reason = ""
    assert rig.texts() == [A_TEXT]
    assert rig.pill.calls[-1] == (PillState.CLIPBOARD_ONLY, CLIPBOARD_WINDOW_CHANGED, None)
    assert dictation_results(rig) == ["ok"]
    assert rig.active(BUSY_RETRY_MS) == []
