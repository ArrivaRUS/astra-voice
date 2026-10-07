"""T-191/T-196: deletion authority and loader environment at the AppRun boundary.

Only shell stubs run; all bundles, command logs and deletion targets are in tmp_path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from astra_voice.core import childenv
from helpers.appimage_bundle import KEY, id_shim, make_bundle

pytestmark = pytest.mark.unit
APPRUN = Path(__file__).resolve().parents[2] / "packaging/appimage/AppRun"

STUB_PYTHON = """#!/bin/sh
shift 2
command=$1
shift
printf '%s\\n' "$command" >> "$HARDENING_CALLS"
case $command in
    selfinstall) exit "$HARDENING_INSTALL_RC" ;;
    app) env > "$HARDENING_ENV"; exit "$HARDENING_APP_RC" ;;
esac
exit 99
"""


@pytest.fixture
def sandbox(tmp_path: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    tmp = home / "cache"
    tmp.mkdir(mode=0o700)
    return id_shim(
        tmp_path,
        {
            "PATH": "/usr/bin:/bin",
            "HOME": str(home),
            "TMPDIR": str(tmp),
            "LANG": "C.UTF-8",
            "HARDENING_CALLS": str(tmp_path / "calls"),
            "HARDENING_ENV": str(tmp_path / "app.env"),
            "HARDENING_INSTALL_RC": "10",
            "HARDENING_APP_RC": "0",
        },
    )


def _bundle(root: Path) -> Path:
    bundle = make_bundle(root, apprun=APPRUN.read_text(encoding="utf-8"))
    bundle.chmod(0o700)
    python = bundle / "opt/python3.11/bin/python3.11"
    python.write_text(STUB_PYTHON, encoding="ascii")
    python.chmod(0o755)
    return bundle


def _run(bundle: Path, env: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(bundle / "AppRun"), *args],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
    )


def _environment(env: dict[str, str]) -> dict[str, str]:
    return dict(
        line.split("=", 1)
        for line in Path(env["HARDENING_ENV"]).read_text(encoding="utf-8").splitlines()
        if "=" in line
    )


@pytest.mark.parametrize("variable", ["LD_LIBRARY_PATH", "LD_AUDIT"])
@pytest.mark.parametrize("value", [None, "", "/nonexistent/loader-path:"])
@pytest.mark.parametrize("nested", [False, True])
def test_loader_variables_removed_and_restored(
    tmp_path: Path, sandbox: dict[str, str], variable: str, value: str | None, nested: bool
) -> None:
    """The app gets no host loader override; children retain unset vs empty vs value."""
    source = _bundle(tmp_path / "manual-bundle")
    env = {**sandbox, "ASTRA_VOICE_PORTABLE": "1"}
    if value is not None:
        env[variable] = value
    assert _run(source, env, "--version").returncode == 0
    launched = _environment(env)
    if nested:
        second = _bundle(tmp_path / "second-bundle")
        assert _run(second, launched, "--version").returncode == 0
        launched = _environment(env)
    assert variable not in launched
    before = launched.copy()
    for options in ({}, {"keep_pulse_config": True}, {"private_tmp": True}):
        external = childenv.clean_env(launched, **options)
        if value is None:
            assert variable not in external
        else:
            assert external[variable] == value
        assert not any(name.startswith("ASTRA_VOICE_ORIG_") for name in external)
    assert launched == before


@pytest.mark.parametrize("colon", [False, True])
@pytest.mark.parametrize(
    "unsafe",
    ["directory", "parent", "symlink-marker", "bad-marker", "symlink-parent", "symlink-dir"],
)
def test_cleanup_preserves_untrusted_extractions(
    sandbox: dict[str, str], colon: bool, unsafe: str
) -> None:
    """A matching filename alone never grants authority to remove a directory tree."""
    parent = Path(sandbox["TMPDIR"]) / ("a:b" if colon else "parent")
    parent.mkdir(mode=0o700)
    extracted = _bundle(parent / "appimage_extracted_case")
    launched = extracted
    sentinel = extracted / "keep-me"
    sentinel.write_bytes(b"user data")
    if unsafe in {"directory", "parent"}:
        (extracted if unsafe == "directory" else parent).chmod(0o777)
    elif unsafe in {"symlink-marker", "bad-marker"}:
        marker = extracted / ".astra-voice-build"
        if unsafe == "bad-marker":
            marker.write_text("VERSION=bad\nBUILD_ID=not-a-build\n", encoding="ascii")
        else:
            original = parent / "external-marker"
            marker.rename(original)
            marker.symlink_to(original)
    elif unsafe == "symlink-parent":
        link = Path(sandbox["TMPDIR"]) / "launch-parent"
        link.symlink_to(parent, target_is_directory=True)
        launched = link / extracted.name
    else:
        launched = parent / "appimage_extracted_link"
        launched.symlink_to(extracted, target_is_directory=True)
    # Observe attempted deletion, not just the outcome of filesystem permissions.
    rm = Path(sandbox["PATH"].split(":", 1)[0]) / "rm"
    rm.write_text('#!/bin/sh\nprintf "rm\\n" >> "$HARDENING_CALLS"\nexit 0\n')
    rm.chmod(0o755)
    proc = _run(launched, sandbox, "--hidden")
    if colon:
        assert proc.returncode == 1
        assert "двоеточие" in proc.stderr
    calls = Path(sandbox["HARDENING_CALLS"])
    assert not calls.exists() or "rm" not in calls.read_text().splitlines()
    assert sentinel.read_bytes() == b"user data"


@pytest.mark.parametrize("install_rc", [1, 0, 10])
def test_cleanup_on_terminal_install_paths(sandbox: dict[str, str], install_rc: int) -> None:
    """Refused/failed installs retain extraction; skipped installs collect it after app exits."""
    extracted = _bundle(Path(sandbox["TMPDIR"]) / "appimage_extracted_valid")
    env = {**sandbox, "HARDENING_INSTALL_RC": str(install_rc), "HARDENING_APP_RC": "7"}
    proc = _run(extracted, env, "--hidden")
    assert proc.returncode == (7 if install_rc == 10 else 1)
    assert extracted.exists() == (install_rc != 10)
    calls = Path(env["HARDENING_CALLS"]).read_text().splitlines()
    assert calls == (["selfinstall", "app"] if install_rc == 10 else ["selfinstall"])


def test_cleanup_on_installed_fast_path(sandbox: dict[str, str]) -> None:
    """An already installed valid copy needs no reinstall; its temporary source is collected."""
    extracted = _bundle(Path(sandbox["TMPDIR"]) / "appimage_extracted_valid")
    app = Path(sandbox["HOME"]) / ".local/share/astra-voice/app"
    target = _bundle(app / KEY)
    (target / ".installed-ok").touch()
    (app / "current").symlink_to(KEY)
    proc = _run(extracted, sandbox, "--hidden")
    assert proc.returncode == 0, proc.stderr
    assert not extracted.exists()
    assert target.is_dir()
    assert Path(sandbox["HARDENING_CALLS"]).read_text().splitlines() == ["app"]


def test_parent_changed_to_symlink_while_app_runs_is_preserved(
    sandbox: dict[str, str],
) -> None:
    """A safe launch does not authorize cleanup after its parent has been replaced."""
    parent = Path(sandbox["TMPDIR"]) / "parent"
    parent.mkdir(mode=0o700)
    extracted = _bundle(parent / "appimage_extracted_valid")
    python = extracted / "opt/python3.11/bin/python3.11"
    python.write_text(
        STUB_PYTHON.replace(
            "app) env",
            'app) mv "$HARDENING_PARENT" "$HARDENING_PARENT.saved"; '
            'ln -s "$HARDENING_PARENT.saved" "$HARDENING_PARENT"; env',
        ),
        encoding="ascii",
    )
    rm = Path(sandbox["PATH"].split(":", 1)[0]) / "rm"
    rm.write_text('#!/bin/sh\nprintf "rm\\n" >> "$HARDENING_CALLS"\nexit 0\n')
    rm.chmod(0o755)
    env = {**sandbox, "HARDENING_PARENT": str(parent)}
    proc = _run(extracted, env, "--hidden")
    assert proc.returncode == 0, proc.stderr
    assert parent.is_symlink()
    assert extracted.is_dir()
    assert Path(env["HARDENING_CALLS"]).read_text().splitlines() == ["selfinstall", "app"]


def test_valid_private_colon_extraction_is_removed(sandbox: dict[str, str]) -> None:
    parent = Path(sandbox["TMPDIR"]) / "a:b"
    parent.mkdir(mode=0o700)
    extracted = _bundle(parent / "appimage_extracted_valid")
    proc = _run(extracted, sandbox, "--hidden")
    assert proc.returncode == 1
    assert "двоеточие" in proc.stderr
    assert not extracted.exists()


@pytest.mark.parametrize("colon", [False, True])
def test_shared_tmpdir_does_not_authorize_cleanup(sandbox: dict[str, str], colon: bool) -> None:
    """Being directly below TMPDIR does not bypass the private-parent requirement."""
    shared = Path(sandbox["TMPDIR"]) / ("shared:tmp" if colon else "shared-tmp")
    shared.mkdir(mode=0o1777)
    shared.chmod(0o1777)
    extracted = _bundle(shared / "appimage_extracted_shared")
    env = {**sandbox, "TMPDIR": str(shared)}
    rm = Path(sandbox["PATH"].split(":", 1)[0]) / "rm"
    rm.write_text('#!/bin/sh\nprintf "rm\\n" >> "$HARDENING_CALLS"\nexit 0\n')
    rm.chmod(0o755)
    proc = _run(extracted, env, "--hidden")
    assert proc.returncode == (1 if colon else 0)
    calls = Path(env["HARDENING_CALLS"])
    assert not calls.exists() or "rm" not in calls.read_text().splitlines()
    assert extracted.is_dir()


@pytest.mark.parametrize(
    "spelling", ["/./AppRun", "/./././AppRun", "/child/../AppRun", "///AppRun"]
)
@pytest.mark.parametrize("private_parent", [False, True])
@pytest.mark.parametrize("colon", [False, True])
def test_lexical_aliases_require_a_real_private_parent(
    sandbox: dict[str, str], spelling: str, private_parent: bool, colon: bool
) -> None:
    """Repeated visits to the extraction itself cannot count as a protected parent."""
    shared = Path(sandbox["TMPDIR"]) / ("shared:tmp" if colon else "shared-tmp")
    shared.mkdir(mode=0o1777)
    shared.chmod(0o1777)
    parent = shared
    if private_parent:
        parent = shared / "private"
        parent.mkdir(mode=0o700)
    extracted = _bundle(parent / "appimage_extracted_alias")
    (extracted / "child").mkdir(mode=0o700)
    bin_dir = Path(sandbox["PATH"].split(":", 1)[0])
    # Simulate a root-owned sticky parent without chown/sudo or using the real /tmp.
    stat = bin_dir / "stat"
    stat.write_text(
        "#!/bin/sh\nformat=$2\nfor arg do last=$arg; done\n"
        'if [ "$format" = %u ] && '
        '[ "$(readlink -f -- "$last")" = "$HARDENING_SHARED" ]; then\n'
        '    printf "0\\n"\nelse\n    exec /usr/bin/stat "$@"\nfi\n',
        encoding="ascii",
    )
    stat.chmod(0o755)
    rm = bin_dir / "rm"
    rm.write_text('#!/bin/sh\nprintf "rm\\n" >> "$HARDENING_CALLS"\nexit 0\n')
    rm.chmod(0o755)
    env = {**sandbox, "TMPDIR": str(shared), "HARDENING_SHARED": str(shared)}
    # Path would normalize away dots and repeated slashes, masking the regression.
    proc = subprocess.run(
        ["sh", str(extracted) + spelling, "--hidden"],
        env=env,
        text=True,
        capture_output=True,
        timeout=10,
        stdin=subprocess.DEVNULL,
    )
    assert proc.returncode == (1 if colon else 0), proc.stderr
    calls = Path(env["HARDENING_CALLS"])
    entries = calls.read_text().splitlines() if calls.exists() else []
    assert entries.count("rm") == int(private_parent)
    assert extracted.is_dir()  # Every rm in this test is a logging stub.


@pytest.mark.parametrize("colon", [False, True])
def test_manual_bundle_preserved_on_exit(sandbox: dict[str, str], colon: bool) -> None:
    bundle = _bundle(Path(sandbox["TMPDIR"]) / ("manual:bundle" if colon else "manual-bundle"))
    proc = _run(bundle, sandbox, "--version")
    assert proc.returncode == (1 if colon else 0)
    assert bundle.is_dir()
