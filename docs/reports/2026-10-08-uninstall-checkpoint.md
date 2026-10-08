# AppImage --uninstall: сохранение перед демо · 08.10.2026

**WIP: не готово к слиянию или установке.** Владелец разрешил код и проверки
во временных каталогах. Установленная dev5, профиль, модели и звук не менялись.
База `722b75aa9b4694ca6e4b09b34034ca2b8b8fa00e`, ветка `codex/appimage-uninstall`,
рабочее дерево `/home/astra/.codex/worktrees/appimage-uninstall/astra-voice`.
Демо объявлено на 07:43 МСК. После возобновления в 07:32 код больше не менялся.

## Сохранено

CLI до GUI; отказ root/.deb; блокировки установки/runtime; защита регистрации,
значков и пользовательских данных; отложенное удаление активной копии после выхода;
отмена pending при переустановке; сохранение кода при неподтверждённом shutdown;
обязательный uninstall в smoke вместо прежнего пропуска; инструкция администратора.
Кнопки About — отдельная незавершённая задача. Новый образ не собран и не установлен.

## Обязательный следующий фикс

`app._children_stopped()` считает любой живой прямой дочерний процесс незавершённым
ресурсом. `_finish_running_copy()` вызывается и при обычном выходе. Поэтому открытое
окно системного звука/внешней программы может создать `.shutdown-incomplete` и
заблокировать последующее удаление/переустановку исправной копии.

Координатор подтвердил это своим изолированным Python-процессом:
`start_new_session=True`, другая session, `_children_stopped()` вернул False;
после завершения ребёнка — True. Настоящее внешнее окно не запускалось.

Согласованное направление, **ещё не реализованное и не проверенное**: исключать
только доказанно отдельную session по `/proc/<pid>/stat`. Ревьюер установил:
`sound._spawn`/`external.open_external` используют `start_new_session=True`,
`WorkerSupervisor._launch` — нет. Перепроверить все relevant launch sites.
Неизвестные данные и исчезнувший task — отказ; исчезнувший PID и zombie — завершённый
процесс. Нужны regressions: живой свой worker, внешняя session, потерянный supervisor
после failed switch, исчезновение task, недоступный procfs, обычный выход без pending.
Демонизированные потомки вне текущего родителя этим механизмом не учитываются.
Альтернатива — точный учёт supervisor при failed stop. Проверка только pending
не годится: обычный выход может оставить orphan без защиты перед следующим CLI.

## Проверки сохранённого дифа

- `make lint`: PASS; Ruff check/format, mypy 251 файлов.
- Целевой pytest: **467 passed, 15 failed**, 13.66 с. Файлы: оба новых
  `test_appimage_uninstall*.py`, `test_app_running_key.py`, `test_userinstall.py`,
  `test_userinstall_register.py`, `test_apprun.py`, `test_bootstrap.py`,
  `test_bootstrap_policy_handoff.py`.
- Все 15 ошибок — stderr/владелец policy.conf в `test_apprun.py` (uid1000 вместо
  ожидаемого root). На неизменённом main воспроизведены **те же 15 failed**, 179 passed,
  6.81 с. Защита прав не ослаблялась.
- `bash -n packaging/appimage/smoke.sh`, `git diff --check`: PASS.
- Ранее независимый тестировщик получил 37 PASS своей ревизии; финальный набор
  координатора включает его неизменённый файл. Полного независимого acceptance нет.

Прогоны: без DISPLAY/Wayland, Qt offscreen, обе D-Bus шины и PulseAudio — несуществующие
адреса; `python -m pytest … -q -o addopts='' --tb=short -p no:cacheprovider`.
VENV `/home/astra/.cache/astra-voice-dev/venv-a`; использованы реальные UID, поскольку
песочница отображает root как 65534. GUI, микрофон и службы не затрагивались.

SHA256 продуктового дифа от базы (app.py, userinstall.py, smoke.sh):
`7f94ffa9849cad27596aa5790ba85639dc2b7fb02094a3fcd9400365a0355d18`.
Авторский тест: `6f634c8935bd5c145f4b4c7108c6fec19936d0d6b9c1ac7960ad993e85b45201`.
Независимый тест: `ed7b79c54fcb8d4c176771e530b7778a7fb5378f9056f08930d871efe058b8e5`.
Логи в `dist/uninstall-check-2026-10-08/`: `targeted.log`, `lint.log`,
`apprun-main-baseline.log`, `external-child-repro.log` (gitignored).

## Продолжение после демо

Security-агент остановился с сообщением о лимите использования. Итоговых независимых
security/code PASS нет; заключение координатора их не заменяет. Не выполнены полный
unit, новая сборка/реальный AppImage smoke, CI WIP, живые KDE/Fly и приёмка. Старые
результаты PR42/43 этот диф не подтверждают. Не сливать и не устанавливать.

Исправить resource blocker; повторить целевые тесты/lint; независимые code/security
review; сборка и настоящий AppImage smoke. Новые задачи — после этого этапа.
Запрет скачивания GCC и отсутствие разрешения на релиз сохраняются.
Voice → Cowork ведётся отдельно в чате «Astra Cowork»; его дерево здесь не менять.
