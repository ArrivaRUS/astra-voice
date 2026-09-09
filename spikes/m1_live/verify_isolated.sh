#!/bin/bash
# Проверка дефектов 1,2,3,5,8 в изолированном KWin (виртуальный вывод + Xwayland).
# Дисплей :0 и настройки заказчика не затрагиваются: сеанс свой, XDG_CONFIG_HOME
# свой, `kdeglobals` — копия пользовательского внутри песочницы.
#
#   REPO=$PWD OUT=$PWD/spikes/m1_live/out dbus-run-session -- \
#     kwin_wayland --virtual --width 1280 --height 900 --xwayland --socket=av-m1a \
#     --exit-with-session=spikes/m1_live/verify_isolated.sh
set -u

REPO="${REPO:?не задан REPO}"
OUT="${OUT:?не задан OUT}"
TMPD="$OUT/iso-home"; rm -rf "$TMPD"; mkdir -p "$TMPD"
LOG="$OUT/live_iso_verify.log"
BOOT="$REPO/src/astra_voice/bootstrap.py"
PAT='^/usr/bin/python3 -I .*/src/astra_voice/bootstrap\.py app'

export XDG_RUNTIME_DIR="$TMPD/run"; mkdir -p "$XDG_RUNTIME_DIR"; chmod 700 "$XDG_RUNTIME_DIR"
export XDG_CONFIG_HOME="$TMPD/config"; mkdir -p "$XDG_CONFIG_HOME"
export XDG_DATA_HOME="$TMPD/data"
export XDG_CACHE_HOME="$TMPD/cache"
APPLOG="$XDG_DATA_HOME/astra-voice/logs/astra-voice.log"

exec > >(tee "$LOG") 2>&1
echo "# изолированная проверка M1-A"
echo "дата: $(date -Is)"
echo "DISPLAY=$DISPLAY (свой Xwayland), XDG_RUNTIME_DIR=$XDG_RUNTIME_DIR"

# Тема берётся из песочницы, файл пользователя только копируется (read-only).
cp ~/.config/kdeglobals "$XDG_CONFIG_HOME/kdeglobals"
python3 - "$XDG_CONFIG_HOME/kdeglobals" "$TMPD/kdeglobals.light" <<'PY'
import re, sys, pathlib
src, dst = pathlib.Path(sys.argv[1]), pathlib.Path(sys.argv[2])
t = src.read_text(encoding="utf-8", errors="replace")
t = re.sub(r"ColorScheme=\S+", "ColorScheme=AstraLight", t)
def fix(m):
    b = m.group(0)
    b = re.sub(r"BackgroundNormal=#\w+", "BackgroundNormal=#F7F7F7", b)
    b = re.sub(r"ForegroundNormal=#\w+", "ForegroundNormal=#1A1A1A", b)
    return b
t = re.sub(r"\[Colors:Window\][^\[]*", fix, t)
dst.write_text(t, encoding="utf-8")
PY
cp "$XDG_CONFIG_HOME/kdeglobals" "$TMPD/kdeglobals.dark"
echo "песочница: тёмная схема $(kreadconfig5 --file "$TMPD/kdeglobals.dark" --group General --key ColorScheme), светлая $(kreadconfig5 --file "$TMPD/kdeglobals.light" --group General --key ColorScheme)"
echo

pass=0; fail=0
check() { # check "имя" "ожидание" "факт"
  if [ "$2" = "$3" ]; then echo "  PASS  $1: $3"; pass=$((pass+1));
  else echo "  FAIL  $1: ожидалось [$2], получено [$3]"; fail=$((fail+1)); fi
}

start_app() { # start_app <файл stderr> → печатает "WID PID"
  /usr/bin/python3 -I "$BOOT" app > "$1" 2>&1 &
  local t=0 wid=""
  while [ $t -lt 60 ]; do
    wid=$(xdotool search --class astra-voice 2>/dev/null | head -1)
    [ -n "$wid" ] && break
    sleep 0.25; t=$((t+1))
  done
  echo "$wid $(pgrep -f "$PAT" | head -1)"
}

luma() { # luma <wid> <png> → средняя яркость центра окна
  import -window "$1" "$2" 2>/dev/null
  convert "$2" -crop 400x200+250+200 +repage -colorspace Gray -format '%[fx:int(mean*255)]' info: 2>/dev/null
}

echo "== запуск из репозитория (тёмная схема в песочнице) =="
read -r WID PID <<< "$(start_app "$OUT/live_iso_app.stderr")"
echo "WID=$WID PID=$PID"
[ -z "$WID" ] && { echo "ОКНО НЕ ПОЯВИЛОСЬ:"; cat "$OUT/live_iso_app.stderr"; exit 1; }

echo
echo "== дефект 8: WM_CLASS instance =="
WMCLASS=$(xprop -id "$WID" WM_CLASS | sed 's/.*= //')
echo "  WM_CLASS = $WMCLASS"
check "instance = astra-voice" '"astra-voice", "astra-voice"' "$WMCLASS"
check "находится по --classname" "$WID" "$(xdotool search --classname astra-voice | head -1)"

echo
echo "== дефект 5: settings.json при первом старте =="
SET="$XDG_CONFIG_HOME/astra-voice/settings.json"
check "файл создан" "да" "$([ -f "$SET" ] && echo да || echo нет)"
check "права" "600" "$(stat -c '%a' "$SET" 2>/dev/null || echo нет)"
echo "  ключи: $(python3 -c "import json,sys;print(','.join(sorted(json.load(open(sys.argv[1])))))" "$SET" 2>/dev/null)"

echo
echo "== дефект 1: тема сессии в окне (тёмная) =="
xdotool windowactivate "$WID" 2>/dev/null; sleep 1
L_DARK=$(luma "$WID" "$OUT/live_iso_window_dark.png")
echo "  средняя яркость центра: $L_DARK (0=чёрное, 255=белое)"
echo "  журнал: $(grep -o 'тема сессии:.*' "$APPLOG" | tail -1)"
check "окно тёмное (яркость < 100)" "да" "$([ "${L_DARK:-255}" -lt 100 ] && echo да || echo нет)"

echo
echo "== дефект 3: show разворачивает свёрнутое окно =="
xdotool windowminimize "$WID"; sleep 1
check "окно свёрнуто" "Iconic" "$(xprop -id "$WID" WM_STATE | grep 'window state' | awk '{print $NF}')"
T0=$(date +%s.%N); /usr/bin/python3 -I "$BOOT" app; RC=$?; T1=$(date +%s.%N)
echo "  второй экземпляр: код=$RC, время=$(echo "$T1-$T0" | bc) с"
sleep 1.5
echo "  _NET_WM_USER_TIME = $(xprop -id "$WID" _NET_WM_USER_TIME 2>/dev/null | sed 's/.*= //')"
echo "  _NET_WM_STATE = $(xprop -id "$WID" _NET_WM_STATE | sed 's/.*= //')"
check "код второго экземпляра" "0" "$RC"
check "окно вернулось из Iconic" "Normal" "$(xprop -id "$WID" WM_STATE | grep 'window state' | awk '{print $NF}')"
check "окно активно" "$WID" "$(xdotool getactivewindow 2>/dev/null)"
check "процессов ровно один" "1" "$(pgrep -c -f "$PAT")"

echo
echo "== дефект 2: закрытие окна завершает процесс =="
echo "  WM_PROTOCOLS: $(xprop -id "$WID" WM_PROTOCOLS | sed 's/.*: //')"
# Именно так закрывает пользователь кнопкой «x»: _NET_CLOSE_WINDOW → WM_DELETE_WINDOW.
# `xdotool windowclose` вместо этого уничтожает окно, и приложение о закрытии не узнаёт.
python3 "$REPO/spikes/m1_live/close_window.py" "$WID" | sed 's/^/  /'
t=0
while [ $t -lt 40 ]; do ps -p "$PID" >/dev/null 2>&1 || break; sleep 0.25; t=$((t+1)); done
echo "  окон осталось: $(xdotool search --class astra-voice 2>/dev/null | wc -l)"
echo "  журнал: $(tail -1 "$APPLOG")"
check "процесс завершился" "0" "$(pgrep -c -f "$PAT")"
check "lock убран" "нет" "$([ -e "$XDG_RUNTIME_DIR/astra-voice/lock" ] && echo есть || echo нет)"
check "ipc убран" "нет" "$([ -e "$XDG_RUNTIME_DIR/astra-voice/ipc" ] && echo есть || echo нет)"

echo
echo "== дефект 1 (обратная сторона): светлая схема → светлое окно =="
if [ "$(pgrep -c -f "$PAT")" != "0" ]; then
  echo "  (снимаю зависший процесс, иначе светлую фазу не запустить)"; pkill -TERM -f "$PAT"; sleep 2
fi
cp "$TMPD/kdeglobals.light" "$XDG_CONFIG_HOME/kdeglobals"
read -r WID2 PID2 <<< "$(start_app "$OUT/live_iso_app_light.stderr")"
if [ -n "$WID2" ]; then
  xdotool windowactivate "$WID2" 2>/dev/null; sleep 1
  L_LIGHT=$(luma "$WID2" "$OUT/live_iso_window_light.png")
  echo "  средняя яркость центра: $L_LIGHT"
  echo "  журнал: $(grep -o 'тема сессии:.*' "$APPLOG" | tail -1)"
  check "окно светлое (яркость > 150)" "да" "$([ "${L_LIGHT:-0}" -gt 150 ] && echo да || echo нет)"
else
  check "окно светлой темы поднялось" "да" "нет"
fi

echo
echo "== уборка по SIGTERM (тот же finally) =="
kill -TERM "$PID2" 2>/dev/null; t=0
while [ $t -lt 40 ]; do ps -p "$PID2" >/dev/null 2>&1 || break; sleep 0.25; t=$((t+1)); done
check "процесс завершился по SIGTERM" "0" "$(pgrep -c -f "$PAT")"
check "lock убран после SIGTERM" "нет" "$([ -e "$XDG_RUNTIME_DIR/astra-voice/lock" ] && echo есть || echo нет)"
check "ipc убран после SIGTERM" "нет" "$([ -e "$XDG_RUNTIME_DIR/astra-voice/ipc" ] && echo есть || echo нет)"

echo
echo "== stderr приложения (должен быть пуст) =="
echo "  тёмный запуск: [$(cat "$OUT/live_iso_app.stderr")]"
echo "  светлый запуск: [$(cat "$OUT/live_iso_app_light.stderr" 2>/dev/null)]"

echo
echo "ИТОГ: PASS=$pass FAIL=$fail"
echo "PASS=$pass FAIL=$fail" > "$OUT/live_iso_verdict.log"
echo
echo "== журнал приложения =="
cat "$APPLOG" 2>/dev/null
exit 0
