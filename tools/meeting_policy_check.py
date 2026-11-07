"""Offline F18.11 matrix runner; temporary INI fixtures, no Qt or network."""

from __future__ import annotations

import itertools
import logging
import os
import runpy
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

from astra_voice.core import policy as core
from astra_voice.core.settings import Settings
from astra_voice.meetings.policy import MeetingPolicy, parse_policy
from astra_voice.meetings.settings_ext import effective_settings

ROOT = Path(__file__).resolve().parents[1]


def matrix_cases() -> list[Any]:
    """Load the source-only test data without depending on pytest or tests packages."""
    result: list[Any] = runpy.run_path(str(ROOT / "tests/unit/policy_matrix.py"))["CASES"]
    return result


def load_case(case: Any, directory: Path) -> core.Policy:
    """Read a real INI and real mode; emulate fixture UID on precisely its inode.

    Unprivileged runs cannot chown fixtures to root. Only fstat's st_uid is
    substituted, never load/permissions_warning/parser outcomes. Unsafe owner
    and mode rows still go through the production permission check.
    """
    path = directory / "policy.conf"
    if path.exists():
        path.unlink()
    if case.absent:
        return core.load(path)
    raw = case.raw or {"autostart": "false"}
    path.write_text(
        "[astra-voice]\n" + "\n".join(f"{k}={v}" for k, v in raw.items()) + "\n", encoding="utf-8"
    )
    path.chmod(case.mode)
    identity = path.stat().st_dev, path.stat().st_ino
    original = os.fstat

    def fixture_stat(fd: int) -> os.stat_result:
        info = original(fd)
        if (info.st_dev, info.st_ino) == identity:
            values = list(info)
            values[4] = case.uid
            return os.stat_result(values)
        return info

    with patch.object(os, "fstat", fixture_stat):
        return core.load(path)


def check_case(case: Any, policy: MeetingPolicy) -> list[str]:
    """Compare every decision, effective value and control lock with contract data."""
    failures = []
    expected = case.expect
    for key, (value, locked, code) in expected["decisions"].items():
        row = policy.decisions[key]
        if (row.value, row.locked, row.code.value) != (value, locked, code):
            failures.append(f"{case.id}: decision {key}")
        if bool(row.reason) != locked:
            failures.append(f"{case.id}: reason {key}")
    result = effective_settings(Settings(**case.user), policy)
    for key, value in expected["settings"].items():
        if getattr(result.settings, key) != value:
            failures.append(f"{case.id}: effective {key}")
    for key, locked in expected["locked"].items():
        if result.controls[key].locked != locked:
            failures.append(f"{case.id}: locked {key}")
    for key in ("recording_allowed", "detector_allowed", "external_without_confirmation"):
        if getattr(policy, key) != expected[key]:
            failures.append(f"{case.id}: {key}")
    if result.recording_allowed != expected["bridge"]["enabled"]:
        failures.append(f"{case.id}: effective recording_allowed")
    if result.detector_allowed != (
        expected["settings"]["meetings_enabled"]
        and expected["settings"]["meeting_offer_on_call"]
        and expected["detector_allowed"]
    ):
        failures.append(f"{case.id}: effective detector_allowed")
    if not result.key_editable or result.controls["meeting_ai_model"].locked:
        failures.append(f"{case.id}: V3")
    return failures


def check_invariants(policy: MeetingPolicy) -> list[str]:
    failures = []
    for enabled, offer in itertools.product((False, True), repeat=2):
        user = Settings(
            meetings_enabled=enabled,
            meeting_offer_on_call=offer,
            meeting_ai_endpoint="http://10.0.0.4/v1",
        )
        result = effective_settings(user, policy)
        if not enabled and result.settings.meetings_enabled:
            failures.append("human-opt-in")
        if (
            not enabled or not offer or policy.meeting_record.value in {"off", "manual"}
        ) and result.detector_allowed:
            failures.append("detector-opt-in")
        if policy.meeting_record.value in {"off", "manual"} and policy.detector_allowed:
            failures.append("detector-policy")
        if not policy.meeting_ai_endpoint.locked and result.settings.meeting_ai_endpoint.startswith(
            "http://"
        ):
            failures.append("plaintext-same-policy")
    return failures


@contextmanager
def quiet_policy_logs() -> Iterator[None]:
    """Rejected fixture values are expected; retain the caller's logging setup."""
    loggers = [logging.getLogger(name) for name in (core.__name__, "astra_voice.meetings.policy")]
    levels = [logger.level for logger in loggers]
    try:
        for logger in loggers:
            logger.setLevel(logging.CRITICAL)
        yield
    finally:
        for logger, level in zip(loggers, levels, strict=True):
            logger.setLevel(level)


def run_matrix() -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    failures: list[str] = []
    with (
        quiet_policy_logs(),
        tempfile.TemporaryDirectory(prefix="astra-policy-matrix-") as temporary,
    ):
        cases = matrix_cases()
        for case in cases:
            source = load_case(case, Path(temporary))
            parsed = parse_policy(source)
            failures.extend(check_case(case, parsed))
            failures.extend(f"{case.id}: {name}" for name in check_invariants(parsed))
            for key, decision in parsed.decisions.items():
                rows.append(
                    {
                        "case": case.id,
                        "key": key,
                        "raw": case.raw.get(key, "<absent>"),
                        "decision": decision.value,
                        "locked": decision.locked,
                        "reason": decision.code.value,
                    }
                )
        # Exhaustive cross-product of the dimensions affecting opt-in/transport;
        # independent diarization/retention/notice dimensions are covered per row.
        dimensions = {
            "meeting_record": (None, "off", "manual", "prompt", "bad"),
            "meeting_ai_endpoint": (None, "off", "https://example.org", "http://10.0.0.4", "bad"),
            "meeting_ai_allow_plaintext": (None, "true", "on", "yes", "1", "false"),
            "meeting_ai_allow_external": (None, "true", "false", "bad"),
            "profile": (None, "secure"),
        }
        combinations = 0
        for values in itertools.product(*dimensions.values()):
            raw = {k: v for k, v in zip(dimensions, values, strict=True) if v is not None}
            parsed = parse_policy(core.Policy(raw=raw, status=core.PolicyStatus.OK))
            failures.extend(
                f"combination-{combinations}: {name}" for name in check_invariants(parsed)
            )
            combinations += 1
    return {
        "ok": not failures,
        "cases": len(cases),
        "rows": rows,
        "invariant_combinations": combinations,
        "failures": failures,
        "fixture_owner": "inode-scoped fstat UID simulation; file contents/modes and load are real",
    }


def text_report(report: dict[str, Any]) -> list[str]:
    lines = ["случай · ключ · значение · решение · locked · reason-код"]
    for row in report["rows"]:
        lines.append(
            " · ".join(str(row[k]) for k in ("case", "key", "raw", "decision", "locked", "reason"))
        )
    lines.append(
        f"{'PASS' if report['ok'] else 'FAIL'}: {report['cases']} cases; "
        f"{report['invariant_combinations']} invariant combinations"
    )
    lines.append("Fixture owner: " + report["fixture_owner"])
    lines.extend(report["failures"])
    return lines
