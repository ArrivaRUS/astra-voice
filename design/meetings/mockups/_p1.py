from _base import *

REC_BTN = btn('Записать встречу', 'pri', 'rec')
SUB = 'Запись встреч и расшифровка на этом компьютере'


def entry_card(policy=None, badges=False):
    if policy:
        b = btn('Включить запись встреч', 'dis')
        lk = f'<span style="font-size:13px;color:var(--fg2);display:inline-flex;gap:5px;align-items:center">{ico("lock","ico","width:13px;height:13px")}Запись встреч отключена администратором</span>'
        extra = ''
        if policy == 'bad':
            extra = f'<span style="font-size:12.5px" class="err-t">Значение политики некорректно — обратитесь к администратору</span>'
        act = f'<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap">{b}{lk}{extra}</div>'
    else:
        act = f'<div>{btn("Включить запись встреч", "pri")}</div>'
    return f'''<div class="card" style="padding:18px 20px;display:flex;gap:16px;align-items:flex-start">
 <div style="width:40px;height:40px;border-radius:10px;background:var(--primary-bg);color:var(--primary);display:flex;align-items:center;justify-content:center;flex:none">{ico("meeting","ico","width:22px;height:22px")}</div>
 <div style="display:flex;flex-direction:column;gap:10px;flex:1">
  <div class="h3">Запись встреч выключена</div>
  <div style="font-size:13px;color:var(--fg2);line-height:1.5;max-width:560px">Astra Voice может записать встречу — ваш микрофон и звук собеседников из программы видеосвязи — и сделать из записи текст прямо на этом компьютере, без интернета. Запись начинается, только когда вы нажмёте «Начать запись».</div>
  {act}
  <div style="font-size:12px;color:var(--fg3)">Пока функция выключена, программа не следит за звонками и ничего не записывает.</div>
 </div></div>'''


def mrow(name, meta, bdg, main, more=True, live=False, prog=None):
    p = f'<div class="pbar"><i style="width:{prog}%"></i></div>' if prog is not None else ''
    m = btn('', 'gh icon sm', 'more') if more else ''
    return (f'<div class="mrow{" live" if live else ""}"><div class="left"><div class="nm">{E(name)}</div>'
            f'<div class="meta">{meta}</div></div>{bdg}{main}{m}{p}</div>')


def meetings_list(kind='full', policy_off=False):
    if kind == 'empty':
        return ('<div class="grp">Записанные встречи</div><div class="card"><div class="r" style="min-height:64px">'
                '<div class="left mut" style="font-size:13px">Встреч пока нет. Нажмите «Записать встречу» или выберите этот пункт в меню значка в трее.</div></div></div>')
    rows = []
    if kind == 'full':
        rows.append(mrow('Встреча 12.10.2026 11:00', '12.10.2026 · 11:00 · <span class="t">идёт сейчас</span>',
                         badge('Запись · <span class="mono">0:42:10</span>', 'b-pri', 'rec'), btn('Остановить', 'sm', 'stop'), more=False, live=True))
        rows.append(mrow('Планёрка по бюджету', '09.10.2026 · 10:00 · 1:12:40 · 168 МБ',
                         badge('Расшифровка · <span class="mono">40 %</span>', 'b-warn', 'clock'), btn('Отменить', 'sm'), prog=40))
    rows.append(mrow('Созвон с подрядчиком', '08.10.2026 · 15:30 · 0:48:05 · 0,4 МБ · <span class="t">звук удалён</span>',
                     badge('Готово', 'b-ok', 'check'), btn('Открыть', 'sm')))
    if kind == 'full':
        rows.append(mrow('Ретро команды', '06.10.2026 · 17:00 · 0:55:30 · 102 МБ',
                         badge('Записана', 'b-n'), btn('Расшифровать', 'sm')))
    rows.append(mrow('Встреча 07.10.2026 09:00', '<span class="t">Сохранено</span> 0:37:02 <span class="t">— программа закрылась во время записи</span>',
                     badge('Прервана', 'b-warn', 'alert'), btn('Расшифровать', 'sm dis' if policy_off else 'sm')))
    rows.append(mrow('Встреча 03.10.2026 16:00', '<span class="t">Распознавание на этом компьютере не работает</span>',
                     badge('Ошибка расшифровки', 'b-err', 'alert'), btn('Повторить', 'sm dis' if policy_off else 'sm')))
    n = len(rows)
    tot = {6: '412 МБ', 3: '87 МБ'}.get(n, '87 МБ')
    return (f'<div class="grp">Записанные встречи</div><div class="card">{"".join(rows)}'
            f'<div class="cfoot"><span>Всего: <span class="mono">{n}</span> встреч · <span class="mono">{tot}</span> на диске</span>'
            f'<span style="flex:1"></span><span>Хранятся только на этом компьютере</span></div></div>')


def settings_groups(locks=False, ai='empty', model_ok=True, badges=True):
    L = (lambda t: t) if (locks and badges) else (lambda t: None)
    g = []
    g.append('<div class="grp">Запись</div><div class="card">' +
             row('Предлагать запись, когда начинается звонок', 'Программа заметит звонок и спросит. Сама запись не начнётся',
                 tgl(False), hint=True, lock=L('Задано администратором'), dis=locks) +
             row('Какие звонки узнаются', 'DION · звонок в браузере (Яндекс Телемост и другие сайты)', '', hint=True) + '</div>')
    notice = 'Встреча записывается для протокола.' if locks else 'Я веду запись и расшифровку этой встречи для протокола.'
    g.append('<div class="grp">Предупреждение участников</div><div class="card">' +
             f'<div class="r col"><div style="display:flex;gap:10px;align-items:center"><div class="left"><div class="lbl">Текст для чата встречи</div>'
             f'<div class="sub">Показывается при каждом начале записи — с кнопкой «Скопировать для чата»</div></div>'
             f'{"<span class=lockb>" + ico("lock") + "Задано администратором</span>" if (locks and badges) else ""}</div>'
             f'<div class="field{" dis" if locks else ""}" style="margin-top:6px">{E(notice)}</div></div>' +
             f'<div class="r col"><div class="left"><div class="lbl">Текст для приглашения на встречу</div>'
             f'<div class="sub">Предупредить заранее надёжнее всего — добавьте этот текст в приглашение</div></div>'
             f'<div style="display:flex;gap:8px;margin-top:6px;align-items:flex-start"><div class="field area" style="flex:1">На встрече может вестись запись и расшифровка для протокола. Запись хранится только на компьютере участника, который её ведёт.</div>{btn("Скопировать", "", "copy")}</div></div>'
             + '</div>')
    mdl = (row('Модель для расшифровки', 'GigaAM v3 RNN-T · установлена', '') if model_ok else
           row('Модель для расшифровки', '<span class="warn-t">Не установлена — без неё расшифровки не будет</span>', btn('Установить', 'pri')))
    g.append('<div class="grp">Расшифровка</div><div class="card">' + mdl +
             row('Расшифровывать сразу после записи', '', tgl(True)) +
             row('Разделять собеседников по голосам', 'Собеседники станут «Спикер 1, 2…». Голоса не запоминаются — ни между встречами, ни внутри встречи',
                 tgl(not locks), hint=True, lock=L('Задано администратором'), dis=locks) +
             row('Как подписывать вас в расшифровке', '', '<span class="field" style="width:200px">Я</span>') + '</div>')
    g.append('<div class="grp">Хранение</div><div class="card">' +
             row('Хранить запись звука после расшифровки', 'Расшифровка остаётся. Если расшифровать не удалось, звук не удаляется',
                 sel('30 дней' if locks else 'Не хранить', dis=locks), lock=L('Задано администратором'), dis=locks) +
             row('Удалять встречи старше', '', sel('Никогда')) +
             row('<span class="mut" style="font-size:13px">Встречи хранятся только на этом компьютере и доступны только вашей учётной записи</span>') + '</div>')
    g.append('<div class="grp">Шаблоны с ИИ</div>' + ai_group(ai, badges))
    g.append('<div style="margin-top:16px;display:flex;align-items:center;gap:12px">' + btn('Выключить запись встреч') +
             '<span style="font-size:12px;color:var(--fg3)">Сохранённые встречи останутся. Удалить их можно в списке выше</span></div>')
    return ''.join(g)


def ai_group(ai, badges=True):
    if ai == 'policy-off':
        return ('<div class="card">' + row('Шаблоны с ИИ отключены администратором',
                'Краткое содержание и протокол встречи недоступны. Локальные шаблоны работают', '', lock='Задано администратором' if badges else None, dis=True) + '</div>')
    nt = note('Краткое содержание и протокол встречи делает языковая модель на сервере, адрес которого вы укажете. '
              'Текст встречи уходит туда только по вашему нажатию. Пока адреса нет, этих шаблонов нет.')
    url = {'empty': ('<span class="field ph mono" style="flex:1">https://…/v1</span>', ''),
           'internal': ('<span class="field mono" style="flex:1">https://10.0.5.20:8000/v1</span>',
                        f'<div style="font-size:12px" class="ok-t">{ico("home","ico","width:12px;height:12px")} Адрес внутри вашей сети</div>'),
           'external': ('<span class="field mono" style="flex:1">https://api.example.ru/v1</span>',
                        f'<div style="font-size:12px" class="warn-t">{ico("globe","ico","width:12px;height:12px")} Внешний адрес — перед каждой отправкой программа предупредит</div>'),
           'locked': ('<span class="field dis mono" style="flex:1">https://llm.corp.local/v1</span>',
                      f'<div style="font-size:12px" class="mut">{ico("globe","ico","width:12px;height:12px")} Внешний адрес согласован администратором — предупреждения перед отправкой не будет</div>'),
           'http': ('<span class="field mono" style="flex:1;border-color:var(--err-ink)">http://10.0.5.20:8000/v1</span>',
                    f'<div style="font-size:12px" class="err-t">{ico("alert","ico","width:12px;height:12px")} Адрес с http:// допустим только для этого компьютера (localhost). Для других адресов нужен https://</div>'),
           'plain': ('<span class="field dis mono" style="flex:1">http://10.0.5.20:8000/v1</span>',
                     f'<div style="font-size:12px" class="warn-t">{ico("alert","ico","width:12px;height:12px")} Без шифрования, разрешено администратором · адрес внутри вашей сети</div>'),
           'loopback': ('<span class="field mono" style="flex:1">http://localhost:8080/v1</span>',
                        f'<div style="font-size:12px" class="ok-t">{ico("home","ico","width:12px;height:12px")} Сервис на этом компьютере</div>'),
           'offline': ('<span class="field mono" style="flex:1">https://api.example.ru/v1</span>',
                       f'<div style="font-size:12px" class="mut">{ico("globe","ico","width:12px;height:12px")} Внешний адрес недоступен в офлайн-режиме</div>'),
           }[ai]
    res = {'empty': '', 'internal': f'<span class="ok-t" style="font-size:12.5px">{ico("check","ico","width:13px;height:13px")} Сервис ИИ отвечает</span>',
           'external': f'<span class="err-t" style="font-size:12.5px">{ico("alert","ico","width:13px;height:13px")} Сервис ИИ не ответил: доступ запрещён — проверьте ключ доступа</span>',
           'locked': '', 'offline': '', 'http': '', 'loopback': '', 'plain': ''}[ai]
    lk = f'<span class="lockb">{ico("lock")}Задано администратором</span>' if (ai == 'locked' and badges) else ''
    lkd = False  # В3, решение владельца 12.10: под замком только адрес
    mdl = '<span class="field ph" style="width:260px">имя модели на сервере</span>' if ai == 'empty' else f'<span class="field{" dis" if lkd else ""}" style="width:260px">glm-5.3</span>'
    key = '<span class="field ph" style="width:260px">необязательно</span>' if ai in ('empty',) else f'<span class="field mono{" dis" if lkd else ""}" style="width:260px">••••••••••••••••</span>'
    chk = btn('Проверить', 'dis' if ai in ('empty', 'offline', 'http') else '')
    return ('<div class="card"><div class="r" style="padding:10px 14px">' + nt.replace('class="note "', 'class="note" style="flex:1"') + '</div>' +
            f'<div class="r col"><div style="display:flex;gap:10px;align-items:center"><div class="left"><div class="lbl">Адрес сервиса ИИ</div>'
            f'<div class="sub">OpenAI-совместимый адрес — своя установка в организации или облачный сервис</div></div>{lk}</div>'
            f'<div style="display:flex;gap:8px;margin-top:6px">{url[0]}</div>{url[1] and "<div style=margin-top:5px>" + url[1] + "</div>"}</div>' +
            (f'<div class="r" style="min-height:0;padding:6px 14px;background:var(--bg-sunk)"><span class="mut" style="font-size:12px">{ico("info","ico","width:12px;height:12px")} Адрес задан администратором, модель и ключ — ваши</span></div>' if ai == 'locked' else '') +
            row('Модель', '', mdl, lock='Задано администратором' if lkd else None, dis=lkd) +
            row('Ключ доступа', 'Хранится в системном хранилище паролей', key, lock='Задано администратором' if lkd else None, dis=lkd) +
            f'<div class="r">{chk}<span style="font-size:12px;color:var(--fg3)">Отправит пробный запрос без текста встреч</span><span style="flex:1"></span>{res}</div>'
            + '</div>')


def section_on(kind='full', locks=False, ai='empty', model_ok=True, rec=True, badges=True):
    right = (f'<div style="display:flex;flex-direction:column;align-items:flex-end;gap:3px">{btn("Записать встречу","dis","rec")}'
             f'<span style="font-size:11.5px;color:var(--fg3)">Уже идёт запись встречи</span></div>') if (kind == 'full' and rec) else REC_BTN
    ban = ''
    if locks and not badges:
        ban = ('<div class="note" style="margin:2px 0 4px">' + ico('lock') + '<div>Часть настроек задана администратором вашей организации — '
               'они показаны серым и недоступны для изменения.</div></div>')
    return shead('Встречи', SUB, right) + ban + meetings_list(kind) + settings_groups(locks, ai, model_ok, badges)


def p01():
    b = []
    b.append('<div class="sec">1 · Чистая установка — функция выключена (US-13.1)</div>')
    b.append(window(shead('Встречи', SUB) + entry_card()))
    b.append('<div class="cap"><b>off.</b> В разделе ровно одна карточка. Нет списка, нет настроек, в трее нет «Записать встречу», детектор не работает. '
             'Раздел «Встречи» — четвёртый пункт навигации (после «Вывод»): функциональные разделы выше системных. Рамка-подсветка пункта — только стенд, в продукте её нет.</div>')
    b.append('<div class="sec">2 · Политика `meeting_record=off` и сохранённые встречи (US-13.12, S26-A4) · v1.1</div>')
    b.append(window(shead('Встречи', SUB) + entry_card('off') + meetings_list('short', policy_off=True), height=None))
    b.append('<div class="cap"><b>policy-off.</b> Кнопка недоступна. Уже записанные встречи можно открыть, сохранить и удалить; '
             '«Расшифровать» / «Повторить» недоступны (новых расшифровок нет). Причина — текстом в самой карточке (это её содержание по US-13.1, а не замок строки). Негодное значение ключа:</div>')
    b.append(f'<div style="align-self:stretch">{entry_card("bad")}</div>')
    b.append('<div class="sec">3 · Включено, встреч ещё нет</div>')
    b.append(window(section_on('empty')))
    b.append('<div class="sec">4 · Включено — раздел целиком (лента прокрутки): список во всех состояниях + настройки</div>')
    b.append(window(section_on('full'), height=None))
    b.append('<div class="legend"><b>Строки списка:</b> идёт запись (подсветка строки, «Остановить»), расшифровка с полоской 4 px, «Готово» → «Открыть», «Записана» (расшифровка выключена или отменена), '
             '«Прервана» (сбой во время записи), «Ошибка расшифровки». Ещё два состояния — «Ждёт памяти» и «Нужна модель» — в <a href="06-meeting-states.html">06</a>. '
             'Длительность и размер — PT Mono. Меню «⋯» у готовой встречи: Сохранить как… · Имена участников · Переименовать · Удалить (ниже). '
             'Во время записи кнопка «Записать встречу» в шапке недоступна с подписью, «Выключить запись встреч» тоже (в макете показан обычный вид).</div>')
    menu = ('<div class="tmenu" style="width:236px">' +
            ''.join(f'<div class="mi">{ico(i)}{t}</div>' for i, t in [('save', 'Сохранить как…'), ('people', 'Имена участников'), ('edit', 'Переименовать')]) +
            f'<div class="sepm"></div><div class="mi err-t">{ico("trash")}Удалить…</div></div>')
    b.append('<div class="grid2"><div class="tile"><div class="tl">Меню «⋯» готовой встречи</div>' + menu +
             '</div><div class="tile"><div class="tl">Загрузка и ошибка чтения списка <code>loading · error</code></div>'
             '<div class="card">' + ''.join('<div class="mrow"><div class="left"><div style="height:12px;width:46%;background:var(--bg-sunk);border-radius:4px"></div>'
                                            '<div style="height:10px;width:30%;background:var(--bg-sunk);border-radius:4px;margin-top:7px"></div></div></div>' for _ in range(2)) + '</div>'
             '<div class="tn">Заглушки — только если чтение дольше 300 мс; без анимации.</div>'
             '<div class="card"><div class="r"><span class="err-t">' + ico('alert') + '</span><div class="left" style="font-size:13px">Не удалось прочитать папку встреч. Проверьте, что диск подключён и доступен.</div>' + btn('Повторить', 'sm') + '</div></div></div></div>')
    b.append('<div class="sec">5 · v1.1 · Политика без замков на строках (В5): ключи `manual`, `meeting_diarization`, `retention=30`, `notice_text`, `ai_endpoint=URL`</div>')
    b.append(window(section_on('short', locks=True, ai='locked', badges=False), height=None))
    b.append('<div class="cap"><b>v1.1 (решение владельца по В5):</b> замков на строках нет — механизм в v1.2. Политика действует: строки серые (`fg-disabled`) и недоступны, '
             'а причину объясняет <b>одна строка вверху раздела</b> — она появляется, только если хоть один ключ `meeting_*` задан. Негодное значение ключа — тот же серый вид, '
             'причина — в журнале и в документе администратора.</div>')
    b.append('<div class="sec">5б · v1.2 (будущее, не для v1.1) · те же ключи с замками на строках</div>')
    b.append(window(section_on('short', locks=True, ai='locked'), height=None))
    b.append('<div class="cap"><b>Замки.</b> Значение видно, контрол в цвете своего значения, текст `fg-disabled`, рядом бейдж «Задано администратором» (канон §3.2/§4.8). '
             '<b>Будущее, v1.2:</b> бейдж «Задано администратором» у каждой строки (канон §3.2/§4.8), общая строка вверху не нужна. При адресе ИИ от администратора под замком только адрес; «Модель» и «Ключ доступа» пользователь меняет сам (В3).</div>')
    b.append('<div class="sec">6 · Группа «Шаблоны с ИИ» — состояния адреса</div>')
    tiles = [('Внутренний адрес, проверка прошла', 'internal'), ('Внешний адрес, проверка не прошла', 'external'),
             ('http:// не на этом компьютере, без разрешения — ошибка поля (T1 MJ-2)', 'http'), ('http:// на этом компьютере — допустимо', 'loopback'),
             ('http:// разрешён администратором для этого адреса (7-й ключ)', 'plain'),
             ('Офлайн-режим, внешний адрес', 'offline'), ('Политика `meeting_ai_endpoint=off`', 'policy-off')]
    b.append('<div class="grid2">' + ''.join(f'<div class="tile" style="background:var(--bg-app)"><div class="tl">{E(t)}</div>{ai_group(k, badges=False)}</div>' for t, k in tiles) + '</div>')
    b.append('<div class="sec">7 · Минимальное окно 900×588 — первый экран</div>')
    b.append(window(section_on('full'), width=900, height=588))
    return page('Встречи — раздел', '01 · Раздел «Встречи» в окне настроек',
                'Одно направление в стиле существующих разделов: шапка с главным действием, группа «Записанные встречи», затем настройки группами. '
                'Тексты — <code>spec.md</code> §4.1, состояния — §3.1, политика — §2.7. Окна 1024 по ширине; лента целиком показана без обрезки по высоте.', ''.join(b), NAV)


# ── 02 экран ответственности ─────────────────────────────────────────────
def resp_dialog(variant='personal', manual=False, w=520, maxh=None):
    items = [('meeting', 'Что записывается', 'Ваш микрофон и звук собеседников из программы видеосвязи. Из записи программа делает текст — на этом компьютере, без интернета.', ''),
             ('rec', 'Когда', 'Только после того, как вы нажмёте «Начать запись». Пока идёт запись, на экране видна плашка с таймером — скрыть её нельзя, остановить можно одним нажатием.', ''),
             ('home', 'Где хранится', 'Только на этом компьютере. Звук удаляется сразу после расшифровки, если вы не выберете иначе. Встречу можно удалить целиком в любой момент.', ''),
             ('alert', 'Предупреждать участников — ваша обязанность', 'При каждом начале записи программа покажет готовый текст для чата встречи — скопируйте его и скажите вслух. Лучше предупреждать заранее — например, в приглашении на встречу.', 'warn'),
             ('people', 'Только свои встречи', 'Записывайте только встречи, в которых участвуете сами, и не передавайте запись другим без согласия участников.', '')]
    its = ''
    for i, t, d, k in items:
        st = 'background:var(--warn-bg);border-radius:8px;padding:9px 10px;margin:0 -10px;' if k else ''
        col = 'var(--warn-ink)' if k else 'var(--fg3)'
        its += (f'<div style="display:flex;gap:11px;{st}"><span style="color:{col};margin-top:2px">{ico(i)}</span><div>'
                f'<div style="font-size:14px;font-weight:500">{E(t)}</div><div style="font-size:13px;color:var(--fg2);line-height:1.5">{E(d)}</div></div></div>')
    last = ('На рабочем компьютере действуют правила вашей организации о записи совещаний.' if variant == 'personal'
            else 'Этим компьютером управляет ваша организация. Записывайте совещания по её правилам — их можно узнать у администратора.')
    lk = ''  # v1.1: замков на строках нет (В5), причина — подписью
    chk = (f'<div style="display:flex;gap:10px;align-items:flex-start;border-top:1px solid var(--border-soft);padding-top:12px">'
           f'<span class="chk{" dis" if manual else ""}" style="margin-top:2px"></span><div style="flex:1"><div style="font-size:13.5px{";color:var(--fgdis)" if manual else ""}">Предлагать запись, когда начинается звонок</div>'
           f'<div style="font-size:12px;color:var(--fg3)">{"Недоступно — так настроено администратором вашей организации" if manual else "Программа только спросит — запись сама не начнётся"}</div></div>{lk}</div>')
    mh = f'max-height:{maxh}px;' if maxh else ''
    body_st = 'flex:1;min-height:0;overflow-y:auto;border-bottom:1px solid var(--border-soft);' if maxh else ''
    return (f'<div class="dlg" style="width:{w}px;{mh}display:flex;flex-direction:column"><div class="dh" style="flex:none">{mark()}Запись встреч</div><div class="db" style="{body_st}">'
            f'<div class="h3">Перед тем как включить</div>{its}'
            f'<div style="font-size:13px;color:var(--fg2)">{E(last)}</div>{chk}</div>'
            f'<div class="df" style="flex:none"><span class="sp"></span>{btn("Отмена", "focus")}{btn("Понятно, включить", "pri")}</div></div>')


def resp_win(w, h, variant='personal', manual=False):
    client = h - 32
    return (f'<div class="win" style="width:{w}px;height:{h}px"><div class="tbar">{mark()}<span class="t">Astra Voice — Настройки</span><span class="sp"></span><span class="wbtn"></span><span class="wbtn"></span><span class="wbtn c"></span></div>'
            f'<div class="scrim" style="height:{client}px;padding:24px">{resp_dialog(variant, manual, 520, client - 48)}</div></div>')


def p02():
    b = []
    b.append('<div class="sec">1 · Личный контур — окно 1024×620: диалог не выше клиентской области − 48, тело прокручивается</div>')
    b.append(resp_win(1024, 620))
    b.append('<div class="cap"><b>Высота D1</b> по содержимому — 685 px (замер с PT Root UI), это больше клиентской области окна (588 при 1024×620, 556 при 900×588). '
             'Поэтому: <b>max-height = клиентская высота окна − 48</b> (по 24 сверху и снизу): 540 / 508. Шапка и кнопки закреплены, тело прокручивается штатным Flickable, '
             'полоса прокрутки видна сразу, над кнопками — разделитель. Прокрутка до конца для «Понятно, включить» <b>не требуется</b>. '
             '<b>Фокус по умолчанию — «Отмена»</b>: Enter ничего не включает (M4). Ширина 520 — исключение из 420.</div>')
    b.append('<div class="sec">2 · Окно 900×588 (минимум)</div>')
    b.append(resp_win(900, 588))
    b.append('<div class="sec">3 · Корпоративный контур и `meeting_record=manual` (диалог целиком, без ограничения высоты — для чтения текста)</div>')
    b.append(f'<div class="grid2"><div class="tile"><div class="tl">Есть ключи <code>meeting_*</code></div>{resp_dialog("corp", w=470)}'
             '<div class="tn">Меняется только последний абзац. Текст экрана — не юридическая консультация; формулировки — к очному юристу до G5 (PRD A37).</div></div>'
             f'<div class="tile"><div class="tl">manual <code>галочка недоступна</code></div>{resp_dialog("corp", manual=True, w=470)}'
             '<div class="tn">Если программа не узнаёт ни одной программы видеосвязи (до спайков SP3), строки с галочкой нет вовсе.</div></div></div>')
    return page('Встречи — экран ответственности', '02 · Экран ответственности (US-13.1, M4)',
                'Пять пунктов из PRD F18.1 (б), разбитые по смыслу, чтобы их прочитали, а не пролистали. Обязанность предупреждать — выделена. Тексты — spec.md §4.2.', ''.join(b), NAV)


# ── 03 предложение ────────────────────────────────────────────────────────
def offer(kind='dion', left=42, starting=False):
    title = 'Идёт звонок в DION. Начать запись встречи?' if kind == 'dion' else 'Идёт звонок в браузере. Начать запись встречи?'
    extra = '' if kind == 'dion' else ' Запишется звук браузера целиком — всех вкладок.'
    b1 = (f'<span class="nbtn p">{ico("spinner")}Начинаю…</span>' if starting else f'<span class="nbtn p">{ico("rec")}Начать запись</span>')
    return (f'<div class="ntf"><div class="nh">{ico("meeting","ico","width:13px;height:13px")}Astra Voice<span class="x">{ico("x")}</span></div>'
            f'<div class="nt">{E(title)}</div><div style="color:var(--pill-muted)">Запись начнётся с момента нажатия — сказанное раньше в неё не попадёт.{E(extra)}</div>'
            f'<div class="nb">{b1}<span class="nbtn">Не записывать</span><span class="cnt">Закроется через {left} с</span></div>'
            f'<div class="timer" style="width:{left / 60 * 100:.0f}%"></div></div>')


def desk(inner, h=420):
    return (f'<div class="desk" style="height:{h}px">'
            f'<div class="vks" style="left:120px;top:56px;width:560px"><div class="vt">DION — Планёрка по бюджету</div>'
            f'<div class="tiles"><div>Ирина С.</div><div>Олег</div><div>Мария К.</div><div>Вы</div></div><div class="ctl"><i></i><i></i><i></i><i class="end"></i></div></div>'
            f'{inner}<div class="panel"><span class="task">DION</span><span class="task">Kate</span><span class="sp"></span>'
            f'<span class="trayicon" style="width:auto;padding:0 4px;background:none">{tray_svg("idle", 18)}</span><span class="clock">11:02</span></div></div>')


def p03():
    b = []
    b.append('<div class="sec">1 · Положение: сверху по центру, на месте будущей плашки записи; поверх DION, без кражи фокуса</div>')
    b.append(desk(f'<div class="ov" style="left:50%;top:12px;transform:translateX(-50%)">{offer()}</div>', 440))
    b.append('<div class="cap"><b>W1.</b> Своё окно поверх всех (как пилюля): не активируется, нет в Alt+Tab, фокус остаётся в DION. '
             'Пока окно открыто, звук не открыт вовсе (S23-A2) — поэтому текст прямо говорит, что сказанное до нажатия в запись не попадёт. '
             'Полоска внизу убывает за 60 с (linear, без мигания). «×» = «Не записывать». Палитра фиксированная (`notify-*`), с темой не меняется. <b>T1 MN-7:</b> место фиксированное (не под курсором), первые 500 мс после появления нажатия не принимаются — случайный щелчок по DION не начнёт запись. <b>BL-5:</b> экран заблокирован или это нельзя узнать — окна нет.</div>')
    b.append('<div class="sec">2 · Состояния</div>')
    b.append('<div class="grid2">'
             f'<div class="tile"><div class="tl">Звонок в программе <code>call-app</code></div>{offer()}</div>'
             f'<div class="tile"><div class="tl">Звонок в браузере <code>call-browser</code></div>{offer("browser", 51)}'
             '<div class="tn">Название сайта не пишется: программа не отличает Телемост от других сайтов со звонками.</div></div>'
             f'<div class="tile"><div class="tl">После «Начать запись» <code>starting ≤ 1 с</code></div>{offer(left=37, starting=True)}'
             '<div class="tn">Затем окно превращается в плашку записи на том же месте (<a href="04-recording.html">04</a>) и рядом появляется «Предупредите участников».</div></div>'
             '<div class="tile"><div class="tl">Когда окна нет</div><div class="tn">«Не записывать», «×», 60 с без ответа, конец звонка (микрофон свободен ≥ 10 с) → окно закрыто, в этом звонке больше не появится. '
             'Не появляется: тумблер выключен, `manual`/`off`, экран заблокирован или сеанс неактивен, уже идёт запись, своя диктовка или проверка микрофона, голосовое короче 5 с, программа не из списка. '
             '«Индикатор: выкл» окно <b>не</b> скрывает — это вопрос, а не индикатор. Экран заблокировали при открытом окне → окно закрыто без ответа; после разблокировки, если звонок идёт, — снова.</div></div>'
             '</div>')
    return page('Встречи — предложение записи', '03 · Предложение «Записать?» при звонке (US-13.2, S23)',
                'Детектор только спрашивает. Тексты — spec.md §4.3, состояния — §3.3, Fly — §6.', ''.join(b), NAV)
