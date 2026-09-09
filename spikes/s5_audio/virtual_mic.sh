#!/bin/sh
# S5 — виртуальный источник звука для спайка (→ scripts/e2e/virtual_mic.sh в M3).
#
# up   — поднять null-sink `av_test` и remap-source `av_test_src` от его монитора;
# down — выгрузить ровно те модули, что подняли (id хранятся в STATE);
# check— показать текущие устройства по умолчанию и громкость.
#
# ПРАВИЛА БЕЗОПАСНОСТИ (звук заказчика не трогаем):
#   * default sink/source запоминаются ДО загрузки и сверяются ПОСЛЕ;
#     если PulseAudio/PipeWire переключил их на наш av_test — возвращаем назад;
#   * `wpctl get-volume` снимается до и после, mute не трогаем;
#   * `systemctl --user restart wireplumber` здесь НЕ выполняется (это звук заказчика) —
#     сценарий описан в README как «требует живого прогона».
set -eu

STATE="${AV_S5_STATE:-/tmp/av_s5_modules}"

snapshot() {
  echo "default-sink   : $(pactl get-default-sink)"
  echo "default-source : $(pactl get-default-source)"
  echo "vol sink       : $(wpctl get-volume @DEFAULT_AUDIO_SINK@ 2>&1)"
  echo "vol source     : $(wpctl get-volume @DEFAULT_AUDIO_SOURCE@ 2>&1)"
}

case "${1:-}" in
  up)
    OLD_SINK=$(pactl get-default-sink)
    OLD_SRC=$(pactl get-default-source)
    echo "ДО:"; snapshot
    M1=$(pactl load-module module-null-sink sink_name=av_test \
          sink_properties=device.description=av_test)
    M2=$(pactl load-module module-remap-source master=av_test.monitor \
          source_name=av_test_src source_properties=device.description=av_test_src)
    printf '%s\n%s\n' "$M2" "$M1" > "$STATE"     # выгружать в обратном порядке
    # ГРАБЛИ S5 (2026-09-09): PipeWire поднял наш null-sink С ВЫКЛЮЧЕННЫМ звуком
    # (`pactl get-sink-mute av_test` → «да»), из-за чего `av_test.monitor` отдавал
    # ровную тишину и захват «не ловил» paplay. Размучиваем ТОЛЬКО СВОЙ sink.
    pactl set-sink-mute av_test 0
    echo "av_test mute → $(pactl get-sink-mute av_test)"
    # возвращаем устройства по умолчанию, если сервер их переключил
    [ "$(pactl get-default-sink)"   = "$OLD_SINK" ] || pactl set-default-sink   "$OLD_SINK"
    [ "$(pactl get-default-source)" = "$OLD_SRC"  ] || pactl set-default-source "$OLD_SRC"
    echo "ПОСЛЕ:"; snapshot
    echo "модули: $M1 (null-sink), $M2 (remap-source) → $STATE"
    ;;
  down)
    [ -f "$STATE" ] || { echo "нет $STATE — нечего выгружать"; exit 0; }
    while read -r m; do [ -n "$m" ] && pactl unload-module "$m" || true; done < "$STATE"
    rm -f "$STATE"
    echo "ПОСЛЕ выгрузки:"; snapshot
    ;;
  check) snapshot ;;
  *) echo "использование: $0 up|down|check" >&2; exit 64 ;;
esac
