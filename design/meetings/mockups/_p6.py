"""08 · Пометки у реплик: «возможное эхо» (D7, 10.11.2026) и «несколько говорящих» (Д5).
Дополнение к spec.md (раздел «Дополнение 10.11.2026»), принято владельцем 10.11.2026: пометка под именем +
приглушённый текст реплики-эха (вариант Б; вариант А отклонён), галочка в D4 — в v1.1."""
import re
from _base import *
from _p5 import head as head_a, ppanel, strip
from _p3 import marks as marks_g3
from _p4 import tpl_window, loc_preview

# Тёмная тема в отдельной рамке рядом со светлой: те же значения, что body.dark в _base.CSS.
_DARK = re.search(r'body\.dark\{(.*?)\}', CSS, re.S).group(1)
DK_CSS = ('<style>.dk{' + _DARK + 'color:var(--canvas-fg)}'
          '.dk .wbtn.c{background:#8C3B36}'
          '.tr .flag{display:flex;align-items:flex-start;gap:4px;font-size:12px;line-height:1.5;font-weight:400;'
          'color:var(--fg3);white-space:normal}'
          '.tr .flag .ico{width:12px;height:12px;margin-top:3px}'
          '.tr .whoc{display:flex;flex-direction:column;min-width:0;padding-top:1px}'
          '.tr .whoc .who{padding-top:0}'
          '.tr.echo-b .tx{color:var(--fg3)}'
          '.md{font-family:var(--font-mono);font-size:11.5px;line-height:1.55;white-space:pre-wrap;word-break:break-word;'
          'background:var(--bg-surface);border:1px solid var(--border);border-radius:8px;padding:10px 12px;color:var(--fg1);margin:0}'
          '.md .hl{background:var(--warn-bg);border-radius:3px}'
          '.mtbl{width:100%;border-collapse:collapse;font-size:12.5px;color:var(--fg1)}'
          '.mtbl td,.mtbl th{border-top:1px solid var(--border-soft);padding:5px 8px;text-align:left;vertical-align:top}'
          '.mtbl th{font-weight:500;color:var(--fg3);font-size:11.5px}'
          '</style>')

NAMES = {'me': 'Я', 's1': 'Ирина Соколова', 's2': 'Олег Ли', 's3': 'Спикер 3'}
NUM = {'s1': '1', 's2': '2', 's3': '3'}
# (время, кто, текст, пометка) — пометка: '' | 'echo' | 'multi'.
# Колонки: эхо идёт сразу за оригиналом (медиана задержки 88 мс); 0:00:22 и 0:00:27 — две реплики-эха
# одной длинной реплики Олега (подряд); 0:00:47 — НАСТОЯЩАЯ речь владельца, ошибочно помеченная
# (двойной разговор) — ради неё пометка не удаляет и не прячет текст.
LINES = [
    ('0:00:04', 'me', 'Коллеги, добрый день. Я веду запись и расшифровку этой встречи для протокола.', ''),
    ('0:00:12', 's1', 'Добрый день. Тогда начнём с бюджета на четвёртый квартал.', ''),
    ('0:00:13', 'me', 'Тогда начнём с бюджета на четвёртый квартал.', 'echo'),
    ('0:00:21', 's2', 'По подрядчикам у нас перерасход около двенадцати процентов, в основном из-за сдвига сроков по второму этапу.', ''),
    ('0:00:22', 'me', 'по подрядчикам у нас перерасход около двенадцати процентов', 'echo'),
    ('0:00:27', 'me', 'в основном из-за сдвига сроков по второму этапу', 'echo'),
    ('0:00:35', 'me', 'Олег, это уже с учётом договора на поддержку?', ''),
    ('0:00:39', 's2', 'Да, договор включён. Без него было бы около семи.', ''),
    ('0:00:47', 'me', 'Понятно. А что с серверами?', 'echo'),
    ('0:01:02', 's3', 'Предлагаю перенести закупку серверов на январь. — Согласна, тогда укладываемся в лимит.', 'multi'),
    ('0:01:15', 's1', 'Олег, подготовишь пересчёт к пятнице?', ''),
    ('0:01:20', 's2', 'Подготовлю, и сразу пришлю Марии.', ''),
]
ECHO_N = sum(1 for *_, f in LINES if f == 'echo')
FLAG = {'echo': ('speaker', 'возможное эхо'), 'multi': ('people', 'несколько говорящих')}


def table(variant='Б', lines=LINES):
    rows = ''
    for t, w, x, f in lines:
        num = f'<span class="sn">{NUM[w]}</span>' if w in NUM else ''
        who = f'<span class="who{" me" if w == "me" else ""}">{num}{E(NAMES[w])}</span>'
        if f:
            ic, lab = FLAG[f]
            who = f'<span class="whoc">{who}<span class="flag">{ico(ic)}<span>{lab}</span></span></span>'
        cls = ' echo-b' if (f == 'echo' and variant == 'Б') else ''
        rows += f'<div class="tr{cls}"><span class="tc">[{t}]</span>{who}<span class="tx">{E(x)}</span></div>'
    return f'<div class="card">{rows}</div>'


def marks_row(live=False):
    echo = ('Возможное эхо' if live else f'Возможное эхо: <span class="mono">{ECHO_N}</span> реплики')
    return ('<div style="display:flex;gap:6px;flex-wrap:wrap;margin:6px 0 10px">'
            + badge('Пауза <span class="mono">0:31:10 · 2 мин 52 с</span>', 'b-n', 'pause')
            + badge(echo, 'b-n', 'speaker') + '</div>')


def card(variant='Б', width=1024, layout='panel', live=False):
    hd = head_a(sub_extra=' · <span class="t">звук удалён</span>')
    hd = hd.replace(marks_g3(), marks_row(live))
    tbl = table(variant)
    if layout == 'panel':
        main = f'<div style="display:flex;gap:12px;align-items:flex-start"><div style="flex:1;min-width:0">{tbl}</div>{ppanel(232, "ready")}</div>'
    else:
        main = strip() + tbl
    return window(hd + main, width=width, height=None)


def md_file(kind='stenogram', fmt='md', excluded=False):
    """Фрагмент сохранённого файла (те же реплики, что в карточке; правила склейки — spec, Дополнение §Д.4)."""
    md = fmt == 'md'
    c = (lambda s: f'`{s}`') if md else (lambda s: s)
    li = '- ' if md else ''
    sub = '  - ' if md else '  '
    head = [('# ' + c('Планёрка по бюджету')) if md else 'Планёрка по бюджету', '',
            f'{li}Дата: 09.10.2026', f'{li}Начало: 10:00', f'{li}Длительность: 1:12:40']
    if kind == 'stenogram':
        head.append(f'{li}Участники: ' + ', '.join(c(n) for n in ('Я', 'Ирина Соколова', 'Олег Ли', 'Спикер 3')))
    elif kind == 'me_and_others':
        head.append(f'{li}Участники: {c("Я")}, {c("Собеседники")}')
    head.append(f'{li}Отметки:')
    head.append(f'{sub}Пауза 0:31:10 · 2 мин 52 с')
    head.append(f'{sub}' + ('Возможное эхо не включено в файл: 4 реплики' if excluded else 'Возможное эхо: 4 реплики'))
    head.append('')
    # Склейка: эхо 0:00:22 и 0:00:27 — одна группа (одна подпись «Я», зазор ≤ 2 с, обе с пометкой).
    merged = [
        ('0:00:04', 'me', LINES[0][2], ''),
        ('0:00:12', 's1', LINES[1][2], ''),
        ('0:00:13', 'me', LINES[2][2], 'echo'),
        ('0:00:21', 's2', LINES[3][2], ''),
        ('0:00:22', 'me', LINES[4][2] + ' ' + LINES[5][2], 'echo'),
        ('0:00:35', 'me', LINES[6][2], ''),
        ('0:00:39', 's2', LINES[7][2], ''),
        ('0:00:47', 'me', LINES[8][2], 'echo'),
        ('0:01:02', 's3', LINES[9][2], 'multi'),
        ('0:01:15', 's1', LINES[10][2], ''),
    ]
    body = []
    for t, w, x, f in merged:
        if excluded and f == 'echo':
            continue
        name = NAMES[w]
        if kind == 'me_and_others' and w != 'me':
            name = 'Собеседники'
        tag = ''
        if f == 'echo':
            tag = ' (возможное эхо)'
        elif f == 'multi' and kind == 'stenogram':
            tag = ' (несколько говорящих)'
        tag_h = f'<span class="hl">{tag}</span>' if tag else ''
        if kind == 'text_only':
            pre = '<span class="hl">(возможное эхо)</span> ' if f == 'echo' else ''
            body.append(pre + E(c(x)))
        else:
            body.append(f'[{t}] {E(c(name))}{tag_h}: {E(c(x))}')
    sep = '\n\n' if (md or kind == 'text_only') else '\n'
    h = '\n'.join(E(s) for s in head)
    return f'<pre class="md">{h}\n{sep.join(body)}\n…</pre>'.replace('`', '&#96;')


def stats_file(fmt='md'):
    md = fmt == 'md'
    c = (lambda s: f'`{s}`') if md else (lambda s: s)
    li, sub = ('- ', '  - ') if md else ('', '  ')
    lines = [('# ' + c('Планёрка по бюджету')) if md else 'Планёрка по бюджету', '',
             f'{li}Дата: 09.10.2026', f'{li}Начало: 10:00', f'{li}Длительность: 1:12:40',
             f'{li}Участники: ' + ', '.join(c(n) for n in ('Я', 'Ирина Соколова', 'Олег Ли', 'Спикер 3')),
             f'{li}Отметки:', f'{sub}Пауза 0:31:10 · 2 мин 52 с', f'{sub}Возможное эхо: 61 реплика', '',
             'Длительность 1:12:40 · реплик 214 · сумма долей 100 %', '']
    data = [('Ирина Соколова', 24, 33, 61), ('Олег Ли', 21, 29, 58), ('Я', 18, 25, 52), ('Спикер 3', 9, 13, 43)]
    if md:
        lines += ['| Участник | Минуты | Доля | Реплики |', '| --- | ---: | ---: | ---: |']
        lines += [f'| {c(n)} | {m} | {p} % | {r} |' for n, m, p, r in data]
    else:
        noun = lambda r: 'реплика' if r % 10 == 1 and r % 100 != 11 else ('реплики' if 2 <= r % 10 <= 4 and not 12 <= r % 100 <= 14 else 'реплик')
        lines += [f'{n} — {m} мин · {p} % · {r} {noun(r)}' for n, m, p, r in data]
    out = '\n'.join(E(s) for s in lines)
    return f'<pre class="md">{out}\n\n<span class="hl">Не учтено в статистике: возможное эхо — 16 мин · 61 реплика</span></pre>'.replace('`', '&#96;')


def d4_echo(checked=False):
    chk = (f'<span class="chk on" style="margin-top:2px">{ico("check")}</span>' if checked else '<span class="chk" style="margin-top:2px"></span>')
    opt = (f'<div style="display:flex;gap:9px;align-items:flex-start">{chk}<div><div style="font-size:13px">Не включать реплики с пометкой «возможное эхо» '
           f'<span class="mono mut">(4)</span></div><div class="mut" style="font-size:12px;line-height:1.45">Похожи на голоса собеседников из колонок, но среди них может быть и ваша речь.</div></div></div>')
    rows = [
        ('0:00:12', 's1', 'Добрый день. Тогда начнём с бюджета на четвёртый квартал.', ''),
        ('0:00:13', 'me', 'Тогда начнём с бюджета на четвёртый квартал.', 'echo'),
        ('0:00:21', 's2', 'По подрядчикам у нас перерасход около двенадцати процентов, в основном…', ''),
        ('0:00:22', 'me', 'по подрядчикам у нас перерасход около двенадцати процентов в основном…', 'echo'),
        ('0:00:35', 'me', 'Олег, это уже с учётом договора на поддержку?', ''),
    ]
    if checked:
        rows = [r for r in rows if r[3] != 'echo'] + [('0:00:39', 's2', 'Да, договор включён. Без него было бы около семи.', '')]
    pv = ''
    for t, w, x, f in rows:
        num = f'<span class="sn">{NUM[w]}</span>' if w in NUM else ''
        fl = '<span class="mut" style="font-weight:400"> (возможное эхо)</span>' if f == 'echo' else ''
        pv += (f'<div style="display:grid;grid-template-columns:62px 1fr;gap:8px;font-size:12.5px;line-height:1.45;padding:4px 0;border-top:1px solid var(--border-soft)">'
               f'<span class="mono mut" style="font-size:11.5px">[{t}]</span><span><b style="font-weight:500;{"color:var(--accent-ink)" if w == "me" else ""}">{num}{E(NAMES[w])}</b>{fl}: '
               f'<span style="{"color:var(--fg3)" if f == "echo" else ""}">{E(x)}</span></span></div>')
    body = (f'<div class="card" style="padding:4px 12px 6px">{pv}<div class="mut" style="font-size:12px;padding-top:4px">…</div></div>{opt}')
    return tpl_window(0, preview=loc_preview('Стенограмма', body), ai=False)


def p08():
    b = [DK_CSS]
    b.append(note('<b>Решение владельца D7 (10.11.2026):</b> реплики «Я», похожие на эхо из колонок, <b>не удаляются, а помечаются</b>; удаление — только явным действием человека. '
                  'Пометка «несколько говорящих» — Д5 (реплику не удалось разрезать по границе говорящих). Обе пометки уточняют колонку <b>«кто»</b>, а не текст: '
                  'текст реплики всегда виден целиком. Тексты и размеры — <code>spec.md</code>, «Дополнение 10.11.2026».', 'info', 'info').replace('class="note info"', 'class="note info" style="align-self:stretch"'))
    b.append('<div class="sec">1 · Принятый вид — пометка под именем + текст реплики-эха приглушён (1024×620, фрагмент таблицы)</div>')
    b.append(card('Б'))
    b.append('<div class="cap">Вторая строка колонки «кто»: глиф 12 + «возможное эхо» / «несколько говорящих» 12 <code>fgMuted</code>. Текст реплики-эха — <code>transcriptEchoFg</code> (= <code>fgMuted</code>, 5,6 : 1 на <code>bgSurface</code> в обеих темах — AA, читается полностью). '
             'Строка 0:00:47 — настоящая речь владельца, которую фильтр принял за эхо: она видна и читается, просто тише. «Я» остаётся акцентным. «Несколько говорящих» не приглушается — это настоящая речь собеседников.</div>')
    b.append('<div class="legend">Вариант А (пометка без приглушения текста) рассматривался и отклонён владельцем 10.11.2026.</div>')
    b.append('<div class="sec">2 · Тёмная тема (та же карточка)</div>')
    b.append(f'<div class="dk" style="align-self:stretch;display:flex;justify-content:center;padding:16px 0;border-radius:10px;background:var(--canvas)">{card("Б")}</div>')
    b.append('<div class="sec">3 · 900×588: полоса «Участники» над таблицей, колонки те же (76 · 124 · текст шире)</div>')
    b.append(card('Б', width=900, layout='strip'))
    b.append('<div class="cap">Колонка «кто» — 124 в обеих ширинах. «возможное эхо» помещается в одну строку (83 px + глиф 12 + зазор 4 = 99 ≤ 124, запас на PT Astra Sans ≥ 8 %). '
             '«несколько говорящих» (119 px текста) переносится по словам на две строки — пометка <b>никогда не сокращается многоточием</b>; имя над ней сокращается как раньше.</div>')
    b.append('<div class="sec">4 · Те же реплики в сохранённом файле — «Стенограмма»</div>')
    b.append(f'<div class="grid2" style="align-items:start"><div class="tile"><div class="tl">Стенограмма · .md</div>{md_file("stenogram", "md")}</div>'
             f'<div class="tile"><div class="tl">Стенограмма · .txt</div>{md_file("stenogram", "txt")}</div></div>')
    b.append('<div class="cap">Пометка — в скобках <b>после подписи, до двоеточия</b>, вне кодовых вставок md (это постоянный текст программы, не данные человека). '
             'Реплики-эха не склеиваются с обычными репликами «Я»; два эха подряд (0:00:22 и 0:00:27, зазор ≤ 2 с) склеиваются между собой в одну строку с пометкой. '
             'Реплика «несколько говорящих» ни с чем не склеивается. Подсветка жёлтым — только на стенде.</div>')
    b.append('<div class="sec">5 · Остальные шаблоны</div>')
    b.append(f'<div class="grid3" style="align-items:start"><div class="tile"><div class="tl">Я и собеседники · .md</div>{md_file("me_and_others", "md")}'
             f'<div class="tn">«несколько говорящих» здесь не пишется: пометка уточняет, <i>какой</i> из собеседников говорил, а эта подпись их не различает.</div></div>'
             f'<div class="tile"><div class="tl">Только текст · .txt</div>{md_file("text_only", "txt")}'
             f'<div class="tn">Имён нет — эхо помечается в начале абзаца «(возможное эхо) »; «несколько говорящих» не пишется (нет подписей, которые она уточняет).</div></div>'
             f'<div class="tile"><div class="tl">Статистика · .md</div>{stats_file("md")}'
             f'<div class="tn">Реплики-эха <b>не входят</b> в минуты, доли и число реплик ни «Я», ни собеседников; одна строка под таблицей показывает, сколько не учтено. '
             f'«Несколько говорящих» — целиком тому, кому приписана.</div></div></div>')
    b.append('<div class="sec">6 · «Сохранить как…» (D4) — галочка «Не включать реплики с пометкой „возможное эхо“» (решение владельца 10.11, v1.1)</div>')
    b.append(f'<div class="grid2" style="align-items:start;grid-template-columns:1fr"><div class="tile" style="align-items:center;background:var(--bg-app)">{d4_echo(False)}</div></div>')
    b.append(f'<div class="tile" style="align-self:stretch;align-items:center;background:var(--bg-app)"><div class="tl" style="align-self:stretch">галочка отмечена — предпросмотр без эха</div>{d4_echo(True)}</div>')
    b.append(f'<div class="grid2" style="align-items:start"><div class="tile"><div class="tl">тот же файл · .md, галочка отмечена</div>{md_file("stenogram", "md", excluded=True)}</div>'
             f'<div class="tile"><div class="tl">тот же файл · .txt, галочка отмечена</div>{md_file("stenogram", "txt", excluded=True)}</div></div>')
    b.append('<div class="cap">Галочка — только если у встречи есть хотя бы одна реплика-эхо, и только у «Стенограммы», «Я и собеседники», «Только текст» (в «Статистике» эхо и так не учтено). '
             'По умолчанию <b>снята</b>, между открытиями D4 не запоминается — исключение из файла каждый раз явное. Реплики остаются во встрече; в шапке файла — «Возможное эхо не включено в файл: N реплик». '
             'Скрытия в карточке и удаления реплик в v1.1 нет — spec.md, Дополнение §Д.7.</div>')
    b.append('<div class="sec">7 · Экранный диктор</div>')
    sr = [('Реплика-эхо', '«0:00:13, Я, возможное эхо: Тогда начнём с бюджета на четвёртый квартал.»'),
          ('Несколько говорящих', '«0:01:02, Спикер 3, несколько говорящих: Предлагаю перенести…»'),
          ('Бейдж в шапке', '«Возможное эхо: 4 реплики»'),
          ('Галочка D4', 'имя «Не включать реплики с пометкой „возможное эхо“, 4», описание — строка под ней')]
    t = ''.join(f'<tr><td style="white-space:nowrap">{a}</td><td>{E(x)}</td></tr>' for a, x in sr)
    b.append(f'<div class="card" style="align-self:stretch;padding:4px 6px"><table class="mtbl"><tr><th>Где</th><th>Что читает диктор</th></tr>{t}</table></div>')
    b.append('<div class="legend">Окна карточки нарисованы по высоте содержимого, чтобы фрагмент таблицы был виден целиком; в продукте — 1024×620 / 900×588 и прокрутка (spec §5.2). '
             'Бейдж «Возможное эхо» в шапке — отметка встречи, как «Пауза»; пока идёт расшифровка — без числа.</div>')
    return page('Встречи — пометки у реплик', '08 · Пометки у реплик: «возможное эхо» и «несколько говорящих»',
                'Дополнение 10.11.2026 к G3 (D7, Д5), принято владельцем 10.11.2026. Тексты и размеры — <code>spec.md</code>, «Дополнение 10.11.2026».', ''.join(b), NAV)
