# Челлендж синтеза (Codex)

- Модель: gpt-6-astra
- Reasoning effort: high
- Дата: 2026-09-09
- Sandbox: read-only
- Вход: arch/plan-synth.md (+ plan-claude.md, plan-codex.md)

---

Reading additional input from stdin...
OpenAI Codex v0.153.4
--------
workdir: /home/astra/Документы/astra-voice
model: gpt-6-astra
provider: openai
approval: never
sandbox: read-only
reasoning effort: high
reasoning summaries: none
session id: 01a084e3-a6f0-7c31-8ea6-5eb83c0b19f6
--------
user
Ты рецензент архитектуры. Прочитай /home/astra/Документы/astra-voice/arch/plan-synth.md - это синтез двух независимых планов (arch/plan-claude.md - базовый, arch/plan-codex.md - заимствования). Синтез двух планов сам по себе никем не проверен. Найди НЕСОСТЫКОВКИ синтеза по четырём осям и только по ним: (1) интерфейсы - согласованы ли контракты между компонентами, взятыми из разных планов (IPC-протокол #1 при отмене через RunOptions из #2; AudioSource в воркере #1 против правил MicRecovery #2; requests-клиент #1 против сетевых правил #2; TrackResolver #2 внутри модели процессов #1); (2) модель данных - манифест каталога: раскладки #1 §4.1 + правила защиты #2 + PRD §7.3 (engine/layout, files[], ревизии, sha256 не-LFS), settings.json/policy.conf, measurements.json, update-cache; (3) инварианты - не противоречат ли друг другу объединённые инварианты безопасности (#1 §6 и #2), решение Р5 о смене модели (MemAvailable-порог) с PRD §8 и S14, решение Р2 (3 .so, без шима) с реальностью ORT-колёс; (4) отказы - что происходит при краше воркера во время записи/вставки, при обрыве apt в root-helper, при обрыве загрузки модели на смоуке, при потере SNI/полкит-агента, при двух экземплярах. Для каждой находки: где именно (файл/раздел), в чём несостыковка, минимальная правка. Не предлагай новую архитектуру и не пересматривай рамки decisions/log.md. Формат: таблица находок (серьёзность blocker/major/minor) + список "проверено, чисто". До 150 строк. Файлы не меняй.
codex
Прочитаю синтез, исходные планы, PRD и журнал решений; проверю только четыре указанные оси. Файлы менять не буду.

exec
/bin/bash -lc "pwd; rg --files -g 'AGENTS.md' -g '*plan*' -g '*prd*' -g '*PRD*' -g 'log.md' -g '*S14*'" in /home/astra/Документы/astra-voice
 succeeded in 0ms:
/home/astra/Документы/astra-voice
arch/plan-synth.md
arch/plan-codex.md
arch/plan-claude.md
docs/test-plan.md
docs/plans.md
PRD.md
decisions/log.md

exec
/bin/bash -lc 'ls -la; wc -l arch/plan-synth.md arch/plan-claude.md arch/plan-codex.md PRD.md decisions/log.md; cat arch/plan-synth.md' in /home/astra/Документы/astra-voice
 succeeded in 0ms:
total 304
drwxr-xr-x  9 astra astra   4096 Sep  8 17:44 .
drwxr-xr-x 13 astra astra   4096 Sep  7 15:49 ..
drwxr-xr-x  8 astra astra   4096 Sep  9 09:37 .git
-rw-r--r--  1 astra astra    358 Sep  7 15:49 .gitignore
drwxr-xr-x  2 astra astra   4096 Sep  9 09:16 .patches
-rw-r--r--  1 astra astra   2662 Sep  8 18:12 HEARTBEAT.md
-rw-r--r--  1 astra astra  35149 Sep  7 16:32 LICENSE
-rw-r--r--  1 astra astra 133105 Sep  8 18:01 PRD.md
-rw-r--r--  1 astra astra   6672 Sep  7 16:24 PROJECT.md
-rw-r--r--  1 astra astra   2098 Sep  7 16:32 README.md
drwxr-xr-x  2 astra astra   4096 Sep  9 09:38 arch
-rw-r--r--  1 astra astra  21208 Sep  8 17:59 backlog.md
drwxr-xr-x  2 astra astra   4096 Sep  7 17:53 decisions
drwxr-xr-x  5 astra astra   4096 Sep  8 18:07 design
drwxr-xr-x  2 astra astra   4096 Sep  7 15:49 docs
drwxr-xr-x  2 astra astra   4096 Sep  8 17:54 research
-rw-r--r--  1 astra astra  45475 Sep  7 17:35 stories.md
-rw-r--r--  1 astra astra   6514 Sep  9 09:35 time.local.md
    82 arch/plan-synth.md
   352 arch/plan-claude.md
   300 arch/plan-codex.md
   641 PRD.md
   216 decisions/log.md
  1591 total
# Astra Voice — синтез архитектуры (суд Юрки, P12)

> 2026-09-09. Входы: `arch/plan-claude.md` (#1, Fable 5.1, 352 строки) и `arch/plan-codex.md` (#2, GPT-6 Astra xhigh,
> 300 строк). Оба независимы, оба прочитаны Юркой целиком; факты среды перепроверены read-only на машине.
> Статус: **черновик до челленджа Astra (P12, шаг 4b) и модели угроз T1**.

## 1. Рубрика (1–5)

| Критерий | План #1 (Claude) | План #2 (Astra) | Комментарий |
|---|---|---|---|
| Корректность | **5** | 4 | #1 сверил среду командами (ORT-колёса без `__isoc23_*`, apt-пакеты, KWin-атомы); #2 держит шим и «4 .so» как допущение. Ошибка #1: «PT Root UI/PT Mono нет на ALSE» — **неверно**, пакеты `fonts-pt-root-ui`/`fonts-pt-mono` есть в `repository-main` и стоят на машине |
| Простота | **5** | 3 | #1: звук в воркере, ноль PCM через IPC; #2: `QAudioInput` в GUI + `memfd` в воркер — больше движущихся частей |
| Соответствие ограничениям | 5 | 5 | оба в рамках журнала решений; оба заменили minisign на GPG/`gpgv` (обоснованно — `minisign` в apt Astra нет) |
| Риск | 4 | **5** | #2 сильнее по границам доверия: отмена через `RunOptions.terminate`, ELF-инвентарь как гейт CI, защита путей манифеста, hardening CI, MemAvailable перед сменой модели |
| Тестируемость | 5 | 5 | #1 — тест у каждого компонента + привязка к историям; #2 — `tools/validate`/`benchmark` как CLI и протокол измерений |
| **Итого** | **24** | **22** | базовый — **#1**, заимствования из #2 — ниже, с предпосылками |

## 2. Что совпало (принято без спора)
Два процесса (GUI ∥ воркер, крах ORT не роняет GUI, ≤3 рестарта/10 мин) · `socketpair` + JSON с `hello`/версией протокола ·
рантайм v1 **`onnx-asr` + `onnxruntime`** (12/12 позиций, цифры протокола сняты на нём; `sherpa-onnx` — второй адаптер за
интерфейсом `Engine`, не «тихий fallback») · хоткей `python3-xlib`/`XGrabKey` + чтение `kglobalshortcutsrc` · вставка
`QClipboard(text/plain)` + XTest, восстановление буфера, терминалы по `WM_CLASS`, `x-kde-passwordManagerHint` · пилюля —
отдельное QML-окно Tool/Frameless/StaysOnTop/DoesNotAcceptFocus + EWMH руками, без `requestActivate` · трей `QSystemTrayIcon`
(SNI), меню `QMenu` · Silero VAD ONNX в воркере, окно GigaAM < 24 с · подпись **GPG/`gpgv`** с закреплённым keyring и в GUI,
и в root-помощнике · root-помощник — Python `python3 -I` под `pkexec`, своё polkit-действие `auth_admin` без `_keep`, копия в
root-staging 0700 и повторная проверка, `apt-get install --no-remove ./deb` с фиксированными аргументами · XDG autostart без
`OnlyShowIn`, путь не переписывается · `settings.json` + `policy.conf` → эффективная конфигурация · тема из `kdeglobals` до
`engine.load` · `Theme.qml` генерируется из `tokens.json` · один `.deb`, всё из apt Astra + вендорные `onnxruntime`/`onnx-asr`,
сборка одной командой в контейнере Debian 12, SBOM, релиз с подписью · спайки до кода · вехи по релизам v0.1/v0.2/v1.0.

## 3. Развилки и решения Юрки

| # | Развилка | #1 | #2 | Решение | Предпосылка / проверка |
|---|---|---|---|---|---|
| Р1 | Где захват звука | в воркере, `libpulse-simple` через ctypes, PCM не покидает процесс | в GUI, `QAudioInput` в QThread, PCM в воркер через `memfd` | **#1** | спайк S5 доказывает ctypes-захват на PipeWire-pulse; провал → схема #2 (memfd) как запасная, интерфейс `AudioSource` общий |
| Р2 | Шим `__isoc23_*` и число `.so` | не нужен (проверено `objdump`: ORT 1.29.0 max GLIBC_2.28), **3 `.so`** | нужен, 4 `.so` | **#1** + гейт #2 | CI: `objdump -T` по вендорным `.so` (max ≤ GLIBC_2.36, 0 `isoc23`) и **счётчик ELF = 3** останавливает сборку при лишнем |
| Р3 | Версия ORT | 1.29.0 (без `sympy`) | 1.23.2 | **1.29.0**, пин sha256 | S3: `import onnxruntime` + инференс на Debian 12 и ALSE; откат — 1.24.4 (GLIBC_2.27, +`python3-sympy` из apt) |
| Р4 | Отмена распознавания | сообщение/kill воркера | `RunOptions.terminate` + точки между сегментами, ≤1 с; kill+reload = непрохождение S14 | **#2** | S3 доказывает ≤1 с для GigaAM и Whisper; kill+reload — только аварийное восстановление |
| Р5 | Смена активной модели | смоук новой ревизии, R13: если `MemAvailable` < 2× — выгрузить текущую | временный второй воркер с кандидатом, старая обслуживает записи; пик двух моделей ≤600 МБ не влезает | **гибрид**: переключение на границе сессии; кандидат грузится во **временный воркер только если `MemAvailable ≥ 1,5× веса кандидата + 200 МБ**, иначе выгрузка → загрузка с состоянием `switching` (спека) | PRD §8 «≤600 МБ» = установившийся режим; пик при смене ограничен `MemAvailable`, не константой. **PRD §8 дополнить одной строкой** |
| Р6 | Сеть | `python3-requests` в `QThreadPool` | `QNetworkAccessManager` | **#1** (requests) + правила #2 | jitter 0–10/0–30 мин, ETag/304, `Retry-After`/RateLimit, 403/429 без циклов, deadline 3 с с редиректами, системный CA + `SSL_CERT_FILE`, редиректы только на разрешённые хосты |
| Р7 | Позиция пилюли | `availableGeometry` | + проверка панелей через EWMH struts | **оба** | S1 проверяет панель > 48 px и боковые панели; расхождение `availableGeometry` ↔ struts → struts |
| Р8 | Шрифты | R11 «нет на ALSE — вкладывать» | выбирать через `QFontDatabase` | **Depends: `fonts-pt-root-ui`, `fonts-pt-mono`** (в repository-main), в пакет не вкладывать; резерв PT Astra Sans через `QFontDatabase` | факт Юрки 2026-09-09: `apt-cache policy` — кандидат 1.001/1.003 из download.astralinux.ru |
| Р9 | Ручная проверка обновлений | разрешена | разрешена, S9-A5 назван ошибкой | **разрешена** (решение 2026-09-08, F12 уже поправлен) | **PRD S9-A5 поправить** под F12 |
| Р10 | Меню трея | рисует Plasma (DBusMenu) — стиль спеки §9.2 недостижим | — | **#1**: меню системное; DesignReviewer сверяет состав и неактивность пунктов, не стиль | запись в спеку §9.2 |
| Р11 | Валидация вех | команды pytest/скрипты | `tools/validate <тема>` и `tools/benchmark` как CLI | **#2** поверх #1 | CLI создаются вместе с функцией; в execution-pack команды из них |
| Р12 | Загрузчик/манифест — защита | sha256, `/resolve/{rev}/`, атомарность, смоук | + запрет `..`/абсолютных путей/symlink при импорте, лимиты (≤64 файлов/модель, ≤2 ГиБ/файл, каталог ≤1 МиБ), git-SHA1 ≠ sha256, диск ×1,2, зеркала отдают одинаковые байты | **объединить** | тесты на вредоносные пути в unit |
| Р13 | CI | lint/unit/xvfb/engine/deb/release | + ABI-pipeline (ELF inventory, readelf, реальный инференс), packaging install→upgrade→purge, две сборки на воспроизводимость, Actions по SHA, секрет подписи вне PR-job | **объединить** | — |

Корневой развилки (разные последствия для продукта) **нет** → ⛔ G4 не требуется.

## 4. Синтез-план (базовый #1 + заимствования)
- **Процессы:** GUI (`astra-voice`, PyQt5 + QtQuick Controls 2, трей, хоткей, вставка, сеть, обновлятор) ∥ воркер
  (`astra-voice.worker`: `PulseSimpleSource` → кольцевой буфер → VAD → `OnnxAsrEngine`; модель в памяти; `measure`) ∥
  одноразовый `update-helper` (root). IPC — `socketpair`, len-prefixed JSON (`hello` с версиями протокола/сборки/рантайма,
  `model.load/unload`, `record.start/stop/cancel`, `transcribe.file`, `measure`; события `state/level/silent/limit/result/
  empty/error/model.loaded/measure/cancelled`). Отменённый `utterance_id` никогда не вставляется.
- **Компоненты и модули** — таблица #1 §3 (30 модулей) целиком; дополнения из #2: `Отмена ASR` (RunOptions), `MicRecovery`
  (сырой пик до VAD, перезапуск WirePlumber только кнопкой), `TrackResolver` (детект `DIGSIG_ELF_MODE` как данные, «не знаю» → B).
- **Рантайм и каталог:** `onnx-asr` 0.12.0 + ORT 1.29.0 (пины sha256, `packaging/wheels.lock`); раскладки манифеста — #1 §4.1
  (7 layout), `engine: onnx-asr` для всех 12; размеры для №2–3 пересчитать по istupakov-репо (#2). Манифест — правила #2
  (закрытый enum layout, полный `files[]`, `/resolve/{sha}/`, подпись каталога → схема → пути → sha256).
- **Безопасность:** инварианты #1 §6 (10) ∪ #2 (12): helper не доверяет GUI, не принимает URL/аргументы apt; policy применяется
  к операциям; неподписанное не активируется; аудио только в ОЗУ; хосты из PRIVACY; иконка «слушаю» обязательна. → **T1**.
- **Упаковка:** #1 §7 (структура, Depends из repository-main + `fonts-pt-root-ui`, `fonts-pt-mono`; 3 `.so`; `python3 -I`
  лаунчер; vendor в `/usr/lib/astra-voice/vendor`) + гейты #2 (ELF-инвентарь, ABI-аудит, воспроизводимость, SBOM CycloneDX,
  hardening Actions). Зеркало весов — релиз `models-YYYY.MM`.
- **Вехи:** M0 спайки S1–S5 (#1) с датами #2 (до 13.09) → M1 скелет/упаковка → M2 воркер/движок → M3 звук → M4 цикл
  диктовки → M5 онбординг/первая модель → **v0.1 30.09** → M6 каталог → M7 сеть/проверки → M8 обновлятор A → M9 автозапуск/
  уведомления/звуки/разделы → **v0.2 15.10** → M10 корпоративный контур/трек B → M11 полировка → **v1.0 31.10**; v1.1 — ГОСТ.
  Пороги: S3 p95 инференса ≤ 300 мс (иначе дефолт e2e_ctc, затем `SherpaEngine`); отмена ≤ 1 с; RSS воркера ≤ 450 МБ.

## 5. Что ещё нужно от человека (не G4 — рутинные допуски)
1. Спайк S3 качает модели с HF (226 МБ минимум, ~680 МБ для трёх вариантов GigaAM) в scratch-venv — «да» на трафик/диск.
2. Спайк S2 (polkit-помощник) требует **одного `sudo`** на машине: положить тестовый `.policy` и помощник. Делать на машине
   заказчика или готовить ВМ ALSE 1.8 (её же — для Fly-сессии S1 и чистого прогона Ц3)?
3. Владелец закрытого ключа GPG для подписи релизов: заказчик (ключ у него, CI подписывает через секрет) или Юрка/репо.

## 6. Правки в документах по итогам суда
- PRD: S9-A5 → ручная проверка разрешена (как F12); §8 память — «≤600 МБ в установившемся режиме; пик при смене модели
  ограничен `MemAvailable`, не константой»; §7.3 пример манифеста — `engine: onnx-asr` для всех записей.
- Спека §9.2: меню трея — системное (DBusMenu), сверка по составу. Spec §8.1 — struts.
- `decisions/log.md`: запись «Архитектура принята (синтез)» после челленджа Astra и T1.

exec
/bin/bash -lc 'cat arch/plan-codex.md' in /home/astra/Документы/astra-voice
 succeeded in 0ms:
# Astra Voice — архитектурный план #2 (Codex)

> **Движок:** OpenAI Codex CLI 0.153.4, модель `gpt-6-astra`, `model_reasoning_effort=xhigh`, sandbox `read-only`.
> **Дата:** 2026-09-09. **Запусков:** 1 (полный бриф одной командой, без деления; ~15 мин, 391 442 токена, exit 0).
> **Команда:** `codex exec -m "gpt-6-astra" -c model_reasoning_effort="xhigh" -s read-only "<бриф>" </dev/null` из `/home/astra/Документы/astra-voice`.
> **Ответ полный** (4 секции: компоненты · упаковка · вехи · риски/тесты/инварианты), обрывов не было. Файлы Codex не менял.
> Ниже — сырой финальный вывод Codex без правок.

---

Предлагаю **onnx-asr как единственный рантайм v1**, отдельный процесс распознавания, PyQt5/QML для интерфейса и GPG/gpgv для подписей. Рамки сохраняются. Файлы не изменены; приведённые ниже команды — планируемые проверки, а не результаты выполненных тестов.

При расхождениях применяю приоритет: последние решения → PRD 0.3 → принятая дизайн-спека и tokens.json → исследования.

| Расхождение в документах | Как учитывать в реализации |
|---|---|
| В `design/spec.md` осталась пометка «черновик» | G3 уже принят в журнале решений |
| S9-A5 и некоторые строки дизайна запрещают ручную проверку при выключенных автопроверках | Ручная проверка разрешена; блокируют только offline/policy |
| PRD требует смены модели с работающей старой и суммарно ≤600 МБ | В обычном режиме — ≤600 МБ; временный пик двух моделей измерять отдельно. Если 600 МБ — абсолютный предел, требования конфликтуют; это зафиксировать до реализации переключения |
| PRD F3 связывает пилюлю с темой; принятая спека задаёт фиксированную палитру | Окно настроек меняет тему; пилюля использует `color.fixed.*` |
| В §9 tokens.md остались «easing не задан» и примеры Qt 6 | Брать кривые из актуального tokens.json; реализовывать средствами Qt 5.15 |
| В исследованиях фигурируют 222 МБ и 415 МБ | Для выбранного экспорта — 226,4 МБ по каталогу; 415 МБ — исторический замер Handy, не измерение нового воркера |

1. Архитектура строится вокруг независимых состояний записи, распознавания, установки моделей и обновления приложения.

| Компонент | Ответственность | Зависит от | Как тестировать |
|---|---|---|---|
| **GUI-процесс** | `QApplication` + `QQmlApplicationEngine`, трей, окна, контроллеры. Не импортирует ORT. Тяжёлые операции вне GUI-потока | Системный PyQt5 | Готовность трея ≤1,5 с; отзывчивость во время загрузки/инференса; RSS без модели ≤120 МБ |
| **DictationController** | Сессия с `utterance_id`, окном назначения и поколением воркера. Не более одного распознавания и одной дополнительной записи; вставки строго по порядку | Хоткей, аудио, WorkerSupervisor, PasteService | Перестановка событий, двойное отпускание, очередь заполнена, отмена перед результатом, поздний ответ |
| **WorkerSupervisor** | Запускает новый Python через `exec`, без `fork` состояния Qt. Один постоянный воркер; временный кандидат при смене модели. Крах/зависание не роняет GUI; максимум 3 автоперезапуска за 10 мин | `subprocess`, Unix IPC | SIGSEGV/SIGKILL, EOF, зависание, несовместимая версия протокола; четвёртый сбой требует действия пользователя |
| **IPC** | Наследуемый `AF_UNIX socketpair`; сообщения JSON с длиной, версией протокола и ID. PCM передаётся через FD `memfd`, после записи — запечатанный от изменения. Без pickle и localhost TCP | `SCM_RIGHTS`, `QSocketNotifier` | Частичные сообщения, неверная длина, неизвестная команда, лишние FD, утечки дескрипторов |
| **ASR-воркер** | `load / recognize / cancel / shutdown`; модель постоянно в памяти. Только CPUExecutionProvider. Потоки по умолчанию `min(4, physical_cores)`, inter-op=1; отключён spinning в простое | onnx-asr, ORT, системный NumPy | Все 12 пресетов; холодный запуск; ≥50 тёплых прогонов дефолта; idle CPU ≤1% |
| **Отмена ASR** | Отдельный управляющий поток принимает cancel; проверка между сегментами/шагами декодера и `RunOptions.terminate` для выполняемого ORT-вызова. Минимальный сопровождаемый патч onnx-asr, если API не проводит RunOptions | ASR-адаптер | S14: готовность к новой записи ≤1 с, без выгрузки модели. Убийство и загрузка за 5 с не считаются прохождением этого теста |
| **Глобальный хоткей X11** | `python3-xlib`, `XGrabKey`, Press/Release; Ctrl+Space PTT, toggle вторым назначением. Caps/NumLock, смена раскладки, autorepeat, отпускание модификаторов, восстановление X-соединения | X11/XKB | ru/en, удержание, повтор клавиши, отпускание Ctrl раньше Space, смена раскладки, suspend/resume |
| **Редактор хоткея** | Читает `kglobalshortcutsrc`, показывает действие KDE. Конфликт — предупреждение; `BadAccess` при реальном grab — ошибка. Временный захват клавиатуры только во время настройки, освобождение по Esc/потере окна | Хоткей, QML | Alt+F2, дубликат PTT/toggle, отказ grab; системные сочетания не переназначаются автоматически |
| **AudioCapture** | `QAudioInput` в отдельном QThread, PulseAudio backend поверх PipeWire. Открытие при хоткее/явном тесте; PCM mono 16 kHz, либо согласованный формат с ресэмплингом в воркере | QtMultimedia, аудиосервер | Виртуальный источник; отключение USB; неподдержанный формат; переполнение; быстрый отпуск до открытия |
| **VAD и сегментация** | Silero VAD ONNX с MIT, отдельный от исключённого Silero STT. До 20 с — один фрагмент; длиннее — границы по паузам, каждый вход GigaAM <24 с. Непрерывная речь — перекрытие и сборка стыков с учётом временных меток | ORT в воркере | Тишина, шум, слабый голос; 19,9/20,1/23,9/24,1 с; речь без пауз; повторяющиеся слова на стыке |
| **Ограничения записи** | <0,3 с — молчаливое игнорирование; 120 с по умолчанию, настройка 30–300 с. Лимит останавливает захват и запускает обработку сразу; двухсекундный текст пилюли не задерживает ASR | AudioCapture, Controller | Границы длительности, отмена, монотонные часы; максимум PCM16 — 9,6 МБ на 300 с |
| **MicRecovery** | Сырой пик до VAD: <−60 dBFS за 2 с → «Микрофон молчит». Обычный retry 3×300 мс; после входа — до 3 повторов через 1 с. Перезапуск WirePlumber только кнопкой пользователя | `systemctl --user`, QProcess | Нулевая амплитуда, mute, greeter-гонка, отсутствующая служба. После restart — повторное открытие и проверка ≤3 с |
| **PasteService** | Запоминает `_NET_ACTIVE_WINDOW` до пилюли. Только текстовый CLIPBOARD → 50 мс → XTest-комбинация → восстановление через 100 мс. Восстанавливает лишь если буфер всё ещё принадлежит нашей операции | QClipboard, X11/XTest | Kate, Firefox, LibreOffice; пользователь скопировал другое во время вставки; медленный получатель; окно закрылось |
| **Терминалы и Klipper** | Правила по WM_CLASS: Konsole/Fly и другие — проверенные комбинации; xterm допускает отдельное правило Shift+Insert. Опция `x-kde-passwordManagerHint`. Последний текст только в RAM | PasteService | Отсутствие `^V`, отсутствие синтетического Enter; CLIPBOARD/PRIMARY; история Klipper действительно не содержит текст |
| **PillWindow** | Отдельный QQuickWindow: Tool, Frameless, StaysOnTop, DoesNotAcceptFocus; skip-taskbar/pager. Без `activateWindow/requestActivate`. Кнопка × остаётся кликабельной | X11/KWin, QScreen, Theme | Активное окно не меняется при показе/скрытии/отмене; fullscreen, Alt+Tab, панели, несколько мониторов |
| **Позиционирование пилюли** | Монитор исходного активного окна, иначе primary; центр по X, 48 px от края рабочей области. Пересчёт при изменении панели, DPR и конфигурации экранов | QScreen, EWMH struts | Панель >48 px, боковая панель, отрицательные координаты, масштаб 100/125/150%, отключение монитора |
| **Трей и уведомления** | `QSystemTrayIcon` + QMenu, SNI через системную интеграцию Qt. Регистрация с retry до 30 с и повторно после перезапуска панели. Notifications D-Bus с actions; ошибки доступны также в окне | QtWidgets, QtDBus, оболочка | Нет watcher/notification service, перезапуск Plasma, Fly; шесть состояний, статичная иконка |
| **SettingsWindow / onboarding** | Шесть разделов + скрытая отладка. Минимум 900×620; завершение onboarding требует модели. Окна и диалоги используют общие компоненты | ViewModel, SettingsStore | S1/S11–S13; Tab/стрелки/Esc, обе локали, обе темы, все состояния макета |
| **ModelRegistry / Activator** | Подписанный каталог, локальные ревизии, фильтры, активная модель. Кандидат загружается и проходит smoke; старая обслуживает записи. Переключение на границе сессий; старый воркер завершается после своих заданий | Загрузчик, Supervisor | Сбой загрузки/смоука, нехватка RAM, диктовка во время переключения, запрещённое удаление активной |
| **ModelDownloader** | Докачка, подпись каталога, sha256 каждого файла, атомарная установка набора, источники и лимиты — ниже | NetworkService, Registry | HTTP-стенд: 206/200/416, обрыв, 429, неправильный Range, диск заполнен, вредоносные пути |
| **RevisionWatcher** | Проверяет установленные экспорты, ветки оригинала и обновление каталога; отдельно сигнал о близких моделях. Только уведомляет | HF API, подписанный каталог, cache | Фикстуры веток main/e2e_rnnt; SHA разных репозиториев; повторные уведомления; offline |
| **AppUpdater / TrackResolver** | Releases API/корпоративный latest.json, SemVer, загрузка и проверка; выбор GUI/admin по policy, ЗПС и правам | NetworkService, gpgv, helper | S6/S9/S10; prerelease, подпись, неверный пакет, отсутствие агента, отказ авторизации |
| **Root-helper** | Единственная привилегированная операция: проверить локальный комплект в root-staging и вызвать apt с фиксированными аргументами | polkit, gpgv, apt/dpkg | TOCTOU, symlink, подмена ключа/пути/версии, apt lock, сбой питания; отдельный security review |
| **Autostart / single-instance** | Один XDG `.desktop`, `Exec=/usr/bin/astra-voice --hidden`, `TryExec`, `X-KDE-autostart-after=panel`, без OnlyShowIn. Создание только по изменению настройки | XDG, QLockFile/локальный IPC | Перелогин KDE/Fly; файл не переписывается при старте; второй запуск открывает первый; Handy не затрагивается |
| **SettingsStore / PolicyStore** | Версионированный JSON, миграции, атомарная запись, `.bak` повреждённого файла. Эффективные значения вычисляются в Python; policy нельзя обойти через QML/CLI | `/etc/astra-voice/policy.conf`, XDG | Повреждённый JSON, новая схема, read-only home, policy-lock, сброс настроек |
| **Theme / i18n** | kdeglobals до `engine.load`; watcher файла/каталога + D-Bus, реакция ≤2 с. ru/en `.ts/.qm`, плюрализация, форматирование QLocale | Генератор tokens.json | Нет светлой вспышки; атомарная замена kdeglobals; длинный английский текст; отсутствующий шрифт |
| **Логи / метрики / диагностика** | 5×1 МБ логов; stats и measurements отдельно. Только коды, длины, времена. Диагностический ZIP по кнопке, с удалением домашних путей, секретов proxy/URL, без текста и аудио | procfs, SettingsStore | Поиск контрольного текста/секретов в файлах; ротация; очистка; закрытые права доступа |

`RunOptions.terminate` предусмотрен ORT, но достижимость ≤1 с для всех декодеров должна быть доказана спайком, включая Whisper. [API ORT](https://onnxruntime.ai/docs/api/python/api_summary.html)

Для пилюли нельзя считать `availableGeometry` достаточным доказательством корректности нескольких мониторов: у Qt/X11 есть ограничения определения рабочей области. Основной API сохраняется, адаптер проверяет панели через EWMH и нормализует координаты. [QScreen Qt 5.15](https://doc.qt.io/archives/qt-5.15/qscreen.html)

| Контракт процессов | Решение |
|---|---|
| Команды | `Hello`, `LoadModel`, `Recognize`, `Cancel`, `Shutdown`; события `Ready`, `Result`, `Cancelled`, `Error`, `Metrics` |
| Проверка совместимости | `protocol_version`, `app_build`, `runtime_version`, `model_id`, `revision`, `generation` |
| Ограничения IPC | JSON ≤64 КиБ, проверка типа/длины PCM, длительность ≤300 с, ограниченная очередь; закрытие неизвестных FD |
| Отмена | Отменённый ID никогда не вставляется, даже если поздний результат корректен |
| Владение моделью | Каждая сессия закрепляет поколение воркера; смена активной модели не перенаправляет уже начатую запись |
| Завершение | Остановить захват, отменить задания, восстановить принадлежащий операции буфер, закрыть FD и воркеры |
| Изоляция | Отдельный процесс защищает GUI от сбоя ORT; сам по себе он не является песочницей против вредоносной ONNX-модели |

Выбор рантайма — внутри разрешённой развилки.

| Критерий | onnx-asr — выбор v1 | sherpa-onnx — альтернатива |
|---|---|---|
| GigaAM v3 e2e_rnnt | Поддержан; собственный пресет под istupakov | Поддержан; совпадает с экспортом Handy/csukuangfj |
| Все 12 моделей | Есть нужные архитектуры: GigaAM, Multilingual, T-one, Vosk, NeMo, два формата Whisper | Для полного каталога остаются непроверенные адаптации T-one/Multilingual и другие экспорты |
| Python/glibc | onnx-asr — pure Python; бинарная граница — ORT. Для ORT 1.23.2 есть cp311 manylinux_2_27/2_28 | Есть manylinux2014-колёса; проверять весь комплект, включая отдельные core-зависимости |
| Собственные `.so` | onnx-asr добавляет 0; план ORT — 3, shim — ещё 1 | Python extension, sherpa core/C API и ORT; точный состав зависит от wheel. Преимущество по числу ELF заранее не доказано |
| RAM/скорость | Проверять на целевом CPU; Python-декодер и preprocessing могут отличаться от Handy | Нативный декодер потенциально выгоднее; локальных сопоставимых замеров пока нет |
| Раскладки | Совпадает с большей частью источников каталога, включая бенчмарченные Whisper | Потребуется изменить источники/размеры, переснять скорость и проверить лицензии экспортов |
| Сопровождение | Узкий RuntimeAdapter; закреплённый wheel и небольшой набор патчей | Возможная замена адаптера и каталога, но не незаметный fallback на тех же файлах |

Поддержка архитектур подтверждается кодом onnx-asr; наличие подходящего тега manylinux подтверждает доступность колеса, **не заменяет запуск на glibc 2.36**. [onnx-asr loader](https://raw.githubusercontent.com/istupakov/onnx-asr/v0.12.0/src/onnx_asr/loader.py), [ORT 1.23.2 на PyPI](https://pypi.org/project/onnxruntime/1.23.2/)

| № | Запись каталога v1 | Репозиторий для onnx-asr | `layout` / обязательные особенности |
|---|---|---|---|
| 1 | GigaAM v3 e2e_rnnt int8 | `istupakov/gigaam-v3-onnx` | `onnx-asr-gigaam-v3`; `v3_e2e_rnnt_{encoder,decoder,joint}.int8.onnx`, vocab/config; 226,4 МБ |
| 2 | GigaAM v3 e2e_ctc int8 | тот же | `onnx-asr-gigaam-v3`; `v3_e2e_ctc.int8.onnx`, соответствующий vocab/config |
| 3 | GigaAM v3 rnnt int8 | тот же | `onnx-asr-gigaam-v3`; `v3_rnnt_*`, обычный словарь; без пунктуации |
| 4 | GigaAM Multilingual 220M | `istupakov/gigaam-multilingual-ctc-onnx` | `onnx-asr-gigaam-multilingual`; 224,8 МБ |
| 5 | T-one | `t-tech/T-one` | `onnx-asr-t-one`; fp32, 144,2 МБ; без kenlm |
| 6 | Vosk ru 0.54 | `alphacep/vosk-model-ru` | `vosk-onnx`; `am-onnx/` + `lang/tokens.txt`; 72,5 МБ |
| 7 | Vosk small ru 0.52 | `alphacep/vosk-model-small-ru` | `vosk-onnx`; `am/` + `lang/tokens.txt`; 26,7 МБ |
| 8 | Whisper large-v3-turbo int8 | `onnx-community/whisper-large-v3-turbo` | `onnx-community-whisper`; encoder, merged decoder, tokenizer/config; 1 089,1 МБ |
| 9 | Whisper small int8 | `onnx-community/whisper-small` | `onnx-community-whisper`; 253,5 МБ; без выдуманных WER/RTFx |
| 10 | Whisper base int8 | `istupakov/whisper-base-onnx` | `onnx-asr-whisper-ort`; beamsearch-граф и tokenizer; 109,1 МБ |
| 11 | GigaAM Multilingual Large | `istupakov/gigaam-multilingual-large-ctc-onnx` | `onnx-asr-gigaam-multilingual`; 591,6 МБ |
| 12 | NeMo FastConformer ru pc CTC | `istupakov/stt_ru_fastconformer_hybrid_large_pc_onnx` | `onnx-asr-nemo`; CTC-граф, vocab/config; 131,6 МБ; CC-BY |

Для №2–3 таблица чисел содержит размеры sherpa-экспортов. До выпуска каталога нужно дополнить источник истины точными байтами выбранных istupakov-наборов; переносить 224,9/229,3 МБ механически нельзя. Файлы этих вариантов присутствуют в закреплённом репозитории. [Экспорт GigaAM](https://huggingface.co/istupakov/gigaam-v3-onnx/tree/322c3b29492673eb7d0b434bfa9dfb8653e34d02)

| Манифест и загрузка | Конкретный контракт |
|---|---|
| Идентичность | Все записи имеют `engine=onnx-asr`; `layout` — закрытый enum из таблицы. ID модели, вариант и precision не выводятся из имени файла |
| Полный состав | `files[]` содержит все реально используемые ONNX, tokenizer, vocab/config и external-data. Вложенность репозитория, включая `onnx/`, сохраняется |
| Ревизии | Полные commit SHA из [catalog-numbers.md](/home/astra/Документы/astra-voice/research/catalog-numbers.md); URL только `/resolve/{sha}/…` |
| Цепочка доверия | Сначала подпись каталога закреплённым ключом, затем схема, ограничения путей и sha256 всех файлов. Git SHA-1 мелких файлов не принимается за sha256 |
| Зеркала | HF → GitHub Releases → corporate при сетевой ошибке/404/429. Все зеркала обязаны отдавать один набор байтов |
| Корпоративный профиль | Корпоративный каталог может содержать только corp-источники; публичного fallback в таком случае нет |
| Докачка | `.part` + состояние `{revision, path, expected_size, sha256, validator}`. Продолжать только при корректном 206/Content-Range; 200 — начать заново; 416 — проверить полноту либо перезапустить |
| Проверка | Размер и sha256 потоково; несовпадение — удалить повреждённую загрузку, показать ошибку. Не назначать фактическую сумму «ожидаемой» |
| Атомарность | Staging на том же файловом разделе → `fsync` файлов/каталога → rename набора → smoke → атомарный `current.json` |
| Откат | Старая ревизия остаётся до следующего успешного запуска новой. Незавершённая транзакция восстанавливается при старте |
| Лимиты | Одно задание загрузки, до 2 файлов одновременно; каталог ≤1 МиБ, ≤64 файлов/модель, ≤2 ГиБ/файл; лимиты явно версионированы |
| Диск | Перед началом минимум `remaining_bytes ×1,2`, с учётом staging и сохраняемой ревизии; ENOSPC обрабатывается и во время записи |
| Кэш | Последний проверенный каталог и ответы API; bounded-кэш обновлений, очистка устаревших `.part`; установленные модели не удаляются как кэш |
| Локальный импорт | Та же проверка. «Своя модель» — только поддержанный layout и явный локальный манифест; никаких скачанных Python-плагинов |
| Защита путей | Запрет абсолютных путей, `..`, symlink/hardlink при импорте, выхода external-data за каталог; отсутствие распаковки произвольных архивов |
| Метрики карточек | WER только по согласованному протоколу; `benchmark_fp32/no_data` сохраняются. Vosk — зарубежные; domestic-фильтр содержит 6 моделей |

| Проверки сети и ревизий | Поведение |
|---|---|
| Общая точка выхода | NetworkService на QNetworkAccessManager; все проверки и загрузки проходят NetworkPolicy |
| По умолчанию | Никаких запросов, в том числе «проверки доступности HF», до разрешённого действия |
| Offline | `policy.offline`, пользовательский offline или `HF_HUB_OFFLINE=1` запрещают новые запросы и отменяют текущие |
| Планировщик | App: jitter 0–10 мин; модели: 0–30 мин; после готовности GUI. Не чаще 24 ч; повторные запуски приложения не сбрасывают backoff |
| Таймаут | Проверка — общий deadline 3 с, включая редиректы; ≤1 повтор внутри бюджета. Для загрузки отдельно connect/inactivity timeout |
| Кэш/лимиты | ETag/304, `Retry-After`, HF RateLimit, GitHub reset; 403/429 не вызывают цикл повторов |
| Экспорт модели | Сравнивать `(repo, branch, observed_sha)` только с предыдущим SHA того же repo/branch |
| Оригинал Сбера | Отдельно отслеживать `ai-sage/GigaAM-v3:e2e_rnnt`. Его SHA не равен SHA `istupakov/gigaam-v3-onnx` |
| Разрешение установки | Новая ветка оригинала — информационный сигнал. Кнопка установки появляется, когда подписанный каталог описал совместимый экспорт с новыми sha256 |
| Близкие модели | Белый список авторов/семейств, ограниченная выдача; баннер без установки до включения в каталог |
| TLS/proxy | Системные CA и `SSL_CERT_FILE`; HTTP(S)_PROXY/NO_PROXY; проверка TLS обязательна. Редиректы проверяются по разрешённым источникам/CDN |
| Runtime | onnx-asr получает только локальный путь и offline resolver; `[hub]`, HF-токены и автоматическое скачивание из воркера отсутствуют |

Обновлятор использует **GPG для выпуска, gpgv для проверки**. Ключ задаётся явным root-owned keyring; пользовательский GPG keyring не участвует. Отзыв и ротация ключей требуют собственной политики: gpgv доверяет всем ключам переданного keyring и сам не решает задачу отзыва. [Документация gpgv](https://www.gnupg.org/documentation/manuals/gnupg/gpgv.html)

| Шаг обновления | Действие |
|---|---|
| 1. Обнаружение | Releases `latest` / corporate latest.json; нормализация одного `v`, SemVer; prerelease не предлагается стабильному каналу |
| 2. Выбор трека | Читать `/etc/digsig/digsig_initramfs.conf` как данные, не shell. `DIGSIG_ELF_MODE≠0`, `zps=on`, `updates=admin` или отсутствие прав → B. Неизвестно → предусмотренный вопрос; «не знаю» → B |
| 3. Загрузка | `.deb`, `SHA256SUMS`, `SHA256SUMS.sig`, инструкция. Текст Release отображается безопасно, без внешних картинок/активного HTML |
| 4. Проверка GUI | Проверить подпись SUMS, затем хеш точного файла; имя, arch, версия. Повреждённый пакет удалить |
| 5A. Авторизация | Один `pkexec /usr/libexec/astra-voice-update-helper …`; собственное action `auth_admin`, без `_keep`, без `rules.d`, без привязки к apt |
| 6A. Root-проверка | Helper повторно читает policy/ЗПС; принимает только локальный комплект. Безопасно копирует в root-staging 0700, затем повторяет подпись/хеш |
| 7A. Установка | Проверяет `Package=astra-voice`, `Architecture=amd64`, повышение версии через `dpkg --compare-versions`; фиксированный `apt-get install --no-remove ./…deb`, без shell и пользовательских аргументов apt |
| 8A. Результат | Проверить установленную версию через dpkg-query; показать «Перезапустить». Перезапуск только кнопкой; текущую запись сначала завершить/отменить |
| 5B. Администратор | Сохранить проверенный комплект, «Открыть папку»; pkexec/apt не запускать |
| Из файла | Тот же verifier и тот же выбор трека; неподписанный deb через GUI не устанавливается |
| Ошибки | Нет агента/отмена → сохранить deb; apt lock → объяснение и повтор; не убивать apt посреди транзакции |
| Сеть apt | Репозитории ОС — отдельная системная стадия, раскрытая в PRIVACY. В offline — `--no-download`; недостающие зависимости передаются администратору |
| После установки | Не смешивать новый код воркера со старым GUI: проверять build/protocol; операции, требующие нового процесса, ждут перезапуска |

Кастомные компоненты соответствуют [макету](/home/astra/Документы/astra-voice/design/mockups/final/index.html), без переноса HTML-стилей буквально в Qt.

| Компоненты | Реализация Qt 5.15 |
|---|---|
| `PillWindow`, `LevelBars` | QQuickWindow, Repeater из 9 Rectangle; обновление уровня около 30 Гц только во время записи; 36 px, ширина 172–320, без elide |
| `ModelCard`, `MetricBar`, `Badge` | Явные состояния ViewModel; 20 состояний карточки; максимум два бейджа; Popup «Как мы считаем» |
| `HotkeyCapture`, `KeyChip` | Нативный X11 backend + QML-представление семи состояний; PT Mono |
| `SegmentedControl`, `PolicySwitch`, `SettingRow` | Controls 2; переключатель — один Tab-stop; строка toggle кликабельна целиком; policy-lock объясняется текстом |
| `StatusBar`, `UpdatePanel`, `OnboardingShell` | Общие контроллеры состояний; 17/14 состояний обновления; переиспользование карточек и микрофонного теста |
| `Theme.qml`, `PillTheme.qml` | Генерация из tokens.json с проверкой типов, единиц, alpha, дробных размеров и Font.Weight; ни одного ручного дубля токенов |
| Иконки и тени | 21 собственный SVG, включая clock. Перекраска через QSvgRenderer/image provider или QtGraphicalEffects; не Qt 6 MultiEffect |
| Шрифты | Выбрать установленный шрифт через QFontDatabase по порядку fallback; строка CSS family-list не считается рабочим fallback Qt 5 |
| Заголовок/меню/уведомления | Заголовок KWin, трей QMenu, уведомления оболочки. Размер внешней рамки измеряется отдельно от QML client area |
| Software rendering | Отдельный прогон; при неподдержанных эффектах — кэшированные растровые тени, без смены принятой геометрии |

2. Поставка — один `astra-voice_<version>_amd64.deb`; системный GUI и NumPy берутся из apt.

| Слой | Состав |
|---|---|
| GUI | `python3`, `python3-pyqt5`, `.qtquick`, `.qtsvg`, `.qtmultimedia`; `qml-module-qtquick2`, `-window2`, `-layouts`, `-controls2`, `qml-module-qtgraphicaleffects` |
| Интеграция | `python3-xlib`, Qt multimedia plugins, `libpulse0`, `pulseaudio-utils`, `xdotool` для аварийного режима; системные polkit/pkexec и gpgv |
| Python из apt | `python3-numpy`, `python3-packaging`, `python3-jsonschema`; зависимости ORT: protobuf, flatbuffers, sympy, humanfriendly и их зависимости |
| Вендоренные wheel-кандидаты | `onnx_asr-0.12.0-py3-none-any.whl`; `onnxruntime-1.23.2-cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl`; `coloredlogs-15.0.1-py2.py3-none-any.whl`, поскольку пакет не обнаружен в локальном apt-index |
| Фиксация | Это начальный набор спайка. До v0.1 — точные версии, SHA256, проверка METADATA против apt-версий и проверка известных уязвимостей; после этого никакого `latest` |
| Не поставлять | pip-Qt, pip-NumPy, torch, transformers, GPU runtime, huggingface_hub/hf_xet, sherpa одновременно с onnx-asr |
| Ресурсы | Каталог + подпись, ключ, переводы, SVG, два OGG, лицензированный тестовый WAV; небольшой VAD ONNX как отдельный технический ресурс с хешем/NOTICE |
| ASR-веса | В deb не входят; скачивание/локальный импорт отдельно |
| Python isolation | Root-owned private vendor directory; запуск `python3 -I` через bootstrap. Не изменять системный site-packages и не использовать пользовательские `.pth` |
| Maintainer scripts | Без сети, pip, GUI, изменения домашнего каталога; purge не удаляет данные пользователей |
| Размер/время | Проверить ≤80 МБ без ASR-весов и сборку ≤5 мин; холодную подготовку среды и повторную сборку показывать отдельно |

Минимальные зависимости onnx-asr допускают системный NumPy; hub является отдельной опцией. [pyproject v0.12.0](https://raw.githubusercontent.com/istupakov/onnx-asr/v0.12.0/pyproject.toml)

| Объекты будущей ГОСТ-подписи | Плановое число |
|---|---:|
| onnx-asr и coloredlogs | 0 ELF |
| ORT: `onnxruntime_pybind11_state*.so`, `libonnxruntime.so.*`, `libonnxruntime_providers_shared.so` | Ожидается 3 |
| `libastra-isoc23-compat.so` | 1 |
| Python root-helper, GUI-код | 0 ELF |
| **Бюджет собственного комплекта** | **4 `.so`; 3, если shim не требуется** |

Это **плановый инвентарь, не результат распаковки wheel**. CI считает все уникальные ELF по magic, включая `.so.*` и транзитивные библиотеки; лишний ELF останавливает сборку до обновления SBOM. Системные apt-библиотеки учитываются отдельно.

Шим загружается только в ASR-процессе, до ORT, из root-owned пути. Не использовать `/etc/ld.so.preload`, не передавать LD_PRELOAD в pkexec. Проверять реальные необходимые символы и семантику C23; обёртки `strto*` не являются общей заменой glibc 2.38. [Факт из Handy](/home/astra/Документы/handy-gigaam/docs/TROUBLESHOOTING.md:5)

| Структура репозитория | Содержание |
|---|---|
| `src/astra_voice/app/` | Контроллеры, bootstrap, lifecycle |
| `src/astra_voice/platform/` | X11, clipboard, audio, tray, theme, autostart |
| `src/astra_voice/models/` | Схема, пресеты, registry, downloader, revisions |
| `src/astra_voice/worker/` | IPC, RuntimeAdapter, VAD, segmentation, cancel |
| `src/astra_voice/updates/` | Release client, verifier, track resolver |
| `src/astra_voice/ui/` | QML screens/components, ViewModels, i18n |
| `helper/`, `native/` | Минимальный root-helper; C23 shim |
| `catalog/`, `resources/` | Подписанные данные, ключи, тестовое аудио, VAD, иконки/звуки |
| `vendor/locks/`, `vendor/patches/` | Wheel-lock с SHA256, provenance и патчи onnx-asr |
| `debian/` | control/rules, desktop, polkit action, policy.conf |
| `tools/` | build-deb, generate-theme, catalog, validate, benchmark, elf-audit |
| `tests/` | unit, integration, qml, packaging, security, fixtures |
| `docs/`, `.github/workflows/` | INSTALL-ADMIN, PRIVACY, threat model, выпуск/CI |

| Сборка и CI GitHub Actions | Проверки/артефакты |
|---|---|
| **Одна команда** | `./tools/build-deb --locked`; фиксированный Debian 12 build image, генерация темы/переводов, применение патчей, staging wheel, dpkg-buildpackage |
| Offline-сборка | `./tools/build-deb --locked --offline` из подготовленного wheelhouse; отсутствие сети внутри сборки |
| PR pipeline | Ruff, unit, schema/preset checks, генератор токенов, Qt 5.15 QML smoke, Xvfb, HTTP-фикстуры |
| ABI pipeline | ELF inventory, `readelf`, аудит GLIBC/GLIBCXX и DT_NEEDED; импорт ORT и реальный inference, а не один импорт |
| Package pipeline | lintian; install → upgrade → remove → purge в чистой среде; сеть maintainer scripts запрещена |
| Model pipeline | На релиз/изменение runtime или пресета — smoke всех 12 закреплённых наборов. Кэш только по хешам; веса не скачиваются на каждый PR |
| Reproducibility | `SOURCE_DATE_EPOCH`, фиксированные входы, две сборки и сравнение; объяснимые отличия не объявляются воспроизводимостью |
| SBOM | SPDX/CycloneDX: apt build/runtime inventory, wheels, native libraries, patches, VAD, ресурсы; отдельно BOM моделей с ревизиями и лицензиями |
| Выпуск | Tag → проверки → deb + corresponding source + SBOM + ELF inventory + SHA256SUMS → GPG-подпись → GitHub Release |
| Защита CI | Actions по commit SHA; минимальные permissions; секрет подписи недоступен PR/fork; ключ вне обычного build job |
| Только ALSE/реальная машина | KWin/Fly, SNI, focus, Klipper, polkit-агент, аудиокарта и WirePlumber после загрузки, Astra apt-источники, реальные RAM/RTFx |
| v1.1 | ГОСТ-подпись полного ELF-инвентаря и проверка на ВМ с включённой ЗПС |

3. Вехи идут по зависимостям: сначала доказать платформу и воркер, затем каталог и привилегированное обновление.

Команды `tools/validate` и `tools/benchmark` ниже обозначают CLI, который следует создать вместе с соответствующей функцией; сейчас таких успешных прогонов не заявляю.

| Срок / веха | Зависит от | Done-критерий | Команды валидации |
|---|---|---|---|
| **09–11.09: спайк пилюли** | apt QtQuick | SNI + прозрачная пилюля; без изменения активного окна; × работает; рабочая область/масштаб; темы до первой отрисовки | `./tools/validate x11-pill --matrix`; `xprop -root _NET_ACTIVE_WINDOW`; `xprop -id <pill-xid>` |
| **09–12.09: спайк ABI/runtime** | Закреплённые wheel-кандидаты | ORT запускается на glibc 2.36, проверен shim; GigaAM e2e_rnnt распознаёт; проверены входы/выходы всех семейств | `./tools/validate runtime --glibc 2.36`; `./tools/elf-audit --strict` |
| **10–13.09: RAM/скорость/отмена** | Runtime | GigaAM warm p95 ≤0,5 с; worker ≤450 МБ; измерен GUI; cancel ≤1 с; определён пик смены модели | `./tools/benchmark --model gigaam-v3-e2e-rnnt-int8 --runs 50 --threads 4`; `./tools/validate worker-cancel` |
| **10–13.09: polkit из GUI** | Тестовый deb/helper | На отдельном стенде ровно один системный диалог; GUI не блокируется; cancel, отсутствие агента, bad signature; root повторяет проверки | `./tools/validate updater --polkit --fixture-upgrade`; `dpkg-query -W astra-voice` |
| **14–20.09: основной тракт** | Все ранние спайки | PTT/toggle → PCM → VAD → ASR → вставка; отмена, очередь, терминальные комбинации, тишина, worker crash | `python3 -m pytest tests/unit tests/integration`; `./tools/validate dictation --virtual-mic` |
| **21–30.09: v0.1 «Диктовка работает»** | Основной тракт | Подписанный deb одной командой; дефолтная модель скачивается/импортируется; пилюля/трей/минимальные настройки; S2–S5, S12, S14; метрики Ц2/Ц7 | `./tools/build-deb --locked`; `./tools/validate release --version 0.1.0`; `./tools/validate e2e --suite v0.1` |
| **01–07.10: каталог и ревизии** | v0.1, verified downloader | 12 пресетов в схеме; обязательные 9 готовы к smoke; T-one и Could проходят тот же путь; подпись/докачка/откат/ветки проверены | `./tools/validate catalog --all`; `./tools/validate downloads --faults`; `./tools/validate model-updates` |
| **05–11.10: обновления/автозапуск** | Polkit-spike, NetworkPolicy | Трек A, безопасная ветка B, import deb, ETag/backoff; XDG и single-instance; нет запросов по умолчанию | `./tools/validate updater --all`; `./tools/validate autostart`; `./tools/validate privacy --network-denied` |
| **12–15.10: v0.2** | Каталог, updater, autostart | Полный onboarding ≤5 мин при 10 Мбит/с и ≤2 мин из файла; ≥3 модели измерены; полный экран настроек, ru/en, Klipper; S1/S6–S9/S11/S13/S15 | `./tools/validate e2e --suite v0.2`; `./tools/benchmark --catalog --minimum-models 3`; `./tools/benchmark wer --model whisper-small-int8 --dataset russian-librispeech-test` |
| **16–23.10: корпоративный режим** | v0.2 | Policy с блокировками, corp-only каталог, offline import, domestic enforcement, полный S10; диагностика и документы администратора | `./tools/validate policy --matrix`; `./tools/validate corporate --no-public-egress`; `./tools/validate diagnostics --redaction` |
| **24–31.10: v1.0** | Корпоративный режим | Все 12 моделей smoke-tested; все применимые S1–S16; T3; release upgrade; длительный прогон без P0/P1; документы и атрибуции; G5 | `./tools/validate release --version 1.0.0`; `./tools/validate e2e --suite all`; `./tools/validate soak --hours 8` |

| Протокол измерений | Правило |
|---|---|
| RAM | Новый процесс на модель/ревизию/настройку потоков; VmHWM после первого inference, дополнительно PSS. HWM предыдущей модели не наследуется |
| Invalidation | Ключ: model ID, revision, quantization, runtime/build, threads, CPU, preprocessing. Смена любого значимого поля сбрасывает локальную достоверность |
| Скорость | Отдельные времена preprocessing, inference, queue, release→text-ready, paste. RTFx = длительность аудио / время обработки; p95 пользовательской задержки включает вставку |
| Выборка | Первый прогон холодный; RTFx — медиана ≥5 тёплых; дефолтная задержка — ≥50 шестисекундных фраз |
| Машина | Core Ultra 7 255U; фиксировать питание, профиль CPU, фоновые процессы и температуру. Проверить 1/2/4 потока без автоматической смены заданного дефолта |
| Качество | WER только на согласованном Russian LibriSpeech test; версия нормализатора фиксируется. Для Whisper small до замера остаётся no_data |
| Виртуальный микрофон | Harness создаёт `module-null-sink` и `module-remap-source`, явно выбирает источник; ждёт `CaptureReady`, затем запускает `paplay --device=av_test …wav`; удаляет только созданные модули |
| Настоящая вставка | E2E-приёмник подтверждает изменение текста; событие отправки Ctrl+V само по себе не доказывает успешную вставку |

4. Риски, альтернативы и границы безопасности должны быть проверяемыми условиями приёмки.

| Риск | Основное решение / альтернатива | Обратимость |
|---|---|---|
| onnx-asr не достигает скорости/RAM | Сначала профилировать preprocessing, декодер, ORT threads/arenas. Затем сравнить sherpa на том же аудио; менять runtime только вместе с пресетами и замерами | RuntimeAdapter и IPC сохраняются; модели потребуется переустановить в другом layout |
| Полный каталог не проходит выбранный ORT | Зафиксировать исправленный совместимый wheel; не добавлять скрыто второй runtime | Замена wheel-lock и повтор всех 12 smoke |
| C23 shim скрывает более широкий ABI-разрыв | Аудит символов; совместимый wheel либо собственная воспроизводимая сборка ORT под базовую glibc | Локальная замена backend-комплекта |
| Cancel не укладывается в 1 с | RunOptions + точки отмены; проверить каждый декодер. Kill/reload — только аварийное восстановление | Патч ограничен adapter/vendor; критерий не ослабляется незаметно |
| Две модели вызывают OOM | Проверка MemAvailable и пика кандидата; при нехватке сохранить старую и отказать в переключении с объяснением | Данные/активная ревизия не меняются |
| Пилюля теряет верхний слой | Повторное применение EWMH hints по событиям; при необходимости X11 bypass-вариант после проверки ×/focus | Один platform adapter |
| QtQuick software backend не рисует эффекты | Кэшированные тени/иконки; анимации только видимых элементов | Не меняет контроллеры и токены |
| WirePlumber restart не помогает | Отличать mute/нет устройства/профиль/ошибку открытия. Предложить диагностику; не менять fly-dm, linger и системные службы автоматически | Только пользовательская служба по кнопке |
| Clipboard/Klipper гонки | Проверка владельца и поколения; настраиваемая задержка; «оставлять текст»/«только буфер» | Настройка, без смены ASR |
| HF SHA постоянно даёт ложное обновление | Раздельные координаты upstream/export; установка только подписанного набора | Миграция update-cache |
| Подмена deb между GUI и helper | Проверка после копирования в root-staging; helper не доверяет флагу «GUI проверил» | Изолированный updater |
| Обрыв apt | Сохранять журнал и системный результат; не обещать транзакционный rollback deb и не запускать автоматический downgrade | Восстановление штатным apt администратором |
| Отозванный/скомпрометированный ключ | Явные fingerprints, версия доверенного набора ключей; ротация через доверенный выпуск/администратора | Отдельный keyring; URL не назначает доверие |
| RAM-only ошибочно трактуется как защита от swap | Нет аудиофайлов; core dumps отключены, audio mappings исключены из dumps. Строгая защита от swap/hibernation зависит от политики ОС | Не требует вмешательства приложения в настройки ОС |
| «Отечественные модели» принимают за сертификацию | Фильтр по принятому юрлицу; NOTICE и документ влияния на среду. Допуск в систему — процедура администратора | Состав каталога управляется подписанным манифестом |

| Уровень тестов | Обязательное покрытие |
|---|---|
| **Unit** | Автоматы состояний, очередь/cancel, миграции и policy, SemVer/Debian version, вычисление метрик, схема каталога, пути, подписи, Range/cache/backoff |
| **Integration** | Реальный worker и ORT; виртуальный PipeWire-микрофон + paplay; HTTP fault server; D-Bus stubs; Xvfb clipboard; crash и заполнение диска |
| **Packaging/security** | Чистый install/upgrade/remove/purge; полный ELF-инвентарь; отсутствие сети в postinst; TOCTOU/symlink/неверный ключ/чужой пакет; T2 на изменениях updater |
| **E2E на ALSE** | KDE и Fly; реальные хоткеи/фокус/трей/polkit/Klipper; Kate/Firefox/LibreOffice/Konsole/fly-term/xterm; перезагрузка, USB, сеть через proxy/CA |
| **Performance/soak** | GUI/RSS/PSS/RTFx; 50 диктовок; 8 часов работы; циклы отмены и смены модели; отсутствие роста FD/памяти |
| **v1.1 отдельно** | Установка и диктовка с настоящей ЗПС и подписанным комплектом; имитация `DIGSIG_ELF_MODE` в v1 проверяет только маршрутизацию UI |

| Инвариант | Где обеспечивается |
|---|---|
| Аудио и распознанный текст не отправляются в сеть и не попадают в логи/диагностику | NetworkService не принимает эти типы данных; тесты с контрольными маркерами |
| Запись начинается только по действию пользователя и имеет видимый индикатор | Controller; при потере всех видимых индикаторов запись останавливается |
| Worker не скачивает модели, не выполняет repo-код и не регистрирует произвольные custom-op `.so` | RuntimeAdapter, локальный resolver, закрытые layouts |
| Неподписанный каталог и непроверенные файлы не становятся активными | Registry/Activator, атомарные транзакции |
| Policy применяется к операциям, а не только к состоянию кнопок | Общий EffectivePolicy для GUI, CLI и updater |
| Root-helper не принимает URL, shell-команду, произвольный keyring или параметры apt | Узкий протокол helper; root-owned конфигурация |
| ЗПС и admin-track нельзя обойти через «Обновить из файла» | Повторная проверка трека в GUI и helper |
| Отмена/крах/смена поколения не приводят к поздней вставке | ID сессии и поколения, единая очередь PasteService |
| Вставка не синтезирует Enter и не начинается без проверенного окна назначения | PasteService; при невозможности назначения — только буфер |
| Установленная рабочая ревизия не удаляется до подтверждения новой | ModelRegistry и журнал транзакции |
| Системные СЗИ, fly-dm, группы пользователя и чужой автозапуск не меняются | Ограниченная область записи пакета/helper |
| В v1 нет обещания готовности неподписанных ELF к ЗПС | INSTALL-ADMIN и трек B; ГОСТ-подпись и фактическая приёмка — v1.1 |

exec
/bin/bash -lc 'cat arch/plan-claude.md' in /home/astra/Документы/astra-voice
 succeeded in 0ms:
# Astra Voice — архитектурный план (Архитектор #1, Claude Fable 5.1)

> Фаза 3 · P11 · 2026-09-09 · независимый план; синтез с планом #2 — за Юркой.
> Входы: `PRD.md` 0.3, `decisions/log.md`, `design/spec.md` 0.2, `design/tokens.md` §9, `design/flows.md`,
> `design/mockups/final/index.html` (список кастомного QML, 21 глиф), `research/*` (summary, tech-platform §A–D,
> tech-models §A4–A5/B–C, tech-codex блоки 2–3/5, catalog-numbers, legal §3–4), `handy-gigaam/docs/TROUBLESHOOTING.md`.
> Среда сверена read-only на машине заказчика (см. §12 «Факты среды»). Рамки из журнала решений не пересматриваются.

## 0. Итог

Два процесса на Python 3.11: **GUI-процесс** (PyQt5 + QtQuick Controls 2, трей `QSystemTrayIcon`, хоткей `python3-xlib`,
вставка через `QClipboard` + XTest, сеть, обновлятор) и **воркер** (захват звука через `libpulse-simple`, VAD, движок
`onnx-asr` поверх `onnxruntime`; модель живёт в его памяти). Связь — `socketpair` с JSON-строками; крах воркера =
перезапуск дочернего процесса, GUI не страдает. Третий, одноразовый, процесс — **root-помощник** через `pkexec`
(одно polkit-действие, повторная проверка подписи в root-staging, `apt-get install --no-remove ./deb`).
Рантайм v1 — **`onnx-asr`** (все 12 позиций каталога одним API, цифры протокола сняты именно на нём, 3 собственных `.so`
против 4–6 у sherpa-onnx; шим `__isoc23_*` для PyPI-колёс ORT **не нужен** — проверено на машине).
Подпись релизов и манифеста — **GPG (Ed25519), проверка `gpgv` с закреплённым keyring** (есть на любой ALSE, нужен и админу трека B).
Один `.deb`: всё из apt Astra + вендорные `onnxruntime` и `onnx-asr`; сборка одной командой в контейнере Debian 12
(та же glibc 2.36 / Python 3.11.2 / PyQt5 5.15.9, `/etc/debian_version` = 12.0).
Порядок сборки — по зависимостям: спайки → скелет+упаковка → воркер+движок → звук → хоткей/пилюля/трей/вставка (= v0.1) →
каталог/сеть/обновлятор/автозапуск (= v0.2) → policy/корп. каталог/трек B/комплаенс (= v1.0).

## 1. Ключевые решения (что · почему · обратимость)

| # | Решение | Почему | Обратимость |
|---|---|---|---|
| Р1 | Два долгоживущих процесса (GUI ∥ воркер), звук **в воркере** | PRD F4.1 (крах ORT не роняет GUI); аудио не пересекает границу процесса — в GUI летят только уровни (~30/с) и текст; ORT и `libpulse` не трогают Qt-loop | IPC-протокол не привязан к тому, где стоит захват: перенос захвата в GUI = добавить сообщение `audio.frames` (день работы) |
| Р2 | Рантайм **`onnx-asr`** + `onnxruntime` (PyPI, CPU) | §4: каталог 12/12, протокол цифр, 3 `.so`, MIT, glibc ok без шима | Интерфейс `Engine` + поле `engine` в манифесте; `SherpaEngine` — второй адаптер, переключение = новые строки манифеста (PRD §1.4 это и предусматривает) |
| Р3 | Хоткей — `python3-xlib` (`XGrabKey` на root, 8 масок Lock/Num/Scroll), события через `QSocketNotifier` на fd Display | Единственный apt-путь с push-to-talk (KGlobalAccel даёт только «нажатие»); без потоков и без root | `evdev` — «надёжный режим» P2 за тем же интерфейсом `HotkeyBackend` |
| Р4 | Пилюля — отдельное QML `Window` (Tool·Frameless·StaysOnTop·DoesNotAcceptFocus) + EWMH-хинты руками (`_NET_WM_STATE_ABOVE/SKIP_TASKBAR/SKIP_PAGER`, `_NET_WM_USER_TIME=0`, тип `NOTIFICATION`) + повторная проверка перекрытия | Спека §8.1, урок Astra Cowork (KWin теряет StaysOnTop); в Fly-сессии WM другой (fly-wm) — хинты EWMH там надёжнее флагов Qt | Запасной режим «совместимость индикатора» = override-redirect (`BypassWindowManagerHint`), тумблер в «Продвинутых» |
| Р5 | Трей — `QSystemTrayIcon` (SNI через Qt) с иконками **по имени** из hicolor; меню — `QMenu` (под SNI рисует Plasma через DBusMenu) | Иконка по имени даёт перекраску `currentColor` под тему панели (контракт спеки §9.1); меню стилизовать нельзя — это Plasma | Пиксмап вместо имени — одна строка; своё SNI по D-Bus — не нужно |
| Р6 | Захват звука — `libpulse-simple` через `ctypes` (16 кГц mono s16le, ресемплинг на сервере), устройство открывается по хоткею | Ноль вендорных `.so`, работает и с PipeWire (pulse-протокол на машине заказчика), и с PulseAudio; список устройств — `pactl list short sources` | `QAudioInput` (apt) или `sounddevice`+PortAudio — за интерфейсом `AudioSource`; тест-источник из wav — тот же интерфейс |
| Р7 | Подпись: GPG Ed25519, `gpgv --keyring /usr/share/astra-voice/keys/release.gpg` и в GUI, и в root-помощнике; корп. ключ — `policy.conf: catalog_pubkey=` (keyring) | `gpgv` есть на каждой ALSE (2.2.40), `minisign` в apt Astra нет — админ трека B не смог бы проверить; `gpgv` без trust-db/агента детерминирован | Интерфейс `Verifier`; minisign через `python3-nacl` (в apt) — 60 строк |
| Р8 | Root-помощник — **Python-скрипт** `/usr/libexec/astra-voice/update-helper` (`python3 -I`), не C-ELF | Ноль собственных ELF под ЗПС; интерпретатор системный (подписан Астрой); логика = копия в root-staging + `gpgv` + sha256 + `dpkg-deb --field` + `dpkg --compare-versions` + `apt-get` | Замена на C-ELF — тот же контракт argv/JSON-stdout |
| Р9 | Тема — парсер `~/.config/kdeglobals` до `engine.load` + `QFileSystemWatcher`; `Theme.qml`/`PillTheme.qml` генерируются из `tokens.json` скриптом, сгенерированный файл коммитится, CI проверяет «чистую» регенерацию | tokens.md §9, урок «палитра Qt врёт»; правка токенов не расходится с кодом | — |
| Р10 | Сеть — `python3-requests` из apt (Range/206, прокси из env, системный CA), без `huggingface_hub`/`httpx` | Меньше вендоринга; все 4 сетевых действия PRD F14 — обычный HTTPS GET | `httpx` 0.23 в apt — тот же `HttpClient` |
| Р11 | Один `.deb`, наш код в `/usr/lib/python3/dist-packages/astra_voice`, вендор в `/usr/lib/astra-voice/vendor` (в `sys.path` добавляет только лаунчер) | Debian-политика для своего кода; вендор не засоряет системный `dist-packages` и не конфликтует с системным numpy | — |
| Р12 | Настройки — `settings.json` + `policy.conf` (INI) → «эффективная конфигурация» с множеством заблокированных ключей; QML видит один `SettingsBridge` | PRD F11.4/F14.3, спека «Задано администратором» | — |

## 2. Процессная модель

```
┌─ astra-voice (GUI, /usr/bin/astra-voice → python3 -m astra_voice) ───────────────────────────────┐
│ main-thread: QApplication + QQmlApplicationEngine · окно 900×620 · пилюля (Window) · QSystemTrayIcon │
│              hotkey (QSocketNotifier на fd Xlib) · QClipboard/XTest · QLocalServer (single-instance) │
│ NetPool (QThreadPool): проверки HF/GitHub (≤3 с), загрузка моделей/deb (Range, sha256, отмена)       │
│ WorkerSupervisor: spawn/restart, socketpair fd → QSocketNotifier, корреляция id запросов            │
└───────────┬───────────────────────────────────────────────┬──────────────────────────────────────┘
            │ socketpair (JSON-lines, UTF-8, len-prefix)     │ QProcess: pkexec (одноразово, трек A)
┌───────────▼──────────────────────────────┐   ┌────────────▼──────────────────────────────────────┐
│ astra-voice-worker (python3 -m astra_voice.worker)  │   │ update-helper (root, python3 -I, polkit auth_admin)│
│ ipc-loop · AudioThread (libpulse read 20 мс,        │   │ argv: install <путь.deb> · копия в root-staging ·  │
│  ring-buffer, уровень, тишина, лимит) · InferThread │   │ gpgv+sha256+dpkg-deb field+compare-versions ·      │
│  (onnx-asr, VAD>20 с, замер VmHWM/PSS)              │   │ apt-get install --no-remove -y ./x.deb · JSON-stdout│
└─────────────────────────────────────────┘   └─────────────────────────────────────────────────┘
```

- **Почему воркер — дочерний процесс, а не поток:** ORT-краш (SIGSEGV/abort) и OOM убивают процесс; GIL не мешает
  (ORT и `pa_simple_read` его отпускают), но изоляция памяти (415 МБ + арена) и «Отмена» одним `kill`/сообщением важнее.
- **Перезапуск:** супервизор поднимает воркер ≤ 3 раз за 10 мин (PRD F4.1); при третьем провале — трей `error`, пилюля
  `error` с текстом «Распознавание перезапущено»; модель после рестарта грузится заново в фоне.
- **IPC-протокол (v1, версия в `hello`):** запросы GUI→воркер: `hello`, `model.load{dir,layout,threads}`, `model.unload`,
  `record.start{device,limit_s,silence_db,insert}`, `record.stop`, `record.cancel`, `transcribe.file{path}` (смоук/тест),
  `measure`, `ping`; события воркер→GUI: `state{idle|recording|processing}`, `level{rms,peak}` (≤ 30/с), `silent`,
  `limit`, `result{text,audio_ms,t_ms,cold}`, `empty`, `error{code,detail}`, `model.loaded{id,revision,load_ms}`,
  `measure{peak_rss_mb,pss_mb}`. Аудио **никогда** не сериализуется и не пишется на диск (§9 инварианты).
- **Бюджет задержки «отпустил → текст» (Ц2, p95 ≤ 500 мс):** `record.stop` <1 мс → хвостовая обрезка ≤ 5 мс →
  инференс 6 с при RTFx ≥ 20 ≈ 300 мс → `result` → задержка вставки 50 мс → `Ctrl+V` через XTest ≈ 5 мс. Итого ≈ 360–420 мс;
  запас мал → спайк S3 обязан замерить RTFx `onnx-asr` на машине; запасной ход — дефолт `e2e_ctc` (+25 %) и потоки ORT.

## 3. Компоненты (модуль · ответственность · вход/выход и состояние · зависит от · тест)

| Модуль (`src/astra_voice/…`) | Ответственность | Состояние / контракт | Зависит от | Тест |
|---|---|---|---|---|
| `app.py` | Бутстрап: argv (`--hidden`, `--show`), single-instance (`QLocalServer` в `$XDG_RUNTIME_DIR`), порядок старта: policy → settings → theme → QML → трей (ретрай ≤ 30 с) → воркер → отложенные проверки | Ничего не хранит | все ниже | e2e: S1-A7, S11-A4; unit: разбор argv |
| `core/paths.py`, `core/version.py`, `core/logging.py` | XDG-пути; `__version__` из `debian/changelog` на сборке; `RotatingFileHandler` 5×1 МБ без аудио/текста | файлы в `~/.local/state/astra-voice/` | — | unit: фильтр логов не пропускает поле `text` |
| `core/settings.py` | `settings.json`: `schema_version`, миграции вперёд, атомарная запись (tmp+fsync+replace), `.bak` при порче, сброс | JSON-документ, сигнал `changed(key)` | paths | unit: миграции v1→vN, битый файл → `.bak` + дефолты |
| `core/policy.py` | `/etc/astra-voice/policy.conf` (INI): `profile/offline/catalog_url/updates_url/updates/zps/domestic_only/autostart_default/catalog_pubkey`; `profile=secure` ⇒ производные | «эффективная конфигурация» + `locked_keys` | settings | unit: таблица истинности secure/offline/updates |
| `core/theme.py` + `qml/Theme.qml`, `qml/PillTheme.qml` | Тема из `kdeglobals` (секция `[General] ColorScheme` + яркость `[Colors:Window] BackgroundNormal`) до `engine.load`; `QFileSystemWatcher` (файл переписывается → re-add) → `Theme.dark` | синглтон QML, свойство `dark` | `scripts/gen_theme.py` ← `tokens.json` | unit: парсер на фикстурах Breeze/BreezeDark/Fly; интеграция: смена файла → сигнал ≤ 2 с (S13) |
| `platform/x11.py` | Своё `Xlib.display.Display`: `XGrabKey/XUngrabKey` (маски × Lock/Num/Scroll), обработчик `BadAccess`, временный grab `Escape` на время записи, `XGrabKeyboard` для поля захвата, XTest-комбинации, `_NET_ACTIVE_WINDOW`/`WM_CLASS`/геометрия, EWMH-свойства окна пилюли, `_NET_CLIENT_LIST_STACKING` (перекрытие) | без состояния, кроме Display | `python3-xlib` (apt 0.33) | интеграция под Xvfb: grab/ungrab, BadAccess при двойном grab, XTest доставляет событие в тестовое окно |
| `platform/hotkey.py` | Автомат PTT/toggle: `Idle→Recording→Processing`, фильтр автоповтора (KeyRelease+KeyPress с одним `time`), порог 0,3 с, лимит 120 с (таймер), конфликт с KDE (парсер `kglobalshortcutsrc`: секция `[app.desktop]`, `key=combo\tcombo,default,описание`), `duplicate`, `not-grabbed` | `HotkeyBackend` (X11 / evdev P2); сигналы `pressed/released/cancelled` | x11, settings | unit: автомат на синтетических событиях; парсер конфликтов на реальном файле (Alt+F2 → «Открыть строку поиска и запуск») |
| `platform/paste.py` | Сохранить все форматы `QMimeData` → положить только `text/plain` (+ `x-kde-passwordManagerHint=secret`, P1) → задержка 50 мс → комбинация (по `WM_CLASS`: терминалы → `Ctrl+Shift+V`) → 100 мс → восстановить; режим «только буфер»; «последний текст» только в памяти | `PasteMethod` = ctrl_v/ctrl_shift_v/shift_insert/clipboard_only | x11, QClipboard | интеграция Xvfb + тестовое Qt-окно с `TextEdit`; e2e S4 (Kate, Konsole, LibreOffice, Klipper) |
| `ui/pill.py` + `qml/Pill.qml` | Контроллер оверлея: 12 состояний спеки §8.4, ширина 172→320 без elide, 9 столбиков `h=max(3,round(l*20))`, позиция по `availableGeometry` экрана активного окна (48 px), таймеры 500/1000/1200/800/3000 мс, показ ≤ 100 мс, re-assert «наверх» + проверка перекрытия | `state`, `levels[9]`, `text` | x11, theme | интеграция: скриншот состояний под Xvfb (DesignReviewer); e2e: фокус остаётся в Kate (S12-A3), Alt+Tab не содержит пилюлю |
| `ui/tray.py` | `QSystemTrayIcon`: 6 состояний иконок по имени (`astravoice-tray-{idle,listening,processing,done,error,nokey}`), подсказки, ретрай регистрации ≤ 30 с (watcher на шине), меню по спеке §9.2 (состав/неактивность), клик — окно; баннер «трей недоступен» | `state` | QtWidgets, notify | интеграция: `QSystemTrayIcon.isSystemTrayAvailable()` под Xvfb (false-ветка); e2e на Plasma и во Fly-сессии |
| `ui/window.py` + `qml/{Main,Sidebar,SettingRow,ModelCard,Onboarding*,Dialogs}.qml` | Окно 900×620 (min), 6 разделов + «Отладка» (Ctrl+Shift+D), строка-статус (5+17 состояний), панель обновления (14), карточка модели (20, state-машина в одном компоненте), поле захвата (7), сегмент, тумблер с замком, Popup «Как мы считаем», клавиатура У13 | мосты `SettingsBridge/ModelsBridge/UpdatesBridge/AudioBridge/AboutBridge` (`QObject`, `pyqtProperty`+`notify`) | theme, все мосты | интеграция: каждый QML-файл создаётся без warnings (`QT_QPA_PLATFORM=offscreen`), скриншоты 24 экранов × 2 темы для DesignReviewer |
| `ui/notify.py` | `org.freedesktop.Notifications` через `QtDBus` (кнопки-действия, `ActionInvoked`), таймаут 1 с на ответ службы → запасной баннер в окне (Fly может убить владельца шины) | — | QtDBus | интеграция с тестовым сервером уведомлений (python-dbus) |
| `worker/supervisor.py` | `subprocess.Popen([sys.executable,'-m','astra_voice.worker'], pass_fds=[sock])`, len-prefixed JSON, `QSocketNotifier`, таймауты на запрос, политика рестартов 3/10 мин, `SIGTERM` при выходе | очередь запросов (≤ 1 запись в очереди, PRD F2.9) | — | unit с фейковым воркером (эхо-скрипт), убийство процесса → рестарт |
| `worker/__main__.py`, `worker/ipc.py` | Цикл IPC, диспетчер команд, аккуратное завершение | автомат `idle/recording/processing` | audio, engine | unit: автомат с `FakeAudioSource` + `FakeEngine` |
| `worker/audio.py` | `AudioSource`: `PulseSimpleSource` (ctypes `libpulse-simple.so.0`, `pa_simple_new(PA_STREAM_RECORD, spec s16le/16k/1, device)`; поток чтения 20 мс; кольцевой буфер в ОЗУ; RMS/пик; «тишина» = пик < −60 дБ за 2 с; лимит; ретраи открытия 3×300 мс; ошибки `ENODEV/EBUSY`), `WavFileSource` для тестов; список устройств — `pactl list short sources` | буфер в памяти, очищается после результата/отмены | `libpulse0` | unit на `WavFileSource`; интеграция на машине: виртуальный источник PipeWire + `paplay` |
| `worker/vad.py` | Silero VAD ONNX (MIT, ~2 МБ, в пакете): сегментация записей > 20 с по паузам без резки слов (окно 24 с GigaAM), обрезка хвостовой тишины; для ≤ 20 с — не применяется | — | onnxruntime | unit: синтетический wav с паузами → границы |
| `worker/engine.py` | `Engine`-протокол: `load(dir, layout, threads)`, `transcribe(np.float32 16k) -> text`, `unload`; `OnnxAsrEngine` (раскладки §4.2), заглушка `SherpaEngine`; потоки ORT = `min(4, физ. ядер)`; постобработка только strip + опции «пробел в конце»/«заглавная» | модель в памяти воркера; выгрузка по простою (таймер в GUI → `model.unload`) | onnx-asr, onnxruntime | интеграция: тестовый wav (6 с, «проверка связи») → ожидаемые слова на трёх GigaAM; смоук каждой раскладки в CI (кеш моделей) |
| `worker/measure.py` | `VmHWM` и `Pss` из `/proc/self/{status,smaps_rollup}` после первого прогона; отдаёт в GUI → `measurements.json`; сброс при смене ревизии/потоков | — | — | unit на фикстурах `/proc` |
| `models/catalog.py` | Манифест: встроенный (`/usr/share/astra-voice/catalog.json` + `.sig`) → последний скачанный (`~/.local/state/.../catalog.json`); `jsonschema` (`catalog.schema.json`), `manifest_version`, `gpgv`-подпись (наш keyring или `catalog_pubkey`), слияние с локальными замерами и статусами; фильтры «язык», «только отечественные» | `CatalogModel` (`QAbstractListModel`) для QML | verify, store | unit: схема, подпись (тестовый ключ), неизвестные поля игнорируются, старший `manifest_version` → отказ |
| `models/store.py` | `~/.local/share/astra-voice/models/<id>/<revision>/` + `current.json`; staging `<rev>.partial/`; удаление, размер на диске, `shutil.disk_usage` (×1,2), `MemTotal` vs `min_ram_mb` | файлы на диске | paths | unit на tmpdir |
| `models/downloader.py` | Загрузка файлов манифеста: `.part`, `Range: bytes=N-` только при 206 с корректным `Content-Range` (200 → сначала), инкрементальный sha256, `fsync`, `os.replace`, источники `hf → github_release → corp` (429/404/сеть → следующий), скорость/ETA, отмена, очередь = 1, пауза «нет места» | сигналы прогресса в `ModelsBridge` | net.http, store | unit: локальный HTTP-сервер (206/200/обрыв/подмена байтов) |
| `models/installer.py` | Установка из файла/папки (те же sha256), смоук через воркер (`transcribe.file` вшитого wav, `expect_any`), атомарная смена `current.json`, откат при провале, «Повреждена — переустановить», выбор активной («Переключаю…» — старая модель работает до `model.loaded`) | — | store, supervisor | интеграция: подмена байта в encoder → смоук провален → откат |
| `net/http.py` | `requests.Session`: UA `astra-voice/X.Y.Z (+repo)`, connect 2 с / total 3 с для проверок, прокси из env, CA из системного хранилища (+`SSL_CERT_FILE`), TLS не отключается, `ETag`/`If-None-Match`, `Retry-After`/`RateLimit` → backoff, кэш `update-cache.json`, гейт «сеть разрешена?» (тумблеры/офлайн/policy/`HF_HUB_OFFLINE`) | кэш на диске | requests | unit: фейковый сервер 200/304/429/407/таймаут; гейт — таблица истинности |
| `net/hf.py`, `net/github.py` | HF: `GET /api/models/{repo}/refs` → `targetCommit` ветки-варианта (`update_watch.branch`), `HEAD …/resolve/{rev}/file` → `X-Repo-Commit`; поиск по `author` (P2). GitHub: `releases/latest` → `tag_name` (один префикс `v`) → SemVer против `__version__` | — | net.http | unit на записанных JSON-ответах |
| `updates/checker.py`, `updates/track.py`, `updates/app_updater.py` | Расписание (≤ 1/24 ч, случайная задержка 0–30 мин, сначала кэш), автомат строки-статуса (17 состояний), определение трека A/B/«спросить» (`DIGSIG_ELF_MODE` из `/etc/digsig/digsig_initramfs.conf`, `policy zps`, группы `astra-admin/sudo`, `policy updates=admin`), загрузка `deb+SHA256SUMS+.asc` в `~/.cache/astra-voice/updates/`, `gpgv`+sha256+имя+arch+версия, `QProcess('pkexec', helper, 'install', path)`, коды 126/127 → «отменена/нет агента», «Обновить из файла», трек B: папка + `INSTALL-ADMIN.md` | `UpdatesBridge` | net, verify, helper | unit: автомат состояний, выбор трека (таблица), проверка подписи; e2e S9/S10 на машине |
| `security/verify.py` | `Verifier`: `gpgv --keyring <pinned> sig file` (subprocess, `--homedir` пустой), sha256 из `SHA256SUMS`; общий для релизов, deb «из файла», манифеста | — | gpgv | unit: тестовый keyring, подмена файла/подписи |
| `helper/update_helper.py` → `/usr/libexec/astra-voice/update-helper` | Root: только `install <путь>`; `os.open(O_NOFOLLOW)`, копия в `/var/lib/astra-voice/staging/` (0700, файл 0644), `gpgv` (тот же keyring), sha256, `dpkg-deb --field` (Package=astra-voice, Architecture=amd64), `dpkg --compare-versions` (только повышение), `apt-get install --no-remove -y ./x.deb` c `DEBIAN_FRONTEND=noninteractive`, JSON в stdout, коды выхода | не хранит | polkit-action `…astra_voice.update` | unit непривилегированно с фейковыми `gpgv/apt-get` (PATH-подмена); e2e: одно окно пароля (S9-A2) |
| `platform/autostart.py` | `~/.config/autostart/astra-voice.desktop`: `Exec=/usr/bin/astra-voice --hidden`, `X-KDE-autostart-after=panel`, без `OnlyShowIn`; состояние = файл есть и нет `Hidden=true`; чужие ярлыки не трогаем; путь **не переписывается** | файл | paths | unit на tmpdir; e2e S11 |
| `core/stats.py` | `stats.json` (последние 1000 событий §10 PRD), p50/p95, медиана RTFx по ≥ 5 «тёплым» прогонам, «Очистить» | файл | paths | unit: перцентили, холодный прогон исключён |
| `core/i18n.py` | Qt Linguist: `qsTr` в QML, `translate` в Python; исходный ru, `en.ts/.qm`; `pylupdate5`/`lrelease` на сборке; плюрализация `%n`; смена языка — с явным «Перезапустить» | `.qm` в `/usr/share/astra-voice/i18n/` | pyqt5-dev-tools, qttools5 | CI: `lupdate` без новых непереведённых строк для en |
| `diag/collect.py` (P2) | zip: логи, `settings.json` без домашних путей, версии, `pactl info`/`wpctl status`; без аудио/текста | файл в `~/` | — | unit: в архиве нет полей `text` |
| `tools/catalog_cli.py` (P2), `scripts/gen_theme.py`, `scripts/build_manifest.py` | CLI корпоративного манифеста из папки (sha256, размеры); генерация `Theme.qml`; сборка `catalog.json` из `research/catalog-numbers.md`-таблицы + HF API (`?blobs=true`, sha256 не-LFS считается локально) | — | — | unit; CI-проверка «регенерация чистая» |

## 4. Рантайм v1: `onnx-asr` против `sherpa-onnx`

| Критерий | `onnx-asr` 0.12.0 (MIT, PyPI 2026-07-15) | `sherpa-onnx` 1.13.7 (Apache-2.0) | Вес |
|---|---|---|---|
| GigaAM v3 e2e_rnnt (дефолт) | загрузчик `gigaam-v3` для всех 4 вариантов (`istupakov/gigaam-v3-onnx`, 226,4 МБ) | `nemo_transducer` + community-экспорт csukuangfj (231,9 МБ); v3 в официальном каталоге sherpa **нет** | паритет |
| Каталог 12 позиций | **12/12** с первоисточником под этот рантайм (istupakov/onnx-community/alphacep/t-tech) — таблица `catalog-numbers.md` §1 | GigaAM v3 ×3, Whisper ×3 (свои экспорты, другие размеры, не бенчмарчены); Vosk/T-one/Multilingual/FastConformer — либо нет, либо не подтверждено | **решающий** |
| Протокол цифр (PRD §7.1) | WER/RTFx `comparison.md`/`benchmarks.md` сняты **на onnx-asr** — полоски честны без пересчёта | цифры под sherpa пришлось бы мерить самим на 12 моделях | сильный |
| Колёса / glibc 2.36 | `py3-none-any` (4 МБ, чистый Python) + `onnxruntime` 1.29.0 `manylinux_2_28` (у нас 2.36); проверено: `libonnxruntime.so.1.29.0` — max `GLIBC_2.28`, **0 ссылок `__isoc23_*`** → шим не нужен | `manylinux_2_17` — тоже ставится | паритет |
| Собственные `.so` (ЗПС v1.1) | **3** (`libonnxruntime.so`, `libonnxruntime_providers_shared.so`, `onnxruntime_pybind11_state…so`); numpy — системный (1.24.2 ≥ 1.22.4) | 4–6 (`_sherpa_onnx…so`, `libsherpa-onnx-core`, `libsherpa-onnx-c-api`, `libonnxruntime`) + свой ORT | сильный |
| ОЗУ / скорость | та же арена ORT; декодер RNN-T — цикл на numpy (≈150 кадров на 6 с, ~50–100 мс) — **риск** для Ц2 | декодер на C++ — на 50–100 мс быстрее на RNN-T | против, измеримо (S3) |
| VAD / длинные записи | встроенный Silero-VAD (через ORT), сегментация > 20 с | встроенный Silero-VAD | паритет |
| Сопровождение | один мейнтейнер, 0.x | k2-fsa, зрелый | против (митигация: вендор + пин + Engine-интерфейс) |

**Решение:** `onnx-asr` + `onnxruntime` **1.29.0** (без `sympy` в зависимостях; `flatbuffers` 2.0.8 / `protobuf` 3.21.12 /
`packaging` 23.0 — из apt Astra, проверено). Пины по sha256 в `packaging/wheels.lock`. **Условие отката** (PRD §1.4): если спайк S3 даст на
эталоне p95 > 0,5 с при e2e_rnnt даже с 4 потоками → дефолт `e2e_ctc` (+25 %), а при провале и этого — `SherpaEngine` для
GigaAM v3 (второй адаптер, строки манифеста с `engine: sherpa-onnx`).

### 4.1 Раскладки манифеста (`engine: onnx-asr`, единый рантайм v1)

| `layout` | Модели каталога | Файлы (относительные пути из HF API `?blobs=true` на закреплённой ревизии) |
|---|---|---|
| `onnx-asr-gigaam-v3` | #1 e2e_rnnt (дефолт), #2 e2e_ctc, #3 rnnt | `v3_<variant>_*.int8.onnx` (+ decoder/joint), `vocab`, yaml — вариант в поле `variant`, один HF-репо `istupakov/gigaam-v3-onnx` |
| `onnx-asr-gigaam-multilingual` | #4 CTC 220M, #11 Large CTC | `multilingual*_ctc.int8.onnx`, vocab, yaml, config |
| `onnx-asr-t-one` | #5 T-one (fp32) | `model.onnx`, vocab, config |
| `onnx-asr-vosk` | #6 ru 0.54, #7 small-ru 0.52 | `am-onnx/…` / `am/…` `{encoder,decoder,joiner}.int8.onnx`, `lang/tokens.txt` — подкаталоги в манифесте явные |
| `onnx-community-whisper` | #8 large-v3-turbo, #9 small | `encoder_model_int8.onnx`, `decoder_model_merged_int8.onnx`, токенайзер |
| `onnx-asr-whisper-ort` | #10 base (`istupakov/whisper-base-onnx`) | `whisper-base_beamsearch.int8.onnx`, токенайзер |
| `onnx-asr-nemo` | #12 FastConformer ru pc (CTC) | `model.int8.onnx`, `vocab.txt`, config |

Адаптер держит таблицу `layout → имя загрузчика onnx-asr + список обязательных файлов`; точные идентификаторы
загрузчиков и имена файлов фиксирует спайк S3 (`scripts/build_manifest.py` генерирует `files[]` с sha256 из HF API,
для не-LFS считает sha256 сам). Пример манифеста PRD §7.3 переписывается под `engine: onnx-asr` для всех записей.

## 5. Потоки данных

1. **Диктовка (PTT).** KeyPress → `hotkey.pressed` → пилюля `listening` (≤ 100 мс, без ожидания воркера) → `record.start`
   → воркер открывает устройство (10–40 мс; 3 ретрая по 300 мс при ошибке) → `level` ≤ 30/с → (2 с пик < −60 дБ → `silent`
   → пилюля `listening-silent` + уведомление «Перезапустить звуковую службу» → `systemctl --user restart wireplumber` →
   `record.start` повторно) → KeyRelease → `record.stop` → < 0,3 с? → `empty`-тихо, пилюля скрыта через 150 мс; иначе
   `processing` → VAD (только > 20 с) → `transcribe` → `result` → `paste` (активное окно, запомненное **до** показа пилюли)
   → пилюля `done` 500 мс → `stats.dictation`. Отмена: Esc (временный grab на время записи) / «×» / трей → `record.cancel`.
   Второе нажатие во время `processing` — одна запись в очередь; вставки по порядку.
2. **Загрузка модели.** карточка «Скачать» → гейт сети (явное действие разрешено даже при выключенных тумблерах) →
   `disk_usage ≥ size×1,2` → очередь → файлы по `sources[]` → `.part`+Range → sha256 → `<rev>.partial/` → **смоук**
   (`transcribe.file` вшитого wav, `expect_any`) → `os.replace` → `current.json` → `model.load` → `model.loaded` → замер
   после первой диктовки → `measurements.json` → карточка «замерено». Из файла/папки — тот же путь с шага sha256.
3. **Проверка ревизии модели (F8).** тумблер вкл ∧ ¬offline ∧ ¬policy ∧ прошло ≥ 24 ч → задержка 0–30 мин →
   `GET /api/models/{update_watch.hf_repo}/refs` (таймаут 3 с, ≤ 1 повтор, 429 → до конца окна) → `targetCommit(branch)`
   ≠ `pinned_revision` → бейдж + строка `model-update` + одно уведомление; манифест с `catalog_url` — той же частотой,
   применяется только с валидной подписью. Ничего не скачивается само.
4. **Обновление утилиты (F9).** кэш → фон 0–10 мин → `releases/latest` (или `latest.json` с `updates_url`) → SemVer >
   `__version__` ∧ не «пропущена» → строка `available|admin-track` → панель → трек: A = `DIGSIG_ELF_MODE=0` ∧ (в
   `astra-admin` ∨ `sudo`) ∧ `policy.updates≠admin`; B = ЗПС ∨ нет прав ∨ policy; «спросить» = конфиг нечитаем ∧
   `zps=auto` (ответ «Не знаю» → B). A: скачать `deb + SHA256SUMS + SHA256SUMS.asc` → `gpgv` → sha256 → имя/arch/версия →
   `pkexec update-helper install <путь>` (одно окно polkit; 126 → «отменена», 127/нет агента → подсказка `sudo apt install ./…`)
   → `restart`. B: та же проверка → папка с `INSTALL-ADMIN.md` → «Открыть папку». «Обновить из файла…» — те же шаги с `gpgv`.
5. **Старт.** policy → settings (миграции) → theme → QML (`--hidden`: окно не создаётся, только трей) → трей (ретрай ≤ 30 с)
   → воркер → `model.load` в фоне (строка-статус `loading`) → онбординг, если нет модели/`!onboarding_done` → отложенные
   проверки. Аудио на старте **не открывается** (гонка WirePlumber), `sleep`-костылей нет.

## 6. Инварианты безопасности (проверяются тестами, T1 — security-analyst)

1. Установка пакета **только** после `gpgv`-подписи ∧ sha256 ∧ `Package=astra-voice` ∧ `Architecture=amd64` ∧ версия выше
   установленной — и в GUI, и повторно в root-помощнике на **копии** в root-staging (`O_NOFOLLOW`, без symlink/TOCTOU).
2. Ровно один привилегированный шаг: polkit-действие `io.github.arrivarus.astra_voice.update` (`auth_admin`, без `_keep`),
   `exec.path` = помощник; никаких `rules.d`, `dpkg -i`, привязки к `/usr/bin/apt`, аргументов кроме `install <путь>`.
3. Аудио живёт только в ОЗУ воркера (кольцевой буфер), очищается после результата/отмены; ни одного wav/tmp на диске;
   тесты проверяют `inotify`/список файлов до и после диктовки.
4. Распознанный текст не пишется в логи/статистику/диагностику (фильтр логгера); «последний текст» — только в памяти GUI.
5. Сеть — четыре действия PRD F14 через один `HttpClient` с гейтом (тумблеры пусты по умолчанию, `offline`, policy,
   `HF_HUB_OFFLINE=1`); хосты — только из `PRIVACY.md` (`huggingface.co`+CDN, `github.com`/`api.github.com`/
   `objects.githubusercontent.com`, `corp_base`); TLS-проверка не отключается; токенов в клиенте нет; UA проекта.
6. Манифест применяется только с валидной подписью (наш keyring или `catalog_pubkey`); `sources[].revision` обязателен,
   URL всегда `/resolve/{revision}/`; sha256 каждого файла до `os.replace`; смоук до переключения `current.json`.
7. `policy.conf` — только чтение, значения из него блокируют строки UI; `settings.json`, кэш и сокет single-instance — 0600.
8. Хоткей: захватываются только назначенные комбинации (+`Escape` на время записи) — никакого XRecord/keylogging; поле
   захвата держит `XGrabKeyboard` только пока открыто.
9. Буфер обмена: только `text/plain` (+ `x-kde-passwordManagerHint`), восстановление исходного содержимого.
10. Нет телеметрии, crash-dumps, «отправить отчёт», скрытой записи (иконка «слушаю» в трее обязательна всегда).

## 7. Упаковка, сборка, репозиторий, CI

### 7.1 Структура репозитория `ArrivaRUS/astra-voice`
```
src/astra_voice/{app.py, core/, platform/, ui/, worker/, models/, net/, updates/, security/, helper/, diag/, tools/}
qml/            Main.qml, Theme.qml (generated), PillTheme.qml, components/*.qml, sections/*.qml, onboarding/*.qml, Pill.qml
data/           catalog.json (+ .sig), catalog.schema.json, icons/ (21 глиф + hicolor/ + tray/), sounds/{start,stop}.ogg,
                fonts/ (PT Mono, PT Root UI — если лицензия позволяет; иначе только fallback PT Astra Sans), vad/silero_vad.onnx,
                test/test-ru-6s.wav, keys/release.gpg, i18n/en.ts, polkit/…update.policy, astra-voice.desktop
packaging/      debian/ (control, rules, changelog, postinst-минимум), wheels.lock (name==ver --hash=sha256), build-deb.sh,
                Containerfile (debian:12), INSTALL-ADMIN.md, sbom/
scripts/        gen_theme.py, build_manifest.py, measure_model.py, release.sh, make_virtual_mic.sh, e2e/*.sh (P21½)
tests/          unit/ integration/ e2e/ (маркеры pytest: unit · xvfb · machine)
docs/           plans.md status.md test-plan.md PRIVACY.md NOTICE  ·  arch/ decisions/ design/ research/  (как сейчас)
```

### 7.2 Содержимое `.deb` `astra-voice_X.Y.Z_amd64.deb` (оценка ≤ 30 МБ; A1 ≤ 80 МБ выполняется)
- `/usr/bin/astra-voice` — лаунчер (добавляет `/usr/lib/astra-voice/vendor` в `sys.path`, запускает `astra_voice.app`);
  `/usr/lib/python3/dist-packages/astra_voice/` — код; `/usr/lib/astra-voice/vendor/` — `onnxruntime` 1.29.0 (3 `.so`, ~60 МБ
  распакованных), `onnx_asr` — только они; `/usr/share/astra-voice/{qml,icons,sounds,fonts,vad,test,keys,
  i18n,catalog.json,catalog.json.sig}`; `/usr/share/icons/hicolor/{16x16,22x22,scalable}/{apps,status}/`;
  `/usr/share/applications/astra-voice.desktop`; `/usr/share/polkit-1/actions/io.github.arrivarus.astra_voice.policy`;
  `/usr/libexec/astra-voice/update-helper` (root:root 0755); `/usr/share/doc/astra-voice/{NOTICE,PRIVACY.md,INSTALL-ADMIN.md,
  examples/policy.conf,sbom.cdx.json}`. `/etc/astra-voice/` пакет **не создаёт** (policy пишет админ). `postinst` — только
  триггеры dh (иконки/desktop), без сети и GUI.
- **Depends:** `python3 (>= 3.11)`, `python3-pyqt5`, `python3-pyqt5.qtquick`, `python3-pyqt5.qtsvg`, `python3-xlib`,
  `python3-numpy (>= 1:1.22.4)`, `python3-requests`, `python3-jsonschema`, `python3-packaging`, `python3-protobuf`, `python3-flatbuffers`,
  `qml-module-qtquick2`, `qml-module-qtquick-controls2`, `qml-module-qtquick-layouts`, `qml-module-qtquick-window2`,
  `qml-module-qtgraphicaleffects`, `qml-module-qtquick-shapes`, `libqt5svg5`, `libpulse0`, `gpgv`, `polkitd`, `pkexec`.
  **Recommends:** `polkit-kde-agent-1 | fly-…-agent`, `pipewire-pulse | pulseaudio`, `xdotool` (P2 fallback), `wireplumber`.
  Всё — из `repository-main` Astra 1.8 (проверено `apt-cache show` на машине; `python3-pyqt5.qtquick` 5.15.9+dfsg-1+b9 доступен,
  но не установлен — тянется автоматически).
- **Собственные ELF/`.so` в пакете: 3** (все — onnxruntime) — фиксируется тестом `tests/unit/test_deb_elf_count.py`
  (`dpkg-deb -c` + `file`), это же число идёт в `INSTALL-ADMIN.md` и SBOM (допущение A11 закрывается «3»).

### 7.3 Сборка и воспроизводимость
- Одна команда: `make deb` → `packaging/build-deb.sh`: (1) `pip download --require-hashes -r packaging/wheels.lock` из
  локального кэша `packaging/wheels/` (сеть только на шаге `make wheels`, отдельно); (2) `scripts/gen_theme.py` + проверка
  чистоты; (3) `pylupdate5`/`lrelease`; (4) `dpkg-buildpackage -us -uc -b` с `SOURCE_DATE_EPOCH` из `debian/changelog`,
  `override_dh_strip` не трогает вендор (`libonnxruntime.so` остаётся байт-в-байт как в колесе — совпадает с sha256 SBOM);
  (5) `lintian`, тест установки в чистом контейнере (`apt-get install ./x.deb && astra-voice --version && python3 -c
  'import astra_voice, onnxruntime'`). Цель Ц7: ≤ 5 мин на эталоне.
- Где собирать: контейнер `debian:12` (`Containerfile`) — та же glibc 2.36 / Python 3.11.2 / PyQt5 5.15.9, что в ALSE 1.8.5
  (`/etc/debian_version` = 12.0); локальная сборка на ALSE — те же скрипты.
- SBOM: CycloneDX JSON (`scripts/sbom.py` из `wheels.lock` + `dpkg-query` по Depends; `syft` в apt нет).
- Релиз (`scripts/release.sh`, GitHub Actions on tag `vX.Y.Z`): deb → `SHA256SUMS` → `gpg --detach-sign --armor` (ключ CI,
  Ed25519, публичный — в `data/keys/release.gpg` и в README) → Release: `deb`, `SHA256SUMS`, `SHA256SUMS.asc`,
  `sbom.cdx.json`, `latest.json` (для корпоративного `updates_url`), `INSTALL-ADMIN.md`; changelog-writer → тело релиза.
  Зеркало весов — отдельный релиз `models-YYYY.MM` (ассеты ≤ 2 ГБ), пути в `sources[].type=github_release`.

### 7.4 CI (GitHub Actions, контейнер `debian:12`)
| Job | Что | Инструменты (apt Debian 12 = apt ALSE) |
|---|---|---|
| lint/types | `ruff`, `mypy --strict` по `src/`, `qmllint` по `qml/` | ruff (pip в CI), python3-mypy 1.0.1, qtdeclarative5-dev-tools |
| unit | `pytest -m unit` (без дисплея) | python3-pytest 7.2 |
| qml/xvfb | `pytest -m xvfb`: каждый QML создаётся без warnings, скриншоты состояний, XGrabKey/XTest под `Xvfb`, single-instance | xvfb, `QT_QPA_PLATFORM=xcb` |
| engine | `pytest -m engine`: `onnx-asr` + ORT на кэшированной модели (226 МБ, кэш по ревизии) — смоук 3 раскладок | кэш GitHub Actions |
| deb | `make deb` → lintian → установка в чистом контейнере → `--version` → ELF-count = 3 | dpkg-dev, debhelper, dh-python |
| release (tag) | deb + SHA256SUMS + подпись + SBOM → GitHub Release | gpg |

**Нельзя проверить в CI → P21½ на машине заказчика (и Fly-сессия):** захват хоткея на KWin/fly-wm и конфликты KDE;
пилюля без фокуса/вне Alt+Tab/над панелью; SNI-иконка и её перекраска; polkit-окно и помощник; PipeWire-захват и гонка
WirePlumber; Klipper; вставка в Kate/Konsole/LibreOffice/Firefox; `tcpdump` хостов (Ц4); автозапуск после перелогина;
живая смена темы; замер ОЗУ/RTFx (Ц2, Ц6); уведомления KDE с кнопками.

## 8. Порядок сборки: milestones по зависимостям (validation-first)

Каждый milestone начинается с тестовой обвязки (фейки `FakeAudioSource`/`FakeEngine`/HTTP-сервер), заканчивается
наблюдаемым «done» и командой проверки. Даты — рамки релизов из G1: v0.1 30.09 · v0.2 15.10 · v1.0 31.10.

### M0 — спайки (до 12.09, ~3 дня; результаты → `arch/spikes.md`, решения → журнал)

| Спайк | Что проверяем | Наблюдаемый критерий / команда | Если провал |
|---|---|---|---|
| S1 пилюля + трей | QML `Window` с флагами + EWMH на KWin **и** во Fly-сессии; композитор вкл/выкл; `QSystemTrayIcon` по имени иконки → перекраска под тему панели | `xdotool getactivewindow` не меняется после показа; `xprop -id <pill> _NET_WM_STATE` содержит `ABOVE, SKIP_TASKBAR, SKIP_PAGER`; пилюли нет в Alt+Tab; над панелью не лежит; трей-иконка меняет цвет строки под светлой/тёмной панелью | override-redirect режим; иконка-пиксмап |
| S2 polkit-помощник | `pkexec /usr/libexec/…/update-helper install x.deb` из `QProcess` GUI-процесса на тестовом пакете 0.0.1→0.0.2 (установка `.policy` и помощника — один `sudo` руками) | ровно **одно** окно пароля KDE; коды 0/126/127 различимы; `dpkg-query -W` показывает 0.0.2; GUI не блокируется | `pkcon install-local` как запасной путь (PackageKit есть) |
| S3 рантайм + замер | venv в scratch: `onnxruntime==1.29.0` + `onnx-asr==0.12.0` с **системным** numpy 1.24.2; загрузка `istupakov/gigaam-v3-onnx` (e2e_rnnt 226 МБ; при «да» — e2e_ctc и rnnt, ~680 МБ); `scripts/measure_model.py` | `import onnxruntime` ok; 10 прогонов тестового wav 6 с: медиана и p95 инференса при 2/4 потоках; `VmHWM`; число `.so` = 3; `objdump -T` без `__isoc23_`; **порог:** p95 ≤ 300 мс хотя бы у e2e_rnnt или e2e_ctc | дефолт e2e_ctc; при > 400 мс у обоих — `SherpaEngine` для GigaAM v3 |
| S4 хоткей + вставка | `python3-xlib`: grab `Ctrl+Space` ×8 масок, PTT с фильтром автоповтора, временный grab `Escape`, `BadAccess` на занятой комбинации (`Alt+F2`), XTest `Ctrl+V` в Kate при русской раскладке, восстановление буфера, `x-kde-passwordManagerHint` против Klipper | текст появился в Kate; буфер вернулся ≤ 200 мс; `qdbus org.kde.klipper /klipper getClipboardHistoryMenu` не содержит фразу; `Alt+F2` → BadAccess пойман | Shift+Insert как дефолт вставки; предупреждение о Klipper вместо защиты (P1 остаётся) |
| S5 звук | `libpulse-simple` через ctypes, 16 кГц mono s16le, виртуальный источник (`pactl load-module module-null-sink sink_name=av_test` + `module-remap-source master=av_test.monitor`), `paplay --device=av_test test.wav`; тишина; повторное открытие после `systemctl --user restart wireplumber` | уровень живой при `paplay`, `silent` через 2 с тишины; после рестарта WirePlumber устройство открывается со 2-й попытки ≤ 1 с; на диске ни одного файла | `QAudioInput` (apt `python3-pyqt5.qtmultimedia`) |

### M1–M11 (порядок = зависимости; в скобках — истории беклога)

| M | Цель | Done (наблюдаемо) | Валидация | Риски |
|---|---|---|---|---|
| **M1 Скелет и упаковка** (v0.1; 12.1, 1.1, 9.2, 10.5, 4.7-старт) | Репозиторий §7.1, `gen_theme.py` → `Theme.qml`, лаунчер, `--hidden`, single-instance, settings/policy/paths/logging/version, тема из `kdeglobals`, оболочка окна (сайдбар + «Общие» + строка-статус), `make deb`, CI зелёный | `apt install ./astra-voice_0.1.0~m1_amd64.deb` на ALSE → окно 900×620 в теме KDE; второй запуск показывает первое окно; `make deb` ≤ 5 мин | `time make deb`; `pytest -m unit`; `lintian`; `dpkg-deb -c \| grep -c '\.so$'` = 3 | QML 5.15 vs макет (тени `DropShadow`, шрифты PT); размер deb |
| **M2 Воркер и движок** (2.5, 8.3, 2.8-часть) | Супервизор, IPC, воркер, `OnnxAsrEngine` (раскладка `onnx-asr-gigaam-v3`), `model.load` из локальной папки, `transcribe.file`, `measure`, рестарт при краше, выгрузка по простою | `astra-voice --debug-transcribe data/test/test-ru-6s.wav` печатает текст с «проверка» и `t_ms`; `kill -9 <worker>` → новый pid ≤ 1 с, модель перезагружена, трей/пилюля показали ошибку один раз | `pytest -m engine` (CI с кэшем модели); `pytest tests/unit/test_supervisor.py` | RTFx (см. S3); ORT-потоки |
| **M3 Звук** (1.6-часть, 2.4, 11.1) | `PulseSimpleSource`, уровни, тишина, лимит 120 с, VAD > 20 с, список/выбор устройства, действие «Перезапустить звуковую службу», `WavFileSource` | e2e с виртуальным микрофоном: `record.start` → `paplay` 6 с → `result`; тишина → `silent` ≤ 2,2 с; 130 с записи → `limit` + текст | `scripts/e2e/virtual_mic.sh`; `pytest -m unit tests/worker/test_audio_fsm.py` | гонка WirePlumber; `EBUSY` при эксклюзивном захвате |
| **M4 Цикл диктовки** (2.1, 2.2, 2.3, 3.1, 3.2, 3.5, 4.1–4.4, 11.2, 8.4, 2.8) | Хоткей PTT/toggle, пилюля 12 состояний, трей 6 состояний + меню, вставка с восстановлением, отмена, очередь 1, статистика, корректное завершение | S2, S3, S4-A1/A3/A4, S12-A3, S14 зелёные на машине; **p95 «отпустил → текст» ≤ 0,5 с по 50 диктовкам** (Ц2) из `stats.json` | `xdotool keydown ctrl+space; paplay …; xdotool keyup ctrl+space` ×50 → `astra-voice --stats`; `pytest -m xvfb` | фокус/стек пилюли; Klipper; BadAccess |
| **M5 Онбординг и первая модель** (1.2, 1.3, 1.5, 1.6, 9.1-Общие) | Экраны O1–O4 (+O5 «Готово» без автозапуска), минимальный загрузчик (HF, Range, sha256, смоук) для модели по умолчанию, установка из папки, поле захвата + конфликты KDE, тест микрофона без вставки | S1-A1/A2/A3/A5/A7 на чистой ВМ ALSE 1.8; `tcpdump`: до включения тумблеров — 0 соединений, кроме явного «Скачать» | `pytest -m unit tests/models/test_downloader.py` (206/200/обрыв/подмена); ручной прогон S1 по секундомеру (Ц3-черновик) | HF недоступен → «Из файла» |
| **→ v0.1 «Диктовка работает» 30.09** | тег `v0.1.0` → deb + `SHA256SUMS` + `.asc` + SBOM → GitHub Release (Ц7 требует подпись уже здесь; 12.2 частично раньше плана) | заказчик пользуется вместо Handy; чеклист v0.1 беклога | `scripts/release.sh v0.1.0` | ключ подписи (CI secret) заводится сейчас |
| **M6 Каталог** (5.8, 5.1, 5.2, 5.3, 5.5, 5.6, 5.7, 5.4, 1.4, 11.3) | Манифест 12 записей (`build_manifest.py`), `CatalogModel`, карточка 20 состояний, источники HF→GitHub→corp, очередь/отмена/пауза «нет места», удаление, переключение, замеры и полоски по протоколу, ошибки повреждена/OOM, фильтры | S7, S15 зелёные; **≥ 3 модели замерены** (Ц6); скриншоты 20 состояний = референсы | `pytest -m unit tests/models/`; `pytest -m engine` на 3 раскладках; DesignReviewer | форматы Whisper/Vosk под onnx-asr (S3 закрывает) |
| **M7 Сеть и проверки** (10.8, 10.1, 6.1, 6.2, 6.3, 7.1, 7.6, 7.7) | `HttpClient` (гейт/кэш/backoff/UA/прокси/CA), HF refs, GitHub latest, строка-статус 17 состояний, тумблеры/офлайн/URL, обновление ревизии со смоуком и откатом, манифест «Новое», трей «Проверить обновления» | S6, S8-A1..A4/A6/A7; `tcpdump` за сессию: только хосты `PRIVACY.md`, в офлайне — 0 (Ц4) | `pytest -m unit tests/net/` (фейковый сервер 200/304/429/407/таймаут); `sudo tcpdump -nn 'tcp[13]&2!=0' -w s.pcap` + `ss -tnp` | 429 за NAT; HF из РФ |
| **M8 Обновлятор, трек A** (12.2, 7.2, 7.5) | Помощник + `.policy`, `app_updater`, панель 14 состояний, «Обновить из файла», проверка подписи GUI+root | S9-A1..A7 на машине: реальный апгрейд 0.2.0-rc → 0.2.0 через одно окно polkit; подменённый deb отвергнут | `pytest tests/unit/test_update_helper.py` (фейковые `gpgv/apt-get` через PATH); ручной S9 | polkit-агент во Fly; T2-ревью security-analyst |
| **M9 Автозапуск, уведомления, звук, разделы** (8.2, 1.7, 10.4, 9.1-все, 4.5, 4.6, 3.3, 3.4, 3.6, 2.6, 2.7, 1.5b, 1.8, 4.7-живая) | XDG-ярлык + O5, D-Bus-уведомления с кнопками и запасным баннером, тоны через `pa_simple_write` (без новых зависимостей), «Вывод/Продвинутые/О программе», терминалы по `WM_CLASS`, Klipper-опция, выгрузка модели, живая тема | S11, S12, S13-A2, F13-acceptance; после перелогина — трей есть, аудио не открыто (`wpctl status`) | `pytest -m unit tests/platform/test_autostart.py`; перелогин в KDE **и** во Fly | Fly-уведомления убивают владельца шины |
| **→ v0.2 «Каталог, обновления, автозапуск» 15.10** | требования №1–5 закрыты; Ц3 (≤ 5 мин на ВМ), Ц4, Ц6 | чеклист v0.2 беклога; G5 | `scripts/release.sh v0.2.0` | — |
| **M10 Корпоративный контур** (10.2, 9.4, 5.9, 10.3, 7.3, 7.4, 10.6, 12.4, 10.7) | `policy.conf` + замки, «только отечественные» (6 моделей), корп. URL + `catalog_pubkey` + `latest.json`, трек B + детект ЗПС + вопрос, `INSTALL-ADMIN.md`, документ для админа ИБ, SBOM в релизе, NOTICE/PRIVACY/дисклеймер | S10-A1..A4, S16, F14/F15-acceptance; `profile=secure` → 0 соединений и 6 карточек | `pytest -m unit tests/core/test_policy.py`, `tests/updates/test_track.py`; ВМ с `policy.conf` | согласие на имя (L1) — вне кода, гейт G5 |
| **M11 Полировка** (9.3, 9.6, 11.4, 6.4, 6.5) | Полный английский, клавиатура У13 и кольца фокуса, «Отладка» + сбор диагностики, сигнал «близкая модель», «пропустить ревизию» | DesignReviewer: 24 экрана × 2 темы; `lupdate` без непереведённых; Ц1 — Handy удалён | `pytest -m xvfb` скриншоты; чеклист G5 | — |
| **→ v1.0 31.10** · **v1.1:** ГОСТ-подпись 3 `.so` (`bsign-integrator`), ключ организации, ВМ с ЗПС (12.3, Ц5) | | | | помощник — скрипт, подписывать нечего |

## 9. Ключевые риски и альтернативы (с обратимостью)

| # | Риск | Вер./влияние | Митигация | Альтернатива · обратимость |
|---|---|---|---|---|
| R1 | Python-декодер RNN-T в `onnx-asr` не даёт p95 ≤ 0,5 с на Core Ultra 7 | ср./выс. | S3 в первую неделю; потоки ORT; дефолт e2e_ctc | `SherpaEngine` за тем же `Engine` · дни, манифест не ломается |
| R2 | Пилюля крадёт фокус / уходит под окна / чёрные углы без композитора; во Fly другой WM | ср./выс. | S1 на KWin и fly-wm; EWMH руками; re-assert + проверка перекрытия; непрозрачный вариант без композитора | override-redirect (тумблер) · часы |
| R3 | `Ctrl+Space` занят (ibus/fcitx переключают раскладку этой комбинацией на части ALSE) | ср./ср. | `BadAccess` → `not-grabbed` + «Выбрать другую»; парсер `kglobalshortcutsrc`; подсказка `Scroll Lock` | — |
| R4 | Автоповтор X11 и порядок отпускания модификаторов ломают PTT | ср./ср. | автомат следит только за основной клавишей, фильтр пар Release/Press с одним `time`; `XkbSetDetectableAutoRepeat` если доступен в python-xlib | evdev-режим P2 |
| R5 | Klipper игнорирует `x-kde-passwordManagerHint` на 5.27 | ср./низ. | S4; при провале — постоянная подпись-предупреждение (flows §7.6), опция остаётся P1 | — |
| R6 | HF недоступен из РФ / 429 за NAT; GitHub 60 req/ч | выс./ср. | источники по очереди, зеркало в Releases, корп. URL, «Из файла», кэш 24 ч, ETag, случайная задержка, Retry-After | — |
| R7 | Новые колёса ORT поднимут порог glibc или вернут `__isoc23_*` | низ./выс. | пин 1.29.0 по sha256; CI-тест `objdump -T` (max ≤ GLIBC_2.36, нет `isoc23`); при нужде — шим как `patchelf --add-needed` (+1 `.so`) | ORT 1.24.4 (GLIBC_2.27) · минуты |
| R8 | ORT 1.29 несовместим с системным numpy 1.24 в рантайме | низ./выс. | проверка `import` в S3 и CI на Debian 12 | ORT 1.24.4 (numpy ≥ 1.21.6, + `python3-sympy` из apt) · минуты |
| R9 | Полкит-агента нет (Fly) / пользователь не в `astra-admin` | ср./ср. | детект агента; трек B; текст с `sudo apt install ./…` | `pkcon install-local` · часы |
| R10 | T1 потребует помощник-ELF вместо скрипта | низ./ср. | контракт argv/JSON сохраняется | C-помощник · день (+1 ELF под ЗПС) |
| R11 | Шрифтов PT Root UI / PT Mono на ALSE нет (есть PT Astra Sans/Serif/Fact) | выс./низ. | вложить в пакет при допустимой лицензии (проверка legal); резервный стек из `tokens.json` → PT Astra Sans; DesignReviewer сверяет композицию, не метрику | — |
| R12 | Меню трея рисует Plasma (DBusMenu) — стиль спеки §9.2 недостижим | выс./низ. | зафиксировать в спеке как «системное меню»; состав/неактивность — наши | своё QML-меню-поповер · дни (хуже по UX) |
| R13 | Смоук новой ревизии держит две модели в памяти (~830 МБ пик) | ср./низ. | если `MemAvailable` < 2× — выгрузить текущую перед смоуком | — |
| R14 | Гонка WirePlumber на старте — микрофон молчит | выс./ср. | ленивое открытие, ретраи, кнопка перезапуска службы, авто-перепроверка | — |
| R15 | Ключ подписи релизов (CI secret) утёк/потерян | низ./выс. | keyring с двумя ключами (ротация), отзыв через новый релиз с обновлённым keyring, ключ в README | — |
| R16 | QML 5.15: нет `MultiEffect`/`font.features`; `DropShadow` дорог на CPU | ср./низ. | тени только на окне/поповере, PT Mono для цифр; замер CPU пилюли ≤ 3 % при записи | предрендеренные тени PNG · часы |

## 10. Что тестируем на каждом уровне

| Уровень | Среда | Что | Инструменты / команда |
|---|---|---|---|
| Unit (CI + локально) | Debian 12, без дисплея | settings/миграции, policy-таблица, автомат хоткея, парсер конфликтов, выбор комбинации вставки по `WM_CLASS`, downloader (206/200/обрыв/подмена), схема и подпись манифеста, SemVer/`tag_name`, выбор трека, помощник (фейки `gpgv`/`apt-get`), статистика p95, `kdeglobals`, IPC-кодек, автомат воркера (`FakeAudioSource`+`FakeEngine`), фильтр логов, ELF-count deb, glibc колеса | `pytest -m unit`, `mypy --strict`, `ruff`, `qmllint` |
| Integration (CI, xvfb / offscreen) | `Xvfb` + `QT_QPA_PLATFORM=xcb`, кэш модели | все QML без warnings + скриншоты состояний (24 × 2 темы) для DesignReviewer; XGrabKey/BadAccess/XTest в тестовое Qt-окно; буфер обмена с восстановлением; single-instance; трей-ветка «недоступен»; движок на 3 раскладках GigaAM с тестовым wav; смоук-откат при подменённом байте; VAD-границы | `pytest -m xvfb`, `pytest -m engine` |
| Integration с виртуальным микрофоном (машина) | ALSE, PipeWire | `paplay` в виртуальный источник → `result`; тишина; лимит; рестарт WirePlumber | `scripts/e2e/virtual_mic.sh` |
| E2E (P21½, машина заказчика, KDE **и** Fly) | реальная сессия | S1–S16 из PRD §6 по чеклисту `docs/test-plan.md`; p95 по 50 диктовкам; `tcpdump`/`ss` хосты; polkit одно окно; автозапуск после перелогина; тема live; Kate/Konsole/LibreOffice/Firefox; Klipper; Alt+Tab; ВМ с `policy.conf` | `scripts/e2e/*.sh` + ручной чеклист Юрки |
| Security (T1/T2/T3) | ревью + тесты | инварианты §6 как тест-кейсы (подмена deb/подписи/манифеста, symlink в staging, аргументы помощника, отсутствие файлов аудио, хосты) | security-analyst по `security-gate` |

## 11. Вопросы к человеку

1. **Спайк S3 качает модели с HF** (226 МБ минимум, 680 МБ для трёх вариантов) в scratch-venv — нужно «да» на трафик/диск.
2. **S2 требует одного `sudo`** на машине (положить `.policy` и тестовый помощник) — допустимо ли делать это на машине
   заказчика, или готовим ВМ ALSE 1.8 (её же используем для S1 во Fly-сессии и для чистого прогона S1/Ц3)?
3. Шрифты **PT Root UI / PT Mono** отсутствуют на ALSE: вкладывать в пакет (проверка лицензии ParaType — задача legal)
   или принять резерв PT Astra Sans для UI (метрики макета чуть разойдутся)?
4. Подпись релизов **GPG (Ed25519) через `gpgv`** вместо minisign — принять как решение архитектуры (аргумент: `gpgv` есть
   у любого админа ALSE для трека B; `minisign` в apt Astra нет)? Кто владеет закрытым ключом CI (заказчик или Юрка)?
5. Меню трея под SNI рисует Plasma — согласовать с DesignReviewer, что §9.2 спеки сверяется по составу, не по стилю.
6. Root-помощник как Python-скрипт (ноль своих ELF) — принять, либо T1 потребует C-ELF?

## 12. Факты среды (проверено read-only 2026-09-09; вход для синтеза и спайков)

- ALSE 1.8.5 = Debian 12.0 (`/etc/debian_version`), glibc 2.36, Python 3.11.2, `python3-pyqt5` 5.15.9 (модули Core/Gui/
  Widgets/Network/DBus/…; **QtQml/QtQuick не установлены**, пакет `python3-pyqt5.qtquick` 5.15.9+dfsg-1+b9 есть в apt);
  `qml-module-qtquick-controls2` 5.15.8 и `qml-module-qtgraphicaleffects` установлены; `libqt5svg5`, `libqt5x11extras5`,
  `qmllint`/`qmlscene` есть; `qtdeclarative5-dev-tools`, `pyqt5-dev-tools`, `xvfb`, `python3-pytest` 7.2, `python3-mypy` 1.0.1 — в apt.
- В apt Astra есть: `python3-xlib` 0.33, `python3-evdev` 1.6.1, `python3-requests` 2.32.5 (установлен), `python3-jsonschema`
  4.10, `python3-nacl` 1.5, `python3-flatbuffers` 2.0.8, `python3-protobuf` 3.21.12, `python3-packaging` 23.0, `python3-psutil`,
  `gpgv` 2.2.40 (установлен). **Нет:** `python3-sounddevice`, `python3-pyaudio`, `python3-pynput`, `minisign`, `syft`, `python3-semver`.
- Звук: PipeWire 1.4.9 с pulse-протоколом (`pactl` работает, драйвер источников — PipeWire; `pipewire-pulse.service` inactive —
  протокол отдаёт сам pipewire), WirePlumber 0.5.12 активен; `libpulse-simple.so.0` грузится через ctypes; есть `pw-record`,
  `paplay`, `parecord`; источники `alsa_input…Mic1/Mic2` (s32le, 48 кГц).
- Колёса ORT в `~/.cache/uv`: 1.24.4 (max `GLIBC_2.27`, 22 МБ lib) и 1.29.0 (max `GLIBC_2.28`, 28 МБ lib), **0 ссылок
  `__isoc23_*`**, по 3 `.so`; шим из Handy нужен был только prebuilt-бинарю крейта `ort`. `onnx-asr` 0.12.0: `numpy>=1.22.4`,
  `onnxruntime>=1.18.1,!=1.24.1,!=1.25.*,!=1.26.0`; системный numpy 1.24.2 подходит.
- KWin: композитор активен; EWMH поддерживает `_NET_ACTIVE_WINDOW`, `_NET_CLIENT_LIST_STACKING`, `ABOVE`, `SKIP_TASKBAR`
  (4 из 5 проверенных атомов — тип `NOTIFICATION` проверить в S1); `StatusNotifierWatcher` host зарегистрирован;
  `org.freedesktop.Notifications` — Plasma; `polkit-kde-authentication-agent-1` запущен; пользователь в `astra-admin`, `input`.
- ЗПС выключена (`DIGSIG_ELF_MODE=0`, все режимы 0); ключи изготовителя в `/etc/digsig/`.
- `~/.config/kglobalshortcutsrc` (23 КБ): формат `key=combo\tcombo,default,описание` — пример `_launch=Alt+Space\tAlt+F2\tSearch,…,Открыть строку поиска и запуск`.
- Шрифты: PT Astra Sans/Serif/Fact есть; **PT Root UI и PT Mono — нет** (R11).

exec
/bin/bash -lc "cat decisions/log.md; rg -n '"'^#|S14|7'"\\.3|MemAvailable|measurements|update-cache|policy.conf|settings.json|600|SNI|smoke|смоук|ревиз|sha256|engine|layout' PRD.md" in /home/astra/Документы/astra-voice
 succeeded in 0ms:
# Журнал решений

> Что решили и **почему**. Новые записи — сверху. Пишет Юрка после явного «да» человека
> (скилл `decision-log`).

## 2026-09-08 — G3: полный макет A + дизайн-спека 0.2 + токены 2.1 приняты
- Контекст: UXAnalyst развернул A «Панель» в 48 экранов (~190 состояний), DesignEngineer снял 596 токенов
  и черновик спеки, Юрка сверил размеры с рендером (окно 900×620, сайдбар 184, строка 38/55, пилюля 36,
  min 172) и закрыл 14 «уточнить». Чеклист приёмки G3 — 11/11.
- Решение: **макет `design/mockups/final/`, спека `design/spec.md` (0.2), токены `design/tokens.json` (2.1.1),
  референсы `design/refs/` — источник истины для разработки и DesignReviewer.** Правки дальше — через
  журнал решений и ревизию спеки.
- Почему: «да» заказчика (чат 2026-09-08); контрасты текста ≥ 4,5:1, состояния не только цветом.
- Гейт: G3 · «да» заказчика.
- Последствия: Фаза 3 — двойной архитектор (architect-claude Fable ∥ architect-codex GPT-6 Astra) → суд
  и синтез → челлендж Astra → T1 модель угроз (обновлятор, загрузка моделей, polkit) → execution-pack.
  Вне спеки до кода: глиф `clock` (UX, в работе), два звука .ogg, `Theme.qml` генерировать из tokens.json.

## 2026-09-08 — Решения Юрки по «⚠ уточнить» спеки (У1–У14, рутинные)
- У1 кольцо фокуса: светлая тема — `accent-ink #0A776A` (5,16:1), тёмная — `#2FD9C4`; чистая бирюза в
  светлой не видна (2,2:1). У2 pressed в тёмной — ступень 7 %, чтобы подпись держала ≥ 4,5:1.
- У4 ширина: колонка контента растёт до 760 px, дальше центрируется. У5 строка с тумблером кликабельна
  целиком (как в KDE); строки со списком/кнопкой — только контрол.
- У13 клавиатура: Tab — сайдбар → строки контента сверху вниз → строка-статус; стрелки в сайдбаре;
  Ctrl+PgUp/PgDn — разделы; Esc закрывает диалог/поповер; Space/Enter активируют контрол.
- У3 прокрутка — overlay-скроллбар 8 px, AsNeeded. У6 кнопка `loading` — спиннер вместо иконки, текст
  остаётся, disabled. У7 список — вниз, максимум 6 пунктов (228 px), при нехватке места — вверх.
- У8 глиф `clock` добавить в набор (UX). У9 `loading-model` — вращающийся спиннер, 1 оборот/с, линейно.
- У10 пилюля — по центру снизу **рабочей области** (availableGeometry: без панели KDE) монитора активного
  окна, 48 px от её края (сверху — симметрично); без активного окна — основной монитор. (Уточнение
  2026-09-08 после замечания design-engineer: панель выше 48 px перекрывалась бы пилюлей.) У11 онбординг пропускаемый по экранам, но завершение требует
  установленной модели (S1). У12 диалоги модальны к окну настроек и центрируются над ним; уведомления —
  штатные KDE. У14 звуки — два коротких тона (старт: восходящие, конец: нисходящие, ≤ 120 мс, −18 dBFS),
  по умолчанию выключены; файлы .ogg в комплекте.
- Гейт: — (рутинные); войдут в спеку на G3.

## 2026-09-08 — Решения Юрки по цифрам каталога (research/catalog-numbers.md)
- «Отечественная» модель определяется **по юрлицу правообладателя** (контекст Указа № 166/КИИ):
  Vosk (Alpha Cephei Inc., Делавэр, США; команда — РФ) → **зарубежная**, в фильтр «только отечественные»
  не попадает; подпись карточки: «зарубежная · команда из России».
- Размеры в каталоге — **десятичные МБ по байтам HF API** (PRD §7.2 и макет правятся: 9 из 12 позиций).
- WER/RTFx — только по протоколу §7.1 (onnx-asr, Russian LibriSpeech, x64 int8); оговорка в UI-подсказке:
  «WER бенчмарка измерен на fp32-весах». Позиции без int8-бенчмарка (T-one fp32, GigaAM Multilingual)
  — полоска «по бенчмарку fp32»; **Whisper small — «нет данных по протоколу»** (FLEURS 11,4 % — другой
  протокол, только в подсказке) до собственного замера в v0.2 (US-5.4).
- Whisper лицензии — по моделям: large-v3-turbo MIT, small и base Apache-2.0 (NOTICE — раздельно).
- T-one остаётся Should (fp32 144 МБ, RTFx 26,3; ОЗУ — «оценка», правило ×1,85 неприменимо).
- Рантайм v1 (onnx-asr vs sherpa-onnx) и раскладка файлов манифеста — решение архитекторов; пример
  манифеста в PRD §7.3 должен быть согласован с выбранной раскладкой (поле `engine` + `layout` на модель).
- Гейт: — (рутинные). Последствия: PRD ревизия 3 (§7.2/§7.3/NOTICE), макет каталога — правка цифр.

## 2026-09-08 — Решения Юрки по токенам (вопросы design-engineer В1–В7, конфликты К1–К12)
- Hover/pressed/focus: задать системно — hover = поверхность на ступень темнее/светлее (surface-2),
  pressed = ещё ступень; primary hover/pressed = затемнение 8 % / 14 %; focus — кольцо 2 px accent
  снаружи; пункт сайдбара и карточка — тот же принцип. Значения фиксирует design-engineer в токенах.
- Easing: вход — ease-out (cubic 0.2/0/0/1) 160 мс, выход — ease-in 120 мс, hover 120 мс, уровень звука
  в пилюле — линейно; «Готово» 500 мс. Пульсация: **пилюля «распознаю» пульсирует** (PRD F3.2), **трей
  статичен** (brand-basics §2.4) — оба верны, конфликт К4 снят разделением.
- Контрасты Р1–Р3, Р5 — исправить сдвигом hex/заменой opacity на цвет, композицию не трогать;
  Р4 (трей на светлой панели) — принять: смысл несут форма и подсказка (PRD §8), цвет — подсказка.
- К11 — строка настройки 38 px (как в финальном макете `_shell.py`), базовая 44 px устарела.
- К2 — расширенная шкала кеглей принимается (документировать как шкалу v1). К3 — fg-disabled в тёмной
  теме должен отличаться от fg-muted. К10/В7 — **иконки свои** (20 контуров, currentColor, в комплекте
  приложения), Breeze не зависимость. В5 — кнопка danger в v1 не нужна (деструктив — primary в диалоге
  с текстом-предупреждением). В3 — минимум окна 900×620.
- Гейт: — (рутинные, без «да»); всё это войдёт в спеку на G3.

## 2026-09-08 — Решения Юрки по вопросам полного макета (рутинные, без гейта)
- Vosk (Alpha Cephei): происхождение «уточняется», в фильтр «только отечественные» не попадает, пока
  Researcher не подтвердит (задача на спайк каталога).
- «Проверить обновления» в трей-меню всегда активно: ручная проверка — явное действие пользователя
  (как «Проверить сейчас» в разделе «Сеть»); неактивно только в офлайн-режиме/по policy. PRD F12 поправлен.
- Тумблер VAD в «Продвинутых» — вне v1 (VAD автоматический, без настройки).
- Минимальный размер окна настроек — 900×620 (minimumWidth/Height), ниже карточка модели ломается.
- Проверка макета Юркой: 10 экранов отрисованы; два дефекта вёрстки (кнопки диалога NOTICE, перенос
  заголовка статистики) — раунд правок 1 из 2.

## 2026-09-08 — G2: направление интерфейса — A «Панель»
- Контекст: UXAnalyst сдал `design/flows.md` (S1–S16, все состояния, 19 сообщений об ошибках) и три
  направления × 10 экранов (`design/mockups/directions/`): A «Панель» (сайдбар), B «Лента» (одна колонка),
  C «Пульт» (поповер от трея + второй уровень). Юрка проверил рендер 13 ключевых экранов.
- Решение: **A «Панель»** — окно 900×620, левый сайдбар из 6 разделов (Общие · Модели · Вывод ·
  Сеть и обновления · Продвинутые · О программе) + «Отладка» внизу, строки «заголовок · ? · контрол»,
  строка-статус внизу «модель слева · обновление справа · версия». Плюс: **пилюля резиновая под текст**
  (не фиксированные 172 px), **каталог показывается целиком с прокруткой** (фильтры «язык» и «только
  отечественные»), **подтверждение «Готово» в пилюле — 500 мс** (PRD F3.2: было 300 мс — правка).
- Почему: выбор заказчика (чат 2026-09-08); рекомендация UX — канон категории, дешевле всего, предсказуемо для админа.
- Гейт: G2 · «да» заказчика.
- Последствия: разворот A в полный макет (все экраны и состояния, светлая+тёмная) → токены
  (design-engineer) → дизайн-спец (Юрка) → ⛔ G3 одной приёмкой.

## 2026-09-07 — G1: PRD 0.2 + истории + беклог приняты
- Контекст: ProductManager сдал PRD 0.2 (ревизия 2): 16 модулей F1–F16, 16 сценариев S1–S16 (71 GWT),
  каталог 10 Must + 2 Could, манифест `catalog.json` v1, два трека обновлений, профиль «защищённый
  контур», гипотеза с проверкой 2026-11-15; 79 историй; беклог RICE/MoSCoW.
- Решение: **PRD, stories, backlog версии 0.2 приняты** как договор о том, что строим. Релизы:
  v0.1 «Диктовка работает» (2026-09-30) → v0.2 «Каталог, обновления, автозапуск» (2026-10-15) →
  v1.0 «Корпоративный контур и полировка» (2026-10-31); ГОСТ-подпись/ЗПС-пилот — v1.1.
- Почему: требования заказчика №1–5 покрыты; спорные развилки закрыты ответами заказчика (см. записи выше).
- Гейт: G1 · «да» заказчика (чат 2026-09-07).
- Последствия: старт дизайна (UX-флоу → 2–3 направления → G2 → токены → дизайн-спец G3); дальнейшие
  изменения скоупа — только через журнал решений и ревизию PRD.

## 2026-09-07 — G2a закрыт: лого-пак знака 06 «Слоги» принят к работе
- Контекст: доводка после выбора заказчика; замечание Юрки о похожести на Breeze «⋯/загрузка».
- Решение: финальный знак — `design/brand/logo.svg` (имя в кривых), мастер-сетка 100×51,72; точки
  1 : 1,38 : 1,90, промежутки 2,83 : 1, строка шире ряда в 1,23; **цвет состояния несёт только третья
  точка** на всех размерах; 16 px нарисован отдельно. Пак: hicolor 16…512 + scalable, трей 22/16 × 6
  состояний по соглашению KDE, фавикон, README-марка. Источник истины — `brand-basics.md` §4–5.
- Почему: похожесть с системными иконками снята пятью правками геометрии без смены идеи.
- Гейт: G2a · «да» заказчика на направление и на лого-пак («Лого-пак принял», чат 2026-09-07 18:10).
- Последствия: вход для `design-engineer` (токены) и `ux-analyst` (макеты).

## 2026-09-07 — Ответы заказчика на открытые вопросы PRD (§12.1) — вход для ревизии PRD
- Контекст: ProductManager сдал PRD/истории/беклог с шестью вопросами, меняющими продукт.
- Решения заказчика (чат 2026-09-07):
  1. **Сетевые проверки на первом экране — тумблер ПУСТ**, пользователь включает сам (рекомендация
     юриста); во всех профилях. Требования №1/№4 выполняются после явного включения.
  2. **Модель держим в памяти постоянно** (≈415 МБ); выгрузка по простою — опция в настройках.
  3. **ЗПС-трек (ГОСТ-подпись, ВМ с ЗПС, цель Ц5) — после v1.0.** В v1.0 остаётся детект
     `DIGSIG_ELF_MODE` и ветка «скачать пакет и передать администратору». Уточняет решение
     «оба режима сразу» от 2026-09-07: второй режим в v1 — без подписи артефактов.
  4. **Автозапуск включён по умолчанию** на последнем экране онбординга; **переносится в v0.2**.
- Решения Юрки (рутинные): хоткей по умолчанию — `Ctrl+Space` (push-to-talk, как в Handy заказчика);
  Whisper в каталоге — три позиции (large-v3-turbo / small / base), medium и large-v3 не включаем.
- Гейт: G1 (в процессе приёмки) · «да» заказчика по каждому пункту.
- Последствия: ревизия 2 PRD/историй/беклога; v1.0 = требование №5 + полировка + комплаенс без Ц5.

## 2026-09-07 — G2a (часть 3): знак — 06 «Слоги»
- Контекст: раунд 2 (последний по лимиту) — восемь направлений (01/01b, 03/03b, 05–08); дизайнер
  рекомендовал 05 «Абзац», безопасный — 03b; Юрка отметил риски: 07 читается как тумблер, 06 — как
  значок «ещё/загрузка», 01b — как скобки кода.
- Решение: **06 «Слоги»** — три точки с сужающимися промежутками (дискретная речь) над сплошной строкой
  (непрерывный текст). Доводка: усилить отличие от «⋯/загрузка» в трее (ритм промежутков, вес строки),
  логотип с текстом в кривых, лого-пак (hicolor PNG 16…512 + SVG, моно, фавикон, 5 трей-состояний).
- Почему: выбор заказчика (чат 2026-09-07) — строгая геометрия без «звуковой волны».
- Гейт: G2a · «да» заказчика. Лимит раундов исчерпан — дальше только доводка внутри направления.
- Последствия: UX-макеты и токены берут знак 06 и палитру brand-basics; `design/brand/logo.svg` — источник истины.

## 2026-09-07 — Лицензия репозитория: GPL-3.0-or-later (вместо предварительной MIT)
- Контекст: правовое заключение (`research/legal.md`): PyQt5 доступен только под GPL v3 / коммерческой
  лицензией Riverbank → утилита на PyQt5 обязана распространяться под GPL v3; MIT в репо некорректна.
  Заказчик 2026-09-07 подтвердил, что GPLv3 допустима.
- Решение: `LICENSE` = GPL-3.0-or-later; атрибуции моделей/движков — в `NOTICE` (sherpa-onnx §4(d) Apache-2.0,
  Whisper MIT/Apache-2.0 — обе, CC-BY-модели — с указанием).
- Почему: единственный полный Qt-биндинг для Python в apt Astra 1.8 — PyQt5; закрытая поставка не нужна.
- Гейт: — · «да» заказчика (ответ «GPLv3 допустима»).
- Последствия: сторонние вклады принимаются под GPL; при смене стека на LGPL (PySide6/GTK4) лицензию можно ослабить.

## 2026-09-07 — Рамки продукта до PRD: аудитория/ЗПС, лицензия, индикация записи
- Контекст: ресёрч платформы показал, что при включённой ЗПС (digsig) самообновление «одной кнопкой»
  невозможно (подпись ELF и .so ГОСТ-ключом, регистрация ключа админом с перезагрузкой); единственный
  полный Qt-биндинг для Python в apt Astra 1.8 — PyQt5 (GPLv3); канон индикации записи — пилюля поверх окон.
- Решение (три ответа заказчика, чат 2026-09-07):
  1. **Оба режима обновления в v1**: без ЗПС — GitHub Releases → .deb → polkit → apt из GUI;
     с ЗПС — уведомление + скачивание пакета «передать администратору»; релизы подписываются
     (ГОСТ-подпись пакетов и вендоренных .so — отдельная веха), проверка на ВМ с ЗПС обязательна.
  2. **GPLv3 допустима** → стек Python 3.11 + PyQt5 5.15 из apt Astra (код репо — MIT; сборка
     распространяется на условиях GPL). Закрытая поставка не требуется.
  3. **Индикация записи — пилюля с визуализацией по умолчанию, отключаемая** в настройках
     (остаются трей + звук); опыт фокуса на KDE/X11 из Astra Cowork учитывать.
- Почему: заказчик хочет покрыть и свою машину, и корпоративные ALSE; GPL не мешает; пилюля — ожидание пользователей.
- Гейт: — (рамки для G1) · «да» заказчика.
- Последствия: в PRD — две ветки обновлятора и настройка «источник обновлений/моделей» (корпоративное
  зеркало); архитекторам — PyQt5 как заданное ограничение; UX — экран настроек с выключателем пилюли.

## 2026-09-07 — Решения Юрки по открытым вопросам ресёрча (рутинные, без гейта)
- Модель по умолчанию — текущая GigaAM v3 e2e_rnnt; e2e_ctc — в каталоге как «быстрее на ~25 %».
- Каталог v1 — не менее 8 моделей (GigaAM v3 ×3, GigaAM Multilingual, Whisper ×3–4 int8, Vosk small-ru).
- CC-BY-модели допускаются с атрибуцией в «О программе»; Silero (CC-NC) исключён.
- ОЗУ и скорость в каталоге — замер на этой машине после первого прогона (VmHWM воркера) с пометкой
  «замерено / заявлено / оценка»; до замера — справочные значения.
- Зеркало моделей в GitHub Releases репо допустимо (MIT); настройка «корпоративный URL каталога» — в v1.
- Постобработка LLM (как в Handy) — вне v1.
- История буфера Klipper: история «не оставлять распознанный текст в истории буфера» — в беклог (Should).

## 2026-09-07 — G2a (часть 2): цвет «слушаю» — бирюза; знак — второй раунд
- Контекст: показаны 4 направления знака; заказчик: «Ёлочки» (01) и «Клавиша» (03) — более-менее,
  «Волна» (02) и «Каретка» (04) не зацепили, нужны ещё варианты.
- Решение: цвет состояния «слушаю» в трее — **фирменная бирюза #12B3A0** (красный только для ошибки);
  запущен **раунд 2** brand-designer (3–4 новых направления + 01/03 под именем Astra Voice).
  Это последний раунд по лимиту (🔁 2); если и он не попадёт — расхождение вкусов выносится заказчику
  вопросами, не угадыванием.
- Почему: выбор заказчика (чат 2026-09-07).
- Гейт: G2a (цвет) · «да» заказчика; знак — открыт.
- Последствия: UX-макеты стартуют на палитре/типографике brand-basics с плейсхолдером знака;
  финальный знак подставляется после выбора.

## 2026-09-07 — G2a (часть 1): имя продукта — Astra Voice
- Контекст: brand-designer предложил 6 вариантов (рекомендовал «Вслух»); «Astra Voice» помечен риском —
  «Astra» товарный знак ГК «Астра» (заказчик связан с Астрой; repo под личным аккаунтом ArrivaRUS).
- Решение: имя **Astra Voice** (латиницей), slug/репо `astra-voice` остаются без переименования.
  Текстовая часть во всех концептах знака заменяется с «Вслух» на «Astra Voice» при доводке.
- Почему: выбор заказчика (чат 2026-09-07); продукт живёт в экосистеме Astra Linux.
- Гейт: G2a (имя) · «да» заказчика.
- Последствия: вопрос прав на знак «Astra» остаётся в списке задач комплаенса (`research/legal.md`);
  направление знака и цвет «слушаю» — отдельный выбор (G2a часть 2).

## 2026-09-07 — Публичный репозиторий и лицензия (предварительно)
- Контекст: заказчик попросил публичный GitHub-репо под проект. Публичному репо нужна лицензия.
- Решение: репо `ArrivaRUS/astra-voice` (public, ветка `main`); лицензия **MIT** — предварительно,
  как у GigaAM/Whisper/Handy; смена лицензии — одной правкой файла.
- Почему: MIT совместима с лицензиями используемых моделей и движков и не мешает корпоративному использованию.
- Гейт: — · «да» заказчика на публичный репо (чат 2026-09-07); лицензия — допущение Юрки, ждёт подтверждения.
- Последствия: модели в git не кладём (только манифесты с ревизией и sha256), секреты — никогда.

## 2026-09-07 — G0: строим (короткий маршрут Discovery)
- Контекст: заказчик сформулировал задачу и требования сам («упаковать в новую утилиту с нуля под ALSE»);
  рынок/спрос проверять не нужно — утилита заменяет работающий Handy на машинах заказчика.
- Решение: **GO** без рыночного ресёрча; Discovery только техническое (движки/модели, самообновление,
  автозапуск, GUI-стек под ALSE) + правовое заключение (лицензии моделей, 152-ФЗ по голосу, ЗПС).
  Из-за автономной сессии (заказчик не отвечает по ходу) волны ведутся параллельно, а гейты
  G1/G2a/G2b выносятся заказчику одним пакетом.
- Почему: экономия времени заказчика; риск расхождения PRD↔дизайн мал — требования конкретные.
- Гейт: G0 · «да» заказчика (сам запрос 2026-09-07).
- Последствия: имя проекта — рабочее (`astra-voice`), финальное — на G2a вместе с логотипом.
1:# PRD — Astra Voice v1 (голосовой ввод для Astra Linux SE)
3:> Версия PRD **0.3 (ревизия 3 — только данные каталога)** · 2026-09-08 · ProductManager (команда Юрки) · статус: **принят на G1 (0.2, 2026-09-07)**; ревизия 3 — правка цифр каталога по `research/catalog-numbers.md` и записи «Решения Юрки по цифрам каталога» (2026-09-08).
7:> исходники Handy 0.8.3 (`~/Документы/handy-gigaam/vendor/Handy/`); в ревизии 2 учтены `research/tech-codex.md`
13:## 1. Фон и цели
15:### 1.1 Почему сейчас
24:### 1.2 Целевые пользователи (роль · черта · сценарий)
33:### 1.3 Бизнес-цели и метрики (SMART)
44:### 1.4 Проверка гипотезы (обязательный блок)
53:## 2. Обзор требования (≤ 30 слов)
58:## 3. Рамки (из `decisions/log.md`, не пересматриваются)
78:## 4. Декомпозиция: роли, объекты, путь пользователя
81:Администратор (П3 — не через UI утилиты, а через пакет, `policy.conf` и инструкцию); Система (фоновые
87:| Модель (локальная) | скачать / установить из файла | карточка, статус | обновить до новой ревизии (с откатом) | удалить с диска |
101:| Сетевые проверки | по своим тумблерам | по своим тумблерам | может **запретить** (`policy.conf`: офлайн, корпоративный URL, только отечественные модели) |
104:`~/.local/state/astra-voice/`, `~/.cache/astra-voice/`. Ничего общесистемного, кроме пакета и `/etc/astra-voice/policy.conf`.
112:## 5. Детальный дизайн фич
118:### F1. Установка и первый запуск (онбординг) — P0
125:4. Экраны: **(1) Приветствие и сеть** — язык интерфейса (ru/en, авто по `LANG`) и два тумблера «Проверять обновления утилиты» / «Проверять обновления моделей» (подпись: не чаще раза в сутки; хосты huggingface.co, github.com). **Оба тумблера пусты по умолчанию во всех профилях** — пользователь включает сам; пока не включил — утилита в сеть не выходит (кроме явного «Скачать» на экране 2). Требования заказчика №1/№4 (проверка при запуске) действуют после явного включения. Если `policy.conf` задаёт `offline=true`/`profile=secure`, тумблеры показаны заблокированными («Задано администратором»). **(2) Модель** — рекомендованная карточка сверху (GigaAM v3 RNN-T), остальные по размеру; кнопки «Скачать» (показывает хост и объём) и «Установить из файла/папки…»; шаг нельзя пропустить без модели. **(3) Горячая клавиша** — предзаполнена **`Ctrl+Space`, режим «удерживать» (push-to-talk)**; поле захвата другой комбинации, переключатель «удерживать / нажать-нажать»; проверка конфликта с глобальными шорткатами KDE (`kglobalshortcutsrc`) — предупреждение, не запрет. **(4) Проверка микрофона** — выбор устройства, живой индикатор уровня, кнопка «Сказать тестовую фразу» → распознанный текст на экране (без вставки). **(5) Автозапуск** — тумблер (по умолчанию включён, с текстом, что именно создаётся) и «Готово».
130:### F2. Захват речи и горячие клавиши — P0
146:### F3. Индикация: пилюля, трей, звук — P0
153:4. Иконка трея (SNI/`QSystemTrayIcon`): 4 состояния — «готов», «слушаю» (бирюза), «распознаю», «ошибка/микрофон недоступен» (красный). Тема иконки — по теме панели (светлая/тёмная). Регистрация в трее с ретраем до 30 с после старта (гонка с панелью).
159:### F4. Распознавание (движок, воркер, модель) — P0
173:### F5. Вставка текста в активное окно — P0
187:### F6. Ошибки микрофона и аудио — P0
200:### F7. Каталог моделей — P1 (минимум для v0.1: одна встроенная модель по умолчанию)
201:**Описание.** Экран «Модели»: секции «Установленные» и «Доступные», карточки с честными цифрами, скачивание с докачкой и проверкой sha256, удаление, выбор активной.
216:| Статус | не скачана / скачивается (прогресс, скорость, «Отмена») / проверяется (sha256) / установлена / активна / обновление доступно / повреждена |
219:1. Загрузка во временный `.part` с докачкой по `Range` (продолжать только при 206 с корректным `Content-Range`; на 200 — сначала); URL закреплён на `/resolve/{revision}/…`, не на `main`; после завершения — размер и sha256 против манифеста, `fsync`, атомарный `os.replace`; модель хранится в каталоге `<ревизия>/`, активная ревизия — указатель `current.json` (откат = смена указателя); несовпадение → удалить и показать ошибку, не «доклеивать».
220:2. Источники по очереди: HF → GitHub Releases (наше зеркало) → корпоративный URL; переход к следующему при ошибке сети/429/404; отображается, откуда качаем. Запись каталога привязана к рантайму (`engine`): экспорт под `onnx-asr` (istupakov) и под `sherpa-onnx` (csukuangfj) — разные записи, не взаимозаменяемые.
221:3. Установка из файла/папки: те же файлы, та же проверка sha256 по манифесту; неизвестный набор файлов (нет в манифесте) → «Своя модель» без полосок, P2.
228:**Состав каталога v1** — §7.2. **Формат манифеста** — §7.3.
231:### F8. Проверка обновлений моделей при старте — P1
232:**Описание.** Раз в сутки, в фоне, не блокируя работу: (A) новая ревизия закреплённой модели; (B) обновлённый манифест каталога с новыми моделями; (C) сигнал «у автора вышла близкая модель».
233:**Story.** Как П2, я хочу узнавать при запуске, что вышла новая ревизия моей модели или близкая модель, чтобы не следить за HF вручную.
235:1. Работает только при включённом пользователем тумблере «Проверять обновления моделей» (по умолчанию выключен; онбординг, экран 1); в офлайн-режиме, при `policy.conf: offline` или `HF_HUB_OFFLINE=1` в окружении — никогда.
236:2. Не чаще **1 раза в 24 ч**, запуск через случайную задержку 0–30 мин после готовности GUI, общий таймаут 3 с, ≤ 1 повтор; при 429 — не повторять до конца окна; кэш `~/.local/state/astra-voice/update-cache.json`. User-Agent `astra-voice/X.Y.Z (+repo URL)`. Прокси — из окружения.
237:3. **(A) Новая ревизия.** Для активной и всех установленных моделей сравнивается коммит **нужной ветки** (`sha` из `GET /api/models/{repo}/revision/{branch}`, `targetCommit` из `/refs` или `X-Repo-Commit` в HEAD; для `ai-sage/GigaAM-v3` варианты = ветки, `main` не показатель) с `pinned_revision`. Отличие → бейдж «Обновление доступно» на карточке + строка внизу «Обновление модели: GigaAM v3 RNN-T» (нейтральный цвет) + одно системное уведомление. **Ничего не скачивается само.**
238:4. Установка обновления модели: по кнопке → скачать в staging → sha256 → **смоук-распознавание** вшитого тестового wav (ожидаемые слова) → атомарная подмена → старая ревизия хранится до следующего успешного запуска (откат кнопкой «Вернуть предыдущую», P2) ; провал смоука → откат автоматически + ошибка «Новая ревизия не прошла проверку, оставлена текущая».
241:7. «Пропустить эту ревизию» — не напоминать про конкретный sha.
244:### F9. Проверка и установка обновлений утилиты — P1 (трек без ЗПС), P2 (трек ЗПС)
251:   - **Трек A (без ЗПС, пользователь может пройти polkit):** «Скачать и установить» → скачать `.deb` + `SHA256SUMS` + подпись в `~/.cache/astra-voice/updates/` → проверить sha256 и подпись релиза (ключ вшит в пакет; minisign или GPG — архитекторам) → **один** вызов root-помощника `/usr/libexec/astra-voice-update-helper` через `pkexec` с собственным polkit-действием (`auth_admin`, одно окно пароля KDE); помощник копирует пакет в root-staging, **повторно** проверяет подпись/sha256/имя пакета/архитектуру/повышение версии и выполняет `apt-get install --no-remove ./astra-voice_X.Y.Z_amd64.deb` → «Установлено. Перезапустить сейчас / позже». Если агент polkit не запущен — «Не найден агент авторизации (polkit-kde-authentication-agent-1)» + подсказка «Обновить вручную: sudo apt install ./…»; первая установка — штатным apt (помощник появляется с первым пакетом).
252:   - **Трек B (ЗПС включена, или нет прав администратора, или `policy.conf: updates=admin`):** «Скачать пакет для администратора» → те же файлы + `INSTALL-ADMIN.md` (инструкция: проверка подписи релиза, `apt install`; при включённой ЗПС — подпись ELF/`.so` по процедуре заказчика; в v1 артефакты **не** подписаны ГОСТ-ключом на сборке — это v1.1) → «Открыть папку» + текст «Установку выполняет администратор. Подпись пакета проверена». Кнопки «Установить» **нет**.
254:4. Определение трека: ЗПС = `DIGSIG_ELF_MODE ≠ 0` (если файл конфигурации читаем) **или** `policy.conf: zps=on`; при невозможности определить — вопрос пользователю «На этом компьютере включена замкнутая программная среда? Да / Нет / Не знаю (→ трек B)». Права = членство в `astra-admin`/`sudo`.
255:5. **Никогда**: тихая установка, установка без проверки подписи, установка при несовпадении sha256 (файл удаляется, ошибка «Пакет не прошёл проверку»), запуск `dpkg -i` (только `apt-get install ./файл`), привязка polkit-действия напрямую к `/usr/bin/apt` (произвольные аргументы), `auth_admin_keep`, JS-правила в `/etc/polkit-1/rules.d`.
260:### F10. Автозапуск — P1 (v0.2; заказчик 2026-09-07: включён по умолчанию на последнем экране онбординга)
271:### F11. Настройки, тема, язык — P0 (минимум) / P1 (полный экран)
279:4. Настройки — один JSON в `~/.config/astra-voice/settings.json`, версионированный (`schema_version`), миграции вперёд; «Сбросить настройки» с подтверждением.
280:5. Строки, зафиксированные `policy.conf`, показываются заблокированными с подписью «Задано администратором».
282:**Исключения.** Битый settings.json → резервная копия `.bak`, настройки по умолчанию, уведомление.
284:### F12. Трей и меню — P0
288:### F13. «О программе», NOTICE, PRIVACY — P1
290:**Бизнес-правила.** Экран показывает: версию утилиты (`font-mono`) и дату сборки, версии Python/PyQt5/onnxruntime/onnx-asr, активную модель с ревизией, лицензию утилиты (GPL-3.0-or-later), кнопку «Лицензии компонентов и моделей» (содержимое `NOTICE`: имя, правообладатель, лицензия, ссылка — sherpa-onnx Apache-2.0 с NOTICE по §4(d), Whisper — по моделям: large-v3-turbo MIT, small и base Apache-2.0, CC-BY-модели с атрибуцией), «Приватность» (`PRIVACY.md`: аудио не покидает устройство, не сохраняется; точный список хостов; как выключить сеть), дисклеймер о товарных знаках («Astra Linux — товарный знак ПАО «Группа Астра»; GigaAM — Сбер; Whisper — OpenAI; продукт не аффилирован…»), пути данных/логов с кнопками «Открыть», «Статистика» (§10), «Пройти настройку заново», «Обновить из файла…». Те же `NOTICE`/`PRIVACY.md`/`LICENSE` — внутри deb (`/usr/share/doc/astra-voice/`).
293:### F14. Приватность, сеть, политика администратора — P1
298:3. `/etc/astra-voice/policy.conf` (читается при старте, только чтение): `profile=personal|secure`, `offline=true|false`, `catalog_url=`, `updates_url=`, `updates=gui|admin`, `zps=auto|on|off`, `domestic_only=true|false`, `autostart_default=`. `profile=secure` ⇒ `offline=true`, `updates=admin`, `domestic_only=true`, если не переопределено. Значения из policy блокируют соответствующие строки настроек.
302:**Acceptance.** Given `policy.conf` с `profile=secure`, When запускаю утилиту, Then строка обновлений показывает «Проверка обновлений отключена (задано администратором)», в «Моделях» нет кнопок «Скачать» с сети, показаны только отечественные модели, а `tcpdump` за сессию не фиксирует исходящих соединений процесса.
304:### F15. Манифест каталога и корпоративный URL — P1
306:**Бизнес-правила.** Формат — §7.3; встроенный манифест в пакете — источник по умолчанию; `catalog_url` (настройка или policy) — тот же JSON-формат + отсоединённая подпись (ключ — наш вшитый либо корпоративный из `policy.conf: catalog_pubkey=`); `sources[].type=corp` разрешаются относительно `corp_base`; манифест без валидной подписи не применяется (предыдущий остаётся). Инструмент `astra-voice-catalog` (CLI, P2) для генерации корпоративного манифеста из папки с весами (считает sha256, размеры).
307:**Acceptance.** Given `catalog_url` указывает на внутренний сервер с подписанным манифестом и весами, When открываю «Модели» без доступа в интернет, Then вижу модели из корпоративного манифеста, скачиваю GigaAM v3 с внутреннего сервера, sha256 совпадает, модель становится активной.
309:### F16. Диагностика — P2
311:**Бизнес-правила.** «Отладка» (по `Ctrl+Shift+D`): уровень логов, «Открыть лог», «Собрать диагностику» — zip с логами, `settings.json` (без путей домашнего каталога), версиями, выводом `pactl info`/`wpctl status`, **без** аудио и текстов; отправка — только вручную пользователем. Тестовый экран микрофона (уровень + распознать без вставки).
315:## 6. Сценарии end-to-end и acceptance (Given-When-Then)
317:### S1. Первый запуск / онбординг (F1, F7, F2, F6, F10)
319:- **A2.** Given экран 2 «Модель» и доступный huggingface.co, When нажимаю «Скачать» у GigaAM v3 RNN-T, Then вижу хост и объём (222 МБ), прогресс с процентами и скоростью, затем «Проверяю…» и статус «Установлена · Активна»; sha256 всех файлов совпадает с манифестом.
326:### S2. Диктовка push-to-talk (F2, F3, F4, F5)
333:### S3. Диктовка toggle (F2)
338:### S4. Вставка в активное окно (F5)
345:### S5. Микрофон молчит / отсутствует (F6)
351:### S6. Нет сети (F8, F9, F7)
356:### S7. Смена модели (F7, F4)
362:### S8. Обновление модели при старте (F8)
364:- **A2.** Given то же, When нажимаю «Обновить», Then скачивание в staging, sha256, смоук-распознавание тестового wav; при успехе — активна новая ревизия, при провале — «Новая ревизия не прошла проверку, оставлена текущая», текущая работает.
371:### S9. Обновление утилиты — трек без ЗПС (F9)
373:- **A2.** Given панель, When нажимаю «Скачать и установить», Then «Загрузка… N %» с прогрессом, затем проверка sha256 и подписи, затем **одно** окно polkit KDE (помощник обновления); после ввода пароля — «Установка…», затем «Установлено. Перезапустить сейчас / позже»; `dpkg-query -W astra-voice` = 0.2.1.
380:### S10. Обновление утилиты — трек с ЗПС / без прав (F9)
381:- **A1.** Given `DIGSIG_ELF_MODE=1` (или `policy.conf: zps=on`), When есть новая версия, Then панель показывает «Скачать пакет для администратора», кнопки «Установить» нет, текст «На этом компьютере включена замкнутая программная среда — установку выполняет администратор».
387:### S11. Автозапуск вкл/выкл (F10)
393:### S12. Отключение пилюли (F3)
398:### S13. Тема тёмная/светлая из KDE (F11, F3)
402:### S14. Отмена во время распознавания и очередь (F2)
406:### S15. Диск и удаление модели (F7)
409:- **A3.** Given загрузка оборвалась на 60 %, When сеть вернулась и нажато «Повторить», Then продолжается с 60 % (запрос `Range`), итоговый sha256 верен.
411:### S16. Удаление утилиты
416:## 7. Каталог моделей
418:### 7.1 Требования к карточке — см. F7 (таблица полей). Протокол честности:
422:- ОЗУ — три уровня достоверности; при смене ревизии модели или числа потоков замер сбрасывается.
424:- Единственный источник цифр §7.2 — `research/catalog-numbers.md` (HF API `?blobs=true`, байты; ревизии `main` на 2026-09-08). Правка цифр — только через него.
426:### 7.2 Состав каталога v1 (Must = 9, Should = 1, Could = 2)
427:Состав — по своду `research/summary.md` §6 и решениям 2026-09-07/08 (Whisper — три позиции; T-one — Should). **Цифры — из `research/catalog-numbers.md`** (источник истины, 2026-09-08): диск — сумма точных байт файлов, нужных рантайму, по `GET /api/models/{repo}?blobs=true`, **десятичные МБ** (в ревизии 2 девять позиций были в МиБ); WER — Russian LibriSpeech test, onnx-asr `comparison.md` (**fp32-веса**); RTFx — x64 **int8**, `benchmarks.md` (Ryzen 7 9800X3D). Где у модели два экспорта (sherpa-onnx / onnx-asr), указаны оба размера — рантайм v1 выбирают архитекторы, в манифест попадает один. ОЗУ — оценка ×1,85 от int8-весов (разброс 1,5–3×), к fp32 (T-one) неприменима; заменяется замером (US-5.4).
442:Ревизии `main` всех репозиториев на 2026-09-08 и точные байты — в `research/catalog-numbers.md` §1; манифест закрепляет их как `sources[].revision`. Whisper-репозитории `csukuangfj/sherpa-onnx-whisper-*` **без файла лицензии** — юридически опираемся на карточки `openai/whisper-*` (NOTICE по моделям раздельно, §9.6). Исключены: Silero (CC-NC), Canary/Parakeet (слабы по-русски, CC-BY), GGUF/transcribe.cpp (нужен C++-движок), Whisper medium/large-v3 (непригодны по скорости на CPU — решение 2026-09-07), fp32-варианты GigaAM (885 МБ, без выигрыша). **Рантайм v1 (onnx-asr или sherpa-onnx) и раскладка файлов — решение архитекторов**, PRD не выбирает; условие неизменно: источник весов соответствует рантайму (`onnx-asr` ↔ istupakov/onnx-community; `sherpa-onnx` ↔ csukuangfj), пресеты держим у себя с закреплённым коммитом и sha256.
444:### 7.3 Формат манифеста каталога (`catalog.json`, версия 1)
456:      "engine": "sherpa-onnx", "layout": "sherpa-nemo-transducer",
467:        {"path": "encoder.int8.onnx", "size": 224570820, "sha256": "369f35a7…"},
468:        {"path": "decoder.onnx", "size": 4600132, "sha256": "38fc7475…"},
469:        {"path": "joiner.onnx", "size": 2712896, "sha256": "602ff701…"},
470:        {"path": "tokens.txt", "size": 13354, "sha256": "…"}
479:      "smoke_test": {"wav": "builtin:test-ru-6s.wav", "expect_any": ["проверка", "связи"]}
484:      "engine": "onnx-asr", "layout": "onnx-community-whisper",
492:      "files": [{"path": "encoder_model_int8.onnx", "size": 92326127, "sha256": "…"}, {"path": "decoder_model_merged_int8.onnx", "size": 156750845, "sha256": "…"}],
498:Правила: **`engine`** ∈ {`onnx-asr`, `sherpa-onnx`} и **`layout`** (именованная раскладка файлов: `sherpa-nemo-transducer`, `sherpa-nemo-ctc`, `onnx-asr-gigaam-v3`, `onnx-asr-gigaam-multilingual`, `onnx-community-whisper`, `sherpa-whisper`, `vosk-onnx`, `onnx-asr-nemo` — список закрывают архитекторы) обязательны на каждую модель; репозиторий и файлы записи — из таблицы `research/catalog-numbers.md` §1 для выбранного рантайма (в v1 поставляется один рантайм, манифест содержит записи только для него; второй пример выше показывает раскладку onnx-asr). `quality.kind`/`speed.kind` ∈ {`measured`, `benchmark`, `benchmark_fp32`, `no_data`}; `quality.weights` фиксирует, на каких весах измерен WER (в бенчмарке onnx-asr — `fp32`); при `no_data` полоска не рисуется. `origin.kind` ∈ {`ru`, `foreign`} — по юрлицу правообладателя, `note_ru` — уточнение для карточки («команда из России»). `sha256` обязателен для каждого файла (для LFS — из HF API: `lfs.sha256` в `GET /api/models/{repo}?blobs=true` или `lfs.oid` в `/api/models/{repo}/tree/{rev}` — одно и то же значение; для не-LFS файлов вроде `tokens.txt` API даёт git-SHA-1, поэтому sha256 считается при публикации манифеста); `sources[].revision` обязателен, загрузка идёт по `/resolve/{revision}/…`, не по `main`; `update_watch.branch` — ветка-вариант (для `ai-sage/GigaAM-v3` следим за `targetCommit` ветки, не `main`); манифест сопровождается отсоединённой подписью `catalog.json.sig` (тот же инструмент и ключ, что у релизов утилиты — minisign или GPG; все зеркала проверяются одним закреплённым ключом); корпоративный манифест — тот же формат, `sources` может содержать только `corp`; поля `ram_peak_mb.kind` ∈ {`measured`, `claimed`, `estimated`}; локальные замеры хранятся отдельно (`~/.local/state/astra-voice/measurements.json`) и перекрывают манифест в UI; неизвестные поля игнорируются (forward-compat), `manifest_version` > поддерживаемой → отказ с сообщением.
502:## 8. Нефункциональные требования
508:| Память | GUI + трей в покое без модели ≤ 120 МБ RSS (допущение для PyQt5/QML — проверить спайком); воркер с GigaAM v3 ≤ 450 МБ (замер 415; для незамеренных моделей оценка 1,5–3× от int8-весов); суммарно ≤ 600 МБ; замер — VmHWM воркера, дополнительно PSS из `smaps_rollup` |
522:## 9. Безопасность и комплаенс
524:### 9.1 Целостность моделей
525:Каждый файл — sha256 из манифеста (для HF — `lfs.oid`), проверка до `rename`; закреплённые ревизии (`pinned_revision`); смоук-тест после обновления ревизии с автоматическим откатом; манифест подписан (наш ключ вшит; корпоративный — из `policy.conf`). Runtime-загрузка модели трактуется как supply-chain-изменение, а не «данные».
527:### 9.2 Подпись релизов и проверка перед установкой
528:`SHA256SUMS` + отсоединённая подпись релиза (**один инструмент — minisign или GPG — для релизов и манифеста каталога**, выбор за архитекторами; открытый ключ закреплён в пакете; все зеркала проверяются тем же ключом); проверка обязательна и для «обновить из файла»; **ровно один привилегированный шаг**: собственное polkit-действие (`auth_admin`, без `_keep`) привязано к небольшому root-помощнику `/usr/libexec/astra-voice-update-helper`, а не к `/usr/bin/apt`; помощник повторно проверяет подпись/sha256/имя/архитектуру/версию в root-staging и выполняет `apt-get install --no-remove`; политика ставится из deb в `/usr/share/polkit-1/actions/`, JS-правила в `rules.d` не поставляются; никакого `dpkg -i` и никакой установки без проверки; первая установка — штатным apt (помощник появляется с первым пакетом).
530:### 9.3 Трек ЗПС
531:**В v1.0 (заказчик 2026-09-07):** детект `DIGSIG_ELF_MODE`/`policy.conf: zps`, трек B «скачать пакет и передать администратору» (S10), документ для администратора; собственные ELF/`.so` (onnxruntime/движок, помощник обновления — ориентировочно 2–5 объектов, A11) в v1 **не подписаны** ГОСТ-ключом на сборке — на машине с ЗПС их подписывает интегратор/администратор заказчика по своей процедуре (`INSTALL-ADMIN.md`). **После v1.0 (v1.1, Ц5):** подпись всех собственных ELF/`.so` на сборке (`bsign-integrator`, ключ организации), открытый ключ и регистрация админом, `postinst` ничего не подписывает на месте, прогон на ВМ с ЗПС (P21½). Минимизировать число собственных ELF (Python поверх системного Qt) — действует с v0.1.
533:### 9.4 Профиль «защищённый контур» (приказы ФСТЭК № 117/239, Указ № 166)
534:`profile=secure`: тумблеры проверок заблокированы в выключенном состоянии (во всех профилях они выключены, пока пользователь не включил), установка — только администратором, офлайн-путь для моделей и утилиты, `domestic_only=true` (зарубежные по юрлицу правообладателя модели скрыты из каталога: Whisper, FastConformer и обе версии Vosk — Alpha Cephei Inc., США), закреплённые ревизии, документ «Влияние на среду функционирования ALSE» для администратора ИБ (tech-writer, до пилота). Установка обновления **всегда** по явному действию пользователя/администратора — тихой автоустановки не существует ни в одном профиле.
536:### 9.5 Запреты функциональности (явно, L8/L9 из `research/legal.md`)
539:### 9.6 Документы и атрибуции
542:### 9.7 Модель угроз
543:T1 (проектирование) — security-analyst по `security-gate`: поверхность = загрузка моделей, обновлятор (polkit), глобальный хоткей и вставка в чужие окна, `policy.conf`, парсинг манифеста. Ревью T2 на каждом изменении обновлятора; T3 перед публичным релизом.
547:## 10. Аналитика (локальная, без телеметрии)
561:## 11. Не-цели / вне скоупа v1
576:## 12. Открытые вопросы и допущения
578:### 12.1 Открытые вопросы к заказчику (меняют продукт)
579:**Нет.** Шесть вопросов ревизии 1 закрыты заказчиком и Юркой 2026-09-07 (`decisions/log.md`, «Ответы заказчика на открытые вопросы PRD»): 1 — сетевые тумблеры пусты во всех профилях; 2 — `Ctrl+Space` push-to-talk, toggle второй; 3 — модель в памяти постоянно, выгрузка — опция; 4 — ЗПС-трек (подпись, ВМ, Ц5) после v1.0; 5 — Whisper ×3; 6 — автозапуск включён по умолчанию, в v0.2.
581:### 12.2 Допущения (помечены, не выдуманы как факты)
596:## 13. QA-чеклист PRD (10 пунктов + обязательный блок)
601:- [x] Покрыты исключения: таймауты (F8/F9, S6), нет прав (F9 трек B, S10), битые данные (sha256, манифест, settings.json), границы (0,3 с / 120 с / диск / ОЗУ / 429).
613:## 14. Что изменилось в ревизии 2 (2026-09-07)
622:| 7 | F7, §7.3 | sha256 «из `lfs.oid`», загрузка с `main`, `rename` | `lfs.sha256`/`lfs.oid` — одно значение; не-LFS — считать при публикации; загрузка по `/resolve/{revision}/`; каталог `<ревизия>/` + `current.json`; запись каталога привязана к рантайму | tech-codex блок 2 |
630:## 15. Что изменилось в ревизии 3 (2026-09-08, только данные каталога)
639:| 7 | §7.3 | `engine: onnx-asr` при csukuangfj-раскладке (несовместимо) | На модель `engine` ∈ {onnx-asr, sherpa-onnx} + `layout`; пример согласован (sherpa-onnx ↔ csukuangfj), второй пример — onnx-asr ↔ onnx-community; `quality/speed.kind` ∈ {measured, benchmark, benchmark_fp32, no_data}, `quality.weights`; `origin` — объект по юрлицу; CPU бенчмарка — Ryzen 7 9800X3D. Выбор рантайма v1 — за архитекторами | catalog-numbers §4.4; решение Юрки 2026-09-08 |

exec
/bin/bash -lc 'for p in /AGENTS.md /home/AGENTS.md /home/astra/AGENTS.md /home/astra/Документы/AGENTS.md /home/astra/Документы/astra-voice/AGENTS.md; do if [ -f "$p" ]; then cat "$p"; fi; done; rg --files arch design research docs' in /home/astra/Документы/astra-voice
 succeeded in 0ms:
docs/test-plan.md
docs/plans.md
docs/status.md
arch/challenge-astra.md
arch/plan-synth.md
arch/plan-codex.md
arch/plan-claude.md
research/tech-models-claude.md
research/catalog-numbers.md
research/tech-platform-claude.md
research/competitors-ux.md
research/summary.md
research/legal.md
research/tech-codex.md
design/spec.md
design/tokens.json
design/flows.md
design/tokens.md
design/refs/08-onboarding-4-mic-dark.png
design/refs/01-general-mic.png
design/refs/06-about.png
design/refs/09-pill-dark.png
design/refs/09-pill.png
design/refs/01-general-indicator.png
design/refs/08-onboarding-4-mic.png
design/refs/02-models-file-dialogs-dark.png
design/refs/01-general-indicator-dark.png
design/refs/01-general-base.png
design/refs/08-onboarding-2-model-dark.png
design/refs/04-network.png
design/refs/02-models-file-dialogs.png
design/refs/08-onboarding-1-network.png
design/refs/05-advanced-dark.png
design/refs/08-onboarding-2-model.png
design/refs/04-footer-states-dark.png
design/refs/01-general-hotkey.png
design/refs/04-update-admin-dark.png
design/refs/02-models-card-states-dark.png
design/refs/10-dialogs.png
design/refs/08-onboarding-3-hotkey-dark.png
design/refs/04-network-dark.png
design/refs/01-general-mic-dark.png
design/refs/07-debug-dark.png
design/refs/03-output-dark.png
design/refs/04-update-admin.png
design/refs/08-onboarding-1-network-dark.png
design/refs/09-tray-dark.png
design/refs/08-onboarding-5-done-dark.png
design/refs/05-advanced.png
design/refs/02-models-empty-offline-dark.png
design/refs/02-models-catalog-dark.png
design/refs/08-onboarding-3-hotkey.png
design/refs/01-general-hotkey-dark.png
design/refs/02-models-empty-offline.png
design/refs/08-onboarding-5-done.png
design/refs/02-models-catalog.png
design/refs/09-tray.png
design/refs/06-about-dark.png
design/refs/04-update-panel-dark.png
design/refs/07-debug.png
design/refs/02-models-card-states.png
design/refs/01-general-base-dark.png
design/refs/03-output.png
design/refs/04-footer-states.png
design/refs/10-dialogs-dark.png
design/refs/04-update-panel.png
design/brand/concept-6.svg
design/brand/concept-2-tray.svg
design/brand/_build-concepts-r2.py
design/brand/logo-mono.svg
design/brand/concept-3.svg
design/brand/concept-1b-tray.svg
design/brand/concept-3-tray.svg
design/brand/concept-3b.svg
design/brand/logo-mark.svg
design/brand/favicon.svg
design/brand/_build-pack-sheet.py
design/brand/concept-6-tray.svg
design/brand/concept-7.svg
design/brand/logo-pack.html
design/brand/concept-7-tray.svg
design/brand/concepts-r2.html
design/brand/concept-2.svg
design/brand/readme-mark.svg
design/brand/concept-3b-tray.svg
design/brand/concept-8-tray.svg
design/brand/concept-1-tray.svg
design/mockups/directions/B/03-updates.html
design/mockups/directions/B/04-onboarding-hotkey.html
design/mockups/directions/B/03-updates-admin.html
design/mockups/directions/B/04-onboarding-network.html
design/mockups/directions/B/05-pill.html
design/mockups/directions/B/06-tray.html
design/mockups/directions/B/01-general.html
design/mockups/directions/B/02-models-dark.html
design/mockups/directions/B/01-general-dark.html
design/mockups/directions/B/02-models.html
design/mockups/directions/_dir_b.py
design/mockups/directions/index.html
design/mockups/directions/A/03-updates.html
design/mockups/directions/A/04-onboarding-hotkey.html
design/mockups/directions/A/03-updates-admin.html
design/mockups/directions/A/04-onboarding-network.html
design/mockups/directions/A/05-pill.html
design/mockups/directions/A/06-tray.html
design/mockups/directions/A/01-general.html
design/mockups/directions/A/02-models-dark.html
design/mockups/directions/A/01-general-dark.html
design/mockups/directions/A/02-models.html
design/mockups/directions/_dir_a.py
design/mockups/directions/_dir_c.py
design/brand/icons/hicolor/32x32/apps/astra-voice.png
design/mockups/directions/C/03-updates.html
design/mockups/directions/C/04-onboarding-hotkey.html
design/mockups/directions/C/01-popover.html
design/mockups/directions/C/01-popover-dark.html
design/mockups/directions/C/03-updates-admin.html
design/mockups/directions/C/04-onboarding-network.html
design/mockups/directions/C/05-pill.html
design/mockups/directions/C/06-tray.html
design/mockups/directions/C/02-models-dark.html
design/mockups/directions/C/02-models.html
design/mockups/directions/_build.py
design/mockups/directions/_base.py
design/mockups/final/09-pill-dark.html
design/mockups/final/04-update-panel-dark.html
design/mockups/final/06-about.html
design/mockups/final/_p2.py
design/mockups/final/08-onboarding-3-hotkey.html
design/mockups/final/07-debug.html
design/mockups/final/09-tray-dark.html
design/mockups/final/01-general-base-dark.html
design/mockups/final/01-general-hotkey.html
design/mockups/final/05-advanced-dark.html
design/mockups/final/08-onboarding-3-hotkey-dark.html
design/mockups/final/_build.py
design/mockups/final/02-models-catalog-dark.html
design/mockups/final/01-general-mic-dark.html
design/mockups/final/04-update-admin-dark.html
design/mockups/final/04-network-dark.html
design/mockups/final/_p3.py
design/mockups/final/04-update-admin.html
design/mockups/final/09-pill.html
design/mockups/final/02-models-empty-offline-dark.html
design/mockups/final/04-network.html
design/mockups/final/08-onboarding-2-model.html
design/mockups/final/index.html
design/mockups/final/02-models-card-states-dark.html
design/mockups/final/07-debug-dark.html
design/mockups/final/10-dialogs.html
design/mockups/final/_p1.py
design/mockups/final/08-onboarding-1-network-dark.html
design/mockups/final/08-onboarding-2-model-dark.html
design/mockups/final/10-dialogs-dark.html
design/mockups/final/01-general-hotkey-dark.html
design/mockups/final/08-onboarding-4-mic.html
design/mockups/final/01-general-indicator.html
design/mockups/final/02-models-catalog.html
design/mockups/final/04-footer-states.html
design/mockups/final/04-update-panel.html
design/mockups/final/01-general-base.html
design/mockups/final/01-general-indicator-dark.html
design/mockups/final/06-about-dark.html
design/mockups/final/02-models-file-dialogs.html
design/mockups/final/02-models-empty-offline.html
design/mockups/final/03-output-dark.html
design/mockups/final/08-onboarding-4-mic-dark.html
design/mockups/final/_shell.py
design/mockups/final/08-onboarding-1-network.html
design/mockups/final/03-output.html
design/mockups/final/02-models-file-dialogs-dark.html
design/mockups/final/05-advanced.html
design/mockups/final/01-general-mic.html
design/mockups/final/08-onboarding-5-done.html
design/mockups/final/02-models-card-states.html
design/mockups/final/04-footer-states-dark.html
design/brand/favicon-32.png
design/brand/concept-1b.svg
design/brand/concepts.html
design/brand/concept-4.svg
design/brand/concept-5-tray.svg
design/brand/concept-4-tray.svg
design/brand/concept-8.svg
design/brand/logo.svg
design/brand/brand-basics.md
design/brand/concept-5.svg
design/brand/_build-logo-pack.py
design/brand/concept-1.svg
design/mockups/final/09-tray.html
design/mockups/final/08-onboarding-5-done-dark.html
design/brand/icons/hicolor/16x16/status/astra-voice-tray-idle.svg
design/brand/icons/hicolor/16x16/status/astra-voice-tray-mono.svg
design/brand/icons/hicolor/16x16/status/astra-voice-tray-listening.svg
design/brand/icons/hicolor/16x16/status/astra-voice-tray-processing.svg
design/brand/icons/hicolor/16x16/status/astra-voice-tray-error.svg
design/brand/icons/hicolor/16x16/status/astra-voice-tray-done.svg
design/brand/icons/hicolor/48x48/apps/astra-voice.png
design/brand/icons/hicolor/16x16/apps/astra-voice.png
design/brand/icons/hicolor/256x256/apps/astra-voice.png
design/brand/icons/hicolor/512x512/apps/astra-voice.png
design/brand/icons/hicolor/64x64/apps/astra-voice.png
design/brand/icons/hicolor/22x22/status/astra-voice-tray-idle.svg
design/brand/icons/hicolor/22x22/status/astra-voice-tray-mono.svg
design/brand/icons/hicolor/22x22/status/astra-voice-tray-listening.svg
design/brand/icons/hicolor/22x22/status/astra-voice-tray-processing.svg
design/brand/icons/hicolor/22x22/status/astra-voice-tray-error.svg
design/brand/icons/hicolor/22x22/status/astra-voice-tray-done.svg
design/brand/icons/hicolor/128x128/apps/astra-voice.png
design/brand/icons/hicolor/22x22/apps/astra-voice.png
design/brand/logo-dark.svg
design/brand/icons/_src/wordmark.d
design/brand/icons/_src/icon-22.svg
design/brand/icons/_src/icon-64.svg
design/brand/icons/_src/icon-24.svg
design/brand/icons/_src/icon-32.svg
design/brand/icons/_src/icon-16.svg
design/brand/icons/hicolor/24x24/apps/astra-voice.png
design/brand/icons/hicolor/scalable/apps/astra-voice.svg

exec
/bin/bash -lc "nl -ba arch/plan-claude.md | sed -n '103,250p'" in /home/astra/Документы/astra-voice
 succeeded in 0ms:
   103	| `platform/autostart.py` | `~/.config/autostart/astra-voice.desktop`: `Exec=/usr/bin/astra-voice --hidden`, `X-KDE-autostart-after=panel`, без `OnlyShowIn`; состояние = файл есть и нет `Hidden=true`; чужие ярлыки не трогаем; путь **не переписывается** | файл | paths | unit на tmpdir; e2e S11 |
   104	| `core/stats.py` | `stats.json` (последние 1000 событий §10 PRD), p50/p95, медиана RTFx по ≥ 5 «тёплым» прогонам, «Очистить» | файл | paths | unit: перцентили, холодный прогон исключён |
   105	| `core/i18n.py` | Qt Linguist: `qsTr` в QML, `translate` в Python; исходный ru, `en.ts/.qm`; `pylupdate5`/`lrelease` на сборке; плюрализация `%n`; смена языка — с явным «Перезапустить» | `.qm` в `/usr/share/astra-voice/i18n/` | pyqt5-dev-tools, qttools5 | CI: `lupdate` без новых непереведённых строк для en |
   106	| `diag/collect.py` (P2) | zip: логи, `settings.json` без домашних путей, версии, `pactl info`/`wpctl status`; без аудио/текста | файл в `~/` | — | unit: в архиве нет полей `text` |
   107	| `tools/catalog_cli.py` (P2), `scripts/gen_theme.py`, `scripts/build_manifest.py` | CLI корпоративного манифеста из папки (sha256, размеры); генерация `Theme.qml`; сборка `catalog.json` из `research/catalog-numbers.md`-таблицы + HF API (`?blobs=true`, sha256 не-LFS считается локально) | — | — | unit; CI-проверка «регенерация чистая» |
   108	
   109	## 4. Рантайм v1: `onnx-asr` против `sherpa-onnx`
   110	
   111	| Критерий | `onnx-asr` 0.12.0 (MIT, PyPI 2026-07-15) | `sherpa-onnx` 1.13.7 (Apache-2.0) | Вес |
   112	|---|---|---|---|
   113	| GigaAM v3 e2e_rnnt (дефолт) | загрузчик `gigaam-v3` для всех 4 вариантов (`istupakov/gigaam-v3-onnx`, 226,4 МБ) | `nemo_transducer` + community-экспорт csukuangfj (231,9 МБ); v3 в официальном каталоге sherpa **нет** | паритет |
   114	| Каталог 12 позиций | **12/12** с первоисточником под этот рантайм (istupakov/onnx-community/alphacep/t-tech) — таблица `catalog-numbers.md` §1 | GigaAM v3 ×3, Whisper ×3 (свои экспорты, другие размеры, не бенчмарчены); Vosk/T-one/Multilingual/FastConformer — либо нет, либо не подтверждено | **решающий** |
   115	| Протокол цифр (PRD §7.1) | WER/RTFx `comparison.md`/`benchmarks.md` сняты **на onnx-asr** — полоски честны без пересчёта | цифры под sherpa пришлось бы мерить самим на 12 моделях | сильный |
   116	| Колёса / glibc 2.36 | `py3-none-any` (4 МБ, чистый Python) + `onnxruntime` 1.29.0 `manylinux_2_28` (у нас 2.36); проверено: `libonnxruntime.so.1.29.0` — max `GLIBC_2.28`, **0 ссылок `__isoc23_*`** → шим не нужен | `manylinux_2_17` — тоже ставится | паритет |
   117	| Собственные `.so` (ЗПС v1.1) | **3** (`libonnxruntime.so`, `libonnxruntime_providers_shared.so`, `onnxruntime_pybind11_state…so`); numpy — системный (1.24.2 ≥ 1.22.4) | 4–6 (`_sherpa_onnx…so`, `libsherpa-onnx-core`, `libsherpa-onnx-c-api`, `libonnxruntime`) + свой ORT | сильный |
   118	| ОЗУ / скорость | та же арена ORT; декодер RNN-T — цикл на numpy (≈150 кадров на 6 с, ~50–100 мс) — **риск** для Ц2 | декодер на C++ — на 50–100 мс быстрее на RNN-T | против, измеримо (S3) |
   119	| VAD / длинные записи | встроенный Silero-VAD (через ORT), сегментация > 20 с | встроенный Silero-VAD | паритет |
   120	| Сопровождение | один мейнтейнер, 0.x | k2-fsa, зрелый | против (митигация: вендор + пин + Engine-интерфейс) |
   121	
   122	**Решение:** `onnx-asr` + `onnxruntime` **1.29.0** (без `sympy` в зависимостях; `flatbuffers` 2.0.8 / `protobuf` 3.21.12 /
   123	`packaging` 23.0 — из apt Astra, проверено). Пины по sha256 в `packaging/wheels.lock`. **Условие отката** (PRD §1.4): если спайк S3 даст на
   124	эталоне p95 > 0,5 с при e2e_rnnt даже с 4 потоками → дефолт `e2e_ctc` (+25 %), а при провале и этого — `SherpaEngine` для
   125	GigaAM v3 (второй адаптер, строки манифеста с `engine: sherpa-onnx`).
   126	
   127	### 4.1 Раскладки манифеста (`engine: onnx-asr`, единый рантайм v1)
   128	
   129	| `layout` | Модели каталога | Файлы (относительные пути из HF API `?blobs=true` на закреплённой ревизии) |
   130	|---|---|---|
   131	| `onnx-asr-gigaam-v3` | #1 e2e_rnnt (дефолт), #2 e2e_ctc, #3 rnnt | `v3_<variant>_*.int8.onnx` (+ decoder/joint), `vocab`, yaml — вариант в поле `variant`, один HF-репо `istupakov/gigaam-v3-onnx` |
   132	| `onnx-asr-gigaam-multilingual` | #4 CTC 220M, #11 Large CTC | `multilingual*_ctc.int8.onnx`, vocab, yaml, config |
   133	| `onnx-asr-t-one` | #5 T-one (fp32) | `model.onnx`, vocab, config |
   134	| `onnx-asr-vosk` | #6 ru 0.54, #7 small-ru 0.52 | `am-onnx/…` / `am/…` `{encoder,decoder,joiner}.int8.onnx`, `lang/tokens.txt` — подкаталоги в манифесте явные |
   135	| `onnx-community-whisper` | #8 large-v3-turbo, #9 small | `encoder_model_int8.onnx`, `decoder_model_merged_int8.onnx`, токенайзер |
   136	| `onnx-asr-whisper-ort` | #10 base (`istupakov/whisper-base-onnx`) | `whisper-base_beamsearch.int8.onnx`, токенайзер |
   137	| `onnx-asr-nemo` | #12 FastConformer ru pc (CTC) | `model.int8.onnx`, `vocab.txt`, config |
   138	
   139	Адаптер держит таблицу `layout → имя загрузчика onnx-asr + список обязательных файлов`; точные идентификаторы
   140	загрузчиков и имена файлов фиксирует спайк S3 (`scripts/build_manifest.py` генерирует `files[]` с sha256 из HF API,
   141	для не-LFS считает sha256 сам). Пример манифеста PRD §7.3 переписывается под `engine: onnx-asr` для всех записей.
   142	
   143	## 5. Потоки данных
   144	
   145	1. **Диктовка (PTT).** KeyPress → `hotkey.pressed` → пилюля `listening` (≤ 100 мс, без ожидания воркера) → `record.start`
   146	   → воркер открывает устройство (10–40 мс; 3 ретрая по 300 мс при ошибке) → `level` ≤ 30/с → (2 с пик < −60 дБ → `silent`
   147	   → пилюля `listening-silent` + уведомление «Перезапустить звуковую службу» → `systemctl --user restart wireplumber` →
   148	   `record.start` повторно) → KeyRelease → `record.stop` → < 0,3 с? → `empty`-тихо, пилюля скрыта через 150 мс; иначе
   149	   `processing` → VAD (только > 20 с) → `transcribe` → `result` → `paste` (активное окно, запомненное **до** показа пилюли)
   150	   → пилюля `done` 500 мс → `stats.dictation`. Отмена: Esc (временный grab на время записи) / «×» / трей → `record.cancel`.
   151	   Второе нажатие во время `processing` — одна запись в очередь; вставки по порядку.
   152	2. **Загрузка модели.** карточка «Скачать» → гейт сети (явное действие разрешено даже при выключенных тумблерах) →
   153	   `disk_usage ≥ size×1,2` → очередь → файлы по `sources[]` → `.part`+Range → sha256 → `<rev>.partial/` → **смоук**
   154	   (`transcribe.file` вшитого wav, `expect_any`) → `os.replace` → `current.json` → `model.load` → `model.loaded` → замер
   155	   после первой диктовки → `measurements.json` → карточка «замерено». Из файла/папки — тот же путь с шага sha256.
   156	3. **Проверка ревизии модели (F8).** тумблер вкл ∧ ¬offline ∧ ¬policy ∧ прошло ≥ 24 ч → задержка 0–30 мин →
   157	   `GET /api/models/{update_watch.hf_repo}/refs` (таймаут 3 с, ≤ 1 повтор, 429 → до конца окна) → `targetCommit(branch)`
   158	   ≠ `pinned_revision` → бейдж + строка `model-update` + одно уведомление; манифест с `catalog_url` — той же частотой,
   159	   применяется только с валидной подписью. Ничего не скачивается само.
   160	4. **Обновление утилиты (F9).** кэш → фон 0–10 мин → `releases/latest` (или `latest.json` с `updates_url`) → SemVer >
   161	   `__version__` ∧ не «пропущена» → строка `available|admin-track` → панель → трек: A = `DIGSIG_ELF_MODE=0` ∧ (в
   162	   `astra-admin` ∨ `sudo`) ∧ `policy.updates≠admin`; B = ЗПС ∨ нет прав ∨ policy; «спросить» = конфиг нечитаем ∧
   163	   `zps=auto` (ответ «Не знаю» → B). A: скачать `deb + SHA256SUMS + SHA256SUMS.asc` → `gpgv` → sha256 → имя/arch/версия →
   164	   `pkexec update-helper install <путь>` (одно окно polkit; 126 → «отменена», 127/нет агента → подсказка `sudo apt install ./…`)
   165	   → `restart`. B: та же проверка → папка с `INSTALL-ADMIN.md` → «Открыть папку». «Обновить из файла…» — те же шаги с `gpgv`.
   166	5. **Старт.** policy → settings (миграции) → theme → QML (`--hidden`: окно не создаётся, только трей) → трей (ретрай ≤ 30 с)
   167	   → воркер → `model.load` в фоне (строка-статус `loading`) → онбординг, если нет модели/`!onboarding_done` → отложенные
   168	   проверки. Аудио на старте **не открывается** (гонка WirePlumber), `sleep`-костылей нет.
   169	
   170	## 6. Инварианты безопасности (проверяются тестами, T1 — security-analyst)
   171	
   172	1. Установка пакета **только** после `gpgv`-подписи ∧ sha256 ∧ `Package=astra-voice` ∧ `Architecture=amd64` ∧ версия выше
   173	   установленной — и в GUI, и повторно в root-помощнике на **копии** в root-staging (`O_NOFOLLOW`, без symlink/TOCTOU).
   174	2. Ровно один привилегированный шаг: polkit-действие `io.github.arrivarus.astra_voice.update` (`auth_admin`, без `_keep`),
   175	   `exec.path` = помощник; никаких `rules.d`, `dpkg -i`, привязки к `/usr/bin/apt`, аргументов кроме `install <путь>`.
   176	3. Аудио живёт только в ОЗУ воркера (кольцевой буфер), очищается после результата/отмены; ни одного wav/tmp на диске;
   177	   тесты проверяют `inotify`/список файлов до и после диктовки.
   178	4. Распознанный текст не пишется в логи/статистику/диагностику (фильтр логгера); «последний текст» — только в памяти GUI.
   179	5. Сеть — четыре действия PRD F14 через один `HttpClient` с гейтом (тумблеры пусты по умолчанию, `offline`, policy,
   180	   `HF_HUB_OFFLINE=1`); хосты — только из `PRIVACY.md` (`huggingface.co`+CDN, `github.com`/`api.github.com`/
   181	   `objects.githubusercontent.com`, `corp_base`); TLS-проверка не отключается; токенов в клиенте нет; UA проекта.
   182	6. Манифест применяется только с валидной подписью (наш keyring или `catalog_pubkey`); `sources[].revision` обязателен,
   183	   URL всегда `/resolve/{revision}/`; sha256 каждого файла до `os.replace`; смоук до переключения `current.json`.
   184	7. `policy.conf` — только чтение, значения из него блокируют строки UI; `settings.json`, кэш и сокет single-instance — 0600.
   185	8. Хоткей: захватываются только назначенные комбинации (+`Escape` на время записи) — никакого XRecord/keylogging; поле
   186	   захвата держит `XGrabKeyboard` только пока открыто.
   187	9. Буфер обмена: только `text/plain` (+ `x-kde-passwordManagerHint`), восстановление исходного содержимого.
   188	10. Нет телеметрии, crash-dumps, «отправить отчёт», скрытой записи (иконка «слушаю» в трее обязательна всегда).
   189	
   190	## 7. Упаковка, сборка, репозиторий, CI
   191	
   192	### 7.1 Структура репозитория `ArrivaRUS/astra-voice`
   193	```
   194	src/astra_voice/{app.py, core/, platform/, ui/, worker/, models/, net/, updates/, security/, helper/, diag/, tools/}
   195	qml/            Main.qml, Theme.qml (generated), PillTheme.qml, components/*.qml, sections/*.qml, onboarding/*.qml, Pill.qml
   196	data/           catalog.json (+ .sig), catalog.schema.json, icons/ (21 глиф + hicolor/ + tray/), sounds/{start,stop}.ogg,
   197	                fonts/ (PT Mono, PT Root UI — если лицензия позволяет; иначе только fallback PT Astra Sans), vad/silero_vad.onnx,
   198	                test/test-ru-6s.wav, keys/release.gpg, i18n/en.ts, polkit/…update.policy, astra-voice.desktop
   199	packaging/      debian/ (control, rules, changelog, postinst-минимум), wheels.lock (name==ver --hash=sha256), build-deb.sh,
   200	                Containerfile (debian:12), INSTALL-ADMIN.md, sbom/
   201	scripts/        gen_theme.py, build_manifest.py, measure_model.py, release.sh, make_virtual_mic.sh, e2e/*.sh (P21½)
   202	tests/          unit/ integration/ e2e/ (маркеры pytest: unit · xvfb · machine)
   203	docs/           plans.md status.md test-plan.md PRIVACY.md NOTICE  ·  arch/ decisions/ design/ research/  (как сейчас)
   204	```
   205	
   206	### 7.2 Содержимое `.deb` `astra-voice_X.Y.Z_amd64.deb` (оценка ≤ 30 МБ; A1 ≤ 80 МБ выполняется)
   207	- `/usr/bin/astra-voice` — лаунчер (добавляет `/usr/lib/astra-voice/vendor` в `sys.path`, запускает `astra_voice.app`);
   208	  `/usr/lib/python3/dist-packages/astra_voice/` — код; `/usr/lib/astra-voice/vendor/` — `onnxruntime` 1.29.0 (3 `.so`, ~60 МБ
   209	  распакованных), `onnx_asr` — только они; `/usr/share/astra-voice/{qml,icons,sounds,fonts,vad,test,keys,
   210	  i18n,catalog.json,catalog.json.sig}`; `/usr/share/icons/hicolor/{16x16,22x22,scalable}/{apps,status}/`;
   211	  `/usr/share/applications/astra-voice.desktop`; `/usr/share/polkit-1/actions/io.github.arrivarus.astra_voice.policy`;
   212	  `/usr/libexec/astra-voice/update-helper` (root:root 0755); `/usr/share/doc/astra-voice/{NOTICE,PRIVACY.md,INSTALL-ADMIN.md,
   213	  examples/policy.conf,sbom.cdx.json}`. `/etc/astra-voice/` пакет **не создаёт** (policy пишет админ). `postinst` — только
   214	  триггеры dh (иконки/desktop), без сети и GUI.
   215	- **Depends:** `python3 (>= 3.11)`, `python3-pyqt5`, `python3-pyqt5.qtquick`, `python3-pyqt5.qtsvg`, `python3-xlib`,
   216	  `python3-numpy (>= 1:1.22.4)`, `python3-requests`, `python3-jsonschema`, `python3-packaging`, `python3-protobuf`, `python3-flatbuffers`,
   217	  `qml-module-qtquick2`, `qml-module-qtquick-controls2`, `qml-module-qtquick-layouts`, `qml-module-qtquick-window2`,
   218	  `qml-module-qtgraphicaleffects`, `qml-module-qtquick-shapes`, `libqt5svg5`, `libpulse0`, `gpgv`, `polkitd`, `pkexec`.
   219	  **Recommends:** `polkit-kde-agent-1 | fly-…-agent`, `pipewire-pulse | pulseaudio`, `xdotool` (P2 fallback), `wireplumber`.
   220	  Всё — из `repository-main` Astra 1.8 (проверено `apt-cache show` на машине; `python3-pyqt5.qtquick` 5.15.9+dfsg-1+b9 доступен,
   221	  но не установлен — тянется автоматически).
   222	- **Собственные ELF/`.so` в пакете: 3** (все — onnxruntime) — фиксируется тестом `tests/unit/test_deb_elf_count.py`
   223	  (`dpkg-deb -c` + `file`), это же число идёт в `INSTALL-ADMIN.md` и SBOM (допущение A11 закрывается «3»).
   224	
   225	### 7.3 Сборка и воспроизводимость
   226	- Одна команда: `make deb` → `packaging/build-deb.sh`: (1) `pip download --require-hashes -r packaging/wheels.lock` из
   227	  локального кэша `packaging/wheels/` (сеть только на шаге `make wheels`, отдельно); (2) `scripts/gen_theme.py` + проверка
   228	  чистоты; (3) `pylupdate5`/`lrelease`; (4) `dpkg-buildpackage -us -uc -b` с `SOURCE_DATE_EPOCH` из `debian/changelog`,
   229	  `override_dh_strip` не трогает вендор (`libonnxruntime.so` остаётся байт-в-байт как в колесе — совпадает с sha256 SBOM);
   230	  (5) `lintian`, тест установки в чистом контейнере (`apt-get install ./x.deb && astra-voice --version && python3 -c
   231	  'import astra_voice, onnxruntime'`). Цель Ц7: ≤ 5 мин на эталоне.
   232	- Где собирать: контейнер `debian:12` (`Containerfile`) — та же glibc 2.36 / Python 3.11.2 / PyQt5 5.15.9, что в ALSE 1.8.5
   233	  (`/etc/debian_version` = 12.0); локальная сборка на ALSE — те же скрипты.
   234	- SBOM: CycloneDX JSON (`scripts/sbom.py` из `wheels.lock` + `dpkg-query` по Depends; `syft` в apt нет).
   235	- Релиз (`scripts/release.sh`, GitHub Actions on tag `vX.Y.Z`): deb → `SHA256SUMS` → `gpg --detach-sign --armor` (ключ CI,
   236	  Ed25519, публичный — в `data/keys/release.gpg` и в README) → Release: `deb`, `SHA256SUMS`, `SHA256SUMS.asc`,
   237	  `sbom.cdx.json`, `latest.json` (для корпоративного `updates_url`), `INSTALL-ADMIN.md`; changelog-writer → тело релиза.
   238	  Зеркало весов — отдельный релиз `models-YYYY.MM` (ассеты ≤ 2 ГБ), пути в `sources[].type=github_release`.
   239	
   240	### 7.4 CI (GitHub Actions, контейнер `debian:12`)
   241	| Job | Что | Инструменты (apt Debian 12 = apt ALSE) |
   242	|---|---|---|
   243	| lint/types | `ruff`, `mypy --strict` по `src/`, `qmllint` по `qml/` | ruff (pip в CI), python3-mypy 1.0.1, qtdeclarative5-dev-tools |
   244	| unit | `pytest -m unit` (без дисплея) | python3-pytest 7.2 |
   245	| qml/xvfb | `pytest -m xvfb`: каждый QML создаётся без warnings, скриншоты состояний, XGrabKey/XTest под `Xvfb`, single-instance | xvfb, `QT_QPA_PLATFORM=xcb` |
   246	| engine | `pytest -m engine`: `onnx-asr` + ORT на кэшированной модели (226 МБ, кэш по ревизии) — смоук 3 раскладок | кэш GitHub Actions |
   247	| deb | `make deb` → lintian → установка в чистом контейнере → `--version` → ELF-count = 3 | dpkg-dev, debhelper, dh-python |
   248	| release (tag) | deb + SHA256SUMS + подпись + SBOM → GitHub Release | gpg |
   249	
   250	**Нельзя проверить в CI → P21½ на машине заказчика (и Fly-сессия):** захват хоткея на KWin/fly-wm и конфликты KDE;

exec
/bin/bash -lc "nl -ba arch/plan-codex.md | sed -n '1,205p'" in /home/astra/Документы/astra-voice
 succeeded in 0ms:
     1	# Astra Voice — архитектурный план #2 (Codex)
     2	
     3	> **Движок:** OpenAI Codex CLI 0.153.4, модель `gpt-6-astra`, `model_reasoning_effort=xhigh`, sandbox `read-only`.
     4	> **Дата:** 2026-09-09. **Запусков:** 1 (полный бриф одной командой, без деления; ~15 мин, 391 442 токена, exit 0).
     5	> **Команда:** `codex exec -m "gpt-6-astra" -c model_reasoning_effort="xhigh" -s read-only "<бриф>" </dev/null` из `/home/astra/Документы/astra-voice`.
     6	> **Ответ полный** (4 секции: компоненты · упаковка · вехи · риски/тесты/инварианты), обрывов не было. Файлы Codex не менял.
     7	> Ниже — сырой финальный вывод Codex без правок.
     8	
     9	---
    10	
    11	Предлагаю **onnx-asr как единственный рантайм v1**, отдельный процесс распознавания, PyQt5/QML для интерфейса и GPG/gpgv для подписей. Рамки сохраняются. Файлы не изменены; приведённые ниже команды — планируемые проверки, а не результаты выполненных тестов.
    12	
    13	При расхождениях применяю приоритет: последние решения → PRD 0.3 → принятая дизайн-спека и tokens.json → исследования.
    14	
    15	| Расхождение в документах | Как учитывать в реализации |
    16	|---|---|
    17	| В `design/spec.md` осталась пометка «черновик» | G3 уже принят в журнале решений |
    18	| S9-A5 и некоторые строки дизайна запрещают ручную проверку при выключенных автопроверках | Ручная проверка разрешена; блокируют только offline/policy |
    19	| PRD требует смены модели с работающей старой и суммарно ≤600 МБ | В обычном режиме — ≤600 МБ; временный пик двух моделей измерять отдельно. Если 600 МБ — абсолютный предел, требования конфликтуют; это зафиксировать до реализации переключения |
    20	| PRD F3 связывает пилюлю с темой; принятая спека задаёт фиксированную палитру | Окно настроек меняет тему; пилюля использует `color.fixed.*` |
    21	| В §9 tokens.md остались «easing не задан» и примеры Qt 6 | Брать кривые из актуального tokens.json; реализовывать средствами Qt 5.15 |
    22	| В исследованиях фигурируют 222 МБ и 415 МБ | Для выбранного экспорта — 226,4 МБ по каталогу; 415 МБ — исторический замер Handy, не измерение нового воркера |
    23	
    24	1. Архитектура строится вокруг независимых состояний записи, распознавания, установки моделей и обновления приложения.
    25	
    26	| Компонент | Ответственность | Зависит от | Как тестировать |
    27	|---|---|---|---|
    28	| **GUI-процесс** | `QApplication` + `QQmlApplicationEngine`, трей, окна, контроллеры. Не импортирует ORT. Тяжёлые операции вне GUI-потока | Системный PyQt5 | Готовность трея ≤1,5 с; отзывчивость во время загрузки/инференса; RSS без модели ≤120 МБ |
    29	| **DictationController** | Сессия с `utterance_id`, окном назначения и поколением воркера. Не более одного распознавания и одной дополнительной записи; вставки строго по порядку | Хоткей, аудио, WorkerSupervisor, PasteService | Перестановка событий, двойное отпускание, очередь заполнена, отмена перед результатом, поздний ответ |
    30	| **WorkerSupervisor** | Запускает новый Python через `exec`, без `fork` состояния Qt. Один постоянный воркер; временный кандидат при смене модели. Крах/зависание не роняет GUI; максимум 3 автоперезапуска за 10 мин | `subprocess`, Unix IPC | SIGSEGV/SIGKILL, EOF, зависание, несовместимая версия протокола; четвёртый сбой требует действия пользователя |
    31	| **IPC** | Наследуемый `AF_UNIX socketpair`; сообщения JSON с длиной, версией протокола и ID. PCM передаётся через FD `memfd`, после записи — запечатанный от изменения. Без pickle и localhost TCP | `SCM_RIGHTS`, `QSocketNotifier` | Частичные сообщения, неверная длина, неизвестная команда, лишние FD, утечки дескрипторов |
    32	| **ASR-воркер** | `load / recognize / cancel / shutdown`; модель постоянно в памяти. Только CPUExecutionProvider. Потоки по умолчанию `min(4, physical_cores)`, inter-op=1; отключён spinning в простое | onnx-asr, ORT, системный NumPy | Все 12 пресетов; холодный запуск; ≥50 тёплых прогонов дефолта; idle CPU ≤1% |
    33	| **Отмена ASR** | Отдельный управляющий поток принимает cancel; проверка между сегментами/шагами декодера и `RunOptions.terminate` для выполняемого ORT-вызова. Минимальный сопровождаемый патч onnx-asr, если API не проводит RunOptions | ASR-адаптер | S14: готовность к новой записи ≤1 с, без выгрузки модели. Убийство и загрузка за 5 с не считаются прохождением этого теста |
    34	| **Глобальный хоткей X11** | `python3-xlib`, `XGrabKey`, Press/Release; Ctrl+Space PTT, toggle вторым назначением. Caps/NumLock, смена раскладки, autorepeat, отпускание модификаторов, восстановление X-соединения | X11/XKB | ru/en, удержание, повтор клавиши, отпускание Ctrl раньше Space, смена раскладки, suspend/resume |
    35	| **Редактор хоткея** | Читает `kglobalshortcutsrc`, показывает действие KDE. Конфликт — предупреждение; `BadAccess` при реальном grab — ошибка. Временный захват клавиатуры только во время настройки, освобождение по Esc/потере окна | Хоткей, QML | Alt+F2, дубликат PTT/toggle, отказ grab; системные сочетания не переназначаются автоматически |
    36	| **AudioCapture** | `QAudioInput` в отдельном QThread, PulseAudio backend поверх PipeWire. Открытие при хоткее/явном тесте; PCM mono 16 kHz, либо согласованный формат с ресэмплингом в воркере | QtMultimedia, аудиосервер | Виртуальный источник; отключение USB; неподдержанный формат; переполнение; быстрый отпуск до открытия |
    37	| **VAD и сегментация** | Silero VAD ONNX с MIT, отдельный от исключённого Silero STT. До 20 с — один фрагмент; длиннее — границы по паузам, каждый вход GigaAM <24 с. Непрерывная речь — перекрытие и сборка стыков с учётом временных меток | ORT в воркере | Тишина, шум, слабый голос; 19,9/20,1/23,9/24,1 с; речь без пауз; повторяющиеся слова на стыке |
    38	| **Ограничения записи** | <0,3 с — молчаливое игнорирование; 120 с по умолчанию, настройка 30–300 с. Лимит останавливает захват и запускает обработку сразу; двухсекундный текст пилюли не задерживает ASR | AudioCapture, Controller | Границы длительности, отмена, монотонные часы; максимум PCM16 — 9,6 МБ на 300 с |
    39	| **MicRecovery** | Сырой пик до VAD: <−60 dBFS за 2 с → «Микрофон молчит». Обычный retry 3×300 мс; после входа — до 3 повторов через 1 с. Перезапуск WirePlumber только кнопкой пользователя | `systemctl --user`, QProcess | Нулевая амплитуда, mute, greeter-гонка, отсутствующая служба. После restart — повторное открытие и проверка ≤3 с |
    40	| **PasteService** | Запоминает `_NET_ACTIVE_WINDOW` до пилюли. Только текстовый CLIPBOARD → 50 мс → XTest-комбинация → восстановление через 100 мс. Восстанавливает лишь если буфер всё ещё принадлежит нашей операции | QClipboard, X11/XTest | Kate, Firefox, LibreOffice; пользователь скопировал другое во время вставки; медленный получатель; окно закрылось |
    41	| **Терминалы и Klipper** | Правила по WM_CLASS: Konsole/Fly и другие — проверенные комбинации; xterm допускает отдельное правило Shift+Insert. Опция `x-kde-passwordManagerHint`. Последний текст только в RAM | PasteService | Отсутствие `^V`, отсутствие синтетического Enter; CLIPBOARD/PRIMARY; история Klipper действительно не содержит текст |
    42	| **PillWindow** | Отдельный QQuickWindow: Tool, Frameless, StaysOnTop, DoesNotAcceptFocus; skip-taskbar/pager. Без `activateWindow/requestActivate`. Кнопка × остаётся кликабельной | X11/KWin, QScreen, Theme | Активное окно не меняется при показе/скрытии/отмене; fullscreen, Alt+Tab, панели, несколько мониторов |
    43	| **Позиционирование пилюли** | Монитор исходного активного окна, иначе primary; центр по X, 48 px от края рабочей области. Пересчёт при изменении панели, DPR и конфигурации экранов | QScreen, EWMH struts | Панель >48 px, боковая панель, отрицательные координаты, масштаб 100/125/150%, отключение монитора |
    44	| **Трей и уведомления** | `QSystemTrayIcon` + QMenu, SNI через системную интеграцию Qt. Регистрация с retry до 30 с и повторно после перезапуска панели. Notifications D-Bus с actions; ошибки доступны также в окне | QtWidgets, QtDBus, оболочка | Нет watcher/notification service, перезапуск Plasma, Fly; шесть состояний, статичная иконка |
    45	| **SettingsWindow / onboarding** | Шесть разделов + скрытая отладка. Минимум 900×620; завершение onboarding требует модели. Окна и диалоги используют общие компоненты | ViewModel, SettingsStore | S1/S11–S13; Tab/стрелки/Esc, обе локали, обе темы, все состояния макета |
    46	| **ModelRegistry / Activator** | Подписанный каталог, локальные ревизии, фильтры, активная модель. Кандидат загружается и проходит smoke; старая обслуживает записи. Переключение на границе сессий; старый воркер завершается после своих заданий | Загрузчик, Supervisor | Сбой загрузки/смоука, нехватка RAM, диктовка во время переключения, запрещённое удаление активной |
    47	| **ModelDownloader** | Докачка, подпись каталога, sha256 каждого файла, атомарная установка набора, источники и лимиты — ниже | NetworkService, Registry | HTTP-стенд: 206/200/416, обрыв, 429, неправильный Range, диск заполнен, вредоносные пути |
    48	| **RevisionWatcher** | Проверяет установленные экспорты, ветки оригинала и обновление каталога; отдельно сигнал о близких моделях. Только уведомляет | HF API, подписанный каталог, cache | Фикстуры веток main/e2e_rnnt; SHA разных репозиториев; повторные уведомления; offline |
    49	| **AppUpdater / TrackResolver** | Releases API/корпоративный latest.json, SemVer, загрузка и проверка; выбор GUI/admin по policy, ЗПС и правам | NetworkService, gpgv, helper | S6/S9/S10; prerelease, подпись, неверный пакет, отсутствие агента, отказ авторизации |
    50	| **Root-helper** | Единственная привилегированная операция: проверить локальный комплект в root-staging и вызвать apt с фиксированными аргументами | polkit, gpgv, apt/dpkg | TOCTOU, symlink, подмена ключа/пути/версии, apt lock, сбой питания; отдельный security review |
    51	| **Autostart / single-instance** | Один XDG `.desktop`, `Exec=/usr/bin/astra-voice --hidden`, `TryExec`, `X-KDE-autostart-after=panel`, без OnlyShowIn. Создание только по изменению настройки | XDG, QLockFile/локальный IPC | Перелогин KDE/Fly; файл не переписывается при старте; второй запуск открывает первый; Handy не затрагивается |
    52	| **SettingsStore / PolicyStore** | Версионированный JSON, миграции, атомарная запись, `.bak` повреждённого файла. Эффективные значения вычисляются в Python; policy нельзя обойти через QML/CLI | `/etc/astra-voice/policy.conf`, XDG | Повреждённый JSON, новая схема, read-only home, policy-lock, сброс настроек |
    53	| **Theme / i18n** | kdeglobals до `engine.load`; watcher файла/каталога + D-Bus, реакция ≤2 с. ru/en `.ts/.qm`, плюрализация, форматирование QLocale | Генератор tokens.json | Нет светлой вспышки; атомарная замена kdeglobals; длинный английский текст; отсутствующий шрифт |
    54	| **Логи / метрики / диагностика** | 5×1 МБ логов; stats и measurements отдельно. Только коды, длины, времена. Диагностический ZIP по кнопке, с удалением домашних путей, секретов proxy/URL, без текста и аудио | procfs, SettingsStore | Поиск контрольного текста/секретов в файлах; ротация; очистка; закрытые права доступа |
    55	
    56	`RunOptions.terminate` предусмотрен ORT, но достижимость ≤1 с для всех декодеров должна быть доказана спайком, включая Whisper. [API ORT](https://onnxruntime.ai/docs/api/python/api_summary.html)
    57	
    58	Для пилюли нельзя считать `availableGeometry` достаточным доказательством корректности нескольких мониторов: у Qt/X11 есть ограничения определения рабочей области. Основной API сохраняется, адаптер проверяет панели через EWMH и нормализует координаты. [QScreen Qt 5.15](https://doc.qt.io/archives/qt-5.15/qscreen.html)
    59	
    60	| Контракт процессов | Решение |
    61	|---|---|
    62	| Команды | `Hello`, `LoadModel`, `Recognize`, `Cancel`, `Shutdown`; события `Ready`, `Result`, `Cancelled`, `Error`, `Metrics` |
    63	| Проверка совместимости | `protocol_version`, `app_build`, `runtime_version`, `model_id`, `revision`, `generation` |
    64	| Ограничения IPC | JSON ≤64 КиБ, проверка типа/длины PCM, длительность ≤300 с, ограниченная очередь; закрытие неизвестных FD |
    65	| Отмена | Отменённый ID никогда не вставляется, даже если поздний результат корректен |
    66	| Владение моделью | Каждая сессия закрепляет поколение воркера; смена активной модели не перенаправляет уже начатую запись |
    67	| Завершение | Остановить захват, отменить задания, восстановить принадлежащий операции буфер, закрыть FD и воркеры |
    68	| Изоляция | Отдельный процесс защищает GUI от сбоя ORT; сам по себе он не является песочницей против вредоносной ONNX-модели |
    69	
    70	Выбор рантайма — внутри разрешённой развилки.
    71	
    72	| Критерий | onnx-asr — выбор v1 | sherpa-onnx — альтернатива |
    73	|---|---|---|
    74	| GigaAM v3 e2e_rnnt | Поддержан; собственный пресет под istupakov | Поддержан; совпадает с экспортом Handy/csukuangfj |
    75	| Все 12 моделей | Есть нужные архитектуры: GigaAM, Multilingual, T-one, Vosk, NeMo, два формата Whisper | Для полного каталога остаются непроверенные адаптации T-one/Multilingual и другие экспорты |
    76	| Python/glibc | onnx-asr — pure Python; бинарная граница — ORT. Для ORT 1.23.2 есть cp311 manylinux_2_27/2_28 | Есть manylinux2014-колёса; проверять весь комплект, включая отдельные core-зависимости |
    77	| Собственные `.so` | onnx-asr добавляет 0; план ORT — 3, shim — ещё 1 | Python extension, sherpa core/C API и ORT; точный состав зависит от wheel. Преимущество по числу ELF заранее не доказано |
    78	| RAM/скорость | Проверять на целевом CPU; Python-декодер и preprocessing могут отличаться от Handy | Нативный декодер потенциально выгоднее; локальных сопоставимых замеров пока нет |
    79	| Раскладки | Совпадает с большей частью источников каталога, включая бенчмарченные Whisper | Потребуется изменить источники/размеры, переснять скорость и проверить лицензии экспортов |
    80	| Сопровождение | Узкий RuntimeAdapter; закреплённый wheel и небольшой набор патчей | Возможная замена адаптера и каталога, но не незаметный fallback на тех же файлах |
    81	
    82	Поддержка архитектур подтверждается кодом onnx-asr; наличие подходящего тега manylinux подтверждает доступность колеса, **не заменяет запуск на glibc 2.36**. [onnx-asr loader](https://raw.githubusercontent.com/istupakov/onnx-asr/v0.12.0/src/onnx_asr/loader.py), [ORT 1.23.2 на PyPI](https://pypi.org/project/onnxruntime/1.23.2/)
    83	
    84	| № | Запись каталога v1 | Репозиторий для onnx-asr | `layout` / обязательные особенности |
    85	|---|---|---|---|
    86	| 1 | GigaAM v3 e2e_rnnt int8 | `istupakov/gigaam-v3-onnx` | `onnx-asr-gigaam-v3`; `v3_e2e_rnnt_{encoder,decoder,joint}.int8.onnx`, vocab/config; 226,4 МБ |
    87	| 2 | GigaAM v3 e2e_ctc int8 | тот же | `onnx-asr-gigaam-v3`; `v3_e2e_ctc.int8.onnx`, соответствующий vocab/config |
    88	| 3 | GigaAM v3 rnnt int8 | тот же | `onnx-asr-gigaam-v3`; `v3_rnnt_*`, обычный словарь; без пунктуации |
    89	| 4 | GigaAM Multilingual 220M | `istupakov/gigaam-multilingual-ctc-onnx` | `onnx-asr-gigaam-multilingual`; 224,8 МБ |
    90	| 5 | T-one | `t-tech/T-one` | `onnx-asr-t-one`; fp32, 144,2 МБ; без kenlm |
    91	| 6 | Vosk ru 0.54 | `alphacep/vosk-model-ru` | `vosk-onnx`; `am-onnx/` + `lang/tokens.txt`; 72,5 МБ |
    92	| 7 | Vosk small ru 0.52 | `alphacep/vosk-model-small-ru` | `vosk-onnx`; `am/` + `lang/tokens.txt`; 26,7 МБ |
    93	| 8 | Whisper large-v3-turbo int8 | `onnx-community/whisper-large-v3-turbo` | `onnx-community-whisper`; encoder, merged decoder, tokenizer/config; 1 089,1 МБ |
    94	| 9 | Whisper small int8 | `onnx-community/whisper-small` | `onnx-community-whisper`; 253,5 МБ; без выдуманных WER/RTFx |
    95	| 10 | Whisper base int8 | `istupakov/whisper-base-onnx` | `onnx-asr-whisper-ort`; beamsearch-граф и tokenizer; 109,1 МБ |
    96	| 11 | GigaAM Multilingual Large | `istupakov/gigaam-multilingual-large-ctc-onnx` | `onnx-asr-gigaam-multilingual`; 591,6 МБ |
    97	| 12 | NeMo FastConformer ru pc CTC | `istupakov/stt_ru_fastconformer_hybrid_large_pc_onnx` | `onnx-asr-nemo`; CTC-граф, vocab/config; 131,6 МБ; CC-BY |
    98	
    99	Для №2–3 таблица чисел содержит размеры sherpa-экспортов. До выпуска каталога нужно дополнить источник истины точными байтами выбранных istupakov-наборов; переносить 224,9/229,3 МБ механически нельзя. Файлы этих вариантов присутствуют в закреплённом репозитории. [Экспорт GigaAM](https://huggingface.co/istupakov/gigaam-v3-onnx/tree/322c3b29492673eb7d0b434bfa9dfb8653e34d02)
   100	
   101	| Манифест и загрузка | Конкретный контракт |
   102	|---|---|
   103	| Идентичность | Все записи имеют `engine=onnx-asr`; `layout` — закрытый enum из таблицы. ID модели, вариант и precision не выводятся из имени файла |
   104	| Полный состав | `files[]` содержит все реально используемые ONNX, tokenizer, vocab/config и external-data. Вложенность репозитория, включая `onnx/`, сохраняется |
   105	| Ревизии | Полные commit SHA из [catalog-numbers.md](/home/astra/Документы/astra-voice/research/catalog-numbers.md); URL только `/resolve/{sha}/…` |
   106	| Цепочка доверия | Сначала подпись каталога закреплённым ключом, затем схема, ограничения путей и sha256 всех файлов. Git SHA-1 мелких файлов не принимается за sha256 |
   107	| Зеркала | HF → GitHub Releases → corporate при сетевой ошибке/404/429. Все зеркала обязаны отдавать один набор байтов |
   108	| Корпоративный профиль | Корпоративный каталог может содержать только corp-источники; публичного fallback в таком случае нет |
   109	| Докачка | `.part` + состояние `{revision, path, expected_size, sha256, validator}`. Продолжать только при корректном 206/Content-Range; 200 — начать заново; 416 — проверить полноту либо перезапустить |
   110	| Проверка | Размер и sha256 потоково; несовпадение — удалить повреждённую загрузку, показать ошибку. Не назначать фактическую сумму «ожидаемой» |
   111	| Атомарность | Staging на том же файловом разделе → `fsync` файлов/каталога → rename набора → smoke → атомарный `current.json` |
   112	| Откат | Старая ревизия остаётся до следующего успешного запуска новой. Незавершённая транзакция восстанавливается при старте |
   113	| Лимиты | Одно задание загрузки, до 2 файлов одновременно; каталог ≤1 МиБ, ≤64 файлов/модель, ≤2 ГиБ/файл; лимиты явно версионированы |
   114	| Диск | Перед началом минимум `remaining_bytes ×1,2`, с учётом staging и сохраняемой ревизии; ENOSPC обрабатывается и во время записи |
   115	| Кэш | Последний проверенный каталог и ответы API; bounded-кэш обновлений, очистка устаревших `.part`; установленные модели не удаляются как кэш |
   116	| Локальный импорт | Та же проверка. «Своя модель» — только поддержанный layout и явный локальный манифест; никаких скачанных Python-плагинов |
   117	| Защита путей | Запрет абсолютных путей, `..`, symlink/hardlink при импорте, выхода external-data за каталог; отсутствие распаковки произвольных архивов |
   118	| Метрики карточек | WER только по согласованному протоколу; `benchmark_fp32/no_data` сохраняются. Vosk — зарубежные; domestic-фильтр содержит 6 моделей |
   119	
   120	| Проверки сети и ревизий | Поведение |
   121	|---|---|
   122	| Общая точка выхода | NetworkService на QNetworkAccessManager; все проверки и загрузки проходят NetworkPolicy |
   123	| По умолчанию | Никаких запросов, в том числе «проверки доступности HF», до разрешённого действия |
   124	| Offline | `policy.offline`, пользовательский offline или `HF_HUB_OFFLINE=1` запрещают новые запросы и отменяют текущие |
   125	| Планировщик | App: jitter 0–10 мин; модели: 0–30 мин; после готовности GUI. Не чаще 24 ч; повторные запуски приложения не сбрасывают backoff |
   126	| Таймаут | Проверка — общий deadline 3 с, включая редиректы; ≤1 повтор внутри бюджета. Для загрузки отдельно connect/inactivity timeout |
   127	| Кэш/лимиты | ETag/304, `Retry-After`, HF RateLimit, GitHub reset; 403/429 не вызывают цикл повторов |
   128	| Экспорт модели | Сравнивать `(repo, branch, observed_sha)` только с предыдущим SHA того же repo/branch |
   129	| Оригинал Сбера | Отдельно отслеживать `ai-sage/GigaAM-v3:e2e_rnnt`. Его SHA не равен SHA `istupakov/gigaam-v3-onnx` |
   130	| Разрешение установки | Новая ветка оригинала — информационный сигнал. Кнопка установки появляется, когда подписанный каталог описал совместимый экспорт с новыми sha256 |
   131	| Близкие модели | Белый список авторов/семейств, ограниченная выдача; баннер без установки до включения в каталог |
   132	| TLS/proxy | Системные CA и `SSL_CERT_FILE`; HTTP(S)_PROXY/NO_PROXY; проверка TLS обязательна. Редиректы проверяются по разрешённым источникам/CDN |
   133	| Runtime | onnx-asr получает только локальный путь и offline resolver; `[hub]`, HF-токены и автоматическое скачивание из воркера отсутствуют |
   134	
   135	Обновлятор использует **GPG для выпуска, gpgv для проверки**. Ключ задаётся явным root-owned keyring; пользовательский GPG keyring не участвует. Отзыв и ротация ключей требуют собственной политики: gpgv доверяет всем ключам переданного keyring и сам не решает задачу отзыва. [Документация gpgv](https://www.gnupg.org/documentation/manuals/gnupg/gpgv.html)
   136	
   137	| Шаг обновления | Действие |
   138	|---|---|
   139	| 1. Обнаружение | Releases `latest` / corporate latest.json; нормализация одного `v`, SemVer; prerelease не предлагается стабильному каналу |
   140	| 2. Выбор трека | Читать `/etc/digsig/digsig_initramfs.conf` как данные, не shell. `DIGSIG_ELF_MODE≠0`, `zps=on`, `updates=admin` или отсутствие прав → B. Неизвестно → предусмотренный вопрос; «не знаю» → B |
   141	| 3. Загрузка | `.deb`, `SHA256SUMS`, `SHA256SUMS.sig`, инструкция. Текст Release отображается безопасно, без внешних картинок/активного HTML |
   142	| 4. Проверка GUI | Проверить подпись SUMS, затем хеш точного файла; имя, arch, версия. Повреждённый пакет удалить |
   143	| 5A. Авторизация | Один `pkexec /usr/libexec/astra-voice-update-helper …`; собственное action `auth_admin`, без `_keep`, без `rules.d`, без привязки к apt |
   144	| 6A. Root-проверка | Helper повторно читает policy/ЗПС; принимает только локальный комплект. Безопасно копирует в root-staging 0700, затем повторяет подпись/хеш |
   145	| 7A. Установка | Проверяет `Package=astra-voice`, `Architecture=amd64`, повышение версии через `dpkg --compare-versions`; фиксированный `apt-get install --no-remove ./…deb`, без shell и пользовательских аргументов apt |
   146	| 8A. Результат | Проверить установленную версию через dpkg-query; показать «Перезапустить». Перезапуск только кнопкой; текущую запись сначала завершить/отменить |
   147	| 5B. Администратор | Сохранить проверенный комплект, «Открыть папку»; pkexec/apt не запускать |
   148	| Из файла | Тот же verifier и тот же выбор трека; неподписанный deb через GUI не устанавливается |
   149	| Ошибки | Нет агента/отмена → сохранить deb; apt lock → объяснение и повтор; не убивать apt посреди транзакции |
   150	| Сеть apt | Репозитории ОС — отдельная системная стадия, раскрытая в PRIVACY. В offline — `--no-download`; недостающие зависимости передаются администратору |
   151	| После установки | Не смешивать новый код воркера со старым GUI: проверять build/protocol; операции, требующие нового процесса, ждут перезапуска |
   152	
   153	Кастомные компоненты соответствуют [макету](/home/astra/Документы/astra-voice/design/mockups/final/index.html), без переноса HTML-стилей буквально в Qt.
   154	
   155	| Компоненты | Реализация Qt 5.15 |
   156	|---|---|
   157	| `PillWindow`, `LevelBars` | QQuickWindow, Repeater из 9 Rectangle; обновление уровня около 30 Гц только во время записи; 36 px, ширина 172–320, без elide |
   158	| `ModelCard`, `MetricBar`, `Badge` | Явные состояния ViewModel; 20 состояний карточки; максимум два бейджа; Popup «Как мы считаем» |
   159	| `HotkeyCapture`, `KeyChip` | Нативный X11 backend + QML-представление семи состояний; PT Mono |
   160	| `SegmentedControl`, `PolicySwitch`, `SettingRow` | Controls 2; переключатель — один Tab-stop; строка toggle кликабельна целиком; policy-lock объясняется текстом |
   161	| `StatusBar`, `UpdatePanel`, `OnboardingShell` | Общие контроллеры состояний; 17/14 состояний обновления; переиспользование карточек и микрофонного теста |
   162	| `Theme.qml`, `PillTheme.qml` | Генерация из tokens.json с проверкой типов, единиц, alpha, дробных размеров и Font.Weight; ни одного ручного дубля токенов |
   163	| Иконки и тени | 21 собственный SVG, включая clock. Перекраска через QSvgRenderer/image provider или QtGraphicalEffects; не Qt 6 MultiEffect |
   164	| Шрифты | Выбрать установленный шрифт через QFontDatabase по порядку fallback; строка CSS family-list не считается рабочим fallback Qt 5 |
   165	| Заголовок/меню/уведомления | Заголовок KWin, трей QMenu, уведомления оболочки. Размер внешней рамки измеряется отдельно от QML client area |
   166	| Software rendering | Отдельный прогон; при неподдержанных эффектах — кэшированные растровые тени, без смены принятой геометрии |
   167	
   168	2. Поставка — один `astra-voice_<version>_amd64.deb`; системный GUI и NumPy берутся из apt.
   169	
   170	| Слой | Состав |
   171	|---|---|
   172	| GUI | `python3`, `python3-pyqt5`, `.qtquick`, `.qtsvg`, `.qtmultimedia`; `qml-module-qtquick2`, `-window2`, `-layouts`, `-controls2`, `qml-module-qtgraphicaleffects` |
   173	| Интеграция | `python3-xlib`, Qt multimedia plugins, `libpulse0`, `pulseaudio-utils`, `xdotool` для аварийного режима; системные polkit/pkexec и gpgv |
   174	| Python из apt | `python3-numpy`, `python3-packaging`, `python3-jsonschema`; зависимости ORT: protobuf, flatbuffers, sympy, humanfriendly и их зависимости |
   175	| Вендоренные wheel-кандидаты | `onnx_asr-0.12.0-py3-none-any.whl`; `onnxruntime-1.23.2-cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl`; `coloredlogs-15.0.1-py2.py3-none-any.whl`, поскольку пакет не обнаружен в локальном apt-index |
   176	| Фиксация | Это начальный набор спайка. До v0.1 — точные версии, SHA256, проверка METADATA против apt-версий и проверка известных уязвимостей; после этого никакого `latest` |
   177	| Не поставлять | pip-Qt, pip-NumPy, torch, transformers, GPU runtime, huggingface_hub/hf_xet, sherpa одновременно с onnx-asr |
   178	| Ресурсы | Каталог + подпись, ключ, переводы, SVG, два OGG, лицензированный тестовый WAV; небольшой VAD ONNX как отдельный технический ресурс с хешем/NOTICE |
   179	| ASR-веса | В deb не входят; скачивание/локальный импорт отдельно |
   180	| Python isolation | Root-owned private vendor directory; запуск `python3 -I` через bootstrap. Не изменять системный site-packages и не использовать пользовательские `.pth` |
   181	| Maintainer scripts | Без сети, pip, GUI, изменения домашнего каталога; purge не удаляет данные пользователей |
   182	| Размер/время | Проверить ≤80 МБ без ASR-весов и сборку ≤5 мин; холодную подготовку среды и повторную сборку показывать отдельно |
   183	
   184	Минимальные зависимости onnx-asr допускают системный NumPy; hub является отдельной опцией. [pyproject v0.12.0](https://raw.githubusercontent.com/istupakov/onnx-asr/v0.12.0/pyproject.toml)
   185	
   186	| Объекты будущей ГОСТ-подписи | Плановое число |
   187	|---|---:|
   188	| onnx-asr и coloredlogs | 0 ELF |
   189	| ORT: `onnxruntime_pybind11_state*.so`, `libonnxruntime.so.*`, `libonnxruntime_providers_shared.so` | Ожидается 3 |
   190	| `libastra-isoc23-compat.so` | 1 |
   191	| Python root-helper, GUI-код | 0 ELF |
   192	| **Бюджет собственного комплекта** | **4 `.so`; 3, если shim не требуется** |
   193	
   194	Это **плановый инвентарь, не результат распаковки wheel**. CI считает все уникальные ELF по magic, включая `.so.*` и транзитивные библиотеки; лишний ELF останавливает сборку до обновления SBOM. Системные apt-библиотеки учитываются отдельно.
   195	
   196	Шим загружается только в ASR-процессе, до ORT, из root-owned пути. Не использовать `/etc/ld.so.preload`, не передавать LD_PRELOAD в pkexec. Проверять реальные необходимые символы и семантику C23; обёртки `strto*` не являются общей заменой glibc 2.38. [Факт из Handy](/home/astra/Документы/handy-gigaam/docs/TROUBLESHOOTING.md:5)
   197	
   198	| Структура репозитория | Содержание |
   199	|---|---|
   200	| `src/astra_voice/app/` | Контроллеры, bootstrap, lifecycle |
   201	| `src/astra_voice/platform/` | X11, clipboard, audio, tray, theme, autostart |
   202	| `src/astra_voice/models/` | Схема, пресеты, registry, downloader, revisions |
   203	| `src/astra_voice/worker/` | IPC, RuntimeAdapter, VAD, segmentation, cancel |
   204	| `src/astra_voice/updates/` | Release client, verifier, track resolver |
   205	| `src/astra_voice/ui/` | QML screens/components, ViewModels, i18n |

codex
Уже вижу разрывы на стыках: синтез добавляет отмену и временный воркер, но оставляет базовые IPC-контракты почти без изменений. Проверю, какие пробелы действительно не закрыты в исходных планах, особенно для восстановления после сбоев.

exec
/bin/bash -lc "nl -ba arch/plan-codex.md | sed -n '255,300p'; nl -ba decisions/log.md | sed -n '1,216p'" in /home/astra/Документы/astra-voice
 succeeded in 0ms:
   255	| Виртуальный микрофон | Harness создаёт `module-null-sink` и `module-remap-source`, явно выбирает источник; ждёт `CaptureReady`, затем запускает `paplay --device=av_test …wav`; удаляет только созданные модули |
   256	| Настоящая вставка | E2E-приёмник подтверждает изменение текста; событие отправки Ctrl+V само по себе не доказывает успешную вставку |
   257	
   258	4. Риски, альтернативы и границы безопасности должны быть проверяемыми условиями приёмки.
   259	
   260	| Риск | Основное решение / альтернатива | Обратимость |
   261	|---|---|---|
   262	| onnx-asr не достигает скорости/RAM | Сначала профилировать preprocessing, декодер, ORT threads/arenas. Затем сравнить sherpa на том же аудио; менять runtime только вместе с пресетами и замерами | RuntimeAdapter и IPC сохраняются; модели потребуется переустановить в другом layout |
   263	| Полный каталог не проходит выбранный ORT | Зафиксировать исправленный совместимый wheel; не добавлять скрыто второй runtime | Замена wheel-lock и повтор всех 12 smoke |
   264	| C23 shim скрывает более широкий ABI-разрыв | Аудит символов; совместимый wheel либо собственная воспроизводимая сборка ORT под базовую glibc | Локальная замена backend-комплекта |
   265	| Cancel не укладывается в 1 с | RunOptions + точки отмены; проверить каждый декодер. Kill/reload — только аварийное восстановление | Патч ограничен adapter/vendor; критерий не ослабляется незаметно |
   266	| Две модели вызывают OOM | Проверка MemAvailable и пика кандидата; при нехватке сохранить старую и отказать в переключении с объяснением | Данные/активная ревизия не меняются |
   267	| Пилюля теряет верхний слой | Повторное применение EWMH hints по событиям; при необходимости X11 bypass-вариант после проверки ×/focus | Один platform adapter |
   268	| QtQuick software backend не рисует эффекты | Кэшированные тени/иконки; анимации только видимых элементов | Не меняет контроллеры и токены |
   269	| WirePlumber restart не помогает | Отличать mute/нет устройства/профиль/ошибку открытия. Предложить диагностику; не менять fly-dm, linger и системные службы автоматически | Только пользовательская служба по кнопке |
   270	| Clipboard/Klipper гонки | Проверка владельца и поколения; настраиваемая задержка; «оставлять текст»/«только буфер» | Настройка, без смены ASR |
   271	| HF SHA постоянно даёт ложное обновление | Раздельные координаты upstream/export; установка только подписанного набора | Миграция update-cache |
   272	| Подмена deb между GUI и helper | Проверка после копирования в root-staging; helper не доверяет флагу «GUI проверил» | Изолированный updater |
   273	| Обрыв apt | Сохранять журнал и системный результат; не обещать транзакционный rollback deb и не запускать автоматический downgrade | Восстановление штатным apt администратором |
   274	| Отозванный/скомпрометированный ключ | Явные fingerprints, версия доверенного набора ключей; ротация через доверенный выпуск/администратора | Отдельный keyring; URL не назначает доверие |
   275	| RAM-only ошибочно трактуется как защита от swap | Нет аудиофайлов; core dumps отключены, audio mappings исключены из dumps. Строгая защита от swap/hibernation зависит от политики ОС | Не требует вмешательства приложения в настройки ОС |
   276	| «Отечественные модели» принимают за сертификацию | Фильтр по принятому юрлицу; NOTICE и документ влияния на среду. Допуск в систему — процедура администратора | Состав каталога управляется подписанным манифестом |
   277	
   278	| Уровень тестов | Обязательное покрытие |
   279	|---|---|
   280	| **Unit** | Автоматы состояний, очередь/cancel, миграции и policy, SemVer/Debian version, вычисление метрик, схема каталога, пути, подписи, Range/cache/backoff |
   281	| **Integration** | Реальный worker и ORT; виртуальный PipeWire-микрофон + paplay; HTTP fault server; D-Bus stubs; Xvfb clipboard; crash и заполнение диска |
   282	| **Packaging/security** | Чистый install/upgrade/remove/purge; полный ELF-инвентарь; отсутствие сети в postinst; TOCTOU/symlink/неверный ключ/чужой пакет; T2 на изменениях updater |
   283	| **E2E на ALSE** | KDE и Fly; реальные хоткеи/фокус/трей/polkit/Klipper; Kate/Firefox/LibreOffice/Konsole/fly-term/xterm; перезагрузка, USB, сеть через proxy/CA |
   284	| **Performance/soak** | GUI/RSS/PSS/RTFx; 50 диктовок; 8 часов работы; циклы отмены и смены модели; отсутствие роста FD/памяти |
   285	| **v1.1 отдельно** | Установка и диктовка с настоящей ЗПС и подписанным комплектом; имитация `DIGSIG_ELF_MODE` в v1 проверяет только маршрутизацию UI |
   286	
   287	| Инвариант | Где обеспечивается |
   288	|---|---|
   289	| Аудио и распознанный текст не отправляются в сеть и не попадают в логи/диагностику | NetworkService не принимает эти типы данных; тесты с контрольными маркерами |
   290	| Запись начинается только по действию пользователя и имеет видимый индикатор | Controller; при потере всех видимых индикаторов запись останавливается |
   291	| Worker не скачивает модели, не выполняет repo-код и не регистрирует произвольные custom-op `.so` | RuntimeAdapter, локальный resolver, закрытые layouts |
   292	| Неподписанный каталог и непроверенные файлы не становятся активными | Registry/Activator, атомарные транзакции |
   293	| Policy применяется к операциям, а не только к состоянию кнопок | Общий EffectivePolicy для GUI, CLI и updater |
   294	| Root-helper не принимает URL, shell-команду, произвольный keyring или параметры apt | Узкий протокол helper; root-owned конфигурация |
   295	| ЗПС и admin-track нельзя обойти через «Обновить из файла» | Повторная проверка трека в GUI и helper |
   296	| Отмена/крах/смена поколения не приводят к поздней вставке | ID сессии и поколения, единая очередь PasteService |
   297	| Вставка не синтезирует Enter и не начинается без проверенного окна назначения | PasteService; при невозможности назначения — только буфер |
   298	| Установленная рабочая ревизия не удаляется до подтверждения новой | ModelRegistry и журнал транзакции |
   299	| Системные СЗИ, fly-dm, группы пользователя и чужой автозапуск не меняются | Ограниченная область записи пакета/helper |
   300	| В v1 нет обещания готовности неподписанных ELF к ЗПС | INSTALL-ADMIN и трек B; ГОСТ-подпись и фактическая приёмка — v1.1 |
     1	# Журнал решений
     2	
     3	> Что решили и **почему**. Новые записи — сверху. Пишет Юрка после явного «да» человека
     4	> (скилл `decision-log`).
     5	
     6	## 2026-09-08 — G3: полный макет A + дизайн-спека 0.2 + токены 2.1 приняты
     7	- Контекст: UXAnalyst развернул A «Панель» в 48 экранов (~190 состояний), DesignEngineer снял 596 токенов
     8	  и черновик спеки, Юрка сверил размеры с рендером (окно 900×620, сайдбар 184, строка 38/55, пилюля 36,
     9	  min 172) и закрыл 14 «уточнить». Чеклист приёмки G3 — 11/11.
    10	- Решение: **макет `design/mockups/final/`, спека `design/spec.md` (0.2), токены `design/tokens.json` (2.1.1),
    11	  референсы `design/refs/` — источник истины для разработки и DesignReviewer.** Правки дальше — через
    12	  журнал решений и ревизию спеки.
    13	- Почему: «да» заказчика (чат 2026-09-08); контрасты текста ≥ 4,5:1, состояния не только цветом.
    14	- Гейт: G3 · «да» заказчика.
    15	- Последствия: Фаза 3 — двойной архитектор (architect-claude Fable ∥ architect-codex GPT-6 Astra) → суд
    16	  и синтез → челлендж Astra → T1 модель угроз (обновлятор, загрузка моделей, polkit) → execution-pack.
    17	  Вне спеки до кода: глиф `clock` (UX, в работе), два звука .ogg, `Theme.qml` генерировать из tokens.json.
    18	
    19	## 2026-09-08 — Решения Юрки по «⚠ уточнить» спеки (У1–У14, рутинные)
    20	- У1 кольцо фокуса: светлая тема — `accent-ink #0A776A` (5,16:1), тёмная — `#2FD9C4`; чистая бирюза в
    21	  светлой не видна (2,2:1). У2 pressed в тёмной — ступень 7 %, чтобы подпись держала ≥ 4,5:1.
    22	- У4 ширина: колонка контента растёт до 760 px, дальше центрируется. У5 строка с тумблером кликабельна
    23	  целиком (как в KDE); строки со списком/кнопкой — только контрол.
    24	- У13 клавиатура: Tab — сайдбар → строки контента сверху вниз → строка-статус; стрелки в сайдбаре;
    25	  Ctrl+PgUp/PgDn — разделы; Esc закрывает диалог/поповер; Space/Enter активируют контрол.
    26	- У3 прокрутка — overlay-скроллбар 8 px, AsNeeded. У6 кнопка `loading` — спиннер вместо иконки, текст
    27	  остаётся, disabled. У7 список — вниз, максимум 6 пунктов (228 px), при нехватке места — вверх.
    28	- У8 глиф `clock` добавить в набор (UX). У9 `loading-model` — вращающийся спиннер, 1 оборот/с, линейно.
    29	- У10 пилюля — по центру снизу **рабочей области** (availableGeometry: без панели KDE) монитора активного
    30	  окна, 48 px от её края (сверху — симметрично); без активного окна — основной монитор. (Уточнение
    31	  2026-09-08 после замечания design-engineer: панель выше 48 px перекрывалась бы пилюлей.) У11 онбординг пропускаемый по экранам, но завершение требует
    32	  установленной модели (S1). У12 диалоги модальны к окну настроек и центрируются над ним; уведомления —
    33	  штатные KDE. У14 звуки — два коротких тона (старт: восходящие, конец: нисходящие, ≤ 120 мс, −18 dBFS),
    34	  по умолчанию выключены; файлы .ogg в комплекте.
    35	- Гейт: — (рутинные); войдут в спеку на G3.
    36	
    37	## 2026-09-08 — Решения Юрки по цифрам каталога (research/catalog-numbers.md)
    38	- «Отечественная» модель определяется **по юрлицу правообладателя** (контекст Указа № 166/КИИ):
    39	  Vosk (Alpha Cephei Inc., Делавэр, США; команда — РФ) → **зарубежная**, в фильтр «только отечественные»
    40	  не попадает; подпись карточки: «зарубежная · команда из России».
    41	- Размеры в каталоге — **десятичные МБ по байтам HF API** (PRD §7.2 и макет правятся: 9 из 12 позиций).
    42	- WER/RTFx — только по протоколу §7.1 (onnx-asr, Russian LibriSpeech, x64 int8); оговорка в UI-подсказке:
    43	  «WER бенчмарка измерен на fp32-весах». Позиции без int8-бенчмарка (T-one fp32, GigaAM Multilingual)
    44	  — полоска «по бенчмарку fp32»; **Whisper small — «нет данных по протоколу»** (FLEURS 11,4 % — другой
    45	  протокол, только в подсказке) до собственного замера в v0.2 (US-5.4).
    46	- Whisper лицензии — по моделям: large-v3-turbo MIT, small и base Apache-2.0 (NOTICE — раздельно).
    47	- T-one остаётся Should (fp32 144 МБ, RTFx 26,3; ОЗУ — «оценка», правило ×1,85 неприменимо).
    48	- Рантайм v1 (onnx-asr vs sherpa-onnx) и раскладка файлов манифеста — решение архитекторов; пример
    49	  манифеста в PRD §7.3 должен быть согласован с выбранной раскладкой (поле `engine` + `layout` на модель).
    50	- Гейт: — (рутинные). Последствия: PRD ревизия 3 (§7.2/§7.3/NOTICE), макет каталога — правка цифр.
    51	
    52	## 2026-09-08 — Решения Юрки по токенам (вопросы design-engineer В1–В7, конфликты К1–К12)
    53	- Hover/pressed/focus: задать системно — hover = поверхность на ступень темнее/светлее (surface-2),
    54	  pressed = ещё ступень; primary hover/pressed = затемнение 8 % / 14 %; focus — кольцо 2 px accent
    55	  снаружи; пункт сайдбара и карточка — тот же принцип. Значения фиксирует design-engineer в токенах.
    56	- Easing: вход — ease-out (cubic 0.2/0/0/1) 160 мс, выход — ease-in 120 мс, hover 120 мс, уровень звука
    57	  в пилюле — линейно; «Готово» 500 мс. Пульсация: **пилюля «распознаю» пульсирует** (PRD F3.2), **трей
    58	  статичен** (brand-basics §2.4) — оба верны, конфликт К4 снят разделением.
    59	- Контрасты Р1–Р3, Р5 — исправить сдвигом hex/заменой opacity на цвет, композицию не трогать;
    60	  Р4 (трей на светлой панели) — принять: смысл несут форма и подсказка (PRD §8), цвет — подсказка.
    61	- К11 — строка настройки 38 px (как в финальном макете `_shell.py`), базовая 44 px устарела.
    62	- К2 — расширенная шкала кеглей принимается (документировать как шкалу v1). К3 — fg-disabled в тёмной
    63	  теме должен отличаться от fg-muted. К10/В7 — **иконки свои** (20 контуров, currentColor, в комплекте
    64	  приложения), Breeze не зависимость. В5 — кнопка danger в v1 не нужна (деструктив — primary в диалоге
    65	  с текстом-предупреждением). В3 — минимум окна 900×620.
    66	- Гейт: — (рутинные, без «да»); всё это войдёт в спеку на G3.
    67	
    68	## 2026-09-08 — Решения Юрки по вопросам полного макета (рутинные, без гейта)
    69	- Vosk (Alpha Cephei): происхождение «уточняется», в фильтр «только отечественные» не попадает, пока
    70	  Researcher не подтвердит (задача на спайк каталога).
    71	- «Проверить обновления» в трей-меню всегда активно: ручная проверка — явное действие пользователя
    72	  (как «Проверить сейчас» в разделе «Сеть»); неактивно только в офлайн-режиме/по policy. PRD F12 поправлен.
    73	- Тумблер VAD в «Продвинутых» — вне v1 (VAD автоматический, без настройки).
    74	- Минимальный размер окна настроек — 900×620 (minimumWidth/Height), ниже карточка модели ломается.
    75	- Проверка макета Юркой: 10 экранов отрисованы; два дефекта вёрстки (кнопки диалога NOTICE, перенос
    76	  заголовка статистики) — раунд правок 1 из 2.
    77	
    78	## 2026-09-08 — G2: направление интерфейса — A «Панель»
    79	- Контекст: UXAnalyst сдал `design/flows.md` (S1–S16, все состояния, 19 сообщений об ошибках) и три
    80	  направления × 10 экранов (`design/mockups/directions/`): A «Панель» (сайдбар), B «Лента» (одна колонка),
    81	  C «Пульт» (поповер от трея + второй уровень). Юрка проверил рендер 13 ключевых экранов.
    82	- Решение: **A «Панель»** — окно 900×620, левый сайдбар из 6 разделов (Общие · Модели · Вывод ·
    83	  Сеть и обновления · Продвинутые · О программе) + «Отладка» внизу, строки «заголовок · ? · контрол»,
    84	  строка-статус внизу «модель слева · обновление справа · версия». Плюс: **пилюля резиновая под текст**
    85	  (не фиксированные 172 px), **каталог показывается целиком с прокруткой** (фильтры «язык» и «только
    86	  отечественные»), **подтверждение «Готово» в пилюле — 500 мс** (PRD F3.2: было 300 мс — правка).
    87	- Почему: выбор заказчика (чат 2026-09-08); рекомендация UX — канон категории, дешевле всего, предсказуемо для админа.
    88	- Гейт: G2 · «да» заказчика.
    89	- Последствия: разворот A в полный макет (все экраны и состояния, светлая+тёмная) → токены
    90	  (design-engineer) → дизайн-спец (Юрка) → ⛔ G3 одной приёмкой.
    91	
    92	## 2026-09-07 — G1: PRD 0.2 + истории + беклог приняты
    93	- Контекст: ProductManager сдал PRD 0.2 (ревизия 2): 16 модулей F1–F16, 16 сценариев S1–S16 (71 GWT),
    94	  каталог 10 Must + 2 Could, манифест `catalog.json` v1, два трека обновлений, профиль «защищённый
    95	  контур», гипотеза с проверкой 2026-11-15; 79 историй; беклог RICE/MoSCoW.
    96	- Решение: **PRD, stories, backlog версии 0.2 приняты** как договор о том, что строим. Релизы:
    97	  v0.1 «Диктовка работает» (2026-09-30) → v0.2 «Каталог, обновления, автозапуск» (2026-10-15) →
    98	  v1.0 «Корпоративный контур и полировка» (2026-10-31); ГОСТ-подпись/ЗПС-пилот — v1.1.
    99	- Почему: требования заказчика №1–5 покрыты; спорные развилки закрыты ответами заказчика (см. записи выше).
   100	- Гейт: G1 · «да» заказчика (чат 2026-09-07).
   101	- Последствия: старт дизайна (UX-флоу → 2–3 направления → G2 → токены → дизайн-спец G3); дальнейшие
   102	  изменения скоупа — только через журнал решений и ревизию PRD.
   103	
   104	## 2026-09-07 — G2a закрыт: лого-пак знака 06 «Слоги» принят к работе
   105	- Контекст: доводка после выбора заказчика; замечание Юрки о похожести на Breeze «⋯/загрузка».
   106	- Решение: финальный знак — `design/brand/logo.svg` (имя в кривых), мастер-сетка 100×51,72; точки
   107	  1 : 1,38 : 1,90, промежутки 2,83 : 1, строка шире ряда в 1,23; **цвет состояния несёт только третья
   108	  точка** на всех размерах; 16 px нарисован отдельно. Пак: hicolor 16…512 + scalable, трей 22/16 × 6
   109	  состояний по соглашению KDE, фавикон, README-марка. Источник истины — `brand-basics.md` §4–5.
   110	- Почему: похожесть с системными иконками снята пятью правками геометрии без смены идеи.
   111	- Гейт: G2a · «да» заказчика на направление и на лого-пак («Лого-пак принял», чат 2026-09-07 18:10).
   112	- Последствия: вход для `design-engineer` (токены) и `ux-analyst` (макеты).
   113	
   114	## 2026-09-07 — Ответы заказчика на открытые вопросы PRD (§12.1) — вход для ревизии PRD
   115	- Контекст: ProductManager сдал PRD/истории/беклог с шестью вопросами, меняющими продукт.
   116	- Решения заказчика (чат 2026-09-07):
   117	  1. **Сетевые проверки на первом экране — тумблер ПУСТ**, пользователь включает сам (рекомендация
   118	     юриста); во всех профилях. Требования №1/№4 выполняются после явного включения.
   119	  2. **Модель держим в памяти постоянно** (≈415 МБ); выгрузка по простою — опция в настройках.
   120	  3. **ЗПС-трек (ГОСТ-подпись, ВМ с ЗПС, цель Ц5) — после v1.0.** В v1.0 остаётся детект
   121	     `DIGSIG_ELF_MODE` и ветка «скачать пакет и передать администратору». Уточняет решение
   122	     «оба режима сразу» от 2026-09-07: второй режим в v1 — без подписи артефактов.
   123	  4. **Автозапуск включён по умолчанию** на последнем экране онбординга; **переносится в v0.2**.
   124	- Решения Юрки (рутинные): хоткей по умолчанию — `Ctrl+Space` (push-to-talk, как в Handy заказчика);
   125	  Whisper в каталоге — три позиции (large-v3-turbo / small / base), medium и large-v3 не включаем.
   126	- Гейт: G1 (в процессе приёмки) · «да» заказчика по каждому пункту.
   127	- Последствия: ревизия 2 PRD/историй/беклога; v1.0 = требование №5 + полировка + комплаенс без Ц5.
   128	
   129	## 2026-09-07 — G2a (часть 3): знак — 06 «Слоги»
   130	- Контекст: раунд 2 (последний по лимиту) — восемь направлений (01/01b, 03/03b, 05–08); дизайнер
   131	  рекомендовал 05 «Абзац», безопасный — 03b; Юрка отметил риски: 07 читается как тумблер, 06 — как
   132	  значок «ещё/загрузка», 01b — как скобки кода.
   133	- Решение: **06 «Слоги»** — три точки с сужающимися промежутками (дискретная речь) над сплошной строкой
   134	  (непрерывный текст). Доводка: усилить отличие от «⋯/загрузка» в трее (ритм промежутков, вес строки),
   135	  логотип с текстом в кривых, лого-пак (hicolor PNG 16…512 + SVG, моно, фавикон, 5 трей-состояний).
   136	- Почему: выбор заказчика (чат 2026-09-07) — строгая геометрия без «звуковой волны».
   137	- Гейт: G2a · «да» заказчика. Лимит раундов исчерпан — дальше только доводка внутри направления.
   138	- Последствия: UX-макеты и токены берут знак 06 и палитру brand-basics; `design/brand/logo.svg` — источник истины.
   139	
   140	## 2026-09-07 — Лицензия репозитория: GPL-3.0-or-later (вместо предварительной MIT)
   141	- Контекст: правовое заключение (`research/legal.md`): PyQt5 доступен только под GPL v3 / коммерческой
   142	  лицензией Riverbank → утилита на PyQt5 обязана распространяться под GPL v3; MIT в репо некорректна.
   143	  Заказчик 2026-09-07 подтвердил, что GPLv3 допустима.
   144	- Решение: `LICENSE` = GPL-3.0-or-later; атрибуции моделей/движков — в `NOTICE` (sherpa-onnx §4(d) Apache-2.0,
   145	  Whisper MIT/Apache-2.0 — обе, CC-BY-модели — с указанием).
   146	- Почему: единственный полный Qt-биндинг для Python в apt Astra 1.8 — PyQt5; закрытая поставка не нужна.
   147	- Гейт: — · «да» заказчика (ответ «GPLv3 допустима»).
   148	- Последствия: сторонние вклады принимаются под GPL; при смене стека на LGPL (PySide6/GTK4) лицензию можно ослабить.
   149	
   150	## 2026-09-07 — Рамки продукта до PRD: аудитория/ЗПС, лицензия, индикация записи
   151	- Контекст: ресёрч платформы показал, что при включённой ЗПС (digsig) самообновление «одной кнопкой»
   152	  невозможно (подпись ELF и .so ГОСТ-ключом, регистрация ключа админом с перезагрузкой); единственный
   153	  полный Qt-биндинг для Python в apt Astra 1.8 — PyQt5 (GPLv3); канон индикации записи — пилюля поверх окон.
   154	- Решение (три ответа заказчика, чат 2026-09-07):
   155	  1. **Оба режима обновления в v1**: без ЗПС — GitHub Releases → .deb → polkit → apt из GUI;
   156	     с ЗПС — уведомление + скачивание пакета «передать администратору»; релизы подписываются
   157	     (ГОСТ-подпись пакетов и вендоренных .so — отдельная веха), проверка на ВМ с ЗПС обязательна.
   158	  2. **GPLv3 допустима** → стек Python 3.11 + PyQt5 5.15 из apt Astra (код репо — MIT; сборка
   159	     распространяется на условиях GPL). Закрытая поставка не требуется.
   160	  3. **Индикация записи — пилюля с визуализацией по умолчанию, отключаемая** в настройках
   161	     (остаются трей + звук); опыт фокуса на KDE/X11 из Astra Cowork учитывать.
   162	- Почему: заказчик хочет покрыть и свою машину, и корпоративные ALSE; GPL не мешает; пилюля — ожидание пользователей.
   163	- Гейт: — (рамки для G1) · «да» заказчика.
   164	- Последствия: в PRD — две ветки обновлятора и настройка «источник обновлений/моделей» (корпоративное
   165	  зеркало); архитекторам — PyQt5 как заданное ограничение; UX — экран настроек с выключателем пилюли.
   166	
   167	## 2026-09-07 — Решения Юрки по открытым вопросам ресёрча (рутинные, без гейта)
   168	- Модель по умолчанию — текущая GigaAM v3 e2e_rnnt; e2e_ctc — в каталоге как «быстрее на ~25 %».
   169	- Каталог v1 — не менее 8 моделей (GigaAM v3 ×3, GigaAM Multilingual, Whisper ×3–4 int8, Vosk small-ru).
   170	- CC-BY-модели допускаются с атрибуцией в «О программе»; Silero (CC-NC) исключён.
   171	- ОЗУ и скорость в каталоге — замер на этой машине после первого прогона (VmHWM воркера) с пометкой
   172	  «замерено / заявлено / оценка»; до замера — справочные значения.
   173	- Зеркало моделей в GitHub Releases репо допустимо (MIT); настройка «корпоративный URL каталога» — в v1.
   174	- Постобработка LLM (как в Handy) — вне v1.
   175	- История буфера Klipper: история «не оставлять распознанный текст в истории буфера» — в беклог (Should).
   176	
   177	## 2026-09-07 — G2a (часть 2): цвет «слушаю» — бирюза; знак — второй раунд
   178	- Контекст: показаны 4 направления знака; заказчик: «Ёлочки» (01) и «Клавиша» (03) — более-менее,
   179	  «Волна» (02) и «Каретка» (04) не зацепили, нужны ещё варианты.
   180	- Решение: цвет состояния «слушаю» в трее — **фирменная бирюза #12B3A0** (красный только для ошибки);
   181	  запущен **раунд 2** brand-designer (3–4 новых направления + 01/03 под именем Astra Voice).
   182	  Это последний раунд по лимиту (🔁 2); если и он не попадёт — расхождение вкусов выносится заказчику
   183	  вопросами, не угадыванием.
   184	- Почему: выбор заказчика (чат 2026-09-07).
   185	- Гейт: G2a (цвет) · «да» заказчика; знак — открыт.
   186	- Последствия: UX-макеты стартуют на палитре/типографике brand-basics с плейсхолдером знака;
   187	  финальный знак подставляется после выбора.
   188	
   189	## 2026-09-07 — G2a (часть 1): имя продукта — Astra Voice
   190	- Контекст: brand-designer предложил 6 вариантов (рекомендовал «Вслух»); «Astra Voice» помечен риском —
   191	  «Astra» товарный знак ГК «Астра» (заказчик связан с Астрой; repo под личным аккаунтом ArrivaRUS).
   192	- Решение: имя **Astra Voice** (латиницей), slug/репо `astra-voice` остаются без переименования.
   193	  Текстовая часть во всех концептах знака заменяется с «Вслух» на «Astra Voice» при доводке.
   194	- Почему: выбор заказчика (чат 2026-09-07); продукт живёт в экосистеме Astra Linux.
   195	- Гейт: G2a (имя) · «да» заказчика.
   196	- Последствия: вопрос прав на знак «Astra» остаётся в списке задач комплаенса (`research/legal.md`);
   197	  направление знака и цвет «слушаю» — отдельный выбор (G2a часть 2).
   198	
   199	## 2026-09-07 — Публичный репозиторий и лицензия (предварительно)
   200	- Контекст: заказчик попросил публичный GitHub-репо под проект. Публичному репо нужна лицензия.
   201	- Решение: репо `ArrivaRUS/astra-voice` (public, ветка `main`); лицензия **MIT** — предварительно,
   202	  как у GigaAM/Whisper/Handy; смена лицензии — одной правкой файла.
   203	- Почему: MIT совместима с лицензиями используемых моделей и движков и не мешает корпоративному использованию.
   204	- Гейт: — · «да» заказчика на публичный репо (чат 2026-09-07); лицензия — допущение Юрки, ждёт подтверждения.
   205	- Последствия: модели в git не кладём (только манифесты с ревизией и sha256), секреты — никогда.
   206	
   207	## 2026-09-07 — G0: строим (короткий маршрут Discovery)
   208	- Контекст: заказчик сформулировал задачу и требования сам («упаковать в новую утилиту с нуля под ALSE»);
   209	  рынок/спрос проверять не нужно — утилита заменяет работающий Handy на машинах заказчика.
   210	- Решение: **GO** без рыночного ресёрча; Discovery только техническое (движки/модели, самообновление,
   211	  автозапуск, GUI-стек под ALSE) + правовое заключение (лицензии моделей, 152-ФЗ по голосу, ЗПС).
   212	  Из-за автономной сессии (заказчик не отвечает по ходу) волны ведутся параллельно, а гейты
   213	  G1/G2a/G2b выносятся заказчику одним пакетом.
   214	- Почему: экономия времени заказчика; риск расхождения PRD↔дизайн мал — требования конкретные.
   215	- Гейт: G0 · «да» заказчика (сам запрос 2026-09-07).
   216	- Последствия: имя проекта — рабочее (`astra-voice`), финальное — на G2a вместе с логотипом.

exec
/bin/bash -lc "nl -ba PRD.md | sed -n '410,510p'; rg -n 'S14|S1-A7|S11-A4|SNI|агент|обрыв|краш|сбой|settings.json|measurements|update-cache|policy.conf|смен|выгруз' PRD.md" in /home/astra/Документы/astra-voice
 succeeded in 0ms:
   410	
   411	### S16. Удаление утилиты
   412	- **A1.** Given утилита установлена с моделями и автозапуском, When `apt remove astra-voice`, Then пакет удалён, пользовательские данные (`~/.config`, `~/.local/share/astra-voice`) **не** тронуты, ярлык автозапуска остаётся, но не ломает вход (Exec отсутствует → KDE молча пропускает); `apt purge` не трогает домашние каталоги; в «О программе» есть подсказка «Удалить модели и настройки» с путями.
   413	
   414	---
   415	
   416	## 7. Каталог моделей
   417	
   418	### 7.1 Требования к карточке — см. F7 (таблица полей). Протокол честности:
   419	- Одна единица размера во всём UI — **МБ (десятичные)**.
   420	- Полоска качества строится **только** по одному набору (Russian LibriSpeech *test*, бенчмарк onnx-asr, `comparison.md`); пунктуация — отдельный значок, не участвует в полоске; по клику — «Как мы считаем» с источником и датой. **Оговорка в подсказке: «WER бенчмарка измерен на fp32-весах той же модели»** — публичного WER для int8 нет ни у кого; расхождение int8/fp32 считаем малым, пока собственный замер (US-5.4) не покажет иное.
   421	- Полоска скорости — из RTFx **x64 int8** (`benchmarks.md` onnx-asr, столбец `x64 RTFx (int8)`, Ryzen 7 9800X3D); «замерено» перекрывает «бенчмарк»; холодный первый прогон исключается. Три состояния достоверности бенчмарка: `benchmark` (int8 по протоколу) · `benchmark_fp32` (int8-замера нет — T-one, GigaAM Multilingual) · `no_data` (Whisper small: ни WER, ни RTFx по протоколу; FLEURS 11,4 % — другой протокол, только в подсказке; замер в v0.2 по US-5.4). Цифры fp32 из `comparison.md` в полоску **не** подставляются как int8.
   422	- ОЗУ — три уровня достоверности; при смене ревизии модели или числа потоков замер сбрасывается.
   423	- Whisper base показывается с почти пустой полоской качества (WER 38,33 %) и подписью «для русского не рекомендуется — ошибка в каждом третьем слове» — честно, чтобы пользователь не решил, что «утилита плохая».
   424	- Единственный источник цифр §7.2 — `research/catalog-numbers.md` (HF API `?blobs=true`, байты; ревизии `main` на 2026-09-08). Правка цифр — только через него.
   425	
   426	### 7.2 Состав каталога v1 (Must = 9, Should = 1, Could = 2)
   427	Состав — по своду `research/summary.md` §6 и решениям 2026-09-07/08 (Whisper — три позиции; T-one — Should). **Цифры — из `research/catalog-numbers.md`** (источник истины, 2026-09-08): диск — сумма точных байт файлов, нужных рантайму, по `GET /api/models/{repo}?blobs=true`, **десятичные МБ** (в ревизии 2 девять позиций были в МиБ); WER — Russian LibriSpeech test, onnx-asr `comparison.md` (**fp32-веса**); RTFx — x64 **int8**, `benchmarks.md` (Ryzen 7 9800X3D). Где у модели два экспорта (sherpa-onnx / onnx-asr), указаны оба размера — рантайм v1 выбирают архитекторы, в манифест попадает один. ОЗУ — оценка ×1,85 от int8-весов (разброс 1,5–3×), к fp32 (T-one) неприменима; заменяется замером (US-5.4).
   428	| # | Модель · вариант | Назначение (текст карточки) | Диск, МБ | WER ru (fp32) | RTFx x64 int8 | ОЗУ (уровень) | Пункт. | Язык | Лицензия | Происх. (по юрлицу) | Приор. |
   429	|---|---|---|---|---|---|---|---|---|---|---|---|
   430	| 1 | **GigaAM v3 RNN-T** (e2e_rnnt int8) — по умолчанию | Русская диктовка с пунктуацией. Рекомендуем | **231,9** (sherpa: csukuangfj `…transducer-punct-…v3…`) / 226,4 (onnx-asr: istupakov `gigaam-v3-onnx`) | 7,60 % | 42,5 | 415 МБ (замерено, эталон) | да | ru | MIT · GigaChat Team, 2024 | Сбер — отеч. | Must |
   431	| 2 | GigaAM v3 CTC (e2e_ctc int8) | То же, быстрее на ~25 %, точность чуть ниже | 224,9 | 7,80 % | 52,2 | ~415 МБ (оценка) | да | ru | MIT | Сбер — отеч. | Must |
   432	| 3 | GigaAM v3 RNN-T без пунктуации (rnnt int8) | Максимальная точность слов, без знаков препинания | 229,3 | **4,39 %** | 42,8 | ~425 МБ (оценка) | нет | ru | MIT | Сбер — отеч. | Must |
   433	| 4 | GigaAM Multilingual CTC 220M (int8) | Русский + казахский/киргизский/узбекский/английский; без пунктуации | 224,8 (istupakov) | 8,43 % | **по бенчмарку fp32: 58,5** (int8-замера нет) | ~415 МБ (оценка) | нет | ru, kk, ky, uz, en | MIT | Сбер — отеч. | Must |
   434	| 5 | T-one (Т-Банк) | Русская, лёгкая, без пунктуации; хорошая точность | 144,2 (**fp32 — int8-весов не существует**) | 6,57 % | **по бенчмарку fp32: 26,3** (int8 N/A) | нет оценки (fp32; замер в v0.2) | нет | ru | Apache-2.0 | T-Tech / АО «ТБанк» — отеч. | **Should** |
   435	| 6 | Vosk ru 0.54 (int8) | Лёгкая полная Vosk; точность ниже GigaAM | 72,5 | 9,89 % | 70,5 | ~135 МБ (оценка) | нет | ru | Apache-2.0 · Alpha Cephei Inc. | **зарубежная · команда из России** (Alpha Cephei Inc., Делавэр, США); **вне фильтра «только отечественные»** | Must |
   436	| 7 | Vosk small ru 0.52 (int8) | Для слабых машин и малого диска | 26,7 | 14,53 % | 83,5 | ~50 МБ (оценка) | нет | ru | Apache-2.0 · Alpha Cephei Inc. | зарубежная · команда из России; вне фильтра | Must |
   437	| 8 | Whisper large-v3-turbo (int8) | Многоязычная, качественно, **медленно на CPU** | 1 089,1 (onnx-community, бенчмарчен) / 1 036,6 (sherpa) | 10,10 % | **3,9** | ~2 ГБ (оценка) | да | 99 языков | **MIT** (`openai/whisper-large-v3-turbo`) | OpenAI (США) — зарубеж. | Must |
   438	| 9 | Whisper small (int8) | Многоязычная, средняя; по-русски слабее GigaAM | 253,5 (onnx-community — единственный экспорт под onnx-asr) / 375,5 (sherpa) | **нет данных по протоколу** (FLEURS 11,4 % — другой протокол, только в подсказке; оценка ≈20 % — не показывать) | **нет данных** (оценка ≈13–16 — не показывать); замер в v0.2 (US-5.4) | ~470 МБ (оценка) | да | 99 языков | **Apache-2.0** (`openai/whisper-small`) | OpenAI — зарубеж. | Must |
   439	| 10 | Whisper base (int8) | Быстрая и лёгкая; **для русского не рекомендуется — ошибка в каждом третьем слове** | 109,1 (istupakov `whisper-base-onnx`, бенчмарчен) / 160,6 (sherpa) | **38,33 %** | 51,6 | ~200 МБ (оценка) | да | 99 языков | **Apache-2.0** (`openai/whisper-base`) | OpenAI — зарубеж. | Must |
   440	| 11 | GigaAM Multilingual Large CTC (int8) | «Качество любой ценой»: точнее v3, тяжёлая, без пунктуации | 591,6 (istupakov) | **5,55 %** | по бенчмарку fp32: 30,4 (int8-замера нет) | ~1,1 ГБ (оценка) | нет | ru, kk, ky, uz, en | MIT | Сбер — отеч. | Could |
   441	| 12 | NeMo FastConformer ru pc (CTC) | Самая быстрая, с пунктуацией, точность ниже | 131,6 (CTC) / 136,4 (RNN-T-декодер: WER 11,57 %, RTFx 54,2) | 13,10 % | 70,6 | ~245 МБ (оценка) | да | ru | CC-BY-4.0 (атрибуция обязательна) | NVIDIA (США) — зарубеж. | Could |
   442	Ревизии `main` всех репозиториев на 2026-09-08 и точные байты — в `research/catalog-numbers.md` §1; манифест закрепляет их как `sources[].revision`. Whisper-репозитории `csukuangfj/sherpa-onnx-whisper-*` **без файла лицензии** — юридически опираемся на карточки `openai/whisper-*` (NOTICE по моделям раздельно, §9.6). Исключены: Silero (CC-NC), Canary/Parakeet (слабы по-русски, CC-BY), GGUF/transcribe.cpp (нужен C++-движок), Whisper medium/large-v3 (непригодны по скорости на CPU — решение 2026-09-07), fp32-варианты GigaAM (885 МБ, без выигрыша). **Рантайм v1 (onnx-asr или sherpa-onnx) и раскладка файлов — решение архитекторов**, PRD не выбирает; условие неизменно: источник весов соответствует рантайму (`onnx-asr` ↔ istupakov/onnx-community; `sherpa-onnx` ↔ csukuangfj), пресеты держим у себя с закреплённым коммитом и sha256.
   443	
   444	### 7.3 Формат манифеста каталога (`catalog.json`, версия 1)
   445	```json
   446	{
   447	  "manifest_version": 1,
   448	  "generated_at": "2026-09-07T12:00:00Z",
   449	  "publisher": "astra-voice",
   450	  "models": [
   451	    {
   452	      "id": "gigaam-v3-e2e-rnnt-int8",
   453	      "name": "GigaAM v3 RNN-T",
   454	      "vendor": "Сбер (GigaChat Team)",
   455	      "family": "gigaam-v3", "variant": "e2e_rnnt",
   456	      "engine": "sherpa-onnx", "layout": "sherpa-nemo-transducer",
   457	      "purpose": {"ru": "Русская диктовка с пунктуацией — по умолчанию", "en": "…"},
   458	      "languages": ["ru"], "punctuation": true,
   459	      "origin": {"kind": "ru", "holder_entity": "ПАО Сбербанк (РФ)", "note_ru": null},
   460	      "license": {"spdx": "MIT", "holder": "GigaChat Team, 2024", "url": "https://…/LICENSE", "attribution_required": false},
   461	      "recommended": true, "min_ram_mb": 1024,
   462	      "disk_bytes": 231897202,
   463	      "ram_peak_mb": {"value": 415, "kind": "measured", "cpu": "Intel Core Ultra 7 255U", "threads": 2, "at": "2026-09-04"},
   464	      "quality": {"kind": "benchmark", "wer_pct": 7.60, "dataset": "Russian LibriSpeech test", "weights": "fp32", "runtime": "onnx-asr", "source_url": "https://github.com/istupakov/onnx-asr/blob/main/docs/comparison.md"},
   465	      "speed": {"kind": "benchmark", "rtfx": 42.5, "weights": "int8", "cpu": "Ryzen 7 9800X3D", "source_url": "https://istupakov.github.io/onnx-asr/benchmarks/"},
   466	      "files": [
   467	        {"path": "encoder.int8.onnx", "size": 224570820, "sha256": "369f35a7…"},
   468	        {"path": "decoder.onnx", "size": 4600132, "sha256": "38fc7475…"},
   469	        {"path": "joiner.onnx", "size": 2712896, "sha256": "602ff701…"},
   470	        {"path": "tokens.txt", "size": 13354, "sha256": "…"}
   471	      ],
   472	      "sources": [
   473	        {"type": "hf", "repo": "csukuangfj/sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16", "revision": "a6039be7cee829a9044a69ac0ebaf1c191217c97"},
   474	        {"type": "github_release", "base": "https://github.com/ArrivaRUS/astra-voice/releases/download/models-2026.09/"},
   475	        {"type": "corp", "base": "{corp_base}/models/gigaam-v3-e2e-rnnt-int8/"}
   476	      ],
   477	      "update_watch": {"hf_repo": "ai-sage/GigaAM-v3", "branch": "e2e_rnnt", "authors": ["ai-sage", "csukuangfj", "istupakov"], "family_keywords": ["giga-am", "gigaam"]},
   478	      "related": ["gigaam-v3-e2e-ctc-int8", "gigaam-v3-rnnt-int8"],
   479	      "smoke_test": {"wav": "builtin:test-ru-6s.wav", "expect_any": ["проверка", "связи"]}
   480	    },
   481	    {
   482	      "id": "whisper-small-int8",
   483	      "name": "Whisper small", "vendor": "OpenAI",
   484	      "engine": "onnx-asr", "layout": "onnx-community-whisper",
   485	      "languages": ["*"], "punctuation": true,
   486	      "origin": {"kind": "foreign", "holder_entity": "OpenAI (США)", "note_ru": null},
   487	      "license": {"spdx": "Apache-2.0", "holder": "OpenAI", "url": "https://huggingface.co/openai/whisper-small", "attribution_required": false},
   488	      "disk_bytes": 253466497,
   489	      "ram_peak_mb": {"value": 470, "kind": "estimated"},
   490	      "quality": {"kind": "no_data", "note_ru": "По протоколу данных нет; FLEURS 11,4 % — другой протокол"},
   491	      "speed": {"kind": "no_data"},
   492	      "files": [{"path": "encoder_model_int8.onnx", "size": 92326127, "sha256": "…"}, {"path": "decoder_model_merged_int8.onnx", "size": 156750845, "sha256": "…"}],
   493	      "sources": [{"type": "hf", "repo": "onnx-community/whisper-small", "revision": "36050c46d777d46dc4b5f43f6d90574fc38f8732"}]
   494	    }
   495	  ]
   496	}
   497	```
   498	Правила: **`engine`** ∈ {`onnx-asr`, `sherpa-onnx`} и **`layout`** (именованная раскладка файлов: `sherpa-nemo-transducer`, `sherpa-nemo-ctc`, `onnx-asr-gigaam-v3`, `onnx-asr-gigaam-multilingual`, `onnx-community-whisper`, `sherpa-whisper`, `vosk-onnx`, `onnx-asr-nemo` — список закрывают архитекторы) обязательны на каждую модель; репозиторий и файлы записи — из таблицы `research/catalog-numbers.md` §1 для выбранного рантайма (в v1 поставляется один рантайм, манифест содержит записи только для него; второй пример выше показывает раскладку onnx-asr). `quality.kind`/`speed.kind` ∈ {`measured`, `benchmark`, `benchmark_fp32`, `no_data`}; `quality.weights` фиксирует, на каких весах измерен WER (в бенчмарке onnx-asr — `fp32`); при `no_data` полоска не рисуется. `origin.kind` ∈ {`ru`, `foreign`} — по юрлицу правообладателя, `note_ru` — уточнение для карточки («команда из России»). `sha256` обязателен для каждого файла (для LFS — из HF API: `lfs.sha256` в `GET /api/models/{repo}?blobs=true` или `lfs.oid` в `/api/models/{repo}/tree/{rev}` — одно и то же значение; для не-LFS файлов вроде `tokens.txt` API даёт git-SHA-1, поэтому sha256 считается при публикации манифеста); `sources[].revision` обязателен, загрузка идёт по `/resolve/{revision}/…`, не по `main`; `update_watch.branch` — ветка-вариант (для `ai-sage/GigaAM-v3` следим за `targetCommit` ветки, не `main`); манифест сопровождается отсоединённой подписью `catalog.json.sig` (тот же инструмент и ключ, что у релизов утилиты — minisign или GPG; все зеркала проверяются одним закреплённым ключом); корпоративный манифест — тот же формат, `sources` может содержать только `corp`; поля `ram_peak_mb.kind` ∈ {`measured`, `claimed`, `estimated`}; локальные замеры хранятся отдельно (`~/.local/state/astra-voice/measurements.json`) и перекрывают манифест в UI; неизвестные поля игнорируются (forward-compat), `manifest_version` > поддерживаемой → отказ с сообщением.
   499	
   500	---
   501	
   502	## 8. Нефункциональные требования
   503	| Область | Требование |
   504	|---|---|
   505	| Офлайн-первый | Все функции, кроме четырёх сетевых действий (F14), работают без сети; ни одна сетевая ошибка не блокирует UI и не показывает модалку |
   506	| Старт | До готовности трея ≤ 1,5 с; до готовности к диктовке (модель в памяти) ≤ 5 с на эталонной машине; при автозапуске — без `sleep`-костылей (прогноз Researcher #2 для PyQt5 из apt: холодный старт GUI 0,15–0,6 с, low) |
   507	| Задержка | «Отпустил → текст» p95 ≤ 0,5 с (6-с фраза, GigaAM v3 e2e_rnnt, модель в памяти, эталон); ориентир для 4 ядер/8 ГБ — ≤ 1,5 с (оценка, проверить) |
   508	| Память | GUI + трей в покое без модели ≤ 120 МБ RSS (допущение для PyQt5/QML — проверить спайком); воркер с GigaAM v3 ≤ 450 МБ (замер 415; для незамеренных моделей оценка 1,5–3× от int8-весов); суммарно ≤ 600 МБ; замер — VmHWM воркера, дополнительно PSS из `smaps_rollup` |
   509	| CPU в покое | ≤ 1 % CPU в простое (нет опроса микрофона, нет таймеров чаще 1 Гц) |
   510	| Сеть | Только хосты из `PRIVACY.md`; проверки выключены, пока пользователь их не включил; ≤ 1 проверка/24 ч на каждый источник; таймаут 3 с; уважение `RateLimit`/`Retry-After`, `ETag`; прокси из окружения; TLS-проверка не отключается, корпоративный CA — из системного хранилища; User-Agent проекта |
67:| Модель в памяти | Держится постоянно; выгрузка по простою — опция (заказчик 2026-09-07) |
81:Администратор (П3 — не через UI утилиты, а через пакет, `policy.conf` и инструкцию); Система (фоновые
101:| Сетевые проверки | по своим тумблерам | по своим тумблерам | может **запретить** (`policy.conf`: офлайн, корпоративный URL, только отечественные модели) |
104:`~/.local/state/astra-voice/`, `~/.cache/astra-voice/`. Ничего общесистемного, кроме пакета и `/etc/astra-voice/policy.conf`.
125:4. Экраны: **(1) Приветствие и сеть** — язык интерфейса (ru/en, авто по `LANG`) и два тумблера «Проверять обновления утилиты» / «Проверять обновления моделей» (подпись: не чаще раза в сутки; хосты huggingface.co, github.com). **Оба тумблера пусты по умолчанию во всех профилях** — пользователь включает сам; пока не включил — утилита в сеть не выходит (кроме явного «Скачать» на экране 2). Требования заказчика №1/№4 (проверка при запуске) действуют после явного включения. Если `policy.conf` задаёт `offline=true`/`profile=secure`, тумблеры показаны заблокированными («Задано администратором»). **(2) Модель** — рекомендованная карточка сверху (GigaAM v3 RNN-T), остальные по размеру; кнопки «Скачать» (показывает хост и объём) и «Установить из файла/папки…»; шаг нельзя пропустить без модели. **(3) Горячая клавиша** — предзаполнена **`Ctrl+Space`, режим «удерживать» (push-to-talk)**; поле захвата другой комбинации, переключатель «удерживать / нажать-нажать»; проверка конфликта с глобальными шорткатами KDE (`kglobalshortcutsrc`) — предупреждение, не запрет. **(4) Проверка микрофона** — выбор устройства, живой индикатор уровня, кнопка «Сказать тестовую фразу» → распознанный текст на экране (без вставки). **(5) Автозапуск** — тумблер (по умолчанию включён, с текстом, что именно создаётся) и «Готово».
128:**Исключения.** Нет сети на шаге 2 → карточки «Скачать» неактивны с текстом «Нет доступа к <хост>», активен «Установить из файла»; обрыв загрузки → докачка (F7); микрофон не найден → F6; ЗПС блокирует запуск воркера → текст «Часть компонентов не подписана для ЗПС — обратитесь к администратору (инструкция)».
153:4. Иконка трея (SNI/`QSystemTrayIcon`): 4 состояния — «готов», «слушаю» (бирюза), «распознаю», «ошибка/микрофон недоступен» (красный). Тема иконки — по теме панели (светлая/тёмная). Регистрация в трее с ретраем до 30 с после старта (гонка с панелью).
164:2. Модель по умолчанию — GigaAM v3 e2e_rnnt int8. Активная модель загружается в память при старте в фоне (после готовности трея) и **держится в памяти постоянно** (решение заказчика 2026-09-07); выгрузка по простою — опция «выгружать после: 5 / 15 / 60 мин / никогда», по умолчанию «никогда».
219:1. Загрузка во временный `.part` с докачкой по `Range` (продолжать только при 206 с корректным `Content-Range`; на 200 — сначала); URL закреплён на `/resolve/{revision}/…`, не на `main`; после завершения — размер и sha256 против манифеста, `fsync`, атомарный `os.replace`; модель хранится в каталоге `<ревизия>/`, активная ревизия — указатель `current.json` (откат = смена указателя); несовпадение → удалить и показать ошибку, не «доклеивать».
235:1. Работает только при включённом пользователем тумблере «Проверять обновления моделей» (по умолчанию выключен; онбординг, экран 1); в офлайн-режиме, при `policy.conf: offline` или `HF_HUB_OFFLINE=1` в окружении — никогда.
236:2. Не чаще **1 раза в 24 ч**, запуск через случайную задержку 0–30 мин после готовности GUI, общий таймаут 3 с, ≤ 1 повтор; при 429 — не повторять до конца окна; кэш `~/.local/state/astra-voice/update-cache.json`. User-Agent `astra-voice/X.Y.Z (+repo URL)`. Прокси — из окружения.
251:   - **Трек A (без ЗПС, пользователь может пройти polkit):** «Скачать и установить» → скачать `.deb` + `SHA256SUMS` + подпись в `~/.cache/astra-voice/updates/` → проверить sha256 и подпись релиза (ключ вшит в пакет; minisign или GPG — архитекторам) → **один** вызов root-помощника `/usr/libexec/astra-voice-update-helper` через `pkexec` с собственным polkit-действием (`auth_admin`, одно окно пароля KDE); помощник копирует пакет в root-staging, **повторно** проверяет подпись/sha256/имя пакета/архитектуру/повышение версии и выполняет `apt-get install --no-remove ./astra-voice_X.Y.Z_amd64.deb` → «Установлено. Перезапустить сейчас / позже». Если агент polkit не запущен — «Не найден агент авторизации (polkit-kde-authentication-agent-1)» + подсказка «Обновить вручную: sudo apt install ./…»; первая установка — штатным apt (помощник появляется с первым пакетом).
252:   - **Трек B (ЗПС включена, или нет прав администратора, или `policy.conf: updates=admin`):** «Скачать пакет для администратора» → те же файлы + `INSTALL-ADMIN.md` (инструкция: проверка подписи релиза, `apt install`; при включённой ЗПС — подпись ELF/`.so` по процедуре заказчика; в v1 артефакты **не** подписаны ГОСТ-ключом на сборке — это v1.1) → «Открыть папку» + текст «Установку выполняет администратор. Подпись пакета проверена». Кнопки «Установить» **нет**.
254:4. Определение трека: ЗПС = `DIGSIG_ELF_MODE ≠ 0` (если файл конфигурации читаем) **или** `policy.conf: zps=on`; при невозможности определить — вопрос пользователю «На этом компьютере включена замкнутая программная среда? Да / Нет / Не знаю (→ трек B)». Права = членство в `astra-admin`/`sudo`.
276:1. Разделы и строки: **Общие** — хоткей(и), режим, микрофон, индикатор (снизу/сверху/выкл), звук, автозапуск; **Модели** — F7; **Вывод** — комбинация вставки, задержка, восстановление буфера, защита истории Klipper, пробел в конце, терминалы; **Продвинутые** — потоки ORT, выгрузка модели, лимит записи, порог тишины, «надёжный режим evdev», экспериментальные; **Сеть и обновления** (отдельный раздел сайдбара — G2 2026-09-08) — тумблеры «проверять обновления утилиты» / «…моделей», «офлайн-режим», URL каталога/обновлений, «проверить сейчас», «обновить из файла»; **О программе** — F13.
277:2. Тема — из `kdeglobals` до первой отрисовки, пересчёт при смене темы в KDE (сигнал D-Bus/файл) без перезапуска.
278:3. Язык UI — русский исходный, английский второй; все строки во внешних файлах перевода; смена языка — без перезапуска (или с явным «перезапустить»).
279:4. Настройки — один JSON в `~/.config/astra-voice/settings.json`, версионированный (`schema_version`), миграции вперёд; «Сбросить настройки» с подтверждением.
280:5. Строки, зафиксированные `policy.conf`, показываются заблокированными с подписью «Задано администратором».
282:**Исключения.** Битый settings.json → резервная копия `.bak`, настройки по умолчанию, уведомление.
298:3. `/etc/astra-voice/policy.conf` (читается при старте, только чтение): `profile=personal|secure`, `offline=true|false`, `catalog_url=`, `updates_url=`, `updates=gui|admin`, `zps=auto|on|off`, `domestic_only=true|false`, `autostart_default=`. `profile=secure` ⇒ `offline=true`, `updates=admin`, `domestic_only=true`, если не переопределено. Значения из policy блокируют соответствующие строки настроек.
302:**Acceptance.** Given `policy.conf` с `profile=secure`, When запускаю утилиту, Then строка обновлений показывает «Проверка обновлений отключена (задано администратором)», в «Моделях» нет кнопок «Скачать» с сети, показаны только отечественные модели, а `tcpdump` за сессию не фиксирует исходящих соединений процесса.
306:**Бизнес-правила.** Формат — §7.3; встроенный манифест в пакете — источник по умолчанию; `catalog_url` (настройка или policy) — тот же JSON-формат + отсоединённая подпись (ключ — наш вшитый либо корпоративный из `policy.conf: catalog_pubkey=`); `sources[].type=corp` разрешаются относительно `corp_base`; манифест без валидной подписи не применяется (предыдущий остаётся). Инструмент `astra-voice-catalog` (CLI, P2) для генерации корпоративного манифеста из папки с весами (считает sha256, размеры).
311:**Бизнес-правила.** «Отладка» (по `Ctrl+Shift+D`): уровень логов, «Открыть лог», «Собрать диагностику» — zip с логами, `settings.json` (без путей домашнего каталога), версиями, выводом `pactl info`/`wpctl status`, **без** аудио и текстов; отправка — только вручную пользователем. Тестовый экран микрофона (уровень + распознать без вставки).
331:- **A5.** Given включена выгрузка по простою и модель выгружена из памяти, When зажимаю хоткей, Then пилюля показывает «Загружаю модель…», запись при этом идёт, и текст вставляется после загрузки (≤ 5 с + время распознавания).
381:- **A1.** Given `DIGSIG_ELF_MODE=1` (или `policy.conf: zps=on`), When есть новая версия, Then панель показывает «Скачать пакет для администратора», кнопки «Установить» нет, текст «На этом компьютере включена замкнутая программная среда — установку выполняет администратор».
400:- **A2.** Given окно открыто, When меняю схему KDE на светлую, Then окно и пилюля перекрашиваются ≤ 2 с без перезапуска.
402:### S14. Отмена во время распознавания и очередь (F2)
422:- ОЗУ — три уровня достоверности; при смене ревизии модели или числа потоков замер сбрасывается.
498:Правила: **`engine`** ∈ {`onnx-asr`, `sherpa-onnx`} и **`layout`** (именованная раскладка файлов: `sherpa-nemo-transducer`, `sherpa-nemo-ctc`, `onnx-asr-gigaam-v3`, `onnx-asr-gigaam-multilingual`, `onnx-community-whisper`, `sherpa-whisper`, `vosk-onnx`, `onnx-asr-nemo` — список закрывают архитекторы) обязательны на каждую модель; репозиторий и файлы записи — из таблицы `research/catalog-numbers.md` §1 для выбранного рантайма (в v1 поставляется один рантайм, манифест содержит записи только для него; второй пример выше показывает раскладку onnx-asr). `quality.kind`/`speed.kind` ∈ {`measured`, `benchmark`, `benchmark_fp32`, `no_data`}; `quality.weights` фиксирует, на каких весах измерен WER (в бенчмарке onnx-asr — `fp32`); при `no_data` полоска не рисуется. `origin.kind` ∈ {`ru`, `foreign`} — по юрлицу правообладателя, `note_ru` — уточнение для карточки («команда из России»). `sha256` обязателен для каждого файла (для LFS — из HF API: `lfs.sha256` в `GET /api/models/{repo}?blobs=true` или `lfs.oid` в `/api/models/{repo}/tree/{rev}` — одно и то же значение; для не-LFS файлов вроде `tokens.txt` API даёт git-SHA-1, поэтому sha256 считается при публикации манифеста); `sources[].revision` обязателен, загрузка идёт по `/resolve/{revision}/…`, не по `main`; `update_watch.branch` — ветка-вариант (для `ai-sage/GigaAM-v3` следим за `targetCommit` ветки, не `main`); манифест сопровождается отсоединённой подписью `catalog.json.sig` (тот же инструмент и ключ, что у релизов утилиты — minisign или GPG; все зеркала проверяются одним закреплённым ключом); корпоративный манифест — тот же формат, `sources` может содержать только `corp`; поля `ram_peak_mb.kind` ∈ {`measured`, `claimed`, `estimated`}; локальные замеры хранятся отдельно (`~/.local/state/astra-voice/measurements.json`) и перекрывают манифест в UI; неизвестные поля игнорируются (forward-compat), `manifest_version` > поддерживаемой → отказ с сообщением.
525:Каждый файл — sha256 из манифеста (для HF — `lfs.oid`), проверка до `rename`; закреплённые ревизии (`pinned_revision`); смоук-тест после обновления ревизии с автоматическим откатом; манифест подписан (наш ключ вшит; корпоративный — из `policy.conf`). Runtime-загрузка модели трактуется как supply-chain-изменение, а не «данные».
531:**В v1.0 (заказчик 2026-09-07):** детект `DIGSIG_ELF_MODE`/`policy.conf: zps`, трек B «скачать пакет и передать администратору» (S10), документ для администратора; собственные ELF/`.so` (onnxruntime/движок, помощник обновления — ориентировочно 2–5 объектов, A11) в v1 **не подписаны** ГОСТ-ключом на сборке — на машине с ЗПС их подписывает интегратор/администратор заказчика по своей процедуре (`INSTALL-ADMIN.md`). **После v1.0 (v1.1, Ц5):** подпись всех собственных ELF/`.so` на сборке (`bsign-integrator`, ключ организации), открытый ключ и регистрация админом, `postinst` ничего не подписывает на месте, прогон на ВМ с ЗПС (P21½). Минимизировать число собственных ELF (Python поверх системного Qt) — действует с v0.1.
543:T1 (проектирование) — security-analyst по `security-gate`: поверхность = загрузка моделей, обновлятор (polkit), глобальный хоткей и вставка в чужие окна, `policy.conf`, парсинг манифеста. Ревью T2 на каждом изменении обновлятора; T3 перед публичным релизом.
579:**Нет.** Шесть вопросов ревизии 1 закрыты заказчиком и Юркой 2026-09-07 (`decisions/log.md`, «Ответы заказчика на открытые вопросы PRD»): 1 — сетевые тумблеры пусты во всех профилях; 2 — `Ctrl+Space` push-to-talk, toggle второй; 3 — модель в памяти постоянно, выгрузка — опция; 4 — ЗПС-трек (подпись, ВМ, Ц5) после v1.0; 5 — Whisper ×3; 6 — автозапуск включён по умолчанию, в v0.2.
601:- [x] Покрыты исключения: таймауты (F8/F9, S6), нет прав (F9 трек B, S10), битые данные (sha256, манифест, settings.json), границы (0,3 с / 120 с / диск / ОЗУ / 429).
618:| 3 | F4, S2-A5, §3 | «Никогда не выгружать» как допущение | Модель в памяти постоянно — решение; выгрузка по простою — опция | заказчик, вопрос 3 |

exec
/bin/bash -lc "nl -ba arch/plan-claude.md | sed -n '84,103p;285,352p'" in /home/astra/Документы/astra-voice
 succeeded in 0ms:
    84	| `ui/pill.py` + `qml/Pill.qml` | Контроллер оверлея: 12 состояний спеки §8.4, ширина 172→320 без elide, 9 столбиков `h=max(3,round(l*20))`, позиция по `availableGeometry` экрана активного окна (48 px), таймеры 500/1000/1200/800/3000 мс, показ ≤ 100 мс, re-assert «наверх» + проверка перекрытия | `state`, `levels[9]`, `text` | x11, theme | интеграция: скриншот состояний под Xvfb (DesignReviewer); e2e: фокус остаётся в Kate (S12-A3), Alt+Tab не содержит пилюлю |
    85	| `ui/tray.py` | `QSystemTrayIcon`: 6 состояний иконок по имени (`astra-voice-tray-{idle,listening,processing,done,error,nokey}`), подсказки, ретрай регистрации ≤ 30 с (watcher на шине), меню по спеке §9.2 (состав/неактивность), клик — окно; баннер «трей недоступен» | `state` | QtWidgets, notify | интеграция: `QSystemTrayIcon.isSystemTrayAvailable()` под Xvfb (false-ветка); e2e на Plasma и во Fly-сессии |
    86	| `ui/window.py` + `qml/{Main,Sidebar,SettingRow,ModelCard,Onboarding*,Dialogs}.qml` | Окно 900×620 (min), 6 разделов + «Отладка» (Ctrl+Shift+D), строка-статус (5+17 состояний), панель обновления (14), карточка модели (20, state-машина в одном компоненте), поле захвата (7), сегмент, тумблер с замком, Popup «Как мы считаем», клавиатура У13 | мосты `SettingsBridge/ModelsBridge/UpdatesBridge/AudioBridge/AboutBridge` (`QObject`, `pyqtProperty`+`notify`) | theme, все мосты | интеграция: каждый QML-файл создаётся без warnings (`QT_QPA_PLATFORM=offscreen`), скриншоты 24 экранов × 2 темы для DesignReviewer |
    87	| `ui/notify.py` | `org.freedesktop.Notifications` через `QtDBus` (кнопки-действия, `ActionInvoked`), таймаут 1 с на ответ службы → запасной баннер в окне (Fly может убить владельца шины) | — | QtDBus | интеграция с тестовым сервером уведомлений (python-dbus) |
    88	| `worker/supervisor.py` | `subprocess.Popen([sys.executable,'-m','astra_voice.worker'], pass_fds=[sock])`, len-prefixed JSON, `QSocketNotifier`, таймауты на запрос, политика рестартов 3/10 мин, `SIGTERM` при выходе | очередь запросов (≤ 1 запись в очереди, PRD F2.9) | — | unit с фейковым воркером (эхо-скрипт), убийство процесса → рестарт |
    89	| `worker/__main__.py`, `worker/ipc.py` | Цикл IPC, диспетчер команд, аккуратное завершение | автомат `idle/recording/processing` | audio, engine | unit: автомат с `FakeAudioSource` + `FakeEngine` |
    90	| `worker/audio.py` | `AudioSource`: `PulseSimpleSource` (ctypes `libpulse-simple.so.0`, `pa_simple_new(PA_STREAM_RECORD, spec s16le/16k/1, device)`; поток чтения 20 мс; кольцевой буфер в ОЗУ; RMS/пик; «тишина» = пик < −60 дБ за 2 с; лимит; ретраи открытия 3×300 мс; ошибки `ENODEV/EBUSY`), `WavFileSource` для тестов; список устройств — `pactl list short sources` | буфер в памяти, очищается после результата/отмены | `libpulse0` | unit на `WavFileSource`; интеграция на машине: виртуальный источник PipeWire + `paplay` |
    91	| `worker/vad.py` | Silero VAD ONNX (MIT, ~2 МБ, в пакете): сегментация записей > 20 с по паузам без резки слов (окно 24 с GigaAM), обрезка хвостовой тишины; для ≤ 20 с — не применяется | — | onnxruntime | unit: синтетический wav с паузами → границы |
    92	| `worker/engine.py` | `Engine`-протокол: `load(dir, layout, threads)`, `transcribe(np.float32 16k) -> text`, `unload`; `OnnxAsrEngine` (раскладки §4.2), заглушка `SherpaEngine`; потоки ORT = `min(4, физ. ядер)`; постобработка только strip + опции «пробел в конце»/«заглавная» | модель в памяти воркера; выгрузка по простою (таймер в GUI → `model.unload`) | onnx-asr, onnxruntime | интеграция: тестовый wav (6 с, «проверка связи») → ожидаемые слова на трёх GigaAM; смоук каждой раскладки в CI (кеш моделей) |
    93	| `worker/measure.py` | `VmHWM` и `Pss` из `/proc/self/{status,smaps_rollup}` после первого прогона; отдаёт в GUI → `measurements.json`; сброс при смене ревизии/потоков | — | — | unit на фикстурах `/proc` |
    94	| `models/catalog.py` | Манифест: встроенный (`/usr/share/astra-voice/catalog.json` + `.sig`) → последний скачанный (`~/.local/state/.../catalog.json`); `jsonschema` (`catalog.schema.json`), `manifest_version`, `gpgv`-подпись (наш keyring или `catalog_pubkey`), слияние с локальными замерами и статусами; фильтры «язык», «только отечественные» | `CatalogModel` (`QAbstractListModel`) для QML | verify, store | unit: схема, подпись (тестовый ключ), неизвестные поля игнорируются, старший `manifest_version` → отказ |
    95	| `models/store.py` | `~/.local/share/astra-voice/models/<id>/<revision>/` + `current.json`; staging `<rev>.partial/`; удаление, размер на диске, `shutil.disk_usage` (×1,2), `MemTotal` vs `min_ram_mb` | файлы на диске | paths | unit на tmpdir |
    96	| `models/downloader.py` | Загрузка файлов манифеста: `.part`, `Range: bytes=N-` только при 206 с корректным `Content-Range` (200 → сначала), инкрементальный sha256, `fsync`, `os.replace`, источники `hf → github_release → corp` (429/404/сеть → следующий), скорость/ETA, отмена, очередь = 1, пауза «нет места» | сигналы прогресса в `ModelsBridge` | net.http, store | unit: локальный HTTP-сервер (206/200/обрыв/подмена байтов) |
    97	| `models/installer.py` | Установка из файла/папки (те же sha256), смоук через воркер (`transcribe.file` вшитого wav, `expect_any`), атомарная смена `current.json`, откат при провале, «Повреждена — переустановить», выбор активной («Переключаю…» — старая модель работает до `model.loaded`) | — | store, supervisor | интеграция: подмена байта в encoder → смоук провален → откат |
    98	| `net/http.py` | `requests.Session`: UA `astra-voice/X.Y.Z (+repo)`, connect 2 с / total 3 с для проверок, прокси из env, CA из системного хранилища (+`SSL_CERT_FILE`), TLS не отключается, `ETag`/`If-None-Match`, `Retry-After`/`RateLimit` → backoff, кэш `update-cache.json`, гейт «сеть разрешена?» (тумблеры/офлайн/policy/`HF_HUB_OFFLINE`) | кэш на диске | requests | unit: фейковый сервер 200/304/429/407/таймаут; гейт — таблица истинности |
    99	| `net/hf.py`, `net/github.py` | HF: `GET /api/models/{repo}/refs` → `targetCommit` ветки-варианта (`update_watch.branch`), `HEAD …/resolve/{rev}/file` → `X-Repo-Commit`; поиск по `author` (P2). GitHub: `releases/latest` → `tag_name` (один префикс `v`) → SemVer против `__version__` | — | net.http | unit на записанных JSON-ответах |
   100	| `updates/checker.py`, `updates/track.py`, `updates/app_updater.py` | Расписание (≤ 1/24 ч, случайная задержка 0–30 мин, сначала кэш), автомат строки-статуса (17 состояний), определение трека A/B/«спросить» (`DIGSIG_ELF_MODE` из `/etc/digsig/digsig_initramfs.conf`, `policy zps`, группы `astra-admin/sudo`, `policy updates=admin`), загрузка `deb+SHA256SUMS+.asc` в `~/.cache/astra-voice/updates/`, `gpgv`+sha256+имя+arch+версия, `QProcess('pkexec', helper, 'install', path)`, коды 126/127 → «отменена/нет агента», «Обновить из файла», трек B: папка + `INSTALL-ADMIN.md` | `UpdatesBridge` | net, verify, helper | unit: автомат состояний, выбор трека (таблица), проверка подписи; e2e S9/S10 на машине |
   101	| `security/verify.py` | `Verifier`: `gpgv --keyring <pinned> sig file` (subprocess, `--homedir` пустой), sha256 из `SHA256SUMS`; общий для релизов, deb «из файла», манифеста | — | gpgv | unit: тестовый keyring, подмена файла/подписи |
   102	| `helper/update_helper.py` → `/usr/libexec/astra-voice/update-helper` | Root: только `install <путь>`; `os.open(O_NOFOLLOW)`, копия в `/var/lib/astra-voice/staging/` (0700, файл 0644), `gpgv` (тот же keyring), sha256, `dpkg-deb --field` (Package=astra-voice, Architecture=amd64), `dpkg --compare-versions` (только повышение), `apt-get install --no-remove -y ./x.deb` c `DEBIAN_FRONTEND=noninteractive`, JSON в stdout, коды выхода | не хранит | polkit-action `…astra_voice.update` | unit непривилегированно с фейковыми `gpgv/apt-get` (PATH-подмена); e2e: одно окно пароля (S9-A2) |
   103	| `platform/autostart.py` | `~/.config/autostart/astra-voice.desktop`: `Exec=/usr/bin/astra-voice --hidden`, `X-KDE-autostart-after=panel`, без `OnlyShowIn`; состояние = файл есть и нет `Hidden=true`; чужие ярлыки не трогаем; путь **не переписывается** | файл | paths | unit на tmpdir; e2e S11 |
   285	| **M10 Корпоративный контур** (10.2, 9.4, 5.9, 10.3, 7.3, 7.4, 10.6, 12.4, 10.7) | `policy.conf` + замки, «только отечественные» (6 моделей), корп. URL + `catalog_pubkey` + `latest.json`, трек B + детект ЗПС + вопрос, `INSTALL-ADMIN.md`, документ для админа ИБ, SBOM в релизе, NOTICE/PRIVACY/дисклеймер | S10-A1..A4, S16, F14/F15-acceptance; `profile=secure` → 0 соединений и 6 карточек | `pytest -m unit tests/core/test_policy.py`, `tests/updates/test_track.py`; ВМ с `policy.conf` | согласие на имя (L1) — вне кода, гейт G5 |
   286	| **M11 Полировка** (9.3, 9.6, 11.4, 6.4, 6.5) | Полный английский, клавиатура У13 и кольца фокуса, «Отладка» + сбор диагностики, сигнал «близкая модель», «пропустить ревизию» | DesignReviewer: 24 экрана × 2 темы; `lupdate` без непереведённых; Ц1 — Handy удалён | `pytest -m xvfb` скриншоты; чеклист G5 | — |
   287	| **→ v1.0 31.10** · **v1.1:** ГОСТ-подпись 3 `.so` (`bsign-integrator`), ключ организации, ВМ с ЗПС (12.3, Ц5) | | | | помощник — скрипт, подписывать нечего |
   288	
   289	## 9. Ключевые риски и альтернативы (с обратимостью)
   290	
   291	| # | Риск | Вер./влияние | Митигация | Альтернатива · обратимость |
   292	|---|---|---|---|---|
   293	| R1 | Python-декодер RNN-T в `onnx-asr` не даёт p95 ≤ 0,5 с на Core Ultra 7 | ср./выс. | S3 в первую неделю; потоки ORT; дефолт e2e_ctc | `SherpaEngine` за тем же `Engine` · дни, манифест не ломается |
   294	| R2 | Пилюля крадёт фокус / уходит под окна / чёрные углы без композитора; во Fly другой WM | ср./выс. | S1 на KWin и fly-wm; EWMH руками; re-assert + проверка перекрытия; непрозрачный вариант без композитора | override-redirect (тумблер) · часы |
   295	| R3 | `Ctrl+Space` занят (ibus/fcitx переключают раскладку этой комбинацией на части ALSE) | ср./ср. | `BadAccess` → `not-grabbed` + «Выбрать другую»; парсер `kglobalshortcutsrc`; подсказка `Scroll Lock` | — |
   296	| R4 | Автоповтор X11 и порядок отпускания модификаторов ломают PTT | ср./ср. | автомат следит только за основной клавишей, фильтр пар Release/Press с одним `time`; `XkbSetDetectableAutoRepeat` если доступен в python-xlib | evdev-режим P2 |
   297	| R5 | Klipper игнорирует `x-kde-passwordManagerHint` на 5.27 | ср./низ. | S4; при провале — постоянная подпись-предупреждение (flows §7.6), опция остаётся P1 | — |
   298	| R6 | HF недоступен из РФ / 429 за NAT; GitHub 60 req/ч | выс./ср. | источники по очереди, зеркало в Releases, корп. URL, «Из файла», кэш 24 ч, ETag, случайная задержка, Retry-After | — |
   299	| R7 | Новые колёса ORT поднимут порог glibc или вернут `__isoc23_*` | низ./выс. | пин 1.29.0 по sha256; CI-тест `objdump -T` (max ≤ GLIBC_2.36, нет `isoc23`); при нужде — шим как `patchelf --add-needed` (+1 `.so`) | ORT 1.24.4 (GLIBC_2.27) · минуты |
   300	| R8 | ORT 1.29 несовместим с системным numpy 1.24 в рантайме | низ./выс. | проверка `import` в S3 и CI на Debian 12 | ORT 1.24.4 (numpy ≥ 1.21.6, + `python3-sympy` из apt) · минуты |
   301	| R9 | Полкит-агента нет (Fly) / пользователь не в `astra-admin` | ср./ср. | детект агента; трек B; текст с `sudo apt install ./…` | `pkcon install-local` · часы |
   302	| R10 | T1 потребует помощник-ELF вместо скрипта | низ./ср. | контракт argv/JSON сохраняется | C-помощник · день (+1 ELF под ЗПС) |
   303	| R11 | Шрифтов PT Root UI / PT Mono на ALSE нет (есть PT Astra Sans/Serif/Fact) | выс./низ. | вложить в пакет при допустимой лицензии (проверка legal); резервный стек из `tokens.json` → PT Astra Sans; DesignReviewer сверяет композицию, не метрику | — |
   304	| R12 | Меню трея рисует Plasma (DBusMenu) — стиль спеки §9.2 недостижим | выс./низ. | зафиксировать в спеке как «системное меню»; состав/неактивность — наши | своё QML-меню-поповер · дни (хуже по UX) |
   305	| R13 | Смоук новой ревизии держит две модели в памяти (~830 МБ пик) | ср./низ. | если `MemAvailable` < 2× — выгрузить текущую перед смоуком | — |
   306	| R14 | Гонка WirePlumber на старте — микрофон молчит | выс./ср. | ленивое открытие, ретраи, кнопка перезапуска службы, авто-перепроверка | — |
   307	| R15 | Ключ подписи релизов (CI secret) утёк/потерян | низ./выс. | keyring с двумя ключами (ротация), отзыв через новый релиз с обновлённым keyring, ключ в README | — |
   308	| R16 | QML 5.15: нет `MultiEffect`/`font.features`; `DropShadow` дорог на CPU | ср./низ. | тени только на окне/поповере, PT Mono для цифр; замер CPU пилюли ≤ 3 % при записи | предрендеренные тени PNG · часы |
   309	
   310	## 10. Что тестируем на каждом уровне
   311	
   312	| Уровень | Среда | Что | Инструменты / команда |
   313	|---|---|---|---|
   314	| Unit (CI + локально) | Debian 12, без дисплея | settings/миграции, policy-таблица, автомат хоткея, парсер конфликтов, выбор комбинации вставки по `WM_CLASS`, downloader (206/200/обрыв/подмена), схема и подпись манифеста, SemVer/`tag_name`, выбор трека, помощник (фейки `gpgv`/`apt-get`), статистика p95, `kdeglobals`, IPC-кодек, автомат воркера (`FakeAudioSource`+`FakeEngine`), фильтр логов, ELF-count deb, glibc колеса | `pytest -m unit`, `mypy --strict`, `ruff`, `qmllint` |
   315	| Integration (CI, xvfb / offscreen) | `Xvfb` + `QT_QPA_PLATFORM=xcb`, кэш модели | все QML без warnings + скриншоты состояний (24 × 2 темы) для DesignReviewer; XGrabKey/BadAccess/XTest в тестовое Qt-окно; буфер обмена с восстановлением; single-instance; трей-ветка «недоступен»; движок на 3 раскладках GigaAM с тестовым wav; смоук-откат при подменённом байте; VAD-границы | `pytest -m xvfb`, `pytest -m engine` |
   316	| Integration с виртуальным микрофоном (машина) | ALSE, PipeWire | `paplay` в виртуальный источник → `result`; тишина; лимит; рестарт WirePlumber | `scripts/e2e/virtual_mic.sh` |
   317	| E2E (P21½, машина заказчика, KDE **и** Fly) | реальная сессия | S1–S16 из PRD §6 по чеклисту `docs/test-plan.md`; p95 по 50 диктовкам; `tcpdump`/`ss` хосты; polkit одно окно; автозапуск после перелогина; тема live; Kate/Konsole/LibreOffice/Firefox; Klipper; Alt+Tab; ВМ с `policy.conf` | `scripts/e2e/*.sh` + ручной чеклист Юрки |
   318	| Security (T1/T2/T3) | ревью + тесты | инварианты §6 как тест-кейсы (подмена deb/подписи/манифеста, symlink в staging, аргументы помощника, отсутствие файлов аудио, хосты) | security-analyst по `security-gate` |
   319	
   320	## 11. Вопросы к человеку
   321	
   322	1. **Спайк S3 качает модели с HF** (226 МБ минимум, 680 МБ для трёх вариантов) в scratch-venv — нужно «да» на трафик/диск.
   323	2. **S2 требует одного `sudo`** на машине (положить `.policy` и тестовый помощник) — допустимо ли делать это на машине
   324	   заказчика, или готовим ВМ ALSE 1.8 (её же используем для S1 во Fly-сессии и для чистого прогона S1/Ц3)?
   325	3. Шрифты **PT Root UI / PT Mono** отсутствуют на ALSE: вкладывать в пакет (проверка лицензии ParaType — задача legal)
   326	   или принять резерв PT Astra Sans для UI (метрики макета чуть разойдутся)?
   327	4. Подпись релизов **GPG (Ed25519) через `gpgv`** вместо minisign — принять как решение архитектуры (аргумент: `gpgv` есть
   328	   у любого админа ALSE для трека B; `minisign` в apt Astra нет)? Кто владеет закрытым ключом CI (заказчик или Юрка)?
   329	5. Меню трея под SNI рисует Plasma — согласовать с DesignReviewer, что §9.2 спеки сверяется по составу, не по стилю.
   330	6. Root-помощник как Python-скрипт (ноль своих ELF) — принять, либо T1 потребует C-ELF?
   331	
   332	## 12. Факты среды (проверено read-only 2026-09-09; вход для синтеза и спайков)
   333	
   334	- ALSE 1.8.5 = Debian 12.0 (`/etc/debian_version`), glibc 2.36, Python 3.11.2, `python3-pyqt5` 5.15.9 (модули Core/Gui/
   335	  Widgets/Network/DBus/…; **QtQml/QtQuick не установлены**, пакет `python3-pyqt5.qtquick` 5.15.9+dfsg-1+b9 есть в apt);
   336	  `qml-module-qtquick-controls2` 5.15.8 и `qml-module-qtgraphicaleffects` установлены; `libqt5svg5`, `libqt5x11extras5`,
   337	  `qmllint`/`qmlscene` есть; `qtdeclarative5-dev-tools`, `pyqt5-dev-tools`, `xvfb`, `python3-pytest` 7.2, `python3-mypy` 1.0.1 — в apt.
   338	- В apt Astra есть: `python3-xlib` 0.33, `python3-evdev` 1.6.1, `python3-requests` 2.32.5 (установлен), `python3-jsonschema`
   339	  4.10, `python3-nacl` 1.5, `python3-flatbuffers` 2.0.8, `python3-protobuf` 3.21.12, `python3-packaging` 23.0, `python3-psutil`,
   340	  `gpgv` 2.2.40 (установлен). **Нет:** `python3-sounddevice`, `python3-pyaudio`, `python3-pynput`, `minisign`, `syft`, `python3-semver`.
   341	- Звук: PipeWire 1.4.9 с pulse-протоколом (`pactl` работает, драйвер источников — PipeWire; `pipewire-pulse.service` inactive —
   342	  протокол отдаёт сам pipewire), WirePlumber 0.5.12 активен; `libpulse-simple.so.0` грузится через ctypes; есть `pw-record`,
   343	  `paplay`, `parecord`; источники `alsa_input…Mic1/Mic2` (s32le, 48 кГц).
   344	- Колёса ORT в `~/.cache/uv`: 1.24.4 (max `GLIBC_2.27`, 22 МБ lib) и 1.29.0 (max `GLIBC_2.28`, 28 МБ lib), **0 ссылок
   345	  `__isoc23_*`**, по 3 `.so`; шим из Handy нужен был только prebuilt-бинарю крейта `ort`. `onnx-asr` 0.12.0: `numpy>=1.22.4`,
   346	  `onnxruntime>=1.18.1,!=1.24.1,!=1.25.*,!=1.26.0`; системный numpy 1.24.2 подходит.
   347	- KWin: композитор активен; EWMH поддерживает `_NET_ACTIVE_WINDOW`, `_NET_CLIENT_LIST_STACKING`, `ABOVE`, `SKIP_TASKBAR`
   348	  (4 из 5 проверенных атомов — тип `NOTIFICATION` проверить в S1); `StatusNotifierWatcher` host зарегистрирован;
   349	  `org.freedesktop.Notifications` — Plasma; `polkit-kde-authentication-agent-1` запущен; пользователь в `astra-admin`, `input`.
   350	- ЗПС выключена (`DIGSIG_ELF_MODE=0`, все режимы 0); ключи изготовителя в `/etc/digsig/`.
   351	- `~/.config/kglobalshortcutsrc` (23 КБ): формат `key=combo\tcombo,default,описание` — пример `_launch=Alt+Space\tAlt+F2\tSearch,…,Открыть строку поиска и запуск`.
   352	- Шрифты: PT Astra Sans/Serif/Fact есть; **PT Root UI и PT Mono — нет** (R11).

exec
/bin/bash -lc "nl -ba PRD.md | sed -n '402,499p'; nl -ba arch/plan-codex.md | sed -n '265,300p'" in /home/astra/Документы/astra-voice
 succeeded in 0ms:
   402	### S14. Отмена во время распознавания и очередь (F2)
   403	- **A1.** Given идёт распознавание 20-с фразы, When нажимаю «Отмена» в трее, Then вставки нет, воркер готов к следующей записи ≤ 1 с.
   404	- **A2.** Given идёт распознавание, When начинаю новую запись, Then она принимается, обе вставки приходят по порядку.
   405	
   406	### S15. Диск и удаление модели (F7)
   407	- **A1.** Given свободно 100 МБ, When нажимаю «Скачать» у модели 222 МБ, Then ошибка «Недостаточно места: нужно ~270 МБ, свободно 100 МБ», загрузка не начата.
   408	- **A2.** Given модель установлена и не активна, When «Удалить» → подтверждение, Then папка удалена, статус «Не скачана», размер каталога уменьшился на размер модели.
   409	- **A3.** Given загрузка оборвалась на 60 %, When сеть вернулась и нажато «Повторить», Then продолжается с 60 % (запрос `Range`), итоговый sha256 верен.
   410	
   411	### S16. Удаление утилиты
   412	- **A1.** Given утилита установлена с моделями и автозапуском, When `apt remove astra-voice`, Then пакет удалён, пользовательские данные (`~/.config`, `~/.local/share/astra-voice`) **не** тронуты, ярлык автозапуска остаётся, но не ломает вход (Exec отсутствует → KDE молча пропускает); `apt purge` не трогает домашние каталоги; в «О программе» есть подсказка «Удалить модели и настройки» с путями.
   413	
   414	---
   415	
   416	## 7. Каталог моделей
   417	
   418	### 7.1 Требования к карточке — см. F7 (таблица полей). Протокол честности:
   419	- Одна единица размера во всём UI — **МБ (десятичные)**.
   420	- Полоска качества строится **только** по одному набору (Russian LibriSpeech *test*, бенчмарк onnx-asr, `comparison.md`); пунктуация — отдельный значок, не участвует в полоске; по клику — «Как мы считаем» с источником и датой. **Оговорка в подсказке: «WER бенчмарка измерен на fp32-весах той же модели»** — публичного WER для int8 нет ни у кого; расхождение int8/fp32 считаем малым, пока собственный замер (US-5.4) не покажет иное.
   421	- Полоска скорости — из RTFx **x64 int8** (`benchmarks.md` onnx-asr, столбец `x64 RTFx (int8)`, Ryzen 7 9800X3D); «замерено» перекрывает «бенчмарк»; холодный первый прогон исключается. Три состояния достоверности бенчмарка: `benchmark` (int8 по протоколу) · `benchmark_fp32` (int8-замера нет — T-one, GigaAM Multilingual) · `no_data` (Whisper small: ни WER, ни RTFx по протоколу; FLEURS 11,4 % — другой протокол, только в подсказке; замер в v0.2 по US-5.4). Цифры fp32 из `comparison.md` в полоску **не** подставляются как int8.
   422	- ОЗУ — три уровня достоверности; при смене ревизии модели или числа потоков замер сбрасывается.
   423	- Whisper base показывается с почти пустой полоской качества (WER 38,33 %) и подписью «для русского не рекомендуется — ошибка в каждом третьем слове» — честно, чтобы пользователь не решил, что «утилита плохая».
   424	- Единственный источник цифр §7.2 — `research/catalog-numbers.md` (HF API `?blobs=true`, байты; ревизии `main` на 2026-09-08). Правка цифр — только через него.
   425	
   426	### 7.2 Состав каталога v1 (Must = 9, Should = 1, Could = 2)
   427	Состав — по своду `research/summary.md` §6 и решениям 2026-09-07/08 (Whisper — три позиции; T-one — Should). **Цифры — из `research/catalog-numbers.md`** (источник истины, 2026-09-08): диск — сумма точных байт файлов, нужных рантайму, по `GET /api/models/{repo}?blobs=true`, **десятичные МБ** (в ревизии 2 девять позиций были в МиБ); WER — Russian LibriSpeech test, onnx-asr `comparison.md` (**fp32-веса**); RTFx — x64 **int8**, `benchmarks.md` (Ryzen 7 9800X3D). Где у модели два экспорта (sherpa-onnx / onnx-asr), указаны оба размера — рантайм v1 выбирают архитекторы, в манифест попадает один. ОЗУ — оценка ×1,85 от int8-весов (разброс 1,5–3×), к fp32 (T-one) неприменима; заменяется замером (US-5.4).
   428	| # | Модель · вариант | Назначение (текст карточки) | Диск, МБ | WER ru (fp32) | RTFx x64 int8 | ОЗУ (уровень) | Пункт. | Язык | Лицензия | Происх. (по юрлицу) | Приор. |
   429	|---|---|---|---|---|---|---|---|---|---|---|---|
   430	| 1 | **GigaAM v3 RNN-T** (e2e_rnnt int8) — по умолчанию | Русская диктовка с пунктуацией. Рекомендуем | **231,9** (sherpa: csukuangfj `…transducer-punct-…v3…`) / 226,4 (onnx-asr: istupakov `gigaam-v3-onnx`) | 7,60 % | 42,5 | 415 МБ (замерено, эталон) | да | ru | MIT · GigaChat Team, 2024 | Сбер — отеч. | Must |
   431	| 2 | GigaAM v3 CTC (e2e_ctc int8) | То же, быстрее на ~25 %, точность чуть ниже | 224,9 | 7,80 % | 52,2 | ~415 МБ (оценка) | да | ru | MIT | Сбер — отеч. | Must |
   432	| 3 | GigaAM v3 RNN-T без пунктуации (rnnt int8) | Максимальная точность слов, без знаков препинания | 229,3 | **4,39 %** | 42,8 | ~425 МБ (оценка) | нет | ru | MIT | Сбер — отеч. | Must |
   433	| 4 | GigaAM Multilingual CTC 220M (int8) | Русский + казахский/киргизский/узбекский/английский; без пунктуации | 224,8 (istupakov) | 8,43 % | **по бенчмарку fp32: 58,5** (int8-замера нет) | ~415 МБ (оценка) | нет | ru, kk, ky, uz, en | MIT | Сбер — отеч. | Must |
   434	| 5 | T-one (Т-Банк) | Русская, лёгкая, без пунктуации; хорошая точность | 144,2 (**fp32 — int8-весов не существует**) | 6,57 % | **по бенчмарку fp32: 26,3** (int8 N/A) | нет оценки (fp32; замер в v0.2) | нет | ru | Apache-2.0 | T-Tech / АО «ТБанк» — отеч. | **Should** |
   435	| 6 | Vosk ru 0.54 (int8) | Лёгкая полная Vosk; точность ниже GigaAM | 72,5 | 9,89 % | 70,5 | ~135 МБ (оценка) | нет | ru | Apache-2.0 · Alpha Cephei Inc. | **зарубежная · команда из России** (Alpha Cephei Inc., Делавэр, США); **вне фильтра «только отечественные»** | Must |
   436	| 7 | Vosk small ru 0.52 (int8) | Для слабых машин и малого диска | 26,7 | 14,53 % | 83,5 | ~50 МБ (оценка) | нет | ru | Apache-2.0 · Alpha Cephei Inc. | зарубежная · команда из России; вне фильтра | Must |
   437	| 8 | Whisper large-v3-turbo (int8) | Многоязычная, качественно, **медленно на CPU** | 1 089,1 (onnx-community, бенчмарчен) / 1 036,6 (sherpa) | 10,10 % | **3,9** | ~2 ГБ (оценка) | да | 99 языков | **MIT** (`openai/whisper-large-v3-turbo`) | OpenAI (США) — зарубеж. | Must |
   438	| 9 | Whisper small (int8) | Многоязычная, средняя; по-русски слабее GigaAM | 253,5 (onnx-community — единственный экспорт под onnx-asr) / 375,5 (sherpa) | **нет данных по протоколу** (FLEURS 11,4 % — другой протокол, только в подсказке; оценка ≈20 % — не показывать) | **нет данных** (оценка ≈13–16 — не показывать); замер в v0.2 (US-5.4) | ~470 МБ (оценка) | да | 99 языков | **Apache-2.0** (`openai/whisper-small`) | OpenAI — зарубеж. | Must |
   439	| 10 | Whisper base (int8) | Быстрая и лёгкая; **для русского не рекомендуется — ошибка в каждом третьем слове** | 109,1 (istupakov `whisper-base-onnx`, бенчмарчен) / 160,6 (sherpa) | **38,33 %** | 51,6 | ~200 МБ (оценка) | да | 99 языков | **Apache-2.0** (`openai/whisper-base`) | OpenAI — зарубеж. | Must |
   440	| 11 | GigaAM Multilingual Large CTC (int8) | «Качество любой ценой»: точнее v3, тяжёлая, без пунктуации | 591,6 (istupakov) | **5,55 %** | по бенчмарку fp32: 30,4 (int8-замера нет) | ~1,1 ГБ (оценка) | нет | ru, kk, ky, uz, en | MIT | Сбер — отеч. | Could |
   441	| 12 | NeMo FastConformer ru pc (CTC) | Самая быстрая, с пунктуацией, точность ниже | 131,6 (CTC) / 136,4 (RNN-T-декодер: WER 11,57 %, RTFx 54,2) | 13,10 % | 70,6 | ~245 МБ (оценка) | да | ru | CC-BY-4.0 (атрибуция обязательна) | NVIDIA (США) — зарубеж. | Could |
   442	Ревизии `main` всех репозиториев на 2026-09-08 и точные байты — в `research/catalog-numbers.md` §1; манифест закрепляет их как `sources[].revision`. Whisper-репозитории `csukuangfj/sherpa-onnx-whisper-*` **без файла лицензии** — юридически опираемся на карточки `openai/whisper-*` (NOTICE по моделям раздельно, §9.6). Исключены: Silero (CC-NC), Canary/Parakeet (слабы по-русски, CC-BY), GGUF/transcribe.cpp (нужен C++-движок), Whisper medium/large-v3 (непригодны по скорости на CPU — решение 2026-09-07), fp32-варианты GigaAM (885 МБ, без выигрыша). **Рантайм v1 (onnx-asr или sherpa-onnx) и раскладка файлов — решение архитекторов**, PRD не выбирает; условие неизменно: источник весов соответствует рантайму (`onnx-asr` ↔ istupakov/onnx-community; `sherpa-onnx` ↔ csukuangfj), пресеты держим у себя с закреплённым коммитом и sha256.
   443	
   444	### 7.3 Формат манифеста каталога (`catalog.json`, версия 1)
   445	```json
   446	{
   447	  "manifest_version": 1,
   448	  "generated_at": "2026-09-07T12:00:00Z",
   449	  "publisher": "astra-voice",
   450	  "models": [
   451	    {
   452	      "id": "gigaam-v3-e2e-rnnt-int8",
   453	      "name": "GigaAM v3 RNN-T",
   454	      "vendor": "Сбер (GigaChat Team)",
   455	      "family": "gigaam-v3", "variant": "e2e_rnnt",
   456	      "engine": "sherpa-onnx", "layout": "sherpa-nemo-transducer",
   457	      "purpose": {"ru": "Русская диктовка с пунктуацией — по умолчанию", "en": "…"},
   458	      "languages": ["ru"], "punctuation": true,
   459	      "origin": {"kind": "ru", "holder_entity": "ПАО Сбербанк (РФ)", "note_ru": null},
   460	      "license": {"spdx": "MIT", "holder": "GigaChat Team, 2024", "url": "https://…/LICENSE", "attribution_required": false},
   461	      "recommended": true, "min_ram_mb": 1024,
   462	      "disk_bytes": 231897202,
   463	      "ram_peak_mb": {"value": 415, "kind": "measured", "cpu": "Intel Core Ultra 7 255U", "threads": 2, "at": "2026-09-04"},
   464	      "quality": {"kind": "benchmark", "wer_pct": 7.60, "dataset": "Russian LibriSpeech test", "weights": "fp32", "runtime": "onnx-asr", "source_url": "https://github.com/istupakov/onnx-asr/blob/main/docs/comparison.md"},
   465	      "speed": {"kind": "benchmark", "rtfx": 42.5, "weights": "int8", "cpu": "Ryzen 7 9800X3D", "source_url": "https://istupakov.github.io/onnx-asr/benchmarks/"},
   466	      "files": [
   467	        {"path": "encoder.int8.onnx", "size": 224570820, "sha256": "369f35a7…"},
   468	        {"path": "decoder.onnx", "size": 4600132, "sha256": "38fc7475…"},
   469	        {"path": "joiner.onnx", "size": 2712896, "sha256": "602ff701…"},
   470	        {"path": "tokens.txt", "size": 13354, "sha256": "…"}
   471	      ],
   472	      "sources": [
   473	        {"type": "hf", "repo": "csukuangfj/sherpa-onnx-nemo-transducer-punct-giga-am-v3-russian-2025-12-16", "revision": "a6039be7cee829a9044a69ac0ebaf1c191217c97"},
   474	        {"type": "github_release", "base": "https://github.com/ArrivaRUS/astra-voice/releases/download/models-2026.09/"},
   475	        {"type": "corp", "base": "{corp_base}/models/gigaam-v3-e2e-rnnt-int8/"}
   476	      ],
   477	      "update_watch": {"hf_repo": "ai-sage/GigaAM-v3", "branch": "e2e_rnnt", "authors": ["ai-sage", "csukuangfj", "istupakov"], "family_keywords": ["giga-am", "gigaam"]},
   478	      "related": ["gigaam-v3-e2e-ctc-int8", "gigaam-v3-rnnt-int8"],
   479	      "smoke_test": {"wav": "builtin:test-ru-6s.wav", "expect_any": ["проверка", "связи"]}
   480	    },
   481	    {
   482	      "id": "whisper-small-int8",
   483	      "name": "Whisper small", "vendor": "OpenAI",
   484	      "engine": "onnx-asr", "layout": "onnx-community-whisper",
   485	      "languages": ["*"], "punctuation": true,
   486	      "origin": {"kind": "foreign", "holder_entity": "OpenAI (США)", "note_ru": null},
   487	      "license": {"spdx": "Apache-2.0", "holder": "OpenAI", "url": "https://huggingface.co/openai/whisper-small", "attribution_required": false},
   488	      "disk_bytes": 253466497,
   489	      "ram_peak_mb": {"value": 470, "kind": "estimated"},
   490	      "quality": {"kind": "no_data", "note_ru": "По протоколу данных нет; FLEURS 11,4 % — другой протокол"},
   491	      "speed": {"kind": "no_data"},
   492	      "files": [{"path": "encoder_model_int8.onnx", "size": 92326127, "sha256": "…"}, {"path": "decoder_model_merged_int8.onnx", "size": 156750845, "sha256": "…"}],
   493	      "sources": [{"type": "hf", "repo": "onnx-community/whisper-small", "revision": "36050c46d777d46dc4b5f43f6d90574fc38f8732"}]
   494	    }
   495	  ]
   496	}
   497	```
   498	Правила: **`engine`** ∈ {`onnx-asr`, `sherpa-onnx`} и **`layout`** (именованная раскладка файлов: `sherpa-nemo-transducer`, `sherpa-nemo-ctc`, `onnx-asr-gigaam-v3`, `onnx-asr-gigaam-multilingual`, `onnx-community-whisper`, `sherpa-whisper`, `vosk-onnx`, `onnx-asr-nemo` — список закрывают архитекторы) обязательны на каждую модель; репозиторий и файлы записи — из таблицы `research/catalog-numbers.md` §1 для выбранного рантайма (в v1 поставляется один рантайм, манифест содержит записи только для него; второй пример выше показывает раскладку onnx-asr). `quality.kind`/`speed.kind` ∈ {`measured`, `benchmark`, `benchmark_fp32`, `no_data`}; `quality.weights` фиксирует, на каких весах измерен WER (в бенчмарке onnx-asr — `fp32`); при `no_data` полоска не рисуется. `origin.kind` ∈ {`ru`, `foreign`} — по юрлицу правообладателя, `note_ru` — уточнение для карточки («команда из России»). `sha256` обязателен для каждого файла (для LFS — из HF API: `lfs.sha256` в `GET /api/models/{repo}?blobs=true` или `lfs.oid` в `/api/models/{repo}/tree/{rev}` — одно и то же значение; для не-LFS файлов вроде `tokens.txt` API даёт git-SHA-1, поэтому sha256 считается при публикации манифеста); `sources[].revision` обязателен, загрузка идёт по `/resolve/{revision}/…`, не по `main`; `update_watch.branch` — ветка-вариант (для `ai-sage/GigaAM-v3` следим за `targetCommit` ветки, не `main`); манифест сопровождается отсоединённой подписью `catalog.json.sig` (тот же инструмент и ключ, что у релизов утилиты — minisign или GPG; все зеркала проверяются одним закреплённым ключом); корпоративный манифест — тот же формат, `sources` может содержать только `corp`; поля `ram_peak_mb.kind` ∈ {`measured`, `claimed`, `estimated`}; локальные замеры хранятся отдельно (`~/.local/state/astra-voice/measurements.json`) и перекрывают манифест в UI; неизвестные поля игнорируются (forward-compat), `manifest_version` > поддерживаемой → отказ с сообщением.
   499	
   265	| Cancel не укладывается в 1 с | RunOptions + точки отмены; проверить каждый декодер. Kill/reload — только аварийное восстановление | Патч ограничен adapter/vendor; критерий не ослабляется незаметно |
   266	| Две модели вызывают OOM | Проверка MemAvailable и пика кандидата; при нехватке сохранить старую и отказать в переключении с объяснением | Данные/активная ревизия не меняются |
   267	| Пилюля теряет верхний слой | Повторное применение EWMH hints по событиям; при необходимости X11 bypass-вариант после проверки ×/focus | Один platform adapter |
   268	| QtQuick software backend не рисует эффекты | Кэшированные тени/иконки; анимации только видимых элементов | Не меняет контроллеры и токены |
   269	| WirePlumber restart не помогает | Отличать mute/нет устройства/профиль/ошибку открытия. Предложить диагностику; не менять fly-dm, linger и системные службы автоматически | Только пользовательская служба по кнопке |
   270	| Clipboard/Klipper гонки | Проверка владельца и поколения; настраиваемая задержка; «оставлять текст»/«только буфер» | Настройка, без смены ASR |
   271	| HF SHA постоянно даёт ложное обновление | Раздельные координаты upstream/export; установка только подписанного набора | Миграция update-cache |
   272	| Подмена deb между GUI и helper | Проверка после копирования в root-staging; helper не доверяет флагу «GUI проверил» | Изолированный updater |
   273	| Обрыв apt | Сохранять журнал и системный результат; не обещать транзакционный rollback deb и не запускать автоматический downgrade | Восстановление штатным apt администратором |
   274	| Отозванный/скомпрометированный ключ | Явные fingerprints, версия доверенного набора ключей; ротация через доверенный выпуск/администратора | Отдельный keyring; URL не назначает доверие |
   275	| RAM-only ошибочно трактуется как защита от swap | Нет аудиофайлов; core dumps отключены, audio mappings исключены из dumps. Строгая защита от swap/hibernation зависит от политики ОС | Не требует вмешательства приложения в настройки ОС |
   276	| «Отечественные модели» принимают за сертификацию | Фильтр по принятому юрлицу; NOTICE и документ влияния на среду. Допуск в систему — процедура администратора | Состав каталога управляется подписанным манифестом |
   277	
   278	| Уровень тестов | Обязательное покрытие |
   279	|---|---|
   280	| **Unit** | Автоматы состояний, очередь/cancel, миграции и policy, SemVer/Debian version, вычисление метрик, схема каталога, пути, подписи, Range/cache/backoff |
   281	| **Integration** | Реальный worker и ORT; виртуальный PipeWire-микрофон + paplay; HTTP fault server; D-Bus stubs; Xvfb clipboard; crash и заполнение диска |
   282	| **Packaging/security** | Чистый install/upgrade/remove/purge; полный ELF-инвентарь; отсутствие сети в postinst; TOCTOU/symlink/неверный ключ/чужой пакет; T2 на изменениях updater |
   283	| **E2E на ALSE** | KDE и Fly; реальные хоткеи/фокус/трей/polkit/Klipper; Kate/Firefox/LibreOffice/Konsole/fly-term/xterm; перезагрузка, USB, сеть через proxy/CA |
   284	| **Performance/soak** | GUI/RSS/PSS/RTFx; 50 диктовок; 8 часов работы; циклы отмены и смены модели; отсутствие роста FD/памяти |
   285	| **v1.1 отдельно** | Установка и диктовка с настоящей ЗПС и подписанным комплектом; имитация `DIGSIG_ELF_MODE` в v1 проверяет только маршрутизацию UI |
   286	
   287	| Инвариант | Где обеспечивается |
   288	|---|---|
   289	| Аудио и распознанный текст не отправляются в сеть и не попадают в логи/диагностику | NetworkService не принимает эти типы данных; тесты с контрольными маркерами |
   290	| Запись начинается только по действию пользователя и имеет видимый индикатор | Controller; при потере всех видимых индикаторов запись останавливается |
   291	| Worker не скачивает модели, не выполняет repo-код и не регистрирует произвольные custom-op `.so` | RuntimeAdapter, локальный resolver, закрытые layouts |
   292	| Неподписанный каталог и непроверенные файлы не становятся активными | Registry/Activator, атомарные транзакции |
   293	| Policy применяется к операциям, а не только к состоянию кнопок | Общий EffectivePolicy для GUI, CLI и updater |
   294	| Root-helper не принимает URL, shell-команду, произвольный keyring или параметры apt | Узкий протокол helper; root-owned конфигурация |
   295	| ЗПС и admin-track нельзя обойти через «Обновить из файла» | Повторная проверка трека в GUI и helper |
   296	| Отмена/крах/смена поколения не приводят к поздней вставке | ID сессии и поколения, единая очередь PasteService |
   297	| Вставка не синтезирует Enter и не начинается без проверенного окна назначения | PasteService; при невозможности назначения — только буфер |
   298	| Установленная рабочая ревизия не удаляется до подтверждения новой | ModelRegistry и журнал транзакции |
   299	| Системные СЗИ, fly-dm, группы пользователя и чужой автозапуск не меняются | Ограниченная область записи пакета/helper |
   300	| В v1 нет обещания готовности неподписанных ELF к ЗПС | INSTALL-ADMIN и трек B; ГОСТ-подпись и фактическая приёмка — v1.1 |

exec
/bin/bash -lc 'cat decisions/log.md' in /home/astra/Документы/astra-voice
 succeeded in 0ms:
# Журнал решений

> Что решили и **почему**. Новые записи — сверху. Пишет Юрка после явного «да» человека
> (скилл `decision-log`).

## 2026-09-09 — Требование заказчика: работа в KDE Plasma и в родном Fly ALSE
- Контекст: заказчик (чат 2026-09-09): «хочу, чтобы утилита работала на ALSE под KDE, как сейчас, и в родном Fly ALSE».
  PRD §8 упоминал «KDE Plasma 5.27 и Fly» одной строкой, без разбора отличий Fly.
- Решение: **Fly-сессия ALSE 1.8 — обязательная целевая среда v1.0 наравне с KDE.** Архитекторы обязаны разобрать
  отличия Fly явно: оконный менеджер fly-wm (EWMH: StaysOnTop / DoesNotAcceptFocus / skip-taskbar для пилюли),
  панель и трей (SNI vs XEmbed), уведомления (fly-notifications; известно, что модуль убивает владельца шины
  org.freedesktop.Notifications), polkit-агент Fly, автозапуск (XDG autostart в Fly), детект темы (без kdeglobals —
  тема Fly), буфер обмена без Klipper, хоткеи/вставка. Спайк S1 — в обеих сессиях; E2E (P21½) — в обеих; PRD — сценарий
  S17 «Fly-сессия» (ревизия PM).
- Почему: у корпоративных пользователей ALSE по умолчанию Fly; KDE — выбор заказчика на его машине.
- Гейт: — · «да» заказчика (само требование). Последствия: дополнение к плану #1 → синтез → челлендж (при необходимости
  точечный второй круг по Fly) → execution-pack с задачами Fly в M0/M4/M9.

## 2026-09-08 — G3: полный макет A + дизайн-спека 0.2 + токены 2.1 приняты
- Контекст: UXAnalyst развернул A «Панель» в 48 экранов (~190 состояний), DesignEngineer снял 596 токенов
  и черновик спеки, Юрка сверил размеры с рендером (окно 900×620, сайдбар 184, строка 38/55, пилюля 36,
  min 172) и закрыл 14 «уточнить». Чеклист приёмки G3 — 11/11.
- Решение: **макет `design/mockups/final/`, спека `design/spec.md` (0.2), токены `design/tokens.json` (2.1.1),
  референсы `design/refs/` — источник истины для разработки и DesignReviewer.** Правки дальше — через
  журнал решений и ревизию спеки.
- Почему: «да» заказчика (чат 2026-09-08); контрасты текста ≥ 4,5:1, состояния не только цветом.
- Гейт: G3 · «да» заказчика.
- Последствия: Фаза 3 — двойной архитектор (architect-claude Fable ∥ architect-codex GPT-6 Astra) → суд
  и синтез → челлендж Astra → T1 модель угроз (обновлятор, загрузка моделей, polkit) → execution-pack.
  Вне спеки до кода: глиф `clock` (UX, в работе), два звука .ogg, `Theme.qml` генерировать из tokens.json.

## 2026-09-08 — Решения Юрки по «⚠ уточнить» спеки (У1–У14, рутинные)
- У1 кольцо фокуса: светлая тема — `accent-ink #0A776A` (5,16:1), тёмная — `#2FD9C4`; чистая бирюза в
  светлой не видна (2,2:1). У2 pressed в тёмной — ступень 7 %, чтобы подпись держала ≥ 4,5:1.
- У4 ширина: колонка контента растёт до 760 px, дальше центрируется. У5 строка с тумблером кликабельна
  целиком (как в KDE); строки со списком/кнопкой — только контрол.
- У13 клавиатура: Tab — сайдбар → строки контента сверху вниз → строка-статус; стрелки в сайдбаре;
  Ctrl+PgUp/PgDn — разделы; Esc закрывает диалог/поповер; Space/Enter активируют контрол.
- У3 прокрутка — overlay-скроллбар 8 px, AsNeeded. У6 кнопка `loading` — спиннер вместо иконки, текст
  остаётся, disabled. У7 список — вниз, максимум 6 пунктов (228 px), при нехватке места — вверх.
- У8 глиф `clock` добавить в набор (UX). У9 `loading-model` — вращающийся спиннер, 1 оборот/с, линейно.
- У10 пилюля — по центру снизу **рабочей области** (availableGeometry: без панели KDE) монитора активного
  окна, 48 px от её края (сверху — симметрично); без активного окна — основной монитор. (Уточнение
  2026-09-08 после замечания design-engineer: панель выше 48 px перекрывалась бы пилюлей.) У11 онбординг пропускаемый по экранам, но завершение требует
  установленной модели (S1). У12 диалоги модальны к окну настроек и центрируются над ним; уведомления —
  штатные KDE. У14 звуки — два коротких тона (старт: восходящие, конец: нисходящие, ≤ 120 мс, −18 dBFS),
  по умолчанию выключены; файлы .ogg в комплекте.
- Гейт: — (рутинные); войдут в спеку на G3.

## 2026-09-08 — Решения Юрки по цифрам каталога (research/catalog-numbers.md)
- «Отечественная» модель определяется **по юрлицу правообладателя** (контекст Указа № 166/КИИ):
  Vosk (Alpha Cephei Inc., Делавэр, США; команда — РФ) → **зарубежная**, в фильтр «только отечественные»
  не попадает; подпись карточки: «зарубежная · команда из России».
- Размеры в каталоге — **десятичные МБ по байтам HF API** (PRD §7.2 и макет правятся: 9 из 12 позиций).
- WER/RTFx — только по протоколу §7.1 (onnx-asr, Russian LibriSpeech, x64 int8); оговорка в UI-подсказке:
  «WER бенчмарка измерен на fp32-весах». Позиции без int8-бенчмарка (T-one fp32, GigaAM Multilingual)
  — полоска «по бенчмарку fp32»; **Whisper small — «нет данных по протоколу»** (FLEURS 11,4 % — другой
  протокол, только в подсказке) до собственного замера в v0.2 (US-5.4).
- Whisper лицензии — по моделям: large-v3-turbo MIT, small и base Apache-2.0 (NOTICE — раздельно).
- T-one остаётся Should (fp32 144 МБ, RTFx 26,3; ОЗУ — «оценка», правило ×1,85 неприменимо).
- Рантайм v1 (onnx-asr vs sherpa-onnx) и раскладка файлов манифеста — решение архитекторов; пример
  манифеста в PRD §7.3 должен быть согласован с выбранной раскладкой (поле `engine` + `layout` на модель).
- Гейт: — (рутинные). Последствия: PRD ревизия 3 (§7.2/§7.3/NOTICE), макет каталога — правка цифр.

## 2026-09-08 — Решения Юрки по токенам (вопросы design-engineer В1–В7, конфликты К1–К12)
- Hover/pressed/focus: задать системно — hover = поверхность на ступень темнее/светлее (surface-2),
  pressed = ещё ступень; primary hover/pressed = затемнение 8 % / 14 %; focus — кольцо 2 px accent
  снаружи; пункт сайдбара и карточка — тот же принцип. Значения фиксирует design-engineer в токенах.
- Easing: вход — ease-out (cubic 0.2/0/0/1) 160 мс, выход — ease-in 120 мс, hover 120 мс, уровень звука
  в пилюле — линейно; «Готово» 500 мс. Пульсация: **пилюля «распознаю» пульсирует** (PRD F3.2), **трей
  статичен** (brand-basics §2.4) — оба верны, конфликт К4 снят разделением.
- Контрасты Р1–Р3, Р5 — исправить сдвигом hex/заменой opacity на цвет, композицию не трогать;
  Р4 (трей на светлой панели) — принять: смысл несут форма и подсказка (PRD §8), цвет — подсказка.
- К11 — строка настройки 38 px (как в финальном макете `_shell.py`), базовая 44 px устарела.
- К2 — расширенная шкала кеглей принимается (документировать как шкалу v1). К3 — fg-disabled в тёмной
  теме должен отличаться от fg-muted. К10/В7 — **иконки свои** (20 контуров, currentColor, в комплекте
  приложения), Breeze не зависимость. В5 — кнопка danger в v1 не нужна (деструктив — primary в диалоге
  с текстом-предупреждением). В3 — минимум окна 900×620.
- Гейт: — (рутинные, без «да»); всё это войдёт в спеку на G3.

## 2026-09-08 — Решения Юрки по вопросам полного макета (рутинные, без гейта)
- Vosk (Alpha Cephei): происхождение «уточняется», в фильтр «только отечественные» не попадает, пока
  Researcher не подтвердит (задача на спайк каталога).
- «Проверить обновления» в трей-меню всегда активно: ручная проверка — явное действие пользователя
  (как «Проверить сейчас» в разделе «Сеть»); неактивно только в офлайн-режиме/по policy. PRD F12 поправлен.
- Тумблер VAD в «Продвинутых» — вне v1 (VAD автоматический, без настройки).
- Минимальный размер окна настроек — 900×620 (minimumWidth/Height), ниже карточка модели ломается.
- Проверка макета Юркой: 10 экранов отрисованы; два дефекта вёрстки (кнопки диалога NOTICE, перенос
  заголовка статистики) — раунд правок 1 из 2.

## 2026-09-08 — G2: направление интерфейса — A «Панель»
- Контекст: UXAnalyst сдал `design/flows.md` (S1–S16, все состояния, 19 сообщений об ошибках) и три
  направления × 10 экранов (`design/mockups/directions/`): A «Панель» (сайдбар), B «Лента» (одна колонка),
  C «Пульт» (поповер от трея + второй уровень). Юрка проверил рендер 13 ключевых экранов.
- Решение: **A «Панель»** — окно 900×620, левый сайдбар из 6 разделов (Общие · Модели · Вывод ·
  Сеть и обновления · Продвинутые · О программе) + «Отладка» внизу, строки «заголовок · ? · контрол»,
  строка-статус внизу «модель слева · обновление справа · версия». Плюс: **пилюля резиновая под текст**
  (не фиксированные 172 px), **каталог показывается целиком с прокруткой** (фильтры «язык» и «только
  отечественные»), **подтверждение «Готово» в пилюле — 500 мс** (PRD F3.2: было 300 мс — правка).
- Почему: выбор заказчика (чат 2026-09-08); рекомендация UX — канон категории, дешевле всего, предсказуемо для админа.
- Гейт: G2 · «да» заказчика.
- Последствия: разворот A в полный макет (все экраны и состояния, светлая+тёмная) → токены
  (design-engineer) → дизайн-спец (Юрка) → ⛔ G3 одной приёмкой.

## 2026-09-07 — G1: PRD 0.2 + истории + беклог приняты
- Контекст: ProductManager сдал PRD 0.2 (ревизия 2): 16 модулей F1–F16, 16 сценариев S1–S16 (71 GWT),
  каталог 10 Must + 2 Could, манифест `catalog.json` v1, два трека обновлений, профиль «защищённый
  контур», гипотеза с проверкой 2026-11-15; 79 историй; беклог RICE/MoSCoW.
- Решение: **PRD, stories, backlog версии 0.2 приняты** как договор о том, что строим. Релизы:
  v0.1 «Диктовка работает» (2026-09-30) → v0.2 «Каталог, обновления, автозапуск» (2026-10-15) →
  v1.0 «Корпоративный контур и полировка» (2026-10-31); ГОСТ-подпись/ЗПС-пилот — v1.1.
- Почему: требования заказчика №1–5 покрыты; спорные развилки закрыты ответами заказчика (см. записи выше).
- Гейт: G1 · «да» заказчика (чат 2026-09-07).
- Последствия: старт дизайна (UX-флоу → 2–3 направления → G2 → токены → дизайн-спец G3); дальнейшие
  изменения скоупа — только через журнал решений и ревизию PRD.

## 2026-09-07 — G2a закрыт: лого-пак знака 06 «Слоги» принят к работе
- Контекст: доводка после выбора заказчика; замечание Юрки о похожести на Breeze «⋯/загрузка».
- Решение: финальный знак — `design/brand/logo.svg` (имя в кривых), мастер-сетка 100×51,72; точки
  1 : 1,38 : 1,90, промежутки 2,83 : 1, строка шире ряда в 1,23; **цвет состояния несёт только третья
  точка** на всех размерах; 16 px нарисован отдельно. Пак: hicolor 16…512 + scalable, трей 22/16 × 6
  состояний по соглашению KDE, фавикон, README-марка. Источник истины — `brand-basics.md` §4–5.
- Почему: похожесть с системными иконками снята пятью правками геометрии без смены идеи.
- Гейт: G2a · «да» заказчика на направление и на лого-пак («Лого-пак принял», чат 2026-09-07 18:10).
- Последствия: вход для `design-engineer` (токены) и `ux-analyst` (макеты).

## 2026-09-07 — Ответы заказчика на открытые вопросы PRD (§12.1) — вход для ревизии PRD
- Контекст: ProductManager сдал PRD/истории/беклог с шестью вопросами, меняющими продукт.
- Решения заказчика (чат 2026-09-07):
  1. **Сетевые проверки на первом экране — тумблер ПУСТ**, пользователь включает сам (рекомендация
     юриста); во всех профилях. Требования №1/№4 выполняются после явного включения.
  2. **Модель держим в памяти постоянно** (≈415 МБ); выгрузка по простою — опция в настройках.
  3. **ЗПС-трек (ГОСТ-подпись, ВМ с ЗПС, цель Ц5) — после v1.0.** В v1.0 остаётся детект
     `DIGSIG_ELF_MODE` и ветка «скачать пакет и передать администратору». Уточняет решение
     «оба режима сразу» от 2026-09-07: второй режим в v1 — без подписи артефактов.
  4. **Автозапуск включён по умолчанию** на последнем экране онбординга; **переносится в v0.2**.
- Решения Юрки (рутинные): хоткей по умолчанию — `Ctrl+Space` (push-to-talk, как в Handy заказчика);
  Whisper в каталоге — три позиции (large-v3-turbo / small / base), medium и large-v3 не включаем.
- Гейт: G1 (в процессе приёмки) · «да» заказчика по каждому пункту.
- Последствия: ревизия 2 PRD/историй/беклога; v1.0 = требование №5 + полировка + комплаенс без Ц5.

## 2026-09-07 — G2a (часть 3): знак — 06 «Слоги»
- Контекст: раунд 2 (последний по лимиту) — восемь направлений (01/01b, 03/03b, 05–08); дизайнер
  рекомендовал 05 «Абзац», безопасный — 03b; Юрка отметил риски: 07 читается как тумблер, 06 — как
  значок «ещё/загрузка», 01b — как скобки кода.
- Решение: **06 «Слоги»** — три точки с сужающимися промежутками (дискретная речь) над сплошной строкой
  (непрерывный текст). Доводка: усилить отличие от «⋯/загрузка» в трее (ритм промежутков, вес строки),
  логотип с текстом в кривых, лого-пак (hicolor PNG 16…512 + SVG, моно, фавикон, 5 трей-состояний).
- Почему: выбор заказчика (чат 2026-09-07) — строгая геометрия без «звуковой волны».
- Гейт: G2a · «да» заказчика. Лимит раундов исчерпан — дальше только доводка внутри направления.
- Последствия: UX-макеты и токены берут знак 06 и палитру brand-basics; `design/brand/logo.svg` — источник истины.

## 2026-09-07 — Лицензия репозитория: GPL-3.0-or-later (вместо предварительной MIT)
- Контекст: правовое заключение (`research/legal.md`): PyQt5 доступен только под GPL v3 / коммерческой
  лицензией Riverbank → утилита на PyQt5 обязана распространяться под GPL v3; MIT в репо некорректна.
  Заказчик 2026-09-07 подтвердил, что GPLv3 допустима.
- Решение: `LICENSE` = GPL-3.0-or-later; атрибуции моделей/движков — в `NOTICE` (sherpa-onnx §4(d) Apache-2.0,
  Whisper MIT/Apache-2.0 — обе, CC-BY-модели — с указанием).
- Почему: единственный полный Qt-биндинг для Python в apt Astra 1.8 — PyQt5; закрытая поставка не нужна.
- Гейт: — · «да» заказчика (ответ «GPLv3 допустима»).
- Последствия: сторонние вклады принимаются под GPL; при смене стека на LGPL (PySide6/GTK4) лицензию можно ослабить.

## 2026-09-07 — Рамки продукта до PRD: аудитория/ЗПС, лицензия, индикация записи
- Контекст: ресёрч платформы показал, что при включённой ЗПС (digsig) самообновление «одной кнопкой»
  невозможно (подпись ELF и .so ГОСТ-ключом, регистрация ключа админом с перезагрузкой); единственный
  полный Qt-биндинг для Python в apt Astra 1.8 — PyQt5 (GPLv3); канон индикации записи — пилюля поверх окон.
- Решение (три ответа заказчика, чат 2026-09-07):
  1. **Оба режима обновления в v1**: без ЗПС — GitHub Releases → .deb → polkit → apt из GUI;
     с ЗПС — уведомление + скачивание пакета «передать администратору»; релизы подписываются
     (ГОСТ-подпись пакетов и вендоренных .so — отдельная веха), проверка на ВМ с ЗПС обязательна.
  2. **GPLv3 допустима** → стек Python 3.11 + PyQt5 5.15 из apt Astra (код репо — MIT; сборка
     распространяется на условиях GPL). Закрытая поставка не требуется.
  3. **Индикация записи — пилюля с визуализацией по умолчанию, отключаемая** в настройках
     (остаются трей + звук); опыт фокуса на KDE/X11 из Astra Cowork учитывать.
- Почему: заказчик хочет покрыть и свою машину, и корпоративные ALSE; GPL не мешает; пилюля — ожидание пользователей.
- Гейт: — (рамки для G1) · «да» заказчика.
- Последствия: в PRD — две ветки обновлятора и настройка «источник обновлений/моделей» (корпоративное
  зеркало); архитекторам — PyQt5 как заданное ограничение; UX — экран настроек с выключателем пилюли.

## 2026-09-07 — Решения Юрки по открытым вопросам ресёрча (рутинные, без гейта)
- Модель по умолчанию — текущая GigaAM v3 e2e_rnnt; e2e_ctc — в каталоге как «быстрее на ~25 %».
- Каталог v1 — не менее 8 моделей (GigaAM v3 ×3, GigaAM Multilingual, Whisper ×3–4 int8, Vosk small-ru).
- CC-BY-модели допускаются с атрибуцией в «О программе»; Silero (CC-NC) исключён.
- ОЗУ и скорость в каталоге — замер на этой машине после первого прогона (VmHWM воркера) с пометкой
  «замерено / заявлено / оценка»; до замера — справочные значения.
- Зеркало моделей в GitHub Releases репо допустимо (MIT); настройка «корпоративный URL каталога» — в v1.
- Постобработка LLM (как в Handy) — вне v1.
- История буфера Klipper: история «не оставлять распознанный текст в истории буфера» — в беклог (Should).

## 2026-09-07 — G2a (часть 2): цвет «слушаю» — бирюза; знак — второй раунд
- Контекст: показаны 4 направления знака; заказчик: «Ёлочки» (01) и «Клавиша» (03) — более-менее,
  «Волна» (02) и «Каретка» (04) не зацепили, нужны ещё варианты.
- Решение: цвет состояния «слушаю» в трее — **фирменная бирюза #12B3A0** (красный только для ошибки);
  запущен **раунд 2** brand-designer (3–4 новых направления + 01/03 под именем Astra Voice).
  Это последний раунд по лимиту (🔁 2); если и он не попадёт — расхождение вкусов выносится заказчику
  вопросами, не угадыванием.
- Почему: выбор заказчика (чат 2026-09-07).
- Гейт: G2a (цвет) · «да» заказчика; знак — открыт.
- Последствия: UX-макеты стартуют на палитре/типографике brand-basics с плейсхолдером знака;
  финальный знак подставляется после выбора.

## 2026-09-07 — G2a (часть 1): имя продукта — Astra Voice
- Контекст: brand-designer предложил 6 вариантов (рекомендовал «Вслух»); «Astra Voice» помечен риском —
  «Astra» товарный знак ГК «Астра» (заказчик связан с Астрой; repo под личным аккаунтом ArrivaRUS).
- Решение: имя **Astra Voice** (латиницей), slug/репо `astra-voice` остаются без переименования.
  Текстовая часть во всех концептах знака заменяется с «Вслух» на «Astra Voice» при доводке.
- Почему: выбор заказчика (чат 2026-09-07); продукт живёт в экосистеме Astra Linux.
- Гейт: G2a (имя) · «да» заказчика.
- Последствия: вопрос прав на знак «Astra» остаётся в списке задач комплаенса (`research/legal.md`);
  направление знака и цвет «слушаю» — отдельный выбор (G2a часть 2).

## 2026-09-07 — Публичный репозиторий и лицензия (предварительно)
- Контекст: заказчик попросил публичный GitHub-репо под проект. Публичному репо нужна лицензия.
- Решение: репо `ArrivaRUS/astra-voice` (public, ветка `main`); лицензия **MIT** — предварительно,
  как у GigaAM/Whisper/Handy; смена лицензии — одной правкой файла.
- Почему: MIT совместима с лицензиями используемых моделей и движков и не мешает корпоративному использованию.
- Гейт: — · «да» заказчика на публичный репо (чат 2026-09-07); лицензия — допущение Юрки, ждёт подтверждения.
- Последствия: модели в git не кладём (только манифесты с ревизией и sha256), секреты — никогда.

## 2026-09-07 — G0: строим (короткий маршрут Discovery)
- Контекст: заказчик сформулировал задачу и требования сам («упаковать в новую утилиту с нуля под ALSE»);
  рынок/спрос проверять не нужно — утилита заменяет работающий Handy на машинах заказчика.
- Решение: **GO** без рыночного ресёрча; Discovery только техническое (движки/модели, самообновление,
  автозапуск, GUI-стек под ALSE) + правовое заключение (лицензии моделей, 152-ФЗ по голосу, ЗПС).
  Из-за автономной сессии (заказчик не отвечает по ходу) волны ведутся параллельно, а гейты
  G1/G2a/G2b выносятся заказчику одним пакетом.
- Почему: экономия времени заказчика; риск расхождения PRD↔дизайн мал — требования конкретные.
- Гейт: G0 · «да» заказчика (сам запрос 2026-09-07).
- Последствия: имя проекта — рабочее (`astra-voice`), финальное — на G2a вместе с логотипом.

exec
/bin/bash -lc "nl -ba arch/plan-claude.md | sed -n '320,352p'; rg --files --hidden /tmp /home/astra/.cache/pip /home/astra/Документы/handy-gigaam 2>/dev/null | rg 'onnxruntime.*(whl|\\.so)|onnx_asr.*whl|requests/(sessions|adapters)\\.py' | head -60; rg -n 'switching|смен|переключ|трей недоступен|SNI|сбой|прерва|агент' design/spec.md | head -50" in /home/astra/Документы/astra-voice
 succeeded in 0ms:
   320	## 11. Вопросы к человеку
   321	
   322	1. **Спайк S3 качает модели с HF** (226 МБ минимум, 680 МБ для трёх вариантов) в scratch-venv — нужно «да» на трафик/диск.
   323	2. **S2 требует одного `sudo`** на машине (положить `.policy` и тестовый помощник) — допустимо ли делать это на машине
   324	   заказчика, или готовим ВМ ALSE 1.8 (её же используем для S1 во Fly-сессии и для чистого прогона S1/Ц3)?
   325	3. Шрифты **PT Root UI / PT Mono** отсутствуют на ALSE: вкладывать в пакет (проверка лицензии ParaType — задача legal)
   326	   или принять резерв PT Astra Sans для UI (метрики макета чуть разойдутся)?
   327	4. Подпись релизов **GPG (Ed25519) через `gpgv`** вместо minisign — принять как решение архитектуры (аргумент: `gpgv` есть
   328	   у любого админа ALSE для трека B; `minisign` в apt Astra нет)? Кто владеет закрытым ключом CI (заказчик или Юрка)?
   329	5. Меню трея под SNI рисует Plasma — согласовать с DesignReviewer, что §9.2 спеки сверяется по составу, не по стилю.
   330	6. Root-помощник как Python-скрипт (ноль своих ELF) — принять, либо T1 потребует C-ELF?
   331	
   332	## 12. Факты среды (проверено read-only 2026-09-09; вход для синтеза и спайков)
   333	
   334	- ALSE 1.8.5 = Debian 12.0 (`/etc/debian_version`), glibc 2.36, Python 3.11.2, `python3-pyqt5` 5.15.9 (модули Core/Gui/
   335	  Widgets/Network/DBus/…; **QtQml/QtQuick не установлены**, пакет `python3-pyqt5.qtquick` 5.15.9+dfsg-1+b9 есть в apt);
   336	  `qml-module-qtquick-controls2` 5.15.8 и `qml-module-qtgraphicaleffects` установлены; `libqt5svg5`, `libqt5x11extras5`,
   337	  `qmllint`/`qmlscene` есть; `qtdeclarative5-dev-tools`, `pyqt5-dev-tools`, `xvfb`, `python3-pytest` 7.2, `python3-mypy` 1.0.1 — в apt.
   338	- В apt Astra есть: `python3-xlib` 0.33, `python3-evdev` 1.6.1, `python3-requests` 2.32.5 (установлен), `python3-jsonschema`
   339	  4.10, `python3-nacl` 1.5, `python3-flatbuffers` 2.0.8, `python3-protobuf` 3.21.12, `python3-packaging` 23.0, `python3-psutil`,
   340	  `gpgv` 2.2.40 (установлен). **Нет:** `python3-sounddevice`, `python3-pyaudio`, `python3-pynput`, `minisign`, `syft`, `python3-semver`.
   341	- Звук: PipeWire 1.4.9 с pulse-протоколом (`pactl` работает, драйвер источников — PipeWire; `pipewire-pulse.service` inactive —
   342	  протокол отдаёт сам pipewire), WirePlumber 0.5.12 активен; `libpulse-simple.so.0` грузится через ctypes; есть `pw-record`,
   343	  `paplay`, `parecord`; источники `alsa_input…Mic1/Mic2` (s32le, 48 кГц).
   344	- Колёса ORT в `~/.cache/uv`: 1.24.4 (max `GLIBC_2.27`, 22 МБ lib) и 1.29.0 (max `GLIBC_2.28`, 28 МБ lib), **0 ссылок
   345	  `__isoc23_*`**, по 3 `.so`; шим из Handy нужен был только prebuilt-бинарю крейта `ort`. `onnx-asr` 0.12.0: `numpy>=1.22.4`,
   346	  `onnxruntime>=1.18.1,!=1.24.1,!=1.25.*,!=1.26.0`; системный numpy 1.24.2 подходит.
   347	- KWin: композитор активен; EWMH поддерживает `_NET_ACTIVE_WINDOW`, `_NET_CLIENT_LIST_STACKING`, `ABOVE`, `SKIP_TASKBAR`
   348	  (4 из 5 проверенных атомов — тип `NOTIFICATION` проверить в S1); `StatusNotifierWatcher` host зарегистрирован;
   349	  `org.freedesktop.Notifications` — Plasma; `polkit-kde-authentication-agent-1` запущен; пользователь в `astra-admin`, `input`.
   350	- ЗПС выключена (`DIGSIG_ELF_MODE=0`, все режимы 0); ключи изготовителя в `/etc/digsig/`.
   351	- `~/.config/kglobalshortcutsrc` (23 КБ): формат `key=combo\tcombo,default,описание` — пример `_launch=Alt+Space\tAlt+F2\tSearch,…,Открыть строку поиска и запуск`.
   352	- Шрифты: PT Astra Sans/Serif/Fact есть; **PT Root UI и PT Mono — нет** (R11).
171:| `switching` | «Переключаю на GigaAM v3 CTC…» |
255:переключает тумблер, и вся строка подсвечивается на hover. Строки **со списком, кнопкой
333:## 4.3 Сегментированный переключатель
343:| focus | кольцо вокруг **всего** переключателя, стрелки ← → переключают |
470:| 6 | **переключение активной** | граница `accent`, подпись «Переключаю…» | — |
492:| активная / переключение | `accent` | `accent-bg` | обычный |
598:**Палитра пилюли не переключается с темой приложения** — она рисуется поверх чужих обоев.
805:## 12.1 Как переключается

exec
/bin/bash -lc "rg --files --hidden /home/astra/.cache/uv | rg 'onnxruntime/(capi/.*|.*METADATA)|onnxruntime.*whl|onnxruntime.*dist-info/METADATA|onnx_asr/.+(\\.py)"'$'"' | head -65; nl -ba PRD.md | sed -n '130,181p;218,229p;365,392p'" in /home/astra/Документы/astra-voice
 succeeded in 0ms:
/home/astra/.cache/uv/archive-v0/8erzEYGrUvug4ChskfrAS/onnxruntime/capi/onnxruntime_validation.py
/home/astra/.cache/uv/archive-v0/8erzEYGrUvug4ChskfrAS/onnxruntime/capi/__init__.py
/home/astra/.cache/uv/archive-v0/8erzEYGrUvug4ChskfrAS/onnxruntime/capi/onnxruntime_inference_collection.py
/home/astra/.cache/uv/archive-v0/8erzEYGrUvug4ChskfrAS/onnxruntime/capi/build_and_package_info.py
/home/astra/.cache/uv/archive-v0/8erzEYGrUvug4ChskfrAS/onnxruntime/capi/_ld_preload.py
/home/astra/.cache/uv/archive-v0/8erzEYGrUvug4ChskfrAS/onnxruntime/capi/libonnxruntime_providers_shared.so
/home/astra/.cache/uv/archive-v0/8erzEYGrUvug4ChskfrAS/onnxruntime/capi/onnxruntime_pybind11_state.cpython-311-x86_64-linux-gnu.so
/home/astra/.cache/uv/archive-v0/8erzEYGrUvug4ChskfrAS/onnxruntime/capi/onnxruntime_collect_build_info.py
/home/astra/.cache/uv/archive-v0/8erzEYGrUvug4ChskfrAS/onnxruntime/capi/_pybind_state.py
/home/astra/.cache/uv/archive-v0/8erzEYGrUvug4ChskfrAS/onnxruntime/capi/convert_npz_to_onnx_adapter.py
/home/astra/.cache/uv/archive-v0/8erzEYGrUvug4ChskfrAS/onnxruntime/capi/libonnxruntime.so.1.24.4
/home/astra/.cache/uv/archive-v0/8erzEYGrUvug4ChskfrAS/onnxruntime-1.24.4.dist-info/METADATA
/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime-1.29.0.dist-info/METADATA
/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime/capi/onnxruntime_validation.py
/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime/capi/__init__.py
/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime/capi/libonnxruntime.so.1.29.0
/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime/capi/onnxruntime_inference_collection.py
/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime/capi/build_and_package_info.py
/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime/capi/_ld_preload.py
/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime/capi/libonnxruntime_providers_shared.so
/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime/capi/onnxruntime_pybind11_state.cpython-311-x86_64-linux-gnu.so
/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime/capi/onnxruntime_collect_build_info.py
/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime/capi/_pybind_state.py
/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime/capi/convert_npz_to_onnx_adapter.py
   130	### F2. Захват речи и горячие клавиши — P0
   131	**Описание.** Глобальный хоткей в X11 (XGrabKey через `python3-xlib`), два режима: push-to-talk (удержание) и toggle (нажал — говоришь — нажал).
   132	**Story.** Как П1, я хочу зажать клавишу и говорить, чтобы текст появился без мыши и переключения окон.
   133	**Бизнес-правила.**
   134	1. Режим по умолчанию — push-to-talk; toggle — второй режим (отдельная комбинация или переключатель режима в настройках).
   135	2. Комбинация по умолчанию — **`Ctrl+Space`** для push-to-talk (как в Handy заказчика; решение 2026-09-07); toggle — второй режим на отдельной комбинации (по умолчанию не назначена, задаётся в настройках); проверка конфликтов с KDE обязательна; альтернатива в подсказке — `Scroll Lock`.
   136	3. Захват работает поверх любого X11-окна, не требует root и группы `input`; `evdev` — опциональный «надёжный режим» (P2).
   137	4. Запись короче **0,3 с** игнорируется молча (случайное нажатие): без вставки, без ошибки, пилюля не показывается дольше 150 мс.
   138	5. Лимит одной записи — **120 с** (настройка 30–300 с); по достижении — автостоп + распознавание + подсказка в пилюле «Достигнут лимит записи».
   139	6. Фразы до 20 с распознаются одним куском; длиннее — сегментация по паузам (VAD) с сохранением порядка; лимит окна модели (24 с у GigaAM) не должен резать слова.
   140	7. Аудиоустройство открывается **только при нажатии хоткея**, не при старте (гонка WirePlumber, F6). Опция «держать микрофон открытым 30 с после записи» — P1.
   141	8. Отмена записи: `Esc` во время записи (пока хоткей зажат) или «×» в пилюле, или «Отмена» в трее — аудио отбрасывается, вставки нет.
   142	9. Повторное нажатие хоткея во время распознавания — новая запись становится в очередь (максимум 1); вставка идёт в порядке записи.
   143	10. Пока идёт распознавание, хоткей не блокируется; пока GUI-окно настроек в фокусе, хоткей работает так же (вставка пойдёт в поле настроек — допустимо).
   144	**Исключения.** Комбинацию не удалось захватить (занята) → в трее статус «Горячая клавиша не захвачена» + уведомление с кнопкой «Выбрать другую»; потеря X-соединения → перезахват при восстановлении; клавиша отпущена раньше, чем открылся микрофон → см. п. 4.
   145	
   146	### F3. Индикация: пилюля, трей, звук — P0
   147	**Описание.** Три канала: пилюля-оверлей с живым уровнем (по умолчанию включена, отключаемая), иконка трея с состояниями, опциональные звуки.
   148	**Story.** Как П1, я хочу видеть, что микрофон меня слышит, чтобы не диктовать в пустоту.
   149	**Бизнес-правила.**
   150	1. Пилюля появляется ≤ 100 мс после нажатия, у нижнего края экрана по центру (настройка: снизу / сверху / выключена), поверх всех окон, **не забирает фокус** и не появляется в панели задач/Alt+Tab.
   151	2. Состояния пилюли: «слушаю» (бирюза `#12B3A0`, столбики уровня), «распознаю» (нейтральный, пульс), «готово» (краткое подтверждение 500 мс — решение G2 2026-09-08, затем скрытие), «ничего не распознано» (1 с), «ошибка» (красный + короткий текст, 3 с, клик открывает подробности). Точные размеры/анимация — дизайн-спека.
   152	3. В пилюле есть «×» — отмена записи.
   153	4. Иконка трея (SNI/`QSystemTrayIcon`): 4 состояния — «готов», «слушаю» (бирюза), «распознаю», «ошибка/микрофон недоступен» (красный). Тема иконки — по теме панели (светлая/тёмная). Регистрация в трее с ретраем до 30 с после старта (гонка с панелью).
   154	5. Звуки «начало/конец записи» — настройка, по умолчанию **выключены**; при отключении пилюли утилита один раз предлагает включить звук («Вы отключили индикатор — включить звуковой сигнал?»).
   155	6. Отключение пилюли **не** отключает индикацию в трее: иконка «слушаю» обязательна всегда (запрет скрытой записи, §9.5).
   156	7. Тема пилюли и окна — из `~/.config/kdeglobals` (не из палитры Qt), применяется до первой отрисовки (без «вспышки светлой темы»).
   157	**Исключения.** Композитор не даёт «поверх всех» → пилюля всё равно показывается (может быть перекрыта); трей недоступен > 30 с → окно настроек открывается само с баннером «Системный трей недоступен, приложение работает; закрыть окно = свернуть».
   158	
   159	### F4. Распознавание (движок, воркер, модель) — P0
   160	**Описание.** Инференс в отдельном процессе-воркере (`onnx-asr`), модель держится в памяти, GUI не блокируется и не падает вместе с ORT.
   161	**Story.** Как П2, я хочу ту же скорость и качество, что в Handy с GigaAM, чтобы замена не была шагом назад.
   162	**Бизнес-правила.**
   163	1. Воркер — отдельный процесс; крах воркера не роняет GUI: автоперезапуск (до 3 раз за 10 мин), пользователю — ошибка в пилюле/трее.
   164	2. Модель по умолчанию — GigaAM v3 e2e_rnnt int8. Активная модель загружается в память при старте в фоне (после готовности трея) и **держится в памяти постоянно** (решение заказчика 2026-09-07); выгрузка по простою — опция «выгружать после: 5 / 15 / 60 мин / никогда», по умолчанию «никогда».
   165	3. Целевая задержка: «отпустил → текст» p95 ≤ 0,5 с (6-с фраза, эталонная машина, модель в памяти); холодный старт модели ≤ 5 с с индикацией «Загружаю модель…» в пилюле.
   166	4. Потоки ORT — по умолчанию `min(4, физических ядер)`; настройка в «Продвинутых».
   167	5. Результат — текст UTF-8 как выдала модель; единственная обработка: обрезка пробелов по краям, опция «пробел в конце» (по умолчанию вкл — допущение), опция «первая буква заглавной» для моделей без пунктуации (P1). Никакой LLM-постобработки.
   168	6. Пустой результат → «ничего не распознано», вставки нет.
   169	7. Каждое распознавание пишет в локальную статистику `(model_id, audio_ms, t_ms, ts)`; первый прогон после загрузки помечается «холодным» и в медиану не входит.
   170	8. После первого прогона каждой модели снимается `VmHWM` воркера → карточка получает «ОЗУ: N МБ (замерено на этом компьютере)».
   171	**Исключения.** Модель не загрузилась (битый файл, несовпадение входов ONNX) → карточка «Повреждена — переустановить», активной становится предыдущая рабочая модель, если есть; нехватка ОЗУ (`MemoryError`/OOM) → ошибка «Недостаточно памяти для модели X (нужно ~N МБ)» и предложение более лёгкой модели из каталога.
   172	
   173	### F5. Вставка текста в активное окно — P0
   174	**Описание.** Через буфер обмена: сохранить буфер → положить `text/plain` → послать комбинацию вставки → восстановить буфер.
   175	**Story.** Как П1, я хочу, чтобы текст оказался там, где курсор, в терминале, браузере и LibreOffice, а мой буфер обмена остался нетронутым.
   176	**Бизнес-правила.**
   177	1. В буфер кладётся **только `text/plain`** (без форматирования — LibreOffice вставит чистый текст без диалога).
   178	2. Комбинация вставки — настройка: `Ctrl+V` (по умолчанию) / `Ctrl+Shift+V` / `Shift+Insert` / «не вставлять, только в буфер».
   179	3. Задержка перед вставкой — настройка, по умолчанию 50 мс; после вставки через 100 мс восстанавливается исходное содержимое буфера (если оно было текстом; нетекстовое содержимое не восстанавливается — допущение, §12).
   180	4. Автоопределение терминалов по `WM_CLASS` (`konsole`, `fly-term`, `xterm`, `yakuake`, `alacritty`, список редактируемый) → `Ctrl+Shift+V` — P1.
   181	5. `xdotool type` (посимвольный ввод) — только как явно помеченный аварийный вариант в «Продвинутых» (кириллица ненадёжна) — P2.
   218	**Бизнес-правила — поведение.**
   219	1. Загрузка во временный `.part` с докачкой по `Range` (продолжать только при 206 с корректным `Content-Range`; на 200 — сначала); URL закреплён на `/resolve/{revision}/…`, не на `main`; после завершения — размер и sha256 против манифеста, `fsync`, атомарный `os.replace`; модель хранится в каталоге `<ревизия>/`, активная ревизия — указатель `current.json` (откат = смена указателя); несовпадение → удалить и показать ошибку, не «доклеивать».
   220	2. Источники по очереди: HF → GitHub Releases (наше зеркало) → корпоративный URL; переход к следующему при ошибке сети/429/404; отображается, откуда качаем. Запись каталога привязана к рантайму (`engine`): экспорт под `onnx-asr` (istupakov) и под `sherpa-onnx` (csukuangfj) — разные записи, не взаимозаменяемые.
   221	3. Установка из файла/папки: те же файлы, та же проверка sha256 по манифесту; неизвестный набор файлов (нет в манифесте) → «Своя модель» без полосок, P2.
   222	4. Удаление активной модели запрещено, пока не выбрана другая; удаление освобождает диск и снимает «замерено».
   223	5. Выбор модели — переключение воркера с индикацией «Переключаю…»; пока идёт — хоткей работает на старой модели.
   224	6. Перед скачиванием проверяется свободное место (размер × 1,2); недостаточно → ошибка с цифрами.
   225	7. Проверка на минимальную ОЗУ машины: если `MemTotal` < `min_ram_mb` модели — предупреждение, не запрет.
   226	8. Фильтр по языку (P2) и переключатель «только отечественные» (P1, автоматически в защищённом профиле).
   227	9. Каталог всегда работает офлайн: встроенный манифест из пакета + последний скачанный; «нет сети» = состояние секции «Доступные» с кнопкой «Установить из файла», а не пустой список.
   228	**Состав каталога v1** — §7.2. **Формат манифеста** — §7.3.
   229	**Исключения.** Манифест с URL не прошёл подпись/схему → используется предыдущий, в статусной строке «Каталог не обновлён: <причина>»; диск заполнился во время загрузки → пауза, ошибка с кнопкой «Повторить» после освобождения места.
   365	- **A3.** Given HF вернул изменение только ветки `main`, а не `e2e_rnnt`, When проверка прошла, Then обновление **не** предлагается.
   366	- **A4.** Given манифест с `catalog_url` содержит новую модель, When проверка прошла, Then в «Доступных» карточка с бейджем «Новое».
   367	- **A5 (P2).** Given у автора `ai-sage` появился репозиторий семейства GigaAM, которого нет в манифесте, When проверка прошла, Then в «Моделях» информационный баннер с именем, датой и ссылкой, без кнопки «Скачать».
   368	- **A6.** Given проверка была < 24 ч назад, When запускаю утилиту снова, Then новой проверки нет (по логу и `last_checked_at`).
   369	- **A7.** Given HF вернул 429, When проверка, Then пропуск, повтор не раньше окна из `RateLimit`, в UI — тишина.
   370	
   371	### S9. Обновление утилиты — трек без ЗПС (F9)
   372	- **A1.** Given версия 0.2.0, GitHub `releases/latest` = 0.2.1, ЗПС выкл, пользователь в `astra-admin`, When утилита запущена, Then строка внизу «Доступна версия 0.2.1» цветом акцента; клик открывает панель с «Что нового», размером и кнопкой «Скачать и установить».
   373	- **A2.** Given панель, When нажимаю «Скачать и установить», Then «Загрузка… N %» с прогрессом, затем проверка sha256 и подписи, затем **одно** окно polkit KDE (помощник обновления); после ввода пароля — «Установка…», затем «Установлено. Перезапустить сейчас / позже»; `dpkg-query -W astra-voice` = 0.2.1.
   374	- **A3.** Given подпись не совпала (подменённый deb), When проверка, Then файл удалён, строка «Пакет не прошёл проверку подписи», установки не было.
   375	- **A4.** Given polkit отменён, When отмена, Then «Установка отменена», deb в `~/.cache/astra-voice/updates/`, кнопка «Открыть папку».
   376	- **A5.** Given тумблер «Проверять обновления утилиты» выключен, When запускаю, Then строка «Проверка обновлений отключена», сетевых запросов нет; «Проверить сейчас» в трее и в разделе «Сеть» **активны** (ручная проверка — явное действие пользователя; неактивны только в офлайн-режиме или по политике администратора — решение 2026-09-08, F12).
   377	- **A6.** Given нажато «Пропустить эту версию», When перезапускаю, Then строка не показывает 0.2.1; при выходе 0.2.2 — показывает.
   378	- **A7.** Given версия актуальна, When нажимаю «Проверить сейчас», Then «Установлена последняя версия» на 3 с, потом строка возвращается к обычному виду.
   379	
   380	### S10. Обновление утилиты — трек с ЗПС / без прав (F9)
   381	- **A1.** Given `DIGSIG_ELF_MODE=1` (или `policy.conf: zps=on`), When есть новая версия, Then панель показывает «Скачать пакет для администратора», кнопки «Установить» нет, текст «На этом компьютере включена замкнутая программная среда — установку выполняет администратор».
   382	- **A2.** Given то же, When нажимаю «Скачать пакет для администратора», Then в папке — `.deb`, `SHA256SUMS`, подпись, `INSTALL-ADMIN.md`; подпись проверена; кнопка «Открыть папку»; утилита **не** запускала `pkexec`/`apt`.
   383	- **A3.** Given ЗПС выкл, но пользователь не в `astra-admin`/`sudo`, When есть новая версия, Then тот же трек B.
   384	- **A4.** Given невозможно определить ЗПС (конфиг нечитаем, policy `auto`), When открываю панель, Then вопрос «Включена ли ЗПС?» с вариантами Да / Нет / Не знаю; «Не знаю» → трек B.
   385	- **A5 (v1.1, после v1.0).** Given ВМ с включённой ЗПС и зарегистрированным ключом организации, When администратор ставит подписанный deb по `INSTALL-ADMIN.md`, Then утилита запускается, воркер работает, диктовка вставляет текст (Ц5).
   386	
   387	### S11. Автозапуск вкл/выкл (F10)
   388	- **A1.** Given тумблер выключен, When включаю, Then файл `.desktop` создан с каноническим `Exec`, без `OnlyShowIn`, с `X-KDE-autostart-after=panel`; после перелогина утилита в трее, окно не открыто, аудио не открыто (нет потока в `wpctl status` до первого хоткея).
   389	- **A2.** Given включён, When выключаю, Then файл удалён (или `Hidden=true`), после перелогина утилиты нет.
   390	- **A3.** Given старые `handy.desktop`/`Handy.desktop` в `~/.config/autostart`, When управляю тумблером, Then эти файлы не изменены.
   391	- **A4.** Given утилита уже запущена, When запускаю второй экземпляр, Then открывается окно первого, второй процесс завершился.
   392	

web search: 
exec
/bin/bash -lc "python3 -B - <<'PY'
from pathlib import Path
import re,subprocess
for root in (Path('/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-'), Path('/home/astra/.cache/uv/archive-v0/8erzEYGrUvug4ChskfrAS')):
    for p in root.glob('onnxruntime-*.dist-info/METADATA'):
        print('\\n'+str(p)); print('\\n'.join(x for x in p.read_text().splitlines() if x.startswith(('Version:', 'Requires-Dist:'))))
    for p in root.rglob('*'):
        if not p.is_file(): continue
        with p.open('rb') as f: elf=f.read(4)==b'\\x7fELF'
        if elf:
            out=subprocess.check_output(['objdump','-T',str(p)],text=True)
            ver=sorted(set(re.findall(r'GLIBC_(\\d+(?:\\.\\d+)+)',out)),key=lambda v:tuple(map(int,v.split('.'))))
            print(p.relative_to(root), 'maxGLIBC='+str(ver[-1:]), 'isoc23='+str(len([x for x in out.splitlines() if 'isoc23' in x])))
PY" in /home/astra/Документы/astra-voice
 succeeded in 0ms:

/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime-1.29.0.dist-info/METADATA
Version: 1.29.0
Requires-Dist: flatbuffers
Requires-Dist: numpy>=1.21.6
Requires-Dist: packaging
Requires-Dist: protobuf>=4.25.8
Requires-Dist: sympy; extra == "symbolic"
Requires-Dist: ml_dtypes; extra == "quantization"
onnxruntime/capi/onnxruntime_pybind11_state.cpython-311-x86_64-linux-gnu.so maxGLIBC=['2.28'] isoc23=0
onnxruntime/capi/libonnxruntime_providers_shared.so maxGLIBC=['2.2.5'] isoc23=0
onnxruntime/capi/libonnxruntime.so.1.29.0 maxGLIBC=['2.28'] isoc23=0

/home/astra/.cache/uv/archive-v0/8erzEYGrUvug4ChskfrAS/onnxruntime-1.24.4.dist-info/METADATA
Version: 1.24.4
Requires-Dist: flatbuffers
Requires-Dist: numpy>=1.21.6
Requires-Dist: packaging
Requires-Dist: protobuf
Requires-Dist: sympy
onnxruntime/capi/libonnxruntime.so.1.24.4 maxGLIBC=['2.27'] isoc23=0
onnxruntime/capi/onnxruntime_pybind11_state.cpython-311-x86_64-linux-gnu.so maxGLIBC=['2.27'] isoc23=0
onnxruntime/capi/libonnxruntime_providers_shared.so maxGLIBC=['2.2.5'] isoc23=0

web search: https://requests.readthedocs.io/en/latest/user/advanced/
codex
Проверил локальные ORT-библиотеки: у 1.29.0 и запасной 1.24.4 действительно по три ELF, ссылок `__isoc23_*` нет. Но обнаружился соседний конфликт: ORT 1.29.0 требует `protobuf>=4.25.8`, а базовый план рассчитывает на системный 3.21.12. Также правка только поля `engine` в примере PRD оставит несовместимые раскладку и файлы.

exec
/bin/bash -lc "dpkg-query -W -f='"'${Package} ${Version}'"\\n' python3-protobuf python3-requests; rg --files --hidden /home/astra/.cache/uv | rg '/onnx_asr["'^/]*dist-info/METADATA|/onnx_asr/.+'"\\.py"'$'"' | head -35; sed -n '680,750p' /usr/lib/python3/dist-packages/requests/sessions.py; sed -n '350,395p' /usr/lib/python3/dist-packages/requests/adapters.py; rg -n 'MemAvailable|mem|measurements|update-cache|settings' arch/plan-codex.md" in /home/astra/Документы/astra-voice
 succeeded in 0ms:
python3-requests 2.32.5+dfsg-1.astra1
dpkg-query: no packages found matching python3-protobuf
        kwargs.setdefault("stream", self.stream)
        kwargs.setdefault("verify", self.verify)
        kwargs.setdefault("cert", self.cert)
        if "proxies" not in kwargs:
            kwargs["proxies"] = resolve_proxies(request, self.proxies, self.trust_env)

        # It's possible that users might accidentally send a Request object.
        # Guard against that specific failure case.
        if isinstance(request, Request):
            raise ValueError("You can only send PreparedRequests.")

        # Set up variables needed for resolve_redirects and dispatching of hooks
        allow_redirects = kwargs.pop("allow_redirects", True)
        stream = kwargs.get("stream")
        hooks = request.hooks

        # Get the appropriate adapter to use
        adapter = self.get_adapter(url=request.url)

        # Start time (approximately) of the request
        start = preferred_clock()

        # Send the request
        r = adapter.send(request, **kwargs)

        # Total elapsed time of the request (approximately)
        elapsed = preferred_clock() - start
        r.elapsed = timedelta(seconds=elapsed)

        # Response manipulation hooks
        r = dispatch_hook("response", hooks, r, **kwargs)

        # Persist cookies
        if r.history:
            # If the hooks create history then we want those cookies too
            for resp in r.history:
                extract_cookies_to_jar(self.cookies, resp.request, resp.raw)

        extract_cookies_to_jar(self.cookies, request, r.raw)

        # Resolve redirects if allowed.
        if allow_redirects:
            # Redirect resolving generator.
            gen = self.resolve_redirects(r, request, **kwargs)
            history = [resp for resp in gen]
        else:
            history = []

        # Shuffle things around if there's history.
        if history:
            # Insert the first (original) request at the start
            history.insert(0, r)
            # Get the last request made
            r = history.pop()
            r.history = history

        # If redirects aren't being followed, store the response on the Request for Response.next().
        if not allow_redirects:
            try:
                r._next = next(
                    self.resolve_redirects(r, request, yield_requests=True, **kwargs)
                )
            except StopIteration:
                pass

        if not stream:
            r.content

        return r

    def merge_environment_settings(self, url, proxies, stream, verify, cert):

        # Make headers case-insensitive.
        response.headers = CaseInsensitiveDict(getattr(resp, "headers", {}))

        # Set encoding.
        response.encoding = get_encoding_from_headers(response.headers)
        response.raw = resp
        response.reason = response.raw.reason

        if isinstance(req.url, bytes):
            response.url = req.url.decode("utf-8")
        else:
            response.url = req.url

        # Add new cookies from the server.
        extract_cookies_to_jar(response.cookies, req, resp)

        # Give the Response some context.
        response.request = req
        response.connection = self

        return response

    def build_connection_pool_key_attributes(self, request, verify, cert=None):
        """Build the PoolKey attributes used by urllib3 to return a connection.

        This looks at the PreparedRequest, the user-specified verify value,
        and the value of the cert parameter to determine what PoolKey values
        to use to select a connection from a given urllib3 Connection Pool.

        The SSL related pool key arguments are not consistently set. As of
        this writing, use the following to determine what keys may be in that
        dictionary:

        * If ``verify`` is ``True``, ``"ssl_context"`` will be set and will be the
          default Requests SSL Context
        * If ``verify`` is ``False``, ``"ssl_context"`` will not be set but
          ``"cert_reqs"`` will be set
        * If ``verify`` is a string, (i.e., it is a user-specified trust bundle)
          ``"ca_certs"`` will be set if the string is not a directory recognized
          by :py:func:`os.path.isdir`, otherwise ``"ca_cert_dir"`` will be
          set.
        * If ``"cert"`` is specified, ``"cert_file"`` will always be set. If
          ``"cert"`` is a tuple with a second item, ``"key_file"`` will also
          be present

31:| **IPC** | Наследуемый `AF_UNIX socketpair`; сообщения JSON с длиной, версией протокола и ID. PCM передаётся через FD `memfd`, после записи — запечатанный от изменения. Без pickle и localhost TCP | `SCM_RIGHTS`, `QSocketNotifier` | Частичные сообщения, неверная длина, неизвестная команда, лишние FD, утечки дескрипторов |
54:| **Логи / метрики / диагностика** | 5×1 МБ логов; stats и measurements отдельно. Только коды, длины, времена. Диагностический ZIP по кнопке, с удалением домашних путей, секретов proxy/URL, без текста и аудио | procfs, SettingsStore | Поиск контрольного текста/секретов в файлах; ротация; очистка; закрытые права доступа |
266:| Две модели вызывают OOM | Проверка MemAvailable и пика кандидата; при нехватке сохранить старую и отказать в переключении с объяснением | Данные/активная ревизия не меняются |
271:| HF SHA постоянно даёт ложное обновление | Раздельные координаты upstream/export; установка только подписанного набора | Миграция update-cache |

exec
/bin/bash -lc "apt-cache policy python3-protobuf python3-sympy; rg -n 'measure|rtfx|VmHWM|peak_rss|protocol|schema' arch/plan-claude.md arch/plan-codex.md | tail -40; sed -n '745,790p' /usr/lib/python3/dist-packages/requests/sessions.py; nl -ba PRD.md | sed -n '190,198p;502,516p'" in /home/astra/Документы/astra-voice
 succeeded in 0ms:
python3-protobuf:
  Installed: (none)
  Candidate: 3.21.12-3+ci4
  Version table:
     3.21.12-3+ci4 900
        900 https://download.astralinux.ru/astra/stable/1.8_x86-64/repository-extended 1.8_x86-64/main amd64 Packages
python3-sympy:
  Installed: (none)
  Candidate: 1.11.1-1+b5
  Version table:
     1.11.1-1+b5 900
        900 https://download.astralinux.ru/astra/stable/1.8_x86-64/repository-extended 1.8_x86-64/main amd64 Packages
arch/plan-codex.md:54:| **Логи / метрики / диагностика** | 5×1 МБ логов; stats и measurements отдельно. Только коды, длины, времена. Диагностический ZIP по кнопке, с удалением домашних путей, секретов proxy/URL, без текста и аудио | procfs, SettingsStore | Поиск контрольного текста/секретов в файлах; ротация; очистка; закрытые права доступа |
arch/plan-codex.md:63:| Проверка совместимости | `protocol_version`, `app_build`, `runtime_version`, `model_id`, `revision`, `generation` |
arch/plan-codex.md:151:| После установки | Не смешивать новый код воркера со старым GUI: проверять build/protocol; операции, требующие нового процесса, ждут перезапуска |
arch/plan-codex.md:174:| Python из apt | `python3-numpy`, `python3-packaging`, `python3-jsonschema`; зависимости ORT: protobuf, flatbuffers, sympy, humanfriendly и их зависимости |
arch/plan-codex.md:218:| PR pipeline | Ruff, unit, schema/preset checks, генератор токенов, Qt 5.15 QML smoke, Xvfb, HTTP-фикстуры |
arch/plan-codex.md:249:| RAM | Новый процесс на модель/ревизию/настройку потоков; VmHWM после первого inference, дополнительно PSS. HWM предыдущей модели не наследуется |
arch/plan-claude.md:55:│  (onnx-asr, VAD>20 с, замер VmHWM/PSS)              │   │ apt-get install --no-remove -y ./x.deb · JSON-stdout│
arch/plan-claude.md:65:  `measure`, `ping`; события воркер→GUI: `state{idle|recording|processing}`, `level{rms,peak}` (≤ 30/с), `silent`,
arch/plan-claude.md:67:  `measure{peak_rss_mb,pss_mb}`. Аудио **никогда** не сериализуется и не пишется на диск (§9 инварианты).
arch/plan-claude.md:78:| `core/settings.py` | `settings.json`: `schema_version`, миграции вперёд, атомарная запись (tmp+fsync+replace), `.bak` при порче, сброс | JSON-документ, сигнал `changed(key)` | paths | unit: миграции v1→vN, битый файл → `.bak` + дефолты |
arch/plan-claude.md:93:| `worker/measure.py` | `VmHWM` и `Pss` из `/proc/self/{status,smaps_rollup}` после первого прогона; отдаёт в GUI → `measurements.json`; сброс при смене ревизии/потоков | — | — | unit на фикстурах `/proc` |
arch/plan-claude.md:94:| `models/catalog.py` | Манифест: встроенный (`/usr/share/astra-voice/catalog.json` + `.sig`) → последний скачанный (`~/.local/state/.../catalog.json`); `jsonschema` (`catalog.schema.json`), `manifest_version`, `gpgv`-подпись (наш keyring или `catalog_pubkey`), слияние с локальными замерами и статусами; фильтры «язык», «только отечественные» | `CatalogModel` (`QAbstractListModel`) для QML | verify, store | unit: схема, подпись (тестовый ключ), неизвестные поля игнорируются, старший `manifest_version` → отказ |
arch/plan-claude.md:155:   после первой диктовки → `measurements.json` → карточка «замерено». Из файла/папки — тот же путь с шага sha256.
arch/plan-claude.md:196:data/           catalog.json (+ .sig), catalog.schema.json, icons/ (21 глиф + hicolor/ + tray/), sounds/{start,stop}.ogg,
arch/plan-claude.md:201:scripts/        gen_theme.py, build_manifest.py, measure_model.py, release.sh, make_virtual_mic.sh, e2e/*.sh (P21½)
arch/plan-claude.md:216:  `python3-numpy (>= 1:1.22.4)`, `python3-requests`, `python3-jsonschema`, `python3-packaging`, `python3-protobuf`, `python3-flatbuffers`,
arch/plan-claude.md:266:| S3 рантайм + замер | venv в scratch: `onnxruntime==1.29.0` + `onnx-asr==0.12.0` с **системным** numpy 1.24.2; загрузка `istupakov/gigaam-v3-onnx` (e2e_rnnt 226 МБ; при «да» — e2e_ctc и rnnt, ~680 МБ); `scripts/measure_model.py` | `import onnxruntime` ok; 10 прогонов тестового wav 6 с: медиана и p95 инференса при 2/4 потоках; `VmHWM`; число `.so` = 3; `objdump -T` без `__isoc23_`; **порог:** p95 ≤ 300 мс хотя бы у e2e_rnnt или e2e_ctc | дефолт e2e_ctc; при > 400 мс у обоих — `SherpaEngine` для GigaAM v3 |
arch/plan-claude.md:275:| **M2 Воркер и движок** (2.5, 8.3, 2.8-часть) | Супервизор, IPC, воркер, `OnnxAsrEngine` (раскладка `onnx-asr-gigaam-v3`), `model.load` из локальной папки, `transcribe.file`, `measure`, рестарт при краше, выгрузка по простою | `astra-voice --debug-transcribe data/test/test-ru-6s.wav` печатает текст с «проверка» и `t_ms`; `kill -9 <worker>` → новый pid ≤ 1 с, модель перезагружена, трей/пилюля показали ошибку один раз | `pytest -m engine` (CI с кэшем модели); `pytest tests/unit/test_supervisor.py` | RTFx (см. S3); ORT-потоки |
arch/plan-claude.md:338:- В apt Astra есть: `python3-xlib` 0.33, `python3-evdev` 1.6.1, `python3-requests` 2.32.5 (установлен), `python3-jsonschema`
        if not stream:
            r.content

        return r

    def merge_environment_settings(self, url, proxies, stream, verify, cert):
        """
        Check the environment and merge it with some settings.

        :rtype: dict
        """
        # Gather clues from the surrounding environment.
        if self.trust_env:
            # Set environment's proxies.
            no_proxy = proxies.get("no_proxy") if proxies is not None else None
            env_proxies = get_environ_proxies(url, no_proxy=no_proxy)
            for k, v in env_proxies.items():
                proxies.setdefault(k, v)

            # Look for requests environment configuration
            # and be compatible with cURL.
            if verify is True or verify is None:
                verify = (
                    os.environ.get("REQUESTS_CA_BUNDLE")
                    or os.environ.get("CURL_CA_BUNDLE")
                    or verify
                )

        # Merge all the kwargs.
        proxies = merge_setting(proxies, self.proxies)
        stream = merge_setting(stream, self.stream)
        verify = merge_setting(verify, self.verify)
        cert = merge_setting(cert, self.cert)

        return {"proxies": proxies, "stream": stream, "verify": verify, "cert": cert}

    def get_adapter(self, url):
        """
        Returns the appropriate connection adapter for the given URL.

        :rtype: requests.adapters.BaseAdapter
        """
        for prefix, adapter in self.adapters.items():
            if url.lower().startswith(prefix.lower()):
                return adapter

   190	**Бизнес-правила.**
   191	1. Устройство ввода — настройка «Микрофон: системный по умолчанию / конкретное»; список — из PipeWire/ALSA.
   192	2. Ошибки и тексты (русский, путь ALSE/KDE):
   193	   - нет устройств: «Микрофон не найден. Подключите микрофон или гарнитуру и попробуйте снова» + кнопка «Обновить список»;
   194	   - устройство занято/ошибка открытия: «Микрофон недоступен (код). Повторить» — ретрай 3 раза по 300 мс автоматически;
   195	   - **тишина**: если за 2 с записи пик уровня < −60 дБ (порог настраиваемый) → пилюля «Микрофон молчит» и уведомление с кнопкой **«Перезапустить звуковую службу»** (`systemctl --user restart wireplumber`, без root) и ссылкой «Что это» (гонка при загрузке, профиль карты). После перезапуска — автоматическая повторная проверка уровня.
   196	3. Первая запись после входа в систему при автозапуске: если открытие устройства не удалось — повторить через 1 с (до 3 раз) до показа ошибки.
   197	4. Утилита никогда не пишет аудио на диск (ни временных wav); буфер записи живёт в памяти и очищается после распознавания или отмены.
   198	**Исключения.** `wireplumber` не установлен/не отвечает → кнопка неактивна, текст «Обратитесь к администратору: служба звука не отвечает»; уровень есть, но результат пустой → «ничего не распознано» (это не ошибка микрофона).
   502	## 8. Нефункциональные требования
   503	| Область | Требование |
   504	|---|---|
   505	| Офлайн-первый | Все функции, кроме четырёх сетевых действий (F14), работают без сети; ни одна сетевая ошибка не блокирует UI и не показывает модалку |
   506	| Старт | До готовности трея ≤ 1,5 с; до готовности к диктовке (модель в памяти) ≤ 5 с на эталонной машине; при автозапуске — без `sleep`-костылей (прогноз Researcher #2 для PyQt5 из apt: холодный старт GUI 0,15–0,6 с, low) |
   507	| Задержка | «Отпустил → текст» p95 ≤ 0,5 с (6-с фраза, GigaAM v3 e2e_rnnt, модель в памяти, эталон); ориентир для 4 ядер/8 ГБ — ≤ 1,5 с (оценка, проверить) |
   508	| Память | GUI + трей в покое без модели ≤ 120 МБ RSS (допущение для PyQt5/QML — проверить спайком); воркер с GigaAM v3 ≤ 450 МБ (замер 415; для незамеренных моделей оценка 1,5–3× от int8-весов); суммарно ≤ 600 МБ; замер — VmHWM воркера, дополнительно PSS из `smaps_rollup` |
   509	| CPU в покое | ≤ 1 % CPU в простое (нет опроса микрофона, нет таймеров чаще 1 Гц) |
   510	| Сеть | Только хосты из `PRIVACY.md`; проверки выключены, пока пользователь их не включил; ≤ 1 проверка/24 ч на каждый источник; таймаут 3 с; уважение `RateLimit`/`Retry-After`, `ETag`; прокси из окружения; TLS-проверка не отключается, корпоративный CA — из системного хранилища; User-Agent проекта |
   511	| Приватность | Аудио — только в ОЗУ, никогда на диск; распознанный текст — не логируется и не сохраняется (кроме «последний текст» в памяти); опция защиты истории Klipper |
   512	| Надёжность | Крах воркера не роняет GUI; single-instance; ретрай трея; корректное завершение по `Выход`/SIGTERM (восстановление буфера, освобождение микрофона) |
   513	| Совместимость | Astra Linux SE 1.8.x, X11, KDE Plasma 5.27 и Fly; Python 3.11; PyQt5 5.15; glibc 2.36 (шим `__isoc23_*` для onnxruntime — архитекторам) |
   514	| Упаковка | Один deb, зависимости из репозиториев Astra + вендоренные python-пакеты; `postinst` без сети; воспроизводимая сборка одной командой; SBOM в релизе |
   515	| i18n | Русский — исходный язык всех строк; английский — полный второй; плюрализация; даты/числа по локали; без английских «хвостов» |
   516	| Доступность | Все настройки доступны с клавиатуры; контраст текста ≥ 4,5:1 в обеих темах; состояние записи никогда не передаётся только цветом (форма/текст/трей/звук); accessible-имена у контролов |

web search: 
exec
/bin/bash -lc "nl -ba arch/plan-codex.md | sed -n '238,258p'; rg -n 'onnx/|whisper-small|onnx-asr-gigaam|vocab|config' research/catalog-numbers.md | head -25; nl -ba arch/plan-synth.md | sed -n '30,82p'; sed -n '750,775p' /usr/lib/python3/dist-packages/requests/sessions.py" in /home/astra/Документы/astra-voice
 succeeded in 0ms:
   238	| **10–13.09: polkit из GUI** | Тестовый deb/helper | На отдельном стенде ровно один системный диалог; GUI не блокируется; cancel, отсутствие агента, bad signature; root повторяет проверки | `./tools/validate updater --polkit --fixture-upgrade`; `dpkg-query -W astra-voice` |
   239	| **14–20.09: основной тракт** | Все ранние спайки | PTT/toggle → PCM → VAD → ASR → вставка; отмена, очередь, терминальные комбинации, тишина, worker crash | `python3 -m pytest tests/unit tests/integration`; `./tools/validate dictation --virtual-mic` |
   240	| **21–30.09: v0.1 «Диктовка работает»** | Основной тракт | Подписанный deb одной командой; дефолтная модель скачивается/импортируется; пилюля/трей/минимальные настройки; S2–S5, S12, S14; метрики Ц2/Ц7 | `./tools/build-deb --locked`; `./tools/validate release --version 0.1.0`; `./tools/validate e2e --suite v0.1` |
   241	| **01–07.10: каталог и ревизии** | v0.1, verified downloader | 12 пресетов в схеме; обязательные 9 готовы к smoke; T-one и Could проходят тот же путь; подпись/докачка/откат/ветки проверены | `./tools/validate catalog --all`; `./tools/validate downloads --faults`; `./tools/validate model-updates` |
   242	| **05–11.10: обновления/автозапуск** | Polkit-spike, NetworkPolicy | Трек A, безопасная ветка B, import deb, ETag/backoff; XDG и single-instance; нет запросов по умолчанию | `./tools/validate updater --all`; `./tools/validate autostart`; `./tools/validate privacy --network-denied` |
   243	| **12–15.10: v0.2** | Каталог, updater, autostart | Полный onboarding ≤5 мин при 10 Мбит/с и ≤2 мин из файла; ≥3 модели измерены; полный экран настроек, ru/en, Klipper; S1/S6–S9/S11/S13/S15 | `./tools/validate e2e --suite v0.2`; `./tools/benchmark --catalog --minimum-models 3`; `./tools/benchmark wer --model whisper-small-int8 --dataset russian-librispeech-test` |
   244	| **16–23.10: корпоративный режим** | v0.2 | Policy с блокировками, corp-only каталог, offline import, domestic enforcement, полный S10; диагностика и документы администратора | `./tools/validate policy --matrix`; `./tools/validate corporate --no-public-egress`; `./tools/validate diagnostics --redaction` |
   245	| **24–31.10: v1.0** | Корпоративный режим | Все 12 моделей smoke-tested; все применимые S1–S16; T3; release upgrade; длительный прогон без P0/P1; документы и атрибуции; G5 | `./tools/validate release --version 1.0.0`; `./tools/validate e2e --suite all`; `./tools/validate soak --hours 8` |
   246	
   247	| Протокол измерений | Правило |
   248	|---|---|
   249	| RAM | Новый процесс на модель/ревизию/настройку потоков; VmHWM после первого inference, дополнительно PSS. HWM предыдущей модели не наследуется |
   250	| Invalidation | Ключ: model ID, revision, quantization, runtime/build, threads, CPU, preprocessing. Смена любого значимого поля сбрасывает локальную достоверность |
   251	| Скорость | Отдельные времена preprocessing, inference, queue, release→text-ready, paste. RTFx = длительность аудио / время обработки; p95 пользовательской задержки включает вставку |
   252	| Выборка | Первый прогон холодный; RTFx — медиана ≥5 тёплых; дефолтная задержка — ≥50 шестисекундных фраз |
   253	| Машина | Core Ultra 7 255U; фиксировать питание, профиль CPU, фоновые процессы и температуру. Проверить 1/2/4 потока без автоматической смены заданного дефолта |
   254	| Качество | WER только на согласованном Russian LibriSpeech test; версия нормализатора фиксируется. Для Whisper small до замера остаётся no_data |
   255	| Виртуальный микрофон | Harness создаёт `module-null-sink` и `module-remap-source`, явно выбирает источник; ждёт `CaptureReady`, затем запускает `paplay --device=av_test …wav`; удаляет только созданные модули |
   256	| Настоящая вставка | E2E-приёмник подтверждает изменение текста; событие отправки Ctrl+V само по себе не доказывает успешную вставку |
   257	
   258	4. Риски, альтернативы и границы безопасности должны быть проверяемыми условиями приёмки.
25:| 1a | ↳ вариант под onnx-asr | `istupakov/gigaam-v3-onnx` (`v3_e2e_rnnt_*`) | `322c3b29492673eb7d0b434bfa9dfb8653e34d02` | enc+dec+joint int8 + vocab + yaml | 226 431 968 | **226,4** | те же | те же | да | ru | [MIT](https://huggingface.co/istupakov/gigaam-v3-onnx/blob/main/LICENSE.txt) | Сбер — отеч. | high |
28:| 4 | GigaAM Multilingual CTC 220M | `istupakov/gigaam-multilingual-ctc-onnx` | `458860e1983aef670dd9795fb6af603c82767d5d` | multilingual_ctc.int8 + vocab + yaml + config | 224 763 921 | **224,8** | **8,43 %** | ✖ нет int8-замера (fp32 = 58,5) | нет | ru, kk, ky, uz, en | [MIT](https://huggingface.co/istupakov/gigaam-multilingual-ctc-onnx) (тег `license:mit`) | Сбер — отеч. | high / RTFx **low** |
29:| 5 | T-one | `t-tech/T-one` | `106f3b0b32a9e107eb613312e4ebc61ff3d53926` | **model.onnx (fp32!)** + vocab + config | 144 196 802 | **144,2** | **6,57 %** | ✖ int8 = `N/A` (fp32 = 26,3) | нет | ru | [Apache-2.0](https://huggingface.co/t-tech/T-one) (тег) | Т-Банк / T-Tech (АО «ТБанк», РФ) — **отеч.** | high |
30:| 6 | Vosk ru 0.54 | `alphacep/vosk-model-ru` | `df6a54a4d8e5d43e82675e4f5dba2d507731a0d1` | am-onnx/{encoder,decoder,joiner}.int8 + lang/tokens.txt | 72 468 733 | **72,5** | **9,89 %** | **70,5** | нет | ru | [Apache-2.0](https://huggingface.co/alphacep/vosk-model-ru) (тег) | Alpha Cephei Inc — **юрлицо США (DE)**, команда РФ | high / происх. **med** |
34:| 9 | **Whisper small** | `onnx-community/whisper-small` | `36050c46d777d46dc4b5f43f6d90574fc38f8732` | encoder_model_int8 + decoder_model_merged_int8 + токенайзер | 253 466 497 | **253,5** | **нет по протоколу** (см. §2) | **нет по протоколу** (см. §2) | да | 99 | **Apache-2.0** (по [openai/whisper-small](https://huggingface.co/openai/whisper-small)) | OpenAI — зарубеж. | размер high / цифры **low** |
35:| 9a | ↳ вариант sherpa-onnx | `csukuangfj/sherpa-onnx-whisper-small` | `8f3c18b358db4d1f2fc1eae49d75cd20989e4309` | small-encoder.int8 + small-decoder.int8 + small-tokens | 375 485 327 | **375,5** | — | — | да | 99 | лицензии в репо нет | OpenAI — зарубеж. | high / лиц. low |
38:| 11 | GigaAM Multilingual Large CTC | `istupakov/gigaam-multilingual-large-ctc-onnx` | `07665ab5e54371dd1ac7b8b10f06478003723573` | multilingual_large_ctc.int8 + vocab + yaml + config | 591 646 507 | **591,6** | **5,55 %** | ✖ нет int8-замера (fp32 = 30,4) | нет | ru, kk, ky, uz, en | [MIT](https://huggingface.co/istupakov/gigaam-multilingual-large-ctc-onnx) (тег) | Сбер — отеч. | high / RTFx **low** |
39:| 12 | NeMo FastConformer ru pc (CTC) | `istupakov/stt_ru_fastconformer_hybrid_large_pc_onnx` | `fb6753736c56de3259d78a26a488548c84b9dccb` | model.int8 + vocab.txt + config | 131 577 107 | **131,6** | **13,10 %** | **70,6** | да | ru | [CC-BY-4.0](https://huggingface.co/nvidia/stt_ru_fastconformer_hybrid_large_pc) — **атрибуция обязательна** | NVIDIA (США) — зарубеж. | high |
40:| 12a | ↳ тот же репо, RNN-T-декодер | там же | там же | encoder-model.int8 + decoder_joint-model.int8 + vocab | 136 402 626 | **136,4** | 11,57 % | 54,2 | да | ru | CC-BY-4.0 | NVIDIA — зарубеж. | high |
56:**Рекомендация:** Whisper small — единственная позиция каталога без цифр по протоколу. Либо замерить самим (`onnx-asr` умеет `onnx-community/whisper-small`, датасет `istupakov/russian_librispeech` открыт), либо показывать полоски как «нет данных» — по §7.1 выдумывать нельзя. Оценка 20–21 % ставит small между turbo (10,1) и base (38,3) — правдоподобно, но это интерполяция.
127:| Whisper small в onnx-asr | не оговорено | Поддержан **только** как `onnx-community/whisper-small` (optimum-экспорт); отдельного istupakov-репо нет |
    30	
    31	## 3. Развилки и решения Юрки
    32	
    33	| # | Развилка | #1 | #2 | Решение | Предпосылка / проверка |
    34	|---|---|---|---|---|---|
    35	| Р1 | Где захват звука | в воркере, `libpulse-simple` через ctypes, PCM не покидает процесс | в GUI, `QAudioInput` в QThread, PCM в воркер через `memfd` | **#1** | спайк S5 доказывает ctypes-захват на PipeWire-pulse; провал → схема #2 (memfd) как запасная, интерфейс `AudioSource` общий |
    36	| Р2 | Шим `__isoc23_*` и число `.so` | не нужен (проверено `objdump`: ORT 1.29.0 max GLIBC_2.28), **3 `.so`** | нужен, 4 `.so` | **#1** + гейт #2 | CI: `objdump -T` по вендорным `.so` (max ≤ GLIBC_2.36, 0 `isoc23`) и **счётчик ELF = 3** останавливает сборку при лишнем |
    37	| Р3 | Версия ORT | 1.29.0 (без `sympy`) | 1.23.2 | **1.29.0**, пин sha256 | S3: `import onnxruntime` + инференс на Debian 12 и ALSE; откат — 1.24.4 (GLIBC_2.27, +`python3-sympy` из apt) |
    38	| Р4 | Отмена распознавания | сообщение/kill воркера | `RunOptions.terminate` + точки между сегментами, ≤1 с; kill+reload = непрохождение S14 | **#2** | S3 доказывает ≤1 с для GigaAM и Whisper; kill+reload — только аварийное восстановление |
    39	| Р5 | Смена активной модели | смоук новой ревизии, R13: если `MemAvailable` < 2× — выгрузить текущую | временный второй воркер с кандидатом, старая обслуживает записи; пик двух моделей ≤600 МБ не влезает | **гибрид**: переключение на границе сессии; кандидат грузится во **временный воркер только если `MemAvailable ≥ 1,5× веса кандидата + 200 МБ**, иначе выгрузка → загрузка с состоянием `switching` (спека) | PRD §8 «≤600 МБ» = установившийся режим; пик при смене ограничен `MemAvailable`, не константой. **PRD §8 дополнить одной строкой** |
    40	| Р6 | Сеть | `python3-requests` в `QThreadPool` | `QNetworkAccessManager` | **#1** (requests) + правила #2 | jitter 0–10/0–30 мин, ETag/304, `Retry-After`/RateLimit, 403/429 без циклов, deadline 3 с с редиректами, системный CA + `SSL_CERT_FILE`, редиректы только на разрешённые хосты |
    41	| Р7 | Позиция пилюли | `availableGeometry` | + проверка панелей через EWMH struts | **оба** | S1 проверяет панель > 48 px и боковые панели; расхождение `availableGeometry` ↔ struts → struts |
    42	| Р8 | Шрифты | R11 «нет на ALSE — вкладывать» | выбирать через `QFontDatabase` | **Depends: `fonts-pt-root-ui`, `fonts-pt-mono`** (в repository-main), в пакет не вкладывать; резерв PT Astra Sans через `QFontDatabase` | факт Юрки 2026-09-09: `apt-cache policy` — кандидат 1.001/1.003 из download.astralinux.ru |
    43	| Р9 | Ручная проверка обновлений | разрешена | разрешена, S9-A5 назван ошибкой | **разрешена** (решение 2026-09-08, F12 уже поправлен) | **PRD S9-A5 поправить** под F12 |
    44	| Р10 | Меню трея | рисует Plasma (DBusMenu) — стиль спеки §9.2 недостижим | — | **#1**: меню системное; DesignReviewer сверяет состав и неактивность пунктов, не стиль | запись в спеку §9.2 |
    45	| Р11 | Валидация вех | команды pytest/скрипты | `tools/validate <тема>` и `tools/benchmark` как CLI | **#2** поверх #1 | CLI создаются вместе с функцией; в execution-pack команды из них |
    46	| Р12 | Загрузчик/манифест — защита | sha256, `/resolve/{rev}/`, атомарность, смоук | + запрет `..`/абсолютных путей/symlink при импорте, лимиты (≤64 файлов/модель, ≤2 ГиБ/файл, каталог ≤1 МиБ), git-SHA1 ≠ sha256, диск ×1,2, зеркала отдают одинаковые байты | **объединить** | тесты на вредоносные пути в unit |
    47	| Р13 | CI | lint/unit/xvfb/engine/deb/release | + ABI-pipeline (ELF inventory, readelf, реальный инференс), packaging install→upgrade→purge, две сборки на воспроизводимость, Actions по SHA, секрет подписи вне PR-job | **объединить** | — |
    48	
    49	Корневой развилки (разные последствия для продукта) **нет** → ⛔ G4 не требуется.
    50	
    51	## 4. Синтез-план (базовый #1 + заимствования)
    52	- **Процессы:** GUI (`astra-voice`, PyQt5 + QtQuick Controls 2, трей, хоткей, вставка, сеть, обновлятор) ∥ воркер
    53	  (`astra-voice.worker`: `PulseSimpleSource` → кольцевой буфер → VAD → `OnnxAsrEngine`; модель в памяти; `measure`) ∥
    54	  одноразовый `update-helper` (root). IPC — `socketpair`, len-prefixed JSON (`hello` с версиями протокола/сборки/рантайма,
    55	  `model.load/unload`, `record.start/stop/cancel`, `transcribe.file`, `measure`; события `state/level/silent/limit/result/
    56	  empty/error/model.loaded/measure/cancelled`). Отменённый `utterance_id` никогда не вставляется.
    57	- **Компоненты и модули** — таблица #1 §3 (30 модулей) целиком; дополнения из #2: `Отмена ASR` (RunOptions), `MicRecovery`
    58	  (сырой пик до VAD, перезапуск WirePlumber только кнопкой), `TrackResolver` (детект `DIGSIG_ELF_MODE` как данные, «не знаю» → B).
    59	- **Рантайм и каталог:** `onnx-asr` 0.12.0 + ORT 1.29.0 (пины sha256, `packaging/wheels.lock`); раскладки манифеста — #1 §4.1
    60	  (7 layout), `engine: onnx-asr` для всех 12; размеры для №2–3 пересчитать по istupakov-репо (#2). Манифест — правила #2
    61	  (закрытый enum layout, полный `files[]`, `/resolve/{sha}/`, подпись каталога → схема → пути → sha256).
    62	- **Безопасность:** инварианты #1 §6 (10) ∪ #2 (12): helper не доверяет GUI, не принимает URL/аргументы apt; policy применяется
    63	  к операциям; неподписанное не активируется; аудио только в ОЗУ; хосты из PRIVACY; иконка «слушаю» обязательна. → **T1**.
    64	- **Упаковка:** #1 §7 (структура, Depends из repository-main + `fonts-pt-root-ui`, `fonts-pt-mono`; 3 `.so`; `python3 -I`
    65	  лаунчер; vendor в `/usr/lib/astra-voice/vendor`) + гейты #2 (ELF-инвентарь, ABI-аудит, воспроизводимость, SBOM CycloneDX,
    66	  hardening Actions). Зеркало весов — релиз `models-YYYY.MM`.
    67	- **Вехи:** M0 спайки S1–S5 (#1) с датами #2 (до 13.09) → M1 скелет/упаковка → M2 воркер/движок → M3 звук → M4 цикл
    68	  диктовки → M5 онбординг/первая модель → **v0.1 30.09** → M6 каталог → M7 сеть/проверки → M8 обновлятор A → M9 автозапуск/
    69	  уведомления/звуки/разделы → **v0.2 15.10** → M10 корпоративный контур/трек B → M11 полировка → **v1.0 31.10**; v1.1 — ГОСТ.
    70	  Пороги: S3 p95 инференса ≤ 300 мс (иначе дефолт e2e_ctc, затем `SherpaEngine`); отмена ≤ 1 с; RSS воркера ≤ 450 МБ.
    71	
    72	## 5. Что ещё нужно от человека (не G4 — рутинные допуски)
    73	1. Спайк S3 качает модели с HF (226 МБ минимум, ~680 МБ для трёх вариантов GigaAM) в scratch-venv — «да» на трафик/диск.
    74	2. Спайк S2 (polkit-помощник) требует **одного `sudo`** на машине: положить тестовый `.policy` и помощник. Делать на машине
    75	   заказчика или готовить ВМ ALSE 1.8 (её же — для Fly-сессии S1 и чистого прогона Ц3)?
    76	3. Владелец закрытого ключа GPG для подписи релизов: заказчик (ключ у него, CI подписывает через секрет) или Юрка/репо.
    77	
    78	## 6. Правки в документах по итогам суда
    79	- PRD: S9-A5 → ручная проверка разрешена (как F12); §8 память — «≤600 МБ в установившемся режиме; пик при смене модели
    80	  ограничен `MemAvailable`, не константой»; §7.3 пример манифеста — `engine: onnx-asr` для всех записей.
    81	- Спека §9.2: меню трея — системное (DBusMenu), сверка по составу. Spec §8.1 — struts.
    82	- `decisions/log.md`: запись «Архитектура принята (синтез)» после челленджа Astra и T1.
    def merge_environment_settings(self, url, proxies, stream, verify, cert):
        """
        Check the environment and merge it with some settings.

        :rtype: dict
        """
        # Gather clues from the surrounding environment.
        if self.trust_env:
            # Set environment's proxies.
            no_proxy = proxies.get("no_proxy") if proxies is not None else None
            env_proxies = get_environ_proxies(url, no_proxy=no_proxy)
            for k, v in env_proxies.items():
                proxies.setdefault(k, v)

            # Look for requests environment configuration
            # and be compatible with cURL.
            if verify is True or verify is None:
                verify = (
                    os.environ.get("REQUESTS_CA_BUNDLE")
                    or os.environ.get("CURL_CA_BUNDLE")
                    or verify
                )

        # Merge all the kwargs.
        proxies = merge_setting(proxies, self.proxies)
        stream = merge_setting(stream, self.stream)

web search: 'Neither'
codex
Восстановление тоже перенесено не полностью: в плане №2 есть журнал установки и отдельные поколения сессий, а в синтезе сохранены помощник «без состояния» и базовый тракт вставки. Отдельно отмечу Р5: состояние `switching` не сохраняет работу хоткея на старой модели после её выгрузки, а порог по размеру весов не гарантирует достаточную память для смоука.

codex
Синтез пока не согласован: есть прямые противоречия и незамкнутые контракты. Файлы не менял; рамки `decisions/log.md` не пересматриваю.

Обозначения: [С — синтез](/home/astra/Документы/astra-voice/arch/plan-synth.md), [#1 — базовый план](/home/astra/Документы/astra-voice/arch/plan-claude.md), [#2 — заимствования](/home/astra/Документы/astra-voice/arch/plan-codex.md), [PRD](/home/astra/Документы/astra-voice/PRD.md).

| № / ось | Серьёзность | Где именно | Несостыковка | Минимальная правка |
|---|---|---|---|---|
| И1 · интерфейсы | **blocker** | С §3 Р4, §4 IPC; #1 §2, §3 `Engine`; #2 «Отмена ASR» | Добавлены `RunOptions` и событие `cancelled`, но сохранён `transcribe(audio) → text` без контекста отмены; у `record.cancel` не определена адресация выполняемого распознавания. ORT прекращает только вызовы, использующие **конкретный экземпляр** RunOptions. [API ORT](https://onnxruntime.ai/docs/api/python/api_summary.html#onnxruntime.RunOptions.terminate) | Передавать контекст задания с `utterance_id` и поколением; связать его с RunOptions всех вызовов декодера и точками отмены. Определить адресный cancel и подтверждение после освобождения ресурсов; отмену преобразовывать в `cancelled`, сохраняя модель. |
| И2 · интерфейсы | major | С §4 «компоненты #1 целиком»; #1 §3 `worker/ipc.py`, `audio.py`; PRD S14-A2 | Автомат `idle/recording/processing` взаимоисключающий, хотя во время обработки должна приниматься следующая запись. Один описанный буфер «очищается после результата/отмены»: не определено, как не очистить аудио следующей сессии. | Разделить состояния захвата и обработки внутри существующего воркера; закрепить буфер за ID. Зафиксировать предел: одно распознавание плюс одна дополнительная запись, адресная очистка и порядок результатов. |
| И3 · интерфейсы | major | С §4 каталог/IPC; #1 §2 `model.load`, §3 `Engine`, §4.1 | `load(dir,layout,threads)` не получает `variant`, хотя три GigaAM имеют одинаковый layout, а выбор варианта обязателен. `model.loaded` возвращает ID/ревизию без определённого источника этих значений; `transcribe.file{path}` не указывает, какой кандидат проходит смоук. | Передавать проверенный дескриптор модели: ID, ревизию, layout, variant/precision; привязать смоук и ответ к конкретному воркеру и запросу загрузки. |
| И4 · интерфейсы | major | С Р1, §4 `MicRecovery`; #1 §3 `AudioSource`, §5.1; #2 строка `MicRecovery` | Recovery из #2 зависит от GUI/QProcess, а устройство теперь принадлежит воркеру. Не определены закрытие источника перед рестартом и подтверждение повторного открытия. Базовое повторное `record.start` может возобновить диктовку после отпускания хоткея или отмены. | GUI выполняет рестарт службы только по кнопке; воркер подтверждает закрытие/открытие источника. Повторная проверка уровня — отдельный режим без вставки. Поздние ретраи проверяют ID/актуальность сессии; отдельно определить обычные и послелогинные ретраи. |
| И5 · интерфейсы | major | С Р6; #1 §3 `net/http.py`; #2 «Таймаут», «Offline» | `requests` в QThreadPool сам не обеспечивает общий deadline 3 с и прекращение текущего запроса при offline. Connect/read timeout не ограничивает полное время операции; отмена ожидающего задания не прерывает уже выполняющийся запрос. [Requests](https://requests.readthedocs.io/en/latest/user/advanced/#timeouts) | Дописать контракт HttpClient: единый deadline на DNS/соединение/редиректы/повтор/чтение, прекращение транспорта при отмене и отбрасывание позднего ответа. Проверить медленное тело, DNS и цепочку редиректов. |
| И6 · интерфейсы | major | С Р6; #1 §3 `requests.Session`; #2 «TLS/proxy» | `SSL_CERT_FILE` не является штатной CA-переменной Requests: используются `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`. Автоматические редиректы также не проверяют проектный список разрешённых хостов. Простое сохранение Session не реализует заимствованные правила. [Requests](https://requests.readthedocs.io/en/latest/user/advanced/#ssl-cert-verification) | Явно преобразовать `SSL_CERT_FILE` в `verify`, определить приоритет CA-настроек; проверять каждый следующий HTTPS URL **до** запроса, отключив автоматическое следование редиректам. |
| И7 · интерфейсы | **blocker** | С §4 упаковка; #1 §3 `worker/supervisor.py`, §7.2 | Лаунчер добавляет private vendor в собственный `sys.path`, но воркер запускается как `[sys.executable,'-m','astra_voice.worker']`. Новый Python не наследует этот `sys.path`; также не наследуется флаг `-I`. Контракт не обеспечивает ни импорт вендорного ORT, ни заявленную изоляцию воркера. | Запускать воркер через тот же изолированный bootstrap с явным `-I` и добавлением root-owned vendor до импортов. Проверку сборки выполнять этим же путём. |
| И8 · интерфейсы | **blocker** | С §4 `TrackResolver` и объединение инвариантов; #1 §3 `update_helper.py`; #2 «Root-проверка», инвариант admin-track | В унаследованном helper перечислены проверки пакета, но отсутствует повторная проверка policy/ЗПС. Добавление TrackResolver в GUI не обеспечивает инвариант #2: прямой вызов helper не должен обходить трек B. В root-процессе также нельзя определять права инициатора по группам самого helper. | Включить чтение root-owned policy и конфигурации ЗПС непосредственно перед apt; запрет трека B действует независимо от GUI. Разделить GUI-оценку прав инициатора и привилегированную проверку разрешённости операции. |
| Д1 · данные | major | С §6 правка PRD; #1 §4.1; PRD §7.3, строки 456–493 | Предписана замена только `engine`. Тогда у GigaAM останутся sherpa-layout, csukuangfj-репозиторий, его ревизия и файлы. Пример Whisper содержит только два ONNX, хотя заимствованное правило требует также tokenizer/config и полный набор используемых файлов. | Перегенерировать пример целиком: engine/layout/variant, repo/revision, все пути/размеры/sha256 и `disk_bytes`. Схема и адаптер используют выбранные семь layout из #1, включая единое имя Vosk-layout. |
| Д2 · данные | major | С Р12, §4 каталог; #1 §6.6, §7.3; #2 «Ревизии»; PRD §7.3 `sources` | Правило «URL всегда `/resolve/{revision}/`» применено ко всем источникам, но GitHub Releases и corp используют `base`. В примере у них вообще нет обязательного `sources[].revision`. Единый буквальный URL-контракт несовместим с выбранными зеркалами. | Ограничить `/resolve/{sha}/` источником HF. Для GitHub/corp явно определить отображение пути и идентичность набора/ревизии; каждый источник обязан соответствовать тем же `files[]` и sha256. Исправить пример и схему одновременно. |
| Д3 · данные | major | С Р6, §4 заимствования; #1 §5.3, `update-cache.json`; #2 «Экспорт модели», «Оригинал Сбера», «Разрешение установки» | Базовый поток сравнивает `targetCommit(update_watch)` с `pinned_revision`. В PRD watcher смотрит `ai-sage/GigaAM-v3`, а установленный экспорт после синтеза — `istupakov/gigaam-v3-onnx`: SHA разных репозиториев будут давать ложное обновление. Формат кэша этого различия не фиксирует. | В `update-cache` разделить upstream/export; ключ наблюдения — repo+branch, сравнение с предыдущим SHA того же ключа. Хранить ETag, срок следующей проверки и backoff по источнику. Установку разрешать только по новому подписанному набору каталога. |
| Д4 · данные | major | С Р5, Р11; #1 §3 `worker/measure.py`; #2 «Протокол измерений», строки 249–250; PRD §7.1 | #1 обещает сброс замера после смены ревизии/потоков, сохраняя процесс; #2 требует новый процесс для такого замера. Сброс JSON не сбрасывает `VmHWM` процесса: малая модель может получить пик прежней большой. Ответ `measure` также не содержит идентичности измеренной конфигурации. [Linux proc](https://man7.org/linux/man-pages/man5/proc_pid_status.5.html) | Сохранить правило #2 о свежем процессе для достоверного замера; даже после последовательной выгрузки не переиспользовать старый HWM. Передавать и хранить полный ключ измерения из #2; поздние метрики другой конфигурации не перекрывают карточку. |
| Н1 · инварианты | **blocker** | С Р5, §6; #1 §3 `models/installer.py`; PRD F7.5, S14 | Ветка «выгрузка → загрузка» лишает хоткей старой модели, хотя PRD и унаследованный installer обещают её работу до готовности новой. Название `switching` этого не исправляет. Переключение сразу после отмены также может превратить готовность ≤1 с в ожидание загрузки. | При нехватке памяти сохранить старую модель и отказать/отложить переключение — это уже предусмотрено #2. Граница переключения должна учитывать текущую обработку и дополнительную запись; отмена не запускает скрытую выгрузку. |
| Н2 · инварианты | major | С Р5; #2 риск «Две модели вызывают OOM»; PRD §7.2, §8 | `1,5× веса + 200 МБ` — не верхняя оценка пика кандидата: сам PRD допускает 1,5–3×, а для fp32 правило неприменимо. Проверка текущего MemAvailable не резервирует память. Следовательно, заявленный «пик ограничен MemAvailable» из этой формулы не следует. | Использовать пик загрузки/смоука конкретной конфигурации с запасом, для неизвестного — консервативную оценку. Назвать проверку предварительной оценкой, а не гарантией; при сбое кандидата сохранять старую модель. |
| Н3 · инварианты | major | С §4 последний абзац, §6; PRD §7.2 и §8 | Порог «RSS воркера ≤450 МБ» в синтезе потерял привязку к GigaAM. Общее «≤600 МБ в установившемся режиме» также не объясняет Whisper turbo (~2 ГБ) и Multilingual Large (~1,1 ГБ), уже входящие в каталог. | Явно привязать 450/600 МБ к приёмочному сценарию с дефолтной GigaAM; отдельно обозначить измерения остальных моделей и временный пик переключения. |
| Н4 · инварианты | **blocker** | С Р2–Р3, §4 упаковка; #1 §4, §7.2 | ELF-часть Р2 подтверждается, но пакетный контракт ORT 1.29.0 — нет: локальный [METADATA](/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime-1.29.0.dist-info/METADATA) требует `protobuf>=4.25.8`; план рассчитывает на 3.21.12, и текущий apt-кандидат действительно 3.21.12. Успешный `import` не доказывает согласованность зависимостей. | Применить уже предусмотренный откат на 1.24.4 с проверкой его зависимостей либо подтвердить допустимый источник нужного protobuf. Добавить гейт METADATA↔Depends, сохранив три ELF и отсутствие шима. |
| Н5 · инварианты | **blocker** | С §2 root-helper, Р6, §4 безопасность; #1 §3 helper; #2 «Сеть apt» | Offline запрещает сеть, но унаследованная фиксированная команда apt может скачать зависимости. Гейт Requests на системный apt не распространяется. Правило #2 про `--no-download` в контракт helper не перенесено. | Зафиксировать отдельную сетевую стадию apt и её политику. При offline helper применяет фиксированный `--no-download`; нехватка зависимостей заканчивается передачей администратору. Определить передачу пользовательского запрета сети без возможности ослабить root-policy. |
| О1 · отказы | major | С §4 IPC/инварианты; #1 §2 рестарт, §3 `paste.py`; #2 инвариант «Отмена/крах/смена поколения» | Рестарт описан, но не определена судьба сессий и уже заведённых таймеров вставки. При краше во время записи PCM теряется; при краше после `result`, но до XTest, базовый PasteService ещё может вставить текст старого поколения. | На EOF/краш завершать затронутые сессии ошибкой, отменять их незавершённые вставки и не переигрывать запись. Перед XTest проверять поколение/ID; после уже отправленного XTest не повторять вставку. Восстанавливать буфер только пока им владеет эта операция. |
| О2 · отказы | major | С Р5, Р12; #1 §5.2; #2 «Атомарность», «Откат» | #1 задаёт `smoke → rename → current.json → model.load`, #2 — `rename → smoke → current.json` и восстановление транзакции. Сам порядок rename допустим в обоих вариантах, но синтез не определяет восстановление; в базовом потоке указатель уже изменён до окончательного `model.load`, который тоже может упасть. | Закрепить один порядок: проверенный набор → загрузка и смоук именно кандидата → подтверждённая готовность → атомарный commit указателя. Сохранять прежнюю ревизию и фазу операции; после обрыва восстанавливать последнюю подтверждённую. Обрыв сети относится к `.part`; локальный смоук сеть не использует. |
| О3 · отказы | major | С §4 helper «#1 целиком»; #1 §3 helper «не хранит»; #2 риск «Обрыв apt», шаг «Результат» | Stateless-helper с результатом только в stdout теряет исход при обрыве GUI/helper/питания. #2, напротив, требует журнал и проверку системного результата. Нельзя вывести «не установлено» только из исчезновения процесса: dpkg мог уже изменить пакет. | Сохранять журнал операции и установленную версию/состояние dpkg; при следующем запуске сверять фактический результат. Не убивать apt при закрытии GUI, не обещать rollback и не делать автоматический downgrade; незавершённую конфигурацию передавать администратору. |
| О4 · отказы | major | С §4 безопасность; #1 §6.10, §3 трей, §5.5; #2 инвариант видимости; PRD F3.6–7 | #1 требует иконку «слушаю» **всегда**, #2 останавливает запись лишь при потере **всех** индикаторов. При исчезновении SNI и работающей пилюле правила дают разное поведение. При `--hidden` окно вообще не создаётся, хотя fallback должен открыть его с баннером. | Применить более строгое принятое требование: при отсутствии трея остановить/не начинать запись, восстанавливать регистрацию. По истечении ретраев создавать и показывать окно даже после `--hidden`; явно определить возобновление доступности записи. |
| О5 · отказы | major | С §4 компоненты #1; #1 §2 `QLocalServer`, §3 `app.py`; #2 `Autostart / single-instance`; PRD S11-A4 | Назван QLocalServer, но отсутствует контракт двух одновременных стартов: получение владения до воркера/хоткея/записи settings, поведение проигравшего и обработка устаревшего сокета. Проверка «сервер отвечает?» сама по себе оставляет гонку. | Зафиксировать атомарное получение владения до побочных действий. Второй экземпляр отправляет `show` владельцу и завершается; очистка старого сокета допускается только после проверки отсутствия владельца, без удаления действующего endpoint. |

**Проверено, чисто:**

- **Р2: три ELF, без шима.** Read-only проверка всех ELF локальных ORT 1.29.0 и 1.24.4 подтвердила по три объекта, максимум GLIBC соответственно 2.28/2.27 и отсутствие `__isoc23_*`. Это подтверждение ABI-инвентаря; конфликт protobuf указан отдельно.
- **Выбор семи layout из #1 совместим с закрытым enum.** Различие `onnx-asr-vosk`/`vosk-onnx` разрешается уже выбранным приоритетом #1; проблема — синхронизация схемы, адаптера и примера PRD.
- **Цепочка проверки каталога согласована:** подпись → схема → ограничения путей → размеры/sha256. Правила #2 о hardlink и выходе external-data за каталог не противоречат базовому загрузчику.
- **Не-LFS sha256 согласован:** вычисление при публикации; git-SHA-1 не используется вместо sha256. Зеркала проверяются по одному набору ожидаемых хешей.
- **settings.json / policy.conf совместимы на уровне хранения:** версионированный пользовательский JSON, атомарная запись и `.bak`; отдельная read-only policy, эффективные значения и блокировки. Проблемы применения policy в helper вынесены в И8/Н5.
- **Отсутствие polkit-агента предусмотрено** в #1 §5.4/R9: диагностическое сообщение и административный путь. Нового механизма авторизации для синтеза не требуется.
- **GPG/gpgv, закреплённый keyring, повторная проверка в root-staging, `auth_admin` без `_keep` и запрет произвольных аргументов apt** взаимно совместимы.
- **Ручная проверка обновлений при выключенных автопроверках согласована** с журналом решений; в текущем PRD S9-A5 уже исправлен.
tokens used
105 446
Синтез пока не согласован: есть прямые противоречия и незамкнутые контракты. Файлы не менял; рамки `decisions/log.md` не пересматриваю.

Обозначения: [С — синтез](/home/astra/Документы/astra-voice/arch/plan-synth.md), [#1 — базовый план](/home/astra/Документы/astra-voice/arch/plan-claude.md), [#2 — заимствования](/home/astra/Документы/astra-voice/arch/plan-codex.md), [PRD](/home/astra/Документы/astra-voice/PRD.md).

| № / ось | Серьёзность | Где именно | Несостыковка | Минимальная правка |
|---|---|---|---|---|
| И1 · интерфейсы | **blocker** | С §3 Р4, §4 IPC; #1 §2, §3 `Engine`; #2 «Отмена ASR» | Добавлены `RunOptions` и событие `cancelled`, но сохранён `transcribe(audio) → text` без контекста отмены; у `record.cancel` не определена адресация выполняемого распознавания. ORT прекращает только вызовы, использующие **конкретный экземпляр** RunOptions. [API ORT](https://onnxruntime.ai/docs/api/python/api_summary.html#onnxruntime.RunOptions.terminate) | Передавать контекст задания с `utterance_id` и поколением; связать его с RunOptions всех вызовов декодера и точками отмены. Определить адресный cancel и подтверждение после освобождения ресурсов; отмену преобразовывать в `cancelled`, сохраняя модель. |
| И2 · интерфейсы | major | С §4 «компоненты #1 целиком»; #1 §3 `worker/ipc.py`, `audio.py`; PRD S14-A2 | Автомат `idle/recording/processing` взаимоисключающий, хотя во время обработки должна приниматься следующая запись. Один описанный буфер «очищается после результата/отмены»: не определено, как не очистить аудио следующей сессии. | Разделить состояния захвата и обработки внутри существующего воркера; закрепить буфер за ID. Зафиксировать предел: одно распознавание плюс одна дополнительная запись, адресная очистка и порядок результатов. |
| И3 · интерфейсы | major | С §4 каталог/IPC; #1 §2 `model.load`, §3 `Engine`, §4.1 | `load(dir,layout,threads)` не получает `variant`, хотя три GigaAM имеют одинаковый layout, а выбор варианта обязателен. `model.loaded` возвращает ID/ревизию без определённого источника этих значений; `transcribe.file{path}` не указывает, какой кандидат проходит смоук. | Передавать проверенный дескриптор модели: ID, ревизию, layout, variant/precision; привязать смоук и ответ к конкретному воркеру и запросу загрузки. |
| И4 · интерфейсы | major | С Р1, §4 `MicRecovery`; #1 §3 `AudioSource`, §5.1; #2 строка `MicRecovery` | Recovery из #2 зависит от GUI/QProcess, а устройство теперь принадлежит воркеру. Не определены закрытие источника перед рестартом и подтверждение повторного открытия. Базовое повторное `record.start` может возобновить диктовку после отпускания хоткея или отмены. | GUI выполняет рестарт службы только по кнопке; воркер подтверждает закрытие/открытие источника. Повторная проверка уровня — отдельный режим без вставки. Поздние ретраи проверяют ID/актуальность сессии; отдельно определить обычные и послелогинные ретраи. |
| И5 · интерфейсы | major | С Р6; #1 §3 `net/http.py`; #2 «Таймаут», «Offline» | `requests` в QThreadPool сам не обеспечивает общий deadline 3 с и прекращение текущего запроса при offline. Connect/read timeout не ограничивает полное время операции; отмена ожидающего задания не прерывает уже выполняющийся запрос. [Requests](https://requests.readthedocs.io/en/latest/user/advanced/#timeouts) | Дописать контракт HttpClient: единый deadline на DNS/соединение/редиректы/повтор/чтение, прекращение транспорта при отмене и отбрасывание позднего ответа. Проверить медленное тело, DNS и цепочку редиректов. |
| И6 · интерфейсы | major | С Р6; #1 §3 `requests.Session`; #2 «TLS/proxy» | `SSL_CERT_FILE` не является штатной CA-переменной Requests: используются `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`. Автоматические редиректы также не проверяют проектный список разрешённых хостов. Простое сохранение Session не реализует заимствованные правила. [Requests](https://requests.readthedocs.io/en/latest/user/advanced/#ssl-cert-verification) | Явно преобразовать `SSL_CERT_FILE` в `verify`, определить приоритет CA-настроек; проверять каждый следующий HTTPS URL **до** запроса, отключив автоматическое следование редиректам. |
| И7 · интерфейсы | **blocker** | С §4 упаковка; #1 §3 `worker/supervisor.py`, §7.2 | Лаунчер добавляет private vendor в собственный `sys.path`, но воркер запускается как `[sys.executable,'-m','astra_voice.worker']`. Новый Python не наследует этот `sys.path`; также не наследуется флаг `-I`. Контракт не обеспечивает ни импорт вендорного ORT, ни заявленную изоляцию воркера. | Запускать воркер через тот же изолированный bootstrap с явным `-I` и добавлением root-owned vendor до импортов. Проверку сборки выполнять этим же путём. |
| И8 · интерфейсы | **blocker** | С §4 `TrackResolver` и объединение инвариантов; #1 §3 `update_helper.py`; #2 «Root-проверка», инвариант admin-track | В унаследованном helper перечислены проверки пакета, но отсутствует повторная проверка policy/ЗПС. Добавление TrackResolver в GUI не обеспечивает инвариант #2: прямой вызов helper не должен обходить трек B. В root-процессе также нельзя определять права инициатора по группам самого helper. | Включить чтение root-owned policy и конфигурации ЗПС непосредственно перед apt; запрет трека B действует независимо от GUI. Разделить GUI-оценку прав инициатора и привилегированную проверку разрешённости операции. |
| Д1 · данные | major | С §6 правка PRD; #1 §4.1; PRD §7.3, строки 456–493 | Предписана замена только `engine`. Тогда у GigaAM останутся sherpa-layout, csukuangfj-репозиторий, его ревизия и файлы. Пример Whisper содержит только два ONNX, хотя заимствованное правило требует также tokenizer/config и полный набор используемых файлов. | Перегенерировать пример целиком: engine/layout/variant, repo/revision, все пути/размеры/sha256 и `disk_bytes`. Схема и адаптер используют выбранные семь layout из #1, включая единое имя Vosk-layout. |
| Д2 · данные | major | С Р12, §4 каталог; #1 §6.6, §7.3; #2 «Ревизии»; PRD §7.3 `sources` | Правило «URL всегда `/resolve/{revision}/`» применено ко всем источникам, но GitHub Releases и corp используют `base`. В примере у них вообще нет обязательного `sources[].revision`. Единый буквальный URL-контракт несовместим с выбранными зеркалами. | Ограничить `/resolve/{sha}/` источником HF. Для GitHub/corp явно определить отображение пути и идентичность набора/ревизии; каждый источник обязан соответствовать тем же `files[]` и sha256. Исправить пример и схему одновременно. |
| Д3 · данные | major | С Р6, §4 заимствования; #1 §5.3, `update-cache.json`; #2 «Экспорт модели», «Оригинал Сбера», «Разрешение установки» | Базовый поток сравнивает `targetCommit(update_watch)` с `pinned_revision`. В PRD watcher смотрит `ai-sage/GigaAM-v3`, а установленный экспорт после синтеза — `istupakov/gigaam-v3-onnx`: SHA разных репозиториев будут давать ложное обновление. Формат кэша этого различия не фиксирует. | В `update-cache` разделить upstream/export; ключ наблюдения — repo+branch, сравнение с предыдущим SHA того же ключа. Хранить ETag, срок следующей проверки и backoff по источнику. Установку разрешать только по новому подписанному набору каталога. |
| Д4 · данные | major | С Р5, Р11; #1 §3 `worker/measure.py`; #2 «Протокол измерений», строки 249–250; PRD §7.1 | #1 обещает сброс замера после смены ревизии/потоков, сохраняя процесс; #2 требует новый процесс для такого замера. Сброс JSON не сбрасывает `VmHWM` процесса: малая модель может получить пик прежней большой. Ответ `measure` также не содержит идентичности измеренной конфигурации. [Linux proc](https://man7.org/linux/man-pages/man5/proc_pid_status.5.html) | Сохранить правило #2 о свежем процессе для достоверного замера; даже после последовательной выгрузки не переиспользовать старый HWM. Передавать и хранить полный ключ измерения из #2; поздние метрики другой конфигурации не перекрывают карточку. |
| Н1 · инварианты | **blocker** | С Р5, §6; #1 §3 `models/installer.py`; PRD F7.5, S14 | Ветка «выгрузка → загрузка» лишает хоткей старой модели, хотя PRD и унаследованный installer обещают её работу до готовности новой. Название `switching` этого не исправляет. Переключение сразу после отмены также может превратить готовность ≤1 с в ожидание загрузки. | При нехватке памяти сохранить старую модель и отказать/отложить переключение — это уже предусмотрено #2. Граница переключения должна учитывать текущую обработку и дополнительную запись; отмена не запускает скрытую выгрузку. |
| Н2 · инварианты | major | С Р5; #2 риск «Две модели вызывают OOM»; PRD §7.2, §8 | `1,5× веса + 200 МБ` — не верхняя оценка пика кандидата: сам PRD допускает 1,5–3×, а для fp32 правило неприменимо. Проверка текущего MemAvailable не резервирует память. Следовательно, заявленный «пик ограничен MemAvailable» из этой формулы не следует. | Использовать пик загрузки/смоука конкретной конфигурации с запасом, для неизвестного — консервативную оценку. Назвать проверку предварительной оценкой, а не гарантией; при сбое кандидата сохранять старую модель. |
| Н3 · инварианты | major | С §4 последний абзац, §6; PRD §7.2 и §8 | Порог «RSS воркера ≤450 МБ» в синтезе потерял привязку к GigaAM. Общее «≤600 МБ в установившемся режиме» также не объясняет Whisper turbo (~2 ГБ) и Multilingual Large (~1,1 ГБ), уже входящие в каталог. | Явно привязать 450/600 МБ к приёмочному сценарию с дефолтной GigaAM; отдельно обозначить измерения остальных моделей и временный пик переключения. |
| Н4 · инварианты | **blocker** | С Р2–Р3, §4 упаковка; #1 §4, §7.2 | ELF-часть Р2 подтверждается, но пакетный контракт ORT 1.29.0 — нет: локальный [METADATA](/home/astra/.cache/uv/archive-v0/a83T7auSal9YHma-/onnxruntime-1.29.0.dist-info/METADATA) требует `protobuf>=4.25.8`; план рассчитывает на 3.21.12, и текущий apt-кандидат действительно 3.21.12. Успешный `import` не доказывает согласованность зависимостей. | Применить уже предусмотренный откат на 1.24.4 с проверкой его зависимостей либо подтвердить допустимый источник нужного protobuf. Добавить гейт METADATA↔Depends, сохранив три ELF и отсутствие шима. |
| Н5 · инварианты | **blocker** | С §2 root-helper, Р6, §4 безопасность; #1 §3 helper; #2 «Сеть apt» | Offline запрещает сеть, но унаследованная фиксированная команда apt может скачать зависимости. Гейт Requests на системный apt не распространяется. Правило #2 про `--no-download` в контракт helper не перенесено. | Зафиксировать отдельную сетевую стадию apt и её политику. При offline helper применяет фиксированный `--no-download`; нехватка зависимостей заканчивается передачей администратору. Определить передачу пользовательского запрета сети без возможности ослабить root-policy. |
| О1 · отказы | major | С §4 IPC/инварианты; #1 §2 рестарт, §3 `paste.py`; #2 инвариант «Отмена/крах/смена поколения» | Рестарт описан, но не определена судьба сессий и уже заведённых таймеров вставки. При краше во время записи PCM теряется; при краше после `result`, но до XTest, базовый PasteService ещё может вставить текст старого поколения. | На EOF/краш завершать затронутые сессии ошибкой, отменять их незавершённые вставки и не переигрывать запись. Перед XTest проверять поколение/ID; после уже отправленного XTest не повторять вставку. Восстанавливать буфер только пока им владеет эта операция. |
| О2 · отказы | major | С Р5, Р12; #1 §5.2; #2 «Атомарность», «Откат» | #1 задаёт `smoke → rename → current.json → model.load`, #2 — `rename → smoke → current.json` и восстановление транзакции. Сам порядок rename допустим в обоих вариантах, но синтез не определяет восстановление; в базовом потоке указатель уже изменён до окончательного `model.load`, который тоже может упасть. | Закрепить один порядок: проверенный набор → загрузка и смоук именно кандидата → подтверждённая готовность → атомарный commit указателя. Сохранять прежнюю ревизию и фазу операции; после обрыва восстанавливать последнюю подтверждённую. Обрыв сети относится к `.part`; локальный смоук сеть не использует. |
| О3 · отказы | major | С §4 helper «#1 целиком»; #1 §3 helper «не хранит»; #2 риск «Обрыв apt», шаг «Результат» | Stateless-helper с результатом только в stdout теряет исход при обрыве GUI/helper/питания. #2, напротив, требует журнал и проверку системного результата. Нельзя вывести «не установлено» только из исчезновения процесса: dpkg мог уже изменить пакет. | Сохранять журнал операции и установленную версию/состояние dpkg; при следующем запуске сверять фактический результат. Не убивать apt при закрытии GUI, не обещать rollback и не делать автоматический downgrade; незавершённую конфигурацию передавать администратору. |
| О4 · отказы | major | С §4 безопасность; #1 §6.10, §3 трей, §5.5; #2 инвариант видимости; PRD F3.6–7 | #1 требует иконку «слушаю» **всегда**, #2 останавливает запись лишь при потере **всех** индикаторов. При исчезновении SNI и работающей пилюле правила дают разное поведение. При `--hidden` окно вообще не создаётся, хотя fallback должен открыть его с баннером. | Применить более строгое принятое требование: при отсутствии трея остановить/не начинать запись, восстанавливать регистрацию. По истечении ретраев создавать и показывать окно даже после `--hidden`; явно определить возобновление доступности записи. |
| О5 · отказы | major | С §4 компоненты #1; #1 §2 `QLocalServer`, §3 `app.py`; #2 `Autostart / single-instance`; PRD S11-A4 | Назван QLocalServer, но отсутствует контракт двух одновременных стартов: получение владения до воркера/хоткея/записи settings, поведение проигравшего и обработка устаревшего сокета. Проверка «сервер отвечает?» сама по себе оставляет гонку. | Зафиксировать атомарное получение владения до побочных действий. Второй экземпляр отправляет `show` владельцу и завершается; очистка старого сокета допускается только после проверки отсутствия владельца, без удаления действующего endpoint. |

**Проверено, чисто:**

- **Р2: три ELF, без шима.** Read-only проверка всех ELF локальных ORT 1.29.0 и 1.24.4 подтвердила по три объекта, максимум GLIBC соответственно 2.28/2.27 и отсутствие `__isoc23_*`. Это подтверждение ABI-инвентаря; конфликт protobuf указан отдельно.
- **Выбор семи layout из #1 совместим с закрытым enum.** Различие `onnx-asr-vosk`/`vosk-onnx` разрешается уже выбранным приоритетом #1; проблема — синхронизация схемы, адаптера и примера PRD.
- **Цепочка проверки каталога согласована:** подпись → схема → ограничения путей → размеры/sha256. Правила #2 о hardlink и выходе external-data за каталог не противоречат базовому загрузчику.
- **Не-LFS sha256 согласован:** вычисление при публикации; git-SHA-1 не используется вместо sha256. Зеркала проверяются по одному набору ожидаемых хешей.
- **settings.json / policy.conf совместимы на уровне хранения:** версионированный пользовательский JSON, атомарная запись и `.bak`; отдельная read-only policy, эффективные значения и блокировки. Проблемы применения policy в helper вынесены в И8/Н5.
- **Отсутствие polkit-агента предусмотрено** в #1 §5.4/R9: диагностическое сообщение и административный путь. Нового механизма авторизации для синтеза не требуется.
- **GPG/gpgv, закреплённый keyring, повторная проверка в root-staging, `auth_admin` без `_keep` и запрет произвольных аргументов apt** взаимно совместимы.
- **Ручная проверка обновлений при выключенных автопроверках согласована** с журналом решений; в текущем PRD S9-A5 уже исправлен.
EXIT=0
