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
* `subprocess` без `shell=True`, argv фиксирован, окружение обнулено.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Literal

__all__ = [
    "GPGV_PATH",
    "PINNED_FINGERPRINTS",
    "REVOKED_FINGERPRINTS",
    "Purpose",
    "VerifyResult",
    "Verifier",
    "sha256_file",
]

Purpose = Literal["release", "catalog"]

#: Путь к `gpgv` по умолчанию. Абсолютный: `PATH` не используется намеренно.
GPGV_PATH: Final = Path("/usr/bin/gpgv")

# TODO(M1→R1, до 25.09): заменить на отпечатки офлайн-мастера заказчика и
# подключей S1/S2 после генерации по процедуре `docs/SECURITY.md` §4.
# Сейчас здесь — ТЕСТОВЫЙ ключ спайка S2 (`spikes/s2_polkit_helper/keys/`),
# его закрытая часть живёт только в `~/.cache/astra-voice-spike/gpg/`
# и в репозиторий не попадает. С этим ключом релизы не подписываются.
PINNED_FINGERPRINTS: Final[frozenset[str]] = frozenset(
    {
        "CB951AD794407972A0B1A5BBAFA87398C4953A71",  # тестовый «S1» спайка S2
    }
)

#: Отозванные отпечатки. Проверяются раньше пина: пересечение = отказ.
REVOKED_FINGERPRINTS: Final[frozenset[str]] = frozenset()

_STATUS_PREFIX: Final = "[GNUPG:] "
_VALIDSIG: Final = "VALIDSIG"
_FPR_RE: Final = re.compile(r"\A[0-9A-F]{40}\Z")
_SUMS_LINE_RE: Final = re.compile(r"\A(?P<hex>[0-9a-f]{64}) [ *](?P<name>[^\n]+)\Z")

#: Верхняя граница разбора `SHA256SUMS` (У19: лимиты на всё, что читаем).
_SUMS_MAX_BYTES: Final = 1 << 20
_GPGV_TIMEOUT_S: Final = 30.0
_HASH_CHUNK: Final = 1 << 20


@dataclass(frozen=True)
class VerifyResult:
    """Итог проверки. Ложь всегда объясняется полем :attr:`reason`."""

    ok: bool
    reason: str = ""
    purpose: Purpose | None = None
    #: Отпечаток подписавшего (под)ключа из `VALIDSIG`.
    fingerprint: str | None = None
    #: Отпечаток первичного ключа (последнее поле `VALIDSIG`).
    primary_fingerprint: str | None = None
    #: Строки `[GNUPG:] …` — для журнала и разбора инцидентов.
    status: tuple[str, ...] = field(default=())

    def __bool__(self) -> bool:  # pragma: no cover - тривиально
        return self.ok


def sha256_file(path: Path, *, chunk: int = _HASH_CHUNK) -> str:
    """Шестнадцатеричный sha256 файла, потоково (файлы бывают по 2 ГБ)."""
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while block := fh.read(chunk):
            digest.update(block)
    return digest.hexdigest()


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
    ) -> None:
        if purpose not in ("release", "catalog"):
            raise ValueError(f"неизвестная цель проверки: {purpose!r}")
        self.purpose: Purpose = purpose
        self.keyring = Path(keyring)
        self.pinned = frozenset(f.upper() for f in pinned)
        self.revoked = frozenset(f.upper() for f in revoked)
        self.gpgv_path = Path(gpgv_path)
        bad = {f for f in self.pinned | self.revoked if not _FPR_RE.match(f)}
        if bad:
            raise ValueError(f"отпечаток не в формате 40 hex: {sorted(bad)}")

    # -- подписи ---------------------------------------------------------

    def verify_detached(self, data: Path, sig: Path) -> VerifyResult:
        """Проверить отсоединённую подпись `sig` для файла `data`."""
        data, sig = Path(data), Path(sig)
        keyring = self.keyring
        if not keyring.is_absolute():
            keyring = keyring.resolve()
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
                proc = subprocess.run(  # noqa: S603 - argv фиксирован, shell не используется
                    argv,
                    capture_output=True,
                    text=True,
                    timeout=_GPGV_TIMEOUT_S,
                    check=False,
                    env={"LC_ALL": "C", "GNUPGHOME": home},
                    cwd=home,
                )
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

    def _fail(
        self,
        reason: str,
        status: tuple[str, ...] = (),
        *,
        fpr: str | None = None,
        primary: str | None = None,
    ) -> VerifyResult:
        return VerifyResult(
            ok=False,
            reason=reason,
            purpose=self.purpose,
            fingerprint=fpr,
            primary_fingerprint=primary,
            status=status,
        )
