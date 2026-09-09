# -*- coding: utf-8 -*-
"""Unit-тесты помощника S2 (docs/threat-model.md §4.1 п.10, §6 T-01…T-07, T-17…T-30).

Изоляция (урок .patches/002): ни одного вызова pkexec, ни одного окна, ни одного
обращения к D-Bus, ничего не ставится в систему. Помощник запускается из
ТЕСТОВОГО chroot-каталога (tmp_path), все внешние программы — по абсолютным путям
внутри этого каталога; PATH помощник игнорирует (проверяется отдельно, T-04).

Запуск:  ~/.cache/astra-voice-spike/s2venv/bin/python -m pytest tests/ -q
"""

import ast
import fcntl
import json
import os
import shutil
import stat
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SPIKE = os.path.dirname(HERE)
HELPER_SRC = os.path.join(SPIKE, "update-helper")
KEYRING_SRC = os.path.join(SPIKE, "keys", "test-release.gpg")
PACKAGES = os.path.join(SPIKE, "packages")
FIXTURES = os.path.join(HERE, "fixtures")

DEB1 = "astra-voice-spike_0.0.1_all.deb"
DEB2 = "astra-voice-spike_0.0.2_all.deb"

REAL = {"gpgv": "/usr/bin/gpgv", "dpkg-deb": "/usr/bin/dpkg-deb", "dpkg": "/usr/bin/dpkg"}

WRAPPER = '''#!/usr/bin/env python3
import json, os, subprocess, sys
rec = {{"argv": sys.argv, "cwd": os.getcwd(), "env": dict(os.environ)}}
proc = subprocess.run([{real!r}] + sys.argv[1:], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
rec["rc"] = proc.returncode
with open({calls!r}, "a") as fh:
    fh.write(json.dumps(rec) + "\\n")
sys.stdout.buffer.write(proc.stdout)
sys.stderr.buffer.write(proc.stderr)
sys.exit(proc.returncode)
'''

FAKE_APT = '''#!/usr/bin/env python3
import json, os, sys
rec = {{"argv": sys.argv, "cwd": os.getcwd(), "env": dict(os.environ)}}
with open({calls!r}, "a") as fh:
    fh.write(json.dumps(rec) + "\\n")
rc, out = 0, ""
if os.path.exists({rc_file!r}):
    rc = int(open({rc_file!r}).read().strip())
if os.path.exists({out_file!r}):
    out = open({out_file!r}).read()
sys.stderr.write(out)
sys.exit(rc)
'''

FAKE_QUERY = '''#!/usr/bin/env python3
import json, os, sys
rec = {{"argv": sys.argv, "cwd": os.getcwd(), "env": dict(os.environ)}}
with open({calls!r}, "a") as fh:
    fh.write(json.dumps(rec) + "\\n")
if not os.path.exists({ver_file!r}):
    sys.exit(1)
sys.stdout.write(open({ver_file!r}).read().strip())
'''

EVIL = '#!/bin/sh\ntouch "%s"\nexit 0\n'


# --- инфраструктура -------------------------------------------------------------

def _write_exec(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, 0o755)


@pytest.fixture()
def chroot(tmp_path):
    """Тестовый chroot-каталог: помощник + фейки внешних программ по абс. путям."""
    root = tmp_path / "chroot"
    for sub in ("usr/libexec/astra-voice", "usr/bin", "usr/share/astra-voice/keys",
                "etc/astra-voice", "etc/digsig", "calls", "fix", "var/lib"):
        (root / sub).mkdir(parents=True, exist_ok=True)

    helper = root / "usr/libexec/astra-voice/update-helper"
    shutil.copy2(HELPER_SRC, helper)
    os.chmod(helper, 0o755)
    (root / "etc/astra-voice/SPIKE-TEST-MODE").write_text("S2 spike unit tests\n")
    shutil.copy2(KEYRING_SRC, root / "usr/share/astra-voice/keys/test-release.gpg")

    calls = root / "calls"
    for tool, real in REAL.items():
        _write_exec(str(root / "usr/bin" / tool),
                    WRAPPER.format(real=real, calls=str(calls / (tool + ".jsonl"))))
    _write_exec(str(root / "usr/bin/apt-get"),
                FAKE_APT.format(calls=str(calls / "apt-get.jsonl"),
                                rc_file=str(root / "fix/apt_rc"),
                                out_file=str(root / "fix/apt_out")))
    _write_exec(str(root / "usr/bin/dpkg-query"),
                FAKE_QUERY.format(calls=str(calls / "dpkg-query.jsonl"),
                                  ver_file=str(root / "fix/installed_version")))
    return root


def run_helper(root, args, env=None):
    helper = str(root / "usr/libexec/astra-voice/update-helper")
    return subprocess.run([helper] + list(args), stdout=subprocess.PIPE,
                          stderr=subprocess.PIPE, stdin=subprocess.DEVNULL,
                          env=env if env is not None else dict(os.environ), timeout=300)


def out_json(proc):
    return json.loads(proc.stdout.decode("utf-8").strip().splitlines()[-1])


def calls(root, tool):
    path = root / "calls" / (tool + ".jsonl")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def make_src(tmp_path, name="src", deb=DEB2, sums=None, asc=None, extra=None):
    """Каталог-источник: .deb + SHA256SUMS + SHA256SUMS.asc (как отдаёт GUI)."""
    src = tmp_path / name
    src.mkdir()
    if deb:
        shutil.copy2(os.path.join(PACKAGES, deb) if os.path.dirname(deb) == "" else deb,
                     src / os.path.basename(deb))
    shutil.copy2(sums or os.path.join(PACKAGES, "SHA256SUMS"), src / "SHA256SUMS")
    shutil.copy2(asc or os.path.join(PACKAGES, "SHA256SUMS.asc"), src / "SHA256SUMS.asc")
    for rel, data in (extra or {}).items():
        (src / rel).write_bytes(data)
    return src


def deb_arg(src, name=DEB2):
    return str(src / name)


# --- T-03: argv ------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    [],
    ["install"],
    ["install", "/abs/a.deb", "/abs/b.deb"],
    ["install", "../x.deb"],
    ["install", "--help"],
    ["install", "x.deb"],
    ["install", "/var/tmp/../x.deb"],
    ["install", "/var/tmp/x.txt"],
    ["remove", "/var/tmp/x.deb"],
    ["--help"],
])
def test_t03_argv_rejected(chroot, args):
    proc = run_helper(chroot, args)
    assert proc.returncode == 64, proc.stdout
    assert out_json(proc)["result"] == "usage"
    for tool in ("gpgv", "apt-get", "dpkg-deb", "dpkg", "dpkg-query"):
        assert calls(chroot, tool) == [], "запущен подпроцесс %s при неверном argv" % tool


def test_install_ok(chroot, tmp_path):
    src = make_src(tmp_path)
    proc = run_helper(chroot, ["install", deb_arg(src)])
    payload = out_json(proc)
    assert proc.returncode == 0, payload
    assert payload["result"] == "ok"
    assert payload["package"] == "astra-voice-spike"
    assert payload["version"] == "0.0.2"
    apt = calls(chroot, "apt-get")
    assert len(apt) == 1
    assert apt[0]["argv"][1:] == ["install", "-y", "--no-remove", "./" + DEB2]
    assert "--allow-unauthenticated" not in apt[0]["argv"]      # T-18, вторая половина
    assert apt[0]["cwd"] == str(chroot / "var/lib/astra-voice/staging")
    staging = chroot / "var/lib/astra-voice/staging"
    assert stat.S_IMODE(os.stat(str(staging)).st_mode) == 0o700
    assert (staging / DEB2).exists()


# --- T-04: окружение --------------------------------------------------------------

def test_t04_environment_is_fixed(chroot, tmp_path):
    evil = tmp_path / "evil"
    evil.mkdir()
    markers = {}
    for tool in ("gpgv", "apt-get", "dpkg-deb", "dpkg", "dpkg-query"):
        markers[tool] = tmp_path / ("evil-" + tool)
        _write_exec(str(evil / tool), EVIL % markers[tool])
    # ловушка на импорт: сработает, только если -I не отработал и PYTHONPATH учтён
    hashlib_marker = tmp_path / "evil-import"
    (evil / "hashlib.py").write_text("open(%r, 'w').write('x')\n" % str(hashlib_marker))
    (evil / "sitecustomize.py").write_text("open(%r, 'w').write('x')\n" % str(hashlib_marker))

    hostile = dict(os.environ)
    hostile.update({
        "PATH": str(evil) + ":/usr/bin:/bin",
        "PYTHONPATH": str(evil),
        "PYTHONSTARTUP": str(evil / "sitecustomize.py"),
        "LD_PRELOAD": str(evil / "nonexistent.so"),
        "LD_LIBRARY_PATH": str(evil),
        "GNUPGHOME": str(evil),
        "DEBIAN_FRONTEND": "teletype",
        "IFS": " ",
    })
    src = make_src(tmp_path)
    proc = run_helper(chroot, ["install", deb_arg(src)], env=hostile)
    assert proc.returncode == 0, proc.stdout + proc.stderr

    for tool, marker in markers.items():
        assert not marker.exists(), "помощник нашёл %s через PATH" % tool
    assert not hashlib_marker.exists(), "PYTHONPATH/site не проигнорирован (-IS)"

    apt = calls(chroot, "apt-get")[0]
    assert apt["argv"][0] == str(chroot / "usr/bin/apt-get")
    assert calls(chroot, "gpgv")[0]["argv"][0] == str(chroot / "usr/bin/gpgv")
    env = apt["env"]
    assert env == {
        "PATH": "/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": "/root",
        "SHELL": "/bin/false",
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "DEBIAN_FRONTEND": "noninteractive",
    }, env


# --- T-05: ключи ------------------------------------------------------------------

def test_t05_foreign_key(chroot, tmp_path):
    src = make_src(tmp_path, asc=os.path.join(FIXTURES, "asc_foreign", "SHA256SUMS.asc"))
    proc = run_helper(chroot, ["install", deb_arg(src)])
    assert proc.returncode == 67
    assert out_json(proc)["result"] == "bad-signature"
    assert calls(chroot, "apt-get") == []


def test_t05_revoked_key(chroot, tmp_path):
    """Ключ ЕСТЬ в keyring и gpgv его принимает — отказ обязан дать наш код."""
    src = make_src(tmp_path, asc=os.path.join(FIXTURES, "asc_revoked", "SHA256SUMS.asc"))
    proc = run_helper(chroot, ["install", deb_arg(src)])
    payload = out_json(proc)
    assert proc.returncode == 67
    assert payload["result"] == "bad-signature"
    assert "отозван" in payload["message"]
    assert calls(chroot, "apt-get") == []
    # gpgv сам по себе сказал бы «хорошо»: фиксируем, что без пина проверка дырявая
    gpgv = calls(chroot, "gpgv")[0]
    assert gpgv["rc"] == 0


def test_t05_second_keyring_key_ok(chroot, tmp_path):
    """Плановая ротация: подпись S2 из того же keyring принимается без правки кода."""
    src = make_src(tmp_path, asc=os.path.join(FIXTURES, "asc_s2", "SHA256SUMS.asc"))
    proc = run_helper(chroot, ["install", deb_arg(src)])
    assert proc.returncode == 0, proc.stdout
    assert out_json(proc)["fingerprint"] == "53648D58F2B03C06CCE80580E137446C3835E961"


def test_signature_tampered_sums(chroot, tmp_path):
    src = make_src(tmp_path)
    with open(str(src / "SHA256SUMS"), "ab") as fh:
        fh.write(b"# tampered\n")
    proc = run_helper(chroot, ["install", deb_arg(src)])
    assert proc.returncode == 67
    assert calls(chroot, "apt-get") == []


# --- T-30 / суммы -----------------------------------------------------------------

def test_t30_duplicate_sums_line(chroot, tmp_path):
    src = make_src(tmp_path,
                   sums=os.path.join(FIXTURES, "dup", "SHA256SUMS"),
                   asc=os.path.join(FIXTURES, "dup", "SHA256SUMS.asc"))
    proc = run_helper(chroot, ["install", deb_arg(src)])
    assert proc.returncode == 68
    assert out_json(proc)["result"] == "bad-checksum"
    assert calls(chroot, "apt-get") == []


def test_bad_checksum(chroot, tmp_path):
    src = make_src(tmp_path,
                   sums=os.path.join(FIXTURES, "badsum", "SHA256SUMS"),
                   asc=os.path.join(FIXTURES, "badsum", "SHA256SUMS.asc"))
    proc = run_helper(chroot, ["install", deb_arg(src)])
    assert proc.returncode == 68
    assert calls(chroot, "apt-get") == []


def test_bad_package_name(chroot, tmp_path):
    src = tmp_path / "other"
    shutil.copytree(os.path.join(FIXTURES, "other"), str(src))
    proc = run_helper(chroot, ["install", str(src / "astra-voice-other_0.0.3_all.deb")])
    assert proc.returncode == 69
    assert out_json(proc)["result"] == "bad-package"
    assert calls(chroot, "apt-get") == []


# --- T-07: downgrade ---------------------------------------------------------------

def test_t07_downgrade_refused(chroot, tmp_path):
    (chroot / "fix/installed_version").write_text("0.0.2\n")
    src = make_src(tmp_path, deb=DEB1)
    proc = run_helper(chroot, ["install", deb_arg(src, DEB1)])
    payload = out_json(proc)
    assert proc.returncode == 70
    assert payload["result"] == "downgrade"
    assert payload["installed"] == "0.0.2"
    assert calls(chroot, "apt-get") == []


def test_upgrade_allowed(chroot, tmp_path):
    (chroot / "fix/installed_version").write_text("0.0.1\n")
    src = make_src(tmp_path)
    proc = run_helper(chroot, ["install", deb_arg(src)])
    assert proc.returncode == 0, proc.stdout
    assert len(calls(chroot, "apt-get")) == 1


def test_same_version_refused(chroot, tmp_path):
    (chroot / "fix/installed_version").write_text("0.0.2\n")
    src = make_src(tmp_path)
    proc = run_helper(chroot, ["install", deb_arg(src)])
    assert proc.returncode == 70
    assert calls(chroot, "apt-get") == []


# --- T-01 / T-02: symlink и staging -------------------------------------------------

def test_t01_deb_is_symlink(chroot, tmp_path):
    src = make_src(tmp_path)
    os.unlink(str(src / DEB2))
    os.symlink(os.path.join(PACKAGES, DEB2), str(src / DEB2))
    proc = run_helper(chroot, ["install", deb_arg(src)])
    payload = out_json(proc)
    assert proc.returncode == 65
    assert payload["result"] == "bad-input"
    assert payload["detail"]["errno"] == 40      # ELOOP
    assert calls(chroot, "apt-get") == []


def test_t02_staging_is_symlink(chroot, tmp_path):
    state = chroot / "var/lib/astra-voice"
    state.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir(mode=0o700)
    os.symlink(str(elsewhere), str(state / "staging"))
    src = make_src(tmp_path)
    proc = run_helper(chroot, ["install", deb_arg(src)])
    assert proc.returncode == 66
    assert out_json(proc)["result"] == "bad-staging"
    assert list(elsewhere.iterdir()) == []
    assert calls(chroot, "apt-get") == []


def test_t02_staging_mode_too_wide(chroot, tmp_path):
    staging = chroot / "var/lib/astra-voice/staging"
    staging.mkdir(parents=True)
    os.chmod(str(staging), 0o755)
    src = make_src(tmp_path)
    proc = run_helper(chroot, ["install", deb_arg(src)])
    assert proc.returncode == 66
    assert list(staging.iterdir()) == []
    assert calls(chroot, "apt-get") == []


# --- T-17 / T-19: политика ----------------------------------------------------------

def test_t17_policy_updates_admin(chroot, tmp_path):
    (chroot / "etc/astra-voice/policy.conf").write_text("[updates]\nupdates=admin\n")
    src = make_src(tmp_path)
    proc = run_helper(chroot, ["install", deb_arg(src)])
    assert proc.returncode == 71
    assert out_json(proc)["result"] == "policy-denied"
    assert calls(chroot, "apt-get") == []
    assert calls(chroot, "gpgv") == []


def test_t17_digsig_elf_mode(chroot, tmp_path):
    (chroot / "etc/digsig/digsig_initramfs.conf").write_text('DIGSIG_ELF_MODE="1"\n')
    src = make_src(tmp_path)
    proc = run_helper(chroot, ["install", deb_arg(src)])
    assert proc.returncode == 71
    assert calls(chroot, "apt-get") == []


def test_digsig_zero_allows(chroot, tmp_path):
    (chroot / "etc/digsig/digsig_initramfs.conf").write_text("DIGSIG_ELF_MODE=0\n")
    src = make_src(tmp_path)
    assert run_helper(chroot, ["install", deb_arg(src)]).returncode == 0


def test_t19_policy_bad_permissions_ignored(chroot, tmp_path):
    policy = chroot / "etc/astra-voice/policy.conf"
    policy.write_text("updates=admin\n")
    os.chmod(str(policy), 0o666)
    src = make_src(tmp_path)
    proc = run_helper(chroot, ["install", deb_arg(src)])
    payload = out_json(proc)
    assert proc.returncode == 0, payload
    assert payload["policy"].startswith("policy-ignored")


def test_offline_adds_no_download(chroot, tmp_path):
    (chroot / "etc/astra-voice/policy.conf").write_text("updates=offline\n")
    src = make_src(tmp_path)
    assert run_helper(chroot, ["install", deb_arg(src)]).returncode == 0
    assert "--no-download" in calls(chroot, "apt-get")[0]["argv"]


# --- T-18 / T-29 / T-27: apt, блокировка, журнал -------------------------------------

def test_t18_apt_locked(chroot, tmp_path):
    (chroot / "fix/apt_rc").write_text("100\n")
    (chroot / "fix/apt_out").write_text(
        "E: Could not get lock /var/lib/dpkg/lock-frontend. It is held by process 1\n")
    src = make_src(tmp_path)
    proc = run_helper(chroot, ["install", deb_arg(src)])
    assert proc.returncode == 72
    assert out_json(proc)["result"] == "apt-locked"


def test_apt_failed(chroot, tmp_path):
    (chroot / "fix/apt_rc").write_text("100\n")
    (chroot / "fix/apt_out").write_text("E: Unable to correct problems\n")
    src = make_src(tmp_path)
    proc = run_helper(chroot, ["install", deb_arg(src)])
    assert proc.returncode == 73


def test_t29_busy_when_locked(chroot, tmp_path):
    state = chroot / "var/lib/astra-voice"
    state.mkdir(parents=True)
    fd = os.open(str(state / "update.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        src = make_src(tmp_path)
        proc = run_helper(chroot, ["install", deb_arg(src)])
        assert proc.returncode == 74
        assert out_json(proc)["result"] == "busy"
        assert calls(chroot, "apt-get") == []
    finally:
        os.close(fd)


def test_t27_journal_line(chroot, tmp_path):
    src = make_src(tmp_path)
    assert run_helper(chroot, ["install", deb_arg(src)]).returncode == 0
    log = chroot / "var/lib/astra-voice/updates.log"
    lines = log.read_text().strip().splitlines()
    assert len(lines) == 1
    line = lines[0]
    assert "result=ok" in line and "version=0.0.2" in line and "package=astra-voice-spike" in line
    assert "sha256=" in line
    assert os.path.expanduser("~") not in line          # без путей $HOME
    assert stat.S_IMODE(os.stat(str(log)).st_mode) == 0o644
    run_helper(chroot, ["install", "x.deb"])
    assert len(log.read_text().strip().splitlines()) == 2


def test_shebang_and_stdlib_only():
    with open(HELPER_SRC, "rb") as fh:
        assert fh.readline().strip() == b"#!/usr/bin/python3 -IS"
    text = open(HELPER_SRC, encoding="utf-8").read()
    assert "sys.path" not in text.split('"""', 2)[2]     # sys.path не правится
    assert "astra_voice" not in text.split('"""', 2)[2].replace("astra_voice.update", "")
    tree = ast.parse(text)
    imports = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imports.add((node.module or "").split(".")[0])
    assert imports <= {"errno", "fcntl", "hashlib", "json", "os", "re", "stat",
                       "subprocess", "sys", "time", "syslog"}, imports
