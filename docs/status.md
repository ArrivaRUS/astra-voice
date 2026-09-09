# Status — Astra Voice

> Живой лог исполнения. Засеян так, чтобы другой прогон продолжил без чтения чата.
> **Обновлено:** 2026-09-09 — execution-pack собран (architect-claude), ждёт приёмки Юрки.

## Current phase
- **Фаза 4 «Разработка» не начата**: `M0` (спайки S1–S5) `[ ]`. Первый невыполненный milestone — `docs/plans.md` § M0.
- Фаза 3 закрыта: планы #1/#2 → синтез (`arch/plan-synth.md`) → челлендж Astra (22 находки) → T1 (`docs/threat-model.md`) → G4 → execution-pack (P13).
- Гейты: G0, G1, G2a, G2, G3, G4 пройдены; следующий ⛔ — **G5 на v0.1 (30.09)**. Открытый вопрос заказчику: согласие ГК «Астра» на имя (L1).

## Done
- 2026-09-07: интейк (`PROJECT.md`), публичный репо `ArrivaRUS/astra-voice`, Discovery (5 отчётов, `research/summary.md`), G2a (имя Astra Voice, знак 06 «Слоги», лого-пак), G1 (PRD 0.2, stories, backlog), лицензия GPL-3.0-or-later.
- 2026-09-08: G2 (направление A «Панель»), G3 (макет A 48 экранов, `design/spec.md` 0.2, `design/tokens.json` 2.1.1, `design/refs/` 48 png), решения У1–У14, цифры каталога.
- 2026-09-09: `arch/plan-claude.md` (#1) и `arch/plan-codex.md` (#2); синтез `arch/plan-synth.md` (Р1–Р13, §7 22 контракта, §8 Fly); T1 `docs/threat-model.md` (40 угроз, T-01…T-36); G4 закрыт (v1.0 = `.deb` + polkit, user-bundle v1.1); Fly-адаптеры; PRD **0.5** (S17, память Р5, манифест onnx-asr, `serial`/`trust_epoch`/`revoked[]`, `trust.json`), stories 0.4 (US-6.6, US-12.5), backlog 0.4; урок `.patches/001`; допуски заказчика на спайки; execution-pack: `docs/plans.md` (M0–M11, R1–R3, T1 §5 по вехам), `docs/status.md`, `docs/test-plan.md` (T-01…T-36, S1–S17).

## In progress
- Приёмка execution-pack Юркой (полировка ≤ 2 раунда) → commit+push.

## Next
- → **M0.S1** — пилюля + трей в KDE (без sudo), затем во Fly (перелогин заказчика). Параллельно **M0.S3** (venv, ORT 1.24.4 + onnx-asr 0.12.0, три GigaAM v3 ~680 МБ, замер p95/VmHWM, отмена ≤ 1 с) — не требует ни sudo, ни Fly. **M0.S2** — после подготовки тестового пакета/помощника/`.policy` и одного `sudo` заказчика. S4/S5 — KDE сразу, Fly — при том же перелогине, что S1.
- После M0: запись решений в `decisions/log.md`, `arch/spikes.md` в git → M1 (скелет/упаковка/CI).

## Decisions (decisions/log.md)
- 2026-09-09 «Архитектура принята: синтез + челлендж Astra + T1; допуски на спайки» — execution-pack по синтезу; T1 §5 → plans (по вехам), §6 → test-plan; T2 на каждый диф `helper/`, `security/`, `updates/`, `net/`; T3 перед v0.2; пин `onnx-asr` 0.12.0 + `onnxruntime` 1.24.4; S3 ~680 МБ; S2/Fly на машине заказчика, `sudo` вводит он; ключ — офлайн-мастер у заказчика, подключ в GitHub Environment `release`.
- 2026-09-09 «G4 закрыт» — v1.0 вариант A (`.deb`, интерфейс по спеке, polkit-обновлятор); relocatable-раскладка в M1; user-bundle C1 — v1.1.
- 2026-09-09 «Fly-сессия: адаптеры» — детект по `XDG_CURRENT_DESKTOP`, тема `~/.fly/paletterc`, трей явных цветов, клип-менеджер fly-wm, лаунчер ставит `QT_QUICK_CONTROLS_STYLE`, риск R17 (блокировка интерпретаторов).
- 2026-09-09 «Требование: KDE и родной Fly» — S17, E2E в обеих сессиях; 2026-09-09 «Установка без прав администратора — мягкое требование».
- 2026-09-08 G3 · У1–У14 · цифры каталога · токены; 2026-09-07 G1 · G2a · рамки (оба трека обновления, PyQt5 из apt, пилюля по умолчанию с выключателем, `Ctrl+Space`, модель в памяти, автозапуск v0.2, ЗПС-трек после v1.0).

## Assumptions
- Полный список — `docs/plans.md` § Assumptions **A-01…A-16**: ORT 1.24.4 (факты плана #1 сняты на 1.29.0 — S3 повторяет на 1.24.4) · 3 `.so` без шима · шрифты из apt (`fonts-pt-root-ui`, `fonts-pt-mono`) · детект сессии `XDG_CURRENT_DESKTOP` с резервом по атомам root (план #1 §13.1: во Fly переменная может быть пуста) · relocatable с M1 · GPG/gpgv + пины + `trust.json` (M8) · каталог `serial`/`trust_epoch`/`revoked[]` (PRD 0.5) · requests + jsonschema за фасадом · память Р5 · отмена Р4 · маркеры/CLI создаются с функцией · микрозадачи дробит Developer · спайки на машине заказчика · меню трея системное · звуки `.ogg` (способ воспроизведения — M9).
- Путь помощника: план/синтез — `/usr/libexec/astra-voice/update-helper`; PRD 0.5 §9.2 пишет `/usr/libexec/astra-voice-update-helper` — берётся путь плана (архитектура), PRD поправить при следующей ревизии.

## Commands (copy-paste)
- lint: `ruff check src tests scripts tools && mypy --strict src && qmllint qml/**/*.qml`
- unit: `pytest -m unit -q`
- xvfb: `xvfb-run -a -s "-screen 0 1600x1000x24" pytest -m xvfb -q`
- engine: `pytest -m engine -q`
- build: `make deb && lintian dist/astra-voice_*.deb && tools/elf-audit --strict dist/astra-voice_*.deb`
- validate: `tools/validate <тема> [--session kde|fly]` · `tools/benchmark --model gigaam-v3-e2e-rnnt-int8 --runs 50 --threads 4`
- virtual mic up: `pactl load-module module-null-sink sink_name=av_test sink_properties=device.description=av_test && pactl load-module module-remap-source master=av_test.monitor source_name=av_test_mic source_properties=device.description=av_test_mic`
- e2e dictation: `xdotool keydown ctrl+space; paplay --device=av_test data/test/test-ru-6s.wav; xdotool keyup ctrl+space; astra-voice --stats`
- virtual mic down: `pactl unload-module module-remap-source; pactl unload-module module-null-sink`
- release: `scripts/release.sh vX.Y.Z && tools/validate release --version X.Y.Z`
- До M1 команд в репо нет — все помечены «создать» в `docs/plans.md` (Validation Assumptions).

## Blockers
- **S2**: нужен один `sudo` заказчика на его машине (положить `.policy`, помощник, создать `/var/lib/astra-voice/staging` 0700) — файлы и команду готовит команда, команда отката прилагается.
- **S1/S4/S5 во Fly**: нужен вход заказчика во Fly-сессию (перелогин через fly-dm); без него Fly-вердикты спайков не закрываются.
- Чистая ВМ ALSE 1.8 (сток Fly) для Ц3 и §14.5 плана #1 — не выделена; нужна к R2 (15.10).
- **Ключи подписи**: офлайн-мастер (cert-only) + подключи S1/S2 генерирует заказчик по `docs/SECURITY.md` (M1); S1 — в GitHub Environment `release`. Без этого R1 (30.09) не подписать — процедура вне кода, нужна дата.
- Согласие ГК «Астра» на имя (L1) — открытый вопрос к G5.

## Audit log
- 2026-09-09: architect-claude — execution-pack собран по синтезу: `docs/plans.md` (M0 спайки с критериями/Fly/ручными шагами заказчика, M1–M11, R1–R3, требования T1 §5 по вехам M1/M2/M4/M6/M7/M8/M0-S2/T3), `docs/status.md`, `docs/test-plan.md` (уровни, фикстуры, smoke, T-01…T-36 дословно, S1–S17 × тест, готовность v0.1/v0.2/v1.0). Код не писался, ничего не устанавливалось, GUI не запускался. Учтены PRD 0.5 / stories 0.4 / backlog 0.4 (US-6.6, US-12.5), появившиеся во время сборки.

## Smoke / demo checks
- [ ] M0: `arch/spikes.md` — 5 вердиктов; S1/S4/S5 отдельно для KDE и Fly; S3 таблица p95/VmHWM на 1.24.4.
- [ ] M1: `apt install ./dist/astra-voice_0.1.0~m1_amd64.deb` → окно 900×620 в теме сессии; `make deb` ≤ 5 мин; ELF = 3; CI зелёный.
- [ ] M2: `astra-voice --debug-transcribe data/test/test-ru-6s.wav` → «проверка связи»; отмена ≤ 1 с; `kill -9` воркера/GUI по протоколу.
- [ ] M3: виртуальный микрофон → `result`; тишина → `silent`; 0 аудиофайлов.
- [ ] M4: 50 диктовок в Kate (KDE) и `fly-term` (Fly) — p95 ≤ 0,5 с; фокус на месте; маркерная фраза нигде не осела.
- [ ] M5: чистый профиль → онбординг → скачивание → первая фраза; `tcpdump` 0 до тумблеров; секундомер Ц3.
- [ ] R1: `v0.1.0` в GitHub Releases с подписью; заказчик диктует вместо Handy; E2E-чек Fly (Should).
- [ ] M6–M9 / R2: каталог 12 карточек, ≥ 3 замера; апгрейд через одно окно polkit; автозапуск после перелогина (KDE+Fly); T3; P21½ в обеих сессиях.
- [ ] M10–M11 / R3: `profile=secure` → 0 соединений и 6 карточек; S17 A1–A9; DesignReviewer 24×2; soak 8 ч; G5.
