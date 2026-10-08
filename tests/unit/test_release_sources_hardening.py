"""T-192: bounded tar/XZ input and verification before release archive parsing."""

from __future__ import annotations

import hashlib
import importlib.util
import io
import lzma
import sys
import tarfile
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, cast

import pytest
from test_validate_release import (
    SOURCES,
    ReleaseAssets,
    add_appimage,
    release_checks,
)
from test_validate_release import assets as assets
from test_validate_release import validate as validate
from test_validate_release import validate_globals as validate_globals

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
FILES = {"README.txt": b"r", "astra-voice/a.tar": b"code", "upstream/q/q.tar.gz": b"q"}
TOP = "astra-voice-0.1.0-sources"


@pytest.fixture
def sources() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "release_sources_hardening", ROOT / "scripts/release_sources.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _block(name: str, data: bytes, kind: bytes = tarfile.REGTYPE) -> bytes:
    member = tarfile.TarInfo(name)
    member.size = len(data)
    member.type = kind
    header = bytearray(member.tobuf(format=tarfile.GNU_FORMAT))
    if kind == tarfile.GNUTYPE_SPARSE:
        # A valid old-GNU sparse file with one extent, not an already malformed tar.
        header[386:398] = b"00000000000\0"
        header[398:410] = f"{len(data):011o}\0".encode("ascii")
        header[483:495] = f"{len(data):011o}\0".encode("ascii")
        header[148:156] = b"        "
        header[148:156] = f"{sum(header):06o}\0 ".encode("ascii")
    return bytes(header) + data + b"\0" * (-len(data) % 512)


def _raw_tar(kind: bytes = tarfile.REGTYPE, longnames: int = 0) -> bytes:
    manifest = "".join(
        f"{hashlib.sha256(data).hexdigest()}\t{len(data)}\t{name}\tx\n"
        for name, data in FILES.items()
    ).encode()
    headers = b"".join(
        _block("././@LongLink", f"{TOP}/MANIFEST.txt\0".encode(), tarfile.GNUTYPE_LONGNAME)
        for _ in range(longnames)
    )
    return (
        headers
        + _block(f"{TOP}/MANIFEST.txt", manifest)
        + b"".join(
            _block(f"{TOP}/{name}", data, kind if name == "README.txt" else tarfile.REGTYPE)
            for name, data in FILES.items()
        )
        + b"\0" * 1024
    )


def _compressed(tmp_path: Path, raw: bytes) -> Path:
    path = tmp_path / "sources.tar.xz"
    path.write_bytes(lzma.compress(raw, preset=0))
    return path


@pytest.mark.parametrize("kind", [tarfile.GNUTYPE_SPARSE, tarfile.CONTTYPE])
def test_nonordinary_tar_types_rejected(tmp_path: Path, sources: ModuleType, kind: bytes) -> None:
    path = _compressed(tmp_path, _raw_tar(kind))
    with pytest.raises(sources.SourcesError, match="не обычный файл|sparse"):
        sources.check(path)


def test_pax_sparse_rejected(tmp_path: Path, sources: ModuleType) -> None:
    path = tmp_path / "pax.tar.xz"
    manifest = "".join(
        f"{hashlib.sha256(data).hexdigest()}\t{len(data)}\t{name}\tx\n"
        for name, data in FILES.items()
    ).encode()
    with tarfile.open(path, "w:xz", format=tarfile.PAX_FORMAT) as tar:
        for name, data in [("MANIFEST.txt", manifest), *FILES.items()]:
            info = tarfile.TarInfo(f"{TOP}/{name}")
            info.size = len(data)
            if name == "README.txt":
                info.pax_headers = {"GNU.sparse.map": "0,1", "GNU.sparse.size": "1"}
            tar.addfile(info, io.BytesIO(data))
    with pytest.raises(sources.SourcesError, match="не обычный файл|sparse"):
        sources.check(path)


@pytest.mark.parametrize("kind", [tarfile.REGTYPE, tarfile.AREGTYPE])
def test_normal_tar_types_still_accepted(tmp_path: Path, sources: ModuleType, kind: bytes) -> None:
    path = _compressed(tmp_path, _raw_tar(kind))
    assert sources.check(path, top=TOP) == (hashlib.sha256(path.read_bytes()).hexdigest(), 3)


@pytest.mark.parametrize("tail", [b"", b"x" * 1024], ids=["padding", "tail"])
def test_total_expanded_limit_counts_padding_and_tail(
    tmp_path: Path, sources: ModuleType, monkeypatch: pytest.MonkeyPatch, tail: bytes
) -> None:
    raw = _raw_tar() + tail
    path = _compressed(tmp_path, raw)
    monkeypatch.setattr(sources, "UNPACKED_LIMIT", len(raw) - 1, raising=False)
    with pytest.raises(sources.SourcesError, match="распакованн.*предел"):
        sources.check(path)
    monkeypatch.setattr(sources, "UNPACKED_LIMIT", len(raw))
    assert sources.check(path)[1] == 3


def test_expanded_limit_also_counts_concatenated_xz(
    tmp_path: Path, sources: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    raw = _raw_tar()
    path = _compressed(tmp_path, raw)
    path.write_bytes(path.read_bytes() + lzma.compress(b"x" * 1024, preset=0))
    monkeypatch.setattr(sources, "UNPACKED_LIMIT", len(raw), raising=False)
    with pytest.raises(sources.SourcesError, match="распакованн.*предел"):
        sources.check(path)


def test_lzma_memory_limit_enforced(
    tmp_path: Path, sources: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    # preset0 uses a small dictionary; reducing the allowed memory avoids a large allocation.
    path = _compressed(tmp_path, _raw_tar())
    monkeypatch.setattr(sources, "LZMA_MEMLIMIT", 64 << 10, raising=False)
    with pytest.raises(sources.SourcesError):
        sources.check(path)
    monkeypatch.setattr(sources, "LZMA_MEMLIMIT", 128 << 20)
    assert sources.check(path)[1] == 3


def test_metadata_header_count_has_a_boundary(
    tmp_path: Path, sources: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _compressed(tmp_path, _raw_tar(longnames=4))
    monkeypatch.setattr(sources, "HEADER_COUNT_LIMIT", 3, raising=False)
    with pytest.raises(sources.SourcesError, match="число.*заголовк.*предел"):
        sources.check(path)
    monkeypatch.setattr(sources, "HEADER_COUNT_LIMIT", 4)
    assert sources.check(path)[1] == 3


def test_archive_changed_after_external_hash_rejected_before_parser(
    tmp_path: Path, sources: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _compressed(tmp_path, _raw_tar())
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    path.write_bytes(b"tampered after external hash")

    def parser_must_not_run(*args: Any, **kwargs: Any) -> None:
        pytest.fail("unverified compressed bytes reached the tar parser")

    monkeypatch.setattr(sources, "_check_members", parser_must_not_run)
    with pytest.raises(sources.SourcesError):
        sources.check(path, expected_sha256=expected)


def test_verified_snapshot_survives_original_file_change(
    tmp_path: Path, sources: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _compressed(tmp_path, _raw_tar())
    expected = hashlib.sha256(path.read_bytes()).hexdigest()
    original = sources._check_members

    def mutate_then_parse(*args: Any, **kwargs: Any) -> Any:
        path.write_bytes(b"changed after verification")
        return original(*args, **kwargs)

    monkeypatch.setattr(sources, "_check_members", mutate_then_parse)
    assert sources.check(path, expected_sha256=expected) == (expected, 3)
    assert path.read_bytes() == b"changed after verification"


@pytest.mark.parametrize("valid_signature", [False, True])
def test_release_verifies_signature_before_opening_archive(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    validate_globals: dict[str, object],
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    valid_signature: bool,
) -> None:
    add_appimage(assets, size=4096)
    monkeypatch.setitem(validate_globals, "APPIMAGE_MIN_SIZE", 100)
    if not valid_signature:
        (assets.dist / "SHA256SUMS.asc").write_bytes(b"not a signature")
    events: list[str] = []
    source_module = cast(Callable[[], ModuleType], validate_globals["_release_sources_module"])()
    verifier = cast(Any, validate_globals["Verifier"])
    original_verify = verifier.verify_detached
    original_open = source_module._open_regular

    def verified(*args: Any, **kwargs: Any) -> Any:
        result = original_verify(*args, **kwargs)
        events.append("signature-ok" if result.ok else "signature-failed")
        return result

    def opened(*args: Any, **kwargs: Any) -> Any:
        events.append("archive-open")
        assert events[0] == "signature-ok"
        return original_open(*args, **kwargs)

    monkeypatch.setattr(verifier, "verify_detached", verified)
    monkeypatch.setattr(source_module, "_open_regular", opened)
    checks = release_checks(validate, assets, capsys, expected_code=0 if valid_signature else 1)
    assert checks["signature"]["ok"] is valid_signature
    assert checks["sources"]["ok"] is valid_signature
    if valid_signature:
        assert events[0] == "signature-ok"
        assert "archive-open" in events
    else:
        assert events == ["signature-failed"]
    assert SOURCES in {entry.name for entry in assets.dist.iterdir()}
