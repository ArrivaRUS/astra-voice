#!/bin/sh
# S2 (M0) — единственный шаг спайка, который требует sudo. Выполняет заказчик.
#
#   sudo sh "/home/astra/Документы/astra-voice/spikes/s2_polkit_helper/install-spike.sh"
#
# Что делает (ровно четыре файла + один тестовый пакет, всё перечислено в README):
#   1. /usr/libexec/astra-voice/update-helper                       root:root 0755
#   2. /usr/share/polkit-1/actions/io.github.arrivarus.astra_voice.update.policy  root:root 0644
#   3. /usr/share/astra-voice/keys/test-release.gpg                 root:root 0644
#   4. /var/lib/astra-voice/staging                                 root:root 0700
#   5. apt-get install ./astra-voice-spike_0.0.1_all.deb            (тестовый пакет)
# Откат — uninstall-spike.sh рядом.
set -eu

SRC=$(cd "$(dirname "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "нужен root: sudo sh $0" >&2
    exit 1
fi

echo "== 1/5 помощник =="
install -d -m 0755 -o root -g root /usr/libexec/astra-voice
install -m 0755 -o root -g root "$SRC/update-helper" /usr/libexec/astra-voice/update-helper

echo "== 2/5 polkit-действие =="
install -m 0644 -o root -g root \
    "$SRC/io.github.arrivarus.astra_voice.update.policy" \
    /usr/share/polkit-1/actions/io.github.arrivarus.astra_voice.update.policy

echo "== 3/5 keyring =="
install -d -m 0755 -o root -g root /usr/share/astra-voice/keys
install -m 0644 -o root -g root "$SRC/keys/test-release.gpg" \
    /usr/share/astra-voice/keys/test-release.gpg

echo "== 4/5 staging =="
install -d -m 0755 -o root -g root /var/lib/astra-voice
install -d -m 0700 -o root -g root /var/lib/astra-voice/staging

echo "== 5/5 тестовый пакет 0.0.1 =="
cd "$SRC/packages"
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-remove ./astra-voice-spike_0.0.1_all.deb

echo
echo "== проверка =="
dpkg-query -W astra-voice-spike
ls -l /usr/libexec/astra-voice/update-helper \
      /usr/share/polkit-1/actions/io.github.arrivarus.astra_voice.update.policy \
      /usr/share/astra-voice/keys/test-release.gpg
ls -ld /var/lib/astra-voice/staging
echo
echo "Готово. Откат одной командой:"
echo "  sudo sh \"$SRC/uninstall-spike.sh\""
