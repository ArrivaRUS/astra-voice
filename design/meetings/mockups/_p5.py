"""Направление A «Документ» к G3 (решение владельца на G2b, 12.10.2026)."""
from _base import *
from _p3 import TITLE, LINES, NAMES, NUM, SPK, table, marks

NAMED = {**NAMES, 's2': 'Олег Ли'}


def head(bdg=None, sub_extra=' · <span class="t">звук удалён</span>', listen=False, actions=True):
    bdg = bdg or badge('Готово', 'b-ok', 'check')
    pl = btn('Прослушать', '', 'play') if listen else ''
    act = f'<div style="display:flex;gap:8px">{pl}{btn("Сохранить как…", "pri", "save")}{btn("", "icon", "more")}</div>' if actions else ''
    return (f'<div style="font-size:13px;color:var(--primary);display:flex;align-items:center;gap:4px;margin-bottom:6px">{ico("chevl","ico","width:13px;height:13px")}Все встречи</div>'
            f'<div class="shead" style="margin-bottom:0"><div class="h" style="min-width:0"><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><div class="h2">{TITLE}</div>'
            f'<span class="mut">{ico("edit","ico","width:14px;height:14px")}</span>{bdg}</div>'
            f'<div class="ssub">09.10.2026 · 10:00–11:12 · <span class="mono">1:12:40</span> · Звонок в DION{sub_extra}</div></div>{act}</div>{marks()}')


def ppanel(w=232, state='ready', drawer=False):
    """Панель «Участники». state: ready | edit | live | merge."""
    items = ''
    for k, lab, mins, q, nm in SPK:
        if state == 'merged' and k == 's3':
            continue
        if k == 'me':
            items += (f'<div style="padding:9px 12px;border-top:1px solid var(--border-soft)"><div style="display:flex;gap:6px;align-items:baseline">'
                      f'<b style="font-weight:500;color:var(--accent-ink)">Я</b><span class="mut mono" style="font-size:11.5px;margin-left:auto">{mins}</span></div>'
                      f'<div class="mut" style="font-size:11.5px">Ваш микрофон — подпись из настроек</div></div>')
            continue
        if state == 'merged' and k == 's3':
            continue
        if state == 'live':
            fld = '<div class="field dis" style="padding:4px 8px;font-size:12.5px">Как подписать</div>'
        elif state == 'edit' and k == 's2':
            fld = '<div class="field foc" style="padding:4px 8px;font-size:12.5px">Олег Ли<span style="border-left:1.5px solid var(--fg1);height:14px;margin-left:-6px"></span></div>'
        else:
            v = {'s1': 'Ирина Соколова', 's2': 'Олег Ли' if state in ('ready', 'merge', 'merged') else '', 's3': ''}[k]
            fld = f'<div class="field{" ph" if not v else ""}" style="padding:4px 8px;font-size:12.5px">{E(v) if v else "Как подписать"}</div>'
        hl = 'background:var(--primary-bg);' if (state == 'merge' and k == 's3') else ''
        mm = '—' if state == 'live' else mins
        items += (f'<div style="padding:9px 12px;border-top:1px solid var(--border-soft);display:flex;flex-direction:column;gap:5px;{hl}">'
                  f'<div style="display:flex;gap:6px;align-items:baseline"><b style="font-weight:500"><span class="sn">{NUM[k]}</span>{lab}</b><span class="mut mono" style="font-size:11.5px;margin-left:auto">{mm}</span></div>'
                  f'<div class="mut" style="font-size:11.5px;line-height:1.4">«{E(q)}»</div>{fld}</div>')
    foot = ('<div style="padding:8px 12px 10px;border-top:1px solid var(--border-soft);font-size:11.5px;color:var(--fg3)">Имена можно вписать после окончания расшифровки — номера спикеров до конца могут измениться.</div>'
            if state == 'live' else
            f'<div style="padding:8px 12px;border-top:1px solid var(--border-soft)">{btn("Это один человек…", "gh sm", "merge")}</div>'
            '<div style="padding:0 12px 10px;font-size:11.5px;color:var(--fg3)">Имена действуют только в этой встрече.</div>')
    x = f'<span style="margin-left:auto">{btn("", "gh icon sm", "x")}</span>' if drawer else f'<span style="margin-left:auto" class="mut">{ico("people")}</span>'
    st = 'border:0;border-radius:0;background:transparent;' if drawer else ''
    return (f'<div class="card" style="width:{w}px;flex:none;align-self:flex-start;{st}"><div style="padding:9px 12px;display:flex;align-items:center;gap:6px">'
            f'<span class="h3" style="font-size:14px">Участники</span>{x}</div>{items}{foot}</div>')


def strip(live=False):
    chips = ''.join(f'<span style="display:inline-flex;align-items:center;font-size:12.5px;white-space:nowrap">{"<span class=sn>" + NUM[k] + "</span>" if k in NUM else ""}'
                    f'{E({"me": "Я", "s1": "Ирина Соколова", "s2": "Олег Ли", "s3": "Спикер 3"}[k])}</span>' for k, *_ in SPK)
    b = btn('Имена участников', 'sm dis' if live else 'sm', 'people')
    return (f'<div class="card" style="padding:7px 10px 7px 12px;display:flex;gap:12px;align-items:center;margin-bottom:10px;flex-wrap:wrap">'
            f'<span class="mut" style="font-size:12px">Участники:</span>{chips}<span style="flex:1"></span>{b}</div>')


def live_bar():
    return (f'<div class="card" style="padding:10px 12px;display:flex;gap:12px;align-items:center;margin-bottom:10px">'
            f'<span style="color:var(--primary)">{ico("clock")}</span><div style="flex:1;min-width:0"><div style="font-size:13.5px">Расшифровано <span class="mono">0:29:04</span> из <span class="mono">1:12:40</span> · имена спикеров уточняются до конца</div>'
            f'<div class="mut" style="font-size:12px">Готовые реплики появляются сразу. Можно закрыть окно или выйти из программы — расшифровка продолжится.</div>'
            f'<div class="prog" style="margin-top:6px"><i style="width:40%"></i></div></div>{btn("Отменить", "sm")}</div>')


LIVE_TAIL = ('<div class="tr" style="color:var(--fg3)"><span class="tc">…</span><span></span>'
             '<span style="font-size:12.5px">Дальше — по мере расшифровки</span></div>')


def body(layout='panel', state='ready', width=1024, height=620, extra='', listen=False, toast=''):
    names = NAMES if state == 'live' else (NAMED if state != 'edit' else {**NAMES})
    if state == 'live':
        hd = head(badge('Расшифровка · <span class="mono">40 %</span>', 'b-warn', 'clock'), ' · <span class="t">звук хранится до конца расшифровки</span>', actions=False)
        tbl = table(LINES[:7], {**NAMES, 's1': 'Спикер 1'}, tail=LIVE_TAIL)
        pre = live_bar()
    else:
        sub = ' · <span class="t">звук хранится до 19.10</span>' if listen else ' · <span class="t">звук удалён</span>'
        hd = head(sub_extra=sub, listen=listen)
        lines = LINES
        if state == 'merged':
            lines = [(t, 's1' if w == 's3' else w, x) for t, w, x in LINES]
        tbl = table(lines, names if state != 'edit' else {**NAMES, 's2': 'Олег Ли'})
        pre = ''
    pst = {'live': 'live', 'edit': 'edit', 'merge': 'merge', 'merged': 'merged'}.get(state, 'ready')
    if layout == 'panel':
        main = f'{pre}<div style="display:flex;gap:12px;align-items:flex-start"><div style="flex:1;min-width:0">{tbl}</div>{ppanel(232, pst)}</div>'
    else:
        main = pre + strip(state == 'live') + tbl
        if layout == 'drawer':
            main += f'<div class="drawer" style="padding:0">{ppanel(300, pst, drawer=True)}</div>'
    t = f'<div style="position:absolute;left:50%;bottom:14px;transform:translateX(-50%);z-index:5">{toast}</div>' if toast else ''
    return window(hd + main + extra + t, width=width, height=height)


def merge_pop():
    return (f'<div class="card" style="position:absolute;right:262px;top:330px;width:300px;border-radius:8px;padding:12px;display:flex;flex-direction:column;gap:8px;box-shadow:0 12px 34px var(--shadow);z-index:6">'
            f'<div style="font-size:14px;font-weight:500"><span class="sn">3</span>Спикер 3 — это…</div>'
            f'<div class="mut" style="font-size:12.5px">Выберите, кто это на самом деле. Реплики получат одну подпись, статистика пересчитается.</div>'
            f'<span class="sel"><span><span class="sn">1</span>Ирина Соколова</span>{ico("chevd")}</span><div style="font-size:12px" class="warn-t">{ico("alert","ico","width:12px;height:12px")} Разделить обратно будет нельзя</div>'
            f'<div style="display:flex;gap:8px;justify-content:flex-end">{btn("Отмена", "sm focus")}{btn("Объединить", "pri sm")}</div></div>')


def more_menu():
    return (f'<div class="tmenu" style="position:absolute;right:22px;top:76px;width:236px;z-index:6">'
            f'<div class="mi">{ico("edit")}Переименовать…</div><div class="mi">{ico("people")}Имена участников</div>'
            f'<div class="sepm"></div><div class="mi err-t">{ico("trash")}Удалить встречу…</div></div>')


def player():
    return (f'<div class="card" style="padding:8px 12px;display:flex;gap:10px;align-items:center;margin:0 0 10px">{btn("", "pri icon", "pause")}'
            f'<span class="mono" style="font-size:12px">0:12:03</span><span class="prog" style="flex:1;height:6px"><i style="width:17%"></i></span>'
            f'<span class="mono mut" style="font-size:12px">1:12:40</span><span class="mut" style="font-size:12px">Звук удалится 19.10.2026</span></div>')


def pA():
    b = ['<div class="note ok" style="align-self:stretch">' + ico('check') + '<div><b>Решение G2b (12.10.2026):</b> направление A «Документ» + номер спикера у каждой реплики (из B). '
         'B и C сняты. В2 (плашка сверху по центру) закреплена владельцем; В3 и В4 решены владельцем; допущение В5 ждёт ответа — spec.md §0, §8.</div></div>']
    b.append('<div class="sec">1 · Готово — 1024×620: таблица реплик + панель «Участники» справа</div>')
    b.append(body())
    b.append('<div class="cap"><b>Номер спикера</b> — у каждой реплики и в панели (нейтральный кружок, без новой палитры): с именем «① Ирина Соколова», без — «② Спикер 2». «Я» — акцент <b>и</b> слово, без номера. '
             'Шапка: «Прослушать» (только пока звук хранится) · «Сохранить как…» (главное) · «⋯» (Переименовать, Имена участников, Удалить).</div>')
    b.append('<div class="sec">2 · Готово — 900×588: панель складывается в полосу «Участники» над таблицей; имена — в выезжающей панели</div>')
    b.append(f'<div class="grid2" style="grid-template-columns:1fr">{body("strip", width=900, height=588)}</div>')
    b.append(body('drawer', 'edit', width=900, height=588))
    b.append('<div class="cap"><b>Правило ширины:</b> контент ≥ 760 px (окно ≥ ≈ 990) — панель справа 232; меньше — полоса над таблицей (одна строка, переносится) + кнопка «Имена участников», '
             'которая открывает панель 300 px поверх правого края (тень, «×», Esc закрывает, фокус — в первое поле имени). Пункт «Имена участников» в «⋯» делает то же на любой ширине. '
             'Правка имени применяется сразу (таблица и шапка), без «Сохранить»; подсказок из прошлых встреч нет (П15).</div>')
    b.append('<div class="sec">3 · Расшифровка идёт — текст по ходу (В4, решение владельца), имена закрыты до конца (T1 BL-3)</div>')
    b.append(body('panel', 'live'))
    b.append(body('strip', 'live', width=900, height=588))
    b.append('<div class="cap">Реплики появляются по мере готовности; подписи — «Спикер N» без имён, номера могут смениться к концу. Поля имён и «Это один человек» недоступны до окончания. '
             '«Сохранить как…» нет, пока расшифровка не закончена. Выход из программы во время расшифровки — без подтверждения: она продолжится при следующем запуске.</div>')
    b.append('<div class="sec">4 · «Это один человек» — выбор и результат</div>')
    b.append(body('panel', 'merge', extra=merge_pop()))
    toast = f'<span class="toast" style="--toast-max:752px">{ico("check")}Спикер 3 объединён с «Ирина Соколова» · статистика пересчитана</span>'
    b.append(body('panel', 'merged', toast=toast))
    b.append('<div class="cap">Фокус в поповере — на «Отмена». Обратного действия нет (вне объёма, PRD F18.9 (д)) — поэтому строка-предупреждение прямо в поповере, без отдельного диалога. После объединения «Спикер 3» исчезает из панели.</div>')
    b.append('<div class="sec">5 · Меню «⋯», «Прослушать» (только audio-kept), сохранено</div>')
    saved = f'<span class="toast" style="--toast-max:752px">{ico("check")}<span>Сохранено в «Документы»:</span><span style="overflow:hidden;min-width:0">Планёрка по бюджету.md</span>{btn("Открыть папку", "sm")}</span>'
    longt = (f'<span class="toast" style="--toast-max:628px">{ico("check")}<span>Сохранено в «Документы»:</span>'
             f'<span style="overflow:hidden;min-width:0">Планёрка по бюджету четвёртого квартала с подр…ками и ИТ.md</span>{btn("Открыть папку", "sm")}</span>')
    b.append(body(extra=more_menu()))
    b.append(body(listen=True, extra='', toast=saved).replace(marks(), marks() + player(), 1))
    b.append(f'<div class="tile" style="align-self:stretch;align-items:center;background:var(--bg-app)"><div class="tl" style="align-self:stretch">Тост с длинным именем файла (окно 900: max 628)</div>{longt}</div>')
    b.append('<div class="cap"><b>Тост</b> — всегда одна строка, по центру низа контента; max-width = ширина контента − 2×22 (752 при 1024, 628 при 900). '
             'Не помещается — сокращается <b>только имя файла</b>, многоточием в середине (расширение видно); «Сохранено в …» и кнопка не сокращаются.</div>')
    b.append('<div class="cap">«Прослушать» есть только пока звук хранится (настройка «Хранить запись звука» 7/30 дней или политика); по умолчанию звук удаляется после расшифровки — кнопки нет. '
             'Плеер — Could: одна строка над таблицей, пауза/время/полоса, без скорости. Тост «Сохранено…» — после системного диалога, 4 с; путь не показывается, только имя папки.</div>')
    b.append('<div class="legend">Переименование, удаление, состояния ошибок и отсутствия модели — <a href="06-meeting-states.html">06</a>. «Сохранить как…» — наш шаг перед системным диалогом — <a href="07-templates.html">07</a>.</div>')
    return page('Встречи — карточка встречи', '05 · Карточка встречи — A «Документ» (к G3)', 'US-13.7, 13.8, 13.11. Геометрия — spec.md §5.2, тексты — §4.7, состояния — §3.7.', ''.join(b), NAV)
