"""Итоговые замеры модели после диктовки, без записи речи."""

from __future__ import annotations

import json
import logging
import math
import os
import stat
import statistics
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from astra_voice.core.paths import PathError, ensure_private_dir
from astra_voice.core.version import __version__

log = logging.getLogger(__name__)
MAX_MEASUREMENTS_BYTES = 1 << 20


def read_measurements(path: Path | None) -> dict[str, Any]:
    """Читает общий файл замеров; ошибка не влияет на диктовку."""
    if path is None:
        return {}
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MEASUREMENTS_BYTES:
            raise ValueError("Неверный тип или размер файла замеров")
        with path.open("rb") as stream:
            raw = stream.read(MAX_MEASUREMENTS_BYTES + 1)
        if len(raw) > MAX_MEASUREMENTS_BYTES:
            raise ValueError("Файл замеров слишком большой")
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("ожидался объект")
        return {
            key: value
            for key, value in data.items()
            if isinstance(value, dict) and value.get("build") == __version__
        }
    except FileNotFoundError:
        return {}
    except (OSError, ValueError, UnicodeError, RecursionError):
        log.warning("Не удалось прочитать замеры моделей")
        return {}


class MeasurementTracker:
    """Замеры текущей модели и ограниченное окно тёплых прогонов."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self.identity: tuple[str, str, int] | None = None
        self.requested = False
        self.remeasure_pending = False
        self.warm_runs: list[float] = []
        self.ram_mb: int | None = None
        self.measured_at: str | None = None
        self.max_audio_ms = 0.0

    def new_generation(self) -> None:
        """Новый движок снова холодный; запрос к старому уже не завершится."""
        self.requested = False
        self.remeasure_pending = False
        self.warm_runs.clear()
        self.max_audio_ms = 0.0

    def measure_failed(self) -> None:
        self.requested = False
        self.remeasure_pending = True

    def set_model(self, model_id: str, revision: str, threads: int) -> None:
        identity = (model_id, revision, threads)
        if self.identity == identity:
            return
        self.identity = identity
        self.requested = False
        self.remeasure_pending = False
        self.warm_runs.clear()
        self.ram_mb = None
        self.measured_at = None
        self.max_audio_ms = 0.0
        entries = read_measurements(self.path)
        key = f"{model_id}@{revision}"
        previous = entries.get(key)
        changed = False
        if isinstance(previous, dict) and previous.get("threads") != threads:
            del entries[key]
            changed = True
        elif isinstance(previous, dict):
            old_ram = previous.get("ram_mb")
            if type(old_ram) is int and old_ram >= 0:
                self.ram_mb = old_ram
            old_date = previous.get("measured_at")
            if isinstance(old_date, str):
                self.measured_at = old_date
        for old_key in list(entries):
            if old_key.startswith(f"{model_id}@") and old_key != key:
                del entries[old_key]
                changed = True
        if changed:
            self._write(entries)

    def dictation(self, *, audio_ms: float | None, infer_ms: float | None, cold: bool) -> bool:
        """Запрашивает память после первого успеха, медианы и более длинной записи."""
        if (
            self.identity is None
            or audio_ms is None
            or infer_ms is None
            or not math.isfinite(audio_ms)
            or not math.isfinite(infer_ms)
            or audio_ms <= 0
            or infer_ms <= 0
        ):
            if self.identity is not None and self.remeasure_pending and not self.requested:
                self.remeasure_pending = False
                self.requested = True
                return True
            return False
        longer = audio_ms > self.max_audio_ms
        self.max_audio_ms = max(self.max_audio_ms, audio_ms)
        first_median = False
        if not cold:
            self.warm_runs.append(audio_ms / infer_ms)
            if len(self.warm_runs) > 20:
                self.warm_runs.pop(0)
            first_median = len(self.warm_runs) == 5
            if len(self.warm_runs) >= 5:
                self._save()
        if self.requested:
            self.remeasure_pending |= first_median or longer
            return False
        if self.ram_mb is None or first_median or longer or self.remeasure_pending:
            self.requested = True
            self.remeasure_pending = False
            return True
        return False

    def measured(self, vm_hwm_kb: int) -> bool:
        if self.identity is None or vm_hwm_kb < 0:
            return False
        measured = round(vm_hwm_kb * 1024 / 1_000_000)
        ram_mb = max(self.ram_mb or 0, measured)
        if ram_mb != self.ram_mb:
            self.ram_mb = ram_mb
            self.measured_at = datetime.now(UTC).isoformat()
        self.requested = False
        self._save()
        if self.remeasure_pending:
            self.remeasure_pending = False
            self.requested = True
            return True
        return False

    def _save(self) -> None:
        if self.identity is None:
            return
        model_id, revision, threads = self.identity
        entries = read_measurements(self.path)
        key = f"{model_id}@{revision}"
        result = {
            "ram_mb": self.ram_mb,
            "rtfx": statistics.median(self.warm_runs) if len(self.warm_runs) >= 5 else None,
            "measured_at": self.measured_at,
            "threads": threads,
            "build": __version__,
        }
        previous = entries.get(key)
        if isinstance(previous, dict) and all(
            previous.get(field) == result[field] for field in ("ram_mb", "rtfx", "threads")
        ):
            return
        if result["measured_at"] is None:
            result["measured_at"] = datetime.now(UTC).isoformat()
        entries[key] = result
        self._write(entries)

    def _write(self, entries: dict[str, Any]) -> None:
        if self.path is None:
            return
        temp_path: str | None = None
        try:
            ensure_private_dir(self.path.parent)
            with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=self.path.parent, delete=False
            ) as stream:
                temp_path = stream.name
                os.fchmod(stream.fileno(), 0o600)
                json.dump(entries, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp_path, self.path)
        except (OSError, PathError):
            log.warning("Не удалось сохранить замеры моделей")
        finally:
            if temp_path is not None and os.path.exists(temp_path):
                try:
                    os.unlink(temp_path)
                except OSError:
                    log.warning("Не удалось удалить временный файл замеров")
