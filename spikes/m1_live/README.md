# M1 live — прогон DoD на реальной KDE-сессии

Живая проверка DoD/Validation вехи M1 (`docs/plans.md`) на машине заказчика:
Astra 1.8 SE, KDE 5.27, X11, `DISPLAY=:0`, схема сессии **AstraDark**.
Пакет `astra-voice 0.1.0~m1` установлен через apt. Допуск получен в чате 2026-09-09 16:40.

Дата прогона: 2026-09-09, 16:40–16:46. CI на коммите `e03d6ae` — зелёный (run 34358041504).

## Итог по пунктам

| # | Проверка | Итог |
|---|---|---|
| 1 | `astra-voice --version` | **PASS** — `astra-voice 0.1.0~m1`, код 0 |
| 2 | Окно, геометрия, свойства, журнал | **PASS частично** — геометрия и свойства верны; RSS и тема — FAIL |
| 3 | Второй запуск (single-instance) | **PASS частично** — код 0 за 0,15 с, `show` дошёл; окно НЕ развернулось |
| 4 | Живая тема | **FAIL** — окно не получает тему; определение темы при этом корректно |
| 5 | Закрытие и уборка | **FAIL** — процесс переживает закрытие окна; `settings.json` не создаётся |

Состояние машины до = после: схема `AstraDark` не менялась (шаг 4 сделан в песочнице),
процессов 0, `$XDG_RUNTIME_DIR/astra-voice` удалён, `~/.config/astra-voice` пуст, `/tmp/av-theme` убран.

## Найденные дефекты

1. **Тема сессии игнорируется** (зона M1-A, `src/astra_voice/app.py`). `Theme.qml:9` берёт
   `themeSource.dark`, а `app.py` кладёт в контекст QML только `appInfo` — `themeSource` не
   регистрируется. При схеме `AstraDark` окно светлое. Сам `KdeThemeSource` работает верно:
   `dark=True`, `window_bg=#262626`, `accent=#2FB3FA` (лог `live_kde_02f`, `live_kde_04b`).
   Нужен шаг «theme» из порядка старта: источник по `SessionKind` → `ThemeBridge` →
   `setContextProperty("themeSource", …)` → `source.start()`.
2. **Процесс переживает закрытие окна** (зона M1-A, `app.py`). После `_NET_CLOSE_WINDOW` окно
   исчезает, процесс жив; следующий запуск отдаёт ему `show` (в журнале есть), но окно не
   возвращается — приложение перестаёт открываться до `kill` (лог `live_kde_05c`).
   Причина: `quitOnLastWindowClosed` не срабатывает для окна QML под `QApplication`, а `_show()`
   умеет только `setVisible` на уже существующем корневом объекте.
3. **`show` не разворачивает свёрнутое окно** (зона M1-A, `app.py`, `_show()`). Окно остаётся
   `Iconic`, KWin превращает активацию в `_NET_WM_STATE_DEMANDS_ATTENTION` (лог `live_kde_03b`).
   Нужен `showNormal()`/сброс состояния перед `requestActivate()`.
4. **RSS 167 720 КБ против порога 122 880 КБ** (PRD A1) — превышение на 44 840 КБ (36 %) на пустой
   оболочке окна. Разложить память не удалось: `/proc/PID/smaps` пуст из-за нашего же
   `PR_SET_DUMPABLE=0` (У9) — для замеров в M2 понадобится обходной путь (`VmHWM` из
   `/proc/PID/status` читается).
5. **`settings.json` не создаётся** при первом запуске: `app.py` настройки только читает.
   DoD ожидает файл 0600 в `~/.config/astra-voice/`.
6. **`KdeThemeSource` игнорирует `XDG_CONFIG_HOME`** (зона M1-C, `core/theme.py:226`):
   `DEFAULT_PATH = Path.home()/".config"/"kdeglobals"` жёстко. Из-за этого тему нельзя проверить
   в песочнице, не трогая настройки пользователя.
7. **Команды DoD в `plans.md` врут**: `pgrep -c -f 'bootstrap.py app'` считает и собственную
   оболочку с этой строкой в командной строке (дало 3 при 0 процессов) — нужен якорь
   `'^/usr/bin/python3 -I /usr/lib/astra-voice/bootstrap\.py app'`;
   `xdotool search --classname astra-voice` не находит окно, потому что `WM_CLASS` =
   `"bootstrap.py", "astra-voice"` — искать надо `--class`.
8. **`WM_CLASS` instance = `bootstrap.py`** (зона M1-A): Qt берёт имя из `argv[0]`. Некрасиво в
   диспетчере задач; `_KDE_NET_WM_DESKTOP_FILE=astra-voice` при этом выставлен верно.

## Что где лежит

| Файл в `out/` | Содержимое |
|---|---|
| `live_kde_01_version.log` | шаг 1: версия, код возврата, статус пакета |
| `live_kde_02a_before.log` | снимок состояния до прогона |
| `live_kde_02_stderr.log` | stderr приложения за весь сеанс (пуст — QML без warnings) |
| `live_kde_02b_window.log` | геометрия и свойства окна (`xdotool`, `xwininfo`, `xprop`) |
| `live_kde_02c_rss_log.log` | RSS/VmHWM в покое, строки журнала |
| `live_kde_02d_memory.log` | попытка разложить память (пусто из-за `PR_SET_DUMPABLE=0`) |
| `live_kde_02_window.png` | скриншот только нашего окна (900×588) |
| `live_kde_02e_theme_diag.log` | `kdeglobals` заказчика: схема и цвета |
| `live_kde_02f_theme_probe.log` | что отдаёт установленный `KdeThemeSource` |
| `live_kde_03_second_instance.log` | шаг 3: второй запуск, код, время, журнал |
| `live_kde_03b_window_state.log` | состояние окна после `show` (осталось `Iconic`) |
| `live_kde_03c_show_unminimized.log` | `show` на несвёрнутом окне |
| `live_kde_04a_theme_probe_both.log` | песочница `XDG_CONFIG_HOME` (не подействовала) |
| `live_kde_04b_theme_explicit.log` | обе ветки темы по явному пути — работают |
| `live_kde_05_shutdown.log` | шаг 5: закрытие, уборка, `settings.json` |
| `live_kde_05b_close_proper.log` | старт поверх устаревших lock/ipc + `_NET_CLOSE_WINDOW` |
| `live_kde_05c_orphan.log` | последствие: процесс-призрак перехватывает `show` |
| `live_kde_06_after.log` | снимок состояния после + сверка «до = после» |
| `live_kde_summary.json` | цифры прогона одним файлом |

## Как повторить

```sh
astra-voice --version
setsid astra-voice >/tmp/av.log 2>&1 &
WID=$(xdotool search --class astra-voice | head -1)        # именно --class
xdotool getwindowgeometry $WID; xprop -id $WID _NET_FRAME_EXTENTS
ps -o rss= -p "$(pgrep -f '^/usr/bin/python3 -I /usr/lib/astra-voice/bootstrap\.py app')"
astra-voice; grep 'получена команда show' ~/.local/share/astra-voice/logs/astra-voice.log
```

Схему сессии не переключать: тему проверять подставным `kdeglobals` по явному пути
(`KdeThemeSource(Path(...))`), пока не починен пункт 6.

## Починка 2026-09-09 (после прогона): дефекты 1, 2, 3, 5, 8 закрыты

Проверка — **не переустановкой пакета**, а из репозитория в изолированном сеансе KWin
(`verify_isolated.sh`); дисплей `:0` и настройки заказчика не затрагиваются:

```sh
REPO=$PWD OUT=$PWD/spikes/m1_live/out dbus-run-session -- \
  kwin_wayland --virtual --width 1280 --height 900 --xwayland --socket=av-m1a \
  --exit-with-session=spikes/m1_live/verify_isolated.sh
```

`kdeglobals` копируется в песочницу (`XDG_CONFIG_HOME`), тема проверяется в обе стороны.
Результат: **PASS=17, FAIL=0** (`out/live_iso_verify.log`, `out/live_iso_verdict.log`).

| Дефект | Что сделано (`src/astra_voice/app.py`, если не сказано иное) | Факт из изолированного прогона |
|---|---|---|
| 1 тема | Шаг «theme» в порядке старта: `make_theme_source()` по `SessionKind` → `ThemeBridge` → контекст QML **и** глобальный объект JS (`Theme.qml` — `pragma Singleton`, контекстных свойств не видит), `source.start()` после загрузки | тёмная схема → яркость центра **31**, журнал `тема сессии: dark=True accent=#2FB3FA`; светлая → **249**, `dark=False` |
| 2 закрытие | `CLOSE_TO_TRAY = False  # M4: True` + фильтр события `QEvent.Close` (сигнал `closing` в PyQt5 недоступен: `QQuickCloseEvent*` не поддержан) + уборка `lock`/`ipc` в `finally` и по SIGTERM/SIGINT (`signal` + `QTimer`, чтобы Qt отпускал интерпретатор) | `окно закрыто — завершаю процесс`, процессов **0**, `lock`/`ipc` убраны; то же по SIGTERM |
| 3 разворачивание | `showNormal()` → `_NET_WM_USER_TIME` → `raise_()` → `requestActivate()`; метку времени X второй экземпляр берёт у сервера и шлёт в протоколе `show <ts>`; новый модуль `platform/x11.py` | `Iconic` → **Normal**, окно активно, `_NET_WM_USER_TIME` выставлен, код второго экземпляра 0 за ~0,18 с |
| 5 настройки | `_ensure_settings_file()` пишет дефолты при первом старте (на диск идут настройки пользователя, не результат наложения политики) | файл создан, права **600** |
| 8 WM_CLASS | `QApplication([APP_NAME])` — instance-часть берётся из `argv[0]` | `WM_CLASS = "astra-voice", "astra-voice"`, окно находится по `--classname` |

Протокол сокета остался единственной командой: `show` либо `show <ts>`; всё иное — отбой
(таблица разбора — `tests/unit/test_app_lock.py::test_parse_command_*`).
`stderr` приложения пуст в обоих запусках.

### Уточнение к дефекту 2

Исходный вывод «процесс переживает закрытие окна» был снят **неверным жестом**:
`xdotool windowclose` по man-странице *уничтожает* окно (`XDestroyWindow`) и не шлёт
`WM_DELETE_WINDOW`, поэтому приложение о закрытии не узнавало — отсюда же были
`BadWindow` в `stderr` на `:0`. Пользователь кнопкой «×» вызывает
`_NET_CLOSE_WINDOW` → `WM_DELETE_WINDOW`; для проверки это делает `close_window.py`.
Сам фикс всё равно нужен: `quitOnLastWindowClosed` считает только окна-виджеты,
и намерение (в M1 закрываем процесс, в M4 прячем в трей) теперь выражено явно.

### Дефекты 4, 6, 7

* 4 (RSS 167 720 КБ) — не трогал, отдельная задача после Debugger.
* 6 (`XDG_CONFIG_HOME` в `KdeThemeSource`) — починил M1-C, подтверждено этим прогоном.
* 7 — команды в `docs/plans.md` M1 Validation исправлены: якорный `pgrep`,
  `xdotool search --class`, проверка закрытия через `close_window.py`.

### Файлы починки

| Файл | Назначение |
|---|---|
| `verify_isolated.sh` | стенд в изолированном KWin, 17 проверок |
| `close_window.py` | закрытие окна как пользователем (`_NET_CLOSE_WINDOW`) |
| `out/live_iso_verify.log` | полный лог прогона + журнал приложения |
| `out/live_iso_window_dark.png`, `out/live_iso_window_light.png` | окно в обеих темах |
| `out/live_iso_verdict.log` | `PASS=17 FAIL=0` |

Каталог `out/iso-home/` (песочница с копией `kdeglobals`) создаётся стендом и удаляется
после прогона — в git его быть не должно.
