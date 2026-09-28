#!/usr/bin/env bash
# Замеры пробного AppImage Astra Voice без GUI (спайк 2026-09-28).
#
#   spikes/appimage/measure.sh [путь к .AppImage]
#
# Что делает: размер и состав, ELF-аудит AppDir, ldd ключевых .so, время
# `--version` (холодное и тёплое) в четырёх режимах запуска, самоустановка во
# ВРЕМЕННЫЙ HOME. GUI не запускается: только `--version` и служебный
# `--selfinstall-status`, всё в изолированном окружении (без DISPLAY,
# D-Bus сессии и PulseAudio). Настоящий профиль пользователя не трогается.
#
# «Холодный» старт без root: страницы файлов выкидываются из page cache
# через posix_fadvise(DONTNEED) — это работает для чистых страниц файлов,
# которые может читать пользователь; кэш метаданных (dentry/inode) остаётся.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
WORK=${ASTRA_VOICE_APPIMAGE_WORK:-$HOME/.cache/astra-voice-dev/appimage}
APPDIR=$WORK/AppDir
IMAGE=${1:-$(ls -1 "$WORK"/out/Astra_Voice-*-x86_64.AppImage | tail -1)}
RUNS=${RUNS:-5}

[ -f "$IMAGE" ] || { echo "нет $IMAGE" >&2; exit 1; }
[ -x "$APPDIR/AppRun" ] || { echo "нет $APPDIR/AppRun — сначала build.sh" >&2; exit 1; }

SCRATCH=$(mktemp -d "$WORK/measure.XXXXXX")
scratch_mounts() { awk -v s="$SCRATCH/" 'index($5, s) == 1 {print $5}' /proc/self/mountinfo; }
cleanup() {
    # FUSE-монтирование runtime снимается асинхронно; в одном прогоне из
    # нескольких оно висело на момент уборки. Ждём до 3 с, потом снимаем сами.
    local i m
    for i in $(seq 1 30); do
        [ -z "$(scratch_mounts)" ] && break
        sleep 0.1
    done
    for m in $(scratch_mounts); do
        echo "уборка: монтирование $m не снялось само, fusermount -u" >&2
        fusermount -u "$m" || fusermount -uz "$m" || true
    done
    # Самоустановка кладёт копии в $SCRATCH/home — всё временное, удаляем.
    rm -rf "$SCRATCH"
}
trap cleanup EXIT

section() { printf '\n### %s\n' "$*"; }

new_home() {
    local h
    h=$(mktemp -d "$SCRATCH/home.XXXXXX")
    mkdir -m 700 "$h/run" "$h/tmp"
    printf '%s' "$h"
}

# Изолированный запуск: без дисплея, без шины сессии, без звука, временный HOME.
iso() {
    local h=$1
    shift
    env -u DISPLAY -u WAYLAND_DISPLAY -u QT_ACCESSIBILITY -u AT_SPI_BUS_ADDRESS \
        -u APPIMAGE -u APPDIR -u ARGV0 -u OWD -u APPIMAGE_EXTRACT_AND_RUN \
        DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent \
        QT_QPA_PLATFORM=offscreen PULSE_SERVER=unix:/nonexistent \
        HOME="$h" XDG_DATA_HOME= XDG_CONFIG_HOME= XDG_CACHE_HOME= \
        XDG_RUNTIME_DIR="$h/run" TMPDIR="$h/tmp" "$@"
}

# Выкинуть страницы файлов из page cache (без root).
evict() {
    python3 - "$@" <<'EOF'
import os, sys
n = 0
for top in sys.argv[1:]:
    paths = [top] if os.path.isfile(top) else (
        os.path.join(d, f) for d, _, fs in os.walk(top) for f in fs)
    for p in paths:
        if os.path.islink(p) or not os.path.isfile(p):
            continue
        try:
            fd = os.open(p, os.O_RDONLY)
        except OSError:
            continue
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED)
            n += 1
        finally:
            os.close(fd)
EOF
}

ms_now() { date +%s%N; }

# time_cmd <метка> <команда…>: одна попытка, печатает мс и первую строку вывода.
time_cmd() {
    local label=$1 s e out rc
    shift
    s=$(ms_now)
    set +e
    out=$("$@" 2>&1)
    rc=$?
    set -e
    e=$(ms_now)
    printf '%-44s %6d мс  rc=%d  %s\n' "$label" $(((e - s) / 1000000)) "$rc" "$(printf '%s' "$out" | tail -1)"
}

fuse_mounts() { grep -c ' fuse\.' /proc/self/mountinfo | tr -d '\n'; printf ' (из них .mount_: %s)' "$(grep -c '/\.mount_' /proc/self/mountinfo || true)"; }

section "Файл"
printf 'AppImage: %s\n' "$IMAGE"
printf 'Размер: %s байт (%s)\n' "$(stat -c %s "$IMAGE")" "$(du -h "$IMAGE" | cut -f1)"
printf 'sha256: %s\n' "$(sha256sum "$IMAGE" | cut -d' ' -f1)"
printf 'AppDir: %s (du), файлов %s\n' "$(du -sh "$APPDIR" | cut -f1)" "$(find "$APPDIR" -type f | wc -l)"
cat "$APPDIR/.astra-voice-build"

section "Состав AppDir по крупным частям (du -sh)"
SITE=$APPDIR/opt/python3.11/lib/python3.11/site-packages
du -sh "$SITE/PyQt5/Qt5/lib" "$SITE/PyQt5/Qt5/qml" "$SITE/PyQt5/Qt5/plugins" "$SITE/PyQt5" \
    "$SITE/onnxruntime" "$SITE/numpy" "$SITE/numpy.libs" "$SITE/onnx_asr" \
    "$APPDIR/opt/python3.11/lib/python3.11" "$APPDIR/usr/lib" \
    "$APPDIR/usr/lib/astra-voice" "$APPDIR/usr/share/astra-voice" 2>/dev/null | sed "s|$APPDIR/||"
printf 'Qt lib: %s\n' "$(cd "$SITE/PyQt5/Qt5/lib" && ls | tr '\n' ' ')"
printf 'Qt plugins: %s\n' "$(cd "$SITE/PyQt5/Qt5/plugins" && find . -name '*.so' | sort | tr '\n' ' ')"
printf 'QML: %s\n' "$(cd "$SITE/PyQt5/Qt5/qml" && find . -name qmldir -printf '%h\n' | sort | tr '\n' ' ')"

section "ELF-аудит AppDir (tools/elf-audit, без --strict: ожидание 3 — для .deb)"
set +e
python3 "$ROOT/tools/elf-audit" --expect 0 "$APPDIR" >"$SCRATCH/elf-audit.txt" 2>&1
grep -E '^ELF найдено' "$SCRATCH/elf-audit.txt"
printf 'max GLIBC по всем ELF: %s\n' "$(grep -oE 'GLIBC_[0-9.]+' "$SCRATCH/elf-audit.txt" | sort -V | tail -1)"
printf 'ELF с __isoc23_: %s\n' "$(grep -c 'isoc23' "$SCRATCH/elf-audit.txt")"
set -e

section "ldd ключевых .so (что берётся у хоста, чего нет)"
LDD_SET=(
    "$SITE/PyQt5/Qt5/lib/libQt5Quick.so.5"
    "$SITE/PyQt5/Qt5/plugins/platforms/libqxcb.so"
    "$SITE/PyQt5/Qt5/plugins/xcbglintegrations/libqxcb-glx-integration.so"
    "$SITE/PyQt5/QtDBus.abi3.so"
    "$(ls "$SITE"/onnxruntime/capi/onnxruntime_pybind11_state*.so)"
    "$APPDIR/opt/python3.11/bin/python3.11"
)
for so in "${LDD_SET[@]}"; do
    printf -- '- %s\n' "${so#"$APPDIR"/}"
    ldd "$so" | awk -v appdir="$APPDIR" '
        /not found/ { print "    НЕ НАЙДЕНО: " $1; next }
        $3 ~ "^" appdir { inb++; next }
        $3 ~ /^\// { print "    хост: " $1 " -> " $3; next }'
    printf '    (из бандла: %s)\n' "$(ldd "$so" | grep -c "$APPDIR" || true)"
done
section "Уникальные хостовые библиотеки по всем ELF AppDir"
while IFS= read -r -d '' f; do
    readelf -h "$f" >/dev/null 2>&1 || continue
    ldd "$f" 2>/dev/null | awk -v appdir="$APPDIR" '$3 ~ /^\// && $3 !~ "^" appdir {print $1} /not found/ {print "НЕ НАЙДЕНО " $1}'
done < <(find "$APPDIR" -type f \( -name '*.so' -o -name '*.so.*' -o -name 'python3.11' \) -print0) |
    sort | uniq -c | sort -rn

section "Утечки путей хоста в Python"
H=$(new_home)
iso "$H" "$APPDIR/opt/python3.11/bin/python3.11" -I -c '
import sys, sysconfig, ssl
print("prefix:", sys.prefix)
print("sys.path:", sys.path)
print("purelib:", sysconfig.get_paths()["purelib"])
print("openssl:", ssl.OPENSSL_VERSION, "| verify paths:", ssl.get_default_verify_paths())
bad = [p for p in sys.path if p.startswith(("/usr/lib/python3", "/usr/local", "/usr/lib/python3.11"))]
print("утечки /usr/lib/python3 в sys.path:", bad or "нет")
'
printf 'SSL_CERT_FILE, который выставит AppRun: '
iso "$H" sh -c '. /dev/null; if [ -r /etc/ssl/certs/ca-certificates.crt ]; then echo /etc/ssl/certs/ca-certificates.crt; else echo certifi; fi'
printf 'Строки /usr/lib/python3 в файлах AppDir (кроме .py/.pyi/.txt): '
grep -rlI --exclude='*.py' --exclude='*.pyi' --exclude='*.txt' '/usr/lib/python3' "$APPDIR" 2>/dev/null | sed "s|$APPDIR/||" | head -5 | tr '\n' ' ' || true
echo

section "Установленный .deb для сравнения"
if [ -x /usr/bin/astra-voice ]; then
    H=$(new_home)
    for i in $(seq 1 "$RUNS"); do time_cmd ".deb /usr/bin/astra-voice --version (тёпл.)" iso "$H" /usr/bin/astra-voice --version; done
fi

section "Режим Г: распакованный AppDir, AppRun --version"
H=$(new_home)
evict "$APPDIR"
time_cmd "AppDir холодный (fadvise DONTNEED)" iso "$H" "$APPDIR/AppRun" --version
for i in $(seq 1 "$RUNS"); do time_cmd "AppDir тёплый" iso "$H" "$APPDIR/AppRun" --version; done
iso "$H" "$APPDIR/AppRun" --selfinstall-status | sed 's/^/    /'

section "Режим Б: обычный запуск через FUSE (на этой машине enable_exec_on_fuse=N)"
printf 'FUSE-монтирований до: %s\n' "$(fuse_mounts)"
H=$(new_home)
cp "$IMAGE" "$SCRATCH/av.AppImage"
chmod 755 "$SCRATCH/av.AppImage"
evict "$SCRATCH/av.AppImage"
time_cmd "FUSE холодный (файл вне кэша)" iso "$H" "$SCRATCH/av.AppImage" --version
for i in $(seq 1 "$RUNS"); do time_cmd "FUSE тёплый" iso "$H" "$SCRATCH/av.AppImage" --version; done
for i in $(seq 1 3); do time_cmd "FUSE: монтирование + AppRun без Python" iso "$H" "$SCRATCH/av.AppImage" --selfinstall-status; done
echo "    статус изнутри монтирования:"
iso "$H" "$SCRATCH/av.AppImage" --selfinstall-status | sed 's/^/    /'
printf 'Самоустановка в режиме Б без флага (ожидаем: не ставится): %s\n' \
    "$(ls "$H/.local/share/astra-voice/app" 2>/dev/null | tr '\n' ' ' || true)"
printf 'FUSE-монтирований после: %s\n' "$(fuse_mounts)"

section "Режим Б + ASTRA_VOICE_SELFINSTALL=1: самоустановка из FUSE"
H=$(new_home)
time_cmd "FUSE + самоустановка (1-й запуск)" iso "$H" env ASTRA_VOICE_SELFINSTALL=1 "$SCRATCH/av.AppImage" --version
ls -la "$H/.local/share/astra-voice/app" | sed 's/^/    /'
for i in $(seq 1 "$RUNS"); do time_cmd "FUSE → уже установленная копия" iso "$H" "$SCRATCH/av.AppImage" --version; done
INST=$(ls -d "$H"/.local/share/astra-voice/app/*/ | head -1)
iso "$H" "$INST/AppRun" --selfinstall-status | sed 's/^/    /'
evict "$INST"
time_cmd "установленная копия напрямую, холодный" iso "$H" "$INST/AppRun" --version
for i in $(seq 1 "$RUNS"); do time_cmd "установленная копия напрямую, тёплый" iso "$H" "$INST/AppRun" --version; done
printf 'Размер установленной копии: %s\n' "$(du -sh "$INST" | cut -f1)"

section "Режим В: --appimage-extract-and-run (запасной путь при запрете FUSE-исполнения)"
H=$(new_home)
evict "$SCRATCH/av.AppImage"
time_cmd "extract-and-run, 1-й запуск (холодный)" iso "$H" "$SCRATCH/av.AppImage" --appimage-extract-and-run --version
printf '    остаток во временном каталоге runtime: %s\n' "$(ls "$H/tmp" | tr '\n' ' ')"
printf '    установлено: %s\n' "$(ls "$H/.local/share/astra-voice/app" | tr '\n' ' ')"
for i in $(seq 1 "$RUNS"); do time_cmd "extract-and-run, повторный" iso "$H" "$SCRATCH/av.AppImage" --appimage-extract-and-run --version; done
H2=$(new_home)
time_cmd "APPIMAGE_EXTRACT_AND_RUN=1, 1-й запуск" iso "$H2" env APPIMAGE_EXTRACT_AND_RUN=1 "$SCRATCH/av.AppImage" --version
printf '    статус через extract-and-run: '
iso "$H2" "$SCRATCH/av.AppImage" --appimage-extract-and-run --selfinstall-status | tr '\n' ' '
echo

section "Итог по монтированиям и процессам"
printf 'FUSE-монтирований в конце: %s\n' "$(fuse_mounts)"
