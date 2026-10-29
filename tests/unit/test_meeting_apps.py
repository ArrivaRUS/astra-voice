"""E13 app catalog is a bounded data contract, with no detector/subscriptions."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from astra_voice.meetings import apps
from astra_voice.security.files import read_private

pytestmark = pytest.mark.unit


def entry(**changes: object) -> dict[str, object]:
    return {
        "id": "dion",
        "title": "DION",
        "binary": ["dion"],
        "exe": [],
        "class": "dion",
        **changes,
    }


def test_bundled_catalog_minimum_and_roundtrip() -> None:
    catalog = apps.load_apps()
    assert any(
        row.id == "dion" and row.app_class is apps.AppClass.DION and "dion" in row.binary
        for row in catalog
    )
    for row in catalog:
        assert apps.AppDefinition.from_dict(row.to_dict()) == row


@pytest.mark.parametrize(
    "kind,label", [("dion", "DION"), ("browser", "Браузер"), ("other", "Другая программа")]
)
def test_only_fixed_class_labels(kind: str, label: str) -> None:
    row = apps.AppDefinition.from_dict(entry(**{"class": kind, "title": "PRIVATE_MANIFEST_TITLE"}))
    assert row.display_class == label
    assert "PRIVATE_MANIFEST_TITLE" not in row.display_class


@pytest.mark.parametrize(
    "field,value",
    [
        ("class", "raw-process-class"),
        ("id", "../escape"),
        ("id", ""),
        ("title", "x" * 256),
        ("title", "я" * 128),
        ("title", "a\u202eb"),
        ("title", "a\nb"),
        ("binary", ["../dion"]),
        ("binary", ["a\x00b"]),
        ("binary", ["dion", "dion"]),
        ("binary", "dion"),
        ("exe", ["relative/dion"]),
        ("exe", ["/opt/../tmp/dion"]),
        ("unexpected", "PRIVATE_MARKER"),
    ],
)
def test_invalid_catalog_fields_rejected_without_content(field: str, value: object) -> None:
    with pytest.raises(apps.AppDataError) as caught:
        apps.AppDefinition.from_dict(entry(**{field: value}))
    assert "PRIVATE_MARKER" not in str(caught.value)


def test_nfc_and_utf8_255_boundary() -> None:
    row = apps.AppDefinition.from_dict(entry(title="е\u0308" * 127 + "a"))
    assert row.title == "ё" * 127 + "a" and len(row.title.encode()) == 255


@pytest.mark.parametrize(
    "document",
    [
        {"schema": 2, "apps": []},
        {"schema": True, "apps": []},
        {"schema": 1, "apps": [entry(), entry()]},
        {"schema": 1, "apps": [], "unknown": 1},
        {"schema": 1, "apps": [entry(), entry(id="other")]},
    ],
)
def test_invalid_whole_catalog(tmp_path: Path, document: object) -> None:
    path = tmp_path / "apps.json"
    path.write_text(json.dumps(document))
    with pytest.raises(apps.AppDataError):
        apps.load_apps_for_tests(path)


def test_missing_catalog_is_empty_and_duplicate_keys_refused(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    assert apps.load_apps_for_tests(path) == ()
    path.write_text('{"schema":1,"schema":1,"apps":[]}')
    with pytest.raises(apps.AppDataError):
        apps.load_apps_for_tests(path)


def test_bounded_read_and_symlink_refusal(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    path.write_bytes(b" " * (apps.MAX_APPS_BYTES + 1))
    with pytest.raises(apps.AppDataError):
        apps.load_apps_for_tests(path)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(apps.AppDataError) as caught:
        apps.load_apps_for_tests(link)
    assert str(tmp_path) not in str(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [("title", "   "), ("binary", ["   "]), ("exe", ["/opt//dion"]), ("exe", ["//opt/dion"])],
)
def test_revision_blank_names_and_empty_path_components_refused(field: str, value: object) -> None:
    with pytest.raises(apps.AppDataError):
        apps.AppDefinition.from_dict(entry(**{field: value}))


def test_revision_production_loader_has_no_path_override(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        apps.load_apps(tmp_path / "untrusted.json")  # type: ignore[call-arg]


def test_revision_production_loader_uses_package_data(monkeypatch: pytest.MonkeyPatch) -> None:
    from astra_voice.core.paths import data_dir_static

    original = read_private
    seen: list[Path] = []

    def read(path: Path, limit: int) -> bytes:
        seen.append(path)
        return original(path, limit)

    monkeypatch.setattr(apps, "read_private", read)
    assert apps.load_apps()
    assert seen == [data_dir_static() / "meeting-apps.json"]


@pytest.mark.parametrize(
    "content",
    [
        b'\xef\xbb\xbf{"schema":1,"apps":[]}',
        b'{"schema":1,"apps":[],"apps":[]}',
        b'{"schema":1,"apps":[{"id":"dion","id":"other",'
        b'"title":"DION","binary":["dion"],"exe":[],"class":"dion"}]}',
        b'{"schema":1,"apps":[]}'.ljust(apps.MAX_APPS_BYTES + 1, b" "),
    ],
    ids=["bom", "duplicate-top-level", "duplicate-nested", "oversize"],
)
def test_revision_catalog_rejects_ambiguous_or_oversize_bytes(
    tmp_path: Path, content: bytes
) -> None:
    path = tmp_path / "PRIVATE_PATH.json"
    path.write_bytes(content)
    with pytest.raises(apps.AppDataError) as caught:
        apps.load_apps_for_tests(path)
    assert "PRIVATE_PATH" not in str(caught.value) and str(tmp_path) not in str(caught.value)


def test_revision_catalog_size_boundary_and_path(tmp_path: Path) -> None:
    path = tmp_path / "apps.json"
    prefix = b'{"schema":1,"apps":[]}'
    path.write_bytes(prefix + b" " * (apps.MAX_APPS_BYTES - len(prefix)))
    assert apps.load_apps_for_tests(path) == ()
    with pytest.raises(apps.AppDataError):
        apps.load_apps_for_tests(tmp_path)
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(apps.AppDataError):
        apps.load_apps_for_tests(link)
