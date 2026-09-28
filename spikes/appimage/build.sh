#!/usr/bin/env bash
# Пробный AppImage Astra Voice (спайк 2026-09-28, см. SPIKE-2026-09-28.md).
#
#   spikes/appimage/build.sh
#
# Сети не нужно: входы заранее скачаны и закреплены в spikes/appimage/lock.txt
# (инструменты — по sha256, колёса — pip --require-hashes). Собирает на хосте,
# без root и без контейнера: $WORK/AppDir → $WORK/out/Astra_Voice-<версия>-x86_64.AppImage.
# Рабочий каталог: ASTRA_VOICE_APPIMAGE_WORK (по умолчанию ~/.cache/astra-voice-dev/appimage).
# Черновой код спайка (первая версия — Codex/GPT-6 Sol, доводка — Claude).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
WORK=${ASTRA_VOICE_APPIMAGE_WORK:-$HOME/.cache/astra-voice-dev/appimage}
DOWNLOADS=$WORK/downloads
WHEELS=$WORK/wheels
ORT_WHEELS=$HOME/.cache/astra-voice-dev/wheels
BUILD=$WORK/build
APPDIR=$WORK/AppDir
OUT=$WORK/out
LOCK=$ROOT/spikes/appimage/lock.txt
VERSION=$(sed -n '1s/.*(\([^)]*\)).*/\1/p' "$ROOT/packaging/debian/changelog")
CHANGELOG_DATE=$(sed -n 's/^ -- .*>  //p' "$ROOT/packaging/debian/changelog" | head -1)
SOURCE_DATE_EPOCH=${SOURCE_DATE_EPOCH:-$(date -u -d "$CHANGELOG_DATE" +%s)}
export SOURCE_DATE_EPOCH

say() { printf '==> %s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }
[ -n "$VERSION" ] || die 'не найдена версия в changelog'
[ -d "$DOWNLOADS" ] && [ -d "$WHEELS" ] && [ -d "$ORT_WHEELS" ] || die 'нет входных файлов в кэше'

say 'сверяю sha256 инструментов из lock.txt'
tool_count=0
while read -r marker kind name hash size url; do
    [ "$marker" = '#' ] && [ "$kind" = 'tool:' ] || continue
    file=$DOWNLOADS/$name
    [ -f "$file" ] || die "нет инструмента $file"
    actual=$(sha256sum "$file" | cut -d' ' -f1)
    [ "$actual" = "$hash" ] || die "sha256 не совпал: $name"
    actual_size=$(stat -c %s "$file")
    [ "$actual_size" = "$size" ] || die "размер не совпал: $name"
    tool_count=$((tool_count + 1))
done < "$LOCK"
[ "$tool_count" -eq 4 ] || die "ожидались 4 инструмента, найдено $tool_count"
PY_IMAGE=$DOWNLOADS/python3.11.16-cp311-cp311-manylinux_2_28_x86_64.AppImage
TOOL=$DOWNLOADS/appimagetool-x86_64.AppImage
RUNTIME=$DOWNLOADS/runtime-x86_64
chmod 755 "$PY_IMAGE" "$TOOL"

rm -rf "$APPDIR" "$BUILD/extract"
mkdir -p "$APPDIR" "$BUILD/extract" "$OUT"
say 'распаковываю Python AppImage'
(cd "$BUILD/extract" && "$PY_IMAGE" --appimage-extract >/dev/null)
mv "$BUILD/extract/squashfs-root/opt" "$APPDIR/opt"
mv "$BUILD/extract/squashfs-root/usr" "$APPDIR/usr"
rm -rf "$BUILD/extract"
PY=$APPDIR/opt/python3.11/bin/python3.11
SITE=$APPDIR/opt/python3.11/lib/python3.11/site-packages
STDLIB=$APPDIR/opt/python3.11/lib/python3.11
mkdir -p "$WORK/home-build" "$WORK/home-build/runtime"
chmod 700 "$WORK/home-build" "$WORK/home-build/runtime"

isolated() {
    env -u DISPLAY -u WAYLAND_DISPLAY -u QT_ACCESSIBILITY -u AT_SPI_BUS_ADDRESS \
        DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent \
        QT_QPA_PLATFORM=offscreen PULSE_SERVER=unix:/nonexistent \
        HOME="$WORK/home-build" XDG_DATA_HOME= XDG_CONFIG_HOME= XDG_CACHE_HOME= \
        XDG_RUNTIME_DIR="$WORK/home-build/runtime" "$@"
}

say 'ставлю колёса бандловым Python по lock.txt'
isolated "$PY" -I -m pip install --no-deps --no-index --require-hashes \
    --find-links "$WHEELS" --find-links "$ORT_WHEELS" \
    --no-cache-dir --no-compile --target "$SITE" -r "$LOCK"

for vendor_patch in "$ROOT"/vendor/patches/*.patch; do
    [ -f "$vendor_patch" ] || continue
    (cd "$SITE" && patch -p1 --dry-run --forward --batch < "$vendor_patch" >/dev/null) ||
        die "патч $(basename "$vendor_patch") не подходит к onnx_asr"
    (cd "$SITE" && patch -p1 --forward --no-backup-if-mismatch --batch < "$vendor_patch")
done

say 'удаляю ненужные модули Python и Qt'
rm -rf "$SITE/pip" "$SITE"/pip-*.dist-info "$SITE/build" "$SITE"/build-*.dist-info \
    "$SITE/pyproject_hooks" "$SITE"/pyproject_hooks-*.dist-info "$SITE/sitecustomize.py" \
    "$SITE/bin" "$STDLIB/ensurepip" "$STDLIB/idlelib" "$STDLIB/tkinter" \
    "$STDLIB/turtledemo" "$STDLIB/test" "$STDLIB/lib2to3/tests" \
    "$APPDIR/opt/python3.11/include" "$APPDIR/usr/share/tcltk" "$APPDIR/usr/bin"
find "$APPDIR" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$APPDIR" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete

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
rm -rf "$QML_KEEP"
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

# Начальный набор библиотек дополняется рекурсивно по DT_NEEDED всех
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

# В базовом AppImage Tcl/Tk нужен только удалённому tkinter. Его C-модуль
# _tkinter лежит в lib-dynload и сам держит libtk/libtcl — убираем и его.
# Затем проверяем, что остальные ELF не ссылаются на удаляемые зависимости.
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
mv "$LIB/astra_voice/bootstrap.py" "$LIB/bootstrap.py"
cp -a "$ROOT/qml" "$SHARE/qml"
cp -a "$ROOT/data" "$SHARE/data"
rm -rf "$SHARE/data/icons" "$SHARE/data/astra-voice.desktop" \
    "$SHARE/data/keys/README.md" "$SHARE/data/test" \
    "$SHARE/data/vad/README.md" "$SHARE/data/catalog-source.json" \
    "$SHARE/data/catalog-hf-cache.json"
# Значки темы — как в packaging/debian/rules (/usr/share/icons/hicolor):
# paths.icon_theme_dir() = <ресурсы>/../icons/hicolor, отсюда их берёт трей.
# У python-appimage свои usr/share/{icons,applications,metainfo} (значок Python):
# убираем их, иначе cp -a положит наши значки в hicolor/hicolor.
rm -rf "$APPDIR/usr/share/icons" "$APPDIR/usr/share/applications" "$APPDIR/usr/share/metainfo"
mkdir -p "$APPDIR/usr/share/icons/hicolor"
cp -a "$ROOT/data/icons/hicolor/." "$APPDIR/usr/share/icons/hicolor/"
find "$LIB" -type d -name __pycache__ -prune -exec rm -rf {} +
find "$LIB" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
printf '# Сгенерировано spikes/appimage/build.sh из packaging/debian/changelog.\n__version__ = "%s"\n' "$VERSION" > "$LIB/astra_voice/_version.py"

install -m 755 "$ROOT/spikes/appimage/AppRun" "$APPDIR/AppRun"
install -m 644 "$ROOT/spikes/appimage/astra-voice.desktop" "$APPDIR/astra-voice.desktop"
install -m 644 "$ROOT/data/icons/hicolor/256x256/apps/astravoice.png" "$APPDIR/astravoice.png"
ln -s astravoice.png "$APPDIR/.DirIcon"
BUILD_ID=$(cd "$APPDIR" && find . -type f ! -name .astra-voice-build -print0 | sort -z | \
    xargs -0 sha256sum | awk '{print $2 " " $1}' | sha256sum | cut -c1-12)
printf 'VERSION=%s\nBUILD_ID=%s\n' "$VERSION" "$BUILD_ID" > "$APPDIR/.astra-voice-build"

say 'проверяю AppDir в изолированном окружении'
isolated "$PY" -I -c 'import PyQt5.QtCore, PyQt5.QtGui, PyQt5.QtWidgets, PyQt5.QtQml, PyQt5.QtQuick, PyQt5.QtDBus, PyQt5.QtNetwork, numpy, Xlib, requests, onnxruntime, onnx_asr; print("Импорты: OK")'
isolated "$PY" -I -c 'import sys; print("sys.path:", *sys.path, sep="\n  "); assert not any(p.startswith(("/usr/lib/python3", "/usr/local")) for p in sys.path)'
isolated "$PY" -I "$ROOT/spikes/appimage/check_qml.py" "$SHARE/qml"
isolated env ASTRA_VOICE_RESOURCES="$SHARE" "$PY" -I "$ROOT/spikes/appimage/check_resources.py" "$LIB"
version_output=$(isolated "$APPDIR/AppRun" --version)
[ "$version_output" = "astra-voice $VERSION" ] || die "неверный --version: $version_output"
say "AppRun --version: $version_output"

find "$APPDIR" -print0 | xargs -0 -r touch -h --date="@$SOURCE_DATE_EPOCH"
IMAGE=$OUT/Astra_Voice-$VERSION-x86_64.AppImage
say 'упаковываю AppImage'
ARCH=x86_64 APPIMAGE_EXTRACT_AND_RUN=1 "$TOOL" --no-appstream \
    --runtime-file "$RUNTIME" -n "$APPDIR" "$IMAGE"

elf_count=0
while IFS= read -r -d '' file; do
    if readelf -h "$file" >/dev/null 2>&1; then elf_count=$((elf_count + 1)); fi
done < <(find "$APPDIR" -type f -print0)
say "AppImage: $IMAGE ($(stat -c %s "$IMAGE") байт)"
say "AppDir: $(du -sh "$APPDIR" | cut -f1)"
say "ELF в AppDir: $elf_count"
