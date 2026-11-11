"""Оракул подписи: недоверенные байты не становятся кодом или разметкой сводки."""

from __future__ import annotations

import copy
import hashlib
import json
import os
import subprocess
from pathlib import Path
from typing import Any

import pytest
from scripts import catalog_sign_check as gate

yaml = pytest.importorskip("yaml")

pytestmark = pytest.mark.unit
ROOT = Path(__file__).resolve().parents[2]
COMMIT = "a" * 40
MAIN = "b" * 40
SCHEMA = (ROOT / "data/catalog.schema.json").read_bytes()


def encode(value: dict[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False).encode() + b"\n"


@pytest.fixture
def old() -> dict[str, Any]:
    return json.loads((ROOT / "data/catalog.json").read_bytes())  # type: ignore[no-any-return]


def candidate(old: dict[str, Any]) -> bytes:
    new = copy.deepcopy(old)
    new["serial"] += 1
    return encode(new)


def verify(raw: bytes, bases: list[tuple[str, bytes]], schema: bytes = SCHEMA) -> None:
    gate.check_data(raw, schema, hashlib.sha256(raw).hexdigest(), bases)


@pytest.mark.parametrize("commit", ["", "a" * 39, "a" * 41, "g" * 40, "--help", "a" * 40 + "\n"])
def test_bad_commit(commit: str) -> None:
    with pytest.raises(gate.PublishError, match="40 hex"):
        gate.validate_inputs(commit, "0" * 64)


@pytest.mark.parametrize("digest", ["", "f" * 63, "f" * 65, "g" * 64, "f" * 64 + "\n"])
def test_bad_digest(digest: str) -> None:
    with pytest.raises(gate.PublishError, match="64 hex"):
        gate.validate_inputs(COMMIT, digest)


def test_uppercase_inputs_allowed() -> None:
    gate.validate_inputs(COMMIT.upper(), "F" * 64)


@pytest.mark.parametrize("index", [0, 1], ids=["main", "latest-tag"])
@pytest.mark.parametrize("lost_revocation", [False, True], ids=["rollback", "lost-revocation"])
def test_each_baseline_enforced(old: dict[str, Any], index: int, lost_revocation: bool) -> None:
    baseline = copy.deepcopy(old)
    if lost_revocation:
        baseline["revoked"] = [{"model_id": "withdrawn", "revision": "r1", "reason": "unsafe"}]
    else:
        baseline["serial"] += 2
    bases = [("main", encode(old)), ("tag", encode(old))]
    bases[index] = (bases[index][0], encode(baseline))
    with pytest.raises(gate.PublishError, match="Откат|потеря отзыва"):
        verify(candidate(old), bases)


def test_equal_pair_and_identical_bytes_rejected(old: dict[str, Any]) -> None:
    with pytest.raises(gate.PublishError, match="Равная пара"):
        verify(encode(old), [("main", encode(old))])


def test_epoch_growth_does_not_erase_revocation(old: dict[str, Any]) -> None:
    old["revoked"] = [{"model_id": "withdrawn", "revision": "r1", "reason": "unsafe"}]
    new = copy.deepcopy(old)
    new.update(trust_epoch=old["trust_epoch"] + 1, serial=1, revoked=[])
    with pytest.raises(gate.PublishError, match="потеря отзыва"):
        verify(encode(new), [("main", encode(old))])


def test_epoch_growth_with_preserved_revocation(old: dict[str, Any]) -> None:
    old["revoked"] = [{"model_id": "withdrawn", "revision": "r1", "reason": "unsafe"}]
    new = copy.deepcopy(old)
    new.update(trust_epoch=old["trust_epoch"] + 1, serial=1)
    verify(encode(new), [("main", encode(old))])


def test_digest_mismatch(old: dict[str, Any]) -> None:
    with pytest.raises(gate.PublishError, match="sha256 каталога"):
        gate.check_data(candidate(old), SCHEMA, "0" * 64, [])


def test_schema_exact_bytes(old: dict[str, Any]) -> None:
    with pytest.raises(gate.PublishError, match="schema_sha256"):
        verify(candidate(old), [], SCHEMA + b"\n")


def test_size_limit(old: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    raw = candidate(old)
    monkeypatch.setattr(gate, "CATALOG_MAX_BYTES", len(raw) - 1)
    with pytest.raises(gate.PublishError, match="CATALOG_MAX_BYTES"):
        verify(raw, [])
    monkeypatch.setattr(gate, "CATALOG_MAX_BYTES", max(len(raw), len(SCHEMA)))
    verify(raw, [])


@pytest.mark.parametrize(
    "raw", [b"invalid", b"[]", b'{"serial":1,"serial":2}', b'{"x":NaN}', b'"\xff"']
)
def test_bad_json(raw: bytes) -> None:
    with pytest.raises(gate.PublishError, match="JSON"):
        gate.document(raw)


@pytest.mark.parametrize("field", ["serial", "trust_epoch"])
@pytest.mark.parametrize("value", [True, 0, -1, "5", None])
def test_invalid_pair(old: dict[str, Any], field: str, value: object) -> None:
    old[field] = value
    with pytest.raises(gate.PublishError, match="trust_epoch / serial"):
        verify(encode(old), [])


def test_bad_schema_json(old: dict[str, Any]) -> None:
    schema = b"not JSON"
    old["schema_sha256"] = hashlib.sha256(schema).hexdigest()
    with pytest.raises(gate.PublishError, match="JSON"):
        verify(candidate(old), [], schema)


@pytest.mark.parametrize("bad", [None, [None], [{"model_id": "a@b", "revision": "r1"}]])
def test_invalid_revocations(old: dict[str, Any], bad: object) -> None:
    old["revoked"] = bad
    with pytest.raises(gate.PublishError, match="revoked"):
        verify(candidate(old), [])


def test_duplicate_model_ids(old: dict[str, Any]) -> None:
    old["models"].append(old["models"][0])
    with pytest.raises(gate.PublishError, match="Повторяющийся идентификатор"):
        verify(candidate(old), [])


def test_summary_complete_and_escaped(old: dict[str, Any]) -> None:
    new = copy.deepcopy(old)
    changed_id = new["models"][0]["id"]
    removed_id = new["models"].pop()["id"]
    new["serial"] += 1
    new["models"][0]["name"] = "changed"
    hostile = "`\n# Approve immediately <img src=x> [link](https://evil.invalid)"
    new["models"][0][hostile] = "text"
    for identifier in ("added-one", "added-two"):
        new["models"].append({"id": identifier})
    new["revoked"] = [{"model_id": "withdrawn", "revision": "r1", "reason": hostile}]
    raw = encode(new)
    verify(raw, [("main", encode(old))])
    result = gate.summary(
        COMMIT, hashlib.sha256(raw).hexdigest(), raw, [("main", encode(old))], MAIN
    )
    assert "Добавлены: " + gate.code(["added-one", "added-two"]) in result
    assert "Удалены: " + gate.code([removed_id]) in result
    assert gate.code(changed_id) in result
    assert gate.code("/name") in result
    assert gate.code("/" + hostile.replace("~", "~0").replace("/", "~1")) in result
    assert gate.code("changed") in result
    assert "trust_epoch: 1 → 1" in result
    assert "serial: 4 → 5" in result
    assert gate.code(new["revoked"]) in result
    assert "Изменения содержимого отзывов" in result
    assert hostile not in result and "<img" not in result


@pytest.fixture
def fake_git(old: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, ...]]:
    calls: list[tuple[str, ...]] = []
    raw = candidate(old)

    def git(root: Path, *args: str) -> bytes:
        calls.append(args)
        if args == ("cat-file", "-t", COMMIT):
            return b"commit\n"
        if args[0] == "describe":
            assert args[-1] == MAIN  # тег на самой main включён (не HEAD^).
            return b"v1.0.0\n"
        if args[:2] == ("cat-file", "-t"):
            return b"blob\n"
        content = (
            SCHEMA
            if args[-1].endswith(".schema.json")
            else (raw if args[-1].startswith(COMMIT) else encode(old))
        )
        if args[:2] == ("cat-file", "-s"):
            return str(len(content)).encode()
        assert args[0] == "show"
        return content

    monkeypatch.setattr(gate, "git", git)
    return calls


def test_extracts_only_two_candidate_blobs(
    old: dict[str, Any], tmp_path: Path, fake_git: list[tuple[str, ...]]
) -> None:
    output = tmp_path / "outside-tree"
    raw = candidate(old)
    gate.check(tmp_path / "repo", COMMIT, hashlib.sha256(raw).hexdigest(), output, MAIN)
    assert (output / "catalog.json").read_bytes() == raw
    assert (output / "catalog.schema.json").read_bytes() == SCHEMA
    candidate_reads = [args[-1] for args in fake_git if args[0] == "show" and COMMIT in args[-1]]
    assert candidate_reads == [f"{COMMIT}:data/catalog.json", f"{COMMIT}:data/catalog.schema.json"]
    assert all(args[0] in {"cat-file", "show", "describe"} for args in fake_git)


def test_output_inside_checkout_rejected(tmp_path: Path) -> None:
    with pytest.raises(gate.PublishError, match="вне рабочего дерева"):
        gate.check(tmp_path, COMMIT, "0" * 64, tmp_path / "candidate", MAIN)


def test_not_commit_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gate, "git", lambda *args: b"tag\n")
    with pytest.raises(gate.PublishError, match="не является коммитом"):
        gate.check(tmp_path / "repo", COMMIT, "0" * 64, tmp_path / "data", MAIN)


def test_blob_limit_before_show(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, ...]] = []

    def git(root: Path, *args: str) -> bytes:
        calls.append(args)
        return b"blob" if args[1] == "-t" else str(gate.CATALOG_MAX_BYTES + 1).encode()

    monkeypatch.setattr(gate, "git", git)
    with pytest.raises(gate.PublishError, match="CATALOG_MAX_BYTES"):
        gate.blob(tmp_path, COMMIT, "data/catalog.json")
    assert not any(args[0] == "show" for args in calls)


def test_main_outputs_only_verified_digest(
    old: dict[str, Any],
    tmp_path: Path,
    fake_git: list[tuple[str, ...]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "ROOT", tmp_path / "repo")
    output = tmp_path / "github-output"
    summary = tmp_path / "github-summary"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    digest = hashlib.sha256(candidate(old)).hexdigest()
    args = [
        "--commit",
        COMMIT,
        "--sha256",
        digest.upper(),
        "--main-commit",
        MAIN,
        "--output",
        str(tmp_path / "data"),
    ]
    assert gate.main(args) == 0
    assert output.read_text() == f"sha256={digest}\n"
    assert digest in summary.read_text()
    output.unlink()
    args[3] = "0" * 64
    assert gate.main(args) == 1
    assert not output.exists()


@pytest.mark.parametrize(
    "ref,success",
    [
        ("refs/heads/main", True),
        ("refs/heads/feature", False),
        ("refs/tags/v1", False),
    ],
)
def test_workflow_first_step_refusal(ref: str, success: bool, tmp_path: Path) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/catalog-sign.yml").read_text())
    guard = workflow["jobs"]["check"]["steps"][0]["run"]
    result = subprocess.run(
        ["bash", "-eu", "-c", guard],
        cwd=tmp_path,
        env={**os.environ, "GITHUB_REF": ref},
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode == 0) is success
    if not success:
        assert "только с ветки main" in result.stdout.decode()


@pytest.mark.parametrize("bad", [None, "input", "check", "both", "empty-output"])
def test_workflow_digest_gate(tmp_path: Path, bad: str | None) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/catalog-sign.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["sign"]["steps"]
        if step.get("name") == "Сверка точных байтов"
    )
    data = tmp_path / "catalog-to-sign"
    data.mkdir()
    raw = b"exact candidate bytes\n"
    (data / "catalog.json").write_bytes(raw)
    digest = hashlib.sha256(raw).hexdigest()
    check_digest = "0" * 64 if bad in {"check", "both"} else digest
    input_digest = "0" * 64 if bad in {"input", "both"} else digest.upper()
    if bad == "empty-output":
        check_digest = ""
    result = subprocess.run(
        ["bash", "-eu", "-c", step["run"]],
        cwd=tmp_path,
        env={
            **remote_env(tmp_path, MAIN),
            "RUNNER_TEMP": str(tmp_path),
            "CHECK_SHA256": check_digest,
            "INPUT_SHA256": input_digest,
        },
        capture_output=True,
        timeout=10,
    )
    assert (result.returncode == 0) is (bad is None)


def remote_env(tmp_path: Path, tip: str, *, exit_code: int = 0) -> dict[str, str]:
    """Локальная заглушка только Git-транспорта; ни Git, ни сеть не запускаются."""
    bindir = tmp_path / "fake-bin"
    bindir.mkdir(exist_ok=True)
    git = bindir / "git"
    git.write_text(f"#!/bin/bash\nprintf '%s\\trefs/heads/main\\n' '{tip}'\nexit {exit_code}\n")
    git.chmod(0o755)
    return {
        **os.environ,
        "PATH": str(bindir) + os.pathsep + os.environ["PATH"],
        "GITHUB_WORKSPACE": str(tmp_path),
        "GITHUB_SHA": MAIN,
        "GITHUB_TOKEN": "test-read-only-token",
    }


@pytest.mark.parametrize("tip,exit_code", [("c" * 40, 0), (MAIN, 2), ("invalid", 0)])
def test_workflow_refuses_changed_or_unreadable_main(
    tmp_path: Path,
    tip: str,
    exit_code: int,
) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/catalog-sign.yml").read_text())
    step = next(
        step
        for step in workflow["jobs"]["sign"]["steps"]
        if step.get("name") == "Сверка точных байтов"
    )
    data = tmp_path / "catalog-to-sign"
    data.mkdir()
    (data / "catalog.json").write_bytes(b"unchanged candidate")
    digest = hashlib.sha256(b"unchanged candidate").hexdigest()
    result = subprocess.run(
        ["bash", "-eu", "-c", step["run"]],
        env={
            **remote_env(tmp_path, tip, exit_code=exit_code),
            "RUNNER_TEMP": str(tmp_path),
            "CHECK_SHA256": digest,
            "INPUT_SHA256": digest,
        },
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "main изменился, запустите подпись заново" in result.stdout.decode() or exit_code != 0
    assert (data / "catalog.json").read_bytes() == b"unchanged candidate"
    assert "GPG_SIGNING_KEY" not in step["env"]


@pytest.mark.parametrize("job_id", ["check", "sign"])
def test_workflow_requires_native_tools_first(tmp_path: Path, job_id: str) -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/catalog-sign.yml").read_text())
    job = workflow["jobs"][job_id]
    assert "container" not in job
    assert all(
        "apt-get" not in step.get("run", "") and "pip " not in step.get("run", "")
        for step in job["steps"]
    )
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    result = subprocess.run(
        ["/bin/bash", "-eu", "-c", job["steps"][0]["run"]],
        env={**os.environ, "GITHUB_REF": "refs/heads/main", "PATH": str(empty_bin)},
        capture_output=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "Нет штатной программы git" in result.stdout.decode()


@pytest.mark.parametrize(
    "field,before,after,pointer",
    [
        ("recommended", True, 1, "/recommended"),
        (
            "files",
            [{"path": "m.onnx", "size": 135}],
            [{"path": "m.onnx", "size": 135.0}],
            "/files/m.onnx/0/size",
        ),
    ],
)
def test_summary_type_changes(
    old: dict[str, Any],
    field: str,
    before: Any,
    after: Any,
    pointer: str,
) -> None:
    old["models"][0][field] = before
    new = copy.deepcopy(old)
    new["serial"] += 1
    new["models"][0][field] = after
    raw = encode(new)
    verify(raw, [("main", encode(old))])
    text = gate.summary(COMMIT, hashlib.sha256(raw).hexdigest(), raw, [("main", encode(old))], MAIN)
    assert "изменено 1 записей" in text
    assert gate.code(pointer) in text
    assert MAIN in text
    assert gate.code(before if field != "files" else 135) in text
    assert gate.code(after if field != "files" else 135.0) in text


@pytest.mark.parametrize("same_content", [True, False])
def test_duplicate_revocation_pair_refused(old: dict[str, Any], same_content: bool) -> None:
    old["revoked"] = [
        {"model_id": "withdrawn", "revision": "r1", "reason": "old"},
        {"model_id": "withdrawn", "revision": "r1", "reason": "old" if same_content else "new"},
    ]
    with pytest.raises(gate.PublishError, match="Повторяющаяся пара"):
        verify(candidate(old), [])


def test_summary_preserves_all_baseline_revocations(old: dict[str, Any]) -> None:
    old["revoked"] = [
        {"model_id": "withdrawn", "revision": "r1", "reason": "first"},
        {"model_id": "withdrawn", "revision": "r1", "reason": "second"},
    ]
    new = copy.deepcopy(old)
    new["serial"] += 1
    new["revoked"].pop(0)
    raw = encode(new)
    verify(raw, [("main", encode(old))])
    text = gate.summary(COMMIT, hashlib.sha256(raw).hexdigest(), raw, [("main", encode(old))], MAIN)
    assert "Отзывы изменились: да" in text
    assert gate.code(old["revoked"]) in text
    assert gate.code(new["revoked"]) in text
    assert gate.code("/revoked/0/reason") in text
    assert gate.code("/revoked/1") in text


def test_summary_full_files_added_records_and_catalog_values(old: dict[str, Any]) -> None:
    old["models"][0]["files"] = [
        {"path": "m.onnx", "sha256": "a" * 64, "size": 135, "url_path": "/old.onnx"},
        {"path": "removed.onnx", "sha256": "c" * 64, "size": 136, "url_path": "/gone.onnx"},
    ]
    new = copy.deepcopy(old)
    new["serial"] += 1
    new["publisher"] = "new publisher"
    new["models"][0].update(host="example.invalid", revision="new-revision")
    new["models"][0]["files"] = [
        {"path": "m.onnx", "sha256": "b" * 64, "size": 135, "url_path": "/new.onnx"},
        {"path": "added.onnx", "sha256": "d" * 64, "size": 137, "url_path": "/added.onnx"},
    ]
    new["models"].append(
        {
            "id": "new-model",
            "host": "other.invalid",
            "revision": "r2",
            "files": [
                {"path": "new.onnx", "sha256": "e" * 64, "size": 138, "url_path": "/new-model.onnx"}
            ],
        }
    )
    raw = encode(new)
    text = gate.summary(COMMIT, hashlib.sha256(raw).hexdigest(), raw, [("main", encode(old))], MAIN)
    for path, status in [
        ("m.onnx", "изменён"),
        ("removed.onnx", "удалён"),
        ("added.onnx", "добавлен"),
        ("new.onnx", "добавлен"),
    ]:
        assert f"Файл {gate.code(path)}: {status}" in text
    assert gate.code(new["models"][-1]) in text
    for field in ("host", "revision"):
        assert gate.code("/" + field) in text
        assert gate.code(old["models"][0][field]) in text
        assert gate.code(new["models"][0][field]) in text
    for before, after in zip(
        old["models"][0]["files"][:1], new["models"][0]["files"][:1], strict=True
    ):
        assert gate.code([before]) in text and gate.code([after]) in text
    assert gate.code("/publisher") in text
    assert gate.code(old["publisher"]) in text and gate.code(new["publisher"]) in text


def test_summary_size_refusal_before_artifact(
    old: dict[str, Any],
    fake_git: list[tuple[str, ...]],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(gate, "summary", lambda *args: "x" * (gate.CATALOG_MAX_BYTES + 1))
    output = tmp_path / "data"
    with pytest.raises(gate.PublishError, match="Сводка превышает"):
        gate.check(
            tmp_path / "repo", COMMIT, hashlib.sha256(candidate(old)).hexdigest(), output, MAIN
        )
    assert not output.exists()
