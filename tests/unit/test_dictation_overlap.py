"""US-2.7: запись новой фразы, пока предыдущая распознаётся (тесты до кода, T-200…T-219).

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
from typing import Any

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
        self.core.on_worker_event(
            {
                "type": kind,
                "generation": self.worker_generation,
                "utterance_id": self.uids[index],
                **fields,
            }
        )

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


# T-200 ---------------------------------------------------------------------------------


def test_press_during_recognition_starts_second_recording(rig: OverlapRig) -> None:
    """T-200: B принята: свой id, A не отменена, индикация записи включена."""
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


# T-201 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("arrival", ["a-first", "b-first"])
def test_pastes_follow_press_order(rig: OverlapRig, arrival: str) -> None:
    """T-201: вставки по порядку нажатий, даже если результат B пришёл раньше A."""
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
    """T-201: результат B, пришедший во вложенном цикле вставки A, вставляется после A."""
    begin_overlap(rig, release_b=True)
    rig.during_paste = lambda: rig.result_for(B, B_TEXT)
    rig.result_for(A, A_TEXT)
    rig.during_paste = None
    assert rig.texts() == [A_TEXT, B_TEXT]


def test_busy_retry_of_first_keeps_order(rig: OverlapRig) -> None:
    """T-201: повтор BUSY фразы A не пропускает B вперёд."""
    begin_overlap(rig, release_b=True)
    rig.outcomes.append(PasteOutcomeKind.BUSY)
    rig.result_for(A, A_TEXT)
    rig.result_for(B, B_TEXT)
    assert rig.texts() == [A_TEXT], "B вставлена, пока A ждёт повтора BUSY"
    (retry,) = rig.active(BUSY_RETRY_MS)
    retry.fire()
    assert rig.texts() == [A_TEXT, A_TEXT, B_TEXT]


# T-202 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("b_state", ["recording", "processing"])
def test_escape_cancels_only_newest_phrase(rig: OverlapRig, b_state: str) -> None:
    """T-202: Esc отменяет B; A распознаётся и вставляется."""
    begin_overlap(rig, release_b=b_state == "processing")
    a_uid, b_uid = rig.uids
    rig.fsm.escape(rig.now)
    assert rig.sent_ids("record.cancel") == [b_uid]
    assert not rig.recording
    rig.event_for(B, "cancelled")
    rig.result_for(A, A_TEXT)
    assert rig.texts() == [A_TEXT]
    assert a_uid not in rig.sent_ids("record.cancel")
    results = sorted(str(e["result"]) for e in rig.stats.events if e["type"] == "dictation")
    assert results == ["cancelled", "ok"]


def test_escape_after_first_delivered_cancels_second(rig: OverlapRig) -> None:
    """T-202: A вставлена, B распознаётся — хоткей не сброшен исходом A, Esc отменяет B."""
    begin_overlap(rig, release_b=True)
    rig.result_for(A, A_TEXT)
    assert rig.fsm.state is not HotkeyState.IDLE, "исход A сбросил хоткей фразы B"
    rig.fsm.escape(rig.now)
    assert rig.sent_ids("record.cancel") == [rig.uids[B]]


# T-203 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("a_outcome", ["error", "empty", "watchdog"])
@pytest.mark.parametrize("b_state", ["recording", "processing"])
def test_first_failure_does_not_lose_second(rig: OverlapRig, a_outcome: str, b_state: str) -> None:
    """T-203: ошибка/пусто/сторож A не отменяет B; B вставляется."""
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


# T-204 ---------------------------------------------------------------------------------


def test_each_phrase_pastes_into_its_own_start_window(rig: OverlapRig) -> None:
    """T-204: окно-цель — окно фокуса при старте КАЖДОЙ фразы (Q2)."""
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


# T-205 ---------------------------------------------------------------------------------


def test_third_press_is_ignored_without_cancelling_queue(rig: OverlapRig) -> None:
    """T-205: не больше двух незавершённых фраз; третье нажатие не трогает A и B."""
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


# T-206 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("b_state", ["recording", "processing"])
def test_worker_restart_ends_whole_queue_once(rig: OverlapRig, b_state: str) -> None:
    """T-206: смена поколения (крах/смена модели) — одна ошибка на обе фразы, без вставок."""
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
    """T-206: выход при непустой очереди — отмена обеих фраз, индикаторы погашены."""
    begin_overlap(rig)
    a_uid, b_uid = rig.uids
    rig.core.shutdown()
    assert {a_uid, b_uid} <= set(rig.sent_ids("record.cancel"))
    assert not rig.recording and rig.pill.hidden
    rig.result_for(A, A_TEXT)
    rig.result_for(B, B_TEXT)
    assert rig.pasted == []


# T-207 ---------------------------------------------------------------------------------


def test_phase_and_on_idle_wait_for_whole_queue(rig: OverlapRig) -> None:
    """T-207: смена модели/выгрузка ждут конца очереди — on_idle один раз в конце."""
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
    """T-207 guard: проверка микрофона/мастер не вклиниваются в распознавание."""
    rig.start()
    rig.stop()
    updates: list[MicrophoneTestUpdate] = []
    assert rig.core.start_test("fake-device", updates.append) is False
    assert updates == [MicrophoneTestUpdate("error", message=TEST_BUSY)]
    assert rig.commands().count("record.start") == 1


def test_microphone_test_refused_during_overlap(rig: OverlapRig) -> None:
    """T-207: и при двух фразах в полёте проверка микрофона отказывает."""
    begin_overlap(rig, release_b=True)
    rig.result_for(A, A_TEXT)
    updates: list[MicrophoneTestUpdate] = []
    assert rig.core.start_test("fake-device", updates.append) is False
    assert updates == [MicrophoneTestUpdate("error", message=TEST_BUSY)]


# T-208 ---------------------------------------------------------------------------------


def test_command_press_during_text_recognition_is_ignored(rig: OverlapRig) -> None:
    """T-208 guard: «Команда» не перекрывает распознавание текста (Q4)."""
    rig.start()
    rig.stop()
    rig.core.on_hotkey_state(HotkeyState.RECORDING, "press", mode="command")
    assert rig.commands().count("record.start") == 1
    assert rig.core.mode == "text"


def test_text_press_during_command_recognition_is_ignored(rig: OverlapRig) -> None:
    """T-208 guard: текст не перекрывает распознавание команды (Q4)."""

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
    """T-208: при A в распознавании и B в записи «Команда» не начинает третью запись."""
    begin_overlap(rig)
    rig.core.on_hotkey_state(HotkeyState.RECORDING, "press", mode="command")
    assert len(rig.uids) == 2
    assert rig.core.mode == "text"


# T-209 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("a_outcome", ["result", "error"])
def test_recording_indication_survives_first_outcome(rig: OverlapRig, a_outcome: str) -> None:
    """T-209: пока пишется B, исход A не гасит запись в пилюле, трее и страже О4."""
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
    """T-209 (Q3): ошибку A не прячем навсегда — она видна после записи B."""
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


# T-210 ---------------------------------------------------------------------------------


def test_overlap_keeps_text_out_of_logs_and_stats(
    rig: OverlapRig, caplog: pytest.LogCaptureFixture
) -> None:
    """T-210: текст обеих фраз не попадает ни в журнал, ни в статистику."""
    caplog.set_level(logging.DEBUG)
    begin_overlap(rig, release_b=True)
    rig.result_for(B, B_TEXT)
    rig.result_for(A, A_TEXT)
    assert rig.texts() == [A_TEXT, B_TEXT]
    for marker in ("ГЕЛИОТРОП", A_TEXT, B_TEXT):
        assert marker not in caplog.text
        assert all(marker not in str(v) for e in rig.stats.events for v in e.values())


# T-211 (Q1) ----------------------------------------------------------------------------


def test_first_paste_waits_until_second_recording_ends(rig: OverlapRig) -> None:
    """T-211 (Q1): A не вставляется, пока зажата клавиша B; после отпускания — сразу."""
    begin_overlap(rig)
    rig.result_for(A, A_TEXT)
    assert rig.pasted == [], "вставка A во время записи B (клавиша зажата → keys-held)"
    rig.stop()
    assert rig.texts() == [A_TEXT], "A ждёт результата B вместо вставки после отпускания"
    rig.result_for(B, B_TEXT)
    assert rig.texts() == [A_TEXT, B_TEXT]


# T-212 (Q6) ----------------------------------------------------------------------------


def test_press_during_first_paste_starts_second_after_paste(rig: OverlapRig) -> None:
    """T-212 (Q6): нажатие во время вставки A не теряется — B стартует после вставки."""
    rig.start()
    rig.stop()
    rig.during_paste = rig.start
    rig.result_for(A, A_TEXT)
    rig.during_paste = None
    assert rig.texts() == [A_TEXT]
    assert len(rig.uids) == 2, "нажатие во время вставки A потеряно"
    assert phase_of(rig) == DictationPhase.RECORDING and rig.recording
    rig.assert_hotkey_state(HotkeyState.RECORDING)


# T-213 (Q7) ----------------------------------------------------------------------------


def test_indicator_loss_stops_recording_but_keeps_recorded_phrase(rig: OverlapRig) -> None:
    """T-213 (Q7): потеря индикаторов О4 останавливает запись B; записанная A вставляется."""
    begin_overlap(rig)
    rig.core.on_indicators_lost()
    assert rig.sent_ids("record.cancel") == [rig.uids[B]]
    assert not rig.recording
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
    assert not rig.recording
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
    rig.core.on_worker_event(
        {"type": "error", "generation": rig.worker_generation, "code": "engine-failed"}
    )
    # Ошибка без id относится к распознаваемой A; B продолжает ждать.
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
    """T-217 (уровень оркестратора): processing → listening → processing без hide между ними.

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
