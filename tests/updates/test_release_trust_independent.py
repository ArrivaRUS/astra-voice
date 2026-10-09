"""Independent arch/appimage §6 / T-178 trust tests using real GPG signatures.

HTTP alone is replaced; Verifier, signature checks, checksum checks, snapshots
and metadata parsing are real. Test signing keys live only in a temporary home.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import shutil
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from astra_voice.core.policy import Policy
from astra_voice.core.settings import Settings
from astra_voice.net.gate import NetworkGate
from astra_voice.net.http import HttpClient
from astra_voice.security.verify import Verifier
from astra_voice.updates.release import (
    MAX_LATEST_BYTES,
    ReleaseMetadata,
    ReleaseMetadataError,
    Track,
)

# Runtime-only reuse avoids giving test_verify.py a second mypy module name.
_Gpg = importlib.import_module("unit.test_verify")._Gpg

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        shutil.which("gpg") is None or shutil.which("gpgv") is None,
        reason="real GPG trust tests need gpg and gpgv",
    ),
]

REPO = "https://github.com/ArrivaRUS/astra-voice"
META_NAMES = ("SHA256SUMS", "SHA256SUMS.asc", "latest.json")


@dataclass
class SigningKeys:
    gpg: Any
    root: Path
    master: str
    subkey: str
    foreign: str
    keyring: Path

    def sign(self, sums: bytes, *, foreign: bool = False) -> bytes:
        source = self.root / "sign-input"
        signature = self.root / "sign-output.asc"
        source.write_bytes(sums)
        key = self.foreign if foreign else self.subkey
        self.gpg.sign(source, signature, key + "!")
        return signature.read_bytes()

    def verifier(self) -> Verifier:
        return Verifier("release", self.keyring, pinned=frozenset({self.master}))


@pytest.fixture(scope="module")
def signing_keys(tmp_path_factory: pytest.TempPathFactory) -> Iterator[SigningKeys]:
    root = tmp_path_factory.mktemp("independent-release-gpg")
    gpg = _Gpg(root / "gnupg")
    try:
        master = gpg.gen("Independent Release Test <release@test.invalid>")
        subkey = gpg.add_subkey(master)
        foreign = gpg.gen("Independent Foreign Test <foreign@test.invalid>")
        keyring = root / "both-public.gpg"
        first = gpg.export(master, root / "master-public.gpg").read_bytes()
        second = gpg.export(foreign, root / "foreign-public.gpg").read_bytes()
        keyring.write_bytes(first + second)
        yield SigningKeys(gpg, root, master, subkey, foreign, keyring)
    finally:
        gpg.kill_agent()


def manifest(version: str = "0.2.1") -> dict[str, Any]:
    return {
        "schema": 2,
        "version": version,
        "published_at": "2026-10-15T09:00:00Z",
        "min_astra": "1.8",
        "release_url": f"{REPO}/releases/tag/v{version}",
        "artifacts": {
            "deb": {
                "name": f"astra-voice_{version}_amd64.deb",
                "sha256": "a" * 64,
                "size": 120,
            },
            "appimage": {
                "name": f"Astra_Voice-{version}-x86_64.AppImage",
                "sha256": "b" * 64,
                "size": 240,
            },
        },
    }


def bundle(
    keys: SigningKeys,
    data: dict[str, Any] | None = None,
    *,
    asset_version: str | None = None,
    foreign: bool = False,
    raw_latest: bytes | None = None,
) -> dict[str, bytes]:
    data = manifest() if data is None else data
    latest = json.dumps(data).encode() if raw_latest is None else raw_latest
    version = data["version"] if asset_version is None else asset_version
    sums = (
        f"{hashlib.sha256(latest).hexdigest()}  latest.json\n"
        f"{'a' * 64}  astra-voice_{version}_amd64.deb\n"
        f"{'b' * 64}  Astra_Voice-{version}-x86_64.AppImage\n"
    ).encode()
    return {
        "SHA256SUMS": sums,
        "SHA256SUMS.asc": keys.sign(sums, foreign=foreign),
        "latest.json": latest,
    }


class HttpBody:
    def __init__(self, raw: bytes) -> None:
        self.status = 200
        self.raw = raw
        self.closed = False
        self.limit = 0

    def __enter__(self) -> HttpBody:
        return self

    def __exit__(self, *args: object) -> None:
        self.closed = True

    def iter_chunks(self, *, limit: int) -> Iterator[bytes]:
        self.limit = limit
        # Deliberately ignore the requested limit: consumer must enforce bounds.
        yield self.raw


@dataclass
class MetadataRig:
    service: ReleaseMetadata
    files: dict[str, bytes]
    calls: list[tuple[str, object]]
    responses: list[HttpBody]
    staging: Path


def rig(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    keys: SigningKeys,
    files: dict[str, bytes],
    *,
    default_staging: bool = False,
) -> MetadataRig:
    client = HttpClient(NetworkGate(Settings(), Policy()), user_agent="independent-release-test")
    calls: list[tuple[str, object]] = []
    responses: list[HttpBody] = []

    def stream(url: str, **kwargs: object) -> HttpBody:
        calls.append((url, kwargs["kind"]))
        response = HttpBody(files[url.rsplit("/", 1)[-1]])
        responses.append(response)
        return response

    monkeypatch.setattr(client, "get_stream", stream)
    staging = tmp_path / "own-staging"
    service = ReleaseMetadata(
        client, keys.verifier(), staging_dir=None if default_staging else staging
    )
    return MetadataRig(service, files, calls, responses, staging)


def write_cache(directory: Path, files: dict[str, bytes]) -> None:
    directory.mkdir(mode=0o700)
    for name, body in files.items():
        path = directory / name
        path.write_bytes(body)
        path.chmod(0o600)


def snapshot(directory: Path) -> dict[str, tuple[bytes, int]]:
    return {
        item.name: (item.read_bytes(), item.stat().st_mode & 0o777) for item in directory.iterdir()
    }


@pytest.mark.parametrize("track", ["deb", "appimage"])
@pytest.mark.parametrize("tag", ["0.2.1", "v0.2.1"])
@pytest.mark.parametrize("kind", ["check_app", "check_app_manual"])
def test_real_signed_metadata_accepts_pinned_master_subkey_both_tracks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: SigningKeys,
    track: Track,
    tag: str,
    kind: Any,
) -> None:
    files = bundle(signing_keys)
    test = rig(tmp_path, monkeypatch, signing_keys, files)
    verified = test.service.fetch(tag, "0.1.1~dev9", track, kind=kind)
    expected_name = manifest()["artifacts"][track]["name"]
    assert verified.raw_tag == tag and verified.version == "0.2.1"
    assert verified.artifact.track == track and verified.artifact.name == expected_name
    assert verified.artifact.url == f"{REPO}/releases/download/{tag}/{expected_name}"
    assert verified.release_url == f"{REPO}/releases/tag/{tag}"
    assert (verified.sums, verified.signature, verified.latest) == tuple(
        files[name] for name in META_NAMES
    )
    assert test.calls == [(f"{REPO}/releases/download/{tag}/{name}", kind) for name in META_NAMES]
    assert all(response.closed for response in test.responses)
    assert test.staging.stat().st_mode & 0o777 == 0o700
    assert list(test.staging.iterdir()) == []


@pytest.mark.parametrize("case", ["foreign-key", "bad-signature", "changed-sums", "changed-latest"])
def test_real_gpg_rejects_untrusted_or_tampered_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: SigningKeys,
    case: str,
) -> None:
    files = bundle(signing_keys, foreign=case == "foreign-key")
    if case == "bad-signature":
        files["SHA256SUMS.asc"] = b"not an OpenPGP signature"
    elif case == "changed-sums":
        files["SHA256SUMS"] += b"0" * 64 + b"  another-file\n"
    elif case == "changed-latest":
        files["latest.json"] += b" "
    test = rig(tmp_path, monkeypatch, signing_keys, files)
    with pytest.raises(ReleaseMetadataError) as error:
        test.service.fetch("v0.2.1", "0.1.0", "deb")
    assert error.value.code == (
        "hash-mismatch" if case == "changed-latest" else "signature-invalid"
    )
    assert list(test.staging.iterdir()) == []
    assert all(response.closed for response in test.responses)


@pytest.mark.parametrize("track", ["deb", "appimage"])
@pytest.mark.parametrize("case", ["old-assets", "old-entire-release"])
def test_t178_real_valid_signature_does_not_authorize_replayed_assets_or_release(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: SigningKeys,
    track: Track,
    case: str,
) -> None:
    files = (
        bundle(signing_keys, asset_version="0.2.0")
        if case == "old-assets"
        else bundle(signing_keys, manifest("0.2.0"))
    )
    test = rig(tmp_path, monkeypatch, signing_keys, files)
    with pytest.raises(ReleaseMetadataError, match="metadata-invalid"):
        test.service.fetch("v0.2.1", "0.1.0", track)
    assert list(test.staging.iterdir()) == []


@pytest.mark.parametrize("installed", ["0.2.1", "0.2.1+build", "0.2.2"])
def test_same_or_older_release_never_fetches_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: SigningKeys,
    installed: str,
) -> None:
    test = rig(tmp_path, monkeypatch, signing_keys, bundle(signing_keys))
    with pytest.raises(ReleaseMetadataError, match="version-not-newer"):
        test.service.fetch("v0.2.1", installed, "deb")
    assert test.calls == [] and not test.staging.exists()


@pytest.mark.parametrize(
    "case",
    [
        "schema-bool",
        "schema-old",
        "duplicate-key",
        "bad-artifact-name",
        "bool-size",
        "artifact-hash-disagrees",
        "foreign-release-url",
        "invalid-date",
        "unsupported-platform",
    ],
)
def test_even_genuinely_signed_invalid_schema_cannot_be_advertised(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: SigningKeys,
    case: str,
) -> None:
    data = manifest()
    raw = None
    if case == "schema-bool":
        data["schema"] = True
    elif case == "schema-old":
        data["schema"] = 1
    elif case == "duplicate-key":
        raw = json.dumps(data).replace('"schema": 2', '"schema": 2, "schema": 2').encode()
    elif case == "bad-artifact-name":
        data["artifacts"]["deb"]["name"] = "../../foreign.deb"
    elif case == "bool-size":
        data["artifacts"]["deb"]["size"] = True
    elif case == "artifact-hash-disagrees":
        data["artifacts"]["deb"]["sha256"] = "c" * 64
    elif case == "foreign-release-url":
        data["release_url"] = "https://example.invalid/release"
    elif case == "invalid-date":
        data["published_at"] = "2026-02-30T09:00:00Z"
    elif case == "unsupported-platform":
        data["min_astra"] = "1.9"
    test = rig(tmp_path, monkeypatch, signing_keys, bundle(signing_keys, data, raw_latest=raw))
    with pytest.raises(ReleaseMetadataError) as error:
        test.service.fetch("v0.2.1", "0.1.0", "deb")
    assert error.value.code == (
        "unsupported-platform" if case == "unsupported-platform" else "metadata-invalid"
    )
    assert list(test.staging.iterdir()) == []


@pytest.mark.parametrize("track", ["deb", "appimage"])
def test_real_local_cache_is_reverified_without_http_and_without_changing_input(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: SigningKeys,
    track: Track,
) -> None:
    files = bundle(signing_keys)
    cache = tmp_path / "foreign-cache"
    write_cache(cache, files)
    cache.chmod(0o755)  # Consumer must not modify a supplied directory's mode.
    before = snapshot(cache)
    test = rig(tmp_path, monkeypatch, signing_keys, files)
    verified = test.service.validate_local(cache, "v0.2.1", "0.1.0", track)
    assert verified.signature == files["SHA256SUMS.asc"]
    assert test.calls == []
    assert snapshot(cache) == before and cache.stat().st_mode & 0o777 == 0o755
    (cache / "latest.json").write_bytes(files["latest.json"] + b" ")
    tampered = snapshot(cache)
    with pytest.raises(ReleaseMetadataError, match="hash-mismatch"):
        test.service.validate_local(cache, "v0.2.1", "0.1.0", track)
    assert test.calls == [] and snapshot(cache) == tampered
    assert list(test.staging.iterdir()) == []


@pytest.mark.parametrize(
    "case", ["symlink-file", "symlink-parent", "missing-file", "oversized-file"]
)
def test_bad_local_cache_leaves_foreign_files_untouched_without_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: SigningKeys,
    case: str,
) -> None:
    files = bundle(signing_keys)
    cache = tmp_path / "foreign-cache"
    write_cache(cache, files)
    sentinel = tmp_path / "foreign-sentinel"
    sentinel.write_bytes(b"foreign data")
    sentinel.chmod(0o644)
    source = cache
    if case == "symlink-file":
        (cache / "latest.json").unlink()
        (cache / "latest.json").symlink_to(sentinel)
    elif case == "symlink-parent":
        source = tmp_path / "cache-alias"
        source.symlink_to(cache, target_is_directory=True)
    elif case == "missing-file":
        (cache / "SHA256SUMS.asc").unlink()
    elif case == "oversized-file":
        (cache / "latest.json").write_bytes(b" " * (MAX_LATEST_BYTES + 1))
    before = snapshot(cache)
    test = rig(tmp_path, monkeypatch, signing_keys, files)
    with pytest.raises(ReleaseMetadataError):
        test.service.validate_local(source, "v0.2.1", "0.1.0", "deb")
    assert test.calls == [] and snapshot(cache) == before
    assert sentinel.read_bytes() == b"foreign data" and sentinel.stat().st_mode & 0o777 == 0o644
    assert list(test.staging.iterdir()) == []


def test_default_staging_xdg_symlink_fails_before_foreign_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: SigningKeys,
) -> None:
    target = tmp_path / "foreign-target"
    target.mkdir(mode=0o755)
    sentinel = target / "sentinel"
    sentinel.write_bytes(b"untouched")
    xdg = tmp_path / "xdg-alias"
    xdg.symlink_to(target, target_is_directory=True)
    monkeypatch.setenv("XDG_CACHE_HOME", str(xdg))
    test = rig(tmp_path, monkeypatch, signing_keys, bundle(signing_keys), default_staging=True)
    before = snapshot(target)
    with pytest.raises(ReleaseMetadataError, match="path-unsafe"):
        test.service.fetch("v0.2.1", "0.1.0", "deb")
    assert test.calls == [] and snapshot(target) == before
    assert target.stat().st_mode & 0o777 == 0o755


def test_cancel_before_fetch_or_local_validation_has_no_side_effects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: SigningKeys,
) -> None:
    test = rig(tmp_path, monkeypatch, signing_keys, bundle(signing_keys))
    cancel = threading.Event()
    cancel.set()
    with pytest.raises(ReleaseMetadataError, match="cancelled"):
        test.service.fetch("v0.2.1", "0.1.0", "deb", cancel=cancel)
    with pytest.raises(ReleaseMetadataError, match="cancelled"):
        test.service.validate_local(
            tmp_path / "absent-cache", "v0.2.1", "0.1.0", "deb", cancel=cancel
        )
    assert test.calls == [] and not test.staging.exists()


def test_http_that_ignores_metadata_limit_still_cannot_fill_cache(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    signing_keys: SigningKeys,
) -> None:
    files = bundle(signing_keys)
    files["latest.json"] = b" " * (MAX_LATEST_BYTES + 1)
    test = rig(tmp_path, monkeypatch, signing_keys, files)
    with pytest.raises(ReleaseMetadataError, match="metadata-too-large"):
        test.service.fetch("v0.2.1", "0.1.0", "deb")
    assert all(response.closed for response in test.responses)
    assert list(test.staging.iterdir()) == []
    assert test.responses[-1].limit == MAX_LATEST_BYTES + 1
