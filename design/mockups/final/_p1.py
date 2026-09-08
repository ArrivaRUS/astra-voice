# -*- coding: utf-8 -*-
"""Разделы «Общие» (01) и «Модели» (02) — полный макет A «Панель»."""
from _shell import (page, write, shell, head, footer, sb, m, MODELS, CATALOG_ORDER, DOMESTIC,
                    INSTALLED_SIZE, ic, tgl, key, btn, row, card, group, met, met2, level_bars,
                    stcell, sech, grid, W, H, th)

DD = 'var(--fg3)'


# ══════════════════════════════════════════════════════════════════════════
# 01 · ОБЩИЕ
# ══════════════════════════════════════════════════════════════════════════
def sel(text, w=None, open_=False, dis=False):
    st = f' style="width:{w}px"' if w else ""
    cls = "sel" + (" foc" if open_ else "")
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
                '<div><b>Комбинация занята в KDE</b>'
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
        row("Горячая клавиша", hotkey_field("idle"),
            "Удерживайте и говорите — текст появится там, где курсор"),
        row("Режим", seg("Удерживать", "Нажать-нажать", mode_first),
            "Удерживать — самый предсказуемый вариант"),
        row("Микрофон", sel("Системный по умолчанию", 236),
            "Sof-hda-dsp · встроенный микрофон ноутбука"),
    ])
    g2 = group("Индикация", [
        row("Индикатор записи", sel(indicator, 236),
            "Пилюля не забирает фокус и не появляется в Alt+Tab"),
        row("Звук начала и конца записи", tgl(False)),
        row("Значок в системном трее",
            f'<span style="display:flex;gap:9px;align-items:center">'
            f'<span class="c12">Выключить нельзя</span>{tgl(True)}</span>',
            "Запись всегда видна — это требование приватности (§9.5)", dis=True),
    ])
    if autostart == "error":
        ar = row("Автозапуск при входе в систему", tgl(True),
                 "Создаёт ~/.config/autostart/astra-voice.desktop")
        err = (f'<div class="r"><div class="note e" style="width:100%">{ic("alert", 15, "var(--err-ink)")}'
               '<div><b>Не удалось создать ярлык автозапуска</b>'
               'Папка <span class="mono">~/.config/autostart</span> недоступна для записи. '
               'Астра Voice запустится вручную командой <span class="mono">astra-voice</span>.'
               f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Открыть папку", "", "folder")}'
               f'{btn("Повторить")}</div></div></div></div>')
        g3 = group("Запуск", [ar, err])
    else:
        g3 = group("Запуск", [
            row("Автозапуск при входе в систему", tgl(autostart == "on"),
                "Создаёт ярлык ~/.config/autostart/astra-voice.desktop. Чужие ярлыки не трогаем"),
        ])
    return g1 + g2 + g3


def general_base(theme):
    body = general_rows()
    b = shell("Общие", head("Общие", "Диктовка, индикация и запуск"), body,
              footer(state="disabled"))
    leg = ("<b>Базовое состояние раздела.</b> Семь настроек помещаются без прокрутки; строка «Вторая "
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
        stcell("конфликт с KDE — предупреждение, не запрет", hotkey_field("conflict"), "conflict"),
        stcell("уже назначена на другой режим", hotkey_field("duplicate"), "duplicate"),
    ]) + grid([
        stcell("комбинацию не удалось захватить (X11 забрал другое приложение)",
               hotkey_field("not-grabbed"), "not-grabbed"),
    ], 1)
    body_full = b + sech("Поле захвата — все состояния") + gal
    leg = ("<b>Правило:</b> конфликт с KDE — это <b>предупреждение с именем чужого действия</b>, а не "
           "запрет (PRD S1-A4): пользователь вправе оставить комбинацию. «Не захвачена» — единственное "
           "красное состояние, потому что диктовка в нём не работает. Захват идёт по «сырым» нажатиям; "
           "Esc всегда отменяет и возвращает прежнюю комбинацию.")
    return page(f"A · Общие: захват комбинации — {th(theme)}",
                f'<b>Общие → Горячая клавиша</b> — захват комбинации и конфликт · {th(theme)} тема',
                body_full, theme, leg)


def mic_row_states(state):
    if state == "loading":
        return ('<div class="card">' + row("Микрофон", sel("Ищу устройства…", 236),
                                           "Опрашиваем PipeWire и ALSA", hint=False) + '</div>')
    if state == "list":
        pop = ('<div class="pop" style="position:static;box-shadow:none;margin-top:7px">'
               f'<div class="po on">{ic("check", 13, "var(--primary)")}<span>Системный по умолчанию'
               '<span class="d2">Сейчас: Встроенный микрофон (sof-hda-dsp)</span></span></div>'
               '<div class="po"><span style="width:13px"></span><span>Встроенный микрофон'
               '<span class="d2">alsa_input.pci-0000_00_1f.3 · 2 канала</span></span></div>'
               '<div class="po"><span style="width:13px"></span><span>Гарнитура Jabra Evolve2 30'
               '<span class="d2">alsa_input.usb-0b0e_Jabra · 1 канал</span></span></div>'
               '<div class="po"><span style="width:13px"></span><span>Микрофон монитора Dell U2723QE'
               '<span class="d2">alsa_input.usb-Dell_U2723QE · 2 канала</span></span></div>'
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
                  'Устройство занято другим приложением (код <span class="mono">EBUSY</span>). '
                  'Закройте приложение, которое пишет звук. Мы уже повторили попытку три раза.'
                  f'<div style="margin-top:9px">{btn("Повторить", "pri", "refresh")}</div></div></div>')
    if state == "silent":
        return ('<div class="card">' + row("Микрофон", sel("Встроенный микрофон", 236),
                                           hint=False) + '</div>'
                + f'<div class="note w" style="margin-top:9px">{ic("alert", 15, "var(--warn-ink)")}'
                  '<div><b>Микрофон молчит</b>'
                  'Устройство открыто, но звука нет. Обычно помогает перезапуск звуковой службы — '
                  'это безопасно и не требует пароля.'
                  f'<div style="display:flex;gap:8px;margin-top:9px;align-items:center">'
                  f'{btn("Перезапустить звуковую службу", "pri", "refresh")}{btn("Что это", "gh")}'
                  '<span class="c12">Проверим уровень сами через 3 с</span></div></div></div>')
    if state == "restarting":
        return (f'<div class="note i">{ic("refresh", 15, DD)}'
                '<div><b>Перезапускаю звуковую службу…</b>'
                '<span class="mono">systemctl --user restart wireplumber</span> · проверим уровень '
                'сразу после перезапуска</div></div>')
    if state == "no-service":
        return (f'<div class="note w">{ic("alert", 15, "var(--warn-ink)")}'
                '<div><b>Служба звука не отвечает</b>'
                'В системе нет <span class="mono">wireplumber</span> — перезапустить её отсюда нельзя. '
                'Обратитесь к администратору.'
                f'<div style="margin-top:9px">{btn("Перезапустить звуковую службу", "dis", "refresh")}'
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
        stcell("wireplumber не установлен — кнопка недоступна", mic_row_states("no-service"), "disabled"),
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
    pop = ('<div class="pop" style="right:22px;top:152px">'
           f'<div class="po on">{ic("check", 13, "var(--primary)")}<span>Пилюля снизу экрана'
           '<span class="d2">Как в Handy: у нижнего края, поверх окон</span></span></div>'
           '<div class="po"><span style="width:13px"></span><span>Пилюля сверху экрана'
           '<span class="d2">Если снизу мешает панель задач</span></span></div>'
           '<div class="po"><span style="width:13px"></span><span>Только значок в трее'
           '<span class="d2">Пилюли нет; запись видна по значку</span></span></div></div>')
    body = general_rows() + sb(6, 170)
    b = shell("Общие", head("Общие", "Индикация записи"), body, footer(state="disabled"), over=pop)
    auto_on = ('<div class="card">' + row("Автозапуск при входе в систему", tgl(True),
               "Ярлык ~/.config/autostart/astra-voice.desktop создан", hint=False) + '</div>')
    auto_off = ('<div class="card">' + row("Автозапуск при входе в систему", tgl(False),
                "Запускать вручную: команда astra-voice или меню приложений", hint=False) + '</div>')
    auto_err = (f'<div class="note e">{ic("alert", 15, "var(--err-ink)")}'
                '<div><b>Не удалось создать ярлык автозапуска</b>'
                'Папка <span class="mono">~/.config/autostart</span> недоступна для записи.'
                f'<div style="margin-top:9px">{btn("Открыть папку", "pri", "folder")}</div></div></div>')
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
BADGE_PRIORITY = ["act", "upd", "rec", "new"]


def badges_html(items):
    """Максимум два бейджа: Активна > Обновление доступно > Рекомендуем > Новое."""
    items = sorted(items, key=lambda x: BADGE_PRIORITY.index(x[1]))[:2]
    return "".join(f'<span class="bd {k}">{t}</span>' for t, k in items)


def mcard(mid, state=None, badges=None, note=None, acts=None, extra="", cls_extra=""):
    mo = m(mid)
    st = state or mo["status"]
    bl = badges if badges is not None else mo["badges"]
    cls = "mc" + (" act" if st in ("active", "switching") else "") + cls_extra
    mets = ('<div class="mmets">'
            + met2("Качество", mo["q"], mo["qv"], mo["measured"], mo["qkind"])
            + met2("Скорость", mo["s"], mo["sv"], mo["measured"], mo["skind"]) + '</div>')
    bh = badges_html(bl)
    top = ('<div class="mtop"><div class="ml">'
           f'<div class="mname">{mo["name"]} <span class="mven">· {mo["vendor"]}</span></div>'
           f'<div class="mpurp">{mo["purpose"]}</div>'
           + (f'<div class="mbadges">{bh}</div>' if bh else "")
           + f'</div>{mets}</div>')
    punct = "с пунктуацией" if mo["punct"] else "без пунктуации"
    ram = (f'<b style="color:var(--fg1)">{mo["ram"]} ОЗУ</b> · {mo["ramkind"]}' if mo["measured"]
           else f'{mo["ram"]} ОЗУ · {mo["ramkind"]}')
    tags = (f'{mo["lang"]} <span class="dot"></span> {punct} <span class="dot"></span> '
            f'{mo["lic"]} <span class="dot"></span> {mo["origin"]}')

    # ── состояния с собственным телом карточки ───────────────────────────
    if st == "downloading":
        body = ('<div class="mhr"></div>'
                '<div class="prog" style="margin-bottom:7px"><i style="width:43%"></i></div>'
                '<div class="mbot"><span class="num">Загрузка 43 % · 5,2 МБ/с · осталось ~25 с</span>'
                '<span class="dot"></span><span>huggingface.co</span>'
                f'<span class="sp"></span>{btn("Отмена", "sm")}</div>')
    elif st == "queued":
        body = ('<div class="mhr"></div><div class="mbot"><span>В очереди — начнём, когда докачается '
                'GigaAM v3 RNN-T без пунктуации</span>'
                f'<span class="sp"></span>{btn("Отмена", "sm")}</div>')
    elif st == "verifying":
        body = ('<div class="mhr"></div>'
                '<div class="prog" style="margin-bottom:7px"><i style="width:100%"></i></div>'
                '<div class="mbot"><span>Проверяю контрольную сумму… 4 файла</span>'
                f'<span class="sp"></span><span class="c12">Отмена недоступна</span></div>')
    elif st == "updating":
        body = ('<div class="mhr"></div>'
                '<div class="prog" style="margin-bottom:7px"><i style="width:66%"></i></div>'
                '<div class="mbot"><span>Проверяю новую ревизию: скачано → sha256 → пробное '
                'распознавание</span>'
                f'<span class="sp"></span>{btn("Отмена", "sm")}</div>')
    else:
        n = ""
        if note:
            kind, text = note
            colors = {"w": "var(--warn-ink)", "e": "var(--err-ink)", "i": "var(--fg3)"}
            icon = "alert" if kind in ("w", "e") else "info"
            n = (f'<span style="color:{colors[kind]};display:inline-flex;align-items:center;gap:5px">'
                 f'{ic(icon, 12, colors[kind])}{text}</span><span class="dot"></span>')
        default_acts = {
            "active": btn("Удалить", "sm dis"),
            "installed": btn("Выбрать", "sm pri") + " " + btn("Удалить", "sm"),
            "switching": btn("Удалить", "sm dis"),
            "avail": btn("Скачать", "sm pri") + " " + btn("Из файла…", "sm"),
            "new": btn("Скачать", "sm pri") + " " + btn("Из файла…", "sm"),
            "lowram": btn("Скачать", "sm") + " " + btn("Из файла…", "sm"),
            "nonet": btn("Скачать", "sm dis") + " " + btn("Установить из файла…", "sm"),
            "offline-user": btn("Скачать", "sm dis") + " " + btn("Из файла…", "sm"),
            "policy": btn("Скачать", "sm dis") + " " + btn("Из файла…", "sm"),
            "update": btn("Обновить", "sm pri") + " " + btn("Что нового", "sm"),
            "update-failed": btn("Повторить", "sm pri") + " " + btn("Подробнее", "sm"),
            "corrupted": btn("Переустановить", "sm pri") + " " + btn("Удалить", "sm"),
            "nospace": btn("Повторить", "sm pri") + " " + btn("Открыть папку", "sm"),
            "badsha": btn("Скачать заново", "sm pri") + " " + btn("Подробнее", "sm"),
            "removed": btn("Выбрать", "sm pri") + " " + btn("Удалить", "sm"),
            "custom": btn("Выбрать", "sm pri") + " " + btn("Удалить", "sm"),
            "notrec": btn("Скачать", "sm") + " " + btn("Из файла…", "sm"),
        }[st]
        a = acts if acts is not None else default_acts
        body = ('<div class="mhr"></div><div class="mbot">'
                f'{tags}<span class="sp"></span>{n}<span>{mo["disk"]} на диске</span>'
                f'<span class="dot"></span><span>{ram}</span> {a}</div>')
    return f'<div class="{cls}">{top}{body}{extra}</div>'


def filters(theme, domestic=False, count=None):
    if count is None:
        count = (f"Показано 6 из 12 · установлено 3 · {INSTALLED_SIZE} на диске" if domestic
                 else f"Установлено 3 из 12 · {INSTALLED_SIZE} на диске")
    return ('<div style="display:flex;gap:8px;align-items:center;margin:0 0 12px">'
            f'<span class="chip">{ic("globe", 13, DD)} Все языки {ic("chevd", 12, DD)}</span>'
            f'<span class="chip{" on" if domestic else ""}">Только отечественные {tgl(domestic)}</span>'
            f'<span style="flex:1"></span><span class="c12">{count}</span></div>')


def catalog_body(theme):
    s = filters(theme)
    s += '<div class="grp">Установленные · 3</div>'
    s += mcard("rnnt", "active", [("Активна", "act"), ("Рекомендуем", "rec")])
    s += mcard("ctc", "installed")
    s += mcard("rnnt-np", "downloading")
    s += '<div class="grp" style="margin-top:14px">Доступные · 9</div>'
    s += mcard("ml", "new", [("Новое", "new")])
    s += mcard("tone", "avail")
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
    return s


def catalog_domestic():
    """Состояние фильтра «только отечественные»: 5 GigaAM + T-one (по юрлицу правообладателя)."""
    s = filters(None, domestic=True)
    s += '<div class="grp">Установленные · 3</div>'
    s += mcard("rnnt", "active", [("Активна", "act"), ("Рекомендуем", "rec")])
    s += mcard("ctc", "installed")
    s += mcard("rnnt-np", "downloading")
    s += '<div class="grp" style="margin-top:14px">Доступные · 3</div>'
    s += mcard("ml", "new", [("Новое", "new")])
    s += mcard("tone", "avail")
    s += mcard("mllarge", "avail")
    s += ('<div class="note i" style="margin-top:4px">' + ic("info", 15, DD)
          + '<div><b>Скрыто 6 моделей</b>'
            'Whisper (OpenAI, США), NeMo FastConformer (NVIDIA, США) и обе Vosk '
            '(Alpha Cephei Inc., Делавэр — команда из России, но юрлицо зарубежное). '
            'Фильтр смотрит на правообладателя, а не на страну разработчиков.</div></div>')
    return s


def models_catalog(theme):
    right = btn("Установить из файла…", "sm", "folder")
    hd = head("Модели", "Честные цифры: размер на диске, память в работе, качество и скорость", right)
    b = shell("Модели", hd, catalog_body(theme) + sb(40, 150), footer(state="model-update"))
    full = ('<div class="win" style="width:900px;padding:0">'
            '<div style="background:var(--bg-app);padding:14px 22px 18px">'
            + catalog_body(theme) + '</div></div>')
    leg = ("<b>Решение G2: каталог показывается целиком с прокруткой</b> — без «показать ещё». Всего 12 "
           "карточек (10 Must + 2 Could, PRD §7.2); нужная стоит первой и помечена «Активна». "
           "<b>Все цифры — из <span class=\"mono\">research/catalog-numbers.md</span> (2026-09-08):</b> "
           "размер = сумма точных байт файлов рантайма из HF API в десятичных МБ; WER — Russian "
           "LibriSpeech, бенчмарк onnx-asr; скорость — столбец «x64 RTFx (int8)». Где int8-замера нет "
           "(T-one, обе GigaAM Multilingual) — полоска помечена «бенчмарк fp32». У Whisper small цифр "
           "по протоколу нет вовсе: полоски пунктиром и «нет данных», выдумывать нельзя (§7.1). "
           "<b>ОЗУ:</b> «415 МБ · замерено на этом компьютере» жирным против «~416 МБ · оценка» серым. "
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
        stcell("активная модель", mcard("rnnt", "active", [("Активна", "act"), ("Рекомендуем", "rec")]),
               "active"),
        stcell("установлена", mcard("ctc", "installed"), "installed"),
        stcell("скачивается", mcard("rnnt-np", "downloading"), "downloading"),
        stcell("в очереди", mcard("tone", "queued"), "queued"),
        stcell("проверка контрольной суммы", mcard("tone", "verifying"), "verifying"),
        stcell("переключение активной модели",
               mcard("ctc", "switching", [("Переключаю…", "act")]), "switching"),
        stcell("доступна для скачивания", mcard("tone", "avail"), "not-downloaded"),
        stcell("новая в каталоге (≤ 30 дней)", mcard("ml", "new", [("Новое", "new")]), "new"),
        stcell("доступно обновление ревизии",
               mcard("ctc", "update", [("Обновление доступно · ревизия от 04.09.2026", "upd")]),
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
        stcell("ошибка: sha256 не совпал",
               mcard("tone", "badsha",
                     note=("e", "Файл не прошёл проверку — загруженное удалено")), "error-sha"),
        stcell("ошибка: кончилось место на диске",
               mcard("wturbo", "nospace",
                     note=("e", "Пауза: нужно ещё 120 МБ, свободно 100 МБ")), "paused-no-space"),
        stcell("модель повреждена",
               mcard("ctc", "corrupted",
                     note=("e", "Файлы модели не читаются — переустановите")), "corrupted"),
        stcell("снята с каталога",
               mcard("vosk-s", "removed", note=("i", "Снята с каталога — обновлений не будет")),
               "removed-from-catalog"),
    ]
    rules = ('<div class="stc"><div class="stn">правило бейджей — максимум два</div>'
             '<div class="sm" style="line-height:1.7">Приоритет: <b>Активна</b> → <b>Обновление '
             'доступно</b> → <b>Рекомендуем</b> → <b>Новое</b>. Третий бейдж не рисуется: сообщение '
             'уходит текстом в нижнюю строку карточки. Поэтому «не рекомендуется для русского» и '
             '«мало ОЗУ» — это подписи с иконкой, а не бейджи.'
             '<div style="display:flex;gap:6px;margin-top:9px;flex-wrap:wrap">'
             + badges_html([("Активна", "act"), ("Рекомендуем", "rec"), ("Новое", "new")])
             + '<span class="c12" style="align-self:center">← из трёх остались два</span></div></div></div>')
    tip = ('<div class="stc"><div class="stn">подсказка при наведении на полоску</div>'
           + tooltip_bars() + '</div>')
    leg = ("<b>20 состояний карточки</b> (flows.md §3.3) — каждое различимо не только цветом: у ошибок "
           "треугольник и красная подпись, у предупреждений — треугольник и жёлтая, у нейтральных пояснений "
           "— «i». Кнопка «Удалить» у активной модели <b>недоступна</b>: сначала выберите другую. "
           "Скачивание всегда показывает <b>источник</b> (huggingface.co) — это важно для ИБ.")
    return page(f"A · Модели: состояния карточки — {th(theme)}",
                f'<b>Модели → карточка</b> — все 20 состояний · {th(theme)} тема',
                grid(cells, 1) + sech("Правила") + grid([rules, tip]), theme, leg)


def models_file_dialogs(theme):
    picker = ('<div class="dlg" style="width:460px"><div class="dh">'
              + ic("folder", 14, DD) + 'Выбор папки с моделью</div>'
              '<div class="db" style="flex-direction:column;gap:10px">'
              '<div class="sm">Укажите папку с файлами модели (<span class="mono">*.onnx</span> и '
              '<span class="mono">tokens.txt</span>) или архив, полученный от администратора.</div>'
              '<div class="field mono" style="font-size:12px">/media/usb/models/gigaam-v3-e2e-rnnt-int8'
              f'<span style="flex:1"></span>{ic("folder", 13, DD)}</div>'
              '<div class="c12">Найдено 4 файла · 231,9 МБ · encoder.int8.onnx, decoder.onnx, joiner.onnx, '
              'tokens.txt</div></div>'
              f'<div class="df"><span class="sp"></span>{btn("Отмена")}{btn("Проверить и установить", "pri")}'
              '</div></div>')
    ok = (f'<div class="note o">{ic("check", 15, "var(--ok-ink)")}'
          '<div><b>Модель установлена</b>'
          'GigaAM v3 RNN-T · 231,9 МБ · контрольные суммы совпали с манифестом каталога. '
          'Ревизия <span class="mono">a6039be</span> от 16.12.2025.'
          f'<div style="margin-top:9px">{btn("Выбрать активной", "pri")}</div></div></div>')
    bad = (f'<div class="note e">{ic("alert", 15, "var(--err-ink)")}'
           '<div><b>Файл не прошёл проверку</b>'
           'Контрольная сумма <span class="mono">encoder.int8.onnx</span> не совпала с манифестом: '
           'файл повреждён или получен не из нашего каталога. Мы ничего не устанавливали.'
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
                  'Не найден <span class="mono">tokens.txt</span>. Нужны все файлы модели — обычно они '
                  'лежат рядом в одной папке.'
                  f'<div style="margin-top:9px">{btn("Выбрать другую папку", "pri")}</div></div></div>')
    delete = ('<div class="dlg"><div class="dh">' + ic("trash", 14, DD) + 'Удалить модель</div>'
              f'<div class="db">{ic("trash", 22, "var(--err-ink)")}'
              '<div><div class="h3">Удалить GigaAM v3 CTC?</div>'
              '<div class="sm" style="margin-top:6px">С диска будет удалено <b>224,9 МБ</b> из '
              '<span class="mono">~/.local/share/astra-voice/models/</span>. Настройки и статистика '
              'останутся. Скачать модель заново можно в любой момент.</div></div></div>'
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
              f'{btn("Установить из файла…", "pri", "folder")}{btn("Повторить", "", "refresh")}'
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
           "пустого списка — встроенный манифест показывается всегда, у карточек неактивна только кнопка "
           "«Скачать», а «Установить из файла…» работает. В сообщении нет слова «VPN» (запрет F14.5) — "
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
