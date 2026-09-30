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
  --wav ПУТЬ         WAV (по умолчанию data/test/test-ru-6s.wav)
  --hotkey СОЧЕТАНИЕ Хоткей (из settings.json, запасной Ctrl+Space)
  --yes              Не спрашивать подтверждение занятия клавиатуры и буфера
  --help             Эта справка
Перед запуском выполните virtual_mic.sh up — входом по умолчанию станет av_test_src.
В Astra Voice выберите режим удержания хоткея и загрузите модель.
Проверка «программа запущена» (hotkey_diag.py app-pid) находит только
программу, установленную пакетом
(/usr/bin/python3 -I /usr/lib/astra-voice/bootstrap.py app); запуск из venv
или AppImage она не видит.
Подготовьте поле ввода Kate/fly-term.
После подтверждения даётся 5 секунд, чтобы перевести фокус в это поле.
Если нажатие за 4 с не дошло до диктовки (в журнале нет «record.start отправлен»),
прогон прерывается с диагностикой хоткея. Журнал: $XDG_DATA_HOME/astra-voice/logs,
другой путь — переменная ASTRA_VOICE_E2E_LOG.
Цель: p95 полного времени «отпустил → текст в окне» (t_total_ms) ≤ 500 мс.
Коды возврата: 0 — уложились, 1 — не уложились, 2 — прогон невозможен.
EOF
}

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/../.." && pwd)
session=
count=50
wav="$ROOT/data/test/test-ru-6s.wav"
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

for command in xdotool paplay astra-voice python3 pactl awk; do
  command -v "$command" >/dev/null 2>&1 || fail "Нет команды $command. Установите её перед прогоном."
done
[ -n "${DISPLAY:-}" ] || fail 'Не задан DISPLAY. Запустите сценарий в графической сессии KDE/Fly.'
[ -f "$wav" ] && [ -r "$wav" ] || fail 'WAV не найден или недоступен. Укажите --wav ПУТЬ.'
case "$wav" in
  /*) ;;
  *) wav="$PWD/$wav" ;;
esac
diag="$ROOT/scripts/e2e/hotkey_diag.py"
# Пакетный launcher делает exec python3 -I .../bootstrap.py app: ищем точный argv в /proc.
if ! python3 "$diag" app-pid >/dev/null; then
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
    if not 1 <= count <= 999:
        raise ValueError("--count должен быть от 1 до 999 (история хранит до 1000 событий).")
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
        # Статистика сбрасывается на диск раз в 30 с: учитываем ожидание каждой диктовки.
        "minutes": max(1, math.ceil(((count + 1) * (duration + 2 + 30) + 5) / 60)),
        "hotkey": "+".join(aliases.get(part.lower(), part) for part in parts),
    }))
except (OSError, ValueError, TypeError, AttributeError, EOFError, wave.Error) as exc:
    print(f"Не удалось подготовить прогон: {exc}", file=sys.stderr)
    sys.exit(2)
PY
) || fail 'Проверьте WAV, число диктовок и настройку хоткея.'

json_field() {
  python3 -c '
import json
import sys

try:
    data = json.load(sys.stdin)
except (json.JSONDecodeError, UnicodeDecodeError):
    print("Вердикт: прогон невозможен — итог статистики не читается как JSON.", file=sys.stderr)
    sys.exit(2)
for key in sys.argv[1].split("."):
    if not isinstance(data, dict) or key not in data:
        print(f"Вердикт: прогон невозможен — в итоге статистики нет поля {sys.argv[1]}.", file=sys.stderr)
        sys.exit(2)
    data = data[key]
print(data)
' "$1"
}
count=$(printf '%s\n' "$metadata" | json_field count)
minutes=$(printf '%s\n' "$metadata" | json_field minutes)
hotkey=$(printf '%s\n' "$metadata" | json_field hotkey)
printf 'Скрипт займёт клавиатуру и буфер обмена до %s минут\n' "$minutes"
printf 'Сессия: %s; диктовок: %s; хоткей: %s.\n' "$session" "$count" "$hotkey"
printf '%s\n' 'Вход по умолчанию — av_test_src (virtual_mic.sh up).'
printf '%s\n' 'В Astra Voice должен быть выбран режим удержания; модель загружена.'
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
  printf '%s\n' "$stats_json" | json_field dictations >/dev/null || exit 2
}

printf '%s\n' 'Статистика до прогона:'
astra-voice --stats || fail 'Не удалось прочитать astra-voice --stats.'
read_stats
before_events=$(printf '%s\n' "$stats_json" | json_field events)
before_dictations=$(printf '%s\n' "$stats_json" | json_field dictations)
printf 'До прогона: событий %s, диктовок %s.\n' "$before_events" "$before_dictations"
since=$(python3 -c 'import time; print(time.time())')

log_missing_said=0
# Нажатие не дошло до диктовки: собрать диагностику (без текста) и прервать прогон.
hotkey_diagnostics() {
  say_error "Нажатие $hotkey не дошло до диктовки: за 4 с в журнале нет «record.start отправлен»."
  printf '%s\n' 'Диагностика хоткея:'
  python3 "$diag" probe-grab "$hotkey" || true
  xdotool keyup --delay 100 "$hotkey" || say_error "Не удалось отпустить $hotkey. Отпустите клавиши вручную."
  key_down=0
  sleep 0.2
  python3 "$diag" probe-keyboard || true
  python3 "$diag" keymap "$hotkey" || true
  python3 "$diag" hotkey-lines --offset "$log_offset" || true
  fail 'Вердикт: прогон невозможен — хоткей не запускает диктовку (диагностика выше).'
}

key_down=0
cleanup() {
  exit_status=$?
  trap - 0 HUP INT TERM
  if [ "$key_down" -eq 1 ]; then
    # Дефолтных 12 мс между модификатором и клавишей мало: программа не видит сочетание.
    if ! xdotool keyup --delay 100 "$hotkey"; then
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
done_count=0
max_iterations=$((count + 1))
incomplete=0
while [ "$iteration" -le "$max_iterations" ]; do
  printf 'Диктовка %s из не более %s\n' "$iteration" "$max_iterations"
  log_offset=$(python3 "$diag" offset) || log_offset=-1
  key_down=1
  xdotool keydown --delay 100 "$hotkey" || fail 'Не удалось нажать хоткей.'
  # Нажатие должно дойти до диктовки за 4 с; иначе не ждём 120 опросов впустую.
  start_code=0
  python3 "$diag" wait-start --offset "$log_offset" --timeout 4 || start_code=$?
  case "$start_code" in
    0) ;;
    1) hotkey_diagnostics ;;
    *)
      if [ "$log_missing_said" -eq 0 ]; then
        say_error 'Журнал Astra Voice не найден: проверка «record.start отправлен» пропущена.'
        log_missing_said=1
      fi
      ;;
  esac
  # Даём GUI открыть источник; даже короткий WAV не превращает удержание в тап.
  sleep 0.5
  paplay --device=av_test "$wav" || fail 'Не удалось подать WAV в виртуальный микрофон.'
  xdotool keyup --delay 100 "$hotkey" || fail 'Не удалось отпустить хоткей.'
  key_down=0
  done_count=$iteration
  attempts=0
  while :; do
    # Пауза между итерациями всегда больше 300 мс, ожидание ограничено.
    sleep 1
    read_stats --since "$since"
    added=$(printf '%s\n' "$stats_json" | json_field dictations) || exit 2
    [ "$added" -lt "$iteration" ] || break
    attempts=$((attempts + 1))
    if [ "$attempts" -ge 120 ]; then
      say_error 'Нет новой завершённой диктовки за 120 проверок. Проверьте GUI, модель и хоткей.'
      incomplete=1
      break
    fi
  done
  [ "$incomplete" -eq 0 ] || break
  warm=$(printf '%s\n' "$stats_json" | json_field t_total_ms.n) || exit 2
  [ "$warm" -lt "$count" ] || break
  if [ "$iteration" -ge "$count" ]; then
    cold=$(printf '%s\n' "$stats_json" | json_field cold) || exit 2
    if [ "$iteration" -eq "$count" ] && [ "$warm" -eq "$((count - 1))" ] && [ "$cold" -ge 1 ]; then
      printf '%s\n' 'Первая диктовка была холодной (модель прогревалась), делаем ещё одну'
    else
      break
    fi
  fi
  sleep 0.3
  iteration=$((iteration + 1))
done

printf '%s\n' 'Статистика после прогона:'
astra-voice --stats || fail 'Не удалось прочитать итоговую astra-voice --stats.'
read_stats
after_events=$(printf '%s\n' "$stats_json" | json_field events)
printf 'Событий в истории: до %s, после %s (история ограничена 1000 событиями).\n' \
  "$before_events" "$after_events"
read_stats --since "$since" --min-n "$count"
added_events=$(printf '%s\n' "$stats_json" | json_field events)
added=$(printf '%s\n' "$stats_json" | json_field dictations) || exit 2
warm=$(printf '%s\n' "$stats_json" | json_field t_total_ms.n) || exit 2
printf 'За прогон добавилось событий: %s; диктовок: %s; прогретых вставленных: %s из %s.\n' \
  "$added_events" "$added" "$warm" "$count"
printf '%s\n' "$stats_json"
if [ "$incomplete" -ne 0 ] || [ "$added" -ne "$done_count" ]; then
  fail 'Вердикт: прогон невозможен — число новых диктовок не совпало с заданным.'
fi
if [ "$warm" -lt "$count" ]; then
  cold=$(printf '%s\n' "$stats_json" | json_field cold) || exit 2
  empty=$(printf '%s\n' "$stats_json" | json_field results.empty) || exit 2
  cancelled=$(printf '%s\n' "$stats_json" | json_field results.cancelled) || exit 2
  other=$((added - warm - cold - empty - cancelled))
  other_detail=
  if [ "$other" -ge 0 ]; then
    other_detail=", остальные не вставлены в окно (текст остался в буфере или окно сменилось): $other"
  fi
  say_error "Вердикт: недостаточно данных — прогретых вставленных $warm из $count, холодных $cold, пустых $empty, отменённых $cancelled$other_detail."
  exit 2
fi
total_p95=$(printf '%s\n' "$stats_json" | json_field p95_ms) || exit 2
reference_p95=$(printf '%s\n' "$stats_json" | json_field t_ms.p95) || exit 2
printf 'Справочно: p95 t_ms = %s мс.\n' "$reference_p95"
case "$stats_code" in
  0) printf 'Вердикт: уложились — p95 полного времени «отпустил → текст в окне» (t_total_ms) = %s мс ≤ 500 мс.\n' "$total_p95" ;;
  1) printf 'Вердикт: не уложились — p95 полного времени «отпустил → текст в окне» (t_total_ms) = %s мс > 500 мс.\n' "$total_p95" ;;
  2) say_error 'Вердикт: прогон невозможен — нет t_total_ms у прогретых диктовок.' ;;
esac
exit "$stats_code"
