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

(продолжение — §5 варианты render loop / software, X-события, §6 живой шаг, §7 план фикса)
