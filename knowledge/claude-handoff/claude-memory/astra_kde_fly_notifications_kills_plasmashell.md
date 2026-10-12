---
name: astra-kde-fly-notifications-kills-plasmashell
description: "На машине пользователя (Astra 1.8 + KDE Plasma 5.27.8, X11) Fly-модуль «Уведомления» в «Параметрах системы» запускает fly-notifications, который убивает plasmashell (SIGTERM) и панель не респавнится; как вернуть и как диагностировать."
metadata: 
  node_type: memory
  type: project
  originSessionId: ff577de8-c175-4081-a0c9-078c64e09662
  modified: 2026-09-02T13:43:32.172Z
---

Инцидент 2026-09-02: пользователь открыл «Параметры системы → Персонализация → Уведомления»
(Fly-вариант `fly_kcm_notifications`, а не KDE `kcm_notifications`; в списке они оба называются
«Уведомления»). Модуль по D-Bus активирует `/usr/bin/fly-notifications`; тот берёт
`servicePid("org.freedesktop.Notifications")` (это plasmashell), ждёт таймер и делает
`kill(pid, SIGTERM)`, чтобы забрать сервис уведомлений себе. Юнит `plasma-plasmashell.service`
(`--no-respawn`, `Restart=on-failure`) считает SIGTERM чистым выходом и НЕ перезапускает
оболочку: пропадают панель и рабочий стол, kwin/krunner живут.

Восстановление: `systemctl --user start plasma-plasmashell.service` (работает из KRunner Alt+F2
или Konsole Ctrl+Alt+T). Конфиг панели при этом не страдает.

Диагностика «панель исчезла»: сначала `systemctl --user status plasma-plasmashell` и
`journalctl --user --since ... -o short-precise` (dbus-daemon пишет, кто запросил активацию
сервиса, с pid и comm). Читающие команды `qdbus … evaluateScript`, `spectacle -b -n`,
`plasmashell --version` оболочку не трогают, проверено.

Смежное: «трей прилип к значкам приложений» = у `org.kde.plasma.icontasks` выключена опция
«Занимать всё свободное место на панели» (`fill=false` в plasma-org.kde.plasma.desktop-appletsrc);
включается через настройки виджета или `widget.writeConfig("fill", true); widget.reloadConfig()`.

**Why:** на этой машине KDE Plasma живёт поверх Astra с Fly-компонентами, и Fly-модули
считают, что владеют сессией. Любой Fly-KCM/демон может «зачистить» Plasma-процессы.
**How to apply:** не советовать пользователю Fly-модули в «Параметрах системы» под KDE;
при пропаже панели первым делом проверять юнит и журнал, а не конфиг. См. также
[[target-environment-astra-1-8]], [[overlay-theme-from-kdeglobals]].
