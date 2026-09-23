"""Построение запроса загрузки модели без ввода-вывода."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any


class ModelNotConfigured(ValueError):
    """В настройках отсутствуют модель или ревизия."""


def model_threads(settings: Mapping[str, Any], *, explicit: int | None = None) -> Any:
    """Тот же приоритет потоков, что у запроса загрузки модели."""
    for value in (explicit, settings.get("model_threads"), settings.get("threads")):
        if value is not None:
            return value
    return 2


def build_model_load(
    settings: Mapping[str, Any],
    *,
    model_dir: str | None = None,
    variant: str | None = None,
    threads: int | None = None,
    store_dir: Path,
) -> dict[str, Any]:
    """Выбирает аргумент → model_<name> → <name> → умолчание; раскрывает ~ в dir.

    Путь не разрешается и не проверяется на файловой системе.
    """

    def option(name: str, default: Any = None, *, explicit: Any = None) -> Any:
        for value in (explicit, settings.get(f"model_{name}"), settings.get(name)):
            if value is not None:
                return value
        return default

    model_id = option("id")
    revision = option("revision", "")
    directory = option("dir", explicit=model_dir)
    if model_dir is not None:
        path = Path(model_dir)
        model_id = path.parent.name or "local-model"
        revision = path.name or "local-revision"
    elif not model_id or not revision:
        raise ModelNotConfigured(
            "Модель не настроена. Укажите --model-dir или модель и ревизию в настройках."
        )
    if not directory:
        directory = store_dir / model_id / revision
    return {
        "type": "model.load",
        "id": model_id,
        "revision": revision,
        "dir": str(Path(directory).expanduser()),
        "layout": option("layout", "onnx-asr-gigaam-v3"),
        "variant": option("variant", "gigaam-v3-e2e-rnnt", explicit=variant),
        "threads": model_threads(settings, explicit=threads),
        "min_ram_mb": option("min_ram_mb", 768),
    }
