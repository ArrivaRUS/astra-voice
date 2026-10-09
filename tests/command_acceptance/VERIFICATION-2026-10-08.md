# Независимая приёмка MappingNotify · 08.10.2026

Проверен HEAD `12f632cdf96481b8adabcc345471cd4f3002142c` после объединения
с актуальным main. **168 passed in 6.81s**: прежние 150 acceptance и 18 новых
регрессий `test_mapping_loss.py`. Ruff check/format PASS; mypy acceptance PASS
(15 source files). Семь исходников runtime/transport/orchestration/app сравнивались
SHA256 до и после прогона и не изменились; HEAD также не изменился.

Новые проверки используют настоящий `HotkeyManager`, публичную очередь
`MappingEvent`/`HotkeyEvent`, runtime и существующие фейковые аппаратные порты.
Подтверждены `record.cancel` и `set_recording(False)` сразу при потере клавиши
в PTT/toggle, успешной смене физического keycode с пропавшим отпусканием и
потере захвата в хвосте записи. Поздний результат не отправляется помощнику.
Ожидание 300 мс не будит микрофон после remap; неизменившаяся карта не мешает
нормальной записи. Восстановление по таймеру ≤30 с требует нового жеста для
новой записи. Конфликт ролей и запоздалый retry после lock не захватывают
клавишу «Текст». Независимая текстовая запись не отменяется потерей command grab.

Проверена чувствительность регрессий к исходному дефекту: только обработчик
`DictationRuntime._on_command_hotkey_state` из `0bae9dad0f4ade493840369bdd682860f94aee3e`
подставлялся в память отдельного процесса через AST. При остальных текущих
исходниках получено **10 failed, 8 passed**: нет отмены записи и автоматического
восстановления. Файлы production при этом опыте не изменялись. Это проверка
старого обработчика, а не полный прогон всего старого checkout.

Команды финального прогона:

```sh
env -u DISPLAY -u WAYLAND_DISPLAY -u QT_ACCESSIBILITY -u AT_SPI_BUS_ADDRESS \
  QT_QPA_PLATFORM=offscreen \
  DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent \
  DBUS_SYSTEM_BUS_ADDRESS=unix:path=/nonexistent \
  /home/astra/.cache/astra-voice-dev/venv-a/bin/pytest tests/command_acceptance -m unit
/home/astra/.cache/astra-voice-dev/venv-a/bin/ruff check tests/command_acceptance
/home/astra/.cache/astra-voice-dev/venv-a/bin/ruff format --check tests/command_acceptance
MYPYPATH=src /home/astra/.cache/astra-voice-dev/venv-a/bin/mypy tests/command_acceptance
```

Обе реальные шины отключены; IPC-сценарии поднимают собственную закрытую
session bus без активации служб. Для unix sockets использована одобренная
sandbox escalation. Живой микрофон, X11/KDE/Fly, настройки пользователя,
установленная dev5 и реальные службы не затрагивались. Коммитов/push нет.
Область правок tester — только `tests/command_acceptance`.

Доказательства: `snapshot-2026-10-08.json`, `acceptance-2026-10-08.log`.
SHA256 манифеста acceptance Python/README:
`7ae4e50e28e5fb34a6ea36e39c731531d48656be5928728769db617390bb0236`.
`snapshot.json` оставлен без изменения как исторический результат 07.10;
он не является доказательством текущего HEAD.

Общий unit/lint выполняет координатор. Живой факт закрытия микрофонного
потока и поведение XGrabKey при настоящем MappingNotify здесь не проверялись:
проверены команды управления и состояние через фейковые аппаратные порты.
