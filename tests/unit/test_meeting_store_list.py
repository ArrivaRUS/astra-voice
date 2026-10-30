"""Read-only catalogue required by the meetings section; no audio or Qt."""

from __future__ import annotations

from pathlib import Path

import pytest

from astra_voice.meetings.events import MeetingState
from astra_voice.meetings.model import Meeting
from astra_voice.meetings.store import MeetingStore, MeetingStoreError

pytestmark = pytest.mark.unit
STAMP = "2026-10-30T12:00:00+00:00"
ID = "2026-10-30_120000-ab12"


def test_list_missing_root_is_empty_without_creating_directory(tmp_path: Path) -> None:
    disk = MeetingStore(tmp_path / "absent" / "meetings")
    assert disk.list() == []
    assert not disk.root.parent.exists()


def test_list_roundtrip_sorted_and_ignores_service_entries(tmp_path: Path) -> None:
    disk = MeetingStore(tmp_path / "meetings")
    older = Meeting(id=ID, started_at=STAMP, title="older")
    newer = Meeting(id="2026-10-31_120000-ab12", started_at="2026-10-31T12:00:00+00:00")
    for row in (older, newer):
        disk.create(row)
    (disk.root / "unrelated").write_text("ignored")
    (disk.root / ".deleting-other").mkdir(mode=0o700)
    assert disk.list() == [newer, older]


@pytest.mark.parametrize("location", ["root", "directory", "file"])
def test_list_rejects_symlink_without_reading_target(tmp_path: Path, location: str) -> None:
    disk = MeetingStore(tmp_path / "meetings")
    disk.create(Meeting(id=ID, started_at=STAMP))
    victim = {
        "root": disk.root,
        "directory": disk.root / ID,
        "file": disk.root / ID / "meeting.json",
    }[location]
    original = victim.with_name(victim.name + "-target")
    victim.rename(original)
    victim.symlink_to(original, target_is_directory=location != "file")
    with pytest.raises(MeetingStoreError):
        disk.list()
    assert victim.is_symlink()


def test_list_ignores_tombstone_and_deleting_metadata(tmp_path: Path) -> None:
    disk = MeetingStore(tmp_path / "meetings")
    disk.create(Meeting(id=ID, started_at=STAMP))
    (disk.root / (".deleting-" + ID)).mkdir(mode=0o700)
    disk.create(Meeting(id="2026-10-31_120000-ab12", started_at=STAMP, state=MeetingState.DELETING))
    assert disk.list() == []


@pytest.mark.parametrize("damage", ["json", "missing", "identity"])
def test_list_corruption_is_catalogue_error_without_payload_or_path(
    tmp_path: Path, damage: str
) -> None:
    disk = MeetingStore(tmp_path / "meetings")
    disk.create(Meeting(id=ID, started_at=STAMP))
    metadata = disk.root / ID / "meeting.json"
    if damage == "missing":
        metadata.unlink()
    else:
        metadata.write_text(
            "PRIVATE_MARKER"
            if damage == "json"
            else metadata.read_text().replace(ID, "2026-10-31_120000-zz99")
        )
    with pytest.raises(MeetingStoreError) as caught:
        disk.list()
    assert "PRIVATE_MARKER" not in str(caught.value) and str(tmp_path) not in str(caught.value)


def test_list_orders_absolute_instants_across_timezones(tmp_path: Path) -> None:
    disk = MeetingStore(tmp_path / "meetings")
    latest = Meeting(id=ID, started_at="2026-10-30T10:00:00+00:00")
    earlier = Meeting(id="2026-10-30_110000-ab12", started_at="2026-10-30T11:00:00+03:00")
    disk.create(latest)
    disk.create(earlier)
    assert disk.list() == [latest, earlier]
