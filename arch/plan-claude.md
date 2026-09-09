# Astra Voice — архитектурный план (Архитектор #1, Claude Fable 5.1)

> Фаза 3 · P11 · 2026-09-09 · независимый план; синтез с планом #2 — за Юркой.
> Входы: `PRD.md` 0.3, `decisions/log.md`, `design/spec.md` 0.2, `design/tokens.md` §9, `design/flows.md`,
> `design/mockups/final/index.html` (список кастомного QML, 21 глиф), `research/*` (summary, tech-platform §A–D,
> tech-models §A4–A5/B–C, tech-codex блоки 2–3/5, catalog-numbers, legal §3–4), `handy-gigaam/docs/TROUBLESHOOTING.md`.
> Среда сверена read-only на машине заказчика (см. §12 «Факты среды»). Рамки из журнала решений не пересматриваются.

## 0. Итог

Два процесса на Python 3.11: **GUI-процесс** (PyQt5 + QtQuick Controls 2, трей `QSystemTrayIcon`, хоткей `python3-xlib`,
вставка через `QClipboard` + XTest, сеть, обновлятор) и **воркер** (захват звука через `libpulse-simple`, VAD, движок
`onnx-asr` поверх `onnxruntime`; модель живёт в его памяти). Связь — `socketpair` с JSON-строками; крах воркера =
перезапуск дочернего процесса, GUI не страдает. Третий, одноразовый, процесс — **root-помощник** через `pkexec`
(одно polkit-действие, повторная проверка подписи в root-staging, `apt-get install --no-remove ./deb`).
Рантайм v1 — **`onnx-asr`** (все 12 позиций каталога одним API, цифры протокола сняты именно на нём, 3 собственных `.so`
против 4–6 у sherpa-onnx; шим `__isoc23_*` для PyPI-колёс ORT **не нужен** — проверено на машине).
Подпись релизов и манифеста — **GPG (Ed25519), проверка `gpgv` с закреплённым keyring** (есть на любой ALSE, нужен и админу трека B).
Один `.deb`: всё из apt Astra + вендорные `onnxruntime` и `onnx-asr`; сборка одной командой в контейнере Debian 12
(та же glibc 2.36 / Python 3.11.2 / PyQt5 5.15.9, `/etc/debian_version` = 12.0).
Порядок сборки — по зависимостям: спайки → скелет+упаковка → воркер+движок → звук → хоткей/пилюля/трей/вставка (= v0.1) →
каталог/сеть/обновлятор/автозапуск (= v0.2) → policy/корп. каталог/трек B/комплаенс (= v1.0).

## 1. Ключевые решения (что · почему · обратимость)

| # | Решение | Почему | Обратимость |
|---|---|---|---|
| Р1 | Два долгоживущих процесса (GUI ∥ воркер), звук **в воркере** | PRD F4.1 (крах ORT не роняет GUI); аудио не пересекает границу процесса — в GUI летят только уровни (~30/с) и текст; ORT и `libpulse` не трогают Qt-loop | IPC-протокол не привязан к тому, где стоит захват: перенос захвата в GUI = добавить сообщение `audio.frames` (день работы) |
| Р2 | Рантайм **`onnx-asr`** + `onnxruntime` (PyPI, CPU) | §4: каталог 12/12, протокол цифр, 3 `.so`, MIT, glibc ok без шима | Интерфейс `Engine` + поле `engine` в манифесте; `SherpaEngine` — второй адаптер, переключение = новые строки манифеста (PRD §1.4 это и предусматривает) |
| Р3 | Хоткей — `python3-xlib` (`XGrabKey` на root, 8 масок Lock/Num/Scroll), события через `QSocketNotifier` на fd Display | Единственный apt-путь с push-to-talk (KGlobalAccel даёт только «нажатие»); без потоков и без root | `evdev` — «надёжный режим» P2 за тем же интерфейсом `HotkeyBackend` |
| Р4 | Пилюля — отдельное QML `Window` (Tool·Frameless·StaysOnTop·DoesNotAcceptFocus) + EWMH-хинты руками (`_NET_WM_STATE_ABOVE/SKIP_TASKBAR/SKIP_PAGER`, `_NET_WM_USER_TIME=0`, тип `NOTIFICATION`) + повторная проверка перекрытия | Спека §8.1, урок Astra Cowork (KWin теряет StaysOnTop); в Fly-сессии WM другой (fly-wm) — хинты EWMH там надёжнее флагов Qt | Запасной режим «совместимость индикатора» = override-redirect (`BypassWindowManagerHint`), тумблер в «Продвинутых» |
| Р5 | Трей — `QSystemTrayIcon` (SNI через Qt) с иконками **по имени** из hicolor; меню — `QMenu` (под SNI рисует Plasma через DBusMenu) | Иконка по имени даёт перекраску `currentColor` под тему панели (контракт спеки §9.1); меню стилизовать нельзя — это Plasma | Пиксмап вместо имени — одна строка; своё SNI по D-Bus — не нужно |
| Р6 | Захват звука — `libpulse-simple` через `ctypes` (16 кГц mono s16le, ресемплинг на сервере), устройство открывается по хоткею | Ноль вендорных `.so`, работает и с PipeWire (pulse-протокол на машине заказчика), и с PulseAudio; список устройств — `pactl list short sources` | `QAudioInput` (apt) или `sounddevice`+PortAudio — за интерфейсом `AudioSource`; тест-источник из wav — тот же интерфейс |
| Р7 | Подпись: GPG Ed25519, `gpgv --keyring /usr/share/astra-voice/keys/release.gpg` и в GUI, и в root-помощнике; корп. ключ — `policy.conf: catalog_pubkey=` (keyring) | `gpgv` есть на каждой ALSE (2.2.40), `minisign` в apt Astra нет — админ трека B не смог бы проверить; `gpgv` без trust-db/агента детерминирован | Интерфейс `Verifier`; minisign через `python3-nacl` (в apt) — 60 строк |
| Р8 | Root-помощник — **Python-скрипт** `/usr/libexec/astra-voice/update-helper` (`python3 -I`), не C-ELF | Ноль собственных ELF под ЗПС; интерпретатор системный (подписан Астрой); логика = копия в root-staging + `gpgv` + sha256 + `dpkg-deb --field` + `dpkg --compare-versions` + `apt-get` | Замена на C-ELF — тот же контракт argv/JSON-stdout |
| Р9 | Тема — парсер `~/.config/kdeglobals` до `engine.load` + `QFileSystemWatcher`; `Theme.qml`/`PillTheme.qml` генерируются из `tokens.json` скриптом, сгенерированный файл коммитится, CI проверяет «чистую» регенерацию | tokens.md §9, урок «палитра Qt врёт»; правка токенов не расходится с кодом | — |
| Р10 | Сеть — `python3-requests` из apt (Range/206, прокси из env, системный CA), без `huggingface_hub`/`httpx` | Меньше вендоринга; все 4 сетевых действия PRD F14 — обычный HTTPS GET | `httpx` 0.23 в apt — тот же `HttpClient` |
| Р11 | Один `.deb`, наш код в `/usr/lib/python3/dist-packages/astra_voice`, вендор в `/usr/lib/astra-voice/vendor` (в `sys.path` добавляет только лаунчер) | Debian-политика для своего кода; вендор не засоряет системный `dist-packages` и не конфликтует с системным numpy | — |
| Р12 | Настройки — `settings.json` + `policy.conf` (INI) → «эффективная конфигурация» с множеством заблокированных ключей; QML видит один `SettingsBridge` | PRD F11.4/F14.3, спека «Задано администратором» | — |

## 2. Процессная модель

```
┌─ astra-voice (GUI, /usr/bin/astra-voice → python3 -m astra_voice) ───────────────────────────────┐
│ main-thread: QApplication + QQmlApplicationEngine · окно 900×620 · пилюля (Window) · QSystemTrayIcon │
│              hotkey (QSocketNotifier на fd Xlib) · QClipboard/XTest · QLocalServer (single-instance) │
│ NetPool (QThreadPool): проверки HF/GitHub (≤3 с), загрузка моделей/deb (Range, sha256, отмена)       │
│ WorkerSupervisor: spawn/restart, socketpair fd → QSocketNotifier, корреляция id запросов            │
└───────────┬───────────────────────────────────────────────┬──────────────────────────────────────┘
            │ socketpair (JSON-lines, UTF-8, len-prefix)     │ QProcess: pkexec (одноразово, трек A)
┌───────────▼──────────────────────────────┐   ┌────────────▼──────────────────────────────────────┐
│ astra-voice-worker (python3 -m astra_voice.worker)  │   │ update-helper (root, python3 -I, polkit auth_admin)│
│ ipc-loop · AudioThread (libpulse read 20 мс,        │   │ argv: install <путь.deb> · копия в root-staging ·  │
│  ring-buffer, уровень, тишина, лимит) · InferThread │   │ gpgv+sha256+dpkg-deb field+compare-versions ·      │
│  (onnx-asr, VAD>20 с, замер VmHWM/PSS)              │   │ apt-get install --no-remove -y ./x.deb · JSON-stdout│
└─────────────────────────────────────────┘   └─────────────────────────────────────────────────┘
```

- **Почему воркер — дочерний процесс, а не поток:** ORT-краш (SIGSEGV/abort) и OOM убивают процесс; GIL не мешает
  (ORT и `pa_simple_read` его отпускают), но изоляция памяти (415 МБ + арена) и «Отмена» одним `kill`/сообщением важнее.
- **Перезапуск:** супервизор поднимает воркер ≤ 3 раз за 10 мин (PRD F4.1); при третьем провале — трей `error`, пилюля
  `error` с текстом «Распознавание перезапущено»; модель после рестарта грузится заново в фоне.
- **IPC-протокол (v1, версия в `hello`):** запросы GUI→воркер: `hello`, `model.load{dir,layout,threads}`, `model.unload`,
  `record.start{device,limit_s,silence_db,insert}`, `record.stop`, `record.cancel`, `transcribe.file{path}` (смоук/тест),
  `measure`, `ping`; события воркер→GUI: `state{idle|recording|processing}`, `level{rms,peak}` (≤ 30/с), `silent`,
  `limit`, `result{text,audio_ms,t_ms,cold}`, `empty`, `error{code,detail}`, `model.loaded{id,revision,load_ms}`,
  `measure{peak_rss_mb,pss_mb}`. Аудио **никогда** не сериализуется и не пишется на диск (§9 инварианты).
- **Бюджет задержки «отпустил → текст» (Ц2, p95 ≤ 500 мс):** `record.stop` <1 мс → хвостовая обрезка ≤ 5 мс →
  инференс 6 с при RTFx ≥ 20 ≈ 300 мс → `result` → задержка вставки 50 мс → `Ctrl+V` через XTest ≈ 5 мс. Итого ≈ 360–420 мс;
  запас мал → спайк S3 обязан замерить RTFx `onnx-asr` на машине; запасной ход — дефолт `e2e_ctc` (+25 %) и потоки ORT.

## 3. Компоненты (модуль · ответственность · вход/выход и состояние · зависит от · тест)

| Модуль (`src/astra_voice/…`) | Ответственность | Состояние / контракт | Зависит от | Тест |
|---|---|---|---|---|
| `app.py` | Бутстрап: argv (`--hidden`, `--show`), single-instance (`QLocalServer` в `$XDG_RUNTIME_DIR`), порядок старта: policy → settings → theme → QML → трей (ретрай ≤ 30 с) → воркер → отложенные проверки | Ничего не хранит | все ниже | e2e: S1-A7, S11-A4; unit: разбор argv |
| `core/paths.py`, `core/version.py`, `core/logging.py` | XDG-пути; `__version__` из `debian/changelog` на сборке; `RotatingFileHandler` 5×1 МБ без аудио/текста | файлы в `~/.local/state/astra-voice/` | — | unit: фильтр логов не пропускает поле `text` |
| `core/settings.py` | `settings.json`: `schema_version`, миграции вперёд, атомарная запись (tmp+fsync+replace), `.bak` при порче, сброс | JSON-документ, сигнал `changed(key)` | paths | unit: миграции v1→vN, битый файл → `.bak` + дефолты |
| `core/policy.py` | `/etc/astra-voice/policy.conf` (INI): `profile/offline/catalog_url/updates_url/updates/zps/domestic_only/autostart_default/catalog_pubkey`; `profile=secure` ⇒ производные | «эффективная конфигурация» + `locked_keys` | settings | unit: таблица истинности secure/offline/updates |
| `core/theme.py` + `qml/Theme.qml`, `qml/PillTheme.qml` | Тема из `kdeglobals` (секция `[General] ColorScheme` + яркость `[Colors:Window] BackgroundNormal`) до `engine.load`; `QFileSystemWatcher` (файл переписывается → re-add) → `Theme.dark` | синглтон QML, свойство `dark` | `scripts/gen_theme.py` ← `tokens.json` | unit: парсер на фикстурах Breeze/BreezeDark/Fly; интеграция: смена файла → сигнал ≤ 2 с (S13) |
| `platform/x11.py` | Своё `Xlib.display.Display`: `XGrabKey/XUngrabKey` (маски × Lock/Num/Scroll), обработчик `BadAccess`, временный grab `Escape` на время записи, `XGrabKeyboard` для поля захвата, XTest-комбинации, `_NET_ACTIVE_WINDOW`/`WM_CLASS`/геометрия, EWMH-свойства окна пилюли, `_NET_CLIENT_LIST_STACKING` (перекрытие) | без состояния, кроме Display | `python3-xlib` (apt 0.33) | интеграция под Xvfb: grab/ungrab, BadAccess при двойном grab, XTest доставляет событие в тестовое окно |
| `platform/hotkey.py` | Автомат PTT/toggle: `Idle→Recording→Processing`, фильтр автоповтора (KeyRelease+KeyPress с одним `time`), порог 0,3 с, лимит 120 с (таймер), конфликт с KDE (парсер `kglobalshortcutsrc`: секция `[app.desktop]`, `key=combo\tcombo,default,описание`), `duplicate`, `not-grabbed` | `HotkeyBackend` (X11 / evdev P2); сигналы `pressed/released/cancelled` | x11, settings | unit: автомат на синтетических событиях; парсер конфликтов на реальном файле (Alt+F2 → «Открыть строку поиска и запуск») |
| `platform/paste.py` | Сохранить все форматы `QMimeData` → положить только `text/plain` (+ `x-kde-passwordManagerHint=secret`, P1) → задержка 50 мс → комбинация (по `WM_CLASS`: терминалы → `Ctrl+Shift+V`) → 100 мс → восстановить; режим «только буфер»; «последний текст» только в памяти | `PasteMethod` = ctrl_v/ctrl_shift_v/shift_insert/clipboard_only | x11, QClipboard | интеграция Xvfb + тестовое Qt-окно с `TextEdit`; e2e S4 (Kate, Konsole, LibreOffice, Klipper) |
| `ui/pill.py` + `qml/Pill.qml` | Контроллер оверлея: 12 состояний спеки §8.4, ширина 172→320 без elide, 9 столбиков `h=max(3,round(l*20))`, позиция по `availableGeometry` экрана активного окна (48 px), таймеры 500/1000/1200/800/3000 мс, показ ≤ 100 мс, re-assert «наверх» + проверка перекрытия | `state`, `levels[9]`, `text` | x11, theme | интеграция: скриншот состояний под Xvfb (DesignReviewer); e2e: фокус остаётся в Kate (S12-A3), Alt+Tab не содержит пилюлю |
| `ui/tray.py` | `QSystemTrayIcon`: 6 состояний иконок по имени (`astra-voice-tray-{idle,listening,processing,done,error,nokey}`), подсказки, ретрай регистрации ≤ 30 с (watcher на шине), меню по спеке §9.2 (состав/неактивность), клик — окно; баннер «трей недоступен» | `state` | QtWidgets, notify | интеграция: `QSystemTrayIcon.isSystemTrayAvailable()` под Xvfb (false-ветка); e2e на Plasma и во Fly-сессии |
| `ui/window.py` + `qml/{Main,Sidebar,SettingRow,ModelCard,Onboarding*,Dialogs}.qml` | Окно 900×620 (min), 6 разделов + «Отладка» (Ctrl+Shift+D), строка-статус (5+17 состояний), панель обновления (14), карточка модели (20, state-машина в одном компоненте), поле захвата (7), сегмент, тумблер с замком, Popup «Как мы считаем», клавиатура У13 | мосты `SettingsBridge/ModelsBridge/UpdatesBridge/AudioBridge/AboutBridge` (`QObject`, `pyqtProperty`+`notify`) | theme, все мосты | интеграция: каждый QML-файл создаётся без warnings (`QT_QPA_PLATFORM=offscreen`), скриншоты 24 экранов × 2 темы для DesignReviewer |
| `ui/notify.py` | `org.freedesktop.Notifications` через `QtDBus` (кнопки-действия, `ActionInvoked`), таймаут 1 с на ответ службы → запасной баннер в окне (Fly может убить владельца шины) | — | QtDBus | интеграция с тестовым сервером уведомлений (python-dbus) |
| `worker/supervisor.py` | `subprocess.Popen([sys.executable,'-m','astra_voice.worker'], pass_fds=[sock])`, len-prefixed JSON, `QSocketNotifier`, таймауты на запрос, политика рестартов 3/10 мин, `SIGTERM` при выходе | очередь запросов (≤ 1 запись в очереди, PRD F2.9) | — | unit с фейковым воркером (эхо-скрипт), убийство процесса → рестарт |
| `worker/__main__.py`, `worker/ipc.py` | Цикл IPC, диспетчер команд, аккуратное завершение | автомат `idle/recording/processing` | audio, engine | unit: автомат с `FakeAudioSource` + `FakeEngine` |
| `worker/audio.py` | `AudioSource`: `PulseSimpleSource` (ctypes `libpulse-simple.so.0`, `pa_simple_new(PA_STREAM_RECORD, spec s16le/16k/1, device)`; поток чтения 20 мс; кольцевой буфер в ОЗУ; RMS/пик; «тишина» = пик < −60 дБ за 2 с; лимит; ретраи открытия 3×300 мс; ошибки `ENODEV/EBUSY`), `WavFileSource` для тестов; список устройств — `pactl list short sources` | буфер в памяти, очищается после результата/отмены | `libpulse0` | unit на `WavFileSource`; интеграция на машине: виртуальный источник PipeWire + `paplay` |
| `worker/vad.py` | Silero VAD ONNX (MIT, ~2 МБ, в пакете): сегментация записей > 20 с по паузам без резки слов (окно 24 с GigaAM), обрезка хвостовой тишины; для ≤ 20 с — не применяется | — | onnxruntime | unit: синтетический wav с паузами → границы |
| `worker/engine.py` | `Engine`-протокол: `load(dir, layout, threads)`, `transcribe(np.float32 16k) -> text`, `unload`; `OnnxAsrEngine` (раскладки §4.2), заглушка `SherpaEngine`; потоки ORT = `min(4, физ. ядер)`; постобработка только strip + опции «пробел в конце»/«заглавная» | модель в памяти воркера; выгрузка по простою (таймер в GUI → `model.unload`) | onnx-asr, onnxruntime | интеграция: тестовый wav (6 с, «проверка связи») → ожидаемые слова на трёх GigaAM; смоук каждой раскладки в CI (кеш моделей) |
| `worker/measure.py` | `VmHWM` и `Pss` из `/proc/self/{status,smaps_rollup}` после первого прогона; отдаёт в GUI → `measurements.json`; сброс при смене ревизии/потоков | — | — | unit на фикстурах `/proc` |
| `models/catalog.py` | Манифест: встроенный (`/usr/share/astra-voice/catalog.json` + `.sig`) → последний скачанный (`~/.local/state/.../catalog.json`); `jsonschema` (`catalog.schema.json`), `manifest_version`, `gpgv`-подпись (наш keyring или `catalog_pubkey`), слияние с локальными замерами и статусами; фильтры «язык», «только отечественные» | `CatalogModel` (`QAbstractListModel`) для QML | verify, store | unit: схема, подпись (тестовый ключ), неизвестные поля игнорируются, старший `manifest_version` → отказ |
| `models/store.py` | `~/.local/share/astra-voice/models/<id>/<revision>/` + `current.json`; staging `<rev>.partial/`; удаление, размер на диске, `shutil.disk_usage` (×1,2), `MemTotal` vs `min_ram_mb` | файлы на диске | paths | unit на tmpdir |
| `models/downloader.py` | Загрузка файлов манифеста: `.part`, `Range: bytes=N-` только при 206 с корректным `Content-Range` (200 → сначала), инкрементальный sha256, `fsync`, `os.replace`, источники `hf → github_release → corp` (429/404/сеть → следующий), скорость/ETA, отмена, очередь = 1, пауза «нет места» | сигналы прогресса в `ModelsBridge` | net.http, store | unit: локальный HTTP-сервер (206/200/обрыв/подмена байтов) |
| `models/installer.py` | Установка из файла/папки (те же sha256), смоук через воркер (`transcribe.file` вшитого wav, `expect_any`), атомарная смена `current.json`, откат при провале, «Повреждена — переустановить», выбор активной («Переключаю…» — старая модель работает до `model.loaded`) | — | store, supervisor | интеграция: подмена байта в encoder → смоук провален → откат |
| `net/http.py` | `requests.Session`: UA `astra-voice/X.Y.Z (+repo)`, connect 2 с / total 3 с для проверок, прокси из env, CA из системного хранилища (+`SSL_CERT_FILE`), TLS не отключается, `ETag`/`If-None-Match`, `Retry-After`/`RateLimit` → backoff, кэш `update-cache.json`, гейт «сеть разрешена?» (тумблеры/офлайн/policy/`HF_HUB_OFFLINE`) | кэш на диске | requests | unit: фейковый сервер 200/304/429/407/таймаут; гейт — таблица истинности |
| `net/hf.py`, `net/github.py` | HF: `GET /api/models/{repo}/refs` → `targetCommit` ветки-варианта (`update_watch.branch`), `HEAD …/resolve/{rev}/file` → `X-Repo-Commit`; поиск по `author` (P2). GitHub: `releases/latest` → `tag_name` (один префикс `v`) → SemVer против `__version__` | — | net.http | unit на записанных JSON-ответах |
| `updates/checker.py`, `updates/track.py`, `updates/app_updater.py` | Расписание (≤ 1/24 ч, случайная задержка 0–30 мин, сначала кэш), автомат строки-статуса (17 состояний), определение трека A/B/«спросить» (`DIGSIG_ELF_MODE` из `/etc/digsig/digsig_initramfs.conf`, `policy zps`, группы `astra-admin/sudo`, `policy updates=admin`), загрузка `deb+SHA256SUMS+.asc` в `~/.cache/astra-voice/updates/`, `gpgv`+sha256+имя+arch+версия, `QProcess('pkexec', helper, 'install', path)`, коды 126/127 → «отменена/нет агента», «Обновить из файла», трек B: папка + `INSTALL-ADMIN.md` | `UpdatesBridge` | net, verify, helper | unit: автомат состояний, выбор трека (таблица), проверка подписи; e2e S9/S10 на машине |
| `security/verify.py` | `Verifier`: `gpgv --keyring <pinned> sig file` (subprocess, `--homedir` пустой), sha256 из `SHA256SUMS`; общий для релизов, deb «из файла», манифеста | — | gpgv | unit: тестовый keyring, подмена файла/подписи |
| `helper/update_helper.py` → `/usr/libexec/astra-voice/update-helper` | Root: только `install <путь>`; `os.open(O_NOFOLLOW)`, копия в `/var/lib/astra-voice/staging/` (0700, файл 0644), `gpgv` (тот же keyring), sha256, `dpkg-deb --field` (Package=astra-voice, Architecture=amd64), `dpkg --compare-versions` (только повышение), `apt-get install --no-remove -y ./x.deb` c `DEBIAN_FRONTEND=noninteractive`, JSON в stdout, коды выхода | не хранит | polkit-action `…astra_voice.update` | unit непривилегированно с фейковыми `gpgv/apt-get` (PATH-подмена); e2e: одно окно пароля (S9-A2) |
| `platform/autostart.py` | `~/.config/autostart/astra-voice.desktop`: `Exec=/usr/bin/astra-voice --hidden`, `X-KDE-autostart-after=panel`, без `OnlyShowIn`; состояние = файл есть и нет `Hidden=true`; чужие ярлыки не трогаем; путь **не переписывается** | файл | paths | unit на tmpdir; e2e S11 |
| `core/stats.py` | `stats.json` (последние 1000 событий §10 PRD), p50/p95, медиана RTFx по ≥ 5 «тёплым» прогонам, «Очистить» | файл | paths | unit: перцентили, холодный прогон исключён |
| `core/i18n.py` | Qt Linguist: `qsTr` в QML, `translate` в Python; исходный ru, `en.ts/.qm`; `pylupdate5`/`lrelease` на сборке; плюрализация `%n`; смена языка — с явным «Перезапустить» | `.qm` в `/usr/share/astra-voice/i18n/` | pyqt5-dev-tools, qttools5 | CI: `lupdate` без новых непереведённых строк для en |
| `diag/collect.py` (P2) | zip: логи, `settings.json` без домашних путей, версии, `pactl info`/`wpctl status`; без аудио/текста | файл в `~/` | — | unit: в архиве нет полей `text` |
| `tools/catalog_cli.py` (P2), `scripts/gen_theme.py`, `scripts/build_manifest.py` | CLI корпоративного манифеста из папки (sha256, размеры); генерация `Theme.qml`; сборка `catalog.json` из `research/catalog-numbers.md`-таблицы + HF API (`?blobs=true`, sha256 не-LFS считается локально) | — | — | unit; CI-проверка «регенерация чистая» |

## 4. Рантайм v1: `onnx-asr` против `sherpa-onnx`

| Критерий | `onnx-asr` 0.12.0 (MIT, PyPI 2026-07-15) | `sherpa-onnx` 1.13.7 (Apache-2.0) | Вес |
|---|---|---|---|
| GigaAM v3 e2e_rnnt (дефолт) | загрузчик `gigaam-v3` для всех 4 вариантов (`istupakov/gigaam-v3-onnx`, 226,4 МБ) | `nemo_transducer` + community-экспорт csukuangfj (231,9 МБ); v3 в официальном каталоге sherpa **нет** | паритет |
| Каталог 12 позиций | **12/12** с первоисточником под этот рантайм (istupakov/onnx-community/alphacep/t-tech) — таблица `catalog-numbers.md` §1 | GigaAM v3 ×3, Whisper ×3 (свои экспорты, другие размеры, не бенчмарчены); Vosk/T-one/Multilingual/FastConformer — либо нет, либо не подтверждено | **решающий** |
| Протокол цифр (PRD §7.1) | WER/RTFx `comparison.md`/`benchmarks.md` сняты **на onnx-asr** — полоски честны без пересчёта | цифры под sherpa пришлось бы мерить самим на 12 моделях | сильный |
| Колёса / glibc 2.36 | `py3-none-any` (4 МБ, чистый Python) + `onnxruntime` 1.29.0 `manylinux_2_28` (у нас 2.36); проверено: `libonnxruntime.so.1.29.0` — max `GLIBC_2.28`, **0 ссылок `__isoc23_*`** → шим не нужен | `manylinux_2_17` — тоже ставится | паритет |
| Собственные `.so` (ЗПС v1.1) | **3** (`libonnxruntime.so`, `libonnxruntime_providers_shared.so`, `onnxruntime_pybind11_state…so`); numpy — системный (1.24.2 ≥ 1.22.4) | 4–6 (`_sherpa_onnx…so`, `libsherpa-onnx-core`, `libsherpa-onnx-c-api`, `libonnxruntime`) + свой ORT | сильный |
| ОЗУ / скорость | та же арена ORT; декодер RNN-T — цикл на numpy (≈150 кадров на 6 с, ~50–100 мс) — **риск** для Ц2 | декодер на C++ — на 50–100 мс быстрее на RNN-T | против, измеримо (S3) |
| VAD / длинные записи | встроенный Silero-VAD (через ORT), сегментация > 20 с | встроенный Silero-VAD | паритет |
| Сопровождение | один мейнтейнер, 0.x | k2-fsa, зрелый | против (митигация: вендор + пин + Engine-интерфейс) |

**Решение:** `onnx-asr` + `onnxruntime` **1.29.0** (без `sympy` в зависимостях; `flatbuffers` 2.0.8 / `protobuf` 3.21.12 /
`packaging` 23.0 — из apt Astra, проверено). Пины по sha256 в `packaging/wheels.lock`. **Условие отката** (PRD §1.4): если спайк S3 даст на
эталоне p95 > 0,5 с при e2e_rnnt даже с 4 потоками → дефолт `e2e_ctc` (+25 %), а при провале и этого — `SherpaEngine` для
GigaAM v3 (второй адаптер, строки манифеста с `engine: sherpa-onnx`).

### 4.1 Раскладки манифеста (`engine: onnx-asr`, единый рантайм v1)

| `layout` | Модели каталога | Файлы (относительные пути из HF API `?blobs=true` на закреплённой ревизии) |
|---|---|---|
| `onnx-asr-gigaam-v3` | #1 e2e_rnnt (дефолт), #2 e2e_ctc, #3 rnnt | `v3_<variant>_*.int8.onnx` (+ decoder/joint), `vocab`, yaml — вариант в поле `variant`, один HF-репо `istupakov/gigaam-v3-onnx` |
| `onnx-asr-gigaam-multilingual` | #4 CTC 220M, #11 Large CTC | `multilingual*_ctc.int8.onnx`, vocab, yaml, config |
| `onnx-asr-t-one` | #5 T-one (fp32) | `model.onnx`, vocab, config |
| `onnx-asr-vosk` | #6 ru 0.54, #7 small-ru 0.52 | `am-onnx/…` / `am/…` `{encoder,decoder,joiner}.int8.onnx`, `lang/tokens.txt` — подкаталоги в манифесте явные |
| `onnx-community-whisper` | #8 large-v3-turbo, #9 small | `encoder_model_int8.onnx`, `decoder_model_merged_int8.onnx`, токенайзер |
| `onnx-asr-whisper-ort` | #10 base (`istupakov/whisper-base-onnx`) | `whisper-base_beamsearch.int8.onnx`, токенайзер |
| `onnx-asr-nemo` | #12 FastConformer ru pc (CTC) | `model.int8.onnx`, `vocab.txt`, config |

Адаптер держит таблицу `layout → имя загрузчика onnx-asr + список обязательных файлов`; точные идентификаторы
загрузчиков и имена файлов фиксирует спайк S3 (`scripts/build_manifest.py` генерирует `files[]` с sha256 из HF API,
для не-LFS считает sha256 сам). Пример манифеста PRD §7.3 переписывается под `engine: onnx-asr` для всех записей.

## 5. Потоки данных

1. **Диктовка (PTT).** KeyPress → `hotkey.pressed` → пилюля `listening` (≤ 100 мс, без ожидания воркера) → `record.start`
   → воркер открывает устройство (10–40 мс; 3 ретрая по 300 мс при ошибке) → `level` ≤ 30/с → (2 с пик < −60 дБ → `silent`
   → пилюля `listening-silent` + уведомление «Перезапустить звуковую службу» → `systemctl --user restart wireplumber` →
   `record.start` повторно) → KeyRelease → `record.stop` → < 0,3 с? → `empty`-тихо, пилюля скрыта через 150 мс; иначе
   `processing` → VAD (только > 20 с) → `transcribe` → `result` → `paste` (активное окно, запомненное **до** показа пилюли)
   → пилюля `done` 500 мс → `stats.dictation`. Отмена: Esc (временный grab на время записи) / «×» / трей → `record.cancel`.
   Второе нажатие во время `processing` — одна запись в очередь; вставки по порядку.
2. **Загрузка модели.** карточка «Скачать» → гейт сети (явное действие разрешено даже при выключенных тумблерах) →
   `disk_usage ≥ size×1,2` → очередь → файлы по `sources[]` → `.part`+Range → sha256 → `<rev>.partial/` → **смоук**
   (`transcribe.file` вшитого wav, `expect_any`) → `os.replace` → `current.json` → `model.load` → `model.loaded` → замер
   после первой диктовки → `measurements.json` → карточка «замерено». Из файла/папки — тот же путь с шага sha256.
3. **Проверка ревизии модели (F8).** тумблер вкл ∧ ¬offline ∧ ¬policy ∧ прошло ≥ 24 ч → задержка 0–30 мин →
   `GET /api/models/{update_watch.hf_repo}/refs` (таймаут 3 с, ≤ 1 повтор, 429 → до конца окна) → `targetCommit(branch)`
   ≠ `pinned_revision` → бейдж + строка `model-update` + одно уведомление; манифест с `catalog_url` — той же частотой,
   применяется только с валидной подписью. Ничего не скачивается само.
4. **Обновление утилиты (F9).** кэш → фон 0–10 мин → `releases/latest` (или `latest.json` с `updates_url`) → SemVer >
   `__version__` ∧ не «пропущена» → строка `available|admin-track` → панель → трек: A = `DIGSIG_ELF_MODE=0` ∧ (в
   `astra-admin` ∨ `sudo`) ∧ `policy.updates≠admin`; B = ЗПС ∨ нет прав ∨ policy; «спросить» = конфиг нечитаем ∧
   `zps=auto` (ответ «Не знаю» → B). A: скачать `deb + SHA256SUMS + SHA256SUMS.asc` → `gpgv` → sha256 → имя/arch/версия →
   `pkexec update-helper install <путь>` (одно окно polkit; 126 → «отменена», 127/нет агента → подсказка `sudo apt install ./…`)
   → `restart`. B: та же проверка → папка с `INSTALL-ADMIN.md` → «Открыть папку». «Обновить из файла…» — те же шаги с `gpgv`.
5. **Старт.** policy → settings (миграции) → theme → QML (`--hidden`: окно не создаётся, только трей) → трей (ретрай ≤ 30 с)
   → воркер → `model.load` в фоне (строка-статус `loading`) → онбординг, если нет модели/`!onboarding_done` → отложенные
   проверки. Аудио на старте **не открывается** (гонка WirePlumber), `sleep`-костылей нет.

## 6. Инварианты безопасности (проверяются тестами, T1 — security-analyst)

1. Установка пакета **только** после `gpgv`-подписи ∧ sha256 ∧ `Package=astra-voice` ∧ `Architecture=amd64` ∧ версия выше
   установленной — и в GUI, и повторно в root-помощнике на **копии** в root-staging (`O_NOFOLLOW`, без symlink/TOCTOU).
2. Ровно один привилегированный шаг: polkit-действие `io.github.arrivarus.astra_voice.update` (`auth_admin`, без `_keep`),
   `exec.path` = помощник; никаких `rules.d`, `dpkg -i`, привязки к `/usr/bin/apt`, аргументов кроме `install <путь>`.
3. Аудио живёт только в ОЗУ воркера (кольцевой буфер), очищается после результата/отмены; ни одного wav/tmp на диске;
   тесты проверяют `inotify`/список файлов до и после диктовки.
4. Распознанный текст не пишется в логи/статистику/диагностику (фильтр логгера); «последний текст» — только в памяти GUI.
5. Сеть — четыре действия PRD F14 через один `HttpClient` с гейтом (тумблеры пусты по умолчанию, `offline`, policy,
   `HF_HUB_OFFLINE=1`); хосты — только из `PRIVACY.md` (`huggingface.co`+CDN, `github.com`/`api.github.com`/
   `objects.githubusercontent.com`, `corp_base`); TLS-проверка не отключается; токенов в клиенте нет; UA проекта.
6. Манифест применяется только с валидной подписью (наш keyring или `catalog_pubkey`); `sources[].revision` обязателен,
   URL всегда `/resolve/{revision}/`; sha256 каждого файла до `os.replace`; смоук до переключения `current.json`.
7. `policy.conf` — только чтение, значения из него блокируют строки UI; `settings.json`, кэш и сокет single-instance — 0600.
8. Хоткей: захватываются только назначенные комбинации (+`Escape` на время записи) — никакого XRecord/keylogging; поле
   захвата держит `XGrabKeyboard` только пока открыто.
9. Буфер обмена: только `text/plain` (+ `x-kde-passwordManagerHint`), восстановление исходного содержимого.
10. Нет телеметрии, crash-dumps, «отправить отчёт», скрытой записи (иконка «слушаю» в трее обязательна всегда).

## 7. Упаковка, сборка, репозиторий, CI

### 7.1 Структура репозитория `ArrivaRUS/astra-voice`
```
src/astra_voice/{app.py, core/, platform/, ui/, worker/, models/, net/, updates/, security/, helper/, diag/, tools/}
qml/            Main.qml, Theme.qml (generated), PillTheme.qml, components/*.qml, sections/*.qml, onboarding/*.qml, Pill.qml
data/           catalog.json (+ .sig), catalog.schema.json, icons/ (21 глиф + hicolor/ + tray/), sounds/{start,stop}.ogg,
                fonts/ (PT Mono, PT Root UI — если лицензия позволяет; иначе только fallback PT Astra Sans), vad/silero_vad.onnx,
                test/test-ru-6s.wav, keys/release.gpg, i18n/en.ts, polkit/…update.policy, astra-voice.desktop
packaging/      debian/ (control, rules, changelog, postinst-минимум), wheels.lock (name==ver --hash=sha256), build-deb.sh,
                Containerfile (debian:12), INSTALL-ADMIN.md, sbom/
scripts/        gen_theme.py, build_manifest.py, measure_model.py, release.sh, make_virtual_mic.sh, e2e/*.sh (P21½)
tests/          unit/ integration/ e2e/ (маркеры pytest: unit · xvfb · machine)
docs/           plans.md status.md test-plan.md PRIVACY.md NOTICE  ·  arch/ decisions/ design/ research/  (как сейчас)
```

### 7.2 Содержимое `.deb` `astra-voice_X.Y.Z_amd64.deb` (оценка ≤ 30 МБ; A1 ≤ 80 МБ выполняется)
- `/usr/bin/astra-voice` — лаунчер (добавляет `/usr/lib/astra-voice/vendor` в `sys.path`, запускает `astra_voice.app`);
  `/usr/lib/python3/dist-packages/astra_voice/` — код; `/usr/lib/astra-voice/vendor/` — `onnxruntime` 1.29.0 (3 `.so`, ~60 МБ
  распакованных), `onnx_asr` — только они; `/usr/share/astra-voice/{qml,icons,sounds,fonts,vad,test,keys,
  i18n,catalog.json,catalog.json.sig}`; `/usr/share/icons/hicolor/{16x16,22x22,scalable}/{apps,status}/`;
  `/usr/share/applications/astra-voice.desktop`; `/usr/share/polkit-1/actions/io.github.arrivarus.astra_voice.policy`;
  `/usr/libexec/astra-voice/update-helper` (root:root 0755); `/usr/share/doc/astra-voice/{NOTICE,PRIVACY.md,INSTALL-ADMIN.md,
  examples/policy.conf,sbom.cdx.json}`. `/etc/astra-voice/` пакет **не создаёт** (policy пишет админ). `postinst` — только
  триггеры dh (иконки/desktop), без сети и GUI.
- **Depends:** `python3 (>= 3.11)`, `python3-pyqt5`, `python3-pyqt5.qtquick`, `python3-pyqt5.qtsvg`, `python3-xlib`,
  `python3-numpy (>= 1:1.22.4)`, `python3-requests`, `python3-jsonschema`, `python3-packaging`, `python3-protobuf`, `python3-flatbuffers`,
  `qml-module-qtquick2`, `qml-module-qtquick-controls2`, `qml-module-qtquick-layouts`, `qml-module-qtquick-window2`,
  `qml-module-qtgraphicaleffects`, `qml-module-qtquick-shapes`, `libqt5svg5`, `libpulse0`, `gpgv`, `polkitd`, `pkexec`.
  **Recommends:** `polkit-kde-agent-1 | fly-…-agent`, `pipewire-pulse | pulseaudio`, `xdotool` (P2 fallback), `wireplumber`.
  Всё — из `repository-main` Astra 1.8 (проверено `apt-cache show` на машине; `python3-pyqt5.qtquick` 5.15.9+dfsg-1+b9 доступен,
  но не установлен — тянется автоматически).
- **Собственные ELF/`.so` в пакете: 3** (все — onnxruntime) — фиксируется тестом `tests/unit/test_deb_elf_count.py`
  (`dpkg-deb -c` + `file`), это же число идёт в `INSTALL-ADMIN.md` и SBOM (допущение A11 закрывается «3»).

### 7.3 Сборка и воспроизводимость
- Одна команда: `make deb` → `packaging/build-deb.sh`: (1) `pip download --require-hashes -r packaging/wheels.lock` из
  локального кэша `packaging/wheels/` (сеть только на шаге `make wheels`, отдельно); (2) `scripts/gen_theme.py` + проверка
  чистоты; (3) `pylupdate5`/`lrelease`; (4) `dpkg-buildpackage -us -uc -b` с `SOURCE_DATE_EPOCH` из `debian/changelog`,
  `override_dh_strip` не трогает вендор (`libonnxruntime.so` остаётся байт-в-байт как в колесе — совпадает с sha256 SBOM);
  (5) `lintian`, тест установки в чистом контейнере (`apt-get install ./x.deb && astra-voice --version && python3 -c
  'import astra_voice, onnxruntime'`). Цель Ц7: ≤ 5 мин на эталоне.
- Где собирать: контейнер `debian:12` (`Containerfile`) — та же glibc 2.36 / Python 3.11.2 / PyQt5 5.15.9, что в ALSE 1.8.5
  (`/etc/debian_version` = 12.0); локальная сборка на ALSE — те же скрипты.
- SBOM: CycloneDX JSON (`scripts/sbom.py` из `wheels.lock` + `dpkg-query` по Depends; `syft` в apt нет).
- Релиз (`scripts/release.sh`, GitHub Actions on tag `vX.Y.Z`): deb → `SHA256SUMS` → `gpg --detach-sign --armor` (ключ CI,
  Ed25519, публичный — в `data/keys/release.gpg` и в README) → Release: `deb`, `SHA256SUMS`, `SHA256SUMS.asc`,
  `sbom.cdx.json`, `latest.json` (для корпоративного `updates_url`), `INSTALL-ADMIN.md`; changelog-writer → тело релиза.
  Зеркало весов — отдельный релиз `models-YYYY.MM` (ассеты ≤ 2 ГБ), пути в `sources[].type=github_release`.

### 7.4 CI (GitHub Actions, контейнер `debian:12`)
| Job | Что | Инструменты (apt Debian 12 = apt ALSE) |
|---|---|---|
| lint/types | `ruff`, `mypy --strict` по `src/`, `qmllint` по `qml/` | ruff (pip в CI), python3-mypy 1.0.1, qtdeclarative5-dev-tools |
| unit | `pytest -m unit` (без дисплея) | python3-pytest 7.2 |
| qml/xvfb | `pytest -m xvfb`: каждый QML создаётся без warnings, скриншоты состояний, XGrabKey/XTest под `Xvfb`, single-instance | xvfb, `QT_QPA_PLATFORM=xcb` |
| engine | `pytest -m engine`: `onnx-asr` + ORT на кэшированной модели (226 МБ, кэш по ревизии) — смоук 3 раскладок | кэш GitHub Actions |
| deb | `make deb` → lintian → установка в чистом контейнере → `--version` → ELF-count = 3 | dpkg-dev, debhelper, dh-python |
| release (tag) | deb + SHA256SUMS + подпись + SBOM → GitHub Release | gpg |

**Нельзя проверить в CI → P21½ на машине заказчика (и Fly-сессия):** захват хоткея на KWin/fly-wm и конфликты KDE;
пилюля без фокуса/вне Alt+Tab/над панелью; SNI-иконка и её перекраска; polkit-окно и помощник; PipeWire-захват и гонка
WirePlumber; Klipper; вставка в Kate/Konsole/LibreOffice/Firefox; `tcpdump` хостов (Ц4); автозапуск после перелогина;
живая смена темы; замер ОЗУ/RTFx (Ц2, Ц6); уведомления KDE с кнопками.

## 8. Порядок сборки: milestones по зависимостям (validation-first)

Каждый milestone начинается с тестовой обвязки (фейки `FakeAudioSource`/`FakeEngine`/HTTP-сервер), заканчивается
наблюдаемым «done» и командой проверки. Даты — рамки релизов из G1: v0.1 30.09 · v0.2 15.10 · v1.0 31.10.

### M0 — спайки (до 12.09, ~3 дня; результаты → `arch/spikes.md`, решения → журнал)

| Спайк | Что проверяем | Наблюдаемый критерий / команда | Если провал |
|---|---|---|---|
| S1 пилюля + трей | QML `Window` с флагами + EWMH на KWin **и** во Fly-сессии; композитор вкл/выкл; `QSystemTrayIcon` по имени иконки → перекраска под тему панели | `xdotool getactivewindow` не меняется после показа; `xprop -id <pill> _NET_WM_STATE` содержит `ABOVE, SKIP_TASKBAR, SKIP_PAGER`; пилюли нет в Alt+Tab; над панелью не лежит; трей-иконка меняет цвет строки под светлой/тёмной панелью | override-redirect режим; иконка-пиксмап |
| S2 polkit-помощник | `pkexec /usr/libexec/…/update-helper install x.deb` из `QProcess` GUI-процесса на тестовом пакете 0.0.1→0.0.2 (установка `.policy` и помощника — один `sudo` руками) | ровно **одно** окно пароля KDE; коды 0/126/127 различимы; `dpkg-query -W` показывает 0.0.2; GUI не блокируется | `pkcon install-local` как запасной путь (PackageKit есть) |
| S3 рантайм + замер | venv в scratch: `onnxruntime==1.29.0` + `onnx-asr==0.12.0` с **системным** numpy 1.24.2; загрузка `istupakov/gigaam-v3-onnx` (e2e_rnnt 226 МБ; при «да» — e2e_ctc и rnnt, ~680 МБ); `scripts/measure_model.py` | `import onnxruntime` ok; 10 прогонов тестового wav 6 с: медиана и p95 инференса при 2/4 потоках; `VmHWM`; число `.so` = 3; `objdump -T` без `__isoc23_`; **порог:** p95 ≤ 300 мс хотя бы у e2e_rnnt или e2e_ctc | дефолт e2e_ctc; при > 400 мс у обоих — `SherpaEngine` для GigaAM v3 |
| S4 хоткей + вставка | `python3-xlib`: grab `Ctrl+Space` ×8 масок, PTT с фильтром автоповтора, временный grab `Escape`, `BadAccess` на занятой комбинации (`Alt+F2`), XTest `Ctrl+V` в Kate при русской раскладке, восстановление буфера, `x-kde-passwordManagerHint` против Klipper | текст появился в Kate; буфер вернулся ≤ 200 мс; `qdbus org.kde.klipper /klipper getClipboardHistoryMenu` не содержит фразу; `Alt+F2` → BadAccess пойман | Shift+Insert как дефолт вставки; предупреждение о Klipper вместо защиты (P1 остаётся) |
| S5 звук | `libpulse-simple` через ctypes, 16 кГц mono s16le, виртуальный источник (`pactl load-module module-null-sink sink_name=av_test` + `module-remap-source master=av_test.monitor`), `paplay --device=av_test test.wav`; тишина; повторное открытие после `systemctl --user restart wireplumber` | уровень живой при `paplay`, `silent` через 2 с тишины; после рестарта WirePlumber устройство открывается со 2-й попытки ≤ 1 с; на диске ни одного файла | `QAudioInput` (apt `python3-pyqt5.qtmultimedia`) |

### M1–M11 (порядок = зависимости; в скобках — истории беклога)

| M | Цель | Done (наблюдаемо) | Валидация | Риски |
|---|---|---|---|---|
| **M1 Скелет и упаковка** (v0.1; 12.1, 1.1, 9.2, 10.5, 4.7-старт) | Репозиторий §7.1, `gen_theme.py` → `Theme.qml`, лаунчер, `--hidden`, single-instance, settings/policy/paths/logging/version, тема из `kdeglobals`, оболочка окна (сайдбар + «Общие» + строка-статус), `make deb`, CI зелёный | `apt install ./astra-voice_0.1.0~m1_amd64.deb` на ALSE → окно 900×620 в теме KDE; второй запуск показывает первое окно; `make deb` ≤ 5 мин | `time make deb`; `pytest -m unit`; `lintian`; `dpkg-deb -c \| grep -c '\.so$'` = 3 | QML 5.15 vs макет (тени `DropShadow`, шрифты PT); размер deb |
| **M2 Воркер и движок** (2.5, 8.3, 2.8-часть) | Супервизор, IPC, воркер, `OnnxAsrEngine` (раскладка `onnx-asr-gigaam-v3`), `model.load` из локальной папки, `transcribe.file`, `measure`, рестарт при краше, выгрузка по простою | `astra-voice --debug-transcribe data/test/test-ru-6s.wav` печатает текст с «проверка» и `t_ms`; `kill -9 <worker>` → новый pid ≤ 1 с, модель перезагружена, трей/пилюля показали ошибку один раз | `pytest -m engine` (CI с кэшем модели); `pytest tests/unit/test_supervisor.py` | RTFx (см. S3); ORT-потоки |
| **M3 Звук** (1.6-часть, 2.4, 11.1) | `PulseSimpleSource`, уровни, тишина, лимит 120 с, VAD > 20 с, список/выбор устройства, действие «Перезапустить звуковую службу», `WavFileSource` | e2e с виртуальным микрофоном: `record.start` → `paplay` 6 с → `result`; тишина → `silent` ≤ 2,2 с; 130 с записи → `limit` + текст | `scripts/e2e/virtual_mic.sh`; `pytest -m unit tests/worker/test_audio_fsm.py` | гонка WirePlumber; `EBUSY` при эксклюзивном захвате |
| **M4 Цикл диктовки** (2.1, 2.2, 2.3, 3.1, 3.2, 3.5, 4.1–4.4, 11.2, 8.4, 2.8) | Хоткей PTT/toggle, пилюля 12 состояний, трей 6 состояний + меню, вставка с восстановлением, отмена, очередь 1, статистика, корректное завершение | S2, S3, S4-A1/A3/A4, S12-A3, S14 зелёные на машине; **p95 «отпустил → текст» ≤ 0,5 с по 50 диктовкам** (Ц2) из `stats.json` | `xdotool keydown ctrl+space; paplay …; xdotool keyup ctrl+space` ×50 → `astra-voice --stats`; `pytest -m xvfb` | фокус/стек пилюли; Klipper; BadAccess |
| **M5 Онбординг и первая модель** (1.2, 1.3, 1.5, 1.6, 9.1-Общие) | Экраны O1–O4 (+O5 «Готово» без автозапуска), минимальный загрузчик (HF, Range, sha256, смоук) для модели по умолчанию, установка из папки, поле захвата + конфликты KDE, тест микрофона без вставки | S1-A1/A2/A3/A5/A7 на чистой ВМ ALSE 1.8; `tcpdump`: до включения тумблеров — 0 соединений, кроме явного «Скачать» | `pytest -m unit tests/models/test_downloader.py` (206/200/обрыв/подмена); ручной прогон S1 по секундомеру (Ц3-черновик) | HF недоступен → «Из файла» |
| **→ v0.1 «Диктовка работает» 30.09** | тег `v0.1.0` → deb + `SHA256SUMS` + `.asc` + SBOM → GitHub Release (Ц7 требует подпись уже здесь; 12.2 частично раньше плана) | заказчик пользуется вместо Handy; чеклист v0.1 беклога | `scripts/release.sh v0.1.0` | ключ подписи (CI secret) заводится сейчас |
| **M6 Каталог** (5.8, 5.1, 5.2, 5.3, 5.5, 5.6, 5.7, 5.4, 1.4, 11.3) | Манифест 12 записей (`build_manifest.py`), `CatalogModel`, карточка 20 состояний, источники HF→GitHub→corp, очередь/отмена/пауза «нет места», удаление, переключение, замеры и полоски по протоколу, ошибки повреждена/OOM, фильтры | S7, S15 зелёные; **≥ 3 модели замерены** (Ц6); скриншоты 20 состояний = референсы | `pytest -m unit tests/models/`; `pytest -m engine` на 3 раскладках; DesignReviewer | форматы Whisper/Vosk под onnx-asr (S3 закрывает) |
| **M7 Сеть и проверки** (10.8, 10.1, 6.1, 6.2, 6.3, 7.1, 7.6, 7.7) | `HttpClient` (гейт/кэш/backoff/UA/прокси/CA), HF refs, GitHub latest, строка-статус 17 состояний, тумблеры/офлайн/URL, обновление ревизии со смоуком и откатом, манифест «Новое», трей «Проверить обновления» | S6, S8-A1..A4/A6/A7; `tcpdump` за сессию: только хосты `PRIVACY.md`, в офлайне — 0 (Ц4) | `pytest -m unit tests/net/` (фейковый сервер 200/304/429/407/таймаут); `sudo tcpdump -nn 'tcp[13]&2!=0' -w s.pcap` + `ss -tnp` | 429 за NAT; HF из РФ |
| **M8 Обновлятор, трек A** (12.2, 7.2, 7.5) | Помощник + `.policy`, `app_updater`, панель 14 состояний, «Обновить из файла», проверка подписи GUI+root | S9-A1..A7 на машине: реальный апгрейд 0.2.0-rc → 0.2.0 через одно окно polkit; подменённый deb отвергнут | `pytest tests/unit/test_update_helper.py` (фейковые `gpgv/apt-get` через PATH); ручной S9 | polkit-агент во Fly; T2-ревью security-analyst |
| **M9 Автозапуск, уведомления, звук, разделы** (8.2, 1.7, 10.4, 9.1-все, 4.5, 4.6, 3.3, 3.4, 3.6, 2.6, 2.7, 1.5b, 1.8, 4.7-живая) | XDG-ярлык + O5, D-Bus-уведомления с кнопками и запасным баннером, тоны через `pa_simple_write` (без новых зависимостей), «Вывод/Продвинутые/О программе», терминалы по `WM_CLASS`, Klipper-опция, выгрузка модели, живая тема | S11, S12, S13-A2, F13-acceptance; после перелогина — трей есть, аудио не открыто (`wpctl status`) | `pytest -m unit tests/platform/test_autostart.py`; перелогин в KDE **и** во Fly | Fly-уведомления убивают владельца шины |
| **→ v0.2 «Каталог, обновления, автозапуск» 15.10** | требования №1–5 закрыты; Ц3 (≤ 5 мин на ВМ), Ц4, Ц6 | чеклист v0.2 беклога; G5 | `scripts/release.sh v0.2.0` | — |
| **M10 Корпоративный контур** (10.2, 9.4, 5.9, 10.3, 7.3, 7.4, 10.6, 12.4, 10.7) | `policy.conf` + замки, «только отечественные» (6 моделей), корп. URL + `catalog_pubkey` + `latest.json`, трек B + детект ЗПС + вопрос, `INSTALL-ADMIN.md`, документ для админа ИБ, SBOM в релизе, NOTICE/PRIVACY/дисклеймер | S10-A1..A4, S16, F14/F15-acceptance; `profile=secure` → 0 соединений и 6 карточек | `pytest -m unit tests/core/test_policy.py`, `tests/updates/test_track.py`; ВМ с `policy.conf` | согласие на имя (L1) — вне кода, гейт G5 |
| **M11 Полировка** (9.3, 9.6, 11.4, 6.4, 6.5) | Полный английский, клавиатура У13 и кольца фокуса, «Отладка» + сбор диагностики, сигнал «близкая модель», «пропустить ревизию» | DesignReviewer: 24 экрана × 2 темы; `lupdate` без непереведённых; Ц1 — Handy удалён | `pytest -m xvfb` скриншоты; чеклист G5 | — |
| **→ v1.0 31.10** · **v1.1:** ГОСТ-подпись 3 `.so` (`bsign-integrator`), ключ организации, ВМ с ЗПС (12.3, Ц5) | | | | помощник — скрипт, подписывать нечего |

## 9. Ключевые риски и альтернативы (с обратимостью)

| # | Риск | Вер./влияние | Митигация | Альтернатива · обратимость |
|---|---|---|---|---|
| R1 | Python-декодер RNN-T в `onnx-asr` не даёт p95 ≤ 0,5 с на Core Ultra 7 | ср./выс. | S3 в первую неделю; потоки ORT; дефолт e2e_ctc | `SherpaEngine` за тем же `Engine` · дни, манифест не ломается |
| R2 | Пилюля крадёт фокус / уходит под окна / чёрные углы без композитора; во Fly другой WM | ср./выс. | S1 на KWin и fly-wm; EWMH руками; re-assert + проверка перекрытия; непрозрачный вариант без композитора | override-redirect (тумблер) · часы |
| R3 | `Ctrl+Space` занят (ibus/fcitx переключают раскладку этой комбинацией на части ALSE) | ср./ср. | `BadAccess` → `not-grabbed` + «Выбрать другую»; парсер `kglobalshortcutsrc`; подсказка `Scroll Lock` | — |
| R4 | Автоповтор X11 и порядок отпускания модификаторов ломают PTT | ср./ср. | автомат следит только за основной клавишей, фильтр пар Release/Press с одним `time`; `XkbSetDetectableAutoRepeat` если доступен в python-xlib | evdev-режим P2 |
| R5 | Klipper игнорирует `x-kde-passwordManagerHint` на 5.27 | ср./низ. | S4; при провале — постоянная подпись-предупреждение (flows §7.6), опция остаётся P1 | — |
| R6 | HF недоступен из РФ / 429 за NAT; GitHub 60 req/ч | выс./ср. | источники по очереди, зеркало в Releases, корп. URL, «Из файла», кэш 24 ч, ETag, случайная задержка, Retry-After | — |
| R7 | Новые колёса ORT поднимут порог glibc или вернут `__isoc23_*` | низ./выс. | пин 1.29.0 по sha256; CI-тест `objdump -T` (max ≤ GLIBC_2.36, нет `isoc23`); при нужде — шим как `patchelf --add-needed` (+1 `.so`) | ORT 1.24.4 (GLIBC_2.27) · минуты |
| R8 | ORT 1.29 несовместим с системным numpy 1.24 в рантайме | низ./выс. | проверка `import` в S3 и CI на Debian 12 | ORT 1.24.4 (numpy ≥ 1.21.6, + `python3-sympy` из apt) · минуты |
| R9 | Полкит-агента нет (Fly) / пользователь не в `astra-admin` | ср./ср. | детект агента; трек B; текст с `sudo apt install ./…` | `pkcon install-local` · часы |
| R10 | T1 потребует помощник-ELF вместо скрипта | низ./ср. | контракт argv/JSON сохраняется | C-помощник · день (+1 ELF под ЗПС) |
| R11 | Шрифтов PT Root UI / PT Mono на ALSE нет (есть PT Astra Sans/Serif/Fact) | выс./низ. | вложить в пакет при допустимой лицензии (проверка legal); резервный стек из `tokens.json` → PT Astra Sans; DesignReviewer сверяет композицию, не метрику | — |
| R12 | Меню трея рисует Plasma (DBusMenu) — стиль спеки §9.2 недостижим | выс./низ. | зафиксировать в спеке как «системное меню»; состав/неактивность — наши | своё QML-меню-поповер · дни (хуже по UX) |
| R13 | Смоук новой ревизии держит две модели в памяти (~830 МБ пик) | ср./низ. | если `MemAvailable` < 2× — выгрузить текущую перед смоуком | — |
| R14 | Гонка WirePlumber на старте — микрофон молчит | выс./ср. | ленивое открытие, ретраи, кнопка перезапуска службы, авто-перепроверка | — |
| R15 | Ключ подписи релизов (CI secret) утёк/потерян | низ./выс. | keyring с двумя ключами (ротация), отзыв через новый релиз с обновлённым keyring, ключ в README | — |
| R16 | QML 5.15: нет `MultiEffect`/`font.features`; `DropShadow` дорог на CPU | ср./низ. | тени только на окне/поповере, PT Mono для цифр; замер CPU пилюли ≤ 3 % при записи | предрендеренные тени PNG · часы |

## 10. Что тестируем на каждом уровне

| Уровень | Среда | Что | Инструменты / команда |
|---|---|---|---|
| Unit (CI + локально) | Debian 12, без дисплея | settings/миграции, policy-таблица, автомат хоткея, парсер конфликтов, выбор комбинации вставки по `WM_CLASS`, downloader (206/200/обрыв/подмена), схема и подпись манифеста, SemVer/`tag_name`, выбор трека, помощник (фейки `gpgv`/`apt-get`), статистика p95, `kdeglobals`, IPC-кодек, автомат воркера (`FakeAudioSource`+`FakeEngine`), фильтр логов, ELF-count deb, glibc колеса | `pytest -m unit`, `mypy --strict`, `ruff`, `qmllint` |
| Integration (CI, xvfb / offscreen) | `Xvfb` + `QT_QPA_PLATFORM=xcb`, кэш модели | все QML без warnings + скриншоты состояний (24 × 2 темы) для DesignReviewer; XGrabKey/BadAccess/XTest в тестовое Qt-окно; буфер обмена с восстановлением; single-instance; трей-ветка «недоступен»; движок на 3 раскладках GigaAM с тестовым wav; смоук-откат при подменённом байте; VAD-границы | `pytest -m xvfb`, `pytest -m engine` |
| Integration с виртуальным микрофоном (машина) | ALSE, PipeWire | `paplay` в виртуальный источник → `result`; тишина; лимит; рестарт WirePlumber | `scripts/e2e/virtual_mic.sh` |
| E2E (P21½, машина заказчика, KDE **и** Fly) | реальная сессия | S1–S16 из PRD §6 по чеклисту `docs/test-plan.md`; p95 по 50 диктовкам; `tcpdump`/`ss` хосты; polkit одно окно; автозапуск после перелогина; тема live; Kate/Konsole/LibreOffice/Firefox; Klipper; Alt+Tab; ВМ с `policy.conf` | `scripts/e2e/*.sh` + ручной чеклист Юрки |
| Security (T1/T2/T3) | ревью + тесты | инварианты §6 как тест-кейсы (подмена deb/подписи/манифеста, symlink в staging, аргументы помощника, отсутствие файлов аудио, хосты) | security-analyst по `security-gate` |

## 11. Вопросы к человеку

1. **Спайк S3 качает модели с HF** (226 МБ минимум, 680 МБ для трёх вариантов) в scratch-venv — нужно «да» на трафик/диск.
2. **S2 требует одного `sudo`** на машине (положить `.policy` и тестовый помощник) — допустимо ли делать это на машине
   заказчика, или готовим ВМ ALSE 1.8 (её же используем для S1 во Fly-сессии и для чистого прогона S1/Ц3)?
3. Шрифты **PT Root UI / PT Mono** отсутствуют на ALSE: вкладывать в пакет (проверка лицензии ParaType — задача legal)
   или принять резерв PT Astra Sans для UI (метрики макета чуть разойдутся)?
4. Подпись релизов **GPG (Ed25519) через `gpgv`** вместо minisign — принять как решение архитектуры (аргумент: `gpgv` есть
   у любого админа ALSE для трека B; `minisign` в apt Astra нет)? Кто владеет закрытым ключом CI (заказчик или Юрка)?
5. Меню трея под SNI рисует Plasma — согласовать с DesignReviewer, что §9.2 спеки сверяется по составу, не по стилю.
6. Root-помощник как Python-скрипт (ноль своих ELF) — принять, либо T1 потребует C-ELF?

## 12. Факты среды (проверено read-only 2026-09-09; вход для синтеза и спайков)

- ALSE 1.8.5 = Debian 12.0 (`/etc/debian_version`), glibc 2.36, Python 3.11.2, `python3-pyqt5` 5.15.9 (модули Core/Gui/
  Widgets/Network/DBus/…; **QtQml/QtQuick не установлены**, пакет `python3-pyqt5.qtquick` 5.15.9+dfsg-1+b9 есть в apt);
  `qml-module-qtquick-controls2` 5.15.8 и `qml-module-qtgraphicaleffects` установлены; `libqt5svg5`, `libqt5x11extras5`,
  `qmllint`/`qmlscene` есть; `qtdeclarative5-dev-tools`, `pyqt5-dev-tools`, `xvfb`, `python3-pytest` 7.2, `python3-mypy` 1.0.1 — в apt.
- В apt Astra есть: `python3-xlib` 0.33, `python3-evdev` 1.6.1, `python3-requests` 2.32.5 (установлен), `python3-jsonschema`
  4.10, `python3-nacl` 1.5, `python3-flatbuffers` 2.0.8, `python3-protobuf` 3.21.12, `python3-packaging` 23.0, `python3-psutil`,
  `gpgv` 2.2.40 (установлен). **Нет:** `python3-sounddevice`, `python3-pyaudio`, `python3-pynput`, `minisign`, `syft`, `python3-semver`.
- Звук: PipeWire 1.4.9 с pulse-протоколом (`pactl` работает, драйвер источников — PipeWire; `pipewire-pulse.service` inactive —
  протокол отдаёт сам pipewire), WirePlumber 0.5.12 активен; `libpulse-simple.so.0` грузится через ctypes; есть `pw-record`,
  `paplay`, `parecord`; источники `alsa_input…Mic1/Mic2` (s32le, 48 кГц).
- Колёса ORT в `~/.cache/uv`: 1.24.4 (max `GLIBC_2.27`, 22 МБ lib) и 1.29.0 (max `GLIBC_2.28`, 28 МБ lib), **0 ссылок
  `__isoc23_*`**, по 3 `.so`; шим из Handy нужен был только prebuilt-бинарю крейта `ort`. `onnx-asr` 0.12.0: `numpy>=1.22.4`,
  `onnxruntime>=1.18.1,!=1.24.1,!=1.25.*,!=1.26.0`; системный numpy 1.24.2 подходит.
- KWin: композитор активен; EWMH поддерживает `_NET_ACTIVE_WINDOW`, `_NET_CLIENT_LIST_STACKING`, `ABOVE`, `SKIP_TASKBAR`
  (4 из 5 проверенных атомов — тип `NOTIFICATION` проверить в S1); `StatusNotifierWatcher` host зарегистрирован;
  `org.freedesktop.Notifications` — Plasma; `polkit-kde-authentication-agent-1` запущен; пользователь в `astra-admin`, `input`.
- ЗПС выключена (`DIGSIG_ELF_MODE=0`, все режимы 0); ключи изготовителя в `/etc/digsig/`.
- `~/.config/kglobalshortcutsrc` (23 КБ): формат `key=combo\tcombo,default,описание` — пример `_launch=Alt+Space\tAlt+F2\tSearch,…,Открыть строку поиска и запуск`.
- Шрифты: PT Astra Sans/Serif/Fact есть; **PT Root UI и PT Mono — нет** (R11).
