#!/usr/bin/env bash
# Смоук готового .AppImage в изоляции (arch/appimage.md §9.1–§9.2; T-170, T-171).
#
#   packaging/appimage/smoke.sh dist/Astra_Voice-<версия>-x86_64.AppImage
#   packaging/appimage/smoke.sh --no-gui <файл>   # только служебные флаги, без установки и окна
#
# Всё — во временном HOME (TMPDIR, XDG_RUNTIME_DIR внутри него), без D-Bus сессии и
# звука; FUSE не нужен (extract-and-run, как в контейнере CI). Настоящий профиль не трогается.
# Этапы:
#   1. `--version` из файла — без установки, stderr пуст;
#   2. `--selfinstall-status` — режим В (распаковка), KEY = <версия>-<BUILD_ID>, app/ не создан;
#      2b. пути распаковки: с «:» — понятный отказ; с кириллицей при LC_ALL=C — программа
#      (переносной режим, offscreen) поднимает IPC: Qt не зацикливается (находка 29.09);
#   3. (GUI, под xvfb-run) первый запуск файла: самоустановка в app/<KEY>, current → KEY,
#      запись меню ведёт в app/current, временная распаковка удалена; IPC-сокет
#      появился; `--show` из установленной копии; выход;
#   4. установленная копия: режим А; `--uninstall` — когда появится в программе (шаг 4 §12);
#   5. в TMPDIR не осталось appimage_extracted_*.
# От root (контейнер CI) смоук перезапускает себя под nobody (setpriv): программа в
# бандле от root не работает (T1 MJ-3). Без packaging/appimage/ENABLED — пропуск.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
SELF=$ROOT/packaging/appimage/smoke.sh

say() { printf '==> smoke: %s\n' "$*"; }
die() { printf 'ОШИБКА smoke: %s\n' "$*" >&2; exit 1; }

GUI=1
STAGE=main
IMAGE=''
for arg in "$@"; do
    case $arg in
        --no-gui) GUI=0 ;;
        --inner) STAGE=inner ;;
        --gui-stage) STAGE=gui ;;
        -h | --help) sed -n '2,19p' "${BASH_SOURCE[0]}"; exit 0 ;;
        -*) echo "неизвестный аргумент: $arg" >&2; exit 2 ;;
        *) [ -z "$IMAGE" ] || { echo 'нужен ровно один файл' >&2; exit 2; }; IMAGE=$arg ;;
    esac
done

# --- этап GUI: внутри xvfb-run, окружение уже изолировано ----------------------
if [ "$STAGE" = gui ]; then
    DATA=$HOME/.local/share/astra-voice
    SOCKET=$XDG_RUNTIME_DIR/astra-voice/ipc
    LOG=$HOME/app.log
    APPIMAGE_EXTRACT_AND_RUN=1 setsid "$IMAGE" --hidden >"$LOG" 2>&1 &
    pid=$!
    stop_app() {
        kill -TERM -- "-$pid" 2>/dev/null || true
        for _ in $(seq 1 100); do
            kill -0 "$pid" 2>/dev/null || return 0
            sleep 0.1
        done
        kill -KILL -- "-$pid" 2>/dev/null || true
    }
    trap stop_app EXIT
    for _ in $(seq 1 600); do
        [ -S "$SOCKET" ] && break
        kill -0 "$pid" 2>/dev/null || { cat "$LOG" >&2; die 'программа завершилась до готовности IPC'; }
        sleep 0.1
    done
    [ -S "$SOCKET" ] || { cat "$LOG" >&2; die 'IPC-сокет не появился за 60 с'; }
    say 'IPC-сокет готов'
    "$DATA/app/current/AppRun" --show || die '--show из установленной копии завершился с ошибкой'
    say '--show передан работающей копии'
    stop_app
    trap - EXIT
    exit 0
fi

# --- внутренний этап: уже не root, во временном HOME --------------------------
if [ "$STAGE" = inner ]; then
    [ "$(id -u)" != 0 ] || die 'внутренний этап не должен идти от root'
    name=$(basename "$IMAGE")
    VERSION=${name#Astra_Voice-}
    VERSION=${VERSION%-x86_64.AppImage}
    DATA=$HOME/.local/share/astra-voice
    extracted() { find "$TMPDIR" -maxdepth 1 -name 'appimage_extracted_*' -print; }

    say "1/5 --version без установки"
    out=$(APPIMAGE_EXTRACT_AND_RUN=1 "$IMAGE" --version 2>"$HOME/err") ||
        { cat "$HOME/err" >&2; die '--version завершился с ошибкой'; }
    [ "$out" = "astra-voice $VERSION" ] || die "неверный --version: $out"
    [ ! -s "$HOME/err" ] || { cat "$HOME/err" >&2; die '--version пишет в stderr'; }

    say "2/5 --selfinstall-status: режим В, ничего не установлено"
    status=$(APPIMAGE_EXTRACT_AND_RUN=1 "$IMAGE" --selfinstall-status)
    printf '%s\n' "$status" | grep -qx 'MODE=В' || die "ожидался режим В: $status"
    KEY=$(printf '%s\n' "$status" | sed -n 's/^KEY=//p')
    printf '%s\n' "$KEY" | grep -Eqx "${VERSION//./\\.}-[0-9a-f]{12}" || die "неверный KEY: $KEY"
    [ ! -e "$DATA/app" ] || die 'служебный флаг создал каталог установки'
    # Служебные флаги работают из распаковки и не удаляют её (установки нет) — уборка
    # здесь, в собственном TMPDIR смоука.
    rm -rf "$TMPDIR"/appimage_extracted_*

    say "2b/5 путь распаковки с «:» — отказ; с кириллицей при LC_ALL=C — запуск"
    colon="$TMPDIR/путь:с двоеточием"
    mkdir -p "$colon"
    if TMPDIR=$colon APPIMAGE_EXTRACT_AND_RUN=1 "$IMAGE" --version >/dev/null 2>"$HOME/err"; then
        die 'путь распаковки с «:» не отвергнут'
    fi
    grep -q 'двоеточие «:»' "$HOME/err" || { cat "$HOME/err" >&2; die 'нет понятной ошибки про «:»'; }
    rm -rf "$colon"
    cyr="$TMPDIR/Мои программы"
    mkdir -p "$cyr"
    SOCKET=$XDG_RUNTIME_DIR/astra-voice/ipc
    TMPDIR=$cyr LC_ALL=C ASTRA_VOICE_PORTABLE=1 APPIMAGE_EXTRACT_AND_RUN=1 \
        setsid "$IMAGE" --hidden >"$HOME/cyr.log" 2>&1 &
    pid=$!
    ready=0
    for _ in $(seq 1 600); do
        [ -S "$SOCKET" ] && { ready=1; break; }
        kill -0 "$pid" 2>/dev/null || break
        sleep 0.1
    done
    kill -TERM -- "-$pid" 2>/dev/null || true
    for _ in $(seq 1 100); do kill -0 "$pid" 2>/dev/null || break; sleep 0.1; done
    kill -KILL -- "-$pid" 2>/dev/null || true
    wait "$pid" 2>/dev/null || true
    [ "$ready" = 1 ] || { cat "$HOME/cyr.log" >&2; die 'кириллица в пути при LC_ALL=C: программа не поднялась за 60 с'; }
    rm -rf "$cyr" "$SOCKET"

    if [ "$GUI" = 0 ]; then
        say '3–4/5 пропущены (--no-gui)'
    else
        command -v xvfb-run >/dev/null 2>&1 || die 'нет xvfb-run для GUI-смоука'
        say "3/5 первый запуск файла под xvfb: самоустановка, IPC, --show"
        xvfb-run -a -s '-screen 0 1280x800x24 -nolisten tcp' \
            env QT_QPA_PLATFORM=xcb "$SELF" --gui-stage "$IMAGE" ||
            die 'GUI-смоук не прошёл'
        [ -f "$DATA/app/$KEY/.installed-ok" ] || die "нет установленной копии app/$KEY"
        [ "$(readlink "$DATA/app/current")" = "$KEY" ] || die 'app/current не указывает на новую копию'
        desktop=$HOME/.local/share/applications/astra-voice.desktop
        [ -f "$desktop" ] || die 'нет записи меню'
        grep -q "^Exec=.*$DATA/app/current/AppRun" "$desktop" || die 'запись меню ведёт не в app/current'

        say "4/5 установленная копия: режим А"
        status=$("$DATA/app/current/AppRun" --selfinstall-status)
        printf '%s\n' "$status" | grep -qx 'MODE=А' || die "ожидался режим А: $status"
        if "$DATA/app/current/AppRun" --help 2>/dev/null | grep -q -- '--uninstall'; then
            "$DATA/app/current/AppRun" --uninstall || die '--uninstall завершился с ошибкой'
            [ ! -e "$desktop" ] || die '--uninstall оставил запись меню'
        else
            say '   --uninstall в программе ещё нет — этап пропущен'
        fi
    fi

    say "5/5 мусор распаковок"
    left=$(extracted)
    [ -z "$left" ] || die "в TMPDIR остались распаковки: $left"
    say 'OK'
    exit 0
fi

# --- внешний этап ---------------------------------------------------------------
if [ ! -f "$ROOT/packaging/appimage/ENABLED" ]; then
    say 'AppImage выключен (нет packaging/appimage/ENABLED) — смоук пропущен'
    exit 0
fi
[ -n "$IMAGE" ] || die 'укажите файл .AppImage'
[ -f "$IMAGE" ] || die "нет файла $IMAGE"
case $(basename "$IMAGE") in
    Astra_Voice-*-x86_64.AppImage) ;;
    *) die "имя файла не по шаблону Astra_Voice-<версия>-x86_64.AppImage: $IMAGE" ;;
esac

SMOKE=$(mktemp -d "${TMPDIR:-/tmp}/astra-voice-smoke.XXXXXX")
cleanup() { rm -rf "$SMOKE"; }
trap cleanup EXIT
trap 'exit 1' HUP INT TERM
home=$SMOKE/home
mkdir -p "$SMOKE/img" "$home/tmp" "$home/run"
cp "$IMAGE" "$SMOKE/img/"
copy=$SMOKE/img/$(basename "$IMAGE")
chmod 755 "$SMOKE" "$SMOKE/img" "$copy"
chmod 700 "$home" "$home/tmp" "$home/run"

runner=()
if [ "$(id -u)" = 0 ]; then
    command -v setpriv >/dev/null 2>&1 || die 'нет setpriv (util-linux) для запуска не от root'
    chown -R 65534:65534 "$home"
    runner=(setpriv --reuid=65534 --regid=65534 --clear-groups --)
fi
inner_args=(--inner "$copy")
[ "$GUI" = 1 ] || inner_args=(--no-gui "${inner_args[@]}")
env -u DISPLAY -u WAYLAND_DISPLAY -u QT_ACCESSIBILITY -u AT_SPI_BUS_ADDRESS \
    -u APPIMAGE -u APPDIR -u ARGV0 -u OWD -u APPIMAGE_EXTRACT_AND_RUN \
    -u PYTHONPATH -u PYTHONHOME -u ASTRA_VOICE_RESOURCES -u ASTRA_VOICE_APPIMAGE_DIR \
    -u XAUTHORITY -u XDG_DATA_HOME -u XDG_CONFIG_HOME -u XDG_CACHE_HOME -u XDG_STATE_HOME \
    DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent PULSE_SERVER=unix:/nonexistent \
    QT_QPA_PLATFORM=offscreen HOME="$home" TMPDIR="$home/tmp" XDG_RUNTIME_DIR="$home/run" \
    "${runner[@]}" bash "$SELF" "${inner_args[@]}"
