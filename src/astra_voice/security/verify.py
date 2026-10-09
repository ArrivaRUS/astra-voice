"""Проверка отсоединённых подписей GPG и файлов `SHA256SUMS`.

Модель угроз: `docs/threat-model.md` §4.2 (У3, У4, У5, У26, У31), гейты T-05 и T-31.

Ключевые решения, которые здесь закодированы:

* проверяет **`gpgv`**, а не `gpg`: он не лезет в связку доверия пользователя и
  не умеет импортировать ключи;
* успех — **только** строка `VALIDSIG` в машинном выводе (`--status-fd 1`).
  Кода возврата и `GOODSIG` недостаточно;
* `man gpgv` (2.2.40): «does not check for expired or revoked keys», поэтому
  отзыв реализован списком :data:`REVOKED_FINGERPRINTS` в коде, а не сертификатом
  отзыва в связке;
* последнее поле `VALIDSIG` — отпечаток **первичного** ключа. Пин по нему
  позволяет плановую ротацию подключей без правки кода (`docs/SECURITY.md`);
* `--homedir` — на пустой временный каталог: у `gpgv` не должно быть доступа
  ни к какому состоянию пользователя;
* `subprocess` без `shell=True`, argv фиксирован, окружение обнулено;
* `--ignore-time-conflict` не передаётся никогда (У92): ключ «из будущего»
  относительно часов компьютера — отказ, но с понятной причиной
  :data:`CLOCK_BEHIND` («часы отстают») вместо безымянного отказа подписи.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import selectors
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Final, Literal

__all__ = [
    "CLOCK_BEHIND",
    "GPGV_PATH",
    "GPG_PATH",
    "PINNED_FINGERPRINTS",
    "REVOKED_FINGERPRINTS",
    "HashCancelledError",
    "Purpose",
    "VerifyResult",
    "Verifier",
    "sha256_file",
]

Purpose = Literal["release", "catalog"]

#: Путь к `gpgv` по умолчанию. Абсолютный: `PATH` не используется намеренно.
GPGV_PATH: Final = Path("/usr/bin/gpgv")
#: `gpg` нужен только для чтения дат создания ключей нашей связки (`--show-keys`)
#: при отказе подписи — чтобы отличить отстающие часы (У92). Нет его — обычный отказ.
GPG_PATH: Final = Path("/usr/bin/gpg")

# Офлайн-мастер заказчика от 2026-09-26. Подключи S1/S2 ротируются без правки
# кода: VALIDSIG отдаёт отпечаток первичного ключа.
PINNED_FINGERPRINTS: Final[frozenset[str]] = frozenset(
    {
        "5F1F7718559F8F57FFE1178055BB1162F17A5CB0",
    }
)

#: Отозванные отпечатки. Проверяются раньше пина: пересечение = отказ.
REVOKED_FINGERPRINTS: Final[frozenset[str]] = frozenset()

#: Код отказа: подпись датирована позже часов компьютера (У92, T-116).
CLOCK_BEHIND: Final = "clock-behind"

_STATUS_PREFIX: Final = "[GNUPG:] "
_VALIDSIG: Final = "VALIDSIG"
_ERRSIG: Final = "ERRSIG"
#: Единственный код ERRSIG, который gpgv 2.2 выдаёт на ключ «из будущего»
#: (GPG_ERR_BAD_PUBKEY). Белый список: остальные коды — обычный отказ.
_ERRSIG_TIME_CONFLICT_RC: Final = "6"
_FPR_RE: Final = re.compile(r"\A[0-9A-F]{40}\Z")
_SUMS_LINE_RE: Final = re.compile(r"\A(?P<hex>[0-9a-f]{64}) [ *](?P<name>[^\n]+)\Z")

#: Верхняя граница разбора `SHA256SUMS` (У19: лимиты на всё, что читаем).
_SUMS_MAX_BYTES: Final = 1 << 20
_GPGV_TIMEOUT_S: Final = 30.0
_GPG_OUTPUT_MAX_BYTES: Final = 1 << 20
_GPG_POLL_S: Final = 0.05
_GPG_TERMINATE_GRACE_S: Final = 0.2
_HASH_CHUNK: Final = 1 << 20


@dataclass(frozen=True)
class VerifyResult:
    """Итог проверки. Ложь всегда объясняется полем :attr:`reason`."""

    ok: bool
    reason: str = ""
    purpose: Purpose | None = None
    #: Стабильный код причины для интерфейса; пустой — обычный отказ.
    code: str = ""
    #: Отпечаток подписавшего (под)ключа из `VALIDSIG`.
    fingerprint: str | None = None
    #: Отпечаток первичного ключа (последнее поле `VALIDSIG`).
    primary_fingerprint: str | None = None
    #: Строки `[GNUPG:] …` — для журнала и разбора инцидентов.
    status: tuple[str, ...] = field(default=())

    def __bool__(self) -> bool:  # pragma: no cover - тривиально
        return self.ok


class HashCancelledError(Exception):
    """Подсчёт контрольной суммы отменён вызывающим кодом."""


def sha256_file(
    path: Path, *, chunk: int = _HASH_CHUNK, cancel: Callable[[], bool] | None = None
) -> str:
    """Потоковый sha256 файла; при отмене поднимает HashCancelledError."""
    if cancel is not None and cancel():
        raise HashCancelledError
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
            if cancel is not None and cancel():
                raise HashCancelledError
    return digest.hexdigest()


class _VerificationCancelled(Exception):
    """Internal control flow; never a signature failure needing diagnosis."""


class _OutputLimitExceeded(Exception):
    """Do not parse a truncated status stream, even if it contains VALIDSIG."""


def _check_cancel(cancel: Callable[[], bool] | None) -> None:
    if cancel is not None and cancel():
        raise _VerificationCancelled


def _stop_and_reap(proc: subprocess.Popen[bytes]) -> None:
    """Only our direct child: TERM, short grace, KILL if needed, always wait."""
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=_GPG_TERMINATE_GRACE_S)
        except subprocess.TimeoutExpired:
            proc.kill()
    proc.wait()


def _run_cancellable(
    argv: list[str], home: str, cancel: Callable[[], bool]
) -> subprocess.CompletedProcess[str]:
    """Bound both pipes together; no reader threads or unbounded communicate.

    Timeout applies to execution and draining output. Cleanup adds at most the
    TERM grace before KILL and reaping (subject to OS process-exit scheduling).
    """
    _check_cancel(cancel)
    deadline = time.monotonic() + _GPGV_TIMEOUT_S
    proc = subprocess.Popen(  # noqa: S603 — fixed argv, never a shell
        argv,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env={"LC_ALL": "C", "GNUPGHOME": home},
        cwd=home,
    )
    try:
        assert proc.stdout is not None and proc.stderr is not None
        buffers = {proc.stdout.fileno(): bytearray(), proc.stderr.fileno(): bytearray()}
        total = 0
        with selectors.DefaultSelector() as selector:
            for pipe in (proc.stdout, proc.stderr):
                os.set_blocking(pipe.fileno(), False)
                selector.register(pipe, selectors.EVENT_READ)
            while selector.get_map() or proc.poll() is None:
                _check_cancel(cancel)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, _GPGV_TIMEOUT_S)
                for key, _ in selector.select(min(_GPG_POLL_S, remaining)):
                    _check_cancel(cancel)
                    try:
                        block = os.read(key.fd, min(65536, _GPG_OUTPUT_MAX_BYTES - total + 1))
                    except BlockingIOError:
                        continue
                    if not block:
                        selector.unregister(key.fileobj)
                        continue
                    total += len(block)
                    if total > _GPG_OUTPUT_MAX_BYTES:
                        raise _OutputLimitExceeded
                    buffers[key.fd].extend(block)
        _check_cancel(cancel)
        return subprocess.CompletedProcess(
            argv,
            proc.wait(),
            buffers[proc.stdout.fileno()].decode("utf-8", errors="replace"),
            buffers[proc.stderr.fileno()].decode("utf-8", errors="replace"),
        )
    finally:
        try:
            _stop_and_reap(proc)
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.stderr is not None:
                proc.stderr.close()


class Verifier:
    """Проверяльщик подписей для одной цели (`release` или `catalog`).

    Разделение по цели закрывает T-31: релизный ключ не должен принимать
    каталог, подписанный корпоративным `catalog_pubkey`, и наоборот — у каждой
    цели своя связка ключей и свои пины.
    """

    def __init__(
        self,
        purpose: Purpose,
        keyring: Path,
        pinned: frozenset[str] = PINNED_FINGERPRINTS,
        revoked: frozenset[str] = REVOKED_FINGERPRINTS,
        *,
        gpgv_path: Path = GPGV_PATH,
        gpg_path: Path = GPG_PATH,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if purpose not in ("release", "catalog"):
            raise ValueError(f"неизвестная цель проверки: {purpose!r}")
        self.purpose: Purpose = purpose
        self.keyring = Path(keyring)
        self.pinned = frozenset(f.upper() for f in pinned)
        self.revoked = frozenset(f.upper() for f in revoked)
        self.gpgv_path = Path(gpgv_path)
        self.gpg_path = Path(gpg_path)
        self._clock = clock
        bad = {f for f in self.pinned | self.revoked if not _FPR_RE.match(f)}
        if bad:
            raise ValueError(f"отпечаток не в формате 40 hex: {sorted(bad)}")

    # -- подписи ---------------------------------------------------------

    def verify_detached(
        self, data: Path, sig: Path, *, cancel: Callable[[], bool] | None = None
    ) -> VerifyResult:
        """Проверить подпись; cancel завершает и собирает собственный GPG-процесс.

        При переданном cancel stdout+stderr ограничены суммарно 1 МиБ. Без
        callback сохранён прежний subprocess.run для совместимости вызовов.
        """
        if cancel is not None and cancel():
            return self._fail("Проверка подписи отменена", code="cancelled")
        data, sig = Path(data), Path(sig)
        keyring = self.keyring
        if not keyring.is_absolute():
            keyring = keyring.resolve()
        if not data.is_absolute():
            data = data.resolve()
        if not sig.is_absolute():
            sig = sig.resolve()
        for label, path in (("keyring", keyring), ("данные", data), ("подпись", sig)):
            if not path.is_file():
                return self._fail(f"{label}: файл не найден или не обычный файл: {path}")

        with tempfile.TemporaryDirectory(prefix="astra-voice-gpgv-") as home:
            os.chmod(home, 0o700)
            argv = [
                str(self.gpgv_path),
                "--status-fd",
                "1",
                "--homedir",
                home,
                "--keyring",
                str(keyring),
                str(sig),
                str(data),
            ]
            try:
                if cancel is None:
                    proc = subprocess.run(  # noqa: S603 - argv фиксирован, shell не используется
                        argv,
                        capture_output=True,
                        text=True,
                        errors="replace",
                        timeout=_GPGV_TIMEOUT_S,
                        check=False,
                        env={"LC_ALL": "C", "GNUPGHOME": home},
                        cwd=home,
                    )
                else:
                    proc = _run_cancellable(argv, home, cancel)
                _check_cancel(cancel)
            except _VerificationCancelled:
                return self._fail("Проверка подписи отменена", code="cancelled")
            except _OutputLimitExceeded:
                return self._fail("gpgv превысил лимит вывода", code="output-limit")
            except FileNotFoundError:
                return self._fail(f"gpgv не найден: {self.gpgv_path}")
            except subprocess.TimeoutExpired:
                return self._fail("gpgv не ответил за отведённое время")

        status = tuple(
            line[len(_STATUS_PREFIX) :]
            for line in proc.stdout.splitlines()
            if line.startswith(_STATUS_PREFIX)
        )
        valid = [line.split() for line in status if line.split()[:1] == [_VALIDSIG]]

        if proc.returncode != 0:
            try:
                created_at = self._future_key(status, keyring, cancel=cancel)
                _check_cancel(cancel)
            except _VerificationCancelled:
                return self._fail("Проверка подписи отменена", code="cancelled")
            if created_at is not None:
                moment = datetime.fromtimestamp(created_at, UTC).strftime("%Y-%m-%d %H:%M")
                return self._fail(
                    f"часы отстают: ключ подписи создан {moment} UTC, это позже времени "
                    "компьютера — проверьте дату и время",
                    status,
                    code=CLOCK_BEHIND,
                )
            return self._fail(f"gpgv отверг подпись (код {proc.returncode})", status)
        if not valid:
            # Сюда попадает и «GOODSIG без VALIDSIG»: доверяем только VALIDSIG.
            return self._fail("в выводе gpgv нет VALIDSIG", status)
        if len(valid) > 1:
            return self._fail(f"ожидалась одна подпись, найдено {len(valid)}", status)

        fields = valid[0]
        if len(fields) < 11:
            return self._fail("строка VALIDSIG неполная", status)
        fpr = fields[1].upper()
        primary = fields[10].upper()
        if not _FPR_RE.match(fpr) or not _FPR_RE.match(primary):
            return self._fail("VALIDSIG: отпечаток не в формате 40 hex", status)

        hit_revoked = {fpr, primary} & self.revoked
        if hit_revoked:
            return self._fail(
                f"ключ отозван: {sorted(hit_revoked)[0]}", status, fpr=fpr, primary=primary
            )
        if fpr not in self.pinned and primary not in self.pinned:
            return self._fail(
                f"отпечаток не закреплён: {fpr} (первичный {primary})",
                status,
                fpr=fpr,
                primary=primary,
            )
        if cancel is not None and cancel():
            return self._fail("Проверка подписи отменена", code="cancelled")
        return VerifyResult(
            ok=True,
            reason="",
            purpose=self.purpose,
            fingerprint=fpr,
            primary_fingerprint=primary,
            status=status,
        )

    # -- контрольные суммы ------------------------------------------------

    def verify_sha256sums(self, sums: Path, file: Path) -> VerifyResult:
        """Сверить `file` со строкой в `sums` (формат `sha256sum`).

        Подпись самого `sums` проверяется отдельно (`verify_detached`).
        Имя файла обязано встречаться в `SHA256SUMS` **ровно один раз**:
        дубль имени с разными суммами — попытка подсунуть другой файл (У4).
        """
        sums, file = Path(sums), Path(file)
        for label, path in (("SHA256SUMS", sums), ("файл", file)):
            if not path.is_file():
                return self._fail(f"{label}: файл не найден или не обычный файл: {path}")
        if sums.stat().st_size > _SUMS_MAX_BYTES:
            return self._fail("SHA256SUMS слишком велик")

        try:
            text = sums.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return self._fail("SHA256SUMS: не UTF-8")

        wanted = file.name
        matches: list[str] = []
        for lineno, raw in enumerate(text.splitlines(), start=1):
            if not raw.strip():
                continue
            m = _SUMS_LINE_RE.match(raw)
            if m is None:
                return self._fail(f"SHA256SUMS: строка {lineno} не разобрана")
            name = m.group("name")
            if "/" in name or name in (".", ".."):
                return self._fail(f"SHA256SUMS: имя с путём в строке {lineno}")
            if name == wanted:
                matches.append(m.group("hex"))

        if not matches:
            return self._fail(f"SHA256SUMS: нет строки для {wanted!r}")
        if len(matches) > 1:
            return self._fail(f"SHA256SUMS: {len(matches)} строк для {wanted!r}")

        actual = sha256_file(file)
        if not hmac.compare_digest(actual, matches[0]):
            return self._fail(f"sha256 не совпал для {wanted!r}")
        return VerifyResult(ok=True, purpose=self.purpose)

    # -- служебное --------------------------------------------------------

    def _future_key(
        self, status: tuple[str, ...], keyring: Path, *, cancel: Callable[[], bool] | None = None
    ) -> int | None:
        """Дата создания нашего ключа, если она позже часов компьютера (У92).

        Всё в строке ERRSIG, кроме кода, выбирает автор подписи: дата и issuer
        подделываются без закрытого ключа. Поэтому «часы отстают» — только если
        сразу: код 6 (конфликт времени gpgv 2.2), отпечаток из ERRSIG есть в
        нашей связке и закреплён (сам или его первичный ключ), не отозван, и
        дата **создания этого ключа по связке** позже часов. Отказ остаётся
        отказом, меняется только причина.
        """
        keys: dict[str, tuple[str, int]] | None = None
        for line in status:
            _check_cancel(cancel)
            fields = line.split()
            # ERRSIG <keyid> <pkalgo> <hashalgo> <class> <time> <rc> <fpr>
            if fields[:1] != [_ERRSIG] or len(fields) < 8:
                continue
            fpr = fields[7].upper()
            if fields[6] != _ERRSIG_TIME_CONFLICT_RC or not _FPR_RE.match(fpr):
                continue
            if keys is None:
                keys = self._keyring_keys(keyring, cancel=cancel)
            found = keys.get(fpr)
            if found is None:
                continue
            primary, created = found
            if {fpr, primary} & self.revoked or not {fpr, primary} & self.pinned:
                continue
            if created > self._clock():
                return created
        return None

    def _keyring_keys(
        self, keyring: Path, *, cancel: Callable[[], bool] | None = None
    ) -> dict[str, tuple[str, int]]:
        """Отпечаток → (первичный отпечаток, дата создания) по нашей связке.

        `gpg --show-keys` во временном пустом GNUPGHOME: связка не импортируется,
        состояние пользователя не читается. Любой сбой — пустой результат.
        """
        _check_cancel(cancel)
        with tempfile.TemporaryDirectory(prefix="astra-voice-gpg-") as home:
            os.chmod(home, 0o700)
            argv = [
                str(self.gpg_path),
                "--homedir",
                home,
                "--batch",
                "--no-options",
                "--no-auto-check-trustdb",
                "--with-colons",
                "--show-keys",
                str(keyring),
            ]
            try:
                if cancel is None:
                    proc = subprocess.run(  # noqa: S603 - argv фиксирован, shell не используется
                        argv,
                        capture_output=True,
                        text=True,
                        errors="replace",
                        timeout=_GPGV_TIMEOUT_S,
                        check=False,
                        env={"LC_ALL": "C", "GNUPGHOME": home},
                        cwd=home,
                    )
                else:
                    proc = _run_cancellable(argv, home, cancel)
                _check_cancel(cancel)
            except (OSError, subprocess.TimeoutExpired, _OutputLimitExceeded):
                return {}
        if proc.returncode != 0:
            return {}
        keys: dict[str, tuple[str, int]] = {}
        # None — у текущего первичного ключа нет годной даты или отпечатка:
        # его подключи не принимаются (иначе подключ стал бы «первичным»).
        primary: str | None = None
        pending: tuple[str, int | None] | None = None
        for line in proc.stdout.splitlines():
            _check_cancel(cancel)
            fields = line.split(":")
            kind = fields[0]
            if kind in ("pub", "sub"):
                raw = fields[5] if len(fields) > 5 else ""
                created = int(raw) if raw.isdecimal() and raw.isascii() and len(raw) <= 12 else None
                if kind == "pub":
                    primary = None
                pending = (kind, created)
                continue
            if kind != "fpr":
                pending = None
                continue
            if pending is None:
                continue
            record, created = pending
            pending = None
            fpr = fields[9].upper() if len(fields) > 9 else ""
            if created is None or not _FPR_RE.match(fpr):
                continue
            if record == "pub":
                primary = fpr
                keys[fpr] = (fpr, created)
            elif primary is not None:
                keys[fpr] = (primary, created)
        return keys

    def _fail(
        self,
        reason: str,
        status: tuple[str, ...] = (),
        *,
        fpr: str | None = None,
        primary: str | None = None,
        code: str = "",
    ) -> VerifyResult:
        return VerifyResult(
            ok=False,
            reason=reason,
            purpose=self.purpose,
            code=code,
            fingerprint=fpr,
            primary_fingerprint=primary,
            status=status,
        )
