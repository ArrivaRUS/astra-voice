#!/usr/bin/env bash
# Архив исходников выпуска AppImage (arch/appimage.md R3.6) — без сети.
#
#   scripts/release_sources.sh dist
#
# Входы — записи `# source:` packaging/appimage.lock в кэше AppImage (их скачивает
# packaging/appimage/build.sh --fetch вместе с остальными входами) и текущий коммит.
# Пишет dist/astra-voice-<версия>-sources.tar.xz и сразу проверяет его потоково по
# манифесту. Вызывает release_build.sh при packaging/appimage/ENABLED.
set -euo pipefail
export LC_ALL=C

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

(($# == 1)) || { echo 'Использование: scripts/release_sources.sh <каталог выпуска>' >&2; exit 2; }
mkdir -p "$1"
DIST=$(cd "$1" && pwd)
CACHE=${ASTRA_VOICE_APPIMAGE_CACHE:-$HOME/.cache/astra-voice-dev/appimage}
VERSION=$(sed -n '1s/.*(\([^)]*\)).*/\1/p' "$ROOT/packaging/debian/changelog")
[ -n "$VERSION" ] || die 'не найдена версия в changelog'
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    CHANGELOG_DATE=$(sed -n 's/^ -- .*>  //p' "$ROOT/packaging/debian/changelog" | head -1)
    SOURCE_DATE_EPOCH=$(date -u -d "$CHANGELOG_DATE" +%s) || die 'не разобрал дату changelog'
fi
export SOURCE_DATE_EPOCH
OUT=$DIST/astra-voice-$VERSION-sources.tar.xz
python3 "$ROOT/scripts/release_sources.py" build --lock "$ROOT/packaging/appimage.lock" \
    --cache "$CACHE" --root "$ROOT" --version "$VERSION" --out "$OUT"
python3 "$ROOT/scripts/release_sources.py" check "$OUT"
