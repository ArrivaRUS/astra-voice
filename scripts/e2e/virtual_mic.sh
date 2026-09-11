#!/bin/sh
# Виртуальный микрофон для ручных проверок звука Astra Voice.
set -eu

STATE="${AV_VIRTUAL_MIC_STATE:-${XDG_RUNTIME_DIR:-/tmp}/astra-voice-virtual-mic}"

say_error() {
  printf '%s\n' "$*" >&2
}

# Возвращаем прежние устройства только при переключении на наши.
restore_defaults() {
  restore_status=0
  if current_sink=$(LC_ALL=C pactl get-default-sink); then
    case "$current_sink" in
      av_test) LC_ALL=C pactl set-default-sink "$OLD_SINK" || restore_status=1 ;;
    esac
  else
    restore_status=1
  fi
  if current_source=$(LC_ALL=C pactl get-default-source); then
    case "$current_source" in
      av_test_src|av_test.monitor)
        LC_ALL=C pactl set-default-source "$OLD_SOURCE" || restore_status=1 ;;
    esac
  else
    restore_status=1
  fi
  return "$restore_status"
}

verify_defaults() {
  actual_sink=$(LC_ALL=C pactl get-default-sink) || return 1
  actual_source=$(LC_ALL=C pactl get-default-source) || return 1
  printf 'Выход по умолчанию: %s\nИсточник по умолчанию: %s\n' \
    "$actual_sink" "$actual_source"
  if [ "$actual_sink" != "$OLD_SINK" ] || [ "$actual_source" != "$OLD_SOURCE" ]; then
    say_error "Устройства по умолчанию изменились. Ожидались: $OLD_SINK и $OLD_SOURCE."
    return 1
  fi
}

verify_down() {
  verify_status=0
  if modules=$(LC_ALL=C pactl list short modules); then
    if printf '%s\n' "$modules" | grep av_test; then
      say_error 'Остались модули av_test.'
      verify_status=1
    else
      printf '%s\n' 'Модулей av_test не осталось.'
    fi
  else
    say_error 'Не удалось проверить список модулей.'
    verify_status=1
  fi
  verify_defaults || verify_status=1
  return "$verify_status"
}

rollback() {
  exit_status=$?
  trap - 0 HUP INT TERM
  set +e
  say_error 'Не удалось поднять виртуальный микрофон. Убираю загруженные модули.'
  # Сначала возвращаем устройства: выгрузка сама может выбрать другой default.
  restore_defaults
  cleanup_status=0
  for module_id in "$REMAP_ID" "$SINK_ID"; do
    [ -n "$module_id" ] || continue
    if ! LC_ALL=C pactl unload-module "$module_id"; then
      say_error "Не удалось выгрузить модуль $module_id."
      cleanup_status=1
    fi
  done
  restore_defaults || cleanup_status=1
  verify_down || cleanup_status=1
  rm -f -- "$STATE" || cleanup_status=1
  if [ "$cleanup_status" -ne 0 ]; then
    say_error 'Уборка не завершена. Проверьте и восстановите звук вручную.'
  fi
  [ "$exit_status" -ne 0 ] || exit_status=1
  exit "$exit_status"
}

if [ "$#" -ne 1 ]; then
  say_error "Использование: $0 up|down|check"
  exit 64
fi

case "$1" in
  up)
    modules=$(LC_ALL=C pactl list short modules)
    if printf '%s\n' "$modules" | grep -q av_test; then
      say_error 'Модули av_test уже загружены. Второй микрофон создавать нельзя.'
      exit 1
    fi
    OLD_SINK=$(LC_ALL=C pactl get-default-sink)
    OLD_SOURCE=$(LC_ALL=C pactl get-default-source)
    SINK_ID=
    REMAP_ID=
    # Не затираем старое состояние; файл доступен только владельцу.
    umask 077
    if ! (set -C; : > "$STATE"); then
      say_error "Не удалось создать файл состояния $STATE: он уже есть или недоступен."
      exit 1
    fi
    trap rollback 0
    trap 'exit 129' HUP
    trap 'exit 130' INT
    trap 'exit 143' TERM
    SINK_ID=$(LC_ALL=C pactl load-module module-null-sink sink_name=av_test \
      sink_properties=device.description=av_test)
    # PipeWire может создать выключенный null-sink: включаем только свой.
    LC_ALL=C pactl set-sink-mute av_test 0
    REMAP_ID=$(LC_ALL=C pactl load-module module-remap-source \
      master=av_test.monitor source_name=av_test_src \
      source_properties=device.description=av_test_src)
    restore_defaults
    verify_defaults
    # Четыре строки: id в порядке выгрузки, затем прежние выход и источник.
    # Файл читается как данные, а не исполняется как код shell.
    printf '%s\n' "$REMAP_ID" "$SINK_ID" "$OLD_SINK" "$OLD_SOURCE" > "$STATE"
    printf 'Виртуальный микрофон поднят. Состояние: %s\n' "$STATE"
    trap - 0 HUP INT TERM
    ;;
  down)
    if [ ! -e "$STATE" ] && [ ! -L "$STATE" ]; then
      printf '%s\n' 'Нет файла состояния — нечего выгружать.'
      exit 0
    fi
    if [ ! -f "$STATE" ] || [ -L "$STATE" ]; then
      say_error "Файл состояния $STATE должен быть обычным файлом, а не ссылкой."
      exit 1
    fi
    if ! {
      IFS= read -r REMAP_ID && IFS= read -r SINK_ID &&
      IFS= read -r OLD_SINK && IFS= read -r OLD_SOURCE &&
      ! IFS= read -r extra && [ -z "$extra" ]
    } < "$STATE"; then
      say_error 'Файл состояния повреждён. Проверьте звук вручную.'
      exit 1
    fi
    for module_id in "$REMAP_ID" "$SINK_ID"; do
      case "$module_id" in
        ''|*[!0-9]*)
          say_error 'В файле состояния неверные id модулей. Проверьте звук вручную.'
          exit 1 ;;
      esac
    done
    if [ "$REMAP_ID" = "$SINK_ID" ] || [ -z "$OLD_SINK" ] || [ -z "$OLD_SOURCE" ]; then
      say_error 'Файл состояния повреждён. Проверьте звук вручную.'
      exit 1
    fi
    modules=$(LC_ALL=C pactl list short modules)
    down_status=0
    # Как в rollback: возвращаем умолчания до выгрузки и проверяем после неё.
    restore_defaults || down_status=1
    expected_module=module-remap-source
    expected_argument=source_name=av_test_src
    for module_id in "$REMAP_ID" "$SINK_ID"; do
      # После перезапуска сервера старый id может принадлежать чужому модулю.
      if printf '%s\n' "$modules" | awk -v id="$module_id" \
        -v module="$expected_module" -v argument="$expected_argument" '
          $1 == id && $2 == module {
            for (i = 3; i <= NF; i++) if ($i == argument) found = 1
          }
          END { exit !found }
        '; then
        if ! LC_ALL=C pactl unload-module "$module_id"; then
          say_error "Не удалось выгрузить модуль $module_id."
          down_status=1
        fi
      else
        say_error "Модуль $module_id отсутствует или не совпадает с нашим; пропускаю."
        down_status=1
      fi
      expected_module=module-null-sink
      expected_argument=sink_name=av_test
    done
    restore_defaults || down_status=1
    rm -f -- "$STATE" || down_status=1
    verify_down || down_status=1
    if [ "$down_status" -ne 0 ]; then
      say_error 'Уборка не прошла проверку. Проверьте и восстановите звук вручную.'
      exit 1
    fi
    printf '%s\n' 'Виртуальный микрофон выгружен, устройства по умолчанию совпадают с прежними.'
    ;;
  check)
    current_sink=$(LC_ALL=C pactl get-default-sink)
    current_source=$(LC_ALL=C pactl get-default-source)
    printf 'Выход по умолчанию: %s\nИсточник по умолчанию: %s\n' \
      "$current_sink" "$current_source"
    modules=$(LC_ALL=C pactl list short modules)
    printf '%s\n' 'Модули av_test:'
    if ! printf '%s\n' "$modules" | grep av_test; then
      printf '%s\n' 'Модулей av_test нет.'
    fi
    ;;
  *)
    say_error "Использование: $0 up|down|check"
    exit 64
    ;;
esac
