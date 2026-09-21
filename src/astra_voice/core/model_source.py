"""Выбор источника модели для загрузки."""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from astra_voice.core import paths
from astra_voice.core.model_request import ModelNotConfigured, build_model_load
from astra_voice.core.settings import Settings
from astra_voice.models.store import ModelRecord as ModelRecord
from astra_voice.models.store import ModelStore as ModelStore
from astra_voice.worker.supervisor import WorkerSupervisor

log = logging.getLogger(__name__)

SMOKE_WAV_NAME = "smoke-ru.wav"
SMOKE_EXPECT_ANY: tuple[str, ...] = ("проверка", "связи")


def smoke_wav_path() -> Path:
    """Возвращает путь к пробной записи из поставляемых данных."""
    return paths.data_dir_static() / "smoke" / SMOKE_WAV_NAME


def smoke_matches(text: str, expect_any: Sequence[str] = SMOKE_EXPECT_ANY) -> bool:
    """Проверяет наличие хотя бы одного ожидаемого слова без учёта регистра."""
    folded = text.casefold()
    return any(word.casefold() in folded for word in expect_any)


@dataclass(frozen=True)
class SmokeResult:
    """Итог проверки модели без распознанного текста."""

    ok: bool
    reason: str


class SmokeRunner:
    """Проверяет модель пробным распознаванием в отдельном воркере без Qt."""

    def __init__(
        self,
        *,
        supervisor_factory: Callable[..., Any] = WorkerSupervisor,
        wav_path: Path | None = None,
        expect_any: Sequence[str] = SMOKE_EXPECT_ANY,
        hello_timeout_s: float = 10.0,
        load_timeout_s: float = 30.0,
        transcribe_timeout_s: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._supervisor_factory = supervisor_factory
        self._wav_path = wav_path
        self._expect_any = tuple(expect_any)
        self._hello_timeout_s = hello_timeout_s
        self._load_timeout_s = load_timeout_s
        self._transcribe_timeout_s = transcribe_timeout_s
        self._clock = clock

    def run(
        self, request: Mapping[str, Any], *, cancel: Callable[[], bool] | None = None
    ) -> SmokeResult:
        """Ждёт запуск, загрузку и результат; всегда останавливает свой воркер."""

        def finish(reason: str) -> SmokeResult:
            # Только фиксированная причина: ответы и исключения могут содержать речь.
            log.debug("Смоук модели завершён: %s", reason)
            return SmokeResult(reason == "ok", reason)

        events: deque[dict[str, Any]] = deque()
        supervisor = None

        def wait_for(kind: str, timeout: float) -> dict[str, Any]:
            assert supervisor is not None
            deadline = self._clock() + timeout
            while True:
                if cancel is not None and cancel():
                    return {"type": "cancelled"}
                while events:
                    event = events.popleft()
                    if event["type"] in (kind, "error", "cancelled"):
                        return event
                remaining = deadline - self._clock()
                if remaining <= 0:
                    return {"type": "error", "code": "timeout"}
                supervisor.pump(min(remaining, 0.05))

        try:
            try:
                wav = self._wav_path if self._wav_path is not None else smoke_wav_path()
                if not wav.is_file():
                    return finish("no-wav")
                supervisor = self._supervisor_factory(on_event=events.append, use_qt=False)
                if cancel is not None and cancel():
                    return finish("cancelled")
                supervisor.start()
                for kind, command, timeout, failure in (
                    ("hello", None, self._hello_timeout_s, "worker-failed"),
                    ("model.loaded", dict(request), self._load_timeout_s, "load-failed"),
                    (
                        "result",
                        {"type": "transcribe.file", "path": str(wav)},
                        self._transcribe_timeout_s,
                        "transcribe-failed",
                    ),
                ):
                    if cancel is not None and cancel():
                        return finish("cancelled")
                    if command is not None:
                        supervisor.send(command, timeout=timeout)
                    event = wait_for(kind, timeout)
                    if event["type"] == "cancelled":
                        return finish("cancelled")
                    if event["type"] == "error":
                        if event.get("code") in ("timeout", "load-timeout"):
                            return finish("timeout")
                        if event.get("code") in ("worker-start", "worker-crashed", "restart-limit"):
                            return finish("worker-failed")
                        return finish(failure)
                    if kind == "result":
                        text = event.get("text")
                        if not isinstance(text, str):
                            return finish("transcribe-failed")
                        matched = smoke_matches(text, self._expect_any)
                        return finish("ok" if matched else "no-match")
            finally:
                if supervisor is not None:
                    supervisor.stop()
        except (OSError, RuntimeError):
            return finish("worker-failed")
        return finish("worker-failed")

    __call__ = run


def resolve_model_request(
    settings: Settings, store: ModelStore | None = None, *, store_dir: Path
) -> dict[str, Any] | None:
    """Выбирает явно заданную модель, модель хранилища или модель из настроек."""
    data = settings.to_dict()
    if model_dir := data.get("model_dir"):
        if data.get("model_id") and data.get("model_revision"):
            return build_model_load(data, store_dir=store_dir)
        return build_model_load(data, model_dir=model_dir, store_dir=store_dir)

    if store is not None:
        try:
            record = store.current()
        except Exception:
            log.warning("Не удалось получить выбранную модель из хранилища")
        else:
            if (
                record is not None
                and record.state == "ok"
                and record.recheck is not True
                and record.metadata_ok
            ):
                return build_model_load(
                    {
                        **data,
                        "model_id": record.id,
                        "model_revision": record.revision,
                        "model_dir": str(record.dir),
                        "model_layout": record.layout,
                        "model_variant": record.variant,
                    },
                    store_dir=store_dir,
                )

    try:
        return build_model_load(data, store_dir=store_dir)
    except ModelNotConfigured:
        return None
