import pytest

pytest.importorskip("jsonschema")

# Зависимость проверяется до импорта каталога (scripts/ci_require_imports.py).
# ruff: noqa: E402
import builtins
import io
import json
import os
import socket
from copy import deepcopy
from pathlib import Path
from types import ModuleType
from typing import IO, Any, cast
from unittest.mock import Mock

from astra_voice.models.catalog import CatalogError, load_builtin
from astra_voice.security.verify import Verifier, VerifyResult

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_ROOT = REPO_ROOT / "data"
KEYRING = DATA_ROOT / "keys/release.gpg"
MODEL_ID = "gigaam-v3-e2e-rnnt-int8"
REVISION = "322c3b29492673eb7d0b434bfa9dfb8653e34d02"
URL_PREFIX = f"/istupakov/gigaam-v3-onnx/resolve/{REVISION}/"


class StubVerifier(Verifier):
    """Позволяет проверять разбор без закрытого ключа и запуска gpgv."""

    def __init__(self, *, ok: bool = True) -> None:
        super().__init__("catalog", keyring=KEYRING)
        self.result = VerifyResult(ok=True) if ok else VerifyResult(ok=False, reason="отказ теста")

    def verify_detached(self, data: Path, sig: Path) -> VerifyResult:
        assert data.is_absolute()
        assert sig.is_absolute()
        return self.result


@pytest.fixture
def document() -> dict[str, Any]:
    return cast(dict[str, Any], json.loads((DATA_ROOT / "catalog.json").read_bytes()))


@pytest.fixture
def catalog_root(tmp_path: Path, document: dict[str, Any]) -> Path:
    (tmp_path / "catalog.schema.json").write_bytes((DATA_ROOT / "catalog.schema.json").read_bytes())
    (tmp_path / "catalog.json.sig").write_bytes(b"stub signature")
    write_document(tmp_path, document)
    return tmp_path


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Даже случайное обращение к сети должно падать немедленно."""
    forbidden = Mock(side_effect=AssertionError("сеть при разборе каталога"))
    for name in ("connect", "connect_ex", "sendto", "sendmsg"):
        monkeypatch.setattr(socket.socket, name, forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def write_document(root: Path, document: dict[str, Any]) -> None:
    (root / "catalog.json").write_text(json.dumps(document), encoding="utf-8")


def assert_rejected(root: Path, code: str, verifier: Verifier | None = None) -> None:
    with pytest.raises(CatalogError) as error:
        load_builtin(StubVerifier() if verifier is None else verifier, root=root)
    assert error.value.code == code


def assert_rejected_without_side_effects(
    root: Path, code: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """У6 / T-09: запрет записи и сетевых импортов действует именно при разборе."""
    before = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    original_import = builtins.__import__
    original_open = builtins.open
    original_io_open = io.open
    original_os_open = os.open
    network_modules = (
        "socket",
        "_socket",
        "requests",
        "urllib3",
        "urllib.request",
        "http",
        "httpx",
        "aiohttp",
        "astra_voice.net",
    )

    def guarded_import(name: str, *args: Any, **kwargs: Any) -> ModuleType:
        assert not any(name == item or name.startswith(item + ".") for item in network_modules), (
            name
        )
        return original_import(name, *args, **kwargs)

    def guarded_open(
        file: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> IO[Any]:
        assert not set(mode) & set("wax+"), "запись при разборе каталога"
        return original_open(file, mode, *args, **kwargs)

    def guarded_io_open(
        file: str | bytes | os.PathLike[str] | os.PathLike[bytes] | int,
        mode: str = "r",
        *args: Any,
        **kwargs: Any,
    ) -> IO[Any]:
        assert not set(mode) & set("wax+"), "запись при разборе каталога"
        return original_io_open(file, mode, *args, **kwargs)

    def guarded_os_open(
        path: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        *args: Any,
        **kwargs: Any,
    ) -> int:
        assert not flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND)
        return original_os_open(path, flags, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(builtins, "__import__", guarded_import)
        guard.setattr(builtins, "open", guarded_open)
        guard.setattr(io, "open", guarded_io_open)
        guard.setattr(os, "open", guarded_os_open)
        guard.setattr(
            os, "mkdir", Mock(side_effect=AssertionError("создание каталога при разборе"))
        )
        assert_rejected(root, code)

    after = {
        path.relative_to(root): path.read_bytes() for path in root.rglob("*") if path.is_file()
    }
    assert after == before
    assert set(root.iterdir()) == {root / name for name in before}


def test_builtin_catalog() -> None:
    result = load_builtin(Verifier("catalog", keyring=KEYRING), root=DATA_ROOT)
    assert result.serial == 1
    assert result.trust_epoch == 1
    assert result.revoked == ()
    assert len(result.entries) == 1
    entry = result.entries[0]
    assert result.entry(MODEL_ID) == entry
    assert entry.id == MODEL_ID
    assert entry.revision == REVISION
    assert entry.size_bytes == 226431968
    assert entry.min_ram_mb == 768
    assert entry.layout == "onnx-asr-gigaam-v3"
    assert entry.variant == "gigaam-v3-e2e-rnnt"
    assert entry.host == "huggingface.co"
    assert entry.recommended is True
    assert len(entry.files) == 6
    assert sum(file.size for file in entry.files) == entry.size_bytes
    assert all(file.url_path.startswith(URL_PREFIX) for file in entry.files)


def test_builtin_catalog_relative_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(REPO_ROOT)
    result = load_builtin(
        Verifier("catalog", keyring=Path("data/keys/release.gpg")), root=Path("data")
    )
    assert result.serial == 1
    assert result == load_builtin(Verifier("catalog", keyring=KEYRING), root=DATA_ROOT)


@pytest.mark.parametrize("has_signature", [True, False], ids=["changed-byte", "missing-signature"])
def test_signature_checked_before_json(
    catalog_root: Path, monkeypatch: pytest.MonkeyPatch, has_signature: bool
) -> None:
    original = (DATA_ROOT / "catalog.json").read_bytes()
    assert original.startswith(b"{")
    broken = b"!" + original[1:]
    assert sum(left != right for left, right in zip(original, broken, strict=True)) == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(broken)
    (catalog_root / "catalog.json").write_bytes(broken)
    if has_signature:
        (catalog_root / "catalog.json.sig").write_bytes(
            (DATA_ROOT / "catalog.json.sig").read_bytes()
        )
    else:
        (catalog_root / "catalog.json.sig").unlink()

    parse = Mock(side_effect=AssertionError("JSON разобран до проверки подписи"))
    with monkeypatch.context() as guard:
        guard.setattr(json, "loads", parse)
        assert_rejected(catalog_root, "bad-signature", Verifier("catalog", keyring=KEYRING))
    parse.assert_not_called()
    # При разрешённом разборе эти же байты дают другой код ошибки.
    (catalog_root / "catalog.json.sig").write_bytes(b"stub signature")
    assert_rejected(catalog_root, "bad-schema")


def test_verifier_refusal(catalog_root: Path) -> None:
    assert_rejected(catalog_root, "bad-signature", StubVerifier(ok=False))


def test_too_large_before_json(catalog_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (catalog_root / "catalog.json").write_bytes(b" " * ((1 << 20) + 1))
    parse = Mock(side_effect=AssertionError("разбор JSON сверх лимита"))
    monkeypatch.setattr(json, "loads", parse)
    assert_rejected(catalog_root, "too-large")
    parse.assert_not_called()


def test_duplicate_json_keys(catalog_root: Path, document: dict[str, Any]) -> None:
    text = json.dumps(document)
    assert '"serial": 1' in text
    text = text.replace('"serial": 1', '"serial": 1, "serial": 2', 1)
    (catalog_root / "catalog.json").write_text(text, encoding="utf-8")
    assert_rejected(catalog_root, "bad-schema")


@pytest.mark.parametrize(
    ("location", "field", "value"),
    [
        ("root", "extra", True),
        ("root", "manifest_version", 2),
        ("model", "layout", "unknown"),
        ("model", "host", "example.org"),
        ("model", "variant", "unknown"),
        ("model", "files", []),
        ("model", "size_bytes", 1),
        ("file", "sha256", "a" * 63),
        ("file", "size", 0),
    ],
    ids=["extra", "version", "layout", "host", "variant", "empty-files", "total", "sha256", "size"],
)
def test_bad_schema(
    catalog_root: Path, document: dict[str, Any], location: str, field: str, value: object
) -> None:
    model = document["models"][0]
    target = {"root": document, "model": model, "file": model["files"][0]}[location]
    target[field] = value
    write_document(catalog_root, document)
    assert_rejected(catalog_root, "bad-schema")


def test_missing_required_field(catalog_root: Path, document: dict[str, Any]) -> None:
    del document["serial"]
    write_document(catalog_root, document)
    assert_rejected(catalog_root, "bad-schema")


def test_duplicate_model_id(catalog_root: Path, document: dict[str, Any]) -> None:
    document["models"].append(deepcopy(document["models"][0]))
    write_document(catalog_root, document)
    assert_rejected(catalog_root, "bad-schema")


def test_duplicate_file_path(catalog_root: Path, document: dict[str, Any]) -> None:
    files = document["models"][0]["files"]
    files[1]["path"] = files[0]["path"]
    write_document(catalog_root, document)
    assert_rejected(catalog_root, "bad-schema")


@pytest.mark.parametrize(
    "path",
    [
        "../x.onnx",
        "/etc/passwd",
        "a/../../x",
        "a//b",
        "./x",
        "a\\b",
        "C:/x",
        "/".join(["a" * 205] * 5),  # Больше 1024 байт, каждый компонент короче 255.
        "a" * 256,
        "a\x00b",
        "a\x1fb",
    ],
    ids=[
        "parent",
        "absolute",
        "nested-parent",
        "empty-part",
        "dot",
        "backslash",
        "drive",
        "long-path",
        "long-component",
        "nul",
        "control",
    ],
)
def test_unsafe_file_path(
    catalog_root: Path, document: dict[str, Any], monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    document["models"][0]["files"][0]["path"] = path
    write_document(catalog_root, document)
    assert_rejected_without_side_effects(catalog_root, "bad-path", monkeypatch)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("id", "../evil"),
        ("id", "a/b"),
        ("id", ""),
        ("id", "a" * 65),
        ("revision", "."),
        ("revision", ".."),
        ("revision", "a b"),
    ],
    ids=[
        "parent-id",
        "slash-id",
        "empty-id",
        "long-id",
        "dot-revision",
        "parent-revision",
        "space",
    ],
)
def test_unsafe_identifier(
    catalog_root: Path,
    document: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    document["models"][0][field] = value
    write_document(catalog_root, document)
    assert_rejected_without_side_effects(catalog_root, "bad-schema", monkeypatch)


@pytest.mark.parametrize(
    "url_path",
    [URL_PREFIX.lstrip("/") + "x.onnx", URL_PREFIX + "../x.onnx"],
    ids=["relative-url", "parent-url"],
)
def test_unsafe_url_path(
    catalog_root: Path, document: dict[str, Any], monkeypatch: pytest.MonkeyPatch, url_path: str
) -> None:
    document["models"][0]["files"][0]["url_path"] = url_path
    write_document(catalog_root, document)
    assert_rejected_without_side_effects(catalog_root, "bad-path", monkeypatch)


def test_entry_and_revocation(catalog_root: Path, document: dict[str, Any]) -> None:
    document["revoked"] = [{"model_id": MODEL_ID, "revision": REVISION, "reason": "тест отзыва"}]
    write_document(catalog_root, document)
    result = load_builtin(StubVerifier(), root=catalog_root)
    assert result.entry("unknown") is None
    assert result.entry(MODEL_ID) == result.entries[0]
    assert len(result.revoked) == 1
    assert result.revoked[0].reason == "тест отзыва"
    assert result.is_revoked(MODEL_ID, REVISION) is True
    assert result.is_revoked(MODEL_ID, "another-revision") is False
    assert result.is_revoked("another-model", REVISION) is False
    assert result.is_revoked("another-model", "another-revision") is False
