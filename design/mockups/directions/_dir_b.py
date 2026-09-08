# -*- coding: utf-8 -*-
"""Направление B «Лента» — одноколоночное окно карточек-групп без сайдбара.
Окно 900×620. Воздуха больше, подсказки видны текстом (а не в тултипах), крупнее шрифт.
"""
from _base import (page, write, mark, logo, appicon, tray, ic, tgl, key, bd, btn, card,
                   met, foot, foot_left, pill, pill_gallery, m)

W, H = 900, 620

CSS_B = """
.hdr{height:56px;flex:none;display:flex;align-items:center;gap:12px;padding:0 20px;
  background:var(--bg-app);border-bottom:1px solid var(--border-soft)}
.hdr .sp{flex:1}
.srch{width:210px;background:var(--bg-sunk);border:1px solid transparent;border-radius:8px;
  padding:7px 11px;font-size:13px;color:var(--fg3);display:flex;align-items:center;gap:8px}
.nav{height:46px;flex:none;display:flex;align-items:center;gap:8px;padding:0 20px;
  border-bottom:1px solid var(--border);background:var(--bg-app)}
.feed{flex:1;padding:20px 0 24px;display:flex;justify-content:center}
.col{width:620px}
.sect{margin-bottom:22px}
.sect>.h2{margin-bottom:3px}
.sect>.sm{margin-bottom:11px}
.card{border-radius:12px}
.r{padding:14px 16px;min-height:52px}
.r .lbl{font-size:15px;font-weight:500}
.r .sub{font-size:13px;line-height:1.45;margin-top:3px;max-width:380px}
.hero{background:var(--bg-surface);border:1px solid var(--border);border-radius:12px;
  padding:18px 20px;margin-bottom:22px}
.hero .st{display:flex;align-items:center;gap:11px}
.hero .stn{font-size:20px;font-weight:700;color:var(--fg1)}
.lvl{display:flex;align-items:flex-end;gap:4px;height:22px}
.lvl i{width:5px;border-radius:3px;background:var(--fg4);display:block}
.mgrid{display:flex;gap:18px;margin:12px 0 2px}
.mg{flex:1}
.mg .mgn{font-size:11.5px;letter-spacing:.06em;text-transform:uppercase;color:var(--fg3);margin-bottom:5px}
.mg .mgv{font-size:13px;color:var(--fg1);margin-top:5px}
.mg .mgv b{font-weight:700}
.mg .trk{width:100%;display:block}
.mc{padding:16px 18px;border-radius:12px;margin-bottom:12px}
.mname{font-size:17px}
.mpurp{font-size:13px;margin-top:3px}
.foot{height:40px}
.menu{width:296px;padding:7px}
.mi{padding:9px 11px;font-size:13.5px;border-radius:7px}
.pill.b{height:44px;border-radius:22px;font-size:13.5px;padding:0 15px;gap:12px}
.pill.b .lv{height:24px;gap:4px}
.pill.b .lv i{width:5px}
.pill.b .x{width:26px;height:26px;font-size:14px}
.obody{flex:1;display:flex;flex-direction:column;align-items:center;justify-content:flex-start;padding:34px 40px 0}
.obar{height:64px;flex:none;border-top:1px solid var(--border-soft);display:flex;align-items:center;
  gap:10px;padding:0 24px;background:var(--bg-app)}
.sd{width:7px;height:7px;border-radius:50%;background:var(--border)}
.sd.on{background:var(--primary);width:22px;border-radius:4px}
.sd.dn{background:var(--fg4)}
.upan{background:var(--bg-surface);border:1.5px solid var(--primary);border-radius:12px;padding:18px 20px}
.ul{margin:8px 0 0;padding-left:18px;font-size:13px;color:var(--fg2);line-height:1.65}
"""

CHIPS = ["Общие", "Модели", "Вывод", "Сеть и обновления", "Ещё"]


def brow(label, control, sub=None, dis=False, lock=False):
    left = '<div class="left"><div class="lbl">%s</div>%s</div>' % (
        label, ('<div class="sub">%s</div>' % sub) if sub else "")
    lk = ('<span class="lockb">%s Задано администратором</span>' % ic("lock", 11)) if lock else ""
    return '<div class="r%s">%s%s%s</div>' % (" dis" if dis else "", left, lk, control)


def sect(h2, sub, inner):
    return '<div class="sect"><div class="h2">%s</div><div class="sm">%s</div>%s</div>' % (h2, sub, inner)


def shell(theme, active, inner, footer, title="Astra Voice"):
    tb = ('<div class="tbar">%s<span class="t">%s</span><span class="sp"></span>'
          '<span class="wbtn"></span><span class="wbtn"></span><span class="wbtn c"></span></div>'
          % (mark(16), title))
    hdr = ('<div class="hdr">%s<span class="sp"></span>'
           '<span class="srch">%s Поиск по настройкам</span>%s</div>'
           % (logo(24, 16), ic("search", 14, "var(--fg3)"), btn("", "sm", "sliders")))
    chips = "".join('<span class="chip%s">%s</span>' % (" on" if c == active else "", c) for c in CHIPS)
    nav = '<div class="nav">%s</div>' % chips
    return ('<div class="win" style="width:%dpx;height:%dpx">%s%s%s'
            '<div class="feed scr"><div class="col">%s</div>'
            '<div class="sb" style="top:12px;height:170px"></div></div>%s</div>'
            % (W, H, tb, hdr, nav, inner, footer))


def hero():
    bars = "".join('<i style="height:%dpx"></i>' % h for h in (4, 4, 4, 4, 4, 4, 4, 4, 4))
    return ('<div class="hero"><div class="st">%s<span class="stn">Готов</span>'
            '<span style="flex:1"></span>%s</div>'
            '<div class="sm" style="margin:8px 0 0;font-size:14px">Зажмите %s и говорите — '
            'текст появится там, где стоит курсор.</div>'
            '<div style="display:flex;align-items:center;gap:14px;margin-top:14px">'
            '<span class="lvl">%s</span><span class="c12">Микрофон: системный по умолчанию · тишина</span></div>'
            '</div>' % (mark(30), btn("Проверить микрофон", "sm"), key("Ctrl + Space"), bars))


# ── 1. Общие ───────────────────────────────────────────────────────────────
def general(theme):
    s1 = sect("Диктовка", "Как запускается запись и с какого устройства", card([
        brow("Горячая клавиша", '<span style="display:flex;gap:9px;align-items:center">%s%s</span>'
             % (key("Ctrl + Space"), btn("Изменить", "sm")),
             "Удерживайте и говорите. Проверяем конфликты с горячими клавишами KDE."),
        brow("Режим", '<span class="seg"><span class="on">Удерживать</span><span>Нажать-нажать</span></span>',
             "«Нажать-нажать» удобно для длинных диктовок — назначьте для него отдельную комбинацию."),
        brow("Микрофон", '<span class="sel">Системный по умолчанию %s</span>' % ic("chevd", 13, "var(--fg3)"),
             "Устройство открывается только на время записи."),
    ]))
    s2 = sect("Индикация", "Как программа показывает, что слышит вас", card([
        brow("Индикатор записи", '<span class="sel">Пилюля снизу экрана %s</span>' % ic("chevd", 13, "var(--fg3)"),
             "Небольшая полоска у нижнего края экрана. Не забирает фокус у активного окна."),
        brow("Звук начала и конца", tgl(False),
             "Короткий сигнал вместо визуального индикатора — если пилюля мешает."),
        brow("Значок в системном трее", tgl(True),
             "Выключить нельзя: запись всегда должна быть видна.", dis=True),
    ]))
    s3 = sect("Запуск", "Что происходит при входе в систему", card([
        brow("Автозапуск", tgl(True),
             "Создаёт ярлык ~/.config/autostart/astra-voice.desktop. Программа стартует свёрнутой в трей, "
             "микрофон не открывается до нажатия горячей клавиши."),
    ]))
    f = foot(foot_left("GigaAM v3 RNN-T", "idle"),
             '<span class="muted">Проверка обновлений отключена</span>' if theme == "light"
             else '<span class="muted">Источник обновлений недоступен · Обновить из файла…</span>')
    b = shell(theme, "Общие", hero() + s1 + s2 + s3, f)
    cap = ('<b>B · Лента</b> — главный экран · %s тема · окно 900×620'
           % ("светлая" if theme == "light" else "тёмная"))
    leg = ("<b>Отличие направления:</b> сайдбара нет — разделы идут одной лентой, переключатель сверху "
           "прокручивает к разделу. Подсказки видны текстом под каждой строкой (а не прячутся в «?»), "
           "шрифт крупнее, воздух больше. Сверху — блок состояния: «Готов», горячая клавиша, уровень микрофона. "
           "<b>Состояния:</b> disabled (значок в трее), тумблеры по умолчанию выключены, строка внизу — "
           "«Проверка обновлений отключена» / «Источник обновлений недоступен».")
    return page("B · Лента — Общие", cap, b, theme, CSS_B, leg)


# ── 2. Модели ──────────────────────────────────────────────────────────────
def mcard(mo, state=None):
    st = state or mo["status"]
    cls = "mc" + (" act" if st == "active" else "")
    badges = "".join('<span class="bd %s">%s</span>' % (k, t) for t, k in mo["badges"])
    if st == "downloading":
        badges = '<span class="bd new">Загрузка</span>'
    ramv = ('<b>%s</b> замерено' % mo["ram"]) if mo["measured"] else ('%s оценка' % mo["ram"])
    grid = ('<div class="mgrid">'
            '<div class="mg"><div class="mgn">Качество</div>'
            '<span class="trk%s"><i style="width:%d%%"></i></span><div class="mgv">%s</div></div>'
            '<div class="mg"><div class="mgn">Скорость</div>'
            '<span class="trk%s"><i style="width:%d%%"></i></span><div class="mgv">%s</div></div>'
            '<div class="mg"><div class="mgn">Память в работе</div>'
            '<div class="mgv" style="margin-top:0;font-size:15px">%s</div>'
            '<div class="c12" style="margin-top:2px">на диске %s</div></div></div>'
            % ("" if mo["measured"] else " est", mo["q"], mo["qv"],
               "" if mo["measured"] else " est", mo["s"], mo["sv"], ramv, mo["disk"]))
    punct = "с пунктуацией" if mo["punct"] else "без пунктуации"
    tags = ('%s <span class="dot"></span> %s <span class="dot"></span> %s'
            % (mo["lang"], punct, mo["lic"]))
    if st == "downloading":
        bottom = ('<div class="prog" style="margin:12px 0 8px"><i style="width:43%%"></i></div>'
                  '<div class="mbot"><span>Загрузка 43 %% · 5,2 МБ/с · осталось ~25 с · huggingface.co</span>'
                  '<span class="sp"></span>%s</div>' % btn("Отмена", "sm"))
    else:
        acts = {"active": btn("Удалить", "sm dis"),
                "installed": btn("Выбрать", "sm pri") + " " + btn("Удалить", "sm"),
                "avail": btn("Скачать", "sm pri") + " " + btn("Из файла…", "sm"),
                "nonet": btn("Скачать", "sm dis") + " " + btn("Установить из файла…", "sm"),
                "update": btn("Обновить", "sm pri") + " " + btn("Пропустить ревизию", "sm")}[st]
        pre = ""
        if st == "nonet":
            pre = '<span class="muted">Нет доступа к huggingface.co</span><span class="dot"></span>'
        bottom = ('<div class="mhr"></div><div class="mbot">%s<span class="sp"></span>%s%s</div>'
                  % (tags, pre, acts))
    return ('<div class="%s"><div class="mtop"><div class="ml">'
            '<div class="mname">%s <span class="mven">· %s</span></div>'
            '<div class="mpurp">%s</div></div>%s</div>%s%s</div>'
            % (cls, mo["name"], mo["vendor"], mo["purpose"],
               ('<div style="display:flex;gap:6px;flex:none">%s</div>' % badges) if badges else "",
               grid, bottom))


def models(theme):
    head = ('<div style="display:flex;align-items:flex-end;gap:12px;margin-bottom:14px">'
            '<div><div class="h2">Модели</div>'
            '<div class="sm" style="margin-top:3px">Установлено 3 из 12 · 687 МБ на диске</div></div>'
            '<span style="flex:1"></span>%s</div>' % btn("Установить из файла…", "sm", "folder"))
    chips = ('<div style="display:flex;gap:8px;align-items:center;margin-bottom:16px">'
             '<span class="chip">%s Все языки %s</span><span class="chip">Только отечественные %s</span></div>'
             % (ic("globe", 13, "var(--fg3)"), ic("chevd", 12, "var(--fg3)"), tgl(False)))
    s1 = ('<div class="grp">Установленные</div>' + mcard(m("rnnt"), "active")
          + mcard(m("rnnt-np"), "downloading"))
    s2 = ('<div class="grp" style="margin-top:18px">Доступные</div>'
          + mcard(m("tone"), "nonet" if theme == "dark" else "avail"))
    f = foot(foot_left("GigaAM v3 RNN-T", "idle"),
             '<span class="muted">Обновление модели: GigaAM v3 RNN-T</span>')
    b = shell(theme, "Модели", head + chips + s1 + s2, f)
    cap = ('<b>B · Лента</b> — каталог моделей · %s тема · окно 900×620'
           % ("светлая" if theme == "light" else "тёмная"))
    leg = ("<b>Отличие направления:</b> метрики вынесены в три равные колонки — «Качество», «Скорость» и "
           "<b>«Память в работе»</b> (наша добавка к паттерну Handy, ни у кого из конкурентов нет). "
           "Цифра всегда рядом с полоской, поэтому она читается без наведения. "
           "<b>Состояния:</b> активна · скачивается 43 % · доступна"
           + (" · нет сети (кнопка «Скачать» disabled)." if theme == "dark" else "."))
    return page("B · Лента — Модели", cap, b, theme, CSS_B, leg)


# ── 3. Обновления ──────────────────────────────────────────────────────────
def updates(admin=False):
    rows = card([
        brow("Проверять обновления утилиты", tgl(not admin, lock=admin),
             "Раз в сутки к github.com. Ничего не устанавливается само — только по вашей кнопке.", lock=admin),
        brow("Проверять обновления моделей", tgl(not admin, lock=admin),
             "Раз в сутки к huggingface.co: новая ревизия вашей модели и новые модели каталога.", lock=admin),
        brow("Офлайн-режим", tgl(admin, lock=admin),
             "Полностью запрещает сетевые запросы. Модели можно ставить из файла.", lock=admin),
    ])
    manual = ('<div style="display:flex;gap:9px;margin-top:12px">%s%s</div>'
              % (btn("Проверить сейчас", "sm dis" if admin else "sm", "refresh"),
                 btn("Обновить из файла…", "sm", "file")))
    if admin:
        panel = ('<div class="upan" style="border-color:var(--border)">'
                 '<div style="display:flex;align-items:center;gap:10px">%s<div class="h3">Доступна версия 0.2.1</div>'
                 '<span class="bd upd">Устанавливает администратор</span></div>'
                 '<div class="sm" style="margin-top:8px;font-size:13.5px">На этом компьютере включена '
                 'замкнутая программная среда (<span class="mono">DIGSIG_ELF_MODE=1</span>). '
                 'Мы скачаем пакет, контрольные суммы, подпись и инструкцию — передайте их администратору.</div>'
                 '<ul class="ul"><li>astra-voice_0.2.1_amd64.deb — 74,3 МБ</li>'
                 '<li>SHA256SUMS + SHA256SUMS.minisig — подпись проверена нашим ключом</li>'
                 '<li>INSTALL-ADMIN.md — порядок установки и подписи для ЗПС</li></ul>'
                 '<div style="display:flex;gap:9px;margin-top:14px">%s%s</div></div>'
                 % (ic("shield", 20, "var(--warn-ink)"),
                    btn("Скачать пакет для администратора", "pri", "down"), btn("Открыть папку", "", "folder")))
        f = foot(foot_left("GigaAM v3 RNN-T", "idle"),
                 '<span class="up">%s Доступна 0.2.1 · Скачать для администратора</span>'
                 % ic("shield", 13, "var(--primary)"))
        top = ('<div class="note w" style="margin-bottom:18px">%s<div><b>Профиль «защищённый контур»</b>'
               'Настройки заданы администратором в <span class="mono">/etc/astra-voice/policy.conf</span>: '
               'офлайн-режим, корпоративный каталог моделей, установка только администратором.</div></div>'
               % ic("lock", 16, "var(--warn-ink)"))
    else:
        panel = ('<div class="upan"><div style="display:flex;align-items:center;gap:10px">'
                 '<div class="h3">Доступна версия 0.2.1</div><span class="bd rec">Новая</span>'
                 '<span style="flex:1"></span><span class="c12">7 сентября 2026 · 74,3 МБ</span></div>'
                 '<div class="sm" style="margin-top:9px;color:var(--fg2);font-size:13.5px"><b>Что нового</b></div>'
                 '<ul class="ul"><li>Каталог моделей: 10 моделей, память в работе замеряется на вашем компьютере</li>'
                 '<li>Автозапуск при входе в систему</li>'
                 '<li>Исправлено: пилюля перекрывалась панелью при смене раскладки</li></ul>'
                 '<div style="display:flex;gap:9px;margin-top:14px;align-items:center">%s%s'
                 '<span style="flex:1"></span><span class="c12">Подпись релиза проверяется до установки</span>'
                 '</div></div>'
                 % (btn("Скачать и установить", "pri", "down"), btn("Пропустить эту версию", "")))
        f = foot(foot_left("GigaAM v3 RNN-T", "idle"),
                 '<span class="up">Доступна версия 0.2.1 · Установить</span>')
        top = ""
    inner = top + panel + '<div style="height:22px"></div>' + sect(
        "Сеть и обновления",
        "Программа выходит в сеть только для четырёх действий — и ни для чего больше",
        rows + manual)
    b = shell("light", "Сеть и обновления", inner, f)
    if admin:
        cap = '<b>B · Лента</b> — обновления, ветка «устанавливает администратор» (ЗПС / нет прав) · 900×620'
        leg = ("<b>Трек B (PRD F9):</b> кнопки «Установить» нет; тумблеры заблокированы политикой "
               "администратора; строка внизу — «Скачать для администратора». Тот же экран при отсутствии прав "
               "и при <span class=\"mono\">policy.conf: updates=admin</span>.")
    else:
        cap = '<b>B · Лента</b> — обновления, трек без ЗПС + строка внизу «Доступна версия» · 900×620'
        leg = ("<b>Трек A (PRD F9):</b> строка внизу справа — единственное цветовое пятно; панель «Что нового» "
               "живёт в ленте, а не в модалке. Установка — только по явному действию, дальше одно окно polkit.")
    return page("B · Лента — Обновления", cap, b, "light", CSS_B, leg)


# ── 4. Онбординг ───────────────────────────────────────────────────────────
def onb_shell(step, body, actions):
    dots = "".join('<span class="sd %s"></span>' % ("on" if i == step else ("dn" if i < step else ""))
                   for i in range(1, 6))
    tb = ('<div class="tbar">%s<span class="t">Astra Voice — первый запуск</span><span class="sp"></span>'
          '<span class="wbtn"></span><span class="wbtn"></span><span class="wbtn c"></span></div>' % mark(16))
    hd = ('<div style="display:flex;justify-content:center;align-items:center;gap:8px;padding:16px 0 0">%s</div>'
          % dots)
    return ('<div class="win" style="width:%dpx;height:%dpx">%s%s<div class="obody">%s</div>'
            '<div class="obar">%s</div></div>' % (W, H, tb, hd, body, actions))


def onb_network():
    body = ('<div style="width:560px;text-align:center">%s'
            '<div class="h1" style="margin-top:14px">Astra Voice</div>'
            '<div class="sm" style="font-size:15px;margin:8px 0 22px">Голосовой ввод для Astra Linux. '
            'Распознавание идёт на этом компьютере: записи и текст никуда не отправляются.</div></div>'
            '<div style="width:560px;text-align:left">%s'
            '<div class="note i" style="margin-top:14px;font-size:13px">%s<div>'
            '<b>Пока оба переключателя выключены, программа не выходит в сеть.</b>'
            'Скачать модель на следующем шаге можно и без них — это ваше явное действие. '
            'Полный список хостов — в «О программе → Приватность».</div></div></div>'
            % (appicon(64),
               card([
                   brow("Язык интерфейса", '<span class="sel">Русский %s</span>' % ic("chevd", 13, "var(--fg3)"),
                        "Определён по системной локали ru_RU."),
                   brow("Проверять обновления утилиты", tgl(False),
                        "Раз в сутки к github.com. Ничего не устанавливается само."),
                   brow("Проверять обновления моделей", tgl(False),
                        "Раз в сутки к huggingface.co — новая ревизия вашей модели."),
               ]), ic("info", 16, "var(--fg3)")))
    actions = ('<span class="c12">Изменить можно позже в разделе «Сеть и обновления»</span>'
               '<span style="flex:1"></span>%s%s' % (btn("Пропустить", "gh"), btn("Продолжить", "pri")))
    leg = ("Оба тумблера <b>пусты</b> — решение заказчика 2026-09-07, действует во всех профилях. "
           "В направлении B онбординг такой же воздушный, как основное окно: крупная иконка, один вопрос "
           "на экран, объяснение видно текстом.")
    return page("B · Лента — Онбординг 1",
                '<b>B · Лента</b> — онбординг 1/5: приветствие и сеть · 900×620',
                onb_shell(1, body, actions), "light", CSS_B, leg)


def onb_hotkey():
    body = ('<div style="width:560px;text-align:center">'
            '<div class="h1" style="font-size:26px">Горячая клавиша</div>'
            '<div class="sm" style="font-size:15px;margin:8px 0 22px">Зажмите её и говорите. Отпустили — '
            'текст появится там, где стоит курсор.</div></div>'
            '<div style="width:560px;text-align:left">%s'
            '<div class="grp" style="margin:18px 0 8px">Новая комбинация</div>'
            '<div class="field cap" style="padding:11px 14px">'
            '<span class="mono" style="font-size:15px;color:var(--fg1)">Alt + F2</span>'
            '<span style="flex:1"></span><span class="c12">Нажмите комбинацию · Esc — отмена</span></div>'
            '<div class="note w" style="margin-top:11px;font-size:13px">%s<div>'
            '<b>Комбинация занята в KDE</b>Alt + F2 назначена на действие «Показать KRunner». '
            'Оставить можно, но диктовка может не сработать — система заберёт нажатие себе.</div></div>'
            '<div style="display:flex;gap:9px;margin-top:12px">%s%s</div></div>'
            % (card([
                brow("Текущая комбинация", '<span style="display:flex;gap:9px;align-items:center">%s%s</span>'
                     % (key("Ctrl + Space"), btn("Изменить", "sm")),
                     "По умолчанию — как в Handy, которым вы пользовались."),
                brow("Режим", '<span class="seg"><span class="on">Удерживать</span><span>Нажать-нажать</span></span>',
                     "«Удерживать» — самый предсказуемый вариант: запись идёт ровно пока клавиша зажата."),
            ]), ic("alert", 16, "var(--warn-ink)"),
                btn("Оставить Alt + F2", ""), btn("Выбрать другую", "pri")))
    actions = ('%s<span style="flex:1"></span>%s%s'
               % (btn("Назад", ""), btn("Пропустить", "gh"), btn("Продолжить", "pri")))
    leg = ("<b>Состояния поля захвата:</b> покой · захват (рамка акцентом, «Нажмите комбинацию · Esc — отмена») · "
           "конфликт с KDE — предупреждение с именем системного действия, <b>не запрет</b> (PRD S1-A4).")
    return page("B · Лента — Онбординг 3",
                '<b>B · Лента</b> — онбординг 3/5: горячая клавиша и захват · 900×620',
                onb_shell(3, body, actions), "light", CSS_B, leg)


# ── 5. Пилюля ──────────────────────────────────────────────────────────────
def pills():
    scene = ('<div class="desk" style="width:900px;height:420px">'
             '<div class="dwin" style="left:70px;top:34px;width:520px;height:270px">'
             '<div class="dt">Письмо — без имени — Kate</div>'
             '<div class="dc">Добрый день, Сергей Петрович!<br><br>По итогам совещания направляю сводку: '
             'сроки по первому этапу сдвигаются на неделю, смета остаётся прежней. Прошу подтвердить'
             '<span style="border-left:1.5px solid #1B3A73;margin-left:1px">&nbsp;</span></div></div>'
             '<div style="position:absolute;left:50%%;transform:translateX(-50%%);bottom:58px">%s</div>'
             '<div class="panel"><span class="pb">Kate</span><span class="pb">Firefox</span>'
             '<span class="sp"></span><span style="display:inline-flex;align-items:center;gap:9px;'
             'padding-right:6px">%s%s</span><span class="pb mono">14:26</span></div></div>'
             % (pill("listening", None, "b"), tray("listening", 17, "#C4CDDC"), tray("idle", 17, "#8C97AC")))
    body = (scene + '<div class="cap" style="margin-top:6px">Все состояния пилюли (F3.2)</div>'
            + pill_gallery("b"))
    leg = ("<b>Отличие направления:</b> пилюля выше (44 px, r=22) и всегда с подписью — та же логика, что и "
           "в окне: объяснение видно, а не спрятано. Состояние передаётся <b>формой и текстом</b>, не только "
           "цветом. Бирюза #12B3A0 — только «слушаю», красный — только ошибка. Оверлей не забирает фокус.")
    return page("B · Лента — Пилюля", '<b>B · Лента</b> — пилюля-оверлей на рабочем столе + все состояния',
                body, "light", CSS_B, leg)


# ── 6. Трей ────────────────────────────────────────────────────────────────
def menu(rec=False):
    if rec:
        hd = ('<div class="mi hd" style="color:var(--accent-ink)">%s Слушаю… · GigaAM v3 RNN-T</div>'
              % tray("listening", 16, "var(--accent)"))
        cancel = '<div class="mi hl">Отмена записи</div>'
        model = '<div class="mi dis">Модель<span class="sp"></span><span class="c12">GigaAM v3 RNN-T</span></div>'
        copy = '<div class="mi dis">Скопировать последний текст</div>'
    else:
        hd = '<div class="mi hd">%s Готов · Ctrl + Space</div>' % tray("idle", 16, "var(--fg3)")
        cancel = '<div class="mi dis">Отмена записи</div>'
        model = ('<div class="mi">Модель<span class="sp"></span>'
                 '<span class="c12">GigaAM v3 RNN-T</span>%s</div>' % ic("chev", 13, "var(--fg3)"))
        copy = '<div class="mi">Скопировать последний текст</div>'
    return ('<div class="menu">%s<div class="msep"></div>%s%s%s<div class="msep"></div>'
            '<div class="mi">Настройки…<span class="sp"></span><span class="sc">Ctrl+,</span></div>'
            '<div class="mi dis">Проверить обновления</div><div class="mi">О программе</div>'
            '<div class="msep"></div>'
            '<div class="mi">Выход<span class="sp"></span><span class="sc">Ctrl+Q</span></div></div>'
            % (hd, cancel, model, copy))


def trays():
    icons = "".join('<div class="cell">%s<span class="cn">%s</span></div>'
                    % ('<span style="background:#0B1220;border-radius:6px;padding:7px;display:inline-flex">%s</span>'
                       % tray(s, 20, "#C4CDDC"), n)
                    for s, n in [("idle", "готов"), ("listening", "слушаю"), ("processing", "распознаю"),
                                 ("done", "готово"), ("error", "ошибка")])
    scene = ('<div class="desk" style="width:900px;height:406px">'
             '<div style="position:absolute;right:16px;bottom:52px">%s</div>'
             '<div class="panel"><span class="pb">Kate</span><span class="sp"></span>'
             '<span style="display:inline-flex;align-items:center;gap:9px;padding-right:6px">%s%s</span>'
             '<span class="pb mono">14:26</span></div></div>' % (menu(False), tray("idle", 17, "#F2F5FA"),
                                                                 tray("idle", 17, "#8C97AC")))
    two = ('<div style="display:flex;gap:26px;align-items:flex-start">'
           '<div><div class="cap" style="margin-bottom:7px">В покое</div>%s</div>'
           '<div><div class="cap" style="margin-bottom:7px">Во время записи</div>%s</div>'
           '<div class="gal" style="align-self:flex-start">%s</div></div>' % (menu(False), menu(True), icons))
    leg = ("<b>Трей (PRD F12).</b> Строка статуса в шапке меню — отличие направления B: состояние и активная "
           "модель видны сразу, без наведения на значок. Значок обязателен всегда, даже с выключенной пилюлей "
           "(запрет скрытой записи, §9.5).")
    return page("B · Лента — Трей", '<b>B · Лента</b> — значок и меню в системном трее',
                scene + two, "light", CSS_B, leg)


def build():
    return [write("B/01-general.html", general("light")),
            write("B/01-general-dark.html", general("dark")),
            write("B/02-models.html", models("light")),
            write("B/02-models-dark.html", models("dark")),
            write("B/03-updates.html", updates(False)),
            write("B/03-updates-admin.html", updates(True)),
            write("B/04-onboarding-network.html", onb_network()),
            write("B/04-onboarding-hotkey.html", onb_hotkey()),
            write("B/05-pill.html", pills()),
            write("B/06-tray.html", trays())]
