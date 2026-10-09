# Cowork в «Продвинутых» и кнопка мыши · 2026-10-09

**Статус: независимые code/security/design проверки и полный локальный прогон закрыты; CI и поставка ещё выполняются.**

## Объём

Поручение владельца: перенести существующие настройки Cowork в существующие «Продвинутые», сохранить настраиваемую клавишу, добавить опциональное удержание настраиваемой кнопки мыши (средняя по умолчанию). Детали — дополнение F17 в PRD, `design/cowork-input/spec.md`, `docs/arch/command-mouse.md`. Мышь выключена по умолчанию; короткий клик не записывает; назначения требуют явного применения.

## База и установленный пакет

PR46 слит в main как `20e1b13656f4976dabdb120603546f81d503ee65`; его проверенный head `83cdbd3` и финальный CI37902921498 прошли все7 обязательных jobs. Dev7 установлена в этой сессии: dpkg exit0, штатный `/usr/bin/astra-voice --hidden`, PID126256; старт11:24:20, модель/selfcheckok11:24:22. Автозапуск побайтно сохранён. Пакет SHA256 `7ff2e5d8ed2bb99c9b9fab0d9ba9980a0ac13d6b29c5d28798cafe5597c9e280`. Новая функция мыши в установленную dev7 не входит.

Рабочая ветка `codex/cowork-mouse-settings` начала с `0b7c580` (база0550c66 и тестовая поправка83cdbd3). Проверенный Win-коммит соседнего чата `43dfcf02dd13d91292d95fa3b933042aa9053dc9` перенесён как `46b58d619e13fc02e30835c3b8958197e4a68d5e`, без конфликтов. Его отдельный отчёт — `2026-10-09-win-lifecycle-plan.md`.

## Разделение ролей и промежуточные доказательства

- UX analyst Sol6.1/high подготовил target; design engineer Sol6.1/high реализует QML; отдельный design reviewer Sol6.1/high сравнивает target и actual.
- Developer complex Astra/high реализовал автономный backend. Независимый tester Sol6.1/high выявил remap physical8→logical31: XIQueryDevice возвращал физическое состояние. Исправлениеv2 использует XIQueryPointer. Backend SHA256 `5ae5f053a9332dd5da4682c11688246eb9c22d91566599a275f9ade4728469fc`; автор86unit/12nativePASS, координатор независимо повторил20тестов во frozen/tmp-копии.
- Другой developer complex Astra/high реализовал bridge/local capture. Frozen patch SHA256 `6e430ef238c93dac6c3a2e6a4f050a0c6710c07c699817ea187b8d5effd4954a`;911targetPASS и make lintPASS. Отдельный security analyst Astra/high подтвердил совпадение9файлов и77inlinechecks, блокирующих дефектов backend+bridge не нашёл. Runtime в это заключение не входит.
- Design v3 принят отдельным design_reviewer Sol6.1/high:139PNG,40состояний,18проверок; исправлены selector logical31 и полная видимость SettingRow приTab. Hash9UI/design files: `1f01732d9c72c619cf4fee861463a81aa162a0f964b83729734cfd62c33230f7`; manifest SHA `e49af3bd1e42a8695fba276647af723e178e42af2c514ee0f5ddf76f1ee8e2bb`. Независимые QML регрессии7PASS. Полный evidence скопирован в корневой проект `dist/dev8-validation/design-v3`; screenshot fixtures не являются живой проверкой установленной версии.
- Runtime использует единый input owner/generation, общий Command1, opaque local capture lease и Escape через существующий keyboard reader. Три отказных пути v1 закрыты в v2; отдельный security reviewer подтвердил 15 сценариев (исключения UI callback, factory/open/close watchdog, terminal record.start error, session/model/capture guards).
- Отдельный code_reviewer Astra/high обнаружил в v2 потерю text-hotkey mapping retry, снятие нового mouse PENDING старым finish timer и потерянный диалог объяснения команды. Автор исправил runtime (5 регрессий падают до правки и проходят после); design_engineer вернул единственный вызов открытия существующего диалога (2 регрессии падают до и проходят после).
- Независимый code review runtime v3: 12 сценариев PASS, включая mapping recovery в pending/recording/processing/preview и обоих редакторах, а также старое завершение text/mouse перед новым hold/Escape/tap. Runtime SHA256 `74b30f633d44f30d9604edf023698972c0373290bcf4436f27439167d2ca070b`, hotkey SHA256 `eeaf7883a639b5dfc7946c235ad6931b077f32a303e2d3e71fcb163425fcc8ba`.
- Финальная поправка Main: `a6cc98e768425fb74709bff769d3e02bfc82111a8423ab6817368d91a61ea30d`; design_reviewer подтвердил, что единственное отличие от v3 — `commandDetailsDialog.open()`, остальные восемь UI/design файлов совпали, геометрия не менялась. Общий SHA девяти файлов теперь `230b6b41d70560c4656405ecaebd854ddecefe8ea58b60320260b7060cadcdbe`.
- Независимый tester Sol6.1/high: 174 целевых PASS и 11 PASS на собственном Xvfb/xcb, lint шести тестовых файлов PASS. Проверял runtime v2; окончательный полный повтор на v3 обязателен. Исправлены устаревшие fixtures/ожидания после переноса раздела и новый event filter; popup selection проверяется с указателем вне popup. Тесты не ослабляют проверку текущего выбора/текста/delegates.
- Финальная авторская ограниченная проверка runtime v3: 687 PASS, 8 SKIP (native в отдельном Xvfb). Полные диагностические прогоны v1 (8758 unit PASS / 2 FAIL, 600 xvfb PASS / 6 FAIL) не являются финальной приёмкой. Makefile теперь устанавливает `ASTRA_VOICE_TEST_X11_ISOLATED=1` только внутри своего `xvfb-run`, чтобы native Win acceptance действительно выполнялись.

## Полный повтор

Снимок `46b58d6` с финальными исходниками и независимыми тестами сохранён в `/tmp/astra-dev8-validation-v3` и отдельной копии `/tmp/astra-dev8-xvfb-v3`; manifest `/tmp/astra-dev8-validation-v3.json`. `make lint` PASS: Ruff/format 406 файлов, mypy 300 файлов. `make test`: **8841 PASS, 10 SKIP**, 266.40 с. `make test-xvfb` после исправления оснастки: **612 PASS, 2 SKIP**, 233.80 с. Пропуски Xvfb: дополнительный каталог снимков M6DL не задан; qmllint не установлен. Проверки выполнялись с недействительными D-Bus/Pulse, без живого DISPLAY; Xvfb создавал собственный сервер.

Первый полный Xvfb v3 дал 611 PASS / 1 FAIL: QTest.mouseMove не перемещал физический X11-курсор. При размере экрана 1280×1024 он оставался в (640,512) и подсвечивал иной пункт popup, хотя выбранная кнопка была правильной. Независимый tester воспроизвёл причину (1 FAIL / 6 PASS), добавил QCursor.setPos + sync исключительно при явно изолированном X11 и сохранил все assertions выбора. Первые 105 тестов в исходном порядке прошли, затем полный повтор v4 — 612 PASS. Дифф v3→v4 содержит только этот тест и документацию; продукт и unit-тесты побайтно прежние, поэтому полный unit не повторялся.

Логи, снимки, manifests и diffs сохранены в основном проекте `dist/dev8-validation`. Исходный manifest v4 — `/tmp/astra-dev8-validation-v4.json`. Финальный code review закрывает все три найденных P2 на приведённых runtime/Main SHA.


Точные промежуточные артефакты в `/tmp/astra-mouse-remap-fix`, `/tmp/astra-mouse-independent`, `/tmp/astra-mouse-coordinator-v2.log`, `/tmp/astra-mouse-bridge-freeze.json`, `/tmp/cowork-ui-evidence-v2/manifest.json`. Перед окончательной фиксацией сохранить нужные отчёты и воспроизводимые команды, не считать временные пути долговременным архивом.

## Ограничения и установка

Живые физические клавиши/мышь, KDE/Fly, блокировка/сон и микрофон новой версии не приняты по unit/Xvfb. Публичного релиза/тега нет. Не запускать диктовку или менять звук без действующего разрешения.

При установке версии с KWin lease сначала штатно остановить старый Voice. Прежний demo оставил Meta=empty; известный исходный baseline — отсутствие ключа. Сверить текущее значение и вернуть только этот ключ при совпадении с demoempty, затем запускать новую версию. Backup: `/home/astra/Документы/Astra Cowork/demo/runtime-backup/kwin-meta-before.json`, обратимый `demo/restore-win-menu.sh`; чужие настройки и автозапуск сохранить. Новый продукт не угадывает происхождение произвольного empty.
