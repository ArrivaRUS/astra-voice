# Status — Astra Voice

> Живой лог исполнения. Засеян так, чтобы другой прогон продолжил без чтения чата.
> **Обновлено:** 2026-09-09 — M0 в работе: S3 принят, S1 идёт в KDE.

## Current phase
- **Фаза 4 «Разработка» — M0 `[~]`**: S3 принят 2026-09-09 (`arch/spikes/S3.md`), S1 идёт в KDE; S2, S4, S5 и Fly-части не начаты. Первый невыполненный milestone — `docs/plans.md` § M0.
- Фаза 3 закрыта: планы #1/#2 → синтез (`arch/plan-synth.md`) → челлендж Astra (22 находки) → T1 (`docs/threat-model.md`) → G4 → execution-pack (P13).
- Гейты: G0, G1, G2a, G2, G3, G4 пройдены; следующий ⛔ — **G5 на v0.1 (30.09)**. Открытый вопрос заказчику: согласие ГК «Астра» на имя (L1).

## Done
- 2026-09-07: интейк (`PROJECT.md`), публичный репо `ArrivaRUS/astra-voice`, Discovery (5 отчётов, `research/summary.md`), G2a (имя Astra Voice, знак 06 «Слоги», лого-пак), G1 (PRD 0.2, stories, backlog), лицензия GPL-3.0-or-later.
- 2026-09-08: G2 (направление A «Панель»), G3 (макет A 48 экранов, `design/spec.md` 0.2, `design/tokens.json` 2.1.1, `design/refs/` 48 png), решения У1–У14, цифры каталога.
- 2026-09-09: `arch/plan-claude.md` (#1) и `arch/plan-codex.md` (#2); синтез `arch/plan-synth.md` (Р1–Р13, §7 22 контракта, §8 Fly); T1 `docs/threat-model.md` (40 угроз, T-01…T-36); G4 закрыт (v1.0 = `.deb` + polkit, user-bundle v1.1); Fly-адаптеры; PRD **0.5** (S17, память Р5, манифест onnx-asr, `serial`/`trust_epoch`/`revoked[]`, `trust.json`), stories 0.4 (US-6.6, US-12.5), backlog 0.4; урок `.patches/001`; допуски заказчика на спайки; execution-pack: `docs/plans.md` (M0–M11, R1–R3, T1 §5 по вехам), `docs/status.md`, `docs/test-plan.md` (T-01…T-36, S1–S17).
- 2026-09-09: **M0.S3 принят** (`arch/spikes/S3.md`, журнал «M0.S3 принят») — ORT 1.24.4: 3 `.so`, max GLIBC_2.27, 0 isoc23, Requires-Dist 5/5 из apt; три GigaAM v3 @ `322c3b29…` 677 109 053 Б, 16 файлов с sha256; p95 e2e_rnnt 228/253/247 мс, e2e_ctc 229/153/194 мс (1/2/4 потока), VmHWM 366–404 МиБ при ≤ 2 потоках (467 при 4); отмена `terminate` 0,5–4,1 мс (патч +28 строк); `RLIMIT_AS` 512/768/2048 МиБ; дефолт `e2e_rnnt`, `SherpaEngine` не нужен; `intra_op=2`, `allow_spinning=0`, `RLIMIT_AS` 3 ГиБ, дедлайн `engine.load`; `scripts/measure_model.py` в репо.

## In progress
- **M0.S1** — живой прогон пилюли + трея в KDE на машине заказчика (ок заказчика получен); Fly-часть — после перелогина.

## Next
- → **S1-Fly** (перелогин заказчика) → **S2** (после подготовки тестового пакета/помощника/`.policy` и одного `sudo` заказчика) → **S4**, **S5** (KDE сразу, Fly — при том же перелогине) → **M1** (скелет/упаковка/CI) с параметрами S3: `intra_op=2`, `allow_spinning=0`, `RLIMIT_AS` 3 ГиБ, дедлайн `engine.load` 10 с, каталог на вариант, патч отмены в `vendor/patches/`, `tools/benchmark` из `scripts/measure_model.py`.
- После M0: запись решений в `decisions/log.md`, `arch/spikes/S*.md` в git → M1 (скелет/упаковка/CI).

## Decisions (decisions/log.md)
- 2026-09-09 «Архитектура принята: синтез + челлендж Astra + T1; допуски на спайки» — execution-pack по синтезу; T1 §5 → plans (по вехам), §6 → test-plan; T2 на каждый диф `helper/`, `security/`, `updates/`, `net/`; T3 перед v0.2; пин `onnx-asr` 0.12.0 + `onnxruntime` 1.24.4; S3 ~680 МБ; S2/Fly на машине заказчика, `sudo` вводит он; ключ — офлайн-мастер у заказчика, подключ в GitHub Environment `release`.
- 2026-09-09 «G4 закрыт» — v1.0 вариант A (`.deb`, интерфейс по спеке, polkit-обновлятор); relocatable-раскладка в M1; user-bundle C1 — v1.1.
- 2026-09-09 «Fly-сессия: адаптеры» — детект по `XDG_CURRENT_DESKTOP`, тема `~/.fly/paletterc`, трей явных цветов, клип-менеджер fly-wm, лаунчер ставит `QT_QUICK_CONTROLS_STYLE`, риск R17 (блокировка интерпретаторов).
- 2026-09-09 «Требование: KDE и родной Fly» — S17, E2E в обеих сессиях; 2026-09-09 «Установка без прав администратора — мягкое требование».
- 2026-09-08 G3 · У1–У14 · цифры каталога · токены; 2026-09-07 G1 · G2a · рамки (оба трека обновления, PyQt5 из apt, пилюля по умолчанию с выключателем, `Ctrl+Space`, модель в памяти, автозапуск v0.2, ЗПС-трек после v1.0).

## Assumptions
- Полный список — `docs/plans.md` § Assumptions **A-01…A-16**: ORT 1.24.4 (подтверждено S3) · 3 `.so` без шима · шрифты из apt (`fonts-pt-root-ui`, `fonts-pt-mono`) · детект сессии `XDG_CURRENT_DESKTOP` с резервом по атомам root (план #1 §13.1: во Fly переменная может быть пуста) · relocatable с M1 · GPG/gpgv + пины + `trust.json` (M8) · каталог `serial`/`trust_epoch`/`revoked[]` (PRD 0.5) · requests + jsonschema за фасадом · память Р5 · отмена Р4 · маркеры/CLI создаются с функцией · микрозадачи дробит Developer · спайки на машине заказчика · меню трея системное · звуки `.ogg` (способ воспроизведения — M9).
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
- 2026-09-09: architect-claude — по приёмке S3 узким диффом обновлены `docs/plans.md` (M0.S3 `[x]` → `arch/spikes/S3.md`; M0 `[~]`; M2: `intra_op=2`, `allow_spinning=0`, `RLIMIT_AS` 3 ГиБ, дедлайн `engine.load` 10 с, каталог на вариант, без 7 ресемплеров, `vendor/patches/onnx-asr-cancellable.patch`, `tools/benchmark` из `scripts/measure_model.py`; scratch `~/.cache/astra-voice-spike/`; A-01 подтверждено) и `docs/status.md` (Done/In progress/Next).
- 2026-09-09: developer (M1-A) — живой прогон DoD M1 на KDE-сессии заказчика (допуск в чате 16:40), `spikes/m1_live/` (README + 20 файлов `out/`, скриншот окна, `live_kde_summary.json`). Пакет `0.1.0~m1` из apt, схема сессии `AstraDark`. Замеры: клиентская геометрия 900×588 (внешняя 908×620), RSS/VmHWM 167 720 КБ (порог 122 880), 9 потоков, второй запуск 0,152 с код 0, старт поверх устаревших `lock`/`ipc` — успешный. Схему сессии **не менял**: шаг «живая тема» сделан подставным `kdeglobals` (детект верен: `AstraDark`→dark, `AstraLight`→light), потому что окно тему всё равно не получает. Состояние машины до = после (схема, процессы 0, runtime-каталог удалён, `~/.config/astra-voice` пуст). Ничего не коммитил.
- 2026-09-09: developer (M1-A) — по решению Юрки на отчёт Optimizer `spikes/m1_live/rss.md` GUI по умолчанию переведён на программный рендер Qt Quick: `bootstrap.py` для команды `app` (не `worker`/`helper`) до импорта Qt делает `setdefault` для `QT_QUICK_BACKEND=software` и `QT_XCB_GL_INTEGRATION=none`; переключатель `ASTRA_VOICE_RENDER=gl` возвращает аппаратный рендер, заданные пользователем значения не перебиваются. `scripts/astra-voice` больше не дублирует переменные Qt — единственный источник правды `bootstrap.py`. В `app.py` установлен `qInstallMessageHandler`: две ожидаемые строки про OpenGL уходят в журнал на уровне debug, остальные сообщения Qt — в наш журнал, а не в stderr. Замер в изолированном KWin: **software 102 544 кБ** против **gl 177 924 кБ**, окно 900×588 и тёмная тема в обоих режимах, stderr пуст, RMSE картинок 4,5 %. Тесты: unit 146 passed, xvfb 18 passed + 1 skipped (offscreen), `make lint` зелёный.

## Smoke / demo checks
- [~] M0: `arch/spikes/S*.md` — 5 вердиктов; S1/S4/S5 отдельно для KDE и Fly; S3 `[x]` (таблица p95/VmHWM на 1.24.4 — `arch/spikes/S3.md` §4).
- [~] M1: живой прогон на KDE заказчика 2026-09-09 (`spikes/m1_live/`, лог `out/live_kde_*`): `--version` и `apt install` — PASS; окно **900×588** клиентской + рамка 4/4/28/4 = **908×620** внешней, `WM_NAME`/`_NET_WM_NAME` «Astra Voice», `_KDE_NET_WM_DESKTOP_FILE=astra-voice`, `min 900×588`, журнал «сеанс=KDE политика=absent», QML-warnings 0; single-instance — код 0 за **0,15 с**, «получена команда show» в журнале, процессов 1; CI на `e03d6ae` зелёный (run 34358041504). **RSS закрыт** программным рендером (`spikes/m1_live/rss.md`): 167 740 → **102 580 кБ**, запас 16,5 % до порога 122 880. **Не закрыто:** окно светлое при схеме сессии `AstraDark` (`themeSource` не подан в QML); `show` не разворачивает свёрнутое окно; процесс переживает закрытие окна; `settings.json` не создаётся. Восемь дефектов — `spikes/m1_live/README.md`.
- [~] M2: живой прогон DoD 2026-09-11 — распознавание, отмена и `kill -9` воркера/GUI выполнены; пункт про `RLIMIT_AS` выполнен частично. Факты и расхождения — в разделе «M2. Живой прогон Definition of Done» ниже.
- [ ] M3: виртуальный микрофон → `result`; тишина → `silent`; 0 аудиофайлов.
- [ ] M4: 50 диктовок в Kate (KDE) и `fly-term` (Fly) — p95 ≤ 0,5 с; фокус на месте; маркерная фраза нигде не осела.
- [ ] M5: чистый профиль → онбординг → скачивание → первая фраза; `tcpdump` 0 до тумблеров; секундомер Ц3.
- [ ] R1: `v0.1.0` в GitHub Releases с подписью; заказчик диктует вместо Handy; E2E-чек Fly (Should).
- [ ] M6–M9 / R2: каталог 12 карточек, ≥ 3 замера; апгрейд через одно окно polkit; автозапуск после перелогина (KDE+Fly); T3; P21½ в обеих сессиях.
- [ ] M10–M11 / R3: `profile=secure` → 0 соединений и 6 карточек; S17 A1–A9; DesignReviewer 24×2; soak 8 ч; G5.

## M2. Живой прогон Definition of Done

**2026-09-11, машина заказчика, офлайн.** Изолированное окружение: свои `XDG_RUNTIME_DIR`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, `XDG_CACHE_HOME` во временном каталоге; `QT_QPA_PLATFORM=offscreen`, дисплей `:0`. D-Bus и микрофон не затрагивались.

Модель: `gigaam-v3-e2e-rnnt` int8 из `~/.cache/astra-voice-spike/gigaam-v3/e2e_rnnt`. Интерпретатор: `~/.cache/astra-voice-dev/venv-e` (`onnxruntime` 1.24.4, `onnx-asr` 0.12.0). Результаты получены живым прогоном агента-разработчика 2026-09-11 на машине заказчика в изолированном окружении. Сырые логи каждого сценария лежат в `spikes/m2_live/out/`: `01-debug-transcribe.log`, `02-serve.log`, `02-kill-worker.log`, `03-kill-gui.log`, `04-worker-cancel.log`, `05-rlimit-load.log`, `05-rlimit-load-full.log`, `06-cpu-wall.log`, `07-soak.log`.

### Факты прогона

| пункт DoD | факт | вердикт |
| --- | --- | --- |
| CLI: распознавание файла | `--debug-transcribe data/test/test-ru-6s.wav --model-dir <модель> --variant gigaam-v3-e2e-rnnt --threads 2`: напечатано «Проверка связи. Проверка связи. Проверка связи.», `t_ms=157`, полный цикл процесса 1,108 с, код возврата 0. Контрольный прогон оркестратора: 206 мс, 1,14 с. | Выполнено. |
| `kill -9` воркера: рестарт, перезагрузка модели, одна ошибка; `oom_score_adj` | У живого воркера `oom_score_adj` = 500. Новый pid поднялся не позднее чем через 0,0033 с при пороге ≤ 1 с. Модель перезагружена: `load_ms=719,7` в новом поколении 2. Событий `error` наружу ровно одно: `worker-crashed` «Распознавание перезапущено». | Выполнено. |
| `kill -9` процесса-GUI: завершение воркера (У36 / T-34) | Воркер исчез за 0,052 с при пороге ≤ 1 с; осиротевших воркеров 0. | Выполнено. |
| Отмена ≤ 1000 мс | Создан `tools/validate`, подкоманда `worker-cancel`. От `send(record.cancel)` до события `cancelled` — 0,419 мс, код 0. Способ: `transcribe.file` на `data/test/test-ru-20s.wav`. Для сравнения S3 давал 0,5–4 мс. | Выполнено. |
| `RLIMIT_AS`, `threads=4`, дедлайн загрузки 10 с | 768 МиБ (значение из плана) — модель загрузилась за 0,708 с, дедлайн не сработал; 700 МиБ — молчаливое зависание, дедлайн выдал `error{code=load-timeout}` за 10,0005 с; 640 МиБ — модель загрузилась за 0,707 с; 512 МиБ — `error{code=engine-failed}` за 0,184 с; 384 МиБ — `error{code=engine-failed}` за 0,124 с; 256 МиБ — `error{code=engine-failed}` за 0,0067 с. | Частично. Смысл требования «не зависает, отказ виден» выполнен на всех пределах. Ошибка по дедлайну на 768 МиБ не воспроизводится; на 700 МиБ механизм доказан сквозным сценарием. |
| `cpu/wall` ≤ 2 при `threads=2` | `cpu_ms=880,0`, `wall_ms=751,4`, отношение 1,171. Загрузка модели 703,8 мс. Движок: `onnx-asr` 0.12.0 / `onnxruntime` 1.24.4, режим отмены `fallback-wrapper`. Интервал включает чтение WAV и доставку результата по IPC, а не только инференс. | Выполнено. |
| Долгий сценарий: память и число сессий | 20 подряд распознаваний 6-секундного файла в одном воркере: `VmHWM` 392212 → 412844 кБ (прирост 20632 кБ), `Pss` 378781 → 399350 кБ, `sessions` 3 → 3 (ожидание 3). `sessions` — снимок `LoadResult` последней загрузки, а не обход живых объектов. Очистка буферов PCM снаружи не наблюдаема и покрыта unit-тестом `test_soak_releases_pcm`. | В снимке число сессий соответствует ожиданию. Порог прироста памяти в плане не задан; вердикт по нему не выставлен. |
| Время запроса в долгом сценарии | `t_ms`: медиана 156 мс, p95 207 мс. Это время всего запроса, не чистого инференса. | Факт записан; пункт DoD про p95 инференса этим замером не подтверждён. |
| Холодная загрузка: уточнённый критерий | По решению от 2026-09-11 «M2: DoD по холодной загрузке исправлен по факту; отмена работает и без вендорного патча» в [журнале решений](../decisions/log.md) новый критерий — 3 `InferenceSession` вместо 10 и холодная загрузка ≤ 1,0 с. Старый пункт плана «холодная загрузка ≤ 0,3 с быстрее S3» снят как ошибочный: расчёт исходил из затрат на 7 сессий ресемплера, а замер показал, что загрузку держит энкодер. Четыре замера: 693,5 мс, 700,9 мс, 703,8 мс и 719,7 мс (последний — перезагрузка после `kill -9`); число сессий 3. Новый критерий выполнен с запасом. | выполнено (по уточнённому критерию) |
| Сборочные проверки | `make lint` зелёный: ruff, ruff format 84 файла, mypy 50 файлов. `make test`: 504 passed, 25 deselected. `pytest -m engine`: 6 passed (тесты зоны B). | Проверки зелёные. |
| T-21 и T-34 | T-21 — зелёный: `tests/unit/test_ipc.py`, 83 теста, плюс негативные сценарии супервизора: кадр 2 ГиБ, чужой `utterance_id`, `text` 50 МБ. T-34 подтверждён живым замером 0,052 с при уничтожении GUI. | Выполнено. |
| T-10 и T-11 | Зона B; в этот живой прогон не входили. | Вердикт этого прогона не выставлен. |

### Расхождения с планом и оговорки

**`RLIMIT_AS` 768 МиБ.** Буквальная формулировка плана — «под 768 МиБ при 4 потоках завершается ошибкой по дедлайну» — на этой ревизии **не воспроизводится**. После того как зона B убрала лишние сессии ресемплера (3 `InferenceSession` вместо 10), потребление упало и 768 МиБ модели теперь хватает. Формулировку плана надо уточнить по факту: предел, на котором воспроизводится зависание, сместился вниз. Риск S3-R1 (молчаливое зависание) воспроизведён на 700 МиБ; именно там доказана работа дедлайна сквозным сценарием. Поведение немонотонно по пределу: 700 МиБ — зависание, 640 МиБ — загрузка. Граница узкая, исчерпывающий поиск не проводился.

**Команда Validation.** `pytest -m unit tests/worker tests/unit/test_supervisor.py` ссылается на каталог `tests/worker`, которого нет. Unit-тесты воркера лежат в `tests/unit/`: `test_ipc.py`, `test_worker_state.py`, `test_worker_main.py`, `test_supervisor.py`. Команда в плане оставлена без изменения.

**CLI и p95.** `astra-voice --debug-transcribe` готов и прогнан; `tools/validate worker-cancel` создан и прогнан. `tools/benchmark` в этой вехе не создавался — это зона движка (зона B), поэтому маркер CLI остаётся `[~]`. P95 инференса зона B записала в `arch/spikes/S3.md`. P95 этого прогона, 207 мс, — время всего запроса через IPC; напрямую сравнивать его с p95 чистого инференса нельзя. Пункт про выгрузку по простою также остаётся `[~]`: `ping` и `model.unload` есть, таймер в GUI относится к M4.

**«Ошибка показана один раз».** Стенд изначально ожидал событие `error` со штампом старого поколения, а реализация штампует его новым поколением. Требование DoD выполнено: наружу поступило ровно одно событие. Штамп новым поколением безопаснее: GUI, фильтрующий события по текущему поколению, такую ошибку не отбросит.

**Долгоживущий GUI для M4.** Путь с загруженной моделью: `spikes/m2_live/dod.py serve --model-dir <каталог> --variant <вариант> --threads 2` под своим XDG-окружением и `QT_QPA_PLATFORM=offscreen`. Он держит супервизор, печатает pid воркера, факты перезапуска и каждое событие `error`.

**Предупреждение об отмене.** На каждом прогоне движок пишет в журнал, что отмена работает через обходной путь (`fallback-wrapper`). Вендорный патч `cancellable()` в dev-дереве не применён. Это ожидаемо и относится к зоне B / сборке пакета.
