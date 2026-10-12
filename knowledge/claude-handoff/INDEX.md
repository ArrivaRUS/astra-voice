# Индекс материалов Astra Voice

Сначала прочитайте [вводную](00_START_HERE.md) и [состояние проекта](01_PROJECT_STATE.md). Ниже полный список документов; содержимое исходников сохранено, кроме отмеченного обезличивания.

## Документы проекта

- [project/.patches/001-агенты-пишут-артефакт-в-конце-обрыв-теряет-всё.md](project/.patches/001-%D0%B0%D0%B3%D0%B5%D0%BD%D1%82%D1%8B-%D0%BF%D0%B8%D1%88%D1%83%D1%82-%D0%B0%D1%80%D1%82%D0%B5%D1%84%D0%B0%D0%BA%D1%82-%D0%B2-%D0%BA%D0%BE%D0%BD%D1%86%D0%B5-%D0%BE%D0%B1%D1%80%D1%8B%D0%B2-%D1%82%D0%B5%D1%80%D1%8F%D0%B5%D1%82-%D0%B2%D1%81%D1%91.md) — 001 — Агенты пишут артефакт в конце → обрыв сессии теряет всю работу
- [project/.patches/002-спайк-мигнул-иконкой-на-панели-заказчика-шина-не-дисплей.md](project/.patches/002-%D1%81%D0%BF%D0%B0%D0%B9%D0%BA-%D0%BC%D0%B8%D0%B3%D0%BD%D1%83%D0%BB-%D0%B8%D0%BA%D0%BE%D0%BD%D0%BA%D0%BE%D0%B9-%D0%BD%D0%B0-%D0%BF%D0%B0%D0%BD%D0%B5%D0%BB%D0%B8-%D0%B7%D0%B0%D0%BA%D0%B0%D0%B7%D1%87%D0%B8%D0%BA%D0%B0-%D1%88%D0%B8%D0%BD%D0%B0-%D0%BD%D0%B5-%D0%B4%D0%B8%D1%81%D0%BF%D0%BB%D0%B5%D0%B9.md) — 002 — Спайк трея мигнул иконкой на панели заказчика: изолировали дисплей, не шину
- [project/.patches/003-параллельные-агенты-делят-scratchpad-пересеклись-дисплеи.md](project/.patches/003-%D0%BF%D0%B0%D1%80%D0%B0%D0%BB%D0%BB%D0%B5%D0%BB%D1%8C%D0%BD%D1%8B%D0%B5-%D0%B0%D0%B3%D0%B5%D0%BD%D1%82%D1%8B-%D0%B4%D0%B5%D0%BB%D1%8F%D1%82-scratchpad-%D0%BF%D0%B5%D1%80%D0%B5%D1%81%D0%B5%D0%BA%D0%BB%D0%B8%D1%81%D1%8C-%D0%B4%D0%B8%D1%81%D0%BF%D0%BB%D0%B5%D0%B8.md) — 003 — Параллельные агенты делят один scratchpad: перезаписали чужой скрипт, дисплеи пересеклись
- [project/.patches/004-микрофон-оставался-открытым-между-записями-звук-из-паузы.md](project/.patches/004-%D0%BC%D0%B8%D0%BA%D1%80%D0%BE%D1%84%D0%BE%D0%BD-%D0%BE%D1%81%D1%82%D0%B0%D0%B2%D0%B0%D0%BB%D1%81%D1%8F-%D0%BE%D1%82%D0%BA%D1%80%D1%8B%D1%82%D1%8B%D0%BC-%D0%BC%D0%B5%D0%B6%D0%B4%D1%83-%D0%B7%D0%B0%D0%BF%D0%B8%D1%81%D1%8F%D0%BC%D0%B8-%D0%B7%D0%B2%D1%83%D0%BA-%D0%B8%D0%B7-%D0%BF%D0%B0%D1%83%D0%B7%D1%8B.md) — 004 — Микрофон оставался открытым между записями: звук из паузы попадал бы в следующую диктовку
- [project/.patches/005-локально-зелено-в-CI-красно-три-разных-окружения.md](project/.patches/005-%D0%BB%D0%BE%D0%BA%D0%B0%D0%BB%D1%8C%D0%BD%D0%BE-%D0%B7%D0%B5%D0%BB%D0%B5%D0%BD%D0%BE-%D0%B2-CI-%D0%BA%D1%80%D0%B0%D1%81%D0%BD%D0%BE-%D1%82%D1%80%D0%B8-%D1%80%D0%B0%D0%B7%D0%BD%D1%8B%D1%85-%D0%BE%D0%BA%D1%80%D1%83%D0%B6%D0%B5%D0%BD%D0%B8%D1%8F.md) — 005 — Локально зелено, в CI красно: три job живут в трёх разных окружениях
- [project/.patches/006-пустой-вывод-при-нормальном-времени-это-ядро-CPU-а-не-декодер.md](project/.patches/006-%D0%BF%D1%83%D1%81%D1%82%D0%BE%D0%B9-%D0%B2%D1%8B%D0%B2%D0%BE%D0%B4-%D0%BF%D1%80%D0%B8-%D0%BD%D0%BE%D1%80%D0%BC%D0%B0%D0%BB%D1%8C%D0%BD%D0%BE%D0%BC-%D0%B2%D1%80%D0%B5%D0%BC%D0%B5%D0%BD%D0%B8-%D1%8D%D1%82%D0%BE-%D1%8F%D0%B4%D1%80%D0%BE-CPU-%D0%B0-%D0%BD%D0%B5-%D0%B4%D0%B5%D0%BA%D0%BE%D0%B4%D0%B5%D1%80.md) — 006 — Пустой вывод при нормальном времени инференса: виновато SIMD-ядро, а не логика (2026-09-14)
- [project/.patches/007-тест-зелёный-на-невозможных-данных-и-окно-до-первого-события.md](project/.patches/007-%D1%82%D0%B5%D1%81%D1%82-%D0%B7%D0%B5%D0%BB%D1%91%D0%BD%D1%8B%D0%B9-%D0%BD%D0%B0-%D0%BD%D0%B5%D0%B2%D0%BE%D0%B7%D0%BC%D0%BE%D0%B6%D0%BD%D1%8B%D1%85-%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D1%85-%D0%B8-%D0%BE%D0%BA%D0%BD%D0%BE-%D0%B4%D0%BE-%D0%BF%D0%B5%D1%80%D0%B2%D0%BE%D0%B3%D0%BE-%D1%81%D0%BE%D0%B1%D1%8B%D1%82%D0%B8%D1%8F.md) — 007 — Тест зелёный на невозможных данных; событийная инициализация открывает окно до первого события (2026-09-14)
- [project/.patches/008-установленный-пакет-это-четвёртое-окружение-лаунчер-рядом-а-не-внутри.md](project/.patches/008-%D1%83%D1%81%D1%82%D0%B0%D0%BD%D0%BE%D0%B2%D0%BB%D0%B5%D0%BD%D0%BD%D1%8B%D0%B9-%D0%BF%D0%B0%D0%BA%D0%B5%D1%82-%D1%8D%D1%82%D0%BE-%D1%87%D0%B5%D1%82%D0%B2%D1%91%D1%80%D1%82%D0%BE%D0%B5-%D0%BE%D0%BA%D1%80%D1%83%D0%B6%D0%B5%D0%BD%D0%B8%D0%B5-%D0%BB%D0%B0%D1%83%D0%BD%D1%87%D0%B5%D1%80-%D1%80%D1%8F%D0%B4%D0%BE%D0%BC-%D0%B0-%D0%BD%D0%B5-%D0%B2%D0%BD%D1%83%D1%82%D1%80%D0%B8.md) — 008 — Установленный пакет — четвёртое окружение: лаунчер «рядом, а не внутри» ломает импорт (2026-09-14)
- [project/.patches/009-цикл-select-не-будится-событиями-других-потоков-ступень-250-мс.md](project/.patches/009-%D1%86%D0%B8%D0%BA%D0%BB-select-%D0%BD%D0%B5-%D0%B1%D1%83%D0%B4%D0%B8%D1%82%D1%81%D1%8F-%D1%81%D0%BE%D0%B1%D1%8B%D1%82%D0%B8%D1%8F%D0%BC%D0%B8-%D0%B4%D1%80%D1%83%D0%B3%D0%B8%D1%85-%D0%BF%D0%BE%D1%82%D0%BE%D0%BA%D0%BE%D0%B2-%D1%81%D1%82%D1%83%D0%BF%D0%B5%D0%BD%D1%8C-250-%D0%BC%D1%81.md) — 009 — Цикл `select` не будится событиями других потоков: ступень +250 мс в p95 (2026-09-15)
- [project/.patches/010-бриф-микрошага-без-mypy-и-чистого-окружения-пропускает-дыру.md](project/.patches/010-%D0%B1%D1%80%D0%B8%D1%84-%D0%BC%D0%B8%D0%BA%D1%80%D0%BE%D1%88%D0%B0%D0%B3%D0%B0-%D0%B1%D0%B5%D0%B7-mypy-%D0%B8-%D1%87%D0%B8%D1%81%D1%82%D0%BE%D0%B3%D0%BE-%D0%BE%D0%BA%D1%80%D1%83%D0%B6%D0%B5%D0%BD%D0%B8%D1%8F-%D0%BF%D1%80%D0%BE%D0%BF%D1%83%D1%81%D0%BA%D0%B0%D0%B5%D1%82-%D0%B4%D1%8B%D1%80%D1%83.md) — 010 · Бриф микрошага без mypy и чистого окружения: сравнение строки с булевым открыло сеть
- [project/.patches/011-зелёный-xvfb-на-подставном-мосте-контракт-QML-Python-не-проверялся.md](project/.patches/011-%D0%B7%D0%B5%D0%BB%D1%91%D0%BD%D1%8B%D0%B9-xvfb-%D0%BD%D0%B0-%D0%BF%D0%BE%D0%B4%D1%81%D1%82%D0%B0%D0%B2%D0%BD%D0%BE%D0%BC-%D0%BC%D0%BE%D1%81%D1%82%D0%B5-%D0%BA%D0%BE%D0%BD%D1%82%D1%80%D0%B0%D0%BA%D1%82-QML-Python-%D0%BD%D0%B5-%D0%BF%D1%80%D0%BE%D0%B2%D0%B5%D1%80%D1%8F%D0%BB%D1%81%D1%8F.md) — 011 · Зелёный xvfb на подставном мосте: контракт QML ↔ Python не проверял никто
- [project/.patches/012-git-add-A-при-работающих-зонах-утащил-чужие-файлы-и-временные-снимки.md](project/.patches/012-git-add-A-%D0%BF%D1%80%D0%B8-%D1%80%D0%B0%D0%B1%D0%BE%D1%82%D0%B0%D1%8E%D1%89%D0%B8%D1%85-%D0%B7%D0%BE%D0%BD%D0%B0%D1%85-%D1%83%D1%82%D0%B0%D1%89%D0%B8%D0%BB-%D1%87%D1%83%D0%B6%D0%B8%D0%B5-%D1%84%D0%B0%D0%B9%D0%BB%D1%8B-%D0%B8-%D0%B2%D1%80%D0%B5%D0%BC%D0%B5%D0%BD%D0%BD%D1%8B%D0%B5-%D1%81%D0%BD%D0%B8%D0%BC%D0%BA%D0%B8.md) — 012 · `git add -A` при работающих зонах: чужие файлы на середине правки и временные снимки уехали в коммит журналов
- [project/.patches/013-переснимать-только-затронутые-экраны-18-из-48-референсов-протухли.md](project/.patches/013-%D0%BF%D0%B5%D1%80%D0%B5%D1%81%D0%BD%D0%B8%D0%BC%D0%B0%D1%82%D1%8C-%D1%82%D0%BE%D0%BB%D1%8C%D0%BA%D0%BE-%D0%B7%D0%B0%D1%82%D1%80%D0%BE%D0%BD%D1%83%D1%82%D1%8B%D0%B5-%D1%8D%D0%BA%D1%80%D0%B0%D0%BD%D1%8B-18-%D0%B8%D0%B7-48-%D1%80%D0%B5%D1%84%D0%B5%D1%80%D0%B5%D0%BD%D1%81%D0%BE%D0%B2-%D0%BF%D1%80%D0%BE%D1%82%D1%83%D1%85%D0%BB%D0%B8.md) — 013 · «Переснимать только затронутые экраны»: 18 из 48 референсов протухли, ревью три круга гонялось за макетом
- [project/.patches/014-агент-прогнал-xvfb-на-живом-дисплее-заказчика-бриф-без-offscreen.md](project/.patches/014-%D0%B0%D0%B3%D0%B5%D0%BD%D1%82-%D0%BF%D1%80%D0%BE%D0%B3%D0%BD%D0%B0%D0%BB-xvfb-%D0%BD%D0%B0-%D0%B6%D0%B8%D0%B2%D0%BE%D0%BC-%D0%B4%D0%B8%D1%81%D0%BF%D0%BB%D0%B5%D0%B5-%D0%B7%D0%B0%D0%BA%D0%B0%D0%B7%D1%87%D0%B8%D0%BA%D0%B0-%D0%B1%D1%80%D0%B8%D1%84-%D0%B1%D0%B5%D0%B7-offscreen.md) — 014 · Агент прогнал тесты интерфейса на живом дисплее заказчика: в брифе не было `offscreen`
- [project/.patches/015-test-harness-must-not-depend-on-patched-apis.md](project/.patches/015-test-harness-must-not-depend-on-patched-apis.md) — Урок 015 — оснастка тестов не должна зависеть от того, что тесты подменяют у себя
- [project/.patches/016-tests-xdg-isolation-and-shared-worktree.md](project/.patches/016-tests-xdg-isolation-and-shared-worktree.md) — Урок: тесты писали в настоящие каталоги пользователя; два агента в одном рабочем дереве
- [project/.patches/017-fly-ветки-не-проверены-по-kde-экран-без-данных-моста.md](project/.patches/017-fly-%D0%B2%D0%B5%D1%82%D0%BA%D0%B8-%D0%BD%D0%B5-%D0%BF%D1%80%D0%BE%D0%B2%D0%B5%D1%80%D0%B5%D0%BD%D1%8B-%D0%BF%D0%BE-kde-%D1%8D%D0%BA%D1%80%D0%B0%D0%BD-%D0%B1%D0%B5%D0%B7-%D0%B4%D0%B0%D0%BD%D0%BD%D1%8B%D1%85-%D0%BC%D0%BE%D1%81%D1%82%D0%B0.md) — 017 — Fly-ветки нельзя считать проверенными по KDE; экран, не читающий мост, молчит без ошибки
- [project/.patches/018-codex-завис-после-сна-ноутбука-агент-ждал-вечно.md](project/.patches/018-codex-%D0%B7%D0%B0%D0%B2%D0%B8%D1%81-%D0%BF%D0%BE%D1%81%D0%BB%D0%B5-%D1%81%D0%BD%D0%B0-%D0%BD%D0%BE%D1%83%D1%82%D0%B1%D1%83%D0%BA%D0%B0-%D0%B0%D0%B3%D0%B5%D0%BD%D1%82-%D0%B6%D0%B4%D0%B0%D0%BB-%D0%B2%D0%B5%D1%87%D0%BD%D0%BE.md) — 018 · Codex завис после сна ноутбука, агент-обёртка ждал его 11 часов
- [project/.patches/019-shape-в-программном-рендере-не-обрезается-снимки-grabwindow-прячут-дефект.md](project/.patches/019-shape-%D0%B2-%D0%BF%D1%80%D0%BE%D0%B3%D1%80%D0%B0%D0%BC%D0%BC%D0%BD%D0%BE%D0%BC-%D1%80%D0%B5%D0%BD%D0%B4%D0%B5%D1%80%D0%B5-%D0%BD%D0%B5-%D0%BE%D0%B1%D1%80%D0%B5%D0%B7%D0%B0%D0%B5%D1%82%D1%81%D1%8F-%D1%81%D0%BD%D0%B8%D0%BC%D0%BA%D0%B8-grabwindow-%D0%BF%D1%80%D1%8F%D1%87%D1%83%D1%82-%D0%B4%D0%B5%D1%84%D0%B5%D0%BA%D1%82.md) — 019 · `Shape` в программном рендере не обрезается Flickable и оставляет «призраки»; снимочные тесты этого не видят
- [project/.patches/020-ci-красный-сутки-после-push-никто-не-смотрел.md](project/.patches/020-ci-%D0%BA%D1%80%D0%B0%D1%81%D0%BD%D1%8B%D0%B9-%D1%81%D1%83%D1%82%D0%BA%D0%B8-%D0%BF%D0%BE%D1%81%D0%BB%D0%B5-push-%D0%BD%D0%B8%D0%BA%D1%82%D0%BE-%D0%BD%D0%B5-%D1%81%D0%BC%D0%BE%D1%82%D1%80%D0%B5%D0%BB.md) — 020 · CI на main красный с 26.09 05:02, заметил заказчик по письмам GitHub
- [project/.patches/021-лямбда-на-destroyed-чужого-объекта-segfault-через-gc-pyqt.md](project/.patches/021-%D0%BB%D1%8F%D0%BC%D0%B1%D0%B4%D0%B0-%D0%BD%D0%B0-destroyed-%D1%87%D1%83%D0%B6%D0%BE%D0%B3%D0%BE-%D0%BE%D0%B1%D1%8A%D0%B5%D0%BA%D1%82%D0%B0-segfault-%D1%87%D0%B5%D1%80%D0%B5%D0%B7-gc-pyqt.md) — 021 · Лямбда с замыканием на сигнале `destroyed` движка — segfault через сборщик мусора PyQt
- [project/.patches/022-пересоздание-qapplication-в-тестах-sip-не-помечает-удалённые-и-агент-в-шине-заказчика.md](project/.patches/022-%D0%BF%D0%B5%D1%80%D0%B5%D1%81%D0%BE%D0%B7%D0%B4%D0%B0%D0%BD%D0%B8%D0%B5-qapplication-%D0%B2-%D1%82%D0%B5%D1%81%D1%82%D0%B0%D1%85-sip-%D0%BD%D0%B5-%D0%BF%D0%BE%D0%BC%D0%B5%D1%87%D0%B0%D0%B5%D1%82-%D1%83%D0%B4%D0%B0%D0%BB%D1%91%D0%BD%D0%BD%D1%8B%D0%B5-%D0%B8-%D0%B0%D0%B3%D0%B5%D0%BD%D1%82-%D0%B2-%D1%88%D0%B8%D0%BD%D0%B5-%D0%B7%D0%B0%D0%BA%D0%B0%D0%B7%D1%87%D0%B8%D0%BA%D0%B0.md) — 022 · Пересоздание QApplication в тестах — sip перестаёт помечать удалённые QML-элементы; агент ходил в D-Bus заказчика
- [project/.patches/023-xvfb-тесты-локально-гоняются-в-offscreen-а-в-ci-на-xcb-shortcut-ждёт-активного-окна.md](project/.patches/023-xvfb-%D1%82%D0%B5%D1%81%D1%82%D1%8B-%D0%BB%D0%BE%D0%BA%D0%B0%D0%BB%D1%8C%D0%BD%D0%BE-%D0%B3%D0%BE%D0%BD%D1%8F%D1%8E%D1%82%D1%81%D1%8F-%D0%B2-offscreen-%D0%B0-%D0%B2-ci-%D0%BD%D0%B0-xcb-shortcut-%D0%B6%D0%B4%D1%91%D1%82-%D0%B0%D0%BA%D1%82%D0%B8%D0%B2%D0%BD%D0%BE%D0%B3%D0%BE-%D0%BE%D0%BA%D0%BD%D0%B0.md) — 023 · xvfb-тесты локально идут в offscreen, а в CI — на настоящем xcb; `Shortcut` ждёт активного окна
- [project/.patches/024-синхронный-notify-с-бюджетом-450-мс-считал-поздний-ответ-провалом-dbus-run-session-поднимал-службы.md](project/.patches/024-%D1%81%D0%B8%D0%BD%D1%85%D1%80%D0%BE%D0%BD%D0%BD%D1%8B%D0%B9-notify-%D1%81-%D0%B1%D1%8E%D0%B4%D0%B6%D0%B5%D1%82%D0%BE%D0%BC-450-%D0%BC%D1%81-%D1%81%D1%87%D0%B8%D1%82%D0%B0%D0%BB-%D0%BF%D0%BE%D0%B7%D0%B4%D0%BD%D0%B8%D0%B9-%D0%BE%D1%82%D0%B2%D0%B5%D1%82-%D0%BF%D1%80%D0%BE%D0%B2%D0%B0%D0%BB%D0%BE%D0%BC-dbus-run-session-%D0%BF%D0%BE%D0%B4%D0%BD%D0%B8%D0%BC%D0%B0%D0%BB-%D1%81%D0%BB%D1%83%D0%B6%D0%B1%D1%8B.md) — 024 · Синхронный `Notify` с бюджетом 450 мс считал поздний ответ провалом; `dbus-run-session` сам поднимал службы
- [project/.patches/025-qobject-из-python-удалялся-в-рабочем-qthread-взаимная-блокировка-gil-и-мьютекса-сигналов-qt.md](project/.patches/025-qobject-%D0%B8%D0%B7-python-%D1%83%D0%B4%D0%B0%D0%BB%D1%8F%D0%BB%D1%81%D1%8F-%D0%B2-%D1%80%D0%B0%D0%B1%D0%BE%D1%87%D0%B5%D0%BC-qthread-%D0%B2%D0%B7%D0%B0%D0%B8%D0%BC%D0%BD%D0%B0%D1%8F-%D0%B1%D0%BB%D0%BE%D0%BA%D0%B8%D1%80%D0%BE%D0%B2%D0%BA%D0%B0-gil-%D0%B8-%D0%BC%D1%8C%D1%8E%D1%82%D0%B5%D0%BA%D1%81%D0%B0-%D1%81%D0%B8%D0%B3%D0%BD%D0%B0%D0%BB%D0%BE%D0%B2-qt.md) — 025 · QObject из Python удалялся в рабочем `QThread`: взаимная блокировка GIL и мьютекса сигналов Qt
- [project/.patches/026-фикс-025-через-movetothread-и-sip-delete-в-трее-повесил-остановку-qtdbus-держит-blockingqueued-на-destroyed.md](project/.patches/026-%D1%84%D0%B8%D0%BA%D1%81-025-%D1%87%D0%B5%D1%80%D0%B5%D0%B7-movetothread-%D0%B8-sip-delete-%D0%B2-%D1%82%D1%80%D0%B5%D0%B5-%D0%BF%D0%BE%D0%B2%D0%B5%D1%81%D0%B8%D0%BB-%D0%BE%D1%81%D1%82%D0%B0%D0%BD%D0%BE%D0%B2%D0%BA%D1%83-qtdbus-%D0%B4%D0%B5%D1%80%D0%B6%D0%B8%D1%82-blockingqueued-%D0%BD%D0%B0-destroyed.md) — 026 · Фикс 025 через `moveToThread` + `sip.delete` в трее повесил остановку: QtDBus держит BlockingQueued на `destroyed`
- [project/.patches/027-проба-захвата-на-окне-фокуса-давала-focusout-ungrab-kwin-снимал-активацию-enter-в-форме-cowork.md](project/.patches/027-%D0%BF%D1%80%D0%BE%D0%B1%D0%B0-%D0%B7%D0%B0%D1%85%D0%B2%D0%B0%D1%82%D0%B0-%D0%BD%D0%B0-%D0%BE%D0%BA%D0%BD%D0%B5-%D1%84%D0%BE%D0%BA%D1%83%D1%81%D0%B0-%D0%B4%D0%B0%D0%B2%D0%B0%D0%BB%D0%B0-focusout-ungrab-kwin-%D1%81%D0%BD%D0%B8%D0%BC%D0%B0%D0%BB-%D0%B0%D0%BA%D1%82%D0%B8%D0%B2%D0%B0%D1%86%D0%B8%D1%8E-enter-%D0%B2-%D1%84%D0%BE%D1%80%D0%BC%D0%B5-cowork.md) — 027 · Проба захвата на окне фокуса давала FocusOut/NotifyUngrab — KWin снимал активацию (Enter в форме Cowork)
- [project/.patches/028-перезахват-хоткея-по-mappingnotify-через-ungrab-grab-молча-терял-клавишу-диктовка-не-стартовала.md](project/.patches/028-%D0%BF%D0%B5%D1%80%D0%B5%D0%B7%D0%B0%D1%85%D0%B2%D0%B0%D1%82-%D1%85%D0%BE%D1%82%D0%BA%D0%B5%D1%8F-%D0%BF%D0%BE-mappingnotify-%D1%87%D0%B5%D1%80%D0%B5%D0%B7-ungrab-grab-%D0%BC%D0%BE%D0%BB%D1%87%D0%B0-%D1%82%D0%B5%D1%80%D1%8F%D0%BB-%D0%BA%D0%BB%D0%B0%D0%B2%D0%B8%D1%88%D1%83-%D0%B4%D0%B8%D0%BA%D1%82%D0%BE%D0%B2%D0%BA%D0%B0-%D0%BD%D0%B5-%D1%81%D1%82%D0%B0%D1%80%D1%82%D0%BE%D0%B2%D0%B0%D0%BB%D0%B0.md) — 028 · Перезахват хоткея по MappingNotify через ungrab→grab молча терял клавишу — диктовка не стартовала
- [project/.patches/029-squash-слияние-стопки-веток-ломает-следующую-ветку-x-ours-портит-хунки.md](project/.patches/029-squash-%D1%81%D0%BB%D0%B8%D1%8F%D0%BD%D0%B8%D0%B5-%D1%81%D1%82%D0%BE%D0%BF%D0%BA%D0%B8-%D0%B2%D0%B5%D1%82%D0%BE%D0%BA-%D0%BB%D0%BE%D0%BC%D0%B0%D0%B5%D1%82-%D1%81%D0%BB%D0%B5%D0%B4%D1%83%D1%8E%D1%89%D1%83%D1%8E-%D0%B2%D0%B5%D1%82%D0%BA%D1%83-x-ours-%D0%BF%D0%BE%D1%80%D1%82%D0%B8%D1%82-%D1%85%D1%83%D0%BD%D0%BA%D0%B8.md) — 029 · Squash-слияние стопки веток ломает следующую ветку; `-X ours` при этом молча портит хунки
- [project/.patches/INDEX.md](project/.patches/INDEX.md) — Уроки — индекс
- [project/AGENTS.md](project/AGENTS.md) — AstraVoice — команда Codex-only
- [project/HEARTBEAT.md](project/HEARTBEAT.md) — HEARTBEAT — где мы сейчас
- [project/LICENSE.txt](project/LICENSE.txt) — LICENSE
- [project/NOTICE.txt](project/NOTICE.txt) — NOTICE
- [project/PRD.md](project/PRD.md) — PRD — Astra Voice v1 (голосовой ввод для Astra Linux SE)
- [project/PROJECT.md](project/PROJECT.md) — Astra Voice — голосовой ввод для Astra Linux (astra-voice)
- [project/README.md](project/README.md) — Astra Voice
- [project/arch/appimage.md](project/arch/appimage.md) — AppImage — архитектура установки, сосуществования и сборки (v0.2, вариант Б)
- [project/arch/challenge-astra-cowork-contract.md](project/arch/challenge-astra-cowork-contract.md) — Челлендж синтеза: контракт Voice ↔ Cowork (US-2.10)
- [project/arch/challenge-astra.md](project/arch/challenge-astra.md) — Челлендж синтеза (Codex)
- [project/arch/onboarding-repeat.md](project/arch/onboarding-repeat.md) — US-1.8 «Пройти настройку заново» — технический план (architect-codex, 30.09.2026)
- [project/arch/plan-claude-cowork-contract.md](project/arch/plan-claude-cowork-contract.md) — Контракт Astra Voice ↔ Astra Cowork — «Команда помощнику» (US-2.10, F17, S18) — план архитектора #1 (Claude)
- [project/arch/plan-claude.md](project/arch/plan-claude.md) — Astra Voice — архитектурный план (Архитектор #1, Claude Fable 5.1)
- [project/arch/plan-codex-cowork-contract.md](project/arch/plan-codex-cowork-contract.md) — Межпроектный контракт Astra Voice ↔ Astra Cowork
- [project/arch/plan-codex.md](project/arch/plan-codex.md) — Astra Voice — архитектурный план #2 (Codex)
- [project/arch/plan-synth-cowork-contract.md](project/arch/plan-synth-cowork-contract.md) — Синтез: контракт Astra Voice ↔ Astra Cowork для US-2.10 «Команда помощнику» — версия 2 (после челленджа Astra 24.09)
- [project/arch/plan-synth.md](project/arch/plan-synth.md) — Astra Voice — синтез архитектуры (суд Юрки, P12)
- [project/arch/s5a5-mic-change.md](project/arch/s5a5-mic-change.md) — S5-A5 / US-11.6 «Микрофон сменился посреди записи»: механизм, контракт, план
- [project/arch/spikes/S1.md](project/arch/spikes/S1.md) — S1 — спайк «пилюля + трей»
- [project/arch/spikes/S2.md](project/arch/spikes/S2.md) — S2 — спайк polkit-помощника (обновлятор, трек A)
- [project/arch/spikes/S3.md](project/arch/spikes/S3.md) — S3 — спайк рантайма (ORT 1.24.4 + onnx-asr 0.12.0 + GigaAM v3)
- [project/arch/spikes/S4.md](project/arch/spikes/S4.md) — S4 — спайк «хоткей + вставка» (`XGrabKey` + XTest + буфер обмена)
- [project/arch/spikes/S5.md](project/arch/spikes/S5.md) — S5 — спайк «захват звука» (`libpulse-simple` через ctypes)
- [project/arch/spikes/S5A5.md](project/arch/spikes/S5A5.md) — S5-A5 · Спайк C: `pa_stream` + `DONT_MOVE` на стенде `virtual_mic`
- [project/arch/spikes/S9.md](project/arch/spikes/S9.md) — S9 — D-Bus между PyQt6-сервером и PyQt5-клиентом
- [project/backlog.md](project/backlog.md) — Astra Voice — беклог: RICE + MoSCoW, разрез по релизам
- [project/data/keys/README.md](project/data/keys/README.md) — Связка релизных ключей
- [project/data/smoke/README.md](project/data/smoke/README.md) — Звук для проверки модели
- [project/data/test/README.md](project/data/test/README.md) — Тестовые записи
- [project/data/vad/README.md](project/data/vad/README.md) — Silero VAD
- [project/decisions/log.md](project/decisions/log.md) — Журнал решений
- [project/design/brand/brand-basics.md](project/design/brand/brand-basics.md) — Базовая айдентика — Astra Voice
- [project/design/flows.md](project/design/flows.md) — UX-флоу и состояния экранов — Astra Voice v1
- [project/design/refs/README.md](project/design/refs/README.md) — Референсы экранов — как они сняты
- [project/design/spec.md](project/design/spec.md) — Дизайн-спецификация — Astra Voice
- [project/design/tokens.md](project/design/tokens.md) — Токены дизайн-системы — Astra Voice
- [project/docs/INSTALL-ADMIN.md](project/docs/INSTALL-ADMIN.md) — Astra Voice — инструкция администратора
- [project/docs/PRIVACY.md](project/docs/PRIVACY.md) — Приватность Astra Voice
- [project/docs/SECURITY.md](project/docs/SECURITY.md) — Безопасность Astra Voice
- [project/docs/codex-team.md](project/docs/codex-team.md) — Юрка: режим «Только Codex»
- [project/docs/contracts/astra-cowork-command1.md](project/docs/contracts/astra-cowork-command1.md) — Контракт Astra Voice ↔ Astra Cowork: голосовая команда помощнику (`ru.astralinux.Cowork.Command1`)
- [project/docs/contracts/latest.json.md](project/docs/contracts/latest.json.md) — Контракт `latest.json`
- [project/docs/plans.md](project/docs/plans.md) — Plans — Astra Voice (execution-pack)
- [project/docs/security/2026-09-30-t1t2-appimage.md](project/docs/security/2026-09-30-t1t2-appimage.md) — Astra Voice — ИБ-проход T1 (AppImage-трек, ревизия 3) + T2 (инкремент ma-foundation / r2-ci / appimage-debian3) — 2026-09-30
- [project/docs/security/T1-appimage-2026-09-28.md](project/docs/security/T1-appimage-2026-09-28.md) — Astra Voice — ИБ-проход T1 (проектирование): AppImage как второй артефакт v0.2 (2026-09-28)
- [project/docs/security/T2-2026-09-26-downloader.md](project/docs/security/T2-2026-09-26-downloader.md) — Astra Voice — ИБ-ревью T2: полный загрузчик моделей (`wip/m6-downloader`, 2026-09-26)
- [project/docs/security/T2-2026-09-26-master-key.md](project/docs/security/T2-2026-09-26-master-key.md) — Astra Voice — ИБ-ревью T2: смена якоря доверия подписи + релизный конвейер (2026-09-26)
- [project/docs/security/T2-2026-09-26.md](project/docs/security/T2-2026-09-26.md) — Astra Voice — ИБ-ревью T2, инкремент 22–24.09 (2026-09-26)
- [project/docs/security/T3-2026-09-28-v0.1.md](project/docs/security/T3-2026-09-28-v0.1.md) — Astra Voice — ИБ-чек релиза T3, v0.1.0 «Диктовка работает» (2026-09-28)
- [project/docs/status.md](project/docs/status.md) — Status — Astra Voice
- [project/docs/test-plan.md](project/docs/test-plan.md) — Test Plan — Astra Voice
- [project/docs/threat-model.md](project/docs/threat-model.md) — Astra Voice — модель угроз (T1, проектирование)
- [project/docs/ui-bridge.md](project/docs/ui-bridge.md) — Контракт интерфейса и логики (QML ↔ Python)
- [project/packaging/appimage/ci-workflow-patch.md](project/packaging/appimage/ci-workflow-patch.md) — Правка `.github/workflows/ci.yml` для AppImage — один раз, 05–06.10
- [project/research/catalog-numbers.md](project/research/catalog-numbers.md) — Каталог моделей v1 — таблица-источник истины (P2, факт-чек)
- [project/research/competitors-ux.md](project/research/competitors-ux.md) — Конкуренты и UX-паттерны утилит голосового ввода (2025–2026)
- [project/research/legal-meetings-astra.md](project/research/legal-meetings-astra.md) — Второе мнение GPT-6 Astra — E12 (право РФ): предзапись и профили голосов
- [project/research/legal-meetings.md](project/research/legal-meetings.md) — Правовое заключение — E12 «Запись встреч и транскрибация» (право РФ)
- [project/research/legal.md](project/research/legal.md) — Правовое заключение — Astra Voice (голосовой ввод для Astra Linux SE)
- [project/research/summary.md](project/research/summary.md) — Свод Discovery — Astra Voice
- [project/research/tech-amx-int8.md](project/research/tech-amx-int8.md) — Пустой текст GigaAM v3 int8 на Intel Xeon с AMX — разбор (Researcher, 2026-09-14)
- [project/research/tech-codex.md](project/research/tech-codex.md) — Технический ресёрч #2 (Codex / GPT-5.6 Sol) — astra-voice
- [project/research/tech-meetings.md](project/research/tech-meetings.md) — E12 «Запись встреч и транскрибация»: техресёрч Discovery
- [project/research/tech-models-claude.md](project/research/tech-models-claude.md) — Техисследование P2 — модели распознавания и проверка их обновлений
- [project/research/tech-platform-claude.md](project/research/tech-platform-claude.md) — Техисследование: платформа под ALSE — самообновление, автозапуск, GUI-стек, хоткей/вставка
- [project/research/vks-clients.md](project/research/vks-clients.md) — E13 «Запись встреч»: какие ВКС-программы реально стоят на ALSE 1.8 и как детектору узнать звонок
- [project/spikes/appimage/RESEARCH-2026-09-28.md](project/spikes/appimage/RESEARCH-2026-09-28.md) — AppImage для Astra Voice — техисследование (2026-09-28)
- [project/spikes/m1_live/README.md](project/spikes/m1_live/README.md) — M1 live — прогон DoD на реальной KDE-сессии
- [project/spikes/m1_live/flicker.md](project/spikes/m1_live/flicker.md) — Баг «моргает экран при наведении на сайдбар» — диагностика (Debugger)
- [project/spikes/m1_live/rss.md](project/spikes/m1_live/rss.md) — RSS GUI-процесса в покое — замеры Optimizer (M1)
- [project/spikes/m4_live/README.md](project/spikes/m4_live/README.md) — M4 — живой прогон Ц2 у заказчика (KDE, 2026-09-14, 0.1.0~m4.3)
- [project/spikes/s1_pill_tray/README.md](project/spikes/s1_pill_tray/README.md) — S1 — спайк «пилюля + трей» (M0)
- [project/spikes/s2_polkit_helper/README.md](project/spikes/s2_polkit_helper/README.md) — S2 — спайк polkit-помощника (M0, трек A обновлятора)
- [project/spikes/s4_hotkey_paste/README.md](project/spikes/s4_hotkey_paste/README.md) — S4 — спайк «хоткей + вставка» (M0)
- [project/spikes/s5_audio/README.md](project/spikes/s5_audio/README.md) — S5 — спайк «захват звука» (M0)
- [project/stories.md](project/stories.md) — Astra Voice — пользовательские истории (INVEST) и story map
- [project/time.local.md](project/time.local.md) — ⏱ Тайм-лог — astra-voice (локально, не в git)

## Долгосрочная память Claude

- [claude-memory/agent_worktree_needs_repo_cwd.md](claude-memory/agent_worktree_needs_repo_cwd.md) — agent_worktree_needs_repo_cwd
- [claude-memory/astra_kde_fly_notifications_kills_plasmashell.md](claude-memory/astra_kde_fly_notifications_kills_plasmashell.md) — astra_kde_fly_notifications_kills_plasmashell
- [claude-memory/astra_voice_autostart.md](claude-memory/astra_voice_autostart.md) — astra_voice_autostart
- [claude-memory/codex_cli_astra_machine.md](claude-memory/codex_cli_astra_machine.md) — codex_cli_astra_machine
- [claude-memory/environment/astra-linux-environment.md](claude-memory/environment/astra-linux-environment.md) — astra-linux-environment
- [claude-memory/environment/astra-linux-plasma-environment.md](claude-memory/environment/astra-linux-plasma-environment.md) — astra-linux-plasma-environment
- [claude-memory/environment/kde-icon-name-dash-fallback.md](claude-memory/environment/kde-icon-name-dash-fallback.md) — kde-icon-name-dash-fallback
- [claude-memory/feedback_codex_limits_architecture_only.md](claude-memory/feedback_codex_limits_architecture_only.md) — feedback_codex_limits_architecture_only
- [claude-memory/feedback_fewer_security_reviews.md](claude-memory/feedback_fewer_security_reviews.md) — feedback_fewer_security_reviews
- [claude-memory/feedback_no_repeat_security_nags.md](claude-memory/feedback_no_repeat_security_nags.md) — feedback_no_repeat_security_nags
- [claude-memory/feedback_no_security_nagging_beta.md](claude-memory/feedback_no_security_nagging_beta.md) — feedback_no_security_nagging_beta
- [claude-memory/feedback_no_ui_text_for_timing.md](claude-memory/feedback_no_ui_text_for_timing.md) — feedback_no_ui_text_for_timing
- [claude-memory/feedback_respond_in_russian.md](claude-memory/feedback_respond_in_russian.md) — feedback_respond_in_russian
- [claude-memory/feedback_security_reviews_sparingly.md](claude-memory/feedback_security_reviews_sparingly.md) — feedback_security_reviews_sparingly
- [claude-memory/feedback_ui_copy_plain_language.md](claude-memory/feedback_ui_copy_plain_language.md) — feedback_ui_copy_plain_language
- [claude-memory/handy_custom_build.md](claude-memory/handy_custom_build.md) — handy_custom_build
- [claude-memory/needrestart_user_session_dialog.md](claude-memory/needrestart_user_session_dialog.md) — needrestart_user_session_dialog
- [claude-memory/project_astra_voice.md](claude-memory/project_astra_voice.md) — project_astra_voice
- [claude-memory/project_astra_voice_dev_docs.md](claude-memory/project_astra_voice_dev_docs.md) — project_astra_voice_dev_docs
- [claude-memory/repo_handy_gigaam.md](claude-memory/repo_handy_gigaam.md) — repo_handy_gigaam
- [claude-memory/stt_engine_gigaam.md](claude-memory/stt_engine_gigaam.md) — stt_engine_gigaam
- [claude-memory/target_environment_astra_1_8.md](claude-memory/target_environment_astra_1_8.md) — target_environment_astra_1_8
- [claude-memory/user_role_astra_cpo.md](claude-memory/user_role_astra_cpo.md) — user_role_astra_cpo

## Документы отдельных веток

- [branch-notes/wt-micvol/PRD.md](branch-notes/wt-micvol/PRD.md) — PRD — Astra Voice v1 (голосовой ввод для Astra Linux SE)
- [branch-notes/wt-micvol/docs/ui-bridge.md](branch-notes/wt-micvol/docs/ui-bridge.md) — Контракт интерфейса и логики (QML ↔ Python)
- [branch-notes/wt-rv-mic/docs/ui-bridge.md](branch-notes/wt-rv-mic/docs/ui-bridge.md) — Контракт интерфейса и логики (QML ↔ Python)

## Сводки контекста чатов

- [chat-history/summaries/2026-09-09_46e5d286_01.md](chat-history/summaries/2026-09-09_46e5d286_01.md) — Разработка Astra Voice
- [chat-history/summaries/2026-09-14_46e5d286_02.md](chat-history/summaries/2026-09-14_46e5d286_02.md) — Разработка Astra Voice
- [chat-history/summaries/2026-09-15_46e5d286_03.md](chat-history/summaries/2026-09-15_46e5d286_03.md) — Разработка Astra Voice
- [chat-history/summaries/2026-09-17_46e5d286_04.md](chat-history/summaries/2026-09-17_46e5d286_04.md) — Разработка Astra Voice
- [chat-history/summaries/2026-09-22_46e5d286_05.md](chat-history/summaries/2026-09-22_46e5d286_05.md) — Разработка Astra Voice
- [chat-history/summaries/2026-09-22_46e5d286_06.md](chat-history/summaries/2026-09-22_46e5d286_06.md) — Разработка Astra Voice
- [chat-history/summaries/2026-09-23_46e5d286_07.md](chat-history/summaries/2026-09-23_46e5d286_07.md) — Разработка Astra Voice
- [chat-history/summaries/2026-09-26_46e5d286_08.md](chat-history/summaries/2026-09-26_46e5d286_08.md) — Разработка Astra Voice
- [chat-history/summaries/2026-09-27_46e5d286_09.md](chat-history/summaries/2026-09-27_46e5d286_09.md) — Разработка Astra Voice
- [chat-history/summaries/2026-09-28_46e5d286_10.md](chat-history/summaries/2026-09-28_46e5d286_10.md) — Разработка Astra Voice
- [chat-history/summaries/2026-09-28_46e5d286_11.md](chat-history/summaries/2026-09-28_46e5d286_11.md) — Разработка Astra Voice
- [chat-history/summaries/2026-09-29_46e5d286_12.md](chat-history/summaries/2026-09-29_46e5d286_12.md) — Разработка Astra Voice
- [chat-history/summaries/2026-09-29_46e5d286_13.md](chat-history/summaries/2026-09-29_46e5d286_13.md) — Разработка Astra Voice
- [chat-history/summaries/2026-09-30_46e5d286_14.md](chat-history/summaries/2026-09-30_46e5d286_14.md) — Разработка Astra Voice
- [chat-history/summaries/2026-09-30_46e5d286_15.md](chat-history/summaries/2026-09-30_46e5d286_15.md) — Разработка Astra Voice

## Диалоги по дням

- [chat-history/dialogue/2026-08-27_ba215ac1.md](chat-history/dialogue/2026-08-27_ba215ac1.md) — Предыстория astra stt
- [chat-history/dialogue/2026-09-01_f80945d6.md](chat-history/dialogue/2026-09-01_f80945d6.md) — Предыстория Handy и GigaAM
- [chat-history/dialogue/2026-09-02_f80945d6.md](chat-history/dialogue/2026-09-02_f80945d6.md) — Предыстория Handy и GigaAM
- [chat-history/dialogue/2026-09-04_f80945d6.md](chat-history/dialogue/2026-09-04_f80945d6.md) — Предыстория Handy и GigaAM
- [chat-history/dialogue/2026-09-07_46e5d286.md](chat-history/dialogue/2026-09-07_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-08_46e5d286.md](chat-history/dialogue/2026-09-08_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-09_46e5d286.md](chat-history/dialogue/2026-09-09_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-11_46e5d286.md](chat-history/dialogue/2026-09-11_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-14_46e5d286.md](chat-history/dialogue/2026-09-14_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-15_46e5d286.md](chat-history/dialogue/2026-09-15_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-16_46e5d286.md](chat-history/dialogue/2026-09-16_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-17_46e5d286.md](chat-history/dialogue/2026-09-17_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-21_46e5d286.md](chat-history/dialogue/2026-09-21_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-22_46e5d286.md](chat-history/dialogue/2026-09-22_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-23_46e5d286.md](chat-history/dialogue/2026-09-23_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-24_46e5d286.md](chat-history/dialogue/2026-09-24_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-26_46e5d286.md](chat-history/dialogue/2026-09-26_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-27_46e5d286.md](chat-history/dialogue/2026-09-27_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-28_46e5d286.md](chat-history/dialogue/2026-09-28_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-29_46e5d286.md](chat-history/dialogue/2026-09-29_46e5d286.md) — Разработка Astra Voice
- [chat-history/dialogue/2026-09-30_46e5d286.md](chat-history/dialogue/2026-09-30_46e5d286.md) — Разработка Astra Voice

