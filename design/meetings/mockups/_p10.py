"""10 · «Разделить заново» — карточка встречи после сбоя разделения по голосам и повторное разделение
(дополнение 12.11.2026 к spec.md, веха E13.M7; T-485, T-436). Сборка отдельно: python3 -I _p10.py
(пишет 10-rediarize.html рядом). Собирается и общим _build.py. Новых цветов, токенов и компонентов нет:
строка над таблицей — та же, что в §У.4 (09), диалог — канон D2, меню «⋯» — §5.2."""
import os
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _base import *
from _p3 import TITLE, LINES, table
from _p4 import dialog
from _p5 import NAMED, head as named_head, ppanel
from _p9 import DK_CSS, bar, frame, tiles

PREFIX = 'Разделение по голосам: '
ONE = {'me': 'Я', 's1': 'Собеседники', 's2': 'Собеседники', 's3': 'Собеседники'}
ROOM = {'me': 'Участники', 's1': 'Участники', 's2': 'Участники', 's3': 'Участники'}
KEPT = 'звук хранится до разделения'

# state → (вид текста, текст, полоса %, кнопки)
#   вид: '' — fgMuted; 'err' — dangerInk + alert 12; 'ok' — fgMuted + check 12 successInk; 'warn' — fgMuted + alert 12 warningInk
#   кнопки: список (текст, вид); focus=N — кольцо фокуса на N-й кнопке (с 1)
TWO = [('Оставить так', ''), ('Разделить заново', '')]
R = {
    'failed':       ('err', PREFIX + 'не удалось — собеседники подписаны «Собеседники». Звук сохранён, можно попробовать ещё раз', None, TWO),
    'failed-room':  ('err', PREFIX + 'не удалось — все реплики подписаны «Участники». Звук сохранён, можно попробовать ещё раз', None, TWO),
    'queued':       ('', PREFIX + 'начнётся после текущей расшифровки', None, [('Отменить', '')]),
    'memory':       ('', PREFIX + 'подождёт — сейчас мало свободной памяти', None, [('Отменить', '')]),
    'running':      ('', PREFIX + 'разделяю заново · 35 %', 35, [('Отменить', '')]),
    'running-first': ('', PREFIX + 'разделяю · 35 %', 35, [('Отменить', '')]),
    'refining':     ('', PREFIX + 'уточняю реплики · 92 %', 92, [('Отменить', '')]),
    'cancelled':    ('', PREFIX + 'отменено — собеседники подписаны «Собеседники»', None, TWO),
    'failed-again': ('err', PREFIX + 'снова не удалось — собеседники подписаны «Собеседники». Звук сохранён, можно повторить позже', None, TWO),
    'no-audio':     ('', PREFIX + 'не удалось — собеседники подписаны «Собеседники». Записи звука нет, разделить заново нельзя', None, []),
    'done':         ('ok', 'Собеседники разделены по голосам — можно вписать имена участников', None, []),
    'redo-failed':  ('err', PREFIX + 'разделить заново не удалось — оставлены прежнее разделение и имена', None, []),
    'confirm':      ('warn', 'Имена участников и объединения сбросятся и не перенесутся — номера спикеров могут поменяться', None, [('Отмена', ''), ('Разделить заново', 'dan')]),
    'offer':        ('', PREFIX + 'эту встречу можно разделить, пока хранится звук (до 19.11)', None, [('Разделить по голосам', '')]),
    'offer-new':    ('ok', 'Модели установлены — эту встречу тоже можно разделить по голосам', None, [('Разделить по голосам', '')]),
    'kept-0':       ('ok', PREFIX + 'оставлено как есть. Звук удалён по настройке хранения', None, []),
    'kept-7':       ('ok', PREFIX + 'оставлено как есть. Звук хранится до 19.11 — до этого встречу можно разделить', None, [('Разделить по голосам', '')]),
    'kept-30':      ('ok', PREFIX + 'оставлено как есть. Звук хранится до 12.12 — до этого встречу можно разделить', None, [('Разделить по голосам', '')]),
}


def crow(state, focus=0):
    """Строка над таблицей (канон §У.4): текст 13 занимает остаток и переносится; полоса 160×4 и кнопки sm справа, зазор 8."""
    kind, text, pct, buttons = R[state]
    if kind == 'err':
        tx = f'<span class="tx dst err-t">{ico("alert")}<span>{E(text)}</span></span>'
    elif kind == 'ok':
        tx = f'<span class="tx dst"><span class="ok-t">{ico("check")}</span><span>{E(text)}</span></span>'
    elif kind == 'warn':
        tx = f'<span class="tx dst"><span class="warn-t">{ico("alert")}</span><span style="color:var(--fg1)">{E(text)}</span></span>'
    else:
        tx = f'<span class="tx">{E(text)}</span>'
    p = bar(pct, 160) if pct is not None else ''
    bs = ''.join(btn(t, ('sm ' + k + (' focus' if focus == i + 1 else '')).strip()) for i, (t, k) in enumerate(buttons))
    return f'<div class="crow">{tx}{p}{bs}</div>'


def chead(bdg, sub, listen=False, save=True):
    pl = btn('Прослушать', '', 'play') if listen else ''
    sv = btn('Сохранить как…', 'pri', 'save') if save else ''
    return (f'<div style="font-size:13px;color:var(--primary);display:flex;align-items:center;gap:4px;margin-bottom:6px">{ico("chevl","ico","width:13px;height:13px")}Все встречи</div>'
            f'<div class="shead" style="margin-bottom:0"><div class="h" style="min-width:0"><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><div class="h2">{TITLE}</div>'
            f'<span class="mut">{ico("edit","ico","width:14px;height:14px")}</span>{bdg}</div>'
            f'<div class="ssub">09.10.2026 · 10:00–11:12 · <span class="mono">1:12:40</span> · Звонок в DION · <span class="t">{sub}</span></div></div>'
            f'<div style="display:flex;gap:8px">{pl}{sv}{btn("", "icon", "more")}</div></div><div style="height:10px"></div>')


READY = badge('Готово', 'b-ok', 'check')
RUN = badge('Разделение по голосам · <span class="mono">35 %</span>', 'b-warn', 'clock')
NOTE_RUN = 'Имена можно вписать после разделения по голосам.'


def panel_plain(w=232, foot=''):
    """Панель «Участники» без разделения (существующий вид no-diarization): «Я» и «Собеседники», полей имён нет."""
    def item(lab, mins, sub, me=False):
        c = 'color:var(--accent-ink);' if me else ''
        s = f'<div class="mut" style="font-size:11.5px">{sub}</div>' if sub else ''
        return (f'<div style="padding:9px 12px;border-top:1px solid var(--border-soft)"><div style="display:flex;gap:6px;align-items:baseline">'
                f'<b style="font-weight:500;{c}">{lab}</b><span class="mut mono" style="font-size:11.5px;margin-left:auto">{mins}</span></div>{s}</div>')
    f = f'<div style="padding:8px 12px 10px;border-top:1px solid var(--border-soft);font-size:11.5px;color:var(--fg3)">{foot}</div>' if foot else ''
    return (f'<div class="card" style="width:{w}px;flex:none;align-self:flex-start"><div style="padding:9px 12px;display:flex;align-items:center;gap:6px">'
            f'<span class="h3" style="font-size:14px">Участники</span><span style="margin-left:auto" class="mut">{ico("people")}</span></div>'
            + item('Я', '18 мин', 'Ваш микрофон — подпись из настроек', True) + item('Собеседники', '54 мин', '') + f + '</div>')


def strip_plain():
    return ('<div class="card" style="padding:7px 10px 7px 12px;display:flex;gap:12px;align-items:center;margin-bottom:10px;flex-wrap:wrap">'
            '<span class="mut" style="font-size:12px">Участники:</span><span style="font-size:12.5px">Я</span><span style="font-size:12.5px">Собеседники</span>'
            f'<span style="flex:1"></span>{btn("Имена участников", "sm dis", "people")}</div>')


def card(state, width=1024, height=620, bdg=READY, sub=KEPT, focus=0, foot='', names=ONE, n=8):
    lines = [l for l in LINES if l[1] != 'mark'][:n]
    top = chead(bdg, sub) + crow(state, focus)
    if width >= 990:
        main = f'<div style="display:flex;gap:12px;align-items:flex-start"><div style="flex:1;min-width:0">{table(lines, names)}</div>{panel_plain(232, foot)}</div>'
    else:
        main = strip_plain() + table(lines, names)
    return window(top + main, width=width, height=height)


def cframe(state, w=796, focus=0, names=ONE):
    return frame('<div style="height:10px"></div>' + crow(state, focus) + table([l for l in LINES if l[1] != 'mark'][:2], names), w)


# ── уже разделённая встреча с именами (звук хранится) ─────────────────────
def menu_new(disabled=False):
    it = (f'<div class="mi" style="color:var(--fgdis)">{ico("refresh")}Разделить заново…</div>' if disabled
          else f'<div class="mi" style="background:var(--primary-bg)">{ico("refresh")}Разделить заново…</div>')
    return (f'<div class="tmenu" style="position:absolute;right:22px;top:76px;width:236px;z-index:6">'
            f'<div class="mi">{ico("edit")}Переименовать…</div><div class="mi">{ico("people")}Имена участников</div>{it}'
            f'<div class="sepm"></div><div class="mi err-t">{ico("trash")}Удалить встречу…</div></div>')


def named_card(extra_top='', overlay='', panel_btn=False, width=1024, height=620):
    hd = named_head(sub_extra=' · <span class="t">звук хранится до 19.10</span>', listen=True)
    pp = ppanel(232, 'ready')
    if panel_btn:
        mb = btn('Это один человек…', 'gh sm', 'merge')
        pp = pp.replace(mb, mb + '<div style="height:4px"></div>' + btn('Разделить заново…', 'gh sm focus', 'refresh'))
    main = f'<div style="display:flex;gap:12px;align-items:flex-start"><div style="flex:1;min-width:0">{table(LINES[:7], NAMED)}</div>{pp}</div>'
    return window(hd + extra_top + main + overlay, width=width, height=height)


CONFIRM_DLG = dialog(
    'Разделить заново',
    f'<div style="font-weight:500">Разделить встречу «{TITLE}» по голосам заново?</div>'
    '<div style="font-size:13px;color:var(--fg2)">Имена участников и объединения «Это один человек» сбросятся: номера спикеров могут поменяться, '
    'поэтому имена не переносятся сами. Сейчас заданы: Ирина Соколова, Олег Ли.</div>'
    '<div style="font-size:13px;color:var(--fg2)">Пока идёт разделение, встречу можно читать и сохранять. Если отменить или разделить не получится — всё останется как сейчас.</div>',
    [('Отмена', 'focus'), ('Разделить заново', 'dan')], icon='people', icls='warn-t')


def kframe(state, sub, listen=False, focus=0, w=796):
    """Строка в карточке вместе с шапкой — чтобы видеть подстроку и «Прослушать»."""
    return frame(chead(READY, sub, listen) + crow(state, focus) + table([l for l in LINES if l[1] != 'mark'][:2], ONE), w)


REJ = '<span class="badge b-err" style="margin-left:8px;text-transform:none;letter-spacing:0">отклонено 12.11</span>'


def p10():
    b = [DK_CSS]
    b.append(note('<b>Принято владельцем 12.11.2026.</b> После сбоя — строка над таблицей, как у установки моделей (09, §У.4): причина простыми словами и две sm-кнопки '
                  '<b>«Оставить так» · «Разделить заново»</b>; прогресс и «Отменить» — в той же строке. Имён у такой встречи нет — запуск <b>сразу, без предупреждения</b>. '
                  'У уже разделённой встречи, пока хранится звук, — пункт «Разделить заново…» в «⋯» и <b>диалог</b> о сбросе имён (вариант А). '
                  'Встрече без разделения, пока хранится звук, — кнопка <b>«Разделить по голосам»</b>. Имена сбрасываются <b>в момент готовности</b> нового разделения. '
                  'Тексты и правила — <code>spec.md</code>, «Дополнение 12.11.2026», решения — §Р.10. Новых цветов, токенов и компонентов нет.', 'ok', 'check')
             .replace('class="note ok"', 'class="note ok" style="align-self:stretch"'))

    b.append('<div class="sec" id="s1">1 · Разделение не удалось — 1024 × 620 (<code>diar-failed</code>, processing_status partial)</div>')
    b.append(card('failed', focus=2))
    b.append('<div class="cap">Текст встречи готов, поэтому бейдж «Готово»; подстрока — «звук хранится до разделения». Строка — канон §У.4: 13 <code>dangerInk</code> + <code>alert</code> 12. '
             'Кнопки — <b>«Оставить так» · «Разделить заново»</b>, обе <code>AvButton</code> sm не primary, зазор <b>8</b>, прижаты вправо; правая — тот же элемент, что во время работы становится «Отменить» '
             '(не сдвигается, фокус сохраняется). Автофокуса нет; кольцо фокуса показано на правой кнопке. Панель «Участники» — существующий вид без разделения.</div>')

    b.append('<div class="sec" id="s1n">1 · То же — 900 × 588 (содержимое 672): переносится только текст, кнопки не сжимаются</div>')
    b.append(card('failed', width=900, height=588))

    b.append('<div class="sec" id="s2">2 · Строка над таблицей — все состояния (ширина содержимого 796)</div>')
    items = [
        ('failed', 'Не удалось', 'diar failed · partial', '«Разделить заново» — сразу запуск: имён нет, предупреждать не о чем. «Оставить так» — итог в §6.'),
        ('failed-room', 'Не удалось — встреча в комнате', 'diar failed · mic_only', 'Одна дорожка: «Я» нет, все реплики — «Участники».'),
        ('queued', 'Запущено, ждёт очереди', 'queued', '«Оставить так» скрыта; правая кнопка — теперь «Отменить», фокус остаётся на ней.'),
        ('memory', 'Запущено, ждёт памяти', 'wait-memory', 'Текст — как «Расшифровка подождёт…» (§4.7) с приставкой.'),
        ('running', 'Идёт разделение', 'running · 0..1', 'Полоса <b>160 × 4</b> встаёт на место «Оставить так». Процент — общий на оба этапа, не откатывается.'),
        ('refining', 'Уточнение реплик', 'reconcile', 'Второй этап (повторное распознавание мест, где говорили двое) — только смена слова, полоса продолжается.'),
        ('cancelled', 'Отменено', 'cancelled', 'Всё как до запуска; текст держится до закрытия карточки, потом — «Не удалось».'),
        ('failed-again', 'Снова не удалось', 'diar failed ×2', 'Обе кнопки на месте; фокус остаётся на правой («Отменить» → «Разделить заново»).'),
        ('no-audio', 'Звука нет', 'audio.present=false', 'Кнопок нет. <code>fgMuted</code>, без глифа. Подстрока шапки — «звук удалён».'),
        ('done', 'Готово', 'diar done', 'До закрытия карточки: <code>check</code> 12 <code>successInk</code>. «Спикер N» в таблице, поля имён в панели. Фокус — на таблицу. При следующем открытии строки нет.'),
    ]
    b.append(tiles(items, lambda st: cframe(st, focus=2 if st == 'failed-again' else (1 if st == 'running' else 0), names=ROOM if st == 'failed-room' else (
        {**NAMED, 's1': 'Спикер 1', 's2': 'Спикер 2'} if st == 'done' else ONE))))
    b.append('<div class="st"><div class="tl">900 × 588 — ширина содержимого 672<code>running / failed-again</code></div>'
             + cframe('running', 672) + cframe('failed-again', 672) + '</div>')

    b.append('<div class="sec" id="s3">3 · Повторное разделение идёт — 1024 × 620</div>')
    b.append(card('running', bdg=RUN, sub='звук хранится до конца разделения', foot=NOTE_RUN, focus=1))
    b.append('<div class="cap">Бейдж — как у расшифровки: <code>warningBg</code> + <code>clock</code> «Разделение по голосам · 35 %» (и в строке списка встреч). '
             'Таблица <b>не меняется</b> до самого конца — новые подписи появляются разом. «Сохранить как…», «Переименовать» и «Удалить встречу…» доступны; '
             '«Имена участников» и «Разделить заново…» в «⋯» недоступны. Окно можно закрыть — разделение продолжится; при выходе из программы начнётся с начала при следующем запуске.</div>')

    b.append('<div class="sec" id="s4">4 · Уже разделённая встреча с именами, звук хранится: «⋯» → «Разделить заново…» → диалог (принято, вариант А)</div>')
    b.append(f'<div class="st"><div class="tl">А · меню «⋯» → диалог · принято 12.11</div>{named_card(overlay=menu_new())}'
             f'<div style="display:flex;justify-content:center;padding:16px;border-radius:10px;background:var(--canvas)">{CONFIRM_DLG}</div>'
             '<div class="cap">Пункт «Разделить заново…» — в «⋯» после «Имена участников»; виден, только когда звук хранится, модели установлены и разделение не выключено; '
             'пока разделение идёт — недоступен. Диалог — канон D2 (420, глиф 22, «Отмена» в фокусе, вторая кнопка — secondary + <code>dangerInk</code>).</div></div>')
    b.append(f'<div class="st"><div class="tl">Повтор у разделённой встречи не удался<code>redo failed</code></div>{cframe("redo-failed", names=NAMED)}'
             '<div class="cap">Прежнее разделение и имена остаются (сброс — только в момент готовности нового), кнопки нет: повторить — снова из «⋯». '
             'Если имён и объединений нет (все «Спикер N») — пункт без многоточия, «Разделить заново», запуск сразу, без диалога.</div></div>')
    b.append(f'<div class="st" style="opacity:.55"><div class="tl">Б · подтверждение в строке над таблицей{REJ}</div>{named_card(extra_top=crow("confirm", focus=1))}'
             '<div class="cap">Отклонено владельцем 12.11.2026 — оставлено для истории, не реализуется.</div></div>')
    b.append(f'<div class="st" style="opacity:.55"><div class="tl">Б′ · кнопка в панели «Участники»{REJ}</div>{named_card(panel_btn=True, height=None)}'
             '<div class="cap">Отклонено владельцем 12.11.2026 — вход только из «⋯».</div></div>')

    b.append('<div class="sec" id="s5">5 · «Разделить по голосам» — встреча без разделения, звук хранится</div>')
    b.append(f'<div class="st"><div class="tl">Карточка, открытая заново<code>offer</code></div>{kframe("offer", "звук хранится до 19.11", listen=True, focus=1)}</div>')
    b.append(f'<div class="st"><div class="tl">Сразу после установки моделей в этой карточке<code>installed + audio kept</code></div>{cframe("offer-new")}</div>')
    b.append(f'<div class="st"><div class="tl">После нажатия — первый раз<code>running</code></div>{cframe("running-first", focus=1)}</div>')
    b.append('<div class="cap">Встреча расшифрована без моделей (или оставлена «как есть», §6) и звук ещё хранится (7 / 30 дней), модели установлены, разделение включено. '
             'Одна sm-кнопка справа, не primary; имён нет — без предупреждения. Дальше — те же состояния, что в §2, со словом «разделяю» вместо «разделяю заново»; '
             'отмена возвращает эту строку. Звука нет — строки нет.</div>')

    b.append('<div class="sec" id="s6">6 · «Оставить так» — итог после нажатия (по настройке «Хранить запись звука после расшифровки»)</div>')
    b.append(f'<div class="st"><div class="tl">До нажатия, фокус на «Оставить так»<code>failed</code></div>{kframe("failed", "звук хранится до разделения", focus=1)}</div>')
    b.append(f'<div class="st"><div class="tl">«Не хранить» (по умолчанию)<code>kept · retention 0</code></div>{kframe("kept-0", "звук удалён")}'
             '<div class="cap">Звук удаляется сразу, как после успешной расшифровки. Кнопок нет, фокус — на таблицу. При следующем открытии строки нет.</div></div>')
    b.append(f'<div class="st"><div class="tl">7 дней (нажато 12.11)<code>kept · retention 7</code></div>{kframe("kept-7", "звук хранится до 19.11", listen=True, focus=1)}'
             '<div class="cap">Срок считается от нажатия. Появляется «Прослушать». Фокус переходит на «Разделить по голосам». При следующем открытии — строка §5 до 19.11, потом строки нет.</div></div>')
    b.append(f'<div class="st"><div class="tl">30 дней (нажато 12.11)<code>kept · retention 30</code></div>{kframe("kept-30", "звук хранится до 12.12", listen=True)}</div>')
    b.append('<div class="cap">Без подтверждения: звук после сбоя держится только ради повтора, цену нажатия называют описание кнопки для диктора и итоговая строка. '
             'Встреча становится обычной готовой встречей без разделения (processing_status succeeded, <code>finished_at</code> = момент нажатия). '
             'Строка списка встреч — «Готово», «звук удалён» / «звук до 19.11». Администратор задал срок — как соответствующая плитка.</div>')

    b.append('<div class="sec" id="s7">7 · Клавиатура и экранный диктор</div>')
    sr = [('Порядок Tab, карточка', '«Сохранить как…» → «⋯» → «Оставить так» → «Разделить заново» (или одна кнопка строки) → таблица реплик → панель «Участники»'),
          ('Кнопка «Разделить заново»', 'имя «Разделить собеседников по голосам заново»; описание — текст строки; Enter / Пробел'),
          ('Кнопка «Оставить так»', 'имя «Оставить без разделения по голосам»; описание — «Звук удалится сразу, разделить заново будет нельзя» («Не хранить») или «Звук будет храниться до 19.11» (7 / 30 дней)'),
          ('Кнопка «Отменить»', 'имя «Отменить разделение по голосам»; Esc разделение не отменяет'),
          ('Кнопка «Разделить по голосам»', 'имя «Разделить собеседников этой встречи по голосам»; описание — «Звук хранится до 19.11»'),
          ('Полоса', 'роль ProgressBar, имя «Разделение по голосам», значение 0–100, для диктора не чаще раза в секунду'),
          ('Текст строки', 'StaticText; при сбое, отмене, готовности и после «Оставить так» — AlertMessage, читается один раз (образец — UpdatePanel)'),
          ('Фокус после итога', 'кнопка сменила текст — фокус остаётся; «Оставить так» исчезла — на правую кнопку, если она есть, иначе на таблицу'),
          ('Пункт меню', '«Разделить заново…» (с именами) / «Разделить заново» (без имён); недоступен, пока разделение идёт'),
          ('Диалог', 'фокус на «Отмена»; Esc и Enter на «Отмена» закрывают без запуска; после закрытия фокус — на «⋯»; после запуска — на «Отменить» в строке')]
    t = ''.join(f'<tr><td style="white-space:nowrap">{a}</td><td>{E(x)}</td></tr>' for a, x in sr)
    b.append(f'<div class="card" style="align-self:stretch;padding:4px 6px"><table class="mtbl"><tr><th>Где</th><th>Что</th></tr>{t}</table></div>')
    b.append('<div class="legend">Окна §1, §3, §4 — в размере продукта (1024×620 / 900×588), низ обрезан, как при прокрутке. Тёмная тема — переключатель вверху или <code>#dark</code> в адресе.</div>')
    return page('Встречи — разделить заново', '10 · «Разделить заново»: сбой разделения по голосам и повтор',
                'Дополнение 12.11.2026 к G3, веха E13.M7 (T-485, T-436) — принято владельцем 12.11.2026. Тексты и правила — <code>spec.md</code>, «Дополнение 12.11.2026».', ''.join(b), NAV)


if __name__ == '__main__':
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, '10-rediarize.html'), 'w', encoding='utf-8') as f:
        f.write(p10())
    print('10-rediarize.html')
