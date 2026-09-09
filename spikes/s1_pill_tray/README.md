# S1 — спайк «пилюля + трей» (M0)

> Проверяет допущения архитектуры Р4/Р5/Р7 и §8 (Fly) синтез-плана: окно-оверлей не крадёт
> фокус, стоит над всем, встаёт по рабочей области; трей находит иконку по имени и строит меню
> спеки §9.2; тема детектится из файлов, а не из палитры Qt.
>
> **Этап 1 (сделано, 2026-09-09): код + прогон в изолированном X.**
> **Этап 2 (ждёт «ок» заказчика): живой прогон в KDE. Этап 3: живой прогон во Fly (перелогин).**

## Файлы

| Файл | Что это | Куда переедет |
|---|---|---|
| `pill.py` | Пилюля-оверлей: 4 состояния, 9 столбиков уровня, резиновая ширина, позиция по рабочей области, EWMH руками | `src/astra_voice/ui/pill.py` |
| `qml/Pill.qml` | Тот же экран на QtQuick — **не прогнан**, нет `python3-pyqt5.qtquick` | `qml/Pill.qml` |
| `x11ewmh.py` | EWMH через `ctypes` + `libX11` (без `python3-xlib`) | `src/astra_voice/platform/x11.py` |
| `tray.py` | `QSystemTrayIcon` по имени из hicolor + меню спеки §9.2 + 6 состояний | `src/astra_voice/ui/{tray,tray_icons}.py` |
| `theme_probe.py` | Тема из `kdeglobals` / `~/.fly/paletterc`, детект сессии по атомам root | `src/astra_voice/core/theme.py`, `platform/session.py` |
| `out/` | Артефакты прогона: PNG состояний, логи, замер CPU | — |

Все числа берутся из `design/tokens.json` (`component.pill.*`, `color.fixed.*`) на старте процесса —
хардкода размеров и цветов в коде спайка нет (см. `pill.py: load_tokens()`).

## Правила безопасности прогона

1. **Дисплей заказчика не трогаем.** `pill.py` и `tray.py` сами отказываются стартовать при
   `DISPLAY` пустом или `:0` (обход — `AV_ALLOW_DISPLAY=1`, только для живого этапа).
2. **Сессионная шина D-Bus — общая с реальной сессией, независимо от `$DISPLAY`.**
   `QSystemTrayIcon` регистрируется по SNI на шине, поэтому запуск `tray.py` на пользовательской
   шине показывает иконку на **настоящей панели Plasma**. `tray.py` это блокирует (код возврата 4);
   для прогона обязателен `dbus-run-session --`.
   *Это грабли, пойманные в этом спайке: до появления защиты иконка дважды на несколько секунд
   появлялась на реальной панели заказчика.*

## Как поднять изолированный X (без Xvfb)

`xvfb` в системе **не установлен**, ставить нельзя. Изолированный X даёт `kwin_wayland` в
безголовом режиме — бонусом это **настоящий KWin**, то есть EWMH-состояния реально обрабатываются:

```sh
cat > /tmp/av_s1_child.sh <<'EOF'
#!/bin/sh
echo "$DISPLAY" > /tmp/av_s1_display
sleep 3600
EOF
chmod +x /tmp/av_s1_child.sh
XDG_RUNTIME_DIR=/run/user/$(id -u) setsid kwin_wayland \
  --virtual --width 1920 --height 1080 --xwayland \
  --no-lockscreen --no-global-shortcuts --socket=av-s1 /tmp/av_s1_child.sh &
sleep 5; export DISPLAY=$(cat /tmp/av_s1_display)      # выдал :1
```

Прогон:

```sh
cd <PROJECT_ROOT>/spikes/s1_pill_tray
setsid xterm -title s1-test-window -geometry 60x12+200+200 &   # окно, которое держит фокус
python3 -u pill.py --state listening --seconds 30 &
xdotool getactivewindow
xprop -id "$(xdotool search --name astra-voice-pill | head -1)" \
      _NET_WM_STATE _NET_WM_WINDOW_TYPE _NET_WM_USER_TIME
dbus-run-session -- python3 -u tray.py --seconds 3 --cycle-ms 400
python3 theme_probe.py --x
```

Снимок: `xwd -root` под Xwayland **не работает** (`BadMatch` на `X_GetImage` — root не
перенаправлен композитором), снимается окно:
`xwd -id <WID> -silent | convert xwd:- out/pill_listening.png`.

## Как запустить живой прогон в KDE (этап 2 — только после «ок» заказчика)

В **сессии заказчика** (реальный `DISPLAY`, обычно `:0`), из терминала этой сессии:

```sh
cd ~/Документы/astra-voice/spikes/s1_pill_tray

# 1) тема и сессия — ничего не показывает на экране
python3 theme_probe.py --x

# 2) пилюля: 4 состояния по кругу, 12 с; фокус должен остаться в том окне, где был
kate &                                   # или любое окно, куда будем «диктовать»
AV_ALLOW_DISPLAY=1 python3 -u pill.py --cycle --seconds 12
#    во время показа, из другого терминала:
xdotool getactivewindow                                        # не должен меняться
WID=$(xdotool search --name astra-voice-pill | head -1)
xprop -id $WID _NET_WM_STATE _NET_WM_WINDOW_TYPE _NET_WM_USER_TIME
xprop -root _NET_WORKAREA                                      # Р7: сверить с availableGeometry
#    Alt+Tab руками: пилюли в списке быть не должно
#    панель KDE: пилюля не должна лежать поверх панели (48 px от края рабочей области)

# 3) трей: иконка на настоящей панели, 6 состояний, меню
AV_ALLOW_DISPLAY=1 AV_ALLOW_SESSION_BUS=1 python3 -u tray.py \
      --install-icons --force-hicolor --cycle-ms 1500 --seconds 20
#    смотрим: знак виден и перекрашен под панель; третья точка меняет цвет;
#    правый клик — меню спеки §9.2; левый клик — (в спайке действия нет)

# 4) CPU пилюли при 30 кадр/с
AV_ALLOW_DISPLAY=1 python3 -u pill.py --state listening --seconds 40 &
PID=$(pgrep -f "^python3 -u pill"); top -b -n 3 -d 2 -p $PID
```

## Как запустить живой прогон во Fly (этап 3 — после перелогина заказчика)

Те же команды, плюс Fly-ветка атомов и проверки из `arch/plan-claude.md` §13.2:

```sh
# сессия: что реально в окружении и на root-окне (A-04)
echo "XDG=$XDG_CURRENT_DESKTOP DS=$DESKTOP_SESSION"; python3 theme_probe.py --x
xprop -root | grep -E '_FLY_WM_PID|_FLY_WORKAREA|_NET_SUPPORTING_WM_CHECK'

# пилюля с атомами fly-wm (без них показ > 100 мс из-за анимации map/fade)
AV_ALLOW_DISPLAY=1 python3 -u pill.py --state listening --seconds 20 --fly
xprop -id $(xdotool search --name astra-voice-pill | head -1) \
      _NET_WM_STATE _NET_WM_WINDOW_TYPE _FLY_WM_WINDOW_MAP_ANIMATION _FLY_WM_FADE_SHOW
#   проверить: fly-wm НЕ центрирует окно (CenterWindowPos), пилюля над панелью Fly

# композитор вкл/выкл (fly-admin-theme), повторить показ — прозрачность и скругление
xprop -root _NET_WM_CM_S0

# трей на панели Fly (SNI-хост реализует сам fly-wm)
AV_ALLOW_DISPLAY=1 AV_ALLOW_SESSION_BUS=1 python3 -u tray.py \
      --install-icons --force-hicolor --cycle-ms 1500 --seconds 20
#   ВАЖНО: во Fly перекраски по currentColor нет — нужны иконки явных цветов (TrayIconProvider)
```

## Чеклист S1 (из `docs/plans.md` → M0)

| # | Критерий | Xvfb/KWin-headless | KDE (живой) | Fly (живой) |
|---|---|---|---|---|
| 1 | `xdotool getactivewindow` не меняется при показе пилюли | ✅ `0x40000c` до и после | ☐ | ☐ |
| 2 | `xprop` содержит `ABOVE`, `SKIP_TASKBAR`, `SKIP_PAGER` | ✅ + `STAYS_ON_TOP` от Qt | ☐ | ☐ |
| 3 | Тип окна `_NET_WM_WINDOW_TYPE_NOTIFICATION` | ✅ | ☐ | ☐ |
| 4 | `_NET_WM_USER_TIME = 0` | ✅ | ☐ | ☐ |
| 5 | Пилюли нет в Alt+Tab | ⚠️ косвенно (SKIP_TASKBAR стоит) — нужен живой прогон | ☐ | ☐ |
| 6 | Показ ≤ 100 мс | ✅ 4,8–21,3 мс до первого кадра | ☐ | ☐ |
| 7 | Над панелью не лежит; Р7 `availableGeometry` ↔ struts | ⛔ `_NET_WORKAREA` не публикуется — только живой прогон | ☐ | ☐ |
| 8 | Ширина в диапазоне 172…320, ellipsis нет | ✅ 172 / 183 / 188 по состояниям | ☐ | ☐ |
| 9 | 48 px от нижнего края рабочей области | ✅ низ пилюли 1032 при рабочей области до 1080 | ☐ | ☐ |
| 10 | Трей: иконка по имени из hicolor, 6 состояний различимы | ⚠️ имена резолвятся **только** с `--force-hicolor` (см. находку 2) | ☐ | ☐ |
| 11 | Трей: меню состава спеки §9.2, правила неактивности | ✅ собрано и распечатано | ☐ | ☐ |
| 12 | Трей: перекраска знака под панель (`currentColor`) | ⛔ проверяется только в живой Plasma | ☐ | ☐ |
| 13 | CPU пилюли при 30 кадр/с ≤ 3 % | ✅ 1,0–2,0 % одного ядра, VmRSS 76 МБ | ☐ | ☐ |
| 14 | Fly: `_FLY_WM_WINDOW_MAP_ANIMATION=0`/`_FLY_WM_FADE_SHOW=0`, `CenterWindowPos`, compton | — (код готов, флаг `--fly`) | — | ☐ |
| 15 | Детект сессии A-04 (`XDG_CURRENT_DESKTOP`, `_FLY_WM_PID`) | ✅ по атомам определил `KDE` (`_NET_WM_NAME=KWin`) | ☐ | ☐ |
| 16 | Уведомление с кнопкой (Plasma / `fly-notifications`) | не входит в этот этап | ☐ | ☐ |
| 17 | Запасной путь: override-redirect (`BypassWindowManagerHint`), пиксмап-иконка, XEmbed | не понадобился, не писан | ☐ | ☐ |

## Что уже проверено в изолированном X — команды и вывод дословно

Полный лог: `out/xvfb_session.log`. Ключевое:

```
$ dpkg -l xvfb | tail -1
un  xvfb           <нет>        <нет>        (описание недоступно)

$ xdotool getactivewindow                      →  4194316 (0x40000c)     # xterm
$ xdotool getactivewindow (пилюля показана)    →  4194316 (0x40000c)
ВЕРДИКТ: активное окно НЕ изменилось — фокус не украден

$ xprop -id 0x600006 _NET_WM_STATE _NET_WM_WINDOW_TYPE _NET_WM_USER_TIME WM_CLASS
_NET_WM_STATE(ATOM) = _NET_WM_STATE_ABOVE, _NET_WM_STATE_STAYS_ON_TOP, _NET_WM_STATE_SKIP_TASKBAR, _NET_WM_STATE_SKIP_PAGER
_NET_WM_WINDOW_TYPE(ATOM) = _NET_WM_WINDOW_TYPE_NOTIFICATION
_NET_WM_USER_TIME(CARDINAL) = 0
WM_CLASS(STRING) = "pill.py", "astra-voice"

$ xprop -root _NET_CLIENT_LIST_STACKING          # последний — самый верхний
_NET_CLIENT_LIST_STACKING(WINDOW): window id # 0x40000c, 0x600006
_NET_ACTIVE_WINDOW(WINDOW): window id # 0x40000c

$ xdotool getwindowgeometry 0x600006
  Position: 844,972 (screen: 0)
  Geometry: 231x84                              # 183 пилюля + 2×24 поле под тень

[pill] первый кадр: 7.2 мс (spec §8.4 — показ ≤ 100 мс)
[pill] availableGeometry: (0, 0, 1920, 1080)
[pill] _NET_WORKAREA: None
[pill] Р7: _NET_WORKAREA не опубликован WM — сверку struts переносим на живой прогон
[pill] видимый низ пилюли: 1032        [pill] низ рабочей области: 1080   # ровно 48 px

$ python3 theme_probe.py --x
KDE: тёмная (ColorScheme='AstraDark', BackgroundNormal='#262626', luma=38.0, по имя схемы),
Fly: светлая (ColorScheme='AstraLight', BackgroundColor='#ffffff', luma=255.0, по имя схемы)
ВНИМАНИЕ: kdeglobals и paletterc расходятся — источник выбирается по SessionKind
SessionKind по атомам: KDE

$ dbus-run-session -- python3 -u tray.py --seconds 3
[tray] isSystemTrayAvailable() = False          # ветка «трея нет» — как и ждали без панели
[tray] idle   icon=astra-voice-tray-idle   найдена=True   разрешилось в ПОДМЕНА → 'astra'
[tray] меню (spec §9.2): Готов(off) | Отмена(off) | Модель: GigaAM v3 RNN-T(off) |
       Скопировать последний текст(off) | Настройки… Ctrl+, | Проверить обновления(off) |
       О программе | Выход Ctrl+Q

# CPU пилюли, состояние listening, 30 кадр/с
 PID  %CPU  RES     COMMAND
 …    2,0   76148   python3
утилизация одного ядра за 10 с: ticks=15 HZ=100 → 1.00 %
```

Артефакты в `out/`: `pill_listening.png`, `pill_processing.png`, `pill_done.png`,
`pill_error.png`, `states_montage.png` (все четыре состояния), `tray_icons_22.png`
(6 иконок на светлой и тёмной подложке), `pill_cpu_top.txt`, `xvfb_session.log`.

## Находки этапа 1 (для журнала решений)

1. **Xvfb в системе нет** — прогон сделан на `kwin_wayland --virtual --xwayland`. Плюс: это
   настоящий KWin, EWMH обрабатывается по-честному. Минус: **не X11-сессия заказчика**, поэтому
   Alt+Tab, панель, struts и перекраска трея всё равно требуют живого прогона.
   Для CI (`pytest -m xvfb` в плане M1) `xvfb` придётся внести в build-зависимости.
2. **`QIcon.fromTheme` подменяет имя иконки.** При системной теме `astra-proxima`
   `astra-voice-tray-idle` не находится и Qt срезает имя по дефисам до `astra` — берётся
   звезда Astra Linux, а `icon.name()` становится `'astra'`. Через SNI Plasma получит
   `IconName=astra` и нарисует чужой знак. `QIcon.setThemeName("hicolor")` чинит (`--force-hicolor`),
   но меняет тему иконок всему приложению. **Нужно решение до M4:** своя тема
   (`~/.local/share/icons/astra-voice/index.theme` с `Inherits=<системная>`) либо иконка файлом
   (теряется перекраска Plasma). Цепочка тем на машине: `astra-proxima → fly-astra-flat →
   fly-astra → hicolor`.
3. **Трей ходит по сессионной шине, а не по `$DISPLAY`** — изолировать прогон обязан
   `dbus-run-session`. Защита встроена в `tray.py`.
4. **На Астре `XDG_RUNTIME_DIR` несёт мандатную метку** (`/run/user/1000z0_0_0x0_0x0`) —
   любые проверки «это пользовательская шина/каталог» по шаблону `/run/user/<uid>` ломаются.
5. **`ctypes` вместо `python3-xlib`.** Без `argtypes` ctypes передаёт `Display*` как 32-битный
   int и процесс падает по SIGSEGV — все функции подписаны явно (`x11ewmh.py`).
   Зависимость `python3-xlib` для S1 не нужна; для S4 (`XGrabKey`) — решать отдельно.
6. **Иконки трея не имеют варианта `nokey`.** Спека §9.1 требует шестое состояние
   `hotkey-not-grabbed`; в наборе `design/brand/icons/hicolor/*/status/` его нет —
   временно берётся `astra-voice-tray-error`. Задача UX: `astra-voice-tray-nokey.svg`.
7. **`_NET_WORKAREA` под `kwin_wayland` не публикуется** — проверку Р7 (`availableGeometry` ↔
   struts при панели > 48 px и боковой панели) закрыть можно только живьём.
8. **QtQuick недоступен** (`python3-pyqt5.qtquick` не установлен, ставить нельзя):
   рабочий бэкенд — `QWidget` + `QPainter`; `qml/Pill.qml` написан и ждёт M1.
   `qmllint` тоже нерабочий (обёртка есть, бинаря `/usr/lib/qt5/bin/qmllint` нет —
   пакет `qtdeclarative5-dev-tools` не установлен), так что QML **не проверен даже синтаксически**.
9. **Ширина пилюли** в состоянии «Слушаю» получилась **183 px** (макет мерил 187,6 px в DOM) —
   в допуске 172…320; расхождение от метрик Qt против метрик браузера. `processing` и `done`
   упираются в минимум 172 и центрируют содержимое.

## Открытые вопросы к человеку

1. Ставить ли `xvfb` (и `qtdeclarative5-dev-tools` для `qmllint`) — это `apt` и пароль заказчика.
   Без них CI-гейт «скриншоты состояний под Xvfb» из M1/M4 не собрать.
2. Как чиним подмену имени иконки (находка 2): своя тема иконок или иконка файлом?
3. Кто рисует `astra-voice-tray-nokey.svg` (находка 6) и когда.
