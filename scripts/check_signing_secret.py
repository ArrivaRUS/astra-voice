#!/usr/bin/env python3
"""Проверить состав секрета подписи и отпечаток готовой подписи."""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from astra_voice.security.verify import (  # noqa: E402
    PINNED_FINGERPRINTS,
    REVOKED_FINGERPRINTS,
    Verifier,
)

_FPR = re.compile(r"[0-9A-F]{40}\Z")


class _KeyRecord(NamedTuple):
    kind: str
    fingerprint: str
    validity: str
    capabilities: str
    marker: str


def _gpg(executable: str, home: Path, *args: str) -> tuple[str | None, str | None]:
    try:
        result = subprocess.run(
            [executable, "--homedir", str(home), "--batch", "--no-tty", "--with-colons", *args],
            capture_output=True,
            text=True,
            env={**os.environ, "LC_ALL": "C", "GNUPGHOME": str(home)},
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, str(exc)
    if result.returncode:
        return None, f"gpg завершился с кодом {result.returncode}: {result.stderr.strip()}"
    return result.stdout, None


def _records(listing: str, kinds: tuple[str, ...]) -> list[_KeyRecord]:
    records: list[_KeyRecord] = []
    pending = False
    for line in listing.splitlines():
        fields = line.split(":")
        if fields[0] in kinds:
            records.append(
                _KeyRecord(
                    fields[0],
                    "",
                    fields[1] if len(fields) > 1 else "",
                    fields[11] if len(fields) > 11 else "",
                    fields[14] if len(fields) > 14 else "",
                )
            )
            pending = True
        elif fields[0] == "fpr" and pending and len(fields) > 9:
            records[-1] = records[-1]._replace(fingerprint=fields[9].upper())
            pending = False
        else:
            pending = False
    return records


def check_secret(
    homedir: Path,
    keyring: Path,
    pinned: frozenset[str],
    revoked: frozenset[str],
    gpg: str | None = None,
) -> tuple[str | None, list[str]]:
    """Вернуть единственный допустимый секретный подключ или причины отказа."""
    executable = gpg if gpg is not None else shutil.which("gpg")
    if executable is None:
        return None, ["gpg не найден"]
    listing, error = _gpg(executable, homedir, "--list-secret-keys")
    if error is not None:
        return None, [f"не удалось прочитать секретные ключи: {error}"]
    assert listing is not None
    secret = _records(listing, ("sec", "ssb"))
    primary = [record for record in secret if record.kind == "sec"]
    subkeys = [record for record in secret if record.kind == "ssb"]
    errors: list[str] = []
    if len(primary) != 1:
        errors.append(f"ожидался ровно один sec, найдено {len(primary)}")
    allowed = {value.upper() for value in pinned}
    denied = {value.upper() for value in revoked}
    master = primary[0].fingerprint if len(primary) == 1 else None
    if master is not None:
        if primary[0].marker != "#":
            errors.append("секрет мастера не является заглушкой")
        if primary[0].validity in ("r", "e"):
            errors.append(f"первичный ключ отозван или истёк: {master}")
        if not _FPR.fullmatch(master):
            errors.append("отпечаток мастера некорректен")
        if master not in allowed:
            errors.append(f"первичный ключ не закреплён: {master}")
        if master in denied:
            errors.append(f"первичный ключ отозван: {master}")
    real = [record for record in subkeys if record.marker != "#"]
    if len(real) != 1:
        errors.append(f"ожидался ровно один секретный ssb, найдено {len(real)}")
    signing = real[0].fingerprint if len(real) == 1 else None
    if signing is not None:
        if "s" not in real[0].capabilities:
            errors.append(f"секретный подключ не имеет возможности подписи: {signing}")
        if real[0].validity in ("r", "e"):
            errors.append(f"секретный подключ отозван или истёк: {signing}")
        if not _FPR.fullmatch(signing):
            errors.append("отпечаток подключа некорректен")
        if signing in denied:
            errors.append(f"секретный подключ отозван: {signing}")
    try:
        with tempfile.TemporaryDirectory(prefix="astra-signing-keyring-") as directory:
            public_home = Path(directory)
            os.chmod(public_home, 0o700)
            public, error = _gpg(executable, public_home, "--show-keys", str(keyring))
    except OSError as exc:
        public, error = None, str(exc)
    if error is not None:
        errors.append(f"не удалось прочитать публичную связку: {error}")
    elif public is not None and master is not None and signing is not None:
        current: str | None = None
        related: dict[str, set[str]] = {}
        pending: str | None = None
        for line in public.splitlines():
            fields = line.split(":")
            if fields[0] in ("pub", "sub"):
                pending = fields[0]
            elif fields[0] == "fpr" and pending is not None and len(fields) > 9:
                fingerprint = fields[9].upper()
                if pending == "pub":
                    current = fingerprint
                    related.setdefault(current, set())
                elif current is not None:
                    related[current].add(fingerprint)
                pending = None
            else:
                pending = None
        if signing not in related.get(master, set()):
            errors.append(f"подключ {signing} отсутствует в публичной связке мастера {master}")
    return (signing if not errors else None), errors


def check_signature(
    keyring: Path,
    sig: Path,
    data: Path,
    subkey: str,
    pinned: frozenset[str],
    revoked: frozenset[str],
) -> list[str]:
    """Проверить подпись именно выбранным подключом закреплённого мастера."""
    result = Verifier("release", keyring, pinned=pinned, revoked=revoked).verify_detached(data, sig)
    errors: list[str] = []
    if not result.ok:
        errors.append(f"подпись не прошла проверку: {result.reason}")
    if result.fingerprint != subkey.upper():
        errors.append(
            f"подпись выполнена другим подключом: {result.fingerprint or 'нет отпечатка'}"
        )
    if result.primary_fingerprint not in {value.upper() for value in pinned}:
        errors.append(
            f"мастер подписи не закреплён: {result.primary_fingerprint or 'нет отпечатка'}"
        )
    return errors


def main(
    argv: list[str] | None = None,
    pinned: frozenset[str] | None = None,
    revoked: frozenset[str] | None = None,
) -> int:
    """Разобрать CLI и напечатать только отпечаток при проверке секрета."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    secret_parser = commands.add_parser("secret")
    secret_parser.add_argument("--homedir", required=True, type=Path)
    secret_parser.add_argument("--keyring", required=True, type=Path)
    signature_parser = commands.add_parser("signature")
    signature_parser.add_argument("--keyring", required=True, type=Path)
    signature_parser.add_argument("--subkey", required=True)
    signature_parser.add_argument("sig", type=Path)
    signature_parser.add_argument("data", type=Path)
    args = parser.parse_args(argv)
    allowed = PINNED_FINGERPRINTS if pinned is None else pinned
    denied = REVOKED_FINGERPRINTS if revoked is None else revoked
    if args.command == "secret":
        fingerprint, errors = check_secret(args.homedir, args.keyring, allowed, denied)
    else:
        fingerprint = None
        errors = check_signature(args.keyring, args.sig, args.data, args.subkey, allowed, denied)
    for error in errors:
        print(f"ОШИБКА: {error}", file=sys.stderr)
    if errors:
        return 1
    if fingerprint is not None:
        print(fingerprint)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
