#!/usr/bin/env python3
"""Проверить, что публичная связка содержит только разрешённые ключи."""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def check(
    keyring: Path,
    pinned: frozenset[str],
    revoked: frozenset[str],
    gpg: str | None = None,
) -> tuple[bool, list[str]]:
    """Проверить первичные ключи по белому списку и все ключи по отзыву."""
    executable = gpg if gpg is not None else shutil.which("gpg")
    if executable is None:
        return False, ["gpg не найден"]
    try:
        with tempfile.TemporaryDirectory(prefix="astra-keyring-") as home:
            os.chmod(home, 0o700)
            command = [executable, "--homedir", home, "--batch", "--no-tty", "--with-colons"]
            result = subprocess.run(
                [
                    *command,
                    "--show-keys",
                    str(keyring),
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "LC_ALL": "C", "GNUPGHOME": home},
                timeout=30,
                check=False,
            )
            packets = subprocess.run(
                [*command, "--list-packets", str(keyring)],
                capture_output=True,
                text=True,
                env={**os.environ, "LC_ALL": "C", "GNUPGHOME": home},
                timeout=30,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, [f"не удалось проверить связку: {exc}"]
    if ":secret key packet:" in packets.stdout or ":secret sub key packet:" in packets.stdout:
        return False, ["в публичной связке есть секретные пакеты"]
    if result.returncode != 0:
        return False, [
            f"gpg не смог прочитать связку (код {result.returncode}): {result.stderr.strip()}"
        ]
    if packets.returncode != 0:
        return False, [
            f"gpg не смог разобрать пакеты связки (код {packets.returncode}): "
            f"{packets.stderr.strip()}"
        ]

    allowed = {value.upper() for value in pinned}
    denied = {value.upper() for value in revoked}
    primary: list[str] = []
    subkeys: list[str] = []
    awaiting: str | None = None
    for line in result.stdout.splitlines():
        fields = line.split(":")
        kind = fields[0]
        if kind in ("pub", "sub"):
            awaiting = kind
        elif kind == "fpr":
            if awaiting is not None and len(fields) > 9:
                fingerprint = fields[9].upper()
                (primary if awaiting == "pub" else subkeys).append(fingerprint)
            awaiting = None
        else:
            awaiting = None
    errors: list[str] = []
    if not primary:
        errors.append("в связке нет первичного ключа")
    for fingerprint in primary:
        if fingerprint not in allowed:
            errors.append(f"первичный ключ не закреплён: {fingerprint}")
        if fingerprint in denied:
            errors.append(f"первичный ключ отозван: {fingerprint}")
    for fingerprint in subkeys:
        if fingerprint in denied:
            errors.append(f"подключ отозван: {fingerprint}")
    return not errors, errors


def warnings(keyring: Path, gpg: str | None = None) -> list[str]:
    """Предупредить об общей дате истечения подписывающих подключей."""
    executable = gpg if gpg is not None else shutil.which("gpg")
    if executable is None:
        return []
    try:
        with tempfile.TemporaryDirectory(prefix="astra-keyring-") as home:
            os.chmod(home, 0o700)
            result = subprocess.run(
                [
                    executable,
                    "--homedir",
                    home,
                    "--batch",
                    "--no-tty",
                    "--with-colons",
                    "--show-keys",
                    str(keyring),
                ],
                capture_output=True,
                text=True,
                env={**os.environ, "LC_ALL": "C", "GNUPGHOME": home},
                timeout=30,
                check=False,
            )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if result.returncode:
        return []
    dates: list[str | None] = []
    for line in result.stdout.splitlines():
        fields = line.split(":")
        if fields[0] != "sub" or len(fields) <= 11:
            continue
        if fields[1] in ("r", "e") or "s" not in fields[11]:
            continue
        expiry = fields[6]
        dates.append(
            datetime.fromtimestamp(int(expiry), UTC).date().isoformat()
            if expiry.isdigit()
            else None
        )
    if len(dates) >= 2 and dates[0] is not None and len(set(dates)) == 1:
        return [
            f"все подписывающие подключи истекают в один день ({dates[0]})"
            " — резерв не даёт запаса времени"
        ]
    return []


def main() -> int:
    """Разобрать CLI и прочитать пины из единственного источника."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("keyring", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(ROOT / "src"))
    try:
        from astra_voice.security.verify import PINNED_FINGERPRINTS, REVOKED_FINGERPRINTS
    except Exception as exc:
        print(f"ОШИБКА: не удалось импортировать пины: {exc}", file=sys.stderr)
        return 1
    ok, messages = check(args.keyring, PINNED_FINGERPRINTS, REVOKED_FINGERPRINTS)
    for message in messages:
        print(f"ОШИБКА: {message}", file=sys.stderr)
    if ok:
        for message in warnings(args.keyring):
            print(f"ПРЕДУПРЕЖДЕНИЕ: {message}", file=sys.stderr)
        print("OK: связка ключей в белом списке")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
