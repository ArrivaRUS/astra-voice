"""Офлайн-гейт выпуска: временная история Git и подписи, связанные с байтами."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from astra_voice.core.release_identity import RELEASES_REPO, schema_id
from astra_voice.security.verify import Verifier, VerifyResult

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "catalog_publish_check", ROOT / "scripts/catalog_publish_check.py"
)
assert SPEC and SPEC.loader
gate = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(gate)


def signature(raw: bytes) -> bytes:
    """Как test_catalog_effective: тестовый подписант без GPG-агента / сокетов."""
    return b"test-signature:" + hashlib.sha256(raw).digest()


class BoundVerifier(Verifier):
    def verify_detached(
        self, data: Path, sig: Path, *, cancel: Callable[[], bool] | None = None
    ) -> VerifyResult:
        assert self.purpose == "catalog"
        assert self.keyring == data.parent / "keys/release.gpg"
        assert data.is_absolute() and sig.is_absolute()
        return VerifyResult(ok=sig.read_bytes() == signature(data.read_bytes()))


@dataclass
class Release:
    root: Path
    document: dict[str, Any]

    def git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.root, check=True, capture_output=True)

    def write(self, *, suffix: bytes = b"\n") -> None:
        raw = json.dumps(self.document, ensure_ascii=False).encode() + suffix
        (self.root / "data/catalog.json").write_bytes(raw)
        (self.root / "data/catalog.json.sig").write_bytes(signature(raw))

    def dist(self) -> Path:
        dist = self.root / "dist"
        dist.mkdir()
        name = f"catalog-{schema_id((self.root / 'data/catalog.schema.json').read_bytes())}.json"
        for source, target in (("catalog.json", name), ("catalog.json.sig", name + ".sig")):
            shutil.copyfile(self.root / "data" / source, dist / target)
        return dist


@pytest.fixture
def release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Release:
    root = tmp_path / "repo"
    (root / "data/keys").mkdir(parents=True)
    shutil.copyfile(ROOT / "data/catalog.schema.json", root / "data/catalog.schema.json")
    (root / "data/keys/release.gpg").write_bytes(b"test keyring")
    document = json.loads((ROOT / "data/catalog.json").read_bytes())
    document.update(trust_epoch=2, serial=10)
    scenario = Release(root, document)
    scenario.write()
    scenario.git("init")
    scenario.git("config", "user.name", "Catalog Test")
    scenario.git("config", "user.email", "test@example.invalid")
    scenario.git("config", "commit.gpgsign", "false")
    scenario.git("config", "tag.gpgsign", "false")
    scenario.git("add", "data")
    scenario.git("commit", "-m", "previous catalog")
    scenario.git("tag", "v0.1.6")
    scenario.git("commit", "--allow-empty", "-m", "release with gate")
    monkeypatch.setattr(gate, "ROOT", root)
    monkeypatch.setattr(gate, "Verifier", BoundVerifier)
    monkeypatch.delenv("GITHUB_REPOSITORY", raising=False)
    return scenario


@pytest.mark.parametrize(
    ("epoch", "serial", "message"),
    [(2, 9, "Откат каталога"), (2, 10, "Равная пара"), (1, 100, "trust_epoch убыла")],
)
def test_rejects_rollback(
    release: Release, capsys: pytest.CaptureFixture[str], epoch: int, serial: int, message: str
) -> None:
    release.document.update(trust_epoch=epoch, serial=serial)
    release.write(suffix=b"\n\n")
    assert gate.main([]) == 1
    assert message in capsys.readouterr().err


def test_identical_bytes_allowed(release: Release, capsys: pytest.CaptureFixture[str]) -> None:
    assert gate.main([]) == 0
    output = capsys.readouterr().out
    assert "Гейт каталога пройден" in output
    assert "прошлый тег v0.1.6" in output


@pytest.mark.parametrize(("epoch", "serial"), [(2, 11), (3, 1)])
def test_strictly_greater_pair_allowed(release: Release, epoch: int, serial: int) -> None:
    release.document.update(trust_epoch=epoch, serial=serial)
    release.write()
    assert gate.main([]) == 0


def test_explicit_previous_tag(release: Release, capsys: pytest.CaptureFixture[str]) -> None:
    release.git("tag", "v0.1.7")
    assert gate.main(["--previous-tag", "v0.1.6"]) == 0
    assert "прошлый тег v0.1.6" in capsys.readouterr().out


def test_no_previous_tag_is_error(release: Release, capsys: pytest.CaptureFixture[str]) -> None:
    release.git("tag", "-d", "v0.1.6")
    assert gate.main([]) == 1
    assert "Не найден прошлый тег" in capsys.readouterr().err


def test_tag_on_head_is_not_previous(release: Release) -> None:
    release.git("tag", "v1.0.0")
    release.document["serial"] = 11
    release.write()
    release.git("add", "data")
    release.git("commit", "--amend", "--no-edit")
    release.git("tag", "-f", "v1.0.0")
    assert gate.main([]) == 0


def test_missing_explicit_tag_is_error(
    release: Release, capsys: pytest.CaptureFixture[str]
) -> None:
    assert gate.main(["--previous-tag", "v9.9.9"]) == 1
    assert "Не удалось прочитать Git" in capsys.readouterr().err


def test_matching_dist_allowed(release: Release) -> None:
    assert gate.main(["--dist", str(release.dist())]) == 0


@pytest.mark.parametrize("suffix", [".json", ".json.sig"])
def test_dist_mismatch_rejected(
    release: Release, capsys: pytest.CaptureFixture[str], suffix: str
) -> None:
    dist = release.dist()
    next(dist.glob(f"*{suffix}")).write_bytes(b"different bytes")
    assert gate.main(["--dist", str(dist)]) == 1
    assert "побайтно не совпадает" in capsys.readouterr().err


def test_bad_signature_rejected(release: Release, capsys: pytest.CaptureFixture[str]) -> None:
    (release.root / "data/catalog.json.sig").write_bytes(b"signature for other bytes")
    assert gate.main([]) == 1
    assert "подпись каталога" in capsys.readouterr().err


def test_schema_mismatch_rejected(release: Release, capsys: pytest.CaptureFixture[str]) -> None:
    schema = release.root / "data/catalog.schema.json"
    schema.write_bytes(schema.read_bytes() + b"\n")
    assert gate.main([]) == 1
    assert "схему каталога" in capsys.readouterr().err


def test_repository_mismatch_rejected(
    release: Release, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("GITHUB_REPOSITORY", "other/repo")
    assert gate.main([]) == 1
    assert "не совпадает с RELEASES_REPO" in capsys.readouterr().err
    monkeypatch.setenv("GITHUB_REPOSITORY", RELEASES_REPO)
    assert gate.main([]) == 0
