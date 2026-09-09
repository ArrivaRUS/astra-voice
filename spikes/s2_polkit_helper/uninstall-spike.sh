#!/bin/sh
# S2 (M0) — полный откат спайка. Выполняет заказчик:
#
#   sudo sh "/home/astra/Документы/astra-voice/spikes/s2_polkit_helper/uninstall-spike.sh"
#
# Удаляет ровно то, что положил install-spike.sh: тестовый пакет, помощник,
# polkit-действие, keyring, staging и журнал. Ничего больше система не получала.
set -eu

if [ "$(id -u)" -ne 0 ]; then
    echo "нужен root: sudo sh $0" >&2
    exit 1
fi

echo "== 1/4 тестовый пакет =="
if dpkg-query -W astra-voice-spike >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get purge -y astra-voice-spike
else
    echo "astra-voice-spike не установлен"
fi

echo "== 2/4 помощник и polkit-действие =="
rm -f /usr/libexec/astra-voice/update-helper
rm -f /usr/share/polkit-1/actions/io.github.arrivarus.astra_voice.update.policy
rmdir --ignore-fail-on-non-empty /usr/libexec/astra-voice 2>/dev/null || true

echo "== 3/4 keyring =="
rm -f /usr/share/astra-voice/keys/test-release.gpg
rmdir --ignore-fail-on-non-empty /usr/share/astra-voice/keys /usr/share/astra-voice 2>/dev/null || true

echo "== 4/4 staging и журнал =="
rm -rf /var/lib/astra-voice/staging
rm -f /var/lib/astra-voice/updates.log /var/lib/astra-voice/update.lock
rmdir --ignore-fail-on-non-empty /var/lib/astra-voice 2>/dev/null || true

echo
echo "== проверка (всё должно отсутствовать) =="
for p in /usr/libexec/astra-voice/update-helper \
         /usr/share/polkit-1/actions/io.github.arrivarus.astra_voice.update.policy \
         /usr/share/astra-voice/keys/test-release.gpg \
         /var/lib/astra-voice; do
    if [ -e "$p" ]; then echo "ОСТАЛОСЬ: $p"; else echo "нет: $p"; fi
done
dpkg-query -W astra-voice-spike 2>/dev/null || echo "нет: пакет astra-voice-spike"
