---
name: target-environment-astra-1-8
description: Подтверждённые особенности рабочей среды разработки и целевой ОС — Astra Linux 1.8 SE + KDE Plasma 5
metadata: 
  node_type: memory
  type: project
  originSessionId: 5f445429-1a45-407a-a3c0-77d9ac4ca7c4
---

**Подтверждённая среда (проверено на текущей машине пользователя):**
- ОС: Astra Linux 1.8 SE x86_64
- Сессия: **X11** (`XDG_CURRENT_DESKTOP=KDE`)
- Plasma: 5.27.8, KF5 5.104
- Python: 3.11.2
- **Файловый менеджер: Fly-FM 1.7.173 (собственный ФМ Astra) — Dolphin НЕ установлен!** (проверено 2026-05-29: `xdg-mime query default inode/directory` → `fly-fm.desktop`; пакетов dolphin нет). Сессия при этом — KDE Plasma + KWin (kwin_x11, plasmashell 5.27.8). KIO-servicemenus из `~/.local/share/kio/servicemenus/` Fly-FM показывает через свой плагин `/usr/lib/x86_64-linux-gnu/fly-fm/libkdeactions.so` — поэтому ПКМ-пункты Astra Cowork работают. У Fly-FM есть C++-плагины (тот же каталог: libactions/libarchiver/libcrypt/libmetadata/libpreviewer…), но это контекст-меню и страницы свойств, НЕ виджеты тулбара; **публичного SDK/заголовков (fly-fm-dev) в репо нет**. Вывод: «нативный плагин в тулбар» (как у Dolphin) на Fly-FM невозможен поддерживаемым способом — для кнопки+прогресса «на панели ФМ» остаётся либо плавающий оверлей (PyQt+KWin/X11), либо индикация в нашей собственной панели. ВАЖНО: план/README/доки проекта ошибочно говорят «Dolphin» — фактическая цель интеграции это Fly-FM.
- Qt6 dev-библиотеки в системе есть (libqt6*), но **PyQt6/PySide6 в apt отсутствуют** — ставим через pip в venv.

**Доступные системные утилиты:** `spectacle`, `xsel`, `qdbus`, `kreadconfig5`, `inotify-tools`, `python3-pyinotify`.

**Ключевые D-Bus API на месте:**
- `org.kde.KGlobalAccel` — регистрация шорткатов
- `org.kde.KWin`: `queryWindowInfo()`, `getWindowInfo(QString uuid)`
- `org.kde.kwin.Screenshot`: `interactive()`, `screenshotArea(x,y,w,h)`, `screenshotForWindow(winid)`
- `org.freedesktop.Notifications`

**Emoji-шрифт (проверено 2026-07-13):** пакет `fonts-noto-color-emoji` на Astra УСТАНОВЛЕН (`/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf`), но НЕ ставит fontconfig-правило fallback → Qt/PyQt6 рисует эмодзи (🙂,👍,💭) как «квадратик»-tofu. Симптом: агент отвечает с эмодзи, в UI — перечёркнутый квадрат. Фикс — user-level конфиг `~/.config/fontconfig/conf.d/75-noto-color-emoji.conf` (append «Noto Color Emoji» в fallback для sans-serif/serif/monospace) + `fc-cache -f` + рестарт приложения (Qt кэширует fontconfig на старте). В репо: `packaging/fontconfig/75-noto-color-emoji.conf`, ставит `scripts/install_user_service.sh`. Проверено: после фикса PyQt6 рендерит эмодзи цветными.

**Why:** до начала кода важно подтвердить, что нужные системные интеграции существуют на целевой ОС, иначе план может развалиться на ровном месте.


Связано: [[project-astra-cowork]], [[tech-stack-astra-cowork]].

- 2026-09-01 (новый ThinkPad T14 Gen6): `xdotool` и `xclip` УСТАНОВЛЕНЫ (старая запись «отсутствуют» относилась к прежней машине). Звуковая карта: PipeWire-профиль `off`, реальных sink нет — работает только ALSA (`amixer -c 0`); `pactl` возвращает rc=0 без эффекта. Bluetooth soft-blocked, но `/dev/rfkill` доступен на запись (группа netdev). XDG_RUNTIME_DIR содержит PARSEC-уровень в пути — не хардкодить.
- 2026-09-02: `~/Desktop` — СИМЛИНК на `~/Desktops/Desktop1` (Fly держит 4 стола в `~/Desktops/DesktopN`; xdg-user-dir DESKTOP = ~/Desktop, тот же inode). Следствие: любой индекс/скан по домашним папкам — по realpath с дедупом корней, иначе двойники; «стол пустой/невидимый» — ложная тревога, папки автоматизаций и create_folder работают. Fly-FM реализует org.freedesktop.FileManager1 (ShowItems/ShowFolders через gdbus) — «открыть папку с выделением файла» есть штатно.

