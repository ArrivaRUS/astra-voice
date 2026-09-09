# Astra Voice — синтез архитектуры (суд Юрки, P12)

> 2026-09-09. Входы: `arch/plan-claude.md` (#1, Fable 5.1, 352 строки) и `arch/plan-codex.md` (#2, GPT-6 Astra xhigh,
> 300 строк). Оба независимы, оба прочитаны Юркой целиком; факты среды перепроверены read-only на машине.
> Статус: **челлендж Astra пройден (22 находки → правки в §7); ждёт модели угроз T1 и решения G4 по установке без прав администратора**.

## 1. Рубрика (1–5)

| Критерий | План #1 (Claude) | План #2 (Astra) | Комментарий |
|---|---|---|---|
| Корректность | **5** | 4 | #1 сверил среду командами (ORT-колёса без `__isoc23_*`, apt-пакеты, KWin-атомы); #2 держит шим и «4 .so» как допущение. Ошибка #1: «PT Root UI/PT Mono нет на ALSE» — **неверно**, пакеты `fonts-pt-root-ui`/`fonts-pt-mono` есть в `repository-main` и стоят на машине |
| Простота | **5** | 3 | #1: звук в воркере, ноль PCM через IPC; #2: `QAudioInput` в GUI + `memfd` в воркер — больше движущихся частей |
| Соответствие ограничениям | 5 | 5 | оба в рамках журнала решений; оба заменили minisign на GPG/`gpgv` (обоснованно — `minisign` в apt Astra нет) |
| Риск | 4 | **5** | #2 сильнее по границам доверия: отмена через `RunOptions.terminate`, ELF-инвентарь как гейт CI, защита путей манифеста, hardening CI, MemAvailable перед сменой модели |
| Тестируемость | 5 | 5 | #1 — тест у каждого компонента + привязка к историям; #2 — `tools/validate`/`benchmark` как CLI и протокол измерений |
| **Итого** | **24** | **22** | базовый — **#1**, заимствования из #2 — ниже, с предпосылками |

## 2. Что совпало (принято без спора)
Два процесса (GUI ∥ воркер, крах ORT не роняет GUI, ≤3 рестарта/10 мин) · `socketpair` + JSON с `hello`/версией протокола ·
рантайм v1 **`onnx-asr` + `onnxruntime`** (12/12 позиций, цифры протокола сняты на нём; `sherpa-onnx` — второй адаптер за
интерфейсом `Engine`, не «тихий fallback») · хоткей `python3-xlib`/`XGrabKey` + чтение `kglobalshortcutsrc` · вставка
`QClipboard(text/plain)` + XTest, восстановление буфера, терминалы по `WM_CLASS`, `x-kde-passwordManagerHint` · пилюля —
отдельное QML-окно Tool/Frameless/StaysOnTop/DoesNotAcceptFocus + EWMH руками, без `requestActivate` · трей `QSystemTrayIcon`
(SNI), меню `QMenu` · Silero VAD ONNX в воркере, окно GigaAM < 24 с · подпись **GPG/`gpgv`** с закреплённым keyring и в GUI,
и в root-помощнике · root-помощник — Python `python3 -I` под `pkexec`, своё polkit-действие `auth_admin` без `_keep`, копия в
root-staging 0700 и повторная проверка, `apt-get install --no-remove ./deb` с фиксированными аргументами · XDG autostart без
`OnlyShowIn`, путь не переписывается · `settings.json` + `policy.conf` → эффективная конфигурация · тема из `kdeglobals` до
`engine.load` · `Theme.qml` генерируется из `tokens.json` · один `.deb`, всё из apt Astra + вендорные `onnxruntime`/`onnx-asr`,
сборка одной командой в контейнере Debian 12, SBOM, релиз с подписью · спайки до кода · вехи по релизам v0.1/v0.2/v1.0.

## 3. Развилки и решения Юрки

| # | Развилка | #1 | #2 | Решение | Предпосылка / проверка |
|---|---|---|---|---|---|
| Р1 | Где захват звука | в воркере, `libpulse-simple` через ctypes, PCM не покидает процесс | в GUI, `QAudioInput` в QThread, PCM в воркер через `memfd` | **#1** | спайк S5 доказывает ctypes-захват на PipeWire-pulse; провал → схема #2 (memfd) как запасная, интерфейс `AudioSource` общий |
| Р2 | Шим `__isoc23_*` и число `.so` | не нужен (проверено `objdump`: ORT 1.29.0 max GLIBC_2.28), **3 `.so`** | нужен, 4 `.so` | **#1** + гейт #2 | CI: `objdump -T` по вендорным `.so` (max ≤ GLIBC_2.36, 0 `isoc23`) и **счётчик ELF = 3** останавливает сборку при лишнем |
| Р3 | Версия ORT | 1.29.0 (без `sympy`) | 1.23.2 | **1.24.4**, пин sha256 (после челленджа Н4: 1.29.0 требует `protobuf>=4.25.8`, в apt Astra — 3.21.12; у 1.24.4 все Requires-Dist закрываются apt: protobuf, flatbuffers 2.0.8, sympy 1.11.1, numpy ≥ 1.21.6) | CI-гейт «METADATA Requires-Dist ↔ версии apt»; 1.29.0 — путь апгрейда только вместе с легальным protobuf 4.x |
| Р4 | Отмена распознавания | сообщение/kill воркера | `RunOptions.terminate` + точки между сегментами, ≤1 с; kill+reload = непрохождение S14 | **#2** | S3 доказывает ≤1 с для GigaAM и Whisper; kill+reload — только аварийное восстановление |
| Р5 | Смена активной модели | смоук новой ревизии, R13: если `MemAvailable` < 2× — выгрузить текущую | временный второй воркер с кандидатом, старая обслуживает записи; пик двух моделей ≤600 МБ не влезает | **#2 + порог по каталогу** (после челленджа Н1/Н2): переключение на границе сессии; кандидат грузится во временный воркер, если `MemAvailable ≥ min_ram_mb(кандидата из каталога) + 200 МБ`; иначе — **отказ с объяснением** и явная кнопка «Переключить с паузой» (выгрузка → загрузка, диктовка недоступна ≈5 с, состояние `switching`). Автоматической выгрузки нет — F7.5 соблюдён | PRD §8 «≤600 МБ» = установившийся режим с моделью по умолчанию; для других моделей — `min_ram_mb` карточки |
| Р6 | Сеть | `python3-requests` в `QThreadPool` | `QNetworkAccessManager` | **#1** (requests) + правила #2 | jitter 0–10/0–30 мин, ETag/304, `Retry-After`/RateLimit, 403/429 без циклов, deadline 3 с с редиректами, системный CA + `SSL_CERT_FILE`, редиректы только на разрешённые хосты |
| Р7 | Позиция пилюли | `availableGeometry` | + проверка панелей через EWMH struts | **оба** | S1 проверяет панель > 48 px и боковые панели; расхождение `availableGeometry` ↔ struts → struts |
| Р8 | Шрифты | R11 «нет на ALSE — вкладывать» | выбирать через `QFontDatabase` | **Depends: `fonts-pt-root-ui`, `fonts-pt-mono`** (в repository-main), в пакет не вкладывать; резерв PT Astra Sans через `QFontDatabase` | факт Юрки 2026-09-09: `apt-cache policy` — кандидат 1.001/1.003 из download.astralinux.ru |
| Р9 | Ручная проверка обновлений | разрешена | разрешена, S9-A5 назван ошибкой | **разрешена** (решение 2026-09-08, F12 уже поправлен) | **PRD S9-A5 поправить** под F12 |
| Р10 | Меню трея | рисует Plasma (DBusMenu) — стиль спеки §9.2 недостижим | — | **#1**: меню системное; DesignReviewer сверяет состав и неактивность пунктов, не стиль | запись в спеку §9.2 |
| Р11 | Валидация вех | команды pytest/скрипты | `tools/validate <тема>` и `tools/benchmark` как CLI | **#2** поверх #1 | CLI создаются вместе с функцией; в execution-pack команды из них |
| Р12 | Загрузчик/манифест — защита | sha256, `/resolve/{rev}/`, атомарность, смоук | + запрет `..`/абсолютных путей/symlink при импорте, лимиты (≤64 файлов/модель, ≤2 ГиБ/файл, каталог ≤1 МиБ), git-SHA1 ≠ sha256, диск ×1,2, зеркала отдают одинаковые байты | **объединить** | тесты на вредоносные пути в unit |
| Р13 | CI | lint/unit/xvfb/engine/deb/release | + ABI-pipeline (ELF inventory, readelf, реальный инференс), packaging install→upgrade→purge, две сборки на воспроизводимость, Actions по SHA, секрет подписи вне PR-job | **объединить** | — |

Корневой развилки (разные последствия для продукта) **нет** → ⛔ G4 не требуется.

## 4. Синтез-план (базовый #1 + заимствования)
- **Процессы:** GUI (`astra-voice`, PyQt5 + QtQuick Controls 2, трей, хоткей, вставка, сеть, обновлятор) ∥ воркер
  (`astra-voice.worker`: `PulseSimpleSource` → кольцевой буфер → VAD → `OnnxAsrEngine`; модель в памяти; `measure`) ∥
  одноразовый `update-helper` (root). IPC — `socketpair`, len-prefixed JSON (`hello` с версиями протокола/сборки/рантайма,
  `model.load/unload`, `record.start/stop/cancel`, `transcribe.file`, `measure`; события `state/level/silent/limit/result/
  empty/error/model.loaded/measure/cancelled`). Отменённый `utterance_id` никогда не вставляется.
- **Компоненты и модули** — таблица #1 §3 (30 модулей) целиком; дополнения из #2: `Отмена ASR` (RunOptions), `MicRecovery`
  (сырой пик до VAD, перезапуск WirePlumber только кнопкой), `TrackResolver` (детект `DIGSIG_ELF_MODE` как данные, «не знаю» → B).
- **Рантайм и каталог:** `onnx-asr` 0.12.0 + ORT 1.29.0 (пины sha256, `packaging/wheels.lock`); раскладки манифеста — #1 §4.1
  (7 layout), `engine: onnx-asr` для всех 12; размеры для №2–3 пересчитать по istupakov-репо (#2). Манифест — правила #2
  (закрытый enum layout, полный `files[]`, `/resolve/{sha}/`, подпись каталога → схема → пути → sha256).
- **Безопасность:** инварианты #1 §6 (10) ∪ #2 (12): helper не доверяет GUI, не принимает URL/аргументы apt; policy применяется
  к операциям; неподписанное не активируется; аудио только в ОЗУ; хосты из PRIVACY; иконка «слушаю» обязательна. → **T1**.
- **Упаковка:** #1 §7 (структура, Depends из repository-main + `fonts-pt-root-ui`, `fonts-pt-mono`; 3 `.so`; `python3 -I`
  лаунчер; vendor в `/usr/lib/astra-voice/vendor`) + гейты #2 (ELF-инвентарь, ABI-аудит, воспроизводимость, SBOM CycloneDX,
  hardening Actions). Зеркало весов — релиз `models-YYYY.MM`.
- **Вехи:** M0 спайки S1–S5 (#1) с датами #2 (до 13.09) → M1 скелет/упаковка → M2 воркер/движок → M3 звук → M4 цикл
  диктовки → M5 онбординг/первая модель → **v0.1 30.09** → M6 каталог → M7 сеть/проверки → M8 обновлятор A → M9 автозапуск/
  уведомления/звуки/разделы → **v0.2 15.10** → M10 корпоративный контур/трек B → M11 полировка → **v1.0 31.10**; v1.1 — ГОСТ.
  Пороги: S3 p95 инференса ≤ 300 мс (иначе дефолт e2e_ctc, затем `SherpaEngine`); отмена ≤ 1 с; RSS воркера ≤ 450 МБ.

## 5. Что ещё нужно от человека (не G4 — рутинные допуски)
1. Спайк S3 качает модели с HF (226 МБ минимум, ~680 МБ для трёх вариантов GigaAM) в scratch-venv — «да» на трафик/диск.
2. Спайк S2 (polkit-помощник) требует **одного `sudo`** на машине: положить тестовый `.policy` и помощник. Делать на машине
   заказчика или готовить ВМ ALSE 1.8 (её же — для Fly-сессии S1 и чистого прогона Ц3)?
3. Владелец закрытого ключа GPG для подписи релизов: заказчик (ключ у него, CI подписывает через секрет) или Юрка/репо.

## 6. Правки в документах по итогам суда
- PRD: S9-A5 → ручная проверка разрешена (как F12); §8 память — «≤600 МБ в установившемся режиме; пик при смене модели
  ограничен `MemAvailable`, не константой»; §7.3 пример манифеста — `engine: onnx-asr` для всех записей.
- Спека §9.2: меню трея — системное (DBusMenu), сверка по составу. Spec §8.1 — struts.
- `decisions/log.md`: запись «Архитектура принята (синтез)» после челленджа Astra и T1.

## 7. Правки по челленджу Astra (P12 шаг 4b, один круг; `arch/challenge-astra.md`)

| № | Серьёзность | Решение в синтезе |
|---|---|---|
| И1 | blocker | IPC: `recognize{utterance_id}`; воркер держит `RunOptions` на каждое выполняемое распознавание; `record.cancel{utterance_id}` → `terminate=True` того же экземпляра + точки отмены между сегментами; `Engine.transcribe(audio, cancel_token)`; событие `cancelled{utterance_id}`; поздний `result` с отменённым id GUI отбрасывает. S3 доказывает ≤1 с |
| И7 | blocker | Единый бутстрап `/usr/lib/astra-voice/bootstrap.py` (вставляет vendor в `sys.path`, диспетчер `app|worker|helper`); GUI: `python3 -I bootstrap.py app`; воркер: `[sys.executable, '-I', bootstrap, 'worker']` с `pass_fds`; `PYTHONPATH` не используется |
| И8 | blocker | Root-помощник сам читает `/etc/astra-voice/policy.conf` и `/etc/digsig/digsig_initramfs.conf` (как данные) и **отказывает**, если `updates=admin`/`offline` или `DIGSIG_ELF_MODE≠0`, независимо от GUI; права инициатора обеспечивает polkit `auth_admin`, группы helper не проверяет |
| Н1 | blocker | см. Р5 (отказ вместо выгрузки; пауза — только явным действием) |
| Н4 | blocker | см. Р3 (ORT 1.24.4; гейт METADATA↔apt) |
| Н5 | blocker | Helper: при `policy.offline` или отсутствии сети — `apt-get install --no-remove --no-download ./deb`; недостающие зависимости → сообщение «передайте администратору»; в обычном режиме apt может скачать зависимости из репозиториев ОС — это отдельная системная стадия, раскрыта в PRIVACY; GUI не передаёт флагов |
| И2 | major | Автомат воркера: `idle · recording · processing · recording+processing` (очередь 1); буфер PCM — на каждую `utterance_id`, очищается по её завершению |
| И3 | major | `model.load{id, revision, dir, layout, variant, threads}` → `model.loaded{id, revision, variant, load_ms, engine_version}` (значения из запроса + рантайма) |
| И4 | major | Устройство у воркера: `audio.close` (воркер закрывает источник, ack) → GUI выполняет `systemctl --user restart wireplumber` (QProcess) → воркер открывает лениво на следующем `record.start`; событие `audio.ready` |
| И5 | major | `requests` с `stream=True`: общий deadline 3 с по `time.monotonic()` в цикле чтения + `cancel` (threading.Event) между чанками; offline → закрытие ответа/сессии; для загрузок — inactivity timeout 30 с |
| И6 | major | `verify=policy.ca_bundle or '/etc/ssl/certs/ca-certificates.crt'` (системный CA, не certifi); уважаем `REQUESTS_CA_BUNDLE`/`CURL_CA_BUNDLE`; `allow_redirects=False` + ручной переход только на хосты из allowlist (≤3 переходов) |
| Д1 | major | PRD §7.3 переведён на istupakov (ревизия 4); `files[]` всегда генерирует `build_manifest.py` из HF API — полный список (ONNX, tokenizer, vocab/config, external-data) |
| Д2 | major | Семантика `sources[]` по типу: `hf` → `/resolve/{revision}/`; `github_release` → `base` + `revision` = тег релиза, имена ассетов включают короткий sha; `corp` → `base` + `revision` обязателен как имя подкаталога; sha256 одинаковы для всех |
| Д3 | major | RevisionWatcher: сравнивает sha ветки **экспорт-репо** (istupakov) с `pinned_revision` установленной; кнопка «Обновить» — только по подписанному каталогу с новой ревизией; ветка оригинала `ai-sage/GigaAM-v3` — информационный сигнал |
| Д4 | major | Смена id/ревизии/потоков модели = **перезапуск воркера** (модель грузится заново в любом случае) → `VmHWM` честный; `measurements.json` ключ = (id, revision, threads, runtime, build) |
| Н2 | major | Порог по `min_ram_mb` каталога (см. Р5); `MemAvailable` — предусловие, не резерв; при OOM-kill кандидата активная модель не меняется |
| Н3 | major | Пороги памяти: воркер ≤ 450 МБ и суммарно ≤ 600 МБ — **для модели по умолчанию**; для прочих — `min_ram_mb` карточки (Whisper turbo ~2 ГБ, Multilingual Large ~1,1 ГБ) с предупреждением при `MemTotal` < 2× |
| О1 | major | Крах воркера: во время записи — PCM потерян, пилюля `error` «Распознавание перезапущено», вставки нет; во время обработки — то же; таймеры вставки отменяются; поколение воркера растёт, все `utterance_id` старого поколения аннулируются |
| О2 | major | Порядок: скачать → sha256 → `rename` набора в `<rev>/` (полный, неактивный) → смоук → `current.json`; провал смоука — набор помечается `broken` в `state.json`, старая ревизия остаётся; на старте незавершённая транзакция → перепроверка |
| О3 | major | Helper ведёт журнал `/var/lib/astra-voice/updates.log` (0644) и пишет результат в файл; GUI после перезапуска определяет версию через `dpkg-query -W`; при обрыве — сообщение «проверьте журнал», автоматического отката нет |
| О4 | major | Инвариант: запись идёт, пока виден **хотя бы один** индикатор (трей или пилюля); при потере SNI на время записи пилюля включается принудительно; потеря всех индикаторов → запись останавливается с уведомлением |
| О5 | major | `QLockFile` в `$XDG_RUNTIME_DIR/astra-voice/lock` берётся первым действием (до воркера, хоткея и записи настроек); проигравший шлёт `--show` через `QLocalSocket` и выходит 0; устаревший lock снимает `QLockFile` |

Проверено чистым (Astra): 3 ELF без шима · 7 layout ↔ enum · цепочка подписи каталога · sha256 не-LFS · settings/policy · отсутствие polkit-агента · GPG/gpgv + root-staging + `auth_admin` · ручная проверка обновлений.

## 8. Открыто до execution-pack
- **T1 модель угроз** (в работе) — И8, Н5, О3, О4 переданы security-analyst.
- **G4 «установка без прав администратора»** — разбор архитектора #1 (§14 плана #1) → выбор заказчика. Влияет на Р11
  (GUI-стек QtQuick/QtWidgets) и упаковку (второй артефакт user-bundle). До решения execution-pack не собирается.
- **Fly-сессия** — §13 плана #1 → в синтез (адаптеры, S1/S4/S5 во Fly, S17).
