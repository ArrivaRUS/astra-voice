"""Bounded metadata source for meeting-capture; no call rule, IPC or audio.

The role's metadata loop calls start/poll/stop, serially, outside the GUI.
No Python thread is created here. Native callbacks only enqueue notices; list
queries are reserved for snapshots/resync, changes use by-index introspection.
Only NormalizedEvent / diagnostic_snapshot leave this boundary. Raw properties
are private, projected before caching, and never logged or included in repr.
"""

from __future__ import annotations

import logging
import math
import time
import unicodedata
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Protocol

from astra_voice.meetings.apps import AppDataError, load_apps

log = logging.getLogger(__name__)

# Signed seconds left on the source's monotonic batch clock.
TimeRemaining = Callable[[], float]

MAX_ENTRIES = 1024
MAX_PENDING = 256
CHANGE_SECONDS = 1.0
BATCH_SECONDS = 1.0
RETRY_SECONDS = 1.0
MAX_CAPACITY_RETRY_SECONDS = 30.0
MAX_PROPERTY_BYTES = 4096
INVALID_INDEX = 0xFFFFFFFF
SUBSCRIPTION_MASK = (1 << 2) | (1 << 3) | (1 << 5) | (1 << 7)
PROPERTY_KEYS = ("application.process.id", "application.process.binary", "object.serial")


class Direction(StrEnum):
    MICROPHONE = "microphone"
    OUTPUT = "output"


class Facility(StrEnum):
    CLIENT = "client"
    MICROPHONE = "microphone"
    OUTPUT = "output"
    SERVER = "server"


class Change(StrEnum):
    NEW = "new"
    CHANGE = "change"
    REMOVE = "remove"


class EventKind(StrEnum):
    RESET = "reset"
    UPSERT = "upsert"
    REMOVE = "remove"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class StreamKey:
    generation: int
    direction: Direction
    index: int
    incarnation: int
    serial: int | None


@dataclass(frozen=True)
class ProgramKey:
    app_id: str
    binary: str


@dataclass(frozen=True)
class NormalizedEvent:
    kind: EventKind
    at: float
    key: StreamKey | None = None
    program: ProgramKey | None = None
    corked: bool | None = None
    muted: bool | None = None
    # Pulse stream introspection has cork/mute, but no running signal.
    running: bool | None = None


@dataclass(frozen=True)
class RawClient:
    index: int
    properties: Mapping[str, object] = field(default_factory=dict, repr=False)
    valid: bool = True


@dataclass(frozen=True)
class RawStream:
    index: int
    client: int
    direction: Direction
    properties: Mapping[str, object] = field(default_factory=dict, repr=False)
    corked: bool | None = None
    muted: bool | None = None
    running: bool | None = None
    valid: bool = True


@dataclass(frozen=True)
class Notice:
    facility: Facility
    change: Change
    index: int


@dataclass(frozen=True)
class _Pending:
    change: Change
    recreated: bool


class MetadataUnavailable(Exception):
    """Fixed internal failure; external exception text never crosses the boundary."""


class MetadataCapacity(MetadataUnavailable):
    "The server picture exceeds the shared row limit."


class MetadataDisconnected(MetadataUnavailable):
    "The backend is no longer usable."


class MetadataRequestTimeout(MetadataUnavailable):
    "An individual request did not finish on time."


class MetadataOpenFailed(MetadataUnavailable):
    "Opening failed and the caller must finish closing the backend."

    def __init__(self, backend: MetadataBackend) -> None:
        super().__init__()
        self.backend = backend


def check_picture_size(rows: int) -> None:
    if rows > MAX_ENTRIES:
        raise MetadataCapacity()


class _Phase(StrEnum):
    STOPPED = "stopped"
    REBUILD = "rebuild"
    LIVE = "live"


class MetadataBackend(Protocol):
    own_index: int

    def snapshot(
        self, *, remaining: TimeRemaining | None = None
    ) -> tuple[tuple[RawClient, ...], tuple[RawStream, ...]]: ...
    def client(self, index: int, *, remaining: TimeRemaining | None = None) -> RawClient | None: ...
    def stream(
        self, direction: Direction, index: int, *, remaining: TimeRemaining | None = None
    ) -> RawStream | None: ...
    def poll(self) -> tuple[tuple[Notice, ...], bool]: ...
    def take_overflow(self) -> bool: ...
    def close(self) -> None: ...


def _index(value: int) -> bool:
    return type(value) is int and 0 <= value < INVALID_INDEX


def project_properties(properties: Mapping[str, object]) -> tuple[dict[str, object], bool]:
    """Never enumerate unknown/private properties. The transport is libpulse.

    Native peer credentials cannot be asserted by a pulse client's proplist.
    application.name/media.name, sec.pid and client.api are never copied.
    """
    result: dict[str, object] = {}
    valid = True
    for name in PROPERTY_KEYS:
        value = properties.get(name)
        if value is None:
            continue
        try:
            if name != "application.process.binary":
                limit = 2**64 - 1 if name == "object.serial" else 2**31 - 1
                if type(value) is int:
                    number = value
                elif (
                    isinstance(value, str)
                    and value.isascii()
                    and value.isdigit()
                    and len(value) <= 20
                ):
                    number = int(value)
                else:
                    raise ValueError()
                if not 0 <= number <= limit or (name != "object.serial" and number == 0):
                    raise ValueError()
                result[name] = number
            else:
                if not isinstance(value, str) or len(value) > MAX_PROPERTY_BYTES:
                    raise ValueError()
                if len(value.encode("utf-8")) > MAX_PROPERTY_BYTES or any(
                    unicodedata.category(char) in {"Cc", "Cf", "Cs"} for char in value
                ):
                    raise ValueError()
                result[name] = unicodedata.normalize("NFC", value)
        except (ValueError, UnicodeError):
            valid = False
    return result, valid


def native_backend() -> MetadataBackend:
    from astra_voice.worker.meeting_metadata import PulseMetadata

    return PulseMetadata()


class MeetingDetectorSource:
    """Publish a bounded metadata picture from one serialized owner."""

    def __init__(
        self,
        *,
        factory: Callable[[], MetadataBackend] = native_backend,
        own_contexts: Callable[[int], frozenset[int]] = lambda generation: frozenset(),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.factory = factory
        self.own_contexts, self.clock = own_contexts, clock
        self._backend: MetadataBackend | None = None
        self._closing = False
        self._phase = _Phase.STOPPED
        self._generation = self._server_generation = self._incarnation = 0
        self._last_time = self._next_at = 0.0
        self._next_snapshot_at = 0.0
        self._capacity_retry = RETRY_SECONDS
        self._programs: dict[str, ProgramKey] = {}
        self._catalog_retry_at: float | None = None
        self._catalog_warned = False
        self._clients: dict[int, RawClient] = {}
        self._streams: dict[tuple[Direction, int], RawStream] = {}
        self._keys: dict[tuple[Direction, int], StreamKey] = {}
        self._published: dict[tuple[Direction, int], NormalizedEvent] = {}
        self._pending: dict[tuple[Facility, int], _Pending] = {}
        self._own: frozenset[int] = frozenset()

    def _now(self) -> float:
        now = self.clock()
        if not math.isfinite(now):
            raise MetadataUnavailable()
        self._last_time = max(self._last_time, now)
        return self._last_time

    def _clear(self, now: float) -> NormalizedEvent:
        self._generation += 1
        self._clients.clear()
        self._streams.clear()
        self._keys.clear()
        self._published.clear()
        self._pending.clear()
        self._own = frozenset()
        return NormalizedEvent(EventKind.RESET, now)

    def _close_backend(self) -> bool:
        if self._backend is not None:
            self._closing = True
            try:
                self._backend.close()
            except Exception:
                return False
            self._backend = None
        self._closing = False
        return True

    def _revoke(self, *, retry: float | None = None) -> tuple[NormalizedEvent, ...]:
        self._phase = _Phase.REBUILD
        reset = self._clear(self._now())
        if retry is not None:
            self._next_at = reset.at + retry
        return reset, NormalizedEvent(EventKind.UNAVAILABLE, reset.at)

    def _load_catalog(self) -> bool:
        try:
            apps = load_apps()
        except AppDataError:
            if not self._catalog_warned:
                log.warning("Не удалось загрузить каталог программ встреч")
                self._catalog_warned = True
            apps = ()
        else:
            self._catalog_warned = False
        self._programs = {
            binary: ProgramKey(app.id, binary) for app in apps for binary in app.binary
        }
        # The catalog loader also returns an empty result when the file is absent.
        self._catalog_retry_at = None if apps else self._now() + RETRY_SECONDS
        return bool(apps)

    def _disconnect(self) -> tuple[NormalizedEvent, ...]:
        self._close_backend()
        self._capacity_retry = RETRY_SECONDS
        return self._revoke(retry=RETRY_SECONDS)

    def _observe(self, notices: tuple[Notice, ...], overflow: bool) -> None:
        if overflow:
            raise MetadataUnavailable()
        for notice in notices:
            if (
                not _index(notice.index)
                or type(notice.facility) is not Facility
                or type(notice.change) is not Change
            ):
                raise MetadataUnavailable()
            key = (notice.facility, notice.index)
            previous = self._pending.get(key)
            self._pending[key] = _Pending(
                notice.change,
                notice.change is not Change.CHANGE or bool(previous and previous.recreated),
            )
            if len(self._pending) > MAX_PENDING:
                raise MetadataUnavailable()

    def _own_indexes(self) -> frozenset[int]:
        assert self._backend is not None
        indexes = self.own_contexts(self._server_generation) | {self._backend.own_index}
        check_picture_size(len(indexes))
        if any(not _index(index) for index in indexes):
            raise MetadataUnavailable()
        return frozenset(indexes)

    @staticmethod
    def _client(row: RawClient) -> RawClient:
        if not _index(row.index):
            raise MetadataUnavailable()
        props, valid = project_properties(row.properties)
        return replace(row, properties=props, valid=row.valid is True and valid)

    @staticmethod
    def _stream(row: RawStream) -> RawStream:
        if (
            not _index(row.index)
            or type(row.client) is not int
            or not 0 <= row.client <= INVALID_INDEX
            or type(row.direction) is not Direction
        ):
            raise MetadataUnavailable()
        if any(
            value is not None and type(value) is not bool
            for value in (row.corked, row.muted, row.running)
        ):
            raise MetadataUnavailable()
        props, valid = project_properties(row.properties)
        return replace(row, properties=props, valid=row.valid is True and valid)

    def _program(self, stream: RawStream) -> ProgramKey | None:
        client = self._clients.get(stream.client)
        if client is None or not client.valid or not stream.valid:
            return None
        binary = client.properties.get("application.process.binary")
        pid = client.properties.get("application.process.id")
        if not isinstance(binary, str) or pid is None:
            return None
        if stream.properties.get("application.process.binary", binary) != binary:
            return None
        if stream.properties.get("application.process.id", pid) != pid:
            return None
        return self._programs.get(binary)

    def _check_cache_growth(self) -> None:
        check_picture_size(len(self._clients) + len(self._streams) + 1)

    def _key(self, address: tuple[Direction, int], row: RawStream) -> StreamKey:
        self._incarnation += 1
        serial = row.properties.get("object.serial")
        return StreamKey(
            self._generation,
            address[0],
            address[1],
            self._incarnation,
            serial if type(serial) is int else None,
        )

    def _load_snapshot(self, remaining: TimeRemaining) -> None:
        assert self._backend is not None
        self._budget(remaining)
        try:
            clients, streams = self._backend.snapshot(remaining=remaining)
        finally:
            self._next_snapshot_at = self._now() + CHANGE_SECONDS
        check_picture_size(len(clients) + len(streams))
        for raw in clients:
            row = self._client(raw)
            if row.index in self._clients:
                raise MetadataUnavailable()
            self._clients[row.index] = row
        for raw_stream in streams:
            stream = self._stream(raw_stream)
            address = (stream.direction, stream.index)
            if address in self._streams:
                raise MetadataUnavailable()
            self._streams[address] = stream
            self._keys[address] = self._key(address, stream)

    def _budget(self, remaining: TimeRemaining) -> None:
        if remaining() <= 0:
            raise MetadataUnavailable()

    def _remove_stream(self, address: tuple[Direction, int]) -> None:
        self._streams.pop(address, None)
        self._keys.pop(address, None)

    def _load_changes(self, remaining: TimeRemaining) -> None:
        assert self._backend is not None
        pending, self._pending = self._pending, {}
        # Apply known removals before growing either cache.
        removed_clients = {
            index
            for (facility, index), change in pending.items()
            if facility is Facility.CLIENT and change.change is Change.REMOVE
        }
        for index in removed_clients:
            self._clients.pop(index, None)
        if removed_clients:
            for address in [
                key for key, row in self._streams.items() if row.client in removed_clients
            ]:
                self._remove_stream(address)
        for (facility, index), change in pending.items():
            if facility is Facility.OUTPUT and change.change is Change.REMOVE:
                self._remove_stream((Direction.OUTPUT, index))
            elif facility is Facility.MICROPHONE and change.change is Change.REMOVE:
                self._remove_stream((Direction.MICROPHONE, index))

        requested_clients = removed_clients
        for (facility, index), change in pending.items():
            if facility is not Facility.CLIENT or change.change is Change.REMOVE:
                continue
            self._budget(remaining)
            requested_clients.add(index)
            self._refresh_client(index, remaining)
            if change.recreated:
                for address, row in self._streams.items():
                    if row.client == index:
                        self._keys[address] = self._key(address, row)
        for (facility, index), change in pending.items():
            if (
                facility not in {Facility.OUTPUT, Facility.MICROPHONE}
                or change.change is Change.REMOVE
            ):
                continue
            direction = Direction.OUTPUT if facility is Facility.OUTPUT else Direction.MICROPHONE
            address = (direction, index)
            self._budget(remaining)
            raw_stream = self._backend.stream(direction, index, remaining=remaining)
            if (
                raw_stream is None
                or raw_stream.index != index
                or raw_stream.direction is not direction
            ):
                raise MetadataUnavailable()
            old = self._streams.get(address)
            if old is None:
                self._check_cache_growth()
            row = self._stream(raw_stream)
            if (
                old is None
                or change.recreated
                or old.client != row.client
                or old.properties.get("object.serial") != row.properties.get("object.serial")
            ):
                self._keys[address] = self._key(address, row)
            self._streams[address] = row
            if (
                row.client != INVALID_INDEX
                and row.client not in self._clients
                and row.client not in requested_clients
            ):
                self._budget(remaining)
                requested_clients.add(row.client)
                self._refresh_client(row.client, remaining)

    def _refresh_client(self, index: int, remaining: TimeRemaining) -> None:
        assert self._backend is not None
        raw = self._backend.client(index, remaining=remaining)
        if raw is None:
            self._clients.pop(index, None)
            return
        if raw.index != index:
            raise MetadataUnavailable()
        if index not in self._clients:
            self._check_cache_growth()
        self._clients[index] = self._client(raw)

    def _publish(self, *, remaining: TimeRemaining) -> tuple[NormalizedEvent, ...]:
        now = self._now()
        updated: dict[tuple[Direction, int], NormalizedEvent] = {}
        events: list[NormalizedEvent] = []
        for address, row in self._streams.items():
            if row.client in self._own:
                continue
            event = NormalizedEvent(
                EventKind.UPSERT,
                now,
                self._keys[address],
                self._program(row),
                row.corked,
                row.muted,
                row.running,
            )
            updated[address] = event
            previous = self._published.get(address)
            if previous is None or replace(previous, at=now) != event:
                if previous is not None and previous.key != event.key:
                    events.append(replace(previous, kind=EventKind.REMOVE, at=now))
                events.append(event)
        events.extend(
            replace(previous, kind=EventKind.REMOVE, at=now)
            for address, previous in self._published.items()
            if address not in updated
        )
        assert self._backend is not None
        if self._backend.take_overflow():
            raise MetadataUnavailable()
        self._budget(remaining)
        self._published = updated
        self._phase = _Phase.LIVE
        return tuple(events)

    def _capacity(self) -> tuple[NormalizedEvent, ...]:
        delay = self._capacity_retry
        self._capacity_retry = min(2 * delay, MAX_CAPACITY_RETRY_SECONDS)
        return self._revoke(retry=delay)

    def _remaining(self, now: float) -> TimeRemaining:
        deadline = now + BATCH_SECONDS
        return lambda: deadline - self._now()

    def _attempt(self, now: float, *, full: bool) -> tuple[NormalizedEvent, ...]:
        remaining = self._remaining(now)
        try:
            reset = self._clear(now) if full else None
            if full:
                self._load_snapshot(remaining)
            else:
                self._load_changes(remaining)
            self._own = self._own_indexes()
            events = self._publish(remaining=remaining)
        except MetadataCapacity:
            return self._capacity()
        except MetadataDisconnected:
            return self._disconnect()
        except Exception:
            return self._revoke(retry=RETRY_SECONDS)
        self._capacity_retry = RETRY_SECONDS
        self._next_at = self._now() + CHANGE_SECONDS
        return (reset, *events) if reset is not None else events

    def _connect(self) -> tuple[NormalizedEvent, ...]:
        try:
            self._backend = self.factory()
        except MetadataOpenFailed as error:
            self._backend = error.backend
            self._closing = True
            return self._revoke(retry=RETRY_SECONDS)
        except Exception:
            return self._revoke(retry=RETRY_SECONDS)
        self._server_generation += 1
        return self._attempt(self._now(), full=True)

    def start(self) -> tuple[NormalizedEvent, ...]:
        if self._phase is not _Phase.STOPPED:
            return ()
        now = self._now()
        if now >= self._next_snapshot_at and (
            self._catalog_retry_at is None or now >= self._catalog_retry_at
        ):
            self._load_catalog()
        if not self._close_backend():
            return self._revoke(retry=RETRY_SECONDS)
        self._capacity_retry = RETRY_SECONDS
        if now < self._next_snapshot_at:
            self._phase = _Phase.REBUILD
            self._next_at = max(self._next_snapshot_at, self._catalog_retry_at or 0.0)
            self._catalog_retry_at = self._next_at
            return ()
        return self._connect()

    def poll(self) -> tuple[NormalizedEvent, ...]:
        if self._phase is _Phase.STOPPED:
            return ()
        started = now = self._now()
        catalog_loaded = (
            self._catalog_retry_at is not None
            and now >= self._catalog_retry_at
            and (self._phase is not _Phase.LIVE or now >= self._next_at)
            and self._load_catalog()
        )
        if self._closing:
            if now < self._next_at:
                return ()
            if not self._close_backend():
                return self._revoke(retry=RETRY_SECONDS)
        if self._backend is None:
            return self._connect() if now >= self._next_at else ()
        try:
            notices, overflow = self._backend.poll()
            self._observe(notices, overflow)
            now = self._now()
            if self._phase is _Phase.REBUILD or catalog_loaded:
                if now >= self._next_at:
                    return self._attempt(started if catalog_loaded else now, full=True)
                return ()
            if self._pending and now >= self._next_at:
                full = any(facility is Facility.SERVER for facility, _ in self._pending)
                return self._attempt(now, full=full)
            own = self._own_indexes()
            if own != self._own:
                self._own = own
                return self._publish(remaining=self._remaining(started))
        except MetadataCapacity:
            return self._capacity()
        except MetadataDisconnected:
            return self._disconnect()
        except Exception:
            return self._revoke()
        return ()

    def stop(self) -> tuple[NormalizedEvent, ...]:
        self._close_backend()
        if self._phase is _Phase.STOPPED:
            return ()
        self._phase = _Phase.STOPPED
        return (self._clear(self._now()),)

    def diagnostic_snapshot(self) -> tuple[dict[str, object], ...]:
        """Explicit whitelist of committed events, never the tentative picture."""
        result: list[dict[str, object]] = []
        for event in self._published.values():
            key = event.key
            assert key is not None
            result.append(
                {
                    "generation": key.generation,
                    "index": key.index,
                    "direction": key.direction.value,
                    "incarnation": key.incarnation,
                    "serial": key.serial,
                    "at": event.at,
                    "corked": event.corked,
                    "muted": event.muted,
                    "running": event.running,
                    "program": (
                        {"app_id": event.program.app_id, "binary": event.program.binary}
                        if event.program is not None
                        else None
                    ),
                }
            )
        return tuple(result)
