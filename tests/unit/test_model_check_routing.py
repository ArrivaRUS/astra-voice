"""Ручная проверка каталога не запускает проверку программы и уважает свой гейт."""

from __future__ import annotations

import threading
from pathlib import Path
from unittest.mock import Mock

import pytest

# astra_voice.net.http импортирует requests, а в окружении движка (job engine)
# его нет: без пропуска падает сбор всего прогона. В unit-окружении requests
# обязателен — это проверяет scripts/ci_require_imports.py.
pytest.importorskip("requests")

# ruff: noqa: E402 — импорт проверяемых модулей после importorskip.
from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import HttpClient
from astra_voice.net.update_cache import UpdateCache
from astra_voice.updates.checker import UpdateChecker
from astra_voice.updates.model_updates import ModelCheckSummary, ModelUpdateChecker
from astra_voice.updates.state import StateStore

pytestmark = pytest.mark.unit


def drain_worker(checker: UpdateChecker) -> None:
    """Два барьера завершают и обработку заявки, и следующий круг планировщика."""
    for _ in range(2):
        done = threading.Event()
        checker._request(action=done.set)
        assert done.wait(5), "рабочий поток не обработал очередь"


def make_checker(
    tmp_path: Path, settings: Settings, policy: Policy
) -> tuple[UpdateChecker, Mock, Mock, StateStore]:
    gate = NetworkGate(settings, policy)
    models = Mock(spec=ModelUpdateChecker)
    models.run_once.return_value = ModelCheckSummary("checked")
    store = StateStore(tmp_path / "update-state.json")
    checker = UpdateChecker(
        gate,
        HttpClient(gate, user_agent="astra-voice/test"),
        cache=UpdateCache(tmp_path / "update-cache.json"),
        store=store,
        model_checker=models,
    )
    # Наблюдаем границу проверки программы: даже запрещённая попытка не должна
    # проходить этим путём при действии восстановления каталога.
    app_check = Mock(wraps=checker._check)
    checker._check = app_check  # type: ignore[method-assign]
    return checker, models.run_once, app_check, store


@pytest.mark.parametrize("app_locked", [False, True], ids=["app-allowed", "app-denied"])
def test_models_only_request_runs_in_worker_without_app_check(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app_locked: bool
) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    settings = Settings(check_model_updates=True, check_app_updates=not app_locked)
    policy = Policy(locked_keys=frozenset({"check_app_updates"}) if app_locked else frozenset())
    checker, run_models, app_check, store = make_checker(tmp_path, settings, policy)
    threads: list[int] = []

    def record(*_args: object, **_kwargs: object) -> ModelCheckSummary:
        threads.append(threading.get_ident())
        return ModelCheckSummary("checked")

    run_models.side_effect = record
    try:
        checker.start(scheduled=False)
        drain_worker(checker)
        before = checker.status
        checker.check_models_now()
        drain_worker(checker)
        run_models.assert_called_once_with(checker._stop, manual=True)
        assert len(threads) == 1 and threads[0] != threading.get_ident()
        assert checker._thread is not None and threads == [checker._thread.ident]
        app_check.assert_not_called()
        assert checker.status == before
        assert store.get("app").last_attempt_at is None
        assert store.get("app").last_success_at is None
        assert checker._scheduled_enabled is False
        # Явное повторное действие не теряется из-за суточного расписания.
        checker.check_models_now()
        drain_worker(checker)
        assert run_models.call_count == 2
        app_check.assert_not_called()
    finally:
        checker.stop()


@pytest.mark.parametrize("refusal", ["user-off", "policy", "offline"])
def test_models_only_request_with_closed_model_gate_checks_neither_source(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, refusal: str
) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    settings = Settings(check_app_updates=True, check_model_updates=refusal != "user-off")
    settings.offline = refusal == "offline"
    policy = Policy(values={"check_model_updates": False}) if refusal == "policy" else Policy()
    checker, run_models, app_check, store = make_checker(tmp_path, settings, policy)
    try:
        checker.start(scheduled=False)
        drain_worker(checker)
        checker.check_models_now()
        drain_worker(checker)
        run_models.assert_not_called()
        app_check.assert_not_called()
        assert store.get("app").last_attempt_at is None
    finally:
        checker.stop()


def test_model_request_arriving_during_check_is_not_lost(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    checker, run_models, app_check, _store = make_checker(
        tmp_path, Settings(check_model_updates=True), Policy()
    )
    entered = threading.Event()
    release = threading.Event()
    second = threading.Event()

    def blocking(*_args: object, **_kwargs: object) -> ModelCheckSummary:
        if not entered.is_set():
            entered.set()
            assert release.wait(5), "тест не отпустил первую проверку"
        else:
            second.set()
        return ModelCheckSummary("checked")

    run_models.side_effect = blocking
    try:
        checker.start(scheduled=False)
        checker.check_models_now()
        assert entered.wait(5)
        checker.check_models_now()
        release.set()
        assert second.wait(5), "заявка во время проверки потерялась"
        drain_worker(checker)
        assert run_models.call_count == 2
        app_check.assert_not_called()
    finally:
        release.set()
        checker.stop()
