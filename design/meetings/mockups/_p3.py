from _base import *

TITLE = 'Планёрка по бюджету'
META = '09.10.2026 · 10:00–11:12 · <span class="mono">1:12:40</span> · Звонок в DION'
LINES = [  # (время, кто, текст)  кто: me / s1 / s2 / s3 / mark
    ('0:00:04', 'me', 'Коллеги, добрый день. Я веду запись и расшифровку этой встречи для протокола.'),
    ('0:00:12', 's1', 'Добрый день. Тогда начнём с бюджета на четвёртый квартал.'),
    ('0:00:21', 's2', 'По подрядчикам у нас перерасход около двенадцати процентов, в основном из-за сдвига сроков.'),
    ('0:00:35', 'me', 'Олег, это уже с учётом договора на поддержку?'),
    ('0:00:39', 's2', 'Да, договор включён. Без него было бы около семи.'),
    ('0:01:02', 's3', 'Предлагаю перенести закупку серверов на январь — тогда укладываемся.'),
    ('0:01:15', 's1', 'Согласна. Олег, подготовишь пересчёт к пятнице?'),
    ('0:01:20', 's2', 'Подготовлю, и сразу пришлю Марии.'),
    ('0:12:03', 'mark', 'Ваш микрофон не записывался: 0:12:03–0:12:40'),
    ('0:12:41', 'me', 'Я вернулся, гарнитура отключилась. На чём остановились?'),
]
NAMES = {'me': 'Я', 's1': 'Ирина Соколова', 's2': 'Спикер 2', 's3': 'Спикер 3'}
NUM = {'s1': '1', 's2': '2', 's3': '3'}
SPK = [('me', 'Я', '18 мин', 'Коллеги, добрый день. Я веду запись…', None),
       ('s1', 'Спикер 1', '24 мин', 'Добрый день. Тогда начнём с бюджета…', 'Ирина Соколова'),
       ('s2', 'Спикер 2', '21 мин', 'По подрядчикам у нас перерасход около…', 'Олег Ли|'),
       ('s3', 'Спикер 3', '9 мин', 'Предлагаю перенести закупку серверов…', '')]


def marks():
    return ('<div style="display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 10px">' +
            badge('Пауза <span class="mono">0:31:10–0:34:02</span>', 'b-n', 'pause') +
            badge('Ваш микрофон не записывался <span class="mono">0:12:03–0:12:40</span>', 'b-warn', 'mic') +
            badge('Пауза: экран был заблокирован <span class="mono">0:50:00–1:02:00</span>', 'b-n', 'lock') + '</div>')


def head_actions(kind='A'):
    tpl = btn('Шаблоны…', 'pri', 'doc') if kind != 'C' else ''
    return f'<div style="display:flex;gap:8px">{tpl}{btn("Сохранить как…", "", "save")}{btn("", "icon", "more")}</div>'


def mhead(kind='A', back=True):
    bk = f'<div style="font-size:13px;color:var(--primary);display:flex;align-items:center;gap:4px;margin-bottom:6px">{ico("chevl","ico","width:13px;height:13px")}Все встречи</div>' if back else ''
    return (f'{bk}<div class="shead" style="margin-bottom:0"><div class="h"><div style="display:flex;align-items:center;gap:8px"><div class="h2">{TITLE}</div>'
            f'<span class="mut">{ico("edit","ico","width:14px;height:14px")}</span>{badge("Готово", "b-ok", "check")}</div>'
            f'<div class="ssub">{META} · <span class="t">звук удалён</span></div></div>{head_actions(kind)}</div>{marks()}')


def table(lines=LINES, names=NAMES, h=None, tail=''):
    rows = ''
    for t, w, x in lines:
        if w == 'mark':
            rows += f'<div class="tr mark"><span class="tc">{t}</span><span>{ico("mic","ico","width:12px;height:12px")} {E(x)}</span></div>'
        else:
            nm = names[w]
            num = f'<span class="sn">{NUM[w]}</span>' if (w in NUM and nm not in ('Собеседники', 'Участники')) else ''
            rows += f'<div class="tr"><span class="tc">[{t}]</span><span class="who{" me" if w == "me" else ""}">{num}{E(nm)}</span><span>{E(x)}</span></div>'
    st = f'max-height:{h}px;overflow:hidden;' if h else ''
    return f'<div class="card" style="{st}">{rows}{tail}</div>'


def participants_panel(w=230):
    items = ''
    for k, lab, mins, q, nm in SPK:
        if k == 'me':
            items += (f'<div style="padding:9px 12px;border-top:1px solid var(--border-soft)"><div style="display:flex;gap:6px;align-items:baseline">'
                      f'<b style="font-weight:500;color:var(--accent-ink)">Я</b><span class="mut mono" style="font-size:11.5px;margin-left:auto">{mins}</span></div>'
                      f'<div class="mut" style="font-size:11.5px">Ваш микрофон — подпись из настроек</div></div>')
            continue
        fld = (f'<div class="field foc" style="padding:4px 8px;font-size:12.5px">{E(nm[:-1])}<span style="border-left:1.5px solid var(--fg1);height:14px;margin-left:-6px"></span></div>' if nm and nm.endswith('|') else
               f'<div class="field{" ph" if not nm else ""}" style="padding:4px 8px;font-size:12.5px">{E(nm) if nm else "Как подписать"}</div>')
        items += (f'<div style="padding:9px 12px;border-top:1px solid var(--border-soft);display:flex;flex-direction:column;gap:5px">'
                  f'<div style="display:flex;gap:6px;align-items:baseline"><b style="font-weight:500">{lab}</b><span class="mut mono" style="font-size:11.5px;margin-left:auto">{mins}</span></div>'
                  f'<div class="mut" style="font-size:11.5px;line-height:1.4">«{E(q)}»</div>{fld}</div>')
    return (f'<div class="card" style="width:{w}px;flex:none;align-self:flex-start"><div style="padding:9px 12px;display:flex;align-items:center;gap:6px">'
            f'<span class="h3" style="font-size:14px">Участники</span><span style="margin-left:auto" class="mut">{ico("people")}</span></div>{items}'
            f'<div style="padding:8px 12px;border-top:1px solid var(--border-soft)">{btn("Это один человек…", "gh sm", "merge")}</div>'
            f'<div style="padding:0 12px 10px;font-size:11.5px;color:var(--fg3)">Имена действуют только в этой встрече.</div></div>')


def dirA(width=1024, height=None):
    c = mhead('A') + f'<div style="display:flex;gap:12px;align-items:flex-start"><div style="flex:1;min-width:0">{table()}</div>{participants_panel()}</div>'
    return window(c, width=width, height=height)


def pA():
    b = []
    b.append('<div class="sec">Направление A · «Документ» — карточка внутри окна настроек, расшифровка таблицей, участники справа</div>')
    b.append(dirA(height=None))
    b.append('<div class="legend"><b>Идея:</b> что видно — то и сохранится («Стенограмма»). Реплика — строка: время (PT Mono) · кто · текст; «Я» — цветом акцента <b>и</b> словом. '
             'Подряд идущие реплики одного говорящего в файле склеиваются, на экране — нет (видны таймкоды). Отметки встречи — бейджи под шапкой и строки внутри таблицы на своём месте во времени. '
             '<b>Имена:</b> поле у каждого спикера, рядом длительность и первая реплика; ввод сразу меняет таблицу и шапку («Спикер 1» → «Ирина Соколова»), подсказок из прошлых встреч нет. '
             '«Это один человек…» — выбрать второго спикера и «Объединить» (<a href="06-meeting-states.html">06</a>). Шаблоны — отдельное окно (<a href="07-templates.html">07</a>).</div>')
    b.append('<div class="sec">Первый экран в окне 1024×620 и 900×588</div>')
    b.append(dirA(height=620))
    narrow = mhead('A') + (f'<div style="display:flex;gap:12px;align-items:flex-start"><div style="flex:1;min-width:0">{table()}</div></div>')
    b.append(window(narrow, width=900, height=588))
    b.append('<div class="cap"><b>900×588:</b> контенту 672 px — панель «Участники» уходит под таблицу (в конец ленты) и открывается кнопкой «Имена участников» в «⋯». Это главный минус A.</div>')
    return page('Встречи — карточка A', '05 · Карточка встречи — направление A «Документ»', 'US-13.7, 13.8, 13.11. Сравнение направлений — spec.md §0.', ''.join(b), NAV)


def spk_chip(k, name=None):
    if k == 'me':
        return f'<span class="spk" style="background:var(--accent-bg);color:var(--accent-ink);padding-left:8px">Я</span>'
    col = {'s1': 'var(--sp1)', 's2': 'var(--sp2)', 's3': 'var(--sp3)'}[k]
    return (f'<span class="spk" style="background:color-mix(in srgb,{col} 14%,transparent);color:{col}">'
            f'<span class="n" style="background:{col}">{NUM[k]}</span>{E(name or NAMES[k])}</span>')


def dirB(height=None, pop=True):
    strip = ('<div class="card" style="padding:8px 12px;display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:10px">'
             '<span class="mut" style="font-size:12px">Участники:</span>' +
             ''.join(f'<span style="display:inline-flex;align-items:center;gap:4px">{spk_chip(k)}<span class="mut mono" style="font-size:11px">{m}</span></span>'
                     for k, _, m, _, _ in SPK) +
             f'<span style="flex:1"></span>{btn("Это один человек…", "gh sm", "merge")}</div>')
    msgs = ''
    for t, w, x in LINES:
        if w == 'mark':
            msgs += f'<div style="align-self:center;font-size:12px" class="mut">{ico("mic","ico","width:12px;height:12px")} {E(x)}</div>'
            continue
        me = w == 'me'
        msgs += (f'<div class="msg{" me" if me else ""}"><div class="mh">{"" if me else spk_chip(w)}<span class="mono">{t}</span>{spk_chip("me") if me else ""}</div>'
                 f'<div class="bub{" me" if me else ""}">{E(x)}</div></div>')
    popover = ''
    if pop:
        popover = (f'<div style="position:absolute;left:330px;top:150px;width:280px;background:var(--bg-surface);border:1px solid var(--border);border-radius:8px;'
                   f'box-shadow:0 12px 34px var(--shadow);padding:12px;display:flex;flex-direction:column;gap:8px;z-index:3">'
                   f'<div style="display:flex;gap:6px;align-items:center">{spk_chip("s2")}<span class="mut mono" style="font-size:11.5px">21 мин · 34 реплики</span></div>'
                   f'<div class="mut" style="font-size:12px;line-height:1.45">«По подрядчикам у нас перерасход около…»<br>«Да, договор включён. Без него…»</div>'
                   f'<div class="field foc">Олег Ли<span style="border-left:1.5px solid var(--fg1);height:14px;margin-left:-6px"></span></div>'
                   f'<div style="font-size:11.5px" class="mut">Имя действует только в этой встрече</div>'
                   f'<div style="display:flex;gap:8px;justify-content:flex-end">{btn("Отмена", "sm")}{btn("Подписать", "pri sm")}</div></div>')
    c = mhead('B') + strip + f'<div class="card" style="padding:12px 14px;display:flex;flex-direction:column;gap:10px;background:var(--bg-sunk)">{msgs}</div>' + popover
    return window(c, height=height)


def pB():
    b = []
    b.append('<div class="hist"><b>История, не к реализации.</b> Направление снято 12.10.2026 (решение владельца на G2b: выбрано A «Документ» с номером спикера у каждой реплики). Актуальная карточка — <a href="05-meeting-A.html">05-meeting-A.html</a>.</div>')
    b.append('<div class="sec">Направление B · «Беседа» — лента реплик, «Я» справа, собеседники слева; участники — полоска над лентой</div>')
    b.append(dirB())
    b.append('<div class="legend"><b>Идея:</b> расшифровка читается как разговор. Метка спикера — номер в круге + имя + цвет (цвет никогда не один). '
             'Клик по метке (в полоске или у реплики) открывает поповер: длительность, первые реплики, поле «Имя» — «Подписать» меняет все его реплики в этой встрече. '
             '<b>Цена:</b> 8 цветов спикеров — новые токены (в макете 3, контраст в обеих темах не проверен); лента длинной встречи заметно длиннее таблицы; на экране не то, что в итоговом файле. Шаблоны — как в A, окном (07).</div>')
    b.append('<div class="sec">Первый экран 1024×620</div>')
    b.append(dirB(height=620, pop=False))
    return page('Встречи — карточка B', '05 · [снято 12.10] Карточка встречи — направление B «Беседа»', 'US-13.7, 13.8, 13.11. Сравнение направлений — spec.md §0.', ''.join(b), NAV)


def c_window(sel='Стенограмма', body='', w=1024, h=660):
    def it(t, i, on=False, sub=''):
        s = f'<span class="cnt">{sub}</span>' if sub else ''
        return f'<div class="nav{" on" if on else ""}" style="font-weight:{500 if on else 400}">{ico(i)}<span>{E(t)}</span>{s}</div>'
    left = ('<div class="side" style="width:232px">'
            f'<div style="padding:4px 6px 10px;border-bottom:1px solid var(--border);margin-bottom:6px"><div style="font-size:15px;font-weight:500">{TITLE}</div>'
            f'<div style="font-size:12px;color:var(--fg3)">09.10.2026 · <span class="mono">1:12:40</span></div><div style="margin-top:5px">{badge("Готово", "b-ok", "check")}</div></div>'
            + it('Участники', 'people', sel == 'Участники', '4') +
            '<div class="grp" style="margin:10px 0 3px 8px">Шаблоны</div>' +
            it('Стенограмма', 'doc', sel == 'Стенограмма') + it('Я и собеседники', 'meeting', sel == 'Я и собеседники') +
            it('Только текст', 'file', sel == 'Только текст') + it('Статистика встречи', 'chart', sel == 'Статистика встречи') +
            '<div class="grp" style="margin:10px 0 3px 8px">С ИИ · <span class="mono" style="text-transform:none;letter-spacing:0">llm.corp.local</span></div>' +
            it('Краткое содержание', 'spark', sel == 'Краткое содержание') + it('Протокол: решения и поручения', 'spark', sel == 'Протокол') +
            f'<div class="grow"></div><div class="nav" style="font-weight:400;color:var(--err-ink)">{ico("trash")}<span>Удалить встречу…</span></div></div>')
    return (f'<div class="win" style="width:{w}px;height:{h}px"><div class="tbar">{mark()}<span class="t">Встреча — {TITLE}</span><span class="sp"></span>'
            f'<span class="wbtn"></span><span class="wbtn"></span><span class="wbtn c"></span></div><div class="body">{left}'
            f'<div class="main"><div class="content" style="padding:12px 20px">{body}</div></div></div></div>')


def doc_preview():
    md = ('<div style="font-family:var(--font-mono);font-size:12.5px;line-height:1.6;color:var(--fg2);background:var(--bg-surface);border:1px solid var(--border);border-radius:8px;padding:14px 16px">'
          f'<div style="color:var(--fg1);font-weight:700"># {TITLE}</div><div>Дата: 09.10.2026 · Начало: 10:00 · Длительность: 1:12:40</div>'
          '<div>Участники: Я, Ирина Соколова, Олег Ли, Спикер 3</div><div>Отметки: пауза 0:31:10–0:34:02; ваш микрофон не записывался 0:12:03–0:12:40</div><br>')
    for t, w, x in LINES:
        if w == 'mark':
            md += f'<div class="mut">_{E(x)}_</div>'
        else:
            nm = {'s2': 'Олег Ли'}.get(w, NAMES[w])
            md += f'<div><b style="color:var(--fg1)">[{t}] {E(nm)}:</b> {E(x)}</div>'
    return md + '</div>'


def c_toolbar(name, ai=False):
    fmt = '<span class="seg" style="display:inline-flex;border:1px solid var(--border);border-radius:7px;overflow:hidden"><span style="font-size:12.5px;padding:4px 10px;background:var(--primary);color:var(--primary-fg)">.md</span><span style="font-size:12.5px;padding:4px 10px">.txt</span></span>'
    return (f'<div style="display:flex;align-items:center;gap:10px;margin-bottom:10px"><div class="h3" style="flex:1">{E(name)}</div>'
            f'{"" if ai else fmt}{btn("Сохранить как…", "pri", "save")}</div>')


def pC():
    b = []
    b.append('<div class="hist"><b>История, не к реализации.</b> Направление снято 12.10.2026 (решение владельца на G2b: выбрано A «Документ» с номером спикера у каждой реплики). Актуальная карточка — <a href="05-meeting-A.html">05-meeting-A.html</a>.</div>')
    b.append('<div class="sec">Направление C · «Окно встречи» — отдельное окно: слева участники и шаблоны, справа предпросмотр выбранного документа</div>')
    b.append(c_window(body=c_toolbar('Стенограмма') + doc_preview(), h=640))
    b.append('<div class="legend"><b>Идея:</b> одна встреча — одно окно, которое можно держать рядом с видеосвязью или с другим окном. Шаблон выбран слева — справа ровно тот файл, что сохранится; '
             '«Сохранить как…» всегда в одном месте. Шаблоны с ИИ — тем же списком, с хостом в заголовке группы (видно, куда уйдёт текст). Окна шаблонов нет. '
             '<b>Цена:</b> новый top-level (≈ +0,5–1 дн. QML), вопросы фокуса и положения во Fly, из «Встреч» «Открыть» запускает второе окно.</div>')
    names = '<div style="display:flex;gap:16px;align-items:flex-start">' + participants_panel(300) + \
            ('<div style="flex:1" class="card"><div style="padding:10px 12px;font-size:12.5px;color:var(--fg3)">Предпросмотр «Стенограммы» обновляется сразу:</div>' +
             table(LINES[:6], {**NAMES, 's2': 'Олег Ли'}) + '</div></div>')
    ai_body = (c_toolbar('Краткое содержание', ai=True) +
               '<div class="card" style="padding:12px 14px;display:flex;flex-direction:column;gap:8px;margin-bottom:10px"><div style="display:flex;gap:10px;align-items:center">'
               f'<span class="ok-t">{ico("home")}</span><div style="flex:1;font-size:13px">Уйдёт на <span class="mono">llm.corp.local</span> — адрес внутри вашей сети</div>{btn("Сделать заново", "sm", "refresh")}</div>'
               f'<div style="display:flex;gap:8px;align-items:center;font-size:12.5px"><span class="chk"></span>Заменить имена участников на «Участник 1, 2…»</div></div>'
               '<div class="card" style="padding:14px 16px;font-size:13.5px;line-height:1.6"><div class="mut" style="font-size:12px;margin-bottom:6px">Ответ сервиса ИИ · 09.10.2026 11:20 · сохранён в данных встречи</div>'
               '<b>Главное.</b> Перерасход по подрядчикам — около 12 %, из них 5 % — договор на поддержку. Решили перенести закупку серверов на январь, чтобы уложиться в бюджет квартала.<br>'
               '<b>Дальше.</b> Олег Ли готовит пересчёт к пятнице и отправляет Марии.</div>')
    b.append('<div class="grid2" style="grid-template-columns:1fr">' +
             f'<div class="tile" style="background:transparent;border:0;padding:0"><div class="tl">C · раздел «Участники»</div>{c_window("Участники", names, h=560)}</div>' +
             f'<div class="tile" style="background:transparent;border:0;padding:0"><div class="tl">C · шаблон с ИИ, ответ получен</div>{c_window("Краткое содержание", ai_body, h=520)}</div></div>')
    return page('Встречи — карточка C', '05 · [снято 12.10] Карточка встречи — направление C «Окно встречи»', 'US-13.7–13.11. Сравнение направлений — spec.md §0.', ''.join(b), NAV)
