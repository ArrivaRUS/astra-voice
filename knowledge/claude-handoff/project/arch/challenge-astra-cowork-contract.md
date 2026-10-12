# Челлендж синтеза: контракт Voice ↔ Cowork (US-2.10)

> Шаг 4b dual-architect, 2026-09-24. Движок: codex-cli 0.156.1, `gpt-6-astra`, effort high, service_tier default, sandbox read-only, cwd astra-voice. Сессия 01a0d1aa-e592-76f2-8a30-4bcc2ff82c45. Бриф: `arch/.brief-challenge-cowork.txt`. Ниже — сырой вывод Codex без правок.

---

Синтез требует уточнений перед реализацией. Главные проблемы: неполная C0, неопределённая граница `accepted`, обход блокировки сеанса и недостаточная спецификация QtDBus.

Файлы не менял. **V** — `src/astra_voice`, **C** — `src/astra_cowork` второго репозитория, **S** — [plan-synth-cowork-contract.md](/home/astra/Документы/astra-voice/arch/plan-synth-cowork-contract.md). Проверки импортов и метаобъектов выполнял без подключения к рабочей шине.

| Серьёзность · ось | Где | Суть | Минимальная правка синтеза |
|---|---|---|---|
| **blocker · 3** | S:27, C0; C/[service.py:577](</home/astra/Документы/Astra Cowork/astra-cowork/src/astra_cowork/daemon/service.py:577>), `:1255`, `:1271`; `hermes/client.py:153`; `ui/fullscreen/main_window.py:642` | **Двух `preview` недостаточно.** Третий находится в `mainview_submit`; также журналируются локальный и сгенерированный заголовки диалогов. Последний может воспроизводить запрос. В `core/logging.py:18` нет фильтра приватных полей. | Расширить C0 на три `preview` и заголовки; оставлять идентификаторы, длины и закрытые коды. Проверять INFO **и DEBUG**, включая завершение задачи и автозаголовок. |
| **blocker · 3** | C/[hermes/client.py:238](</home/astra/Документы/Astra Cowork/astra-cowork/src/astra_cowork/hermes/client.py:238>), `:248`, `:255`, `:271`, `:313`, `:316`; `task_pool.py:850`, `:1068`, `:1211`, `:1214`; `service.py:591` | Утечки через ошибки: тела HTTP при отмене, произвольный `reason`; тела SSE/decision включаются в исключения и затем журналируются. `MessageReply.model_validate()` тоже способен включить входные данные в `ValidationError`; это подтвердил контрольной строкой. SQLAlchemy-исключение при сохранении может содержать параметры INSERT, включая текст. | C0 должен охватывать **все HTTP-методы, ошибки схемы и БД**, а не только HTTP status основной отправки. В журналах — тип исключения, разрешённый код/статус; без `str/repr(exc)`, тела ответа и traceback с исходным исключением на этих границах. |
| **major · 1, 3** | S:19–24, :27, :32; C/[__init__.py:8](</home/astra/Документы/Astra Cowork/astra-cowork/src/astra_cowork/__init__.py:8>); база #1:209 | Реально **Voice — PyQt5 5.15.9/Qt 5.15.8; Cowork — qtpy/PyQt6 6.7.1**. В установленном PyQt6 отсутствуют `QDBusContext` и `QDBusVirtualObject`. Проверка UID «из контекста» не описана; `GetConnectionUnixUser` — дополнительный обмен, его стоимость не нулевая. | Зафиксировать `QDBusAbstractAdaptor`, `pyqtClassInfo`, `ExportAdaptors`; `Submit` получает дополнительный служебный `QDBusMessage`, отправитель берётся из `message.service()`. UID проверять у шины, ошибку проверки закрывать отказом. Проверка входит в общий deadline; синхронный `serviceUid()` без ограниченного ожидания не удовлетворяет ≤5 мс. См. эксперимент 1 и [правила Qt](https://doc.qt.io/qt-6/qdbusdeclaringslots.html). |
| **major · 1** | S:21–23, :27; база #1:76–105 | Не определены ответы на неверный UUID, отсутствующий/нецелый deadline и управляющие символы: закрытый список причин их не покрывает. Формула базы `monotonic()*1000+300` даёт **float**, а не целое. Проверено: Python `int` превращается также в `uint`, не только `i/x`; `bool` — отдельный тип. | Закрепить слоты `Slot(..., "QVariantMap", ..., result="QVariantMap")`; словарь передавать как map, не оборачивать целиком в `QDBusVariant`. Deadline: `monotonic_ns() // 1_000_000 + 300`; принимать `i/u/x/t` в диапазоне, исключать `bool/float`. Добавить `invalid_options`/`invalid_text` либо явно назначить `InvalidArgs`. Voice нормализует, Cowork независимо **проверяет**, не исправляет. |
| **major · 1, 4** | S:23–24; V/[ui/tray.py:48](/home/astra/Документы/astra-voice/src/astra_voice/ui/tray.py:48), `:108`, `:195`; V/`ui/notify.py:225` | Образцы Voice нельзя переносить буквально: `_BusRequest.failed()` превращает **любую** ошибку в `None`, теряя различие `NoReply`/`ServiceUnknown`; таймаут там 500 мс. Уведомления используют блокирующий вызов. Таймаут метода также не ограничивает создание соединения и интроспекцию. | В V1 сохранить `QDBusError.name()/type()` и признак отправки; использовать raw `QDBusMessage`, `setAutoStartService(False)`, асинхронный вызов с **остатком** 300 мс. Соединение/подписки — вне GUI, как в tray. Не использовать `QDBusInterface` с неограниченной интроспекцией. [Qt API](https://doc.qt.io/qt-6/qdbusconnection.html). |
| **major · 4** | S:23, :28, :33 | «Исход неизвестен» ограничен `NoReply`, но **обрыв соединения после отправки** также не доказывает отказ. Повреждённый ответ и смена владельца во время незавершённого вызова аналогично не дают права утверждать «не принято». После перезапуска приёмник теряет дедуп. | Разделить доказанное «не отправлено/отказано» и «отправлено, достоверного ответа нет». Второе всегда unknown, без повторов, включая смену владельца. Перезапуск **до отправки** может разрешать доставку новому владельцу — это явно записать; новый `instance_id` для этого не требуется. |
| **major · 3, 4** | S:24–25; база #1:104–110; C/[service.py:1496](</home/astra/Документы/Astra Cowork/astra-cowork/src/astra_cowork/daemon/service.py:1496>) | `singleShot(0)` откладывает работу, но не делает её сохранённой и не подтверждает получение ответа клиентом. Между `accepted` и callback возможны остановка, блокировка или сбой БД. Неясно, должен ли callback после deadline исполнять уже принятую команду: вернуть второй `expired` невозможно. | Определить точку принятия: в методе атомарно зарезервированы место и дедуп; `accepted` означает **приём в память**. Deadline ограничивает допуск нового запроса. Callback имеет родителя/защиту shutdown и обработку ошибок с уведомлением Cowork. Не обещать долговечность до записи задачи; не запускать вложенный event loop перед возвратом ответа. |
| **major · 3** | S:25, :37; C/[task_pool.py:2324](</home/astra/Документы/Astra Cowork/astra-cowork/src/astra_cowork/core/orchestrator/task_pool.py:2324>), `:2352`, `:2388`, `:2418`, `:2586` | Реальный класс — `TaskOrchestrator`, не `TaskPool`. В `submit()` нет выделенного preflight: отмена чужой задачи происходит раньше создания новой; отмена меняет БД и вызывает Hermes. Затем возможен отказ сохранения. Перенос одной проверки длины эту границу не исправляет. | Назвать фактический API и перечислить порядок: проверка входа/допуска и подготовка → побочные эффекты. Для невалидной команды гарантировать отсутствие отмены, создания диалога и записи задачи. Отдельно определить исход ошибки хранения **после** начала эффектов; не называть его доказанным отказом без эффекта. |
| **major · 3** | S:27, :33; база #1:104–105 | Дедуп не имеет определённого порядка и области гарантии. В базе deadline проверяется **раньше** дедупа: повтор принятого запроса после 300 мс получает `expired`, хотя задача уже исполняется. При 2 новых командах/с LRU на 32 записи вытесняет первую примерно через 16 с, а не гарантирует 60 с. Один ID с другим текстом также не определён. | Сначала проверка отправителя/формата и поиск принятого ID, затем deadline/rate для **нового** ID. Резервировать ID до callback; повтор не расходует rate limit. Хранить отпечаток полезной нагрузки и определить конфликт ID. Записать: LRU — вспомогательная защита до вытеснения/TTL/перезапуска; at-most-once клиента обеспечивается отсутствием повторной отправки. |
| **major · 3, 4** | S:23, :33; C/[task_pool.py:1883](</home/astra/Документы/Astra Cowork/astra-cowork/src/astra_cowork/core/orchestrator/task_pool.py:1883>), `:2518` | Лимит 1/500 мс **не заменяет ограничение незавершённых задач**. Пул имеет 10 исполнителей, ожидающие задания и списки `_dialog_pending` не ограничены этим rate limit. При задачах длительностью 30 с входной поток 2/с создаёт очередь. `queue_full` сейчас не имеет определённого условия. | Ввести предел именно голосовых незавершённых допусков поверх существующего пула, резервировать место до `accepted`, освобождать на терминальном исходе. Это не требует возвращать отдельную очередь из плана #2. |
| **major · 2** | S:25, В-2; C/[task_pool.py:2130](</home/astra/Документы/Astra Cowork/astra-cowork/src/astra_cowork/core/orchestrator/task_pool.py:2130>), `:2358`, `:2385`; база #1:111 | После включения `astra_voice` в `FASTPATH_SOURCES` команда может попасть в общую ветку «Операционные команды», а просьба об откате — в предыдущий диалог. Фраза синтеза «новый диалог, как у виджета» потеряла оговорку базы и не описывает реальный путь. | Вернуть оговорку о локальных командах и явно описать маршрутизацию отката. Различать «новая задача» и «новый `dialog_id`»; если В-2 требует буквально новый диалог, C2 должен обеспечивать это отдельным правилом для voice. |
| **minor · 2** | S:25, :38; V/[core/stats.py:24](/home/astra/Документы/astra-voice/src/astra_voice/core/stats.py:24), `:99`, `:239`; C/`core/state/models.py:80`, `task_pool.py:2422`, `:2468` | `Task.source` и `Task.hotkey` существуют; **`Task.request_id` и сохраняемого `Task.context` нет**. `context.request_id` проходит дальше в Hermes, но не становится квитанцией в БД. В Voice нынешние `dictation.result` — только `ok/empty/cancelled`; схема базы `delivered/undelivered` тоже не выражает unknown. | Указать, что command `request_id` живёт в памяти приёмника, отдельно от `attention_request_id`. Для статистики перечислить новые поля/enum, включая unknown, обновить фильтрацию и summary; не записывать текст или произвольную причину. |
| **major · 3** | S:28, V3; V/[core/policy.py:78](/home/astra/Документы/astra-voice/src/astra_voice/core/policy.py:78), `:98`, `:106`; `settings.py:89` | Fail-closed для значения `command_hotkey` недостаточен: ошибка разбора **всего файла политики** возвращает пустые значения, затем `effective()` оставляет пользовательскую настройку/дефолт. Ошибка даже другого ключа может таким образом снова включить Win. | В `command_mode.resolve` отдельно обработать `PolicyStatus.INVALID`: командный режим выключен, политика не считается отсутствующей. Проверить также неверный JSON-тип настройки и сравнение сочетаний по разобранным keycode/modifiers, включая алиасы. |
| **major · 4** | S:23, :28; база #1:106; C/[controller.py:330](</home/astra/Документы/Astra Cowork/astra-cowork/src/astra_cowork/ui/prompt_widget/controller.py:330>), `monitor.py:85`, `:662`; V/`dictation.py:528` | Проверка через виджет зависит от существования контроллера и устаревающего снимка. `LockedHint` кешируется на 2 с, запрос `loginctl` может ждать 2 с; при недоступности источников возможен результат «не заблокирован». У Voice нет соответствующего гейта записи/доставки. Команда началась до lock — распознавание закончилось после него. | Вынести доступность сеанса из виджета в общий асинхронный источник; неизвестное состояние закрывает голосовой допуск. Проверять перед записью/отправкой и при допуске сервером. Для уже принятого callback определить реакцию на lock; публикацию текста/уведомления после lock не делать неявным следствием общего fallback. |
| **major · 4** | S:22, :33 | Обоснование отказа от BOOTTIME неверно: Linux `CLOCK_MONOTONIC` **не считает suspend**, поэтому оставшиеся 200 мс могут сохраниться после часового сна. Сопоставимость между процессами здесь работает, сон — отдельная проблема. [Linux clock_gettime](https://man7.org/linux/man-pages/man2/clock_gettime.2.html). | Сохранить выбранный MONOTONIC, но добавить отмену незавершённой командной диктовки/допуска при suspend и защиту от старых сообщений после resume. В эксперименте отдельно проверить сон; не выводить защиту из одного deadline. |
| **major · 4** | S:19, :26, §4; C/[daemon/tray.py:54](</home/astra/Документы/Astra Cowork/astra-cowork/src/astra_cowork/daemon/tray.py:54>), `:60`; `packaging/systemd/astra-cowork-daemon.service:8`; `packaging/bin/astra-cowork-desktop-env:88` | Видимый трей во Fly не доказывает наличие шины: предусмотрен XEmbed. `setAutoStartService(False)` запрещает активацию **сервиса**, но не autolaunch шины. Юнит демона не обеспечивает импорт адреса; имеющаяся desktop-env-обёртка восстанавливает только адрес, уже известный менеджеру. | До C1/V1 проверить одну и ту же шину из GUI и юнита. Для командного соединения использовать явно проверенный адрес `unix:`, исключив альтернативы `tcp:`/`autolaunch:`. Разделить «шины нет» и «адрес не передан»; во втором случае исправляется окружение запуска, не транспорт. |
| **major · 4** | S:28; V/[platform/paste.py:254](/home/astra/Документы/astra-voice/src/astra_voice/platform/paste.py:254), `:430`; `platform/x11.py:127`; `runtime.py:1373` | Владельцем CLIPBOARD становится **Qt-соединение Voice**, не `X11Display`. После выхода Voice выделение не обязано сохраняться; hints специально препятствуют истории менеджера буфера. Кроме того, `publish_clipboard()` возвращает `False`, но синтез безусловно обещает «текст в буфере». При отсутствии шины уведомление с действиями тоже не появится. | Пилюлю показывать по результату публикации; при ошибке хранить последнюю фразу в памяти для повторного копирования. Явно ограничить сохранность временем жизни Voice/владения выделением. Действия восстановления должны быть доступны из самой пилюли/трея, независимо от Notifications. |
| **major · 4** | S:38–39; V/[platform/x11.py:309](/home/astra/Документы/astra-voice/src/astra_voice/platform/x11.py:309), `:328`; `core/constants.py:3`; C/`hotkeys/double_modifier.py:185`, `daemon/form_routing.py:108` | `XGrabKeyboard` не подавляет чтение `/dev/input` Cowork. В текущем коде его комбинация — **Ctrl+C+C**, не Ctrl+Space; ещё есть правые Ctrl×2/Alt×2. Захват Voice ограничен 30 с и снимается лишь при последующих обращениях, запись допускается 120 с. | Исправить матрицу K5 на реальные комбинации; S7 должен проверить evdev и сочетания блокировки экрана. Для записи определить отдельный срок/сторож захвата и снятие на всех исходах, а не использовать без изменений примитив поля настройки хоткея. |
| **minor · 3** | S:27; C/[task_pool.py:1871](</home/astra/Документы/Astra Cowork/astra-cowork/src/astra_cowork/core/orchestrator/task_pool.py:1871>), `:1066` | Буквальное «текст никогда не в сигналах» несовместимо со штатным `task_completed(task_id, reply_text)` и другими внутренними Qt-сигналами. Ответ может содержать исходную команду. | Ограничить запрет **публичными D-Bus-сигналами и диагностикой**. Адаптор экспортирует только контракт; внутренние сигналы не должны попадать на шину через `ExportAllSignals/Contents`. |
| **minor · 4** | S:28; C/[astra-cowork.desktop:10](</home/astra/Документы/Astra Cowork/astra-cowork/packaging/desktop/astra-cowork.desktop:10>) | Действие «Открыть Astra Cowork» вызывает `systemctl start`; ярлык делает то же. Для уже работающего демона при `busy`/`policy_disabled` это не открывает окно и ничего не исправляет. | Назвать существующее действие «Запустить Astra Cowork» и предлагать при отсутствии владельца. Для остальных отказов оставить доступное «Что случилось» либо отдельно определить существующий путь открытия UI. |

Для полноты аудита текста: вне прямого пути `Submit` найдены также C/`daemon/form_routing.py:88,134,147` — текст уведомления/заголовок чужого окна; `automations/cover_finder.py:92,107,138` — поисковая строка из названия и автора книги; `cli/prompt_widget_preview.py:66` — печать введённого текста в stdout демонстрационного приложения. Их нельзя объявлять утечками именно D-Bus-команды без дополнительного пути, но глобальное обещание S:27 их сейчас не учитывает. В проверенных путях Voice прямого журналирования распознанной фразы не нашёл; его `RedactTextFilter` не является универсальной очисткой произвольного текста исключений.

### Эксперименты

**1. Qt5 → Qt6, vardict, UID и ошибки — 10–15 минут.** Изолированная шина, без рабочего Cowork, без файлов. Ниже пробник; здесь его межпроцессную часть не запускал. Ожидания: `Status` и `Submit` возвращают `a{sv}`, UID совпадает с `id -u`; задержка даёт `NoReply`, остановка сервера — `ServiceUnknown`.

```bash
dbus-run-session -- /usr/bin/python3 - <<'PY'
import subprocess
cowork_py = "/home/astra/Документы/Astra Cowork/astra-cowork/.venv/bin/python"
server = r'''
import time
from PyQt6.QtCore import QCoreApplication, QObject, pyqtSlot, pyqtClassInfo
from PyQt6.QtDBus import QDBusAbstractAdaptor, QDBusConnection, QDBusMessage
app = QCoreApplication([])
bus = QDBusConnection.sessionBus()
class API(QDBusAbstractAdaptor):
    pyqtClassInfo("D-Bus Interface", "ru.astralinux.Cowork.Command1")
    @pyqtSlot(result="QVariantMap")
    def Status(self):
        return {"contract": 1, "state": "ready"}
    @pyqtSlot(str, "QVariantMap", QDBusMessage, result="QVariantMap")
    def Submit(self, text, options, message):
        uid = bus.interface().serviceUid(message.service())
        assert uid.isValid()
        time.sleep(options.get("delay_ms", 0) / 1000)
        return {"status": "accepted", "uid": uid.value(), "echo": options}
obj = QObject()
api = API(obj)
assert bus.registerObject("/ru/astralinux/Cowork", obj,
                          QDBusConnection.RegisterOption.ExportAdaptors)
assert bus.registerService("ru.astralinux.Cowork")
print("READY", flush=True)
app.exec()
'''
client = r'''
from PyQt5.QtCore import QCoreApplication
from PyQt5.QtDBus import QDBus, QDBusConnection, QDBusMessage
app = QCoreApplication([])
bus = QDBusConnection.sessionBus()
for method, args in [
    ("Status", []),
    ("Submit", ["probe", {"i": 1, "u": 2**31, "x": 2**40, "s": "тест"}]),
    ("Missing", []),
    ("Submit", ["probe", {"delay_ms": 600}]),
]:
    m = QDBusMessage.createMethodCall(
        "ru.astralinux.Cowork", "/ru/astralinux/Cowork",
        "ru.astralinux.Cowork.Command1", method)
    m.setAutoStartService(False)
    m.setArguments(args)
    r = bus.call(m, QDBus.Block, 300)
    print(method, r.signature(), r.errorName(), r.arguments(), flush=True)
'''
p = subprocess.Popen([cowork_py, "-B", "-c", server], stdout=subprocess.PIPE, text=True)
try:
    assert p.stdout.readline().strip() == "READY"
    subprocess.run(["/usr/bin/python3", "-B", "-c", client], check=True)
finally:
    p.terminate()
    p.wait()
subprocess.run(["/usr/bin/python3", "-B", "-c", client], check=True)
PY
```

Синхронный `call` здесь служит только измерительным пробником без GUI. Для production повторить те же проверки через выбранный `callWithCallback`/watcher, включая поздний ответ и ровно один терминальный callback.

**2. Шина Fly и юнита — 5 минут, только чтение.** Сравнить адрес в терминале графического сеанса, менеджере и процессе демона; наличие адреса дополнить успешным `GetId` этой шины:

```bash
printenv DBUS_SESSION_BUS_ADDRESS
systemctl --user show-environment | rg '^DBUS_SESSION_BUS_ADDRESS='
systemctl --user show astra-cowork-daemon.service -p MainPID -p ExecStart
busctl --user call org.freedesktop.DBus /org/freedesktop/DBus org.freedesktop.DBus GetId
```

Для PID из третьей команды читать из `/proc/PID/environ` **только** `DBUS_SESSION_BUS_ADDRESS`, не выводить всё окружение. Отсутствие переменной само по себе не доказывает отсутствие шины.

**3. Допуск/дедуп/очередь — 10 минут на заглушке исполнителя.** Сценарии: повтор одного ID через 100 и 350 мс; тот же ID с другим текстом; 33 разных ID с шагом 500 мс при незавершающемся исполнителе; остановка между ответом и callback. Считать число запусков и допусков. Это разрешит спор о LRU/TTL и границе `accepted` без выполнения реальных команд.

**4. Главный цикл — 5–10 минут на пробнике.** Добавить задержку 600 мс **до обработки Submit**, затем отдельно **после формирования accepted**. В первом случае просроченная команда не допускается; во втором клиент может получить unknown при единственной принятой задаче. Проверить, что перенос приёмника в поток передаёт работу в GUI очередью, а не вызывает `TaskOrchestrator` из чужого потока.

**5. X11/lock/suspend/CLIPBOARD — 10–15 минут в отдельном тестовом сеансе.** Проверить запись дольше 30 с, реальные evdev-комбинации, блокировку во время распознавания, sleep/resume между отправкой и допуском, чтение контрольного CLIPBOARD до/после выхода Voice при выключенном менеджере истории. Для диагностики достаточно `xclip -selection clipboard -o` и `loginctl show-session "$XDG_SESSION_ID" -p LockedHint`. Рабочий сеанс этим ревью не затрагивал.

### Проверено, чисто

- **Ось 1 — интерфейсы.** Имена шины/объекта/интерфейса допустимы; `ru.astralinux.Cowork` совпадает с документацией Cowork. Реализованных `Status/Submit` пока нет — это новый API. Локально проверены версии биндингов, наличие адаптора, построение метаслотов с `QVariantMap` и служебным `QDBusMessage`. Разные версии Qt сами по себе не препятствуют стандартному D-Bus-контракту; фактический wire round-trip оставлен эксперименту.
- **Ось 2 — данные.** `submit(text, attachments=None, context=None, engine_text=None) -> str` существует. `source/hotkey` доходят в `Task`; история хранится в SQLite `data_dir()/state.db`, включая текст, ответ, диалоги и события. Voice пишет ограниченный набор метрик в `state_dir()/stats.json`, до 1000 событий, файл 0600; текста в текущем пути статистики нет.
- **Ось 3 — инварианты.** Отложенный callback после возврата обычного слота совместим с постановкой ответа раньше работы, если нет реентрантного ожидания. Fast-path действительно проверяет `FASTPATH_SOURCES`. `astra_voice` отсутствует в `_AUTOOPEN_SOURCES`, поэтому штатные create/cloud-routing обработчики окно не откроют. Основной HTTP POST Hermes повторяется только при `ConnectError/ConnectTimeout`, не при `ReadTimeout`.
- **Ось 4 — отказы.** В обоих окружениях Python использует `clock_gettime(CLOCK_MONOTONIC)`; проверенные Qt `msecsSinceReference()` лежат между соседними Python-измерениями. Перезапуск процесса эту шкалу не сбрасывает. `publish_clipboard()` действительно не вызывает XTest и `focus_client`, не трогает PRIMARY и не восстанавливает прежний CLIPBOARD. Штатный путь завершения голосовой задачи сможет показать уведомление без участия формы Cowork.
