# Astra Voice — архитектурный план (Архитектор #1, Claude Fable 5.1)

> Фаза 3 · P11 · 2026-09-09 · независимый план; синтез с планом #2 — за Юркой.
> Входы: `PRD.md` 0.3, `decisions/log.md`, `design/spec.md` 0.2, `design/tokens.md` §9, `design/flows.md`,
> `design/mockups/final/index.html` (список кастомного QML, 21 глиф), `research/*` (summary, tech-platform §A–D,
> tech-models §A4–A5/B–C, tech-codex блоки 2–3/5, catalog-numbers, legal §3–4), `handy-gigaam/docs/TROUBLESHOOTING.md`.
> Среда сверена read-only на машине заказчика (см. §12 «Факты среды»). Рамки из журнала решений не пересматриваются.
> Дополнения 2026-09-09: паритет **KDE Plasma ∥ Fly** (v1.0) — §13 (правки R2, S1/S4/S5, M4, M9, §10); установка **без прав администратора** (развилка G4) — §14 (правки Р11, M1, R11).

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
| Р11 | Один `.deb`, наш код в `/usr/lib/python3/dist-packages/astra_voice`, вендор в `/usr/lib/astra-voice/vendor` (в `sys.path` добавляет только лаунчер) | Debian-политика для своего кода; вендор не засоряет системный `dist-packages` и не конфликтует с системным numpy | Если G4 выберет B/C (§14): раскладка становится **relocatable** (ресурсы относительно `__file__`), тот же `.deb` распаковывается пользователем в `~/.local/opt` без root |
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
| `ui/tray.py` | `QSystemTrayIcon`: 6 состояний иконок по имени (`astravoice-tray-{idle,listening,processing,done,error,nokey}`), подсказки, ретрай регистрации ≤ 30 с (watcher на шине), меню по спеке §9.2 (состав/неактивность), клик — окно; баннер «трей недоступен» | `state` | QtWidgets, notify | интеграция: `QSystemTrayIcon.isSystemTrayAvailable()` под Xvfb (false-ветка); e2e на Plasma и во Fly-сессии |
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
| S1 пилюля + трей (KDE **и** Fly) | QML `Window` с флагами + EWMH на KWin и на fly-wm (§13: анимации map/fade, `CenterWindowPos`, compton вкл/выкл); `QSystemTrayIcon`: KDE — иконка по имени с перекраской, Fly — SNI-хост fly-wm, иконка явных цветов, DBusMenu; уведомления с кнопкой через Plasma и `fly-notifications` | `xdotool getactivewindow` не меняется после показа; `xprop -id <pill> _NET_WM_STATE` содержит `ABOVE, SKIP_TASKBAR, SKIP_PAGER`; пилюли нет в Alt+Tab; над панелью не лежит; показ ≤ 100 мс (во Fly — с анимациями и без); трей-иконка различима на панели KDE и Fly, меню открывается | override-redirect режим; иконка-пиксмап; во Fly — XEmbed-фолбэк Qt |
| S2 polkit-помощник | `pkexec /usr/libexec/…/update-helper install x.deb` из `QProcess` GUI-процесса на тестовом пакете 0.0.1→0.0.2 (установка `.policy` и помощника — один `sudo` руками) | ровно **одно** окно пароля KDE; коды 0/126/127 различимы; `dpkg-query -W` показывает 0.0.2; GUI не блокируется | `pkcon install-local` как запасной путь (PackageKit есть) |
| S3 рантайм + замер | venv в scratch: `onnxruntime==1.29.0` + `onnx-asr==0.12.0` с **системным** numpy 1.24.2; загрузка `istupakov/gigaam-v3-onnx` (e2e_rnnt 226 МБ; при «да» — e2e_ctc и rnnt, ~680 МБ); `scripts/measure_model.py` | `import onnxruntime` ok; 10 прогонов тестового wav 6 с: медиана и p95 инференса при 2/4 потоках; `VmHWM`; число `.so` = 3; `objdump -T` без `__isoc23_`; **порог:** p95 ≤ 300 мс хотя бы у e2e_rnnt или e2e_ctc | дефолт e2e_ctc; при > 400 мс у обоих — `SherpaEngine` для GigaAM v3 |
| S4 хоткей + вставка (KDE **и** Fly) | `python3-xlib`: grab `Ctrl+Space` ×8 масок, PTT с фильтром автоповтора, временный grab `Escape`, `BadAccess` на занятой комбинации (`Alt+F2` в KDE, `Alt+space` во Fly), XTest `Ctrl+V` в Kate при русской раскладке, восстановление буфера, `x-kde-passwordManagerHint` против Klipper; во Fly — клип-менеджер fly-wm и файл `~/.fly/clipboard` (трюк с типом из чёрного списка) | текст появился в Kate; буфер вернулся ≤ 200 мс; `qdbus org.kde.klipper /klipper getClipboardHistoryMenu` не содержит фразу; `Alt+F2`/`Alt+space` → BadAccess пойман; во Fly `~/.fly/clipboard` не содержит фразу | Shift+Insert как дефолт вставки; предупреждение о Klipper/клип-менеджере Fly вместо защиты (P1 остаётся) |
| S5 звук (после входа в KDE **и** во Fly) | `libpulse-simple` через ctypes, 16 кГц mono s16le, виртуальный источник (`pactl load-module module-null-sink sink_name=av_test` + `module-remap-source master=av_test.monitor`), `paplay --device=av_test test.wav`; тишина; повторное открытие после `systemctl --user restart wireplumber`; уведомление с кнопкой в обеих службах уведомлений | уровень живой при `paplay`, `silent` через 2 с тишины; после рестарта WirePlumber устройство открывается со 2-й попытки ≤ 1 с; на диске ни одного файла; кнопка «Перезапустить звуковую службу» приходит через `ActionInvoked` и в Plasma, и в `fly-notifications` | `QAudioInput` (apt `python3-pyqt5.qtmultimedia`) |

### M1–M11 (порядок = зависимости; в скобках — истории беклога)

| M | Цель | Done (наблюдаемо) | Валидация | Риски |
|---|---|---|---|---|
| **M1 Скелет и упаковка** (v0.1; 12.1, 1.1, 9.2, 10.5, 4.7-старт) | Репозиторий §7.1, `gen_theme.py` → `Theme.qml`, лаунчер, `--hidden`, single-instance, settings/policy/paths/logging/version, тема из `kdeglobals`, оболочка окна (сайдбар + «Общие» + строка-статус), `make deb`, CI зелёный; **если выбран B/C (§14)** — плюс relocatable-лаунчер, `make user-bundle` и самопроверка зависимостей при старте | `apt install ./astra-voice_0.1.0~m1_amd64.deb` на ALSE → окно 900×620 в теме KDE; второй запуск показывает первое окно; `make deb` ≤ 5 мин | `time make deb`; `pytest -m unit`; `lintian`; `dpkg-deb -c \| grep -c '\.so$'` = 3 | QML 5.15 vs макет (тени `DropShadow`, шрифты PT); размер deb |
| **M2 Воркер и движок** (2.5, 8.3, 2.8-часть) | Супервизор, IPC, воркер, `OnnxAsrEngine` (раскладка `onnx-asr-gigaam-v3`), `model.load` из локальной папки, `transcribe.file`, `measure`, рестарт при краше, выгрузка по простою | `astra-voice --debug-transcribe data/test/test-ru-6s.wav` печатает текст с «проверка» и `t_ms`; `kill -9 <worker>` → новый pid ≤ 1 с, модель перезагружена, трей/пилюля показали ошибку один раз | `pytest -m engine` (CI с кэшем модели); `pytest tests/unit/test_supervisor.py` | RTFx (см. S3); ORT-потоки |
| **M3 Звук** (1.6-часть, 2.4, 11.1) | `PulseSimpleSource`, уровни, тишина, лимит 120 с, VAD > 20 с, список/выбор устройства, действие «Перезапустить звуковую службу», `WavFileSource` | e2e с виртуальным микрофоном: `record.start` → `paplay` 6 с → `result`; тишина → `silent` ≤ 2,2 с; 130 с записи → `limit` + текст | `scripts/e2e/virtual_mic.sh`; `pytest -m unit tests/worker/test_audio_fsm.py` | гонка WirePlumber; `EBUSY` при эксклюзивном захвате |
| **M4 Цикл диктовки** (2.1, 2.2, 2.3, 3.1, 3.2, 3.5, 4.1–4.4, 11.2, 8.4, 2.8 + Fly §13) | Хоткей PTT/toggle, пилюля 12 состояний, трей 6 состояний + меню, вставка с восстановлением, отмена, очередь 1, статистика, корректное завершение; `SessionKind`, `TrayIconProvider` (KDE/Fly), `ShortcutConflictSource` (kglobalshortcutsrc/keyshortcutrc), ветка клип-менеджера Fly, атомы анимации fly-wm для пилюли | S2, S3, S4-A1/A3/A4, S12-A3, S14 зелёные на машине **в KDE и во Fly-сессии**; **p95 «отпустил → текст» ≤ 0,5 с по 50 диктовкам** (Ц2) из `stats.json` | `xdotool keydown ctrl+space; paplay …; xdotool keyup ctrl+space` ×50 → `astra-voice --stats` (в обеих сессиях); `pytest -m xvfb` | фокус/стек пилюли (KWin и fly-wm); Klipper / `~/.fly/clipboard`; BadAccess |
| **M5 Онбординг и первая модель** (1.2, 1.3, 1.5, 1.6, 9.1-Общие) | Экраны O1–O4 (+O5 «Готово» без автозапуска), минимальный загрузчик (HF, Range, sha256, смоук) для модели по умолчанию, установка из папки, поле захвата + конфликты KDE, тест микрофона без вставки | S1-A1/A2/A3/A5/A7 на чистой ВМ ALSE 1.8; `tcpdump`: до включения тумблеров — 0 соединений, кроме явного «Скачать» | `pytest -m unit tests/models/test_downloader.py` (206/200/обрыв/подмена); ручной прогон S1 по секундомеру (Ц3-черновик) | HF недоступен → «Из файла» |
| **→ v0.1 «Диктовка работает» 30.09** | тег `v0.1.0` → deb + `SHA256SUMS` + `.asc` + SBOM → GitHub Release (Ц7 требует подпись уже здесь; 12.2 частично раньше плана) | заказчик пользуется вместо Handy; чеклист v0.1 беклога | `scripts/release.sh v0.1.0` | ключ подписи (CI secret) заводится сейчас |
| **M6 Каталог** (5.8, 5.1, 5.2, 5.3, 5.5, 5.6, 5.7, 5.4, 1.4, 11.3) | Манифест 12 записей (`build_manifest.py`), `CatalogModel`, карточка 20 состояний, источники HF→GitHub→corp, очередь/отмена/пауза «нет места», удаление, переключение, замеры и полоски по протоколу, ошибки повреждена/OOM, фильтры | S7, S15 зелёные; **≥ 3 модели замерены** (Ц6); скриншоты 20 состояний = референсы | `pytest -m unit tests/models/`; `pytest -m engine` на 3 раскладках; DesignReviewer | форматы Whisper/Vosk под onnx-asr (S3 закрывает) |
| **M7 Сеть и проверки** (10.8, 10.1, 6.1, 6.2, 6.3, 7.1, 7.6, 7.7) | `HttpClient` (гейт/кэш/backoff/UA/прокси/CA), HF refs, GitHub latest, строка-статус 17 состояний, тумблеры/офлайн/URL, обновление ревизии со смоуком и откатом, манифест «Новое», трей «Проверить обновления» | S6, S8-A1..A4/A6/A7; `tcpdump` за сессию: только хосты `PRIVACY.md`, в офлайне — 0 (Ц4) | `pytest -m unit tests/net/` (фейковый сервер 200/304/429/407/таймаут); `sudo tcpdump -nn 'tcp[13]&2!=0' -w s.pcap` + `ss -tnp` | 429 за NAT; HF из РФ |
| **M8 Обновлятор, трек A** (12.2, 7.2, 7.5) | Помощник + `.policy`, `app_updater`, панель 14 состояний, «Обновить из файла», проверка подписи GUI+root | S9-A1..A7 на машине: реальный апгрейд 0.2.0-rc → 0.2.0 через одно окно polkit; подменённый deb отвергнут | `pytest tests/unit/test_update_helper.py` (фейковые `gpgv/apt-get` через PATH); ручной S9 | polkit-агент во Fly; T2-ревью security-analyst |
| **M9 Автозапуск, уведомления, звук, разделы** (8.2, 1.7, 10.4, 9.1-все, 4.5, 4.6, 3.3, 3.4, 3.6, 2.6, 2.7, 1.5b, 1.8, 4.7-живая + Fly §13) | XDG-ярлык + O5 (во Fly пункт виден в `fly-admin-autostart`, без `OnlyShowIn`), D-Bus-уведомления с кнопками и запасным баннером (Plasma и `fly-notifications`), тоны через `pa_simple_write` (без новых зависимостей), «Вывод/Продвинутые/О программе», терминалы по `WM_CLASS` (+ `fly-term`), Klipper-опция и предупреждение о клип-менеджере Fly, выгрузка модели, живая тема из `kdeglobals` **и** `~/.fly/paletterc` (`ThemeSource`), принудительный стиль Controls 2 в лаунчере | S11, S12, S13-A2, F13-acceptance **в обеих сессиях**; после перелогина (KDE и Fly) — трей есть, аудио не открыто (`wpctl status`) | `pytest -m unit tests/platform/test_autostart.py`, `tests/core/test_theme.py` (фикстуры kdeglobals/paletterc); перелогин в KDE **и** во Fly | Fly-уведомления убивают владельца шины в KDE; `CenterWindowPos`/анимации fly-wm |
| **→ v0.2 «Каталог, обновления, автозапуск» 15.10** | требования №1–5 закрыты; Ц3 (≤ 5 мин на ВМ), Ц4, Ц6 | чеклист v0.2 беклога; G5 | `scripts/release.sh v0.2.0` | — |
| **M10 Корпоративный контур** (10.2, 9.4, 5.9, 10.3, 7.3, 7.4, 10.6, 12.4, 10.7) | `policy.conf` + замки, «только отечественные» (6 моделей), корп. URL + `catalog_pubkey` + `latest.json`, трек B + детект ЗПС + вопрос, `INSTALL-ADMIN.md`, документ для админа ИБ, SBOM в релизе, NOTICE/PRIVACY/дисклеймер | S10-A1..A4, S16, F14/F15-acceptance; `profile=secure` → 0 соединений и 6 карточек | `pytest -m unit tests/core/test_policy.py`, `tests/updates/test_track.py`; ВМ с `policy.conf` | согласие на имя (L1) — вне кода, гейт G5 |
| **M11 Полировка** (9.3, 9.6, 11.4, 6.4, 6.5) | Полный английский, клавиатура У13 и кольца фокуса, «Отладка» + сбор диагностики, сигнал «близкая модель», «пропустить ревизию» | DesignReviewer: 24 экрана × 2 темы; `lupdate` без непереведённых; Ц1 — Handy удалён | `pytest -m xvfb` скриншоты; чеклист G5 | — |
| **→ v1.0 31.10** · **v1.1:** ГОСТ-подпись 3 `.so` (`bsign-integrator`), ключ организации, ВМ с ЗПС (12.3, Ц5) | | | | помощник — скрипт, подписывать нечего |

## 9. Ключевые риски и альтернативы (с обратимостью)

| # | Риск | Вер./влияние | Митигация | Альтернатива · обратимость |
|---|---|---|---|---|
| R1 | Python-декодер RNN-T в `onnx-asr` не даёт p95 ≤ 0,5 с на Core Ultra 7 | ср./выс. | S3 в первую неделю; потоки ORT; дефолт e2e_ctc | `SherpaEngine` за тем же `Engine` · дни, манифест не ломается |
| R2 | Пилюля крадёт фокус / уходит под окна / чёрные углы без композитора; **fly-wm** — другой WM (анимации map/fade, `CenterWindowPos`, внешний compton, обработка `input=False` не подтверждена) | ср./выс. | S1 на KWin **и** во Fly-сессии (§13); EWMH руками + `USER_TIME=0`; во Fly — атомы `_FLY_WM_WINDOW_MAP_ANIMATION=0`/`_FLY_WM_FADE_SHOW=0`; re-assert + проверка перекрытия; непрозрачный вариант без композитора | override-redirect (тумблер) · часы |
| R3 | `Ctrl+Space` занят (ibus/fcitx переключают раскладку этой комбинацией на части ALSE) | ср./ср. | `BadAccess` → `not-grabbed` + «Выбрать другую»; парсер `kglobalshortcutsrc`; подсказка `Scroll Lock` | — |
| R4 | Автоповтор X11 и порядок отпускания модификаторов ломают PTT | ср./ср. | автомат следит только за основной клавишей, фильтр пар Release/Press с одним `time`; `XkbSetDetectableAutoRepeat` если доступен в python-xlib | evdev-режим P2 |
| R5 | Klipper игнорирует `x-kde-passwordManagerHint` на 5.27 | ср./низ. | S4; при провале — постоянная подпись-предупреждение (flows §7.6), опция остаётся P1 | — |
| R6 | HF недоступен из РФ / 429 за NAT; GitHub 60 req/ч | выс./ср. | источники по очереди, зеркало в Releases, корп. URL, «Из файла», кэш 24 ч, ETag, случайная задержка, Retry-After | — |
| R7 | Новые колёса ORT поднимут порог glibc или вернут `__isoc23_*` | низ./выс. | пин 1.29.0 по sha256; CI-тест `objdump -T` (max ≤ GLIBC_2.36, нет `isoc23`); при нужде — шим как `patchelf --add-needed` (+1 `.so`) | ORT 1.24.4 (GLIBC_2.27) · минуты |
| R8 | ORT 1.29 несовместим с системным numpy 1.24 в рантайме | низ./выс. | проверка `import` в S3 и CI на Debian 12 | ORT 1.24.4 (numpy ≥ 1.21.6, + `python3-sympy` из apt) · минуты |
| R9 | Полкит-агента нет (Fly) / пользователь не в `astra-admin` | ср./ср. | детект агента; трек B; текст с `sudo apt install ./…` | `pkcon install-local` · часы |
| R10 | T1 потребует помощник-ELF вместо скрипта | низ./ср. | контракт argv/JSON сохраняется | C-помощник · день (+1 ELF под ЗПС) |
| R11 | Шрифты макета (PT Root UI, PT Mono) — **в стоке** (`fly-all-main` Pre-Depends `fonts-pt` → `fonts-pt-root-ui`, `fonts-pt-mono`; `fc-list` подтверждает); ранняя проверка обрезалась `head` — риск снят | низ./низ. | Depends: `fonts-pt`; резервный стек PT Astra Sans/Fact из `tokens.json` остаётся | — |
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
| E2E (P21½, машина заказчика, **KDE-сессия и Fly-сессия — полный прогон в каждой**) | реальные сессии (перелогин), ВМ ALSE 1.8 для чистого S1 | S1–S16 из PRD §6 по чеклисту `docs/test-plan.md` × 2 сессии; p95 по 50 диктовкам в каждой; `tcpdump`/`ss` хосты; polkit одно окно (агент в KDE и во Fly); автозапуск после перелогина (systemd-генератор vs fly-wm); тема live (`kdeglobals` vs `paletterc`); трей на панели Plasma и панели Fly (SNI/XEmbed); Kate/Konsole/`fly-term`/LibreOffice/Firefox; Klipper и `~/.fly/clipboard`; Alt+Tab; ВМ с `policy.conf`; чек-лист §13.2 закрывается пунктами | `scripts/e2e/*.sh --session {kde,fly}` + ручной чеклист Юрки |
| Security (T1/T2/T3) | ревью + тесты | инварианты §6 как тест-кейсы (подмена deb/подписи/манифеста, symlink в staging, аргументы помощника, отсутствие файлов аудио, хосты) | security-analyst по `security-gate` |

## 11. Вопросы к человеку

1. **Спайк S3 качает модели с HF** (226 МБ минимум, 680 МБ для трёх вариантов) в scratch-venv — нужно «да» на трафик/диск.
2. **S2 требует одного `sudo`** на машине (положить `.policy` и тестовый помощник) — допустимо ли делать это на машине
   заказчика, или готовим ВМ ALSE 1.8 (её же используем для S1 во Fly-сессии и для чистого прогона S1/Ц3)?
3. **Развилка G4 (§14):** принять «A для `.deb` + C1 user-bundle» (мой выбор) или B (QtWidgets, +7–8 дн и повторная приёмка макета)? Нужна чистая ВМ стоковой ALSE 1.8 для §14.5.
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
- Шрифты: установлены `fonts-pt` 1.0.9 (Pre-Depends `fly-all-main`) → PT Root UI (Regular/Medium/Bold/Light), PT Mono, PT Sans/Serif, PT Astra Sans/Serif/Fact — **весь стек макета есть в стоке** (поправка к ранней проверке).

## 13. Fly-сессия ALSE: отличия и адаптеры (требование заказчика 2026-09-09 — паритет KDE ∥ Fly в v1.0)

Факты ниже сняты read-only с этой машины (пакеты `fly-all-main` 2.9.0, `fly-dm` 2.12.8, `fly-start-menu` 2.10.4 — панель
и трей; `fly-start-panel` 2.5 помечен deprecated с 1.8.0; `fly-notifications` 1.0.15; `strings /usr/bin/fly-wm`,
`~/.fly/*`, `/etc/xdg/autostart/*.desktop`, `/etc/X11/Xsession.d/*`, `/etc/X11/fly-dm/*`). Сама Fly-сессия не
запускалась — что требует входа в неё, вынесено в §13.2. Принцип: **одно приложение, никаких `OnlyShowIn`**, различия
спрятаны за интерфейсами `platform/session.py` (`SessionKind`), `core/theme.py` (`ThemeSource`), `ui/tray_icons.py`,
`platform/shortcuts_conflict.py`, `platform/paste.py`, `ui/pill.py`.

### 13.1 Отличия и адаптеры

| Область | KDE Plasma 5.27 | Fly (fly-wm) — факт с машины | Адаптер / ветка в коде | Проверка |
|---|---|---|---|---|
| Детект сессии | `_NET_SUPPORTING_WM_CHECK` → `_NET_WM_NAME=KWin`; `XDG_CURRENT_DESKTOP=KDE` | `DESKTOP_SESSION=fly` ставит `/etc/X11/Xsession.d/01-fly-detect-default-session`; `XDG_CURRENT_DESKTOP` сессией **не** экспортируется (автозапуск-файлы используют `Fly`/`fly`/`FLY`); на root есть атом `_FLY_WM_PID` | `SessionKind` по атомам root-окна (`_FLY_WM_PID` → FLY; имя WM `KWin` → KDE; иначе OTHER), env — только подсказка; пишется в лог и в «О программе» | unit на фикстурах; e2e в обеих сессиях |
| WM / пилюля | KWin: EWMH полный; урок — теряет StaysOnTop | fly-wm поддерживает `_NET_WM_STATE_ABOVE/SKIP_TASKBAR/SKIP_PAGER`, `_NET_WM_WINDOW_TYPE_NOTIFICATION/UTILITY/DOCK`, `_NET_ACTIVE_WINDOW`, `_NET_CLIENT_LIST_STACKING`, `_NET_WM_USER_TIME`, `WM_TAKE_FOCUS`, `_NET_WORKAREA` (+ свой `_FLY_WORKAREA`); есть анимации map/fade (`_FLY_WM_WINDOW_MAP_ANIMATION`, `_FLY_WM_FADE_SHOW*`), `CenterWindowPos=true` в themerc, свой атом `_FLY_WM_WINDOW_CORNER_RADIUS`; обработка `WM_HINTS.input=False` по строкам не видна | Единый путь Р4 (флаги Qt + EWMH руками + `USER_TIME=0`); ветка FLY: выставить `_FLY_WM_WINDOW_MAP_ANIMATION=0`/`_FLY_WM_FADE_SHOW=0` на окне пилюли (иначе показ > 100 мс), проверить, что fly-wm не центрирует `NOTIFICATION`-окно; запасной override-redirect общий | S1-Fly: `xdotool getactivewindow` до/после, `xprop` состояния, секундомер показа, Alt+Tab, положение над панелью |
| Композитор / прозрачность | KWin composite всегда (сейчас `active=true`) | fly-wm запускает внешний: `CompositeManager=/usr/bin/compton` (есть `compton`, `picom`, `fly-kompmgr`; `~/.fly/kompmgrrc`); пользователь может выключить в `fly-admin-theme`; env `FLY_NO_REAL_COMPOSITE` | Детект владельца `_NET_WM_CM_S0`; без композитора — непрозрачная пилюля со скруглением через XShape (общий код); ветка FLY-бонус: `_FLY_WM_WINDOW_CORNER_RADIUS=18` | S1-Fly с включённым и выключенным композитором |
| Трей | SNI-хост Plasma; иконка **по имени** перекрашивается через `<style id="current-color-scheme">`; меню — DBusMenu рисует Plasma | **fly-wm сам реализует `org.kde.StatusNotifierWatcher` и `org.freedesktop.StatusNotifierWatcher`** (интроспекция вшита), читает `IconName/AttentionIconName/OverlayIconName/Category/Status/ToolTip`, есть `Scroll/SecondaryActivate`; XEmbed-запас (`_XEMBED`, `_KDE_NET_WM_SYSTEM_TRAY_WINDOW_FOR`, `_FLY_QT5_TRAY_ICON`); DBusMenu через `libdbusmenu-qt5` (`fly-dbus-menu`); своя тема трея (`FlyIconTheme::getCurrentTrayThemeName`); ключ `X-Fly-TrayPriority` | `QSystemTrayIcon` остаётся (SNI в обеих); `TrayIconProvider`: KDE — по имени из hicolor (перекраска), FLY — иконка **явных цветов** (`QIcon` из SVG-файла со светлым/тёмным вариантом по яркости панели из `themerc`), т.к. KDE-перекраска `currentColor` — только Plasma; при XEmbed-фолбэке Qt переключается сам | S1-Fly: иконка видна, 6 состояний различимы на панели Fly, левый клик — окно, меню — состав по спеке |
| Тема / цвета | `~/.config/kdeglobals` (сейчас `ColorScheme=AstraDark`) | Источник истины — `~/.fly/paletterc` (`ColorScheme=/usr/share/color-schemes/AstraLight.colors`, `BackgroundColor`, `PrimaryColor`) + `~/.fly/theme/current.themerc` (цвета диалогов/кнопок, шрифт `PT Astra Fact-10`); `fly-admin-theme --apply-color-scheme` пишет `~/.config/Trolltech.conf` (палитра Qt) и генерирует `~/.local/share/color-schemes/00-generated-{light,dark}.colors`; **kdeglobals и paletterc расходятся** (KDE — Dark, Fly — Light); platform-theme `libfly.so` (`QT_QPA_PLATFORMTHEME=fly` для запущенных из Fly) | `ThemeSource`: FLY — `paletterc.ColorScheme` (имя `*Dark*`/`*Light*`, иначе яркость `BackgroundColor`) с `QFileSystemWatcher` на `paletterc`+`current.themerc`; KDE — kdeglobals; палитру Qt/платформенную тему **не** читаем (урок сохраняется) | unit на фикстурах AstraLight/AstraDark/generated; S13 в Fly (смена темы в `fly-admin-theme` → ≤ 2 с) |
| Qt-окружение сессии | `QT_QPA_PLATFORMTHEME=kde` (plasma-integration) | `/etc/X11/Xsession.d/06-fly-misc-env` экспортирует `QT_QUICK_CONTROLS_STYLE=org.kde.desktop`, `QT_ENABLE_HIGHDPI_SCALING=1`, `QT_SCALE_FACTOR_ROUNDING_POLICY=Round` — для **всех** сессий через fly-dm (и KDE) | Лаунчер принудительно ставит стиль Controls 2 = `Default` (env перебивается до импорта Qt), шрифты — из токенов с резервом PT Astra Sans/Fact; размеры окна логические (900×620 при любом масштабе) | `pytest -m xvfb` с подставленным env; e2e в обеих |
| Хоткеи / конфликты | `~/.config/kglobalshortcutsrc` (KGlobalAccel) | fly-wm грабит свои клавиши из `/usr/share/fly-wm/keyshortcutrc` + `~/.fly/keyshortcutrc` (формат `[ShortCutKeys] Mod4\|Shift\|less = FLYWM_ACTION`; `Alt\|space = FLYWM_POPUP_MENU`; **`Ctrl+Space` свободен**); GUI `fly-admin-hotkeys`; имена действий — `en.miscrc`/`ru.miscrc` | `ShortcutConflictSource`: KDE — парсер kglobalshortcutsrc, FLY — парсер keyshortcutrc (+ словарь `FLYWM_*` → подпись из miscrc); `BadAccess` ловится одинаково | unit на реальных файлах; S4-Fly: grab `Alt+space` → BadAccess + имя «Меню окна» |
| Буфер обмена | Klipper (история; `x-kde-passwordManagerHint`) | **Klipper во Fly обычно не запущен** (`klipper.desktop`: `OnlyShowIn=KDE;fly;` + условие `klipperrc:General:AutoStart:false`); вместо него **встроенный клип-менеджер fly-wm** (`UseClipboardManager=true`): владеет `CLIPBOARD_MANAGER`, следит через XFixes и **сохраняет содержимое буфера в файл `~/.fly/clipboard`**; типы из `ClipboardManagerTypesBlacklist` («Star Embed Source», `x-openoffice-link`) не сохраняются | `paste.py`, ветка FLY: к `text/plain` добавляем целевой тип из чёрного списка (`x-openoffice-link`, пустое тело), чтобы fly-wm не писал текст на диск; если S4-Fly покажет, что это не работает — предупреждение в «Выводе» + пункт в PRIVACY («во Fly последний буфер сохраняется системой в `~/.fly/clipboard`»); восстановление буфера — общее | S4-Fly: `stat ~/.fly/clipboard` и содержимое до/после диктовки |
| polkit-агент | `polkit-kde-authentication-agent-1` | тот же агент: `OnlyShowIn=KDE;fly;fly-tablet;fly-mobile;`; `fly-admin-policykit-1` — редактор политик, не агент; `fly-su` не используем | без ветки; детект агента — по процессу/шине; текст «нет агента» общий | S2-Fly: одно окно пароля |
| Уведомления | Plasma владеет `org.freedesktop.Notifications` | `fly-notifications` (phase 0, `OnlyShowIn=Fly`) владеет шиной, capability **`actions`** (ActionInvoked/NotificationClosed есть); OSD — `fly-notify-osd-service`; в KDE-сессии Fly-модуль «Уведомления» убивает владельца (память Astra Cowork) | `notify.py` без ветки (тот же API); таймаут 1 с → баннер в окне; `GetCapabilities` перед кнопками | S1-Fly/S5-Fly: «Микрофон молчит» с кнопкой; e2e |
| Автозапуск | XDG-генератор systemd (`app-*@autostart.service`) | fly-wm запускает `/etc/xdg/autostart` + `~/.config/autostart` **сам**: фазы (`X-KDE-autostart-phase`), `X-KDE-autostart-after` (лог `after=%s`), `X-KDE-autostart-condition`, `Hidden`, `OnlyShowIn/NotShowIn`, `X-FLY-Force-Hidden`, `X-Fly-TrayPriority`; GUI `fly-admin-autostart`; `AutostartMonitor`/`fly-admin-startup-impact-service` меряет время старта; `systemctl --user` про наш юнит ничего не знает | Файл — единственный источник правды (уже так); `X-KDE-autostart-after=panel` оставляем (безвредно); старт `--hidden` ≤ 1,5 с до трея, чтобы монитор влияния не жаловался; ретрай трея ≤ 30 с общий | S11 после перелогина во Fly; `fly-admin-autostart` показывает наш пункт |
| Звук | PipeWire + гонка WirePlumber | то же (fly-dm-greeter держит свой звуковой сервер — источник гонки); `fly-mutemic.sh`/`fly-sound-applet` | без ветки | S5 во Fly после перелогина |
| Блокировка интерпретаторов | — | fly-wm знает `FLY_INTERPRETERS_LOCKED` и помечает пункты «интерпретатор по `security.exec_id`» (`astra-interpreters-lock` из `astra-safepolicy` 3.0) — при включённой блокировке Python-лаунчер из меню/автозапуска **не стартует** | Не ветка кода: пункт `INSTALL-ADMIN.md` и документа для админа ИБ (исключение для `python3`/утилиты либо запуск от admin); риск R17 | ВМ с `astra-interpreters-lock` (v1.1 вместе с ЗПС) |

### 13.2 Что нельзя проверить без входа в Fly-сессию (список для заказчика; спайки S1/S2/S4/S5-Fly + E2E)
1. Фокус: honors ли fly-wm `WM_HINTS.input=False`/`_NET_WM_USER_TIME=0` — активное окно не меняется при показе пилюли.
2. Стек и место: `NOTIFICATION`/`ABOVE` над панелью Fly и полноэкранными окнами; не центрирует ли fly-wm пилюлю (`CenterWindowPos`); задержка показа с анимацией map/fade и без неё.
3. Композитор у заказчика (compton по themerc) включён ли по умолчанию → прозрачность/скругления; поведение при выключенном.
4. Трей: рендер SNI-иконки по имени vs пиксмап, цвет на панели Fly, левый клик/меню (DBusMenu), XEmbed-фолбэк, порядок `X-Fly-TrayPriority`.
5. Реальные env Fly-сессии (`QT_QPA_PLATFORMTHEME=fly`?, `QT_QUICK_CONTROLS_STYLE`) и их влияние на `QMenu`/`QFileDialog`/шрифты.
6. Автозапуск: запуск нашего `.desktop` fly-wm, семантика `after=panel`, `--hidden`, реакция монитора влияния на старт.
7. Клип-менеджер fly-wm: пишет ли распознанный текст в `~/.fly/clipboard` в окне 100 мс; работает ли трюк с типом из чёрного списка; гонка XFixes при восстановлении буфера.
8. Хоткей: `BadAccess` на комбинациях fly-wm; перехват `Ctrl+Space` переключателем раскладки Fly (`fly-xkbmap`), если админ его так настроил.
9. polkit-окно во Fly (агент действительно поднят через `OnlyShowIn=…fly;`); коды отмены.
10. `fly-notifications`: кнопки действий и доставка `ActionInvoked`, длина текста, поведение при закрытии.
11. Живая смена темы: какой файл меняется (`paletterc`/`current.themerc`/`Trolltech.conf`) и перекрашивается ли окно ≤ 2 с.
12. Вставка в Fly-приложения (`fly-term`: `Ctrl+Shift+V`? — добавить `WM_CLASS` в список терминалов), `fly-fm`, LibreOffice под Fly.
13. Гонка WirePlumber при входе именно через Fly-сессию и кнопка перезапуска службы.

## 14. Установка без прав администратора (требование заказчика 2026-09-09, развилка G4)

### 14.1 Факты о стоковой ALSE 1.8 Desktop (read-only; `/var/log/installer/` на машине нет)
Сток оценён как замыкание зависимостей единственных task-метапакетов машины `fly-all-main` + `fly-all-optional`
(`apt-cache depends --recurse`): **Depends/PreDepends — 1703 пакета**, **с Recommends — 2612** (сток ставит Recommends).
KDE Plasma на этой машине добавлен вручную (`apt -y install plasma-desktop plasma-settings`, 2026-02-19) → **сток = Fly**.
Оговорка: корпоративные образы бывают урезаны, а Recommends можно отключить — итог проверяется на чистой ВМ (§14.5).

| Пакет | В стоке | Как тянется / кто использует | Следствие |
|---|---|---|---|
| `python3` 3.11.2 | да (Depends) | база Astra-утилит | — |
| `python3-pyqt5` (+`.sip`) | **только Recommends** | в стоке — через `software-properties-qt` (Recommends-цепочка); на машине держится `hplip-gui`; стоковые `astra-event-viewer`/`-diagnostics` импортируют PyQt5, **не декларируя** его в Depends | практически есть везде, но гарантии Depends нет → самопроверка импорта при старте с текстом «попросите администратора `apt install python3-pyqt5`» |
| `python3-pyqt5.qtquick`, `.qtsvg` | **нет** (в apt есть: deb 440 КБ, 2,7 МБ, `QtQml/QtQuick/QtQuickWidgets.abi3.so`, зависимость `= python3-pyqt5`) | — | QtQuick без root на стоке невозможен без вендоринга биндинга |
| `qml-module-qtquick-controls2/-quick2/-layouts/-window2/-graphicaleffects/-shapes`, `libqt5qml5/quick5/quickcontrols2-5`, `qqc2desktopstyle`, `kirigami2` | да (Depends) | `fly-all-main` Pre-Depends `qml-module-org-kde-qqc2desktopstyle`; апплеты Fly на QtQuick | QML-рантайм в стоке есть всегда |
| `libqt5svg5`, `libqt5widgets5`, `libqt5dbus5`, `libpulse0`, `pipewire`, `pipewire-pulse`, `pulseaudio`, `gpgv`, `polkitd`, `pkexec`, **`polkit-kde-agent-1`**, `python3-dbus`, `python3-packaging` | да (Depends) | Fly/apt | polkit-агент есть и во Fly-стоке (§13 подтверждено) |
| `fonts-pt` → PT Root UI, PT Mono, PT Sans/Serif, PT Astra | да (Pre-Depends) | `fly-all-main` | шрифты макета в стоке |
| `wireplumber`, `python3-requests`, `konsole`, `kate`, `fly-term` | Recommends | — | кнопка «Перезапустить звуковую службу» проверяет, кто менеджер сессии PipeWire; `requests` заменить на `urllib` (stdlib) |
| `python3-xlib`, `python3-numpy`, `python3-jsonschema`, `python3-protobuf`, `python3-flatbuffers`, `klipper`, `xdotool`, `python3-pip`, `python3-venv` | **нет** | — | в `.deb` — Depends из apt Astra; в user-режиме — вендор (`xlib` и `flatbuffers` чистый Python, `numpy` — колесо ~17 МБ/22 `.so`); `jsonschema` заменить ручной проверкой; pip/venv не использовать |

Следствия для §7 независимо от варианта: `HttpClient` — на stdlib `urllib`/`ssl` (системный CA), проверка манифеста — своя
(без `jsonschema`); Depends `.deb` сокращаются до `python3-pyqt5(.qtquick,.qtsvg)`, `qml-module-*`, `python3-xlib`,
`python3-numpy`, `python3-flatbuffers`, `libqt5svg5`, `libpulse0`, `gpgv`, `polkitd`, `pkexec`.

### 14.2 Варианты

| Критерий | **A** — QtQuick Controls 2 (спека), только `.deb` | **B** — QtWidgets + QSS, «ноль сверх стока», `.deb` + user-bundle | **C** — QtQuick + user-bundle с вендором PyQt5 (C1 — pip-колёса; C2 — только биндинг `python3-pyqt5.qtquick` из apt Astra) |
|---|---|---|---|
| Верность макету G3 | 1:1 | Реализуемо: карточки/бейджи/сегмент/тёмная тема — QSS; тумблер, столбики, пилюля, спиннер — свои `QWidget` на `QPainter`; анимации — `QPropertyAnimation`; тени — PNG (эффекты дороги); overlay-скроллбар и Popup «Как мы считаем» — руками. Риск «как 2010» **средний**: QSS-радиусы без сглаживания в ряде стилей, каждый контрол — ручная отрисовка, повторная приёмка макета | 1:1 (тот же QML) |
| Зависимости сверх стока для `.deb` | `python3-pyqt5.qtquick`, `.qtsvg`, `python3-xlib`, `python3-numpy`, `python3-flatbuffers` (все в apt Astra) | `python3-xlib`, `python3-numpy`, `python3-flatbuffers` | как A |
| Установка **без root** на стоке | **нет** — `.deb` = dpkg = root; для П1 — ставит ИТ | да, если в стоке есть `python3-pyqt5` (Recommends): вендор numpy+ORT+onnx-asr+xlib ≈ 110 МБ распакованных, ~30 `.so` | **C1:** да, автономно от системного Qt/PyQt5 — колёса `PyQt5` 8,2 МБ + `PyQt5-Qt5` 5.15.19 60,9 МБ (manylinux2014, glibc ≥ 2.17) + `PyQt5-sip` + numpy + ORT ≈ **~110 МБ скачать, ~380 МБ на диске, ~250 `.so`** (оценка); **C2:** да, +2,7 МБ (3 `.so` из пакета Astra), но требует стоковый `python3-pyqt5` 5.15.9 и ABI-совпадение (`= версии`) |
| Цена переделки плана | 0 | UI-части §3, §7 (генератор QSS вместо `Theme.qml`), M1/M4/M5/M6: **+7–8 дн** (UI ≈ 20 дн в беклоге, +35 %), правка спеки §9 и повторный DesignReviewer | C1: **+3–4 дн** (relocatable-раскладка, `make user-bundle`, self-replace, самопроверка, CI-job, тест изоляции Qt); C2: +1–2 дн, но + риск ABI |
| Обновление | polkit-помощник (трек A) / админ (трек B) | user-bundle: self-replace без polkit; `.deb` — как A | как B для bundle; `.deb` — как A |
| Интеграция KDE/Fly | системный Qt + platform-theme (`kde`/`fly`): нативные диалоги, шрифты | как A | C1: свой Qt **без** platform-theme плагинов → `QFileDialog`/`QMenu` в стиле Fusion, шрифты из fontconfig (PT Root UI есть), SNI/XEmbed через собственный Qt работают; C2: как A |
| Риски | П1 без прав зависит от ИТ | «2010»-вид, объём кастомной отрисовки, всё равно нужен вендор numpy/xlib и стоковый PyQt5 по Recommends — «ноль сверх стока» не достигается | C1: размер, дубль Qt, обновления безопасности Qt на нас, нет platform-theme; C2: связка с точной версией стокового PyQt5 (репо 1.8 заморожен — низкая вероятность разрыва), проверка на чистой ВМ |

### 14.3 Общее для всех вариантов
- **ЗПС-контур и корпоративный парк** — только `.deb` руками администратора (трек B); user-bundle **не заявляется** совместимым
  с ЗПС/блокировкой интерпретаторов (неподписанные `.so` в `$HOME`), это пишется в `INSTALL-ADMIN.md` и документе для ИБ.
- **Механика user-bundle** (B/C): дерево `~/.local/opt/astra-voice/versions/<ver>/` + симлинк `current`; `~/.local/bin/astra-voice`
  (`~/.profile` добавляет каталог в PATH); `~/.local/share/applications/astra-voice.desktop`, иконки в
  `~/.local/share/icons/hicolor/`, автозапуск — тот же `~/.config/autostart`; установка = распаковка `tar.xz`
  (без pip/venv/root; `dpkg-deb`/`tar`/`xz` в стоке) либо `astra-voice --install-user` из распакованного дерева.
- **Самообновление в user-bundle** — self-replace: скачать → `gpgv` + sha256 (те же ключи) → распаковать в `versions/<new>` →
  смоук (`--version` из нового дерева) → атомарная смена `current` → перезапуск по согласию; откат = предыдущий каталог;
  `policy.conf: updates=admin`/офлайн уважаются; polkit не нужен.
- **Один код**: relocatable-раскладка (ресурсы относительно `__file__`), самопроверка зависимостей при старте (в user-режиме
  список недостающих apt-пакетов с текстом для администратора), оба артефакта из одного `make` и одного `SHA256SUMS`.
- Инварианты §6 действуют; отличие user-режима — файлы в `$HOME` изменяемы пользователем (слабее `/usr`), фиксируется в PRIVACY/ИБ-документе.

### 14.4 Рекомендация: **A для `.deb` + C1 как user-bundle** (C2 — позднее, как оптимизация размера)
- **П3 (админ, корпоративный парк, ЗПС):** подписанный `.deb` с Depends из apt Astra, polkit-обновлятор, трек B, документы —
  без изменений (§7). Это основной артефакт и единственный для защищённых контуров.
- **П1 (сотрудник без прав) и П2 (заказчик на своей машине):** самодостаточный `astra-voice-X.Y.Z-user-x86_64.tar.xz`
  (~110 МБ; модель всё равно 226 МБ) ставится в `~/.local` без root и обновляется сам. Не зависит от того, есть ли в
  стоке `python3-pyqt5` (Recommends-неопределённость снимается), не зависит от ABI стокового PyQt5 (слабость C2).
- **Почему не B:** «ноль сверх стока» недостижим (numpy, xlib, PyQt5-по-Recommends), а цена — +7–8 дн, переделка спеки и
  риск потерять принятый на G3 вид. **Почему не «только A»:** требование заказчика про установку без прав не выполняется.
- **Цена:** +3–4 дн в M1/M8 (relocatable, `make user-bundle`, self-replace, самопроверка, CI-job «bundle на чистом
  `debian:12` без PyQt5»); дизайн и остальные milestones не меняются. Условие: проверка на чистой ВМ ALSE 1.8 (§14.5).

### 14.5 Что нельзя проверить без чистой ВМ стоковой ALSE 1.8 (Fly)
1. Реальный состав стока (Recommends включены? есть ли `python3-pyqt5`, `wireplumber`, `konsole`) — замыкание метапакетов лишь прокси.
2. C1: загрузка pip-Qt (`PyQt5-Qt5` 5.15.19, manylinux2014) на glibc 2.36 с системными `libxcb*`/`libxkbcommon`/`libGL`;
   SNI-трей и уведомления из «чужого» Qt в Fly и KDE; HiDPI-переменные Fly.
3. C2: ABI биндинга `python3-pyqt5.qtquick` `+b9` со стоковым `python3-pyqt5` (`+b8`/`+b9`).
4. `.desktop` из `~/.local/share/applications` в меню Fly и в `fly-admin-autostart`; PATH `~/.local/bin` в стоковом `~/.profile`.
5. Блокировка интерпретаторов и ЗПС на ВМ — user-bundle обязан честно отказать с текстом для администратора.
