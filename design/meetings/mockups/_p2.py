from _base import *
from _p1 import desk


def opt(title, sub, on=False, icon='meeting', warn=None):
    st = 'border:1.5px solid var(--primary);background:var(--primary-bg);' if on else 'border:1px solid var(--border);background:var(--bg-surface);'
    w = f'<div style="font-size:12px;margin-top:3px" class="warn-t">{ico("alert","ico","width:12px;height:12px")} {E(warn)}</div>' if warn else ''
    return (f'<div style="{st}border-radius:8px;padding:10px 12px;display:flex;gap:11px;align-items:flex-start">'
            f'<span class="radio{" on" if on else ""}" style="margin-top:3px"></span><span style="color:var(--fg3);margin-top:2px">{ico(icon)}</span>'
            f'<div style="flex:1"><div style="font-size:14px">{E(title)}</div><div style="font-size:12px;color:var(--fg3);line-height:1.45">{E(sub)}</div>{w}</div></div>')


def start_card(mode='call', hint=True, error=None, starting=False, nosplit=False, w=460):
    o1t = 'Звонок в браузере' if mode == 'browser' else 'Звонок в DION'
    o1s = ('Ваш микрофон и звук браузера — всех вкладок, не только вкладки звонка.' if mode == 'browser'
           else 'Ваш микрофон и звук DION. Другие звуки компьютера в запись не попадут.')
    opts = ''
    if mode in ('call', 'browser'):
        opts += opt(o1t, o1s, on=True, icon='meeting',
                    warn='На этом компьютере программа не умеет отделить звук видеосвязи — в запись попадёт весь звук компьютера.' if nosplit else None)
    opts += opt('Встреча в комнате — только микрофон', 'Звук компьютера не записывается.', on=(mode == 'room'), icon='people')
    opts += opt('Весь звук компьютера и микрофон', 'Для программы видеосвязи, которую Astra Voice не узнаёт. В запись попадут все звуки компьютера: уведомления, музыка, другие программы.', icon='speaker')
    h = note('В гарнитуре запись будет чище: без неё голоса собеседников попадут и в ваш микрофон.', 'warn', 'head') if hint else ''
    errs = {'disk': ('Мало места на диске: для записи встречи нужно около 600 МБ. Освободите место или удалите старые встречи.', 'Открыть встречи'),
            'audio': ('Не удалось начать запись: звуковая служба не отвечает. Попробуйте ещё раз или обратитесь к администратору.', 'Повторить'),
            'folder': ('Не удалось начать запись: папка встреч недоступна для записи. Обратитесь к администратору.', None),
            'nfs': ('Домашняя папка находится на сетевом диске — запись встречи будет храниться не только на этом компьютере. Записывайте, только если это разрешено в вашей организации.', None),
            'remote-audio': ('Звуковая служба работает не на этом компьютере — звук встречи пройдёт через другой компьютер. Записывайте, только если это разрешено в вашей организации.', None),
            'lock-unknown': ('Запись недоступна: в этой сессии нельзя узнать, заблокирован ли экран, — а без этого программа не может поставить запись на паузу при блокировке.', None)}
    er = ''
    if error:
        t, a = errs[error]
        kind = 'warn' if error in ('nfs', 'remote-audio') else 'err'
        er = (f'<div class="note {kind}">{ico("alert")}<div style="flex:1">{E(t)}'
              f'{"<div style=margin-top:7px>" + btn(a, "sm") + "</div>" if a else ""}</div></div>')
    go = btn('Начинаю…', 'pri', 'spinner') if starting else btn('Начать запись', 'dis' if error in ('disk', 'lock-unknown') else 'pri focus', 'rec')
    return (f'<div class="win" style="width:{w}px"><div class="tbar">{mark()}<span class="t">Записать встречу</span><span class="sp"></span><span class="wbtn c"></span></div>'
            f'<div style="padding:14px 16px 0;display:flex;flex-direction:column;gap:9px"><div class="h3">Что записывать</div>{opts}'
            f'<div style="display:flex;align-items:center;gap:10px;margin-top:2px"><span style="font-size:14px;flex:1">Микрофон</span>{sel("Системный по умолчанию")}</div>'
            f'{h}{er}<div style="font-size:12px;color:var(--fg3)">После начала записи появится текст, чтобы предупредить участников.</div></div>'
            f'<div class="df" style="display:flex;gap:8px;padding:12px 16px 14px"><span style="flex:1"></span>{btn("Отмена")}{go}</div></div>')


def sym(kind):
    c = {'rec': ('rec', 'var(--pill-live)'), 'paused': ('pause', 'var(--pill-muted)'), 'warn': ('alert', 'var(--pill-warn)'),
         'err': ('alert', 'var(--pill-err)'), 'spin': ('spinner', 'var(--pill-muted)'), 'clock': ('clock', 'var(--pill-warn)')}[kind]
    return f'<span class="sym" style="color:{c[1]}">{ico(c[0], "ico", "width:14px;height:14px")}</span>'


def rbar(state='rec', t='0:42:10'):
    """W2. Правило ширины (spec §5.3): одна строка — 340…520; всё, что сверх постоянного ряда
    (причина, отсчёт, кнопка действия), уходит во 2-ю строку шириной ряда 1 с переносом текста; ellipsis запрещён."""
    tools = (f'<span class="rb i" title="Текст для участников">{ico("chat")}</span>'
             f'<span class="rb i" title="Свернуть плашку">{ico("collapse")}</span>')
    pause = f'<span class="rb">{ico("pause")}Пауза</span>'
    stop = f'<span class="rb">{ico("stop")}Остановить</span>'
    cont = f'<span class="rb">{ico("play")}Продолжить</span>'
    sep = '<span class="sep"></span>'

    def row1(sg, lab, tm, acts, dim=False):
        return (f'<span style="display:flex;align-items:center;gap:8px;height:36px"><span class="grip"></span>{sym(sg)}<span class="lab">{lab}</span>'
                f'<span class="tm{" dim" if dim else ""}">{tm}</span>{sep}{acts}{sep}{tools}</span>')

    def two(r1, text, btn_html='', tone='var(--pill-muted)'):
        b2 = f'<span style="flex:none">{btn_html}</span>' if btn_html else ''
        return (f'<span class="rbar two" style="height:auto;border-radius:14px;flex-direction:column;align-items:stretch;padding:0 5px 7px 8px;gap:2px">{r1}'
                f'<span style="display:flex;flex-wrap:wrap;align-items:center;gap:6px 8px;padding:0 4px 0 22px;width:0;min-width:100%;box-sizing:border-box"><span style="color:{tone};white-space:normal;flex:1 1 200px">{text}</span>{b2}</span></span>')

    if state == 'rec':
        return f'<span class="rbar">{row1("rec", "Запись встречи", t, pause + stop)[len("<span style=" + chr(34) + "display:flex;align-items:center;gap:8px;height:36px" + chr(34) + ">"):-7]}</span>'
    if state == 'paused':
        return f'<span class="rbar" title="На паузе 3 мин. Через 60 мин паузы встреча сохранится">{row1("paused", "Пауза", t, cont + stop, True)[len("<span style=" + chr(34) + "display:flex;align-items:center;gap:8px;height:36px" + chr(34) + ">"):-7]}</span>'
    if state == 'limit':
        return two(row1('clock', 'Запись встречи', '1:55:12', pause + stop), 'Остановится через 4:48', '<span class="rb p">Продлить на час</span>', 'var(--pill-warn)')
    if state == 'mic':
        return two(row1('warn', 'Запись встречи', '0:12:40', pause + stop), 'Ваш голос не записывается: микрофон сменился',
                   '<span class="rb p">Продолжить с „Встроенный микрофон“</span>', 'var(--pill-warn)')
    if state == 'mic-long':
        return two(row1('warn', 'Запись встречи', '0:12:40', pause + stop),
                   'Ваш голос не записывается: микрофон сменился на «Гарнитура Jabra Evolve2 65 USB Headset Microphone»',
                   '<span class="rb p">Продолжить с другим микрофоном</span>', 'var(--pill-warn)')
    if state == 'app':
        return two(row1('rec', 'Запись встречи', '1:05:21', pause + stop), 'DION закрылся — пишется только микрофон')
    if state == 'lock':
        return two(row1('paused', 'Пауза', t, cont + stop, True), 'Экран был заблокирован — продолжите вручную', '', 'var(--pill-warn)')
    if state in ('lost', 'hang'):
        why = 'индикатор записи не отображался' if state == 'lost' else 'программа не отвечала'
        return two(row1('rec', 'Запись встречи', '0:51:16', pause + stop), f'Запись стояла на паузе 0:00:14: {why}. Отметка — в расшифровке', '', 'var(--pill-warn)')
    if state == 'fallback':
        return two(row1('rec', 'Запись встречи', '0:51:02', pause + stop), 'Встреча сохранена. Делаю расшифровку…')
    if state == 'compact':
        return f'<span class="rbar" style="padding:0 5px 0 8px"><span class="grip"></span>{sym("rec")}<span class="tm">{t}</span><span class="rb i" title="Развернуть плашку">{ico("expand")}</span></span>'
    if state == 'compact-p':
        return f'<span class="rbar" style="padding:0 5px 0 8px"><span class="grip"></span>{sym("paused")}<span class="tm dim">{t}</span><span class="rb i" title="Развернуть плашку">{ico("expand")}</span></span>'
    if state == 'stop':
        return f'<span class="rbar"><span class="grip"></span>{sym("spin")}<span>Сохраняю запись…</span></span>'
    if state == 'err':
        return f'<span class="rbar"><span class="grip"></span>{sym("err")}<span style="color:var(--pill-err)">Запись остановлена: звуковая служба не отвечает. Записанное сохранено</span></span>'
    raise KeyError(state)


def tipdemo():
    return (f'<div style="display:flex;gap:28px;align-items:flex-start;flex-wrap:wrap">'
            f'<div style="display:flex;flex-direction:column;align-items:center;gap:6px">{rbar()}<span class="tip" style="position:static">Свернуть плашку</span>'
            f'<span style="font-size:11.5px;color:var(--pill-muted)">глиф collapse ›‹ — плашка сжимается до знака и таймера</span></div>'
            f'<div style="display:flex;flex-direction:column;align-items:center;gap:6px">{rbar("compact")}<span class="tip" style="position:static">Развернуть плашку</span>'
            f'<span style="font-size:11.5px;color:var(--pill-muted)">глиф expand ‹›</span></div></div>')


def notice(state='default'):
    txt = 'Встреча записывается для протокола.' if state == 'admin' else 'Я веду запись и расшифровку этой встречи для протокола.'
    if state == 'copied':
        b = f'<span class="nbtn">{ico("check")}Скопировано</span>'
    elif state == 'fail':
        b = f'<span class="nbtn" style="white-space:normal"><span style="color:var(--pill-err)">{ico("alert")}</span>Не удалось скопировать — выделите текст и скопируйте вручную</span>'
    else:
        b = f'<span class="nbtn p">{ico("copy")}Скопировать для чата</span>'
    adm = '<div class="muted" style="font-size:11.5px;margin-top:4px">Текст задан администратором</div>' if state == 'admin' else ''
    return (f'<div class="ntf" style="width:340px"><div class="nh">{ico("chat","ico","width:13px;height:13px")}Astra Voice<span class="x">{ico("x")}</span></div>'
            f'<div class="nt">Предупредите участников о записи</div><div class="txtbox">{E(txt)}</div>{adm}'
            f'<div class="nb">{b}</div><div class="muted" style="margin-top:8px">И скажите это вслух в начале встречи.</div>'
            f'<div class="muted" style="margin-top:6px;padding-top:6px;border-top:1px solid var(--nt-border)">Ваш микрофон записывается всё время записи — выключенный микрофон в DION на запись не влияет. Чтобы не записывать, нажмите «Пауза».</div></div>')


def native(title, body='', buttons=()):
    bs = ''.join(f'<span class="nbtn{" p" if i == 0 else ""}">{E(x)}</span>' for i, x in enumerate(buttons))
    return (f'<div class="ntf native"><div class="nh">{mark().replace("22","14",2)}Astra Voice · сейчас<span class="x">{ico("x")}</span></div>'
            f'<div class="nt">{E(title)}</div>{"<div>" + E(body) + "</div>" if body else ""}{"<div class=nb>" + bs + "</div>" if bs else ""}</div>')


def tmenu(state):
    m = []
    if state in ('rec', 'paused', 'limit'):
        st = {'rec': 'Идёт запись встречи · 0:42:10', 'paused': 'Запись встречи на паузе · 0:42:10',
              'limit': 'Идёт запись встречи · остановится через 4:48'}[state]
        m.append(f'<div class="mi st live">{tray_svg("paused" if state == "paused" else "rec", 16)}{st}</div>')
        if state == 'limit':
            m.append(f'<div class="mi new">{ico("clock")}Продлить на час</div>')
        m.append(f'<div class="mi new">{ico("play" if state == "paused" else "pause")}{"Продолжить" if state == "paused" else "Пауза"}</div>')
        m.append(f'<div class="mi new">{ico("stop")}Остановить запись встречи</div>')
        m.append(f'<div class="mi new">{ico("chat")}Текст для участников…</div><div class="sepm"></div>')
        m.append('<div class="mi st">Готов · Ctrl + Space</div>')
    else:
        m.append(f'<div class="mi st">{tray_svg("idle", 16)}Готов · Ctrl + Space</div><div class="sepm"></div>')
    m.append('<div class="mi dis">Отмена</div><div class="mi">Модель<span class="k" style="font-family:var(--font-ui)">GigaAM v3 RNN-T ›</span></div><div class="mi">Скопировать последний текст</div><div class="sepm"></div>')
    if state == 'on':
        m.append(f'<div class="mi new">{ico("rec")}Записать встречу</div><div class="mi new">{ico("meeting")}Встречи…</div><div class="sepm"></div>')
    if state in ('rec', 'paused', 'limit'):
        m.append(f'<div class="mi new">{ico("meeting")}Встречи…</div><div class="sepm"></div>')
    m.append('<div class="mi">Настройки…<span class="k">Ctrl+,</span></div><div class="mi">Проверить обновления</div><div class="mi">О программе</div><div class="sepm"></div><div class="mi">Выход<span class="k">Ctrl+Q</span></div>')
    return f'<div class="tmenu">{"".join(m)}</div>'


def p04():
    b = []
    b.append('<div class="sec">1 · Карточка старта W4 — открывается из трея или кнопкой «Записать встречу» (US-13.4, S24-A1/A12)</div>')
    b.append(f'<div class="grid2" style="align-items:start"><div class="tile" style="align-items:center"><div class="tl" style="align-self:stretch">Идёт звонок в DION, вывод в колонки <code>call · speakers</code></div>{start_card("call")}</div>'
             f'<div class="tile" style="align-items:center"><div class="tl" style="align-self:stretch">Звонка нет — по умолчанию комната <code>room</code></div>{start_card("room", hint=False)}</div></div>')
    b.append('<div class="cap"><b>До «Начать запись» звук не открыт.</b> Выбранный вариант — рамка `primary` + точка радио + иконка (не только цвет). Фокус — на «Начать запись»; стрелки меняют вариант. '
             'Строка о тексте для участников — чтобы человек ждал карточку W3. Подсказка про гарнитуру — только при выводе в колонки (до итогов SP2).</div>')
    b.append('<div class="grid2" style="align-items:start">'
             f'<div class="tile"><div class="tl">Браузер <code>browser</code></div>{start_card("browser", hint=False)}</div>'
             f'<div class="tile"><div class="tl">SP1: звук не отделить <code>no-split</code></div>{start_card("call", hint=False, nosplit=True)}</div>'
             f'<div class="tile"><div class="tl">Старт ≤ 1 с <code>starting</code></div>{start_card("call", hint=False, starting=True)}</div>'
             f'<div class="tile"><div class="tl">Мало места <code>error-disk</code></div>{start_card("room", hint=False, error="disk")}<div class="tn">«Начать запись» недоступна, пока места нет; ничего не создано.</div></div>'
             f'<div class="tile"><div class="tl">Звуковая служба <code>error-audio</code></div>{start_card("room", hint=False, error="audio")}</div>'
             f'<div class="tile"><div class="tl">Папка недоступна <code>error-folder</code></div>{start_card("room", hint=False, error="folder")}</div>'
             f'<div class="tile"><div class="tl">Сетевой диск <code>warn-nfs</code> · можно начать</div>{start_card("room", hint=False, error="nfs")}<div class="tn">Решение владельца 12.10: предупреждение, не отказ. В карточке встречи — отметка «Хранится на сетевом диске».</div></div>'
             f'<div class="tile"><div class="tl">Звуковая служба не здесь <code>warn-remote-audio</code> · можно начать</div>{start_card("room", hint=False, error="remote-audio")}<div class="tn">Отметка в карточке встречи — «Звук шёл через другой компьютер».</div></div>'
             f'<div class="tile"><div class="tl">Блокировку не узнать (Fly) <code>error-lock-unknown</code> · BL-5 п. 3</div>{start_card("room", hint=False, error="lock-unknown")}</div>'
             '</div>')
    b.append('<div class="cap"><b>W4 — 460 по ширине во всех состояниях</b>; высота — по содержимому (замер с PT Root UI: 371 комната · 437–472 с баннером ошибки или предупреждения · 523 звонок с подсказкой про гарнитуру — максимум), '
             '<b>не больше 600</b>: при большем список вариантов прокручивается, заголовок, строка «Микрофон», баннеры и кнопки — на месте. Низ — паддинг 12 16 14 (как тело W4).</div>')
    b.append('<div class="sec">2 · Запись идёт: плашка W2 сверху по центру + карточка «Предупредите участников» W3 под ней</div>')
    b.append(desk(f'<div class="ov" style="left:50%;top:12px;transform:translateX(-50%);display:flex;flex-direction:column;align-items:center;gap:8px">{rbar()}{notice()}</div>', 470))
    b.append('<div class="cap"><b>W2</b> — как пилюля: своё окно, без фокуса, поверх всех; палитра фиксированная. Место — сверху по центру (В2, решение владельца). Скрыть нельзя: нет «×»; плашка и значок трея не читают настройку пилюли «Индикатор» (T1 BL-4); при закрытом окне настроек остаются. '
             'Свернуть — «˄» (до знака и таймера); перетаскивается за «ручку» слева. «💬» снова открывает W3. Знак записи — кольцо с точкой (форма), бирюза — потому что открыт микрофон, как у «Слушаю»; красный — только ошибки.<br>'
             '<b>W3</b> появляется при каждом начале записи. «Скопировать для чата» кладёт ровно этот текст; программа нигде не пишет, что участники предупреждены.</div>')
    b.append('<div class="sec">3 · Состояния плашки W2</div>')
    st = [('Запись <code>recording</code>', rbar()), ('Пауза <code>paused</code>', rbar('paused')),
          ('Свёрнута <code>compact</code>', rbar('compact') + '&nbsp;&nbsp;' + rbar('compact-p', '0:42:10')),
          ('За 5 минут до предела <code>limit-soon</code>', rbar('limit')),
          ('Микрофон сменился <code>mic-paused</code>', rbar('mic')),
          ('Микрофон с длинным названием — кнопка без имени, имя в тексте', rbar('mic-long')),
          ('Свернуть / развернуть — глифы и подсказки', tipdemo()),
          ('Программа видеосвязи закрылась <code>app-closed</code> · 8 с', rbar('app')),
          ('Остановка ≤ 1 с <code>stopping</code>', rbar('stop')),
          ('Сбой звуковой службы <code>error</code> · 3 с', rbar('err')),
          ('Экран был заблокирован <code>paused-lock</code> · T1 BL-5', rbar('lock')),
          ('Индикатор пропадал <code>paused-indicator</code> · T1 BL-4, 8 с после возврата', rbar('lost')),
          ('Программа не отвечала <code>paused-hang</code> · T1 BL-4 п. 5, 8 с', rbar('hang')),
          ('Fly без службы уведомлений <code>notify-fallback</code> · 8 с', rbar('fallback'))]
    b.append('<div class="grid2" style="grid-template-columns:1fr">' + ''.join(f'<div class="tile"><div class="tl">{t}</div><div style="background:linear-gradient(160deg,var(--desk1),var(--desk2));border-radius:8px;padding:16px;display:flex;justify-content:center;flex-wrap:wrap;gap:8px">{x}</div></div>' for t, x in st) + '</div>')
    b.append('<div class="legend"><b>Важные события — в самой плашке, не только в уведомлении:</b> «Продлить на час» и «Продолжить с „…“» есть прямо на W2, потому что во Fly кнопки уведомлений не гарантированы, а служба уведомлений может быть недоступна (S17-A4). '
             'Продление — до 4:00:00; на последнем часе кнопки «Продлить» нет. Пауза: таймер стоит (серый), звук закрыт; через 60 мин паузы встреча сохраняется.<br>'
             '<b>T1 BL-5:</b> блокировка экрана (или неизвестно, заблокирован ли он) → пауза за ≤ 1 с; после разблокировки — только «Продолжить» нажатием. '
             '<b>BL-4:</b> пропали и значок трея, и плашка (> 1 с) или программа зависла (> 5 с) → пауза; вернулась индикация — продолжение само, с отметкой и строкой на плашке. '
             'Таймер — PT Mono с табличными цифрами, ширина плашки не прыгает каждую секунду.</div>')
    b.append('<div class="sec">4 · Карточка «Предупредите участников» W3</div>')
    b.append('<div class="grid2">' + ''.join(f'<div class="tile"><div class="tl">{t}</div><div style="background:linear-gradient(160deg,var(--desk1),var(--desk2));border-radius:8px;padding:16px;display:flex;justify-content:center">{notice(k)}</div></div>'
                                             for t, k in [('По умолчанию <code>default</code>', 'default'), ('После копирования, 1,2 с <code>copied</code>', 'copied'),
                                                          ('Текст администратора <code>meeting_notice_text</code>', 'admin'), ('Буфер обмена недоступен <code>copy-failed</code>', 'fail')]) + '</div>')
    b.append('<div class="sec">5 · Трей: значок и меню</div>')
    icons = ''.join(f'<div class="tile" style="align-items:center"><div class="tl">{t}</div><div style="display:flex;gap:10px">'
                    f'<span class="trayicon">{tray_svg(s, 22)}</span><span class="trayicon">{tray_svg(s, 16)}</span><span class="trayicon light">{tray_svg(s, 22)}</span><span class="trayicon light">{tray_svg(s, 16)}</span></div>'
                    f'<div style="display:flex;gap:12px;align-items:end;color:var(--fg1)"><span style="background:repeating-conic-gradient(var(--bg-sunk) 0 25%,transparent 0 50%) 0 0/8px 8px">{tray_svg(s, 22, 88)}</span>'
                    f'<span style="background:repeating-conic-gradient(var(--bg-sunk) 0 25%,transparent 0 50%) 0 0/8px 8px">{tray_svg(s, 16, 64)}</span></div>'
                    f'<div class="tn" style="font-size:11px">22 и 16 — отдельные рисунки, ×4</div>'
                    f'<span class="tip" style="position:static">{tp}</span></div>'
                    for t, s, tp in [('Готов', 'idle', 'Astra Voice — готов · Ctrl + Space'), ('Запись встречи', 'rec', 'Идёт запись встречи · 0:42:10'),
                                     ('Запись на паузе', 'paused', 'Запись встречи на паузе · 0:42:10'), ('Слушаю (для сравнения)', 'listening', 'Слушаю…')])
    b.append(f'<div style="align-self:stretch;display:grid;grid-template-columns:repeat(4,1fr);gap:16px">{icons}</div>')
    b.append('<div class="cap"><b>Новые состояния значка отличаются формой третьей точки:</b> кольцо с точкой (запись) и две черты (пауза) — против одной точки у «Слушаю». '
             'Геометрия — канон `design/spec.md` §9.1: точки 1–2 и строка без изменений; 22 и 16 нарисованы <b>отдельно</b> (16 — в пиксельной сетке: кольцо 6 px, черты 2×4 px), без масштабирования. '
             'Файлы-предложения: `design/meetings/icons/{22x22,16x16}/astravoice-tray-meeting-{rec,paused}.svg` (контракт Plasma `currentColor` сохранён). '
             'Расшифровка в фоне значок не меняет (диктовка доступна) — во второй строке подсказки: «Расшифровка встречи: 40 %».</div>')
    b.append('<div style="align-self:stretch;display:grid;grid-template-columns:repeat(2,1fr);gap:16px;align-items:start">'
             f'<div class="tile"><div class="tl">Функция включена, записи нет</div>{tmenu("on")}</div>'
             f'<div class="tile"><div class="tl">Идёт запись</div>{tmenu("rec")}</div>'
             f'<div class="tile"><div class="tl">На паузе</div>{tmenu("paused")}</div>'
             f'<div class="tile"><div class="tl">За 5 минут до предела <code>limit-soon</code></div>{tmenu("limit")}'
             '<div class="tn">«Продлить на час» — дубль кнопки плашки: все действия W2 доступны из трея (во Fly у уведомлений нет кнопок). '
             'Так же при смене микрофона появляется «Продолжить с „Встроенный микрофон“».</div></div></div>')
    b.append('<div class="cap">Полоска слева у пункта — только стенд (новые пункты). Функция выключена или `meeting_record=off` — меню как сейчас, без пунктов встреч.</div>')
    b.append('<div class="sec">6 · Уведомления (штатные KDE / Fly) — тексты spec.md §4.6</div>')
    nts = [('Встреча сохранена. Делаю расшифровку…', '', ('Открыть встречи',)), ('Расшифровка готова: Встреча 24.09.2026 14:00', '', ('Открыть',)),
           ('Запись остановится через 5 минут', '', ('Продлить на час',)), ('Микрофон сменился. Запись вашего голоса приостановлена', '', ('Продолжить с „Встроенный микрофон“', 'Остановить запись')),
           ('Запись встречи остановлена: достигнут предел длительности', '', ('Открыть встречи',)), ('Запись остановлена: заканчивается место на диске. Записанное сохранено', '', ('Открыть встречи',)),
           ('Запись на паузе: экран был заблокирован', 'Продолжить можно только вручную', ('Продолжить',)),
           ('Запись встречи на паузе: индикатор записи не отображается', 'Продолжится сама, когда значок или плашка снова появятся', ()), ('Запись встречи остановлена перед сном компьютера. Записанное сохранено', '', ('Открыть встречи',)),
           ('Запись встречи прервалась. Сохранено 0:37:02', '', ('Расшифровать', 'Удалить')), ('Удалено встреч по сроку хранения: 3', '', ())]
    b.append('<div class="grid2" style="background:linear-gradient(160deg,var(--desk1),var(--desk2));border-radius:10px;padding:16px">' + ''.join(native(*n) for n in nts) + '</div>')
    b.append('<div class="sec">7 · Выход во время записи</div>')
    dlg = (f'<div class="dlg" style="width:420px"><div class="dh">{mark()}Astra Voice</div><div class="db"><div style="display:flex;gap:13px">'
           f'<span class="warn-t">{ico("alert","ico","width:22px;height:22px")}</span><div><div style="font-weight:500">Идёт запись встречи. Остановить запись и выйти?</div>'
           f'<div style="font-size:13px;color:var(--fg2);margin-top:4px">Записанное сохранится и будет расшифровано при следующем запуске.</div></div></div></div>'
           f'<div class="df"><span class="sp"></span>{btn("Отмена", "focus")}{btn("Остановить и выйти", "pri")}</div></div>')
    b.append(f'<div class="tile" style="align-self:stretch;align-items:center;background:var(--bg-app)">{dlg}</div>')
    return page('Встречи — запись', '04 · Запись встречи: старт, плашка, участники, трей, уведомления',
                'US-13.4, 13.5, 13.6; S24. Одно направление: плашка повторяет язык пилюли диктовки. Тексты — spec.md §4.4–4.6, состояния — §3.4–3.6, §3.9.', ''.join(b), NAV)
