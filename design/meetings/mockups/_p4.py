from _base import *
from _p3 import TITLE, table, LINES, NAMES, participants_panel


def state_head(bdg, sub):
    return (f'<div style="font-size:13px;color:var(--primary);display:flex;align-items:center;gap:4px;margin-bottom:6px">{ico("chevl","ico","width:13px;height:13px")}Все встречи</div>'
            f'<div style="display:flex;align-items:center;gap:8px"><div class="h2">{TITLE}</div>{bdg}</div><div class="ssub" style="margin-bottom:12px">{sub}</div>')


def panel(icon, kind, title, text, buttons=(), prog=None):
    p = f'<div class="prog" style="margin-top:4px"><i style="width:{prog}%"></i></div>' if prog is not None else ''
    bs = ''.join(btn(t, k) for t, k in buttons)
    col = {'ok': 'var(--ok-ink)', 'warn': 'var(--warn-ink)', 'err': 'var(--err-ink)', 'n': 'var(--fg3)', 'pri': 'var(--primary)'}[kind]
    return (f'<div class="card" style="padding:16px 18px;display:flex;gap:14px;align-items:flex-start"><span style="color:{col}">{ico(icon,"ico","width:22px;height:22px")}</span>'
            f'<div style="flex:1;display:flex;flex-direction:column;gap:6px"><div style="font-size:15px;font-weight:500">{title}</div>'
            f'<div style="font-size:13px;color:var(--fg2)">{text}</div>{p}{"<div style=display:flex;gap:8px;margin-top:4px>" + bs + "</div>" if bs else ""}</div></div>')


def frame(c, h=None):
    return f'<div class="win" style="width:100%;{f"height:{h}px;" if h else ""}"><div class="content" style="padding:14px 18px">{c}</div></div>'


def dialog(title, body, buttons, w=420, icon=None, icls=''):
    i = f'<span class="{icls}">{ico(icon,"ico","width:22px;height:22px")}</span>' if icon else ''
    return (f'<div class="dlg" style="width:{w}px"><div class="dh">{mark()}{E(title)}</div><div class="db"><div style="display:flex;gap:13px">{i}<div style="flex:1;display:flex;flex-direction:column;gap:8px">{body}</div></div></div>'
            f'<div class="df"><span class="sp"></span>{"".join(btn(t, k) for t, k in buttons)}</div></div>')


def p06():
    meta = '09.10.2026 · 10:00 · <span class="mono">1:12:40</span> · Звонок в DION'
    tiles = [
        ('Расшифровка идёт <code>transcribing</code>', state_head(badge('Расшифровка · <span class="mono">40 %</span>', 'b-warn', 'clock'), meta) +
         panel('clock', 'pri', 'Расшифровываю встречу · <span class="mono">40 %</span>', 'Можно закрыть окно — расшифровка продолжится в фоне, диктовка работает. Текст появляется по ходу — вид в <a href=05-meeting-A.html>05</a>, §3.', [('Отменить', '')], 40)),
        ('Отменено <code>cancelled</code> = «Записана»', state_head(badge('Записана', 'b-n'), meta) +
         panel('info', 'n', 'Расшифровка отменена', 'Запись сохранена — можно начать снова.', [('Расшифровать', 'pri'), ('Удалить встречу…', '')])),
        ('Нет модели <code>no-model</code>', state_head(badge('Нужна модель', 'b-warn', 'alert'), meta) +
         panel('chip', 'warn', 'Для расшифровки встреч нужна модель GigaAM v3', 'Запись сохранена. После установки расшифровка начнётся сама.', [('Установить', 'pri'), ('Установить из файла…', '')])),
        ('Мало памяти <code>wait-memory</code>', state_head(badge('Ждёт памяти', 'b-warn', 'clock'), meta) +
         panel('clock', 'warn', 'Расшифровка подождёт: сейчас мало свободной памяти', 'Начнётся сама, когда памяти станет достаточно. Можно закрыть другие программы.', [('Отменить', '')])),
        ('Самопроверка модели <code>error</code>', state_head(badge('Ошибка расшифровки', 'b-err', 'alert'), meta) +
         panel('alert', 'err', 'Распознавание на этом компьютере не работает', 'Запись сохранена и не удалится, пока вы её не удалите. Подробности — в разделе «Модели».', [('Повторить', 'pri'), ('Подробнее', '')])),
        ('Прервана при записи <code>interrupted</code>', state_head(badge('Прервана', 'b-warn', 'alert'), '07.10.2026 · 09:00 · сохранено <span class="mono">0:37:02</span>') +
         panel('alert', 'warn', 'Запись встречи прервалась. Сохранено <span class="mono">0:37:02</span>', 'Программа закрылась во время записи. Последние секунды могли потеряться.', [('Расшифровать', 'pri'), ('Удалить…', '')])),
        ('Речи нет <code>empty</code>', state_head(badge('Готово', 'b-ok', 'check'), meta) +
         panel('mic', 'n', 'В записи не нашлось речи', 'Проверьте, что выбран нужный микрофон, и что звук программы видеосвязи был слышен.', [('Удалить встречу…', '')])),
        ('Политика `off` <code>read-only</code>', state_head(badge('Готово', 'b-ok', 'check'), meta) +
         f'<div class="note">{ico("lock")}<div>Запись встреч отключена администратором. Встречу можно прочитать, сохранить как файл и удалить.</div></div><div style="height:8px"></div>' + table(LINES[:3], {**NAMES, 's1': 'Спикер 1'})),
        ('Встреча в комнате <code>room</code>', state_head(badge('Готово', 'b-ok', 'check'), '10.10.2026 · 14:00 · <span class="mono">0:41:12</span> · Встреча в комнате') +
         table([('0:00:03', 's1', 'Начнём? Сегодня три вопроса.'), ('0:00:09', 's2', 'Да, у меня первый — по отпускам.'), ('0:00:15', 's3', 'Я тоже по графику.')], {'s1': 'Спикер 1', 's2': 'Спикер 2', 's3': 'Спикер 3'}) +
         '<div class="tn mut" style="font-size:12px;margin-top:6px">Одна дорожка: «Я» определить нельзя — все «Спикер N».</div>'),
        ('Разделение выключено <code>no-diarization</code>', state_head(badge('Готово', 'b-ok', 'check'), meta) +
         table(LINES[:5], {'me': 'Я', 's1': 'Собеседники', 's2': 'Собеседники', 's3': 'Собеседники'}) +
         '<div class="mut" style="font-size:12px;margin-top:6px">Настройкой или `meeting_diarization=off`. В комнате — «Участники». Поля имён нет.</div>'),
        ('Записано с предупреждением <code>warn-marks</code>', state_head(badge('Готово', 'b-ok', 'check'), meta) +
         '<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">' + badge('Хранится на сетевом диске', 'b-warn', 'alert') + badge('Звук шёл через другой компьютер', 'b-warn', 'alert') + '</div>'
         '<div class="mut" style="font-size:12px">Отметки ставятся, если при старте было предупреждение (решение владельца 12.10); они же — в шапке файла расшифровки.</div>'),
        ('Звук хранится <code>audio-kept</code> · Could', state_head(badge('Готово', 'b-ok', 'check'), meta + ' · <span class="t">звук хранится до 19.10</span>') +
         f'<div style="display:flex;gap:8px;align-items:center">{btn("Прослушать", "", "play")}<span class="mut" style="font-size:12px">Звук удалится 19.10.2026 — так задано в «Хранении»</span></div>'),
    ]
    b = ['<div class="sec">Состояния карточки встречи — общие для направлений A / B / C (нарисованы в рамке A)</div>']
    b.append('<div class="grid2" style="align-items:start">' + ''.join(f'<div class="tile" style="background:var(--bg-app)"><div class="tl">{t}</div>{frame(c)}</div>' for t, c in tiles) + '</div>')
    # «Это один человек» — актуальный вид в 05-meeting-A.html §4 (старая плитка удалена, tokens.md №31)
    b.append('<div class="sec">Диалоги: удалить (D2) и переименовать (D3) — из «⋯» и по значку карандаша у названия</div>')
    d2 = dialog('Удалить встречу', f'<div style="font-weight:500">Удалить встречу «{TITLE}»?</div><div style="font-size:13px;color:var(--fg2)">Удалятся запись звука, расшифровка и все её файлы в программе. Файлы, которые вы сохранили в другие папки, останутся — удалите их сами.</div>',
                [('Отмена', 'focus'), ('Удалить встречу', 'dan')], icon='trash', icls='err-t')
    d2ai = dialog('Удалить встречу', f'<div style="font-weight:500">Удалить встречу «{TITLE}»?</div><div style="font-size:13px;color:var(--fg2)">Удалятся запись звука, расшифровка и все её файлы в программе. Файлы, которые вы сохранили в другие папки, останутся — удалите их сами.</div>'
                  '<div class="note warn" style="margin-top:2px">' + ico('globe') + '<div>Текст встречи отправлялся в сервис ИИ (<span class="mono">api.example.ru</span>) — копия могла остаться у владельца сервиса.</div></div>',
                  [('Отмена', 'focus'), ('Удалить встречу', 'dan')], icon='trash', icls='err-t')
    d3 = dialog('Переименовать встречу', '<div style="font-size:13px">Название встречи</div><div class="field foc">Планёрка по бюджету</div>', [('Отмена', ''), ('Сохранить', 'pri')], w=380)
    b.append(f'<div class="grid3" style="align-items:start"><div class="tile">{d2}</div><div class="tile">{d2ai}<div class="tn">Если была хотя бы одна попытка отправки в ИИ (отметка ставится с начала передачи — MJ-13).</div></div><div class="tile">{d3}</div></div>')
    return page('Встречи — состояния карточки', '06 · Состояния карточки встречи', 'US-13.7, 13.8, 13.11. Тексты — spec.md §4.7. Состояния — §3.7.', ''.join(b), NAV)


AI_NOSAVE = ('ready', 'ready-plain', 'ready-ext', 'sending', 'err-net', 'err-auth', 'err-time', 'err-redirect', 'too-long', 'empty', 'offline', 'maybe', 'addr-changed')
TPLS = [('doc', 'Стенограмма', 'Реплики по порядку, с именами и временем'), ('meeting', 'Я и собеседники', 'Только две подписи — без разделения собеседников'),
        ('file', 'Только текст', 'Абзацами, без имён и времени'), ('chart', 'Статистика встречи', 'Кто сколько говорил')]
AIT = [('Краткое содержание', 'Главное из встречи в нескольких абзацах'), ('Протокол: решения и поручения', 'Что решили, кто и что делает, сроки')]


def stats():
    rows = [('Ирина Соколова', 24, 33), ('Олег Ли', 21, 29), ('Я', 18, 25), ('Спикер 3', 9, 13)]
    r = ''.join(f'<div style="display:grid;grid-template-columns:minmax(0,1fr) minmax(40px,1fr) 52px 40px;gap:10px;align-items:center;font-size:13px"><span>{n}</span>'
                f'<span class="prog" style="height:8px"><i style="width:{p}%"></i></span><span class="mono mut" style="font-size:12px">{m} мин</span><span class="mono" style="font-size:12px">{p} %</span></div>' for n, m, p in rows)
    return (f'<div class="card" style="padding:14px 16px;display:flex;flex-direction:column;gap:9px"><div style="font-size:13px" class="mut">Длительность <span class="mono">1:12:40</span> · реплик <span class="mono">214</span> · сумма долей <span class="mono">100 %</span></div>{r}</div>')


def tpl_window(sel=0, ai_state=None, preview=None, w=760, h=None, host='llm.corp.local', internal=True, ai=True):
    items = ''
    for i, (ic, t, d) in enumerate(TPLS):
        on = sel == i
        items += (f'<div style="display:flex;gap:10px;padding:8px 10px;border-radius:7px;{"background:var(--primary-bg);" if on else ""}">'
                  f'<span style="color:{"var(--primary)" if on else "var(--fg3)"};margin-top:2px">{ico(ic)}</span><div><div style="font-size:13.5px;{"font-weight:500" if on else ""}">{t}</div>'
                  f'<div style="font-size:11.5px;color:var(--fg3);line-height:1.35">{d}</div></div></div>')
    if ai:
        items += f'<div class="grp" style="margin:12px 0 4px 10px">С ИИ · <span class="mono" style="text-transform:none;letter-spacing:0">{host}</span></div>'
        for i, (t, d) in enumerate(AIT):
            on = sel == 10 + i
            dis = ai_state == 'offline'
            items += (f'<div style="display:flex;gap:10px;padding:8px 10px;border-radius:7px;{"background:var(--primary-bg);" if on else ""}">'
                      f'<span style="color:{"var(--primary)" if on else "var(--fg3)"};margin-top:2px">{ico("spark")}</span><div><div style="font-size:13.5px;{"color:var(--fgdis)" if dis else ""}">{t}</div>'
                      f'<div style="font-size:11.5px;color:var(--fg3);line-height:1.35">{"Недоступно в офлайн-режиме" if dis else d}</div></div></div>')
    right = preview or ''
    lw = 250 if w >= 700 else 190
    return (f'<div class="dlg" style="width:{w}px;{f"height:{h}px;" if h else ""}display:flex;flex-direction:column"><div class="dh">{mark()}Сохранить как — {TITLE}</div>'
            f'<div style="display:flex;flex:1;min-height:0"><div style="width:{lw}px;border-right:1px solid var(--border);padding:10px 8px;flex:none">{items}</div>'
            f'<div style="flex:1;padding:14px 16px;display:flex;flex-direction:column;gap:10px;min-width:0">{right}</div></div>'
            f'<div class="df" style="border-top:1px solid var(--border-soft);flex-wrap:wrap"><span class="mut" style="font-size:12px;flex:1 1 260px">За файлами, сохранёнными вне программы, программа не следит — удалите их сами, если нужно. Файл будет доступен только вашей учётной записи.</span>'
            f'{btn("Отмена", "")}{btn("Выбрать папку и сохранить…", "dis" if ai_state in AI_NOSAVE else "pri", "save")}</div></div>')


def loc_preview(name, body):
    fmt = ('<span style="display:inline-flex;border:1px solid var(--border);border-radius:7px;overflow:hidden"><span style="font-size:12.5px;padding:4px 10px;background:var(--primary);color:var(--primary-fg)">.md</span>'
           '<span style="font-size:12.5px;padding:4px 10px">.txt</span></span>')
    return f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><div class="h3" style="flex:1 1 140px">{name}</div>{fmt}</div>{body}'


def ai_preview(name, state, host='llm.corp.local'):
    hdr = f'<div class="h3">{name}</div>'
    where_int = f'<div style="display:flex;gap:8px;align-items:flex-start;font-size:12.5px"><span class="ok-t">{ico("home")}</span><span style="flex:1">Уйдёт на <span class="mono">{host}</span> — адрес внутри вашей сети</span></div>'
    where_ext = f'<div style="display:flex;gap:8px;align-items:center;font-size:12.5px"><span class="warn-t">{ico("globe")}</span><span style="flex:1">Уйдёт на <span class="mono">{host}</span> — внешний адрес, перед отправкой программа предупредит</span></div>'
    what = '<div class="mut" style="font-size:12px">Уходит только текст расшифровки этой встречи и инструкция шаблона. Звук и файлы — никогда.</div>'
    chk = f'<div style="display:flex;gap:8px;align-items:center;font-size:12.5px"><span class="chk"></span>Заменить имена участников на «Участник 1, 2…»</div>'
    if state == 'ready-plain':
        wp = (f'<div style="display:flex;gap:8px;align-items:flex-start;font-size:12.5px"><span class="warn-t">{ico("alert")}</span><span style="flex:1">Уйдёт на <span class="mono">10.0.5.20</span> — адрес внутри вашей сети, '
              f'<b>без шифрования, разрешено администратором</b></span></div>')
        return hdr + wp + chk + what + f'<div>{btn("Сделать", "pri", "spark")}</div>'
    if state == 'ready':
        return hdr + where_int + chk + what + f'<div>{btn("Сделать", "pri", "spark")}</div>'
    if state == 'ready-ext':
        return hdr + where_ext + what + f'<div>{btn("Сделать", "pri", "spark")}</div>'
    if state == 'sending':
        return hdr + where_int + what + (f'<div class="card" style="padding:12px 14px;display:flex;gap:10px;align-items:center"><span class="mut">{ico("spinner")}</span>'
                                          f'<div style="flex:1;font-size:13px;min-width:0">Жду ответ сервиса ИИ…</div>{btn("Отменить", "sm")}</div>')
    if state == 'done':
        return (hdr + where_int + '<div class="card" style="padding:12px 14px;font-size:13px;line-height:1.6"><div class="mut" style="font-size:12px;margin-bottom:4px">Ответ сервиса ИИ · 09.10.2026 11:20 · сохранён в данных встречи</div>'
                '<b>Решения.</b> 1. Перенести закупку серверов на январь [0:01:02].<br><b>Поручения.</b> 1. Олег Ли — пересчёт бюджета, до пятницы, отправить Марии [0:01:15].</div>'
                f'<div class="note warn">{ico("spark")}<div>Сделано сервисом ИИ по тексту встречи — проверьте по стенограмме. Ссылки и картинки из ответа убраны.</div></div>'
                f'<div style="display:flex;gap:8px">{btn("Сделать заново", "", "refresh")}</div>')
    errs = {'err-net': 'Не удалось связаться с сервисом ИИ: адрес недоступен. Текст встречи не отправлен',
            'err-auth': f'Сервис ИИ отказал в доступе — проверьте ключ доступа. Текст встречи мог быть получен сервером {host}',
            'err-time': f'Сервис ИИ не ответил за 3 минуты. Текст встречи мог быть получен сервером {host}',
            'err-redirect': f'Сервер перенаправил запрос на другой адрес — запрос остановлен. Текст встречи мог быть получен сервером {host}',
            'maybe': f'Связь оборвалась во время отправки. Текст встречи мог быть получен сервером {host}',
            'addr-changed': 'Адрес сервера изменился после проверки — отправка отменена. Текст встречи не отправлен',
            'too-long': 'Встреча слишком длинная для этой модели',
            'empty': 'Сервис ИИ вернул пустой ответ'}
    if state in errs:
        rb = '' if state in ('too-long', 'addr-changed') else btn('Повторить', 'sm')
        w_ = where_ext if host != 'llm.corp.local' else where_int
        return hdr + w_ + f'<div class="note err">{ico("alert")}<div style="flex:1">{errs[state]}{"<div style=margin-top:7px>" + rb + "</div>" if rb else ""}</div></div>'
    if state == 'offline':
        return hdr + f'<div class="note">{ico("globe")}<div>Недоступно в офлайн-режиме: адрес сервиса ИИ внешний. Выключить офлайн-режим можно в разделе «Сеть и обновления».</div></div>'
    return hdr


def d5():
    return dialog('Отправка текста встречи',
                  '<div style="font-size:13.5px">Текст встречи уйдёт на внешний сервер <b class="mono">api.example.ru</b>. Участники встречи на это согласия не давали. Отправляйте, только если это разрешено в вашей организации.</div>'
                  f'<div style="display:flex;gap:9px;align-items:flex-start;margin-top:2px"><span class="chk on" style="margin-top:2px">{ico("check")}</span><div><div style="font-size:13.5px">Заменить имена участников на «Участник 1, 2…»</div>'
                  '<div style="font-size:12px;color:var(--fg3)">Имена, произнесённые в разговоре, в тексте останутся</div></div></div>',
                  [('Отмена', 'focus'), ('Отправить', 'pri')], w=460, icon='globe', icls='warn-t')


def p07():
    b = ['<div class="sec">D4 «Сохранить как…» — наш шаг перед системным диалогом: шаблон, формат, предпросмотр</div>']
    sten = table(LINES[:6], {**NAMES, 's2': 'Олег Ли'})
    b.append(tpl_window(0, preview=loc_preview('Стенограмма', sten)))
    b.append('<div class="cap"><b>D4 «Сохранить как».</b> Открывается кнопкой «Сохранить как…» в карточке и пунктом списка; модален к окну настроек, 760. Слева — шаблоны (ИИ — группой с хостом), справа — предпросмотр того, что сохранится. '
             'Внизу — то, что системный диалог файлов сказать не может: за копиями программа не следит, файл создаётся только для вашей учётной записи (MN-15). «Выбрать папку и сохранить…» открывает системный диалог (KDE-портал / Qt во Fly), папка по умолчанию — «Документы», имя — из названия встречи. '
             'Формат `.md`/`.txt` (`.odt`/`.docx` — если архитектор найдёт лёгкий способ). '
             'Адреса ИИ нет или `meeting_ai_endpoint=off` → группы «С ИИ» нет вовсе.</div>')
    b.append('<div class="grid2" style="align-items:start">'
             f'<div class="tile"><div class="tl">Статистика встречи</div>{tpl_window(3, preview=loc_preview("Статистика встречи", stats()), w=500, ai=False)}</div>'
             f'<div class="tile"><div class="tl">Только текст</div>{tpl_window(2, preview=loc_preview("Только текст", "<div class=card style=padding:12px;font-size:13px;line-height:1.6>Коллеги, добрый день. Я веду запись и расшифровку этой встречи для протокола.<br><br>Добрый день. Тогда начнём с бюджета на четвёртый квартал…</div>"), w=500, ai=False)}</div></div>')
    b.append('<div class="sec">Шаблоны с ИИ — состояния</div>')
    sts = [('Внутренний адрес, готов <code>ready</code>', 'ready', 10), ('http:// разрешён администратором <code>ready-plain</code> · 7-й ключ', 'ready-plain', 10), ('Отправка <code>sending</code>', 'sending', 10), ('Ответ получен <code>done</code>', 'done', 11),
           ('Внешний адрес <code>ready-ext</code> → D5', 'ready-ext', 10), ('Ошибка сети <code>error</code>', 'err-net', 10), ('Доступ запрещён', 'err-auth', 10),
           ('Нет ответа 180 с', 'err-time', 10), ('Редирект на другой хост', 'err-redirect', 10), ('Обрыв во время отправки <code>maybe-sent</code> · MJ-13', 'maybe', 10),
           ('Адрес изменился между проверкой и отправкой', 'addr-changed', 10), ('Слишком длинная <code>too-long</code>', 'too-long', 11),
           ('Офлайн-режим, внешний адрес <code>offline</code>', 'offline', 10)]
    tiles = ''
    for t, s, sl in sts:
        host = 'api.example.ru' if s in ('ready-ext', 'offline') else ('10.0.5.20' if s == 'ready-plain' else 'llm.corp.local')
        extra = ('<div class="tn"><b>Существует только при седьмом ключе</b> (`meeting_ai_allow_plaintext=true` вместе с адресом из политики): '
                 'администратор разрешил `http://` для своего внутреннего адреса. Без ключа такой адрес не сохраняется — ошибка поля «http:// допустим только для этого компьютера» (01, блок 6).</div>'
                 if s == 'ready-plain' else '')
        tiles += (f'<div class="tile"><div class="tl">{t}</div>'
                  f'{tpl_window(sl, ai_state=s, preview=ai_preview(AIT[sl - 10][0], s, host), w=500, host=host)}{extra}</div>')
    b.append(f'<div class="grid2" style="align-items:start">{tiles}</div>')
    b.append('<div class="sec">D5 · Предупреждение перед каждой отправкой на внешний адрес (нет `meeting_ai_allow_external=true`)</div>')
    b.append(f'<div class="tile" style="align-self:stretch;align-items:center;background:var(--bg-app)">{d5()}</div>')
    b.append('<div class="cap"><b>Фокус на «Отмена»</b> — Enter не отправляет. Показывается перед <b>каждой</b> отправкой на внешний адрес, включая «Повторить» и «Сделать заново» (MJ-13). Галочка замены имён по умолчанию отмечена. «Отмена» → ничего не ушло. Запрос — только по нажатию, никогда в фоне и не после расшифровки автоматически. '
             'Для внутреннего адреса предупреждения нет, галочка — в самом шаблоне, снята.</div>')
    return page('Встречи — шаблоны', '07 · «Сохранить как…» и шаблоны: локальные и с ИИ', 'US-13.9, 13.10, 13.12. Тексты — spec.md §4.7, состояния — §3.8. Учтены T1 MJ-11, MJ-13, MJ-2, MN-15.', ''.join(b), NAV)


def index():
    rows = [('01-section.html', '01 · Раздел «Встречи»', 'выключено, политика, пусто, список, настройки, замки, ИИ-адрес', 'одно'),
            ('02-responsibility.html', '02 · Экран ответственности', 'личный / корпоративный / manual', 'одно'),
            ('03-offer.html', '03 · Предложение «Записать?»', 'DION, браузер, старт, когда не показывается', 'одно'),
            ('04-recording.html', '04 · Запись', 'карточка старта и отказы, плашка (9 состояний), участники, трей, уведомления, выход', 'одно'),
            ('05-meeting-A.html', '05 · Карточка встречи (A «Документ»)', 'готово 1024 и 900, расшифровка по ходу, имена, «Это один человек», меню, «Прослушать», сохранено', 'решено G2b'),
            ('06-meeting-states.html', '06 · Состояния карточки', 'расшифровка, нет модели, память, ошибка, прервана, комната, удалить, объединить', 'общие'),
            ('07-templates.html', '07 · Сохранить как и шаблоны', 'наш шаг перед системным диалогом, локальные шаблоны, ИИ (13 состояний), предупреждение о внешнем адресе', 'одно')]
    t = ''.join(f'<div class="r"><div class="left"><div class="lbl"><a href="{h}" style="color:var(--primary)">{E(n)}</a></div><div class="sub">{d}</div></div><span style="font-size:12.5px">{k}</span></div>' for h, n, d, k in rows)
    body = (f'<div class="card" style="align-self:stretch">{t}</div>'
            '<div class="legend">Спецификация — <code>design/meetings/spec.md</code>: флоу (§2), состояния (§3), точные тексты (§4), геометрия (§5), KDE/Fly (§6), клавиатура (§7), '
            'вопросы к заказчику (§8), находки по PRD/историям (§9). Все страницы — в светлой и тёмной теме (переключатель вверху, `#dark` в адресе). Без внешних зависимостей.</div>')
    body += ('<div class="legend">Направления B «Беседа» и C «Окно встречи» сняты 12.10.2026 (решение владельца на G2b) — '
             '<a href="05-meeting-B.html">05-meeting-B</a>, <a href="05-meeting-C.html">05-meeting-C</a> оставлены как история.</div>')
    return page('Встречи — макеты', 'Запись встреч (E13) — макеты к G3', 'Направление карточки встречи выбрано на G2b: A «Документ» с номером спикера у каждой реплики. Учтена модель угроз T1 (12.10).', body, NAV)
