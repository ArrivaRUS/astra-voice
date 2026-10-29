# E13 «Запись встреч» — архитектура v1.1 (архитектор #1, Claude Fable 5.1)

> 2026-10-22. Вход: PRD р22 F18/S23–S26 · stories 0.16 (E13 рев. 15) · backlog рев. 22 · `design/meetings` (G3 принят 12.10) ·
> `docs/security/T1-meetings-2026-10-12.md` (+ второе мнение Astra) · `research/legal-meetings.md` M1–M12 · `research/tech-meetings.md` ·
> `arch/spikes/SP-E13-protocol.md` · результаты спайков (SP1 стенд, SP2 синтетика, SP4 VPS) · решения владельца 12.10–22.10 ·
> код `main` 6712919 (wt-v10a). Статус: **план #1 для суда Юрки** (план #2 — Astra). Спорные решения помечены «⚖», у каждого — альтернатива.
> Нумерация разделов — по брифу (1–13); в конце — открытые вопросы владельцу (по одному, с рекомендацией).

---

## 1. Контекст и допущения

### 1.1 Что зафиксировано и не пересматривается
- **Продуктовые границы** — PRD F18 п. 1–14, S23–S26; BL-1…BL-5 — часть DoD историй; MJ-1…MJ-14, MJ-2′, MN-1…MN-18 — до приёмки историй, где появляется поверхность (T1 §3).
- **Решения владельца:** В2 плашка сверху по центру; В3 «Модель»/«Ключ» меняет пользователь даже при адресе из политики; В4 расшифровка показывается по ходу, выход во время расшифровки без подтверждения; В5 замки «Задано администратором» — v1.2 (в v1.1 серые строки + одна строка-пояснение); блокировка экрана → пауза, продолжение только нажатием; седьмой ключ `meeting_ai_allow_plaintext`; сетевой диск/сетевая звуковая служба — предупреждение, не отказ (П33); **п. 12 (14.10): разделение по говорящим — блоки 20–30 мин + связывание, движок sherpa-onnx, вариант diar_v2 не делаем**; v1.1 = только E13, ориентир ~31.12; код E13 — после Must-хвостов 1.0 (решение Б 22.10).
- **Факты спайков (все — стенд VPS или синтетика, ноутбук не трогали):**
  - SP1: кандидат **B′** (`pa_stream_set_monitor_stream` на `_PulseAsync`) — PASS, исключительность 83–87 дБ (`out/SP1/bprime_*`); кандидат C (`pw-record --target`) работает; **дорожки расходятся −154…−215 ppm/ч** и это не зависит от пути микрофона (A/B); CPU пути `PulseStreamSource` 3,1 % ядра на VPS, `pw-record` 0,4–0,6 %; разрывы 0 при A/B (11 за 60 мин без sysmon — не атрибутированы).
  - SP2 (синтетика Piper): гарнитура — 0 дублей; колонки — утечка 0,94; **гейт по корреляции дорожек θ 0,35 снимает 91 % эха без потерь «Я»** (прокси; ASR-вердикт «inconclusive»).
  - SP4 (VPS, относительно): sherpa целиком на 2 ч — `MemoryError` при `RLIMIT_AS` 3 ГиБ (VmHWM 2,2–2,6 ГБ); **блоки 20 мин — VmHWM 737 МБ**, 30 мин — 957 МБ; время ≈ 40 мин на 2 ч при `nice 19` на нагруженном VPS (RTF ≈ 0,33); θ 0,5 дробит дикторов (0,9 → 7, 1,05 → 4–5, 1,2 → 1–2); 3D-Speaker CAM++ даёт 19 кластеров против 39 у WeSpeaker при том же времени; инвентарь sherpa-onnx — 4 ELF, свой `libonnxruntime.so` (второй ORT), провайдеры включают Azure (не используется).
- **Что не сделано и влияет на план:** заходы 1–2 на ноутбуке (SP1 E0–E5′ с настоящим DION и BT-гарнитурой, SP3a/b/c детектор, **SP-F блокировка во Fly**, E6-calib 5 мин настоящего звука для порога θ, SP4-C соседство с диктовкой). Архитектура ниже не зависит от их исхода (все развилки закрыты безопасной стороной), но **оценки, пороги и объём Fly-поддержки** — зависят (M0, §12).

### 1.2 Допущения архитектуры (каждое — с проверкой)

| # | Допущение | Если неверно | Где проверяется |
|---|---|---|---|
| A1 | На парке ALSE 1.8 звуковой сервер — pipewire-pulse; часть машин — чистый PulseAudio 16.1 (R-A3) | B′ работает на обоих (это и есть причина выбора); C — только PipeWire | SP1 S1.15 (стенд PulseAudio уже есть в `vps_stand.sh`) |
| A2 | Монитор-поток B′ на pipewire-pulse **следует за sink-input** при переезде на другой sink (BT-гарнитура) | переоткрываем поток по событию `sink-input changed` (код предусмотрен, §5.4) | SP1 E4 на ноутбуке |
| A3 | Два читателя одного микрофона (диктовка в воркере + встреча в захватчике) работают одновременно | разветвление PCM внутри одного процесса — ломает принцип изоляции; тогда диктовка во время встречи читает из захватчика по IPC (худший вариант, не планирую) | SP1 S1.14 |
| A4 | GigaAM v3 e2e RNN-T через onnx-asr: RTFx ≥ 15 на эталонной машине (замер 1.0: 22,8) | 2 ч ASR > 8 мин → бюджет A31 «≤ 4 мин/ч без разделения» не выполняется; снижать нечем, честно фиксируем | SP4-C / M4 на ноутбуке |
| A5 | sherpa-onnx (свой ORT) и onnx-asr (наш ORT 1.24.4) живут в **разных процессах** — конфликта символов нет по построению | — (так и спроектировано) | unit: импорт-гейт по процессам |
| A6 | KDE: `LockedHint` + `Lock/Unlock` login1 работают (SF.1); Fly: сигнала нет, пока не доказано обратное | Fly: старт отклоняется честной строкой BL-5 п. 3; при PASS SP-F — Fly включается таблицей `LOCK_SIGNAL_BY_SESSION` без изменения кода автомата | SP-F |
| A7 | Бюджеты A31 (1 ч ≤ 10 мин с разделением, ≤ 4 мин без; пик ≤ 1,5 ГБ сверх покоя) достижимы на Core Ultra 7 с блоками 20 мин | блоки 15 мин; ASR и разделение — последовательно, не параллельно; `MemAvailable`-ожидание обязательно | SP4 заход 1 (A2 при θ ≈ 1,05, C1) |
| A8 | Secret Service есть в KDE (`kwalletd` ≥ 5.97 владеет `org.freedesktop.secrets`); во Fly — скорее нет | MJ-8 п. 3: файл 0600 только при отсутствии владельца имени на шине; остальное — ключ в памяти до конца сеанса | M9, живой прогон во Fly |
| A9 | Диктовка p95 ≤ 0,8 с при фоновой расшифровке достигается `nice 19` + 2 потоками ORT в отдельном процессе | «пауза расшифровки на время диктовки» — сигнал `meeting.job.yield` от GUI (предусмотрен как резерв, не включаю по умолчанию) | SP4-C1 |

### 1.3 Принципы, из которых выведены решения
1. **Звук и признаки не покидают своих процессов.** GUI — политика, таймеры, индикация, файлы метаданных; ни одного байта PCM в GUI (как Р1 синтеза 1.0).
2. **Один крах не тянет соседа.** Запись не зависит от модели и ORT; диктовка — от расшифровки; расшифровка (текст) — от разделения (признаки). Отсюда — три новых процесса вместо одного «воркера встреч» (§2.1, ⚖).
3. **Отказ — в безопасную сторону.** Нет аренды → потоки закрыты; нет индикации → пауза; крах → захват не возобновляется; признаки на диск — невозможно по конструкции (у процесса разделения нет путей для записи, кроме pipe).
4. **Минимум нового в проверенном коде 1.0.** Переиспользуются `PulseStreamSource`/`_PulseAsync`, `ipc`/`WorkerSupervisor`, оверлей пилюли (X11/EWMH), `IndicatorGuard`, `SessionMonitor`, `policy`, каталог моделей, `HttpClient`/`NetworkGate`, `Stats`, `notify`. Расширяются параметрами и новыми модулями, не ветками внутри диктовки.
5. **Контракты раньше кода.** IPC-схемы (§4), схема файлов (§3) и таблица отказов (§9) фиксируются в M1 и покрываются тестами до реализации (validation-first, как в execution-pack 1.0).

---

## 2. Компоненты и границы процессов

### 2.1 Процессы (⚖ ключевое решение плана)

```
┌──────────────────────────────── GUI · astra-voice (PyQt5/QML) ─────────────────────────────────┐
│ core/meeting_session.py  MeetingController — автомат записи, лимиты 2/4 ч, пауза ≤ 60 мин,      │
│                          аренда (keepalive 1 с), страж индикаторов, сеанс/сон/блокировка, диск   │
│ core/meeting_jobs.py     MeetingJobs — очередь расшифровок (1 за раз), MemAvailable, резюм,      │
│                          отмена, удаление с «надгробием» и поколением (MJ-14)                   │
│ meetings/store.py        MeetingStore — каталоги 0700/0600, meeting.json, retention, экспорт     │
│ meetings/detector_policy │ meetings/policy.py (7 ключей, fail-closed) │ meetings/templates.py    │
│ meetings/ai_client.py + meetings/netaddr.py + meetings/sanitize.py │ platform/secret_store.py    │
│ ui/meeting_bridge.py (QML) · ui/meeting_banner.py (W2) · ui/meeting_offer.py (W1) ·              │
│ ui/meeting_notice.py (W3) · ui/tray.py (+2 состояния, +меню) · ui/indicators.py (reuse)          │
└──────────┬─────────────────────────────┬──────────────────────────────┬─────────────────────────┘
           │ socketpair, IPC v5          │ socketpair, IPC v5           │ socketpair, IPC v5 (та же схема,
           │ (аренда, без авторестарта)  │ (рестарт ≤ 3/10 мин, резюм)  │  диктовка без изменений)
┌──────────▼──────────────┐  ┌───────────▼────────────────────┐  ┌──────▼───────────────────────┐
│ meeting-capture         │  │ meeting-transcribe              │  │ worker (диктовка, как в 1.0) │
│ libpulse async (ctypes):│  │ onnx-asr GigaAM v3 e2e RNN-T,   │  │ GigaAM/Whisper, Silero VAD,  │
│ • детектор звонка       │  │ Silero VAD, эхо-гейт, таймкоды │  │ PCM диктовки                 │
│   (подписка, 0 потоков) │  │ по timeline, чекпойнт — текст, │  └──────────────────────────────┘
│ • 2 потока записи       │  │ credit-поток реплик в GUI       │
│   (mic + far)           │  │   ┌───────────────────────────┐ │
│ • сегменты WAV 5 мин    │  │   │ meeting-diar (дочерний,   │ │
│ • timeline, аренда,     │  │   │ на задание): sherpa-onnx, │ │
│   самопауза             │  │   │ блоки 20 мин + связывание,│ │
│ без ORT, без numpy-ML   │  │   │ центроиды только в ОЗУ    │ │
└─────────────────────────┘  │   └───────────────────────────┘ │
                             └────────────────────────────────┘
```

| Процесс | Запуск | Живёт | Hardening | `RLIMIT_AS` | Политика рестарта | Чего **никогда** не делает |
|---|---|---|---|---|---|---|
| **GUI** | как сейчас | весь сеанс | — | — | — | не открывает звук, не держит PCM, не считает признаки; единственная точка старта захвата — обработчик кнопки (BL-2 п. 4) |
| **meeting-capture** (`bootstrap.py worker --role meeting-capture`, `python3 -I`, `pass_fds`) | лениво: функция включена **и** (детектор включён **или** нажато «Записать встречу»); при выключении функции — завершается | до выключения функции/выхода; при простое без детектора — завершается через 60 с после стопа | `harden_process` **fail-closed** (MJ-5 п. 2: проверка действующих `RLIMIT_CORE=0`, `PR_GET_DUMPABLE=0`, иначе exit 3 → «Не удалось защитить память процесса»), `PR_SET_PDEATHSIG=SIGTERM`, `oom_score_adj=+300` (ниже, чем у транскрайбера: запись важнее) | 512 МиБ (PCM буферы ≤ 2 × 1 с, журналы) | **без авторестарта** (BL-2 п. 2): крах → встреча `interrupted`, диалог «Запись прервалась. Сохранено …»; новый процесс — только следующим нажатием | не грузит ORT/numpy-ML (импорт-гейт тестом); не меняет граф звука (`load-module`, `move-sink-input`, `set-default-*` — отсутствуют в коде); не открывает поток до `open` и без свежего разрешения (§4.2) |
| **meeting-transcribe** (`--role meeting-transcribe`) | по заданию (`MeetingJobs`), один экземпляр | на время задания; после `done` держится 120 с (следующая встреча в очереди), затем `model.unload` и выход | как воркер диктовки + fail-closed; `nice 19`; ORT `intra_op=2`, только `CPUExecutionProvider` | `max(min_ram_mb(GigaAM)×2, 2 ГиБ)` отдельно от диктовки | рестарт ≤ 3/10 мин (как `WorkerSupervisor`); задание **резюмируется** с чекпойнта (текст), разделение — заново (BL-3) | не читает файлы вне `meetings/<id>/` (`realpath().is_relative_to`, `O_NOFOLLOW`); не пишет ничего, кроме `transcript.json`/`checkpoint.json` этой встречи (единственный writer); не открывает звук |
| **meeting-diar** (`--role meeting-diar`, дочерний процесс транскрайбера, socketpair) | на задание с разделением | блоки одного задания; завершается с `done`/отменой (`SIGKILL` группы при отмене ≤ 1 с) | как транскрайбер; `TMPDIR` → несуществующий путь (`/nonexistent/astra-voice-diar`), `HOME` не передаётся, `cache_dir` не задаётся | 1,5 ГиБ (737 МБ на блок 20 мин × запас 2) | без рестарта: крах → задание завершается **без** разделения (подпись «Собеседники»), встреча `done` с отметкой «разделение не удалось», кнопка «Разделить заново» | не пишет файлы вообще (единственный выход — pipe к родителю); `strace -f` тест T-205 на весь процесс |
| **worker** (диктовка) | как в 1.0 | как в 1.0 | как в 1.0 | как в 1.0 | как в 1.0 | **не получает** новых полей и команд встреч (MJ-3 п. 1); роль `worker` отвечает `unknown-message` на `meeting.*` |

**⚖ Почему три новых процесса, а не один «воркер встреч».** (1) Запись длится до 4 ч и обязана пережить всё: OOM расшифровки (SP4: пик 2,2–2,6 ГБ при ошибке планирования), крах ORT, смену модели диктовки (Д4 синтеза: смена модели = перезапуск воркера — убила бы запись). (2) BL-4 п. 5 «аренда» и BL-2 «после краха не возобновлять» естественны для маленького процесса без модели: его легко убить и легко не перезапускать. (3) MJ-5 п. 3 и S4.11: `RLIMIT_AS` и `oom_score_adj` разные у захвата и расшифровки. (4) BL-3 «признаки не касаются диска по конструкции»: у `meeting-diar` просто нет путей записи, и `strace -f` на него — узкий и дешёвый тест. (5) A5: два onnxruntime в одном процессе — лишний риск, который не нужен.
**Альтернатива A (один процесс `meeting-worker`: захват + ASR + sherpa):** −1 процесс, −0,5 дн.; но запись умирает вместе с расшифровкой, нужен общий `RLIMIT_AS` ≥ 3 ГиБ на процесс с открытым микрофоном, и два ORT в одном адресном пространстве. Не рекомендую.
**Альтернатива B (захват внутри воркера диктовки):** переиспользует `AudioCapture` целиком, но перезапуск воркера при смене модели/ревизии/потоков и OOM чужой модели (Whisper turbo ~2 ГБ) рвут запись; `RLIMIT_AS` воркера 3 ГиБ становится общим. Не рекомендую.
**Альтернатива C (разделение в том же процессе, что ASR, без `meeting-diar`):** −1 процесс; приемлемо, если спайк на VPS покажет, что `import onnxruntime` + `import sherpa_onnx` в одном процессе работают и суммарный пик ≤ 1,5 ГБ. Оставляю как запасной путь M7 (переключается флагом сборки), по умолчанию — дочерний процесс.

### 2.2 Модули (компонент · ответственность · зависит от · тест)

| Модуль | Ответственность | Зависит от | Тест |
|---|---|---|---|
| `meetings/__init__.py` + `meetings/model.py` | датаклассы `Meeting`, `MeetingState`, `Mark`, `Utterance`, `SpeakerMap`; сериализация `meeting.json`/`transcript.json` (схемы §3) | — | U: round-trip, схема, лимиты полей (120/60/40 символов, Cc/Cf) |
| `meetings/store.py` | корень `meetings/`, идентификаторы `YYYY-MM-DD_HHMMSS-xxxx`, каталоги 0700/файлы 0600 (`O_EXCL|O_NOFOLLOW`), список, атомарная запись `meeting.json`, «надгробие» `.deleting-<id>`, retention звука/встреч по датам из `meeting.json`, подсчёт места, доведение незавершённых удалений при старте | `core/paths`, `model` | U: права, symlink-атаки (MN-6), retention по `started_at`, tombstone + повторный старт (MJ-14), T-223/T-231 |
| `meetings/segments.py` | порт `spikes/e13/segwriter.py`: WAV с заранее записанным заголовком + sidecar JSON, `fsync` раз в 10 с, восстановление по физической длине, timeline-точки (отсчёты ↔ монотонное время) | — | U: `kill -9` в середине → потеря ≤ 10 с; битый сегмент → отметка, не падение |
| `meetings/timeline.py` | кусочно-линейное отображение «отсчёт дорожки → t_mono → recorded_ms» (с учётом пауз), оценка относительной скорости дорожек, поиск локального лага для эхо-гейта | `segments` | U: синтетика с дрейфом −200 ppm/ч → ошибка таймкода ≤ 50 мс за 2 ч |
| `meetings/policy.py` | 7 ключей `meeting_*` из **сырых** строк политики (не через `_coerce`), по-ключевая валидация fail-closed, набор «максимально закрыто» при `INVALID`/небезопасных правах, строка-пояснение для раздела | `core/policy` (+ поле `raw`) | U: матрица F18.11 (S26-A4…A6, A10, A11), T-208, T-227 |
| `meetings/settings_ext.py` | поля `Settings` (schema 3), миграция, эффективные значения «настройка ⊕ политика» | `core/settings`, `policy` | U: миграция 2→3, откат, политика главнее |
| `meetings/apps.py` | список программ видеосвязи (`data/meeting-apps.json`: id, title, binary[], exe[], class), нормализация имён (NFC, ≤ 255 байт, без Cc/Cf) | — | U: DION по `binary=dion`, браузеры → «браузер», неизвестное → «другая программа» |
| `worker/meeting_capture.py` (процесс) | `MeetingCaptureState`: детектор (подписка `SINK_INPUT|SOURCE_OUTPUT|SERVER`, правило звонка T_on 5 с/T_off 10 с, исключение своих потоков по `pa_context_get_index`), двухфазный старт, 2 дорожки (`PulseStreamSource` для mic; `MonitorStreamSource` для far: B′/монитор sink), сегменты, аренда, самопауза, события устройств, статистика CPU/гэпов | `worker/pulse_stream`, `worker/audio`, `segments`, `timeline`, `apps`, `ipc` | U: фейк libpulse (`tests/helpers/pulse_fakes.py` расширить: sink-inputs, monitor_stream, счётчик потоков) — T-200/T-202/T-206/T-207/T-212/T-213 |
| `worker/monitor_stream.py` | источник far: `pa_stream_new` на `<sink>.monitor` + `pa_stream_set_monitor_stream(idx)` до `connect_record`; свойства потока фиксированные (MN-10); реакция на `sink-input removed/changed` (MJ-4 п. 4) | `_PulseAsync` | U: фейк; стенд: `bprime_*` сценарий как интеграционный |
| `worker/meeting_transcribe.py` (процесс) | `MeetingJobState`: чтение дорожек окнами, Silero VAD по паузам (`worker/vad.py`), эхо-гейт, ASR (`worker/engine.py`), таймкоды по timeline, credit-поток реплик, чекпойнт-текст, резюм, отмена (`RunOptions.terminate`), запуск/связь с `meeting-diar`, назначение меток репликам, совокупные пределы (MJ-10 п. 4) | `engine`, `vad`, `timeline`, `ipc`, `segments` | U: FakeEngine (tests/fakes.py) + синтетические WAV; I: GigaAM на корпусе SP4 (`transcript.json` ↔ текст TTS, S4.9) |
| `worker/meeting_diar.py` (процесс) | sherpa `OfflineSpeakerDiarization` на блоке → локальные сегменты; `SpeakerEmbeddingExtractor` → центроиды локальных дикторов в памяти; связывание с глобальными (косинус ≥ τ, один-к-одному, кумулятивные центроиды); нумерация по первому появлению; вывод только `{start,end,label}` | sherpa-onnx (vendor), `ipc` | U: `link_blocks` на фикстурах (`spikes/e13/tests`); I: корпус SP4, `strace -f` (T-205) |
| `worker/main.py` (+`bootstrap.py`) | диспетчер ролей `worker|meeting-capture|meeting-transcribe|meeting-diar`; общий `WorkerLoop`; hardening fail-closed для ролей встреч | — | U: роль без ORT не импортирует `onnxruntime` (sys.modules-гейт) |
| `worker/ipc.py` | схемы `meeting.*` (§4), `PROTOCOL_VERSION=5`, поле корреляции `req` | — | U: схемы, лимиты 64 КиБ/32 КиБ, `bad-field` на мусор |
| `worker/supervisor.py` | корреляция по `req`; флаг `auto_restart=False`; `on_exit` с кодом | — | U: расширение `test_dictation_ack` |
| `core/meeting_session.py` | `MeetingController` (§5.1 автомат): проверки перед стартом, двухфазный старт, keepalive, лимиты, паузы по причинам, сон/сеанс/блокировка, смена устройств, диск, остановка, отметки, статистика | `SessionMonitor`, `IndicatorGuard`, `store`, супервизор захвата | U: фейковый транспорт + фейковые часы — вся таблица §9; xvfb: связка с плашкой |
| `core/meeting_jobs.py` | очередь расшифровок, `MemAvailable`-гейт, модель GigaAM v3 (каталог, самопроверка F4.9), резюм при старте программы, отмена, поколение, удаление звука после успеха, «Ждёт памяти» с повтором раз в 60 с | `store`, супервизор транскрайбера, `models/*` | U: FakeSupervisor; порядок удаление↔задание (T-231) |
| `meetings/templates.py` | локальные шаблоны: «Стенограмма», «Я и собеседники», «Только текст», «Статистика» (сумма 100 %); шапка; склейка подряд идущих реплик; подстановка имён; `.md`/`.txt`; свой шаблон оформления — `str.replace` по белому списку полей (MN-12) | `model` | U: золотые файлы |
| `meetings/export_odt.py` (Could) | `.odt`/`.docx` минимальным ZIP+XML через `zipfile`+`xml.etree` (без зависимостей), экранирование (MN-15) | — | U: распаковка, валидный XML, 0600 |
| `meetings/netaddr.py` | классификация адреса по явным CIDR (MJ-2 п. 1), закреплённое множество адресов, IPv4-mapped, отказ числовых форм, `plaintext` только из политики (MJ-2′) | — | U: матрица T-209/T-210/T-235…T-237 |
| `meetings/ai_client.py` | OpenAI-совместимый `POST /chat/completions` через `HttpClient` scope `ai` (новый `NetworkKind.ai_template`), закреплённые адреса в `_bounded_connection.resolve()`, без прокси для loopback/internal, без редиректов, таймаут 180 с на весь запрос, лимит тела 4 МиБ, `ai_sent_attempted` с момента начала передачи (MJ-13), «Проверить» без текста | `net/http`, `net/gate`, `netaddr`, `sanitize` | U: поддельный сервер (`tests/helpers/http_fault_server.py`): обрыв после тела, 3xx, 429, таймаут, мусор |
| `meetings/sanitize.py` | ответ ИИ → ограниченная структура (заголовки/списки/абзацы), удаление ссылок/картинок/URL/HTML/Cc/Cf; рендер экспорта из структуры (MJ-11) | — | U: T-228 |
| `platform/secret_store.py` | Secret Service через QtDBus (plain-сессия, `CreateItem`/`SearchItems`, атрибуты только `astra-voice/meeting-ai` + `host:port`); отсутствие владельца имени → файл `config_dir()/meeting-ai-key` 0600; заперто/отменено/ошибка → ключ в памяти + предложение повторить (MJ-8) | QtDBus | U: фейковый сервис на приватной шине (`tests/helpers/private_bus.py`) |
| `ui/meeting_banner.py` + `qml/MeetingBanner.qml` | плашка W2: оверлей поверх окон, сверху по центру, без фокуса, свёртывание/перетаскивание, нельзя скрыть; кнопки «Пауза/Продолжить», «Остановить», «Продлить на час», «Продолжить с „<микрофон>“», «Текст для участников…»; `IndicatorGuard` видимость | общие X11/EWMH-функции с пилюлей (`ui/overlay.py`, вынос из `pill.py`) | xvfb: mapped/StaysOnTop/не берёт фокус, геометрия 340–524, Fly-режим совместимости |
| `ui/meeting_offer.py` + `qml/MeetingOffer.qml` | окно W1: сверху по центру, фокус не берёт, первые 500 мс нажатия игнорируются, «Начать запись» только явным щелчком/клавишей при фокусе на кнопке, автозакрытие 60 с, закрытие при блокировке без ответа | `overlay` | xvfb: T-224 |
| `ui/meeting_notice.py` + QML | карточка W3 «Предупредите участников»: текст, «Скопировать для чата» (`text/plain`), строка про mute (MN-17), повторное открытие из плашки/трея | `QClipboard` | xvfb: буфер ровно текст |
| `ui/tray.py` | состояния `MEETING_REC`/`MEETING_PAUSED` (иконки `astravoice-tray-meeting-{rec,paused}`), подсказка с таймером, пункты меню по §4.5 спеки (полный путь с клавиатуры) | `tray_icons` | U/xvfb: состав меню по состояниям; значки в теме Fly |
| `ui/meeting_bridge.py` + `qml/sections/Meetings.qml`, `qml/MeetingCard.qml`, диалоги D1/W4/«Сохранить как…»/ИИ-предупреждение/удаление | раздел «Встречи» (седьмой, после «Вывод»), экран ответственности, карточка старта, список состояний, карточка встречи A (таблица 76/124/остальное, панель участников 232/300), имена/объединение, шаблоны | `store`, `MeetingController`, `MeetingJobs`, `Theme.qml` 2.4.0 | xvfb: состояния списка (11), карточки, два размера окна; design-reviewer по `refs/` |
| `ui/icons.py` | 22 глифа из `tokens.md` §8.2; **режим заливки** (№34: параметр `fill=True` у `rec/stop/play/more`, остальные контур) | — | U: snapshot-тест глифов |
| `core/logging.py` + `meetings/events.py` | схема событий встреч: только коды + числовые/перечислимые поля; lint-тест «в `astra_voice/meetings/**` и `worker/meeting_*` `logger.*` только с константами из `events.py`»; `SECRET_FIELDS` += `meeting_title`, `speaker_name`, `notice_text`, `api_key`, `authorization`, `prompt`, `completion`, `segment_text` (страховка, MJ-12) | — | U: T-229 (маркер в исключении/теле → не в журнале), lint |
| `core/stats.py` | события `meeting_*` только числа/коды/класс программы (валидатор отбрасывает строки) | — | U: T-215 |
| `app.py` (диагностика) | архив «Собрать диагностику» по белому списку: `meetings/`, файл ключа, чекпойнты исключены; адрес эндпоинта → класс | — | U: T-215 |
| `models/catalog*` | записи `kind=diarization` (сегментация pyannote-3.0 ONNX, 3D-Speaker CAM++ ONNX), та же цепочка подписи/sha256/ONNX-скан (MJ-9); GigaAM v3 e2e для встреч — та же запись каталога, что у диктовки | — | U: T-221; CI: ELF-гейт (3 → 3 + 4 sherpa) |
| `docs/admin/meetings.md`, `PRIVACY.md` | tech-writer по US-13.13 | — | CI: grep «перехват» (T по S26-A8) |

### 2.3 Соответствие экранам дизайна (G3)

| Экран/объект (spec §1, §3) | Компонент | Состояния (spec §3) |
|---|---|---|
| Раздел «Встречи» `01-section.html` | `Meetings.qml` + `MeetingBridge` | выключено / включено / `meeting_record=off` / `manual` / негодная политика; список 11 состояний встреч; итог места; группы настроек; строка-пояснение политики |
| Экран ответственности D1 `02-responsibility.html` | `AvDialog` + `ResponsibilityDialog.qml` | тело с прокруткой, галочка детектора (недоступна при `manual`), «Понятно, включить»/«Отмена» |
| Предложение W1 `03-offer.html` | `MeetingOffer` | показ ≤ 2 с после T_on, 500 мс защита, 60 с автозакрытие, закрытие при блокировке |
| Карточка старта W4 `04-recording.html` | `StartDialog.qml` | 3 варианта, подсказка «гарнитура», предупреждения NFS/сетевой звук, отказы (диск/блокировка неизвестна/звуковая служба) |
| Плашка W2 | `MeetingBanner` | запись / пауза (причина) / предупреждение о лимите / микрофон сменился / свёрнутая |
| Карточка W3 | `MeetingNotice` | текст пользователя / текст администратора / строка mute |
| Карточка встречи A `05-meeting-A.html`, `06-meeting-states.html` | `MeetingCard.qml` | записывается / расшифровывается по ходу / готово / имена / ошибка / ждёт памяти / нужна модель; 1024×620 и 900×588 |
| «Сохранить как…», шаблоны `07-templates.html` | `SaveAsDialog.qml`, `AiTemplateCard.qml` | наш шаг → системный диалог; ИИ: предупреждение внешнего адреса, «мог быть получен», результат с меткой |
| Трей | `Tray` | «идёт запись» / «на паузе» (форма, не цвет); меню по §4.5 спеки |
| Уведомления | `notify` | лимит/микрофон/блокировка/потеря индикации/сохранено/удалено по сроку; во Fly без кнопок — дубли на плашке |

---

## 3. Данные и файлы на диске

### 3.1 Раскладка

```
~/.local/share/astra-voice/meetings/                 0700 (ensure_private_dir; XDG_DATA_HOME уважается)
├── 2026-12-03_140512-k7q2/                           0700 — id = время старта + 4 случайных [a-z0-9] (MN-3)
│   ├── meeting.json                                  0600 — метаданные и состояние (писатель: GUI; атомарно tmp→rename)
│   ├── audio/                                        0700 — есть, пока звук не удалён
│   │   ├── mic-0000.wav  mic-0000.json               0600 — сегмент ≤ 5 мин + sidecar (писатель: meeting-capture)
│   │   ├── far-0000.wav  far-0000.json
│   │   └── …                                         (индексы сквозные через паузы; пропуски допустимы)
│   ├── transcript.json                               0600 — реплики (писатель: meeting-transcribe во время задания, затем GUI)
│   ├── checkpoint.json                               0600 — прогресс задания, только текст/числа (писатель: meeting-transcribe)
│   ├── speakers.json                                 0600 — имена и объединения как текст (писатель: GUI)
│   ├── exports/                                      0700 — результаты локальных шаблонов внутри программы (0600)
│   └── ai/                                           0700 — результаты шаблонов с ИИ: <template>-<n>.json (структура) + .md (0600)
├── .deleting-2026-11-20_101500-abcd/                 «надгробие» удаления (MJ-14) — доводится при старте
└── (индекса нет: список = чтение каталогов ≤ сотен; порядок по started_at)
```
Вне этого дерева программа файлов встреч не создаёт; «Сохранить как…» — по выбору человека, обычный файл `open(..., "x")`/замена по подтверждению, 0600 (строка в диалоге).

### 3.2 `meeting.json` (schema 1)

```json
{"schema": 1, "id": "2026-12-03_140512-k7q2", "generation": 3,
 "state": "done",                 // recording | paused | recorded | transcribing | waiting_memory | needs_model | done | failed | interrupted | deleting
 "variant": "app_stream",         // app_stream | mic_only | sink_monitor
 "app_class": "dion",             // класс из списка (dion | browser | other | null) — никаких сырых имён (MJ-7)
 "title": "Встреча 03.12.2026 14:05", "started_at": "2026-12-03T14:05:12+03:00", "ended_at": "…",
 "recorded_ms": 4360000,          // записанное время без пауз (лимит 2/4 ч считается по нему)
 "limit_ms": 10800000,            // 7 200 000 (+3 600 000 за продление, потолок 14 400 000 — константа в коде)
 "intervals": [{"t_mono_ms": 191920785, "recorded_ms": 0}, {"t_mono_ms": 191920785+…, "recorded_ms": …}],
                                  // активные интервалы (пара «старт/стоп») — ось recorded_ms ↔ монотонное время
 "marks": [{"at_ms": 723000, "kind": "pause", "reason": "lock"},      // kind: pause|resume|mic_gap_start|mic_gap_end|far_ended|
           {"at_ms": 2520000, "kind": "resume"},                      //        limit_extended|damaged|indicators_lost|…
           {"at_ms": 3001000, "kind": "mic_gap_start"}, {"at_ms": 3038000, "kind": "mic_gap_end"}],
 "tracks": {"mic": {"segments": 15, "samples": 69760000}, "far": {"segments": 15, "samples": 69761200}},
 "audio": {"present": false, "deleted_at": "…", "retain_days": 0},
 "transcription": {"model_id": "gigaam-v3-e2e-rnnt", "revision": "…", "diarization": true, "diar_status": "done",
                   "finished_at": "…", "wall_ms": 412000, "peak_rss_kb": 1210000},
 "self_label": "Я",               // копия настройки на момент расшифровки (до 40 символов)
 "warnings": {"storage_network": false, "audio_server_remote": false, "whole_output": false},
 "ai": {"sent_attempted": [{"host_class": "external", "host": "api.example.com", "template": "summary", "at": "…"}]},
 "size_bytes": 1832190}
```
Правила: `title`, `self_label` — текст после санитизации (Cc/Cf, NFC, лимиты); `app_class` — только из `meeting-apps.json`; никаких путей, имён хостов для `storage_network`/`audio_server_remote` (только булевы — MN-18); `generation` растёт при отмене/перезапуске задания и при удалении (MJ-14).

### 3.3 Сегменты и sidecar (`audio/<track>-NNNN.json`)

```json
{"schema": 1, "track": "far", "index": 12, "rate": 16000, "channels": 1, "width": 2,
 "opened_t_mono_ms": 191992301, "recorded_ms_at_open": 3600000,
 "closed": true, "frames": 4800000, "frames_at_checkpoint": 4800000,
 "timeline": [[0, 191992301], [16000, 191993302], …],        // (отсчёт, t_mono_ms серверного времени) раз в 1 с (§5.3)
 "source_kind": "app_stream", "target_changed": false, "lost_estimate_ms": 10000}
```
WAV: заголовок пишется при открытии с полной длиной сегмента; восстановление (порт `segwriter.recover`) считает по физической длине, не по заголовку; незакрытый сегмент после `kill -9` → `closed=false`, хвост после последнего `fsync` сохраняется, оценка потери ≤ 10 с (S1.5d).

### 3.4 `transcript.json` и `checkpoint.json` (BL-3: только текст и числа)

```json
// transcript.json
{"schema": 1, "meeting_id": "…", "generation": 3,
 "utterances": [{"id": "u000123", "track": "far", "start_ms": 723400, "end_ms": 729900,
                 "text": "Давайте перенесём релиз на четверг.", "speaker": "S2", "final": true, "echo": false}],
 "labels_final": true}
// checkpoint.json
{"schema": 1, "meeting_id": "…", "generation": 3, "model": {"id": "…", "revision": "…", "variant": "int8", "vad": "silero-v5", "engine": "onnx-asr 0.12"},
 "asr": {"mic": {"done_until_ms": 4360000}, "far": {"done_until_ms": 3910000}},
 "diar": {"status": "pending", "block_ms": 1200000, "algo": "sherpa-pyannote3+3dspeaker-campplus+link-v1", "threshold": 1.05, "tau": 0.6},
 "limits": {"utterances": 9000, "blocks": 12}}
```
`speaker`: `"me"` (дорожка микрофона), `"others"` (разделение выключено/не удалось), `"S<n>"` (номер по первому появлению). Резюм: `asr.done_until_ms` по дорожкам → продолжение со следующего VAD-сегмента; `diar.status != done` → разделение **заново по всей дорожке**, метки всех реплик перезаписываются (BL-3 п. 1); имена (`speakers.json`) при повторном разделении сбрасываются с предупреждением. Чекпойнт с другой версией модели/алгоритма отвергается (разделение и ASR заново — T-326); после `done` `checkpoint.json` удаляется (T-304).

### 3.5 `speakers.json`
```json
{"schema": 1, "labels": {"S1": {"name": "Иван Петров"}, "S2": {"name": null}, "S3": {"merged_into": "S1"}}, "renumbered": false}
```
Номера не перенумеровываются (UX §9 п. 15): объединение — `merged_into`, номер исчезает из отображения, остальные не меняются; отображение «② Иван Петров» строится при рендере.

### 3.6 Жизненный цикл встречи

```
(нажатие «Начать запись») ─prepare/open─► recording ◄──resume (нажатие|авто по причине)── paused(reason)
      │ стоп/лимит/диск/сон/сеанс/пауза>60 мин                 ▲ pause(reason: user|lock|indicators|lease|…)
      ▼                                                         │
   recorded ──«Расшифровывать сразу» или кнопка──► transcribing ──► done ──(retain_days=0)──► звук удалён
      │                                              │   │  └─ failed (звук НЕ удалён; «Расшифровать» снова)
      │ крах/ kill -9 GUI или захватчика              │   └─ waiting_memory (повтор раз в 60 с)
      ▼                                              └─ needs_model (кнопка «Установить»)
  interrupted ──«Расшифровать»──► transcribing | ──«Удалить»──► deleting ──► (каталог удалён)
```
Удаление (MJ-14): `state=deleting` в `meeting.json` → `rename` каталога в `.deleting-<id>` → отмена заданий по этой встрече (ожидание `cancelled` ≤ 5 с, иначе `SIGKILL` транскрайбера) → `rmtree` через `dir_fd`; при старте программы все `.deleting-*` доводятся. Публикация результата задания проверяет `(id, generation)` — для удалённой/перезапущенной встречи результат отбрасывается, каталог не воссоздаётся. Retention: при старте и раз в 24 ч — звук по `transcription.finished_at + retain_days`, встречи по `started_at + meeting_retention_days`; одно уведомление с числом.

### 3.7 Настройки и политика

`Settings` (schema 2 → **3**, миграция — только добавление полей с умолчаниями, поля существующих установок не трогаются):

| Поле | Тип/умолчание | Политика |
|---|---|---|
| `meetings_enabled` | bool, **false** (включает только экран ответственности) | `meeting_record=off` → всегда false и недоступно |
| `meeting_offer_on_call` | bool, false | `manual` → заблокирован выключенным |
| `meeting_transcribe_immediately` | bool, true | — |
| `meeting_diarization` | bool, true | `meeting_diarization` |
| `meeting_audio_retention_days` | int, 0 (0/7/30 в UI; политика — любое 0–365) | `meeting_audio_retention_days` |
| `meeting_retention_days` | int, 0 = никогда (30/90/365) | — (A34) |
| `meeting_notice_text` | str, текст по умолчанию (≤ 500, NFC, без Cc/Cf) | `meeting_notice_text` |
| `meeting_invite_text` | str, текст по умолчанию | — |
| `meeting_self_label` | str, «Я» (≤ 40) | — |
| `meeting_ai_endpoint` | str, "" | `meeting_ai_endpoint` (URL/`off`) |
| `meeting_ai_model` | str, "" | — (В3) |
| `meeting_ai_replace_names` | bool, false (для внутреннего адреса; для внешнего — галочка в диалоге, по умолчанию отмечена) | — |
| (ключ доступа) | **не в settings.json** — Secret Service / файл 0600 | — |

Политика: `core/policy.Policy` получает поле `raw: Mapping[str, str]` (все пары секции до `_coerce`); `meetings/policy.py` разбирает 7 ключей сам, по-ключево, fail-closed (MJ-1 п. 3), и отдаёт `MeetingPolicy` (значение, `locked`, причина для UI, код в журнал). `Policy.status == INVALID` или файл не root/`mode & 0o022` → набор «максимально закрыто». `profile=secure` → `meeting_ai_allow_plaintext` игнорируется (Д46, MJ-2′ п. 3). Значения `meeting_*` **не** добавляются в `_SETTINGS_TYPES`, чтобы негодное `meeting_*` не делало INVALID всю политику (сейчас `_coerce` по типам `Settings` сделал бы именно это).

### 3.8 Каталог моделей
Две новые записи `kind=diarization` (подписанный каталог, `files[]` с sha256, лицензия в манифесте): `sherpa-onnx-pyannote-segmentation-3-0` (MIT, ~5,8 МБ) и `3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced` (Apache-2.0, ~28 МБ; ⚖ D19 — 3D-Speaker: лицензия чистая, ложных дикторов меньше (A7), время то же; альтернатива WeSpeaker CAM++ LM при CC-BY-4.0 + NOTICE, если калибровка на ноутбуке покажет DER хуже на > 2 п.п.). Устанавливаются вместе одной кнопкой «Установить разделение собеседников» (карточка встречи/раздел); без них — «Собеседники» и строка «Разделение по голосам: модель не установлена» (Could: автоустановка вместе с GigaAM v3). GigaAM v3 e2e RNN-T — та же запись, что для диктовки (если пользователь работает на другой модели, для встреч нужна установленная GigaAM v3 — «Для расшифровки встреч нужна модель GigaAM v3», «Установить»).

---

## 4. IPC и контракты

### 4.1 Общее
- Транспорт, кадрирование и схемы — `worker/ipc.py` (len-prefixed JSON, 64 КиБ кадр, 32 КиБ текст, `bad-field`/`unknown-message`), **`PROTOCOL_VERSION = 5`** (одна константа у GUI и всех ролей — обновляются вместе). Диктовка: схемы не меняются; роль `worker` на `meeting.*` отвечает `unknown-message`.
- **Корреляция:** все команды встреч несут `req` (строка `[A-Za-z0-9_-]{1,64}`, уникальная на запрос); `WorkerSupervisor._correlation` → `("req", req, request_type)`; ответ-подтверждение всегда содержит `req` и `request_type` (как сейчас для ACK диктовки). События (без `req`) несут `meeting_id` и `generation`. Таймаут ожидания — у каждой команды (таблица ниже); истечение при живом ответчике → `error{code: timeout}` только этому ожиданию; при мёртвом (ни одного кадра после отправки) → SIGKILL + политика рестарта роли.
- **Поколение процесса** (`generation` супервизора) + **поколение встречи** (`meeting.json.generation`) — события старого поколения отбрасываются.
- Лимиты `ipc.py` не повышаются (У35): реплика ≤ 32 КиБ текста (ASR-сегмент ≤ 24 с таких размеров не даёт); метки — порциями по ≤ 2 000 присвоений на кадр.

### 4.2 Роль `meeting-capture`

| Команда (GUI → capture) | Поля | Ответ | Таймаут | Семантика |
|---|---|---|---|---|
| `meeting.detector.start` | `req`, `apps_version` | `meeting.detector.started{req, own_client_index}` | 3 с | подписка `SINK_INPUT|SOURCE_OUTPUT|SERVER` + начальные списки; **ни одного потока** (BL-1) |
| `meeting.detector.stop` | `req` | `meeting.detector.stopped{req}` | 3 с | отписка; контекст остаётся до `shutdown` |
| `meeting.call.query` | `req` | `meeting.call.state{req, active, call_id?, app_class?, target: {sink_input, object_serial, verified}?}` | 2 с | снимок для карточки старта (вариант 1 доступен только при `verified ∈ {declared-verified, declared}`) |
| `meeting.capture.prepare` | `req`, `meeting_id`, `dir`, `mic_device?`, `far: {kind: app_stream|sink_monitor|none, sink_input?, object_serial?, call_id?}`, `segment_s` (300), `fsync_s` (10), `segment_index_from` | `meeting.capture.prepared{req, meeting_id, mic_label, server_local: bool, server_kind: pipewire|pulseaudio, far_available: bool, far_reason?}` | 5 с | контекст готов, устройства разрешены, цель сверена (индекс+serial+pid), писатели сегментов созданы (файлы ещё нет), **потоков нет** |
| `meeting.capture.open` | `req`, `meeting_id`, `admission_mono_ms` | `meeting.capture.started{req, meeting_id, t0_mono_ms, tracks: [mic, far]}` | 3 с | создаёт потоки; отказ, если `now − admission_mono_ms > 1 000` (разрешение устарело) или не было `prepare`; «≤ 1 с до первого сэмпла» — холодное открытие потока на тёплом контексте |
| `meeting.capture.keepalive` | `meeting_id`, `seq` (без `req`) | `meeting.capture.alive{meeting_id, seq, state, recorded_ms, tracks: {mic: live|paused|lost, far: live|ended|…}, free_mb, cpu_pct}` | — (событие) | аренда: GUI шлёт раз в 1 с; капчер без `keepalive` 5 с подряд → **самопауза** `autopaused{reason: lease}` |
| `meeting.capture.pause` | `req`, `meeting_id`, `reason` ∈ {user, lock, indicators, mic_change, disk} | `meeting.capture.paused{req, meeting_id, at_recorded_ms, segments_closed}` | 2 с | потоки закрыты ≤ 1 с, сегменты дописаны и `fsync`; контекст жив |
| `meeting.capture.resume` | `req`, `meeting_id`, `mic_device?`, `tracks?: [mic, far]`, `admission_mono_ms` | `meeting.capture.started{…}` | 3 с | как `open` (тот же порядок проверок); `tracks=[far]` — продолжить без микрофона (после смены устройства «Остановить запись»/«Продолжить с …») |
| `meeting.capture.track.resume` | `req`, `meeting_id`, `track: mic`, `mic_device` | `meeting.capture.track{…state: live}` | 3 с | «Продолжить с „<микрофон>“» — переоткрыть только дорожку микрофона |
| `meeting.capture.stop` | `req`, `meeting_id` | `meeting.capture.stopped{req, meeting_id, recorded_ms, segments: {mic, far}, lost_estimate_ms}` | 3 с | потоки закрыты ≤ 1 с; при таймауте — SIGKILL процесса (сегменты восстановимы) |
| `meeting.capture.shutdown` | `req` | `meeting.capture.bye{req}` | 2 с | завершение процесса (функция выключена/выход) |
| `ping` | — | `pong` | 2 с | как в 1.0 |

| Событие (capture → GUI) | Поля | Когда |
|---|---|---|
| `meeting.call.started` | `call_id`, `app_class`, `verified`, `has_target` | T_on выполнено (программа из списка держит source-output **и** sink-input ≥ 5 с); не чаще 1 раза на звонок |
| `meeting.call.ended` | `call_id`, `reason: mic_released|app_gone` | программа отпустила микрофон ≥ 10 с или клиент исчез |
| `meeting.capture.track` | `meeting_id`, `track`, `state: live|paused|lost|ended`, `kind: switched|device-lost|target-removed|sink-moved|reopened`, `label?`, `at_recorded_ms` | смена/потеря микрофона (У44 через `PulseStreamSource.device_change`), исчезновение цели far (MJ-4 п. 4), переоткрытие |
| `meeting.capture.autopaused` | `meeting_id`, `reason: lease|audio-failed|disk-write`, `at_recorded_ms` | капчер закрыл потоки сам |
| `meeting.capture.segment` | `meeting_id`, `track`, `index`, `frames`, `closed` | сегмент закрыт (для прогресса/места; не чаще раза в 5 мин на дорожку) |
| `meeting.capture.warning` | `meeting_id`, `code: gaps|cpu|late-read`, `value` | диагностика без текста |
| `error` | `code`, `message`, `req?`, `request_type?` | коды: `audio-no-device`, `audio-busy`, `audio-failed`, `target-gone`, `target-mismatch`, `not-prepared`, `admission-stale`, `hardening-failed`, `dir-unwritable` |

**Протокол «потеря GUI»:** `PR_SET_PDEATHSIG=SIGTERM` → обработчик: закрыть потоки, дописать сегменты, `fsync`, `meeting.json` **не трогать** (писатель — GUI), выйти 0. Зависший GUI (`SIGSTOP`) → через 5 с без `keepalive` самопауза; возврат GUI → GUI видит `alive{state: paused, reason: lease}` → шлёт `resume` автоматически (BL-4 п. 2: индикатор вернулся) с отметкой.

### 4.3 Роль `meeting-transcribe`

| Команда (GUI → transcribe) | Поля | Ответ | Таймаут |
|---|---|---|---|
| `model.load` | как в 1.0 (`id, revision, dir, layout, variant, threads=2, min_ram_mb`) | `model.loaded` | как в 1.0 (по RTFx) |
| `meeting.job.start` | `req`, `meeting_id`, `generation`, `dir`, `tracks: [mic, far]`, `variant`, `diarization: bool`, `diar_models: {segmentation_dir, embedder_dir}?`, `self_track: mic|none`, `resume: bool`, `echo_gate: bool`, `limits: {max_ms: 14 400 000, max_utterances, max_blocks}` | `meeting.job.accepted{req, meeting_id, resume_from: {mic_ms, far_ms}}` | 5 с |
| `meeting.job.credit` | `meeting_id`, `n` (без `req`) | — | — | кредиты на реплики: GUI выдаёт 64 при старте и по 32 после обработки каждых 32; транскрайбер **блокируется** без кредитов (MJ-10 п. 4, S4.12) |
| `meeting.job.cancel` | `req`, `meeting_id` | `meeting.job.cancelled{req, meeting_id}` | 3 с (иначе SIGKILL группы) |
| `model.unload`, `ping`, `measure` | как в 1.0 | как в 1.0 | как в 1.0 |

| Событие (transcribe → GUI) | Поля |
|---|---|
| `meeting.job.progress` | `meeting_id`, `generation`, `stage: vad|asr|diar|link|finalize`, `done_ms`, `total_ms`, `block?`, `blocks?` — не чаще 2 раз в секунду |
| `meeting.job.utterance` | `meeting_id`, `generation`, `id`, `track`, `start_ms`, `end_ms`, `text`, `speaker: me|others|null`, `provisional: true` (до финальных меток) |
| `meeting.job.labels` | `meeting_id`, `generation`, `items: [[utt_id, "S2"], …]` ≤ 2 000 на кадр, `final: bool`, `speakers_total` |
| `meeting.job.waiting` | `meeting_id`, `reason: memory`, `need_mb`, `available_mb` |
| `meeting.job.done` | `meeting_id`, `generation`, `utterances`, `speakers`, `diar_status: done|skipped|failed`, `wall_ms`, `peak_rss_kb`, `echo_dropped` |
| `meeting.job.failed` | `meeting_id`, `generation`, `code: model|memory|limit|damaged|io|cancelled-by-delete` |
| `error` | как в 1.0 |

**Перезапуск воркера при 2-часовой задаче.** Транскрайбер пишет `checkpoint.json` атомарно после каждого закрытого окна ASR (≈ каждые 30 с аудио) и после каждого блока разделения; `transcript.json` — после каждых 32 реплик и в конце. Крах → супервизор рестартует (≤ 3/10 мин) → `MeetingJobs` отправляет `job.start{resume: true}` → транскрайбер читает чекпойнт, проверяет совпадение `model`/`algo`/`generation`, продолжает ASR с `done_until_ms`, разделение — с нуля (BL-3). 3 краха за 10 мин → `failed{code: …}`, встреча «Ошибка расшифровки», звук сохранён. Отмена/удаление: `job.cancel` → `RunOptions.terminate` текущего инференса + `SIGKILL` группы `meeting-diar` → `cancelled` ≤ 1 с.

### 4.4 Транскрайбер ↔ `meeting-diar` (внутренний, тот же `ipc.py`)

| Сообщение | Поля |
|---|---|
| `diar.load` → `diar.loaded` | `segmentation_dir`, `embedder_dir`, `threshold`, `tau`, `threads` |
| `diar.block` → `diar.block.done` | вход: `block_index`, `offset_ms`, `pcm_fd` (**memfd** с PCM блока ≤ 20 мин float32, передаётся `SCM_RIGHTS`; признаки/аудио не касаются ФС) → выход: `segments: [[start_ms, end_ms, "S3"], …]` глобальные метки после связывания |
| `diar.finish` → `diar.finished` | финальная консолидация (повторная кластеризация кумулятивных центроидов всех блоков, ⚖ см. §6.4), `speakers_total` |
| `diar.cancel` | — (плюс `SIGKILL` группы по таймауту 1 с) |

Таймаут блока: `max(120 с, 3 × длительность блока × RTF_ожид)`; превышение → `failed{code: limit}` для разделения (встреча завершается без разделения, не падает).

### 4.5 Что остаётся без изменений
`record.*`, `recognize`, `transcribe.file`, `audio.close`, `model.*`, `measure` — ни одного нового поля (MJ-3 п. 1). Тест: `record.start` с полями встреч → поля отброшены, монитор не открывается никогда (T-212).

---

## 5. Захват звука

### 5.1 Автомат `MeetingController` (GUI) и «кто за что отвечает»

| Решение | Где принимается | Как доходит до звука |
|---|---|---|
| Можно ли начать (диск ≥ 600 МБ, каталог записываем, вторая запись не идёт, сессия `known && active && !locked`, функция включена, политика) | GUI, при открытии карточки старта и **повторно** перед `prepare` и перед `open` | `prepare` → `open` (двухфазный старт, §4.2) |
| Старт/продление/продолжение — только нажатием (BL-2) | обработчики кнопок (карточка старта, предложение, плашка, трей, уведомление); у `MeetingController.start()` единственный вызывающий — `MeetingBridge.on_start_clicked` (тест фиксирует граф вызовов) | команда с `admission_mono_ms` |
| Пауза по блокировке/неизвестному состоянию (BL-5) | `SessionMonitor.changed` → `locked or not known` → `pause(reason=lock)` ≤ 1 с | `pause` → потоки закрыты; `resume` только нажатием «Продолжить» |
| Пауза по индикации (BL-4) | `IndicatorGuard` (трей не зарегистрирован **и** плашка не mapped > 1 с) → `pause(reason=indicators)`; возврат → `resume` автоматически с отметкой | как выше |
| Аренда (BL-4 п. 5) | GUI шлёт `keepalive` раз в 1 с из `QTimer` GUI-потока; захватчик: 5 с без него → самопауза | `autopaused{lease}` → после возврата GUI авто-`resume` |
| Лимит 2 ч / продление / потолок 4 ч | GUI по `recorded_ms` из `alive` (+ локальные часы между ответами); за 5 мин — уведомление + кнопка; потолок — константа `MEETING_HARD_LIMIT_MS = 14_400_000` в коде | `stop` |
| Пауза > 60 мин → завершение | GUI-таймер | `stop` |
| Диск < 200 МБ | GUI (`statvfs` каталога встреч раз в 10 с) **и** захватчик (`free_mb` в `alive`; при ошибке записи — `autopaused{disk-write}`) | `stop` с сохранением |
| Сон (`PrepareForSleep=true`) / смена сеанса (`Active=false`) | `SessionMonitor` | `stop` немедленно (сегменты `fsync` ≤ 10 с назад; Could — delay-inhibitor login1 на 5 с) |
| Смена/потеря микрофона (У44) | захватчик (`device_change` от `PulseStreamSource`) → `track{mic, paused, kind}` | дорожка микрофона закрыта; GUI показывает уведомление/плашку с «Продолжить с „…“» → `track.resume` |
| Исчезновение цели far (звонок закончился, программа закрылась) | захватчик (`sink-input removed`) → `track{far, ended, target-removed}` | far закрыт; микрофон пишется дальше; отметка |
| Выход из программы/SIGTERM | GUI: диалог / обработчик SIGTERM → `stop` | захватчик при смерти GUI — PDEATHSIG-путь |

Состояния `MeetingController`: `idle → preparing → opening → recording ⇄ paused(reason) → stopping → stopped`; `recording` имеет подсостояния дорожек (`mic: live|paused|lost`, `far: live|ended|none`). Таймер плашки считает `recorded_ms` локально между `alive` и корректируется по ним (расхождение > 2 с → предупреждение в журнал кодом).

### 5.2 Две дорожки (⚖ D1: B′ как основной путь)

| Вариант карточки старта | Дорожка mic | Дорожка far | Условия |
|---|---|---|---|
| 1 «Звонок в <программа>» | `PulseStreamSource(mic_device)` — тот же код, что у диктовки (второй читатель того же источника, A3) | **B′:** `pa_stream` на `<sink>.monitor` того sink, где живёт sink-input программы, с `pa_stream_set_monitor_stream(sink_input_index)` **до** `connect_record`; proplist фиксированный (`application.name="Astra Voice"`, `media.name="Запись встречи"`, `astra-voice.run=<token>` MJ-3 п. 4) | цель — `(index, object.serial, pid-verified)` из детектора/`call.query`, сверенная повторно в `prepare` (MN-9) |
| 2 «Встреча в комнате» | как выше | нет | — |
| 3 «Весь звук компьютера» | как выше | **A:** `pa_stream` на `@DEFAULT_MONITOR@`-эквиваленте: монитор текущего умолчального sink; подписка `SERVER` → смена умолчания → переоткрытие на новый монитор с отметкой (MN-4) | явная строка в карточке |
| 1 при сервере без B′ (PulseAudio без `set_monitor_stream`, редкий случай) | как выше | откат на **A** с честной строкой A30 в карточке и шапке (MN-2); `whole_output=true` в `meeting.json` | `prepared{far_available: false, far_reason: monitor-stream-unsupported}` |

**⚖ Почему B′, а не C (`pw-record --target`).** B′ — тот же `_PulseAsync`, что уже в продукте, без подпроцесса, pipe и парсинга; работает на pipewire-pulse и на PulseAudio (R-A3); потоки обеих дорожек — в одном процессе с одним серверным временем (`pa_stream_get_time`) — это упрощает timeline (§5.3); проверено на стенде (84–87 дБ). C остаётся **резервом** (D1-fallback): если на ноутбуке B′ не следует за sink-input при переезде на BT (A2) и переоткрытие даёт разрыв > 1 с — M2 добавляет `PwRecordSource` (подпроцесс `pw-record --target <object.serial> -P '{stream.capture.sink=true node.dont-reconnect=true node.dont-fallback=true}'`, raw s16 в pipe, +1 дн.). Null-sink/loopback с восстановителем (E7) — **не делаем** ни при каком исходе v1.1: при провале обоих — вариант 3 с A30 (решение по MJ-4 п. 2 отложено в беклог).

**Исключительность и цель (MJ-4 п. 4, MN-9).** Цель привязана к `(sink_input.index, object.serial, server cookie)`; `sink-input removed` с нашим индексом → far `ended` с отметкой, **никакого** перехода на монитор умолчания; новый sink-input **того же проверенного pid** в течение звонка (Electron заводит узел на поток, S1.2) → переоткрытие на него с отметкой `reopened` (допустимо по MJ-4 п. 4); другой процесс — только новым выбором человека (в v1.1 — только остановка/новая встреча).

### 5.3 Дрейф дорожек (⚖ выравнивание по времени, без ресемплинга)

Факт SP1: −154…−215 ppm/ч между дорожками (≈ 0,55–0,77 с/ч) даже на стенде; на ноутбуке добавится реальный дрейф карт. Требование продукта — таймкоды ±1 с и корректная сборка реплик двух дорожек в одну ленту; **ресемплинг звука не нужен ни для ASR, ни для разделения** (каждая дорожка обрабатывается отдельно и независимо от другой). Поэтому:
1. **Timeline вместо счётчика.** Каждая дорожка раз в 1 с пишет в sidecar пару `(отсчётов_записано, t_server_mono_ms)`, где время — `pa_stream_get_time()` при `PA_STREAM_INTERPOLATE_TIMING|AUTO_TIMING_UPDATE` (уже используются `PulseStreamSource`), с поправкой на задержку; при недоступности — `CLOCK_MONOTONIC` чтения с пометкой `clock: local`.
2. **Отображение** `meetings/timeline.py`: отсчёт → t_mono (кусочно-линейно по точкам сегмента) → `recorded_ms` (по `intervals` встречи). Ошибка таймкода ограничена шагом точек и джиттером чтения (≤ 50 мс), не дрейфом; за 4 ч накопления нет.
3. **Эхо-гейт** (§6.2) ищет локальный лаг mic↔far кросс-корреляцией в окне ±300 мс вокруг лага, предсказанного timeline, — дрейф внутри окна 20 с (≤ 5 мс) несущественен.
4. **Контроль:** отчёт `drift_report` (из спайка) как `tools/validate meeting-drift` на 60-мин виртуальном прогоне стенда: ошибка таймкода по бипам ≤ 50 мс.
**Альтернатива (ресемплинг far по оценке ppm, как предлагал протокол S1.5b):** +DSP (`resample.py` есть, но линейный/полифазный ресемплинг 2 ч float32 — CPU и память), и всё равно нужен timeline для recorded_ms через паузы. Не вижу требования, которое он закрывает; оставляю как резерв на случай, если SP2-гейт на реальном звуке потребует выравнивания точнее 5 мс на длинных окнах.

### 5.4 Смена устройств
- **Микрофон** (гарнитура отключена, умолчание переехало): `PulseStreamSource` уже отдаёт `device_change(kind=switched|device-lost, label)` ≤ 0,7 с (`DEVICE_CHANGE_BUDGET_S`) и **не читает** новое устройство (У44) → дорожка mic `paused`, GUI: уведомление + кнопки на плашке «Продолжить с „<label>“» / «Остановить запись»; `mic_gap_start/end` в отметках; far продолжается.
- **Вывод** (гарнитура ↔ колонки): вариант 1 — B′ привязан к sink-input; если pipewire-pulse переносит монитор-поток вместе с sink-input (A2) — ничего не делаем; иначе по `sink-input changed (sink ≠ прежний)` переоткрываем монитор на новом sink с `set_monitor_stream` (разрыв ≤ 1 с, отметка `sink-moved` только в журнал). Вариант 3 — по `SERVER` (default sink changed) переоткрываем монитор умолчания (MN-4).
- **Перезапуск звуковой службы** (контекст `FAILED/TERMINATED`): обе дорожки `lost` → `autopaused{audio-failed}`; GUI: пауза с причиной, «Продолжить» нажатием (BL-2: не возобновляем сами, т. к. это не потеря индикации, а потеря звука).

### 5.5 Пауза, блокировка, сон — что именно происходит со звуком
- `pause(reason)`: `pa_stream_disconnect` обеих дорожек ≤ 250 мс (`CLOSE_DEADLINE_S`), сегменты закрываются с `fsync`; `pactl list short source-outputs` не содержит наших потоков (T-206/T-218 на стенде); `pa_context` остаётся (introspection — допустимо, BL-1 п. 1).
- Блокировка: `SessionMonitor` → `locked=True` (LockedHint/`Lock`/`ScreenSaver.ActiveChanged`) **или** `known=False` → `pause(lock)` ≤ 1 с; после разблокировки (`Unlock`+повторное чтение) — уведомление + «Продолжить» на плашке/в трее/в уведомлении; **авто-`resume` нет**; 60 мин → `stop`.
- Fly: `LOCK_SIGNAL_BY_SESSION = {KDE: login1, FLY: none}` → при `FLY` состояние блокировки считается **неизвестным** → старт отклонён текстом F18.4 (м). После SP-F — `FLY: login1` или `FLY: screensaver` (одна строка таблицы + тест), без правок автомата (D24).
- Сон: `PrepareForSleep(true)` → `stop` (не пауза: после пробуждения продолжение запрещено PRD); уведомление после пробуждения. Смена сеанса: `Active=false` → `stop`.

### 5.6 Проверки перед стартом и «ноль потоков»
Порядок при нажатии «Начать запись» (все проверки — в GUI, до `prepare`; повтор сеанса — перед `open`):
1. функция включена, политика не `off`; вторая запись не идёт; модель — не требуется для записи;
2. сессия: `known && active && !locked && !preparing_for_sleep`, иначе отказ без создания чего-либо (BL-1, BL-5 п. 4);
3. диск: `statvfs(meetings_root)` ≥ 600 МБ; каталог встреч записываем (`ensure_private_dir`, пробный `open(O_EXCL)` + `unlink`);
4. сетевая ФС (`statfs.f_type` ∈ {NFS, CIFS/SMB2, FUSE-сетевые}) → предупреждение в карточке (П33), флаг в `meeting.json`, событие кодом;
5. `prepare` → захватчик: `pa_context_get_server_info` → Server String не `unix:` → `server_local=false` → предупреждение (П33); цель far сверена; `far_available`;
6. повторная проверка сессии (п. 2) → `open{admission_mono_ms=now}`; захватчик открывает потоки ≤ 1 с; `started` → GUI ещё раз сверяет сессию — заблокировали в эту секунду → немедленный `pause(lock)`.
Гарантия BL-1 тестируется фейком libpulse со счётчиком `stream_connect_record` от показа предложения/карточки до `open` → 0; и на стенде `pactl list short source-outputs` в окне предложения 50 с (T-201).

### 5.7 Бюджеты захвата
CPU ≤ 3 % ядра на обе дорожки (S1.5c: `PulseStreamSource` 3,1 % на VPS с `fragsize` 20 мс — для встреч `fragsize` 100 мс и чтение блоками 1 с снижают нагрузку ×3–5; у диктовки параметры не меняются); RSS ≤ 60 МБ, рост ≤ 20 МБ/ч; диск 230 МБ/ч; `fsync` раз в 10 с; потеря при `kill -9` ≤ 10 с (S1.5d).

---

## 6. Расшифровка и диаризация блоками

### 6.1 Конвейер задания (`meeting-transcribe`)

```
job.start ─► 0 проверка каталога/поколения, timeline, повреждённые сегменты → отметки
          ─► 1 VAD (Silero, по паузам) по каждой дорожке → список реплик (отсчёты)
          ─► 2 эхо-гейт (только mic при наличии far): NCC mic↔far → echo=true → без ASR
          ─► 3 ASR реплик в хронологическом порядке обеих дорожек → utterance{provisional} → GUI (кредиты)
          │     чекпойнт-текст каждые 32 реплики / 30 с аудио
          ─► 4 разделение (если включено и модели есть): блоки 20 мин целевой дорожки → meeting-diar (memfd)
          │     → метки по блокам (provisional) → diar.finish (консолидация) → labels{final}
          ─► 5 финализация: transcript.json, checkpoint{diar: done}, job.done → GUI: звук удалить (retain=0)
```

| Этап | Как именно | Память/время | Отмена/резюм |
|---|---|---|---|
| 0 | `realpath(dir).is_relative_to(meetings_root)`; sidecar'ы → timeline; сегмент с `closed=false` → восстановление по физической длине; несогласованный/битый → отметка «кусок записи повреждён: 0:40:00–0:40:10» (F18 исключения), не падение | — | — |
| 1 VAD | `worker/vad.py` (порог пауз 0,35, минимальный сегмент 2 с) окнами по 60 с с перекрытием 1 с; **жёсткий предел реплики 24 с** (окно GigaAM): длинная речь без пауз делится в точке минимальной вероятности речи внутри [18; 24] с | Silero RTF ≈ 0,01 | детерминирован → при резюме даёт те же границы; реплики с `end_ms ≤ done_until_ms` пропускаются |
| 2 эхо-гейт | порт `sp2_analyze.ncc_numpy`: обе дорожки → 4 кГц, NCC в окне реплики, лаги 0…400 мс вокруг предсказанного timeline; `max_ncc ≥ θ_echo` (0,35 по SP2, константа с калибровкой на ноутбуке) **и** перекрытие с речью far ≥ 50 % → `echo=true`; защита двойного разговора: энергия mic в реплике выше оценки эха на ≥ 6 дБ → оставить (S2.2 — целостность «Я» важнее) | numpy, ≈ 1 % от ASR | флаг `echo_gate=false` выключает (резерв «только гарнитура») |
| 3 ASR | `OnnxAsrEngine.transcribe(audio, cancel)` (тот же движок, что у диктовки; GigaAM v3 e2e, `threads=2`); дедлайн на реплику `INFER_TIMEOUT_S` → «[не распознано]» + отметка, задание продолжается; mic → `speaker=me`, far → `others` (временно) | модель ≈ 450–700 МБ RSS; буфер реплики ≤ 24 с | `RunOptions.terminate` ≤ 1 с; чекпойнт `done_until_ms` по дорожкам |
| 4 разделение | целевая дорожка: far (вариант 1/3), mic (вариант 2 «комната»); блоки по **20 мин** (A2: 737 МБ против 957 МБ у 30 мин), границы сдвигаются к ближайшей паузе VAD ≥ 0,5 с в пределах ±90 с; PCM блока float32 → `memfd_create` → `SCM_RIGHTS` ребёнку; результат — глобальные метки `[start,end,S<n>]`; присвоение реплике — по максимальному перекрытию (≥ 30 % длительности реплики; иначе ближайший сегмент) | ребёнок ≤ 1,5 ГиБ (`RLIMIT_AS`); блок 20 мин float32 = 77 МБ | крах/лимит ребёнка → `diar_status=failed`, встреча `done` с подписью «Собеседники» и кнопкой «Разделить заново»; резюм — заново по всей дорожке (BL-3) |
| 5 | `transcript.json` (атомарно), `checkpoint.json`, `done{stats}`; GUI: `meeting.json` (`transcription`, `audio.present=false` при `retain_days=0` → удаление `audio/`) | — | `done` с устаревшим `generation` отбрасывается (MJ-14) |

**Совокупные пределы (MJ-10 п. 4):** реплик ≤ 4 ч / 0,5 с = 28 800; блоков ≤ 12; общий потолок стены = 2 × длительность встречи + 10 мин → `failed{limit}`; очередь реплик к GUI — кредиты (64/32), без кредитов транскрайбер блокируется между репликами (память не растёт — S4.12); `progress` ≤ 2/с.

**Очередь заданий (GUI `MeetingJobs`):** одно задание за раз, FIFO по времени остановки; «Расшифровывать сразу» ставит задание при `stopped`; при старте программы — все встречи в `recorded|transcribing|waiting_memory` с незавершённым чекпойнтом (кроме отменённых/удалённых — BL-2 п. 2, MJ-14) возвращаются в очередь. Запись новой встречи во время расшифровки прошлой — допустима (разные процессы).

**Память и приоритет:** перед стартом задания GUI проверяет `MemAvailable ≥ min_ram_mb(GigaAM) + (900 МБ, если разделение) + 200 МБ` (формула §8 PRD); иначе состояние «Ждёт памяти», повтор раз в 60 с; транскрайбер повторяет ту же проверку перед `model.load` и перед каждым блоком разделения (`waiting{memory}`), `MemoryError` → `failed{memory}` → «Ждёт памяти» (звук цел). Процесс — `nice 19`, ORT 2 потока; диктовка — отдельный процесс (A9); резерв: `meeting.job.yield` (пауза между репликами, пока идёт диктовка) — выключен, включается, если SP4-C1 покажет p95 > 0,8 с.

### 6.2 Таймкоды и слияние дорожек
Реплика получает `start_ms/end_ms` в оси `recorded_ms` через `timeline.py` (§5.3): отсчёт → серверное монотонное время → recorded (вычет пауз по `intervals`). Лента карточки — слияние двух дорожек по `start_ms`; формат «[ч:мм:сс]»; точность ограничена шагом точек timeline (1 с → интерполяция ≤ 50 мс) и дрейфом внутри секунды (пренебрежимо). Паузы и пропуски — отметки `marks` → строки в таблице и шапке.

### 6.3 Движок разделения (⚖ D14/D19 с учётом решения п. 12)
- **sherpa-onnx `OfflineSpeakerDiarization`** (решение владельца 14.10: блоки + связывание, не diar_v2): сегментация `pyannote-segmentation-3.0` (MIT), эмбеддер **3D-Speaker CAM++** (Apache-2.0; A7 — меньше ложных дикторов, время то же; WeSpeaker CAM++ LM — альтернатива при DER хуже > 2 п.п. на калибровке, тогда NOTICE с CC-BY-4.0), `FastClustering(num_clusters=-1, threshold=θ)`; `num_threads=2`, `provider="cpu"`.
- **Порог θ и τ связывания — профиль эмбеддера** (`DIAR_PROFILES = {"3dspeaker-campplus": (θ, τ), "wespeaker-campplus-lm": (1.05, 0.6)}`): для 3D-Speaker θ **калибруется на ноутбуке** (M0: A6 с 3D-Speaker + блок F 5 мин настоящего DION); до калибровки — 0,9 (по форме кривой A6: 0,9 → 7 дикторов при 4 истинных на синтетике, 1,05 → 4–5; 3D-Speaker на том же θ дробит меньше). Настройки «мягче/строже» в UI нет; человеческая правка — «Это один человек».
- **Упаковка:** колесо `sherpa-onnx` (пин sha256 в `packaging/wheels.lock`, vendor в `/usr/lib/astra-voice/vendor`), 4 ELF + свой `libonnxruntime.so` → **ELF-гейт CI: 3 → 7** (`test_deb_elf_count.py`), запись в `elf-audit`, NOTICE (Apache-2.0 sherpa, MIT pyannote-seg, Apache 3D-Speaker); AppImage — те же файлы. ЗПС-подпись (US-12.3, v1.2) включает эти ELF. `register_custom_ops_library` не вызывается; провайдер только CPU (инвентарь показал `AzureExecutionProvider` в списке — не используется, тест проверяет `providers=["CPUExecutionProvider"]`).

### 6.4 Блоки и связывание (⚖ отличие от спайка)
1. Блок → sherpa → локальные метки `L1..Lk` + сегменты.
2. **Центроиды локальных дикторов** считаются `SpeakerEmbeddingExtractor` по речи каждого локального диктора (до 60 с самых длинных сегментов, конкатенация; sherpa внутренние эмбеддинги не отдаёт — как в спайке `centroid-repeat-extractor`, +10–15 % времени).
3. **Связывание с глобальными:** кандидаты — **все** глобальные дикторы, виденные ранее (кумулятивные центроиды, взвешенные длительностью), а не только предыдущий блок (⚖ отличие от `link_blocks.py`: человек, молчавший один блок, не должен стать новым спикером); косинус ≥ τ; назначение один-к-одному — максимум пар ≥ τ, затем сумма косинусов (венгерский, порт `der.maximize_assignment`, ≤ 16 дикторов → перебор не нужен); несопоставленные → новые номера по порядку первого появления.
4. После каждого блока — `labels{provisional}` → GUI переразмечает (В4: «имена спикеров уточняются до конца»).
5. `diar.finish`: **консолидация** — агломеративная кластеризация всех кумулятивных центроидов глобальных дикторов тем же θ → слияние «расщеплённых» (S4≡S7) → `labels{final}`; после `final` номера заморожены, имена доступны. Номера не перенумеровываются (UX §9 п. 15): объединённые при консолидации получают меньший из номеров, остальные сохраняют свои (дыры допустимы).
6. Признаки: только в памяти `meeting-diar`; процесс завершается с заданием; чекпойнт хранит `diar.status` и `block_ms`, **никаких векторов** (схема-тест: в `checkpoint.json` нет массивов чисел длиной > 16).
**Альтернатива:** связывание только по соседнему блоку (как в спайке) — проще, но даёт лишних дикторов при длинном молчании; **альтернатива 2:** без консолидации — меньше переразметки в конце, но «6 дикторов при 4» (Р18-4) чаще. Качество связывания измеримо на корпусе SP4 (`der.py`, S4.2) — тест M7.

### 6.5 Когда разделение не делается
`meeting_diarization=off` (политика) или настройка выключена → `speaker=others` («Собеседники»/«Участники»); модели не установлены → так же + строка «Разделение по голосам: модели не установлены» и кнопка «Установить»; вариант 2 «комната» → разделяется mic, подписи «Я» нет; крах ребёнка → как выше с «Разделить заново».

---

## 7. Шаблоны/саммари и корпоративный ИИ (контракт)

### 7.1 Локальные шаблоны (`meetings/templates.py`, без сети)

| Шаблон | Правило | Вход |
|---|---|---|
| Стенограмма (по умолчанию) | реплики по порядку `start_ms`; подряд идущие реплики одного говорящего с разрывом ≤ 2 с склеиваются в одну; подпись = имя или «Спикер N» (`②`-номер в UI, в файле — «Спикер 2 (Иван Петров)» → по макету); таймкод «[ч:мм:сс]» у каждой; отметки — строками `— Пауза: экран был заблокирован (0:12:03)`; шапка: название, дата, начало, длительность, участники, предупреждения (сетевой диск/звук через другой компьютер/весь звук компьютера) | `transcript.json` (без `echo=true`), `speakers.json`, `meeting.json` |
| Я и собеседники | только по дорожкам: «Я»/«Собеседники», без номеров | те же |
| Только текст | абзацы по сменам говорящего, без подписей и таймкодов | те же |
| Статистика встречи | длительность; по каждому говорящему — минуты и доля (сумма ровно 100 % методом наибольших остатков), число реплик; объединённые спикеры — как один | те же |
| Свой шаблон оформления (Could) | файл пользователя/организации `~/.config/astra-voice/meeting-templates/*.md` с полями `{название} {дата} {длительность} {участники} {стенограмма}` — `str.replace` по белому списку (MN-12) | — |

Форматы `.md`/`.txt`; `.odt`/`.docx` (Could внутри US-13.9) — `zipfile` + `xml.etree`: минимальные `content.xml`/`document.xml` с экранированием (MN-15), без зависимостей; оценка 1 дн., делается только при остатке ёмкости. Имя файла — из названия после санитизации (MN-3), файл 0600, `open(..., "x")`/замена по подтверждению. Результаты внутри программы — `exports/`; «Сохранить как…» — наш шаг D4 → системный диалог.

### 7.2 Шаблоны с ИИ — контракт (`meetings/ai_client.py`)

| Аспект | Контракт |
|---|---|
| Включение | только если адрес задан (настройка или политика) и не `off`; по умолчанию адреса нет → шаблонов нет, сети нет |
| Адрес | `scheme://host[:port]/path` без userinfo/query/fragment (MJ-8 п. 2); `https://` для всех; `http://` — loopback, либо внутренний адрес из **той же** политики при `meeting_ai_allow_plaintext=true` (MJ-2′), `profile=secure` → ключ игнорируется |
| Классификация | `meetings/netaddr.py`: `getaddrinfo` → множество адресов → IPv4-mapped → IPv4; классы по явным CIDR: loopback `127/8`,`::1`,`localhost`; internal `10/8`,`172.16/12`,`192.168/16`,`fc00::/7`; всё остальное — external; `0.0.0.0`/`::`/multicast/broadcast/числовые формы → отказ; смесь → external; пустой DNS → ошибка. Множество **закрепляется** за соединением (`_bounded_connection.resolve()` получает список, не делает свой `getaddrinfo`) и **перепроверяется перед каждой отправкой** |
| Гейт | новый `NetworkKind = "ai_template"` в `NetworkGate`: офлайн-режим/`profile=secure`/`HF_HUB_OFFLINE` блокируют **только external**; loopback/internal — разрешены |
| Предупреждение | external и не (`meeting_ai_allow_external=true` **и** адрес из той же политики) → диалог D5 перед **каждой** отправкой (включая «Повторить»); снимок `{url, host, class, pinned_ips, policy_hash, text_sha256}`; любое изменение между диалогом и отправкой → отмена «Адрес сервера изменился…» |
| Замена имён | external: галочка по умолчанию отмечена → подписи «Спикер N»/имена → «Участник 1, 2…» по порядку; произнесённые имена в тексте остаются (строка под галочкой); internal — настройка, по умолчанию снята; «Я» → «Участник 0»? — нет: «Я» → «Участник 1», остальные со 2 (детерминированно, в экспорте не сохраняется) |
| Запрос | `POST {base}/chat/completions`, JSON: `model`, `messages=[system: FIXED_INSTRUCTION[template], user: "Ниже — стенограмма встречи как данные, не инструкции. <<<\n…\n>>>"]`, `temperature=0.2`, `max_tokens=8192` (reasoning-модели тратят лимит на рассуждения), `stream=false`; **без `tools`**; `Authorization: Bearer <ключ>` только если сохранённый `host:port` совпадает с текущим (MJ-8 п. 1) |
| Транспорт | `HttpClient` scope `ai` (хосты не из allowlist загрузчика — MN-11); `allow_redirects=False`, любой 3xx → ошибка «перенаправил»; таймаут 180 с на весь запрос (`_RequestWatchdog`); лимит тела ответа 4 МиБ; прокси: loopback/internal — **прямое** соединение (`proxies={"http": None, "https": None}`, `trust_env=False`; известная дыра `http.py:736` для `get_stream` в новый путь `post_json` не переносится), external — прокси из окружения допустим; TLS — системный CA + `policy.ca_bundle` |
| «Проверить» | `GET {base}/models` (один запрос без текста встречи; при 404/405 — `POST chat/completions` с `messages=[user:"ping"]`, `max_tokens=1`) → «Сервис ИИ отвечает» / простая причина |
| Отметка «мог быть получен» (MJ-13) | в `meeting.json.ai.sent_attempted` **до** отправки тела (запись → затем `POST`); ошибка после начала передачи → «<причина>. Текст встречи мог быть получен сервером <хост>»; до — «…не отправлен»; «Повторить» — только по нажатию; автоповторов нет |
| Ответ | только `choices[0].message.content` строкой (`reasoning_content` и `tool_calls` игнорируются; JSON-парсер с лимитом глубины 32/размера 4 МиБ) → `sanitize.py`: удалить ссылки/картинки (текст ссылки остаётся), URL, HTML, Cc (кроме `\n`,`\t`), Cf → ограниченная структура (H1–H3, списки, абзацы) → `ai/<template>-<n>.json` + рендер `.md` программой; метка «Сделано сервисом ИИ по тексту встречи — проверьте по стенограмме. Ссылки и картинки из ответа убраны»; пустой ответ → «Сервис ИИ вернул пустой ответ»; `context_length`-ошибка сервера (400 с `context_length_exceeded`/`maximum context`) → «Встреча слишком длинная для этой модели» |
| Ошибки → тексты | таблица состояний спеки §3.8: `err-net` (до тела) · `err-auth` 401/403 · `err-time` 429 · `err-redirect` · `maybe-sent` · `addr-changed` · `too-long` · `empty` · `offline` |
| Журнал | одна строка на запрос: `template`, `host` (для `https`) **или только класс** (при plaintext — MJ-2′ п. 5), `class`, `plaintext`, `http_status`/код, `duration_ms`; без тела, URL, заголовков |
| Ключ | `platform/secret_store.py` (§2.2); смена адреса → ключ очищается с уведомлением «Введите ключ для нового адреса» |

Astra Cowork не нужен (A33). Совместимость с конкретным сервером (A33, «тестовый адрес GLM 5.3 — потом посмотрим»): проверка на поддельном сервере в CI + один живой прогон на адресе владельца перед релизом (вопрос §12).

---

## 8. Безопасность и приватность — сверка с T1

| Требование T1 | Где в архитектуре | Проверка |
|---|---|---|
| **BL-1** ни потока, ни буфера до нажатия; перепроверка сеанса перед потоком | детектор — только подписка/introspection в `meeting-capture` (§4.2); двухфазный `prepare`/`open` с `admission_mono_ms` ≤ 1 с (§5.6); PCM-буферы создаются в `open`; GUI/капчер не знают `pa_simple`; `warm_up` для встреч не вызывается | T-200 (фейк: счётчик `stream_connect_record` = 0 до `open`), T-201 (стенд `pactl`), I3 |
| **BL-2** старт/продолжение/продление — только нажатием; извне входов нет; после сбоя не возобновлять | единственный вызывающий `MeetingController.start()` — обработчик кнопки (тест графа вызовов); `--role` и env только у дочерних процессов, у GUI нет аргумента/переменной/глагола сокета/D-Bus для старта; политика `prompt` — разрешение; капчер без авторестарта; потолок 4 ч — константа | T-202, T-203, I2, I7 |
| **BL-3** признаки не касаются диска | `meeting-diar` без путей записи (pipe/memfd), `TMPDIR` в несуществующий путь, `cache_dir` не задаётся; чекпойнт — схема без массивов; резюм разделения — заново; имена после `final`; повторное разделение сбрасывает имена с предупреждением | T-204, T-205 (`strace -f`), grep-тест `memmap|np\.save|tofile|pickle|shelve|marshal` по `worker/meeting_diar.py`, I5 |
| **BL-4** индикация не зависит от настроек; потеря → пауза; аренда | плашка/трей не читают `pill_enabled`, выключателя нет (grep-тест по QML/settings); `IndicatorGuard` → `pause(indicators)` через 1 с, авто-`resume` при возврате; аренда `keepalive` 1 с / 5 с → самопауза в капчере | T-206, T-207, T-225, I6 |
| **BL-5** блокировка/неизвестно → пауза, продолжение нажатием | `SessionMonitor` → `pause(lock)` ≤ 1 с; Fly: `LOCK_SIGNAL_BY_SESSION[FLY]=none` → `known=False` → отказ старта; 60 мин → `stop` | T-218 |
| MJ-1 политика fail-closed, `allow_external` привязан к адресу | `meetings/policy.py` на сырых строках; INVALID/небезопасный файл → набор «закрыто»; `allow_external` действует только с `meeting_ai_endpoint` той же загрузки | T-208, T-227 |
| MJ-2, MJ-2′ адрес/`http`/прокси/редиректы/7-й ключ | `netaddr.py`, `ai_client.py` (§7.2) | T-209, T-210, T-211, T-219, T-235…T-237 |
| MJ-3 монитор только для встреч; `record.start` без новых полей; второй читатель микрофона; свои потоки по индексу клиента | схемы `ipc.py` не трогают диктовку; `MonitorStreamSource` только в роли `meeting-capture`; mic — второй `PulseStreamSource`; детектор исключает `pa_context_get_index()` своих контекстов + маркер `astra-voice.run` | T-212, T-238 |
| MJ-4 без правки графа; потеря цели ≠ расширение | B′/A без `load-module`/`move-sink-input`/`set-default-*` (grep-тест: этих строк нет в коде продукта); `sink-input removed` → far `ended`; перецеливание — только тот же pid | T-213, T-214 |
| MJ-5 hardening fail-closed; второй воркер | `harden_process(strict=True)` для ролей встреч; три процесса с раздельными `RLIMIT_AS`/`oom_score_adj` | T-216, T-217 |
| MJ-6 | не применяется (исключение не принято) | — |
| MJ-7 только класс программы | `apps.py` → `app_class`; сырые имена живут в памяти детектора; журнал/статистика/UI — класс | T-215, T-239 |
| MJ-8 ключ: хост, заголовок, без тихого понижения | `secret_store.py` (§2.2) | T-220 |
| MJ-9 модели через каталог, hardened-воркер; sherpa → ELF-перечень | записи `kind=diarization`; ELF-гейт 7; `elf-audit` | T-221 |
| MJ-10 отдельная команда, окна, поток сегментов, путь, пределы | `meeting.job.*`, окна VAD, кредиты, `realpath`, лимиты (§6.1) | T-217, T-233 |
| MJ-11 ответ ИИ — чужой текст | `sanitize.py`, рендер из структуры, метка | T-228, T-222 |
| MJ-12 журнал по схеме | `meetings/events.py` + lint-тест; `SECRET_FIELDS`; stderr нативных библиотек в ролях встреч → `/dev/null`; диагностика по белому списку | T-229, T-215 |
| MJ-13 «мог быть получен» | `sent_attempted` до тела; без автоповторов | T-230, T-219 |
| MJ-14 удаление согласовано | надгробие → отмена заданий → `rmtree`; поколение | T-231 |
| MN-1…MN-18 | MN-1 `Text.PlainText`/лимиты; MN-2 строка A30 + `whole_output`; MN-3 id каталога/имя файла; MN-4 переоткрытие монитора; MN-5 `statvfs` 10 с; MN-6 `lstat`/`dir_fd`/retention по датам; MN-7 окно W1 500 мс/фокус; MN-8 документ (Baloo, бэкапы); MN-9 pid-проверки детектора (`declared-verified`/`declared`/`mismatch`); MN-10 фиксированные proplist; MN-11 scope `ai`; MN-12 NFC/Cf, `str.replace`; MN-15 экспорт XML/0600; MN-16 таймаут на весь запрос; MN-17 строка про mute; MN-18 обнаружение сети/ФС + журнал кодом | T-222…T-224, T-232 |
| Принятые риски П26–П33 | без изменений; П33 — предупреждение + флаги в `meeting.json` без адресов | — |
| **Новое для T2 (поверхности, которых в T1 не было):** (1) `memfd` + `SCM_RIGHTS` между транскрайбером и ребёнком; (2) кредитный поток и его отказ; (3) `LOCK_SIGNAL_BY_SESSION`; (4) консолидация меток (не признаки, но пере-разметка); (5) `GET /models` в «Проверить» | помечены в DoD M4/M7 как «Security surface: да» | T2 на диф |

---

## 9. Отказы и инварианты

### 9.1 Инварианты (проверяются тестами и ревью)

| # | Инвариант | Гарант |
|---|---|---|
| I1 | Поток записи у звуковой службы существует ⇔ состояние `recording` **и** аренда свежа (< 5 с) **и** последнее известное состояние сеанса допустимо | капчер (аренда), GUI (сеанс/индикация), двухфазный старт |
| I2 | Процесс захвата никогда не перезапускается автоматически; после его смерти встреча `interrupted` | супервизор `auto_restart=False` |
| I3 | Одновременно ≤ 1 записи, ≤ 1 процесса захвата, ≤ 1 задания расшифровки, ≤ 1 процесса транскрайбера | `MeetingController`, `MeetingJobs` |
| I4 | Звук существует только в `meetings/<id>/audio/`; удаляется только `MeetingStore` после `done` (по сроку) или удаления встречи; при `failed` — никогда | store |
| I5 | Ни один процесс встреч не записывает признаки голоса: у `meeting-diar` нет файлового вывода; схема чекпойнта без массивов | конструкция + `strace`/схема-тест |
| I6 | `recording` ⇒ (трей зарегистрирован ∨ плашка mapped) с грейсом 1 с, иначе `paused(indicators)` | `IndicatorGuard` |
| I7 | `recorded_ms ≤ 14 400 000`; продление только нажатием, ≤ 2 раз по 3 600 000 | константы, GUI |
| I8 | Сетевых соединений у процессов встреч нет; у GUI — только `ai_client` по нажатию | импорт-гейт (`requests`/`socket.connect` отсутствуют в ролях), `tcpdump` живьём |
| I9 | В журнале/статистике/диагностике модуля встреч нет свободного текста | lint-тест, валидатор `Stats`, белый список диагностики |
| I10 | После `labels{final}` номера спикеров не меняются; имена — только текст в `speakers.json` | store/UI |
| I11 | События с устаревшим `generation` отбрасываются; удалённая встреча не воссоздаётся | супервизоры, `MeetingJobs` |
| I12 | Негодное значение любого `meeting_*` → значение «меньше записи/передачи»; INVALID/небезопасный файл → набор «закрыто» | `meetings/policy.py` |
| I13 | Таймкод реплики вычисляется из timeline, никогда из счётчика отсчётов | `timeline.py` |
| I14 | `meeting.json` пишет только GUI; `transcript.json`/`checkpoint.json` во время задания — только транскрайбер; сегменты — только капчер | единственный writer на файл |

### 9.2 Таблица отказов

| Ситуация | Кто замечает | Действие | Состояние/текст (PRD) | Тест |
|---|---|---|---|---|
| Звуковая служба не отвечает при старте | капчер (`prepare` таймаут 5 с / контекст FAILED) | отказ, ничего не создано | «Не удалось начать запись: звуковая служба не отвечает…» | U |
| Экран заблокирован/неизвестно при нажатии | GUI (§5.6 п. 2) | отказ без `prepare` | F18.4 (м) | T-218 |
| Заблокировали между `prepared` и `open` | GUI повторная проверка | `open` не отправляется, `prepare` отменяется (`stop`) | как выше | T-200 |
| Заблокировали в первую секунду после `started` | GUI (`changed`) | `pause(lock)` ≤ 1 с | «Запись на паузе: экран был заблокирован» | T-218 |
| Диск < 600 МБ перед стартом / < 200 МБ во время | GUI `statvfs` / капчер `free_mb` | отказ / `stop` с сохранением | тексты F18.4 (д) | U |
| Каталог встреч не записываем / на сетевой ФС | GUI (`ensure_private_dir`, `statfs`) | отказ / предупреждение (П33) | F18.4 (м) | T-232 |
| Звуковая служба по сети (`Server String` не `unix:`) | капчер `prepared{server_local:false}` | предупреждение, запись разрешена | F18.4 (м) | T-232 |
| Цель far исчезла/не совпала при `prepare` | капчер | `prepared{far_available:false, far_reason}` → GUI: вариант 1 → вариант 2 с отметкой («нажал в ту секунду, когда звонок закончился») | F18 исключения | U |
| Программа видеосвязи закрылась во время записи | капчер `sink-input removed` | far `ended` + отметка; mic продолжается | «программа видеосвязи закрылась» | T-213 |
| Микрофон сменился/отключился | капчер (`device_change`) | mic `paused`; уведомление + кнопки на плашке | F18.4 (ж) | U (по образцу T-39) |
| Сменился вывод (BT) | капчер (`sink-input changed`/`SERVER`) | переоткрытие far ≤ 1 с, отметка в журнале | — | U |
| Перезапуск звуковой службы | капчер (контекст TERMINATED) | `autopaused{audio-failed}` → пауза; «Продолжить» нажатием | пауза с причиной | U |
| GUI завис > 5 с | капчер (аренда) | самопауза; возврат GUI → авто-`resume` + отметка | «…индикатор записи не отображается» | T-207 |
| GUI умер | капчер (PDEATHSIG) | дописать сегменты, выйти; при следующем старте — диалог | «Запись прервалась. Сохранено 0:37:12» | T-202 |
| Капчер умер (`kill -9`) | GUI (`exited`) | встреча `interrupted`, диалог сразу; потоки закрыло ядро; потеря ≤ 10 с | то же | S24-A8 |
| Оба индикатора пропали > 1 с | `IndicatorGuard` | `pause(indicators)`; возврат → авто-`resume` | F18.5 (б) | T-206 |
| Лимит 2 ч / 3 ч / 4 ч | GUI | уведомление за 5 мин (+кнопка на плашке) → `stop` | F18.4 (г) | U (фейковые часы) |
| Пауза > 60 мин | GUI | `stop` с сохранением | — | U |
| Сон | `SessionMonitor` | `stop`; уведомление после пробуждения | F18.4 (з) | U (фейковый транспорт) |
| Смена сеанса ОС | `SessionMonitor.active=false` | `stop` | — | U |
| Выход из программы во время записи / SIGTERM | GUI | диалог / `stop` | F18.4 (и) | xvfb |
| Вторая запись | GUI | отказ | «Уже идёт запись встречи» | U |
| Диктовка во время записи | — | работает (отдельные процессы) | F18.4 (к) | S1.14 стенд |
| Нет модели GigaAM v3 | `MeetingJobs` | `needs_model` + «Установить» | F18.7 (б) | U |
| Самопроверка модели не прошла | транскрайбер (`model.load`) | `failed{model}`; звук цел | «Распознавание на этом компьютере не работает» | U |
| Мало памяти | GUI/транскрайбер | `waiting_memory`, повтор 60 с | «Расшифровка подождёт…» | U |
| Транскрайбер умер посреди задания | супервизор | рестарт ≤ 3/10 мин, резюм с чекпойнта; разделение заново | прогресс продолжается | T-204 |
| `meeting-diar` умер / лимит | транскрайбер | `done` без разделения, «Разделить заново» | «Собеседники» | U |
| Битый сегмент | транскрайбер | отметка «кусок записи повреждён» | F18 исключения | U |
| Реплика не распознана за дедлайн | транскрайбер | «[не распознано]» + отметка | — | U |
| Отмена расшифровки | GUI → `job.cancel` | `recorded`, звук цел | «Записана» | U |
| Удаление во время задания | `MeetingStore` | надгробие → `cancel` → `rmtree`; результат старого поколения отброшен | — | T-231 |
| Сервис ИИ: до тела / после тела / 3xx / 429 / таймаут / пусто / слишком длинно / адрес изменился | `ai_client` | тексты §7.2; `sent_attempted` | S25-A12 | T-219, T-230 |
| Secret Service заперт/отменён/ошибка | `secret_store` | ключ в памяти до конца сеанса, предложение повторить; файл не создаётся | MJ-8 | T-220 |
| Политика INVALID/небезопасный файл | `meetings/policy.py` | набор «закрыто» | S26-A11 | T-208 |
| Hardening не удался в роли встреч | процесс (exit 3) | старт отклонён | «Не удалось защитить память процесса — запись встречи недоступна» | T-216 |

---

## 10. Milestones (порядок — по зависимостям; оценка — дни потока разработки, как Effort беклога)

```
M0 спайки на ноутбуке ─────────────┐ (параллельно всему; владелец ≈ 60 мин + заход 1 без присмотра)
M1 основание ──► M2 захват ──► M3 индикация ──► M4 расшифровка-текст ──► M5 хранение/шаблоны ──► M6 политика+документы ──► M10 релиз
                     │                                 │                                        ▲
                     └──► M8 детектор (Should, после SP3a) ──────────────────────────────────────┤
                                                       └──► M7 разделение (Should) ──► M9 ИИ-шаблоны (Should) ─┘
```

| Веха | Истории | Оценка | Must v1.1 | Зависит от |
|---|---|---|---|---|
| **M0. Спайки на ноутбуке и VPS-добор** | — | 1,5 (анализ) + 🙋 ≈ 60 мин | да (снимает A2, A6, A7, θ) | «ок» владельца на заход |
| **M1. Основание** | 13.1, 13.14, 13.12 (ядро) | **3,5** | да | — |
| **M2. Процесс захвата и старт записи** | 13.4, часть 13.11 | **5** | да | M1; D1 (M0) — для DoD, не для старта |
| **M3. Индикация, остановка, уведомление участников** | 13.5, 13.6 | **3** | да | M2 |
| **M4. Расшифровка: текст по ходу, очередь, резюм** | 13.7, часть 13.11, часть 13.9 | **5** | да | M2 (файлы), M1 |
| **M5. Хранение, удаление, локальные шаблоны** | 13.9, 13.11 (остаток) | **2,5** (+1 odt/docx Could) | да | M4 |
| **M6. Политика (матрица) и документы** | 13.12 (остаток), 13.13 | **1** + 1 tech-writer | да | M1–M5 |
| **M7. Разделение по говорящим** | 13.8 | **4,5** | нет (Should) | M4; θ/τ из M0 |
| **M8. Детектор звонка и предложение** | 13.2 | **3** | нет (Should) | M2; SP3a из M0 |
| **M9. Шаблоны с ИИ** | 13.10 | **4** | нет (Should; первая на вынос при нехватке — Д46) | M4, M5 |
| **M10. Релиз v1.1** | QA, T3, changelog, PR | **2,5** | да | всё принятое |
| **Итого** | | **Must ≈ 21,5 (+2,5 релиз) · Should ≈ 11,5 · всего ≈ 35,5** | | беклог: 16,5 + 11 = 27,5 |

**Почему больше беклога (+4 к Must, +0,5 к Should, +2,5 релиз):** три процесса вместо одного (+1), двухфазный старт и аренда (+0,5), timeline вместо «счётчика» (+0,5), резюм с чекпойнтом и кредитный поток (+1), эхо-гейт как часть 13.7 (+0,5), миграция настроек/сырые ключи политики (+0,5); релиз (QA в двух сессиях, P21½, T3) беклог не считал. **Что резать при нехватке ёмкости (по порядку):** M9 → odt/docx (Could) → M8 (детектор; ручной старт — основной путь) → консолидация в M7 (оставить только связывание). Календарно при одном потоке ≈ 5 ч/дн: Must ≈ 4,5–5 недель → при старте ~10.11 — середина декабря; M7 ∥ M5/M6 и M8 ∥ M9 вторым потоком Codex → Must + M7 + M8 к ~31.12 реально, M9 — под вопросом (см. §12).

### M0. Спайки на ноутбуке и VPS-добор `[ ]` — первая неделя E13, параллельно M1
**Goal:** закрыть допущения A2/A6/A7 и калибровки, не меняя архитектуру.
**Tasks:** заход 1 (без присмотра): `sp1_soak --virtual 60` (S1.5a–e на реальной карте), `sp4_bench A2/A3 --embedder 3dspeaker --threshold {0.7,0.9,1.05}` + C1 (p95 диктовки), E1 (`strace`), E6 (kill на 60-й мин → цена «заново»). Заход 2 (владелец, ≈ 60 мин): SP1 E0–E5′ с DION (B′ `monitor_stream_probe` **и** C как контроль; E4 BT-гарнитура — A2), SP3a (свойства DION, mute, негативы), SP3c (браузер), блок F (5 мин настоящего звука → θ для 3D-Speaker), H-KDE `lockprobe`, заход 2b `lockprobe --fly` (SF.2/SF.3), блок G по желанию. VPS: проба «`import onnxruntime` + `import sherpa_onnx` в одном процессе» (резерв альтернативы C §2.1), A2 с `--threshold 1.05` для WeSpeaker-контроля.
**DoD:** `arch/spikes/SP1.md`, `SP3.md` (+SP-F), `SP4.md` по §1.5 протокола; D1, D15–D19, D24 закрыты строками «факт → решение»; θ/τ профили в `DIAR_PROFILES`.
**Validation:** `spikes/e13/run.sh unattended`, `run.sh live`, `run.sh lockprobe --kde|--fly`, `sp4_bench --analyze`.
**Known Risks:** время владельца; Fly без сигнала блокировки (→ D24 = «недоступно»); B′ не следует за BT (→ переоткрытие или C).
**Stop-and-Fix:** B′ и C оба не работают на ноутбуке → вариант 3 с A30 для v1.1 + вопрос владельцу (E7 не делаем).

### M1. Основание `[ ]` — ≈ 3,5 дн.
**Goal:** контракты и каркас: без звука и расшифровки, но с полной схемой данных, политикой, IPC v5, разделом «Встречи» и стражами журналов.
**Tasks:** `core/settings.py` schema 3 (поля §3.7, миграция, откат) · `core/policy.Policy.raw` + `meetings/policy.py` (7 ключей, таблица F18.11, набор «закрыто», `profile=secure`) · `meetings/model.py`, `store.py` (id, 0700/0600, `meeting.json` атомарно, список, надгробие, retention, доведение при старте), `segments.py` (порт `segwriter` + восстановление), `timeline.py` · `worker/ipc.py` v5 (все схемы §4), `supervisor.py` (`req`, `auto_restart`, `on_exit`), `worker/main.py`/`bootstrap.py` роли, `harden_process(strict=True)` · `meetings/events.py` + lint-тест, `SECRET_FIELDS`, валидатор `Stats`, белый список диагностики, grep-тест «перехват» · `design/tokens.json` 2.4.0 → `Theme.qml`/`PillTheme.qml` (генератор), `icons.py` 22 глифа + режим заливки (№34) · `qml/sections/Meetings.qml` каркас: карточка-вход, D1 экран ответственности, тумблеры/группы (серые по политике + строка-пояснение), пункты трея «Записать встречу»/«Встречи…» (без действия записи до M2), `meeting_record=off|manual|негодное` · `data/meeting-apps.json` + `meetings/apps.py`.
**DoD:** US-13.1 (S26-A5, A8 политика), US-13.14 (T-329 lint/маркеры, S26-A8), US-13.12 матрица ключей на unit (S26-A4–A6, A10, A11 — без сетевой части); тесты схем IPC; `settings.json` 0.1.x → 3 и обратно без потерь; design-reviewer: раздел «Встречи» ↔ `refs/01-*`.
**Validation:** `pytest -m unit tests/unit/test_meeting_{policy,store,segments,timeline,ipc,events}.py -q`; `xvfb-run pytest tests/xvfb/test_meetings_section.py`; `tools/validate meetings --policy-matrix`.
**Known Risks:** регрессия генератора темы при 300 токенах (гейт: snapshot-тесты пилюли/разделов 1.0 зелёные).

### M2. Процесс захвата и старт записи `[ ]` — ≈ 5 дн.
**Goal:** две дорожки на диск с гарантиями BL-1/BL-2/BL-5, лимиты и восстановление; карточка старта.
**Tasks:** `worker/meeting_capture.py` (`MeetingCaptureState`: контекст, `prepare/open/pause/resume/stop/keepalive/shutdown`, самопауза, события дорожек, `free_mb`, детектор-основа: подписка и `pa_context_get_index`, без правила звонка) · `worker/monitor_stream.py` (B′ + A; proplist; `sink-input removed/changed`; `SERVER`) · mic через `PulseStreamSource` с `fragsize` 100 мс · сегменты + timeline в sidecar · `core/meeting_session.py` (`MeetingController`: §5.1 целиком, таймеры с инъекцией часов, сеанс/сон/блокировка, диск, аренда, `interrupted` при `exited`) · GUI: W4 карточка старта (варианты, подсказки, предупреждения П33, отказы), диалог «Запись прервалась…», строки списка `recording/paused/recorded/interrupted`, запись `meeting.json` · `LOCK_SIGNAL_BY_SESSION` · `tools/validate meetings --stand` (виртуальный стенд: `virtual_mic.sh` + `pw-play`, 60 мин, `kill -9`, `pactl` ноль потоков).
**DoD:** S24-A1 (без плашки — M3), A2, A4–A9, A11–A13, A17; T-300/302/303/312/313/316/318 (нумерация §11); на стенде: 60 мин без гэпов, дрейф таймкодов по бипам ≤ 50 мс; `prlimit`/`prctl` у капчера; импорт-гейт роли.
**Validation:** `pytest -m unit tests/unit/test_meeting_capture*.py tests/unit/test_meeting_session.py -q`; `tools/validate meetings --stand --minutes 60`; стенд PulseAudio 16.1 (`vps_stand.sh --pulseaudio`) — B′/A доступны.
**Known Risks:** A2 (BT) — решается M0; два читателя микрофона (A3); `fragsize` 100 мс и латентность событий смены устройства (бюджет 0,7 с сохраняется — проверить).
**Stop-and-Fix:** любой поток у звуковой службы до `open` в T-300 → блокер вехи.

### M3. Индикация, остановка, уведомление участников `[ ]` — ≈ 3 дн.
**Tasks:** `ui/overlay.py` (вынос X11/EWMH/размещения из `pill.py`, пилюля на нём же — её xvfb-тесты гейт) · `ui/meeting_banner.py` + `MeetingBanner.qml` (W2: 36/63.8–120, 340–524, свёрнутая, перетаскивание, кнопки по состояниям, Fly-режим) · трей: иконки `astravoice-tray-meeting-*`, состояния/подсказка/меню §4.5 спеки, путь с клавиатуры · `IndicatorGuard` → `MeetingController` · уведомления (`notify`) с действиями + дубли на плашке · W3 карточка + «Скопировать для чата» + тексты в разделе (`meeting_notice_text`, `meeting_invite_text`, политика) · выход/SIGTERM.
**DoD:** US-13.5, US-13.6: S24-A1 (полностью), A3, A10, A15, A16, A18; T-306/307/325/324(часть)/334; design-reviewer W2/W3/трей ↔ `refs/`; во Fly (живой прогон) клик по кнопке плашки не крадёт фокус у программы видеосвязи.
**Validation:** `xvfb-run pytest tests/xvfb/test_meeting_banner.py tests/xvfb/test_pill_states.py -q`; `tests/unit/test_indicators.py` расширение; живой чек Fly.
**Known Risks:** fly-wm активирует окно по клику (spec §6) → запасной `_NET_WM_WINDOW_TYPE_NOTIFICATION`.

### M4. Расшифровка: текст по ходу, очередь, резюм `[ ]` — ≈ 5 дн.
**Tasks:** `worker/meeting_transcribe.py` (§6.1 этапы 0–3, 5; кредиты; чекпойнт; резюм; дедлайны; лимиты; `model.load` GigaAM v3; `nice 19`) · `meetings/echo_gate.py` (порт NCC) · `core/meeting_jobs.py` (очередь, `MemAvailable`, модель/самопроверка, резюм при старте, отмена, поколение, удаление звука после `done`, retention-свип, «Ждёт памяти» повтор) · GUI: карточка встречи A (таблица, прогресс-строка В4, бейджи отметок, 1024/900), состояния списка `transcribing/waiting_memory/needs_model/done/failed`, подсказка трея «Расшифровка встречи: 40 %», кнопка «Установить» (F7) · шаблон «Стенограмма» `.md` + D4 «Сохранить как…» (минимум) · `tools/validate meetings --transcribe <корпус>`.
**DoD:** US-13.7: S25-A1 (без разделения; с разделением — M7), A2, A3 («Я/Собеседники»), A7, A8, A14, A17; S26-A1, A3; T-304 (без разделения), T-305, T-317, T-333; корпус SP4: WER ≤ WER(диктовка на том же TTS) + 3 п.п., таймкоды ±1 с (S4.9); p95 диктовки при фоновой расшифровке ≤ 0,8 с (ноутбук).
**Validation:** `pytest -m unit tests/unit/test_meeting_transcribe*.py tests/unit/test_meeting_jobs.py tests/unit/test_echo_gate.py -q`; `pytest -m integration tests/integration/test_meeting_transcribe_gigaam.py`; `strace -f` сценарий T-305 (часть «текст»).
**Known Risks:** A4 (RTFx); эхо-гейт режет «Я» (S2.2) → флаг; A9.
**Stop-and-Fix:** чекпойнт с массивом чисел → блокер (схема-тест).

### M5. Хранение, удаление, локальные шаблоны `[ ]` — ≈ 2,5 дн. (+1 Could odt/docx)
**Tasks:** удаление с надгробием и согласованием заданий (MJ-14), переименование D3, действия списка, итог места, retention UI · 4 шаблона + статистика + свой шаблон (Could) · D4 полностью (предпросмотр, формат) → системный диалог, тосты · `export_odt.py` (Could).
**DoD:** US-13.9, US-13.11: S25-A6, S26-A2; T-323, T-331; золотые файлы шаблонов; design-reviewer ↔ `refs/07-*`.

### M6. Политика (матрица) и документы `[ ]` — ≈ 1 дн. + 1 tech-writer
**Tasks:** оставшиеся строки UI по ключам (после M7/M9 — их строки входят в те вехи), полная матрица S26-A4–A6/A10/A11 на xvfb; `docs/admin/meetings.md`, `PRIVACY.md` раздел «Запись встреч» (US-13.13; формулировки П28/П33/MN-17/BL-5/Fly), `INSTALL-ADMIN.md` 7 ключей; заметки к выпуску.
**DoD:** S26-A9, T-334 grep; очный юрист — правки внесены (вне оценки).

### M7. Разделение по говорящим `[ ]` — ≈ 4,5 дн. (Should)
**Tasks:** vendoring `sherpa-onnx` (пины, `wheels.lock`, AppImage, NOTICE), ELF-гейт 3 → 7, `elf-audit` · каталог: 2 записи `kind=diarization`, установщик, кнопка «Установить разделение» · `worker/meeting_diar.py` (sherpa, центроиды, связывание по кумулятивным центроидам, консолидация, `memfd`, без путей записи) · транскрайбер: этап 4, блоки по паузам, присвоение меток, `provisional/final`, «Разделить заново» · GUI: панель участников, имена (60 символов), «Это один человек» (поповер, тост), `speakers.json`, номера `②`.
**DoD:** US-13.8: S25-A3 (Спикер N), A4, A5; T-304, T-305 (`strace -f` ребёнка), T-321, T-326; корпус SP4: число дикторов = эталон ± 1 на блоках, DER(блоки) ≤ DER(целиком на 30-мин подмножестве) + 3 п.п. (S4.2); пик памяти ребёнка ≤ 1,2 ГБ; 1 ч ≤ 10 мин на ноутбуке (A31).
**Validation:** `pytest -m integration tests/integration/test_meeting_diar_sherpa.py`; `tools/validate meetings --diar <корпус>`; `tools/elf-audit --expect 7`.
**Known Risks:** качество на реальном звуке (Р18-4) — «Это один человек» как страховка; второй ORT в пакете (размер +40 МБ).

### M8. Детектор звонка и предложение `[ ]` — ≈ 3 дн. (Should; после SP3a)
**Tasks:** правило звонка в `meeting-capture` (порт `detector_rule.py`: T_on 5 с, T_off 10 с, `apps.json`, MN-9 проверки `declared-verified/declared/mismatch`, исключение своих потоков, не чаще 1/с на поток) · события `call.*`, `call.query` · W1 окно (500 мс, без фокуса, 60 с, закрытие при блокировке, «в этом звонке больше не показывать») · тумблер и галочка D1, `manual` · «Начать запись» → вариант 1 сразу (цель из `call.query`).
**DoD:** US-13.2: S23-A1–A11 (A10 — DION и браузер живьём; Jazz — только при SP3b); T-300 (сценарий предложения), T-315, T-324, T-338; спуф-тест П30.
**Known Risks:** Electron 43 «Dion» (узнаём по бинарю — не влияет); ложные срабатывания браузера (принято: «в браузере»).

### M9. Шаблоны с ИИ `[ ]` — ≈ 4 дн. (Should; первая на вынос)
**Tasks:** `meetings/netaddr.py`, `platform/secret_store.py`, `meetings/ai_client.py` (+`NetworkKind.ai_template`, `post_json` в `HttpClient` без прокси для internal, без редиректов), `sanitize.py`, два шаблона (инструкции фиксированы, требование ссылок на таймкоды), UI: адрес/модель/ключ/«Проверить», D5, галочка замены имён, карточка результата, состояния §3.8 спеки, строки политики (`endpoint/allow_external/allow_plaintext`), журнал по схеме, отметка `sent_attempted` + подтверждение удаления.
**DoD:** US-13.10: S25-A9–A13, A15, A16; S26-A6, A10; T-308–311, T-319, T-320, T-322, T-327–330, T-335–337; T2 security-analyst на диф (Security surface: да); один живой запрос на адрес владельца (A33).
**Known Risks:** Secret Service во Fly; reasoning-модели (`max_tokens`); совместимость `/models`.

### M10. Релиз v1.1 `[ ]` — ≈ 2,5 дн.
QA-регрессия KDE + Fly (S23–S26 по критерию готовности беклога, диктовка 1.0 без регрессий — T-35/T-39/T-105), P21½ на ноутбуке (DION, BT, блокировка, `tcpdump`, `prlimit`), T3 ИБ (решение владельца 19.10: только перед публичным релизом — здесь да), changelog, PR, smoke после публикации; через 14 дней — Ц8.

---

## 11. Тест-стратегия

### 11.1 Уровни и инструменты

| Уровень | Что покрывает | Инструменты (существующие → расширение) |
|---|---|---|
| **U unit (CI)** | автоматы (`MeetingController`, `MeetingJobs`, `MeetingCaptureState`, `MeetingJobState`), схемы IPC/файлов, политика (матрица), store (права, symlink, надгробие, retention), segments/timeline (синтетика с дрейфом), эхо-гейт (синтетические смеси), связывание (фикстуры `spikes/e13/tests`), netaddr (матрица), sanitize, templates (золотые файлы), secret_store (приватная шина), ai_client (`http_fault_server`), события/lint, stats, миграция настроек | `tests/fakes.py` (FakeEngine, FakeAudioSource), `tests/helpers/pulse_fakes.py` → **расширить:** sink-inputs/source-outputs с proplist, `set_monitor_stream`, события подписки, **счётчик `stream_connect_record`**; фейковый `Login1Transport` (есть в unit `session_state`); инъекция часов (`clock=`) — везде; `FakeSupervisor` с записью отправленных кадров; скриптуемый ребёнок `meeting-diar` (детерминированные метки) |
| **I integration (CI, тяжёлые)** | GigaAM на корпусе SP4 (WER/таймкоды), sherpa на 3-мин корпусе (число дикторов, связывание), `strace -f` (BL-3), `kill -9` транскрайбера/ребёнка и резюм, RSS под `SIGSTOP` читателя (кредиты), hardening (`prlimit`, `PR_GET_DUMPABLE`) | `tests/integration/*` (модели из кэша CI как `test_engine_gigaam.py`), корпус Piper CC0 (`corpus_build.py`, генерируется в CI или кэш) |
| **xvfb (CI)** | раздел «Встречи» (все состояния списка/карточки/диалогов, два размера), W1/W2/W3 (окна без фокуса, 500 мс, StaysOnTop, геометрия), трей (меню/состояния на изолированной шине), клавиатурный путь, тексты (без «перехват») | `tests/xvfb/*`, `tests/helpers/{tray_dbus_lifetime,notify_fake_server,private_bus,snapshots}.py`; design-reviewer по `refs/*.png` |
| **Стенд VPS (ночной/по запросу)** | 60-мин виртуальный захват (гэпы, дрейф по бипам, CPU), `kill -9` → ноль объектов, B′/A на pipewire-pulse и PulseAudio 16.1, детектор на `pw-play`-«программе», диктовка во время захвата (два читателя) | `spikes/e13/vps_stand.sh` → `tools/validate meetings --stand`; `virtual_mic.sh` дисциплина (T-42) |
| **Живьём (ноутбук владельца, KDE и Fly)** | S23–S26 по критерию готовности беклога; DION/BT/блокировка/сон/Fly; `tcpdump`; `pactl` ноль потоков в окне предложения; `prlimit`/`/proc/<pid>/mem`; p95 диктовки при расшифровке; P21½ | `remote-hands`, чеклисты `docs/qa/`, Юрка руками (T1 §9.2) |

### 11.2 Нумерация и привязка к T1
T1 §5 нумерует требования T-200…T-240, но в `docs/test-plan.md` T-200…T-239 уже заняты M7-моделями (и US-2.7 перенесена в T-260…T-279). **Предложение:** раздел «E13 Запись встреч» в test-plan — **T-300…T-340 с тем же суффиксом** (T1 T-2xx ↔ test-plan T-3xx), чтобы ссылки T1 читались без таблицы соответствия. Формулировки T1 переносятся дословно с двумя поправками под эту архитектуру: T-302 (единственная точка — `prepare/open`, не `capture.start`), T-314 (в коде нет `move-sink-input`/`load-module` — тест превращается в grep-гейт + стенд «после `kill -9` ноль объектов и умолчания прежние»). Тесты пишутся **до кода** вехи (tester по test-plan, как в 1.0): красные по правильной причине → зелёные.

### 11.3 CI-гейты (новые)
ELF-счётчик 7 (M7) · grep «перехват» по `docs/`, переводам, README, описанию пакета · lint «логгер только с константами» по модулям встреч · импорт-гейты ролей (`meeting-capture` без `onnxruntime`; роли без `requests`) · схема-тест чекпойнта (нет массивов) · snapshot глифов/темы 1.0 неизменны · `tools/validate meetings --policy-matrix` · маркер-тест журналов/статистики/диагностики (T-329).

### 11.4 Что проверяет только человек
Живой звук DION и BT (A2), Fly-локер (A6), клик по плашке во Fly без кражи фокуса, слышимость звонка после `kill -9` захватчика (T-313), `pw-dump` из другой сессии (П26), один живой запрос к сервису ИИ владельца (A33).

---

## 12. Риски и открытые вопросы

### 12.1 Риски плана

| # | Риск | Вероятность/влияние | Мера |
|---|---|---|---|
| Р1 | B′ на ноутбуке не следует за sink-input при переезде на BT; переоткрытие даёт разрыв > 1 с | средняя / средняя | M0 E4; резерв C (`pw-record`, +1 дн.) за интерфейсом `FarSource` |
| Р2 | Fly без сигнала блокировки → записи во Fly нет в v1.1 (S17, US-13.1 «в KDE и Fly» с оговоркой) | средняя / высокая для владельца-Fly | SP-F в M0; D24 = «недоступно» (строже побеждает); эвристика по окну локера — только решением владельца с ИБ |
| Р3 | A31 не достигается: 1 ч > 10 мин с разделением на ноутбуке (VPS дал RTF 0,33 при `nice 19` под нагрузкой) | средняя / средняя | M0 A2 на ноутбуке; блоки 15 мин; ASR и разделение последовательно; честное «≤ N мин» в заметках |
| Р4 | Качество разделения на русском/ВКС (Р18-4): лишние дикторы, расщепление | высокая / средняя | θ-калибровка на настоящем звуке (блок F), консолидация, «Это один человек»; подпись «Собеседники» всегда доступна |
| Р5 | Эхо-гейт вырезает «Я» в двойном разговоре на реальном звуке | средняя / средняя | порог энергии +6 дБ, флаг отключения, подсказка «в гарнитуре чище» |
| Р6 | Второй ORT и +4 ELF в пакете: размер, ЗПС v1.2, CI-гейт | низкая / средняя | пины, `elf-audit`, NOTICE; альтернатива C §2.1 не снимает второй ORT — только diar_v2, который владелец отклонил |
| Р7 | Вынос оверлея из `pill.py` ломает пилюлю | низкая / высокая | гейт — все xvfb-тесты пилюли; при красном — копия кода вместо выноса |
| Р8 | Ёмкость: Must 21,5 дн. при старте в ноябре; Should не влезает к 31.12 | высокая / средняя | порядок выноса §10; второй поток Codex на M7/M8 |
| Р9 | Два читателя микрофона конфликтуют (A3) | низкая / средняя | S1.14 на стенде в M2; резерв — диктовка во время встречи получает PCM из капчера (не планируется) |
| Р10 | Secret Service во Fly отсутствует/заперт — ключ в памяти, повторный ввод | средняя / низкая | по MJ-8; документ администратора |
| Р11 | `oom_score_adj` капчера 300 (ниже T1 «500 для всех процессов F18») | — / низкая | обоснование: запись важнее, RSS ≤ 60 МБ; вопрос T2 |

### 12.2 Что решает Юрка (не владелец)
Вынос `ui/overlay.py` vs копия (Р7); альтернатива C §2.1 (один процесс для ASR+sherpa) при положительной пробе на VPS; нумерация тестов T-3xx; второй поток Codex для M7/M8; `fragsize` 100 мс для встреч.

---

## 13. Что НЕ делаем в v1.1
- Предзапись/кольцевой буфер (US-13.3, Could) и ключ `meeting_prerecord`.
- Профили голосов, узнавание между встречами, подсказки имён (§9.5).
- Шифрование на диске (M6 юриста), затирание на SSD.
- Правка графа звука: null-sink/loopback/`module-echo-cancel`/восстановитель (E7, MJ-4 п. 2); при провале B′ и C — вариант 3 с A30.
- Эвристика блокировки по окну локера во Fly (D24-эвристика) — только решением владельца с ИБ/юристом.
- Пауза дорожки микрофона по mute в программе видеосвязи (Could после v1.1, MN-17).
- Разбиение реплики по границе смены говорящего и пословные таймкоды; обработка перекрывающейся речи.
- Ресемплинг дорожек (только timeline); `meeting.job.yield` (резерв, выключен).
- Автоустановка моделей разделения вместе с GigaAM; настройка порога θ в UI; разделение одного спикера на двоих.
- `.odt`/`.docx`, свой шаблон оформления, «Прослушать» с плеером — Could, только при остатке ёмкости.
- Горячая клавиша записи встречи; языки кроме русского; другие модели для встреч.
- Замки «Задано администратором» у строк (v1.2, US-9.4); ЗПС-подпись новых ELF (v1.2, US-12.3).
- Деление длинной встречи на части для модели ИИ (A40); стриминг ответа ИИ; инструменты в запросе.
- Delay-inhibitor login1 перед сном (Could; потеря ≤ 10 с принята).
- Перецеливание far на другой процесс без нового выбора человека.

---

## Открытые вопросы владельцу (по одному, с рекомендацией)

**Вопрос 1 из 5 — заход на ноутбуке (M0).** Нужно ≈ 60 минут одним заходом (звонок DION 8 мин с BT-гарнитурой, 5-мин калибровочный звонок, негативы, блокировка в KDE, перелогин во Fly на 10 мин) + одна команда без присмотра на 3 часа. Без него архитектура не меняется, но остаются открытыми: поддержка Fly, порог разделения, бюджет «1 ч ≤ 10 мин», путь B′ при BT. **Рекомендация:** первая неделя кодинга E13, до M2 DoD.

**Вопрос 2 из 5 — Fly в v1.1.** Если SP-F покажет, что во Fly нет надёжного сигнала блокировки, запись встреч во Fly в v1.1 **недоступна** (честный отказ при старте, расшифровка/просмотр/удаление работают). Альтернатива — эвристика по окну локера с риском «запись идёт под локером» (нарушение M2). **Рекомендация:** принять «недоступно во Fly» для v1.1, эвристику не делать; вернуться в v1.2 с данными Fly.

**Вопрос 3 из 5 — объём v1.1 к 31.12.** Must ≈ 21,5 дн. + релиз 2,5; Should ≈ 11,5 (разделение 4,5 · детектор 3 · ИИ-шаблоны 4). При одном потоке к 31.12 реалистичны Must + разделение + детектор; ИИ-шаблоны — v1.1.1 (январь). **Рекомендация:** так и планировать (ИИ-шаблоны первыми на вынос — совпадает с Д46), второй поток Codex подключать на M7/M8.

**Вопрос 4 из 5 — эмбеддер.** 3D-Speaker CAM++ (Apache-2.0, меньше ложных дикторов на VPS) против WeSpeaker CAM++ LM (CC-BY-4.0 с атрибуцией в NOTICE; порог 1,05 уже известен из Cowork). **Рекомендация:** 3D-Speaker; окончательно — после калибровки на 5 мин настоящего звука (блок F); если DER хуже > 2 п.п. — WeSpeaker + NOTICE.

**Вопрос 5 из 5 — эхо-гейт по умолчанию.** Без гарнитуры собеседники попадают в микрофон; гейт по корреляции (SP2: −91 % эха на синтетике) включён по умолчанию, с подсказкой «в гарнитуре чище» и без системного AEC. Альтернатива — только подсказка, без гейта (реплики собеседников будут дублироваться как «Я»). **Рекомендация:** гейт включён; константа отключения на случай потерь «Я» на реальном звуке.
