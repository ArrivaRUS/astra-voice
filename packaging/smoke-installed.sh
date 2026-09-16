#!/bin/sh
# Проверяем именно установленную раскладку: bootstrap.py лежит вне пакета.
set -eu

say() { printf '\033[1m==> %s\033[0m\n' "$*"; }
die() {
	printf '\033[31mОШИБКА: %s\033[0m\n' "$*" >&2
	exit 1
}

ROOTDIR="${1:-/}"
ROOTDIR="$(cd "$ROOTDIR" && pwd -P)" || die "нет корня установленного дерева $ROOTDIR"
ROOTDIR="${ROOTDIR%/}"
LIB="$ROOTDIR/usr/lib/astra-voice"
[ -d "$LIB" ] || die "нет установленного каталога $LIB"
export ASTRA_VOICE_RESOURCES="$ROOTDIR/usr/share/astra-voice"

# Оба запуска используют временные XDG-каталоги и не трогают профиль пользователя.
TMP="$(mktemp -d)" || die "не удалось создать временный каталог для смоука"
trap 'rm -rf "$TMP"' EXIT
trap 'exit 1' HUP INT TERM
export QT_QPA_PLATFORM=offscreen
export XDG_CONFIG_HOME="$TMP/config" XDG_DATA_HOME="$TMP/data"
export XDG_CACHE_HOME="$TMP/cache" XDG_RUNTIME_DIR="$TMP/runtime"
mkdir -m 0700 "$XDG_CONFIG_HOME" "$XDG_DATA_HOME" "$XDG_CACHE_HOME" "$XDG_RUNTIME_DIR" ||
	die "не удалось создать временные XDG-каталоги"

say "смоук импортов: $LIB"
# Python -I игнорирует PYTHONPATH: передаём LIB аргументом и меняем sys.path сами.
python3 -I -c '
import importlib
import pkgutil
import sys
from pathlib import Path

lib = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(lib))
if (lib / "vendor").is_dir():
    sys.path.insert(0, str(lib / "vendor"))

failures = {}
checked = 0

def record_failure(name, error):
    failures[name] = f"{type(error).__name__}: {error}"

def walk_error(name):
    # walk_packages сам импортирует пакеты при обходе; его ошибки тоже не скрываем.
    record_failure(name, sys.exception())

try:
    checked += 1
    import astra_voice

    for module in pkgutil.walk_packages(astra_voice.__path__, "astra_voice.", onerror=walk_error):
        checked += 1
        try:
            importlib.import_module(module.name)
        except BaseException as error:
            record_failure(module.name, error)
except BaseException as error:
    record_failure("astra_voice / обход пакета", error)

print(f"Проверено модулей: {checked}", flush=True)
for name, error in failures.items():
    print(f"{name}: {error}", file=sys.stderr)
sys.exit(1 if failures else 0)
' "$LIB" || die "смоук импортов установленного дерева завершился с ошибкой: $LIB"

say "смоук ресурсов: $ASTRA_VOICE_RESOURCES"
python3 -I -c '
import sys
from pathlib import Path

lib = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(lib))
if (lib / "vendor").is_dir():
    sys.path.insert(0, str(lib / "vendor"))

from astra_voice.core import paths
from astra_voice.core.model_source import smoke_wav_path
from astra_voice.platform.session import SessionKind
from astra_voice.ui.tray_icons import TrayIconProvider, TrayState, find_tray_icon_path

checked = 0
failures = []
provider = TrayIconProvider(SessionKind.FLY)
for state in TrayState:
    for size in (16, 22):
        checked += 1
        try:
            find_tray_icon_path(provider.icon_name(state), size)
        except FileNotFoundError as error:
            failures.append(str(error))

for path in (
    paths.qml_dir() / "Pill.qml",
    paths.qml_dir() / "Main.qml",
    paths.data_dir_static() / "vad" / "silero_vad.onnx",
    paths.data_dir_static() / "catalog.json",
    paths.data_dir_static() / "catalog.json.sig",
    paths.data_dir_static() / "catalog.schema.json",
    smoke_wav_path(),
):
    checked += 1
    if not path.is_file():
        failures.append(f"Нет файла ресурса: {path}")

print(f"Проверено ресурсов: {checked}", flush=True)
for error in failures:
    print(error, file=sys.stderr)
sys.exit(1 if failures else 0)
' "$LIB" || die "смоук ресурсов установленного дерева завершился с ошибкой: $ASTRA_VOICE_RESOURCES"

say "смоук app --version"
python3 -I "$LIB/bootstrap.py" app --version ||
	die "bootstrap.py app --version завершился с ошибкой: $LIB"
