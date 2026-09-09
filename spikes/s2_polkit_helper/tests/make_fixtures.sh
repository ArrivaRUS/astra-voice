#!/bin/sh
# Пересборка фикстур подписи для tests/test_helper.py.
# Требует тестовых ключей спайка: GNUPGHOME=~/.cache/astra-voice-spike/gpg
# (генерируются один раз, см. README «Тестовые ключи»).
set -eu

HERE=$(cd "$(dirname "$0")" && pwd)
SPIKE=$(dirname "$HERE")
FX="$HERE/fixtures"
export GNUPGHOME="${GNUPGHOME:-$HOME/.cache/astra-voice-spike/gpg}"

sign() {  # sign <uid> <файл SUMS> <выход .asc>
    gpg --batch --yes --pinentry-mode loopback --passphrase '' -u "$1" \
        --detach-sign --armor --output "$3" "$2"
}

rm -rf "$FX"
mkdir -p "$FX/asc_s2" "$FX/asc_revoked" "$FX/asc_foreign" "$FX/dup" "$FX/badsum" "$FX/other"

# 1. те же SHA256SUMS, подписанные другими ключами (T-05)
for k in s2 revoked foreign; do
    sign "spike-$k@example.invalid" "$SPIKE/packages/SHA256SUMS" "$FX/asc_$k/SHA256SUMS.asc"
done

# 2. подписанный SHA256SUMS с двумя строками на одно имя (T-30)
cat "$SPIKE/packages/SHA256SUMS" > "$FX/dup/SHA256SUMS"
grep 'astra-voice-spike_0.0.2_all.deb' "$SPIKE/packages/SHA256SUMS" >> "$FX/dup/SHA256SUMS"
sign spike-s1@example.invalid "$FX/dup/SHA256SUMS" "$FX/dup/SHA256SUMS.asc"

# 3. подписанный SHA256SUMS с неверной суммой для 0.0.2
sed 's/^[0-9a-f]\{64\}\(  astra-voice-spike_0.0.2_all.deb\)$/0000000000000000000000000000000000000000000000000000000000000000\1/' \
    "$SPIKE/packages/SHA256SUMS" > "$FX/badsum/SHA256SUMS"
sign spike-s1@example.invalid "$FX/badsum/SHA256SUMS" "$FX/badsum/SHA256SUMS.asc"

# 4. корректно подписанный пакет с ЧУЖИМ Package (проверка dpkg-deb --field)
B=$(mktemp -d)
mkdir -p "$B/pkg/DEBIAN" "$B/pkg/usr/share/doc/astra-voice-other"
cat > "$B/pkg/DEBIAN/control" <<EOF
Package: astra-voice-other
Version: 0.0.3
Architecture: all
Maintainer: Astra Voice spike <spike@example.invalid>
Section: misc
Priority: optional
Description: Foreign package fixture for the S2 helper spike
 Must be rejected by update-helper (bad-package).
EOF
echo 0.0.3 > "$B/pkg/usr/share/doc/astra-voice-other/VERSION"
dpkg-deb --root-owner-group -b "$B/pkg" "$FX/other/astra-voice-other_0.0.3_all.deb" >/dev/null
(cd "$FX/other" && sha256sum astra-voice-other_0.0.3_all.deb > SHA256SUMS)
sign spike-s1@example.invalid "$FX/other/SHA256SUMS" "$FX/other/SHA256SUMS.asc"
rm -rf "$B"

find "$FX" -type f | sort
