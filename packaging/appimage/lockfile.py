#!/usr/bin/env python3
"""Разбор `packaging/appimage.lock` — единственного источника закреплённых входов AppImage.

Формат (arch/appimage.md §9.1–§9.2, T1 MN-9):

* pip-требования с `--hash=sha256:` — колёса ставятся `pip install --require-hashes`;
* `# tool: <файл> <sha256> <размер> <url>` — инструменты сборки (ровно четыре);
* `# expect-elf: N`, `# max-glibc: X.Y` — гейт `tools/elf-audit` (MN-9): новый ELF
  или символ glibc новее закреплённого роняет сборку, а не проходит молча;
* `# runtime-key: <40 hex>` — отпечаток ключа подписи `runtime-x86_64.sig`
  (проверяет `build.sh --fetch`);
* `# TODO-HASH: <имя>==<версия> …` — колесо решено добавить, но хэш ещё не
  закреплён (нет сети). Сборка с такой строкой — только локальная проверка.

    lockfile.py packaging/appimage.lock check          # формат; код 1 при ошибке
    lockfile.py packaging/appimage.lock get expect-elf # значение директивы
    lockfile.py packaging/appimage.lock tools          # строки «файл sha256 размер url»
    lockfile.py packaging/appimage.lock todo           # незакреплённые колёса, по строке
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

#: Инструменты, без которых сборка невозможна (имена файлов из `# tool:`).
REQUIRED_TOOLS = ("runtime-x86_64", "runtime-x86_64.sig", "appimagetool-x86_64.AppImage")
TOOL_COUNT = 4
PYTHON_TOOL_RE = re.compile(r"python3\.11\.\d+-cp311-cp311-manylinux_2_\d+_x86_64\.AppImage")
DIRECTIVES = ("expect-elf", "max-glibc", "runtime-key")

_TOOL_RE = re.compile(
    r"#\s*tool:\s+(?P<name>[A-Za-z0-9._+-]+)\s+(?P<sha>[0-9a-f]{64})\s+(?P<size>[0-9]+)"
    r"\s+(?P<url>https://\S+)\s*"
)
_DIRECTIVE_RE = re.compile(r"#\s*(?P<key>expect-elf|max-glibc|runtime-key):\s*(?P<value>\S+)\s*")
_TODO_RE = re.compile(r"#\s*TODO-HASH:\s*(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s]+)(?:\s.*)?")
_REQ_RE = re.compile(r"(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s\\]+)")
_HASH_RE = re.compile(r"--hash=sha256:(?P<sha>[0-9a-f]{64})")
_VALUE_RE = {
    "expect-elf": re.compile(r"[0-9]+"),
    "max-glibc": re.compile(r"[0-9]+\.[0-9]+"),
    "runtime-key": re.compile(r"[0-9A-F]{40}"),
}


class LockError(ValueError):
    """Файл блокировки не соответствует формату."""


@dataclass(frozen=True)
class Tool:
    name: str
    sha256: str
    size: int
    url: str


@dataclass(frozen=True)
class Requirement:
    name: str
    version: str
    hashes: tuple[str, ...]


@dataclass
class Lock:
    tools: list[Tool] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    directives: dict[str, str] = field(default_factory=dict)
    todo: list[tuple[str, str]] = field(default_factory=list)

    @property
    def expect_elf(self) -> int:
        return int(self.directives["expect-elf"])

    @property
    def max_glibc(self) -> tuple[int, int]:
        major, minor = self.directives["max-glibc"].split(".")
        return int(major), int(minor)

    @property
    def runtime_key(self) -> str:
        return self.directives["runtime-key"]

    def tool(self, name: str) -> Tool:
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise LockError(f"нет инструмента {name}")

    @property
    def python_tool(self) -> Tool:
        for tool in self.tools:
            if PYTHON_TOOL_RE.fullmatch(tool.name):
                return tool
        raise LockError("нет инструмента python3.11 AppImage")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse(text: str) -> Lock:
    """Разобрать текст lock-файла и проверить его полноту."""
    lock = Lock()
    # Продолжения строк «\» склеиваем, как pip.
    logical: list[str] = []
    buffer = ""
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.lstrip().startswith("#"):
            if buffer:
                raise LockError(f"комментарий внутри требования: {raw.strip()}")
            logical.append(line.strip())
            continue
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        logical.append((buffer + line).strip())
        buffer = ""
    if buffer:
        raise LockError("требование оборвано продолжением «\\» в конце файла")

    for line in logical:
        if not line:
            continue
        if line.startswith("#"):
            if match := _TOOL_RE.fullmatch(line):
                lock.tools.append(
                    Tool(match["name"], match["sha"], int(match["size"]), match["url"])
                )
            elif line.startswith(("# tool:", "#tool:")):
                raise LockError(f"неверная строка инструмента: {line}")
            elif match := _DIRECTIVE_RE.fullmatch(line):
                key, value = match["key"], match["value"]
                if key in lock.directives:
                    raise LockError(f"директива {key} повторяется")
                if not _VALUE_RE[key].fullmatch(value):
                    raise LockError(f"неверное значение {key}: {value}")
                lock.directives[key] = value
            elif any(line.startswith(f"# {key}") for key in DIRECTIVES):
                raise LockError(f"неверная директива: {line}")
            elif match := _TODO_RE.fullmatch(line):
                lock.todo.append((match["name"], match["version"]))
            elif "TODO-HASH" in line:
                raise LockError(f"неверная строка TODO-HASH: {line}")
            continue
        parts = line.split()
        req = _REQ_RE.fullmatch(parts[0])
        if req is None:
            raise LockError(f"требование без точной версии: {parts[0]}")
        hashes: list[str] = []
        for option in parts[1:]:
            hash_match = _HASH_RE.fullmatch(option)
            if hash_match is None:
                raise LockError(f"{req['name']}: неожиданный параметр {option}")
            hashes.append(hash_match["sha"])
        if not hashes:
            raise LockError(f"{req['name']}: нет --hash=sha256")
        lock.requirements.append(Requirement(req["name"], req["version"], tuple(hashes)))

    missing = [key for key in DIRECTIVES if key not in lock.directives]
    if missing:
        raise LockError(f"нет директив: {', '.join(missing)}")
    names = [tool.name for tool in lock.tools]
    if len(names) != len(set(names)):
        raise LockError("инструменты повторяются")
    if len(lock.tools) != TOOL_COUNT:
        raise LockError(f"ожидалось {TOOL_COUNT} инструмента, найдено {len(lock.tools)}")
    for name in REQUIRED_TOOLS:
        lock.tool(name)
    lock.python_tool  # noqa: B018 — проверка наличия
    seen: set[str] = set()
    for item in [(r.name, r.version) for r in lock.requirements] + lock.todo:
        key = _normalize(item[0])
        if key in seen:
            raise LockError(f"пакет {item[0]} указан дважды")
        seen.add(key)
    return lock


def load(path: Path) -> Lock:
    return parse(path.read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) < 2 or args[1] not in ("check", "get", "tools", "todo", "requirements"):
        sys.stderr.write(__doc__ or "")
        return 2
    try:
        lock = load(Path(args[0]))
    except (OSError, LockError) as exc:
        sys.stderr.write(f"ОШИБКА: {args[0]}: {exc}\n")
        return 1
    command = args[1]
    if command == "get":
        if len(args) != 3 or args[2] not in DIRECTIVES:
            sys.stderr.write(f"get: одна из {', '.join(DIRECTIVES)}\n")
            return 2
        print(lock.directives[args[2]])
    elif command == "tools":
        for tool in lock.tools:
            print(tool.name, tool.sha256, tool.size, tool.url)
    elif command == "todo":
        for name, version in lock.todo:
            print(f"{name}=={version}")
    elif command == "requirements":
        for req in lock.requirements:
            print(f"{req.name}=={req.version}")
    else:
        print(
            f"lock: {len(lock.tools)} инструмента, {len(lock.requirements)} колёс, "
            f"ELF {lock.expect_elf}, glibc ≤ {lock.directives['max-glibc']}, "
            f"незакреплено: {len(lock.todo)}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
