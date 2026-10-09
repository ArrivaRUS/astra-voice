# T2 — Voice → Cowork Command1, 07.10.2026

Независимый security review: security_analyst, GPT-6 Astra/high. Область — локальная
реализация клиента Command1 1.7 и новые границы доверия. Основание — текущий
незакоммиченный diff и новые production-файлы относительно
`a23095f264beaa87a2ffe3d42954fb3541152001`.

## Итог

В проверенной области на указанном ниже снимке открытых подтверждённых ИБ-находок
не осталось. Две P1-гонки исправлены автором и независимо перепроверены.
Это заключение по коду и изолированным сценариям, **не разрешение выпуска**.
Живая приёмка KDE/Fly, одиночной Win, ранних Win-сочетаний, меню KWin и времени
снятия захватов при блокировке/сне в этот обзор не входит.

## Находки и повторная проверка

1. **P1, закрыто — доверие к модели между GUI и очередью Submit.**
   Первоначально `CommandMode.deliver()` проверял модель только перед постановкой
   в очередь, а `_Worker.submit()` проверял только settings/session. Изолированный
   reproducer: trusted=True при deliver, False до worker.submit; FakeConnection
   получал Submit. Исправление: отдельный model_guard непосредственно перед
   вызовом транспорта; ошибка/исключение → `undelivered/model_untrusted`;
   бюджет перечитывается после guard. Повторный опыт: Submit=0, model_untrusted.
   `DictationRuntime._command_model_trusted()` сверяет request, generation,
   capture, store и revoked callback до и после файлового чтения; изменения
   request/generation/revocation во время current() дают False.

2. **P1, закрыто — потеря lock/sleep при задержке GUI.**
   Раньше queued changed() не содержал состояния: lock/sleep→unlock до обработки
   GUI приводил к двум чтениям уже разрешённого snapshot. Реальный Qt queued
   signal reproducer дал preview_cancel=0 и session_cancel=0. Исправление:
   immutable transition payload, blocked_epoch/sleep_epoch, привязка capture,
   отправки и автоматической публикации к эпохе. После исправления тот же опыт
   даёт preview_cancel=1 и session_cancel=1. Отложенное уведомление хранит эпоху
   конкретного Notice и не оживает после новой записи. Явное копирование после
   подтверждённой разблокировки остаётся разрешённым по §8.5.

3. **P2, закрыто — неверное обещание удаления в PRIVACY.**
   Документ обещал очистку сохранённой фразы при блокировке, хотя контракт и код
   сохраняют её в памяти для восстановления. Теперь различаются новая фраза без
   публикации, память до выхода и уже опубликованный буфер, для которого lock/exit
   не обещают очистки.

## Проверенные границы

- Нормализация/валидация текста, ограниченный набор кодов ошибок и server reason,
  типы и request_id ответа; произвольная remote error string не передаётся в UI/лог.
- Один Submit, без retry и активации службы; адрес только unix, закрепление
  получателя за unique owner; нет резервной вставки команды в активное окно.
- Допуск login1 по собственному uid, Class/Type/Remote/Seat/Display и единственному
  графическому сеансу; unknown закрывает допуск, Unlock сам его не открывает.
  Runtime дополнительно ограничивает командный режим KDE.
- Clipboard/recovery и поздние результаты проходят гейт сеанса; автоматическая
  публикация старой записи закрыта эпохой, уведомления не содержат фразы.
- Предпросмотр использует PlainText; аппаратный Esc и stale timer проверены
  изолированными тестами. Это не native X11/KWin acceptance.
- Worker model_guard не вызывает Qt API: Settings/ModelStore/ModelService —
  обычные Python-объекты, Catalog — frozen data. ModelStore.current() не является
  чистым immutable API: legacy migration/quarantine могут менять файлы. Проверки
  GUI и worker одной отправки последовательны, ошибки закрывают допуск;
  повторная сверка mutable identity после I/O проверена. Отдельная смена всего
  хранилища внешним процессом не является проверенной транзакцией этого обзора.

## Проверки и ограничения

Финальный независимый запуск: **151 passed, 1 deselected** (0.79 s; после добавления двух тестов MappingNotify).
Предыдущий запуск до этой delta: 149 passed, 1 deselected (0.76 s).

```sh
env -u QT_ACCESSIBILITY -u AT_SPI_BUS_ADDRESS \
  QT_QPA_PLATFORM=offscreen \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent \
  DBUS_SYSTEM_BUS_ADDRESS=unix:path=/nonexistent \
  /home/astra/.cache/astra-voice-dev/venv-a/bin/python -m pytest -q -o addopts='' \
  tests/command_acceptance/test_model_trust.py \
  tests/command_acceptance/test_publication.py \
  tests/command_acceptance/test_session_cache.py \
  tests/command_acceptance/test_payload.py \
  tests/unit/test_command_notifications.py tests/unit/test_command_runtime.py \
  tests/unit/test_command_edges.py \
  -k 'not runtime_socket_fallback_resolves_symlink'
```

Исключён единственный `test_runtime_socket_fallback_resolves_symlink`: в первом
запуске sandbox запретил socket.bind (EPERM) до обращения к проверяемому resolver;
это ограничение окружения, не PASS и не воспроизведённый product failure.
Дополнительно выполнены независимые минимальные reproducer до/после fixes с
QCoreApplication и FakeConnection, без подключения к D-Bus. `git diff --check`
прошёл. Полный unit/lint и private-bus Qt5↔Qt6 smoke координатора здесь не выдаются
за независимо выполненные проверки.

Живые пользовательские session/system bus, микрофон, клавиатура и установленная
dev5 не использовались. Никаких production-правок этим reviewer не внесено.

## Граница финальной проверки

После security freeze автор возобновил работу над fail-closed обработкой
MappingNotify/collision клавиш. Эта последующая delta должна пройти отдельный
correctness review; заключение здесь относится к проверенным trust/session/
publication границам и указанному снимку, а не к итоговой будущей ветке.

## Точный production snapshot

SHA256 отсортированного UTF-8 manifest ниже (каждая строка оканчивается LF):
`63be0374a6adc5b9190109fdaee62cbe7e559ffc5ac367f022d52ad63f54145d`.

```text
26b17888f89b400ce0cc668e6d74083780d8413530a8f6a0e673e6eb9d2667e5  docs/PRIVACY.md
20cf971e5af8e0b91c2318564b869d0389224b318fc209102c1e0e2825f3e25c  qml/Main.qml
c1d04b5dfdc6f21e371cb71d2741c07c67942839d553cd72e03fa5745803dbef  qml/Pill.qml
241bff79b8aefd314a32168e991a3025ae0ca5866f499d473ab8ef480c78f3fe  qml/components/CaptureField.qml
21f4edb77eaafb1dfc95417d1f52a55b372c90211f5568e50d3baa9874533454  qml/components/CommandHotkeySettings.qml
778807c32febc5d8fd532ce9c7699a428551246c4b3b07066f5b881e9ad1d574  qml/onboarding/Step3Hotkey.qml
00ccffd884db16b98dc9c675dd3e53ca85be87d0222c891ba75e91fcc07a01c7  qml/sections/General.qml
040962b078516d5057c649733d5d721f05ed504e41d318ee121f768c0a6e8c2d  src/astra_voice/app.py
b98041a6018e99441372a2006fe0279bdc5f4fd1716acd5d96f7666b095c0bbc  src/astra_voice/core/command_mode.py
f617e8c04c221663be5b44b5f3ab8e9ae9c80ec737fedd9e8135b0dfe4cb75ae  src/astra_voice/core/dictation.py
5a89cd18237a83dc3aab5ddacbd5642414833fb8f24b7e161c51026c4485dd99  src/astra_voice/core/settings.py
a196aa36e4cdeb3f400afceb7787ba1446e9fa3b08aaf53f2136cbed4420563e  src/astra_voice/core/stats.py
a5a680a3b212f87f1ee37b5b5177737a448f2a6118571f3a64ee5e900080dd02  src/astra_voice/platform/cowork.py
92a5f5b9b6e224694f745fcf3e42194f23317c2a1b43f3cbe023ebab895a96c0  src/astra_voice/platform/hotkey.py
7adff86840cd03284fd1275809c6d9aeb38e91351debabacd65020359ff451ab  src/astra_voice/platform/session_state.py
05e810c67f249bd99670fd90531cb1ac39306afdd26aa3b237c1944160510737  src/astra_voice/platform/x11.py
2771d1847da40ded2b3b4b7d531f18aa8cbad3f15826d5f52799e56c89f616a1  src/astra_voice/runtime.py
83ecba0c2130de38c88db88331f3ce3c989d819f390839d611f570a1e572b63e  src/astra_voice/ui/bridges.py
d714dac1475e8c1f993bc427ce6a1dcff251c6a688b721a4c838601ead1dc99a  src/astra_voice/ui/hotkey_capture.py
b953fbb07f90244eabeb307e4bf0c17c52b1d3204ad0879af3c5e480bfe12e22  src/astra_voice/ui/notify.py
b7f695d6716041db3ac0ba93e0aa61119738d8d191322ae9b2db3675d0255790  src/astra_voice/ui/pill.py
e14033c07a1bd69f9f4ccf588719ba5466c35e329ac38c833281b8d3a9814957  src/astra_voice/ui/tray.py
6b59fe723a0f2205953077025234348611af0888958a282f25ba4365003b6214  src/astra_voice/ui/tray_icons.py
```

Следующий шаг: независимое correctness review этого снимка и плановая native S7
приёмка в разрешённом владельцем окружении. Изменение перечисленных файлов
требует оценки влияния и перепроверки; этот отчёт не распространяется на будущие
ревизии автоматически.
