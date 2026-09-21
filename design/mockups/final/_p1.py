# -*- coding: utf-8 -*-
"""Разделы «Общие» (01) и «Модели» (02) — полный макет A «Панель»."""
from _shell import (page, write, shell, head, footer, sb, m, MODELS, CATALOG_ORDER, DOMESTIC,
                    INSTALLED_SIZE, ic, tgl, key, btn, row, card, group, met, met2, level_bars,
                    stcell, sech, grid, cbx, dlbar, W, H, th)

DD = 'var(--fg3)'


# ══════════════════════════════════════════════════════════════════════════
# 01 · ОБЩИЕ
# ══════════════════════════════════════════════════════════════════════════
def sel(text, w=None, open_=False, dis=False):
    st = f' style="width:{w}px"' if w else ""
    cls = "sel" + (" selopen" if open_ else "")   # раскрытый список — граница primary, не фокус
    col = ' style="color:var(--fg-dis)"' if dis else ""
    return (f'<span class="{cls}"{st}><span{col}>{text}</span>'
            f'<span style="flex:1"></span>{ic("chevd", 13, DD)}</span>')


def seg(a, b, first=True):
    return (f'<span class="seg"><span class="{"on" if first else ""}">{a}</span>'
            f'<span class="{"" if first else "on"}">{b}</span></span>')


def hotkey_field(state):
    """Все состояния поля захвата комбинации (flows.md §3.4)."""
    if state == "idle":
        return (f'<div style="display:flex;gap:8px;align-items:center">{key("Ctrl + Space")}'
                f'{btn("Изменить", "sm")}</div>')
    if state == "capturing":
        return ('<div class="field cap"><span class="mono" style="font-size:14px;color:var(--fg1)">'
                'Нажмите комбинацию…</span><span style="flex:1"></span>'
                '<span class="c12">Esc — отмена</span></div>')
    if state == "captured":
        return ('<div class="field cap"><span class="mono" style="font-size:14px;color:var(--fg1)">'
                'Alt + F2</span><span style="flex:1"></span><span class="c12">Отпустите клавиши</span></div>')
    if state == "conflict":
        return (f'<div class="note w" style="margin-top:8px">{ic("alert", 15, "var(--warn-ink)")}'
                '<div><b>Комбинация занята в системе</b>'
                '<span class="mono">Alt + F2</span> назначена на действие «Показать KRunner». Оставить её '
                'можно, но диктовка может не сработать — система заберёт нажатие себе.'
                f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Оставить Alt + F2")}'
                f'{btn("Выбрать другую", "pri")}</div></div></div>')
    if state == "duplicate":
        return (f'<div class="note w" style="margin-top:8px">{ic("alert", 15, "var(--warn-ink)")}'
                '<div><b>Эта комбинация уже назначена</b>'
                '<span class="mono">Ctrl + Space</span> используется режимом «нажать-нажать». '
                'Выберите другую или сначала освободите её.'
                f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Выбрать другую", "pri")}'
                f'{btn("Открыть «нажать-нажать»", "gh")}</div></div></div>')
    if state == "not-grabbed":
        return (f'<div class="note e" style="margin-top:8px">{ic("alert", 15, "var(--err-ink)")}'
                '<div><b>Горячая клавиша не захвачена</b>'
                'Комбинацию <span class="mono">Ctrl + Space</span> уже использует другое приложение. '
                'Выберите другую — диктовка не работает, пока комбинация не назначена.'
                f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Выбрать другую", "pri")}'
                f'{btn("Повторить попытку")}</div></div></div>')
    if state == "success":
        return (f'<div class="note o" style="margin-top:8px">{ic("check", 15, "var(--ok-ink)")}'
                '<div><b>Комбинация назначена</b>'
                'Теперь зажмите <span class="mono">Ctrl + Alt + Пробел</span> и говорите. '
                'Проверить можно прямо сейчас — вставки не будет, пока курсор в этом окне.</div></div>')
    raise KeyError(state)


def general_rows(mode_first=True, autostart="on", indicator="Пилюля снизу экрана"):
    g1 = group("Диктовка", [
        row("Модель распознавания", btn("Переустановить", "sm", "refresh"),
            "GigaAM v3 RNN-T · 226 МБ на диске"),
        row("Горячая клавиша", hotkey_field("idle"),
            "Удерживайте и говорите — текст появится там, где курсор"),
        row("Режим", seg("Удерживать", "Нажать-нажать", mode_first),
            "Удерживать — самый предсказуемый вариант"),
        row("Микрофон", sel("Системный по умолчанию", 236)),
    ])
    g2 = group("Индикация", [
        row("Индикатор записи", sel(indicator, 236),
            "Пилюля не забирает фокус и не появляется в Alt+Tab"),
        row("Звук начала и конца записи", tgl(False)),
        row("Значок в системном трее",
            f'<span style="display:flex;gap:9px;align-items:center">'
            f'<span class="c12">Выключить нельзя</span>{tgl(True)}</span>',
            "Запись всегда видна — это требование приватности", dis=True),
    ])
    if autostart == "error":
        ar = row("Автозапуск при входе в систему", tgl(True))
        err = (f'<div class="r"><div class="note e" style="width:100%">{ic("alert", 15, "var(--err-ink)")}'
               '<div><b>Не удалось включить автозапуск</b>'
               'Программа не запустится сама при входе — открывайте её из меню приложений.'
               f'<div style="margin-top:9px">{btn("Повторить")}</div></div></div></div>')
        g3 = group("Запуск", [ar, err])
    else:
        g3 = group("Запуск", [
            row("Автозапуск при входе в систему", tgl(autostart == "on")),
        ])
    return g1 + g2 + g3


def general_base(theme):
    body = general_rows() + sb(6, 200)
    b = shell("Общие", head("Общие", "Модель, диктовка, индикация и запуск"), body,
              footer(state="disabled"))
    leg = ("<b>Базовое состояние раздела.</b> Первая строка — <b>модель распознавания</b>: имя, размер "
           "на диске и «Переустановить» (решение 2026-09-17: после мастера моделью управлять было негде; "
           "полноценный раздел «Модели» с каталогом — веха M6). Строка «Вторая "
           "комбинация» появляется только в режиме «нажать-нажать» — в режиме «удерживать» она не нужна. "
           "Строка = «заголовок · подпись · «?» · контрол справа» (канон категории). "
           "<b>disabled:</b> «Значок в системном трее» выключить нельзя — иначе запись стала бы скрытой "
           "(PRD §9.5); контрол показан с подписью «Выключить нельзя», не только серым цветом. "
           "Строка внизу: слева активная модель, справа «Проверка обновлений отключена» — оба сетевых "
           "тумблера по умолчанию пусты (F14.2).")
    return page(f"A · Общие — {th(theme)}",
                f'<b>Общие</b> — базовое состояние · {th(theme)} тема · 900×620', b, theme, leg)


def general_hotkey(theme):
    rows = [
        row("Горячая клавиша", "", "Нажмите нужную комбинацию — она запишется сама"),
        row("Вторая комбинация («нажать-нажать»)",
            f'<span class="muted" style="font-size:13px">Не назначена</span>', hint=False, dis=True),
    ]
    cap = ('<div style="margin-bottom:16px"><div class="grp">Диктовка</div>'
           f'<div class="card">{rows[0]}</div>'
           f'<div style="padding:0 0 0 0;margin-top:9px">{hotkey_field("capturing")}</div>'
           f'{hotkey_field("conflict")}</div>')
    body = cap
    body += ('<div class="grp">Как ведёт себя захват</div>'
             '<div class="card">'
             + row("Пока идёт захват", '<span class="c12">Все прочие строки заблокированы</span>',
                   "Esc отменяет захват и возвращает прежнюю комбинацию", hint=False, dis=True)
             + row("Проверка конфликтов", '<span class="c12">KDE · Astra Voice · системные</span>',
                   "Читаем kglobalshortcutsrc — имя чужого действия показываем прямым текстом",
                   hint=False) + '</div>')
    b = shell("Общие", head("Общие", "Назначение горячей клавиши"), body + sb(6, 210),
              footer(state="disabled"))
    gal = grid([
        stcell("покой", hotkey_field("idle"), "idle"),
        stcell("захват: ждём нажатия", hotkey_field("capturing"), "capturing"),
        stcell("клавиши зажаты", hotkey_field("captured"), "capturing"),
        stcell("успех", hotkey_field("success"), "success"),
    ]) + grid([
        stcell("комбинация занята в системе — предупреждение, не запрет", hotkey_field("conflict"), "conflict"),
        stcell("уже назначена на другой режим", hotkey_field("duplicate"), "duplicate"),
    ]) + grid([
        stcell("комбинацию не удалось захватить (X11 забрал другое приложение)",
               hotkey_field("not-grabbed"), "not-grabbed"),
    ], 1)
    body_full = b + sech("Поле захвата — все состояния") + gal
    leg = ("<b>Правило:</b> занятая комбинация — это <b>предупреждение с именем чужого действия</b>, а не "
           "запрет (PRD S1-A4): пользователь вправе оставить комбинацию. «Не захвачена» — единственное "
           "красное состояние, потому что диктовка в нём не работает. Захват идёт по «сырым» нажатиям; "
           "Esc всегда отменяет и возвращает прежнюю комбинацию.")
    return page(f"A · Общие: захват комбинации — {th(theme)}",
                f'<b>Общие → Горячая клавиша</b> — захват комбинации и конфликт · {th(theme)} тема',
                body_full, theme, leg)


def mic_row_states(state):
    if state == "loading":
        return ('<div class="card">' + row("Микрофон", sel("Ищу устройства…", 236),
                                           hint=False) + '</div>')
    if state == "list":
        pop = ('<div class="pop" style="position:static;box-shadow:none;margin-top:7px">'
               f'<div class="po on">{ic("check", 13, "var(--primary)")}<span>Системный по умолчанию'
               '<span class="d2">Сейчас: Встроенный микрофон</span></span></div>'
               '<div class="po"><span style="width:13px"></span><span>Встроенный микрофон</span></div>'
               '<div class="po"><span style="width:13px"></span><span>Гарнитура Jabra Evolve2 30</span></div>'
               '<div class="po"><span style="width:13px"></span><span>Микрофон монитора Dell U2723QE'
               '</span></div>'
               '<div class="msep"></div>'
               f'<div class="po">{ic("refresh", 13, DD)}<span>Обновить список</span></div></div>')
        return ('<div class="card">' + row("Микрофон", sel("Системный по умолчанию", 236, True),
                                           hint=False) + '</div>' + pop)
    if state == "empty":
        return ('<div class="card">' + row("Микрофон", sel("Устройств нет", 236, dis=True),
                                           hint=False, dis=True) + '</div>'
                + f'<div class="note e" style="margin-top:9px">{ic("alert", 15, "var(--err-ink)")}'
                  '<div><b>Микрофон не найден</b>'
                  'Подключите микрофон или гарнитуру и попробуйте снова.'
                  f'<div style="margin-top:9px">{btn("Обновить список", "pri", "refresh")}</div></div></div>')
    if state == "busy":
        return ('<div class="card">' + row("Микрофон", sel("Гарнитура Jabra Evolve2 30", 236),
                                           hint=False) + '</div>'
                + f'<div class="note e" style="margin-top:9px">{ic("alert", 15, "var(--err-ink)")}'
                  '<div><b>Микрофон недоступен</b>'
                  'Устройство занято другой программой. '
                  'Закройте приложение, которое пишет звук. Мы уже повторили попытку три раза.'
                  f'<div style="margin-top:9px">{btn("Повторить", "pri", "refresh")}</div></div></div>')
    if state == "silent":
        return ('<div class="card">' + row("Микрофон", sel("Встроенный микрофон", 236),
                                           hint=False) + '</div>'
                + f'<div class="note w" style="margin-top:9px">{ic("alert", 15, "var(--warn-ink)")}'
                  '<div><b>Микрофон молчит</b>'
                  'Устройство открыто, но звука нет. Обычно помогает перезапуск звука — '
                  'это безопасно и не требует пароля.'
                  f'<div style="display:flex;gap:8px;margin-top:9px;align-items:center">'
                  f'{btn("Перезапустить звук", "pri", "refresh")}{btn("Что это", "gh")}'
                  '<span class="c12">Проверим уровень сами через 3 с</span></div></div></div>')
    if state == "restarting":
        return (f'<div class="note i">{ic("refresh", 15, DD)}'
                '<div><b>Перезапускаю звук…</b>'
                'Проверим уровень сразу после перезапуска</div></div>')
    if state == "no-service":
        return (f'<div class="note w">{ic("alert", 15, "var(--warn-ink)")}'
                '<div><b>Звук не отвечает</b>'
                'Перезапуск звука на этом компьютере недоступен. Обратитесь к администратору.'
                f'<div style="margin-top:9px">{btn("Перезапустить звук", "dis", "refresh")}'
                '</div></div></div>')
    raise KeyError(state)


def general_mic(theme):
    body = ('<div style="margin-bottom:16px"><div class="grp">Устройство ввода</div>'
            + mic_row_states("list") + '</div>'
            + '<div style="margin-bottom:16px"><div class="grp">Проверка</div>'
            '<div class="card"><div class="r" style="gap:14px">'
            f'<div class="lvbox" style="flex:1">{level_bars("live")}'
            '<div><div class="lbl">Скажите что-нибудь</div>'
            '<div class="sub">Пик −18 дБ · устройство слышит вас</div></div></div>'
            f'{btn("Тестовая диктовка", "", "chip")}</div></div></div>' + sb(6, 200))
    b = shell("Общие", head("Общие", "Микрофон и проверка звука"), body, footer(state="disabled"))
    gal = grid([
        stcell("поиск устройств", mic_row_states("loading"), "loading"),
        stcell("устройств нет", mic_row_states("empty"), "empty"),
    ]) + grid([
        stcell("устройство занято (3 автоповтора уже прошли)", mic_row_states("busy"), "busy"),
        stcell("тишина: известная гонка WirePlumber", mic_row_states("silent"), "silent"),
    ]) + grid([
        stcell("идёт перезапуск службы", mic_row_states("restarting"), "restarting"),
        stcell("перезапуск звука недоступен", mic_row_states("no-service"), "disabled"),
    ]) + grid([
        stcell("уровень: живой сигнал",
               f'<div class="lvbox">{level_bars("live")}<div class="sm">Пик −18 дБ · слышим вас</div></div>',
               "live"),
        stcell("уровень: тишина 2 с",
               f'<div class="lvbox">{level_bars("flat")}<div class="sm" style="color:var(--warn-ink)">'
               'Пик ниже −60 дБ · похоже, устройство молчит</div></div>', "silent"),
        stcell("уровень: покой",
               f'<div class="lvbox">{level_bars("quiet")}<div class="sm">Скажите что-нибудь</div></div>',
               "idle"),
    ], 3)
    leg = ("<b>Ключевой случай — «микрофон молчит»</b> (пик &lt; −60 дБ за 2 с): это известная гонка двух "
           "WirePlumber на Astra, поэтому вместо «ошибка -3» человек видит объяснение и <b>одну кнопку</b> "
           "«Перезапустить звуковую службу» (<span class=\"mono\">systemctl --user restart wireplumber</span>, "
           "без пароля), после которой уровень перепроверяется сам. Все состояния списка устройств "
           "закрыты: loading · list · empty · busy · silent · служба недоступна.")
    return page(f"A · Общие: микрофон — {th(theme)}",
                f'<b>Общие → Микрофон</b> — устройства, уровень и «микрофон молчит» · {th(theme)} тема',
                b + sech("Состояния микрофона") + gal, theme, leg)


def general_indicator(theme):
    pop = ('<div class="pop" style="right:22px;top:208px">'
           f'<div class="po on">{ic("check", 13, "var(--primary)")}<span>Пилюля снизу экрана'
           '<span class="d2">Как в Handy: у нижнего края, поверх окон</span></span></div>'
           '<div class="po"><span style="width:13px"></span><span>Пилюля сверху экрана'
           '<span class="d2">Если снизу мешает панель задач</span></span></div>'
           '<div class="po"><span style="width:13px"></span><span>Только значок в трее'
           '<span class="d2">Пилюли нет; запись видна по значку</span></span></div></div>')
    body = general_rows() + sb(6, 170)
    b = shell("Общие", head("Общие", "Индикация записи"), body, footer(state="disabled"), over=pop)
    auto_on = ('<div class="card">' + row("Автозапуск при входе в систему", tgl(True),
               "Автозапуск включён", hint=False) + '</div>')
    auto_off = ('<div class="card">' + row("Автозапуск при входе в систему", tgl(False),
                "Открывать программу придётся вручную — из меню приложений", hint=False) + '</div>')
    auto_err = (f'<div class="note e">{ic("alert", 15, "var(--err-ink)")}'
                '<div><b>Не удалось включить автозапуск</b>'
                'Программа не запустится сама при входе — открывайте её из меню приложений.'
                f'<div style="margin-top:9px">{btn("Повторить", "pri", "refresh")}</div></div></div>')
    off_note = (f'<div class="note i">{ic("info", 15, DD)}'
                '<div><b>Вы отключили индикатор</b>'
                'Включить звуковой сигнал начала и конца записи? Значок в трее продолжит показывать '
                'запись в любом случае.'
                f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Включить звук", "pri")}'
                f'{btn("Не нужно")}</div></div></div>')
    toggle_mode = ('<div class="card">'
                   + row("Режим", seg("Удерживать", "Нажать-нажать", False), hint=False)
                   + row("Вторая комбинация («нажать-нажать»)",
                         f'<span style="display:flex;gap:8px;align-items:center">'
                         f'<span class="muted" style="font-size:13px">Не назначена</span>'
                         f'{btn("Назначить", "sm")}</span>',
                         "Одно нажатие — старт, второе — стоп", hint=False) + '</div>')
    gal = grid([
        stcell("режим: удерживать (по умолчанию)",
               '<div class="card">' + row("Режим", seg("Удерживать", "Нажать-нажать", True),
                                          "Вторая комбинация не нужна", hint=False) + '</div>', "hold"),
        stcell("режим: нажать-нажать — появляется строка второй комбинации", toggle_mode, "toggle"),
    ]) + grid([
        stcell("автозапуск включён", auto_on, "on"),
        stcell("автозапуск выключен", auto_off, "off"),
    ]) + grid([
        stcell("автозапуск: ошибка записи ярлыка", auto_err, "error"),
        stcell("после выключения пилюли предлагаем звук — один раз (S12)", off_note, "success"),
    ])
    leg = ("<b>Выпадающий список индикатора</b> — три значения (пилюля снизу · пилюля сверху · только трей); "
           "четвёртого «выключить всё» нет: полностью скрыть запись нельзя. «Пилюля сверху» нужна там, "
           "где снизу стоит панель задач KDE. Автозапуск показан во всех трёх состояниях, включая ошибку "
           "записи ярлыка (S11).")
    return page(f"A · Общие: индикатор и запуск — {th(theme)}",
                f'<b>Общие → Индикатор</b> — выпадающий список, режим, автозапуск · {th(theme)} тема',
                b + sech("Режим, автозапуск, отключение пилюли") + gal, theme, leg)


# ══════════════════════════════════════════════════════════════════════════
# 02 · МОДЕЛИ
# ══════════════════════════════════════════════════════════════════════════
# Карточка модели = ВЫБОР (решение заказчика 2026-09-17, decisions/log.md).
# Верхняя строка: имя · вендор + «Рекомендуем» + ОДИН бейдж состояния.
# Слева отметка выбора (чекбокс — выбор множественный); «Скачать» и «Из файла…» из карточки
# убраны, прогресс загрузки уехал в сквозную полоску мастера (_shell.dlbar).
#
# состояние → (отметка выбора, доп. класс карточки, бейдж состояния)
CARD_STATE = {
    # ── выбор сетевой модели ────────────────────────────────────────────
    "avail":         ("off",     "",      None),
    "selected":      ("on",      " pick", None),
    "hover":         ("off",     " hov",  None),
    "focus":         ("off",     " foc",  None),
    "new":           ("off",     "",      ("Новое", "new")),
    "lowram":        ("off",     "",      None),
    "notrec":        ("off",     "",      None),
    # выбрать нельзя: отметка ПУСТАЯ и недоступная, причина — текстом в низу карточки
    "nospace":       ("blocked", "",      None),
    "nonet":         ("blocked", "",      None),
    "offline-user":  ("blocked", "",      None),
    "policy":        ("blocked", "",      None),
    # ── загрузка: подробности в сквозной полоске, в карточке только итог ──
    "downloading":   ("locked",  " pick", ("Загружается", "new")),
    "queued":        ("locked",  " pick", ("В очереди", "new")),
    "verifying":     ("locked",  " pick", ("Проверяю…", "new")),
    "failed":        ("off",     "",      None),
    "badsha":        ("off",     "",      None),
    # ── установленные: отметка стоит, менять её нельзя ───────────────────
    "installed":     ("locked",  "",      ("Установлена", "ins")),
    "active":        ("locked",  " act",  ("Установлена и активна", "act")),
    "switching":     ("locked",  " act",  ("Переключаю…", "act")),
    "custom":        ("locked",  "",      ("Установлена", "ins")),
    "corrupted":     ("locked",  "",      None),
    "removed":       ("locked",  "",      ("Установлена", "ins")),
    "update":        ("locked",  "",      ("Обновление доступно", "upd")),
    "updating":      ("locked",  "",      ("Обновляю…", "upd")),
    "update-failed": ("locked",  "",      ("Обновление доступно", "upd")),
}

# Низ карточки. Действий по загрузке здесь больше нет — только то, что делают
# с УЖЕ установленной моделью, и «Повторить» после неудачи.
CARD_ACTS = {
    "active": btn("Удалить", "sm dis"),
    "switching": btn("Удалить", "sm dis"),
    "installed": btn("Сделать рабочей", "sm pri") + " " + btn("Удалить", "sm"),
    "custom": btn("Сделать рабочей", "sm pri") + " " + btn("Удалить", "sm"),
    "removed": btn("Сделать рабочей", "sm pri") + " " + btn("Удалить", "sm"),
    "corrupted": btn("Переустановить", "sm pri") + " " + btn("Удалить", "sm"),
    "update": btn("Обновить", "sm pri") + " " + btn("Что нового", "sm"),
    "update-failed": btn("Повторить", "sm pri") + " " + btn("Подробнее", "sm"),
    "updating": btn("Отмена", "sm"),
    "failed": btn("Повторить", "sm pri"),
    "badsha": btn("Повторить", "sm pri") + " " + btn("Подробнее", "sm"),
    "nospace": btn("Открыть папку моделей", "sm", "folder"),
    "downloading": btn("Отмена", "sm"),
    "queued": btn("Отмена", "sm"),
    "verifying": '<span class="c12">Отмена недоступна</span>',
}


def badges_html(items):
    """Бейджи верхней строки: «Рекомендуем» и один бейдж состояния, больше двух не бывает."""
    return "".join(f'<span class="bd {k}">{t}</span>' for t, k in items[:2])


def mets_block(mo):
    """Полоски метрик + честная подпись об источнике цифр (§5.3): это чужой бенчмарк."""
    has_data = mo["qkind"] != "none" or mo["skind"] != "none"
    src = '<div class="msrc">цифры авторов, не с этого компьютера</div>' if has_data else ""
    return ('<div class="mmets">'
            + met2("Качество", mo["q"], mo["qv"], mo["measured"], mo["qkind"])
            + met2("Скорость", mo["s"], mo["sv"], mo["measured"], mo["skind"])
            + src + '</div>')


def mcard(mid, state=None, badges=None, note=None, acts=None, extra="", cls_extra="", mets=True):
    mo = m(mid)
    st = state or mo["status"]
    sel, cls_state, state_bd = CARD_STATE[st]
    cls = "mc" + cls_state + cls_extra
    bl = list(badges if badges is not None else mo["badges"])
    if state_bd:
        bl.append(state_bd)
    bh = badges_html(bl)
    top = (f'<div class="mtop">{cbx(sel)}<div class="ml">'
           f'<div class="mhead"><span class="mname">{mo["name"]} '
           f'<span class="mven">· {mo["vendor"]}</span></span>{bh}</div>'
           f'<div class="mpurp">{mo["purpose"]}</div>'
           f'</div>{mets_block(mo) if mets else ""}</div>')
    punct = "с пунктуацией" if mo["punct"] else "без пунктуации"
    tags = (f'{mo["lang"]} <span class="dot"></span> {punct} <span class="dot"></span> '
            f'{mo["lic"]} <span class="dot"></span> {mo["origin"]}')
    n = ""
    if note:
        kind, text = note
        colors = {"w": "var(--warn-ink)", "e": "var(--err-ink)", "i": "var(--fg3)"}
        icon = "alert" if kind in ("w", "e") else "info"
        n = (f'<span style="color:{colors[kind]};display:inline-flex;align-items:center;gap:5px">'
             f'{ic(icon, 12, colors[kind])}{text}</span><span class="dot"></span>')
    a = acts if acts is not None else CARD_ACTS.get(st, "")
    body = ('<div class="mhr"></div>'
            f'<div class="mspace">Занимает места: <b>{mo["disk"]}</b> на диске '
            f'<span class="dot"></span> <b>{mo["ram"]}</b> в памяти при работе</div>'
            '<div class="mbot" style="margin-top:6px">'
            f'{tags}<span class="sp"></span>{n} {a}</div>')
    return f'<div class="{cls}">{top}{body}{extra}</div>'


def filters(theme, domestic=False, count=None):
    if count is None:
        count = (f"Показано 6 из 12 · установлено 3 · {INSTALLED_SIZE} на диске" if domestic
                 else f"Установлено 3 из 12 · {INSTALLED_SIZE} на диске")
    return ('<div style="display:flex;gap:8px;align-items:center;margin:0 0 12px">'
            f'<span class="chip">{ic("globe", 13, DD)} Все языки {ic("chevd", 12, DD)}</span>'
            f'<span class="chip{" on" if domestic else ""}">Только отечественные {tgl(domestic)}</span>'
            f'<span style="flex:1"></span><span class="c12">{count}</span></div>')


def pick_bar(total, free, action="", warn=False):
    """Строка итога под карточками: сколько будет скачано + действие. Общая для шага 2 и каталога."""
    col = "var(--err-ink)" if warn else "var(--fg1)"
    return ('<div style="display:flex;align-items:center;gap:9px;margin:10px 0 0;flex-wrap:wrap">'
            f'<span style="font-size:13px;color:{col}">{total}</span>'
            f'<span class="dot"></span><span class="c12">{free}</span>'
            f'<span style="flex:1"></span>{action}</div>')


def catalog_body(theme):
    s = filters(theme)
    s += '<div class="grp">Установленные · 3</div>'
    s += mcard("rnnt", "active", [("Рекомендуем", "rec")])
    s += mcard("ctc", "installed")
    s += mcard("rnnt-np", "downloading")
    s += '<div class="grp" style="margin-top:14px">Доступные · 9</div>'
    s += mcard("ml", "new")
    s += mcard("tone", "selected")
    s += mcard("vosk", "avail",
               note=("i", "Правообладатель — Alpha Cephei Inc. (США), команда из России"))
    s += mcard("vosk-s", "avail")
    s += mcard("wturbo", "lowram",
               note=("w", "Нужно ~2,0 ГБ ОЗУ — на этом компьютере 8 ГБ, может не хватить"))
    s += mcard("wsmall", "avail",
               note=("i", "Нет цифр по нашему протоколу: FLEURS 11,4 % — другой набор; "
                          "замерим на вашем компьютере"))
    s += mcard("wbase", "notrec",
               note=("w", "Для русского не рекомендуется: ошибка в каждом третьем слове"))
    s += mcard("mllarge", "avail")
    s += mcard("nemo", "avail",
               note=("i", "Лицензия CC-BY-4.0 — атрибуция автора попадёт в «О программе»"))
    s += pick_bar("Будет скачано 144,2 МБ", "свободно на диске 42,1 ГБ",
                  btn("Скачать выбранное", "pri", "down"))
    return s


def catalog_domestic():
    """Состояние фильтра «только отечественные»: 5 GigaAM + T-one (по юрлицу правообладателя)."""
    s = filters(None, domestic=True)
    s += '<div class="grp">Установленные · 3</div>'
    s += mcard("rnnt", "active", [("Рекомендуем", "rec")])
    s += mcard("ctc", "installed")
    s += mcard("rnnt-np", "downloading")
    s += '<div class="grp" style="margin-top:14px">Доступные · 3</div>'
    s += mcard("ml", "new")
    s += mcard("tone", "avail")
    s += mcard("mllarge", "avail")
    s += pick_bar("Ничего не выбрано", "свободно на диске 42,1 ГБ",
                  btn("Скачать выбранное", "pri dis", "down"))
    s += ('<div class="note i" style="margin-top:4px">' + ic("info", 15, DD)
          + '<div><b>Скрыто 6 моделей</b>'
            'Whisper (OpenAI, США), NeMo FastConformer (NVIDIA, США) и обе Vosk '
            '(Alpha Cephei Inc., Делавэр — команда из России, но юрлицо зарубежное). '
            'Фильтр смотрит на правообладателя, а не на страну разработчиков.</div></div>')
    return s


def models_catalog(theme):
    right = btn("Установить из файла или папки…", "sm", "folder")
    hd = head("Модели", "Честные цифры: размер на диске, память в работе, качество и скорость", right)
    b = shell("Модели", hd, catalog_body(theme) + sb(40, 150), footer(state="model-update"))
    full = ('<div class="win" style="width:900px;padding:0">'
            '<div style="background:var(--bg-app);padding:14px 22px 18px">'
            + catalog_body(theme) + '</div></div>')
    leg = ("<b>Решение G2: каталог показывается целиком с прокруткой</b> — без «показать ещё». Всего 12 "
           "карточек (10 Must + 2 Could, PRD §7.2); нужная стоит первой и помечена «Установлена и активна». "
           "<b>Все цифры — из <span class=\"mono\">research/catalog-numbers.md</span> (2026-09-08):</b> "
           "размер = сумма точных байт файлов рантайма из HF API в десятичных МБ; WER — Russian "
           "LibriSpeech, бенчмарк onnx-asr; скорость — столбец «x64 RTFx (int8)». Где int8-замера нет "
           "(T-one, обе GigaAM Multilingual) — полоска помечена «бенчмарк fp32». У Whisper small цифр "
           "по протоколу нет вовсе: полоски пунктиром и «нет данных», выдумывать нельзя (§7.1). "
           "<b>Место:</b> «Занимает места: 226 МБ на диске · 768 МБ в памяти при работе» — ОДНА строка обычным "
           "весом; слово «замерено» в карточке не появляется, пока замер не сделан на этом компьютере "
           "(веха M6), поэтому все полоски метрик серые. <b>Карточка — это выбор:</b> отметка слева, "
           "кнопок «Скачать»/«Из файла…» в ней нет, внизу списка общий итог и одна кнопка. "
           "<b>Фильтр «только отечественные»</b> смотрит на юрлицо правообладателя: остаются 5 GigaAM "
           "и T-one, обе Vosk уходят (Alpha Cephei Inc., США).")
    return page(f"A · Модели: каталог — {th(theme)}",
                f'<b>Модели</b> — каталог целиком (12 карточек) · {th(theme)} тема · 900×620',
                b + sech("Каталог целиком — лента прокрутки, 12 карточек") + full
                + sech("Фильтр «только отечественные» включён — 6 моделей")
                + '<div class="win" style="width:900px;padding:0">'
                  '<div style="background:var(--bg-app);padding:14px 22px 18px">'
                + catalog_domestic() + '</div></div>', theme, leg)


def tooltip_bars():
    return ('<div class="pop" style="position:static;box-shadow:none;width:340px;min-width:0;padding:12px">'
            '<div class="strong" style="font-size:13px;margin-bottom:5px">Качество · WER 7,60 %</div>'
            '<div class="c12" style="line-height:1.55">Доля слов с ошибкой на наборе Russian LibriSpeech, '
            'бенчмарк onnx-asr. Полоска одна и та же для всех моделей: чем длиннее — тем меньше ошибок. '
            'Пунктуация в полоску не входит — у неё отдельный значок.<br>'
            '<b>WER бенчмарка измерен на fp32-весах</b> той же модели: для int8, которые мы скачиваем, '
            'публичных замеров нет ни у одной модели.<br>'
            'Скорость берётся из столбца «x64 RTFx (int8)»; где int8-замера нет, полоска помечена '
            '«бенчмарк fp32».<br>'
            '<span style="color:var(--primary)">Как мы считаем →</span></div></div>')


def models_card_states(theme):
    cells = [
        stcell("не выбрана — исходное состояние сетевой модели",
               mcard("tone", "avail"), "unselected"),
        stcell("выбрана — отметка стоит, карточка подсвечена",
               mcard("tone", "selected"), "selected"),
        stcell("наведена мышью — вся карточка кликабельна",
               mcard("tone", "hover"), "hover"),
        stcell("в фокусе с клавиатуры — рамка 2 px, Пробел переключает отметку",
               mcard("tone", "focus"), "focus"),
        stcell("установлена и активна — отметка недоступна",
               mcard("rnnt", "active", [("Рекомендуем", "rec")]), "active"),
        stcell("установлена, но не рабочая", mcard("ctc", "installed"), "installed"),
        stcell("загружается — подробности в сквозной полоске мастера",
               mcard("rnnt-np", "downloading"), "downloading"),
        stcell("в очереди — загрузки идут по одной", mcard("tone", "queued"), "queued"),
        stcell("проверяю модель", mcard("tone", "verifying"), "verifying"),
        stcell("переключение рабочей модели",
               mcard("ctc", "switching"), "switching"),
        stcell("не получилось загрузить",
               mcard("tone", "failed",
                     note=("e", "Не удалось загрузить модель — соединение оборвалось")), "failed"),
        stcell("новая в каталоге (≤ 30 дней)", mcard("ml", "new"), "new"),
        stcell("доступно обновление ревизии",
               mcard("ctc", "update"),
               "update-available"),
        stcell("проверяю новую ревизию", mcard("ctc", "updating"), "updating"),
        stcell("новая ревизия не прошла проверку",
               mcard("ctc", "update-failed",
                     note=("w", "Новая ревизия не прошла пробное распознавание — оставлена текущая")),
               "update-failed"),
        stcell("предупреждение по ОЗУ (не запрет)",
               mcard("wturbo", "lowram",
                     note=("w", "Нужно ~2,0 ГБ ОЗУ — на этом компьютере 8 ГБ, может не хватить")),
               "low-ram"),
        stcell("не рекомендуется для русского",
               mcard("wbase", "notrec",
                     note=("w", "Для русского не рекомендуется: ошибка в каждом третьем слове")),
               "not-recommended"),
        stcell("нет цифр по нашему протоколу — полоски пунктиром",
               mcard("wsmall", "avail",
                     note=("i", "FLEURS 11,4 % — другой набор и другой формат весов; "
                                "замерим на вашем компьютере")),
               "no-benchmark"),
        stcell("скорость измерена на fp32-весах (int8-замера нет)",
               mcard("tone", "avail",
                     note=("i", "Модель поставляется только в fp32 — 144,2 МБ, ОЗУ выше оценки")),
               "fp32-benchmark"),
        stcell("нет сети",
               mcard("tone", "nonet", note=("i", "Нет доступа к huggingface.co")), "no-network"),
        stcell("офлайн-режим включён вами",
               mcard("tone", "offline-user",
                     note=("i", "Включён офлайн-режим — «Сеть и обновления»")), "offline-user"),
        stcell("запрещено администратором",
               mcard("wturbo", "policy",
                     note=("i", "Задано администратором: только отечественные модели")), "policy-offline"),
        stcell("ошибка: файл не прошёл проверку",
               mcard("tone", "badsha",
                     note=("e", "Файл не прошёл проверку — загруженное удалено")), "error-sha"),
        stcell("не хватает места — выбрать нельзя",
               mcard("wturbo", "nospace",
                     note=("e", "Нужно ещё 120 МБ, свободно 100 МБ")), "no-space"),
        stcell("модель повреждена",
               mcard("ctc", "corrupted",
                     note=("e", "Файлы модели не читаются — переустановите")), "corrupted"),
        stcell("снята с каталога",
               mcard("vosk-s", "removed", note=("i", "Снята с каталога — обновлений не будет")),
               "removed-from-catalog"),
    ]
    rules = ('<div class="stc"><div class="stn">правило бейджей — «Рекомендуем» + один бейдж состояния</div>'
             '<div class="sm" style="line-height:1.7">Верхняя строка карточки: имя · производитель, '
             'затем <b>Рекомендуем</b> и справа от него <b>ровно один</b> бейдж состояния — '
             '«Установлена и активна» у рабочей модели, «Установлена» у скачанной, но не рабочей, '
             '«Загружается» / «В очереди» / «Проверяю…» / «Обновление доступно» по ходу дела. '
             'Одновременно «Установлена» и «Установлена и активна» не показываются. Нижний чип '
             '«Установлена · 226 МБ» убран — размер стоит строкой «Занимает места».'
             '<div style="display:flex;gap:6px;margin-top:9px;flex-wrap:wrap">'
             + badges_html([("Рекомендуем", "rec"), ("Установлена и активна", "act")])
             + badges_html([("Установлена", "ins")]) + badges_html([("Загружается", "new")])
             + badges_html([("Обновление доступно", "upd")])
             + '</div></div></div>')
    tip = ('<div class="stc"><div class="stn">подсказка при наведении на полоску</div>'
           + tooltip_bars() + '</div>')
    leg = ("<b>Карточка сетевой модели — это ВЫБОР</b> (решение заказчика 2026-09-17): кликабельна "
           "целиком, слева отметка выбора, выбор <b>множественный</b>. Кнопок «Скачать» и «Из файла…» "
           "в карточке больше нет — загрузка стартует по «Продолжить», а её ход показывает сквозная "
           "полоска внизу мастера. Каждое состояние различимо не только цветом: у ошибок треугольник и "
           "красная подпись, у предупреждений — треугольник и жёлтая, у нейтральных пояснений — «i». "
           "<b>Полоски метрик серые у всех моделей</b>: это чужой бенчмарк, а не замер на этом "
           "компьютере (§5.3) — под ними стоит подпись «цифры авторов, не с этого компьютера».")
    return page(f"A · Модели: состояния карточки — {th(theme)}",
                f'<b>Модели → карточка</b> — все состояния выбора и установки · {th(theme)} тема',
                grid(cells, 1) + sech("Правила") + grid([rules, tip]), theme, leg)


def models_file_dialogs(theme):
    picker = ('<div class="dlg" style="width:460px"><div class="dh">'
              + ic("folder", 14, DD) + 'Выбор папки с моделью</div>'
              '<div class="db" style="flex-direction:column;gap:10px">'
              '<div class="sm">Укажите папку с моделью или архив, полученный от '
              'администратора.</div>'
              '<div class="field mono" style="font-size:12px">/media/usb/models/gigaam-v3-e2e-rnnt-int8'
              f'<span style="flex:1"></span>{ic("folder", 13, DD)}</div>'
              '<div class="c12">Найдено 4 файла · 226 МБ</div></div>'
              f'<div class="df"><span class="sp"></span>{btn("Отмена")}{btn("Проверить и установить", "pri")}'
              '</div></div>')
    ok = (f'<div class="note o">{ic("check", 15, "var(--ok-ink)")}'
          '<div><b>Модель установлена</b>'
          'GigaAM v3 RNN-T · 226 МБ · контрольные суммы совпали с манифестом каталога. '
          'Ревизия <span class="mono">a6039be</span> от 16.12.2025.'
          f'<div style="margin-top:9px">{btn("Выбрать активной", "pri")}</div></div></div>')
    bad = (f'<div class="note e">{ic("alert", 15, "var(--err-ink)")}'
           '<div><b>Файл не прошёл проверку</b>'
           'Файлы модели повреждены — не совпадают с описанием в каталоге. '
           'Мы ничего не устанавливали.'
           f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Выбрать другую папку", "pri")}'
           f'{btn("Подробнее", "gh")}</div></div></div>')
    unknown = (f'<div class="note w">{ic("alert", 15, "var(--warn-ink)")}'
               '<div><b>Модель не из каталога</b>'
               'Сверить контрольные суммы не с чем — этой модели нет в манифесте. Установим как '
               '«Своя модель»: обновлений и метрик качества у неё не будет.'
               f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Установить всё равно", "pri")}'
               f'{btn("Отмена")}</div></div></div>')
    incomplete = (f'<div class="note e">{ic("alert", 15, "var(--err-ink)")}'
                  '<div><b>В папке не хватает файлов</b>'
                  'Не хватает одного из файлов модели. Нужны все файлы — обычно они лежат рядом '
                  'в одной папке.'
                  f'<div style="margin-top:9px">{btn("Выбрать другую папку", "pri")}</div></div></div>')
    delete = ('<div class="dlg"><div class="dh">' + ic("trash", 14, DD) + 'Удалить модель</div>'
              f'<div class="db">{ic("trash", 22, "var(--err-ink)")}'
              '<div><div class="h3">Удалить GigaAM v3 CTC?</div>'
              '<div class="sm" style="margin-top:6px">С диска будет удалено <b>224,9 МБ</b> из папки '
              'моделей. Настройки и статистика останутся. Скачать модель заново можно '
              'в любой момент.</div></div></div>'
              f'<div class="df"><span class="sp"></span>{btn("Отмена")}'
              f'{btn("Удалить", "pri")}</div></div>')
    delete_active = ('<div class="dlg"><div class="dh">' + ic("info", 14, DD) + 'Удаление недоступно</div>'
                     f'<div class="db">{ic("info", 22, "var(--fg3)")}'
                     '<div><div class="h3">Сначала выберите другую модель</div>'
                     '<div class="sm" style="margin-top:6px">GigaAM v3 RNN-T сейчас активна — без модели '
                     'диктовка работать не будет. Выберите другую установленную модель, после этого '
                     'удаление станет доступно.</div></div></div>'
                     f'<div class="df"><span class="sp"></span>{btn("Понятно")}'
                     f'{btn("Открыть список моделей", "pri")}</div></div>')
    custom = stcell("установленная своя модель — без метрик и обновлений",
                    mcard("nemo", "custom", badges=[],
                          note=("i", "Своя модель: качество и скорость не измерены, обновлений нет")),
                    "custom")
    leg = ("<b>«Установить из файла…»</b> — единственный путь в закрытом контуре, поэтому он есть на каждом "
           "экране, где есть «Скачать». Показаны все четыре исхода проверки: сумма сошлась · сумма не "
           "сошлась · модели нет в манифесте (ставим как «Своя модель») · не хватает файлов. "
           "<b>Удаление</b> подтверждается всегда и честно называет объём; у активной модели удаление "
           "недоступно и объясняет, почему.")
    return page(f"A · Модели: файл и удаление — {th(theme)}",
                f'<b>Модели</b> — установка из файла, проверка, удаление · {th(theme)} тема',
                sech("Установить из файла…") + grid([stcell("выбор папки", picker, "picker"),
                                                     stcell("проверка прошла", ok, "ok")])
                + grid([stcell("контрольная сумма не совпала", bad, "bad-sum"),
                        stcell("модели нет в манифесте", unknown, "unknown")])
                + grid([stcell("в папке не хватает файлов", incomplete, "incomplete"), custom])
                + sech("Удаление модели")
                + grid([stcell("подтверждение удаления", delete, "confirm"),
                        stcell("удаление активной модели недоступно", delete_active, "disabled")]),
                theme, leg)


def models_empty_offline(theme):
    body = (filters(theme, count="Установлено 0 · моделей на диске нет")
            + f'<div class="note w" style="margin-bottom:13px">{ic("alert", 15, "var(--warn-ink)")}'
              '<div><b>Нет доступа к каталогу моделей</b>'
              'Не удалось связаться с <span class="mono">huggingface.co</span>. Проверьте подключение '
              'или попросите у администратора адрес корпоративного зеркала. Модель можно поставить '
              'из файла или папки — например, с флешки.'
              f'<div style="display:flex;gap:8px;margin-top:10px">'
              f'{btn("Установить из файла или папки…", "pri", "folder")}{btn("Повторить", "", "refresh")}'
              f'{btn("Указать адрес каталога", "gh")}</div></div></div>'
            + '<div class="grp">Установленные · 0</div>'
              '<div class="card"><div class="r" style="padding:22px 14px;justify-content:center">'
              '<div style="text-align:center;max-width:420px">'
              '<div class="h3">Пока ни одной модели</div>'
              '<div class="sm" style="margin-top:5px">Без модели диктовка не работает. Встроенный '
              'список моделей показан ниже — он есть в пакете и не требует сети, но сами веса нужно '
              'принести из файла.</div></div></div></div>'
            + '<div class="grp" style="margin-top:14px">Доступные · 12 — из встроенного списка</div>'
            + mcard("rnnt", "nonet", [("Рекомендуем", "rec")],
                    note=("i", "Нет доступа к huggingface.co"))
            + mcard("ctc", "nonet", note=("i", "Нет доступа к huggingface.co"))
            + sb(40, 160))
    b = shell("Модели", head("Модели", "Каталог недоступен — работает установка из файла"), body,
              footer("Модель не выбрана — установить", "unavailable"))
    leg = ("<b>Пустое состояние + нет сети.</b> Правило PRD: сеть не блокирует UI модалкой и не даёт "
           "пустого списка — встроенный манифест показывается всегда, у карточек недоступна только "
           "отметка выбора, а «Установить из файла или папки…» работает. В сообщении нет слова «VPN» "
           "(запрет F14.5) — "
           "есть путь через администратора и корпоративное зеркало. Строка внизу слева честно говорит "
           "«Модель не выбрана — установить».")
    return page(f"A · Модели: пусто и нет сети — {th(theme)}",
                f'<b>Модели</b> — нет сети и нет установленных моделей · {th(theme)} тема',
                b, theme, leg)


def build():
    out = []
    for theme in ("light", "dark"):
        d = "" if theme == "light" else "-dark"
        out.append(write(f"01-general-base{d}.html", general_base(theme)))
        out.append(write(f"01-general-hotkey{d}.html", general_hotkey(theme)))
        out.append(write(f"01-general-mic{d}.html", general_mic(theme)))
        out.append(write(f"01-general-indicator{d}.html", general_indicator(theme)))
        out.append(write(f"02-models-catalog{d}.html", models_catalog(theme)))
        out.append(write(f"02-models-card-states{d}.html", models_card_states(theme)))
        out.append(write(f"02-models-file-dialogs{d}.html", models_file_dialogs(theme)))
        out.append(write(f"02-models-empty-offline{d}.html", models_empty_offline(theme)))
    return out
