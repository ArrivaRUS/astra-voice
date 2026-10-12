# Astra Voice — архитектурный план #2 (Codex)

> **Движок:** OpenAI Codex CLI 0.153.4, модель `gpt-6-astra`, `model_reasoning_effort=xhigh`, sandbox `read-only`.
> **Дата:** 2026-09-09. **Запусков:** 1 (полный бриф одной командой, без деления; ~15 мин, 391 442 токена, exit 0).
> **Команда:** `codex exec -m "gpt-6-astra" -c model_reasoning_effort="xhigh" -s read-only "<бриф>" </dev/null` из `/home/astra/Документы/astra-voice`.
> **Ответ полный** (4 секции: компоненты · упаковка · вехи · риски/тесты/инварианты), обрывов не было. Файлы Codex не менял.
> Ниже — сырой финальный вывод Codex без правок.

---

Предлагаю **onnx-asr как единственный рантайм v1**, отдельный процесс распознавания, PyQt5/QML для интерфейса и GPG/gpgv для подписей. Рамки сохраняются. Файлы не изменены; приведённые ниже команды — планируемые проверки, а не результаты выполненных тестов.

При расхождениях применяю приоритет: последние решения → PRD 0.3 → принятая дизайн-спека и tokens.json → исследования.

| Расхождение в документах | Как учитывать в реализации |
|---|---|
| В `design/spec.md` осталась пометка «черновик» | G3 уже принят в журнале решений |
| S9-A5 и некоторые строки дизайна запрещают ручную проверку при выключенных автопроверках | Ручная проверка разрешена; блокируют только offline/policy |
| PRD требует смены модели с работающей старой и суммарно ≤600 МБ | В обычном режиме — ≤600 МБ; временный пик двух моделей измерять отдельно. Если 600 МБ — абсолютный предел, требования конфликтуют; это зафиксировать до реализации переключения |
| PRD F3 связывает пилюлю с темой; принятая спека задаёт фиксированную палитру | Окно настроек меняет тему; пилюля использует `color.fixed.*` |
| В §9 tokens.md остались «easing не задан» и примеры Qt 6 | Брать кривые из актуального tokens.json; реализовывать средствами Qt 5.15 |
| В исследованиях фигурируют 222 МБ и 415 МБ | Для выбранного экспорта — 226,4 МБ по каталогу; 415 МБ — исторический замер Handy, не измерение нового воркера |

1. Архитектура строится вокруг независимых состояний записи, распознавания, установки моделей и обновления приложения.

| Компонент | Ответственность | Зависит от | Как тестировать |
|---|---|---|---|
| **GUI-процесс** | `QApplication` + `QQmlApplicationEngine`, трей, окна, контроллеры. Не импортирует ORT. Тяжёлые операции вне GUI-потока | Системный PyQt5 | Готовность трея ≤1,5 с; отзывчивость во время загрузки/инференса; RSS без модели ≤120 МБ |
| **DictationController** | Сессия с `utterance_id`, окном назначения и поколением воркера. Не более одного распознавания и одной дополнительной записи; вставки строго по порядку | Хоткей, аудио, WorkerSupervisor, PasteService | Перестановка событий, двойное отпускание, очередь заполнена, отмена перед результатом, поздний ответ |
| **WorkerSupervisor** | Запускает новый Python через `exec`, без `fork` состояния Qt. Один постоянный воркер; временный кандидат при смене модели. Крах/зависание не роняет GUI; максимум 3 автоперезапуска за 10 мин | `subprocess`, Unix IPC | SIGSEGV/SIGKILL, EOF, зависание, несовместимая версия протокола; четвёртый сбой требует действия пользователя |
| **IPC** | Наследуемый `AF_UNIX socketpair`; сообщения JSON с длиной, версией протокола и ID. PCM передаётся через FD `memfd`, после записи — запечатанный от изменения. Без pickle и localhost TCP | `SCM_RIGHTS`, `QSocketNotifier` | Частичные сообщения, неверная длина, неизвестная команда, лишние FD, утечки дескрипторов |
| **ASR-воркер** | `load / recognize / cancel / shutdown`; модель постоянно в памяти. Только CPUExecutionProvider. Потоки по умолчанию `min(4, physical_cores)`, inter-op=1; отключён spinning в простое | onnx-asr, ORT, системный NumPy | Все 12 пресетов; холодный запуск; ≥50 тёплых прогонов дефолта; idle CPU ≤1% |
| **Отмена ASR** | Отдельный управляющий поток принимает cancel; проверка между сегментами/шагами декодера и `RunOptions.terminate` для выполняемого ORT-вызова. Минимальный сопровождаемый патч onnx-asr, если API не проводит RunOptions | ASR-адаптер | S14: готовность к новой записи ≤1 с, без выгрузки модели. Убийство и загрузка за 5 с не считаются прохождением этого теста |
| **Глобальный хоткей X11** | `python3-xlib`, `XGrabKey`, Press/Release; Ctrl+Space PTT, toggle вторым назначением. Caps/NumLock, смена раскладки, autorepeat, отпускание модификаторов, восстановление X-соединения | X11/XKB | ru/en, удержание, повтор клавиши, отпускание Ctrl раньше Space, смена раскладки, suspend/resume |
| **Редактор хоткея** | Читает `kglobalshortcutsrc`, показывает действие KDE. Конфликт — предупреждение; `BadAccess` при реальном grab — ошибка. Временный захват клавиатуры только во время настройки, освобождение по Esc/потере окна | Хоткей, QML | Alt+F2, дубликат PTT/toggle, отказ grab; системные сочетания не переназначаются автоматически |
| **AudioCapture** | `QAudioInput` в отдельном QThread, PulseAudio backend поверх PipeWire. Открытие при хоткее/явном тесте; PCM mono 16 kHz, либо согласованный формат с ресэмплингом в воркере | QtMultimedia, аудиосервер | Виртуальный источник; отключение USB; неподдержанный формат; переполнение; быстрый отпуск до открытия |
| **VAD и сегментация** | Silero VAD ONNX с MIT, отдельный от исключённого Silero STT. До 20 с — один фрагмент; длиннее — границы по паузам, каждый вход GigaAM <24 с. Непрерывная речь — перекрытие и сборка стыков с учётом временных меток | ORT в воркере | Тишина, шум, слабый голос; 19,9/20,1/23,9/24,1 с; речь без пауз; повторяющиеся слова на стыке |
| **Ограничения записи** | <0,3 с — молчаливое игнорирование; 120 с по умолчанию, настройка 30–300 с. Лимит останавливает захват и запускает обработку сразу; двухсекундный текст пилюли не задерживает ASR | AudioCapture, Controller | Границы длительности, отмена, монотонные часы; максимум PCM16 — 9,6 МБ на 300 с |
| **MicRecovery** | Сырой пик до VAD: <−60 dBFS за 2 с → «Микрофон молчит». Обычный retry 3×300 мс; после входа — до 3 повторов через 1 с. Перезапуск WirePlumber только кнопкой пользователя | `systemctl --user`, QProcess | Нулевая амплитуда, mute, greeter-гонка, отсутствующая служба. После restart — повторное открытие и проверка ≤3 с |
| **PasteService** | Запоминает `_NET_ACTIVE_WINDOW` до пилюли. Только текстовый CLIPBOARD → 50 мс → XTest-комбинация → восстановление через 100 мс. Восстанавливает лишь если буфер всё ещё принадлежит нашей операции | QClipboard, X11/XTest | Kate, Firefox, LibreOffice; пользователь скопировал другое во время вставки; медленный получатель; окно закрылось |
| **Терминалы и Klipper** | Правила по WM_CLASS: Konsole/Fly и другие — проверенные комбинации; xterm допускает отдельное правило Shift+Insert. Опция `x-kde-passwordManagerHint`. Последний текст только в RAM | PasteService | Отсутствие `^V`, отсутствие синтетического Enter; CLIPBOARD/PRIMARY; история Klipper действительно не содержит текст |
| **PillWindow** | Отдельный QQuickWindow: Tool, Frameless, StaysOnTop, DoesNotAcceptFocus; skip-taskbar/pager. Без `activateWindow/requestActivate`. Кнопка × остаётся кликабельной | X11/KWin, QScreen, Theme | Активное окно не меняется при показе/скрытии/отмене; fullscreen, Alt+Tab, панели, несколько мониторов |
| **Позиционирование пилюли** | Монитор исходного активного окна, иначе primary; центр по X, 48 px от края рабочей области. Пересчёт при изменении панели, DPR и конфигурации экранов | QScreen, EWMH struts | Панель >48 px, боковая панель, отрицательные координаты, масштаб 100/125/150%, отключение монитора |
| **Трей и уведомления** | `QSystemTrayIcon` + QMenu, SNI через системную интеграцию Qt. Регистрация с retry до 30 с и повторно после перезапуска панели. Notifications D-Bus с actions; ошибки доступны также в окне | QtWidgets, QtDBus, оболочка | Нет watcher/notification service, перезапуск Plasma, Fly; шесть состояний, статичная иконка |
| **SettingsWindow / onboarding** | Шесть разделов + скрытая отладка. Минимум 900×620; завершение onboarding требует модели. Окна и диалоги используют общие компоненты | ViewModel, SettingsStore | S1/S11–S13; Tab/стрелки/Esc, обе локали, обе темы, все состояния макета |
| **ModelRegistry / Activator** | Подписанный каталог, локальные ревизии, фильтры, активная модель. Кандидат загружается и проходит smoke; старая обслуживает записи. Переключение на границе сессий; старый воркер завершается после своих заданий | Загрузчик, Supervisor | Сбой загрузки/смоука, нехватка RAM, диктовка во время переключения, запрещённое удаление активной |
| **ModelDownloader** | Докачка, подпись каталога, sha256 каждого файла, атомарная установка набора, источники и лимиты — ниже | NetworkService, Registry | HTTP-стенд: 206/200/416, обрыв, 429, неправильный Range, диск заполнен, вредоносные пути |
| **RevisionWatcher** | Проверяет установленные экспорты, ветки оригинала и обновление каталога; отдельно сигнал о близких моделях. Только уведомляет | HF API, подписанный каталог, cache | Фикстуры веток main/e2e_rnnt; SHA разных репозиториев; повторные уведомления; offline |
| **AppUpdater / TrackResolver** | Releases API/корпоративный latest.json, SemVer, загрузка и проверка; выбор GUI/admin по policy, ЗПС и правам | NetworkService, gpgv, helper | S6/S9/S10; prerelease, подпись, неверный пакет, отсутствие агента, отказ авторизации |
| **Root-helper** | Единственная привилегированная операция: проверить локальный комплект в root-staging и вызвать apt с фиксированными аргументами | polkit, gpgv, apt/dpkg | TOCTOU, symlink, подмена ключа/пути/версии, apt lock, сбой питания; отдельный security review |
| **Autostart / single-instance** | Один XDG `.desktop`, `Exec=/usr/bin/astra-voice --hidden`, `TryExec`, `X-KDE-autostart-after=panel`, без OnlyShowIn. Создание только по изменению настройки | XDG, QLockFile/локальный IPC | Перелогин KDE/Fly; файл не переписывается при старте; второй запуск открывает первый; Handy не затрагивается |
| **SettingsStore / PolicyStore** | Версионированный JSON, миграции, атомарная запись, `.bak` повреждённого файла. Эффективные значения вычисляются в Python; policy нельзя обойти через QML/CLI | `/etc/astra-voice/policy.conf`, XDG | Повреждённый JSON, новая схема, read-only home, policy-lock, сброс настроек |
| **Theme / i18n** | kdeglobals до `engine.load`; watcher файла/каталога + D-Bus, реакция ≤2 с. ru/en `.ts/.qm`, плюрализация, форматирование QLocale | Генератор tokens.json | Нет светлой вспышки; атомарная замена kdeglobals; длинный английский текст; отсутствующий шрифт |
| **Логи / метрики / диагностика** | 5×1 МБ логов; stats и measurements отдельно. Только коды, длины, времена. Диагностический ZIP по кнопке, с удалением домашних путей, секретов proxy/URL, без текста и аудио | procfs, SettingsStore | Поиск контрольного текста/секретов в файлах; ротация; очистка; закрытые права доступа |

`RunOptions.terminate` предусмотрен ORT, но достижимость ≤1 с для всех декодеров должна быть доказана спайком, включая Whisper. [API ORT](https://onnxruntime.ai/docs/api/python/api_summary.html)

Для пилюли нельзя считать `availableGeometry` достаточным доказательством корректности нескольких мониторов: у Qt/X11 есть ограничения определения рабочей области. Основной API сохраняется, адаптер проверяет панели через EWMH и нормализует координаты. [QScreen Qt 5.15](https://doc.qt.io/archives/qt-5.15/qscreen.html)

| Контракт процессов | Решение |
|---|---|
| Команды | `Hello`, `LoadModel`, `Recognize`, `Cancel`, `Shutdown`; события `Ready`, `Result`, `Cancelled`, `Error`, `Metrics` |
| Проверка совместимости | `protocol_version`, `app_build`, `runtime_version`, `model_id`, `revision`, `generation` |
| Ограничения IPC | JSON ≤64 КиБ, проверка типа/длины PCM, длительность ≤300 с, ограниченная очередь; закрытие неизвестных FD |
| Отмена | Отменённый ID никогда не вставляется, даже если поздний результат корректен |
| Владение моделью | Каждая сессия закрепляет поколение воркера; смена активной модели не перенаправляет уже начатую запись |
| Завершение | Остановить захват, отменить задания, восстановить принадлежащий операции буфер, закрыть FD и воркеры |
| Изоляция | Отдельный процесс защищает GUI от сбоя ORT; сам по себе он не является песочницей против вредоносной ONNX-модели |

Выбор рантайма — внутри разрешённой развилки.

| Критерий | onnx-asr — выбор v1 | sherpa-onnx — альтернатива |
|---|---|---|
| GigaAM v3 e2e_rnnt | Поддержан; собственный пресет под istupakov | Поддержан; совпадает с экспортом Handy/csukuangfj |
| Все 12 моделей | Есть нужные архитектуры: GigaAM, Multilingual, T-one, Vosk, NeMo, два формата Whisper | Для полного каталога остаются непроверенные адаптации T-one/Multilingual и другие экспорты |
| Python/glibc | onnx-asr — pure Python; бинарная граница — ORT. Для ORT 1.23.2 есть cp311 manylinux_2_27/2_28 | Есть manylinux2014-колёса; проверять весь комплект, включая отдельные core-зависимости |
| Собственные `.so` | onnx-asr добавляет 0; план ORT — 3, shim — ещё 1 | Python extension, sherpa core/C API и ORT; точный состав зависит от wheel. Преимущество по числу ELF заранее не доказано |
| RAM/скорость | Проверять на целевом CPU; Python-декодер и preprocessing могут отличаться от Handy | Нативный декодер потенциально выгоднее; локальных сопоставимых замеров пока нет |
| Раскладки | Совпадает с большей частью источников каталога, включая бенчмарченные Whisper | Потребуется изменить источники/размеры, переснять скорость и проверить лицензии экспортов |
| Сопровождение | Узкий RuntimeAdapter; закреплённый wheel и небольшой набор патчей | Возможная замена адаптера и каталога, но не незаметный fallback на тех же файлах |

Поддержка архитектур подтверждается кодом onnx-asr; наличие подходящего тега manylinux подтверждает доступность колеса, **не заменяет запуск на glibc 2.36**. [onnx-asr loader](https://raw.githubusercontent.com/istupakov/onnx-asr/v0.12.0/src/onnx_asr/loader.py), [ORT 1.23.2 на PyPI](https://pypi.org/project/onnxruntime/1.23.2/)

| № | Запись каталога v1 | Репозиторий для onnx-asr | `layout` / обязательные особенности |
|---|---|---|---|
| 1 | GigaAM v3 e2e_rnnt int8 | `istupakov/gigaam-v3-onnx` | `onnx-asr-gigaam-v3`; `v3_e2e_rnnt_{encoder,decoder,joint}.int8.onnx`, vocab/config; 226,4 МБ |
| 2 | GigaAM v3 e2e_ctc int8 | тот же | `onnx-asr-gigaam-v3`; `v3_e2e_ctc.int8.onnx`, соответствующий vocab/config |
| 3 | GigaAM v3 rnnt int8 | тот же | `onnx-asr-gigaam-v3`; `v3_rnnt_*`, обычный словарь; без пунктуации |
| 4 | GigaAM Multilingual 220M | `istupakov/gigaam-multilingual-ctc-onnx` | `onnx-asr-gigaam-multilingual`; 224,8 МБ |
| 5 | T-one | `t-tech/T-one` | `onnx-asr-t-one`; fp32, 144,2 МБ; без kenlm |
| 6 | Vosk ru 0.54 | `alphacep/vosk-model-ru` | `vosk-onnx`; `am-onnx/` + `lang/tokens.txt`; 72,5 МБ |
| 7 | Vosk small ru 0.52 | `alphacep/vosk-model-small-ru` | `vosk-onnx`; `am/` + `lang/tokens.txt`; 26,7 МБ |
| 8 | Whisper large-v3-turbo int8 | `onnx-community/whisper-large-v3-turbo` | `onnx-community-whisper`; encoder, merged decoder, tokenizer/config; 1 089,1 МБ |
| 9 | Whisper small int8 | `onnx-community/whisper-small` | `onnx-community-whisper`; 253,5 МБ; без выдуманных WER/RTFx |
| 10 | Whisper base int8 | `istupakov/whisper-base-onnx` | `onnx-asr-whisper-ort`; beamsearch-граф и tokenizer; 109,1 МБ |
| 11 | GigaAM Multilingual Large | `istupakov/gigaam-multilingual-large-ctc-onnx` | `onnx-asr-gigaam-multilingual`; 591,6 МБ |
| 12 | NeMo FastConformer ru pc CTC | `istupakov/stt_ru_fastconformer_hybrid_large_pc_onnx` | `onnx-asr-nemo`; CTC-граф, vocab/config; 131,6 МБ; CC-BY |

Для №2–3 таблица чисел содержит размеры sherpa-экспортов. До выпуска каталога нужно дополнить источник истины точными байтами выбранных istupakov-наборов; переносить 224,9/229,3 МБ механически нельзя. Файлы этих вариантов присутствуют в закреплённом репозитории. [Экспорт GigaAM](https://huggingface.co/istupakov/gigaam-v3-onnx/tree/322c3b29492673eb7d0b434bfa9dfb8653e34d02)

| Манифест и загрузка | Конкретный контракт |
|---|---|
| Идентичность | Все записи имеют `engine=onnx-asr`; `layout` — закрытый enum из таблицы. ID модели, вариант и precision не выводятся из имени файла |
| Полный состав | `files[]` содержит все реально используемые ONNX, tokenizer, vocab/config и external-data. Вложенность репозитория, включая `onnx/`, сохраняется |
| Ревизии | Полные commit SHA из [catalog-numbers.md](/home/astra/Документы/astra-voice/research/catalog-numbers.md); URL только `/resolve/{sha}/…` |
| Цепочка доверия | Сначала подпись каталога закреплённым ключом, затем схема, ограничения путей и sha256 всех файлов. Git SHA-1 мелких файлов не принимается за sha256 |
| Зеркала | HF → GitHub Releases → corporate при сетевой ошибке/404/429. Все зеркала обязаны отдавать один набор байтов |
| Корпоративный профиль | Корпоративный каталог может содержать только corp-источники; публичного fallback в таком случае нет |
| Докачка | `.part` + состояние `{revision, path, expected_size, sha256, validator}`. Продолжать только при корректном 206/Content-Range; 200 — начать заново; 416 — проверить полноту либо перезапустить |
| Проверка | Размер и sha256 потоково; несовпадение — удалить повреждённую загрузку, показать ошибку. Не назначать фактическую сумму «ожидаемой» |
| Атомарность | Staging на том же файловом разделе → `fsync` файлов/каталога → rename набора → smoke → атомарный `current.json` |
| Откат | Старая ревизия остаётся до следующего успешного запуска новой. Незавершённая транзакция восстанавливается при старте |
| Лимиты | Одно задание загрузки, до 2 файлов одновременно; каталог ≤1 МиБ, ≤64 файлов/модель, ≤2 ГиБ/файл; лимиты явно версионированы |
| Диск | Перед началом минимум `remaining_bytes ×1,2`, с учётом staging и сохраняемой ревизии; ENOSPC обрабатывается и во время записи |
| Кэш | Последний проверенный каталог и ответы API; bounded-кэш обновлений, очистка устаревших `.part`; установленные модели не удаляются как кэш |
| Локальный импорт | Та же проверка. «Своя модель» — только поддержанный layout и явный локальный манифест; никаких скачанных Python-плагинов |
| Защита путей | Запрет абсолютных путей, `..`, symlink/hardlink при импорте, выхода external-data за каталог; отсутствие распаковки произвольных архивов |
| Метрики карточек | WER только по согласованному протоколу; `benchmark_fp32/no_data` сохраняются. Vosk — зарубежные; domestic-фильтр содержит 6 моделей |

| Проверки сети и ревизий | Поведение |
|---|---|
| Общая точка выхода | NetworkService на QNetworkAccessManager; все проверки и загрузки проходят NetworkPolicy |
| По умолчанию | Никаких запросов, в том числе «проверки доступности HF», до разрешённого действия |
| Offline | `policy.offline`, пользовательский offline или `HF_HUB_OFFLINE=1` запрещают новые запросы и отменяют текущие |
| Планировщик | App: jitter 0–10 мин; модели: 0–30 мин; после готовности GUI. Не чаще 24 ч; повторные запуски приложения не сбрасывают backoff |
| Таймаут | Проверка — общий deadline 3 с, включая редиректы; ≤1 повтор внутри бюджета. Для загрузки отдельно connect/inactivity timeout |
| Кэш/лимиты | ETag/304, `Retry-After`, HF RateLimit, GitHub reset; 403/429 не вызывают цикл повторов |
| Экспорт модели | Сравнивать `(repo, branch, observed_sha)` только с предыдущим SHA того же repo/branch |
| Оригинал Сбера | Отдельно отслеживать `ai-sage/GigaAM-v3:e2e_rnnt`. Его SHA не равен SHA `istupakov/gigaam-v3-onnx` |
| Разрешение установки | Новая ветка оригинала — информационный сигнал. Кнопка установки появляется, когда подписанный каталог описал совместимый экспорт с новыми sha256 |
| Близкие модели | Белый список авторов/семейств, ограниченная выдача; баннер без установки до включения в каталог |
| TLS/proxy | Системные CA и `SSL_CERT_FILE`; HTTP(S)_PROXY/NO_PROXY; проверка TLS обязательна. Редиректы проверяются по разрешённым источникам/CDN |
| Runtime | onnx-asr получает только локальный путь и offline resolver; `[hub]`, HF-токены и автоматическое скачивание из воркера отсутствуют |

Обновлятор использует **GPG для выпуска, gpgv для проверки**. Ключ задаётся явным root-owned keyring; пользовательский GPG keyring не участвует. Отзыв и ротация ключей требуют собственной политики: gpgv доверяет всем ключам переданного keyring и сам не решает задачу отзыва. [Документация gpgv](https://www.gnupg.org/documentation/manuals/gnupg/gpgv.html)

| Шаг обновления | Действие |
|---|---|
| 1. Обнаружение | Releases `latest` / corporate latest.json; нормализация одного `v`, SemVer; prerelease не предлагается стабильному каналу |
| 2. Выбор трека | Читать `/etc/digsig/digsig_initramfs.conf` как данные, не shell. `DIGSIG_ELF_MODE≠0`, `zps=on`, `updates=admin` или отсутствие прав → B. Неизвестно → предусмотренный вопрос; «не знаю» → B |
| 3. Загрузка | `.deb`, `SHA256SUMS`, `SHA256SUMS.sig`, инструкция. Текст Release отображается безопасно, без внешних картинок/активного HTML |
| 4. Проверка GUI | Проверить подпись SUMS, затем хеш точного файла; имя, arch, версия. Повреждённый пакет удалить |
| 5A. Авторизация | Один `pkexec /usr/libexec/astra-voice-update-helper …`; собственное action `auth_admin`, без `_keep`, без `rules.d`, без привязки к apt |
| 6A. Root-проверка | Helper повторно читает policy/ЗПС; принимает только локальный комплект. Безопасно копирует в root-staging 0700, затем повторяет подпись/хеш |
| 7A. Установка | Проверяет `Package=astra-voice`, `Architecture=amd64`, повышение версии через `dpkg --compare-versions`; фиксированный `apt-get install --no-remove ./…deb`, без shell и пользовательских аргументов apt |
| 8A. Результат | Проверить установленную версию через dpkg-query; показать «Перезапустить». Перезапуск только кнопкой; текущую запись сначала завершить/отменить |
| 5B. Администратор | Сохранить проверенный комплект, «Открыть папку»; pkexec/apt не запускать |
| Из файла | Тот же verifier и тот же выбор трека; неподписанный deb через GUI не устанавливается |
| Ошибки | Нет агента/отмена → сохранить deb; apt lock → объяснение и повтор; не убивать apt посреди транзакции |
| Сеть apt | Репозитории ОС — отдельная системная стадия, раскрытая в PRIVACY. В offline — `--no-download`; недостающие зависимости передаются администратору |
| После установки | Не смешивать новый код воркера со старым GUI: проверять build/protocol; операции, требующие нового процесса, ждут перезапуска |

Кастомные компоненты соответствуют [макету](/home/astra/Документы/astra-voice/design/mockups/final/index.html), без переноса HTML-стилей буквально в Qt.

| Компоненты | Реализация Qt 5.15 |
|---|---|
| `PillWindow`, `LevelBars` | QQuickWindow, Repeater из 9 Rectangle; обновление уровня около 30 Гц только во время записи; 36 px, ширина 172–320, без elide |
| `ModelCard`, `MetricBar`, `Badge` | Явные состояния ViewModel; 20 состояний карточки; максимум два бейджа; Popup «Как мы считаем» |
| `HotkeyCapture`, `KeyChip` | Нативный X11 backend + QML-представление семи состояний; PT Mono |
| `SegmentedControl`, `PolicySwitch`, `SettingRow` | Controls 2; переключатель — один Tab-stop; строка toggle кликабельна целиком; policy-lock объясняется текстом |
| `StatusBar`, `UpdatePanel`, `OnboardingShell` | Общие контроллеры состояний; 17/14 состояний обновления; переиспользование карточек и микрофонного теста |
| `Theme.qml`, `PillTheme.qml` | Генерация из tokens.json с проверкой типов, единиц, alpha, дробных размеров и Font.Weight; ни одного ручного дубля токенов |
| Иконки и тени | 21 собственный SVG, включая clock. Перекраска через QSvgRenderer/image provider или QtGraphicalEffects; не Qt 6 MultiEffect |
| Шрифты | Выбрать установленный шрифт через QFontDatabase по порядку fallback; строка CSS family-list не считается рабочим fallback Qt 5 |
| Заголовок/меню/уведомления | Заголовок KWin, трей QMenu, уведомления оболочки. Размер внешней рамки измеряется отдельно от QML client area |
| Software rendering | Отдельный прогон; при неподдержанных эффектах — кэшированные растровые тени, без смены принятой геометрии |

2. Поставка — один `astra-voice_<version>_amd64.deb`; системный GUI и NumPy берутся из apt.

| Слой | Состав |
|---|---|
| GUI | `python3`, `python3-pyqt5`, `.qtquick`, `.qtsvg`, `.qtmultimedia`; `qml-module-qtquick2`, `-window2`, `-layouts`, `-controls2`, `qml-module-qtgraphicaleffects` |
| Интеграция | `python3-xlib`, Qt multimedia plugins, `libpulse0`, `pulseaudio-utils`, `xdotool` для аварийного режима; системные polkit/pkexec и gpgv |
| Python из apt | `python3-numpy`, `python3-packaging`, `python3-jsonschema`; зависимости ORT: protobuf, flatbuffers, sympy, humanfriendly и их зависимости |
| Вендоренные wheel-кандидаты | `onnx_asr-0.12.0-py3-none-any.whl`; `onnxruntime-1.23.2-cp311-cp311-manylinux_2_27_x86_64.manylinux_2_28_x86_64.whl`; `coloredlogs-15.0.1-py2.py3-none-any.whl`, поскольку пакет не обнаружен в локальном apt-index |
| Фиксация | Это начальный набор спайка. До v0.1 — точные версии, SHA256, проверка METADATA против apt-версий и проверка известных уязвимостей; после этого никакого `latest` |
| Не поставлять | pip-Qt, pip-NumPy, torch, transformers, GPU runtime, huggingface_hub/hf_xet, sherpa одновременно с onnx-asr |
| Ресурсы | Каталог + подпись, ключ, переводы, SVG, два OGG, лицензированный тестовый WAV; небольшой VAD ONNX как отдельный технический ресурс с хешем/NOTICE |
| ASR-веса | В deb не входят; скачивание/локальный импорт отдельно |
| Python isolation | Root-owned private vendor directory; запуск `python3 -I` через bootstrap. Не изменять системный site-packages и не использовать пользовательские `.pth` |
| Maintainer scripts | Без сети, pip, GUI, изменения домашнего каталога; purge не удаляет данные пользователей |
| Размер/время | Проверить ≤80 МБ без ASR-весов и сборку ≤5 мин; холодную подготовку среды и повторную сборку показывать отдельно |

Минимальные зависимости onnx-asr допускают системный NumPy; hub является отдельной опцией. [pyproject v0.12.0](https://raw.githubusercontent.com/istupakov/onnx-asr/v0.12.0/pyproject.toml)

| Объекты будущей ГОСТ-подписи | Плановое число |
|---|---:|
| onnx-asr и coloredlogs | 0 ELF |
| ORT: `onnxruntime_pybind11_state*.so`, `libonnxruntime.so.*`, `libonnxruntime_providers_shared.so` | Ожидается 3 |
| `libastra-isoc23-compat.so` | 1 |
| Python root-helper, GUI-код | 0 ELF |
| **Бюджет собственного комплекта** | **4 `.so`; 3, если shim не требуется** |

Это **плановый инвентарь, не результат распаковки wheel**. CI считает все уникальные ELF по magic, включая `.so.*` и транзитивные библиотеки; лишний ELF останавливает сборку до обновления SBOM. Системные apt-библиотеки учитываются отдельно.

Шим загружается только в ASR-процессе, до ORT, из root-owned пути. Не использовать `/etc/ld.so.preload`, не передавать LD_PRELOAD в pkexec. Проверять реальные необходимые символы и семантику C23; обёртки `strto*` не являются общей заменой glibc 2.38. [Факт из Handy](/home/astra/Документы/handy-gigaam/docs/TROUBLESHOOTING.md:5)

| Структура репозитория | Содержание |
|---|---|
| `src/astra_voice/app/` | Контроллеры, bootstrap, lifecycle |
| `src/astra_voice/platform/` | X11, clipboard, audio, tray, theme, autostart |
| `src/astra_voice/models/` | Схема, пресеты, registry, downloader, revisions |
| `src/astra_voice/worker/` | IPC, RuntimeAdapter, VAD, segmentation, cancel |
| `src/astra_voice/updates/` | Release client, verifier, track resolver |
| `src/astra_voice/ui/` | QML screens/components, ViewModels, i18n |
| `helper/`, `native/` | Минимальный root-helper; C23 shim |
| `catalog/`, `resources/` | Подписанные данные, ключи, тестовое аудио, VAD, иконки/звуки |
| `vendor/locks/`, `vendor/patches/` | Wheel-lock с SHA256, provenance и патчи onnx-asr |
| `debian/` | control/rules, desktop, polkit action, policy.conf |
| `tools/` | build-deb, generate-theme, catalog, validate, benchmark, elf-audit |
| `tests/` | unit, integration, qml, packaging, security, fixtures |
| `docs/`, `.github/workflows/` | INSTALL-ADMIN, PRIVACY, threat model, выпуск/CI |

| Сборка и CI GitHub Actions | Проверки/артефакты |
|---|---|
| **Одна команда** | `./tools/build-deb --locked`; фиксированный Debian 12 build image, генерация темы/переводов, применение патчей, staging wheel, dpkg-buildpackage |
| Offline-сборка | `./tools/build-deb --locked --offline` из подготовленного wheelhouse; отсутствие сети внутри сборки |
| PR pipeline | Ruff, unit, schema/preset checks, генератор токенов, Qt 5.15 QML smoke, Xvfb, HTTP-фикстуры |
| ABI pipeline | ELF inventory, `readelf`, аудит GLIBC/GLIBCXX и DT_NEEDED; импорт ORT и реальный inference, а не один импорт |
| Package pipeline | lintian; install → upgrade → remove → purge в чистой среде; сеть maintainer scripts запрещена |
| Model pipeline | На релиз/изменение runtime или пресета — smoke всех 12 закреплённых наборов. Кэш только по хешам; веса не скачиваются на каждый PR |
| Reproducibility | `SOURCE_DATE_EPOCH`, фиксированные входы, две сборки и сравнение; объяснимые отличия не объявляются воспроизводимостью |
| SBOM | SPDX/CycloneDX: apt build/runtime inventory, wheels, native libraries, patches, VAD, ресурсы; отдельно BOM моделей с ревизиями и лицензиями |
| Выпуск | Tag → проверки → deb + corresponding source + SBOM + ELF inventory + SHA256SUMS → GPG-подпись → GitHub Release |
| Защита CI | Actions по commit SHA; минимальные permissions; секрет подписи недоступен PR/fork; ключ вне обычного build job |
| Только ALSE/реальная машина | KWin/Fly, SNI, focus, Klipper, polkit-агент, аудиокарта и WirePlumber после загрузки, Astra apt-источники, реальные RAM/RTFx |
| v1.1 | ГОСТ-подпись полного ELF-инвентаря и проверка на ВМ с включённой ЗПС |

3. Вехи идут по зависимостям: сначала доказать платформу и воркер, затем каталог и привилегированное обновление.

Команды `tools/validate` и `tools/benchmark` ниже обозначают CLI, который следует создать вместе с соответствующей функцией; сейчас таких успешных прогонов не заявляю.

| Срок / веха | Зависит от | Done-критерий | Команды валидации |
|---|---|---|---|
| **09–11.09: спайк пилюли** | apt QtQuick | SNI + прозрачная пилюля; без изменения активного окна; × работает; рабочая область/масштаб; темы до первой отрисовки | `./tools/validate x11-pill --matrix`; `xprop -root _NET_ACTIVE_WINDOW`; `xprop -id <pill-xid>` |
| **09–12.09: спайк ABI/runtime** | Закреплённые wheel-кандидаты | ORT запускается на glibc 2.36, проверен shim; GigaAM e2e_rnnt распознаёт; проверены входы/выходы всех семейств | `./tools/validate runtime --glibc 2.36`; `./tools/elf-audit --strict` |
| **10–13.09: RAM/скорость/отмена** | Runtime | GigaAM warm p95 ≤0,5 с; worker ≤450 МБ; измерен GUI; cancel ≤1 с; определён пик смены модели | `./tools/benchmark --model gigaam-v3-e2e-rnnt-int8 --runs 50 --threads 4`; `./tools/validate worker-cancel` |
| **10–13.09: polkit из GUI** | Тестовый deb/helper | На отдельном стенде ровно один системный диалог; GUI не блокируется; cancel, отсутствие агента, bad signature; root повторяет проверки | `./tools/validate updater --polkit --fixture-upgrade`; `dpkg-query -W astra-voice` |
| **14–20.09: основной тракт** | Все ранние спайки | PTT/toggle → PCM → VAD → ASR → вставка; отмена, очередь, терминальные комбинации, тишина, worker crash | `python3 -m pytest tests/unit tests/integration`; `./tools/validate dictation --virtual-mic` |
| **21–30.09: v0.1 «Диктовка работает»** | Основной тракт | Подписанный deb одной командой; дефолтная модель скачивается/импортируется; пилюля/трей/минимальные настройки; S2–S5, S12, S14; метрики Ц2/Ц7 | `./tools/build-deb --locked`; `./tools/validate release --version 0.1.0`; `./tools/validate e2e --suite v0.1` |
| **01–07.10: каталог и ревизии** | v0.1, verified downloader | 12 пресетов в схеме; обязательные 9 готовы к smoke; T-one и Could проходят тот же путь; подпись/докачка/откат/ветки проверены | `./tools/validate catalog --all`; `./tools/validate downloads --faults`; `./tools/validate model-updates` |
| **05–11.10: обновления/автозапуск** | Polkit-spike, NetworkPolicy | Трек A, безопасная ветка B, import deb, ETag/backoff; XDG и single-instance; нет запросов по умолчанию | `./tools/validate updater --all`; `./tools/validate autostart`; `./tools/validate privacy --network-denied` |
| **12–15.10: v0.2** | Каталог, updater, autostart | Полный onboarding ≤5 мин при 10 Мбит/с и ≤2 мин из файла; ≥3 модели измерены; полный экран настроек, ru/en, Klipper; S1/S6–S9/S11/S13/S15 | `./tools/validate e2e --suite v0.2`; `./tools/benchmark --catalog --minimum-models 3`; `./tools/benchmark wer --model whisper-small-int8 --dataset russian-librispeech-test` |
| **16–23.10: корпоративный режим** | v0.2 | Policy с блокировками, corp-only каталог, offline import, domestic enforcement, полный S10; диагностика и документы администратора | `./tools/validate policy --matrix`; `./tools/validate corporate --no-public-egress`; `./tools/validate diagnostics --redaction` |
| **24–31.10: v1.0** | Корпоративный режим | Все 12 моделей smoke-tested; все применимые S1–S16; T3; release upgrade; длительный прогон без P0/P1; документы и атрибуции; G5 | `./tools/validate release --version 1.0.0`; `./tools/validate e2e --suite all`; `./tools/validate soak --hours 8` |

| Протокол измерений | Правило |
|---|---|
| RAM | Новый процесс на модель/ревизию/настройку потоков; VmHWM после первого inference, дополнительно PSS. HWM предыдущей модели не наследуется |
| Invalidation | Ключ: model ID, revision, quantization, runtime/build, threads, CPU, preprocessing. Смена любого значимого поля сбрасывает локальную достоверность |
| Скорость | Отдельные времена preprocessing, inference, queue, release→text-ready, paste. RTFx = длительность аудио / время обработки; p95 пользовательской задержки включает вставку |
| Выборка | Первый прогон холодный; RTFx — медиана ≥5 тёплых; дефолтная задержка — ≥50 шестисекундных фраз |
| Машина | Core Ultra 7 255U; фиксировать питание, профиль CPU, фоновые процессы и температуру. Проверить 1/2/4 потока без автоматической смены заданного дефолта |
| Качество | WER только на согласованном Russian LibriSpeech test; версия нормализатора фиксируется. Для Whisper small до замера остаётся no_data |
| Виртуальный микрофон | Harness создаёт `module-null-sink` и `module-remap-source`, явно выбирает источник; ждёт `CaptureReady`, затем запускает `paplay --device=av_test …wav`; удаляет только созданные модули |
| Настоящая вставка | E2E-приёмник подтверждает изменение текста; событие отправки Ctrl+V само по себе не доказывает успешную вставку |

4. Риски, альтернативы и границы безопасности должны быть проверяемыми условиями приёмки.

| Риск | Основное решение / альтернатива | Обратимость |
|---|---|---|
| onnx-asr не достигает скорости/RAM | Сначала профилировать preprocessing, декодер, ORT threads/arenas. Затем сравнить sherpa на том же аудио; менять runtime только вместе с пресетами и замерами | RuntimeAdapter и IPC сохраняются; модели потребуется переустановить в другом layout |
| Полный каталог не проходит выбранный ORT | Зафиксировать исправленный совместимый wheel; не добавлять скрыто второй runtime | Замена wheel-lock и повтор всех 12 smoke |
| C23 shim скрывает более широкий ABI-разрыв | Аудит символов; совместимый wheel либо собственная воспроизводимая сборка ORT под базовую glibc | Локальная замена backend-комплекта |
| Cancel не укладывается в 1 с | RunOptions + точки отмены; проверить каждый декодер. Kill/reload — только аварийное восстановление | Патч ограничен adapter/vendor; критерий не ослабляется незаметно |
| Две модели вызывают OOM | Проверка MemAvailable и пика кандидата; при нехватке сохранить старую и отказать в переключении с объяснением | Данные/активная ревизия не меняются |
| Пилюля теряет верхний слой | Повторное применение EWMH hints по событиям; при необходимости X11 bypass-вариант после проверки ×/focus | Один platform adapter |
| QtQuick software backend не рисует эффекты | Кэшированные тени/иконки; анимации только видимых элементов | Не меняет контроллеры и токены |
| WirePlumber restart не помогает | Отличать mute/нет устройства/профиль/ошибку открытия. Предложить диагностику; не менять fly-dm, linger и системные службы автоматически | Только пользовательская служба по кнопке |
| Clipboard/Klipper гонки | Проверка владельца и поколения; настраиваемая задержка; «оставлять текст»/«только буфер» | Настройка, без смены ASR |
| HF SHA постоянно даёт ложное обновление | Раздельные координаты upstream/export; установка только подписанного набора | Миграция update-cache |
| Подмена deb между GUI и helper | Проверка после копирования в root-staging; helper не доверяет флагу «GUI проверил» | Изолированный updater |
| Обрыв apt | Сохранять журнал и системный результат; не обещать транзакционный rollback deb и не запускать автоматический downgrade | Восстановление штатным apt администратором |
| Отозванный/скомпрометированный ключ | Явные fingerprints, версия доверенного набора ключей; ротация через доверенный выпуск/администратора | Отдельный keyring; URL не назначает доверие |
| RAM-only ошибочно трактуется как защита от swap | Нет аудиофайлов; core dumps отключены, audio mappings исключены из dumps. Строгая защита от swap/hibernation зависит от политики ОС | Не требует вмешательства приложения в настройки ОС |
| «Отечественные модели» принимают за сертификацию | Фильтр по принятому юрлицу; NOTICE и документ влияния на среду. Допуск в систему — процедура администратора | Состав каталога управляется подписанным манифестом |

| Уровень тестов | Обязательное покрытие |
|---|---|
| **Unit** | Автоматы состояний, очередь/cancel, миграции и policy, SemVer/Debian version, вычисление метрик, схема каталога, пути, подписи, Range/cache/backoff |
| **Integration** | Реальный worker и ORT; виртуальный PipeWire-микрофон + paplay; HTTP fault server; D-Bus stubs; Xvfb clipboard; crash и заполнение диска |
| **Packaging/security** | Чистый install/upgrade/remove/purge; полный ELF-инвентарь; отсутствие сети в postinst; TOCTOU/symlink/неверный ключ/чужой пакет; T2 на изменениях updater |
| **E2E на ALSE** | KDE и Fly; реальные хоткеи/фокус/трей/polkit/Klipper; Kate/Firefox/LibreOffice/Konsole/fly-term/xterm; перезагрузка, USB, сеть через proxy/CA |
| **Performance/soak** | GUI/RSS/PSS/RTFx; 50 диктовок; 8 часов работы; циклы отмены и смены модели; отсутствие роста FD/памяти |
| **v1.1 отдельно** | Установка и диктовка с настоящей ЗПС и подписанным комплектом; имитация `DIGSIG_ELF_MODE` в v1 проверяет только маршрутизацию UI |

| Инвариант | Где обеспечивается |
|---|---|
| Аудио и распознанный текст не отправляются в сеть и не попадают в логи/диагностику | NetworkService не принимает эти типы данных; тесты с контрольными маркерами |
| Запись начинается только по действию пользователя и имеет видимый индикатор | Controller; при потере всех видимых индикаторов запись останавливается |
| Worker не скачивает модели, не выполняет repo-код и не регистрирует произвольные custom-op `.so` | RuntimeAdapter, локальный resolver, закрытые layouts |
| Неподписанный каталог и непроверенные файлы не становятся активными | Registry/Activator, атомарные транзакции |
| Policy применяется к операциям, а не только к состоянию кнопок | Общий EffectivePolicy для GUI, CLI и updater |
| Root-helper не принимает URL, shell-команду, произвольный keyring или параметры apt | Узкий протокол helper; root-owned конфигурация |
| ЗПС и admin-track нельзя обойти через «Обновить из файла» | Повторная проверка трека в GUI и helper |
| Отмена/крах/смена поколения не приводят к поздней вставке | ID сессии и поколения, единая очередь PasteService |
| Вставка не синтезирует Enter и не начинается без проверенного окна назначения | PasteService; при невозможности назначения — только буфер |
| Установленная рабочая ревизия не удаляется до подтверждения новой | ModelRegistry и журнал транзакции |
| Системные СЗИ, fly-dm, группы пользователя и чужой автозапуск не меняются | Ограниченная область записи пакета/helper |
| В v1 нет обещания готовности неподписанных ELF к ЗПС | INSTALL-ADMIN и трек B; ГОСТ-подпись и фактическая приёмка — v1.1 |
