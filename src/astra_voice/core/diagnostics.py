"""Diagnostic archive allowlist. No traversal, raw logs or raw settings export.

This backend intentionally generates two JSON members instead of archiving a
user-data tree. Only the fixed stats.json source is read through a pinned private
directory; meetings/checkpoints/keys and logs are never opened. UI wiring is a
separate step. Endpoint classification is conservative and performs no DNS/I/O.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from astra_voice.core.paths import state_dir_path
from astra_voice.core.settings import SCHEMA_VERSION, Settings
from astra_voice.core.stats import _CHOICES, MAX_EVENTS, _event
from astra_voice.core.version import __version__
from astra_voice.meetings.events import is_number
from astra_voice.meetings.policy import internal_address, validate_endpoint
from astra_voice.security.files import private_directory, read_private, write_private

ARCHIVE_MEMBERS = frozenset({"summary.json", "stats.json"})
MAX_STATS_BYTES = 1024 * 1024
_BOOL_SETTINGS = (
    "meetings_enabled",
    "meeting_offer_on_call",
    "meeting_transcribe_immediately",
    "meeting_diarization",
    "meeting_ai_replace_names",
    "offline",
    "autostart",
)
_INT_SETTINGS = ("meeting_audio_retention_days", "meeting_retention_days")


class DiagnosticsError(ValueError):
    """A fixed message only; never exposes paths or source contents."""


def endpoint_class(value: object) -> str:
    if value == "off":
        return "off"
    if not isinstance(value, str) or not value:
        return "unset"
    try:
        validated = validate_endpoint(value, allow_http=True)
        host = urlsplit(validated).hostname
        if host is None:
            return "unset"
        if host == "localhost" or internal_address(host):
            return "internal"
        return "external"
    except (ValueError, UnicodeError):
        return "unset"


def settings_summary(settings: Settings) -> dict[str, object]:
    result: dict[str, object] = {"schema_version": SCHEMA_VERSION}
    for name in _BOOL_SETTINGS:
        value = getattr(settings, name)
        if type(value) is bool:
            result[name] = value
    for name in _INT_SETTINGS:
        value = getattr(settings, name)
        if type(value) is int and is_number(value):
            result[name] = value
    result["meeting_ai_endpoint"] = endpoint_class(settings.meeting_ai_endpoint)
    return result


def _stats_summary(data_dir: Path) -> dict[str, object]:
    events: list[dict[str, object]] = []
    try:
        with private_directory(data_dir) as descriptor:
            raw = read_private(
                Path("stats.json"), MAX_STATS_BYTES, directory_fd=descriptor, private=True
            )
        document = json.loads(raw.decode("utf-8"))
        if (
            not isinstance(document, dict)
            or type(document.get("schema_version")) is not int
            or document["schema_version"] != 1
            or not isinstance(document.get("events"), list)
        ):
            raise ValueError
        for row in document["events"][-MAX_EVENTS:]:
            if not isinstance(row, dict) or not isinstance(row.get("type"), str):
                continue
            kind = row["type"]
            try:
                validated = _event(
                    kind,
                    {key: value for key, value in row.items() if key not in {"type", "ts"}},
                    warn_unknown=False,
                )
            except (ValueError, TypeError, OverflowError):
                continue
            # Legacy stats may contain free model/revision/CPU strings. Export
            # only numbers and fixed enum fields, even for those legacy events.
            safe: dict[str, object] = {"type": validated["type"]}
            for key, value in validated.items():
                if key == "type":
                    continue
                if is_number(value) or type(value) is bool:
                    safe[key] = value
                elif kind.startswith("meeting_") or (kind, key) in _CHOICES:
                    safe[key] = value
            if is_number(row.get("ts")):
                safe["ts"] = row["ts"]
            events.append(safe)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        pass  # Missing/unsafe source means an empty optional snapshot, no log.
    return {"schema_version": 1, "events": events}


def collect_diagnostics(
    destination: Path, *, settings: Settings, data_dir: Path, stats_dir: Path | None = None
) -> Path:
    """Publish a new 0600 zip atomically; never overwrite a previous export."""
    try:
        stats_root = state_dir_path() if stats_dir is None else stats_dir
        snapshot = (
            {"schema_version": 1, "events": []}
            if stats_root.is_relative_to(data_dir / "meetings")
            else _stats_summary(stats_root)
        )
        members: dict[str, Any] = {
            "summary.json": {"version": __version__, "settings": settings_summary(settings)},
            "stats.json": snapshot,
        }
        assert frozenset(members) == ARCHIVE_MEMBERS
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, value in members.items():
                info = zipfile.ZipInfo(name)
                info.external_attr = 0o600 << 16
                archive.writestr(
                    info, json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
                )
        write_private(destination, output.getvalue(), create_only=True, private=True)
    except FileExistsError:
        raise
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        raise DiagnosticsError("Не удалось собрать диагностику") from None
    return destination
