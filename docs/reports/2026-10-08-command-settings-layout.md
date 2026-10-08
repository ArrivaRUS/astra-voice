# Карточка команды: отдельный дизайн и проверка соответствия

По замечанию владельца исправляются потерянные внутренние отступы пояснения,
Win-кнопок и поля захвата в CommandHotkeySettings. Исходная ветка Cowork не
изменялась: worktree command-settings-layout создан от bcde2e7, собственная
ветка codex/command-settings-layout. Это отдельная поправка к ещё не вошедшей
в main ветке команд Cowork; не переносить весь её код через PR микрофона45.

## Выполненная цепочка ролей

1. UX-аналитик Sol6.1/high создал target до реализации: design/command-settings-layout.md
   и design/mockups/command-settings-layout.svg. Целевой PNG находится в
   dist/command-settings-layout-design/command-settings-layout-target.png.
2. Инженер дизайна Sol6.1/high изменил только CommandHotkeySettings.qml.
3. Тестировщик Sol6.1/high независимо проверил геометрию и действия.
4. Отдельный design_reviewer Sol6.1/high сравнивает actual с target; code reviewer
   Astra/high проверяет технический диф. Координатор сводит доказательства.

Target: поля14, gap8, нижнее поле7, line-height18; верхняя SettingRow без
двойного padding, обе нижние toggle-строки сохраняются. Удалено ложное обещание
автоматического возврата меню Win при выходе. Действия и bridge-контракт сохранены.

## Проверки

- Независимый baseline RED тем же финальным тестом:10 FAIL/4 PASS на копии
  QML из bcde2e7 в /tmp. Продуктовую базу для опыта не редактировали.
- Кандидат GREEN:33 PASS за2.96с (14 новых,3 существующих QML,16 unit).
  Настоящий SettingsBridge с изолированными hosts, первые контекстные данные
  появляются как в приложении. Окна900/1035 проверяются после позднего binding.
- Тест и отдельные снимки actual Step3Hotkey подтверждают отсутствие Cowork:
  editor visible=false,height0; installed отстоит на16 от текстовой карточки.
  Ранний direct-component wizard-absent PNG этого не доказывал и заменён.
- make lint PASS: Ruff/format, mypy276 файлов. git diff --check PASS.
- Только offscreen/software, без DISPLAY/Wayland, с недоступными D-Bus/PulseAudio.
  Голос, физические клавиши, служба звука, настройки ОС тестами не трогались.

QML SHA256:7c813a94663b8524a8d736ae61a63e7f4808d29161232ce1746e8231fd9402ae.
Diff SHA256:66b7ba67749411dea048c83cdf12b69069c81fe7c8e1504567655f72b496f7e3.
Test SHA256:c06d4c75951705a4be68a04429fd0e4deb3e62e65f2bc7548cb2d16e87d602f9.
Target spec SHA256:ca17f8c271605da9cb6e2d5cf4b24e6491f837928ac31039864404e1ca221b52.
Target SVG SHA256:4efeb6323de1ff164dfeeaf679f851d1c633e3f10713a17bcb7c9d606bff0713.

Рендеры before/after/extra/Step3 и geometry.json сохранены в
`dist/command-settings-layout-check-2026-10-08/`; это gitignored evidence.
Финальное техническое review Astra/high: открытых замечаний нет.
Отдельное design review Sol6.1/high: PASS target→actual для согласованной правки,
просмотрены24 PNG. Подтверждены обе ширины, полные карточки, actual Step3 absent,
длинный статус, conflict/duplicate/not-grabbed, тёмная тема и видимое кольцо фокуса.
Неполное доказательство direct-component wizard-absent заменено реальным Step3;
дизайн-ревьюер закрыл найденный им пробел до переноса в demo.

Наборы PNG SHA256 (отсортированные filename SPACE SHA256 LF):
after9:94c50a2b31a832eb68ba51b385d8f8782bb0460ddec9acc47bcd8ccccf7031ae;
extra12:946cfde5fd1895c51770a92c98691530e8d7cc3e39881406701c5b614d9f849c;
Step3:b4063281d0c7453c01ffa51d3f859907f399f6dcafdc39113da5deac327b3469.

Живые KDE/Fly, голос, физический Tab-цикл и прокрутка целого окна не приняты
этими offscreen-рендерами. Деплой фиксируется отдельно в корневой памяти. Новая сборка пакета/полный CI Cowork этой узкой поправкой не выполнялись.
