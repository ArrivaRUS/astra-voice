#!/usr/bin/env bash
# Набор ассетов выпуска БЕЗ секрета подписи (arch/appimage.md §9.3; T3 P2-1/P2-2).
#
#   scripts/release_assets.sh dist
#
# Копирует в каталог выпуска INSTALL-ADMIN.md, SECURITY.md и release.gpg, пишет
# latest.json (схема 2; artifacts.appimage — при packaging/appimage/ENABLED),
# SHA256SUMS по всем файлам выпуска и assets.txt — список путей для
# `gh release create … $(cat dist/assets.txt)` (SHA256SUMS.asc добавит шаг подписи).
# Подпись делает отдельный шаг workflow с GPG_SIGNING_KEY; этот скрипт секрет не видит.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
say() { printf '==> %s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

(($# == 1)) || { echo 'Использование: scripts/release_assets.sh <каталог выпуска>' >&2; exit 2; }
[ -z "${GPG_SIGNING_KEY:-}" ] || die 'секрет подписи не должен быть в окружении этого шага (T3 P2-1)'
DIST=$(cd "$1" && pwd) || die "нет каталога $1"

VERSION=$(sed -n '1s/.*(\([^)]*\)).*/\1/p' "$ROOT/packaging/debian/changelog")
[[ $VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "версия changelog не вида X.Y.Z: $VERSION"
if [ -n "${GITHUB_REF_NAME:-}" ] && [ "${GITHUB_REF_NAME#v}" != "$VERSION" ]; then
    die "тег $GITHUB_REF_NAME не совпадает с версией changelog $VERSION"
fi

DEB=astra-voice_${VERSION}_amd64.deb
IMAGE=Astra_Voice-$VERSION-x86_64.AppImage
files=("$DEB" sbom.cdx.json)
latest_args=()
if [ -f "$ROOT/packaging/appimage/ENABLED" ]; then
    # Образ из lock с незакреплёнными колёсами не публикуется — без оглядки на переменные.
    todo=$(python3 "$ROOT/packaging/appimage/lockfile.py" "$ROOT/packaging/appimage.lock" todo) ||
        die 'packaging/appimage.lock не прошёл проверку формата'
    [ -z "$todo" ] || die "в packaging/appimage.lock колёса без хэша (TODO-HASH): $(echo "$todo" | tr '\n' ' ')"
    # Архив исходников поставляемых версий (R3.6) публикуется вместе с образом.
    files+=("$IMAGE" sbom-appimage.cdx.json "astra-voice-$VERSION-sources.tar.xz")
    latest_args+=(--appimage)
    say "AppImage включён: $IMAGE"
else
    say 'AppImage выключен (нет packaging/appimage/ENABLED)'
fi
for name in "${files[@]}"; do
    [ -f "$DIST/$name" ] && [ ! -L "$DIST/$name" ] || die "нет артефакта $DIST/$name"
done
# Посторонние пакеты и образы в каталоге выпуска — признак грязной сборки.
shopt -s nullglob
for path in "$DIST"/*.deb "$DIST"/*.AppImage; do
    name=$(basename "$path")
    [ "$name" = "$DEB" ] && continue
    [ "$name" = "$IMAGE" ] && [ ${#latest_args[@]} -gt 0 ] && continue
    die "посторонний артефакт в $DIST: $name"
done
shopt -u nullglob

for source in docs/INSTALL-ADMIN.md docs/SECURITY.md data/keys/release.gpg; do
    [ -f "$ROOT/$source" ] || die "нет $source"
    install -m 0644 "$ROOT/$source" "$DIST/$(basename "$source")"
done
files+=(INSTALL-ADMIN.md SECURITY.md release.gpg)

python3 "$ROOT/scripts/release_latest_json.py" --dist "$DIST" --version "$VERSION" "${latest_args[@]}"
files+=(latest.json)

(cd "$DIST" && sha256sum -- "${files[@]}" > SHA256SUMS)
say "SHA256SUMS: ${#files[@]} файлов"

rel=${DIST#"$ROOT"/}
[ "$rel" != "$DIST" ] || rel=$DIST
: > "$DIST/assets.txt"
for name in "${files[@]}" SHA256SUMS SHA256SUMS.asc; do
    printf '%s/%s\n' "$rel" "$name" >> "$DIST/assets.txt"
done
say "assets.txt: $(wc -l < "$DIST/assets.txt") ассетов (SHA256SUMS.asc — после шага подписи)"
