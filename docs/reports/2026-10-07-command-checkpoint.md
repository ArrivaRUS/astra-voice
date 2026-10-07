# Точка продолжения Voice → Cowork · 2026-10-07

Работа остановлена по просьбе владельца перед перезагрузкой. Ветка
`codex/cowork-command` от `a23095f`; результат локальный, не установлен и не
опубликован. Текущий WIP-коммит посмотреть `git log -1`.

## Что сохранено

Qt5-клиент Command1, 300мс/одна попытка, явный local unix bus; session gate с
передачей переходов и epoch; проверка текущей доверенной модели перед Submit;
отдельная клавиша команды, delayed singleWin PTT, DELIVERING без вставки;
опциональный preview3с, recovery в память/буфер; статистика, пилюля, значок и меню
трея, настройки/онбординг; tools/validate cowork; независимые acceptance-тесты;
privacy, bridge documentation и T2. Production frozen hash runtime:
`2771d1847da40ded2b3b4b7d531f18aa8cbad3f15826d5f52799e56c89f616a1`.

## Следующий обязательный шаг

Final reviewer подтвердил P2 на runtime.py (~537): `mapping-regrab:busy` при разных
signatures не проходит через stop/cancel потери захвата. На fake backend после
события `phase=recording`, `fsm=recording`, `_lost=True`, `_keycode=None`;
отпускание Win ничего не меняет, запись остаётся до лимита120с. Исправить потерю
command grab по аналогии text handler, проверить cancel/retry и добавить regression.
После фикса — независимый recheck, targeted+полный unit и lint, новый snapshot.

Остальные final delta (trust pre/post-I/O + at-send, lost lock/sleep transitions,
epoch-bound уведомления, deferred tap и tray wiring) reviewer просмотрел без
новых находок. Повторный targeted suite самим final reviewer не запускался.
Security report содержит независимые151PASS1deselected и hashes; deselection
из-за запрета собственного AF_UNIX bind в sandbox. Общий unit запускается с
доступом только к тестовым сокетам и обеими живыми шинами, заменёнными /nonexistent.

## Доказательства и ограничения

- make lint PASS (Ruff/format/mypy266); V6 XML+191PASS; targeted1220PASS1skip;
  runtime441PASS; UI1559PASS1skip, badge349PASS.
- Первый полный unit:8106PASS10skip9fail. Семь старых tray menu failures исправлены
  удалением неактивных command actions, два cleanup failures — cleanup только
  зарегистрированных handler. Collection issue pytest entrypoint исправлен
  тремя import_module tests.unit→unit; collection8166PASS и affected11PASS.
- Финальный полный unit прерван по просьбе владельца:3472PASS9skip458deselected,
  exit130. Полного PASS финальной ветки нет. Логи: dist/command-check-2026-10-07/.
- Реальный Qt5 Voice↔Qt6 Cowork на частной шине с фиктивным sink:4PASS.
- Native S7/K5 не выполнены: меню KWin/Fly, ранние Win+сочетания (passive grab без
  replay), снятие захватов/крах/блокировка реального сеанса, микрофон/ASR и буфер.
  Fly command сейчас закрыт. До исправлений и приёмки сборку не устанавливать.
- Установленная dev5 и сторонняя AppImage/PR42 не изменялись этой задачей.

## Cowork PDF → PPTX

Ветка codex/pdf-pptx-voice сохранена на GitHub, HEAD65fa15e, draft PR79:
https://github.com/ArrivaRUS/astra-cowork/pull/79. Только синтетические PDF по
ответу владельца.744 targeted/geometry,6 настоящих roundtrip,4 wirePASS.
Отчёт в том checkout: docs/reports/2026-10-07-pdf-pptx-voice.md.
GitHub CI выключен вручную; OnlyOffice и независимый ИБ T2 #74 открыты.
Прод Cowork68d1fae/почтовый4f1ef9d не изменялись; незавершённый CalDAV main
нельзя выкладывать целиком.

## Дополнительный вопрос о тачпаде

Пользователь сообщил об отказе во время работы. Выполнено только чтение:
тачпада нет в /proc/bus/input/devices/X11, TrackPoint enabled=1;
/sys/bus/i2c/devices/i2c-ELAN0676:00 существует без driver. Xorg.0.log оказался
старым (12.03), не свидетельствует о времени текущего отказа. Чтение kernel journal
недоступно без пароля sudo. Настройки/драйверы не менялись. Пользователь затем
поручил вернуться к Cowork; диагностику не продолжать без нового задания.
