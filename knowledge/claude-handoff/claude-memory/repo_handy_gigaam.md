---
name: repo-handy-gigaam
description: "Приватное репо ArrivaRUS/handy-gigaam — сборка Handy с GigaAM v3 RNN-T одной командой на новой машине"
metadata:
  type: reference
---

**`ArrivaRUS/handy-gigaam`** (приватное, создано 2026-09-04) — самодостаточный репозиторий, чтобы на любой новой машине собрать и поставить Handy с русской моделью GigaAM v3 RNN-T: `git clone` → `./install.sh` (10–20 мин, качает 222 МБ модели, просит sudo для apt и dpkg). Локальная копия — `~/Документы/handy-gigaam`.

Исходники **вендорены целиком** (не патчами): `vendor/Handy` (форк 0.8.3) и `vendor/transcribe-rs` (форк с движком `gigaam_rnnt`) — обязаны лежать рядом, т.к. Cargo.toml подключает движок через `[patch.crates-io]` путём `../../transcribe-rs`. Модель не в git: ревизия HF и sha256 закреплены в `model/`, качает шаг 20. Пакет собирается из шаблона `packaging/deb` (снят с оригинального deb) — tauri CLI намеренно не используется.

Шаги в `scripts/` (10-deps, 20-model, 30-build, 40-package, 50-install, 60-verify), запускаются и по отдельности: `./install.sh 30 40 50`. Шаг 60 — настоящая проверка: распознаёт `vendor/transcribe-rs/samples/russian.wav` и сверяет с эталоном «Проверка связи» (~35 мс).

`CLAUDE.md` в корне — инструкция агенту на другой машине (что делать, чего не делать), `docs/TROUBLESHOOTING.md` — все грабли (isoc23-шим, обязательный `--features tauri/custom-protocol`, хук postinstall с bun, VAD и «sample count: 0», гонка WirePlumber на Astra, старый `/usr/bin/handy`).

**Грабли, найденные при упаковке репо (обе — только живым прогоном, глазами не видны):**
1. `npm ci` падает с кодом 127 — postinstall в package.json зовёт `bun scripts/check-nix-deps.ts` (синхронизация для Nix), bun обычно не установлен. Шаг 30 отключает хук на время установки пакетов.
2. `strings <45МБ бинарь> | grep -q` при `set -o pipefail` ложно проваливает проверку: grep -q закрывает конвейер, strings получает SIGPIPE. Считать через `grep -c ... || true`. **Правило:** в скриптах с pipefail не использовать `grep -q` после команды с большим выводом.
3. Проверять сборку по наличию строки `gigaam-v3-rnnt` бесполезно — она есть и в СЛОМАННОЙ июньской сборке. Единственная надёжная проверка установленного бинаря — побайтовое сравнение с собранным (`cmp -s`), а движка — реальное распознавание `samples/russian.wav`.

Связано: [[handy-custom-build]], [[stt-engine-gigaam]].
