# -*- coding: utf-8 -*-
"""Направление A «Панель» — классическое окно настроек с левым сайдбаром.
Окно 900×620: титул 32 + тело 552 + строка внизу 36. Плотно, как KDE System Settings.
"""
from _base import (page, write, mark, logo, appicon, tray, ic, tgl, key, bd, btn, row, card,
                   group, met, foot, foot_left, pill, pill_gallery, m, MODELS)

W, H = 900, 620
BODY_H = H - 32 - 36  # 552

CSS_A = """
.side{width:184px;flex:none;background:var(--bg-sunk);border-right:1px solid var(--border);
  display:flex;flex-direction:column;padding:12px 10px}
.slogo{display:flex;align-items:center;gap:8px;padding:4px 6px 12px;margin-bottom:6px;
  border-bottom:1px solid var(--border)}
.nav{display:flex;flex-direction:column;gap:2px;margin-top:8px}
.nv{display:flex;align-items:center;gap:9px;padding:7px 9px;border-radius:7px;font-size:13.5px;
  font-weight:500;color:var(--fg2)}
.nv.on{background:var(--primary);color:var(--primary-fg)}
.nv.sub{font-weight:400;color:var(--fg3);font-size:13px}
.cont{flex:1;min-width:0;display:flex;flex-direction:column}
.chead{padding:13px 22px 9px}
.r{padding:10px 14px;min-height:42px}
.cbody{flex:1;padding:0 22px 16px}
.wrap{display:flex;flex:1;min-height:0}
.steps{display:flex;align-items:center;gap:7px}
.sd{width:7px;height:7px;border-radius:50%;background:var(--border)}
.sd.on{background:var(--primary);width:20px;border-radius:4px}
.sd.dn{background:var(--fg4)}
.obody{flex:1;display:flex;flex-direction:column;align-items:center;padding:26px 40px 0}
.obar{height:60px;flex:none;border-top:1px solid var(--border);display:flex;align-items:center;
  gap:10px;padding:0 22px;background:var(--bg-app)}
.upan{background:var(--bg-surface);border:1px solid var(--primary);border-radius:10px;padding:14px 16px}
.ul{margin:6px 0 0;padding-left:17px;font-size:12.5px;color:var(--fg2);line-height:1.6}
"""

NAV = [("Общие", "cog"), ("Модели", "chip"), ("Вывод", "out"),
       ("Сеть и обновления", "refresh"), ("Продвинутые", "sliders"), ("О программе", "info")]


def side(active):
    items = "".join('<div class="nv%s">%s<span>%s</span></div>'
                    % (" on" if n == active else "", ic(i, 17, "currentColor"), n) for n, i in NAV)
    return ('<div class="side"><div class="slogo">%s</div><div class="nav">%s</div>'
            '<div style="flex:1"></div><div class="nv sub">%s<span>Отладка (Ctrl+Shift+D)</span></div></div>'
            % (logo(22, 15, "var(--fg1)"), items, ic("sliders", 17, "currentColor")))


def shell(theme, active, head, body, footer, title="Astra Voice — Настройки"):
    tb = ('<div class="tbar">%s<span class="t">%s</span><span class="sp"></span>'
          '<span class="wbtn"></span><span class="wbtn"></span><span class="wbtn c"></span></div>'
          % (mark(16), title))
    return ('<div class="win" style="width:%dpx;height:%dpx"><div class="tbar-x"></div>%s'
            '<div class="wrap">%s<div class="cont"><div class="chead">%s</div>'
            '<div class="cbody scr">%s</div></div></div>%s</div>'
            % (W, H, tb, side(active), head, body, footer))


def head(h2, sub, right=""):
    r = ('<span style="flex:1"></span>' + right) if right else ""
    return ('<div style="display:flex;align-items:center;gap:10px"><div><div class="h2">%s</div>'
            '<div class="sm" style="margin-top:2px">%s</div></div>%s</div>' % (h2, sub, r))


# ── 1. Общие ───────────────────────────────────────────────────────────────
def general_body():
    g1 = group("Диктовка", [
        row("Горячая клавиша", '<span style="display:flex;gap:8px;align-items:center">%s%s</span>'
            % (key("Ctrl + Space"), btn("Изменить", "sm")), "Удерживайте и говорите"),
        row("Режим", '<span class="seg"><span class="on">Удерживать</span><span>Нажать-нажать</span></span>'),
        row("Микрофон", '<span class="sel">Системный по умолчанию %s</span>' % ic("chevd", 13, "var(--fg3)")),
    ])
    g2 = group("Индикация", [
        row("Индикатор записи", '<span class="sel">Пилюля снизу экрана %s</span>' % ic("chevd", 13, "var(--fg3)")),
        row("Звук начала и конца", tgl(False)),
        row("Значок в системном трее", tgl(True, lock=True) if False else tgl(True),
            "Обязателен: запись всегда видна", dis=True),
    ])
    g3 = group("Запуск", [
        row("Автозапуск при входе в систему", tgl(True),
            "Создаёт ~/.config/autostart/astra-voice.desktop"),
    ])
    return g1 + g2 + g3 + '<div class="sb" style="top:6px;height:150px"></div>'


def general(theme):
    f = foot(foot_left("GigaAM v3 RNN-T", "idle"),
             '<span class="muted">Проверка обновлений отключена</span>' if theme == "light"
             else '<span class="muted">Источник обновлений недоступен · Обновить из файла…</span>')
    b = shell(theme, "Общие", head("Общие", "Диктовка, индикация и запуск"), general_body(), f)
    cap = ('<b>A · Панель</b> — экран «Общие» · %s тема · окно 900×620'
           % ("светлая" if theme == "light" else "тёмная"))
    leg = ("<b>Показаны состояния:</b><ul>"
           "<li>строка настройки = заголовок · «?» · контрол справа (канон категории, Handy <code>SettingContainer</code>);</li>"
           "<li><b>disabled</b> — «Значок в системном трее» выключить нельзя (запрет скрытой записи, PRD §9.5);</li>"
           "<li>строка внизу: слева активная модель, справа состояние обновления — "
           "в светлой «Проверка обновлений отключена» (тумблеры по умолчанию пусты), в тёмной «Источник обновлений недоступен».</li></ul>")
    return page("A · Панель — Общие", cap, b, theme, CSS_A, leg)


# ── 2. Модели ──────────────────────────────────────────────────────────────
def mcard(mo, state=None, extra=""):
    st = state or mo["status"]
    cls = "mc" + (" act" if st == "active" else "")
    badges = "".join('<span class="bd %s">%s</span>' % (k, t) for t, k in mo["badges"])
    if st == "downloading":
        badges = '<span class="bd new">Загрузка</span>'
    mets = ('<div class="mmets">%s%s</div>'
            % (met("Качество", mo["q"], mo["qv"], not mo["measured"]),
               met("Скорость", mo["s"], mo["sv"], not mo["measured"])))
    top = ('<div class="mtop"><div class="ml"><div class="mname">%s <span class="mven">· %s</span></div>'
           '<div class="mpurp">%s</div>%s</div>%s</div>'
           % (mo["name"], mo["vendor"], mo["purpose"],
              ('<div class="mbadges">%s</div>' % badges) if badges else "", mets))
    punct = "с пунктуацией" if mo["punct"] else "без пунктуации"
    ram = ('<b style="color:var(--fg1)">%s ОЗУ</b> · %s' % (mo["ram"], mo["ramkind"])) if mo["measured"] \
        else ('%s ОЗУ · %s' % (mo["ram"], mo["ramkind"]))
    tags = ('%s <span class="dot"></span> %s <span class="dot"></span> %s <span class="dot"></span> %s'
            % (mo["lang"], punct, mo["lic"], mo["origin"]))
    if st == "downloading":
        bottom = ('<div class="mhr"></div>'
                  '<div class="prog" style="margin-bottom:7px"><i style="width:43%%"></i></div>'
                  '<div class="mbot"><span>Загрузка 43 %% · 5,2 МБ/с · осталось ~25 с · huggingface.co</span>'
                  '<span class="sp"></span>%s</div>' % btn("Отмена", "sm"))
    else:
        acts = {
            "active": btn("Удалить", "sm dis"),
            "installed": btn("Выбрать", "sm pri") + " " + btn("Удалить", "sm"),
            "avail": btn("Скачать", "sm pri") + " " + btn("Из файла…", "sm"),
            "new": btn("Скачать", "sm pri") + " " + btn("Из файла…", "sm"),
            "lowram": btn("Скачать", "sm") + " " + btn("Из файла…", "sm"),
            "nonet": btn("Скачать", "sm dis") + " " + btn("Установить из файла…", "sm"),
        }[st]
        note = ""
        if st == "lowram":
            note = ('<span style="color:var(--warn-ink)">%s Нужно ~1,8 ГБ ОЗУ — на этом компьютере 8 ГБ</span>'
                    '<span class="dot"></span>' % ic("alert", 12, "var(--warn-ink)"))
        if st == "nonet":
            note = ('<span style="color:var(--fg3)">Нет доступа к huggingface.co</span><span class="dot"></span>')
        bottom = ('<div class="mhr"></div><div class="mbot">%s<span class="sp"></span>%s'
                  '<span>%s на диске</span><span class="dot"></span><span>%s</span>%s</div>'
                  % (tags, note, mo["disk"], ram, " " + acts))
    return '<div class="%s">%s%s%s</div>' % (cls, top, bottom, extra)


def models_body(theme):
    chips = ('<div style="display:flex;gap:8px;align-items:center;margin:0 0 12px">'
             '<span class="chip">%s Все языки %s</span>'
             '<span class="chip">Только отечественные %s</span>'
             '<span style="flex:1"></span><span class="c12">Установлено 3 из 12 · 687 МБ на диске</span></div>'
             % (ic("globe", 13, "var(--fg3)"), ic("chevd", 12, "var(--fg3)"), tgl(False)))
    sec1 = '<div class="grp">Установленные · 3</div>'
    sec1 += mcard(m("rnnt"), "active") + mcard(m("ctc"), "installed") + mcard(m("rnnt-np"), "downloading")
    sec2 = '<div class="grp" style="margin-top:14px">Доступные · 9</div>'
    sec2 += mcard(m("tone"), "nonet" if theme == "dark" else "avail")
    sec2 += mcard(m("wturbo"), "lowram")
    return chips + sec1 + sec2 + '<div class="sb" style="top:40px;height:190px"></div>'


def models(theme):
    right = btn("Установить из файла…", "sm", "folder")
    f = foot(foot_left("GigaAM v3 RNN-T", "idle"),
             '<span class="muted">Обновление модели: GigaAM v3 RNN-T</span>')
    hd = head("Модели", "Честные цифры: размер на диске, память в работе, качество и скорость", right)
    b = shell(theme, "Модели", hd, models_body(theme), f)
    cap = ('<b>A · Панель</b> — каталог моделей · %s тема · окно 900×620'
           % ("светлая" if theme == "light" else "тёмная"))
    leg = ("<b>Состояния карточек:</b> активна (рамка акцентом) · установлена · <b>скачивается 43 %</b> "
           "(прогресс, скорость, источник, «Отмена») · доступна · предупреждение по ОЗУ (не запрет)"
           + (" · <b>нет сети</b>: «Скачать» disabled, активна «Установить из файла…»." if theme == "dark" else ".")
           + "<br><b>ОЗУ:</b> «415 МБ · замерено на этом компьютере» жирным против «~420 МБ · оценка» серым. "
             "Полоски: серая = бенчмарк автора, синяя = замерено на этой машине.")
    return page("A · Панель — Модели", cap, b, theme, CSS_A, leg)


# ── 3. Обновления ──────────────────────────────────────────────────────────
def updates(admin=False):
    lock = admin
    rows = [
        row("Проверять обновления утилиты", tgl(not admin, lock=admin),
            "Не чаще раза в сутки · github.com", lock=lock),
        row("Проверять обновления моделей", tgl(not admin, lock=admin),
            "Не чаще раза в сутки · huggingface.co", lock=lock),
        row("Офлайн-режим", tgl(admin, lock=admin),
            "Полностью запрещает сетевые запросы", lock=lock),
        row("Адрес каталога моделей",
            '<span class="sel mono" style="font-size:12px">%s %s</span>'
            % ("https://mirror.corp.local/av/" if admin else "встроенный", ic("chevd", 13, "var(--fg3)")),
            lock=lock),
        row("Проверка вручную", btn("Проверить сейчас", "sm dis" if admin else "sm", "refresh")
            + " " + btn("Обновить из файла…", "sm", "file"), hint=False),
    ]
    grp = group("Сеть и обновления", rows)
    if admin:
        panel = (
            '<div class="upan" style="border-color:var(--border)">'
            '<div style="display:flex;align-items:center;gap:9px">%s'
            '<div class="h3">Доступна версия 0.2.1</div>'
            '<span class="bd upd">Установку выполняет администратор</span></div>'
            '<div class="sm" style="margin-top:6px">На этом компьютере включена замкнутая программная среда '
            '(<span class="mono">DIGSIG_ELF_MODE=1</span>). Мы скачаем пакет, файл контрольных сумм, '
            'подпись и инструкцию — передайте их администратору.</div>'
            '<ul class="ul"><li>astra-voice_0.2.1_amd64.deb — 74,3 МБ</li>'
            '<li>SHA256SUMS и SHA256SUMS.minisig — подпись проверена нашим ключом</li>'
            '<li>INSTALL-ADMIN.md — порядок установки и подписи для ЗПС</li></ul>'
            '<div style="display:flex;gap:8px;margin-top:12px">%s%s%s</div></div>'
            % (ic("shield", 18, "var(--warn-ink)"), btn("Скачать пакет для администратора", "pri", "down"),
               btn("Открыть папку", "", "folder"), btn("Напомнить позже", "gh")))
        f = foot(foot_left("GigaAM v3 RNN-T", "idle"),
                 '<span class="up">%s Доступна 0.2.1 · Скачать для администратора</span>'
                 % ic("shield", 13, "var(--primary)"))
        note = ('<div class="note w" style="margin-bottom:14px">%s<div><b>Профиль «защищённый контур»</b>'
                'Часть настроек задана администратором в <span class="mono">/etc/astra-voice/policy.conf</span>: '
                'офлайн-режим, корпоративный каталог, установка только администратором.</div></div>'
                % ic("lock", 15, "var(--warn-ink)"))
    else:
        panel = (
            '<div class="upan"><div style="display:flex;align-items:center;gap:9px">'
            '<div class="h3">Доступна версия 0.2.1</div><span class="bd rec">Новая</span>'
            '<span style="flex:1"></span><span class="c12">7 сентября 2026 · 74,3 МБ</span></div>'
            '<div class="sm" style="margin-top:7px;color:var(--fg2)"><b>Что нового</b></div>'
            '<ul class="ul"><li>Каталог моделей: 10 моделей, память в работе замеряется на вашем компьютере</li>'
            '<li>Автозапуск при входе в систему</li>'
            '<li>Исправлено: пилюля перекрывалась панелью при смене раскладки</li></ul>'
            '<div style="display:flex;gap:8px;margin-top:12px;align-items:center">%s%s%s'
            '<span style="flex:1"></span><span class="c12">Подпись релиза проверяется до установки</span></div></div>'
            % (btn("Скачать и установить", "pri", "down"), btn("Пропустить эту версию", ""),
               btn("Напомнить позже", "gh")))
        f = foot(foot_left("GigaAM v3 RNN-T", "idle"),
                 '<span class="up">Доступна версия 0.2.1 · Установить</span>')
        note = ""
    body = note + panel + '<div style="height:16px"></div>' + grp + ('<div class="sb" style="top:6px;height:200px"></div>')
    hd = head("Сеть и обновления", "Четыре сетевых действия — и ни одного больше")
    b = shell("light", "Сеть и обновления", hd, body, f)
    if admin:
        cap = '<b>A · Панель</b> — обновления, ветка «устанавливает администратор» (ЗПС / нет прав) · 900×620'
        leg = ("<b>Показано:</b> трек B из PRD F9 — кнопки «Установить» нет; тумблеры заблокированы "
               "с подписью «Задано администратором»; строка внизу — «Скачать для администратора» со щитом. "
               "Так же выглядит экран при <b>отсутствии прав</b> (не в группе astra-admin) и при "
               "<span class=\"mono\">policy.conf: updates=admin</span>.")
    else:
        cap = '<b>A · Панель</b> — обновления, трек без ЗПС + строка внизу «Доступна версия» · 900×620'
        leg = ("<b>Показано:</b> строка внизу справа — единственное цветовое пятно («Доступна версия 0.2.1 · "
               "Установить»); панель «Что нового» раскрыта из этой строки; установка только по явному действию, "
               "далее одно окно polkit. <b>Другие состояния строки</b> (checking / uptodate / downloading 42 % / "
               "unavailable / restart) описаны в <code>design/flows.md</code> §3.2.")
    return page("A · Панель — Обновления", cap, b, "light", CSS_A, leg)


# ── 4. Онбординг ───────────────────────────────────────────────────────────
def onb_shell(step, body, actions, title="Astra Voice — первый запуск"):
    dots = "".join('<span class="sd %s"></span>' % ("on" if i == step else ("dn" if i < step else ""))
                   for i in range(1, 6))
    tb = ('<div class="tbar">%s<span class="t">%s</span><span class="sp"></span>'
          '<span class="wbtn"></span><span class="wbtn"></span><span class="wbtn c"></span></div>' % (mark(16), title))
    hd = ('<div style="display:flex;align-items:center;gap:12px;padding:14px 22px 0">'
          '<span class="c12">Шаг %d из 5</span><span class="steps">%s</span></div>' % (step, dots))
    return ('<div class="win" style="width:%dpx;height:%dpx">%s%s<div class="obody">%s</div>'
            '<div class="obar">%s</div></div>' % (W, H, tb, hd, body, actions))


def onb_network():
    body = ('<div style="width:560px">'
            '<div style="display:flex;gap:14px;align-items:center;margin-bottom:18px">%s'
            '<div><div class="h1" style="font-size:24px">Astra Voice</div>'
            '<div class="sm" style="margin-top:3px">Голосовой ввод для Astra Linux. Распознавание идёт '
            'на этом компьютере — записи никуда не отправляются.</div></div></div>'
            '%s'
            '<div class="note i" style="margin-top:14px">%s<div><b>Пока оба переключателя выключены, '
            'программа не выходит в сеть.</b>Скачать модель на следующем шаге можно и без них — это ваше '
            'явное действие. Проверки идут не чаще раза в сутки к huggingface.co и github.com; '
            'список хостов — в «О программе → Приватность».</div></div>'
            '</div>'
            % (appicon(52),
               card([
                   row("Язык интерфейса", '<span class="sel">Русский %s</span>' % ic("chevd", 13, "var(--fg3)"),
                       "Определён по системной локали ru_RU", hint=False),
                   row("Проверять обновления утилиты", tgl(False), "Раз в сутки, github.com"),
                   row("Проверять обновления моделей", tgl(False), "Раз в сутки, huggingface.co"),
               ]),
               ic("info", 15, "var(--fg3)")))
    actions = ('<span class="c12">Эти настройки можно изменить позже в разделе «Сеть и обновления»</span>'
               '<span style="flex:1"></span>%s%s' % (btn("Пропустить", "gh"), btn("Продолжить", "pri")))
    b = onb_shell(1, body, actions)
    leg = ("<b>Ключевое требование PRD:</b> оба тумблера <b>пусты во всех профилях</b> (решение заказчика "
           "2026-09-07) — до явного включения ни одного сетевого запроса. Вариант «Задано администратором» "
           "(policy.conf offline=true) показан на экране обновлений.")
    return page("A · Панель — Онбординг 1", '<b>A · Панель</b> — онбординг 1/5: приветствие и сеть · 900×620',
                b, "light", CSS_A, leg)


def onb_hotkey():
    body = ('<div style="width:560px">'
            '<div class="h2">Горячая клавиша</div>'
            '<div class="sm" style="margin:4px 0 16px">Зажмите её и говорите. Отпустили — текст появится там, '
            'где стоит курсор.</div>'
            '%s'
            '<div class="grp" style="margin:16px 0 7px">Назначение новой комбинации</div>'
            '<div class="field cap"><span class="mono" style="font-size:14px;color:var(--fg1)">Alt + F2</span>'
            '<span style="flex:1"></span><span class="c12">Нажмите комбинацию · Esc — отмена</span></div>'
            '<div class="note w" style="margin-top:10px">%s<div><b>Комбинация занята в KDE</b>'
            'Alt + F2 назначена на действие «Показать KRunner». Оставить её можно, но диктовка может не '
            'сработать — система заберёт нажатие себе.</div></div>'
            '<div style="display:flex;gap:8px;margin-top:10px">%s%s</div>'
            '</div>'
            % (card([
                row("Текущая комбинация", '<span style="display:flex;gap:8px;align-items:center">%s%s</span>'
                    % (key("Ctrl + Space"), btn("Изменить", "sm")), "По умолчанию, как в Handy", hint=False),
                row("Режим", '<span class="seg"><span class="on">Удерживать</span><span>Нажать-нажать</span></span>',
                    "Удерживать — самый предсказуемый вариант", hint=False),
                row("Вторая комбинация для «нажать-нажать»",
                    '<span style="display:flex;gap:8px;align-items:center">'
                    '<span class="muted" style="font-size:13px">Не назначена</span>%s</span>' % btn("Назначить", "sm"),
                    hint=False),
            ]), ic("alert", 15, "var(--warn-ink)"),
               btn("Оставить Alt + F2", ""), btn("Выбрать другую", "pri")))
    actions = ('%s<span style="flex:1"></span>%s%s'
               % (btn("Назад", ""), btn("Пропустить", "gh"), btn("Продолжить", "pri")))
    b = onb_shell(3, body, actions)
    leg = ("<b>Состояния поля захвата:</b> покой (клавиша Ctrl+Space + «Изменить») · <b>захват</b> "
           "(рамка акцентом, «Нажмите комбинацию… Esc — отмена») · <b>конфликт с KDE</b> — предупреждение "
           "с именем действия, не запрет (PRD S1-A4). Не показано здесь, есть в flows.md: «уже назначена на "
           "toggle» и «комбинацию не удалось захватить».")
    return page("A · Панель — Онбординг 3", '<b>A · Панель</b> — онбординг 3/5: горячая клавиша и захват · 900×620',
                b, "light", CSS_A, leg)


# ── 5. Пилюля на рабочем столе ─────────────────────────────────────────────
def desktop_scene(pill_html, w=900, h=420):
    return ('<div class="desk" style="width:%dpx;height:%dpx">'
            '<div class="dwin" style="left:70px;top:34px;width:520px;height:270px">'
            '<div class="dt">Письмо — без имени — Kate</div>'
            '<div class="dc">Добрый день, Сергей Петрович!<br><br>'
            'По итогам совещания направляю сводку: сроки по первому этапу сдвигаются на неделю, '
            'смета остаётся прежней. Прошу подтвердить<span style="border-left:1.5px solid #1B3A73;'
            'margin-left:1px">&nbsp;</span></div></div>'
            '<div style="position:absolute;left:50%%;transform:translateX(-50%%);bottom:56px">%s</div>'
            '<div class="panel"><span class="pb">Kate</span><span class="pb">Firefox</span>'
            '<span class="sp"></span>%s<span class="pb mono">14:26</span></div>'
            '</div>' % (w, h, pill_html,
                        '<span style="display:inline-flex;align-items:center;gap:9px;padding-right:6px">'
                        + tray("listening", 17, "#C4CDDC") + tray("idle", 17, "#8C97AC") + '</span>'))


def pills():
    icons = "".join('<div class="cell">%s<span class="cn">%s</span></div>'
                    % ('<span style="background:#0B1220;border-radius:6px;padding:6px;display:inline-flex">%s</span>'
                       % tray(s, 20, "#C4CDDC"), n)
                    for s, n in [("idle", "готов"), ("listening", "слушаю"), ("processing", "распознаю"),
                                 ("done", "готово"), ("error", "ошибка")])
    body = (desktop_scene(pill("listening")) +
            '<div class="cap" style="margin-top:6px">Все состояния пилюли (F3.2)</div>' + pill_gallery() +
            '<div class="cap" style="margin-top:6px">Значок в трее — четыре состояния + ошибка (F3.4)</div>'
            '<div class="gal">%s</div>' % icons)
    leg = ("<b>Пилюля-оверлей:</b> у нижнего края экрана, поверх окон, <b>не забирает фокус</b> и не "
           "попадает в Alt+Tab. Геометрия — ориентир Handy (172×36, r=18), для русских подписей ширина "
           "резиновая. Состояние всегда передаётся <b>формой + подписью</b>, не только цветом: столбики / "
           "галочка / треугольник. Бирюза #12B3A0 — только «слушаю», красный — только ошибка. "
           "«×» справа — отмена записи.")
    return page("A · Панель — Пилюля", '<b>A · Панель</b> — пилюля-оверлей на рабочем столе + все состояния',
                body, "light", CSS_A, leg)


# ── 6. Трей ────────────────────────────────────────────────────────────────
def menu_idle():
    return ('<div class="menu">'
            '<div class="mi hd">%s Готов · Ctrl + Space</div><div class="msep"></div>'
            '<div class="mi dis">Отмена</div>'
            '<div class="mi">Модель<span class="sp"></span><span class="c12">GigaAM v3 RNN-T</span>%s</div>'
            '<div class="mi">Скопировать последний текст</div>'
            '<div class="msep"></div>'
            '<div class="mi">Настройки…<span class="sp"></span><span class="sc">Ctrl+,</span></div>'
            '<div class="mi dis">Проверить обновления</div>'
            '<div class="mi">О программе</div><div class="msep"></div>'
            '<div class="mi">Выход<span class="sp"></span><span class="sc">Ctrl+Q</span></div></div>'
            % (tray("idle", 15, "var(--fg3)"), ic("chev", 13, "var(--fg3)")))


def menu_rec():
    return ('<div class="menu">'
            '<div class="mi hd" style="color:var(--accent-ink)">%s Слушаю…</div><div class="msep"></div>'
            '<div class="mi hl">Отмена</div>'
            '<div class="mi dis">Модель<span class="sp"></span><span class="c12">GigaAM v3 RNN-T</span></div>'
            '<div class="mi dis">Скопировать последний текст</div>'
            '<div class="msep"></div>'
            '<div class="mi">Настройки…<span class="sp"></span><span class="sc">Ctrl+,</span></div>'
            '<div class="mi dis">Проверить обновления</div>'
            '<div class="mi">О программе</div><div class="msep"></div>'
            '<div class="mi">Выход<span class="sp"></span><span class="sc">Ctrl+Q</span></div></div>'
            % tray("listening", 15, "var(--accent)"))


def trays():
    scene = ('<div class="desk" style="width:900px;height:344px">'
             '<div style="position:absolute;right:16px;bottom:52px">%s</div>'
             '<div class="panel"><span class="pb">Kate</span><span class="sp"></span>'
             '<span style="display:inline-flex;align-items:center;gap:9px;padding-right:6px">%s%s</span>'
             '<span class="pb mono">14:26</span></div></div>'
             % (menu_idle(), tray("idle", 17, "#F2F5FA"), tray("idle", 17, "#8C97AC")))
    two = ('<div style="display:flex;gap:26px;align-items:flex-start">'
           '<div><div class="cap" style="margin-bottom:7px">В покое</div>%s</div>'
           '<div><div class="cap" style="margin-bottom:7px">Во время записи / распознавания</div>%s</div>'
           '<div style="max-width:280px" class="legend"><b>Условия неактивности</b>'
           '<ul><li>«Отмена» — только во время записи или распознавания;</li>'
           '<li>«Скопировать последний текст» — пока текста нет;</li>'
           '<li>«Проверить обновления» — при выключенных проверках или офлайн-режиме;</li>'
           '<li>«Модель ▸» при одной модели — пункт без подменю, ведёт в «Модели».</li></ul></div></div>'
           % (menu_idle(), menu_rec()))
    body = scene + two
    leg = ("<b>Трей — основной дом приложения</b> (PRD F12): левый клик открывает окно настроек, "
           "правый — это меню. Значок обязателен всегда, даже если пилюля выключена — иначе запись была бы "
           "скрытой (§9.5). Значок берёт цвет темы панели, цвет несёт только третья точка знака.")
    return page("A · Панель — Трей", '<b>A · Панель</b> — значок и меню в системном трее', body, "light", CSS_A, leg)


def build():
    out = []
    out.append(write("A/01-general.html", general("light")))
    out.append(write("A/01-general-dark.html", general("dark")))
    out.append(write("A/02-models.html", models("light")))
    out.append(write("A/02-models-dark.html", models("dark")))
    out.append(write("A/03-updates.html", updates(False)))
    out.append(write("A/03-updates-admin.html", updates(True)))
    out.append(write("A/04-onboarding-network.html", onb_network()))
    out.append(write("A/04-onboarding-hotkey.html", onb_hotkey()))
    out.append(write("A/05-pill.html", pills()))
    out.append(write("A/06-tray.html", trays()))
    return out
