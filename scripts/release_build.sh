#!/usr/bin/env bash
# Сборка и проверка артефактов выпуска в job release (arch/appimage.md §9.3).
#
#   scripts/release_build.sh
#
# Только в контейнере CI (debian:12, root): ставит собранный пакет в систему.
#   1. .deb: колёса по wheels.lock (сеть) → dh + lintian + гейты (packaging/build-deb.sh);
#   2. T3 P2-6: ставится ИМЕННО тот .deb, который будет подписан, — apt install,
#      astra-voice --version, smoke-installed.sh; sha256 пакета до и после совпадает;
#   3. при packaging/appimage/ENABLED: build.sh --fetch (сеть) → build.sh → smoke.sh.
# Секрет подписи здесь не нужен и не должен быть в окружении (T3 P2-1).
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
say() { printf '==> %s\n' "$*"; }
die() { printf 'ОШИБКА: %s\n' "$*" >&2; exit 1; }

[ -z "${GPG_SIGNING_KEY:-}" ] || die 'секрет подписи не должен быть в окружении сборки (T3 P2-1)'
[ "$(id -u)" = 0 ] || die 'скрипт ставит пакет в систему: только в контейнере CI от root'
cd "$ROOT"
VERSION=$(sed -n '1s/.*(\([^)]*\)).*/\1/p' packaging/debian/changelog)
DEB=dist/astra-voice_${VERSION}_amd64.deb

say '.deb: колёса по wheels.lock'
packaging/build-deb.sh --fetch-wheels
say '.deb: сборка и гейты'
packaging/build-deb.sh --dh
[ -f "$DEB" ] || die "нет $DEB"

say "T3 P2-6: установка подписываемого пакета $DEB"
before=$(sha256sum "$DEB" | cut -d' ' -f1)
apt-get install -y "./$DEB"
astra-voice --version
packaging/smoke-installed.sh /
after=$(sha256sum "$DEB" | cut -d' ' -f1)
[ "$before" = "$after" ] || die "$DEB изменился во время проверки установки"
say "установлен и проверен: $DEB ($before)"

if [ -f packaging/appimage/ENABLED ]; then
    say 'AppImage: входы по lock (сеть)'
    packaging/appimage/build.sh --fetch
    say 'AppImage: сборка и гейты'
    packaging/appimage/build.sh
    packaging/appimage/smoke.sh "dist/Astra_Voice-$VERSION-x86_64.AppImage"
else
    say 'AppImage выключен (нет packaging/appimage/ENABLED)'
fi
