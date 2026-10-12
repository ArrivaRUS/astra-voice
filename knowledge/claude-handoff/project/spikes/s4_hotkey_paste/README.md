# S4 — спайк «хоткей + вставка» (M0)

> Проверяет допущения плана #1 §3 (`platform/x11.py`, `platform/hotkey.py`, `platform/paste.py`,
> `platform/shortcuts_conflict.py`), синтез-плана §7 (И2) и требования модели угроз
> `docs/threat-model.md` (У11, У12, У21, У40 → тесты T-14, T-15, T-24).
>
> **Этап 1 (сделано, 2026-09-09): код + прогон в изолированном X.**
> **Этап 2 (ждёт «ок» заказчика): живой прогон в KDE. Этап 3: живой прогон во Fly (перелогин).**

## Файлы

| Файл | Что это | Куда переедет |
|---|---|---|
| `hotkey.py` | `XGrabKey` на root, маски Lock/Num/Scroll, автомат PTT/toggle, фильтр автоповтора, временный grab `Escape`, `BadAccess`, события через `QSocketNotifier` (без потоков) | `src/astra_voice/platform/{x11,hotkey}.py` |
| `paste.py` | Сохранение `QMimeData` → `text/plain` + `x-kde-passwordManagerHint` → XTest → восстановление; нормализация текста | `src/astra_voice/platform/paste.py` |
| `shortcuts_conflict.py` | Парсер `~/.config/kglobalshortcutsrc` (только чтение) | `src/astra_voice/platform/shortcuts_conflict.py` |
| `recv_qt.py` | Детерминированная мишень для вставки: `QPlainTextEdit`, сбрасывает содержимое в файл | `tests/platform/` (xvfb-мишень) |
| `set_clip.py` | Владелец буфера для негативных кейсов (эмуляция KeePassXC с `hint=secret`) | `tests/platform/` (фикстура) |
| `out/` | Артефакты прогона: JSON-отчёты, логи, `latency.tsv` | — |

## Зависимость: `python3-xlib` на машине НЕ УСТАНОВЛЕН

```
$ apt-cache policy python3-xlib
python3-xlib:
  Установлен: (отсутствует)
  Кандидат:   0.33-2+b2
     0.33-2+b2 900  https://download.astralinux.ru/.../repository-extended 1.8_x86-64/main amd64
```

Пакет есть в `repository-**extended**` (не в `main`!), но не установлен, а ставить в систему в
рамках спайка нельзя. Для прогона тот же 0.33 положен **в scratch-каталог, без изменения системы**:

```sh
~/.cache/astra-voice-spikes/venv/bin/pip download python-xlib==0.33 --no-deps \
    -d ~/.cache/astra-voice-spikes/s4-wheels
~/.cache/astra-voice-spikes/venv/bin/pip install --no-deps --no-index \
    --find-links ~/.cache/astra-voice-spikes/s4-wheels \
    --target ~/.cache/astra-voice-spikes/s4-site python-xlib==0.33
export PYTHONPATH=~/.cache/astra-voice-spikes/s4-site
```

`python-xlib` тянет `six` — он в системе есть (1.16.0), поэтому `--no-deps` достаточно.
**Для `.deb`:** `Depends: python3-xlib` придётся брать из `repository-extended`; это надо
подтвердить с заказчиком (в его сборке `extended` подключён — приоритет 900).

## Правила безопасности прогона

1. `hotkey.py` и `paste.py` отказываются стартовать при `DISPLAY` пустом или `:0`
   (обход — `AV_ALLOW_DISPLAY=1`, только для живого этапа). Причина — `XGrabKey` на
   реальном root перехватил бы комбинацию у заказчика, а `paste.py` затёр бы его буфер.
2. Всё, что ходит по шине, запускается в `dbus-run-session`: `kate` и `konsole` — приложения
   с single-instance через D-Bus, на пользовательской шине они подняли бы окно в **настоящей**
   сессии заказчика на `:0`.
3. `shortcuts_conflict.py` файл заказчика только **читает**.

## Изолированный X (как в `spikes/s1_pill_tray/README.md`)

```sh
cat > /tmp/av_s4_child.sh <<'EOF'
#!/bin/sh
echo "$DISPLAY" > /tmp/av_s4_display
echo "$DBUS_SESSION_BUS_ADDRESS" > /tmp/av_s4_dbus
exec sleep 7200
EOF
chmod +x /tmp/av_s4_child.sh
XDG_RUNTIME_DIR=/run/user/$(id -u) setsid dbus-run-session -- kwin_wayland \
  --virtual --width 1920 --height 1080 --xwayland \
  --no-lockscreen --no-global-shortcuts --socket=av-s4 /tmp/av_s4_child.sh &
sleep 5
export DISPLAY=$(cat /tmp/av_s4_display) DBUS_SESSION_BUS_ADDRESS=$(cat /tmp/av_s4_dbus)
export PYTHONPATH=~/.cache/astra-voice-spikes/s4-site
setxkbmap -layout ru,us -option grp:alt_shift_toggle     # активная группа — русская
```

Фоновые процессы (мишени вставки) надо поднимать через `nohup setsid … < /dev/null &` —
иначе они умирают вместе с вызвавшей их оболочкой.

## 1. Хоткей

```sh
( sleep 2.5
  xdotool key ctrl+space; sleep 0.8                                        # тап → toggle
  xdotool key ctrl+space; sleep 0.8                                        # тап → стоп
  xdotool keydown ctrl+space; sleep 1.5; xdotool keyup ctrl+space; sleep 0.8   # PTT + автоповтор
  xdotool keydown ctrl+space; sleep 0.6; xdotool key Escape; sleep 0.3; xdotool keyup ctrl+space
) &
python3 -u hotkey.py --grab ctrl+space --probe alt+F2 --seconds 12 --json out/hotkey_isolated.json
```

Дословный вывод (`out/hotkey_isolated.log`):

```
grab ctrl+space → keycode=65 масок=4 ok=True
probe alt+F2: занят другим клиентом → наш grab ok=False ([['0x8', 'BadAccess'], ['0xa', 'BadAccess'], ['0x18', 'BadAccess'], ['0x1a', 'BadAccess']]); после снятия ok=True
  [recording] press
  Escape: grab ok=True
  [recording] tap→toggle (0.014 с)
  [processing] toggle-off
  [idle] done
  Escape: ungrab
  [recording] press
  Escape: grab ok=True
  [processing] release (1.531 с)
  [idle] done
  Escape: ungrab
  [recording] press
  Escape: grab ok=True
  [idle] escape-cancel
  Escape: ungrab
{"key_presses": 5, "autorepeat_dropped": 22}
```

Автомат без X (`--selftest-fsm`): `selftest-fsm: PASS (ptt, tap→toggle, escape-cancel)`.

### Находка: масок 4, а не 8

План говорит «`XGrabKey` × 8 масок Lock/Num/Scroll». На реальной раскладке машины
**Scroll Lock не привязан ни к одному модификатору**, поэтому вариантов ровно 4
(`0`, `Lock`, `Mod2`, `Lock|Mod2`):

```
$ xmodmap -pm            # out/modmap_isolated.log
shift       Shift_L (0x32),  Shift_R (0x3e)
lock        Caps_Lock (0x42)
control     Control_L (0x25),  Control_R (0x69)
mod1        Alt_L (0x40),  Alt_R (0x6c),  Meta_L (0xcd)
mod2        Num_Lock (0x4d)
mod3
mod4        Super_L (0x85),  Super_R (0x86),  Super_L (0xce),  Hyper_L (0xcf)
mod5        ISO_Level3_Shift (0x5c),  Mode_switch (0xcb)
```

Правило для `platform/x11.py`: число масок **считать из `get_modifier_mapping()`**, а не
хардкодить 8. Если у пользователя Scroll Lock замаплен (свой `xmodmap`, другой набор правил) —
вариантов станет 8, и код это учтёт сам. Хардкод «ровно 8» дал бы `BadValue`/лишние grab-и.

## 2. Конфликты KDE

```sh
python3 shortcuts_conflict.py --combo 'Ctrl+Space' --combo 'Alt+F2' --combo 'Alt+Space' --combo 'Meta+V' \
    --json out/conflicts_kde.json
```

```
/home/astra/.config/kglobalshortcutsrc: разделов=19, действий=244, из них с комбинацией=99
  Ctrl+Space   свободна (в kglobalshortcutsrc совпадений нет)
  Alt+F2       ЗАНЯТА → [org.kde.krunner.desktop] Открыть строку поиска и запуска KRunner · действие '_launch' · «Открыть строку поиска и запуска KRunner»
  Alt+Space    ЗАНЯТА → [org.kde.krunner.desktop] Открыть строку поиска и запуска KRunner · действие '_launch' · «Открыть строку поиска и запуска KRunner»
  Meta+V       ЗАНЯТА → [plasmashell] Plasma · действие 'show-on-mouse-pos' · «Показать записи на позиции указателя мыши»
  Fly `/home/astra/.fly/keyshortcutrc`: есть
```

**Отчёт по конфликту `Ctrl+Space` (то, о чём просил бриф):** в `kglobalshortcutsrc` заказчика
`Ctrl+Space` **никем не занята** — дефолт из PRD берётся без конфликта с KDE.

Два уточнения, которые надо проверить живьём:

* `~/.config/kxkbrc` → `Options=grp:ctrl_shift_toggle` — **переключение раскладки висит на
  `Ctrl+Shift`**. Это не конфликт с `Ctrl+Space`, но задевает вставку в терминалы
  (`Ctrl+Shift+V`): надо убедиться, что XTest-комбинация не переключает раскладку.
* `Ctrl+Space` — классическая комбинация переключателей ввода (ibus/fcitx); на этой машине их
  нет, у корпоративного пользователя могут быть.

### Грабли парсера: табуляция в файле экранирована

В `kglobalshortcutsrc` несколько комбинаций одного действия разделены **двухсимвольной**
последовательностью `\t`, а не настоящим TAB (проверено `cat -A`):

```
_launch=Alt+Space\tAlt+F2\tSearch,Alt+Space\tAlt+F2\tSearch,Открыть строку поиска и запуска KRunner
```

Первая версия парсера резала по `"\t"` и **не находила `Alt+F2`**. В `platform/shortcuts_conflict.py`
разбор обязан быть по `re.split(r"\\t|\t", …)`.

## 3. Вставка

Мишень — `recv_qt.py` (сбрасывает своё содержимое в файл), плюс настоящие `kate`, `konsole`, `xterm`.
Раскладка во всех прогонах — **русская** (`layout: ru,us`, активная группа — ru).

| # | Кейс | Команда | Факт |
|---|---|---|---|
| A | вставка + восстановление буфера | `paste.py --text 'Проверка связи, ёж.' --hold-ms 3000` | мишень `[Проверка связи, ёж.]`; буфер во время hold — `[ИСХОДНЫЙ БУФЕР 42]`; в целях восстановленного буфера **нет** `x-kde-passwordManagerHint` |
| B | исходник с `hint=secret` (T-14, У40) | `set_clip.py --text 'пароль-ГЕЛИОТРОП-7' --secret` → `paste.py` | `restore-clear`, буфер после — пусто; пароль **не переиздан** |
| C | окно сменилось (T-15, У12) | `paste.py --delay-before-ms 800` + `xdotool windowactivate` второго окна | `window-changed … action='clipboard-only'`; XTest не послан; **оба** окна пустые |
| D | управляющие символы (T-15) | `paste.py --text $'ls\n rm -rf ~\e[31m\017хвост'` | в мишени `'ls  rm -rf ~[31mхвост'`, управляющих символов **0**, переводов строки **0** |
| E | kate, `Ctrl+V` | `kate -n /tmp/av_s4_kate.txt` → вставка → `Ctrl+S` | файл: `Проверка связи, ёж.` |
| F | konsole, `Ctrl+Shift+V` | `konsole -e sh -c 'stty raw -echo; exec cat -u > файл'` | файл: `Проверка связи, ёж.`, переводов строки **0** (команда не выполнена) |
| G | xterm, `Ctrl+Shift+V` | то же с `xterm` | **FAIL**: в терминал пришёл один байт `026` (= литеральный `Ctrl+V`), вставки нет |
| H | xterm, `Shift+Insert` + PRIMARY | `xclip -selection primary` + `xdotool key shift+Insert` | вставка есть |

Дословный вывод кейса A (`out/paste_a.log`):

```
  active-window      {'wid': 4194310, 'wm_class': ('recv_qt.py', 'recv_qt.py'), 'terminal': False, 't_ms': 0.6}
  save-clipboard     {'formats': 2, 'secret': False, 't_ms': 6.9}
  put-text           {'chars': 19, 'hint': 'secret', 't_ms': 7.1}
  xtest              {'combo': 'ctrl+v', 'ms': 0.39, 't_ms': 58.2}
  restore            {'formats': 2, 't_ms': 159.0}
{"mode": "ctrl+v", "restore": "restored", "total_ms": 152.0, "owns_clipboard": true}
буфер во время hold: [ИСХОДНЫЙ БУФЕР 42]
мишень: [Проверка связи, ёж.]
```

### Находка: `xterm` нельзя держать в списке «терминалы → Ctrl+Shift+V»

`plans.md` M4 перечисляет терминалы `konsole, fly-term, xterm, yakuake, alacritty → Ctrl+Shift+V`.
Штатный xterm этой машины **не знает `Ctrl+Shift+V`**: он отдаёт приложению литеральный `0x16`.
Рабочий путь для xterm — `Shift+Insert`, но он вставляет **PRIMARY**, а не CLIPBOARD, то есть
потребовал бы класть распознанный текст ещё и в первичное выделение (лишняя поверхность утечки,
P1/У11). Решение — за Юркой: либо `xterm` → отдельный метод `shift_insert` с записью в PRIMARY,
либо `xterm` из списка убрать и оставить пользователю ручной выбор метода (US-3.4).

### Замер задержки (10 прогонов, `out/latency.tsv`)

```
процесс paste.py целиком (включая старт QApplication): медиана 420 мс, p95 446 мс
цепочка put→restore:                                  медиана 153 мс, p95 154 мс
момент XTest от put:                                  медиана  56 мс, p95  57 мс
```

10 из 10 вставок долетели (мишень накопила 10 копий фразы). В продакшне старт `QApplication`
не входит в цикл — GUI уже запущен, поэтому значимая цифра — **153 мс**, из которых 150 мс это
наши собственные паузы (50 мс до XTest + 100 мс до восстановления). То есть цепочка вставки
съедает **≈30 % бюджета Ц2 (p95 ≤ 0,5 с)** ещё до инференса — риск, см. ниже.

## Чеклист S4 из `docs/plans.md` (M0)

| Пункт | Вердикт | Основание |
|---|---|---|
| `XGrabKey` `Ctrl+Space` × маски Lock/Num/Scroll | **PASS с уточнением** | grab ok на всех вариантах; масок **4**, а не 8 — Scroll Lock не замаплен (`xmodmap -pm`) |
| PTT с фильтром автоповтора (KeyRelease+KeyPress одним `time`) | **PASS** | 22 пары автоповтора отброшены за 1,5 с удержания; `release (1.531 с)` — один раз |
| Временный grab `Escape` | **PASS** | `grab` при входе в `recording`, `ungrab` при возврате в `idle`; отмена сработала |
| `BadAccess` на занятой комбинации | **PASS** | второй клиент занял `Alt+F2` → `BadAccess` на всех 4 масках, после снятия grab прошёл |
| События без потоков | **PASS** | `QSocketNotifier` на `Display.fileno()`, `threads_used: false` в JSON |
| Имя действия из `kglobalshortcutsrc` | **PASS** | `Alt+F2` → «Открыть строку поиска и запуска KRunner» |
| XTest `Ctrl+V` в Kate при русской раскладке | **PASS** | текст в сохранённом файле |
| Буфер восстановлен ≤ 200 мс | **PASS** | 153–159 мс, подтверждено внешним клиентом (`xclip`) |
| `x-kde-passwordManagerHint=secret` | **PASS частично** | hint кладётся и виден в TARGETS; **проверку истории Klipper делает только живой прогон** (в изолированной сессии Klipper нет) |
| Терминалы → `Ctrl+Shift+V` | **PASS для konsole, FAIL для xterm** | см. кейсы F и G |
| Enter не синтезируется, C0/C1 вырезаны | **PASS** | кейс D: 0 управляющих, 0 переводов строки; в konsole команда не выполнилась |
| Окно сменилось → только буфер | **PASS** | кейс C |
| Fly-ветка (`Alt+space`, `keyshortcutrc`, `~/.fly/clipboard`, `fly-term`) | **не проверено** | нужен перелогин заказчика; `~/.fly/keyshortcutrc` существует, но **пуст (0 строк)** |

## Что требует живого прогона

**KDE (этап 2, `DISPLAY=:0`, `AV_ALLOW_DISPLAY=1`):**

1. `Ctrl+Space` под живым `kglobalaccel`: grab действительно проходит (файл говорит «свободна»,
   но занять комбинацию мог модуль без записи в файл).
2. **Klipper** (T-14/T-13): после диктовки `qdbus org.kde.klipper /klipper getClipboardHistoryMenu`
   не содержит ни маркерной фразы, ни исходного секрета. Изолированная сессия это проверить не может.
3. `Ctrl+Shift`-переключатель раскладки (`grp:ctrl_shift_toggle`) — не срабатывает ли он на
   XTest-комбинации `Ctrl+Shift+V` (группа после вставки та же).
4. Вставка в LibreOffice (`text/plain` без диалога «Специальная вставка») и в браузер.
5. Число масок на живой раскладке (`xmodmap -pm` в сессии заказчика) — 4 или 8.
6. `XGrabKeyboard` для поля захвата комбинации (У21/T-24) — в этом спайке **не реализован**,
   уходит в M4.

**Fly (этап 3, после перелогина):**

1. `BadAccess` на `Alt+space` (по `keyshortcutrc`/`FLYWM_*` — файл сейчас пуст, значит подписи
   надо брать из системных дефолтов, а не из пользовательского файла);
2. клип-менеджер fly-wm: тип из `ClipboardManagerTypesBlacklist` → `~/.fly/clipboard` без фразы;
3. вставка в `fly-term` (какая комбинация), `fly-fm`, LibreOffice;
4. `_FLY_WM_*`-специфика фокуса при XTest.

## Риски

* **R3/R4 сняты частично**: `Ctrl+Space` в файле свободна, автоповтор фильтруется. Остаётся
  риск «занята модулем без записи в файл» — только живой прогон.
* **Бюджет Ц2**: 150 мс фиксированных пауз (50 + 100) — треть бюджета «отпустил → текст».
  Если p95 инференса окажется у верхней границы 300 мс, суммарно останется ≈50 мс запаса.
  Кандидат на оптимизацию — уменьшить паузу восстановления (100 мс) или восстанавливать
  буфер по событию `SelectionRequest`, а не по таймеру.
* **R5 (Klipper игнорирует hint)** — не проверялся, нужен живой KDE.
* **xterm** — см. находку выше; влияет на US-3.4 (выбор метода вставки).
* **`python3-xlib` из `repository-extended`** — если у корпоративного заказчика подключён только
  `repository-main`, `Depends` не разрешится. Альтернатива — ctypes+`libX11` (как `x11ewmh.py` в S1),
  но тогда `XGrabKey`/XTest придётся писать руками.
* Восстановленный буфер живёт, пока жив процесс-владелец: без менеджера буфера (Klipper/fly-wm)
  после выхода GUI CLIPBOARD пустеет. В изолированном X это видно дословно
  (`Error: target STRING not available`), в KDE Klipper это скрывает.

## Уборка

```sh
pkill -f 'kwin_wayland .* --socket=av-s4'
rm -f /tmp/av_s4_display /tmp/av_s4_dbus /tmp/av_s4_child.sh \
      /tmp/av_s4_konsole.txt /tmp/av_s4_xterm.txt /tmp/av_s4_kate.txt
```

Система не изменена: пакеты не ставились, `~/.config/kglobalshortcutsrc` только читался,
`python-xlib` лежит в `~/.cache/astra-voice-spikes/s4-site` (scratch, вне репо).

---

# Живой прогон KDE (этап 2) — 2026-09-09

Допуск на живой прогон получен от заказчика через Юрку (`decisions/log.md`, «Допуски на живые
прогоны M0»). Сессия: KDE Plasma 5.27, X11, `DISPLAY=:0`, раскладки `us,ru` (активная — **ru**,
группа 1), `Options=grp:ctrl_shift_toggle` + `grp:alt_shift_toggle`, Klipper запущен.
Правила прогона: содержимое буфера заказчика в логи не выводилось — только TARGETS-типы,
размеры и **хэши** (первые 16 символов sha256); окна Kate/Konsole/LibreOffice/браузер поднимал
и закрывал сам; трей на панели не запускался (отдельного допуска не было).

Снимок «до» — `out/live_kde_before.log`; артефакты живого этапа — `out/live_kde_*.json|log`.

## 1. Хоткей: `Ctrl+Space` в живой сессии ЗАНЯТА

```
$ AV_ALLOW_DISPLAY=1 python3 hotkey.py --grab ctrl+space --probe alt+F2 --seconds 12
grab ctrl+space → keycode=65 масок=4 ok=False
  BadAccess: комбинация занята другим клиентом
probe alt+F2: занят другим клиентом → наш grab ok=False; после снятия ok=False
{"key_presses": 0, "autorepeat_dropped": 0}
```

При этом **KDE считает комбинацию свободной** — и файл, и живой демон:

```
$ qdbus --literal org.kde.kglobalaccel /kglobalaccel getGlobalShortcutsByKey 67108896   # Ctrl+Space
[Argument: a(ssssssaiai) {}]                       # владельца нет
$ qdbus --literal … isGlobalShortcutAvailable 67108896 ""
true
$ qdbus --literal … getGlobalShortcutsByKey 150994993   # Alt+F2 — контроль
… "org.kde.krunner.desktop", "Открыть строку поиска и запуска KRunner" …
```

Скан комбинаций живьём (`out/live_kde_combo_scan.log`):

| Комбинация | `XGrabKey` | `kglobalaccel` знает владельца |
|---|---|---|
| `Ctrl+Space` | **ok=False** | нет |
| `Ctrl+Alt+Space` | **ok=False** | нет |
| `Meta+Space` | **ok=False** | нет |
| `Alt+Space` | ok=False | **да** — KRunner |
| `Super+V` | ok=False | (не проверялось) |
| **`Ctrl+Shift+Space`** | **ok=True** | нет |
| **`Ctrl+Alt+D`** | **ok=True** | нет |

**Владелец — `/usr/bin/handy` (pid 4011)**, старая сборка Handy на машине заказчика: её хоткей по
умолчанию — `Ctrl+Space` (сведения от Юрки). Handy не останавливался и не трогался. Нажатие
`Ctrl+Space` не открывает ни одного нового окна и не меняет активное
(`out/live_kde_ctrlspace_owner.log`) — то есть по одному только поведению X-клиента владельца
не опознать.

**Находка (в `platform/hotkey.py`, US-1.5b):** детектор конфликтов по `kglobalshortcutsrc`
**не видит захваты обычных X-клиентов** (Handy, ibus/fcitx, Electron-приложения с
`globalShortcut`). Единственный честный признак — `BadAccess` при собственном `XGrabKey`.
Поэтому: при `BadAccess` показывать «комбинация занята другой программой» + подсказку
«если установлен Handy — измените или отключите его горячую клавишу» и сразу предлагать выбор
другой комбинации; онбординг «Astra Voice поверх Handy» — отдельный пункт для PRD/M5.

### Хоткей на свободной `Ctrl+Shift+Space` (`out/live_kde_hotkey_cs_space.log`)

```
grab ctrl+shift+space → keycode=65 масок=4 ok=True
  [recording] press · Escape: grab ok=True · [recording] tap→toggle (0.023 с)
  [processing] toggle-off · [idle] done · Escape: ungrab
  [idle] escape-cancel
{"key_presses": 4, "autorepeat_dropped": 17}
группа раскладки: ДО 1 · во время 1 · ПОСЛЕ 1
повторный grab после выхода: ok=True         # grab снят, процессов не осталось
```

**Побочный эффект `grp:ctrl_shift_toggle` не наступает:** пока комбинация захвачена нашим
`XGrabKey`, события `Ctrl+Shift` до переключателя раскладки не доходят — группа остаётся 1.

⚠ Артефакт синтетического ввода: `xdotool keydown/keyup` даёт нашему автомату «tap» (0,023 с),
а не удержание, поэтому ветка PTT живьём подтверждена только логикой автомата (в изоляции
она проверена честно: `release (1.531 с)`).

## 2. Вставка в Kate (`out/live_kde_paste_kate.log`)

```
  active-window      {'wid': 113246220, 'wm_class': ('kate', 'kate'), 'terminal': False}
  save-clipboard     {'formats': 7, 'secret': False, 't_ms': 8.4}
  put-text           {'chars': 35, 'hint': 'secret', 't_ms': 9.0}
  xtest              {'combo': 'ctrl+v', 'ms': 1.19, 't_ms': 61.2}
  restore            {'formats': 7, 't_ms': 162.5}
{"mode": "ctrl+v", "restore": "restored", "total_ms": 154.0, "owns_clipboard": true}
файл Kate: Проверка связи S4 живой прогон, ёж.
```

**PASS**: текст вставлен при русской раскладке, восстановление 162,5 мс (порог ≤ 200 мс).

## 3. Вставка в Konsole (`out/live_kde_paste_konsole.log`)

```
  active-window      {'wm_class': ('konsole', 'konsole'), 'terminal': True}
  xtest              {'combo': 'ctrl+shift+v', 'ms': 1.07, 't_ms': 57.9}
  restore            {'formats': 9, 't_ms': 158.7}
принято терминалом: 61 байт, переводов строки 0 (команда не выполнилась)
группа раскладки: ДО 1 → ПОСЛЕ 1
```

**PASS**, и главное — **XTest `Ctrl+Shift+V` НЕ переключает раскладку** у заказчика с
`grp:ctrl_shift_toggle` (открытый вопрос §2.4 отчёта закрыт).

## 4. Klipper: наша фраза в историю не попала

```
$ qdbus org.kde.klipper /klipper getClipboardHistoryMenu
записей: 20 · наша фраза найдена: нет        # содержимое чужих записей не выводилось
```

**PASS (T-13/T-14, У40):** `x-kde-passwordManagerHint=secret` в живом Klipper работает —
после двух вставок история осталась прежней (20 записей, до прогона тоже 20).

## 5. Буфер обмена: содержимое сохранилось, но набор форматов пересобрался

| Момент | TARGETS | sha256 `text/plain` |
|---|---|---|
| до прогона | `text/uri-list · text/x-moz-url · text/plain · application/x-kde4-urilist · …` | `a74bece24f6c44a0` |
| после прогона | `text/uri-list · text/x-moz-url · text/plain · application/x-kio-metadata · application/x-kde-cutselection · application/x-kde-onlyReplaceEmpty · …` | `d56eeba4e5b8c67d` |

Расхождение разобрано: текущее содержимое **совпадает с записью №0 в истории Klipper**
(`getClipboardHistoryItem 0` без завершающего `\n` даёт тот же `d56eeba4e5b8c67d`), то есть
последняя пользовательская копия жива и доступна. Механика: `paste.py` восстанавливает
`QMimeData` и держит владение, но после выхода процесса CLIPBOARD подхватывает **Klipper** и
отдаёт уже свою нормализованную версию той же записи — с другим набором MIME-типов
(`application/x-kde4-urilist` исчез, появились `x-kio-metadata`/`x-kde-cutselection`).

**Находка для M4 (S4-R8):** «буфер восстановлен» в живой KDE ≠ побайтовая копия. Для копии файла
из Dolphin приложение, ожидающее `application/x-kde4-urilist`, после нашей вставки получит только
`text/uri-list` + `text/plain`. Проверить в M4, воспроизводится ли это без Klipper и нельзя ли
удержать владение до следующей копии пользователя.

## 6. Окно без Handy: `Ctrl+Space` проверен честно

Заказчик разрешил остановить Handy на ~2 минуты. Handy оказался **systemd-юнитом**
`app-Handy@autostart.service` (`Restart=no`, `ExecStart=/usr/bin/handy`, cwd `/home/astra`),
поэтому вместо `kill -TERM` использован штатный `systemctl --user stop|start` того же юнита —
иначе юнит остался бы «активным» с мёртвым процессом.

**Handy остановлен 15:43:23 → возвращён 15:43:38 (15 с), новый pid 66792,
`Ctrl+Space` снова занят: да** (`grab … ok=False` через 3 с после старта — это и подтверждает
владельца). После возврата Handy показал своё окно; оно свёрнуто, фокус возвращён исходному
окну заказчика (`83886084`), приложение работает.

Дословно (`out/live_kde_handy_window.log`, `out/live_kde_hotkey_ctrlspace.json`):

```
grab ctrl+space → keycode=65 масок=4 ok=True          # без Handy комбинация свободна
probe alt+F2: занят другим клиентом → ok=False; после снятия ok=False   # KRunner держит постоянно
  [recording] press · Escape: grab ok=True · [recording] tap→toggle (0.015 с)
  [processing] toggle-off · [idle] done
  [recording] press · Escape: grab ok=True · [processing] release (1.231 с)   # PTT честно
  [сценарий] KRunner: Alt+F2
  [recording] press · [recording] tap→toggle (0.015 с) · [idle] escape-cancel
{"key_presses": 7, "autorepeat_dropped": 16}
группа раскладки после: 1
```

| Критерий | Живой KDE |
|---|---|
| `XGrabKey` `Ctrl+Space` × 4 маски (Handy остановлен) | **PASS** — `ok=True` |
| PTT + фильтр автоповтора | **PASS** — `release (1.231 с)`, 16 автоповторов отброшено |
| Временный grab/ungrab `Escape`, отмена | **PASS** |
| `BadAccess` на `Alt+F2` под живым `kglobalaccel` | **PASS** — KRunner держит комбинацию и после снятия чужого grab |
| KRunner открыт → наш хоткей не ломается | **PASS** — события продолжали доходить до автомата |
| Grab снят после выхода | **PASS** — повторный grab `ok=True`, процессов не осталось |

⚠ `XGrabKeyboard` для поля захвата комбинации (У21/T-24) в спайке по-прежнему **не реализован** —
проверка того, что чужой глобальный grab не «съест» ввод в этом поле, уходит в M4.

## 7. LibreOffice Writer (`out/live_kde_paste_lo.log`)

Прогон во **временном профиле** (`-env:UserInstallation=file:///tmp/av_s4_lo_profile`) —
настройки и недавние документы заказчика не тронуты.

```
  active-window  {'wm_class': ('libreoffice', 'libreoffice-writer'), 'terminal': False}
  xtest          {'combo': 'ctrl+v', 'ms': 0.85, 't_ms': 58.1}
  restore        {'formats': 9, 't_ms': 159.2}
документ после Ctrl+S → конвертация в txt:
  строка-заглушкаПроверка связи S4 живой прогон, ёж.
```

**PASS**: вставка как обычный текст, диалог «Специальная вставка» не появлялся.

## 8. Браузер — Chromium (`out/live_kde_paste_chromium.log`)

Временный профиль `--user-data-dir=/tmp/av_s4_chrome`, локальная страница с `textarea`,
которая отражает длину и наличие маркера в `document.title` (заголовок читается `xdotool`).

```
заголовок ДО:     AVS4 пусто – Chromium
  xtest           {'combo': 'ctrl+v', 'ms': 0.81, 't_ms': 57.5}
заголовок ПОСЛЕ:  AVS4 len=35 marker=OK – Chromium
```

**PASS** (35 символов, маркер найден). Грабли прогона: при первом открытии окна фокус не в поле —
вставка ушла «в никуда» и заголовок не изменился; понадобился клик в `textarea`. Для M4 это ещё
один довод к правилу «вставляем в то окно, которое было активно в момент старта диктовки»
(фокус внутри окна мы не контролируем).

## 9. Состояние машины после прогона (`out/live_kde_after.log`)

```
остатки процессов: chromium 0 · soffice.bin 0 · kate 0 · konsole 0 · python3 0
активное окно:     83886084   (до прогона — то же)
раскладка:         us,ru · grp:ctrl_shift_toggle,grp:alt_shift_toggle · группа 1 (до — 1)
klipper:           записей 20 · наша фраза: нет   (до — 20 записей)
Handy:             active, pid 66792, видимых окон 0
буфер:             text/plain = d56eeba4… = запись Klipper №0 (см. §5)
```

Временные файлы удалены (`/tmp/av_s4_*`). Система не изменена: пакеты не ставились,
`kglobalshortcutsrc` только читался, профили LibreOffice и Chromium были временные.

## 10. Что живой этап НЕ закрыл

* `XGrabKeyboard` в поле захвата комбинации (У21/T-24) — в M4.
* Fly целиком (этап 3, после перелогина).
* Поведение восстановления буфера **без** Klipper (см. §5) — в M4.
* PTT-жест руками человека (в прогоне — синтетический `xdotool`).
