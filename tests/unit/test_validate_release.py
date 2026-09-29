"""Офлайн-проверка ассетов релиза с временным ключом и настоящим deb."""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import runpy
import shutil
import subprocess
import sys
import tarfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

import astra_voice.security.verify as verify_module

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class ReleaseAssets:
    dist: Path
    keyring: Path
    home: Path
    fingerprint: str

    def __iter__(self) -> Iterator[Path]:
        yield self.dist
        yield self.keyring


@pytest.fixture
def validate_globals(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    monkeypatch.setattr(sys, "path", sys.path.copy())
    namespace = runpy.run_path(str(ROOT / "tools/validate"))
    return cast(dict[str, object], namespace["main"].__globals__)


@pytest.fixture
def validate(validate_globals: dict[str, object]) -> Callable[[list[str]], int]:
    return cast(Callable[[list[str]], int], validate_globals["main"])


@pytest.fixture
def assets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[ReleaseAssets]:
    for command in ("gpg", "gpgv", "gpgconf", "dpkg-deb"):
        if shutil.which(command) is None:
            pytest.skip(f"нет {command}")
    dist = tmp_path / "dist"
    dist.mkdir()
    package = tmp_path / "package" / "DEBIAN"
    package.mkdir(parents=True)
    (package / "control").write_text(
        "Package: astra-voice\nVersion: 0.1.0\nArchitecture: amd64\n"
        "Maintainer: Test <t@example.invalid>\nDescription: Test package\n",
        encoding="utf-8",
    )
    deb = dist / "astra-voice_0.1.0_amd64.deb"
    subprocess.run(
        ["dpkg-deb", "--build", str(package.parent), str(deb)], check=True, capture_output=True
    )
    (dist / "sbom.cdx.json").write_text("{}\n", encoding="utf-8")
    (dist / "INSTALL-ADMIN.md").write_text("# Test\n", encoding="utf-8")
    generator = runpy.run_path(str(ROOT / "scripts/release_latest_json.py"))["generate"]
    generator(dist, "0.1.0", "2026-09-30T12:00:00Z")
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}
    generated = subprocess.run(
        [
            "gpg",
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-gen-key",
            "Test <t@example.invalid>",
            "ed25519",
            "sign",
            "never",
        ],
        env=env,
        check=False,
        capture_output=True,
        timeout=30,
    )
    if generated.returncode != 0:
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=env, check=False, timeout=10)
        pytest.skip("генерация GPG-ключа невозможна")
    listing = subprocess.run(
        ["gpg", "--batch", "--with-colons", "--list-keys"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    fingerprint = next(
        line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")
    )
    monkeypatch.setattr(verify_module, "PINNED_FINGERPRINTS", frozenset({fingerprint}))
    monkeypatch.setattr(verify_module, "REVOKED_FINGERPRINTS", frozenset())
    keyring = tmp_path / "release.gpg"
    with keyring.open("wb") as output:
        subprocess.run(
            ["gpg", "--batch", "--export"], env=env, check=True, stdout=output, timeout=30
        )
    shutil.copy2(keyring, dist / "release.gpg")
    names = (deb.name, "sbom.cdx.json", "INSTALL-ADMIN.md", "latest.json", "release.gpg")
    (dist / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256((dist / name).read_bytes()).hexdigest()}  {name}\n" for name in names
        ),
        encoding="utf-8",
    )
    subprocess.run(
        [
            "gpg",
            "--batch",
            "--yes",
            "--detach-sign",
            "--armor",
            "--output",
            str(dist / "SHA256SUMS.asc"),
            str(dist / "SHA256SUMS"),
        ],
        env=env,
        check=True,
        capture_output=True,
        timeout=30,
    )
    try:
        yield ReleaseAssets(dist, keyring, home, fingerprint)
    finally:
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=env, check=False, timeout=10)


def invoke(validate: Callable[[list[str]], int], dist: Path, keyring: Path, *extra: str) -> int:
    return validate(
        ["release", "--version", "0.1.0", "--dir", str(dist), "--keyring", str(keyring), *extra]
    )


def test_release_valid(assets: ReleaseAssets, validate: Callable[[list[str]], int]) -> None:
    assert invoke(validate, *assets) == 0
    dist, keyring = assets
    assert (
        validate(["release", "--version", "v0.1.0", "--dir", str(dist), "--keyring", str(keyring)])
        == 0
    )


def test_release_relative_paths_from_other_cwd(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    work = assets.dist.parent / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    assert (
        validate(
            [
                "release",
                "--version",
                "0.1.0",
                "--dir",
                "../dist",
                "--keyring",
                "../release.gpg",
            ]
        )
        == 0
    )


def test_release_missing_admin(assets: ReleaseAssets, validate: Callable[[list[str]], int]) -> None:
    dist, keyring = assets
    (dist / "INSTALL-ADMIN.md").unlink()
    assert invoke(validate, dist, keyring) == 1


def test_release_missing_keyring_asset(
    assets: ReleaseAssets, validate: Callable[[list[str]], int]
) -> None:
    dist, keyring = assets
    (dist / "release.gpg").unlink()
    assert invoke(validate, dist, keyring) == 1


def test_release_different_keyring_asset(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    dist, keyring = assets
    (dist / "release.gpg").write_bytes(b"different keyring")
    assert invoke(validate, dist, keyring, "--json") == 1
    checks = json.loads(capsys.readouterr().out)["checks"]
    assert next(check for check in checks if check["name"] == "release.gpg")["ok"] is False


def test_release_corrupt_deb(assets: ReleaseAssets, validate: Callable[[list[str]], int]) -> None:
    dist, keyring = assets
    deb = next(dist.glob("*.deb"))
    deb.write_bytes(deb.read_bytes() + b"x")
    assert invoke(validate, dist, keyring) == 1


def test_release_other_keyring(
    assets: ReleaseAssets, validate: Callable[[list[str]], int], tmp_path: Path
) -> None:
    dist, _ = assets
    home = tmp_path / "other-gnupg"
    home.mkdir(mode=0o700)
    env = {**os.environ, "GNUPGHOME": str(home)}
    generated = subprocess.run(
        [
            "gpg",
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-gen-key",
            "Other <other@example.invalid>",
            "ed25519",
            "sign",
            "never",
        ],
        env=env,
        check=False,
        capture_output=True,
        timeout=30,
    )
    if generated.returncode:
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=env, check=False, timeout=10)
        pytest.skip("генерация второго GPG-ключа невозможна")
    other = tmp_path / "other.gpg"
    try:
        with other.open("wb") as output:
            subprocess.run(
                ["gpg", "--batch", "--export"], env=env, check=True, stdout=output, timeout=30
            )
        assert invoke(validate, dist, other) == 1
    finally:
        subprocess.run(["gpgconf", "--kill", "gpg-agent"], env=env, check=False, timeout=10)


def test_release_wrong_version(assets: ReleaseAssets, validate: Callable[[list[str]], int]) -> None:
    dist, keyring = assets
    assert (
        validate(["release", "--version", "0.2.0", "--dir", str(dist), "--keyring", str(keyring)])
        == 1
    )


def test_release_wrong_latest_sha(
    assets: ReleaseAssets, validate: Callable[[list[str]], int]
) -> None:
    dist, keyring = assets
    latest = json.loads((dist / "latest.json").read_text(encoding="utf-8"))
    latest["artifacts"]["deb"]["sha256"] = "0" * 64
    (dist / "latest.json").write_text(json.dumps(latest), encoding="utf-8")
    assert invoke(validate, dist, keyring) == 1


def test_release_missing_dir(tmp_path: Path, validate: Callable[[list[str]], int]) -> None:
    keyring = tmp_path / "release.gpg"
    keyring.write_bytes(b"")
    assert invoke(validate, tmp_path / "absent", keyring) == 2


def test_release_json(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert invoke(validate, *assets, "--json") == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True


def test_latest_generator(tmp_path: Path) -> None:
    generator = runpy.run_path(str(ROOT / "scripts/release_latest_json.py"))["generate"]
    with pytest.raises(ValueError, match="найдено 0"):
        generator(tmp_path, "0.1.0")
    for arch in ("amd64", "arm64"):
        (tmp_path / f"astra-voice_0.1.0_{arch}.deb").write_bytes(arch.encode())
    with pytest.raises(ValueError, match="найдено 2"):
        generator(tmp_path, "0.1.0")
    (tmp_path / "astra-voice_0.1.0_arm64.deb").unlink()
    data = generator(tmp_path, "0.1.0", "2026-09-30T12:00:00Z")
    assert set(data) == {
        "schema",
        "version",
        "published_at",
        "min_astra",
        "release_url",
        "artifacts",
    }
    assert data["schema"] == 2
    assert data["published_at"] == "2026-09-30T12:00:00Z"
    assert data["version"] == "0.1.0"
    assert data["release_url"] == "https://github.com/ArrivaRUS/astra-voice/releases/tag/v0.1.0"
    assert data["artifacts"] == {
        "deb": {
            "name": "astra-voice_0.1.0_amd64.deb",
            "sha256": hashlib.sha256(b"amd64").hexdigest(),
            "size": 5,
        }
    }
    current = generator(tmp_path, "0.1.0")
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", current["published_at"])
    with pytest.raises(ValueError, match="published-at"):
        generator(tmp_path, "0.1.0", "2026-09-30T12:00:00+03:00")


def resign(assets: ReleaseAssets, fingerprint: str | None = None) -> None:
    env = {**os.environ, "GNUPGHOME": str(assets.home)}
    command = ["gpg", "--batch", "--yes", "--detach-sign", "--armor"]
    if fingerprint is not None:
        command += ["--local-user", fingerprint]
    command += [
        "--output",
        str(assets.dist / "SHA256SUMS.asc"),
        str(assets.dist / "SHA256SUMS"),
    ]
    subprocess.run(command, env=env, check=True, capture_output=True, timeout=30)


def release_checks(
    validate: Callable[[list[str]], int],
    assets: ReleaseAssets,
    capsys: pytest.CaptureFixture[str],
    expected_code: int = 1,
) -> dict[str, dict[str, str | bool]]:
    assert invoke(validate, *assets, "--json") == expected_code
    result = json.loads(capsys.readouterr().out)
    assert isinstance(result, dict)
    return {check["name"]: check for check in result["checks"]}


def test_signature_tampered_sums(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    with (assets.dist / "SHA256SUMS").open("ab") as output:
        output.write(b"x")
    reason = (
        verify_module.Verifier("release", assets.keyring, pinned=frozenset({assets.fingerprint}))
        .verify_detached(assets.dist / "SHA256SUMS", assets.dist / "SHA256SUMS.asc")
        .reason
    )
    checks = release_checks(validate, assets, capsys)
    assert checks["signature"]["ok"] is False
    assert reason
    assert checks["signature"]["detail"] == reason
    assert checks["signature"]["detail"] != assets.fingerprint
    assert checks["release.gpg"]["ok"] is True
    assert checks["deb"]["ok"] is True
    assert checks["latest.json"]["ok"] is True


def test_signature_other_key(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    env = {**os.environ, "GNUPGHOME": str(assets.home)}
    generated = subprocess.run(
        [
            "gpg",
            "--batch",
            "--pinentry-mode",
            "loopback",
            "--passphrase",
            "",
            "--quick-gen-key",
            "Other <other@example.invalid>",
            "ed25519",
            "sign",
            "never",
        ],
        env=env,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if generated.returncode:
        pytest.skip("генерация второго GPG-ключа невозможна")
    listing = subprocess.run(
        ["gpg", "--batch", "--with-colons", "--list-keys"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    other = [line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")][-1]
    resign(assets, other)
    checks = release_checks(validate, assets, capsys)
    assert checks["signature"]["ok"] is False
    assert checks["sha256"]["ok"] is True
    assert checks["release.gpg"]["ok"] is True
    assert checks["deb"]["ok"] is True


@pytest.mark.parametrize(
    ("change", "expected_detail"),
    [
        ("missing", "не покрыты: release.gpg"),
        ("traversal", "недопустимое имя: ../dist/latest.json"),
        ("duplicate", "повтор имени: latest.json"),
        ("dotslash", "недопустимое имя: ./latest.json"),
        ("backslash", "недопустимое имя: latest\\json"),
        ("dotdot", "недопустимое имя: latest..json"),
        ("dot", "недопустимое имя: ."),
    ],
)
def test_sums_coverage_and_names(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
    change: str,
    expected_detail: str,
) -> None:
    path = assets.dist / "SHA256SUMS"
    lines = path.read_text(encoding="utf-8").splitlines()
    if change == "missing":
        lines = [line for line in lines if not line.endswith("  release.gpg")]
    elif change == "traversal":
        digest = hashlib.sha256((assets.dist / "latest.json").read_bytes()).hexdigest()
        lines.append(f"{digest}  ../{assets.dist.name}/latest.json")
    elif change in ("dotslash", "backslash", "dotdot", "dot"):
        digest = hashlib.sha256((assets.dist / "latest.json").read_bytes()).hexdigest()
        filename = {
            "dotslash": "./latest.json",
            "backslash": "latest\\json",
            "dotdot": "latest..json",
            "dot": ".",
        }[change]
        lines.append(f"{digest}  {filename}")
    elif change == "duplicate":
        lines.append(next(line for line in lines if line.endswith("  latest.json")))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    resign(assets, assets.fingerprint)
    checks = release_checks(validate, assets, capsys)
    assert checks["sha256"]["ok"] is False
    assert expected_detail in str(checks["sha256"]["detail"])
    assert checks["signature"]["ok"] is True
    assert checks["release.gpg"]["ok"] is True


def test_unpinned_signature(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(verify_module, "PINNED_FINGERPRINTS", frozenset({"0" * 40}))
    checks = release_checks(validate, assets, capsys)
    assert checks["signature"]["ok"] is False
    detail = str(checks["signature"]["detail"])
    assert "не закреплён" in detail
    assert detail != assets.fingerprint
    assert re.fullmatch(r"[0-9A-Fa-f]{40}", detail) is None
    assert checks["sha256"]["ok"] is True
    assert checks["release.gpg"]["ok"] is True


def test_revoked_signature(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(verify_module, "REVOKED_FINGERPRINTS", frozenset({assets.fingerprint}))
    checks = release_checks(validate, assets, capsys)
    assert checks["signature"]["ok"] is False
    detail = str(checks["signature"]["detail"])
    assert "отозван" in detail
    assert detail != assets.fingerprint
    assert re.fullmatch(r"[0-9A-Fa-f]{40}", detail) is None
    assert checks["sha256"]["ok"] is True
    assert checks["release.gpg"]["ok"] is True


def test_unreadable_deb(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    if os.geteuid() == 0:
        pytest.skip("root может читать файлы с правами 000")
    deb = next(assets.dist.glob("*.deb"))
    deb.chmod(0)
    try:
        checks = release_checks(validate, assets, capsys, expected_code=2)
        assert checks[deb.name]["ok"] is False
        assert "не удалось прочитать" in str(checks[deb.name]["detail"])
    finally:
        deb.chmod(0o644)


def test_latest_symlink(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    latest = assets.dist / "latest.json"
    copy = assets.dist / "latest-copy.json"
    copy.write_bytes(latest.read_bytes())
    latest.unlink()
    latest.symlink_to(copy)
    checks = release_checks(validate, assets, capsys)
    assert checks["assets"]["ok"] is False
    assert "не обычный файл" in str(checks["assets"]["detail"])
    assert checks["signature"]["ok"] is True
    assert checks["release.gpg"]["ok"] is True


# --- второй артефакт AppImage (arch/appimage.md §9.3, T-168) ---------------------------

IMAGE = "Astra_Voice-0.1.0-x86_64.AppImage"
IMAGE_SIZE = (50 << 20) + 4096
SOURCES = "astra-voice-0.1.0-sources.tar.xz"


def write_sources(path: Path, tamper: bool = False) -> None:
    """Архив исходников в формате release_sources.py: MANIFEST.txt первым."""
    files = {"README.txt": b"r", "astra-voice/a.tar": b"code", "upstream/q/q.tar.gz": b"q"}
    manifest = "".join(
        f"{hashlib.sha256(d).hexdigest()}\t{len(d)}\t{p}\thttps://x.invalid\n"
        for p, d in files.items()
    ).encode()
    with tarfile.open(path, "w:xz") as tar:
        for name, data in [("MANIFEST.txt", manifest), *files.items()]:
            if tamper and name == "README.txt":
                data = b"R"
            info = tarfile.TarInfo(f"astra-voice-0.1.0-sources/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))


def add_appimage(
    assets: ReleaseAssets, *, size: int = IMAGE_SIZE, magic: bytes = b"AI\x02"
) -> None:
    """Дописать в выпуск фейковый AppImage type 2, SBOM, SECURITY.md; пересобрать суммы."""
    dist = assets.dist
    with (dist / IMAGE).open("wb") as output:
        output.write(b"\x7fELF\x02\x01\x01\x00" + magic)
        output.truncate(size)
    (dist / "sbom-appimage.cdx.json").write_text("{}\n", encoding="utf-8")
    (dist / "SECURITY.md").write_text("# security\n", encoding="utf-8")
    write_sources(dist / SOURCES)
    generator = runpy.run_path(str(ROOT / "scripts/release_latest_json.py"))["generate"]
    generator(dist, "0.1.0", "2026-09-30T12:00:00Z", appimage=True)
    rewrite_sums(assets)


def rewrite_sums(assets: ReleaseAssets, skip: str | None = None) -> None:
    dist = assets.dist
    names = sorted(
        path.name
        for path in dist.iterdir()
        if path.name not in {"SHA256SUMS", "SHA256SUMS.asc", skip}
    )
    (dist / "SHA256SUMS").write_text(
        "".join(
            f"{hashlib.sha256((dist / name).read_bytes()).hexdigest()}  {name}\n" for name in names
        ),
        encoding="utf-8",
    )
    resign(assets, assets.fingerprint)


def test_appimage_release_valid(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    add_appimage(assets)
    checks = release_checks(validate, assets, capsys, expected_code=0)
    assert checks["appimage"]["ok"] is True
    assert checks["sources"] == {
        "name": "sources",
        "ok": True,
        "detail": "3 файлов по манифесту",
    }
    assert checks["latest.json"]["detail"] == "указатель верен (схема 2: appimage, deb)"
    assert checks["sha256"]["detail"] == "суммы и покрытие верны"


@pytest.mark.parametrize("case", ["missing", "tampered", "sums"])
def test_appimage_release_needs_sources_archive(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
    case: str,
) -> None:
    """R3.6: архив исходников обязателен с AppImage, проверяется потоком по манифесту."""
    add_appimage(assets)
    dist = assets.dist
    if case == "missing":
        (dist / SOURCES).unlink()
        rewrite_sums(assets)
    elif case == "tampered":
        write_sources(dist / SOURCES, tamper=True)
        rewrite_sums(assets)
    else:
        sums = (
            (dist / "SHA256SUMS")
            .read_text("utf-8")
            .replace(hashlib.sha256((dist / SOURCES).read_bytes()).hexdigest(), "0" * 64)
        )
        (dist / "SHA256SUMS").write_text(sums, "utf-8")
        resign(assets, assets.fingerprint)
    checks = release_checks(validate, assets, capsys)
    if case == "sums":
        assert checks["sources"]["ok"] is True
        assert checks["sha256"]["detail"] == f"неверная сумма: {SOURCES}"
    else:
        assert checks["sources"]["ok"] is False
        assert checks["assets"]["ok"] is False
        assert SOURCES in str(checks["assets"]["detail"])
    if case == "tampered":
        assert checks["sources"]["detail"] == "не совпал с манифестом: README.txt"


def test_appimage_expected_but_missing(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert invoke(validate, *assets, "--json", "--expect-appimage", "yes") == 1
    checks = {check["name"]: check for check in json.loads(capsys.readouterr().out)["checks"]}
    assert checks["assets"]["ok"] is False
    assert IMAGE in str(checks["assets"]["detail"])
    assert checks["appimage"]["ok"] is False


@pytest.mark.parametrize("announced_by", ["sums", "latest"])
def test_appimage_required_when_set_announces_it(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
    announced_by: str,
) -> None:
    """auto: AppImage обязателен, если его заявляет сам набор (SHA256SUMS или latest.json)."""
    add_appimage(assets)
    dist = assets.dist
    image = (dist / IMAGE).read_bytes()
    (dist / IMAGE).unlink()
    (dist / "sbom-appimage.cdx.json").unlink()
    if announced_by == "sums":
        with (dist / "SHA256SUMS").open("a", encoding="utf-8") as sums:
            sums.write(f"{hashlib.sha256(image).hexdigest()}  {IMAGE}\n")
        latest = json.loads((dist / "latest.json").read_text(encoding="utf-8"))
        del latest["artifacts"]["appimage"]
        (dist / "latest.json").write_text(json.dumps(latest), encoding="utf-8")
    if announced_by == "latest":
        rewrite_sums(assets)
    else:
        resign(assets, assets.fingerprint)
    checks = release_checks(validate, assets, capsys)
    assert checks["assets"]["ok"] is False
    assert IMAGE in str(checks["assets"]["detail"])
    assert checks["appimage"]["ok"] is False


def test_published_v010_set_passes_with_tree_flag(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """README: `tools/validate release --version 0.1.0 --dir <ассеты v0.1.0>` проходит.

    Набор v0.1.0 — deb, SBOM, INSTALL-ADMIN.md, latest.json схемы 1, release.gpg,
    SHA256SUMS(.asc) — без AppImage; флаг ENABLED дерева на решение не влияет.
    """
    assert (ROOT / "packaging/appimage/ENABLED").is_file()
    dist = assets.dist
    deb = next(dist.glob("*.deb"))
    legacy = {
        "version": "0.1.0",
        "deb": deb.name,
        "sha256": hashlib.sha256(deb.read_bytes()).hexdigest(),
        "published_at": "2026-09-28T12:00:00Z",
        "min_astra": "1.8",
    }
    (dist / "latest.json").write_text(json.dumps(legacy), encoding="utf-8")
    rewrite_sums(assets)
    assert sorted(path.name for path in dist.iterdir()) == sorted(
        [deb.name, "sbom.cdx.json", "INSTALL-ADMIN.md", "latest.json", "release.gpg"]
        + ["SHA256SUMS", "SHA256SUMS.asc"]
    )
    checks = release_checks(validate, assets, capsys, expected_code=0)
    assert "appimage" not in checks
    assert checks["latest.json"]["detail"] == "указатель верен (схема 1)"


@pytest.mark.parametrize(
    ("change", "failed"),
    [
        ("latest-without-appimage", "latest.json"),
        ("latest-wrong-size", "latest.json"),
        ("sums-without-appimage", "sha256"),
        ("sums-without-sbom", "sha256"),
        ("small", "appimage"),
        ("not-type2", "appimage"),
        ("stray-version", "assets"),
    ],
)
def test_appimage_release_broken(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
    change: str,
    failed: str,
) -> None:
    dist = assets.dist
    if change == "small":
        add_appimage(assets, size=1 << 20)
    elif change == "not-type2":
        add_appimage(assets, magic=b"AI\x01")
    else:
        add_appimage(assets)
    if change.startswith("latest-"):
        latest = json.loads((dist / "latest.json").read_text(encoding="utf-8"))
        if change == "latest-without-appimage":
            del latest["artifacts"]["appimage"]
        else:
            latest["artifacts"]["appimage"]["size"] += 1
        (dist / "latest.json").write_text(json.dumps(latest), encoding="utf-8")
        rewrite_sums(assets)
    elif change == "sums-without-appimage":
        rewrite_sums(assets, skip=IMAGE)
    elif change == "sums-without-sbom":
        rewrite_sums(assets, skip="sbom-appimage.cdx.json")
    elif change == "stray-version":
        (dist / "Astra_Voice-0.0.9-x86_64.AppImage").write_bytes(b"old")
    checks = release_checks(validate, assets, capsys)
    assert checks[failed]["ok"] is False
    assert checks["signature"]["ok"] is True
    if change.startswith("sums-"):
        assert "не покрыты" in str(checks["sha256"]["detail"])


def test_legacy_latest_schema_1_accepted(
    assets: ReleaseAssets,
    validate: Callable[[list[str]], int],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Выпуск v0.1.0 опубликован со схемой 1 — его повторная проверка (О-4) проходит."""
    deb = next(assets.dist.glob("*.deb"))
    legacy = {
        "version": "0.1.0",
        "deb": deb.name,
        "sha256": hashlib.sha256(deb.read_bytes()).hexdigest(),
        "published_at": "2026-09-28T12:00:00Z",
        "min_astra": "1.8",
    }
    (assets.dist / "latest.json").write_text(json.dumps(legacy), encoding="utf-8")
    rewrite_sums(assets)
    checks = release_checks(validate, assets, capsys, expected_code=0)
    assert checks["latest.json"]["detail"] == "указатель верен (схема 1)"


def test_latest_generator_appimage(tmp_path: Path) -> None:
    generator = runpy.run_path(str(ROOT / "scripts/release_latest_json.py"))["generate"]
    (tmp_path / "astra-voice_0.2.0_amd64.deb").write_bytes(b"deb")
    with pytest.raises(ValueError, match="ровно один Astra_Voice-0.2.0-x86_64.AppImage"):
        generator(tmp_path, "0.2.0", appimage=True)
    (tmp_path / "Astra_Voice-0.2.0-x86_64.AppImage").write_bytes(b"image")
    with pytest.raises(ValueError, match="без него"):
        generator(tmp_path, "0.2.0")
    data = generator(tmp_path, "0.2.0", "2026-10-15T09:00:00Z", appimage=True)
    assert data["artifacts"]["appimage"] == {
        "name": "Astra_Voice-0.2.0-x86_64.AppImage",
        "sha256": hashlib.sha256(b"image").hexdigest(),
        "size": 5,
    }
    (tmp_path / "Astra_Voice-0.1.9-x86_64.AppImage").write_bytes(b"old")
    with pytest.raises(ValueError, match="найдено: 2"):
        generator(tmp_path, "0.2.0", appimage=True)
