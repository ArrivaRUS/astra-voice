"""Сборка AppImage без сборки: lock, гейт состава бандла, SBOM, elf-audit, пакет правки CI.

arch/appimage.md §9, §11 п.3; T1 MN-5, MN-9; T-169.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / "packaging" / "appimage.lock"
CONTROL = ROOT / "packaging" / "debian" / "control"


def load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


lockfile = load("appimage_lockfile", ROOT / "packaging" / "appimage" / "lockfile.py")
check_bundle = load("appimage_check_bundle", ROOT / "packaging" / "appimage" / "check_bundle.py")

SHA = "a" * 64
GOOD_LOCK = f"""# base: python-appimage
# openssl-origin: bundled
# openssl-major: 1
# expect-elf: 157
# max-glibc: 2.28
# runtime-key: 570C77ACEA40C0F1B758902CBF96CCA56490F695
# tool: runtime-x86_64 {SHA} 1 https://example.invalid/runtime-x86_64
# tool: runtime-x86_64.sig {SHA} 2 https://example.invalid/runtime-x86_64.sig
# tool: appimagetool-x86_64.AppImage {SHA} 3 https://example.invalid/appimagetool
# tool: python3.11.16-cp311-cp311-manylinux_2_28_x86_64.AppImage {SHA} 4 https://example.invalid/py
# TODO-HASH: jsonschema==4.10.3 py3-none-any
numpy==1.24.2 \\
    --hash=sha256:{SHA}
"""


# --- lock ---------------------------------------------------------------------


def test_runtime_signature_uses_debverify() -> None:
    """T-190: runtime использует общий разбор статусов, без собственного awk."""
    build = (ROOT / "packaging/appimage/build.sh").read_text(encoding="utf-8")
    runtime = build.split("verify_runtime() {", 1)[1].split("\n}", 1)[0]
    assert '[ -f "$RUNTIME_KEYRING" ] || die' in runtime
    assert "want=$(lockq get runtime-key)" in runtime
    assert 'python3 "$HERE/debverify.py" verify-sig' in runtime
    assert '--keyring "$RUNTIME_KEYRING" --want-fpr "$want"' in runtime
    assert '"$DOWNLOADS/runtime-x86_64.sig" "$DOWNLOADS/runtime-x86_64" ||' in runtime
    assert "die 'подпись runtime-x86_64" in runtime
    assert "awk" not in runtime
    assert "VALIDSIG" not in runtime


def test_real_lock_parses_with_pins_from_t1() -> None:
    lock = lockfile.load(LOCK)
    # «Ревизия 3»: база Debian 12, OpenSSL 3 с хоста, порог glibc гибрида.
    assert lock.base == "debian12"
    assert lock.directives["openssl-origin"] == "host"
    assert lock.openssl_major == 3
    assert lock.max_glibc == (2, 36)
    assert lock.runtime_key == "570C77ACEA40C0F1B758902CBF96CCA56490F695"
    assert {tool.name for tool in lock.tools} == set(lockfile.REQUIRED_TOOLS)
    assert all(tool.url.startswith("https://github.com/") for tool in lock.tools)
    assert sorted(deb.package for deb in lock.debs) == sorted(lockfile.PYTHON_DEBS)
    assert len({deb.version for deb in lock.debs}) == 1
    assert all(deb.url.startswith("https://snapshot.debian.org/") for deb in lock.debs)
    assert lock.todo_pin == []
    names = {req.name for req in lock.requirements} | {name for name, _ in lock.todo}
    # arch/appimage.md §11 п.1: схема каталога в бандле обязательна.
    assert {"jsonschema", "attrs", "pyrsistent"} <= names
    assert ("jsonschema", "4.10.3") in lock.todo or any(
        req.name == "jsonschema" and req.version == "4.10.3" for req in lock.requirements
    )
    # Манифест хоста (R3.2): OpenSSL и базовые библиотеки Python — с хоста; удалённые
    # модулями readline/gdbm/dbm/curses библиотеки в нём не значатся.
    assert {"libssl.so.3", "libcrypto.so.3", "libz.so.1", "libexpat.so.1", "libffi.so.8"} <= set(
        lock.host_libs
    )
    for soname in ("libreadline.so.8", "libgdbm.so.6", "libdb-5.3.so", "libncursesw.so.6"):
        assert soname not in lock.host_libs
    # Колёса прежние (R3: меняется только интерпретатор).
    pins = {req.name: req.version for req in lock.requirements}
    assert pins["PyQt5"] == "5.15.11"
    assert pins["PyQt5-Qt5"] == "5.15.19"


def test_engine_pins_match_deb_lock() -> None:
    """onnxruntime/onnx-asr в AppImage — те же колёса, что в .deb."""
    deb = (ROOT / "packaging" / "wheels.lock").read_text(encoding="utf-8")
    deb_pins = {req.name: req for req in lockfile.parse(_deb_lock_as_appimage(deb)).requirements}
    for req in lockfile.load(LOCK).requirements:
        if req.name in ("onnxruntime", "onnx-asr"):
            assert deb_pins[req.name].version == req.version
            assert set(req.hashes) <= set(deb_pins[req.name].hashes)


def _deb_lock_as_appimage(text: str) -> str:
    """wheels.lock без директив AppImage — дописываем их, чтобы использовать тот же разбор."""
    head = GOOD_LOCK.split("# TODO-HASH")[0]
    return head + "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))


def test_good_lock_fields() -> None:
    lock = lockfile.parse(GOOD_LOCK)
    assert [req.name for req in lock.requirements] == ["numpy"]
    assert lock.todo == [("jsonschema", "4.10.3")]
    assert lock.tool("runtime-x86_64").size == 1


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (("# expect-elf: 157\n", ""), "нет директив: expect-elf"),
        (("# max-glibc: 2.28", "# max-glibc: 2.28.1"), "неверное значение max-glibc"),
        (("570C77AC", "570c77ac"), "неверное значение runtime-key"),
        (("# expect-elf: 157\n", "# expect-elf: 157\n# expect-elf: 158\n"), "повторяется"),
        (("    --hash=sha256:" + SHA, "    --no-deps"), "неожиданный параметр"),
        (("numpy==1.24.2 \\\n    --hash=sha256:" + SHA, "numpy==1.24.2"), "нет --hash"),
        (("numpy==1.24.2", "numpy>=1.24"), "без точной версии"),
        (("# tool: runtime-x86_64.sig", "# tool: runtime-x86_64.asc"), "нет инструмента"),
        (("# tool: runtime-x86_64 ", "# tool: runtime x86_64 "), "неверная строка инструмента"),
        (("# TODO-HASH: jsonschema", "# TODO-HASH jsonschema"), "неверная строка TODO-HASH"),
        (("# TODO-HASH: jsonschema", "# TODO-HASH: numpy"), "указан дважды"),
    ],
)
def test_bad_lock_rejected(change: tuple[str, str], message: str) -> None:
    old, new = change
    assert old in GOOD_LOCK
    with pytest.raises(lockfile.LockError, match=message):
        lockfile.parse(GOOD_LOCK.replace(old, new, 1))


def test_lock_needs_required_tools() -> None:
    text = "\n".join(line for line in GOOD_LOCK.splitlines() if "appimagetool" not in line)
    with pytest.raises(lockfile.LockError, match="нет инструмента appimagetool"):
        lockfile.parse(text)


def test_python_appimage_base_needs_python_tool() -> None:
    text = "\n".join(line for line in GOOD_LOCK.splitlines() if "python3.11.16" not in line)
    with pytest.raises(lockfile.LockError, match="нужен ровно один инструмент python3.11"):
        lockfile.parse(text)


def test_lock_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "appimage.lock"
    path.write_text(GOOD_LOCK, encoding="utf-8")
    assert lockfile.main([str(path), "get", "max-glibc"]) == 0
    assert capsys.readouterr().out == "2.28\n"
    assert lockfile.main([str(path), "todo"]) == 0
    assert capsys.readouterr().out == "jsonschema==4.10.3\n"
    assert lockfile.main([str(path), "tools"]) == 0
    assert len(capsys.readouterr().out.splitlines()) == 4
    assert lockfile.main([str(path), "get", "nope"]) == 2
    path.write_text(GOOD_LOCK.replace("# max-glibc: 2.28\n", ""), encoding="utf-8")
    assert lockfile.main([str(path), "check"]) == 1


# --- check_bundle (T-169) --------------------------------------------------------


def test_control_table_covers_real_control() -> None:
    packages = check_bundle.control_packages(CONTROL.read_text(encoding="utf-8"))
    assert "python3-jsonschema" in packages
    assert {"pipewire-pulse", "pulseaudio"} <= set(packages)
    modules, unknown = check_bundle.required_modules(packages)
    assert unknown == []
    assert "jsonschema" in modules
    assert "numpy" in modules


def test_unknown_dependency_is_reported() -> None:
    control = (
        "Package: astra-voice\n"
        "Depends: ${misc:Depends},\n python3-numpy (>= 1:1.22.4),\n python3-newthing\n"
        "Recommends: python3-jsonschema (<< 5), fonts-new | fonts-pt-mono\n"
        "Description: x\n"
    )
    packages = check_bundle.control_packages(control)
    assert packages == [
        "misc:Depends",
        "python3-numpy",
        "python3-newthing",
        "python3-jsonschema",
        "fonts-new",
        "fonts-pt-mono",
    ]
    modules, unknown = check_bundle.required_modules(packages)
    assert modules == ["numpy", "jsonschema"]
    assert unknown == ["python3-newthing", "fonts-new"]


@pytest.fixture
def fake_bundle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    appdir = tmp_path / "AppDir"
    site = appdir / "opt" / "python3.11" / "lib" / "python3.11" / "site-packages"
    site.mkdir(parents=True)
    (site / "bundled_fixture_mod.py").write_text("VALUE = 1\n", encoding="utf-8")
    (site / "broken_fixture_mod.py").write_text("raise ImportError('нет libfoo')\n", "utf-8")
    monkeypatch.syspath_prepend(str(site))
    for name in ("bundled_fixture_mod", "broken_fixture_mod"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    return appdir


@pytest.fixture
def system_checks_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """Проверки процесса и дерева (префиксы, OpenSSL, ELF) — для настоящего бандла; здесь
    интерпретатор тестов вне фальшивого AppDir. Их покрывает test_appimage_bundle_gate.py."""
    monkeypatch.setattr(check_bundle, "check_prefixes", lambda appdir: [])
    monkeypatch.setattr(check_bundle, "check_openssl", lambda *args, **kw: [])
    monkeypatch.setattr(check_bundle, "check_needed", lambda appdir, host: (0, []))


def gate_args(where: Path) -> list[str]:
    """Параметры из lock: OpenSSL и манифест хоста (файл, как пишет build.sh)."""
    host_libs = where / "host-libs.txt"
    host_libs.write_text("libssl.so.3 libssl3\nlibcrypto.so.3 libssl3\n", encoding="utf-8")
    return ["--openssl-major", "3", "--openssl-origin", "host", "--host-libs", str(host_libs)]


def test_check_imports_on_fixture(fake_bundle: Path) -> None:
    problems = check_bundle.check_imports(
        ["bundled_fixture_mod", "json", "broken_fixture_mod", "absent_fixture_mod"],
        fake_bundle.resolve(),
    )
    assert len(problems) == 3
    assert problems[0].startswith("json: загружен не из бандла")
    assert "broken_fixture_mod: не импортируется: ImportError: нет libfoo" == problems[1]
    assert problems[2].startswith("absent_fixture_mod: не импортируется: ModuleNotFoundError")


def test_check_sys_path_on_fixture(fake_bundle: Path) -> None:
    inside = str(fake_bundle / "opt" / "python3.11" / "lib" / "python3.11")
    problems = check_bundle.check_sys_path(fake_bundle.resolve(), [inside, "", "/usr/lib/python3"])
    assert problems == ["sys.path вне бандла: /usr/lib/python3"]


@pytest.mark.usefixtures("system_checks_off")
def test_warning_records_fail_the_gate(fake_bundle: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Любая запись WARNING за время проверки — нарушение (arch §11 п.3в)."""
    import logging

    (fake_bundle / ".astra-voice-build").write_text("VERSION=0.2.0\nBUILD_ID=0\n", "ascii")
    control = fake_bundle / "control"
    control.write_text("Depends: python3\nRecommends: fonts-pt-mono\n", encoding="utf-8")
    monkeypatch.setattr(check_bundle, "EXTRA_MODULES", ("bundled_fixture_mod",))
    monkeypatch.setattr(check_bundle, "check_package_tree", lambda lib: (0, []))

    def catalog() -> list[str]:
        logging.getLogger("check_bundle_fixture").warning("Проверка по схеме пропущена")
        return []

    monkeypatch.setattr(check_bundle, "check_catalog", catalog)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if str(fake_bundle) in p])
    args = ["--appdir", str(fake_bundle), "--control", str(control), *gate_args(fake_bundle)]
    assert check_bundle.main(args) == 1
    monkeypatch.setattr(check_bundle, "check_catalog", lambda: [])
    assert check_bundle.main(args) == 0


def test_check_bundle_requires_marker(tmp_path: Path) -> None:
    args = ["--appdir", str(tmp_path), "--control", str(CONTROL), *gate_args(tmp_path)]
    assert check_bundle.main(args) == 2


# --- SBOM AppImage (MN-5) ---------------------------------------------------------


def test_sbom_appdir_names_openssl_and_tools(tmp_path: Path) -> None:
    appdir = tmp_path / "AppDir"
    lib = appdir / "usr" / "lib"
    lib.mkdir(parents=True)
    (appdir / ".astra-voice-build").write_text(
        "VERSION=0.2.0\nBUILD_ID=0123456789ab\n", encoding="ascii"
    )
    (lib / "libssl.so.1.1").write_bytes(b"\x7fELF" + b"\0" * 60 + b"OpenSSL 1.1.1k  FIPS 25 Mar")
    (lib / "libssl.so").symlink_to("libssl.so.1.1")
    (appdir / "data.txt").write_text("не ELF\n", encoding="utf-8")
    out = tmp_path / "sbom-appimage.cdx.json"
    # SBOM базы debian12 (OpenSSL с хоста, Debian-компоненты) — следующий шаг R3; здесь
    # проверяется прежняя модель на lock отката (г) с бандловым OpenSSL.
    legacy = tmp_path / "appimage.lock"
    legacy.write_text(
        GOOD_LOCK.replace("# TODO-HASH: jsonschema==4.10.3 py3-none-any\n", ""), "utf-8"
    )
    command = [
        sys.executable,
        str(ROOT / "scripts" / "sbom.py"),
        "--appdir",
        str(appdir),
        "--lock",
        str(legacy),
        "--out",
        str(out),
    ]
    env = {"PATH": "/usr/bin:/bin", "SOURCE_DATE_EPOCH": "1790000000"}
    subprocess.run(command, check=True, capture_output=True, env=env, timeout=60)
    bom: dict[str, Any] = json.loads(out.read_text(encoding="utf-8"))
    components = {comp["bom-ref"]: comp for comp in bom["components"]}
    assert components["pkg:generic/openssl@1.1.1k"]["name"] == "openssl"
    python = components[f"tool:{lockfile.load(legacy).python_tool.name}"]
    assert python["version"] == "3.11.16"
    assert components["tool:appimagetool-x86_64.AppImage"]["scope"] == "excluded"
    assert components["file:/usr/lib/libssl.so.1.1"]["hashes"][0]["alg"] == "SHA-256"
    assert "file:/usr/lib/libssl.so" not in components
    wheels = [ref for ref in components if ref.startswith("pkg:pypi/")]
    assert len(wheels) == len(lockfile.load(legacy).requirements)
    assert bom["metadata"]["timestamp"] == "2026-09-21T14:13:20Z"
    assert bom["metadata"]["component"]["properties"][0]["value"] == "0123456789ab"
    # Без libssl версия OpenSSL не названа — SBOM не пишется.
    (lib / "libssl.so").unlink()
    (lib / "libssl.so.1.1").unlink()
    failed = subprocess.run(command, capture_output=True, env=env, timeout=60, check=False)
    assert failed.returncode == 1


# --- elf-audit --max-glibc (MN-9) ----------------------------------------------------


@pytest.mark.skipif(shutil.which("objdump") is None, reason="нет objdump (binutils)")
def test_elf_audit_max_glibc(tmp_path: Path) -> None:
    tree = tmp_path / "AppDir"
    tree.mkdir()
    shutil.copy2("/bin/true", tree / "true")
    audit = [sys.executable, str(ROOT / "tools" / "elf-audit"), "--strict", "--expect", "1"]
    low = subprocess.run([*audit, "--max-glibc", "2.0", str(tree)], capture_output=True, text=True)
    assert low.returncode == 1
    assert "GLIBC" in low.stderr and "> 2.0" in low.stderr
    high = subprocess.run([*audit, "--max-glibc", "9.99", str(tree)], capture_output=True)
    assert high.returncode == 0
    bad = subprocess.run([*audit, "--max-glibc", "2", str(tree)], capture_output=True, text=True)
    assert bad.returncode == 2
    extra = subprocess.run(
        [*audit[:-1], "2", "--max-glibc", "9.99", str(tree)], capture_output=True
    )
    assert extra.returncode == 1


# --- пакет правки workflow (§9.4) ------------------------------------------------------


def proposed_jobs() -> dict[str, Any]:
    yaml = pytest.importorskip("yaml")
    text = (ROOT / "packaging" / "appimage" / "ci.yml.proposed").read_text(encoding="utf-8")
    jobs: dict[str, Any] = yaml.safe_load(text)["jobs"]
    return jobs


def test_proposed_workflow_only_calls_repo_scripts() -> None:
    jobs = proposed_jobs()
    runs = [step["run"] for step in jobs["appimage"]["steps"] if "run" in step]
    assert all(run.startswith("apt-get update") for run in runs[:2])
    assert runs[2:] == [
        "packaging/appimage/build.sh --fetch",
        "packaging/appimage/build.sh",
        "packaging/appimage/smoke.sh dist/Astra_Voice-*-x86_64.AppImage",
    ]
    assert "needs" not in jobs["appimage"]
    release = jobs["release"]
    assert "appimage" in release["needs"]
    release_runs = [step.get("run", "") for step in release["steps"]]
    assert "scripts/release_build.sh" in release_runs
    assert "scripts/release_assets.sh dist" in release_runs
    assert not any("build-deb.sh" in run or "sha256sum" in run for run in release_runs)
    assert any("$(cat dist/assets.txt)" in run for run in release_runs)


def test_proposed_signing_step_runs_no_repo_scripts() -> None:
    """T3 P2-1/P2-2: в шаге с секретом — только прежняя проверка ключа."""
    for job in proposed_jobs().values():
        for step in job.get("steps", []):
            if "secrets." not in json.dumps(step.get("env", {})):
                continue
            scripts = {
                word for word in step["run"].split() if "scripts/" in word or "packaging/" in word
            }
            assert scripts == {"../scripts/check_signing_secret.py"}


def test_proposed_checkouts_drop_credentials() -> None:
    """T-131 (T3 P2-2): каждый checkout без сохранённого токена."""
    checkouts = [
        step
        for job in proposed_jobs().values()
        for step in job.get("steps", [])
        if str(step.get("uses", "")).startswith("actions/checkout@")
    ]
    assert len(checkouts) == 8
    assert all(step["with"]["persist-credentials"] is False for step in checkouts)


def test_patch_doc_matches_proposed_file() -> None:
    """В ci-workflow-patch.md — разница именно с ci.yml.proposed."""
    doc = (ROOT / "packaging" / "appimage" / "ci-workflow-patch.md").read_text(encoding="utf-8")
    proposed = (ROOT / "packaging" / "appimage" / "ci.yml.proposed").read_text(encoding="utf-8")
    added = [
        line[1:]
        for line in doc.split("```diff\n", 1)[1].splitlines()
        if line.startswith("+") and not line.startswith("+++")
    ]
    assert added
    assert all(line in proposed.splitlines() for line in added)


def test_allow_missing_forgives_only_todo_modules() -> None:
    """P2-2(г): при ALLOW прощаются только нарушения про модули незакреплённых колёс."""
    problems = [
        "jsonschema: не импортируется: ModuleNotFoundError: No module named 'jsonschema'",
        "jsonschema: SchemaUnavailable: Модуль jsonschema недоступен.",
        "attr: не импортируется: ModuleNotFoundError: No module named 'attr'",
        "numpy: загружен не из бандла: /usr/lib/python3/dist-packages/numpy/__init__.py",
        "jsonschema: загружен не из бандла: /usr/lib/python3/dist-packages/jsonschema/__init__.py",
        "jsonschema: не импортируется: ImportError: libfoo.so",
        "jsonschema: SchemaUnavailable: Несовместимая версия jsonschema.",
        "журнал WARNING: astra_voice.models.catalog: Проверка по схеме пропущена",
    ]
    rest, allowed = check_bundle.split_allowed(problems, ["jsonschema", "attrs", "pyrsistent"])
    # Прощается только отсутствие модуля; «не из бандла», сбой импорта при наличии модуля
    # и несовместимая версия валят сборку и в локальном режиме.
    assert allowed == problems[:3]
    assert rest == problems[3:]
    assert check_bundle.split_allowed(problems, []) == (problems, [])


@pytest.mark.usefixtures("system_checks_off")
def test_check_bundle_allow_missing_exit_code(
    fake_bundle: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (fake_bundle / ".astra-voice-build").write_text("VERSION=0.2.0\nBUILD_ID=0\n", "ascii")
    control = fake_bundle / "control"
    control.write_text("Depends: python3\n", encoding="utf-8")
    monkeypatch.setattr(check_bundle, "EXTRA_MODULES", ("bundled_fixture_mod",))
    monkeypatch.setattr(check_bundle, "check_package_tree", lambda lib: (0, []))
    monkeypatch.setattr(
        check_bundle,
        "check_catalog",
        lambda: ["jsonschema: SchemaUnavailable: Модуль jsonschema недоступен."],
    )
    monkeypatch.setattr(sys, "path", [p for p in sys.path if str(fake_bundle) in p])
    args = ["--appdir", str(fake_bundle), "--control", str(control), *gate_args(fake_bundle)]
    assert check_bundle.main(args) == 1
    assert check_bundle.main([*args, "--allow-missing", "jsonschema"]) == 0
    monkeypatch.setattr(check_bundle, "EXTRA_MODULES", ("bundled_fixture_mod", "json"))
    assert check_bundle.main([*args, "--allow-missing", "jsonschema"]) == 1


@pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("git", "readelf", "objdump", "patch")),
    reason="нет инструментов сборки",
)
@pytest.mark.parametrize("ci_env", [{"GITHUB_ACTIONS": "true"}, {"CI": "1"}])
def test_build_refuses_allow_todo_in_ci(ci_env: dict[str, str], tmp_path: Path) -> None:
    """P2-2(а): послабление локальной сборки в CI — ошибка до любой работы."""
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "ASTRA_VOICE_APPIMAGE_ALLOW_TODO_HASH": "1",
        "ASTRA_VOICE_APPIMAGE_WORK": str(tmp_path / "work"),
        **ci_env,
    }
    result = subprocess.run(
        ["bash", str(ROOT / "packaging/appimage/build.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 1
    assert "в CI запрещена" in result.stderr
    assert not (tmp_path / "work").exists()


@pytest.mark.skipif(
    any(shutil.which(tool) is None for tool in ("gpg", "gpgconf")), reason="нет gpg"
)
def test_runtime_key_matches_lock(tmp_path: Path) -> None:
    """Ключ подписи runtime в репозитории — тот, чей отпечаток закреплён строкой runtime-key."""
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    listing = subprocess.run(
        [
            "gpg",
            "--homedir",
            str(home),
            "--batch",
            "--with-colons",
            "--show-keys",
            str(ROOT / "packaging/appimage/keys/appimage-runtime.gpg"),
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout
    fingerprints = [line.split(":")[9] for line in listing.splitlines() if line.startswith("fpr:")]
    subprocess.run(["gpgconf", "--homedir", str(home), "--kill", "all"], check=False, timeout=10)
    assert fingerprints[0] == lockfile.load(LOCK).runtime_key


strip_record = load("appimage_strip_record", ROOT / "packaging" / "appimage" / "strip_record.py")


def test_strip_record_drops_only_bin_wrappers(tmp_path: Path) -> None:
    text = (
        "../../bin/idna,sha256=abc,231\n"
        "idna/__init__.py,sha256=def,849\n"
        "idna-3.20.dist-info/RECORD,,\n"
    )
    assert strip_record.strip_record(text) == (
        "idna/__init__.py,sha256=def,849\nidna-3.20.dist-info/RECORD,,\n"
    )
    with pytest.raises(strip_record.RecordError, match="не обёртка bin/: ../../share/man/x.1"):
        strip_record.strip_record(text + "../../share/man/x.1,sha256=x,1\n")
    site = tmp_path / "site-packages"
    record = site / "idna-3.20.dist-info" / "RECORD"
    record.parent.mkdir(parents=True)
    record.write_text(text, encoding="utf-8")
    assert strip_record.main([str(site)]) == 0
    assert "../../bin/" not in record.read_text(encoding="utf-8")
    record.write_text("../etc/conf,sha256=x,1\n", encoding="utf-8")
    assert strip_record.main([str(site)]) == 1
