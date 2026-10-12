# Баг «моргает экран при наведении на сайдбар» — диагностика (Debugger)

**Дата:** 2026-09-09 · **Пакет заказчика:** `astra-voice 0.1.0~m1` · **Статус:** пишется частями (урок `.patches/001`).

## 1. Симптом

Заказчик (KDE Plasma 5.27 / X11, живой запуск установленного пакета): «странно моргает экран,
когда вожу мышкой по левой части меню» — сайдбар `qml/Sidebar.qml` + `qml/components/SidebarItem.qml`.
Что именно моргает — весь экран (панель ноутбука) или только окно — из формулировки не ясно;
это ключевой вопрос для живого шага (§6).

## 2. Факты об окружении заказчика (read-only, `DISPLAY=:0` не трогался)

| Что | Значение | Откуда |
|---|---|---|
| Машина | ThinkPad **T14 Gen 6**, GPU Intel **Arrow Lake-U** (8086:7d41), панель eDP-1 | `/sys/class/dmi/id`, `lspci`, `/sys/class/drm` |
| Ядро / драйвер | **6.12.60-1-generic**, модуль `i915` (в ядре есть и `xe`, не привязан) | `/proc/cmdline`, `lsmod` |
| X-сервер | Xorg 21.1.7 (astra.se70), DDX **modesetting + glamor** на «Mesa Intel(R) Graphics (ARL)», DRI3/Present | `/var/log/fly-dm/Xorg.0.log` |
| Mesa | 24.2.8-1.astra1 | `dpkg -l` |
| KWin | kwin-x11 5.27.8-1astra7, `~/.config/kwinrc` без секции `[Compositing]` → OpenGL-композитинг по умолчанию | `~/.config/kwinrc` |
| Qt | 5.15.8 (astra3), PyQt 5.15.9 | python |
| Сессия | `QT_QUICK_CONTROLS_STYLE=org.kde.desktop` (06-fly-misc-env; лаунчер перебивает на `Default`), `QT_SCREEN_SCALE_FACTORS=HDMI-1=1.25;eDP-1=1`, `QSG_*` не заданы; `QT_XCB_NO_XI2=1` экспортируется в `/etc/X11/Xsession.d/20x11-common_process-args` (условие — см. §5) | env, `/etc/X11/Xsession.d` |
| PSR панели | параметры `/sys/module/i915/parameters/enable_psr*` читаются только root — **не проверено** | `ls -l` |
| Установленный QML | отличается от репо: в репо добавлены `antialiasing: true` и убран `forceActiveFocus()` в `SidebarItem`; логика hover идентична | `diff -r /usr/share/astra-voice/qml qml` |

## 3. Что делает код при hover (чтение)

- `SidebarItem.qml`: `bg.color` ← `mouse.containsMouse`/`mouse.pressed`; `Behavior on color` →
  `ColorAnimation` 120 мс (`Theme.durationHover`). Никаких `Loader`/`Component`, размеры не меняются,
  `Theme` (singleton) от hover не зависит, сигнал `themeSource.changed` — только по watcher'у `kdeglobals`.
- `Main.qml`: `ApplicationWindow` непрозрачное (`color: Theme.bgApp`), без `opacity`/`layer`/`DropShadow`;
  `minimumWidth/Height` — константы токенов (WM_NORMAL_HINTS не меняются на hover).
- `SettingRow.qml`: тот же паттерн hover (только строки с тумблером). `Icon.qml` — `QtQuick.Shapes`,
  статичный.
- → По коду H1 (перепривязка всего окна), H2 (пересоздание/relayout), H5 (анимация фона окна) не находят
  опоры. Проверено измерением (§4).

## 4. Воспроизведение в изолированном X (kwin_wayland --virtual + Xwayland, DISPLAY=:1, свой D-Bus и XDG_RUNTIME_DIR)

Окружение изолированного прогона максимально близко к заказчику по GL: `QSG_INFO=1` показал
**threaded render loop, GL_RENDERER = Mesa Intel(R) Graphics (ARL), vsync 16.7 мс**, окно 900×588,
visual TrueColor depth 24 (не ARGB), `_NET_WM_BYPASS_COMPOSITOR`/`_KDE_NET_WM_BLOCK_COMPOSITING` не выставлены.

Метод: `xdotool mousemove` по пунктам сайдбара, счёт кадров по `QT_LOGGING_RULES=qt.scenegraph.time.renderloop=true`
(одна строка `Frame prepared` на кадр), CPU процесса по `/proc/<pid>/stat`. Фазы по 2–3 с.

| Фаза (2–3 с) | репо-QML, threaded | установленный QML | что это значит |
|---|---|---|---|
| P0 простой | **0 кадров** | 0 | в покое не рисует |
| P1 60 движений внутри одного пункта | **0 кадров** | 0 | движение мыши само по себе кадров не даёт (H4 «дребезг containsMouse» — нет) |
| вход в неактивный пункт / выход | 9 / 9 кадров | — | ровно анимация 120 мс |
| P2 проход по всем 6 пунктам | 70 кадров (~35 fps) | 69 | вспышки по 7–9 кадров на каждую границу |
| P3 простой после прохода | **0** | 0 | анимации завершаются, «вечного» цикла нет |
| P4 дёрганье через границу двух пунктов | **131–149 кадров (60–75 fps)** | 149 | пока мышь пересекает границы — полноэкранная (для окна) перерисовка каждый vsync |
| P6 проход по строкам контента | 18 | 18 | hover только у строк с тумблером |

CPU процесса при этом ≤ 11 тиков/2 с (~5 %). Пересчёт Qt-сцены ограничен одним `Rectangle` пункта,
но **каждый кадр Qt Quick на OpenGL перерисовывает и свапает всё окно 900×588 целиком** — это штатно
для scenegraph, и композитор получает damage на всё окно.

## 5. Варианты рендера, X-события, визуализация изменений (изоляция)

`qml/Main.qml` без Python-обвязки (`spikes/m1_live/flicker_probe.py qml/Main.qml`; `qmlscene`/`qml`
на машине нет — только обёртка qtchooser), те же фазы:

| Вариант | P1 внутри пункта | P2 проход | P4 дёрганье через границу | P3/P7 простой |
|---|---|---|---|---|
| threaded (по умолчанию) | 0 | 49 | 140 | 0–5 (хвост анимации) |
| `QSG_RENDER_LOOP=basic` | 0 | 41 | 124 | 0–6 |
| `QT_QUICK_BACKEND=software` | 0 | 41 | 124 | 0–6 |

- Кадров одинаково во всех трёх — hover-логика одна; разница только в **пути вывода**: GL-варианты свапают
  всё окно, software отдаёт в X только грязный прямоугольник пункта (XPutImage). Под software окно
  рисуется корректно (скриншот `Shapes`-иконок, скруглений, текста — без дефектов; шрифты те же).
- `xprop -spy` на окне во время прохода и дёрганья: **ни одного изменения свойств** (WM_NORMAL_HINTS,
  `_NET_WM_*` не трогаются) — окно не переконфигурируется, KWin не получает поводов перерисовывать
  декорацию/экран.
- `QSG_VISUALIZE=changes` (30 снимков `import -window` во время дёрганья): подсвечивается **только
  прямоугольник наводимого пункта**, остальная сцена затемнена и не меняется → H1 снята визуально.
- `QT_XCB_NO_XI2=1` из Xsession.d экспортируется только на `mips64` — к заказчику не относится.
- Установленный QML (`ASTRA_VOICE_RESOURCES=/usr/share/astra-voice`) даёт те же числа, что и репо.

**Вывод по коду приложения:** на hover приложение ведёт себя штатно для Qt Quick — нет лишних кадров,
нет дребезга, нет перепривязок, нет relayout. H1, H2, H4, H5 **опровергнуты измерением**. Остаётся H3:
то, как **полноэкранные для окна GL-свапы 60–75 раз в секунду** (пока мышь пересекает границы пунктов)
доходят до экрана заказчика через KWin X11 (GLX/glamor, Mesa 24.2, Arrow Lake) или до самой панели
(PSR/Panel Replay в i915 6.12 на ARL — известный источник «моргания экрана при активности на экране»).
В изолированном Xwayland этот путь не воспроизводится в принципе — нужен живой шаг.

## 6. Живой шаг (по «ок» заказчика, ≤ 2 мин, только смотреть — ничего не ставить)

Из терминала сессии заказчика, `cd ~/Документы/astra-voice`. Каждый пункт — 15–20 с: поводить мышью по
пунктам сайдбара, ответить «моргает: да/нет» и **что именно** (весь экран, включая панель Plasma и часы,
или только окно программы).

```sh
# 0) как есть — база
astra-voice
# 1) пробник без нашего кода: те же hover-анимации в чистом QML
python3 spikes/m1_live/flicker_probe.py
# 2) тот же GL, но рендер в GUI-потоке
QSG_RENDER_LOOP=basic astra-voice
# 3) без GL в клиенте: растр + частичный damage
QT_QUICK_BACKEND=software astra-voice
# 4) контроль платформы: поводить мышью по панели задач Plasma / сайдбару «Параметров системы» — моргает ли так же
# 5) (если заказчик готов ввести пароль) состояние PSR панели:
sudo cat /sys/kernel/debug/dri/1/i915_edp_psr_status | head -8
```

Чтение результата:

| 1) пробник | 2) basic | 3) software | Что именно | Диагноз |
|---|---|---|---|---|
| моргает | моргает | **не моргает** | окно | GL-свап клиента под KWin X11/glamor на ARL → лечится в лаунчере (§7-B) |
| моргает | **не моргает** | не моргает | окно | threaded render loop + KWin X11 → `QSG_RENDER_LOOP=basic` (§7-B) |
| моргает | моргает | моргает | **весь экран** (и панель Plasma, и в п.4) | панель/PSR i915 на T14 Gen 6 — не наш код (§7-A) |
| **не моргает** | — | — | только в astra-voice | остаётся что-то наше — прислать `QSG_INFO=1 astra-voice 2>&1 \| head -30` и видео, копать дальше (§7-C) |

## 7. План фикса (Developer) — по результату §6

- **A. Весь экран (PSR/панель):** в приложении править нечего. Рекомендация админу — `i915.enable_psr=0`
  (или `=1`, только PSR1) в `/etc/default/grub` + `update-grub`, перезагрузка; записать в
  `docs/INSTALL-ADMIN.md` раздел «Известные проблемы платформы». Опционально (решение дизайна, не баг):
  укоротить/убрать `Behavior on color` в `SidebarItem.qml:169-175` и `SettingRow.qml:37-43` —
  уменьшит частоту свапов с ~9 на переход до 1, но триггер не убирает.
- **B. Только окно, лечится env:** ≤ 10 строк, два места (как уже сделано для стиля контролов):
  1. `src/astra_voice/bootstrap.py:69-71` — рядом с `QT_QUICK_CONTROLS_STYLE`:
     `os.environ.setdefault("QT_QUICK_BACKEND", "software")` (или `"QSG_RENDER_LOOP", "basic"` — что
     подтвердилось в §6; `setdefault`, чтобы админ/отладчик мог переопределить).
  2. `scripts/astra-voice:11-12` — тот же export через `${VAR:-значение}`.
  Software-бэкенд для окна настроек безопасен: 900×588, ≤ 11 тиков CPU на 2 с дёрганья (§5), рендер
  без дефектов; для пилюли (M4) это тоже допустимо (ARGB-окна QBackingStore поддерживает).
  **Регресс-тесты (Tester):** unit — `bootstrap.main(["app"])` выставляет переменную и не перебивает
  уже заданную; xvfb — `tests/xvfb/test_qml_warnings.py` параметризовать `QT_QUICK_BACKEND=software`
  (Main.qml грузится без предупреждений и под software).
- **C. Только наше окно, пробник чист:** повторить §4 живьём с `QT_LOGGING_RULES=qt.qpa.*=true` и
  `QSG_INFO=1`, сравнить render loop/GL с изоляцией; эскалация на второе мнение (`gpt-6-astra`).

## 8. Побочные наблюдения (не по этому багу)

- В момент прогона `src/astra_voice/app.py` менялся параллельным агентом (мост темы, `_wire_close`);
  промежуточная версия падала на старте (`getattr(root, "closing")` → `TypeError: QQuickCloseEvent*`),
  текущая версия уже на фильтре событий. Проверить, что финальный `app.py` стартует, прежде чем
  собирать `.deb`.
- Помощники прогона: `spikes/m1_live/flicker_probe.qml`, `spikes/m1_live/flicker_probe.py`.
- Изолированный `kwin_wayland --virtual` с реальным GL (iris, ARL) — рабочий стенд для Qt Quick;
  `import -window` работает, `xev -id` на чужом окне — нет, `xprop -spy` — да.
  `XDG_RUNTIME_DIR` для kwin нужен короткий (лимит 108 байт на путь сокета) — симлинк `/tmp/av-dbg-xdg`.

## 9. Итог живого шага (2026-09-09 17:37–17:39, `DISPLAY=:0`, пользовательская шина, «ок» заказчика ~18:05 по чату)

**Во всех 4 вариантах одинаково → это hover-анимация `SidebarItem` (`Behavior on color`, 120 мс на вход и на выход),
воспринятая как «мерцание», а не рендер: GL/threaded loop/PSR не при чём.** Заказчик смотрел сам (мышью водил он),
наблюдения передал Юрке. Логи: `spikes/m1_live/out/flicker_live_{00_before_env,1..4,N_xprop,99_after}.log`,
`flicker_live_summary.txt`.

| № | Что запускалось | Env сверх сессии | Render loop / вывод (из лога) | Длит. | Закрытие | Аномалии в логах |
|---|---|---|---|---|---|---|
| 1 | `astra-voice` (установленный 0.1.0~m1) | `QSG_INFO=1 QT_LOGGING_RULES=qt.qpa.*;qt.qpa.input*=false;qt.scenegraph.general` | threaded, GLX gl-integration, Mesa Intel (ARL) 4.6 Compat, vsync 16.67 мс | 25 с | `close_window.py` → WM_DELETE, выход штатный | нет (1702 строк, 0 warning/error) |
| 2 | `python3 spikes/m1_live/flicker_probe.py` (чистое QML) | то же | threaded, GLX, ARL | 25 с | заказчик закрыл «×» до таймера (лог кончается WM_DELETE → UnmapNotify), процесс вышел сам | нет (813 строк) |
| 3 | `astra-voice` | то же + `QSG_RENDER_LOOP=basic` | basic render loop, GLX, ARL | 26 с | заказчик закрыл «×», выход штатный | нет (430 строк) |
| 4 | `astra-voice` | то же + `QT_QUICK_BACKEND=software` | software backend, XShm-буфер 900×588 depth 24 | 26 с | заказчик закрыл «×», выход штатный | нет (347 строк) |

Окно во всех вариантах: 900×588 @510,292, `_NET_WM_BYPASS_COMPOSITOR` не задан, `_KDE_NET_WM_*` только штатные
(ACTIVITIES, DESKTOP_FILE, FRAME_STRUT, USER_CREATION_TIME), `_NET_WM_SYNC_REQUEST` есть, `_NET_WM_STATE_FOCUSED`.
`close_window.py` для 2–4 упал с `BadWindow` — окно к тому моменту уже было закрыто заказчиком; это не дефект приложения.

Окружение из логов/`supportInformation` (только чтение): KWin 5.27.8, «Operation Mode: X11 only», Compositing OpenGL,
platform interface **GLX**, Mesa 24.2.8 «Intel(R) Graphics (ARL)», Xorg 1.21.1, ядро 6.12.60, `kwinrc [Compositing]`
без переопределений (Backend/GLCore/LatencyPolicy/AllowTearing пусты = умолчания); экран eDP-1 1920×1200, dpr 1.0,
XInput 2.2; `glxinfo -B`: direct rendering yes, 4.6 core/compat.

Состояние машины **до = после**: процессов `bootstrap.py app`/`flicker_probe.py` — 0, окон — 0, `$XDG_RUNTIME_DIR/astra-voice`
пуст (lock/ipc сняты), ничего не установлено и не изменено; уведомления `notify-send` — 5 (4 подсказки + «завершено»).

**Фикс (Developer, по решению дизайна):** `qml/components/SidebarItem.qml:169-175` — убрать `Behavior on color`
(или `duration` → 0–40 мс); симметрично `qml/components/SettingRow.qml:37-43`. Токен `Theme.durationHover=120`
остаётся для других контролов, пока дизайн не решит иначе. **Регресс (Tester):** xvfb-тест — hover пункта меняет
`bg.color` за один кадр (нет промежуточных значений) / QML-предупреждений нет. Разделы §6–§7 выше — история
диагностики, план B/C неактуален.
