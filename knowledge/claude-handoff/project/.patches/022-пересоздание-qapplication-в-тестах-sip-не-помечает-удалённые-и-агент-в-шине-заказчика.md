# 022 · Пересоздание QApplication в тестах — sip перестаёт помечать удалённые QML-элементы; агент ходил в D-Bus заказчика

**Дата:** 2026-09-27. **Где:** CI job `pytest -m xvfb` — SIGSEGV в `tests/xvfb/test_onboarding.py` `visible_texts`
(7 из ~35 прогонов с 25.09, в т. ч. 39f8a17 m6.19 — письма заказчику); фикс — ветка `wip/xvfb-fix`.

## Что случилось
1. Фикстура `popup_app` (scope=module) создавала QApplication, которым владел Python; после модуля sip его уничтожал,
   следующий модуль создавал новое. **После пересоздания QApplication PyQt5 перестаёт помечать удалённые из C++
   QML-элементы** (`sip.isdeleted(root)` → False вместо True — доказано минимальной пробой).
2. `test_dictation_cycle` оставлял живые `Pill` (держал `QTimer.singleShot(partial(...))`) с `_root` — обёрткой уже
   удалённого корня QML. sip держит для обёртки QQuickItem доп. запись по адресу **+16** (подобъект QQmlParserStatus).
   Новый элемент onboarding на «старый корень + 16» → `childItems()` отдаёт устаревшую обёртку → `isVisible()` → SIGSEGV.
3. Уроки 020 и 021 лечили симптомы того же класса (кадры traceback, лямбды); корень — пересоздание приложения.
4. **Нарушение ограничения:** при расследовании debugger гонял тесты стандартной командой, которая не изолировала
   D-Bus — `Tray` ходил в **настоящую сессионную шину заказчика** (`GetNameOwner`/`AddMatch`, возможно мелькал значок
   в трее) примерно 07:00–07:45. libdbus запоминает адрес шины при первом подключении — `monkeypatch` в фикстуре не
   спасает. Заказчику сообщено.

## Правило на будущее
1. **Одна QApplication на процесс тестов**, никогда не удалять (`tests/helpers/qt_app.py` держит ссылку); создавать
   только через `get_qapplication()`; страж в conftest — адрес приложения не меняется за сессию.
2. Контрольный тест механизма (`tests/xvfb/test_zz_qapp_single.py`): `sip.delete(view)` → `sip.isdeleted(root)`.
3. **Тесты агентов — только с изоляцией шины:** в команду добавлено
   `-u QT_ACCESSIBILITY -u AT_SPI_BUS_ADDRESS DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent`; conftest выставляет то же
   до импорта Qt (разрешение — только `ASTRA_VOICE_TEST_ALLOW_SESSION_BUS=1`). Бриф агенту без этой строки — ошибка брифа.
4. Зависание CI должно оставлять стек: `faulthandler_timeout = 120`, `timeout -s ABRT` в `make test-xvfb`,
   `timeout-minutes` на шаге.
5. Плавающий красный CI — не «перезапустить и забыть»: разбирать все прошлые красные прогоны (`gh run list`), считать
   частоту и искать общий стек.
