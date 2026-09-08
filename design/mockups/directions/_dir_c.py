# -*- coding: utf-8 -*-
"""Направление C «Пульт» — компактное окно-поповер от значка в трее (380×520).
Главный экран = состояние + модель + горячая клавиша на одном экране.
Полные настройки — вторым уровнем: окно 820×560 с верхними вкладками и каталогом «список + карточка».
"""
from _base import (page, write, mark, logo, appicon, tray, ic, tgl, key, bd, btn, card,
                   met, foot, foot_left, pill, pill_gallery, m)

PW, PH = 380, 520
SW, SH = 820, 560

CSS_C = """
.pop{width:380px;height:520px;background:var(--bg-app);color:var(--fg1);border:1px solid var(--border);
  border-radius:12px;box-shadow:0 18px 48px var(--shadow);display:flex;flex-direction:column;
  overflow:hidden;flex:none;position:relative}
.phd{height:46px;flex:none;display:flex;align-items:center;gap:9px;padding:0 14px;
  border-bottom:1px solid var(--border-soft)}
.phd .sp{flex:1}
.ib{width:28px;height:28px;border-radius:7px;display:inline-flex;align-items:center;justify-content:center;
  color:var(--fg3);background:var(--bg-sunk)}
.pbody{flex:1;min-height:0;overflow:hidden;padding:12px;display:flex;flex-direction:column;gap:7px}
.stbox{background:var(--bg-surface);border:1px solid var(--border);border-radius:11px;padding:10px;
  text-align:center}
.stbox .sst{display:inline-flex;align-items:center;gap:8px;font-size:17px;font-weight:700;color:var(--fg1)}
.stbox .shint{font-size:12px;color:var(--fg3);margin-top:7px;line-height:1.4}
.kbig{font-family:var(--font-mono);font-size:16px;color:var(--fg1);background:var(--bg-sunk);
  border-radius:8px;padding:6px 13px 5px;border-bottom:2px solid var(--border);display:inline-block;
  letter-spacing:.02em;margin-top:9px}
.lvl{display:flex;align-items:flex-end;gap:3px;height:14px;justify-content:center;margin-top:10px}
.lvl i{width:5px;border-radius:2.5px;background:var(--fg4);display:block}
.crow{background:var(--bg-surface);border:1px solid var(--border);border-radius:10px;padding:9px 12px;
  display:flex;align-items:center;gap:10px}
.crow .cl{flex:1;min-width:0;overflow:hidden}
.crow .cn{font-size:13.5px;font-weight:500;color:var(--fg1)}
.crow .cs{font-size:11.5px;color:var(--fg3);margin-top:2px}
.qt{background:var(--bg-surface);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.qt .r{padding:7px 12px;min-height:34px}
.qt .r+.r{border-top:1px solid var(--border-soft)}
.qt .lbl{font-size:13.5px}
.pfoot{height:36px;flex:none;display:flex;align-items:center;gap:8px;padding:0 14px;
  border-top:1px solid var(--border);font-size:12px;color:var(--fg3);background:var(--bg-app)}
.pfoot .sp{flex:1}
.pfoot .up{color:var(--primary);font-weight:500}
.met{gap:6px}
.met .mn{width:42px;font-size:10.5px}
.met .trk{width:44px}
.met .mv{font-size:10.5px}

/* ── второй уровень: окно с верхними вкладками ── */
.tabs{height:44px;flex:none;display:flex;align-items:center;gap:4px;padding:0 14px;
  border-bottom:1px solid var(--border);background:var(--bg-app)}
.tb{font-size:13.5px;color:var(--fg3);padding:6px 12px;border-radius:7px}
.tb.on{background:var(--bg-sunk);color:var(--fg1);font-weight:500;box-shadow:inset 0 -2px 0 var(--primary)}
.split{flex:1;display:flex;min-height:0}
.lst{width:290px;flex:none;border-right:1px solid var(--border);background:var(--bg-sunk);
  display:flex;flex-direction:column}
.lsh{padding:11px 13px 8px;display:flex;align-items:center;gap:8px}
.li{display:flex;align-items:center;gap:9px;padding:9px 13px;border-top:1px solid var(--border-soft)}
.li.on{background:var(--bg-surface);box-shadow:inset 3px 0 0 var(--primary)}
.li .ln{font-size:13.5px;color:var(--fg1);font-weight:500}
.li .ls{font-size:11.5px;color:var(--fg3);margin-top:1px}
.li .lft{flex:1;min-width:0}
.sdot{width:8px;height:8px;border-radius:50%;flex:none}
.det{flex:1;min-width:0;padding:16px 20px;display:flex;flex-direction:column}
.kv{display:flex;gap:10px;font-size:12.5px;padding:5px 0;border-bottom:1px solid var(--border-soft)}
.kv .k{width:140px;color:var(--fg3);flex:none}
.kv .v{color:var(--fg1)}
.mrow{display:flex;align-items:center;gap:10px;padding:5px 0}
.mrow .mn2{width:84px;font-size:12.5px;color:var(--fg3);flex:none}
.mrow .trk{width:130px}
.actbar{margin-top:auto;padding-top:12px;border-top:1px solid var(--border-soft);display:flex;gap:8px;
  align-items:center}
.upan{background:var(--bg-surface);border:1.5px solid var(--primary);border-radius:11px;padding:14px 16px}
.ul{margin:6px 0 0;padding-left:17px;font-size:12.5px;color:var(--fg2);line-height:1.6}
.menu{width:246px}
.mi{font-size:12.5px}
.pill.c{height:30px;border-radius:15px;font-size:11.5px;padding:0 9px;gap:7px}
.pill.c .lv{height:16px;gap:2px}
.pill.c .lv i{width:4px}
.pill.c .x{width:19px;height:19px;font-size:11px}
.owin{width:420px;height:560px;background:var(--bg-app);color:var(--fg1);border-radius:10px;
  overflow:hidden;box-shadow:0 16px 46px var(--shadow);display:flex;flex-direction:column;flex:none}
.obody{flex:1;padding:20px 20px 0;display:flex;flex-direction:column}
.obar{height:56px;flex:none;border-top:1px solid var(--border);display:flex;align-items:center;gap:8px;
  padding:0 16px}
.sd{width:6px;height:6px;border-radius:50%;background:var(--border)}
.sd.on{background:var(--primary);width:18px;border-radius:3px}
.sd.dn{background:var(--fg4)}
"""


def panel_strip(width=380, hi=True):
    return ('<div style="width:%dpx;height:40px;background:#0B1220;border-radius:8px;display:flex;'
            'align-items:center;gap:9px;padding:0 12px;box-shadow:0 8px 24px var(--shadow)">'
            '<span style="font-size:11.5px;color:#C4CDDC;background:rgba(255,255,255,.07);'
            'border-radius:5px;padding:4px 9px">Kate</span><span style="flex:1"></span>'
            '<span style="display:inline-flex;align-items:center;gap:9px;%s">%s</span>'
            '<span style="font-size:11.5px;color:#C4CDDC" class="mono">14:26</span></div>'
            % (width, "background:rgba(255,255,255,.10);border-radius:6px;padding:3px 5px" if hi else "",
               tray("idle", 17, "#F2F5FA") + tray("idle", 17, "#8C97AC")))


# ── 1. Поповер ─────────────────────────────────────────────────────────────
def popover(theme):
    lvl = "".join('<i style="height:4px"></i>' for _ in range(11))
    body = (
        '<div class="phd">%s<span class="sp"></span><span class="ib">%s</span>'
        '<span class="ib">%s</span></div>'
        '<div class="pbody">'
        '<div class="stbox"><span class="sst">%s Готов</span>'
        '<div><span class="kbig">Ctrl + Space</span></div>'
        '<div class="shint">Зажмите и говорите — текст появится у курсора</div>'
        '<div class="lvl">%s</div></div>'
        '<div class="crow">%s<div class="cl"><div class="cn">GigaAM v3 RNN-T</div>'
        '<div class="cs">232 МБ на диске · <b style="color:var(--fg1)">415 МБ ОЗУ</b> · замерено</div>'
        '<div style="display:flex;gap:8px;margin-top:5px">%s%s</div></div>%s</div>'
        '<div class="qt">%s%s%s</div>'
        '<div class="crow" style="opacity:.5"><div class="cl"><div class="cn">Скопировать последний текст</div>'
        '</div><span class="cs">пока нечего</span>%s</div>'
        '</div>'
        '<div class="pfoot"><span class="up">Доступна 0.2.1 · Установить</span><span class="sp"></span>'
        '<span class="mono">v0.2.0</span></div>'
        % (logo(22, 14), ic("cog", 15), ic("power", 15),
           tray("idle", 18, "var(--fg3)"), lvl, mark(18),
           met("Качество", 90, "7,6 %"), met("Скорость", 92, "≈42×"),
           ic("chev", 14, "var(--fg3)"),
           '<div class="r"><div class="left"><div class="lbl">Пилюля-индикатор</div></div>%s</div>' % tgl(True),
           '<div class="r"><div class="left"><div class="lbl">Звук начала и конца</div></div>%s</div>' % tgl(False),
           '<div class="r"><div class="left"><div class="lbl">Автозапуск</div></div>%s</div>' % tgl(True),
           ic("file", 15, "var(--fg4)")))
    scene = ('<div style="display:flex;flex-direction:column;align-items:flex-end;gap:18px">'
             '<div class="pop">%s</div>%s</div>' % (body, panel_strip()))
    cap = ('<b>C · Пульт</b> — главный экран-поповер от значка в трее · %s тема · 380×520'
           % ("светлая" if theme == "light" else "тёмная"))
    leg = ("<b>Отличие направления:</b> главный экран — не окно настроек, а «пульт» у трея. На одном экране "
           "всё, что нужно каждый день: состояние, горячая клавиша, уровень микрофона, активная модель "
           "с честными цифрами и три частых переключателя. Полные настройки открываются шестерёнкой "
           "вторым уровнем. <b>Состояния:</b> «Готов» · «Скопировать последний текст» — <b>disabled</b>, "
           "пока текста нет · строка внизу — «Доступна 0.2.1 · Установить».")
    return page("C · Пульт — Поповер", cap, scene, theme, CSS_C, leg)


# ── 2. Модели (второй уровень) ─────────────────────────────────────────────
TABS = ["Общие", "Модели", "Вывод", "Сеть и обновления", "Продвинутые", "О программе"]


def swin(active_tab, content, footer, title="Astra Voice — Настройки"):
    tb = ('<div class="tbar">%s<span class="t">%s</span><span class="sp"></span>'
          '<span class="wbtn"></span><span class="wbtn"></span><span class="wbtn c"></span></div>'
          % (mark(16), title))
    tabs = "".join('<span class="tb%s">%s</span>' % (" on" if t == active_tab else "", t) for t in TABS)
    return ('<div class="win" style="width:%dpx;height:%dpx">%s<div class="tabs">%s</div>%s%s</div>'
            % (SW, SH, tb, tabs, content, footer))


DOTC = {"active": "var(--accent)", "installed": "var(--fg4)", "downloading": "var(--primary)",
        "avail": "transparent", "upd": "var(--warn-ink)"}


def li(mo, state, on=False):
    sub = {"active": "Активна · 232 МБ", "installed": "Установлена · 225 МБ",
           "downloading": "Загрузка 43 %", "avail": "Не скачана · %s" % mo["disk"],
           "upd": "Обновление доступно"}[state]
    dot = ('<span class="sdot" style="background:%s%s"></span>'
           % (DOTC[state], ";border:1px solid var(--border)" if state == "avail" else ""))
    return ('<div class="li%s">%s<div class="lft"><div class="ln">%s</div><div class="ls">%s</div></div>%s</div>'
            % (" on" if on else "", dot, mo["name"], sub,
               ('<span class="bd act">Активна</span>' if state == "active" else "")))


def models(theme):
    lst = ('<div class="lst scr"><div class="lsh">%s<span class="c12">Все языки</span>'
           '<span style="flex:1"></span>%s</div>'
           '<div class="grp" style="padding:2px 13px 4px;margin:0">Установленные · 3</div>%s%s%s'
           '<div class="grp" style="padding:10px 13px 4px;margin:0">Доступные · 9</div>%s%s%s'
           '<div class="sb" style="top:56px;height:150px"></div></div>'
           % (ic("globe", 14, "var(--fg3)"), btn("Из файла…", "sm"),
              li(m("rnnt"), "active", True), li(m("ctc"), "installed"), li(m("rnnt-np"), "downloading"),
              li(m("ml"), "avail"), li(m("tone"), "avail"), li(m("wturbo"), "avail")))
    mo = m("rnnt")
    kv = "".join('<div class="kv"><span class="k">%s</span><span class="v">%s</span></div>' % (k, v)
                 for k, v in [
                     ("Язык", "Только русский"),
                     ("Пунктуация", "Есть — расставляет знаки и «ё»"),
                     ("Лицензия", "MIT · GigaChat Team, 2024"),
                     ("Происхождение", "Отечественная"),
                     ("Ревизия", '<span class="mono">a6039be7 · закреплена</span>'),
                 ])
    detail = ('<div class="det"><div style="display:flex;align-items:flex-start;gap:10px">'
              '<div style="flex:1"><div class="h2">%s</div>'
              '<div class="sm" style="margin-top:3px">%s · %s</div></div>'
              '<span class="bd act">Активна</span><span class="bd rec">Рекомендуем</span></div>'
              '<div style="margin-top:14px">'
              '<div class="mrow"><span class="mn2">Качество</span>'
              '<span class="trk"><i style="width:90%%"></i></span>'
              '<span class="mv">WER 7,6 %% · Russian LibriSpeech</span></div>'
              '<div class="mrow"><span class="mn2">Скорость</span>'
              '<span class="trk"><i style="width:92%%"></i></span>'
              '<span class="mv"><b>≈42× быстрее речи</b> · 6 с → 0,14 с</span></div>'
              '<div class="mrow"><span class="mn2">Память в работе</span>'
              '<span class="mv" style="font-size:14px"><b>415 МБ</b> · замерено на этом компьютере</span></div>'
              '</div>'
              '<div class="grp" style="margin:12px 0 2px">Подробности</div>%s'
              '<div class="actbar"><span class="c12">Модель загружена в память · переключение ≤ 5 с</span>'
              '<span style="flex:1"></span>%s%s</div></div>'
              % (mo["name"], mo["vendor"], mo["purpose"], kv,
                 btn("Подробнее", "sm", "out"), btn("Удалить", "sm dis")))
    f = foot(foot_left("GigaAM v3 RNN-T", "idle"),
             '<span class="muted">Обновление модели: GigaAM v3 RNN-T</span>')
    b = swin("Модели", '<div class="split">%s%s</div>' % (lst, detail), f)
    scene = b
    cap = ('<b>C · Пульт</b> — каталог моделей, второй уровень · %s тема · окно 820×560'
           % ("светлая" if theme == "light" else "тёмная"))
    leg = ("<b>Отличие направления:</b> каталог — «список слева, карточка справа». Список компактный "
           "(имя · статус · размер), в карточке — все честные поля целиком: полоски с цифрами и источником, "
           "<b>память в работе</b> с пометкой достоверности, лицензия, происхождение, закреплённая ревизия. "
           "Так каталог из 12 моделей не превращается в длинную ленту. "
           "<b>Состояния в списке:</b> активна (бирюзовая точка) · установлена · скачивается 43 % · не скачана.")
    return page("C · Пульт — Модели", cap, scene, theme, CSS_C, leg)


# ── 3. Обновления (второй уровень) ─────────────────────────────────────────
def crow(label, control, sub=None, lock=False):
    lk = ('<span class="lockb">%s Задано администратором</span>' % ic("lock", 11)) if lock else ""
    return ('<div class="r"><div class="left"><div class="lbl">%s</div>%s</div>'
            '<span class="hint">?</span>%s%s</div>'
            % (label, ('<div class="sub">%s</div>' % sub) if sub else "", lk, control))


def updates(admin=False):
    rows = card([
        crow("Проверять обновления утилиты", tgl(not admin, lock=admin),
             "Раз в сутки · github.com", lock=admin),
        crow("Проверять обновления моделей", tgl(not admin, lock=admin),
             "Раз в сутки · huggingface.co", lock=admin),
        crow("Офлайн-режим", tgl(admin, lock=admin), "Запрещает все сетевые запросы", lock=admin),
        crow("Адрес каталога моделей",
             '<span class="sel mono" style="font-size:12px">%s %s</span>'
             % ("https://mirror.corp.local/av/" if admin else "встроенный", ic("chevd", 13, "var(--fg3)")),
             lock=admin),
    ])
    manual = ('<div style="display:flex;gap:8px;margin:12px 0 16px">%s%s</div>'
              % (btn("Проверить сейчас", "sm dis" if admin else "sm", "refresh"),
                 btn("Обновить из файла…", "sm", "file")))
    if admin:
        panel = ('<div class="upan" style="border-color:var(--border);border-width:1px">'
                 '<div style="display:flex;align-items:center;gap:9px">%s'
                 '<div class="h3">Доступна версия 0.2.1</div>'
                 '<span class="bd upd">Устанавливает администратор</span></div>'
                 '<div class="sm" style="margin-top:7px">На этом компьютере включена замкнутая программная '
                 'среда (<span class="mono">DIGSIG_ELF_MODE=1</span>). Мы скачаем пакет, контрольные суммы, '
                 'подпись и инструкцию — передайте их администратору.</div>'
                 '<ul class="ul"><li>astra-voice_0.2.1_amd64.deb — 74,3 МБ</li>'
                 '<li>SHA256SUMS + SHA256SUMS.minisig — подпись проверена</li>'
                 '<li>INSTALL-ADMIN.md — порядок установки и подписи для ЗПС</li></ul>'
                 '<div style="display:flex;gap:8px;margin-top:12px">%s%s</div></div>'
                 % (ic("shield", 18, "var(--warn-ink)"),
                    btn("Скачать пакет для администратора", "pri", "down"), btn("Открыть папку", "", "folder")))
        f = foot(foot_left("GigaAM v3 RNN-T", "idle"),
                 '<span class="up">%s Доступна 0.2.1 · Скачать для администратора</span>'
                 % ic("shield", 13, "var(--primary)"))
        top = ('<div class="note w" style="margin-bottom:14px">%s<div><b>Профиль «защищённый контур»</b>'
               'Часть настроек задана в <span class="mono">/etc/astra-voice/policy.conf</span> '
               'администратором: офлайн-режим, корпоративный каталог, установка только администратором.'
               '</div></div>' % ic("lock", 15, "var(--warn-ink)"))
    else:
        panel = ('<div class="upan"><div style="display:flex;align-items:center;gap:9px">'
                 '<div class="h3">Доступна версия 0.2.1</div><span class="bd rec">Новая</span>'
                 '<span style="flex:1"></span><span class="c12">7 сентября 2026 · 74,3 МБ</span></div>'
                 '<div class="sm" style="margin-top:7px;color:var(--fg2)"><b>Что нового</b></div>'
                 '<ul class="ul"><li>Каталог моделей: 10 моделей, память в работе замеряется у вас</li>'
                 '<li>Автозапуск при входе в систему</li>'
                 '<li>Исправлено: пилюля перекрывалась панелью при смене раскладки</li></ul>'
                 '<div style="display:flex;gap:8px;margin-top:12px;align-items:center">%s%s'
                 '<span style="flex:1"></span><span class="c12">Подпись релиза проверяется до установки</span>'
                 '</div></div>'
                 % (btn("Скачать и установить", "pri", "down"), btn("Пропустить эту версию", "")))
        f = foot(foot_left("GigaAM v3 RNN-T", "idle"),
                 '<span class="up">Доступна версия 0.2.1 · Установить</span>')
        top = ""
    inner = ('<div style="flex:1;padding:16px 20px" class="scr">'
             '<div class="h2">Сеть и обновления</div>'
             '<div class="sm" style="margin:3px 0 12px">Четыре сетевых действия — и ни одного больше</div>'
             '%s%s<div style="height:14px"></div>%s%s</div>' % (top, panel, rows, manual))
    b = swin("Сеть и обновления", inner, f)
    if admin:
        cap = '<b>C · Пульт</b> — обновления, ветка «устанавливает администратор» (ЗПС / нет прав) · 820×560'
        leg = ("<b>Трек B (PRD F9):</b> кнопки «Установить» нет; тумблеры заблокированы политикой; строка "
               "внизу — «Скачать для администратора». Тот же экран при отсутствии прав администратора "
               "и при <span class=\"mono\">policy.conf: updates=admin</span>.")
    else:
        cap = '<b>C · Пульт</b> — обновления, трек без ЗПС + строка внизу «Доступна версия» · 820×560'
        leg = ("<b>Трек A (PRD F9):</b> строка внизу — единственное цветовое пятно, она же дублируется "
               "в поповере. Установка только по явному действию, дальше одно окно polkit.")
    return page("C · Пульт — Обновления", cap, b, "light", CSS_C, leg)


# ── 4. Онбординг (компактный мастер 420×560) ───────────────────────────────
def onb_shell(step, body, actions):
    dots = "".join('<span class="sd %s"></span>' % ("on" if i == step else ("dn" if i < step else ""))
                   for i in range(1, 6))
    tb = ('<div class="tbar">%s<span class="t">Astra Voice — первый запуск</span><span class="sp"></span>'
          '<span class="wbtn"></span><span class="wbtn c"></span></div>' % mark(16))
    return ('<div class="owin">%s<div class="obody">%s</div>'
            '<div class="obar"><span class="steps" style="display:flex;gap:6px;align-items:center">%s</span>'
            '<span style="flex:1"></span>%s</div></div>' % (tb, body, dots, actions))


def onb_network():
    body = ('<div style="display:flex;gap:11px;align-items:center;margin-bottom:16px">%s'
            '<div><div class="h3" style="font-size:18px">Astra Voice</div>'
            '<div class="c12" style="margin-top:3px;line-height:1.45">Голосовой ввод для Astra Linux.<br>'
            'Всё распознавание — на этом компьютере.</div></div></div>%s'
            '<div class="note i" style="margin-top:12px;font-size:12px">%s<div>'
            '<b>Пока переключатели выключены, программа не выходит в сеть.</b>'
            'Скачать модель на следующем шаге можно и без них — это ваше явное действие.<br>'
            'Хосты: huggingface.co, github.com. Проверки — не чаще раза в сутки.</div></div>'
            % (appicon(44),
               card([
                   crow("Язык интерфейса", '<span class="sel sm">Русский %s</span>' % ic("chevd", 12, "var(--fg3)"),
                        "По локали ru_RU"),
                   crow("Обновления утилиты", tgl(False), "Раз в сутки · github.com"),
                   crow("Обновления моделей", tgl(False), "Раз в сутки · huggingface.co"),
               ]), ic("info", 15, "var(--fg3)")))
    actions = btn("Пропустить", "gh") + " " + btn("Продолжить", "pri")
    leg = ("Оба тумблера <b>пусты</b> (решение заказчика 2026-09-07, во всех профилях). Мастер в направлении C "
           "компактный — 420×560, под ту же плотность, что и поповер.")
    return page("C · Пульт — Онбординг 1",
                '<b>C · Пульт</b> — онбординг 1/5: приветствие и сеть · 420×560',
                onb_shell(1, body, actions), "light", CSS_C, leg)


def onb_hotkey():
    body = ('<div class="h3" style="font-size:18px">Горячая клавиша</div>'
            '<div class="c12" style="margin:4px 0 14px;line-height:1.45">Зажмите её и говорите. Отпустили — '
            'текст появится там, где стоит курсор.</div>%s'
            '<div class="grp" style="margin:14px 0 6px">Новая комбинация</div>'
            '<div class="field cap"><span class="mono" style="font-size:14px;color:var(--fg1)">Alt + F2</span>'
            '<span style="flex:1"></span><span class="c12">Esc — отмена</span></div>'
            '<div class="note w" style="margin-top:10px;font-size:12px">%s<div><b>Занята в KDE</b>'
            'Alt + F2 — действие «Показать KRunner». Оставить можно, но диктовка может не сработать.</div></div>'
            '<div style="display:flex;gap:8px;margin-top:10px">%s%s</div>'
            % (card([
                crow("Текущая комбинация", key("Ctrl + Space"), "По умолчанию"),
                crow("Режим", '<span class="seg"><span class="on">Удерживать</span><span>Нажать</span></span>',
                     "Push-to-talk"),
            ]), ic("alert", 15, "var(--warn-ink)"),
                btn("Оставить", "sm"), btn("Выбрать другую", "sm pri")))
    actions = btn("Назад", "gh") + " " + btn("Продолжить", "pri")
    leg = ("<b>Состояния поля захвата:</b> покой · захват (рамка акцентом, «Esc — отмена») · конфликт с KDE "
           "(предупреждение с именем системного действия, не запрет — PRD S1-A4).")
    return page("C · Пульт — Онбординг 3",
                '<b>C · Пульт</b> — онбординг 3/5: горячая клавиша и захват · 420×560',
                onb_shell(3, body, actions), "light", CSS_C, leg)


# ── 5. Пилюля ──────────────────────────────────────────────────────────────
def pills():
    scene = ('<div class="desk" style="width:900px;height:400px">'
             '<div class="dwin" style="left:70px;top:32px;width:520px;height:260px">'
             '<div class="dt">Письмо — без имени — Kate</div>'
             '<div class="dc">Добрый день, Сергей Петрович!<br><br>По итогам совещания направляю сводку: '
             'сроки по первому этапу сдвигаются на неделю, смета остаётся прежней. Прошу подтвердить'
             '<span style="border-left:1.5px solid #1B3A73;margin-left:1px">&nbsp;</span></div></div>'
             '<div style="position:absolute;right:18px;bottom:54px">%s</div>'
             '<div class="panel"><span class="pb">Kate</span><span class="pb">Firefox</span>'
             '<span class="sp"></span><span style="display:inline-flex;align-items:center;gap:9px;'
             'padding-right:6px">%s%s</span><span class="pb mono">14:26</span></div></div>'
             % (pill("listening", None, "c", .8), tray("listening", 17, "#C4CDDC"),
                tray("idle", 17, "#8C97AC")))
    body = (scene + '<div class="cap" style="margin-top:6px">Все состояния пилюли (F3.2)</div>'
            + pill_gallery("c", 900, .8))
    leg = ("<b>Отличие направления:</b> пилюля компактная (30 px) и живёт <b>у значка в трее</b>, а не по центру "
           "экрана — тот же «пульт», только свёрнутый; положение (снизу по центру / сверху / у трея / выключена) "
           "остаётся настройкой. Состояние передаётся формой и подписью, не только цветом.")
    return page("C · Пульт — Пилюля", '<b>C · Пульт</b> — пилюля-оверлей у трея + все состояния',
                body, "light", CSS_C, leg)


# ── 6. Трей ────────────────────────────────────────────────────────────────
def menu(rec=False):
    if rec:
        hd = ('<div class="mi hd" style="color:var(--accent-ink)">%s Слушаю…</div>'
              % tray("listening", 15, "var(--accent)"))
        first = '<div class="mi hl">Отмена записи</div>'
        copy = '<div class="mi dis">Скопировать последний текст</div>'
    else:
        hd = '<div class="mi hd">%s Готов · Ctrl + Space</div>' % tray("idle", 15, "var(--fg3)")
        first = '<div class="mi dis">Отмена записи</div>'
        copy = '<div class="mi">Скопировать последний текст</div>'
    return ('<div class="menu">%s<div class="msep"></div>'
            '<div class="mi">Открыть пульт<span class="sp"></span><span class="sc">клик</span></div>'
            '%s%s<div class="msep"></div>'
            '<div class="mi">Все настройки…<span class="sp"></span><span class="sc">Ctrl+,</span></div>'
            '<div class="mi dis">Проверить обновления</div><div class="mi">О программе</div>'
            '<div class="msep"></div>'
            '<div class="mi">Выход<span class="sp"></span><span class="sc">Ctrl+Q</span></div></div>'
            % (hd, first, copy))


def trays():
    icons = "".join('<div class="cell">%s<span class="cn">%s</span></div>'
                    % ('<span style="background:#0B1220;border-radius:6px;padding:7px;display:inline-flex">%s</span>'
                       % tray(s, 20, "#C4CDDC"), n)
                    for s, n in [("idle", "готов"), ("listening", "слушаю"), ("processing", "распознаю"),
                                 ("done", "готово"), ("error", "ошибка")])
    two = ('<div style="display:flex;gap:24px;align-items:flex-start">'
           '<div><div class="cap" style="margin-bottom:7px">Правый клик — в покое</div>%s</div>'
           '<div><div class="cap" style="margin-bottom:7px">Правый клик — во время записи</div>%s</div>'
           '<div class="gal" style="align-self:flex-start">%s</div></div>' % (menu(False), menu(True), icons))
    scene = ('<div class="desk" style="width:900px;height:344px">'
             '<div style="position:absolute;right:16px;bottom:52px">%s</div>'
             '<div class="panel"><span class="pb">Kate</span><span class="sp"></span>'
             '<span style="display:inline-flex;align-items:center;gap:9px;padding-right:6px">%s%s</span>'
             '<span class="pb mono">14:26</span></div></div>'
             % (menu(False), tray("idle", 17, "#F2F5FA"), tray("idle", 17, "#8C97AC")))
    leg = ("<b>Отличие направления:</b> левый клик по значку открывает <b>пульт</b> (главный экран), правый — "
           "это меню; «Все настройки…» ведут во второй уровень. Значок обязателен всегда, даже с выключенной "
           "пилюлей (запрет скрытой записи, §9.5).")
    return page("C · Пульт — Трей", '<b>C · Пульт</b> — значок и меню в системном трее',
                scene + two, "light", CSS_C, leg)


def build():
    return [write("C/01-popover.html", popover("light")),
            write("C/01-popover-dark.html", popover("dark")),
            write("C/02-models.html", models("light")),
            write("C/02-models-dark.html", models("dark")),
            write("C/03-updates.html", updates(False)),
            write("C/03-updates-admin.html", updates(True)),
            write("C/04-onboarding-network.html", onb_network()),
            write("C/04-onboarding-hotkey.html", onb_hotkey()),
            write("C/05-pill.html", pills()),
            write("C/06-tray.html", trays())]
