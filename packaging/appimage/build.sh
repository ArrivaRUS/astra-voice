#!/usr/bin/env bash
# Сборка AppImage Astra Voice (arch/appimage.md §9; перенос спайка 28.09).
#
#   packaging/appimage/build.sh --fetch   # СЕТЬ: инструменты и колёса по packaging/appimage.lock в кэш
#   packaging/appimage/build.sh           # без сети: AppDir → гейты → dist/Astra_Voice-<версия>-x86_64.AppImage
#
# Включение артефакта — файл packaging/appimage/ENABLED: без него скрипт ничего не
# делает и завершается успешно («AppImage выключен»). Workflow вызывает только этот
# скрипт и smoke.sh, поэтому правки сборки не требуют правки .github/workflows.
#
# Каталоги (переопределяются окружением):
#   ASTRA_VOICE_APPIMAGE_CACHE  входы: downloads/, wheels/  (~/.cache/astra-voice-dev/appimage; кэш CI)
#   ASTRA_VOICE_WHEEL_CACHE     колёса onnxruntime/onnx-asr от .deb (~/.cache/astra-voice-dev/wheels)
#   ASTRA_VOICE_APPIMAGE_WORK   рабочий: AppDir, tmp, временный HOME (<репо>/.build/appimage)
#   ASTRA_VOICE_APPIMAGE_OUT    результат: .AppImage и sbom-appimage.cdx.json (<репо>/dist)
#
# Бандловый Python запускается только в изоляции: без дисплея, шины сессии, звука и
# с временным HOME. От root (контейнер CI) запуск программы идёт под nobody через
# setpriv: программа в бандле от root не работает (T1 MJ-3).
# ASTRA_VOICE_APPIMAGE_ALLOW_TODO_HASH=1 — только локальная проверка при незакреплённых
# колёсах (строки `# TODO-HASH:` в lock): образ НЕ для выпуска; в CI не ставится.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
HERE=$ROOT/packaging/appimage
LOCK=$ROOT/packaging/appimage.lock
CACHE=${ASTRA_VOICE_APPIMAGE_CACHE:-$HOME/.cache/astra-voice-dev/appimage}
DOWNLOADS=$CACHE/downloads
WHEELS=$CACHE/wheels
ORT_WHEELS=${ASTRA_VOICE_WHEEL_CACHE:-$HOME/.cache/astra-voice-dev/wheels}
WORK=${ASTRA_VOICE_APPIMAGE_WORK:-$ROOT/.build/appimage}
OUT=${ASTRA_VOICE_APPIMAGE_OUT:-$ROOT/dist}
RUNTIME_KEYRING=$HERE/keys/appimage-runtime.gpg

say() { printf '==> %s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

FETCH=0
for arg in "$@"; do
    case $arg in
        --fetch) FETCH=1 ;;
        -h | --help) sed -n '2,21p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "неизвестный аргумент: $arg" >&2; exit 2 ;;
    esac
done

if [ ! -f "$HERE/ENABLED" ]; then
    say 'AppImage выключен (нет packaging/appimage/ENABLED) — сборка пропущена'
    exit 0
fi

for tool in python3 sha256sum readelf objdump patch find xargs stat; do
    command -v "$tool" >/dev/null 2>&1 || die "нет $tool на машине сборки"
done
lockq() { python3 "$HERE/lockfile.py" "$LOCK" "$@"; }
lockq check || die "packaging/appimage.lock не прошёл проверку формата"
TODO=$(lockq todo)
if [ -n "$TODO" ]; then
    if [ "${ASTRA_VOICE_APPIMAGE_ALLOW_TODO_HASH:-}" = 1 ]; then
        printf 'ПРЕДУПРЕЖДЕНИЕ: колёса без хэша пропущены — образ НЕ для выпуска:\n%s\n' "$TODO" >&2
    else
        die "в lock есть колёса без хэша (# TODO-HASH): $(echo "$TODO" | tr '\n' ' ')— закрепите хэши"
    fi
fi

VERSION=$(sed -n '1s/.*(\([^)]*\)).*/\1/p' "$ROOT/packaging/debian/changelog")
[ -n "$VERSION" ] || die 'не найдена версия в changelog'
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    CHANGELOG_DATE=$(sed -n 's/^ -- .*>  //p' "$ROOT/packaging/debian/changelog" | head -1)
    SOURCE_DATE_EPOCH=$(date -u -d "$CHANGELOG_DATE" +%s) || die 'не разобрал дату changelog'
fi
export SOURCE_DATE_EPOCH

# Инструмент: файл есть, размер и sha256 совпадают с lock.
tool_ok() {
    local file=$DOWNLOADS/$1
    [ -f "$file" ] && [ "$(stat -c %s "$file")" = "$3" ] &&
        [ "$(sha256sum "$file" | cut -d' ' -f1)" = "$2" ]
}

# --- --fetch: единственный шаг с сетью ---------------------------------------
if [ "$FETCH" = 1 ]; then
    for tool in curl gpgv; do
        command -v "$tool" >/dev/null 2>&1 || die "для --fetch нужен $tool"
    done
    [ -f "$RUNTIME_KEYRING" ] || die "нет ключа подписи runtime: $RUNTIME_KEYRING"
    mkdir -p "$DOWNLOADS" "$WHEELS"
    while read -r name sha size url; do
        if tool_ok "$name" "$sha" "$size"; then
            say "инструмент в кэше: $name"
            continue
        fi
        say "качаю $name"
        part=$DOWNLOADS/.$name.part
        curl -fsSL --proto '=https' --tlsv1.2 --retry 3 --max-filesize $((size + 1)) \
            -o "$part" "$url" || { rm -f "$part"; die "не скачался $name"; }
        mv -f "$part" "$DOWNLOADS/$name"
        tool_ok "$name" "$sha" "$size" || { rm -f "$DOWNLOADS/$name"; die "sha256 или размер не совпал: $name"; }
    done < <(lockq tools)

    # Подпись runtime (T1 §2.2 п.10): хостовый gpgv, ключ из репозитория, отпечаток из lock.
    say 'проверяю подпись runtime-x86_64.sig'
    want=$(lockq get runtime-key)
    status=$(gpgv --status-fd 1 --keyring "$RUNTIME_KEYRING" \
        "$DOWNLOADS/runtime-x86_64.sig" "$DOWNLOADS/runtime-x86_64" 2>/dev/null) ||
        die 'подпись runtime-x86_64 не прошла проверку gpgv'
    validsig=$(printf '%s\n' "$status" | awk '$2 == "VALIDSIG" {print $3, $NF}')
    case " $validsig " in
        *" $want "*) say "подпись runtime: ключ $want" ;;
        *) die "runtime-x86_64 подписан не ключом $want (VALIDSIG: ${validsig:-нет})" ;;
    esac

    say 'качаю колёса по lock (--require-hashes)'
    python3 -m pip download --no-deps --only-binary=:all: --require-hashes \
        --implementation cp --python-version 3.11 --abi cp311 --abi abi3 --abi none \
        --platform manylinux_2_28_x86_64 --platform manylinux_2_27_x86_64 \
        --platform manylinux_2_17_x86_64 --platform manylinux2014_x86_64 \
        --platform manylinux_2_5_x86_64 --platform manylinux1_x86_64 --platform any \
        --dest "$WHEELS" -r "$LOCK"
    say 'готово; дальше сеть не нужна'
    exit 0
fi

# --- сборка ------------------------------------------------------------------
[ -d "$DOWNLOADS" ] && [ -d "$WHEELS" ] || die "нет кэша входов $CACHE — сначала build.sh --fetch (нужна сеть)"
while read -r name sha size _url; do
    tool_ok "$name" "$sha" "$size" || die "инструмент отсутствует или не совпал с lock: $name"
done < <(lockq tools)
PY_IMAGE=$DOWNLOADS/$(lockq tools | awk '$1 ~ /^python3\.11/ {print $1}')
TOOL=$DOWNLOADS/appimagetool-x86_64.AppImage
RUNTIME=$DOWNLOADS/runtime-x86_64
chmod 755 "$PY_IMAGE" "$TOOL"

START=$(date +%s)
APPDIR=$WORK/AppDir
BUILD=$WORK/build
ISOHOME=$WORK/home
STAMP=$WORK/.started
# Мусор extract-and-run (находка 28.09: /tmp/appimage_extracted_* на 36 МБ от
# appimagetool): все распаковки — в свой TMPDIR, уборка при любом выходе.
HOST_TMP=${TMPDIR:-/tmp}
export TMPDIR=$WORK/tmp
cleanup() {
    rm -rf "$WORK/tmp" "$ISOHOME" "$BUILD"
}
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
rm -rf "$APPDIR" "$BUILD" "$WORK/tmp" "$ISOHOME"
mkdir -p "$APPDIR" "$BUILD/extract" "$OUT" "$WORK/tmp" "$ISOHOME/run" "$ISOHOME/tmp"
chmod 700 "$ISOHOME" "$ISOHOME/run" "$ISOHOME/tmp"
touch "$STAMP"

# Изолированный запуск: без дисплея, D-Bus сессии, звука; HOME временный.
isolated() {
    env -u DISPLAY -u WAYLAND_DISPLAY -u QT_ACCESSIBILITY -u AT_SPI_BUS_ADDRESS \
        -u APPIMAGE -u APPDIR -u ARGV0 -u OWD -u APPIMAGE_EXTRACT_AND_RUN \
        -u PYTHONPATH -u PYTHONHOME -u ASTRA_VOICE_RESOURCES -u ASTRA_VOICE_APPIMAGE_DIR \
        DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent \
        QT_QPA_PLATFORM=offscreen PULSE_SERVER=unix:/nonexistent \
        HOME="$ISOHOME" XDG_DATA_HOME= XDG_CONFIG_HOME= XDG_CACHE_HOME= XDG_STATE_HOME= \
        XDG_RUNTIME_DIR="$ISOHOME/run" TMPDIR="$ISOHOME/tmp" "$@"
}
# Запуск программы из бандла (AppRun): от root — под nobody (T1 MJ-3).
as_user() {
    if [ "$(id -u)" = 0 ]; then
        command -v setpriv >/dev/null 2>&1 || die 'нет setpriv (util-linux) для запуска не от root'
        chown -R 65534:65534 "$ISOHOME"
        isolated setpriv --reuid=65534 --regid=65534 --clear-groups -- "$@"
    else
        isolated "$@"
    fi
}

say "версия $VERSION, SOURCE_DATE_EPOCH=$SOURCE_DATE_EPOCH"
say 'распаковываю Python AppImage'
(cd "$BUILD/extract" && "$PY_IMAGE" --appimage-extract >/dev/null)
mv "$BUILD/extract/squashfs-root/opt" "$APPDIR/opt"
mv "$BUILD/extract/squashfs-root/usr" "$APPDIR/usr"
rm -rf "$BUILD/extract"
PY=$APPDIR/opt/python3.11/bin/python3.11
SITE=$APPDIR/opt/python3.11/lib/python3.11/site-packages
STDLIB=$APPDIR/opt/python3.11/lib/python3.11

# База python-appimage приносит свои пакеты (certifi, packaging): их нет в lock и в SBOM,
# а pip --target не заменяет существующие каталоги. Оставляем только pip на время установки.
find "$SITE" -mindepth 1 -maxdepth 1 ! -name pip ! -name 'pip-*.dist-info' -exec rm -rf {} +
say 'ставлю колёса бандловым Python по lock'
find_links=(--find-links "$WHEELS")
[ -d "$ORT_WHEELS" ] && find_links+=(--find-links "$ORT_WHEELS")
isolated "$PY" -I -m pip install --no-deps --no-index --require-hashes \
    "${find_links[@]}" --no-cache-dir --no-compile --disable-pip-version-check \
    --target "$SITE" -r "$LOCK"

for vendor_patch in "$ROOT"/vendor/patches/*.patch; do
    [ -f "$vendor_patch" ] || continue
    (cd "$SITE" && patch -p1 --dry-run --forward --batch < "$vendor_patch" >/dev/null) ||
        die "патч $(basename "$vendor_patch") не подходит к onnx_asr"
    (cd "$SITE" && patch -p1 --forward --no-backup-if-mismatch --batch < "$vendor_patch" >/dev/null)
done

say 'удаляю ненужные модули Python и Qt'
rm -rf "$SITE/pip" "$SITE"/pip-*.dist-info "$SITE/bin" \
    "$STDLIB/ensurepip" "$STDLIB/idlelib" "$STDLIB/tkinter" \
    "$STDLIB/turtledemo" "$STDLIB/test" "$STDLIB/lib2to3/tests" \
    "$APPDIR/opt/python3.11/include" "$APPDIR/usr/share/tcltk" "$APPDIR/usr/bin"

# Состав site-packages = колёса lock, ни больше ни меньше (иначе SBOM врёт).
# shellcheck disable=SC2046
python3 - "$SITE" $(lockq requirements | sed 's/==.*//') <<'PY'
import re
import sys
from pathlib import Path

site = Path(sys.argv[1])
norm = lambda name: re.sub(r"[-_.]+", "-", name).lower()  # noqa: E731
want = {norm(name) for name in sys.argv[2:]}
have = {norm(path.stem.rsplit("-", 1)[0]) for path in site.glob("*.dist-info")}
if have != want:
    sys.exit(f"ОШИБКА: site-packages ≠ lock: лишние {sorted(have - want)}, нет {sorted(want - have)}")
print(f"site-packages: {len(have)} пакетов, ровно по lock")
PY

QT=$SITE/PyQt5/Qt5
PYQT=$SITE/PyQt5
for ext in "$PYQT"/*.abi3.so; do
    case $(basename "$ext") in
        QtCore.abi3.so|QtGui.abi3.so|QtWidgets.abi3.so|QtQml.abi3.so|QtQuick.abi3.so|QtDBus.abi3.so|QtNetwork.abi3.so) ;;
        *) rm -f "$ext" ;;
    esac
done
find "$PYQT" -type f -name '*.pyi' -delete
rm -rf "$QT/translations" "$QT/resources" "$QT/bin" "$QT/include" "$QT/mkspecs" "$QT/metatypes"

PLUGINS=$QT/plugins
for dir in "$PLUGINS"/*; do
    case $(basename "$dir") in
        platforms|xcbglintegrations|imageformats|iconengines|platforminputcontexts) ;;
        *) rm -rf "$dir" ;;
    esac
done
for dir in platforms xcbglintegrations imageformats iconengines platforminputcontexts; do
    for plugin in "$PLUGINS/$dir"/*.so; do
        case "$dir/$(basename "$plugin")" in
            platforms/libqxcb.so|platforms/libqoffscreen.so|xcbglintegrations/libqxcb-glx-integration.so|imageformats/libqsvg.so|iconengines/libqsvgicon.so|platforminputcontexts/libcomposeplatforminputcontextplugin.so|platforminputcontexts/libibusplatforminputcontextplugin.so) ;;
            *) rm -f "$plugin" ;;
        esac
    done
done

QML=$QT/qml
QML_KEEP=$BUILD/qml-keep
mkdir -p "$QML_KEEP/QtQuick"
mv "$QML/QtQuick.2" "$QML_KEEP/"
for module in Controls.2 Templates.2 Layouts Window.2; do
    mv "$QML/QtQuick/$module" "$QML_KEEP/QtQuick/"
done
mv "$QML/QtQml" "$QML_KEEP/"
rm -rf "$QML_KEEP/QtQuick/Controls.2/Material" \
    "$QML_KEEP/QtQuick/Controls.2/Universal" \
    "$QML_KEEP/QtQuick/Controls.2/Imagine" \
    "$QML_KEEP/QtQuick/Controls.2/Fusion" \
    "$QML_KEEP/QtQml/RemoteObjects" "$QML_KEEP/QtQml/StateMachine"
rm -rf "$QML"
mv "$QML_KEEP" "$QML"

# Начальный набор библиотек Qt дополняется рекурсивно по DT_NEEDED всех
# оставленных расширений PyQt, QML и плагинов Qt.
declare -A KEEP_LIB=()
for module in Core Gui Widgets DBus Network Qml QmlModels QmlWorkerScript Quick QuickControls2 QuickTemplates2 Svg XcbQpa; do
    KEEP_LIB[libQt5$module.so.5]=1
done
for file in "$QT/lib"/libicu*.so.*; do KEEP_LIB[$(basename "$file")]=1; done
changed=1
while [ "$changed" = 1 ]; do
    changed=0
    while IFS= read -r -d '' elf; do
        while IFS= read -r needed; do
            case $needed in
                libQt5*.so.*|libicu*.so.*)
                    if [ -f "$QT/lib/$needed" ] && [ -z "${KEEP_LIB[$needed]:-}" ]; then
                        KEEP_LIB[$needed]=1
                        changed=1
                    fi ;;
            esac
        done < <(readelf -d "$elf" 2>/dev/null | sed -n 's/.*Shared library: \[\([^]]*\)\].*/\1/p')
    done < <(find "$PYQT" -maxdepth 1 -name '*.abi3.so' -type f -print0; \
        find "$PLUGINS" "$QML" -type f -name '*.so' -print0; \
        for name in "${!KEEP_LIB[@]}"; do printf '%s\0' "$QT/lib/$name"; done)
done
for file in "$QT/lib"/*; do
    [ -f "$file" ] || continue
    [ -n "${KEEP_LIB[$(basename "$file")]:-}" ] || rm -f "$file"
done
for name in "${!KEEP_LIB[@]}"; do [ -f "$QT/lib/$name" ] || die "отсутствует DT_NEEDED $name"; done
say "Qt: оставлено ${#KEEP_LIB[@]} библиотек по DT_NEEDED"

# Tcl/Tk нужен только удалённому tkinter; его C-модуль держит libtk/libtcl — убираем
# и его, затем проверяем, что остальные ELF не ссылаются на удаляемые библиотеки.
rm -f "$STDLIB"/lib-dynload/_tkinter.cpython-*.so
for old in libtk8.6.so libtcl8.6.so libXft.so.2 libXrender.so.1; do
    [ -e "$APPDIR/usr/lib/$old" ] || continue
    needed_by=''
    while IFS= read -r -d '' elf; do
        [ "$elf" = "$APPDIR/usr/lib/$old" ] && continue
        if readelf -d "$elf" 2>/dev/null | grep -Fq "Shared library: [$old]"; then
            needed_by=$elf
            break
        fi
    done < <(find "$APPDIR" -type f \( -name '*.so' -o -name '*.so.*' -o -name 'python3.11' \) -print0)
    if [ -z "$needed_by" ]; then rm -f "$APPDIR/usr/lib/$old"; else say "сохраняю $old: нужен $needed_by"; fi
done

say 'копирую код и ресурсы'
LIB=$APPDIR/usr/lib/astra-voice
SHARE=$APPDIR/usr/share/astra-voice
mkdir -p "$LIB" "$SHARE"
cp -a "$ROOT/src/astra_voice" "$LIB/astra_voice"
rm -f "$LIB/astra_voice/_version.py"
mv "$LIB/astra_voice/bootstrap.py" "$LIB/bootstrap.py"
cp -a "$ROOT/qml" "$SHARE/qml"
cp -a "$ROOT/data" "$SHARE/data"
rm -rf "$SHARE/data/icons" "$SHARE/data/astra-voice.desktop" \
    "$SHARE/data/keys/README.md" "$SHARE/data/test" \
    "$SHARE/data/vad/README.md" "$SHARE/data/catalog-source.json" \
    "$SHARE/data/catalog-hf-cache.json"
# Значки темы — как в packaging/debian/rules (/usr/share/icons/hicolor):
# paths.icon_theme_dir() = <ресурсы>/../icons/hicolor. Свои usr/share/{icons,
# applications,metainfo} у python-appimage (значок Python) убираем.
rm -rf "$APPDIR/usr/share/icons" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/metainfo"
mkdir -p "$APPDIR/usr/share/icons/hicolor"
cp -a "$ROOT/data/icons/hicolor/." "$APPDIR/usr/share/icons/hicolor/"
printf '# Сгенерировано packaging/appimage/build.sh из packaging/debian/changelog.\n__version__ = "%s"\n' \
    "$VERSION" > "$LIB/astra_voice/_version.py"

install -m 755 "$HERE/AppRun" "$APPDIR/AppRun"
install -m 644 "$HERE/astra-voice.desktop" "$APPDIR/astra-voice.desktop"
install -m 644 "$ROOT/data/icons/hicolor/256x256/apps/astravoice.png" "$APPDIR/astravoice.png"
ln -s astravoice.png "$APPDIR/.DirIcon"

# Воспроизводимость: байт-код и кэши не попадают в образ, время файлов — из changelog.
find "$APPDIR" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$APPDIR" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
# BUILD_ID — хэш содержимого AppDir: та же сборка даёт тот же KEY установки.
content_id() {
    (cd "$APPDIR" && find . -type f ! -name .astra-voice-build -print0 | LC_ALL=C sort -z |
        xargs -0 sha256sum | awk '{print $2 " " $1}' | sha256sum | cut -c1-12)
}
BUILD_ID=$(content_id)
printf 'VERSION=%s\nBUILD_ID=%s\n' "$VERSION" "$BUILD_ID" > "$APPDIR/.astra-voice-build"
say "BUILD_ID=$BUILD_ID"

# --- гейты AppDir --------------------------------------------------------------
say 'гейт: QML-импорты'
isolated "$PY" -I -B "$HERE/check_qml.py" "$SHARE/qml"
say 'гейт: ресурсы'
isolated "$PY" -I -B "$HERE/check_resources.py" "$LIB"
say 'гейт: состав бандла (check_bundle.py)'
if [ -n "$TODO" ]; then
    # Локальная проверка без закреплённых колёс: гейт печатает нарушения, но не
    # останавливает сборку (ASTRA_VOICE_APPIMAGE_ALLOW_TODO_HASH=1, образ не для выпуска).
    isolated "$PY" -I -B "$HERE/check_bundle.py" --appdir "$APPDIR" \
        --control "$ROOT/packaging/debian/control" ||
        printf 'ПРЕДУПРЕЖДЕНИЕ: check_bundle.py не прошёл (ожидаемо без %s)\n' "$(echo "$TODO" | tr '\n' ' ')" >&2
else
    isolated "$PY" -I -B "$HERE/check_bundle.py" --appdir "$APPDIR" \
        --control "$ROOT/packaging/debian/control"
fi
say 'гейт: AppRun --version в изоляции, stderr пуст'
version_err=$WORK/tmp/version.stderr
version_output=$(as_user "$APPDIR/AppRun" --version 2>"$version_err") || {
    cat "$version_err" >&2
    die 'AppRun --version завершился с ошибкой'
}
[ "$version_output" = "astra-voice $VERSION" ] || die "неверный --version: $version_output"
[ ! -s "$version_err" ] || { cat "$version_err" >&2; die 'AppRun --version пишет в stderr'; }
rm -f "$version_err"
say "AppRun --version: $version_output"
# Запуски выше не должны оставить байт-код в AppDir (-B у гейтов, -I у AppRun не спасает).
find "$APPDIR" -type d -name __pycache__ -prune -exec rm -rf {} +
[ "$(content_id)" = "$BUILD_ID" ] || die 'гейты изменили содержимое AppDir — BUILD_ID разошёлся'

say 'гейт: ELF (T1 MN-9 — число и glibc из lock)'
python3 "$ROOT/tools/elf-audit" --strict --expect "$(lockq get expect-elf)" \
    --max-glibc "$(lockq get max-glibc)" "$APPDIR" | tail -n 3

say 'SBOM AppImage'
python3 "$ROOT/scripts/sbom.py" --appdir "$APPDIR" --lock "$LOCK" --out "$OUT/sbom-appimage.cdx.json"

# --- упаковка ------------------------------------------------------------------
find "$APPDIR" -print0 | xargs -0 -r touch -h --date="@$SOURCE_DATE_EPOCH"
IMAGE=$OUT/Astra_Voice-$VERSION-x86_64.AppImage
rm -f "$IMAGE"
say 'упаковываю AppImage'
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" --no-appstream \
    --runtime-file "$RUNTIME" -n "$APPDIR" "$IMAGE" >"$WORK/appimagetool.log" 2>&1 || {
    tail -n 40 "$WORK/appimagetool.log" >&2
    die 'appimagetool завершился с ошибкой'
}
# Распаковка appimagetool (extract-and-run) runtime сам не удаляет — она в нашем TMPDIR.
rm -rf "$TMPDIR"/appimage_extracted_*

# --- гейты образа --------------------------------------------------------------
say 'гейт: образ AppImage type 2'
python3 - "$IMAGE" <<'PY'
import sys
from pathlib import Path

image = Path(sys.argv[1])
with image.open("rb") as fh:
    head = fh.read(16)
size = image.stat().st_size
if head[:4] != b"\x7fELF" or head[8:11] != b"AI\x02":
    sys.exit(f"ОШИБКА: {image.name}: нет подписи AppImage type 2 (AI\\x02 по смещению 8)")
if size <= 50 * 1024 * 1024:
    sys.exit(f"ОШИБКА: {image.name}: подозрительно мал ({size} байт ≤ 50 МиБ)")
PY
say 'гейт: мусор распаковок'
leftovers=$(find "$WORK/tmp" -mindepth 1 -maxdepth 1 -print)
[ -z "$leftovers" ] || die "в рабочем TMPDIR остались файлы: $leftovers"
host_leftovers=$(find "$HOST_TMP" -maxdepth 1 -name 'appimage_extracted_*' -newer "$STAMP" -print 2>/dev/null || true)
[ -z "$host_leftovers" ] || die "в $HOST_TMP появились распаковки AppImage: $host_leftovers"

size=$(stat -c %s "$IMAGE")
digest=$(sha256sum "$IMAGE" | cut -d' ' -f1)
say "AppImage: $IMAGE"
say "  размер $size байт ($(du -h "$IMAGE" | cut -f1)), sha256 $digest"
say "  AppDir $(du -sh "$APPDIR" | cut -f1), BUILD_ID $BUILD_ID, $(($(date +%s) - START)) с"
if [ -n "$TODO" ]; then
    printf 'ПРЕДУПРЕЖДЕНИЕ: образ собран без закреплённых колёс (%s) — НЕ для выпуска\n' \
        "$(echo "$TODO" | tr '\n' ' ')" >&2
fi
