"""T-439: scope-specific event logging; hostile text never reaches handlers."""

from __future__ import annotations

import ast
import io
import json
import logging
from pathlib import Path
from unittest.mock import Mock

import pytest
from requests.exceptions import RequestException

from astra_voice.core import logging as av_logging
from astra_voice.meetings import events

pytestmark = pytest.mark.unit
MARKER = "PRIVATE_MEETING_MARKER"
SECRETS = (
    "meeting_title",
    "speaker_name",
    "notice_text",
    "api_key",
    "authorization",
    "prompt",
    "completion",
    "segment_text",
)


@pytest.mark.parametrize("field", SECRETS)
@pytest.mark.parametrize("where", ["extra", "dict-args"])
def test_secret_fields_redacted(field: str, where: str) -> None:
    record = logging.LogRecord("dictation-test", logging.INFO, __file__, 1, "message", (), None)
    if where == "extra":
        record.__dict__[field] = MARKER
    else:
        record.msg = f"%({field})s"
        record.args = {field: MARKER}
    av_logging.RedactTextFilter().filter(record)
    assert field in av_logging.SECRET_FIELDS
    assert MARKER not in str(record.__dict__)


@pytest.mark.parametrize(
    "name",
    [
        "astra_voice.meetings.policy",
        "astra_voice.meetings.engine",
        "astra_voice.worker.meeting_roles",
        "astra_voice.worker.meeting_capture",
    ],
)
@pytest.mark.parametrize(
    "kind", ["json", "requests", "engine", "positional", "formatted", "dict", "stack"]
)
def test_meeting_scope_drops_raw_text_and_tracebacks(name: str, kind: str) -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.addFilter(av_logging.RedactTextFilter())
    logger = logging.getLogger(name)
    before = logger.level, logger.propagate
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    logger.addHandler(handler)
    try:
        if kind in {"json", "requests", "engine"}:
            exception = {
                "json": json.JSONDecodeError(MARKER, MARKER, 0),
                "requests": RequestException("https://private.example/" + MARKER),
                "engine": RuntimeError(MARKER),
            }[kind]
            try:
                raise exception
            except Exception:
                logger.exception("raw %s", MARKER)
        elif kind == "positional":
            logger.debug("body=%s", MARKER)
        elif kind == "formatted":
            logger.debug(f"body={MARKER}")
        elif kind == "dict":
            logger.debug("%(payload)s", {"payload": MARKER})
        else:
            record = logger.makeRecord(name, logging.ERROR, __file__, 1, "raw", (), None)
            record.stack_info = MARKER
            record.exc_text = MARKER
            handler.handle(record)
        content = stream.getvalue()
        assert content and MARKER not in content
        assert "Traceback" not in content and "private.example" not in content
        if kind in {"json", "requests", "engine"}:
            assert type(exception).__name__ in content
    finally:
        logger.removeHandler(handler)
        logger.setLevel(before[0])
        logger.propagate = before[1]
        handler.close()


def test_valid_events_keep_numeric_enum_fields_and_reject_free_arguments() -> None:
    good = logging.LogRecord(
        "astra_voice.meetings.events",
        logging.INFO,
        __file__,
        1,
        events.CAPTURE_STATS,
        (1200, 42),
        None,
    )
    av_logging.RedactTextFilter().filter(good)
    assert "1200" in good.getMessage() and "42" in good.getMessage()
    bad = logging.LogRecord(
        "astra_voice.meetings.events",
        logging.INFO,
        __file__,
        1,
        events.CAPTURE_STATS,
        (MARKER, 42),
        None,
    )
    av_logging.RedactTextFilter().filter(bad)
    assert MARKER not in bad.getMessage()
    extra = logging.LogRecord(
        "astra_voice.meetings.events", logging.INFO, __file__, 1, events.INVALID_LOG, (), None
    )
    extra.__dict__["application_name"] = {"nested": MARKER}
    av_logging.RedactTextFilter().filter(extra)
    assert MARKER not in str(extra.__dict__)


def test_dictation_positional_message_and_exception_behavior_unchanged() -> None:
    record = logging.LogRecord(
        "astra_voice.worker.state", logging.WARNING, __file__, 1, "value=%s", ("legacy",), None
    )
    av_logging.RedactTextFilter().filter(record)
    assert record.getMessage() == "value=legacy"


def violations(source: str) -> list[int]:
    """Allow direct logging only; reject escaping loggers and event rebinding.

    This is deliberately conservative across scopes/branches: event imports may
    not be rebound anywhere, and logger/method references may not be stored,
    passed around or accessed dynamically. No data-flow inference is needed to
    establish the origin of an accepted message.
    """
    tree = ast.parse(source)
    parents = {child: parent for parent in ast.walk(tree) for child in ast.iter_child_nodes(parent)}
    import_bindings: dict[str, int] = {}
    modules: set[str] = set()
    constants: set[str] = set()
    logging_modules = {"logging"}
    getters: set[str] = set()
    direct: set[str] = set()
    logger_names = {"log", "logger"}
    levels = {
        "debug",
        "info",
        "warning",
        "warn",
        "error",
        "exception",
        "critical",
        "log",
        "_log",
        "handle",
    }
    errors: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                import_bindings[name] = import_bindings.get(name, 0) + 1
        if isinstance(node, ast.Import):
            logging_modules.update(
                alias.asname or alias.name for alias in node.names if alias.name == "logging"
            )
            modules.update(
                alias.asname or alias.name
                for alias in node.names
                if alias.name == "astra_voice.meetings.events"
            )
        if isinstance(node, ast.ImportFrom):
            if node.module == "astra_voice.meetings":
                modules.update(
                    alias.asname or alias.name for alias in node.names if alias.name == "events"
                )
            if node.module == "astra_voice.meetings.events":
                constants.update(
                    alias.asname or alias.name for alias in node.names if alias.name.isupper()
                )
            if node.module == "logging":
                for alias in node.names:
                    if alias.name == "*":
                        errors.append(node.lineno)
                    elif alias.name in {"getLogger", "Logger", "LoggerAdapter"}:
                        getters.add(alias.asname or alias.name)
                    elif alias.name in levels:
                        direct.add(alias.asname or alias.name)

    def logger_call(node: ast.AST) -> bool:
        if not isinstance(node, ast.Call):
            return False
        func = node.func
        return (isinstance(func, ast.Name) and func.id in getters) or (
            isinstance(func, ast.Attribute)
            and isinstance(func.value, ast.Name)
            and func.value.id in logging_modules
            and func.attr in {"getLogger", "Logger", "LoggerAdapter"}
        )

    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and logger_call(node.value):
            logger_names.update(
                target.id for target in node.targets if isinstance(target, ast.Name)
            )

    def from_events(node: ast.AST) -> bool:
        return (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id in modules
        )

    def logger_reference(node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id in logger_names | logging_modules) or (
            isinstance(node, ast.Attribute) and node.attr in {"log", "logger"}
        )

    event_bindings = modules | constants
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and node.id in event_bindings:
            if not isinstance(node.ctx, ast.Load) or import_bindings.get(node.id, 0) != 1:
                errors.append(node.lineno)
            # A module may only be accessed through a literal attribute; e.g.
            # setattr(events, ...) and passing events to a mutator are forbidden.
            elif node.id in modules and not isinstance(parents.get(node), ast.Attribute):
                errors.append(node.lineno)
        elif isinstance(node, ast.arg) and node.arg in event_bindings:
            errors.append(node.lineno)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in event_bindings:
                errors.append(node.lineno)
        elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
            if node.name in event_bindings:
                errors.append(node.lineno)
        elif isinstance(node, ast.MatchMapping):
            if node.rest in event_bindings:
                errors.append(node.lineno)
        elif isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
            root: ast.AST = node
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name) and root.id in modules:
                errors.append(node.lineno)

        if logger_reference(node) and isinstance(node, (ast.Name, ast.Attribute)):
            if not isinstance(node.ctx, ast.Load):
                continue
            parent = parents.get(node)
            grandparent = parents.get(parent) if parent is not None else None
            if not (
                isinstance(parent, ast.Attribute)
                and parent.value is node
                and parent.attr in levels | {"getLogger", "Logger", "LoggerAdapter"}
                and isinstance(grandparent, ast.Call)
                and grandparent.func is parent
            ):
                errors.append(node.lineno)
        elif isinstance(node, ast.Name) and node.id in direct | getters:
            parent = parents.get(node)
            if not isinstance(parent, ast.Call) or parent.func is not node:
                errors.append(node.lineno)

    def constant(node: ast.AST) -> bool:
        return (isinstance(node, ast.Name) and node.id in constants) or (
            from_events(node) and isinstance(node, ast.Attribute) and node.attr.isupper()
        )

    def argument(node: ast.AST) -> bool:
        number = isinstance(node, ast.Constant) and type(node.value) in {int, float}
        helper = (
            isinstance(node, ast.Call)
            and from_events(node.func)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"number", "policy_code", "setting_key", "exception_kind"}
        )
        enum_member = isinstance(node, ast.Attribute) and from_events(node.value)
        return number or helper or enum_member

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in direct:
            method = "log" if func.id == "log" else "direct"
        elif isinstance(func, ast.Attribute) and func.attr in levels:
            receiver = func.value
            if not (logger_reference(receiver) or logger_call(receiver)):
                continue
            method = func.attr
        else:
            continue
        if (
            method in {"log", "_log", "handle"}
            or not node.args
            or not constant(node.args[0])
            or any(not argument(arg) for arg in node.args[1:])
            or node.keywords
        ):
            errors.append(node.lineno)
    return errors


def test_package_logging_uses_only_event_constants_ast() -> None:
    root = Path(__file__).resolve().parents[2] / "src/astra_voice"
    files = sorted((root / "meetings").rglob("*.py")) + sorted(
        (root / "worker").glob("meeting_*.py")
    )
    assert files
    found = {str(path.relative_to(root)): violations(path.read_text()) for path in files}
    assert not {name: lines for name, lines in found.items() if lines}


@pytest.mark.parametrize(
    "call",
    [
        'log.info(f"raw {value}")',
        'log.warning("raw %s", value)',
        "log.error(events.INVALID_LOG, str(error))",
        "logger.exception(events.INVALID_LOG, exc_info=True)",
        "alias.info(events.INVALID_LOG + value)",
        'logging.getLogger(__name__).debug("raw")',
        'log.info(events.INVALID_LOG, {"payload": value})',
    ],
)
def test_ast_guard_catches_bypass_shapes(call: str) -> None:
    source = "from astra_voice.meetings import events\nalias = logging.getLogger(__name__)\n" + call
    assert violations(source)


@pytest.mark.parametrize(
    "body",
    [
        "alias = log\nalias.info(secret)",
        "emit = log.info\nemit(secret)",
        'getattr(log, "info")(secret)',
        "self.log.info(secret)",
        'message = f"raw {secret}"\nalias = log\nalias.info(message)',
        'message = f"raw {secret}"\nemit = log.info\nemit(message)',
        'message = f"raw {secret}"\ngetattr(log, "info")(message)',
        'message = f"raw {secret}"\nself.log.info(message)',
        "INVALID_LOG = secret\nlog.info(INVALID_LOG)",
        "events = replacement\nlog.info(events.INVALID_LOG)",
        "events.INVALID_LOG = secret\nlog.info(events.INVALID_LOG)",
        "def send(INVALID_LOG):\n    log.info(INVALID_LOG)",
        "def send(events):\n    log.info(events.INVALID_LOG)",
        "from unrelated import INVALID_LOG\nlog.info(INVALID_LOG)",
        "del events.INVALID_LOG\nlog.info(events.INVALID_LOG)",
        "try:\n    work()\nexcept Exception as INVALID_LOG:\n    log.info(INVALID_LOG)",
        "match secret:\n    case INVALID_LOG:\n        log.info(INVALID_LOG)",
        "match secret:\n    case [*INVALID_LOG]:\n        log.info(INVALID_LOG)",
        "match secret:\n    case {**INVALID_LOG}:\n        log.info(INVALID_LOG)",
    ],
)
def test_ast_guard_rejects_logger_escape_and_event_rebinding(body: str) -> None:
    source = (
        "from astra_voice.meetings import events\n"
        "from astra_voice.meetings.events import INVALID_LOG\n" + body
    )
    assert violations(source)


def test_ast_guard_accepts_direct_imported_event_and_numeric_enum_arguments() -> None:
    assert not violations(
        "import logging as lg\n"
        "from astra_voice.meetings import events as ev\n"
        "from astra_voice.meetings.events import INVALID_LOG as CODE\n"
        "sink = lg.getLogger(__name__)\n"
        "sink.info(CODE)\n"
        "sink.info(ev.CAPTURE_STATS, 12, ev.number(value))\n"
        "sink.warning(ev.POLICY_REJECTED, ev.PolicyCode.INVALID)\n"
    )


@pytest.mark.parametrize(
    "role", ["meeting-capture", "meeting-transcribe", "meeting-diar", "worker"]
)
def test_only_meeting_children_discard_native_stderr(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    import socket
    import subprocess

    from astra_voice.worker.supervisor import WorkerSupervisor

    parent, child = Mock(), Mock()
    child.fileno.return_value = 123
    monkeypatch.setattr(socket, "socketpair", Mock(return_value=(parent, child)))
    constructor = Mock()
    monkeypatch.setattr(subprocess, "Popen", constructor)
    supervisor = WorkerSupervisor(lambda message: None, role=role, use_qt=False)
    supervisor._launch()
    options = constructor.call_args.kwargs
    if role == "worker":
        assert "stderr" not in options
    else:
        assert options["stderr"] == subprocess.DEVNULL


def test_native_stderr_suppression_is_fd_level_and_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import os

    from astra_voice.worker import meeting_roles

    opened = Mock(return_value=99)
    duplicated = Mock()
    closed = Mock()
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "dup2", duplicated)
    monkeypatch.setattr(os, "close", closed)
    assert meeting_roles.silence_native_stderr()
    duplicated.assert_called_once_with(99, 2)
    closed.assert_called_once_with(99)
    opened.side_effect = OSError(MARKER)
    assert not meeting_roles.silence_native_stderr()


@pytest.mark.parametrize(
    "source",
    [
        'from logging import getLogger\ncustom=getLogger(__name__)\ncustom.info("PRIVATE")',
        'import logging as lg\nlg.warning("PRIVATE")',
        'from logging import warning as emit\nemit("PRIVATE")',
    ],
)
def test_ast_guard_catches_logging_import_aliases(source: str) -> None:
    assert violations(source)


def test_closed_stderr_fd_remains_devnull(monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    from astra_voice.worker import meeting_roles

    monkeypatch.setattr(os, "open", Mock(return_value=2))
    monkeypatch.setattr(os, "dup2", Mock())
    closed = Mock()
    monkeypatch.setattr(os, "close", closed)
    assert meeting_roles.silence_native_stderr()
    closed.assert_not_called()


def test_stderr_failure_refuses_entry_before_hardening(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    from astra_voice.worker import main, meeting_roles

    monkeypatch.setattr(meeting_roles, "silence_native_stderr", lambda: False)
    harden = Mock(side_effect=AssertionError("hardening must not start"))
    monkeypatch.setattr(main, "harden_process", harden)
    monkeypatch.setattr(
        main, "harden_meeting_limits", Mock(side_effect=AssertionError("limits must not change"))
    )
    monkeypatch.setattr(socket, "socket", Mock(side_effect=AssertionError("socket must not open")))
    monkeypatch.setattr(
        meeting_roles, "run_role", Mock(side_effect=AssertionError("role must not run"))
    )
    assert main.meeting_main("meeting-capture", ["12"]) == 3
    harden.assert_not_called()


def test_two_filters_keep_sanitized_exception_class() -> None:
    record = logging.LogRecord(
        "astra_voice.meetings.engine",
        logging.ERROR,
        __file__,
        1,
        MARKER,
        (),
        (RuntimeError, RuntimeError(MARKER), None),
    )
    guard = av_logging.RedactTextFilter()
    guard.filter(record)
    first = record.getMessage()
    guard.filter(record)
    assert record.getMessage() == first and "RuntimeError" in first and MARKER not in first
