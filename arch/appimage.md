# AppImage — архитектура установки, сосуществования и сборки (v0.2, вариант Б)

> 2026-09-28 · architect-claude (Fable 5.1) · для Юрки. Ветка `wip/appimage-arch` от `44a865d`.
> Вход: решения заказчика 28.09 (`decisions/log.md`: «AppImage — второй артефакт v0.2», «Состав v0.2: вариант Б»),
> `docs/plans.md` веха MA (main `bf18dc7`), `spikes/appimage/RESEARCH-2026-09-28.md`, спайк `wip/appimage-spike`
> (`SPIKE-2026-09-28.md`, `AppRun`, `build.sh`, `lock.txt`), код `src/astra_voice/{platform/autostart.py, bootstrap.py,
> core/paths.py, core/policy.py, security/verify.py, net/http.py, models/catalog.py, ui/bridges.py, app.py}`,
> `packaging/`, `scripts/release.sh`, `scripts/release_latest_json.py`, `tools/validate`, `.github/workflows/ci.yml`,
> `docs/INSTALL-ADMIN.md`, `docs/threat-model.md`. Уверенность: **В** высокая · **С** средняя · **Н** низкая.
> Итог ручной GUI-проверки заказчика 28.09 (передал Юрка): зелёный — трей и вставка работают из FUSE и из установленной
> копии; значок трея через 1,8 с (FUSE) / 2,1 с (установленная копия, первый запуск после самоустановки), модель 5,4 / 3,0 с;
> единственное замечание — `jsonschema` (§11).

## Итог

1. **`.AppImage` — носитель и установщик, работает всегда установленная копия.** Первый запуск файла копирует программу
   в `~/.local/share/astra-voice/app/<версия>-<build_id>/`, переключает симлинк `app/current`, регистрирует меню, значок и
   автозапуск, дальше запускается сам из копии. Меню и автозапуск указывают только на `app/current/AppRun` — никогда на
   `$APPIMAGE` (файл в «Загрузках» удалят) и никогда на FUSE (в 2 раза медленнее, под `nochmodx` не исполняется).
2. **Логика установки живёт в Python (`platform/userinstall.py`), `AppRun` — тонкий sh.** Один код для самоустановки
   (0.2), `--uninstall` (0.2) и самообновления (v1.0: та же функция `install_from_dir()` + переключение `current`).
   Блокировка — `fcntl.flock`, не `mkdir` с 30-секундным ожиданием.
3. **Сосуществование с `.deb` — «последняя явно установленная пользователем копия владеет меню и автозапуском»**, общий
   lock одного экземпляра и общие данные уже есть; в «О программе» видно, какая копия работает, и одна кнопка возвращает
   системную версию. Администратор может запретить трек ключом `appimage=deny` (совещательно, не техническое средство).
4. **`latest.json` версии 2 с блоком `artifacts` (`deb`, `appimage`: имя, sha256, размер)**; код выбирает артефакт по
   `paths.install_kind()`; действие «Доступна версия X» для AppImage = скачать → проверить подпись `Verifier("release")` →
   «Установить и перезапустить». Самообновление v1.0 — тот же путь без участия пользователя.
5. **OpenSSL 1.1.1k остаётся в 0.2 как принятый риск** (это сборка AlmaLinux 8 с бэкпортами, только TLS-клиент к
   allowlist-хостам, CA — хостовые, `gpgv` — хостовый). В v1.0 база Python переезжает на пакеты Debian 12 (тот же
   интерпретатор, что в `.deb`, OpenSSL 3 с хоста). Если у python-appimage есть сборка `manylinux_2_34` — дешёвая замена уже в 0.2.
6. **CI: workflow вызывает только скрипты репозитория**; включение AppImage в выпуск — файл-флаг `packaging/appimage/ENABLED`.
   Одна правка `.github/workflows/ci.yml` заказчиком 05–06.10 (список §9.4).

Оценка всего пакета 0.2: **≈ 10 дней потока** (вилка 9–11,5; без переезда OpenSSL; T1 внутри), совпадает со спайком §8.

---

## 1. Раскладка установки в `$HOME`

**Решение.**

```
~/.local/share/astra-voice/                 # = paths.data_dir(), 0700, уже есть (models/, logs/, measurements.json)
  app/                                      # только копии программы; ничего пользовательского здесь нет
    0.2.0-1c866c1789f7/                     # <версия>-<build_id> (KEY из .astra-voice-build, как в спайке)
      AppRun  opt/  usr/  .astra-voice-build  .installed-ok
    0.1.0-8f0e…/                            # предыдущая версия (откат)
    current -> 0.2.0-1c866c1789f7           # стабильная точка входа: меню, автозапуск, «О программе»
    previous -> 0.1.0-8f0e…                 # необязательный; переключается вместе с current
    .install.lock                           # flock
```

- **Стабильная точка входа — симлинк `app/current`**, а не лаунчер в `~/.local/bin`: без лишнего исполняемого файла в
  `$HOME`, без зависимости от `PATH` сеанса (каталог `~/.local/bin`, созданный после входа, в `PATH` не попадает до
  перелогина), переключение атомарное (`os.symlink(tmp)` + `os.replace(tmp, current)`), запущенный процесс не замечает смены:
  он живёт в своём `<KEY>/` (`sys.executable` внутри копии, воркер стартует через него — `worker/supervisor.py`).
  Команда `astra-voice` в терминале для AppImage-пользователей не требование; при желании — v1.0.
- **Правило старых версий:** хранятся ровно `current` и `previous`; всё остальное в `app/`, что подходит под маску
  `^\d+\.\d+\.\d+(~[A-Za-z0-9.]+)?-[0-9a-f]{12}$`, плюс `.tmp-*`, удаляется **при установке новой версии** (не при
  старте). Ничего другого чистка не трогает: модели (`../models`), журналы, `settings.json` лежат вне `app/`, а маска и
  запрет следовать по симлинкам защищают от случайного `rm -rf`. Работающую копию защищает файл
  `runtime_dir()/running-key` (пишет `app.py` при старте): версия из него не удаляется, даже если уже не `current`/`previous`.
  Цена: 2 × 267 МБ ≈ 0,55 ГБ (вопрос В1).
- **Блокировка самоустановки:** `fcntl.flock(app/.install.lock, LOCK_EX)` с ожиданием ≤ 60 с (в `userinstall.py`);
  блокировка исчезает вместе с процессом, зависших `.lock-<KEY>` не бывает. После получения замка — повторная проверка
  «уже установлено». Свободное место: `os.statvfs(app)` ≥ 350 МБ, иначе «Недостаточно места в домашней папке: нужно ещё N МБ».
- **Порядок установки** (`userinstall.install_from_dir(src, key)`): `flock` → `cp` в `app/.tmp-<KEY>.XXXX` (Python
  `shutil.copytree(symlinks=True)`, режимы файлов сохраняются) → маркер `.installed-ok` → **смоук новой копии**
  `<tmp>/AppRun --version` == ожидаемая версия → `os.rename(tmp, app/<KEY>)` → `previous := current` (если был и ≠ KEY)
  → `current := KEY` (атомарно) → чистка по правилу выше → регистрация (§4). Смоук перед переключением — это и есть
  «атомарная замена» для самообновления v1.0: сломанная сборка не становится `current`.
- **Запуск файла всегда делает его версию текущей.** Повторный запуск того же `.AppImage` — быстрый путь (`installed_ok`
  → `exec app/<KEY>/AppRun`, ≈ 20 мс + Python); запуск старого файла — откат (переустановка старой версии как `current`).
- **Не устанавливать** — только явно: `ASTRA_VOICE_PORTABLE=1` (замена `ASTRA_VOICE_SELFINSTALL` спайка, инвертированная):
  запуск из монтирования без записи в `$HOME` (для проверок и администраторов). Служебные флаги `--version`, `--help`,
  `--stats`, `--selfinstall-status` тоже не устанавливают (закрывает спайк §7.10).

**Альтернативы (одной строкой).** Лаунчер `~/.local/bin/astra-voice` — лишний исполняемый файл и зависимость от `PATH`
сеанса; `current` как настоящий каталог с переименованием — работающий процесс потерял бы свой путь; только `current` без
`previous` — дешевле по диску, но чистка удалит работающую версию при обновлении из GUI и нет отката для v1.0.

**Оценка.** 1,5 дня (`userinstall.py` + `bootstrap selfinstall` + тонкий `AppRun` + юнит-тесты).

**Тест.** T-164 (unit, временный `HOME`): установка → `current`/`previous`/чистка по маске; повторная установка того же KEY
не копирует; параллельный запуск двух установок — вторая ждёт flock и берёт готовую копию; нехватка места → сообщение и
ничего не создано; `app/` — симлинк → отказ; смоук новой копии провален → `current` не переключён, `.tmp` убран;
`running-key` защищает работающую версию. T-170 (smoke.sh): `--version` из `--appimage-extract-and-run` не создаёт `app/`.

## 2. Сосуществование с `.deb`

**Факты (В).** Один desktop ID `astra-voice.desktop`: `~/.local/share/applications` перекрывает `/usr/share/applications`;
один файл автозапуска `~/.config/autostart/astra-voice.desktop`; общий lock `$XDG_RUNTIME_DIR/astra-voice/lock`
(`app.py:816`, `QLockFile`) — второй экземпляр любого трека шлёт `--show` первому и выходит; общие `~/.config/astra-voice`,
`~/.local/share/astra-voice/models`, `~/.local/state/astra-voice`. `Settings` держит незнакомые ключи в `extra`
(`core/settings.py:91–101`) — версии переживают друг друга в обе стороны.

**Решение — правило (б) «пользователь выбрал — пользователь владеет», с явным обратным ходом.**

| Ситуация | Меню | Автозапуск | Что видит пользователь |
|---|---|---|---|
| Только `.deb` | системная запись | своя запись `Exec=/usr/bin/astra-voice --hidden` (как в v0.1) | «Установка: пакет (администратором)» |
| Только AppImage | `~/.local/share/applications/astra-voice.desktop` → `app/current/AppRun` | своя запись → `app/current/AppRun --hidden` | «Установка: в домашнюю папку (AppImage)» |
| Стоят оба, AppImage установлен позже | пользовательская запись перекрывает системную → AppImage | своя запись перенацелена на `app/current/AppRun` (только если запись **наша**, чужую не трогаем) | «Установка: AppImage. Есть и системная версия X — [Использовать системную версию]» |
| Стоят оба, `.deb` поставлен позже | без изменений: меню и автозапуск продолжают запускать AppImage | без изменений | то же; если системная новее — «Системная версия новее (X)» + та же кнопка |
| Пользователь нажал «Использовать системную версию» | пользовательская запись удалена → системная | своя запись перенацелена на `/usr/bin/astra-voice` | «Установка: пакет»; копии в `app/` можно удалить второй кнопкой |

- Почему не (а) «при наличии `/usr/bin/astra-voice` AppImage не регистрируется без согласия»: для этого нужен диалог до
  запуска Qt (в sh его нет), а типовой сценарий трека — пользователь без прав администратора, который сам решил поставить
  программу; воля администратора выражается политикой (§5), а не тихим отказом.
- Как программа узнаёт, какая копия работает: `paths.install_kind()` → `InstallKind.DEB | APPIMAGE | SOURCE` по
  `ASTRA_VOICE_APPIMAGE_DIR` (ставит `AppRun`) с запасной проверкой маркера `.astra-voice-build` вверх от `bootstrap.py`
  и `here.is_relative_to(INSTALL_LIB_DIR)` (как сейчас в `resource_root()`); в «О программе» (`qml/sections/About.qml`,
  `AppInfo` в `app.py:364`) — строка «Способ установки» и версия; версию системной копии AppImage узнаёт чтением
  `/usr/lib/astra-voice/astra_voice/_version.py` (без `dpkg-query`, без дочернего процесса).
- Понижение версии (AppImage 0.2 после `.deb` 0.3) не запрещаем, но `catalog-state.json` (анти-откат каталога) и
  `settings.extra` переживают его; в «О программе» — та же подсказка «Системная версия новее».

**Альтернативы.** (а) приоритет администратора — см. выше; отдельные каталоги данных для AppImage — дублирование моделей
(226 МБ) и разъезд настроек, отвергнуто (ресёрч §7, практика Krita).

**Оценка.** 1 день (детект трека, «О программе», две кнопки, перенацеливание записи).

**Тест.** T-163 (unit): `install_kind()` по env/маркеру/пути. T-166 (unit, временный `HOME` + фиктивный
`/usr/bin/astra-voice` через параметр): регистрация при стоящем `.deb` перенацеливает **только** нашу запись; чужая запись
(`Exec=sh -c "sleep 8; …"`) — байт в байт; «Использовать системную версию» возвращает `Exec=/usr/bin/astra-voice`, без
`.deb` — удаляет запись; модели и `settings.json` в фикстуре нетронуты. T-173 (integration): экземпляр с lock из одного
трека + запуск другого → `--show`, второй процесс не стартует. Руками (М): оба стоят → меню запускает AppImage, после
кнопки — пакет.

## 3. Автозапуск

**Решение.**
- Запись AppImage-трека: `Exec="<XDG_DATA_HOME>/astra-voice/app/current/AppRun" --hidden`, `TryExec=<тот же путь>`,
  `X-KDE-autostart-after=panel`, `X-AstraVoice-Managed=true`, `NoDisplay=true`, `Icon=astravoice` — те же строки, что в
  `_ENTRY` (`platform/autostart.py:17–28`), меняется только исполняемый файл. Путь в `Exec` экранируется по спецификации
  Desktop Entry (кавычки); `TryExec` пишется, если в пути нет пробелов и кавычек (иначе опускается — запись автозапускается
  без проверки; на ALSE логины POSIX, случай теоретический). Не `$APPIMAGE`: файл переедет или будет удалён, а
  запуск через FUSE вдвое медленнее и не работает под `nochmodx`. `TryExec` даёт тихий пропуск, если пользователь снёс `app/`.
- `autostart.py`: константа `EXECUTABLE` → функция `executable() -> str` из `core/paths.py`: `DEB`/`SOURCE` →
  `/usr/bin/astra-voice`; `APPIMAGE` → `str(paths.appimage_current_apprun())` (= родитель `ASTRA_VOICE_APPIMAGE_DIR` / `current/AppRun`);
  портативный режим (§1, не установлен) → путь `$APPIMAGE` в кавычках. `_ENTRY` становится `entry_bytes()`.
- **Сверка при смене трека — только при регистрации, не при старте.** Правило v0.1 «при старте файлы автозапуска не
  пишутся» (`INSTALL-ADMIN.md` §6, У27, PRD F10.1) сохраняется. Новая функция `autostart.retarget()` вызывается из
  `userinstall.register()`/`unregister()`: если пользовательская запись **наша** (`X-AstraVoice-Managed=true`) — заменяет
  только строки `Exec`/`TryExec`, сохраняя `Hidden=true`, порядок строк, права; чужую запись не трогает (как сейчас — только
  `Hidden`). При старте — только чтение: `AutostartState` получает поле `target: "ours-this" | "ours-other" | "foreign" | "system" | "none"`,
  и «О программе»/«Общие» показывают подсказку «Автозапуск запускает другую копию» (без записи в файл).
- `ensure_autostart_after_onboarding` (`ui/bridges.py:467`) и `set_enabled` не меняются: они уже берут содержимое записи
  из модуля, а тот — из `executable()`.

**Альтернативы.** Сверять и переписывать при каждом старте — нарушает У27/PRD F10.1 и правило заказчика про чужую запись;
`Exec=astra-voice` через лаунчер в `~/.local/bin` — зависимость от `PATH` сеанса (§1).

**Оценка.** 0,5 дня.

**Тест.** T-165 (unit): содержимое записи по треку; экранирование пути с пробелом; `retarget()` сохраняет `Hidden=true`,
CRLF/BOM и права чужой записи не трогает (расширение T-148/T-28 на новый трек); `state().target` для пяти случаев.
Руками (М): перелогин в KDE → значок в трее из `app/current`; Fly — чеклист коллеги 12.10.

## 4. Меню, значок, удаление

**Решение.**
- `userinstall.register()` пишет: `~/.local/share/applications/astra-voice.desktop` (шаблон `packaging/appimage/astra-voice.desktop`
  = `data/astra-voice.desktop` с `Exec="<app>/current/AppRun"`, `TryExec=<то же>`, плюс `X-AstraVoice-Managed=true`,
  `X-AppImage-Version=<версия>`); значки приложения `~/.local/share/icons/hicolor/<size>/apps/astravoice.*` — копии из
  `usr/share/icons/hicolor` бандла (только `apps/astravoice.*`; значки трея программа берёт из бандла через
  `paths.icon_theme_dir()`, в тему пользователя они не нужны). Атомарная запись как в `autostart._write`. KDE видит
  изменения сразу (KSycoca следит за каталогом); Fly — **не проверено**, пункт чеклиста 12.10; запасной ход, если меню Fly
  не читает пользовательский каталог, — ярлык на рабочем столе (`~/Desktop`, во Fly это `~/Desktops/Desktop1`), +0,25 дня.
- Вызов регистрации: `AppRun` после успешной самоустановки делает `exec app/<KEY>/AppRun --register "$@"`; `app.py`
  обрабатывает `--register` до GUI (идемпотентно) и запускается как обычно. Обычный старт (`--hidden` из автозапуска) ничего
  не пишет. Один раз на KEY — уведомление «Astra Voice установлен в домашнюю папку и добавлен в меню. Файл .AppImage больше
  не нужен» (маркер в `state_dir()`), для новых пользователей это сообщение — в мастере.
- **Удаление** — раздел «О программе» (только для трека AppImage), две кнопки простым языком:
  1. «Убрать из меню и автозапуска» — `unregister()`: удаляет **нашу** запись меню и наши значки, автозапуск перенацеливает
     на `/usr/bin/astra-voice`, если пакет стоит, иначе удаляет нашу запись (чужую — не трогает).
  2. «Удалить программу из домашней папки» — то же плюс `app/` целиком: не работающие версии сразу, работающую — при выходе
     (`atexit` после остановки воркера), затем выход. Текст: «Модели и настройки остались: `~/.local/share/astra-voice/models`,
     `~/.config/astra-voice` — удалите их сами, если больше не нужны» (та же таблица, что в `INSTALL-ADMIN.md` §8).
  Та же операция из терминала: `~/.local/share/astra-voice/app/current/AppRun --uninstall` (для инструкций и администратора,
  который чистит чужой профиль — только под тем же пользователем).

**Альтернативы.** Регистрация в sh внутри `AppRun` — дублирование логики «своя/чужая» запись; `appimaged`/AppImageLauncher —
ставятся root, не полагаемся (ресёрч §6); без кнопки удаления — противоречит правилу «установка без администратора должна
и сниматься без него».

**Оценка.** 1 день.

**Тест.** T-166 (см. §2) + проверка значков и `.desktop`; `unregister()` не удаляет ничего вне `applications/astra-voice.desktop`,
`icons/hicolor/*/apps/astravoice.*`, `autostart/astra-voice.desktop` (наша), `app/`; `--uninstall` из smoke.sh во временном
`HOME` оставляет `models/` и `settings.json`. Руками (М, KDE): пункт в меню появляется без перелогина, после «Удалить» исчезает.

## 5. Политика администратора

**Факт.** `core/policy.py` читает абсолютный `/etc/astra-voice/policy.conf` (`POLICY_PATH`) — из AppImage это тот же
файл хоста, тем же кодом; M7-ядро добавит проверку владельца/прав (У16/У37) — одинаково для обоих треков.

**Решение.** Новый ключ `appimage = allow | deny` (по умолчанию `allow`), действует в 0.2 минимально:
- `app.py` сразу после `policy_mod.load()` (`app.py:828`): `deny` → журнал, уведомление/stderr «Администратор запретил
  версию, установленную в домашнюю папку. Программу на этом компьютере устанавливает администратор (пакет .deb)», код выхода 3;
  самоустановка (`bootstrap selfinstall`) при `deny` не копирует ничего.
- `AppRun` — быстрый предварительный отказ до копирования 267 МБ: `grep -Eiq '^[[:space:]]*appimage[[:space:]]*=[[:space:]]*(deny|no|false|0)'`
  по файлу, если он читаем; окончательное слово — за Python.
- Невалидный файл (`invalid`): для сети остаётся fail-closed (У37), для `appimage` — как `allow` (запуск не сетевая
  операция; пользователь на машине с испорченной политикой не должен остаться без программы); в журнал.
- `INSTALL-ADMIN.md` §5: ключ, и честно — **это совещательный запрет**: владелец копии может её изменить; технический
  запрет пользовательских исполняемых файлов — средства ОС (ЗПС, `astra-nochmodx-lock`, `noexec` на `/home`), при которых
  AppImage не работает по определению.

Надо ли в 0.2: да — 0,25 дня, а без ключа у администратора парка нет документированного ответа «как запретить». Ключи
`updates=admin`/`offline` (PRD F14) начнут действовать на самообновление в v1.0 (MA-2).

**Альтернативы.** Отказ при `profile=secure` автоматически — неожиданно для существующих политик; ничего не делать в 0.2 —
дешевле на 0,25 дня, но вопрос от администраторов неизбежен.

**Оценка.** 0,25 дня.

**Тест.** T-167 (unit): `policy.conf` с `appimage=deny` → `main()` возвращает 3 до создания `QApplication` в треке
APPIMAGE и ничего не делает в треке DEB; `invalid` → запуск разрешён; smoke.sh: `AppRun` с подменённым файлом политики
(параметр `ASTRA_VOICE_POLICY_FILE` только для тестов) не создаёт `app/`.

## 6. M7 для AppImage в 0.2 и задел под самообновление v1.0

**`latest.json` версии 2** (`scripts/release_latest_json.py`; потребителей у версии 1 нет, `tools/validate release` правится):

```json
{
  "schema": 2,
  "version": "0.2.0",
  "published_at": "2026-10-15T09:00:00Z",
  "min_astra": "1.8",
  "release_url": "https://github.com/ArrivaRUS/astra-voice/releases/tag/v0.2.0",
  "artifacts": {
    "deb":      {"name": "astra-voice_0.2.0_amd64.deb",          "sha256": "…", "size": 15728640},
    "appimage": {"name": "Astra_Voice-0.2.0-x86_64.AppImage",     "sha256": "…", "size": 81226232}
  }
}
```

`artifacts.appimage` присутствует только при `packaging/appimage/ENABLED`; `SHA256SUMS(.asc)` покрывает оба файла и оба SBOM
(`sbom.cdx.json`, `sbom-appimage.cdx.json`) — модель доверия одна: подпись `SHA256SUMS.asc` подключом мастера
`5F1F7718559F8F57FFE1178055BB1162F17A5CB0` (`security/verify.py:PINNED_FINGERPRINTS`), sha256 из подписанного файла.

**Как код отличает трек:** `paths.install_kind()` → `updates/checker.py` (M7-ядро) берёт `artifacts["appimage"]` или
`artifacts["deb"]`; `SOURCE` → `deb`. Источник версии — GitHub `releases/latest` (PRD F9.1) → `latest.json` из ассетов тега.

**Куда ведёт «Доступна версия X» (действие 2b плана, без помощника):**
- `.deb`: «Скачать пакет» → `~/.cache/astra-voice/updates/<версия>/` (0700): deb + `SHA256SUMS` + `.asc` →
  `Verifier("release").verify_detached` → `verify_sha256sums` → «Открыть папку» + «Установку выполняет администратор:
  `sudo apt install ./…`».
- AppImage: «Скачать новую версию» → те же три файла → та же проверка → `chmod 0755` **только после** проверки →
  «Установить и перезапустить»: `Popen([файл], start_new_session=True, env=clean_env())` → `app.quit()`. Новый процесс сам
  устанавливает версию, переключает `current`, регистрируется и стартует (§1). Если lock ещё занят (гонка с выходом) —
  новый процесс шлёт `--show` старому и выходит, но `current` уже переключён: следующий запуск из меню — новая версия;
  для пользователя всегда доступна и ссылка `release_url`.
- Понижение версии не предлагается (SemVer > текущей), `T-116` (часы) — общая для треков; `policy: offline`/`invalid` — сеть
  запрещена, как сейчас (`net/gate.py`).

**Задел под самообновление v1.0 (MA-2)** — новые части не нужны, только связка существующих:
1. загрузка в `app/.tmp-download/` (тот же том, что `app/` → атомарный `rename`), проверка `Verifier` (gpgv хостовый,
   `/usr/bin/gpgv`, пин мастера) — как в 2b;
2. распаковка без FUSE: `<файл> --appimage-extract` в `app/.tmp-<KEY>.X` (runtime умеет это без монтирования; работает и при
   запрете исполнения из FUSE, но не при `noexec`/`nochmodx`) — либо `unsquashfs -o <offset>`, если появится в Depends;
3. `userinstall.install_from_dir()` — смоук `--version`, `previous := current`, `current := KEY`, чистка;
4. «Перезапустить» по согласию; откат — «Вернуть предыдущую версию» = `current := previous` (в 0.2 не показываем);
5. `policy.conf: updates=admin | offline` выключает; `.upd_info`/zsync в образ не встраиваем (ресёрч §5).

**Альтернативы.** Плоские ключи `appimage`, `appimage_sha256` в `latest.json` — не расширяемо (архитектура aarch64, второй
трек); только ссылка на страницу выпуска без скачивания — проще на 0,5 дня, но пользователь без проверки подписи и без
единого пути с `.deb`; полноценное самообновление в 0.2 — отвергнуто заказчиком (вариант Б).

**Оценка.** 0,5 дня в потоке 2 сверх плановых 0,75 (`latest.json` v2 + выбор артефакта + запуск нового файла); `tools/validate` — в §9.

**Тест.** T-168 (unit): генератор и `validate release` — с AppImage и без; несовпадение sha256/размера → ошибка;
`SHA256SUMS` без строки AppImage при `ENABLED` → «не покрыты». T-174 (unit, фиктивный `Popen`/`Verifier`): `chmod` не
раньше проверки; неподписанный/битый файл удалён и «Файл не прошёл проверку»; понижение версии не предлагается; запуск
идёт с очищенным окружением. Руками (М): rc1 → rc2 через строку «Доступна версия» в KDE.

## 7. OpenSSL в бандловом Python

**Факты (проверено на машине заказчика, только чтение).** В AppDir спайка `usr/lib/libssl.so.1.1` и `libcrypto.so.1.1` —
`OpenSSL 1.1.1k FIPS 25 Mar 2021`: сборка AlmaLinux 8 (образ `manylinux_2_28`), Red Hat/Alma бэкпортируют исправления
безопасности в 1.1.1k до конца поддержки RHEL 8 (05.2029) при неизменной строке версии; upstream-ветка 1.1.1 закрыта
11.09.2023. `_ssl`/`_hashlib` слинкованы с ними через `RUNPATH=$ORIGIN/../../../../../usr/lib`. На хосте ALSE 1.8.5 —
только `libssl.so.3` (`libssl3 3.4.0-2-astra9`), пакета 1.1 нет; `/etc/ssl/certs/ca-certificates.crt` есть
(`ca-certificates 20230311` + `ca-certificates-local` — корпоративные CA попадают в тот же бандл); `gpgv 2.2.40` хостовый.

**Что зависит от бандлового OpenSSL:** TLS-клиент `requests` (проверка обновлений, загрузка моделей — только `https`,
allowlist хостов, проверка сертификата всегда включена: `net/http.py:117 _verify_bundle`) и `hashlib.sha256` через
`_hashlib`. **Не зависит:** проверка подписей — `/usr/bin/gpgv` хоста (`security/verify.py:GPGV_PATH`).

**Варианты.**

| | Вариант | Дней | Плюсы | Минусы |
|---|---|---|---|---|
| A | Оставить python-appimage `manylinux_2_28` (1.1.1k+бэкпорты Alma 8) | 0 | ничего не менять к 15.10 | upstream-EOL — замечание T1 гарантировано; свежесть зависит от перепина файла python-appimage перед каждым выпуском |
| B | python-appimage `manylinux_2_34` (Alma 9, OpenSSL 3.0.x+бэкпорты; glibc 2.34 ≤ 2.36 ALSE 1.8) | 0,5 | замена одной строки `appimage.lock` + гейт `--max-glibc 2.34` | **есть ли такая сборка — не проверено (Н)**, нужна сеть; ALSE 1.7 (glibc 2.28) отпадает — не цель |
| C | Python из пакетов Debian 12 (`dpkg-deb -x python3.11-minimal libpython3.11-minimal libpython3.11-stdlib` + 5–6 библиотек), OpenSSL 3 — с хоста | 1–1,5 | тот же интерпретатор 3.11.2, что в `.deb`; TLS сопровождает ALSE; CA-пути родные; нет зависимости от проекта python-appimage; входы — подписанный архив Debian | перепись базовой части `build.sh`; относительность `sys.prefix` Debian-сборки проверить (С); порог glibc 2.36 (только 1.8) |
| D | Своя сборка CPython | 2–3 | полный контроль | сопровождение своими силами — нет |
| E | Пересобрать только `_ssl`/`_hashlib` под OpenSSL 3 | 1–1,5 | точечно | хрупко (заголовки/ABI конкретной сборки) — нет |

**Рекомендация.** 0.2 — **A**, с **B**, если сборка `manylinux_2_34` существует (Юрка/CI проверяют 29–30.09 при наличии сети);
v1.0 — **C** вместе с MA-2 (самообновление делает трек полностью сетевым, и Debian-база убирает третью сторону из цепочки
доверия). Для T1 — формулировка принятого риска: бандловая TLS-библиотека 1.1.1k с бэкпортами Alma 8, только клиент,
только allowlist-хосты, проверка сертификата всегда, подпись — хостовым `gpgv`; перепин python-appimage перед каждым
выпуском; SBOM AppImage содержит компонент «openssl 1.1.1k (Alma 8, python-appimage <дата>)».

**CA.** Хостовые: `AppRun` ставит `SSL_CERT_FILE=/etc/ssl/certs/ca-certificates.crt` (как в спайке), `net/http.py` и так
предпочитает этот файл, `policy.conf: ca_bundle` — выше; `certifi` из бандла — только если хостового файла нет.
Для дочерних процессов `SSL_CERT_FILE` безвреден и остаётся.

**Тест.** T-169 (гейт сборки, в изоляции): `ssl.OPENSSL_VERSION` печатается и попадает в SBOM; `ssl.create_default_context()`
с `SSL_CERT_FILE` контейнера загружает > 100 корневых сертификатов; при варианте C — `sys.prefix` внутри AppDir и
`import ssl` берёт `libssl.so.3` хоста (`readelf`/`ldd` в гейте). Ц4 (`tcpdump`) — общий с `.deb`.

## 8. PARSEC / `nochmodx`: что говорим и нужен ли установщик

**Факты.** У заказчика `parsec.enable_exec_on_fuse=N`, `/parsecfs/nochmodx=0` — запуск из FUSE работает (спайк §4).
При включённом `astra-nochmodx-lock` (или параметре ядра): runtime монтирует образ, `execv AppRun` падает, в терминал —
`execv error: …`, по двойному щелчку — ничего; `AppRun` управления не получает. Кроме того, под `nochmodx` пользователь
**не может поставить бит исполнения на скачанный файл** — то есть не запустит и `--appimage-extract-and-run`; выдержит ли
распаковка/копирование режимов файлов сам запрет — **данных нет (Н)**, стоковой ВМ у заказчика нет (ответ 28.09).
`noexec` на `/home` и ЗПС — не работает ничего (ресёрч §4).

**Решение.**
- **Отдельный скрипт-установщик не нужен:** установщик — сам `.AppImage` (режим В `appimage_extracted_*` → самоустановка,
  временная распаковка удаляется, спайк §9.4). Достаточно одной документированной команды на случай «FUSE запрещён, но
  бит исполнения ставить можно»:
  ```sh
  chmod +x Astra_Voice-0.2.0-x86_64.AppImage
  TMPDIR="$HOME/.cache" ./Astra_Voice-0.2.0-x86_64.AppImage --appimage-extract-and-run
  ```
  `TMPDIR` в домашней папке — на случай `noexec` на `/tmp` и против подсадки предсказуемого `/tmp/appimage_extracted_<md5>`
  другим пользователем (вход T1, §10); после установки последующие запуски FUSE не используют.
- **Текст пользователю** (страница выпуска, README, уведомление не показать — программа не стартует): «Если после
  двойного щелчка ничего не происходит, откройте терминал в папке с файлом и выполните две команды выше. Если и это не
  помогло — на компьютере включён режим защиты, и установить программу может только администратор (пакет .deb)».
- **Текст администратору** (`INSTALL-ADMIN.md`, новый раздел «Установка без прав администратора (AppImage)»): где трек
  не работает — ЗПС, `astra-interpreters-lock`/`astra-nochmodx-lock`, `noexec` на `/home`, `parsec.enable_exec_on_fuse=0`
  (при нём — команда выше); что программа кладёт в `$HOME` (§1) и как это снять (§4); ключ `appimage=deny` (§5);
  «на системе с запретом запуска из FUSE не проверено» — так и пишем в заметках к выпуску (решение 28.09).
- В `AppRun` при неудаче самоустановки в режиме Б (нет места и т. п.) — запуск из монтирования с уведомлением
  «Программа работает без установки»; в режиме В — только сообщение и выход.

**Альтернативы.** Патчить runtime, чтобы сам падал в extract-and-run, — чужой бинарь, теряем пин по sha256; `install.sh`
рядом с файлом — ещё один файл без бита исполнения под тем же запретом.

**Оценка.** 0,5 дня (тексты; tech-writer) + 0,25 (ветка отказа в `AppRun`).

**Тест.** T-170 (smoke.sh): режим В во временном `HOME` с `TMPDIR` внутри него → установка, распаковка удалена; отказ по
месту → в режиме Б `exec` из монтирования (эмуляция: `ASTRA_VOICE_TEST_NO_SPACE=1`). Руками (М): «двойной щелчок»
в Dolphin и во Fly-FM (чеклист 12.10 — запускает ли Fly-FM `.AppImage` вообще).

## 9. Сборка в CI

### 9.1 Куда переезжает спайк

```
packaging/appimage/
  build.sh            # из спайка; --fetch (сеть: инструменты + колёса по lock), сборка+гейты; TMPDIR=$WORK/tmp, trap → rm
  AppRun              # тонкий sh: режим А/Б/В/Г, быстрый путь, флаги без установки, policy-grep, exec bootstrap selfinstall
  astra-voice.desktop # шаблон меню (Exec=AppRun внутри образа; пользовательскую запись пишет userinstall)
  check_qml.py, check_resources.py            # как в спайке
  check_bundle.py     # новый: импорт всех модулей (как smoke-installed.sh), optional-модули из Recommends, sys.path без хоста,
                      # OPENSSL_VERSION, каталог с require_schema=True, 0 WARNING (см. §11)
  smoke.sh            # смоук готового .AppImage в изоляции: extract-and-run --version (без установки), самоустановка во
                      # временный HOME, --selfinstall-status, --register/--uninstall, GUI-смоук под xvfb, нет мусора в TMPDIR
  ENABLED             # флаг: файл есть → job appimage обязателен, release кладёт AppImage в ассеты/latest.json/SHA256SUMS
packaging/appimage.lock   # был spikes/appimage/lock.txt: # tool: … (sha256, размер, URL), колёса pip --hash, # expect-elf: N
```

`spikes/appimage/` остаётся как история (ресёрч и отчёт), скрипты из него удаляются при переносе. `measure.sh` — в
`tools/appimage-measure` (не гейт).

### 9.2 Воспроизводимость и гейты (job `appimage`, `container: debian:12`, FUSE нет → всё через `--appimage-extract` / extract-and-run)

- Входы закреплены: 4 инструмента по sha256+размеру (появится проверка подписи `runtime-x86_64.sig` ключом
  `570C77AC…6490F695`, ключ кладём в `packaging/appimage/keys/`), колёса `--require-hashes`, `SOURCE_DATE_EPOCH` из changelog,
  `touch` всех файлов, `__pycache__`/`.pyc` удалены; кэш `actions/cache` (тот же SHA, что в job `engine`) на
  `~/.cache/astra-voice-dev/appimage/{downloads,wheels}` с ключом = sha256 `appimage.lock`.
- **Мусор extract-and-run** (находка Юрки: `/tmp/appimage_extracted_43264887…`, 36 МБ от appimagetool): `build.sh`
  экспортирует `TMPDIR=$WORK/tmp` для `appimagetool` и для распаковки python-appimage, `trap 'rm -rf "$WORK/tmp"' EXIT`;
  гейт в конце — `$WORK/tmp` пуст и в `${TMPDIR:-/tmp}` нет `appimage_extracted_*` новее старта сборки.
- Гейты: `tools/elf-audit --strict --expect <из lock> --max-glibc 2.28 AppDir` (новый параметр `--max-glibc`; сейчас
  константа `MAX_GLIBC=(2,36)`); `scripts/sbom.py --appdir AppDir --lock packaging/appimage.lock --out dist/sbom-appimage.cdx.json`
  (новый режим: колёса из lock, инструменты `# tool:` как компоненты, версия OpenSSL из `libssl`, ELF со sha256);
  `check_qml.py`, `check_resources.py`, `check_bundle.py`; `smoke.sh` по готовому файлу, включая GUI-смоук под `xvfb-run`
  (старт из установленной копии, ожидание IPC-сокета, `--show`, выход; нужны хостовые `libgl1`, `libxkbcommon-x11-0`,
  `libxcb-*` в контейнере — те же, что в job `xvfb`).
- Проверка «две сборки подряд → один sha256» — один раз руками в CI; если не сходится из-за `mksquashfs` — не блокер 0.2, задача v1.0.

### 9.3 Выпуск: `release.sh`, `tools/validate`, скрипты вместо шагов

- `scripts/release_build.sh` (новый): `packaging/build-deb.sh --fetch-wheels && --dh` → установка собранного `.deb` и
  `smoke-installed.sh` (T3 P2-6: подписываем то, что проверили) → при `ENABLED`: `packaging/appimage/build.sh --fetch && build.sh`
  → `smoke.sh dist/*.AppImage`.
- `scripts/release_assets.sh dist` (новый, **без секрета** — T3 P2-2): копирует `INSTALL-ADMIN.md`, `SECURITY.md`,
  `release.gpg`; `release_latest_json.py --dist . --version …` (v2, AppImage при `ENABLED`); `sha256sum <все файлы> > SHA256SUMS`;
  пишет `dist/assets.txt` — список для публикации.
- Подпись — как сейчас (шаг с `GPG_SIGNING_KEY`), затем `gh release create "$TAG" --generate-notes $(cat dist/assets.txt)`.
- `scripts/release.sh`: печать списка ассетов из `ENABLED` (+ `Astra_Voice-*-x86_64.AppImage`, `sbom-appimage.cdx.json`,
  `SECURITY.md`), проверка, что `packaging/appimage.lock` отслеживается Git при `ENABLED`.
- `tools/validate release`: второй артефакт по имени `Astra_Voice-<версия>-x86_64.AppImage` (обязателен при `ENABLED` или
  `--expect-appimage`); проверка магии type-2 (`AI\x02` по смещению 8), размер > 50 МБ; `latest.json` схема 2 (ключи,
  sha256 и размер обоих артефактов); покрытие `SHA256SUMS` с обоими SBOM; ключевое множество ассетов расширяется.

### 9.4 Пакет правок `.github/workflows/ci.yml` — заказчик, один раз, 05–06.10

Принцип (требование PM, `docs/plans.md` MA): workflow вызывает только скрипты репозитория; правки по T1, переход на путь B
(Qt из Debian 12) и на базу Python C (§7) меняют `packaging/appimage/*`, не workflow; снятие AppImage с выпуска — удалить
`packaging/appimage/ENABLED`. Список для веб-редактора (ветку `wip/r2-ci` с полным файлом и «что сверить» готовит агент к 03–04.10):

1. Новый job `appimage` (`runs-on: ubuntu-latest`, `container: debian:12`, `timeout-minutes: 30`, без `needs`):
   `actions/checkout@<тот же SHA>` с `persist-credentials: false`; apt: `binutils file patch git python3 xvfb xauth
   fontconfig libgl1 libxkbcommon-x11-0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-render-util0 libxcb-xinerama0
   libxcb-xkb1 libdbus-1-3`; `actions/cache@caa296126883cff596d87d8935842f9db880ef25` (`path: ~/.cache/astra-voice-dev/appimage`,
   `key: appimage-${{ hashFiles('packaging/appimage.lock') }}`); шаги `packaging/appimage/build.sh --fetch`,
   `packaging/appimage/build.sh`, `packaging/appimage/smoke.sh dist/Astra_Voice-*-x86_64.AppImage`;
   `actions/upload-artifact@<тот же SHA>` (`name: astra-voice-appimage`, `path: dist/Astra_Voice-*.AppImage`,
   `dist/sbom-appimage.cdx.json`, `if-no-files-found: error`). Job запускается всегда (это поставляемый артефакт); при
   отсутствии `ENABLED` `build.sh` завершается успешно с пометкой «AppImage выключен».
2. Job `release`: `needs: [lint, unit, xvfb, engine, ci-lint, deb, appimage]`; `timeout-minutes: 35`; `checkout` с
   `persist-credentials: false`; apt дополнить пакетами из п. 1; шаги «Колёса» + «Сборка и гейты» заменить одним
   `scripts/release_build.sh`; шаг «SHA256SUMS и отсоединённая подпись» разделить: `scripts/release_assets.sh dist` (без
   `env`), затем шаг подписи с `GPG_SIGNING_KEY` (тело — как сейчас, но без `cp`/`release_latest_json`/`sha256sum`);
   публикация — `gh release create "${GITHUB_REF_NAME}" --generate-notes $(cat dist/assets.txt)`.
3. `on.push.paths-ignore` — добавить `arch/**`, `spikes/**` (документы и история не влияют на пакет).
4. Ничего сверх этого: `permissions` (`contents: read` / `write` только в `release`), Actions по SHA, `environment: release`,
   `pull_request_target` — без изменений; `scripts/ci_lint.py` должен пройти на ветке до отправки заказчику.
5. Не входит (v1.0): отдельный job подписи (T-121, решение 28.09).

**Оценка.** 2–2,5 дня (перенос, `check_bundle.py`, `smoke.sh` с GUI-смоуком, `elf-audit`/`sbom.py`, скрипты выпуска,
`validate`, ветка `wip/r2-ci`).

**Тест.** CI зелёный на `wip/r2-ci` до слияния; T-168, T-169, T-170, T-171 (GUI-смоук); `tools/validate release --version 0.2.0 --dir <скачанное>`
на rc1 показывает оба артефакта «OK»; на T3 — `ci.yml` на коммите тега не менялся с последнего зелёного main.

## 10. Вход для ИБ-прохода T1 (сам T1 — security-analyst, один проход ~01.10)

Поверхности и что уже заложено (для STRIDE-lite):
1. **Исполняемые файлы в `$HOME`:** `app/<KEY>/` (~157 ELF + интерпретатор), симлинки `current`/`previous`; граница B2 (тот
   же uid) — как у `~/.config/autostart`; каталог 0700; `app/` — не симлинк, `current` — только наш симлинк с целью внутри
   `app/`; чистка по маске без следования симлинкам; `running-key`.
2. **Самоустановка:** источник — FUSE-монтирование (`/tmp/.mount_XXXXXX`, `mkdtemp`) или `/tmp/appimage_extracted_<md5>`
   (**предсказуемое имя в общем `/tmp`** — подсадка каталога/симлинка другим локальным пользователем до запуска; runtime
   упадёт или использует чужое; смягчение — `TMPDIR` в `$HOME` в документации, `AppRun` проверяет владельца `HERE`);
   `flock`, проверка места, атомарный `rename`, смоук копии до переключения `current`.
3. **Записи рабочего стола:** `~/.local/share/applications/astra-voice.desktop` перекрывает системную (пользователь и так
   может это сделать руками); `Exec` экранируется; чужие записи не исполняются и не переписываются (У27, T-28 расширяется).
4. **Окружение дочерних процессов:** установленный `AppRun` снимает `APPIMAGE APPDIR ARGV0 OWD PYTHONHOME PYTHONPATH
   QT_PLUGIN_PATH QML2_IMPORT_PATH QML_IMPORT_PATH QT_QPA_PLATFORMTHEME LD_PRELOAD`; `LD_LIBRARY_PATH` не ставится (RUNPATH);
   новый `core/childenv.clean_env()` для `pactl`, `xdg-open`, запуска нового файла обновления — убирает `ASTRA_VOICE_*`,
   `QT_QUICK_*`/`QT_XCB_GL_INTEGRATION` (они и в `.deb` сейчас утекают детям — попутное исправление); `gpgv` уже с `env={}`.
5. **OpenSSL 1.1.1k** — §7: принятый риск 0.2, план v1.0; CA хостовые; `certifi` только как запасной.
6. **Политика:** совещательный `appimage=deny`; `policy.conf` читается тем же кодом; проверка владельца/прав — M7-ядро
   (У16/У37) для обоих треков; невалидная политика — сеть закрыта, запуск разрешён.
7. **Сосуществование:** общий lock (`QLockFile` в `$XDG_RUNTIME_DIR`, 0700), общие каталоги данных 0700; понижение версии
   пользователем — допускается, анти-откат каталога независим.
8. **Действие «Доступна версия X» (2b):** загрузка в `~/.cache/astra-voice/updates/` (0700), `verify_detached` +
   `verify_sha256sums` до `chmod` и до предложения запуска; повтор старого подписанного набора (`latest.json` старее) — отказ
   по SemVer; T-116 (часы); лимиты размера при чтении (У19).
9. **Будущее самообновление (v1.0):** тот же `Verifier`, распаковка без FUSE, смоук, `current`/`previous`, `updates=admin|offline`;
   `.upd_info` не встраивается — сторонние обновлялки не обойдут нашу проверку.
10. **Цепочка поставки бандла:** новые корни доверия — PyPI (колёса по sha256), GitHub Releases `AppImage/appimagetool`,
    `AppImage/type2-runtime` (подпись `.sig` — проверять и пинить ключ), `niess/python-appimage` (только sha256); 157–158
    неподписанных ELF в `$HOME` — ЗПС не поддерживается (документировано); SBOM отдельный.
11. **jsonschema (§11):** в AppImage схема каталога проверяется всегда (`require_schema=True`); подпись каталога проверялась и
    без неё — `catalog.py:451–460` (`verify_detached` **до** схемы), sha256 схемы закреплён в подписанном каталоге
    (`:462–469`), поля разбираются строгим кодом. Потеря была только во втором рубеже; на путь подписи не влияет.
12. **Журнал:** пути домашнего каталога в сообщениях установки — заменять на `~` (У69).

## 11. Дополнение по `jsonschema` (находка GUI-проверки 28.09)

WARNING «Проверка каталога по схеме пропущена: модуль jsonschema недоступен» — потому что `schema.py` — фасад с
`ImportError → SchemaUnavailable`, а в `.deb` пакет лишь в Recommends. В бандле состав наш, поэтому:
1. `packaging/appimage.lock` дополняется по хэшам: `jsonschema==4.10.3` (ровно как `python3-jsonschema 4.10.3-1+b5` в ALSE 1.8,
   `apt-cache show`, только чтение), `attrs==22.2.0` (как `python3-attr`), `pyrsistent==0.19.3` в варианте
   `py3-none-any` (у 0.18.1, как в ALSE, нет колеса под cp311; 0.19.3 удовлетворяет `jsonschema 4.10.3` и не добавляет ELF —
   проверить при закреплении, иначе брать cp311-manylinux и поднять `expect-elf`). Код `schema.py` использует `RefResolver` и
   `Draft202012Validator` — в 4.10.3 они есть; версию не поднимать выше 4.x (`<< 5`, как в `control`).
2. `models/catalog.py:load_builtin(..., require_schema=...)`: `require_schema = paths.install_kind() is InstallKind.APPIMAGE`
   (в `.deb` остаётся мягко, Recommends).
3. Гейт `check_bundle.py`: (а) список `python3-*` из `Recommends` в `packaging/debian/control` → таблица «пакет → модуль»
   (`python3-jsonschema → jsonschema`; шрифты/polkit/pipewire — исключения с пометкой «хостовые») → каждый модуль импортируется
   из бандла; (б) `load_builtin(Verifier("catalog", keyring), state_path=tmp, require_schema=True)` проходит; (в) `--version`
   и (б) в изоляции — 0 записей уровня WARNING в захваченном журнале. Тест T-169.
4. Для T1 — п. 10.11: подпись не затронута.

## 12. Изменения в коде (сводно) и порядок сборки

### Компоненты

| Компонент | Ответственность | Зависит от | Тест |
|---|---|---|---|
| `core/paths.py`: `InstallKind`, `install_kind()`, `bundle_root()`, `appimage_app_dir()`, `appimage_current_apprun()`; `resource_root()` с веткой «бандл» до `/usr/share` | единый источник «где я и какой трек» | env `ASTRA_VOICE_APPIMAGE_DIR`, маркер `.astra-voice-build` | T-163 |
| `platform/userinstall.py` | `install_from_dir()`, `switch_current()`, `cleanup()`, `register()`, `unregister()`, `status()`; flock, место, смоук, `running-key` | `core/paths`, `platform/autostart`, stdlib | T-164, T-166 |
| `bootstrap.py`: команда `selfinstall` | вызов `userinstall` без Qt, коды выхода и русские сообщения | `userinstall` | T-170 |
| `packaging/appimage/AppRun` | режимы А/Б/В/Г, быстрый путь, флаги без установки, policy-grep, `exec … selfinstall` → `exec app/<KEY>/AppRun --register`, очистка окружения, `SSL_CERT_FILE`, уборка `appimage_extracted_*` | сборка | T-170 |
| `platform/autostart.py`: `executable()`, `entry_bytes()`, `retarget()`, `AutostartState.target` | запись по треку, перенацеливание только своей записи | `core/paths` | T-165 |
| `app.py`: `--register`/`--uninstall`, `running-key`, `appimage=deny`, `AppInfo.installKind/systemVersion/autostartTarget`, уведомление о первой установке | точки входа и «О программе» | `userinstall`, `policy` | T-167, xvfb |
| `qml/sections/About.qml` | «Способ установки», две кнопки, подсказки | `AppInfo` | xvfb-скриншот (design-reviewer) |
| `core/childenv.py` | `clean_env()` для детей | — | T-172 |
| `core/policy.py` (+M7) | ключ `appimage` | — | T-167 |
| `models/catalog.py` | `require_schema` по треку | `core/paths` | T-169 |
| `updates/checker.py` (M7-ядро, поток 2) | выбор `artifacts[track]`, действие 2b с запуском файла | `core/paths`, `security/verify` | T-174 |
| `scripts/release_latest_json.py` v2, `scripts/release_build.sh`, `scripts/release_assets.sh`, `scripts/release.sh` | выпуск двух артефактов без правок workflow | `ENABLED` | T-168, `validate release` |
| `tools/validate release`, `tools/elf-audit --max-glibc`, `scripts/sbom.py --appdir` | гейты и проверка выпуска | — | T-168, T-169 |
| `packaging/appimage/{build.sh, smoke.sh, check_bundle.py, …}`, `packaging/appimage.lock` | сборка, гейты, смоук | инструменты по lock | CI |
| Документы: `INSTALL-ADMIN.md` (новый раздел + §5 ключ + §8 таблица), README/страница выпуска, `NOTICE` (компоненты бандла, M9-а) | tech-writer | всё выше | ревью |

### Порядок сборки (по зависимостям; параллельно потоку 2 «сеть и обновления»)

1. **Фундамент без GUI (29.09–01.10, ≈ 2,5–3):** `core/paths.py` (трек, ветка «бандл»), `platform/userinstall.py`,
   `bootstrap selfinstall`, тонкий `AppRun`, `autostart.py` (`executable`/`retarget`/`target`), `core/childenv.py`,
   `catalog.py: require_schema`; юнит-тесты T-163…T-166, T-172. Ничего из этого не требует правки workflow.
2. **Перенос сборки и CI (30.09–02.10, ≈ 2–2,5):** `packaging/appimage/*`, `appimage.lock` (+jsonschema), `check_bundle.py`,
   `smoke.sh` (с GUI-смоуком), `elf-audit --max-glibc`, `sbom.py --appdir`, уборка `TMPDIR`; `scripts/release_*`,
   `release_latest_json.py` v2, `tools/validate release`, `release.sh`; ветка `wip/r2-ci` с `ci.yml` (§9.4) — к ревью 02.10,
   готова 03–04.10, заказчик вносит 05–06.10.
3. **⛔ T1 (01.10)** по §10 + коду шагов 1–2; правки по T1 — 02.10 (скрипты и код, не workflow).
4. **GUI и политика (05–07.10, ≈ 1,5–2):** `--register`/`--uninstall` в `app.py`, «О программе», кнопки, подсказка
   автозапуска, уведомление, `appimage=deny`; xvfb-тесты; T-167.
5. **Связка с M7-ядром (07.10, ≈ 0,5 сверх 2b потока 2):** `artifacts[track]`, запуск нового файла; T-174.
6. **Документы и проверки (07–09.10, ≈ 0,5 + 1,5–2):** tech-writer (INSTALL-ADMIN/README/NOTICE), GUI-прогон KDE агентом на
   изолированной шине (08.10), правки; Fly — коллега 12.10.
7. **Заморозка 09.10 → rc1** (`.deb` + AppImage из CI по `ENABLED`), чеклист 12.10, T3, тег 15.10.

**Сумма 0.2:** ≈ 10 дней потока (9–11,5; T1 0,5–1 внутри); OpenSSL: A — 0, B — +0,5, C — v1.0 +1–1,5.

## 13. Риски и альтернативы (где легко ошибиться)

| # | Риск | Смягчение / альтернатива |
|---|---|---|
| Р1 | Меню Fly не читает `~/.local/share/applications` (не проверено, ресёрч §6) | чеклист 12.10; запасной ярлык на `~/Desktop` (+0,25) |
| Р2 | Fly-FM не запускает `.AppImage` двойным щелчком | там же; текст «через терминал» на странице выпуска |
| Р3 | Под `nochmodx` не работает и extract-and-run (бит исполнения); распаковка не переносит режимы | ВМ нет — «не проверено» в заметках; честный текст «ставьте .deb» |
| Р4 | Гонка «новый файл запущен, старый экземпляр ещё держит lock» при действии 2b | `current` переключается до lock; следующий запуск — новая версия; ссылка на выпуск всегда есть |
| Р5 | Чистка удалит работающую версию при двух установках за сессию | `running-key`; `previous` |
| Р6 | Python-запуск из FUSE для `selfinstall` добавляет ~0,6 с к первому запуску | одноразово; при `--version` не выполняется |
| Р7 | T1 требует уйти с OpenSSL 1.1.1k в 0.2 | B (если есть 2_34) +0,5; C +1–1,5 за счёт «если останется время» |
| Р8 | `pyrsistent` без `py3-none-any` колеса | cp311-manylinux, `expect-elf` +1 |
| Р9 | Воспроизводимость squashfs не сходится | не гейт 0.2; v1.0 |
| Р10 | Правка workflow позже 06.10 | пакет §9.4 готов к 03–04.10; при задержке rc1 без AppImage, `ENABLED` не спасёт — дата с заказчиком уже согласована («лучше раньше») |
| Р11 | Пользователь без `.deb` нажал «Убрать из меню и автозапуска» и потерял способ запуска | текст кнопки и пояснение «программа останется в домашней папке: `…/app/current/AppRun`»; вторая кнопка — отдельная |

## 14. Вопросы заказчику (простым языком)

- **В1.** Держать в домашней папке две копии программы — текущую и предыдущую (≈ 0,55 ГБ) — чтобы можно было вернуться
  к прошлой версии, или только одну (≈ 0,27 ГБ)? Рекомендую две; если не ответить — делаем две.
- **В2.** В версии 0.2 AppImage будет использовать встроенную библиотеку шифрования старого поколения (1.1.1k, с
  исправлениями от AlmaLinux 8; проверка сертификатов и подписи не страдают), замена на современную — в v1.0 (+1–1,5 дня).
  Так ок, или заменить уже в 0.2 за счёт пунктов «если останется время»? Рекомендую в v1.0.

Остальное (правило сосуществования, ключ `appimage=deny`, формат `latest.json`, отсутствие отдельного установщика) — решения
архитектора, менять не требуется.
