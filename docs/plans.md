# Plans — Astra Voice (execution-pack)

> Источник правды по сборке. Собран по скиллу `execution-pack` (P13) после принятого синтеза архитектуры.
> Доски/issues — зеркала, не канон. Статусы инспектируемые: `[ ]` не начато · `[~]` в работе · `[x]` сделано.
> **Обновлено:** 2026-09-09 · architect-claude (Fable 5.1) · приёмка — Юрка. Правки — узкими диффами, аудит в `status.md`.

## Source
- Task: голосовой ввод для ALSE 1.8 (KDE Plasma 5.27 ∥ родной Fly, X11): хоткей → речь → текст в активном окне; каталог моделей с честными цифрами, обновления модели и утилиты из GUI, автозапуск; один `.deb`.
- Canonical input: `PRD.md` **0.5** (ревизия 5: `serial`/`trust_epoch`/`revoked[]`, `trust.json`, изоляция процессов; Ц1–Ц7, F1–F16, S1–S17) · `stories.md` 0.4 (82 истории; новые US-6.6, US-12.5) · `backlog.md` 0.4 (v0.1 30.09 → v0.2 15.10 → v1.0 31.10; v1.1 ГОСТ/user-bundle).
- Architecture: `arch/plan-synth.md` (суд: §3 Р1–Р13, §7 контракты И1–И8/Н1–Н5/Д1–Д4/О1–О5, §8 Fly) ← база `arch/plan-claude.md` (§2–§10 компоненты/потоки/вехи, §13 Fly-адаптеры, §14 G4) + заимствования `arch/plan-codex.md` (CLI `tools/validate`/`tools/benchmark`, ABI-гейты).
- Security: `docs/threat-model.md` (T1 ✅; §4 ответы; **§5 требования по вехам = обязательные критерии DoD**; §6 T-01…T-36 → `docs/test-plan.md`).
- Design: `design/spec.md` 0.2 · `design/tokens.json` 2.1.1 · `design/refs/*.png` (карта «экран → референс» — спека §14).
- Decisions: `decisions/log.md` — 2026-09-09 «Архитектура принята; допуски на спайки», «G4 закрыт», «Fly-сессия: адаптеры», «KDE и родной Fly»; 2026-09-08 G3/У1–У14/цифры/токены; 2026-09-07 G1/G2a/рамки/лицензия.
- Repo area: `ArrivaRUS/astra-voice` (`main`); структура §7.1 плана #1 — `src/astra_voice/{app.py,core,platform,ui,worker,models,net,updates,security,helper,diag,tools}`, `qml/`, `data/`, `packaging/`, `scripts/`, `tools/`, `tests/{unit,integration,e2e}`, `docs/`. Кода в репо пока нет.
- Last updated: 2026-09-09.

## Assumptions
- **A-01 Рантайм.** `onnx-asr` 0.12.0 + `onnxruntime` **1.24.4** (синтез Р3/Н4: 1.29.0 требует `protobuf ≥ 4.25.8`, в apt Astra — 3.21.12; у 1.24.4 все Requires-Dist закрываются apt: `python3-protobuf` 3.21.12, `python3-flatbuffers` 2.0.8, `python3-sympy` 1.11.1, `python3-numpy` 1.24.2 ≥ 1.21.6; `onnx-asr` исключает только 1.24.1). Пины sha256 — `packaging/wheels.lock`. **Подтверждено S3 на 1.24.4** (`arch/spikes/S3.md` §1: 3 `.so`, max GLIBC_2.27, 0 isoc23, Requires-Dist 5/5 из apt; `sympy`/`protobuf`/`flatbuffers` у ORT ленивые — путь инференса их не импортирует, в M1 решить Depends vs Suggests). Апгрейд до 1.29.x — только вместе с легальным protobuf 4.x.
- **A-02 ELF.** Ровно **3 `.so`** в пакете (все — onnxruntime); шим `__isoc23_*` **не нужен** (1.24.4: max `GLIBC_2.27`, 0 ссылок `isoc23`); CI-гейт: `objdump -T` (max ≤ GLIBC_2.36, 0 isoc23) + счётчик ELF = 3 останавливает сборку (Р2).
- **A-03 Шрифты из apt.** **Recommends** (не Depends) `fonts-pt-root-ui` 1.001, `fonts-pt-mono` 1.003 (repository-main, имена сверены `apt-cache policy` 09.09): на ALSE apt ставит Recommends по умолчанию, в чистом debian:12 их нет и установка не должна падать; в пакет не вкладывать; резерв PT Astra Sans/Fact через `QFontDatabase` (Р8).
- **A-04 Детект сессии.** По `XDG_CURRENT_DESKTOP` (решение 2026-09-09, синтез §8). Оговорка: план #1 §13.1 зафиксировал, что Fly-сессия может **не экспортировать** `XDG_CURRENT_DESKTOP` (только `DESKTOP_SESSION=fly`) → S1-Fly проверяет; резерв — атомы root-окна (`_FLY_WM_PID` → FLY, `_NET_WM_NAME=KWin` → KDE, иначе OTHER), env — подсказка. Одно приложение, никаких `OnlyShowIn`; различия за интерфейсами `SessionKind`/`ThemeSource`/`TrayIconProvider`/`ShortcutConflictSource`/ветки `paste.py`.
- **A-05 Relocatable-раскладка с M1** под будущий user-bundle (G4): ресурсы относительно `__file__`, единый `/usr/lib/astra-voice/bootstrap.py` (И7), вендор в `/usr/lib/astra-voice/vendor`; цель `make user-bundle` объявляется в M1 **без сборки**; user-bundle C1 и ГОСТ-подпись — v1.1.
- **A-06 Подпись.** GPG Ed25519; `gpgv --status-fd` с пином fingerprint (мастер + S1 + заранее S2), `REVOKED_FINGERPRINTS` в коде; офлайн-мастер у заказчика, signing-subkey S1 в GitHub Environment `release`; `trust.json` от мастера — M8 (T1 §4.2); `docs/SECURITY.md` — M1.
- **A-07 Каталог.** `manifest_version` 1 + `serial`/`trust_epoch`/`revoked[]`/`generated_at` в подписанном JSON (T1 §4.3). PRD 0.5 §7.3 уже содержит их (анти-откат `(trust_epoch, serial)`, `catalog-state.json` с `last_attempt_at`/`last_success_at`, `revoked[]`); реализация — US-6.6 в M6; `trust.json` и импорт «Сеть и обновления → Доверенные ключи» — US-12.5 в M8.
- **A-08 Сеть/схема.** `python3-requests` (Р6) с `trust_env=False`; `python3-jsonschema` за фасадом `models/schema.py` (чтобы user-bundle v1.1 мог заменить ручной проверкой); все Depends — из repository-main Astra 1.8.
- **A-09 Память.** Воркер ≤ 450 МБ, суммарно ≤ 600 МБ — для модели по умолчанию (Н3); смена модели — временный воркер только при `MemAvailable ≥ min_ram_mb(кандидата) + 200 МБ`, иначе отказ с объяснением + «Переключить с паузой»; автоматической выгрузки нет (Р5/Н1/Н2).
- **A-10 Отмена.** `RunOptions.terminate` + точки между сегментами ≤ 1 с (Р4/И1); kill+reload — только аварийное восстановление.
- **A-11 Тесты и CLI.** Маркеры pytest `unit · xvfb · engine · machine`; `tools/validate <тема>` и `tools/benchmark` создаются **вместе с функцией** (Р11) — до этого команды в вехах помечены как «создать»; имена тем — по плану #2.
- **A-12 Микрозадачи.** Ниже — атомарные шаги (один модуль/функция/тест); Developer при исполнении дробит до < 2 мин и ставит статусы в этом файле; каждая задача несёт истории US-x.y и компонент.
- **A-13 Спайки.** S3 качает три варианта GigaAM v3 (~680 МБ) во временный venv (допуск заказчика 2026-09-09); S2 и Fly-части S1/S4/S5 — на машине заказчика, `sudo` вводит он сам по подготовленной команде; чистая ВМ ALSE 1.8 (сток Fly) нужна для Ц3 и §14.5 — пока не выделена.
- **A-14 Меню трея** рисует Plasma/fly-wm (DBusMenu) — DesignReviewer сверяет состав и неактивность пунктов, не стиль (Р10).
- **A-15 Звуки У14.** Файлы `.ogg` в пакете (спека §13.2); воспроизведение — `paplay` (есть в стоке) либо PCM через `pa_simple_write` из предекодированного wav — **решается в M9**; новых Depends не добавлять.
- **A-16 Даты.** M0 — до 13.09 (синтез); R1 30.09, R2 15.10, R3 31.10 (G1). Вехи — по зависимостям, даты — рамки, не порядок.

## Validation Assumptions
Команд в репо ещё нет; ниже — контракт, который M1 обязан сделать реальным (copy-paste после M1).
```sh
# статический контроль
ruff check src tests scripts tools && ruff format --check src tests scripts tools
mypy --strict src
qmllint qml/**/*.qml
# тесты по маркерам
pytest -m unit -q                                                # CI, без дисплея
xvfb-run -a -s "-screen 0 1600x1000x24" pytest -m xvfb -q        # CI, QT_QPA_PLATFORM=xcb
pytest -m engine -q                                              # CI, кэш модели по ревизии (~/.cache/astra-voice-ci/)
pytest -m machine -q                                             # только ALSE (KDE/Fly), не в CI
# сборка и пакет
make deb                                                         # dist/astra-voice_X.Y.Z_amd64.deb + lintian + ELF=3 + objdump + METADATA↔apt
tools/elf-audit --strict dist/astra-voice_*.deb
make user-bundle                                                 # цель объявлена в M1; сборка — v1.1
# CLI валидации (создаются вместе с функцией — Р11)
tools/validate <тема> [--session kde|fly]   # x11-pill · runtime · worker-cancel · dictation --virtual-mic · updater · catalog · downloads --faults
                                            # · model-updates · autostart · privacy --network-denied · policy --matrix · corporate --no-public-egress
                                            # · diagnostics --redaction · release --version X.Y.Z · e2e --suite v0.1|v0.2|s17|all · soak --hours N
tools/benchmark --model gigaam-v3-e2e-rnnt-int8 --runs 50 --threads 2 --json results/m2-6s.json    # p95 ≈ S3 (253 мс), cpu/wall ≤ 2
python3 scripts/measure_model.py --model-dir ~/.cache/astra-voice-spike/gigaam-v3/e2e_rnnt --model gigaam-v3-e2e-rnnt --wav data/test/test-ru-6s.wav --runs 1 --threads 4 --rlimit-as-mb 768 --load-deadline-s 10   # error load-timeout, не зависание
astra-voice --debug-sessions                                                    # InferenceSession в воркере: 3
# виртуальный микрофон (машина, PipeWire-pulse)
pactl load-module module-null-sink sink_name=av_test sink_properties=device.description=av_test
pactl load-module module-remap-source master=av_test.monitor source_name=av_test_mic source_properties=device.description=av_test_mic
paplay --device=av_test data/test/test-ru-6s.wav
```

## Milestone Order
| ID | Title | Depends on | Release | Status |
| --- | --- | --- | --- | --- |
| M0 | Спайки S1–S5 (KDE **и** Fly), до 13.09 | — | — | [~] S3 `[x]` |
| M1 | Скелет, упаковка, CI (+T1 §5 M1) | M0: S3 (пины), S1 (окно/тема/сессия) | v0.1 | [ ] |
| M2 | Воркер и движок (+T1 §5 M2) | M1, S3 | v0.1 | [ ] |
| M3 | Звук | M2, S5 | v0.1 | [ ] |
| M4 | Цикл диктовки: хоткей · пилюля · трей · вставка (+T1 §5 M4 ×2, Fly-ветки) | M3, S1, S4 | v0.1 | [ ] |
| M5 | Онбординг и первая модель | M4 | v0.1 | [ ] |
| R1 | **Релиз v0.1 «Диктовка работает» — 30.09** (⛔ G5) | M5 | v0.1 | [ ] |
| M6 | Каталог (+T1 §5 M6) | R1 | v0.2 | [ ] |
| M7 | Сеть и проверки (+T1 §5 M7) | M6 | v0.2 | [ ] |
| M8 | Обновлятор, трек A (+T1 §5 M8, T2) | M7, S2 | v0.2 | [ ] |
| M9 | Автозапуск, уведомления, звуки, разделы, живая тема KDE/Fly | M4, M7 (M8 не блокирует) | v0.2 | [ ] |
| R2 | **Релиз v0.2 — 15.10** (⛔ T3 до тега, ⛔ G5, P21½ в обеих сессиях) | M8, M9 | v0.2 | [ ] |
| M10 | Корпоративный контур, трек B, Fly-адаптеры (S17) | R2 | v1.0 | [ ] |
| M11 | Полировка (i18n, У13, отладка, DesignReviewer 24×2, soak) | M10 | v1.0 | [ ] |
| R3 | **Релиз v1.0 — 31.10** (⛔ G5) | M11 | v1.0 | [ ] |
| v1.1 | ГОСТ-подпись 3 `.so` + ВМ с ЗПС (Ц5) · user-bundle C1 | R3 | v1.1 | [ ] |

## M0. Спайки S1–S5 (KDE и Fly) `[~]` — до 13.09 (S3 принят 2026-09-09)
### Goal
Пять допущений архитектуры подтверждены или опровергнуты фактами на машине заказчика **в обеих сессиях** до первой строки продакшн-кода; S3 закрывает развилку рантайма (порог p95), S2 доказывает один polkit-диалог. Результаты — `arch/spikes/<S>.md` (образец — `arch/spikes/S3.md`), решения — `decisions/log.md`.
### Tasks
- [~] Каркас: отчёты `arch/spikes/<S>.md` (спайк · критерий · факт · сессия · вердикт · дата; S3 — образец), scratch `~/.cache/astra-voice-spike/` (вне репо, единое имя); `data/test/test-ru-6s.wav` («Проверка связи, запятая, всё работает точка», 16 кГц mono) и `test-ru-20s.wav` — **записать заказчику**: в S3 использованы синтетические повторы эталона из handy-gigaam (лежат в scratch, не в репо).
- **S1 пилюля + трей** (US-4.1, US-4.3; `ui/pill.py`, `ui/tray.py`, `platform/x11.py`, `platform/session.py`; синтез Р4/Р5/Р7, §8 Fly; T1 О4)
  - [~] KDE: QML `Window` Tool·Frameless·StaysOnTop·DoesNotAcceptFocus + EWMH руками (`_NET_WM_STATE_ABOVE/SKIP_TASKBAR/SKIP_PAGER`, `_NET_WM_USER_TIME=0`, тип NOTIFICATION) — критерий: `xdotool getactivewindow` не меняется; `xprop` содержит ABOVE, SKIP_TASKBAR, SKIP_PAGER; нет в Alt+Tab; показ ≤ 100 мс; над панелью не лежит (Р7: `availableGeometry` ↔ struts при панели > 48 px и боковой панели). — **живой KDE ✅** (`arch/spikes/S1.md`: фокус не украден, показ 46 мс, Р7 `0,0,1920,1148` совпал); **QML не проверен** — нет `python3-pyqt5.qtquick`, прототип на QWidget.
  - [ ] Fly (перелогин заказчика): то же + атомы `_FLY_WM_WINDOW_MAP_ANIMATION=0`/`_FLY_WM_FADE_SHOW=0`, `CenterWindowPos`, compton вкл/выкл (план #1 §13.2 п.1–3); детект сессии A-04 — что реально в `XDG_CURRENT_DESKTOP`, есть ли `_FLY_WM_PID`.
  - [~] Трей: KDE — `QSystemTrayIcon` по имени из hicolor с перекраской; Fly — SNI-хост fly-wm, иконка явных цветов, DBusMenu, левый клик → 6 состояний различимы на панели, меню открывается (состав спеки §9.2). — **живой KDE: SNI зарегистрирован, `IconName` меняется, `Menu=/MenuBar` ✅; имя иконки FAIL** (KIconLoader отдаёт звезду Astra Linux; лечение — префикс `astravoice-tray-*`, `arch/spikes/S1.md`); Fly ждёт перелогина.
  - [ ] Уведомление с кнопкой через Plasma и `fly-notifications` (`GetCapabilities`, `ActionInvoked`); таймаут 1 с → баннер.
  - [ ] Провал → override-redirect (`BypassWindowManagerHint`) как режим «совместимость индикатора»; иконка-пиксмап; во Fly — XEmbed-фолбэк Qt. Руками заказчик: только вход во Fly.
- **S2 polkit-помощник** (US-7.2, US-12.5; `helper/update_helper.py`, `updates/app_updater.py`; синтез Р8, §7 И8/Н5; T1 §4.1, §5 «M0 спайк S2»)
  - [x] Тестовый пакет `astra-voice-spike` 0.0.1 → 0.0.2 (`dpkg-deb -b`), помощник-скелет (`#!/usr/bin/python3 -IS`, stdlib only, `os.environ.clear()`, argv `install <абс.путь>`, копия через fd в staging 0700), `.policy` `auth_admin` без `_keep`, `exec.path` = помощник.
  - [x] **Заказчик вводит `sudo` один раз** по подготовленной команде: `.policy` → `/usr/share/polkit-1/actions/`, помощник → `/usr/libexec/astra-voice/`, `mkdir -m 0700 /var/lib/astra-voice/staging`; команда отката записана рядом.
  - [x] Из `QProcess` GUI-процесса `pkexec /usr/libexec/astra-voice/update-helper install <deb>` — критерий: **ровно одно** окно пароля (KDE-агент; во Fly тот же агент по `OnlyShowIn=…fly;` — S17-A5), коды 0/126/127 различимы, `dpkg-query -W` = 0.0.2, GUI не блокируется. — **живой KDE 2026-09-09 ✅** (`arch/spikes/S2.md`): ровно **одно** окно polkit (одновременно max 1, уникальных 1), `exit=0`, JSON `result=ok`, `dpkg-query` = **0.0.2**, от нажатия до установки 5 с. Коды 126/127 живьём не вызывались (второй диалог заказчику) — за юнит-тестами.
  - [~] Подтвердить (T1 §5): `apt-get install -y ./x.deb` ставит локальный пакет **без** `--allow-unauthenticated`; удержанный `lock-frontend` → `apt-locked`, apt не убит (T-18); `-IS` — stdlib грузится без `site`; `--no-download` при offline. — **apt без `--allow-unauthenticated` ✅** (`run_apt` строит фиксированный argv, установка прошла), `-IS` ✅; **`apt-locked` и `--no-download` живьём не проверены** (lock держит root, offline-ветка требует `policy.conf`) — покрыты юнит-тестами, но **`pytest` в системе и venv отсутствует** (S2-R4).
  - [ ] Провал → `pkcon install-local` как запасной путь; нет агента → трек B + подсказка `sudo apt install ./…`.
- [x] **S3 рантайм + замер** — **принят 2026-09-09** (`arch/spikes/S3.md`; журнал «M0.S3 принят») (US-2.5, US-5.4; `worker/engine.py`, `worker/measure.py`, `scripts/measure_model.py`, `scripts/build_manifest.py`; синтез Р2/Р3/Р4, §7 И1/Д1)
  - [x] venv в scratch с `--system-site-packages`: `onnxruntime==1.24.4`, `onnx-asr==0.12.0` (`--no-deps`; numpy 1.24.2/sympy/protobuf/flatbuffers — системные); `import onnxruntime, onnx_asr` ok; `objdump -T` по 3 `.so`: max ≤ GLIBC_2.36, 0 `__isoc23_*`; счётчик ELF = 3.
  - [x] Скачать `istupakov/gigaam-v3-onnx` — e2e_rnnt, e2e_ctc, rnnt (677 109 053 Б, ревизия `322c3b29…`, 16 файлов с sha256 — S3 §2; допуск 2026-09-09); зафиксировать точные `files[]` (имена, размеры, sha256: LFS — из HF API, не-LFS — считать) для `layout=onnx-asr-gigaam-v3`.
  - [x] `scripts/measure_model.py` (в репо): p95 e2e_rnnt 228/253/247 мс, e2e_ctc 229/153/194 мс при 1/2/4 потоках; VmHWM 366–404 МиБ при ≤ 2 потоках (467 МиБ при 4 — выше порога 450 МБ) → **порог выполнен у обоих; RSS — при `intra_op ≤ 2`**.
  - [x] Отмена: `RunOptions.terminate` на 20-с фразе → возврат **0,5–4,1 мс**, модель исправна после отмены (патч onnx-asr +28 строк, S3 §5); `RLIMIT_AS`-порог 512/768/2048 МиБ при 1/2/4 потоках, пол 3 ГиБ безопасен; при 4 потоках и лимите 768–1536 МиБ — **молчаливое зависание** (S3-R1).
  - [x] Развилка **не активирована**: дефолт остаётся `gigaam-v3-e2e-rnnt`, `SherpaEngine` в M2 не нужен (журнал «M0.S3 принят»). Решения для M2: `intra_op_num_threads=2`, `allow_spinning=0`, `RLIMIT_AS` 3 ГиБ, дедлайн `engine.load`, каталог на вариант (S3-R6), без 7 сессий ресемплера (S3-R4). Для M6: строки 2–3 `research/catalog-numbers.md` §1 — размеры istupakov-наборов 224 896 388 / 225 780 697 Б (S3-R5).
- **S4 хоткей + вставка** (US-2.1, US-3.1, US-1.5b, US-3.4; `platform/hotkey.py`, `platform/paste.py`, `platform/shortcuts_conflict.py`; план #1 Р3/§13.1; T1 У11/У12/У40)
  - [~] KDE: `python3-xlib` `XGrabKey` Ctrl+Space × 8 масок Lock/Num/Scroll; PTT с фильтром автоповтора (KeyRelease+KeyPress одним `time`); временный grab `Escape`; `BadAccess` на `Alt+F2` → имя действия из `kglobalshortcutsrc`. — **живой KDE 2026-09-09: механика PASS, дефолт FAIL.** Масок **4**, а не 8; PTT `release 1,231 с`, 16 автоповторов отброшено; `Escape` и `BadAccess` (KRunner) ок. **`Ctrl+Space` занята Handy** (`/usr/bin/handy`, юнит `app-Handy@autostart.service`) — `kglobalaccel` её «не видит», после остановки Handy grab проходит; свободны `Ctrl+Shift+Space`, `Ctrl+Alt+D` (S4.md §2.7). Нужно продуктовое решение по дефолту + сообщение «занята другой программой» (US-1.5b).
  - [x] XTest `Ctrl+V` в Kate при русской раскладке → текст появился; буфер восстановлен ≤ 200 мс (копия `QMimeData`); `x-kde-passwordManagerHint=secret` → история Klipper без фразы. — **живой KDE 2026-09-09 ✅**: Kate, Konsole, LibreOffice Writer, Chromium — вставка прошла; restore 153–162 мс; **история Klipper без нашей фразы** (20 записей до и после); XTest `Ctrl+Shift+V` **не переключает** раскладку при `grp:ctrl_shift_toggle`. **xterm FAIL** (байт 026) → для не-VTE `Shift+Insert`+PRIMARY — решение за Юркой. Находка: после выхода процесса буфер пересобирает Klipper, часть MIME-типов теряется (S4-R8).
  - [ ] Fly (перелогин): `BadAccess` на `Alt+space` (`keyshortcutrc` → «Меню окна»); клип-менеджер fly-wm — тип из `ClipboardManagerTypesBlacklist` → `~/.fly/clipboard` без фразы; вставка в `fly-term` (какая комбинация), fly-fm, LibreOffice.
  - [ ] Провал → `Shift+Insert` как дефолт; предупреждение о Klipper/клип-менеджере вместо защиты (P1 остаётся).
- **S5 звук** (US-2.5, US-11.1; `worker/audio.py`; синтез Р1, §7 И4; T1 У39)
  - [x] `libpulse-simple` через ctypes, 16 кГц mono s16le; виртуальный источник (см. Validation) + `paplay` → уровень живой; тишина → `silent` через 2 с; на диске 0 файлов. — **изоляция ✅** (`arch/spikes/S5.md`: открытие по имени 10,6–14,8 мс, пик −5,9 дБFS, `silent` на 2,016 с, 0 файлов, ASR из ОЗУ). **Находка S5-R1:** `pa_simple_new` с неверным именем молча открывает устройство по умолчанию → `worker/audio.py` обязан проверять имя до открытия (fail closed).
  - [x] `systemctl --user restart wireplumber` → устройство открывается со 2-й попытки ≤ 1 с (ленивое открытие; И4 `audio.close`/`audio.ready`); во Fly — после перелогина (гонка greeter). — **живой KDE 2026-09-09 ✅**: рестарт `rc=0` за 3 116 мс, `pa_simple_new` открыл реальный микрофон **с 1-й попытки за 573 мс**, захват после — пик −20,3 дБFS; профиль карты `HiFi (…Mic1, Mic2, Speaker)` и default source совпали с «до» (гонка `sof-hda-dsp` не проявилась). **S5-R7 для M3:** открытие блокируется внутри libpulse → нужен дедлайн на `open`, а не только счётчик попыток. Fly — ждёт перелогина.
  - [ ] Уведомление «Перезапустить звуковую службу» с кнопкой — `ActionInvoked` в Plasma и `fly-notifications`; имена `*.monitor` в `pactl list short sources` (У39).
  - [ ] Провал → `QAudioInput` (`python3-pyqt5.qtmultimedia`) за тем же `AudioSource`; схема #2 (PCM через memfd) — запасная.
- [ ] Итог: `arch/spikes/S1…S5.md` заполнены; записи в `decisions/log.md` (рантайм/дефолт, режим пилюли, метод вставки, источник звука, детект сессии); расхождения → правка `arch/plan-synth.md`; commit+push.
### Definition of Done
- В `arch/spikes/<S>.md` по каждому S1–S5 — факт и вердикт с датой; S1/S4/S5 — отдельно для KDE и Fly; S3 — таблица медиана/p95/VmHWM для трёх вариантов при 2/4 потоках + `files[]` с sha256; S2 — один polkit-диалог (скриншот) и коды 0/126/127.
- Система не изменена, кроме трёх файлов S2 (перечислены с командой отката); scratch вне репо.
- Открытых развилок нет либо они оформлены записью в журнале.
### Validation
```sh
# S1
xdotool getactivewindow; python3 spikes/s1_pill_tray/pill.py & sleep 0.3; xdotool getactivewindow                 # id совпадают
xprop -id "$(xdotool search --name astra-voice-pill | head -1)" _NET_WM_STATE _NET_WM_WINDOW_TYPE
xprop -root _NET_WORKAREA; xprop -root | grep -E '_FLY_WM_PID|_NET_SUPPORTING_WM_CHECK'; echo "XDG=$XDG_CURRENT_DESKTOP DS=$DESKTOP_SESSION"
# S2 (после sudo-шага заказчика)
python3 spikes/s2_gui.py ./astra-voice-spike_0.0.2_all.deb; dpkg-query -W astra-voice-spike           # одно окно, 0.0.2
# S3
python3 -m venv --system-site-packages ~/.cache/astra-voice-spike/venv && . ~/.cache/astra-voice-spike/venv/bin/activate
pip install --no-deps onnxruntime==1.24.4 onnx-asr==0.12.0 && python3 -c 'import onnxruntime,onnx_asr,numpy;print(onnxruntime.__version__,numpy.__version__)'
find ~/.cache/astra-voice-spike/venv -name '*.so*' -exec file {} + | grep -c ELF                     # 3
for f in $(find ~/.cache/astra-voice-spike/venv -name '*.so*'); do objdump -T "$f" | grep -oE 'GLIBC_[0-9.]+' | sort -Vu | tail -1; objdump -T "$f" | grep -c isoc23; done
python3 scripts/measure_model.py --model-dir ~/.cache/astra-voice-spike/gigaam-v3/e2e_rnnt --wav data/test/test-ru-6s.wav --runs 10 --threads 2,4
python3 scripts/measure_model.py --model-dir ~/.cache/astra-voice-spike/gigaam-v3/e2e_rnnt --wav data/test/test-ru-20s.wav --cancel-after-ms 300   # ≤ 1000 мс
# S4
python3 spikes/s4_hotkey.py --grab ctrl+space --probe alt+F2                                          # Fly: --probe alt+space
qdbus org.kde.klipper /klipper getClipboardHistoryMenu | grep -c 'проверка связи'                    # KDE: 0
stat -c %Y ~/.fly/clipboard; grep -c 'проверка связи' ~/.fly/clipboard                                # Fly: 0
# S5 — виртуальный микрофон
pactl load-module module-null-sink sink_name=av_test sink_properties=device.description=av_test
pactl load-module module-remap-source master=av_test.monitor source_name=av_test_mic source_properties=device.description=av_test_mic
python3 spikes/s5_audio.py --device av_test_mic & paplay --device=av_test data/test/test-ru-6s.wav
systemctl --user restart wireplumber && python3 spikes/s5_audio.py --device av_test_mic --expect-reopen-ms 1000
find ~ /tmp -newer arch/spikes/S3.md \( -name '*.wav' -o -name '*.raw' -o -name '*.pcm' \) | grep -v astra-voice-spike | wc -l   # 0
```
### Known Risks
R1 Python-декодер RNN-T — **снят S3** (~52 мс на 6 с) · R2 fly-wm и фокус пилюли (S1) · R3 `Ctrl+Space` занят (S4) · R9 нет polkit-агента во Fly (S2) · R14 гонка WirePlumber (S5) · A-04 `XDG_CURRENT_DESKTOP` во Fly пуст.
### Stop-and-Fix
- Любой красный спайк → запись в журнал **до** M1; S3 закрыт зелёным — развилка «e2e_ctc / SherpaEngine» не активирована.
- S1 крадёт фокус → override-redirect по умолчанию; S2 второе окно polkit или блокировка GUI → переделка вызова до M8 (v0.1 не блокирует).

## M1. Скелет, упаковка, CI `[~]` — v0.1 (все пункты сделаны, DoD не закрыт: тема, RSS, поведение окна — см. `spikes/m1_live/`)
### Goal
Репозиторий по §7.1 плана #1; `make deb` за одну команду даёт устанавливаемый пакет с окном 900×620 в теме сессии, single-instance, settings/policy; CI зелёный; требования T1 §5 «M1» и «M1 verify» закрыты.
### Tasks (US-12.1, US-1.1, US-9.2, US-10.5, US-4.7-старт, US-8.1, US-12.5-часть; компоненты `app.py`, `core/*`, `bootstrap.py`, `security/verify.py`, `packaging/`, CI)
- [x] Структура репо §7.1; `pyproject.toml` (ruff, mypy strict, маркеры `unit/xvfb/engine/machine`); `Makefile`: `lint · test · deb · wheels · theme · user-bundle` (заглушка, A-05).
- [x] `bootstrap.py` (синтез §7 И7): вставляет `/usr/lib/astra-voice/vendor` в `sys.path`, диспетчер `app|worker|helper`; лаунчер `/usr/bin/astra-voice` = `python3 -I bootstrap.py app`; `PYTHONPATH` не используется; ресурсы относительно `__file__` (relocatable) + unit «запуск из любого cwd».
- [x] Лаунчер: `QT_QUICK_CONTROLS_STYLE=Default` до импорта Qt (Fly `06-fly-misc-env`); `RLIMIT_CORE=0` + `prctl(PR_SET_DUMPABLE,0)` в GUI (T1 У9).
- [x] `core/paths.py` (XDG), `core/version.py` (`__version__` из `debian/changelog`), `core/logging.py` (`RotatingFileHandler` 5×1 МБ, фильтр поля `text`) + unit.
- [x] `core/settings.py`: `settings.json` 0600, `schema_version`, миграции вперёд, tmp+fsync+replace, `.bak` при порче + unit (миграции, битый файл).
- [x] `core/policy.py` (минимум): INI → эффективная конфигурация + `locked_keys`; «файла нет» ≠ «есть, но невалиден» (полный У37 — M7) + unit таблица истинности.
- [x] `scripts/gen_theme.py` → `qml/Theme.qml`, `qml/PillTheme.qml` из `tokens.json` (генерат коммитится; CI проверяет чистую регенерацию); `core/theme.py`: `ThemeSource` KDE — парсер `kdeglobals` до `engine.load` + `QFileSystemWatcher` (re-add при перезаписи) + unit фикстуры Breeze/BreezeDark/AstraDark.
- [x] `platform/session.py`: `SessionKind` по A-04 (env → атомы root) + unit; значение в лог и «О программе».
- [x] `app.py`: argv `--hidden/--show/--version`; `QLockFile` в `$XDG_RUNTIME_DIR/astra-voice/lock` **первым действием** (О5), проигравший шлёт `--show` через `QLocalSocket` и выходит 0; fallback `/tmp/astra-voice-<uid>` 0700 с проверкой владельца/symlink, протокол только `show` (У17, T-20); порядок старта policy → settings → theme → QML.
- [x] Оболочка окна: `qml/Main.qml`, `Sidebar.qml` (6 разделов + «Отладка» по флагу), `SettingRow.qml`, строка-статус (пока «модель · версия», состояние 1 `disabled`), раздел «Общие» — референс `design/refs/01-general-base.png` (+`-dark`), спека §1–§3; `minimumWidth/Height` 900×620; xvfb-тест «каждый QML без warnings».
- [x] `security/verify.py` (T1 §5 M1, US-12.5): `Verifier(purpose=release|catalog)`: `gpgv --status-fd 1 --keyring <abs> --homedir <пустой>`; успех только `VALIDSIG` ∧ fpr ∈ `PINNED_FINGERPRINTS` ∧ ∉ `REVOKED_FINGERPRINTS` (пинится и первичный fingerprint); sha256 из `SHA256SUMS`, ровно одна строка на имя + unit с тестовым keyring (T-05, T-31).
- [x] Ключи: `data/keys/release.gpg` = мастер-pub + S1 + S2 (S2 закрытая — офлайн у заказчика); `docs/SECURITY.md` (владелец мастера, календарь ротации, действия при компрометации, канал уязвимостей) — черновик; fingerprint мастера в README (У26).
- [x] `packaging/`: `debian/{control,rules,changelog,copyright}`, `wheels.lock` (`onnxruntime==1.24.4 --hash`, `onnx-asr==0.12.0 --hash`), `build-deb.sh` (`pip download --require-hashes` из локального кэша; сеть только `make wheels`), `Containerfile` debian:12; Depends A-01/A-03 (+`python3-sympy`), Recommends `polkit-kde-agent-1 | fly-…`, `pipewire-pulse | pulseaudio`, `wireplumber`; `override_dh_strip` не трогает vendor; `SOURCE_DATE_EPOCH`; `postinst` — только триггеры dh; пакет создаёт `/var/lib/astra-voice/staging` root:root 0700 (T1 M8 заранее).
- [x] Гейты сборки (CI run 34358041504 на `e03d6ae` — все job зелёные; локально — только host-режим `dpkg-deb`; dh, lintian и установка в чистом debian:12 — за CI, docker-группу на машине заказчика не выдаём): `tests/unit/test_deb_elf_count.py` (`dpkg-deb -c` + `file` = 3), `tools/elf-audit --strict`, проверка METADATA Requires-Dist ↔ версии apt (Р3), lintian без E, установка в чистом debian:12 → `astra-voice --version`; `scripts/sbom.py` (CycloneDX из `wheels.lock` + `dpkg-query`).
- [x] CI `.github/workflows/ci.yml`: jobs lint/types · unit · xvfb · engine (кэш модели) · deb · release (по тегу `v*`, Environment `release`, сборка+подпись в одном job — У32); Actions по SHA; `permissions` минимальные; `pull_request_target` запрещён; `scripts/ci_lint.py` (T-06); две сборки → воспроизводимость (Р13).
- [x] Документы-заготовки: `docs/PRIVACY.md` (четыре сетевых действия, хосты, apt → репозитории ОС), `NOTICE` скелет, README «установка требует прав администратора; пользовательская — в планах» (G4), `INSTALL-ADMIN.md` черновик.
### Definition of Done
- `[~]` **Живой прогон на KDE заказчика 2026-09-09** (`spikes/m1_live/`, факты в `status.md`): `apt install` + `astra-voice --version` → `0.1.0~m1`, код 0 **PASS**; окно 900×588 клиентской = 908×620 внешней (рамка 4/4/28/4), `min 900×588`, заголовок «Astra Voice», QML-warnings 0 **PASS**; второй запуск код 0 за 0,15 с, «получена команда show», процессов 1 **PASS**; CI зелёный на всех jobs (run 34358041504) **PASS**.
  - `[x]` **окно в теме сессии** — починено 2026-09-09 и проверено в изолированном KWin (`spikes/m1_live/verify_isolated.sh`, PASS=17/FAIL=0): тёмная схема → яркость центра 31, светлая → 249; `themeSource` подаётся и в контекст QML, и в глобальный объект JS (`Theme.qml` — `pragma Singleton`).
  - `[ ]` **RSS ≤ 120 МБ — FAIL**: 167 720 КБ против порога 122 880 (+44 840 КБ, 36 %) на пустой оболочке; разложить память мешает наш `PR_SET_DUMPABLE=0` (дефект 4).
  - `[x]` **«второй запуск показывает первое окно»** — починено: `showNormal()` + `_NET_WM_USER_TIME` из протокола `show <ts>` → окно возвращается из `Iconic` в `Normal` и становится активным (дефект 3); закрытие окна завершает процесс, `lock`/`ipc` убираются в `finally` и по SIGTERM, переключатель `CLOSE_TO_TRAY=False  # M4: True` (дефект 2); `settings.json` 0600 создаётся при первом старте (дефект 5); `WM_CLASS` instance = `astra-voice` (дефект 8).
  - `[ ]` Fly-сессия и `make deb` ≤ 5 мин / lintian без E на машине — не проверялись (deb собирается в CI).
- T1 §5 M1: лаунчер `python3 -I`; `RLIMIT_CORE=0`/`PR_SET_DUMPABLE=0`; CI-гейты (SHA, permissions, секрет только в `release`, без `pull_request_target`); keyring мастер+S1+S2; `PINNED/REVOKED_FINGERPRINTS`; `docs/SECURITY.md`; `Verifier` по `VALIDSIG`+пин для релиза/deb-из-файла/манифеста/`catalog_pubkey`.
### Validation
```sh
make lint && pytest -m unit -q && xvfb-run -a pytest -m xvfb -q
time make deb && lintian dist/astra-voice_*.deb
dpkg-deb -c dist/astra-voice_*.deb | grep -c '\.so' ; tools/elf-audit --strict dist/astra-voice_*.deb          # 3
sudo apt install ./dist/astra-voice_*.deb && astra-voice --version
astra-voice & sleep 2; astra-voice; sleep 1; pgrep -c -f '^/usr/bin/python3 -I /usr/lib/astra-voice/bootstrap\.py app'   # 1 (без якоря pgrep ловит и свою оболочку)
WID=$(xdotool search --class astra-voice | head -1)      # именно --class: instance и class оба = astra-voice
xdotool getwindowgeometry $WID; xprop -id $WID _NET_FRAME_EXTENTS   # 900x588 клиентской, рамка 4/4/28/4
ps -o rss= -p "$(pgrep -f '^/usr/bin/python3 -I /usr/lib/astra-voice/bootstrap\.py app')"                     # ≤ 122880
python3 spikes/m1_live/close_window.py $WID; sleep 2; pgrep -c -f 'bootstrap\.py app'   # 0 — закрытие завершает процесс (M1, CLOSE_TO_TRAY=False)
python3 scripts/gen_theme.py --check && python3 scripts/ci_lint.py .github/workflows/*.yml
```
### Known Risks
QML 5.15 vs макет (`DropShadow`, R16) · размер deb (A1 ≤ 80 МБ) · `python3-sympy` 1.11.1 для ORT 1.24.4 (import) · имена пакетов шрифтов A-03 · Fly `06-fly-misc-env` · `python3-pyqt5.qtquick` тянется автоматически.
### Stop-and-Fix
- ELF ≠ 3 или `isoc23` → стоп сборки; QML warnings → чинить до M2; `make deb` > 5 мин → профилировать (кэш wheels) до R1.

## M2. Воркер и движок `[ ]` — v0.1
### Goal
Воркер — дочерний процесс с IPC; `OnnxAsrEngine` грузит GigaAM v3 из локальной папки; `transcribe.file`, отмена ≤ 1 с, замер, рестарт при краше без вреда GUI; требования T1 §5 «M2» закрыты.
### Tasks (US-2.5, US-8.3, US-2.8-часть, US-2.3-движок; компоненты `worker/{ipc,supervisor,__main__,engine,measure}.py`; синтез §7 И1/И2/И3/И4/Д4/О1; T1 У7/У8/У18/У35/У36)
- [ ] `worker/ipc.py`: len-prefixed JSON, `hello{protocol, build, runtime}`; длина кадра проверяется **до** `read`, ≤ 64 КиБ; схема сообщений, `text ≤ 32 КиБ`, `utterance_id` только из ожидаемых, лишние события отбрасываются + unit кодек (T-21).
- [ ] `worker/supervisor.py`: `Popen([sys.executable,'-I',bootstrap,'worker'], pass_fds=[sock])`, `QSocketNotifier`, таймауты, рестарт ≤ 3/10 мин, `SIGTERM` при выходе; поколение воркера — все `utterance_id` старого поколения аннулируются (О1); результат принимается ровно один раз по `(generation, utterance_id)` + unit с эхо-воркером и `kill -9`.
- [ ] `worker/__main__.py`: `prctl(PR_SET_PDEATHSIG, SIGTERM)` + проверка `getppid()`; EOF сокета → закрыть источник, выход ≤ 1 с (У36, T-34); `RLIMIT_CORE=0`, `PR_SET_DUMPABLE=0`, `oom_score_adj=+500`, **`RLIMIT_AS` = `max(min_ram_mb×3, 3 ГиБ)`** (S3 §6: пороги 512/768/2048 МиБ при 1/2/4 потоках; У8/У9).
- [ ] Автомат `idle · recording · processing · recording+processing` (очередь 1); буфер PCM на каждую `utterance_id`, очищается по её завершению (И2) + unit на `FakeAudioSource`+`FakeEngine`.
- [ ] `worker/engine.py`: протокол `Engine.load(dir, layout, variant, threads)` · `transcribe(audio, cancel_token)` · `unload`; `OnnxAsrEngine` для `onnx-asr-gigaam-v3` (таблица layout → загрузчик + обязательные файлы по S3); **`intra_op_num_threads=2`** (дефолт; настройка в «Продвинутых», > 2 — только явно) и **`sess_options.add_session_config_entry("session.intra_op.allow_spinning", "0")`** (S3 §4.3–4.4: спин жжёт CPU ×3–5 без выигрыша, 4 потока пробивают RSS 450 МБ); только `CPUExecutionProvider`; `register_custom_ops_library` не вызывается; второй адаптер (`SherpaEngine`) не нужен — S3 закрыл развилку, интерфейс `Engine` остаётся.
- [ ] Скан ONNX-protobuf до `InferenceSession`: каждый `external_data.location` ⊆ `files[]` модели и внутри каталога ревизии; лишние файлы → `error{code=extra-file}`; `location` вне каталога → `error{code=external-data}` (У7, T-10) + unit-фикстуры.
- [ ] Дедлайн на `engine.load` — **10 с** (норма 0,64–0,78 с): по истечении `error{code=load-timeout}`, супервизор `SIGKILL` + рестарт (S3-R1: при тесном `RLIMIT_AS` и ≥ 4 потоках воркер зависает молча, а не падает); лимит времени инференса по таймеру — тот же механизм.
- [ ] Раскладка «каталог на вариант» (S3-R6): `store/<id>/<rev>/` содержит файлы **одного** варианта; в одном каталоге нельзя смешивать fp32+int8 и e2e+не-e2e (glob-паттерны onnx-asr `v?_…` → `MoreThanOneModelFileFoundError`); `OnnxAsrEngine.load` передаёт `local_dir` варианта (`Resolver` → `offline=True`, `huggingface_hub` не нужен); `files[]` трёх вариантов — из S3 §2 (16 файлов, sha256).
- [ ] Убрать 7 лишних сессий ресемплера при `load_model` (S3-R4): вход всегда 16 кГц (Р1/И4) → `resampler_config`/ленивое создание, в процессе 3 `InferenceSession` вместо 10 → −640…780 мс холодного старта; замер до/после в `tools/benchmark`.
- [ ] Вендорный патч отмены — `vendor/patches/onnx-asr-cancellable.patch` (+28 строк, `adapters.py`: контекст `cancellable()` с `RunOptions` на все сессии; текст — S3 §5.2), применяется при сборке vendor (`make wheels`), CI проверяет, что патч ложится на пин 0.12.0; проверка флага **перед каждым** `run` (шаги RNN-T-декодера — сотни коротких `run`).
- [ ] Отмена (И1): `recognize{utterance_id}` держит `RunOptions`; `record.cancel{utterance_id}` → `terminate=True` + точки отмены между сегментами → `cancelled{utterance_id}`; лимит времени инференса по таймеру (T1 M2); поздний `result` с отменённым id GUI отбрасывает.
- [ ] `model.load{id, revision, dir, layout, variant, threads}` → `model.loaded{id, revision, variant, load_ms, engine_version}` (И3); смена id/ревизии/потоков = перезапуск воркера (Д4).
- [ ] `transcribe.file{path}` (смоук/тест); `measure` → `VmHWM`/`Pss` из `/proc/self/{status,smaps_rollup}`; `measurements.json` ключ `(id, revision, threads, runtime, build)` (Д4) + unit на фикстурах `/proc`.
- [ ] Выгрузка по простою: таймер в GUI → `model.unload` (опция «никогда» по умолчанию); `ping`.
- [ ] CLI: `astra-voice --debug-transcribe <wav>`; `tools/validate worker-cancel`; `tools/benchmark` — **на основе `scripts/measure_model.py` из S3** (уже в репо: `--runs/--threads/--json/--rlimit-as-mb/--cancel-after-ms`), плюс `--catalog`/`--minimum-models` в M6.
- [ ] `tests/integration/test_engine_gigaam.py` (маркер `engine`, кэш модели по ревизии); `pytest -m engine` в CI.
### Definition of Done
- `astra-voice --debug-transcribe data/test/test-ru-6s.wav` печатает текст с «проверка» и `t_ms`; `kill -9 <worker>` → новый pid ≤ 1 с, модель перезагружена, ошибка показана один раз; `kill -9` GUI → воркер завершился ≤ 1 с; отмена ≤ 1000 мс (S3: 0,5–4 мс); `oom_score_adj` = 500; по умолчанию `intra_op=2`/`allow_spinning=0` (`cpu/wall` ≤ 2 при 2 потоках); `engine.load` под `RLIMIT_AS` 768 МиБ при 4 потоках завершается ошибкой по дедлайну 10 с, не зависает; в воркере 3 `InferenceSession` (не 10), холодная загрузка ≤ 0,3 с быстрее S3; T-10, T-11, T-21, T-34 зелёные; p95 инференса из `tools/benchmark` записан в `arch/spikes/S3.md` §4 (сравнение с замером спайка).
### Validation
```sh
pytest -m unit tests/worker tests/unit/test_supervisor.py -q && pytest -m engine -q
astra-voice --debug-transcribe data/test/test-ru-6s.wav                       # текст с «проверка», t_ms
tools/validate worker-cancel                                                    # ≤ 1000 мс
tools/benchmark --model gigaam-v3-e2e-rnnt-int8 --runs 50 --threads 4
W=$(pgrep -f 'bootstrap.py worker'); kill -9 "$W"; sleep 1; pgrep -f 'bootstrap.py worker'; cat /proc/$(pgrep -f 'bootstrap.py worker')/oom_score_adj   # новый pid; 500
G=$(pgrep -f 'bootstrap.py app'); kill -9 "$G"; sleep 1; pgrep -c -f 'bootstrap.py worker'                  # 0
```
### Known Risks
R1 снят S3 · S3-R1 молчаливое зависание при тесном лимите (дедлайн) · S3-R3 > 2 потоков пробивают 450 МБ (`min_ram_mb` в каталоге считать при 2 потоках) · `RLIMIT_AS` для Whisper turbo (`min_ram_mb×3` ≥ 3 ГиБ) · патч отмены расходится с будущими версиями onnx-asr (пин 0.12.0) · скан external_data на ORT 1.24.4 (Astra: ORT сам проверяет выход за каталог — оставить свой скан).
### Stop-and-Fix
- p95 в M2 хуже S3 (253 мс при 2 потоках) более чем на 20 % → искать регрессию (ресемплеры/потоки/спин), дефолт не менять; T-10 не воспроизводится → проверить по вендорным байтам, скан не убирать.

## M3. Звук `[ ]` — v0.1
### Goal
Захват через `libpulse-simple` в воркере (PCM не покидает процесс), уровни, тишина, лимит 120 с, VAD > 20 с, список/выбор устройства, восстановление после рестарта WirePlumber, `WavFileSource` для тестов.
### Tasks (US-1.6-часть, US-2.4, US-11.1; компоненты `worker/audio.py`, `worker/vad.py`, `scripts/e2e/virtual_mic.sh`; синтез Р1, §7 И4; T1 У39)
- [ ] `worker/audio.py`: протокол `AudioSource`; `PulseSimpleSource` (ctypes `libpulse-simple.so.0`, `pa_simple_new(PA_STREAM_RECORD, s16le/16k/1, device)`), поток чтения 20 мс, кольцевой буфер в ОЗУ, RMS/пик → `level` ≤ 30/с, «тишина» = пик < −60 дБ за 2 с → `silent`, лимит 120 с (настройка 30–300) → `limit`, ретраи открытия 3×300 мс, ошибки `ENODEV/EBUSY` + unit на `WavFileSource`.
- [ ] Устройства: `pactl list short sources`; `*.monitor` — только с явной пометкой; сравнение `name`/`description` выбранного, молчаливой смены нет, при смене — имя источника в пилюле (У39, T-35).
- [ ] И4: `audio.close` (ack) → GUI `systemctl --user restart wireplumber` через `QProcess` → ленивое открытие на следующем `record.start` → `audio.ready`; первая запись после автозапуска — повтор через 1 с до 3 раз (F6.3).
- [ ] `worker/vad.py`: Silero VAD ONNX (`data/vad/silero_vad.onnx`, MIT, ~2 МБ): сегментация > 20 с по паузам без резки слов (окно ≤ 24 с), обрезка хвостовой тишины; ≤ 20 с — не применяется + unit синтетический wav с паузами.
- [ ] Инвариант «ни одного wav/tmp на диске»: тест списком файлов до/после (S5-A4, инвариант #1-3 плана).
- [ ] `scripts/e2e/virtual_mic.sh up|down`; `tools/validate dictation --virtual-mic [--silence N | --wav F]` (без хоткея: `record.start` → `paplay` → `result`).
### Definition of Done
- Виртуальный микрофон: `record.start` → `paplay` 6 с → `result` с «проверка»; тишина → `silent` ≤ 2,2 с; 130 с записи → `limit` + текст; после `restart wireplumber` устройство открывается со 2-й попытки ≤ 1 с; 0 аудиофайлов на диске; CPU воркера в простое ≤ 1 % (нет опроса микрофона).
### Validation
```sh
pytest -m unit tests/worker/test_audio_fsm.py tests/worker/test_vad.py -q
scripts/e2e/virtual_mic.sh up && tools/validate dictation --virtual-mic --wav data/test/test-ru-6s.wav
tools/validate dictation --virtual-mic --silence 3                               # silent ≤ 2200 мс
tools/validate dictation --virtual-mic --wav data/test/test-ru-130s.wav          # limit + result
systemctl --user restart wireplumber && tools/validate dictation --virtual-mic --wav data/test/test-ru-6s.wav   # reopen ≤ 1 с
top -b -n 3 -p "$(pgrep -f 'bootstrap.py worker')" | tail -1                      # %CPU ≤ 1
find ~ /tmp -newer /tmp/av.mark \( -name '*.wav' -o -name '*.raw' -o -name '*.pcm' \) | wc -l ; scripts/e2e/virtual_mic.sh down   # 0
```
### Known Risks
R14 гонка WirePlumber · `EBUSY` при эксклюзивном захвате · ресемплинг 48 кГц → 16 кГц на сервере (качество) · во Fly гонка greeter.
### Stop-and-Fix
- ctypes-захват нестабилен → схема #2 (`QAudioInput` в GUI + PCM через memfd) за тем же `AudioSource` до M4; интерфейс IPC не меняется.

## M4. Цикл диктовки: хоткей · пилюля · трей · вставка `[ ]` — v0.1
### Goal
Полный цикл «хоткей → пилюля → воркер → вставка → восстановление буфера» **в KDE и во Fly**; Ц2 p95 ≤ 0,5 с; обе строки T1 §5 «M4» закрыты; Fly-ветки за интерфейсами (решение 2026-09-09 «задачи Fly в M0/M4/M9»).
### Tasks (US-2.1, 2.2, 2.3, 3.1, 3.2, 3.5, 4.1–4.4, 11.2, 8.4, 2.8 + US-8.5-часть; компоненты `platform/{x11,hotkey,paste,shortcuts_conflict}.py`, `ui/{pill,tray,tray_icons,notify}.py`, `qml/Pill.qml`, `core/stats.py`; синтез §7 И1/И2/О1/О4/О5; T1 У10/У11/У12/У17/У21/У40)
- [ ] `platform/x11.py`: свой `Display`; `XGrabKey/XUngrabKey` × 8 масок; обработчик `BadAccess`; временный grab `Escape` на время записи; `XGrabKeyboard` только пока открыто поле захвата (снятие по потере фокуса/Esc/30 с — У21, T-24); XTest-комбинации; `_NET_ACTIVE_WINDOW`/`WM_CLASS`/геометрия; EWMH-свойства пилюли; `_NET_CLIENT_LIST_STACKING`; struts (Р7) + xvfb-тесты (grab/ungrab, BadAccess при двойном grab, XTest в тестовое окно).
- [ ] `platform/hotkey.py`: автомат PTT/toggle `Idle→Recording→Processing`, фильтр автоповтора, порог 0,3 с, лимит 120 с, `duplicate`/`not-grabbed`; интерфейс `HotkeyBackend` (X11; evdev P2) + unit на синтетических событиях.
- [ ] `platform/shortcuts_conflict.py`: `ShortcutConflictSource` — KDE парсер `kglobalshortcutsrc` (`Alt+F2` → «Открыть строку поиска и запуск»); FLY парсер `keyshortcutrc` + `FLYWM_*` → подпись из `ru.miscrc` + unit на реальных файлах.
- [ ] `platform/paste.py`: копия **всех** форматов `QMimeData` → только `text/plain` (+`x-kde-passwordManagerHint=secret`, P1) → 50 мс → комбинация по `WM_CLASS` (терминалы: `konsole`, `fly-term`, `xterm`, `yakuake`, `alacritty` → `Ctrl+Shift+V`) → 100 мс → восстановить **только если буфер ещё наш** (`ownsClipboard`); исходник с hint=secret не восстанавливать → «Буфер очищен» (У11/У40, T-14); нормализация: `\r\n` → пробел, все C0/C1 (`\x00–\x1f`, `\x7f–\x9f`) удаляются, Enter не синтезируется; окно при вставке ≠ запомненному → только буфер + `clipboard-only` (У12, T-15); режим «только буфер»; ветка FLY — тип из чёрного списка клип-менеджера (по S4) + xvfb-тест с `TextEdit` и вторым окном.
- [ ] `ui/pill.py` + `qml/Pill.qml`: 12 состояний спеки §8.4 (`hidden · loading-model · listening · listening-silent · limit · processing · done 500 · clipboard-only 1200 · empty 1000 · cancelled 800 · error 3000 · disabled`), ширина 172→320 без elide, 9 столбиков `h=max(3,round(l*20))`, «×», позиция У10 (низ по центру `availableGeometry` монитора активного окна, 48 px; struts при расхождении), показ ≤ 100 мс, re-assert «наверх» + проверка перекрытия; ветка FLY — атомы анимации; без композитора — XShape; глиф `clock` (У8), спиннер 1 об/с (У9) — референс `design/refs/09-pill.png` (+`-dark`); xvfb-скриншоты 12 состояний.
- [ ] Инвариант О4: запись идёт, пока виден ≥ 1 индикатор; потеря SNI во время записи → пилюля принудительно; потеря всех → стоп + уведомление.
- [ ] `ui/tray.py` + `ui/tray_icons.py`: `QSystemTrayIcon`, 6 состояний (`idle · listening · processing · done · error · nokey`), подсказки, ретрай регистрации ≤ 30 с (watcher на шине); `TrayIconProvider`: KDE — по имени из hicolor `status/astravoice-tray-{idle,listening,processing,done,error,nokey}` (перекраска; префикс без дефиса — иначе KIconLoader срезает до `astra`, spec §9.1), FLY — SVG явных цветов по яркости панели; меню спеки §9.2 (состав/неактивность: «Готов/Слушаю…» · «Отмена» · «Модель ▸» · «Скопировать последний текст» · «Настройки…» `Ctrl+,` · «Проверить обновления» · «О программе» · «Выход» `Ctrl+Q`) — референс `design/refs/09-tray.png` (+`-dark`); баннер «трей недоступен» > 30 с; xvfb-ветка `isSystemTrayAvailable()=false`.
- [ ] Оркестрация в `app.py`: активное окно запоминается **до** показа пилюли; `record.start{device,limit_s,silence_db,insert,utterance_id}`/`stop`/`cancel`; очередь 1 (S14-A2); крах воркера во время записи/обработки → пилюля `error` «Распознавание перезапущено», вставки нет, таймеры вставки отменены (О1); «последний текст» только в памяти GUI.
- [ ] Отмена: Esc (временный grab), «×», трей «Отмена» → `record.cancel` → `cancelled`; вставки нет (S2-A3, S14-A1).
- [ ] `core/stats.py`: `stats.json` (≤ 1000 событий PRD §10: `dictation`, `mic_error`…), p50/p95 `t_ms`, холодный прогон исключён, «Очистить»; CLI `astra-voice --stats` + unit перцентили.
- [ ] Корректное завершение (US-8.4): «Выход»/SIGTERM → восстановление буфера, `SIGTERM` воркеру, освобождение микрофона.
- [ ] Гигиена данных (T1 M4): фильтр логгера по `text`; текст не живёт в QML-свойствах; уведомления без текста; `ui/notify.py` минимум (D-Bus `Notify` без кнопок) для «Горячая клавиша не захвачена» + «Выбрать другую» (US-11.2, поле `not-grabbed`).
- [ ] `scripts/e2e/dictate50.sh --session {kde,fly}`: 50 диктовок `xdotool keydown/keyup` + `paplay` → `astra-voice --stats`; `tools/validate dictation --toggle`.
### Definition of Done
- На машине заказчика **в KDE и во Fly**: S2-A1…A4, S3-A1…A3, S4-A1/A3/A4, S12-A3, S14-A1/A2 зелёные; **p95 «отпустил → текст» ≤ 0,5 с по 50 диктовкам** (Ц2) в каждой сессии; фокус остаётся в Kate/`fly-term`; пилюли нет в Alt+Tab; T-13 (маркерная фраза — 0 совпадений), T-14, T-15, T-20, T-24 зелёные; CPU пилюли при записи ≤ 3 %.
### Validation
```sh
xvfb-run -a pytest -m xvfb tests/platform tests/ui -q
pytest -m unit tests/platform/test_hotkey_fsm.py tests/platform/test_paste_normalize.py tests/platform/test_shortcuts_conflict.py tests/core/test_stats.py -q
scripts/e2e/virtual_mic.sh up && scripts/e2e/dictate50.sh --session kde     # xdotool keydown ctrl+space; paplay --device=av_test …; xdotool keyup ctrl+space
astra-voice --stats                                                          # p95 t_ms ≤ 500
xdotool getactivewindow                                                      # id Kate до и после диктовки совпадает
xprop -id "$(xdotool search --name astra-voice-pill | head -1)" _NET_WM_STATE | grep -c ABOVE
grep -rl 'ГЕЛИОТРОП-7' ~/.local/state/astra-voice ~/.config/astra-voice ~/.cache/astra-voice; journalctl --user --since -10min | grep -c ГЕЛИОТРОП   # 0
# Fly: перелогин → scripts/e2e/dictate50.sh --session fly; stat/grep ~/.fly/clipboard до и после
```
### Known Risks
R2 стек/фокус пилюли на KWin и fly-wm · R3/R4 `Ctrl+Space`, автоповтор · R5 Klipper игнорирует hint · R12 меню рисует Plasma · R16 CPU пилюли · бюджет задержки 360–420 мс (план #1 §2) — запас мал.
### Stop-and-Fix
- p95 > 0,5 с при движке ≤ 300 мс → профилировать paste/IPC/пилюлю до R1; фокус украден → override-redirect по умолчанию + журнал; Klipper игнорирует hint → предупреждение (flows §7.6), опция остаётся P1.

## M5. Онбординг и первая модель `[ ]` — v0.1
### Goal
Первый запуск проводит через 5 экранов до рабочей диктовки; минимальный загрузчик модели по умолчанию (HF, `Range`, sha256, смоук); установка из папки; поле захвата с конфликтами; тест микрофона без вставки; до включения тумблеров — ноль сети.
### Tasks (US-1.2, 1.3, 1.5, 1.6, 9.1-Общие, 1.4-минимум; компоненты `qml/onboarding/*`, `models/{store,downloader,installer,catalog}.py`, `net/http.py`-минимум, `ui/window.py`; синтез §7 И5/И6/О2; T1 У6/У13/У20/У23)
- [ ] `qml/onboarding/{Step1Network,Step2Model,Step3Hotkey,Step4Mic,Step5Done}.qml` + обрамление спеки §10 (шапка «Шаг N из 5», точки, нижняя панель 60, ghost «Пропустить»; «Готово» disabled без модели — У11) — референсы `design/refs/08-onboarding-1-network.png` … `08-onboarding-5-done.png` (+`-dark`).
- [ ] Шаг 1: язык (авто по `LANG`), два тумблера **пусты**; при policy `offline`/`secure` — заблокированы «Задано администратором»; пока не включены — 0 сетевых запросов (S1-A1).
- [ ] `models/store.py`: `~/.local/share/astra-voice/models/<id>/<revision>/` + `current.json`, staging `<rev>.partial/`, `disk_usage ≥ size×1,2`, `MemTotal` vs `min_ram_mb`, размер на диске + unit tmpdir.
- [ ] `net/http.py` минимум (T1 У13, И5/И6): `requests.Session(trust_env=False)`, UA `astra-voice/X.Y.Z (+repo)`, CA `policy.ca_bundle or /etc/ssl/certs/ca-certificates.crt` + `SSL_CERT_FILE`, `allow_redirects=False` + ручные редиректы по allowlist ≤ 5, `stream=True` с общим deadline по `time.monotonic()` и `cancel` Event между чанками; гейт «сеть разрешена?» (явное «Скачать» разрешено при пустых тумблерах, запрещено при `offline`/policy/`HF_HUB_OFFLINE=1`); `HF_ENDPOINT` не учитывается (T-26).
- [ ] `models/downloader.py` минимум: один источник `hf` по `/resolve/{revision}/`, `.part`, `Range` только при 206 с корректным `Content-Range` (200 → сначала), инкрементальный sha256, обрыв на `size+1` **фактических** байт, `fsync`, `os.replace`, прогресс/скорость/ETA, отмена + unit на fault-server (206/200/обрыв/подмена/`size+1`/redirect-evil).
- [ ] `models/installer.py`: из папки/файла — те же sha256; порядок О2: скачать → sha256 → `rename` в `<rev>/` → смоук `transcribe.file` вшитого wav (`expect_any`) → `current.json` → `model.load`; провал смоука → `broken` в `state.json`, старая остаётся; незавершённая транзакция на старте → перепроверка + интеграционный тест «подменённый байт → откат».
- [ ] `models/catalog.py` минимум + `catalog.schema.json` (поля PRD 0.5 §7.3 с `serial`/`trust_epoch`/`revoked[]`): встроенный `data/catalog.json` с одной записью `gigaam-v3-e2e-rnnt-int8` (`files[]` из S3) + `catalog.json.sig` (S1) → `Verifier(catalog)`; валидатор путей (У6: regex `id`/`revision`, `path` относительный без `..`/NUL, `realpath` внутри staging) — T-09.
- [ ] Шаг 2: карточка рекомендованной модели (состояния 7 · 3 · 5 · 1 · 17 · 18 спеки §5.4), «Скачать» показывает хост и объём (226 МБ), «Установить из файла/папки…» (`QFileDialog`) — референсы `08-onboarding-2-model.png`, `02-models-file-dialogs.png`; нет сети → состояние 14 «Нет доступа к huggingface.co».
- [ ] Шаг 3 + строка «Горячая клавиша» в «Общих»: поле захвата 7 состояний спеки §7 (`idle · capturing · captured · success · conflict · duplicate · not-grabbed`), `XGrabKeyboard` только пока открыто; сегмент «удерживать / нажать-нажать» — референсы `08-onboarding-3-hotkey.png`, `01-general-hotkey.png`.
- [ ] Шаг 4: выбор устройства, живой уровень, «Сказать тестовую фразу» → текст на экране **без вставки** (S1-A5) — референсы `08-onboarding-4-mic.png`, `01-general-mic.png`.
- [ ] Шаг 5 «Готово»: в v0.1 без автозапуска (тумблер появится в M9), уведомление «Astra Voice готов: зажмите <хоткей> и говорите», окно → трей, флаг `onboarding_done`; закрытие окна на середине → следующий запуск с первого незавершённого шага (спека §10).
- [ ] Диалог подтверждения §11.1 как компонент — референс `10-dialogs.png` (+`-dark`).
- [ ] `tools/validate downloads --faults`, `tools/validate privacy --network-denied` (минимум: гейт + `tcpdump`-обёртка).
### Definition of Done
- S1-A1/A2/A3/A5/A7 зелёные на чистом профиле (`rm -rf ~/.{config,local/share,local/state,cache}/astra-voice`) на машине заказчика; на чистой ВМ — как только выделена; `tcpdump`: до включения тумблеров — 0 соединений, кроме явного «Скачать» на `huggingface.co`; T-09, T-16, T-23, T-26 зелёные; Ц3-черновик (секундомер от `apt install` до первой фразы) записан в `status.md`.
### Validation
```sh
pytest -m unit tests/models/test_downloader.py tests/models/test_store.py tests/models/test_paths.py tests/net/test_http_gate.py -q
xvfb-run -a pytest -m xvfb tests/ui/test_onboarding.py -q                  # 5 экранов × 2 темы без warnings, скриншоты
rm -rf ~/.config/astra-voice ~/.local/share/astra-voice ~/.local/state/astra-voice ~/.cache/astra-voice
sudo tcpdump -nn -i any 'tcp[13]&2!=0 and not host 127.0.0.1' -w /tmp/s1.pcap & astra-voice     # до «Скачать» — 0 SYN
tools/validate downloads --faults && tools/validate privacy --network-denied
```
### Known Risks
R6 HF недоступен из РФ → «Из файла» · чистая ВМ не выделена (A-13) · `QFileDialog` платформенный (Fly — стиль Fusion без platform-theme? проверить).
### Stop-and-Fix
- Любой сетевой запрос до включения тумблеров → блокер v0.1; смоук не находит «проверка» на e2e_rnnt → проверить wav/`expect_any` до R1.

## R1. Релиз v0.1 «Диктовка работает» `[ ]` — 30.09 (⛔ G5)
- [ ] Чеклист v0.1 беклога: S1 (A1 A2 A3 A5 A7), S2, S3, S4 (A1 A3 A4), S5, S11-A4, S12-A3, S13-A1, S14 зелёные на машине заказчика (KDE); p95 ≤ 0,5 с по ≥ 50 диктовкам (Ц2); CI зелёный; `make deb` ≤ 5 мин (Ц7).
- [ ] Should: E2E-чек S2, S4, S5 во Fly (US-8.5 часть 1) — расхождения → issues (вход для M10).
- [ ] `docs/test-plan.md` строки v0.1 закрыты; `status.md` обновлён; 0 ошибок в консоли/логах за прогон.
- [ ] `scripts/release.sh v0.1.0`: тег → CI job `release` (Environment `release`, подключ S1; сборка и подпись в одном job) → `deb` + `SHA256SUMS` + `SHA256SUMS.asc` + `sbom.cdx.json` + `INSTALL-ADMIN.md` (черновик) + `latest.json` → GitHub Release; changelog-writer → тело релиза; fingerprint мастера в README.
- [ ] ⛔ G5 заказчика: публичный релиз; откат известен — снять релиз/тег, `apt install` предыдущего deb.
- Validation: `tools/validate release --version 0.1.0` (ассеты, `gpgv --keyring data/keys/release.gpg SHA256SUMS.asc SHA256SUMS`, sha256, версия в deb = тег).

## M6. Каталог `[ ]` — v0.2
### Goal
Экран «Модели» с манифестом 12 записей и подписью, карточка 20 состояний, загрузчик с перебором источников/очередью/паузой, удаление, переключение по Р5, замеры и полоски по протоколу; анти-откат каталога и `revoked[]`; требования T1 §5 «M6» закрыты.
### Tasks (US-5.8, 5.1, 5.2, 5.3, 5.5, 5.6, 5.7, 5.4, 1.4, 11.3, **6.6**; компоненты `models/*`, `qml/components/ModelCard.qml`, `qml/sections/Models.qml`, `scripts/build_manifest.py`; синтез Р5/Р12, §7 Д1/Д2/Д3/Д4/Н2/Н3/О2; T1 У5/У6/У7/У19/У20/У23/У33/У34/У41)
- [ ] `models/schema.py` (фасад над jsonschema, A-08) + `catalog.schema.json`: `manifest_version` 1, `serial`, `trust_epoch`, `generated_at`, `revoked[]`, закрытый enum `layout` (7), `engine`, полный `files[]`, `sources[]` по типам (Д2: `hf` → `/resolve/{revision}/`; `github_release` → `base` + тег; `corp` → `base` + `revision` как подкаталог), `quality/speed.kind` ∈ {measured, benchmark, benchmark_fp32, no_data}, `origin.kind`; `json.loads(object_pairs_hook)` — отказ на дубли ключей; уникальность `id`/`path` после `normpath` (У34, T-33); лимит 1 МиБ до `json.loads`, ≤ 64 файлов/модель, ≤ 2 ГиБ/файл (У20, Р12).
- [ ] Анти-откат и отзыв (US-6.6, PRD 0.5 §7.3): `~/.local/state/astra-voice/catalog-state.json` (`trust_epoch`, `serial`, `sha256`, `last_attempt_at`, `last_success_at`); применять только при `(trust_epoch, serial)` > применённого, равный — только тот же sha256 (no-op), меньший → «Каталог не обновлён: старее текущего»; встроенный ↔ скачанный — больший; `revoked[]` блокирует активацию/откат/старт с текстом причины — карточка «отозвана» (У5/У33, T-08, T-32); даты попытки/успеха раздельно (У41, T-36).
- [ ] `scripts/build_manifest.py`: 12 записей из `research/catalog-numbers.md` + HF API `?blobs=true` на закреплённых ревизиях (LFS sha256; не-LFS считать), скан ONNX external_data при публикации (У7), `serial` инкремент; `data/catalog.json` + подпись → CI «регенерация чистая».
- [ ] `models/catalog.py`: встроенный → последний скачанный; слияние с `measurements.json` и статусами; `CatalogModel` (`QAbstractListModel`); фильтры «язык» (P2 — скрыт), «только отечественные» (6 моделей; Vosk — зарубежная); тексты в QML `textFormat: PlainText`; «Подробнее» только `https://huggingface.co|github.com` (У19, T-22); corp-URL из валидированного `path` без `urljoin`.
- [ ] `models/downloader.py` полный: источники `hf → github_release → corp` (429/404/сеть → следующий; зеркала отдают одинаковые байты — sha256 общий), очередь = 1, пауза «нет места» + «Повторить», отображение источника, inactivity timeout 30 с, «Отмена» (состояния 3 · 4 · 5 · 17 · 18).
- [ ] Переключение модели (Р5/Н2/Н3): на границе сессии; кандидат во временный воркер если `MemAvailable ≥ min_ram_mb + 200 МБ` — старая обслуживает записи до `model.loaded`; иначе отказ с объяснением + «Переключить с паузой» (`switching` ≈ 5 с); OOM-kill кандидата → активная не меняется; `MemTotal < 2×min_ram_mb` → предупреждение (состояние 12).
- [ ] Удаление (активную нельзя — подсказка «Сначала выберите другую модель»), размер каталога, «Повреждена — переустановить» (19), OOM → «Недостаточно памяти… (нужно ~N МБ)» + более лёгкая (US-11.3); «снята с каталога» (20).
- [ ] Замеры и полоски (US-5.4, 5.5): `VmHWM`/PSS после первой диктовки → «ОЗУ: N МБ (замерено на этом компьютере)» жирным; медиана RTFx по ≥ 5 тёплым прогонам → полоска «замерено»; сброс при смене ревизии/потоков (Д4); `quality = clamp((30 − WER)/25, 0, 1)`, лог-шкала RTFx 1…60, `benchmark_fp32` подпись, `no_data` — полоска не рисуется; Popup «Как мы считаем» с источником и датой.
- [ ] `qml/components/ModelCard.qml` — 20 состояний спеки §5.4 (state-машина в одном компоненте), бейджи «Рекомендуем/Активна/Новое/Обновление доступно», значок пунктуации — референсы `02-models-card-states.png`, `02-models-catalog.png`, `02-models-empty-offline.png`, `02-models-file-dialogs.png` (+`-dark`); xvfb-скриншоты 20 × 2.
- [ ] `OnnxAsrEngine`: остальные 6 раскладок (`onnx-asr-gigaam-multilingual`, `onnx-asr-t-one`, `onnx-asr-vosk`, `onnx-community-whisper`, `onnx-asr-whisper-ort`, `onnx-asr-nemo`) по таблице S3/`build_manifest`; `pytest -m engine` на 3 раскладках GigaAM в CI; остальные — `tools/validate catalog --all` на машине (кэш весов).
- [ ] CLI: `tools/validate catalog --all|--switch`, `tools/validate downloads --faults` (полный), `tools/benchmark --catalog --minimum-models 3`.
### Definition of Done
- S7-A1…A4, S15-A1…A3 зелёные; **≥ 3 модели замерены** (Ц6); каталог показывает 12 карточек, «только отечественные» — 6; скриншоты 20 состояний = референсы (DesignReviewer PASS); T-08, T-09, T-22, T-23, T-26, T-32, T-33, T-36 зелёные; `catalog-state.json` переживает очистку `~/.cache`.
### Validation
```sh
pytest -m unit tests/models -q && pytest -m engine -q
python3 scripts/build_manifest.py --check                       # регенерация чистая: 12 записей, 7 layout, serial монотонен
tools/validate catalog --all                                     # смоук каждой раскладки на машине
tools/validate catalog --switch                                  # Р5: temp-worker / отказ / «с паузой»
tools/validate downloads --faults                                # hf→github→corp, 429/404, size+1, нет места, зеркала ≠ байты
tools/benchmark --catalog --minimum-models 3
xvfb-run -a pytest -m xvfb tests/ui/test_model_card_states.py -q   # 20 × 2 → tests/_screens/ для DesignReviewer
```
### Known Risks
Форматы Whisper/Vosk/NeMo под onnx-asr (S3 закрыл только GigaAM) · HF 429 за NAT · Whisper turbo ~2 ГБ ОЗУ · GitHub-зеркало `models-YYYY.MM` (ассеты ≤ 2 ГБ) нужно выложить до R2.
### Stop-and-Fix
- Раскладка не грузится → запись помечается несовместимой и убирается из манифеста до фикса (v0.2 не блокирует, если 9 Must работают); нарушение анти-отката в тесте → блокер.

## M7. Сеть и проверки `[ ]` — v0.2
### Goal
`HttpClient` с гейтом/кэшем/backoff/политикой хостов, проверки HF `refs` и GitHub `latest`, строка-статус 17 состояний, тумблеры/офлайн/URL, обновление ревизии со смоуком и откатом, «Новое», трей «Проверить обновления»; policy с безопасным разбором; требования T1 §5 «M7» закрыты.
### Tasks (US-10.8, 10.1, 6.1, 6.2, 6.3, 7.1, 7.6, 7.7; компоненты `net/{policy,http,hf,github}.py`, `updates/checker.py`, `core/policy.py`-полный, `qml/sections/Network.qml`, `StatusBar.qml`, `UpdatePanel.qml`; синтез Р6/Р9, §7 И5/И6/Д3; T1 У10/У13/У16/У20/У37/У38/У41)
- [ ] `net/policy.py`: `NetworkPolicy.check(url)` **перед каждым** запросом — `https` (или явный `insecure_http=true`), host/port по allowlist (`*.huggingface.co`, `*.hf.co`, `github.com`, `api.github.com`, `objects.githubusercontent.com`, `corp_base`), `userinfo` запрещён, credentials не переносятся между origin; в корпоративном каталоге публичного fallback нет (У38, T-16).
- [ ] `net/http.py` полный: `ETag`/`If-None-Match`, `Retry-After`/`RateLimit` → backoff, 403/429 без циклов, jitter 0–10 мин (утилита) / 0–30 мин (модели), кэш `update-cache.json`, connect 2 с / total 3 с; прокси из `urllib.request.getproxies()`; `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`; `.netrc` не читается + unit на fault-server (200/304/429/407/таймаут/redirect-evil/6 редиректов/`Authorization`-ловушка).
- [ ] `core/policy.py` полный (У16/У37, T-19): читать только если uid 0 и `mode & 0o022 == 0`, иначе `policy-ignored` в лог + строка в «О программе»; «есть, но невалиден» → secure-по-умолчанию для зависимых операций (сеть выкл., трек B) + баннер; `catalog_url`/`updates_url`/`corp_base` только `https`.
- [ ] `net/hf.py`: `GET /api/models/{repo}/refs` → `targetCommit(branch)` vs `pinned_revision` установленной по **экспорт-репо** (Д3); ветка оригинала `ai-sage/GigaAM-v3` — информационный сигнал; без `x-repo-commit` → «неизвестно». `net/github.py`: `releases/latest` → `tag_name` (один префикс `v`) → SemVer vs `__version__` + unit на записанных JSON.
- [ ] `updates/checker.py`: расписание ≤ 1/24 ч на источник, сначала кэш, случайная задержка, гейт (тумблеры/offline/policy/`HF_HUB_OFFLINE`); «Проверить сейчас» — без лимита частоты, с уважением 429; `last_attempt_at` и `last_success_at` раздельно (У41, T-36); события `update_check` в статистику.
- [ ] Строка-статус 17 состояний спеки §2.2 (`disabled · policy-locked · checking · uptodate 3000 мс · available · admin-track · downloading · verifying · polkit · installing · restart · error-net · error-sig · error-polkit · unavailable · model-update · skipped`; кликабельны 5/6/11/12/13/14/15/17) — референс `04-footer-states.png` (+`-dark`); панель «Что нового» спеки §6.1 — состояния без установки (установка — M8), «Пропустить эту версию», «Напомнить позже» (US-7.6, 7.7) — референс `04-update-panel.png`.
- [ ] Обновление ревизии модели (US-6.1, 6.2): бейдж «Обновление доступно» + строка `model-update` + одно уведомление; «Обновить» → staging → sha256 → смоук → `current.json` (состояния 9 · 10 · 11); провал → «Новая ревизия не прошла проверку, оставлена текущая»; ничего не скачивается само (S8-A1); «Вернуть предыдущую» — если дёшево (P2), с проверкой `revoked[]`.
- [ ] Манифест с `catalog_url` той же частотой + анти-откат M6; «Новое» ≤ 30 дней; «снята с каталога» (US-6.3).
- [ ] Раздел «Сеть и обновления» — референс `04-network.png` (+`-dark`): три тумблера (US-10.1), офлайн-режим (перекрывает оба, скрывает «Скачать» с сети), URL каталога/обновлений, «Проверить сейчас», «Обновить из файла…» (действие — M8), «Доверенные ключи → Импортировать trust.json» (действие — M8); трей «Проверить обновления» активно всегда, кроме офлайн/policy (Р9).
- [ ] CLI: `tools/validate model-updates`, `tools/validate privacy --network-denied` (полный), `tools/validate updater --check-only`.
### Definition of Done
- S6-A1…A3, S8-A1…A4/A6/A7 зелёные; `tcpdump` за сессию — только хосты `PRIVACY.md`, в офлайне — 0 (Ц4); GUI готов ≤ 1,5 с без сети, модалок нет; T-16, T-19, T-23, T-26, T-36 зелёные; скриншоты 17 состояний = референс.
### Validation
```sh
pytest -m unit tests/net tests/updates/test_checker.py tests/core/test_policy.py -q
tools/validate model-updates                     # фейковый HF: refs изменилась → бейдж; только main → нет; 429 → тишина; < 24 ч → нет проверки
tools/validate privacy --network-denied          # offline / policy / HF_HUB_OFFLINE=1 → 0 запросов
sudo tcpdump -nn -i any 'tcp[13]&2!=0 and not host 127.0.0.1' -w /tmp/session.pcap &   # 30 мин работы → хосты только из PRIVACY.md
ss -tnp | grep -E 'bootstrap.py|astra-voice'
HTTPS_PROXY=http://127.0.0.1:3128 tools/validate updater --check-only     # 407 → unavailable ≤ 3 с, повтор ≤ 1
xvfb-run -a pytest -m xvfb tests/ui/test_statusbar_states.py -q            # 17 × 2
```
### Known Risks
R6 HF из РФ, GitHub 60/ч анонимно · корпоративный MITM-прокси с CA (легитимен — системный CA) · policy-файл на машине заказчика отсутствует — тесты на фикстурах.
### Stop-and-Fix
- Любой хост вне allowlist в pcap → блокер v0.2; policy-невалидный трактуется как «нет policy» → блокер (У37).

## M8. Обновлятор, трек A `[ ]` — v0.2
### Goal
Root-помощник по условиям T1 §4.1 (1)–(10), polkit-действие, `app_updater`, панель 14 состояний, «Обновить из файла», `trust.json`; реальный апгрейд через **одно** окно polkit; T2-ревью без blocker; требования T1 §5 «M8» закрыты.
### Tasks (US-12.2, 7.2, 7.5, **12.5**; компоненты `helper/update_helper.py`, `updates/{app_updater,track}.py`, `security/trust.py`, `data/polkit/*.policy`, `scripts/release.sh`; синтез Р8, §7 И8/Н5/О3; T1 У1–У4, У14, У15, У24, У25, У29–У32)
- [ ] Помощник `/usr/libexec/astra-voice/update-helper` (root:root 0755): shebang `#!/usr/bin/python3 -IS`, stdlib only (без `import astra_voice`, без правок `sys.path`); `os.environ.clear()` + `PATH=/usr/sbin:/usr/bin:/sbin:/bin`, `DEBIAN_FRONTEND=noninteractive`, `LANG=C.UTF-8`; argv строго `install <абс.путь>` → иначе `usage` код 64 (T-03, T-04).
- [ ] Копия через fd (У1/У29/У30): `open(O_RDONLY|O_NOFOLLOW|O_CLOEXEC)` → `fstat` regular, ≤ 200 МиБ; staging `/var/lib/astra-voice/staging` открыт `O_DIRECTORY|O_NOFOLLOW`, `fstat` uid 0 / 0700 / не symlink → иначе `bad-staging` (T-01, T-02); каталог транзакции `staging/<pid>-<rand>/` (`mkdir 0700`, `O_CREAT|O_EXCL`), `flock` на `staging/.lock` до выхода apt → второй экземпляр `busy` (T-29); **все три** файла (`.deb`, `SHA256SUMS`, `.asc`) через fd, SUMS разбирается из тех же байт, ровно одна строка на имя (T-30).
- [ ] Проверки по копии: `gpgv --status-fd 1` с релизным keyring `/usr/share/astra-voice/keys/release.gpg` и пином fingerprint (помощник знает **только** релизный keyring — У31, T-31) → `hashlib` sha256 → `dpkg-deb --field` (`Package=astra-voice`, `Architecture=amd64`) → `dpkg --compare-versions <new> gt <installed>` иначе `downgrade` (У4, T-07).
- [ ] И8/У14: помощник сам читает `/etc/astra-voice/policy.conf` и `/etc/digsig/digsig_initramfs.conf` как данные → `policy-denied` при `updates=admin`/`offline`/`DIGSIG_ELF_MODE≠0` (T-17); повторная проверка непосредственно перед apt (У37).
- [ ] `apt-get install --no-remove -y ./x.deb` фиксированным argv, `-o Dpkg::Options::=--force-confold`, **без** `--allow-unauthenticated`; `--no-download` при `policy.offline`/нет сети, недостающие зависимости → «передайте администратору» (Н5); lock → `apt-locked` без kill (T-18); JSON в stdout, коды выхода; `syslog(LOG_AUTHPRIV)`: версия, sha256[:12], результат (У24, T-27); журнал `/var/lib/astra-voice/updates.log` 0644 (О3).
- [ ] Polkit: `data/polkit/io.github.arrivarus.astra_voice.policy` — действие `…update`, `auth_admin` без `_keep`, `exec.path` = помощник, `message` «Установка обновления Astra Voice» (У25); никаких `rules.d`, никакой привязки к `/usr/bin/apt`.
- [ ] `security/trust.py` (US-12.5): `trust.json {trust_epoch, allowed[], revoked[]}` подписан офлайн-мастером (fingerprint мастера — константа), проверка `gpgv`; хранение `/var/lib/astra-voice/trust/` (пишет помощник) и `~/.local/state/astra-voice/` (GUI); `trust_epoch` монотонен; поставляется в `.deb` и импортируется «Сеть и обновления → Доверенные ключи → Импортировать trust.json»; релизы/каталоги, подписанные ключом из `revoked[]`, отвергаются + unit (epoch ≤ применённого, подпись не мастером → отказ).
- [ ] `updates/track.py`: A = `DIGSIG_ELF_MODE=0` ∧ (`astra-admin` ∨ `sudo`) ∧ `policy.updates≠admin`; B = ЗПС ∨ нет прав ∨ policy; «спросить» = конфиг нечитаем ∧ `zps=auto` → «Не знаю» → B (полный UI трека B — M10) + unit-таблица.
- [ ] `updates/app_updater.py`: скачать `deb + SHA256SUMS + .asc` в `~/.cache/astra-voice/updates/` → `Verifier(release)` → sha256 → имя/arch/версия (имя в SUMS = ожидаемая версия) → `QProcess('pkexec', helper, 'install', path)`; коды 126 → «Установка отменена», 127/нет агента → «Не найден агент авторизации» + подсказка `sudo apt install ./…`; после установки версия через `dpkg-query -W` (О3); «Перезапустить» только по согласию; обрыв → «проверьте журнал», автоотката нет.
- [ ] «Обновить из файла…» (US-7.5): `.deb` + `SHA256SUMS(.asc)` рядом → та же цепочка → трек A/B; без валидной подписи GUI отказывает и подсказывает штатный `apt` (S9-A3).
- [ ] Панель обновления 14 состояний (спека §6.1) + строка-статус 7–14 — референс `04-update-panel.png` (+`-dark`); `PRIVACY.md`: apt может обращаться к репозиториям ОС за зависимостями (У15).
- [ ] Релизная цепочка (US-12.2): `scripts/release.sh` — сборка и подпись в одном job по защищённому тегу (`tag protection` + required reviewer), commit SHA в `SHA256SUMS` (У32); `latest.json`; `docs/SECURITY.md` — обязательный артефакт релиза (в репо и в пакете).
- [ ] Тесты: `tests/unit/test_update_helper.py` — фейки по **абсолютным путям в chroot-каталоге** (не PATH): T-01…T-04, T-07, T-17, T-27, T-29, T-30, T-31; `tools/validate updater --polkit --fixture-upgrade | --all`.
- [ ] ⛔ **T2 security-analyst** на диф `helper/`, `security/`, `updates/`, `net/` (запуск — Юрка).
### Definition of Done
- S9-A1…A7 на машине заказчика: реальный апгрейд `0.2.0~rc1 → 0.2.0` через **одно** окно polkit (KDE; Fly — S17-A5 в M10); подменённый deb отвергнут и удалён; старый подписанный deb → `downgrade`; T-01…T-07, T-17, T-18, T-27, T-29, T-30, T-31 зелёные; T2 без blocker; условия T1 §4.1 (1)–(10) отмечены в ревью построчно.
### Validation
```sh
pytest -m unit tests/unit/test_update_helper.py tests/updates tests/security -q       # chroot-фикстура, фейки по абс. путям
tools/validate updater --polkit --fixture-upgrade                                       # 0.2.0~rc1 → 0.2.0, одно окно, коды 0/126/127
dpkg-query -W astra-voice; sudo tail -3 /var/lib/astra-voice/updates.log; journalctl -t update-helper --since -10min
strace -f -e trace=execve -o /tmp/helper.trace pkexec /usr/libexec/astra-voice/update-helper install /abs/x.deb; grep -E 'gpgv|apt-get' /tmp/helper.trace   # абсолютные пути, нет --allow-unauthenticated
tools/validate updater --all                                                            # два экземпляра → busy; из файла; policy-denied
```
### Known Risks
R9 нет агента / не в `astra-admin` · polkit во Fly (S17-A5) · T2 может потребовать правок — заложить 1 день · генерация мастера/S1/S2 у заказчика — процедура вне кода (`docs/SECURITY.md`).
### Stop-and-Fix
- Второе окно polkit или блокировка GUI → переделка вызова до R2; любой blocker T2 → релиз не выходит; помощник вызвал что-либо по относительному пути → блокер.

## M9. Автозапуск, уведомления, звуки, разделы, живая тема KDE/Fly `[ ]` — v0.2
### Goal
XDG-автозапуск + экран O5; D-Bus-уведомления с кнопками и запасным баннером (Plasma и `fly-notifications`); звуки У14; разделы «Вывод / Продвинутые / О программе»; живая тема из `kdeglobals` **и** `~/.fly/paletterc`; Klipper-опция; выгрузка модели; терминалы; «Пройти настройку заново».
### Tasks (US-8.2, 1.7, 10.4, 9.1-все, 4.5, 4.6, 3.3, 3.4, 3.6, 2.6, 2.7, 1.5b, 1.8, 4.7-живая + Fly §13; компоненты `platform/autostart.py`, `ui/notify.py`, `core/theme.py`, `qml/sections/{Output,Advanced,About}.qml`, `data/sounds/*.ogg`; T1 У27)
- [ ] `platform/autostart.py`: `~/.config/autostart/astra-voice.desktop` (`Exec=/usr/bin/astra-voice --hidden`, `Icon=astravoice` — имя иконки без дефиса, см. spec §9.1, `X-KDE-autostart-after=panel`, без `OnlyShowIn`); состояние = файл есть и нет `Hidden=true`; чужие ярлыки (`handy.desktop`) не трогаем; выключение удаляет только наш по совпадению `Exec` с каноническим путём, чужой — предупреждение (У27, T-28); путь при старте не переписывается + unit tmpdir.
- [ ] Экран O5 «Автозапуск» (тумблер вкл по умолчанию, текст «что создаётся») — референс `08-onboarding-5-done.png` (+`-dark`); строка «Автозапуск» в «Общих» (`01-general-base.png`).
- [ ] `ui/notify.py` полный: `QtDBus` `Notify` с действиями, `GetCapabilities` перед кнопками, `ActionInvoked`/`NotificationClosed`, таймаут 1 с → запасной баннер в окне/пилюле с тем же текстом и кнопкой (S17-A4); тексты без распознанного содержимого + интеграционный тест с тестовым сервером уведомлений (с `actions` и без).
- [ ] Звуки У14 (A-15): `data/sounds/{start,stop}.ogg` (восходящий/нисходящий, ≤ 120 мс, −18 dBFS), тумблер выкл по умолчанию; при отключении пилюли — предложение включить звук один раз (US-4.5); способ воспроизведения — решить `paplay` vs `pa_simple_write` (Stop-and-Fix).
- [ ] Раздел «Вывод» — референс `03-output.png` (+`-dark`): комбинация вставки, задержка, восстановление буфера, защита истории Klipper (во Fly — неактивна «нет менеджера истории буфера», S17-A8; предупреждение о `~/.fly/clipboard` по итогам S4), пробел в конце / заглавная (US-3.6), список терминалов с `fly-term` (US-3.3).
- [ ] Раздел «Продвинутые» — референс `05-advanced.png` (+`-dark`): потоки ORT, выгрузка модели 5/15/60/никогда (US-2.6), лимит записи 30–300 с, порог тишины, «совместимость индикатора» (override-redirect), «надёжный режим evdev» (P2 — скрыт).
- [ ] Раздел «О программе» — референс `06-about.png` (+`-dark`): версия моно + дата сборки, версии Python/PyQt5/onnxruntime/onnx-asr, активная модель с ревизией, GPL-3.0-or-later, «Лицензии компонентов и моделей» (NOTICE), «Приватность» (PRIVACY.md), дисклеймер о знаках, пути с «Открыть», «Статистика» (§10, «Очистить»), «Пройти настройку заново» (US-1.8), «Обновить из файла…», статус policy (`policy-ignored`), даты попытки/успеха проверок, `SessionKind`.
- [ ] `NOTICE` полный (компоненты + 12 моделей; Whisper по моделям раздельно; CC-BY атрибуция; sherpa-onnx §4(d) — если адаптер включён), `PRIVACY.md` полный — те же файлы в `/usr/share/doc/astra-voice/` (US-10.4).
- [ ] Очередь записей с индикацией (US-2.7, S14-A2); выгрузка по простою → S2-A5 `loading-model`; онбординг 1.5b (конфликт с именем действия KDE/Fly).
- [ ] `core/theme.py` `ThemeSource` FLY: `~/.fly/paletterc` (`ColorScheme` `*Dark*`/`*Light*`, иначе яркость `BackgroundColor`) + watcher на `paletterc` и `current.themerc`; настройка «Тема: авто / светлая / тёмная»; живая смена ≤ 2 с в KDE; во Fly — ≤ 2 с либо подсказка «перезапустить» (S13, S17-A7); палитру Qt не читаем + unit фикстуры AstraLight/AstraDark/generated.
- [ ] Старт `--hidden` ≤ 1,5 с до трея (монитор влияния Fly), аудио не открывается (`wpctl status` без потока), модель грузится в фоне после трея.
- [ ] CLI: `tools/validate autostart`, `tools/validate e2e --suite v0.2 --session kde|fly`.
### Definition of Done
- S11-A1…A4, S12-A1…A3, S13-A1/A2, F13-acceptance зелёные **в обеих сессиях**; после перелогина (KDE и Fly) — трей есть, окно не открыто, аудио не открыто; во Fly пункт виден в `fly-admin-autostart`; T-28 зелёный; уведомление с кнопкой приходит через Plasma и `fly-notifications`, при убитом владельце шины — баннер ≤ 1 с; скриншоты разделов 03/05/06 × 2 темы = референсы (DesignReviewer PASS).
### Validation
```sh
pytest -m unit tests/platform/test_autostart.py tests/core/test_theme.py -q
xvfb-run -a pytest -m xvfb tests/ui/test_sections.py tests/ui/test_notify_fallback.py -q
tools/validate autostart                                   # файл, Exec канонический, без OnlyShowIn, чужие не тронуты
# перелогин (KDE, затем Fly):
pgrep -c -f 'bootstrap.py app'; wpctl status | grep -c astra-voice                           # 1; 0
gdbus call --session -d org.freedesktop.Notifications -o /org/freedesktop/Notifications -m org.freedesktop.Notifications.GetCapabilities   # 'actions'
kwriteconfig5 --file kdeglobals --group General --key ColorScheme BreezeLight                  # KDE: перекраска ≤ 2 с; Fly: fly-admin-theme
tools/validate e2e --suite v0.2 --session kde && tools/validate e2e --suite v0.2 --session fly
```
### Known Risks
Fly-модуль «Уведомления» убивает владельца шины в KDE (баннер закрывает) · `CenterWindowPos`/анимации fly-wm · воспроизведение `.ogg` без новых Depends (A-15) · `QT_QPA_PLATFORMTHEME=fly` и диалоги.
### Stop-and-Fix
- `.ogg` не играется без новых Depends → `paplay` subprocess (есть в стоке) либо wav + `pa_simple_write` — запись в журнал, спека остаётся (.ogg в комплекте); автозапуск во Fly не стартует → разбор фаз `X-KDE-autostart-phase` до R2.

## R2. Релиз v0.2 «Каталог, обновления, автозапуск» `[ ]` — 15.10 (⛔ T3, ⛔ G5)
- [ ] ⛔ **T3 security-analyst до тега** (первый публичный релиз с обновлятором): релизный чеклист `security-gate`; руками Юрки — T-12, T-13, T-14, T-18, T-25 (`tcpdump` 30 мин = 0 без тумблеров), T-29, T-34, T-35.
- [ ] Чеклист v0.2 беклога: S6, S7, S8 (A1–A4 A6 A7), S9, S11, S15, F13/F14-acceptance; US-6.6/US-12.5 acceptance; ≥ 3 модели замерены (Ц6); `tcpdump` подтверждает Ц4; Ц3 ≤ 5 мин при 10 Мбит/с и ≤ 2 мин из файла на чистой ВМ (секундомер).
- [ ] **P21½ боевой прогон в обеих сессиях** — `tools/validate e2e --suite v0.2 --session kde` и `--session fly`; 0 ошибок в консоли/логах.
- [ ] Зеркало весов `models-2026.10` в GitHub Releases (ассеты ≤ 2 ГБ) выложено; `catalog.json` ссылается на него.
- [ ] `scripts/release.sh v0.2.0` (`docs/SECURITY.md` в пакете); changelog-writer; ⛔ G5; откат = предыдущий deb (`dpkg --compare-versions` не даст downgrade из GUI — только `apt install ./старый.deb` админом).

## M10. Корпоративный контур, трек B, Fly-адаптеры `[ ]` — v1.0
### Goal
`policy.conf` с замками, «только отечественные», корпоративный URL + `catalog_pubkey` + `latest.json`, трек B + детект ЗПС + вопрос, `INSTALL-ADMIN.md`, документ для админа ИБ, SBOM, NOTICE/PRIVACY/дисклеймер; S17 A1–A9 во Fly зелёные.
### Tasks (US-10.2, 9.4, 5.9, 10.3, 7.3, 7.4, 10.6, 12.4, 10.7, 8.5-часть 2; компоненты `core/policy.py`, `models/catalog.py`, `updates/track.py`, `qml/sections/*`, документы; синтез §7 Д2; T1 У14, У16, У31, У37, У38)
- [ ] `policy.conf` полный: `profile/offline/catalog_url/updates_url/updates/zps/domestic_only/autostart_default/catalog_pubkey/insecure_http/ca_bundle`; `profile=secure` ⇒ производные; замки «Задано администратором» на всех зависимых строках (US-9.4); `examples/policy.conf` в `/usr/share/doc`.
- [ ] Фильтр «только отечественные» (6 моделей; автоматически в secure) (US-5.9) — референс `02-models-catalog.png` (замки).
- [ ] Корпоративный каталог (US-10.3): `catalog_url` + `Verifier(catalog)` с `catalog_pubkey` (keyring root-owned под `/etc/astra-voice/`), `sources[].type=corp` относительно `corp_base` (Д2), без публичного fallback (У38); `latest.json` с `updates_url` (F9); перекрёстная подпись → `purpose mismatch` (T-31); `tools/catalog_cli.py` (P2, если дёшево).
- [ ] Трек B UI (US-7.3, 7.4): «Скачать пакет для администратора» → папка с `.deb`, `SHA256SUMS`, `.asc`, `INSTALL-ADMIN.md`; «Открыть папку»; вопрос «Включена ли ЗПС? Да / Нет / Не знаю → B» (S10-A4); `pkexec`/`apt` не вызываются (S10-A2) — референс `04-update-admin.png` (+`-dark`); строка-статус `admin-track`.
- [ ] Документы: `INSTALL-ADMIN.md` (проверка `gpgv` по fingerprint мастера, `apt install`, ЗПС — подпись 3 `.so` по процедуре заказчика в v1.1, R17 блокировка интерпретаторов — исключение для `python3`), «Влияние на среду функционирования ALSE» для админа ИБ (tech-writer, US-10.6; ФСТЭК № 117 п. 39 / № 239 п. 29.3.3 — сверка живым ИБ-практиком), SBOM в релизе (US-12.4), дисклеймер о знаках в README/«О программе» + согласие на имя (US-10.7, G5).
- [ ] Fly-адаптеры — закрытие списка §13.2 плана #1 (US-8.5 часть 2): полный прогон S17-A1…A9 во Fly, фиксы по расхождениям из R1/R2-чеков (`TrayIconProvider`, пилюля/анимации, `fly-term`, клип-менеджер, polkit-агент Fly S17-A5, автозапуск S17-A6, тема S17-A7, буфер S17-A8).
- [ ] S16: `apt remove`/`purge` не трогает `$HOME`; подсказка «Удалить модели и настройки» с путями в «О программе».
- [ ] CLI: `tools/validate policy --matrix`, `tools/validate corporate --no-public-egress`, `tools/validate e2e --suite s17 --session fly`.
### Definition of Done
- S10-A1…A4, S16, S17-A1…A9, F14/F15-acceptance зелёные; `profile=secure` → 0 соединений и 6 карточек; корпоративный манифест на локальном HTTPS с `catalog_pubkey`: модель скачивается и становится активной; T-17, T-19, T-25 (secure), T-31 зелёные; документы в пакете.
### Validation
```sh
pytest -m unit tests/core/test_policy.py tests/updates/test_track.py tests/security/test_purpose.py -q
tools/validate policy --matrix                      # secure/offline/updates=admin/domestic_only/0666/symlink/невалидный → secure-по-умолчанию
tools/validate corporate --no-public-egress         # локальный HTTPS + catalog_pubkey → скачивание; tcpdump: только corp_base
tools/validate e2e --suite s17 --session fly        # A1–A9
sudo apt remove astra-voice && ls ~/.config/astra-voice ~/.local/share/astra-voice   # S16: данные на месте
```
### Known Risks
Согласие на имя (L1) — вне кода, гейт G5 · ВМ с `policy.conf` и корпоративным HTTPS нужна · ЗПС-ВМ — v1.1 · `fly-admin-policykit-1` — редактор, не агент.
### Stop-and-Fix
- Любое публичное соединение в secure-профиле → блокер v1.0; S17 красный по пункту, требующему нового спайка → запись в журнал, срок R3 пересматривается Юркой.

## M11. Полировка `[ ]` — v1.0
### Goal
Полный английский, клавиатура У13 и кольца фокуса, «Отладка» + сбор диагностики, «близкая модель», «пропустить ревизию»; DesignReviewer 24 экрана × 2 темы; soak 8 ч; Ц1 — Handy удалён.
### Tasks (US-9.3, 9.6, 11.4, 6.4, 6.5; компоненты `core/i18n.py`, `diag/collect.py`, `qml/sections/Debug.qml`; референсы `07-debug.png` и все экраны)
- [ ] `core/i18n.py`: Qt Linguist (`qsTr`/`translate`), `data/i18n/en.ts/.qm`, `pylupdate5`/`lrelease` на сборке, плюрализация `%n`; смена языка с явным «Перезапустить»; CI — `lupdate` без непереведённых (US-9.3).
- [ ] Клавиатура У13 (Tab-порядок сайдбар → строки → строка-статус, стрелки, Ctrl+PgUp/PgDn, Esc, Space/Enter), кольца фокуса У1, accessible-имена; контраст ≥ 4,5:1 по токенам (US-9.6).
- [ ] «Отладка» `Ctrl+Shift+D` — референс `07-debug.png` (+`-dark`): уровень логов, «Открыть лог», «Собрать диагностику» — zip без аудио/текста, без домашних путей, без `user:pass@` (T-13); тестовый экран микрофона (US-11.4); `tools/validate diagnostics --redaction`.
- [ ] «Близкая модель» (US-6.4, P2): `GET /api/models?author=…` по `update_watch.authors`, баннер без «Скачать» (S8-A5); «Пропустить эту ревизию» (US-6.5).
- [ ] DesignReviewer: скриншоты 24 экранов × 2 темы под xvfb против `design/refs/*.png` (карта — спека §14); ≥ 1 несовпадение = FAIL → раунд правок (≤ 2).
- [ ] `tools/validate soak --hours 8` (диктовки по таймеру через виртуальный микрофон; RSS воркера/GUI без роста; 0 ошибок в логах).
- [ ] Ц1: удаление Handy с машины заказчика по его согласию — действие заказчика/Юрки, не кода; фиксируется чеклистом G5.
### Definition of Done
- `lupdate` без непереведённых; DesignReviewer PASS 24 × 2; `tools/validate soak --hours 8` — 0 ошибок, RSS стабилен; T-13 зелёный с диагностикой; чеклист G5 v1.0 готов.
### Validation
```sh
xvfb-run -a pytest -m xvfb tests/ui/test_screens_vs_refs.py -q      # 24 × 2 → tests/_screens/, отчёт DesignReviewer
pylupdate5 -noobsolete src/**/*.py qml/**/*.qml -ts data/i18n/en.ts && grep -c 'type="unfinished"' data/i18n/en.ts   # 0
tools/validate diagnostics --redaction && tools/validate soak --hours 8 --session kde
```
### Known Risks
QML 5.15 без `font.features` · английские хвосты в платформенных диалогах (`QFileDialog`) · soak на машине заказчика занимает рабочий день.
### Stop-and-Fix
- FAIL DesignReviewer после 2 раундов → расхождение выносится заказчику вопросом, не угадыванием; рост RSS в soak → утечка ищется до R3.

## R3. Релиз v1.0 `[ ]` — 31.10 (⛔ G5) · далее v1.1
- [ ] Чеклист v1.0: S17 (A1–A9), S10 (A1–A4), S16, F15; NOTICE/PRIVACY/дисклеймер; SBOM; `docs/SECURITY.md`; согласие на имя (L1) — или релиз с открытым вопросом в G5.
- [ ] P21½ полный прогон S1–S17 в обеих сессиях (`tools/validate e2e --suite all --session kde|fly`); 0 ошибок в консоли/логах; T2 на дифы `helper/security/updates/net` с v0.2.
- [ ] `scripts/release.sh v1.0.0`; changelog; ⛔ G5; проверка гипотезы PRD §1.4 — **2026-11-15** (напоминание Юрки).
- **v1.1 (после v1.0, ориентир 31.12):** ГОСТ-подпись 3 `.so` (`bsign-integrator`, ключ организации), регистрация ключа админом, ВМ с ЗПС — Ц5, S10-A5, US-12.3; user-bundle C1 (`make user-bundle`, self-replace без polkit, самопроверка зависимостей, чистая ВМ §14.5); Ed25519 vs ГОСТ для релизной подписи — к живому ИБ-практику; provenance/attestation CI.
