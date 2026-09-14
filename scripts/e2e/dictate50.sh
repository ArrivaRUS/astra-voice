#!/bin/sh
# Приёмка Ц2 через настоящий GUI в KDE и Fly.
set -eu

say_error() {
  printf '%s\n' "$*" >&2
}

fail() {
  say_error "$*"
  exit 2
}

usage() {
  cat <<'EOF'
Использование: scripts/e2e/dictate50.sh --session kde|fly [параметры]
  --count N          Число диктовок (по умолчанию 50)
  --wav ПУТЬ         WAV (по умолчанию data/test/test-ru-20s.wav)
  --hotkey СОЧЕТАНИЕ Хоткей (из settings.json, запасной Ctrl+Space)
  --yes              Не спрашивать подтверждение занятия клавиатуры и буфера
  --help             Эта справка
Перед запуском поднимите virtual_mic.sh up, выберите av_test_src в Astra Voice,
режим удержания хоткея и загрузите модель. Подготовьте поле ввода Kate/fly-term.
После подтверждения даётся 5 секунд, чтобы перевести фокус в это поле.
Цель: p95 «отпустил → текст» ≤ 500 мс по событиям текущего прогона.
Коды возврата: 0 — уложились, 1 — не уложились, 2 — прогон невозможен.
EOF
}

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
session=
count=50
wav="$ROOT/data/test/test-ru-20s.wav"
hotkey=
yes=0
while [ "$#" -gt 0 ]; do
  case "$1" in
    --session|--count|--wav|--hotkey)
      [ "$#" -ge 2 ] || fail "После $1 требуется значение."
      [ -n "$2" ] || fail "После $1 требуется непустое значение."
      case "$1" in
        --session) session=$2 ;;
        --count) count=$2 ;;
        --wav) wav=$2 ;;
        --hotkey) hotkey=$2 ;;
      esac
      shift 2
      ;;
    --yes) yes=1; shift ;;
    --help) usage; exit 0 ;;
    *) fail "Неизвестный параметр: $1. Используйте --help." ;;
  esac
done
case "$session" in
  kde|fly) ;;
  *) fail 'Обязательно укажите --session kde или --session fly.' ;;
esac
case "$count" in
  ''|*[!0-9]*) fail '--count должен быть положительным целым числом.' ;;
esac

for command in xdotool paplay astra-voice python3 pgrep id pactl awk; do
  command -v "$command" >/dev/null 2>&1 || fail "Нет команды $command. Установите её перед прогоном."
done
[ -n "${DISPLAY:-}" ] || fail 'Не задан DISPLAY. Запустите сценарий в графической сессии KDE/Fly.'
[ -f "$wav" ] && [ -r "$wav" ] || fail 'WAV не найден или недоступен. Укажите --wav ПУТЬ.'
case "$wav" in
  /*) ;;
  *) wav="$PWD/$wav" ;;
esac
# Пакетный launcher делает exec python3 -I .../bootstrap.py app.
app_pattern='(^|/)astra-voice([[:space:]]|$)'
app_pattern="$app_pattern|python[^[:space:]]*[[:space:]]+-m[[:space:]]+astra_voice([[:space:]]|$)"
app_pattern="$app_pattern|/(astra-voice|astra_voice)/bootstrap[.]py[[:space:]]+app([[:space:]]|$)"
if ! pgrep -u "$(id -u)" -f "$app_pattern" >/dev/null; then
  fail 'Astra Voice не запущен. Запустите astra-voice и дождитесь загрузки модели.'
fi
xdotool getactivewindow >/dev/null 2>&1 || fail 'Нет доступа к активному окну X11. Проверьте DISPLAY.'

mic_status=0
"$ROOT/scripts/e2e/virtual_mic.sh" status >/dev/null 2>&1 || mic_status=$?
case "$mic_status" in
  0) ;;
  64)
    # Совместимость с версией, в которой диагностическая команда называется check.
    "$ROOT/scripts/e2e/virtual_mic.sh" check || fail 'Не удалось проверить виртуальный микрофон.'
    ;;
  *) fail 'Виртуальный микрофон не готов. Выполните scripts/e2e/virtual_mic.sh up.' ;;
esac
sinks=$(LC_ALL=C pactl list short sinks) || fail 'Не удалось получить список звуковых выходов.'
sources=$(LC_ALL=C pactl list short sources) || fail 'Не удалось получить список микрофонов.'
printf '%s\n' "$sinks" | awk '$2 == "av_test" { found = 1 } END { exit !found }' ||
  fail 'Нет выхода av_test. Выполните scripts/e2e/virtual_mic.sh up.'
printf '%s\n' "$sources" | awk '$2 == "av_test_src" { found = 1 } END { exit !found }' ||
  fail 'Нет источника av_test_src. Выполните scripts/e2e/virtual_mic.sh up.'

# Разбираем настройки и WAV как данные, без исполнения содержимого в shell.
metadata=$(python3 - "$wav" "$count" "$hotkey" <<'PY'
import json
import math
import os
import sys
import wave
from pathlib import Path

try:
    count = int(sys.argv[2])
    if not 1 <= count <= 1000:
        raise ValueError("--count должен быть от 1 до 1000 (размер истории статистики).")
    with wave.open(sys.argv[1], "rb") as source:
        duration = source.getnframes() / source.getframerate()
    if not 0 < duration < 119:
        raise ValueError("Нужен непустой WAV короче 119 с, чтобы не сработал лимит записи.")
    hotkey = sys.argv[3]
    if not hotkey:
        base = os.environ.get("XDG_CONFIG_HOME", "")
        config = Path(base) if base.startswith("/") else Path.home() / ".config"
        path = config / "astra-voice/settings.json"
        raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        hotkey = raw.get("hotkey") or "Ctrl+Space"
    if not isinstance(hotkey, str):
        raise ValueError("Хоткей должен быть строкой. Укажите --hotkey.")
    aliases = {
        "ctrl": "ctrl", "control": "ctrl", "shift": "shift", "alt": "alt",
        "meta": "alt", "super": "super", "win": "super", "space": "space",
        "esc": "Escape", "escape": "Escape",
    }
    parts = [part.strip() for part in hotkey.split("+")]
    if any(not part or not part.replace("_", "").isalnum() for part in parts):
        raise ValueError("Некорректное сочетание в --hotkey/settings.json.")
    print(json.dumps({
        "count": count,
        "minutes": max(1, math.ceil((count * (duration + 2) + 5) / 60)),
        "hotkey": "+".join(aliases.get(part.lower(), part) for part in parts),
    }))
except (OSError, ValueError, TypeError, AttributeError, EOFError, wave.Error) as exc:
    print(f"Не удалось подготовить прогон: {exc}", file=sys.stderr)
    sys.exit(2)
PY
) || fail 'Проверьте WAV, число диктовок и настройку хоткея.'

json_field() {
  python3 -c 'import json, sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}
count=$(printf '%s\n' "$metadata" | json_field count)
minutes=$(printf '%s\n' "$metadata" | json_field minutes)
hotkey=$(printf '%s\n' "$metadata" | json_field hotkey)
printf 'Скрипт займёт клавиатуру и буфер обмена примерно на %s минут\n' "$minutes"
printf 'Сессия: %s; диктовок: %s; хоткей: %s.\n' "$session" "$count" "$hotkey"
printf '%s\n' 'В Astra Voice должны быть выбраны av_test_src и режим удержания; модель загружена.'
if [ "$yes" -ne 1 ]; then
  printf '%s' 'Продолжить? Введите да: '
  IFS= read -r answer || fail 'Подтверждение не получено. Для автоматического запуска есть --yes.'
  case "$answer" in
    да|Да|ДА|yes|y) ;;
    *) fail 'Прогон отменён.' ;;
  esac
fi

# Код 2 допустим для пустой начальной истории, но JSON обязателен.
read_stats() {
  stats_code=0
  stats_json=$("$ROOT/tools/validate" stats --json "$@") || stats_code=$?
  case "$stats_code" in
    0|1|2) ;;
    *) fail 'Не удалось запустить tools/validate stats. Проверьте Python и зависимости.' ;;
  esac
  printf '%s\n' "$stats_json" | json_field dictations >/dev/null ||
    fail 'tools/validate stats не вернул корректную статистику JSON.'
}

printf '%s\n' 'Статистика до прогона:'
astra-voice --stats || fail 'Не удалось прочитать astra-voice --stats.'
read_stats
before_events=$(printf '%s\n' "$stats_json" | json_field events)
before_dictations=$(printf '%s\n' "$stats_json" | json_field dictations)
printf 'До прогона: событий %s, диктовок %s.\n' "$before_events" "$before_dictations"
since=$(python3 -c 'import time; print(time.time())')

key_down=0
cleanup() {
  exit_status=$?
  trap - 0 HUP INT TERM
  if [ "$key_down" -eq 1 ]; then
    if ! xdotool keyup "$hotkey"; then
      say_error "Не удалось отпустить $hotkey. Отпустите клавиши вручную."
      exit_status=2
    fi
  fi
  exit "$exit_status"
}
trap cleanup 0
trap 'exit 2' HUP INT TERM
printf '%s\n' 'Переведите фокус в поле ввода Kate/fly-term. Начало через 5 секунд.'
sleep 5
iteration=1
incomplete=0
while [ "$iteration" -le "$count" ]; do
  printf 'Диктовка %s из %s\n' "$iteration" "$count"
  key_down=1
  xdotool keydown "$hotkey" || fail 'Не удалось нажать хоткей.'
  # Даём GUI открыть источник; даже короткий WAV не превращает удержание в тап.
  sleep 0.5
  paplay --device=av_test "$wav" || fail 'Не удалось подать WAV в виртуальный микрофон.'
  xdotool keyup "$hotkey" || fail 'Не удалось отпустить хоткей.'
  key_down=0
  attempts=0
  while :; do
    # Пауза между итерациями всегда больше 300 мс, ожидание ограничено.
    sleep 1
    read_stats --since "$since"
    added=$(printf '%s\n' "$stats_json" | json_field dictations)
    [ "$added" -lt "$iteration" ] || break
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 120 ]; then
      say_error 'Нет новой завершённой диктовки за 120 проверок. Проверьте GUI, модель и хоткей.'
      incomplete=1
      break
    fi
  done
  [ "$incomplete" -eq 0 ] || break
  sleep 0.3
  iteration=$((iteration + 1))
done

printf '%s\n' 'Статистика после прогона:'
astra-voice --stats || fail 'Не удалось прочитать итоговую astra-voice --stats.'
read_stats
after_events=$(printf '%s\n' "$stats_json" | json_field events)
printf 'Событий в истории: до %s, после %s (история ограничена 1000 событиями).\n' \
  "$before_events" "$after_events"
read_stats --since "$since"
added_events=$(printf '%s\n' "$stats_json" | json_field events)
added=$(printf '%s\n' "$stats_json" | json_field dictations)
printf 'За прогон добавилось событий: %s; диктовок: %s из %s.\n' "$added_events" "$added" "$count"
printf '%s\n' "$stats_json"
if [ "$incomplete" -ne 0 ] || [ "$added" -ne "$count" ]; then
  fail 'Вердикт: прогон невозможен — число новых диктовок не совпало с заданным.'
fi
case "$stats_code" in
  0) printf '%s\n' 'Вердикт: уложились — p95 «отпустил → текст» ≤ 500 мс.' ;;
  1) printf '%s\n' 'Вердикт: не уложились — p95 «отпустил → текст» > 500 мс.' ;;
  2) say_error 'Вердикт: прогон невозможен — нет данных о задержке прогретых диктовок.' ;;
esac
exit "$stats_code"
