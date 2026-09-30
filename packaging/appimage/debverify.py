#!/usr/bin/env python3
"""Проверка происхождения Debian-входов AppImage по `packaging/appimage.lock` (R3.3, T-185).

Цепочка для каждого бинарного пакета — любое расхождение означает отказ до распаковки:

    ключ архива из репозитория (keyring) + отпечаток из lock (signer)
      → `gpgv` по `InRelease` (VALIDSIG ключа signer, ни одной плохой подписи);
        sha256 и размер самого `InRelease` — из lock; Codename и Suite — из lock
      → sha256 и размер `Packages[.xz]` из раздела SHA256 подписанного текста = lock = файл
      → строфа пакета в `Packages`: версия, архитектура, имя файла, Source, sha256, размер
      → sha256 и размер `.deb` = lock = файл; поля control `.deb` (dpkg-deb) = lock.

Исходники (`sources`): `.dsc` — по индексу `Sources` той же цепочки, архивы — по разделу
Checksums-Sha256 своего `.dsc`, самостоятельные файлы — по sha256 и размеру из lock.

Проверка идёт и при сборке из кэша (`build.sh` без `--fetch`): попадание в кэш ничего
не доказывает. Сеть не используется.

    debverify.py --lock packaging/appimage.lock --cache ~/.cache/astra-voice-dev/appimage debs
    debverify.py --lock … --cache … sources

Код возврата 0 — всё сошлось, 1 — расхождение (печатается понятная причина), 2 — вызов.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import lzma
import re
import subprocess
import sys
import tarfile
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

import lockfile

#: Статусы gpgv, которые допускаются (белый список, ревью P3-1). ERRSIG/NO_PUBKEY — подписи
#: ключами, которых намеренно нет в нашем keyring (только rc=9 и пара по keyid);
#: всё остальное (BADSIG, EXPSIG,
#: EXPKEYSIG, REVKEYSIG, ERROR, FAILURE, NODATA, BADARMOR …) — отказ целиком.
_ALLOWED_STATUS = frozenset(
    {
        "NEWSIG",
        "KEY_CONSIDERED",
        "SIG_ID",
        "GOODSIG",
        "VALIDSIG",
        "ERRSIG",
        "NO_PUBKEY",
        "PLAINTEXT",
        "PLAINTEXT_LENGTH",
        "NOTATION_NAME",
        "NOTATION_DATA",
        "NOTATION_FLAGS",
    }
)
_SIGNED_BEGIN = "-----BEGIN PGP SIGNED MESSAGE-----"
_SIG_BEGIN = "-----BEGIN PGP SIGNATURE-----"
_SIG_END = "-----END PGP SIGNATURE-----"
_SOURCE_RE = re.compile(r"(?P<name>[a-z0-9][a-z0-9.+-]+)(?:\s+\((?P<version>[^)\s]+)\))?")


class VerifyError(RuntimeError):
    """Вход не прошёл проверку происхождения."""


def file_digest(path: Path) -> tuple[str, int]:
    """sha256 и размер файла потоком."""
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def check_file(path: Path, sha256: str, size: int, what: str) -> None:
    if not path.is_file():
        raise VerifyError(f"{what}: нет файла {path} — нужен build.sh --fetch (сеть)")
    got_sha, got_size = file_digest(path)
    if got_size != size:
        raise VerifyError(f"{what}: размер {got_size}, в lock {size} ({path})")
    if got_sha != sha256:
        raise VerifyError(f"{what}: sha256 {got_sha}, в lock {sha256} ({path})")


def check_clearsigned(raw: bytes, what: str) -> None:
    """Структура clearsigned-файла, как SplitClearSignedFile в apt (ревью P3-1).

    Первая строка — BEGIN PGP SIGNED MESSAGE; заголовки Hash до пустой строки; в тексте
    строки на «-» только экранированные «- » (второй блок не спрятать); затем ровно одна
    подпись; после END PGP SIGNATURE — только пустые строки.
    """
    try:
        lines = raw.decode("utf-8").split("\n")
    except UnicodeDecodeError:
        raise VerifyError(f"{what}: не UTF-8") from None
    lines = [line.removesuffix("\r") for line in lines]
    if not lines or lines[0] != _SIGNED_BEGIN:
        raise VerifyError(f"{what}: не clearsigned-файл (первая строка не {_SIGNED_BEGIN})")
    i = 1
    while i < len(lines) and lines[i] != "":
        if not lines[i].startswith("Hash:"):
            raise VerifyError(f"{what}: неожиданный заголовок {lines[i][:40]!r}")
        i += 1
    i += 1
    while i < len(lines) and lines[i] != _SIG_BEGIN:
        if lines[i].startswith("-") and not lines[i].startswith("- "):
            raise VerifyError(f"{what}: неэкранированная строка «-» в подписанном тексте")
        i += 1
    if i >= len(lines):
        raise VerifyError(f"{what}: нет блока подписи")
    while i < len(lines) and lines[i] != _SIG_END:
        i += 1
        if i < len(lines) and lines[i] in (_SIGNED_BEGIN, _SIG_BEGIN):
            raise VerifyError(f"{what}: вложенный блок внутри подписи")
    if i >= len(lines):
        raise VerifyError(f"{what}: блок подписи не закрыт")
    if any(line.strip() for line in lines[i + 1 :]):
        raise VerifyError(f"{what}: данные после подписи")


def check_gpgv_status(
    stdout: str, signer: str, *, what: str, plaintext: bool, returncode: int
) -> None:
    """Общий белый список статусов; для clearsign нужен основной ключ и один текст."""
    if returncode < 0 or returncode >= 128:
        raise VerifyError(f"{what}: gpgv аварийно завершился с кодом {returncode}")
    status = [line.split() for line in stdout.splitlines()]
    status = [tokens[1:] for tokens in status if tokens[:1] == ["[GNUPG:]"] and tokens[1:]]
    bad = sorted({tokens[0] for tokens in status if tokens[0] not in _ALLOWED_STATUS})
    if bad:
        raise VerifyError(f"{what}: плохая подпись ({', '.join(bad)})")
    errors = [tokens for tokens in status if tokens[0] == "ERRSIG"]
    missing = [tokens for tokens in status if tokens[0] == "NO_PUBKEY"]
    if any(len(tokens) not in (7, 8) or tokens[6] != "9" for tokens in errors):
        raise VerifyError(f"{what}: ERRSIG допускается только с причиной 9 (нет ключа)")
    for tokens in errors:
        if len(tokens) == 8 and tokens[7] != "-":
            fingerprint = tokens[7]
            if (
                re.fullmatch(r"(?:[0-9A-Fa-f]{40}|[0-9A-Fa-f]{64})", fingerprint) is None
                or fingerprint[-16:].upper() != tokens[1].upper()
            ):
                raise VerifyError(f"{what}: ERRSIG: неверный отпечаток или несовпадение с keyid")
    if any(len(tokens) != 2 for tokens in missing) or {tokens[1] for tokens in errors} != {
        tokens[1] for tokens in missing
    }:
        raise VerifyError(f"{what}: ERRSIG и NO_PUBKEY должны иметь парный статус того же keyid")
    valid = [tokens for tokens in status if tokens[0] == "VALIDSIG"]
    fingerprints = [tokens[-1] for tokens in valid]
    if not plaintext:
        fingerprints += [tokens[1] for tokens in valid if len(tokens) > 1]
    if signer not in fingerprints:
        found = ", ".join(fingerprints) or "нет"
        raise VerifyError(f"{what}: нет подписи ключом {signer} (VALIDSIG: {found})")
    expected = {"GOODSIG": 1, "VALIDSIG": 1}
    if plaintext:
        expected["PLAINTEXT"] = 1
    counts = {name: sum(1 for tokens in status if tokens[0] == name) for name in expected}
    if counts != expected:
        text = " и один текст" if plaintext else ""
        raise VerifyError(f"{what}: ожидалась ровно одна подпись{text} {counts}")


def verify_detached(sig: Path, data: Path, keyring: Path, signer: str, gpgv: str = "gpgv") -> None:
    """Проверить detached-подпись тем же белым списком, что и InRelease."""
    if not keyring.is_file():
        raise VerifyError(f"нет ключа подписи {keyring}")
    for path in (sig, data):
        if not path.is_file():
            raise VerifyError(f"нет файла {path}")
    try:
        proc = subprocess.run(
            [gpgv, "--status-fd", "1", "--keyring", str(keyring), str(sig), str(data)],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise VerifyError(f"не запускается {gpgv}: {exc}") from None
    check_gpgv_status(
        proc.stdout, signer, what=data.name, plaintext=False, returncode=proc.returncode
    )
    if proc.returncode != 0:
        raise VerifyError(f"{data.name}: gpgv завершился с кодом {proc.returncode}")


def gpgv_verify(inrelease: Path, keyring: Path, signer: str, gpgv: str = "gpgv") -> str:
    """Проверить подпись InRelease; вернуть только подписанный текст.

    Неаварийный код выхода gpgv не решает: у Debian несколько подписей, ключи прочих подписантов в
    нашем keyring намеренно отсутствуют (NO_PUBKEY). Решает статус по белому списку:
    ровно один GOODSIG и ровно один VALIDSIG — с основным ключом `signer`, ровно один
    PLAINTEXT, остальные статусы — только из `_ALLOWED_STATUS`.
    """
    check_clearsigned(inrelease.read_bytes(), inrelease.name)
    if not keyring.is_file():
        raise VerifyError(f"нет ключа архива {keyring}")
    with tempfile.TemporaryDirectory(prefix="debverify-") as tmp:
        plain = Path(tmp) / "InRelease.txt"
        try:
            proc = subprocess.run(
                [gpgv, "--status-fd", "1", "--keyring", str(keyring), "--output", str(plain)]
                + [str(inrelease)],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            raise VerifyError(f"не запускается {gpgv}: {exc}") from None
        check_gpgv_status(
            proc.stdout, signer, what=inrelease.name, plaintext=True, returncode=proc.returncode
        )
        if not plain.is_file():
            raise VerifyError(f"{inrelease.name}: gpgv не выдал подписанный текст")
        return plain.read_text(encoding="utf-8")


def release_sha256(text: str) -> dict[str, tuple[str, int]]:
    """Раздел SHA256 файла Release/InRelease: путь → (sha256, размер)."""
    entries: dict[str, tuple[str, int]] = {}
    inside = False
    for line in text.splitlines():
        if not line[:1].isspace():
            inside = line.rstrip() == "SHA256:"
            continue
        if inside:
            parts = line.split()
            if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
                raise VerifyError(f"InRelease: неверная строка SHA256: {line.strip()}")
            entries[parts[2]] = (parts[0], int(parts[1]))
    if not entries:
        raise VerifyError("InRelease: нет раздела SHA256")
    return entries


def parse_deb822(text: str) -> list[dict[str, str]]:
    """Строфы deb822 (Packages, Sources, control, .dsc без подписи)."""
    stanzas: list[dict[str, str]] = []
    current: dict[str, str] = {}
    key: str | None = None
    for line in text.splitlines():
        if not line.strip():
            if current:
                stanzas.append(current)
            current, key = {}, None
        elif line[0] in " \t":
            if key is None:
                raise VerifyError(f"deb822: продолжение без поля: {line.strip()}")
            current[key] += "\n" + line.strip()
        else:
            name, sep, value = line.partition(":")
            if not sep:
                raise VerifyError(f"deb822: строка без «:»: {line}")
            key = name.strip()
            current[key] = value.strip()
    if current:
        stanzas.append(current)
    return stanzas


def read_index(path: Path) -> str:
    data = path.read_bytes()
    if path.name.endswith(".xz"):
        try:
            data = lzma.decompress(data, format=lzma.FORMAT_XZ)
        except lzma.LZMAError as exc:
            raise VerifyError(f"{path.name}: не распаковывается xz: {exc}") from None
    return data.decode("utf-8")


def find_stanzas(text: str, field: str, values: Iterable[str]) -> list[dict[str, str]]:
    """Строфы, где `field` — одно из `values` (без разбора всего индекса в словари)."""
    wanted = set(values)
    result: list[dict[str, str]] = []
    for block in re.split(r"\n\s*\n", text):
        match = re.search(rf"^{re.escape(field)}:\s*(\S+)\s*$", block, re.MULTILINE)
        if match and match.group(1) in wanted:
            result.extend(parse_deb822(block))
    return result


def source_of(stanza: dict[str, str]) -> tuple[str, str]:
    """Исходник бинарного пакета: поле Source (с версией в скобках при binNMU) или сам пакет."""
    raw = stanza.get("Source")
    if raw is None:
        return stanza["Package"], stanza["Version"]
    match = _SOURCE_RE.fullmatch(raw.strip())
    if match is None:
        raise VerifyError(f"{stanza['Package']}: неверное поле Source: {raw}")
    return match["name"], match["version"] or stanza["Version"]


def checksums(field: str) -> dict[str, tuple[str, int]]:
    """Поле Checksums-Sha256 (Sources/.dsc): имя файла → (sha256, размер)."""
    result: dict[str, tuple[str, int]] = {}
    for line in field.splitlines():
        parts = line.split()
        if not parts:
            continue
        if len(parts) != 3 or not re.fullmatch(r"[0-9a-f]{64}", parts[0]):
            raise VerifyError(f"неверная строка Checksums-Sha256: {line.strip()}")
        result[parts[2]] = (parts[0], int(parts[1]))
    return result


def deb_control(path: Path, dpkg_deb: str = "dpkg-deb") -> dict[str, str]:
    try:
        proc = subprocess.run(
            [dpkg_deb, "--field", str(path)], capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise VerifyError(f"не запускается {dpkg_deb}: {exc}") from None
    if proc.returncode != 0:
        raise VerifyError(f"{path.name}: dpkg-deb не читает control: {proc.stderr.strip()}")
    stanzas = parse_deb822(proc.stdout)
    if len(stanzas) != 1:
        raise VerifyError(f"{path.name}: control — не одна строфа")
    return stanzas[0]


class Verifier:
    """Проверка входов одного lock против кэша; подписанные тексты кэшируются."""

    def __init__(
        self,
        lock: lockfile.Lock,
        cache: Path,
        root: Path,
        gpgv: str = "gpgv",
        dpkg_deb: str = "dpkg-deb",
        deb_dir: Path | None = None,
    ) -> None:
        self.lock = lock
        self.cache = cache
        #: Где лежат проверяемые .deb: по умолчанию кэш; build.sh передаёт свою копию,
        #: чтобы распаковать ровно проверенные байты (ревью P3-3, TOCTOU).
        self.deb_dir = deb_dir
        self.root = root
        self.gpgv = gpgv
        self.dpkg_deb = dpkg_deb
        self._release: dict[str, dict[str, tuple[str, int]]] = {}
        self._index_text: dict[str, str] = {}

    def archive_entries(self, ident: str) -> dict[str, tuple[str, int]]:
        if ident not in self._release:
            archive = self.lock.archive(ident)
            path = self.cache / archive.cache_path
            check_file(path, archive.sha256, archive.size, f"InRelease {ident}")
            text = gpgv_verify(path, self.root / archive.keyring, archive.signer, self.gpgv)
            header = parse_deb822(text.split("\nMD5Sum:", 1)[0].split("\nSHA256:", 1)[0])
            got = (header[0].get("Codename"), header[0].get("Suite")) if header else (None, None)
            if got != (archive.codename, archive.suite):
                raise VerifyError(
                    f"InRelease {ident}: Codename/Suite {got}, "
                    f"в lock {(archive.codename, archive.suite)}"
                )
            self._release[ident] = release_sha256(text)
        return self._release[ident]

    def index_text(self, ident: str) -> str:
        if ident not in self._index_text:
            index = self.lock.index(ident)
            listed = self.archive_entries(index.archive).get(index.file)
            if listed is None:
                raise VerifyError(f"index {ident}: {index.file} нет в InRelease {index.archive}")
            if listed != (index.sha256, index.size):
                raise VerifyError(
                    f"index {ident}: в InRelease {listed[0]} {listed[1]}, "
                    f"в lock {index.sha256} {index.size}"
                )
            path = self.cache / index.cache_path
            check_file(path, index.sha256, index.size, f"index {ident}")
            self._index_text[ident] = read_index(path)
        return self._index_text[ident]

    def verify_deb(self, deb: lockfile.Deb) -> Path:
        what = f"deb {deb.package}"
        stanzas = [
            s
            for s in find_stanzas(self.index_text(deb.index), "Package", [deb.package])
            if s.get("Version") == deb.version and s.get("Architecture") == deb.arch
        ]
        if len(stanzas) != 1:
            raise VerifyError(
                f"{what}: в индексе {deb.index} {len(stanzas)} строф {deb.version} {deb.arch}"
            )
        stanza = stanzas[0]
        filename = stanza.get("Filename", "")
        if filename.rsplit("/", 1)[-1] != deb.file:
            raise VerifyError(f"{what}: в индексе файл {filename}, в lock {deb.file}")
        if not deb.url.endswith("/" + filename.replace("+", "%2B")) and not deb.url.endswith(
            "/" + filename
        ):
            raise VerifyError(f"{what}: URL {deb.url} не ведёт на {filename} из индекса")
        if (stanza.get("SHA256"), stanza.get("Size")) != (deb.sha256, str(deb.size)):
            raise VerifyError(
                f"{what}: в индексе {stanza.get('SHA256')} {stanza.get('Size')}, "
                f"в lock {deb.sha256} {deb.size}"
            )
        if source_of(stanza) != (deb.source, deb.source_version):
            raise VerifyError(
                f"{what}: исходник в индексе {source_of(stanza)}, "
                f"в lock {(deb.source, deb.source_version)}"
            )
        path = (self.deb_dir / deb.file) if self.deb_dir else (self.cache / deb.cache_path)
        check_file(path, deb.sha256, deb.size, what)
        control = deb_control(path, self.dpkg_deb)
        got = (control.get("Package"), control.get("Version"), control.get("Architecture"))
        if got != (deb.package, deb.version, deb.arch):
            raise VerifyError(f"{what}: control пакета {got}")
        if source_of(control) != (deb.source, deb.source_version):
            raise VerifyError(f"{what}: Source в control {source_of(control)}")
        return path

    def verify_debs(self) -> list[Path]:
        if self.lock.todo_pin:
            raise VerifyError(f"незакреплённые входы (TODO-PIN): {'; '.join(self.lock.todo_pin)}")
        return [self.verify_deb(deb) for deb in self.lock.debs]

    def verify_sources(self) -> list[Path]:
        """Все `# source:` из lock и полнота архивов каждого `.dsc`."""
        if self.lock.todo_pin:
            raise VerifyError(f"незакреплённые входы (TODO-PIN): {'; '.join(self.lock.todo_pin)}")
        paths: list[Path] = []
        dsc_files: dict[str, dict[str, tuple[str, int]]] = {}
        for source in self.lock.sources:
            what = f"source {source.id}"
            path = self.cache / source.cache_path
            if source.index is not None:
                listed = self._listed_in_sources(source)
                if listed != (source.sha256, source.size):
                    raise VerifyError(f"{what}: в индексе {listed}, в lock {source.sha256}")
            check_file(path, source.sha256, source.size, what)
            if source.git_tree is not None:
                delta = None
                if source.git_delta is not None:
                    delta = json.loads((self.root / source.git_delta).read_text(encoding="utf-8"))
                    if (delta.get("commit"), delta.get("tree")) != (
                        source.git_commit,
                        source.git_tree,
                    ):
                        raise VerifyError(f"{what}: {source.git_delta} от другого коммита")
                tree = archive_git_tree(path, str(source.git_commit), delta, source.git_tree)
                if tree != source.git_tree:
                    raise VerifyError(f"{what}: дерево git {tree}, в lock {source.git_tree}")
            if source.file.endswith(".dsc"):
                dsc = parse_deb822(strip_pgp(path.read_text(encoding="utf-8")))
                if len(dsc) != 1 or "Checksums-Sha256" not in dsc[0]:
                    raise VerifyError(f"{what}: нет Checksums-Sha256 в {source.file}")
                dsc_files[source.id] = checksums(dsc[0]["Checksums-Sha256"])
            paths.append(path)
        for source in self.lock.sources:
            if source.dsc is None:
                continue
            listed = dsc_files[source.dsc].get(source.file)
            if listed != (source.sha256, source.size):
                raise VerifyError(f"source {source.id}: в {source.dsc} {listed}, в lock другое")
        for ident, files in dsc_files.items():
            recorded = {s.file for s in self.lock.sources if s.dsc == ident}
            missing = sorted(set(files) - recorded)
            if missing:
                raise VerifyError(f"source {ident}: в lock нет архивов {', '.join(missing)}")
        return paths

    def _listed_in_sources(self, source: lockfile.Source) -> tuple[str, int] | None:
        assert source.index is not None
        text = self.index_text(source.index)
        found: list[tuple[str, int]] = []
        for block in re.split(r"\n\s*\n", text):
            if source.file not in block:
                continue
            for stanza in parse_deb822(block):
                listed = checksums(stanza.get("Checksums-Sha256", "")).get(source.file)
                if listed is not None:
                    found.append(listed)
        if len(found) > 1:
            raise VerifyError(f"source {source.id}: {source.file} в индексе несколько раз")
        return found[0] if found else None


#: Что архив GitHub (git archive) законно меняет относительно дерева коммита (ревью P3-5).
_EXPORT_IGNORED = frozenset({".gitignore", ".gitattributes"})
#: `.tag` Qt: в git — шаблон export-subst, в архиве — хэш коммита (%H) или дерева (%T).
_TAG_TEMPLATES = {"%H": b"$Format:%H$\n", "%T": b"$Format:%T$\n"}


def _blob(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def archive_git_tree(
    path: Path, commit: str, delta: dict[str, Any] | None = None, tree: str | None = None
) -> str:
    """Хэш дерева git, воспроизведённый из архива GitHub по коммиту (ревью P3-5).

    Архив должен нести commit в pax-заголовке. Дельта допускается только известных видов,
    и каждое её изменение воспроизводится: отсутствующие в архиве `.gitignore`/`.gitattributes`
    (export-ignore) и подмодули (160000); `.tag` в корне (export-subst: в архиве — хэш
    коммита или ожидаемого дерева, в git — `$Format:%H$`/`$Format:%T$`); файлы с CRLF в
    архиве, чей git-блоб — тот же текст с LF.
    """
    files: dict[str, tuple[str, str]] = {}
    contents: dict[str, bytes] = {}
    changed: dict[str, Any] = {str(c["path"]): c for c in (delta or {}).get("changed", [])}
    with tarfile.open(path) as tar:
        top: str | None = None
        for member in tar:
            parts = member.name.rstrip("/").split("/")
            if top is None:
                top = parts[0]
            if parts[0] != top or ".." in parts:
                raise VerifyError(f"{path.name}: чужой путь {member.name}")
            rel = "/".join(parts[1:])
            if not rel or member.isdir():
                continue
            if member.issym():
                data, mode = member.linkname.encode(), "120000"
            elif member.isreg():
                fh = tar.extractfile(member)
                assert fh is not None
                data = fh.read()
                mode = "100755" if member.mode & 0o111 else "100644"
            else:
                raise VerifyError(f"{path.name}: неожиданный тип {member.name}")
            files[rel] = (mode, _blob(data))
            if rel in changed:
                contents[rel] = data
        if tar.pax_headers.get("comment") != commit:
            raise VerifyError(f"{path.name}: архив не коммита {commit}")
    for item in (delta or {}).get("missing", []):
        rel, mode, sha = str(item["path"]), str(item["mode"]), str(item["sha1"])
        if rel in files:
            raise VerifyError(f"{path.name}: {rel} есть в архиве, а дельта считает его пропавшим")
        if mode != "160000" and rel.rsplit("/", 1)[-1] not in _EXPORT_IGNORED:
            raise VerifyError(
                f"{path.name}: пропасть из архива может только .gitignore/.gitattributes"
            )
        files[rel] = (mode, sha)
    for rel, item in changed.items():
        mode, sha = str(item["mode"]), str(item["sha1"])
        archived = contents.get(rel)
        if archived is None:
            raise VerifyError(f"{path.name}: изменённого {rel} нет в архиве")
        substituted = {"%H": commit, "%T": tree}
        if rel == ".tag" and any(
            value is not None
            and archived == value.encode() + b"\n"
            and sha == _blob(_TAG_TEMPLATES[key])
            for key, value in substituted.items()
        ):
            pass
        elif b"\r\n" in archived and _blob(archived.replace(b"\r\n", b"\n")) == sha:
            pass
        else:
            raise VerifyError(f"{path.name}: изменение {rel} не объясняется export-subst или eol")
        files[rel] = (mode, sha)
    return _tree_hash(files)


def _tree_hash(files: dict[str, tuple[str, str]]) -> str:
    root: dict[str, Any] = {}
    for rel, entry in files.items():
        node = root
        *dirs, name = rel.split("/")
        for part in dirs:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise VerifyError(f"конфликт файла и каталога: {rel}")
            node = child
        node[name] = entry

    def digest(node: dict[str, Any]) -> bytes:
        items: list[tuple[bytes, bytes]] = []
        for name, value in node.items():
            key = name.encode()
            if isinstance(value, dict):
                items.append((key + b"/", b"40000 " + key + b"\0" + digest(value)))
            else:
                entry_mode, entry_sha = value
                head = str(entry_mode).encode() + b" " + key + b"\0"
                items.append((key, head + bytes.fromhex(str(entry_sha))))
        body = b"".join(entry for _, entry in sorted(items))
        return hashlib.sha1(b"tree %d\0" % len(body) + body).digest()

    return digest(root).hex()


def strip_pgp(text: str) -> str:
    """Тело clearsigned-файла (.dsc) без заголовка и подписи; неподписанный — как есть."""
    lines = text.splitlines()
    if not lines or lines[0] != "-----BEGIN PGP SIGNED MESSAGE-----":
        return text
    try:
        start = lines.index("") + 1
        end = lines.index("-----BEGIN PGP SIGNATURE-----")
    except ValueError:
        raise VerifyError("clearsigned-файл повреждён") from None
    return "\n".join(line[2:] if line.startswith("- ") else line for line in lines[start:end])


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="проверка происхождения Debian-входов AppImage")
    parser.add_argument("--lock", type=Path)
    parser.add_argument("--cache", type=Path)
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[2], help="корень репозитория"
    )
    parser.add_argument("--deb-dir", type=Path, help="каталог с копиями .deb (сборка) вместо кэша")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("debs")
    commands.add_parser("sources")
    sig_parser = commands.add_parser("verify-sig", help="проверка detached-подписи")
    sig_parser.add_argument("--keyring", type=Path, required=True)
    sig_parser.add_argument("--want-fpr", required=True)
    sig_parser.add_argument("sig", type=Path)
    sig_parser.add_argument("file", type=Path)
    args = parser.parse_args(argv)
    if args.command == "verify-sig":
        try:
            verify_detached(args.sig, args.file, args.keyring, args.want_fpr)
        except VerifyError as exc:
            print(f"ОШИБКА: {exc}", file=sys.stderr)
            return 1
        print(f"подпись: ключ {args.want_fpr} — OK")
        return 0
    missing = [name for name in ("--lock", "--cache") if getattr(args, name[2:]) is None]
    if missing:
        parser.error(f"для {args.command} обязательны: {', '.join(missing)}")
    try:
        lock = lockfile.load(args.lock)
        verifier = Verifier(lock, args.cache, args.root, deb_dir=args.deb_dir)
        if args.command == "debs":
            paths = verifier.verify_debs()
            for deb in lock.debs:
                print(f"происхождение: {deb.package} {deb.version} {deb.arch} — OK")
        else:
            paths = verifier.verify_sources()
    except (OSError, lockfile.LockError, VerifyError) as exc:
        print(f"ОШИБКА: происхождение: {exc}", file=sys.stderr)
        return 1
    print(f"debverify {args.command}: {len(paths)} файлов, цепочка подписи и sha256 сошлась")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
