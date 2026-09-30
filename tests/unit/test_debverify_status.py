"""T-190 / MN-10, T-198 / MN-20: единая проверка статусов gpgv, без ключей и настоящего gpgv."""

from __future__ import annotations

import importlib.util
import re
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import Mock

import pytest

pytestmark = pytest.mark.unit
HERE = Path(__file__).resolve().parents[2] / "packaging" / "appimage"
FPR = "A" * 40
SUBKEY = "B" * 40
FOREIGN = "C" * 40
REAL_SIGNER = "94AB9973AB8723F1033E07C8AB46FCD1DF175CCB"
REAL_FOREIGN = "01A8226ADE8F41FB6F517B941ECE02CBA1B7F881"


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lockfile = load("lockfile", HERE / "lockfile.py")
debverify = load("appimage_debverify_status", HERE / "debverify.py")


@pytest.fixture
def statuses() -> dict[str, str]:
    prefix = f"[GNUPG:] NEWSIG\n[GNUPG:] KEY_CONSIDERED {FPR} 0\n[GNUPG:] SIG_ID id 1 2\n"
    valid = f"[GNUPG:] VALIDSIG {SUBKEY} 2020-01-01 1577836800 0 4 0 22 8 00 {FPR}\n"
    return {
        "good": prefix + f"[GNUPG:] GOODSIG {SUBKEY[-16:]} Test\n" + valid,
        "expired": prefix
        + "[GNUPG:] KEYEXPIRED 1577923200\n"
        + f"[GNUPG:] EXPKEYSIG {SUBKEY[-16:]} Test\n"
        + valid,
        "keyexpired": "[GNUPG:] KEYEXPIRED 1577923200\n"
        + prefix
        + f"[GNUPG:] GOODSIG {SUBKEY[-16:]} Test\n"
        + valid,
        "keyrevoked": "[GNUPG:] KEYREVOKED\n"
        + prefix
        + f"[GNUPG:] GOODSIG {SUBKEY[-16:]} Test\n"
        + valid,
        "expkeysig": prefix + f"[GNUPG:] EXPKEYSIG {SUBKEY[-16:]} Test\n" + valid,
        "revoked": prefix + f"[GNUPG:] REVKEYSIG {SUBKEY[-16:]} Test\n" + valid,
        "expsig": prefix + f"[GNUPG:] EXPSIG {SUBKEY[-16:]} Test\n" + valid,
        "badsig": prefix + f"[GNUPG:] BADSIG {SUBKEY[-16:]} Test\n" + valid,
        "foreign": prefix
        + f"[GNUPG:] GOODSIG {FOREIGN[-16:]} Test\n"
        + valid.replace(SUBKEY, FOREIGN).replace(FPR, FOREIGN),
    }


@pytest.fixture
def real_gpgv_status() -> str:
    """gpgv 2.2.40: две detached-подписи, в keyring только первый ключ, rc=2."""
    return (
        "[GNUPG:] NEWSIG\n"
        "[GNUPG:] KEY_CONSIDERED 94AB9973AB8723F1033E07C8AB46FCD1DF175CCB 0\n"
        "[GNUPG:] SIG_ID olqQmIwuF9OsVNNRZO4fw42D8ds 2026-09-30 1790767633\n"
        "[GNUPG:] KEY_CONSIDERED 94AB9973AB8723F1033E07C8AB46FCD1DF175CCB 0\n"
        "[GNUPG:] GOODSIG AB46FCD1DF175CCB Kg1 <k@t.invalid>\n"
        "[GNUPG:] VALIDSIG 94AB9973AB8723F1033E07C8AB46FCD1DF175CCB "
        "2026-09-30 1790767633 0 4 0 22 8 00 94AB9973AB8723F1033E07C8AB46FCD1DF175CCB\n"
        "[GNUPG:] NEWSIG\n"
        "[GNUPG:] ERRSIG 1ECE02CBA1B7F881 22 8 00 1790767633 9 "
        "01A8226ADE8F41FB6F517B941ECE02CBA1B7F881\n"
        "[GNUPG:] NO_PUBKEY 1ECE02CBA1B7F881\n"
    )


@pytest.mark.parametrize("plaintext", [False, True])
def test_real_gpgv_status(real_gpgv_status: str, plaintext: bool) -> None:
    stdout = real_gpgv_status + ("[GNUPG:] PLAINTEXT 74 0\n" if plaintext else "")
    debverify.check_gpgv_status(
        stdout, REAL_SIGNER, what="runtime", plaintext=plaintext, returncode=2
    )


@pytest.mark.parametrize("suffix", ["", " -"])
@pytest.mark.parametrize("plaintext", [False, True])
def test_real_errsig_optional_fingerprint(
    real_gpgv_status: str, suffix: str, plaintext: bool
) -> None:
    stdout = real_gpgv_status.replace(" " + REAL_FOREIGN, suffix)
    stdout += "[GNUPG:] PLAINTEXT 74 0\n" if plaintext else ""
    debverify.check_gpgv_status(
        stdout, REAL_SIGNER, what="runtime", plaintext=plaintext, returncode=2
    )


@pytest.mark.parametrize(
    ("ending", "message"),
    [
        ("4 " + REAL_FOREIGN, "только с причиной 9"),
        ("4", "только с причиной 9"),
        ("9 " + REAL_FOREIGN[:-1] + "0", "неверный отпечаток"),
        ("9 " + "G" + REAL_FOREIGN[1:], "неверный отпечаток"),
        ("9 " + REAL_FOREIGN[1:], "неверный отпечаток"),
        ("9 " + "A" + REAL_FOREIGN, "неверный отпечаток"),
        ("9 " + "A" * 47 + REAL_FOREIGN[-16:], "неверный отпечаток"),
        ("9 " + "A" * 48 + REAL_FOREIGN[-16:], "неверный отпечаток"),
        ("9 " + "A" * 49 + REAL_FOREIGN[-16:], "неверный отпечаток"),
        ("9 - extra", "только с причиной 9"),
        ("9 " + REAL_FOREIGN + " 9", "только с причиной 9"),
        ("", "только с причиной 9"),
    ],
)
@pytest.mark.parametrize("plaintext", [False, True])
def test_real_errsig_invalid_fields_rejected(
    real_gpgv_status: str, ending: str, message: str, plaintext: bool
) -> None:
    stdout = real_gpgv_status.replace("9 " + REAL_FOREIGN, ending)
    stdout += "[GNUPG:] PLAINTEXT 74 0\n" if plaintext else ""
    with pytest.raises(debverify.VerifyError, match=message):
        debverify.check_gpgv_status(
            stdout, REAL_SIGNER, what="runtime", plaintext=plaintext, returncode=2
        )


REJECTED = [
    ("expired", r"плохая подпись \(EXPKEYSIG, KEYEXPIRED\)"),
    ("keyexpired", r"плохая подпись \(KEYEXPIRED\)"),
    ("keyrevoked", r"плохая подпись \(KEYREVOKED\)"),
    ("expkeysig", r"плохая подпись \(EXPKEYSIG\)"),
    ("revoked", r"плохая подпись \(REVKEYSIG\)"),
    ("expsig", r"плохая подпись \(EXPSIG\)"),
    ("badsig", r"плохая подпись \(BADSIG\)"),
    ("foreign", "нет подписи ключом"),
]


@pytest.mark.parametrize("signer", [FPR, SUBKEY])
def test_valid_detached_status(statuses: dict[str, str], signer: str) -> None:
    debverify.check_gpgv_status(
        statuses["good"], signer, what="runtime", plaintext=False, returncode=0
    )


@pytest.mark.parametrize(("case", "message"), REJECTED)
@pytest.mark.parametrize("plaintext", [False, True])
def test_bad_status_rejected(
    statuses: dict[str, str], case: str, message: str, plaintext: bool
) -> None:
    stdout = statuses[case] + ("[GNUPG:] PLAINTEXT 74 0\n" if plaintext else "")
    with pytest.raises(debverify.VerifyError, match=message):
        debverify.check_gpgv_status(stdout, FPR, what="runtime", plaintext=plaintext, returncode=0)


@pytest.mark.parametrize("count", [0, 1, 2])
def test_clearsign_requires_one_plaintext(statuses: dict[str, str], count: int) -> None:
    stdout = statuses["good"] + "[GNUPG:] PLAINTEXT 74 0\n" * count
    if count == 1:
        debverify.check_gpgv_status(stdout, FPR, what="InRelease", plaintext=True, returncode=0)
    else:
        with pytest.raises(debverify.VerifyError, match="ровно одна подпись и один текст"):
            debverify.check_gpgv_status(stdout, FPR, what="InRelease", plaintext=True, returncode=0)


def test_clearsign_still_requires_primary(statuses: dict[str, str]) -> None:
    with pytest.raises(debverify.VerifyError, match="нет подписи ключом"):
        debverify.check_gpgv_status(
            statuses["good"] + "[GNUPG:] PLAINTEXT 74 0\n",
            SUBKEY,
            what="InRelease",
            plaintext=True,
            returncode=0,
        )


@pytest.mark.parametrize("name", ["GOODSIG", "VALIDSIG"])
@pytest.mark.parametrize("count", [0, 2])
@pytest.mark.parametrize("plaintext", [False, True])
def test_exactly_one_signature(
    statuses: dict[str, str], name: str, count: int, plaintext: bool
) -> None:
    lines = statuses["good"].splitlines()
    line = next(line for line in lines if line.startswith(f"[GNUPG:] {name} "))
    lines.remove(line)
    stdout = "\n".join(lines + [line] * count) + "\n[GNUPG:] PLAINTEXT 74 0\n"
    with pytest.raises(debverify.VerifyError):
        debverify.check_gpgv_status(stdout, FPR, what="runtime", plaintext=plaintext, returncode=0)


@pytest.mark.parametrize("stdout", ["", "[GNUPG:]\n", f"VALIDSIG {FPR}\n"])
def test_missing_status_rejected(stdout: str) -> None:
    with pytest.raises(debverify.VerifyError, match="нет подписи ключом"):
        debverify.check_gpgv_status(stdout, FPR, what="runtime", plaintext=False, returncode=0)


@pytest.mark.parametrize(
    "extra",
    [
        "ERRSIG CCCCCCCCCCCCCCCC 1 8 00 1 4\n",
        "ERRSIG CCCCCCCCCCCCCCCC 1 8 00 1 4\nNO_PUBKEY CCCCCCCCCCCCCCCC\n",
        "ERRSIG CCCCCCCCCCCCCCCC 1 8 00 1 9\nNO_PUBKEY DDDDDDDDDDDDDDDD\n",
        "ERRSIG CCCCCCCCCCCCCCCC 1 8 00 1 9\n",
        "NO_PUBKEY CCCCCCCCCCCCCCCC\n",
        "ERRSIG\n",
        "ERRSIG CCCCCCCCCCCCCCCC 9\nNO_PUBKEY CCCCCCCCCCCCCCCC\n",
        "NO_PUBKEY\n",
    ],
)
@pytest.mark.parametrize("plaintext", [False, True])
def test_errsig_requires_missing_key_pair(
    statuses: dict[str, str], extra: str, plaintext: bool
) -> None:
    stdout = statuses["good"] + "[GNUPG:] PLAINTEXT 74 0\n"
    stdout += "".join(f"[GNUPG:] {line}\n" for line in extra.splitlines())
    with pytest.raises(debverify.VerifyError, match="ERRSIG"):
        debverify.check_gpgv_status(stdout, FPR, what="runtime", plaintext=plaintext, returncode=2)


@pytest.mark.parametrize("plaintext", [False, True])
@pytest.mark.parametrize("has_validsig", [False, True])
@pytest.mark.parametrize("reverse", [False, True])
def test_missing_key_pair_requires_valid_signature(
    statuses: dict[str, str], plaintext: bool, has_validsig: bool, reverse: bool
) -> None:
    pair = [
        "[GNUPG:] ERRSIG CCCCCCCCCCCCCCCC 1 8 00 1 9\n",
        "[GNUPG:] NO_PUBKEY CCCCCCCCCCCCCCCC\n",
    ]
    if reverse:
        pair.reverse()
    stdout = statuses["good"] + "[GNUPG:] PLAINTEXT 74 0\n" + "".join(pair)
    if has_validsig:
        debverify.check_gpgv_status(stdout, FPR, what="runtime", plaintext=plaintext, returncode=2)
    else:
        stdout = "\n".join(line for line in stdout.splitlines() if "VALIDSIG" not in line)
        with pytest.raises(debverify.VerifyError, match="нет подписи ключом"):
            debverify.check_gpgv_status(
                stdout, FPR, what="runtime", plaintext=plaintext, returncode=2
            )


@pytest.mark.parametrize("returncode", [128, 134, 139, -6])
@pytest.mark.parametrize("plaintext", [False, True])
@pytest.mark.parametrize("valid_status", [False, True])
def test_abnormal_returncode_rejected(
    statuses: dict[str, str], returncode: int, plaintext: bool, valid_status: bool
) -> None:
    stdout = statuses["good"] + "[GNUPG:] PLAINTEXT 74 0\n" if valid_status else ""
    with pytest.raises(debverify.VerifyError, match=f"gpgv аварийно завершился.*{returncode}"):
        debverify.check_gpgv_status(
            stdout, FPR, what="runtime", plaintext=plaintext, returncode=returncode
        )


@pytest.fixture
def inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    sig, data, keyring = (tmp_path / name for name in ("runtime.sig", "runtime", "keyring"))
    for path in (sig, data, keyring):
        path.write_text("test", encoding="utf-8")
    return sig, data, keyring


def fake_gpgv(tmp_path: Path, stdout: str, args: list[str], rc: int = 0, stderr: str = "") -> str:
    script = tmp_path / "gpgv"
    script.write_text(
        f"#!{sys.executable}\n"
        "import os, resource, signal, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if '--output' in args:\n"
        "    index = args.index('--output')\n"
        "    Path(args[index + 1]).write_text('signed text', encoding='utf-8')\n"
        "    del args[index:index + 2]\n"
        f"assert args == {args!r}, args\n"
        f"sys.stdout.write({stdout!r})\n"
        f"sys.stderr.write({stderr!r})\n"
        "sys.stdout.flush()\n"
        f"if {rc} < 0:\n"
        "    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
        f"    signal.signal(-{rc}, signal.SIG_DFL)\n"
        f"    os.kill(os.getpid(), -{rc})\n"
        f"sys.exit({rc})\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return str(script)


def detached_args(inputs: tuple[Path, Path, Path]) -> list[str]:
    sig, data, keyring = inputs
    return ["--status-fd", "1", "--keyring", str(keyring), str(sig), str(data)]


@pytest.mark.parametrize("signer", [FPR, SUBKEY])
def test_verify_detached(
    tmp_path: Path, inputs: tuple[Path, Path, Path], statuses: dict[str, str], signer: str
) -> None:
    gpgv = fake_gpgv(tmp_path, statuses["good"], detached_args(inputs))
    debverify.verify_detached(*inputs, signer, gpgv=gpgv)


@pytest.mark.parametrize(("case", "message"), REJECTED)
def test_verify_detached_rejects_rc_zero(
    tmp_path: Path,
    inputs: tuple[Path, Path, Path],
    statuses: dict[str, str],
    case: str,
    message: str,
) -> None:
    gpgv = fake_gpgv(tmp_path, statuses[case], detached_args(inputs), rc=0)
    with pytest.raises(debverify.VerifyError, match=message):
        debverify.verify_detached(*inputs, FPR, gpgv=gpgv)


@pytest.mark.parametrize("rc", [1, 2])
def test_verify_detached_nonzero_rc(
    tmp_path: Path, inputs: tuple[Path, Path, Path], statuses: dict[str, str], rc: int
) -> None:
    stdout = statuses["good"]
    if rc == 2:
        stdout += "[GNUPG:] ERRSIG UNKNOWN 1 8 00 1 9\n[GNUPG:] NO_PUBKEY UNKNOWN\n"
    gpgv = fake_gpgv(tmp_path, stdout, detached_args(inputs), rc=rc)
    with pytest.raises(debverify.VerifyError, match=f"gpgv завершился с кодом {rc}"):
        debverify.verify_detached(*inputs, FPR, gpgv=gpgv)


@pytest.mark.parametrize("rc", [1, 2])
def test_verify_detached_nonzero_rc_includes_stderr_tail(
    tmp_path: Path, inputs: tuple[Path, Path, Path], statuses: dict[str, str], rc: int
) -> None:
    stderr = "discarded diagnostic\n" * 30 + "last diagnostic\n  "
    gpgv = fake_gpgv(tmp_path, statuses["good"], detached_args(inputs), rc=rc, stderr=stderr)
    with pytest.raises(debverify.VerifyError) as error:
        debverify.verify_detached(*inputs, FPR, gpgv=gpgv)
    assert str(error.value) == (f"runtime: gpgv завершился с кодом {rc}: {stderr[-200:].strip()}")


@pytest.mark.parametrize("rc", [0, 1, 2, 128, 134, 139, -6])
@pytest.mark.parametrize("plaintext", [False, True])
def test_verifiers_returncode(
    tmp_path: Path,
    inputs: tuple[Path, Path, Path],
    statuses: dict[str, str],
    rc: int,
    plaintext: bool,
) -> None:
    sig, data, keyring = inputs
    stdout = statuses["good"]
    args = detached_args(inputs)
    if plaintext:
        data.write_text(
            "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nsigned text\n"
            "-----BEGIN PGP SIGNATURE-----\nfake\n-----END PGP SIGNATURE-----\n",
            encoding="utf-8",
        )
        stdout += "[GNUPG:] PLAINTEXT 74 0\n"
        args = ["--status-fd", "1", "--keyring", str(keyring), str(data)]
    gpgv = fake_gpgv(tmp_path, stdout, args, rc=rc)

    def verify() -> None:
        if plaintext:
            assert debverify.gpgv_verify(data, keyring, FPR, gpgv=gpgv) == "signed text"
        else:
            debverify.verify_detached(sig, data, keyring, FPR, gpgv=gpgv)

    if rc < 0 or rc >= 128:
        with pytest.raises(debverify.VerifyError, match=f"gpgv аварийно завершился.*{rc}"):
            verify()
    elif rc != 0 and not plaintext:
        with pytest.raises(debverify.VerifyError, match=f"gpgv завершился с кодом {rc}"):
            verify()
    else:
        verify()


@pytest.mark.parametrize("missing", [0, 1, 2])
def test_verify_detached_missing_file(inputs: tuple[Path, Path, Path], missing: int) -> None:
    inputs[missing].unlink()
    with pytest.raises(debverify.VerifyError, match="нет (файла|ключа подписи)"):
        debverify.verify_detached(*inputs, FPR, gpgv="/nonexistent/gpgv")


def test_verify_detached_missing_gpgv(inputs: tuple[Path, Path, Path]) -> None:
    with pytest.raises(debverify.VerifyError, match="не запускается"):
        debverify.verify_detached(*inputs, FPR, gpgv="/nonexistent/gpgv")


@pytest.mark.parametrize(("case", "message"), [("good", "")] + REJECTED)
def test_cli_verify_sig(
    tmp_path: Path,
    inputs: tuple[Path, Path, Path],
    statuses: dict[str, str],
    case: str,
    message: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_gpgv(tmp_path, statuses[case], detached_args(inputs))
    monkeypatch.setenv("PATH", str(tmp_path))
    sig, data, keyring = inputs
    proc = subprocess.run(
        [
            sys.executable,
            str(HERE / "debverify.py"),
            "verify-sig",
            "--keyring",
            str(keyring),
            "--want-fpr",
            FPR,
            str(sig),
            str(data),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if case == "good":
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout == f"подпись: ключ {FPR} — OK\n"
        assert proc.stderr == ""
    else:
        assert proc.returncode == 1
        assert proc.stdout == ""
        assert proc.stderr.startswith("ОШИБКА: ")
        assert re.search(message, proc.stderr)


@pytest.mark.parametrize("command", ["debs", "sources"])
@pytest.mark.parametrize("option", ["--lock", "--cache"])
def test_cli_requires_debian_options(command: str, option: str) -> None:
    with pytest.raises(SystemExit) as exc:
        debverify.main([option, "unused", command])
    assert exc.value.code == 2


@pytest.mark.parametrize("command", ["debs", "sources"])
@pytest.mark.parametrize("deb_dir", [None, "copied-debs"])
def test_cli_preserves_debian_options(
    monkeypatch: pytest.MonkeyPatch, command: str, deb_dir: str | None
) -> None:
    lock = Mock(debs=[])
    load_lock = Mock(return_value=lock)
    verifier = Mock()
    verifier.verify_debs.return_value = []
    verifier.verify_sources.return_value = []
    factory = Mock(return_value=verifier)
    monkeypatch.setattr(debverify.lockfile, "load", load_lock)
    monkeypatch.setattr(debverify, "Verifier", factory)
    args = ["--lock", "test.lock", "--cache", "cache", "--root", "root"]
    if deb_dir:
        args += ["--deb-dir", deb_dir]
    assert debverify.main([*args, command]) == 0
    load_lock.assert_called_once_with(Path("test.lock"))
    factory.assert_called_once_with(
        lock, Path("cache"), Path("root"), deb_dir=Path(deb_dir) if deb_dir else None
    )
    getattr(verifier, f"verify_{command}").assert_called_once_with()


@pytest.mark.parametrize("plaintext", [False, True])
def test_verifiers_share_checker_and_whitelist(
    tmp_path: Path,
    inputs: tuple[Path, Path, Path],
    statuses: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    plaintext: bool,
) -> None:
    sig, data, keyring = inputs
    stdout = statuses["good"]
    args = detached_args(inputs)
    if plaintext:
        data.write_text(
            "-----BEGIN PGP SIGNED MESSAGE-----\nHash: SHA256\n\nsigned text\n"
            "-----BEGIN PGP SIGNATURE-----\nfake\n-----END PGP SIGNATURE-----\n",
            encoding="utf-8",
        )
        stdout += "[GNUPG:] PLAINTEXT 74 0\n"
        # Debian допускает rc=2 при дополнительной подписи неизвестным ключом.
        stdout += "[GNUPG:] ERRSIG UNKNOWN 1 8 00 1 9\n[GNUPG:] NO_PUBKEY UNKNOWN\n"
        args = ["--status-fd", "1", "--keyring", str(keyring), str(data)]
    gpgv = fake_gpgv(tmp_path, stdout, args, rc=2 if plaintext else 0)
    checker = debverify.check_gpgv_status
    allowed = debverify._ALLOWED_STATUS
    calls: list[tuple[str, str, str, bool, int]] = []

    def spy(stdout: str, signer: str, *, what: str, plaintext: bool, returncode: int) -> None:
        assert checker.__globals__["_ALLOWED_STATUS"] is allowed
        calls.append((stdout, signer, what, plaintext, returncode))
        checker(stdout, signer, what=what, plaintext=plaintext, returncode=returncode)

    monkeypatch.setattr(debverify, "check_gpgv_status", spy)

    def verify() -> None:
        if plaintext:
            assert debverify.gpgv_verify(data, keyring, FPR, gpgv=gpgv) == "signed text"
        else:
            debverify.verify_detached(sig, data, keyring, FPR, gpgv=gpgv)

    verify()
    assert calls == [(stdout, FPR, data.name, plaintext, 2 if plaintext else 0)]
    # Подмена единственного объекта влияет на оба пути: скрытая копия не допускается.
    allowed = allowed - {"NEWSIG"}
    monkeypatch.setattr(debverify, "_ALLOWED_STATUS", allowed)
    with pytest.raises(debverify.VerifyError, match=r"плохая подпись \(NEWSIG\)"):
        verify()
    assert len(calls) == 2
