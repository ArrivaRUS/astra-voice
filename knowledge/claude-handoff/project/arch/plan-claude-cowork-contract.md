# Контракт Astra Voice ↔ Astra Cowork — «Команда помощнику» (US-2.10, F17, S18) — план архитектора #1 (Claude)

> 2026-09-24. Вход: бриф Юрки, PRD Voice 0.9 (F17, S18, §12.1 В1–В3, A13), `stories.md` (US-2.10, 1.5b, 2.11),
> `backlog.md` (Д16, Д22, Д23), `docs/threat-model.md` (§1–§2), `arch/plan-synth.md`; код Voice (`platform/x11.py`,
> `platform/hotkey.py`, `platform/paste.py`, `core/dictation.py`, `runtime.py`, `core/policy.py`, `core/settings.py`,
> `core/stats.py`, `ui/notify.py`, `ui/tray.py`, `qml/onboarding/Step3Hotkey.qml`, `docs/ui-bridge.md`); код Cowork
> (`CLAUDE.md`, `docs/hermes_adapter.md`, `docs/architecture.md`, `daemon/service.py`, `daemon/form_routing.py`,
> `daemon/tray.py`, `core/notify.py`, `ui/prompt_widget/{controller,form_request}.py`, `core/orchestrator/task_pool.py`,
> `os_skills/router.py`, `core/state/{models,repo}.py`, `core/config.py`, `packaging/{systemd,desktop,autostart}`,
> `pyproject.toml`). Факты машины — только чтение файлов (без D-Bus, X, служб).
> Статус: **проект, к суду Юрки (второй план не видел)**. Код не менялся.

## 1. Резюме контракта (10 строк)

1. Транспорт — **сеансовая шина D-Bus**, QtDBus с обеих сторон (PyQt5.QtDBus у Voice уже используется в `ui/notify.py`
   и `ui/tray.py`; PyQt6.QtDBus у Cowork — в `core/notify.py`, `daemon/tray.py`). Новых зависимостей ноль, сети ноль.
2. Cowork-демон владеет именем **`ru.astralinux.Cowork`** (имя из `docs/architecture.md` Cowork), объект
   `/ru/astralinux/Cowork`, интерфейс **`ru.astralinux.Cowork.Command1`** с двумя методами: `Status() → a{sv}` и
   `Submit(s text, a{sv} options) → a{sv}`. Цифра в имени интерфейса = мажорная версия контракта.
3. `Submit` несёт одну строку (≤ 4000 символов, без переводов строки) и метаданные: `source="astra-voice"`,
   `contract=1`, `request_id` (uuid4), `deadline_mono_ms` (CLOCK_MONOTONIC, общий для процессов одной машины),
   `mode="submit"` (по умолчанию) | `"draft"`.
4. Ответ — закрытый набор: `accepted | busy | refused | expired` + `reason` из закрытого списка. Voice ждёт **≤ 300 мс**,
   шлёт **ровно один раз**, без автоповтора; Cowork отвечает **до** тяжёлой работы (валидация → ответ → постановка задачи
   следующим оборотом цикла) и **не исполняет** команду, если взялся за неё позже `deadline` (`expired`).
5. Что делает Cowork: в режиме `submit` — ставит задачу в свой оркестратор так же, как поручение из виджета
   (`source="astra_voice"`, fast-path умений ОС работает, ответ — штатным уведомлением/в истории), **окно не открывает**;
   `draft` — открывает единую форму ввода с предзаполненным текстом (резерв, в v0.2 Voice его не шлёт).
6. Признак «установлен» — статический (артефакты: `astra-cowork` в PATH, `astra-cowork.desktop`, юнит
   `astra-cowork-daemon.service`, файл активации `ru.astralinux.Cowork.service`); «запущен» — `NameHasOwner` на шине +
   подписка `NameOwnerChanged` (без опросов); «готов» — сам ответ `Submit`/`Status` в бюджете 300 мс.
7. Безопасность: только локальная шина (unix-сокет, auth EXTERNAL — только uid сеанса); любой процесс того же uid и так может
   отправить запрос помощнику (токен `auth.toml` 0600 того же uid, XTest в виджет) — метод новых прав не даёт; гигиена:
   лимит длины, частота ≤ 1/500 мс, дедуп `request_id`, `NO_AUTO_START`, текст не пишется в журналы/статистику ни там, ни там.
8. Отказы: нет владельца имени / нет ответа за 300 мс / `busy` / `refused` / `expired` → у Voice текст в буфере обмена
   («только буфер» F5.2), пилюля «Помощник недоступен — текст в буфере», уведомление «Открыть Astra Cowork» (запуск юнита или
   ярлыка) и «Что случилось»; Cowork при своих сбоях после `accepted` говорит сам (уведомление), текст в журнал не пишет.
9. Версионирование: поле `contract` + суффикс интерфейса; добавление ключей в `a{sv}` версию не меняет; несовместимое → `Command2`
   рядом со старым; несовпадение → `refused/unsupported_contract` или `UnknownInterface` → у Voice «недоступен» + код в лог.
10. Публикация: один канонический файл `docs/contracts/astra-voice-cowork-command.md` + интроспекция
    `ru.astralinux.Cowork.Command1.xml` в репо **Cowork (владелец — сервер)**, зеркальная копия в Voice с sha256 XML;
    в обоих репо — контрактный тест против XML.

## 2. Выбор транспорта

| Кандидат | За | Против | Вердикт |
|---|---|---|---|
| **D-Bus session bus** (QtDBus) | уже в обоих кодах (Voice: уведомления, SNI-трей; Cowork: уведомления, трей-наблюдатель) — 0 новых зависимостей в deb Voice; обнаружение «запущен» бесплатно (`NameHasOwner`, сигнал `NameOwnerChanged` — без опросов, §8 «CPU в покое»); uid-изоляция шиной; асинхронный вызов с таймаутом (`callWithCallback(…, 300)` — паттерн `ui/tray.py`); нативно для KDE, единственный IPC, который Cowork уже планировал (`architecture.md`: «DBus service — ru.astralinux.Cowork»); тестируется на приватной шине (`dbus-run-session`, есть на машине и в CI обоих репо — `dbus-x11`) | нужен живой главный цикл демона (вызов ждёт оборота Qt-loop) — закрывается `deadline` + «ответ до работы»; во Fly шину надо подтвердить вживую (но трей Voice в S17 уже её требует) | **выбран** |
| Unix-сокет демона (`$XDG_RUNTIME_DIR/astra-cowork/…`) | без шины; простой | у Cowork сокет-сервера нет — писать сервер, кадрирование, auth по SO_PEERCRED, обнаружение; новая поверхность для T2 (права каталога, гонки); ни одной готовой части | запасной, если D-Bus во Fly окажется недоступен |
| CLI `astra-cowork --command "…"` | «очевидно» | старт интерпретатора из `.venv` с импортом PyQt6 — 0,5–1,5 с, бюджет 300 мс не выполним; текст в `argv` виден в `/proc/*/cmdline` всем пользователям (актив A1 Voice); внутри всё равно IPC к демону | отклонён |
| REST плагина Hermes `127.0.0.1:8765` | есть, задокументирован | мимо демона Cowork: задача не попадёт в его чат/историю (`state.db`), не сработает fast-path умений ОС («Открой папку загрузки» уйдёт в облако вместо локального умения), не будет виджета/трея; Voice пришлось бы читать чужой секрет `auth.toml` (расширение круга владельцев токена); `POST /v1/messages` держит соединение до ответа (до 1800 с); «здоров плагин» ≠ «готов Cowork»; TCP на loopback спорит с гарантией A7 «нет исходящих соединений» | отклонён |
| XTest/ClientMessage в виджет | — | костыль, гонки активации (инцидент 03.09 «форма закрылась под диктовкой») | отклонён |

## 3. Спецификация вызова

### 3.1 Имена
- Имя на шине: `ru.astralinux.Cowork` (регистрирует демон после `CoworkDaemon.__init__`, снимает в `_quit`).
- Объект: `/ru/astralinux/Cowork`. Интерфейс: `ru.astralinux.Cowork.Command1`.
- Интроспекция (`docs/contracts/ru.astralinux.Cowork.Command1.xml`):

```xml
<node>
  <interface name="ru.astralinux.Cowork.Command1">
    <method name="Status">
      <arg type="a{sv}" name="info" direction="out"/>
    </method>
    <method name="Submit">
      <arg type="s"     name="text"    direction="in"/>
      <arg type="a{sv}" name="options" direction="in"/>
      <arg type="a{sv}" name="reply"   direction="out"/>
    </method>
  </interface>
</node>
```

Словари `a{sv}` (vardict) выбраны намеренно: добавление ключей не меняет сигнатуру и версию. PyQt маршалит Python-`int`
как `i` или `x` по величине — сервер принимает любой целочисленный вариант (`i/u/x/t`), контрактный тест это проверяет.

### 3.2 `Submit(text, options) → reply`
`text` — распознанная и санитизированная строка Voice (F4.5, `paste.normalize`: C0/C1 убраны, CR/LF → пробел), одна строка.

| `options` | тип | обяз. | значение |
|---|---|---|---|
| `source` | s | да | `"astra-voice"` (идентификатор приложения; Cowork хранит `Task.source = "astra_voice"`, `Task.hotkey = "astra_voice"`) |
| `contract` | целое | да | `1` |
| `request_id` | s | да | `uuid4().hex` (32 hex); ключ идемпотентности |
| `deadline_mono_ms` | целое | да | `time.monotonic()*1000 + 300` на стороне Voice; CLOCK_MONOTONIC системный, сравним между процессами |
| `mode` | s | нет | `"submit"` (умолчание) — сразу задача; `"draft"` — открыть форму ввода с текстом (резерв) |
| `client_version` | s | нет | версия Voice, только для журнала |

| `reply` | тип | значение |
|---|---|---|
| `status` | s | `accepted` · `busy` · `refused` · `expired` |
| `reason` | s | пусто при `accepted`; `busy`: `shutting_down`, `rate_limited`, `locked`; `refused`: `empty`, `too_long`, `unsupported_contract`, `bad_source`, `bad_mode`, `not_ready`; `expired`: `deadline` |
| `request_id` | s | эхо |
| `contract` | целое | максимальная версия, которую понимает сервер |

Ошибки уровня D-Bus, которые Voice трактует как «недоступен» с кодом в лог: `ServiceUnknown` (не запущен),
`NoReply` (таймаут 300 мс), `UnknownInterface`/`UnknownMethod` (старый Cowork без контракта), любая другая.

Семантика на стороне Cowork (`daemon/command_service.py`, новый модуль):
1. Валидация ≤ 5 мс: `source`, `contract ≤ 1`, `request_id` формата, `mode ∈ {submit, draft}`, текст: пробельные серии → один
   пробел, `strip`, длина 1…4000, иначе `refused`.
2. `now > deadline_mono_ms` → `expired` (ничего не делать). Дедуп: `request_id` из LRU (32 записи, 60 с) → `accepted` без
   второй задачи. Частота: принятый `Submit` < 500 мс назад → `busy/rate_limited`. Демон в `begin_shutdown` → `busy/shutting_down`.
   Сеанс заблокирован (`widget.is_available_for_hotkey()` ложь) → `busy/locked`.
3. **Ответ уходит здесь** (возврат из слота); работа — `QTimer.singleShot(0, …)`: режим `submit` →
   `orchestrator.submit(text, context={"source": "astra_voice", "hotkey": "astra_voice", "request_id": …})`; режим `draft` →
   `form_router.open_form(FormRequest(source=FormSource.ASTRA_VOICE, prefill=text))`. Исключение после `accepted` →
   `tray.notify("Astra Cowork", "Не удалось принять голосовую команду")`, журнал без текста.
4. Что видит пользователь Cowork в `submit`: задача в истории (новый диалог, как у виджета; локальная команда — в ветке
   «Операционные команды» по вердикту роутера), счётчик в трее, ответ — существующий путь `_on_task_completed` →
   уведомление «Astra Cowork — ответ» (плашки виджета нет: его автомат принимает `TASK_ACCEPTED` только после `SUBMIT` формы —
   менять автомат ради v0.2 не нужно). Окно не открывается (`_AUTOOPEN_SOURCES` не расширяем).
5. Fast-path умений ОС: `FASTPATH_SOURCES` (`os_skills/router.py`) += `"astra_voice"` — иначе «Открой папку загрузки» уйдёт
   в облако. Гейт роутера ≤ 80 символов остаётся.

### 3.3 `Status() → info`
`state` (s): `ready` · `busy` (shutting_down/rate window) · `locked`; `contract` (целое, = 1); `version` (s, версия Cowork);
`modes` (as, `["submit","draft"]`). Бюджет ответа ≤ 50 мс; ничего не пишет. Используется кнопкой «Проверить снова» и
строкой состояния в «Общих» (см. §4); в цикле доставки не обязателен.

### 3.4 Тайминги и повтор
- Voice: `QDBusMessage.setAutoStartService(False)` (никакой D-Bus-активации демона из доставки), `callWithCallback(msg, ok, err, 300)`.
- Один вызов на команду, повтора нет (at-most-once). `expired` на сервере закрывает «Voice сдался, Cowork исполнил позже».
  Остаточный случай — ответ `accepted` опоздал на десятки мс: пользователь увидит «недоступен — текст в буфере», а Cowork
  выполнит команду; редкость принимается, `request_id` в журналах обеих сторон позволяет сопоставить.
- Отмена у Voice в фазе доставки не действует (PRD F17-исключения: «если запрос уже ушёл, команда доставляется»).

## 4. Признак доступности

| Уровень | Когда | Как | Что показывает |
|---|---|---|---|
| L0 «установлен» (статика) | старт, открытие «Общих»/онбординга, кнопка «Проверить снова» | хотя бы один артефакт: `astra-cowork` в `PATH`; `astra-cowork.desktop` в `$XDG_DATA_HOME/applications`, `$XDG_DATA_DIRS/*/applications`, `$XDG_CONFIG_HOME/autostart`; юнит `astra-cowork-daemon.service` в `$XDG_CONFIG_HOME/systemd/user`, `/etc/systemd/user`, `/usr/lib/systemd/user`, `/usr/local/lib/systemd/user`; файл активации `ru.astralinux.Cowork.service` в `…/dbus-1/services` (когда Cowork его добавит). Факт машины 24.09: символические ссылки `~/.local/bin/astra-cowork{,-daemon}` → `.venv`, юнит в `~/.config/systemd/user`, ярлык в `~/.config/autostart` | «не установлен» → второй режим не регистрируется (F17.3). «Установлен, но нет `~/.config/astra-cowork/`» → «найден · не запущен» (F17-исключения) |
| L1 «запущен» (дёшево, без опросов) | старт; нажатие командной клавиши; подписка на изменения | `NameHasOwner("ru.astralinux.Cowork")` через `org.freedesktop.DBus` (отвечает демон шины, ~1 мс, не зависит от цикла Cowork) + сигнал `NameOwnerChanged` (паттерн `ui/tray.py`) | строка «найден · доступен / найден · не запущен», подсказка пилюли; после `Restart=always` демона строка обновится сама |
| L2 «готов принять» (живой, ≤ 300 мс) | при доставке каждой команды | сам `Submit`; `Status` — по кнопке «Проверить снова» | «Передано помощнику» / «Помощник недоступен — текст в буфере» (A3: ≤ 300 мс после распознавания) |

Почему не отдельный `Status` при каждом нажатии: за 5–40 с диктовки состояние успеет измениться, а лишний вызов ничего не
гарантирует; L1 на нажатии даёт подсказку, L2 — истину в момент доставки. Один round-trip на команду.

## 5. Безопасность (вход для T2 `security-gate`)

- **Только локально.** Шина — unix-сокет `$XDG_RUNTIME_DIR/bus`, `session.conf`: `<auth>EXTERNAL</auth>`, к сеансовой шине
  подключается только uid владельца (и root — вне модели). Voice не открывает ни одного TCP-соединения: A7 (`tcpdump`, 20 команд)
  выполняется по построению — `tools/validate privacy` расширить кейсом «команда».
- **Кто может вызвать.** Любой процесс сеанса пользователя (B2 модели угроз Voice). Это принимается: тот же процесс уже
  может POST'ить в Hermes с токеном `auth.toml` (0600 того же uid) или напечатать в виджет XTest'ом — метод новых привилегий
  не создаёт. PID-проверка (`GetConnectionUnixProcessID` → `/proc/pid/exe`) не даёт защиты (тот же `/usr/bin/python3`) и гоночна —
  не делаем. Гигиена на сервере: длина ≤ 4000, частота ≤ 1/500 мс, дедуп `request_id`, `deadline`, закрытые enum'ы.
- **Никакого shell/exec по строке.** Текст идёт только в `orchestrator.submit` — тем же путём, что напечатанный; умения ОС
  исполняют фиксированные команды с параметрами, а не строку. Cowork не логирует текст (запрет на `preview=text[:80]` из
  `_on_widget_submit` в голосовом пути); Voice — существующие сторожа `test_text_never_reaches_logs_indicators_or_stats`,
  `test_recognized_text_never_reaches_file` расширяются на путь доставки; статистика `dictation{mode=command}` без текста.
  Где текст всё же живёт: `state.db` Cowork (история чата — продуктовая функция) и далее по политике Cowork (облако/локальный
  мозг) — одна фраза в PRIVACY Voice (F17.8).
- **Токен.** D-Bus-транспорту токен Hermes не нужен; Voice его не читает и не хранит. Поведение «без токена» — не применимо.
- **Ослышанная команда.** У заказчика `approvals.mode: off` (decisions Voice 14.09): голосовая команда исполнится без
  подтверждения, как напечатанная. Контракт передаёт `source`, чтобы Cowork мог применить к голосу свою политику
  (например, подтверждать опасные умения) — решение продукта, вопрос В-1 (§9).
- **Захват системной клавиши Win** — отдельный пункт T2 (F17.10 (ж)), контракта не касается.
- **Активация.** `NO_AUTO_START` на всех вызовах Voice: ни одна команда не поднимает демон Cowork сама; запуск — только
  кнопкой «Открыть Astra Cowork».

## 6. Отказы и запасной путь

| Ситуация | Voice | Cowork |
|---|---|---|
| Не установлен | второго режима нет; строка «Astra Cowork не установлен» | — |
| Установлен, не запущен (`ServiceUnknown` ~1 мс) | текст → буфер (`publish_clipboard`, прежнее содержимое не восстанавливается — F5.2); пилюля «Помощник недоступен — текст в буфере» 3 с; уведомление с «Открыть Astra Cowork» (юнит найден → `systemctl --user start astra-cowork-daemon.service`, паттерн QProcess как у WirePlumber И4; иначе `gio launch <ярлык>`) и «Что случилось» (существующий `ACTION_SHOW_DETAILS`); `dictation{result=undelivered}` | — |
| Запущен, нет ответа за 300 мс | то же; код `timeout` в лог | взявшись за вызов позже `deadline` → `expired`, задачи нет, журнал `voice_command_expired request_id=… chars=N` |
| `busy` (rate/выключается/сеанс заблокирован) | то же; в подробностях «Помощник сейчас занят» простым языком | ничего не ставит |
| `refused` (длина, версия, режим) | то же; код `reason` в лог (PRD: «контракт изменился → недоступен + код в лог») | ничего не ставит |
| `accepted`, но задача упала/Cowork перезапустился | «Передано помощнику» уже показано; Voice исполнение не отслеживает (F17.5) | своё уведомление об ошибке (существующий `_on_task_failed`); при перезапуске — `fail_stale_active_tasks` помечает зависшие |
| Перезапуск демона посреди диктовки | L1 на нажатии сказал «доступен», доставка встречает `ServiceUnknown`/`NoReply` → буфер; после `Restart=always` (3 с) `NameOwnerChanged` вернёт «доступен» | регистрирует имя заново при старте |
| Crash Voice после `accepted` | — | задача живёт, ничего не делать |

Тексты интерфейса — без имён служб, путей и шины («Помощник недоступен», «Помощник сейчас занят», «Помощник не поддерживает
голосовые команды — обновите Astra Cowork»).

## 7. Версионирование и публикация

- Версия контракта = `1`: поле `contract` в `options`/`reply`/`Status` и суффикс `Command1`. Совместимые изменения (новые ключи
  `a{sv}`, новые `reason`) — без смены версии; клиент игнорирует незнакомые ключи, сервер — незнакомые опции. Несовместимые —
  `Command2` на том же объекте, `Command1` живёт ещё один релиз Cowork.
- Матрица: Voice новый / Cowork без интерфейса → `UnknownInterface` → «обновите Astra Cowork»; `contract` клиента > сервера →
  `refused/unsupported_contract`; Cowork новый / Voice старый → ничего (режима нет).
- Файлы: `docs/contracts/astra-voice-cowork-command.md` (описание, таблицы §3–§6, журнал изменений) + `ru.astralinux.Cowork.Command1.xml`
  — **владелец Cowork** (сервер), правки — PR в Cowork, затем зеркало в Voice (`docs/contracts/` + строка `xml sha256=…`).
  Тесты: Cowork — интроспекция зарегистрированного адаптора равна XML; Voice — сигнатуры клиента совпадают с XML; в обоих —
  проверка sha256 зеркала (дрейф ловится на CI, не на машине заказчика).

## 8. Компоненты по репозиториям, порядок, оценка

### Astra Voice (≈ 3,75 дн. агента; PRD/backlog: 3,5 + спайк S7 0,5 вне истории)

| # | Компонент | Ответственность | Зависит от | Тест | Оценка |
|---|---|---|---|---|---|
| V1 | `platform/cowork.py`: `CoworkDetector` (L0, чистые функции над env/XDG-путями) + `CoworkClient` (L1/L2: `NameHasOwner`, `NameOwnerChanged`, `Status`, `Submit` async 300 мс, `NO_AUTO_START`, коды исходов `DeliveryOutcome`) + `launch()` («Открыть Astra Cowork») | обнаружение и доставка; никакого текста в логах | контракт (§3) | unit: детектор на tmp-дереве XDG; клиент с подменой транспорта (точка подмены как в `notify.py`); xvfb/dbus: фейковый сервис `Command1` на приватной шине (`dbus-run-session`) — accepted/busy/refused/expired/timeout/`ServiceUnknown` | 0,75 |
| V2 | `platform/hotkey.py`: роли `text`/`command` — `HotkeyManager` держит список привязок (combo, keycode, mods, FSM, key_down) на одном бэкенде, `handle_event` диспетчеризует по keycode, Escape общий; `parse_combo`/`is_valid_combo` — одиночная `super`/`super_r` (keysym `Super_L/R`, mask 0) только для роли `command`; во время записи команды — `x11.keyboard_grab` (даёт F17.10 (в): `Win+D` не доходит до KWin; подтверждает S7) | две клавиши без регрессии текстовой | S7 (только для дефолта Win; сама реализация ролей от S7 не зависит) | `test_hotkey_fsm.py` (две роли, одновременное нажатие → приоритет первой), xvfb `test_x11_hotkey.py` (захват `Super_L` с mask 0, освобождение, `BadAccess` одной роли не мешает другой), `hotkey_grab{key_role=command}` | 0,75 |
| V3 | `core/command_mode.py` (чистая функция `resolve(settings, policy, installed) → CommandDecision{enabled, combo, locked, reason}`) + `settings.command_hotkey` (доп. поле, дефолт `"super"`, без смены `SCHEMA_VERSION`) + `policy.effective`: `command_hotkey=off` → режима нет; неразбираемо / совпадает с `hotkey` → fail-closed (`policy-invalid`, имя ключа в лог); годное → locked | политика и настройки | — | unit: A13 целиком (super / ctrl+shift+space / off / ctrl+space / meta+пробел), `is_locked("command_hotkey")` | 0,25 |
| V4 | `core/dictation.py`: режим на фразу (`mode` от роли в `on_hotkey_state`), фаза `DELIVERING` вместо `PASTING` для команды, порт `deliver(text, on_done)` (async, таймер 300 мс через `_later`), исходы → пилюля/трей, при неуспехе `publish_clipboard` (без восстановления), отмена в `DELIVERING` игнорируется; `stats`: `dictation` + `mode`, `deliver_ms`, `result ∈ {delivered, undelivered}`; уведомление `notify_command_undelivered` с двумя действиями; `runtime.py`: сборка (`CoworkClient`, `notify.set_action_handler(ACTION_OPEN_COWORK)`) | оркестрация командного режима | V1–V3 | `test_dictation.py`: A1/A3/A4/A6 на фейковом клиенте (accepted → `delivered`, буфер не тронут; timeout/busy → `undelivered`, буфер = текст, вставки нет); сторожа текста расширены на путь доставки; `test_stats` — новые поля/выборы | 0,75 |
| V5 | UI: `PillState` + `LISTENING_COMMAND`, `SENDING_COMMAND`, `COMMAND_DELIVERED` (800 мс), `COMMAND_UNAVAILABLE` (3 с, клик — подробности) с подписью и значком (дизайн-спек: дельта от UXAnalyst); `TrayState` подсказки «Слушаю команду», `nokey` называет режим; `ui/bridges.py`: свойства `commandHotkey`, `commandStatus` (`available/not-running/not-installed/policy-invalid/no-contract`), слоты `recheckCowork()`, `beginCommandCapture()`; `General.qml` строка «Команда помощнику» (состояние, «Проверить снова», «Выбрать другую», объяснение про Win, «Задано администратором»); `Step3Hotkey.qml` — вторая строка при найденном Cowork (A12); `docs/ui-bridge.md` §3/§4.4 | экраны и мост | V3, V4, дизайн-дельта | контрактный тест QML↔мост (`rglob`, как в `.patches/011`), xvfb `test_pill_states.py`/`test_onboarding.py` (A2, A5, A9, A12, A13), DesignReviewer по снимкам | 0,75 |
| V6 | Тесты и документы: `tools/validate cowork` (детект, шина, фейковый сервис, privacy-кейс «команда»), `docs/contracts/` зеркало + sha256, PRIVACY фраза, `docs/test-plan.md` строки S18, changelog | приёмка | V1–V5 | CI зелёный; E2E на машине — §8.3 | 0,5 |

### Astra Cowork (≈ 2,25 дн. агента)

| # | Компонент | Ответственность | Зависит от | Тест | Оценка |
|---|---|---|---|---|---|
| C1 | `daemon/command_service.py`: `CommandService(QObject)` + `Command1Adaptor(QDBusAbstractAdaptor)` (`Q_CLASSINFO("D-Bus Interface", "ru.astralinux.Cowork.Command1")`, слоты `Status`, `Submit` с `QVariantMap`); регистрация имени/объекта после `CoworkDaemon.__init__`, снятие в `_quit`; отказ регистрации (имя занято) — журнал, демон работает дальше | сервер контракта | контракт (§3) | unit без шины: валидация, дедуп, deadline, rate limit, `busy` при shutdown/locked; `@pytest.mark.dbus` на `dbus-run-session`: интроспекция = XML, `Submit` с `i/x/t` в `deadline_mono_ms`, ответ ≤ 50 мс | 0,5 |
| C2 | Интеграция: `submit` → `orchestrator.submit(source="astra_voice")`; `FASTPATH_SOURCES` += `astra_voice`; `FormSource.ASTRA_VOICE` (вне `HOTKEY_SOURCES`, без записи в `hints.CATALOG`); `draft` → `form_router.open_form(prefill)`; уведомление при сбое после `accepted`; журнал без текста; `docs/main_view.md`/`architecture.md` — источник `astra_voice` | поведение продукта | C1 | unit: задача создаётся с `source/hotkey=astra_voice`, окно не открывается (`test_autoopen_source.py`), fast-path роутер принимает источник, `draft` открывает форму с префиллом; тест «в журнале нет текста» | 0,75 |
| C3 | Готовность: `Status`; при `begin_shutdown` → `busy`; `scripts/doctor.py` — проверка «имя `ru.astralinux.Cowork` есть на шине, если демон активен» (только чтение) | признак готовности | C1 | unit + doctor | 0,25 |
| C4 | Документы и контракт: `docs/contracts/astra-voice-cowork-command.md`, XML, тест sha256, `CLAUDE.md` абзац «внешние команды по D-Bus», `docs/update-other-machine.md` (ничего ставить не надо — шина уже есть) | публикация | C1–C3 | CI | 0,25 |
| C5 | Ревью (`code-reviewer` + Codex-проход), T2 по дифу, выкладка снимком prod-worktree (checkout hash → рестарт демона, когда нет долгих задач → `log -1`) | выпуск | C1–C4 | `journalctl` без ошибок, `busctl --user` видит имя (с «ок» заказчика) | 0,5 (вкл. буфер на правки ревью) |

Необязательно (после v0.2): файл активации `~/.local/share/dbus-1/services/ru.astralinux.Cowork.service`
(`SystemdService=astra-cowork-daemon.service`) — тогда «Открыть Astra Cowork» = `startService` без ярлыков; плашка виджета
«принято голосом» (нужен новый переход автомата `EXTERNAL_SUBMIT`).

### 8.1 Порядок по зависимостям (milestones)
1. **K0 Контракт зафиксирован** — суд Юрки → `docs/contracts/*` + XML в Cowork, зеркало в Voice; T2 security-analyst по §5
   (проектирование новой IPC-поверхности к агенту — триггер T2). Кода нет. ⛔ условие В2 «до планирования v0.2».
2. **K1 Cowork C1 + C2 + C3** (сервер раньше клиента — Voice E2E нужен живой адресат). Параллельно Voice **V1** против
   фейкового сервиса из XML и **V3** (чистая логика).
3. **K2 Voice V2** (роли клавиш) — вместе со спайком S7 (Win/KWin/Fly); дефолт Win включается только после S7, роли —
   независимо.
4. **K3 Voice V4** (оркестратор) → **V5** (UI, после дизайн-дельты пилюли/строки настроек) → **V6**.
5. **K4 Cowork C4 + C5** (ревью, T2 по дифу, выкладка снимком) — до начала Voice E2E.
6. **K5 E2E на машине заказчика** (§8.3, с его «ок»), приёмка S18 A1–A13 в KDE и Fly, `tcpdump`, затем чеклист R2.

### 8.2 Оценка итого
Voice ≈ 3,75 дн. + S7 0,5 (вне истории) · Cowork ≈ 2,25 дн. · T2 ≈ 0,5 (security-analyst, две стороны) · Юрка: суд, E2E, приёмка.

### 8.3 Только вживую на машине заказчика (с его «ок»: `:0`, шина пользователя)
- Сеансовая шина во **Fly-сессии** (тот же `$XDG_RUNTIME_DIR/bus` systemd-user или отдельный `dbus-launch` fly-dm) — L1/L2 там же.
- Задержка `Submit` у живого демона **под нагрузкой** (индексация RAG, транскрибация, Alt×2 со снимком окна — единственные
  найденные синхронные вызовы главного потока: `capture/active_window.py`, `capture/clipboard.py`, `prompt_widget/monitor.py`
  через `subprocess.run`) — доля `expired` при 20 командах.
- Перезапуск демона посреди диктовки (`Restart=always`, 3 с) и повторная регистрация имени; `NameOwnerChanged` у Voice.
- `NO_AUTO_START`: демон не поднимается от `Submit`; «Открыть Astra Cowork» поднимает через юнит.
- `tcpdump -i any` за сессию с 20 командами: 0 SYN от pid Voice (шина — unix-сокет).
- Спайк S7: KWin `ModifierOnlyShortcuts`/старт-меню Fly при нашем `XGrabKey Super_L`, `Win+D` при удержании, возврат после `kill -9`.
- Выкладка Cowork только снимком prod-worktree (хэш `log -1`), рестарт демона без активных долгих задач.
- Уведомления с двумя действиями в KDE и Fly (действия у Fly-службы уведомлений — проверить).

## 9. Риски и альтернативы
- **R1 Главный цикл Cowork занят > 300 мс** → `expired`/буфер у Voice при живом демоне. Смягчение: ответ до работы, `deadline`,
  замер под нагрузкой (§8.3); если доля > 5 % — вынести `Submit` в отдельное D-Bus-соединение в рабочем потоке демона (паттерн
  `ui/tray.py` Voice, но на сервере) — тогда обработчик не зависит от Qt-loop.
- **R2 Шины нет во Fly** → запасной транспорт unix-сокет (§2) — интерфейс `CoworkClient`/`CommandService` спроектирован так, что
  транспорт меняется внутри модулей V1/C1.
- **R3 Ослышанная команда исполняется без подтверждения** (`approvals.mode: off`) — продуктовый риск, вопрос В-1; техническая
  подстраховка — `source` в задаче и P2-опция Voice «показывать команду перед отправкой» (F17.9).
- **R4 Двойной эффект при опоздавшем `accepted`** — редкость, принимается; `request_id` в журналах.
- **R5 Регрессия текстовой клавиши при рефакторинге `HotkeyManager`** — одна роль остаётся тем же кодом, тесты FSM/xvfb уже есть.
- **R6 Маршаллинг PyQt (`int` → `i`/`x`)** — сервер терпим к целочисленным типам, тест в C1.
- **R7 Имя `ru.astralinux.Cowork`** взято из `architecture.md` Cowork (май); если его решат сменить — одна константа с обеих
  сторон, но менять после выпуска дорого (парк): зафиксировать на K0.
- **R8 Виджет не показывает «принято голосом»** — ответ приходит уведомлением; если заказчик захочет плашку — отдельная
  задача Cowork (автомат виджета), контракт не меняется.
- Альтернатива UX «открывать форму с текстом, Enter отправляет» (`draft`) отвергнута как умолчание: противоречит мотиву
  заказчика («ушла сразу, без окна») и F17.5; оставлена в контракте как режим, чтобы включить без смены версии.

## 10. Вопросы заказчику (≤ 3)
- **В-1.** Голосовая команда исполняется как напечатанная, а подтверждения Hermes у вас выключены: принять это для v0.2
  (быстро, как просили) или Cowork должен для голосовых команд спрашивать подтверждение на опасные действия (медленнее,
  безопаснее при ослышке)?
- **В-2.** Каждая голосовая команда — новый диалог в истории Cowork (как у виджета), или подряд идущие команды в течение,
  скажем, 10 минут продолжают один диалог «голосом» (удобнее для «а теперь покажи последний файл»)?
- **В-3.** Ответ помощника на голосовую команду — уведомлением на рабочем столе (как сейчас для быстрых команд из виджета)
  достаточно, или нужен отдельный знак принятия в самом Cowork (например, анимация марки виджета)?
