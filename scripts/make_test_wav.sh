#!/usr/bin/env bash
# Исходник: transcribe-rs, samples/russian.wav, фраза «Проверка связи».
# Формат: 16 кГц, моно, 16 бит. Команды из arch/spikes/S3.md §3.
# sha256 эталонных файлов из S3 (6-секундный файл копируется из кэша в репозиторий):
# test-ru-6s.wav:  0ca1ae8739e1d779dda3a24407ca94628b44faeac55e5aaf52c01a0f9f063ca4
# test-ru-20s.wav: 07016a138c585fcbd90de41c80eae96a6c0f5fa16b0484d897cca1a8f38580c0
# Исходные команды S3 используют случайный дизеринг SoX. Здесь -R фиксирует его:
# ожидаемые sha256 при повторной генерации с SoX 14.4.2 отличаются от эталонов:
# test-ru-6s.wav:  a6449a86e2f6c2f5b4faf5ec39abd36f99c3bd4416edbb1516e13381fef86f86
# test-ru-20s.wav: f0ca33e1e48150473a95c4ba4d29c9e5469091ef120ab22e84232ec9f5c6cf29
# Оба файла (6 и 20 секунд) лежат в репозитории; скрипт нужен для воспроизводимой перегенерации.
# test-ru-130s.wav проверяет лимит фразы 120 с; в репозиторий НЕ кладём (~4 МБ).
# Он собирается Python-скриптом без sox из data/test/test-ru-6s.wav в каталог результата,
# по умолчанию scratch-кэш вне репозитория. Отдельно: python3 scripts/make_long_wav.py --target …
# Запуск: scripts/make_test_wav.sh [путь к samples/russian.wav] [каталог результата]
set -euo pipefail
# Не наследуем внешние эффекты и настройки, влияющие на байты результата.
export SOX_OPTS=-R

if ! command -v sox >/dev/null 2>&1; then
    echo 'Ошибка: для генерации звуковых сэмплов установите sox.' >&2
    exit 1
fi

source_wav=${1:-"$HOME/Документы/handy-gigaam/vendor/transcribe-rs/samples/russian.wav"}
output_dir=${2:-"$HOME/.cache/astra-voice-spike/wav"}
if [[ ! -f "$source_wav" || ! -r "$source_wav" ]]; then
    echo 'Ошибка: укажите доступный исходник transcribe-rs samples/russian.wav.' >&2
    exit 1
fi

mkdir -p "$output_dir"
work_dir=$(mktemp -d)
trap 'rm -rf -- "$work_dir"' EXIT
sox -n -r 16000 -c 1 -b 16 "$work_dir/sil06.wav" trim 0.0 0.6
sox "$source_wav" "$work_dir/sil06.wav" \
    "$source_wav" "$work_dir/sil06.wav" \
    "$source_wav" "$work_dir/sil06.wav" "$work_dir/tmp6.wav"
sox "$work_dir/tmp6.wav" "$output_dir/test-ru-6s.wav" pad 0 6 trim 0 6
sox "$work_dir/tmp6.wav" "$work_dir/tmp20.wav" repeat 4
sox "$work_dir/tmp20.wav" "$output_dir/test-ru-20s.wav" pad 0 20 trim 0 20
script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
python3 "$script_dir/make_long_wav.py" \
    --source "$script_dir/../data/test/test-ru-6s.wav" \
    --target "$output_dir/test-ru-130s.wav" --seconds 130
