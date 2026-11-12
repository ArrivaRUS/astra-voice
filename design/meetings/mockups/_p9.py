"""09 · Установка моделей разделения по голосам (дополнение 11.11.2026 к spec.md).
Рекомендованный вариант А: установка на месте — строка под переключателем в «Встречах» и строка в карточке встречи.
Сборка отдельно: python3 -I _p9.py (пишет 09-diar-install.html рядом). Принято владельцем 11.11.2026 (вариант А;
«Удалить» для пары — не в 1.1). Собирается и общим _build.py."""
import os
import re
import sys
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _base import *
from _p1 import SUB
from _p3 import TITLE, LINES, table

_DARK = re.search(r'body\.dark\{(.*?)\}', CSS, re.S).group(1)
DK_CSS = ('<style>.dk{' + _DARK + 'color:var(--canvas-fg)}'
          '.dk .wbtn.c{background:#8C3B36}'
          '.dprog{height:4px;background:var(--bg-sunk);border-radius:3px;overflow:hidden}'
          '.dprog i{display:block;height:100%;background:var(--primary)}'
          '.dst{display:flex;align-items:flex-start;gap:5px}'
          '.dst .ico{width:12px;height:12px;margin-top:3px}'
          '.crow{display:flex;align-items:center;gap:8px;margin:0 0 10px;font-size:13px;color:var(--fg3);line-height:1.5}'
          '.crow .tx{flex:1;min-width:0}'
          '.crow .ico{width:12px;height:12px}'
          '.mtbl{width:100%;border-collapse:collapse;font-size:12.5px;color:var(--fg1)}'
          '.mtbl td,.mtbl th{border-top:1px solid var(--border-soft);padding:5px 8px;text-align:left;vertical-align:top}'
          '.mtbl th{font-weight:500;color:var(--fg3);font-size:11.5px}'
          '.st{align-self:stretch;display:flex;flex-direction:column;gap:6px}'
          '.st .tl{font-size:11px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:var(--canvas-fg)}'
          '.st .tl code{text-transform:none;letter-spacing:0;margin-left:6px}'
          '</style>')

SIZE = '34 МБ'  # 5 992 913 + 28 281 164 = 34 274 077 байт, format_size → «34 МБ»
LABEL = 'Модели для разделения по голосам'
TOGGLE_SUB = 'Собеседники станут «Спикер 1, 2…». Голоса не запоминаются — ни между встречами, ни внутри встречи'

ERR = {
    'offline': 'Не удалось загрузить модели — сеть отключена в настройках',
    'admin': 'Не удалось загрузить модели — скачивание запрещено администратором',
    'no-space': 'Не удалось загрузить модели — не хватает места на диске',
    'broken': 'Не удалось установить модели разделения. Попробуйте ещё раз.',
    'busy': 'Уже выполняется установка или удаление моделей разделения. Попробуйте позже.',
    'revoked': 'Эта версия отозвана авторами каталога',
    'damaged': 'Файлы моделей не читаются',
}

# state → (текст строки раздела, вид текста, полоса %, кнопка, вид кнопки)
#   вид текста: '' — fgMuted; 'err' — dangerInk + глиф alert 12; 'ok' — fgMuted + глиф check 12 successInk
S = {
    'missing':     (f'Не установлены — без них все собеседники подписываются «Собеседники» · {SIZE}', '', None, 'Установить', ''),
    'downloading': ('Скачиваю с huggingface.co · 42 %', '', 42, 'Отмена', ''),
    'verifying':   ('Проверяю модели…', '', 100, 'Отмена', ''),
    'installing':  ('Устанавливаю модели…', '', 100, 'Отмена', ''),
    'installed':   (f'Установлены · {SIZE}', '', None, None, ''),
    'cancelled':   ('Установка отменена', '', None, 'Установить', ''),
    'err-offline': (ERR['offline'], 'err', None, 'Повторить', ''),
    'err-admin':   (ERR['admin'], 'err', None, 'Повторить', ''),
    'err-space':   (ERR['no-space'], 'err', None, 'Повторить', ''),
    'err-broken':  (ERR['broken'], 'err', None, 'Повторить', ''),
    'err-busy':    (ERR['busy'], 'err', None, 'Повторить', ''),
    'revoked':     (ERR['revoked'], 'err', None, None, ''),
    'damaged':     (ERR['damaged'], 'err', None, 'Переустановить', ''),
    'blk-offline': ('Не установлены · Включён офлайн-режим — «Сеть и обновления»', '', None, None, ''),
    'blk-admin':   ('Не установлены · Задано администратором: работа без сети', '', None, None, ''),
}


def status(text, kind):
    if kind == 'err':
        return f'<div class="dst err-t">{ico("alert")}<span>{E(text)}</span></div>'
    if kind == 'ok':
        return f'<div class="dst"><span class="ok-t">{ico("check")}</span><span>{E(text)}</span></div>'
    return E(text)


def bar(pct, w=None):
    st = f'width:{w}px;flex:none;' if w else 'margin-top:6px;'
    return f'<div class="dprog" style="{st}"><i style="width:{pct}%"></i></div>'


def install_row(state, focus=False):
    text, kind, pct, b, _ = S[state]
    p = bar(pct) if pct is not None else ''
    bt = btn(b, 'sm focus' if focus else 'sm') if b else ''
    return (f'<div class="r"><div class="left"><div class="lbl">{LABEL}</div>'
            f'<div class="sub">{status(text, kind)}</div>{p}</div>{bt}</div>')


def group(state, policy=False, toggle_on=True, model_row=True, focus=False):
    """Группа «РАСШИФРОВКА» раздела «Встречи». state=None — строки установки нет."""
    rows = ''
    if model_row:
        rows += row('Модель для расшифровки', 'GigaAM v3 RNN-T · установлена', '')
        rows += row('Расшифровывать сразу после записи', '', tgl(True))
    rows += row('Разделять собеседников по голосам', TOGGLE_SUB, tgl(toggle_on and not policy), hint=True, dis=policy)
    if state:
        rows += install_row(state, focus)
    if model_row:
        rows += row('Как подписывать вас в расшифровке', '', '<span class="field" style="width:200px">Я</span>')
    return f'<div class="grp">Расшифровка</div><div class="card">{rows}</div>'


def section_window(state='missing', width=1024):
    hd = shead('Встречи', SUB, btn('Записать встречу', 'pri', 'rec'))
    scrolled = ('<div class="mut" style="font-size:12px;margin:-2px 0 0">… группы «Записанные встречи», «Запись», '
                '«Предупреждение участников» выше — раздел прокручен …</div>')
    return window(hd + scrolled + group(state), width=width, height=None)


def frame(c, w=796):
    return (f'<div class="win" style="width:{w + 44}px"><div class="content" style="padding:4px 22px 14px">{c}</div></div>')


# ── карточка встречи ─────────────────────────────────────────────────────
CARD_PREFIX = 'Разделение по голосам: '
C = {
    'missing':     ('модели не установлены', '', None, 'Установить'),
    'downloading': ('скачиваю с huggingface.co · 42 %', '', 42, 'Отмена'),
    'verifying':   ('проверяю модели…', '', 100, 'Отмена'),
    'installing':  ('устанавливаю модели…', '', 100, 'Отмена'),
    'done':        (None, 'ok', None, None),
    'cancelled':   ('установка отменена', '', None, 'Установить'),
    'err-offline': ('не удалось загрузить модели — сеть отключена в настройках', 'err', None, 'Повторить'),
    'err-admin':   ('не удалось загрузить модели — скачивание запрещено администратором', 'err', None, 'Повторить'),
    'blk-offline': ('модели не установлены', '', None, None),
}
DONE = 'Модели установлены — следующие расшифровки будут разделены по голосам'


def card_row(state, focus=False):
    text, kind, pct, b = C[state]
    if kind == 'ok':
        tx = f'<span class="tx dst"><span class="ok-t">{ico("check")}</span><span>{E(DONE)}</span></span>'
    elif kind == 'err':
        tx = f'<span class="tx dst err-t">{ico("alert")}<span>{E(CARD_PREFIX + text)}</span></span>'
    else:
        tx = f'<span class="tx">{E(CARD_PREFIX + text)}</span>'
    p = bar(pct, 160) if pct is not None else ''
    bt = btn(b, 'sm focus' if focus else 'sm') if b else ''
    return f'<div class="crow">{tx}{p}{bt}</div>'


ONE = {'me': 'Я', 's1': 'Собеседники', 's2': 'Собеседники', 's3': 'Собеседники'}


def card_head():
    return (f'<div style="font-size:13px;color:var(--primary);display:flex;align-items:center;gap:4px;margin-bottom:6px">{ico("chevl","ico","width:13px;height:13px")}Все встречи</div>'
            f'<div class="shead" style="margin-bottom:0"><div class="h" style="min-width:0"><div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap"><div class="h2">{TITLE}</div>'
            f'<span class="mut">{ico("edit","ico","width:14px;height:14px")}</span>{badge("Готово", "b-ok", "check")}</div>'
            f'<div class="ssub">09.10.2026 · 10:00–11:12 · <span class="mono">1:12:40</span> · Звонок в DION · <span class="t">звук удалён</span></div></div>'
            f'<div style="display:flex;gap:8px">{btn("Сохранить как…", "pri", "save")}{btn("", "icon", "more")}</div></div>'
            '<div style="height:10px"></div>')


def card_window(state='missing', width=1024, focus=False):
    lines = [l for l in LINES if l[1] != 'mark'][:7]
    return window(card_head() + card_row(state, focus) + table(lines, ONE), width=width, height=None)


def card_frame(state, w=796, focus=False):
    return frame('<div style="height:10px"></div>' + card_row(state, focus) +
                 table([l for l in LINES if l[1] != 'mark'][:2], ONE), w)


# ── страница ─────────────────────────────────────────────────────────────
def tiles(items, maker, w=796):
    out = ''
    for st, title, code, cap in items:
        c = f'<div class="cap">{cap}</div>' if cap else ''
        out += f'<div class="st"><div class="tl">{title}<code>{code}</code></div>{maker(st)}{c}</div>'
    return out


def p09():
    b = [DK_CSS]
    b.append(note('<b>Рекомендация UX — вариант А «на месте»:</b> пара моделей ставится там, где человек увидел, что её нет — '
                  'в строке под переключателем «Разделять собеседников по голосам» и в карточке встречи. Прогресс, отмена и итог — в той же строке. '
                  'Вариант Б (переход в «Модели») не нарисован: сравнение — <code>spec.md</code>, «Дополнение 11.11.2026», §У.1. '
                  'Тексты и размеры — там же. Новых цветов и компонентов нет: <code>SettingRow</code>, <code>AvButton</code> sm, полоса 4 (<code>progressHThin</code>).', 'info', 'info')
             .replace('class="note info"', 'class="note info" style="align-self:stretch"'))

    b.append('<div class="sec">1 · Раздел «Встречи» — группа «Расшифровка», модели не установлены (1024 × по содержимому)</div>')
    b.append(section_window('missing'))
    b.append('<div class="cap">Новая строка — <b>сразу под переключателем</b> «Разделять собеседников по голосам», канон <code>SettingRow</code> с подписью (55). '
             'Кнопка — <code>AvButton</code> sm, <b>не primary</b>: без моделей расшифровка работает, просто без разделения (у GigaAM — primary, без неё расшифровки нет). '
             'Строка видна, только когда переключатель включён и не выключен администратором.</div>')

    b.append('<div class="sec">2 · Раздел — все состояния строки (ширина содержимого 796; показаны переключатель и новая строка)</div>')
    sec_items = [
        ('missing', 'Не установлены', 'missing', 'Фокус Tab — после переключателя, перед полем «Как подписывать вас».'),
        ('downloading', 'Скачивание', 'downloading · progress 0..1', 'Процент — по общим байтам пары, шрифт интерфейса. Источник — по факту (<code>huggingface.co</code> / корпоративный сервер). Кнопка на том же месте становится «Отмена» — фокус остаётся на ней.'),
        ('verifying', 'Проверка', 'verifying', 'Полоса заполнена и стоит — без бегущего сегмента: проверка 34 МБ короче секунды, анимация мигнула бы. «Отмена» доступна.'),
        ('installing', 'Установка', 'installing', 'То же, «Отмена» доступна до публикации; поздняя отмена честно заканчивается «Установлены».'),
        ('installed', 'Установлены', 'installed', 'Кнопки нет. Фокус, если был на «Отмене», переходит на переключатель выше. Новые расшифровки идут с разделением.'),
        ('cancelled', 'Отменено', 'cancelled', 'Недокачанное удалено службой — «продолжить» нечего, поэтому снова «Установить». Текст держится до ухода из раздела.'),
        ('err-offline', 'Ошибка: нет сети', 'failed:offline / failed:settings', None),
        ('err-admin', 'Ошибка: запрет администратора', 'failed:admin', None),
        ('err-space', 'Ошибка: мало места', 'no-space', None),
        ('err-broken', 'Ошибка: не прошли проверку / каталог', 'broken', 'Общий текст для повреждённых файлов, недоступного каталога и ошибки хранилища.'),
        ('err-busy', 'Занято другой операцией', 'broken (busy)', 'Текст службы <code>_BUSY_MESSAGE</code> как есть.'),
        ('revoked', 'Отозвано', 'broken · reason revoked', 'Кнопки нет: повтор поставит ту же отозванную версию. Строка вернётся к «Установить», когда каталог обновится.'),
        ('damaged', 'Установлены, но не читаются', 'pair check failed', 'Пара на диске, но не прошла проверку при чтении. Если мост не различит это с «не установлены» — допустимо показать «Не установлены» + «Установить» (§У.6).'),
        ('blk-offline', 'Недоступно заранее: офлайн-режим', 'gate offline', 'Сеть закрыта до нажатия — кнопки нет (как карточка модели <code>offline-user</code>, spec §5.4 №11).'),
        ('blk-admin', 'Недоступно заранее: без сети по политике', 'gate admin', 'То же, текст политики — как в «Моделях».'),
    ]
    b.append(tiles(sec_items, lambda st: frame(group(st, model_row=False, focus=st in ('downloading', 'cancelled', 'err-offline')))))
    b.append('<div class="st"><div class="tl">Разделение выключено (человеком или <code>meeting_diarization=off</code>)<code>hidden</code></div>'
             + frame(group(None, model_row=False, toggle_on=False)) +
             frame(group(None, policy=True, model_row=False)) +
             '<div class="cap">Строки установки нет. При запрете политикой переключатель серый (v1.1 — общая строка вверху раздела, замок — v1.2, spec §3.1).</div></div>')

    b.append('<div class="sec">3 · Карточка встречи — модели не установлены (1024; встреча расшифрована без разделения)</div>')
    b.append(card_window('missing'))
    b.append('<div class="cap">Строка уже есть в коде (<code>meetingDiarizationMissing</code>): 13 <code>fgMuted</code> + sm-кнопка, над таблицей. '
             'Дорабатывается только её поведение: прогресс, отмена, итог — те же состояния, что в разделе, с приставкой «Разделение по голосам: ».</div>')

    b.append('<div class="sec">4 · Карточка — состояния строки (ширина содержимого 796)</div>')
    card_items = [
        ('missing', 'Не установлены', 'missing', None),
        ('downloading', 'Скачивание', 'downloading', 'Текст занимает остаток ширины; полоса <b>160 × 4</b> и кнопка прижаты к правому краю и не сдвигаются при смене стадии; текст переносится, полоса и кнопка — нет.'),
        ('verifying', 'Проверка', 'verifying', None),
        ('installing', 'Установка', 'installing', None),
        ('done', 'Установлены', 'installed', 'Вместо исчезновения — итог до закрытия карточки: эта встреча остаётся без разделения, и строка объясняет почему. При следующем открытии строки нет. Фокус — на таблицу реплик.'),
        ('cancelled', 'Отменено', 'cancelled', None),
        ('err-offline', 'Ошибка (пример)', 'failed:offline', 'Все ошибки — тексты раздела с маленькой буквы после приставки.'),
        ('blk-offline', 'Недоступно заранее', 'gate offline', 'Кнопки нет; причина — в разделе «Встречи».'),
    ]
    b.append(tiles(card_items, lambda st: card_frame(st, focus=st == 'downloading')))
    b.append('<div class="st"><div class="tl">900 × 588 — ширина содержимого 672, длинная ошибка переносится<code>failed:admin</code></div>'
             + card_frame('err-admin', 672) + '</div>')

    b.append('<div class="sec">5 · Тёмная тема</div>')
    b.append(f'<div class="dk" style="align-self:stretch;display:flex;flex-direction:column;align-items:center;gap:16px;padding:16px 0;border-radius:10px;background:var(--canvas)">'
             f'{frame(group("downloading", model_row=False))}{frame(group("err-space", model_row=False))}{card_frame("downloading")}{card_frame("done")}</div>')

    b.append('<div class="sec">6 · Клавиатура и экранный диктор</div>')
    sr = [('Порядок Tab, раздел', '… → переключатель «Разделять собеседников по голосам» → кнопка строки (Установить / Отмена / Повторить / Переустановить) → поле «Как подписывать вас» → …'),
          ('Порядок Tab, карточка', '«Сохранить как…» → «⋯» → кнопка строки разделения → таблица реплик'),
          ('Кнопка «Установить»', 'имя «Установить модели для разделения по голосам, 34 МБ»; Enter / Пробел'),
          ('Кнопка «Отмена»', 'имя «Отменить установку моделей разделения»; Esc установку не отменяет'),
          ('Кнопка «Повторить»', 'имя «Повторить установку моделей разделения», описание — текст ошибки'),
          ('Полоса', 'роль ProgressBar, имя «Установка моделей разделения», значение 0–100'),
          ('Текст строки', 'StaticText; при ошибке и при успехе — AlertMessage (как UpdatePanel), читается один раз'),
          ('Фокус после итога', 'кнопка сменила текст — фокус остаётся; кнопка исчезла — раздел: переключатель выше; карточка: таблица')]
    t = ''.join(f'<tr><td style="white-space:nowrap">{a}</td><td>{E(x)}</td></tr>' for a, x in sr)
    b.append(f'<div class="card" style="align-self:stretch;padding:4px 6px"><table class="mtbl"><tr><th>Где</th><th>Что</th></tr>{t}</table></div>')
    b.append('<div class="legend">Окна нарисованы по высоте содержимого; в продукте — 1024×620 / 900×588 и прокрутка. Подсветка фокуса (кольцо) показана на одной кнопке в плитках «Скачивание», «Отменено», «Ошибка: нет сети».</div>')
    return page('Встречи — модели разделения', '09 · Установка моделей разделения по голосам',
                'Дополнение 11.11.2026 к G3. Тексты и правила — <code>spec.md</code>, «Дополнение 11.11.2026».', ''.join(b), NAV)


if __name__ == '__main__':
    here = os.path.dirname(os.path.abspath(__file__))
    with open(os.path.join(here, '09-diar-install.html'), 'w', encoding='utf-8') as f:
        f.write(p09())
    print('09-diar-install.html')
