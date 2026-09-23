# -*- coding: utf-8 -*-
"""Онбординг (08), пилюля и трей (09), диалоги и системные окна (10)."""
from _shell import (page, write, titlebar, sb, ic, tgl, key, btn, row, card, group, met,
                    level_bars, stcell, sech, grid, th, pill, tray, mark, logo, appicon,
                    dlbar, DL_STATES, PILL_MIN, PILL_MAX, W, H, W_DEF, COL_DEF)
from _p1 import sel, seg, mcard, hotkey_field, mic_row_states, pick_bar

DD = 'var(--fg3)'

EXTRA_CSS = """
.desk.lt{background:linear-gradient(160deg,#DCE6F5 0%,#C9D8EE 55%,#AEC2E0 100%)}
.desk.lt .panel{background:#E7ECF4;border-top:1px solid #D7DEEA}
.desk.lt .panel .pb{background:rgba(14,23,41,.07);color:#243350}
.desk.lt .dwin{box-shadow:0 8px 26px rgba(20,35,70,.28)}
.rul{display:flex;align-items:center;gap:6px;font-size:11px;color:var(--fg3);margin-top:5px}
.rul i{height:1px;background:var(--fg4);display:block;flex:none}
.tip{background:#0E1729;color:#F2F5FA;border-radius:6px;padding:5px 9px;font-size:12px;
  box-shadow:0 6px 18px rgba(0,0,0,.35);display:inline-block}
"""


# ══════════════════════════════════════════════════════════════════════════
# 08 · ОНБОРДИНГ
# ══════════════════════════════════════════════════════════════════════════
def onb(step, body, actions, title="Astra Voice — первый запуск", progress=None, queue="", w=None):
    """progress — состояние сквозной полоски загрузки (None = загрузки нет, полоски нет).

    Полоска — часть ОБРАМЛЕНИЯ мастера, а не содержимого шага: она стоит над нижней панелью
    с кнопками и видна на шагах 2–5, пока идёт загрузка (решение заказчика 2026-09-16).
    """
    dots = "".join(f'<span class="sd {"on" if i == step else ("dn" if i < step else "")}"></span>'
                   for i in range(1, 6))
    hd = ('<div style="display:flex;align-items:center;gap:12px;padding:14px 22px 0">'
          f'<span class="c12">Шаг {step} из 5</span><span class="steps">{dots}</span>'
          '<span style="flex:1"></span>'
          f'<span class="c12">{["", "Сеть", "Модель", "Горячая клавиша", "Микрофон", "Готово"][step]}</span>'
          '</div>')
    bar = dlbar(progress, queue=queue) if progress else ""
    return (f'<div class="win" style="width:{w or W}px;height:{H}px">{titlebar(title)}{hd}'
            f'<div class="obody">{body}</div>{bar}<div class="obar">{actions}</div></div>')


def acts(back=True, skip=True, skip_dis=False, next_="Продолжить", note="", next_dis=False):
    b = btn("Назад") if back else ""
    s = ""
    if skip:
        s = btn("Пропустить", "gh dis" if skip_dis else "gh")
    n = f'<span class="c12">{note}</span>' if note else ""
    return f'{b}{n}<span style="flex:1"></span>{s}{btn(next_, "dis" if next_dis else "pri")}'


def onb1(theme):
    body = ('<div style="width:580px">'
            f'<div style="display:flex;gap:14px;align-items:center;margin-bottom:18px">{appicon(52)}'
            '<div><div class="h1" style="font-size:24px">Astra Voice</div>'
            '<div class="sm" style="margin-top:3px">Голосовой ввод для Astra Linux. Распознавание идёт '
            'на этом компьютере — записи никуда не отправляются.</div></div></div>'
            + card([
                row("Язык интерфейса", sel("Русский", 200),
                    "Определён по системной локали ru_RU", hint=False),
                row("Проверять обновления утилиты", tgl(False), "Раз в сутки", hint=False),
                row("Проверять обновления моделей", tgl(False), "Раз в сутки, huggingface.co", hint=False),
            ])
            + f'<div class="note i" style="margin-top:14px">{ic("info", 15, DD)}'
              '<div><b>Пока оба переключателя выключены, программа не выходит в сеть</b>'
              'Скачать модель на следующем шаге можно и без них — это ваше явное действие. '
              'Куда программа ходит в сеть и как это выключить — в «О программе → Приватность».</div></div>'
            '</div>')
    b = onb(1, body, acts(back=False, skip=True,
                          note="Изменить можно позже в «Сеть и обновления»"))
    leg = ("<b>Шаг 1 из 5.</b> Оба сетевых тумблера <b>пусты</b> — требование PRD F14.2 и решение "
           "заказчика: до явного включения ни одного сетевого запроса. «Пропустить» доступен: "
           "пропущенное живёт в настройках. Это единственный шаг, где объясняется приватность — "
           "дальше об этом не напоминаем.")
    return page(f"A · Онбординг 1/5 — {th(theme)}",
                f'<b>Онбординг 1/5</b> — приветствие и сеть · {th(theme)} тема · 900×620',
                b, theme, leg, EXTRA_CSS)


ONB2_W = COL_DEF   # 796 при окне 1024 — та же ширина карточки, что в разделе «Модели» (решение 2026-09-23)


def onb2_body(cards, total, free="свободно на диске 42,1 ГБ", action=None, warn=False,
              head_sub=None, catalog=False, width=None):
    """Тело шага 2: карточки выбора + строка итога + одна общая кнопка «из файла»."""
    sub = head_sub or ('Отметьте, что скачать. Рекомендуем русскую GigaAM: она расставляет знаки '
                       'препинания сама. Загрузка начнётся, когда нажмёте «Продолжить».')
    act = btn("Установить из файла или папки…", "", "folder") if action is None else action
    more = (f'{btn("Показать все 12 моделей", "gh")}' if catalog else "")
    return (f'<div style="width:{width or ONB2_W}px">'
            '<div class="h2">Выберите модель распознавания</div>'
            f'<div class="sm" style="margin:4px 0 14px">{sub}</div>'
            + cards
            + pick_bar(total, free, "", warn)
            + '<div style="display:flex;gap:8px;align-items:center;margin-top:10px">'
            + act + '<span style="flex:1"></span>' + more + '</div></div>')


def onb2_cards(rec_state="avail", extra=""):
    return mcard("rnnt", rec_state, [("Рекомендуем", "rec")]) + extra


def onb2(theme):
    # основной кадр M5: в каталоге одна модель, ничего не выбрано → «Продолжить» неактивна
    body = onb2_body(onb2_cards("avail"), "Пока ничего не выбрано")
    b = onb(2, body, acts(next_="Продолжить", skip=True, skip_dis=True, next_dis=True,
                          note="Выберите хотя бы одну модель — без неё диктовка не работает"), w=W_DEF)
    one = onb(2, onb2_body(onb2_cards("selected"), "Будет скачано 226 МБ"),
              acts(next_="Продолжить", skip=True, skip_dis=True), w=W_DEF)
    two = onb(2, onb2_body(onb2_cards("selected", mcard("vosk-s", "selected")),
                           "Будет скачано 252,7 МБ", catalog=True),
              acts(next_="Продолжить", skip=True, skip_dis=True), w=W_DEF)
    nospace = onb(2, onb2_body(
        mcard("rnnt", "nospace", [("Рекомендуем", "rec")],
              note=("e", "Нужно ещё 126 МБ, свободно 100 МБ"))
        + mcard("vosk-s", "avail"),
        "Не хватает места: нужно ещё 126 МБ", "свободно на диске 100 МБ", warn=True),
        acts(next_="Продолжить", skip=True, skip_dis=True, next_dis=True,
             note="Освободите место или выберите модель полегче"), w=W_DEF)
    nonet = onb2_body(onb2_cards("nonet"), "Пока ничего не выбрано",
                      head_sub='Сети нет — модель можно поставить из файла, например с флешки.')
    failed = onb2_body(
        mcard("rnnt", "failed", [("Рекомендуем", "rec")],
              note=("e", "Не удалось загрузить модель — соединение оборвалось")),
        "Будет скачано 226 МБ")
    leg = ("<b>Шаг 2 из 5 — карточка теперь только ВЫБОР</b> (решение заказчика 2026-09-17): кнопок "
           "«Скачать» и «Из файла…» в карточке нет, отметка слева, выбор множественный, карточка "
           "кликабельна целиком. Под карточками — <b>итог «Будет скачано N МБ»</b> и свободное место; "
           "внизу <b>одна общая</b> «Установить из файла или папки…», она работает и без сети. "
           "«Продолжить» <b>неактивна, пока ничего не выбрано</b>, подсказка стоит рядом, а не "
           "всплывает после нажатия; нажатие запускает загрузку и сразу ведёт на шаг 3 — дальше её "
           "ход показывает сквозная полоска внизу мастера. Отдельного заслона «нет модели» в конце "
           "мастера больше не нужно: пройти шаг 2, не выбрав модель, нельзя. В M5 в каталоге одна "
           "модель — кадр с двумя карточками показывает, как это работает в M6.")
    return page(f"A · Онбординг 2/5 — {th(theme)}",
                f'<b>Онбординг 2/5</b> — выбор модели: ничего не выбрано, выбрана, нет места · {th(theme)} тема · 1024×620',
                b + sech("Шаг 2: выбрана одна модель — «Продолжить» доступна") + one
                + sech("Шаг 2 (M6): выбраны две модели — итог суммируется") + two
                + sech("Шаг 2: не хватает места на диске") + nospace
                + sech("Состояния содержимого шага") + grid([
                    stcell("нет сети — отметка недоступна, остаётся путь через файл", nonet,
                           "no-network"),
                ], 1) + grid([
                    stcell("загрузка не удалась — «Повторить» в карточке", failed, "failed"),
                ], 1),
                theme, leg, EXTRA_CSS + ".sec,.sech,.grid,.grid1,.grid3,.legend,.cap{max-width:1024px}")


def onb3(theme):
    body = ('<div style="width:580px">'
            '<div class="h2">Горячая клавиша</div>'
            '<div class="sm" style="margin:4px 0 14px">Зажмите её и говорите. Отпустили — текст появится '
            'там, где стоит курсор.</div>'
            + card([
                row("Текущая комбинация", hotkey_field("idle"), hint=False),
                row("Режим", seg("Удерживать", "Нажать-нажать", True),
                    "Удерживать — самый предсказуемый вариант", hint=False),
            ])
            + '<div class="grp" style="margin:16px 0 7px">Назначение новой комбинации</div>'
            + hotkey_field("capturing") + hotkey_field("conflict")
            + '</div>')
    # загрузка, запущенная на шаге 2, едет по остальным экранам мастера
    b = onb(3, body, acts(), progress="downloading")
    leg = ("<b>Шаг 3 из 5.</b> Занятая комбинация показывается <b>с именем чужого действия</b> "
           "(«Показать KRunner») и не запрещает выбор — решает человек (PRD S1-A4). Полный набор "
           "состояний поля захвата — на экране «Общие: захват комбинации». Внизу — <b>сквозная "
           "полоска загрузки</b>: модель, выбранная на шаге 2, качается фоном, мастер не ждёт её.")
    return page(f"A · Онбординг 3/5 — {th(theme)}",
                f'<b>Онбординг 3/5</b> — горячая клавиша и захват · {th(theme)} тема',
                b, theme, leg, EXTRA_CSS)


# ── шаг 4: проверка микрофона и тестовая диктовка — ДВЕ РАЗНЫЕ ЧАСТИ ──────
# Решение заказчика 2026-09-16: микрофон проверяется всегда, модель для этого не нужна.
def onb4_mic(kind="live", label="Слышим вас", sub="Пик −18 дБ · уровень в норме"):
    return ('<div class="grp" style="margin:0 0 7px">Проверка микрофона</div>'
            + card([row("Микрофон", sel("Системный по умолчанию", 236),
                        "Встроенный микрофон", hint=False)])
            + f'<div class="lvbox" style="margin-top:9px">{level_bars(kind)}'
              f'<div style="flex:1"><div class="lbl">{label}</div>'
              f'<div class="sub">{sub}</div></div></div>')


def onb4_test(ready=True):
    if ready:
        return ('<div class="grp" style="margin:16px 0 7px">Тестовая диктовка</div>'
                + f'<div class="lvbox">{btn("Тестовая диктовка", "pri", "chip")}'
                  '<div style="flex:1"><div class="sub">Скажите фразу — покажем, '
                  'что распознали.</div></div></div>'
                + '<div class="field" style="margin-top:9px;color:var(--fg1)">'
                  'Проверка связи, раз, два, три.</div>'
                + f'<div class="c12" style="margin-top:7px;color:var(--ok-ink)">'
                  f'{ic("check", 12, "var(--ok-ink)")} Распознано за 0,31 с · '
                  'модель GigaAM v3 RNN-T</div>')
    return ('<div class="grp" style="margin:16px 0 7px">Тестовая диктовка</div>'
            + f'<div class="lvbox">{btn("Тестовая диктовка", "dis", "chip")}'
              '<div style="flex:1"><div class="sub">Будет доступно после установки модели</div>'
              '</div></div>')


def onb4(theme):
    body = ('<div style="width:580px">'
            '<div class="h2">Проверим микрофон</div>'
            '<div class="sm" style="margin:4px 0 14px">Сначала убедимся, что вас слышно. '
            'Распознавание можно попробовать здесь же, когда модель будет готова.</div>'
            + onb4_mic() + onb4_test(True) + '</div>')
    b = onb(4, body, acts())
    body_wait = ('<div style="width:580px">'
                 '<div class="h2">Проверим микрофон</div>'
                 '<div class="sm" style="margin:4px 0 14px">Сначала убедимся, что вас слышно. '
                 'Распознавание можно попробовать здесь же, когда модель будет готова.</div>'
                 + onb4_mic() + onb4_test(False) + '</div>')
    b_wait = onb(4, body_wait, acts(), progress="verifying")
    b_silent = onb(4, ('<div style="width:580px">'
                       '<div class="h2">Проверим микрофон</div>'
                       '<div class="sm" style="margin:4px 0 14px">Сначала убедимся, что вас слышно.</div>'
                       + onb4_mic("flat", "Пока тишина", "Пик ниже −60 дБ уже 2 секунды")
                       + f'<div class="note w" style="margin-top:11px">{ic("alert", 15, "var(--warn-ink)")}'
                         '<div><b>Микрофон молчит</b>'
                         'Устройство открыто, но звука нет. Обычно помогает перезапуск звуковой '
                         'службы — это безопасно и не требует пароля.'
                         f'<div style="display:flex;gap:8px;margin-top:9px;align-items:center">'
                         f'{btn("Перезапустить звуковую службу", "pri", "refresh")}{btn("Что это", "gh")}'
                         '</div></div></div></div>'),
                   acts(note="Можно продолжить и разобраться позже"))
    leg = ("<b>Шаг 4 из 5 — две разные части</b> (решение заказчика 2026-09-16). "
           "<b>(а) Проверка микрофона</b> — выбор устройства и живая полоска уровня — работает "
           "<b>всегда, без модели</b>: человеку не надо ждать 226 МБ, чтобы узнать, слышно его или нет. "
           "<b>(б) Тестовая диктовка</b> — только при готовой модели; пока модель качается, кнопка "
           "неактивна и прямо говорит почему («Будет доступно после установки модели»), а внизу видно "
           "сквозную полоску. Строка результата — «Распознано за 0,31 с · модель GigaAM v3 RNN-T»; "
           "обещания про вставку с экрана убраны по решению заказчика — «текст никуда не вставлен» со "
           "строки результата (2026-09-17) и «Никуда вставлять не будем» из пояснения кнопки "
           "(2026-09-21); поведение не меняется: пробная диктовка по-прежнему ничего не вставляет. "
           "Состояние «молчит» показано "
           "плоскими столбиками, значком и подписью — не одним цветом.")
    return page(f"A · Онбординг 4/5 — {th(theme)}",
                f'<b>Онбординг 4/5</b> — проверка микрофона и тестовая диктовка · {th(theme)} тема',
                b + sech("Тот же шаг: модель ещё готовится — тестовая диктовка недоступна") + b_wait
                + sech("Тот же шаг: устройство даёт тишину") + b_silent, theme, leg, EXTRA_CSS)


def onb5_ready_body():
    return ('<div style="width:580px">'
            '<div class="h2">Всё готово</div>'
            '<div class="sm" style="margin:4px 0 14px">Осталось запомнить одно сочетание — остальное '
            'программа сделает сама.</div>'
            + f'<div class="note o" style="margin-top:16px">{ic("check", 15, "var(--ok-ink)")}'
              '<div><b>Готово: зажмите Ctrl + Space и говорите</b>'
              'Программа свернётся в системный трей — значок слева от часов. Левый клик по значку '
              'открывает настройки, правый — меню с отменой и выбором модели.</div></div>'
            + f'<div class="lvbox" style="margin-top:14px;justify-content:center;gap:14px;padding:14px">'
              f'{pill("listening")}<span class="c12" style="max-width:250px">Так выглядит запись: '
              'пилюля у нижнего края экрана.</span></div>'
            '</div>')


def onb5_wait_body():
    return ('<div style="width:580px">'
            '<div class="h2">Почти всё</div>'
            '<div class="sm" style="margin:4px 0 14px">Начать можно будет, как только загрузится '
            'модель. Окно можно закрыть — загрузка продолжится, мы сообщим, когда всё будет готово.'
            '</div>'
            + f'<div class="note i" style="margin-top:16px">{ic("info", 15, DD)}'
              '<div><b>Комбинация уже назначена: Ctrl + Space</b>'
              'Как только модель будет готова, зажмите её и говорите — текст появится там, где '
              'стоит курсор. Значок в трее покажет, что всё готово.</div></div>'
            '</div>')


def onb5(theme):
    b = onb(5, onb5_ready_body(), acts(skip=False, next_="Готово"))
    b_wait = onb(5, onb5_wait_body(),
                 acts(skip=False, next_="Готово", next_dis=True,
                      note="Модель ещё загружается"),
                 progress="downloading")
    notif = ('<div class="kn"><div class="knh">' + tray("idle", 14, "#8C97AC")
             + 'Astra Voice</div><b>Astra Voice готов</b>'
             'Зажмите <span style="font-family:var(--font-mono)">Ctrl + Space</span> и говорите — '
             'текст появится там, где курсор.'
             '<div class="knb"><span>Открыть настройки</span><span>Понятно</span></div></div>')
    tray_scene = ('<div class="desk" style="width:420px;height:150px">'
                  '<div class="panel"><span class="pb">Kate</span><span class="sp"></span>'
                  '<span style="display:inline-flex;align-items:center;gap:9px;padding-right:6px">'
                  + tray("idle", 17, "#F2F5FA") + tray("idle", 17, "#8C97AC")
                  + '</span><span class="pb mono">14:26</span></div>'
                  '<div style="position:absolute;right:44px;bottom:52px" class="tip">'
                  'Astra Voice — готов · Ctrl + Space</div></div>')
    leg = ("<b>Шаг 5 из 5 — два вида.</b> <b>Ожидание:</b> «Почти всё», кнопка «Готово» неактивна, "
           "внизу едет сквозная полоска; окно <b>само не закрывается</b> и его можно закрыть в любой "
           "момент — загрузка продолжится в трее, по готовности придёт уведомление (решение заказчика "
           "2026-09-16, поправки 1 и 2). <b>Готово:</b> «Всё готово», подсказка про клавишу диктовки, "
           "кнопка активна; экран сам переходит из ожидания в готовность, без действий человека. "
           "Автозапуска на шаге нет — он отложен до вехи M9.")
    return page(f"A · Онбординг 5/5 — {th(theme)}",
                f'<b>Онбординг 5/5</b> — ожидание модели и «Всё готово» · {th(theme)} тема',
                b + sech("Тот же шаг, пока модель не загрузилась") + b_wait
                + sech("Что происходит после «Готово»")
                + grid([stcell("уведомление KDE", notif, "notify"),
                        stcell("значок появился в трее", tray_scene, "tray")]),
                theme, leg, EXTRA_CSS)


# ── сквозная полоска загрузки: отдельный экран со всеми состояниями ───────
def onb_progress(theme):
    fmt = ('<div class="stc"><div class="stn">формат времени и скорости'
           '<span class="code">eta · speed</span></div>'
           '<div class="sm" style="line-height:1.8">'
           'Первые секунды, пока скорость не устоялась — <b>«считаю…»</b><br>'
           'меньше минуты — <b>«осталось меньше минуты»</b><br>'
           'меньше часа — <b>«осталось ~12 мин»</b><br>'
           'час и больше — <b>«осталось ~1 ч 10 мин»</b><br>'
           'Секунд нет ни в одном виде — они дёргаются, точность мнимая.<br>'
           'Скорость: <b>«860 КБ/с»</b> ниже мегабайта, <b>«5,2 МБ/с»</b> выше.'
           '</div></div>')
    where = ('<div class="stc"><div class="stn">где стоит полоска</div>'
             '<div class="sm" style="line-height:1.7">Полоска — часть <b>обрамления мастера</b>, '
             'а не содержимого шага: она стоит <b>над нижней панелью с кнопками</b>, во всю ширину '
             'окна, и видна на шагах <b>2–5</b>, пока есть загрузка. Так она повторяет строку-статус '
             'из направления A (§2): у окна внизу одна полоса, которая говорит, что сейчас '
             'происходит. В панель с кнопками её не кладём — там человек принимает решение, и живой '
             'текст рядом с «Продолжить» заставлял бы кнопки прыгать.'
             '<div style="margin-top:9px">Высота <b>36</b>, поля <b>0 22</b>, граница сверху 1 px, '
             'фон окна. Тело шага при появлении полоски становится ниже на 36 — окно 900 × 620 '
             'не меняется.</div></div></div>')
    cells = [stcell(name, dlbar(st, queue="· 1 из 2" if st in ("downloading", "calc") else ""), code)
             for st, name, code in DL_STATES]
    leg = ("<b>Пять состояний полоски</b> (решение заказчика 2026-09-16, поправка 3): идёт загрузка · "
           "проверяю модель · готово · не получилось · не хватает места. Шестая ячейка — та же "
           "«идёт загрузка» в первые секунды, когда оценка времени ещё не устоялась: вместо «осталось "
           "~4 ч» человек видит «считаю…» (поправка 4). «Проверяю модель…» — <b>неопределённая</b> "
           "полоска: сумма и пробное распознавание идут без процентов. Счётчик «1 из 2» появляется "
           "только когда моделей в очереди больше одной; в M5 модель одна. «Модель готова» держится "
           "3 с и исчезает вместе с полоской.")
    return page(f"A · Онбординг: полоска загрузки — {th(theme)}",
                f'<b>Онбординг → полоска загрузки</b> — пять состояний обрамления · {th(theme)} тема',
                onb(3, ('<div style="width:580px">'
                        '<div class="h2">Горячая клавиша</div>'
                        '<div class="sm" style="margin:4px 0 14px">Зажмите её и говорите. Отпустили — '
                        'текст появится там, где стоит курсор.</div>'
                        + card([row("Текущая комбинация", hotkey_field("idle"),
                                    hint=False)]) + '</div>'),
                    acts(), progress="downloading", queue="· 1 из 2")
                + sech("Пять состояний полоски") + grid(cells, 1)
                + sech("Правила") + grid([where, fmt]), theme, leg, EXTRA_CSS)


def desk(pill_html, light=False, top=False, w=880, h=380, mini=False):
    cls = "desk lt" if light else "desk"
    pos = ('top:14px' if mini else 'top:52px') if top else 'bottom:52px'
    if mini:
        wtop = 62 if top else 26
        win = 'left:26px;top:%dpx;width:%dpx;height:%dpx' % (wtop, w - 52, h - 130 - (34 if top else 0))
        text = ('<div class="dc" style="font-size:11px;line-height:1.6;padding:9px 11px">'
                'Добрый день, Сергей Петрович!<br>По итогам совещания направляю сводку: сроки '
                'сдвигаются на неделю<span style="border-left:1.5px solid #1B3A73">&nbsp;</span></div>')
        panel = ('<div class="panel" style="height:32px"><span class="pb">Kate</span>'
                 '<span class="sp"></span><span class="pb mono">14:26</span></div>')
    else:
        win = 'left:60px;top:34px;width:520px;height:250px'
        text = ('<div class="dc">Добрый день, Сергей Петрович!<br><br>'
                'По итогам совещания направляю сводку: сроки по первому этапу сдвигаются на неделю, '
                'смета остаётся прежней. Прошу подтвердить'
                '<span style="border-left:1.5px solid #1B3A73;margin-left:1px">&nbsp;</span></div>')
        panel = ('<div class="panel"><span class="pb">Kate</span><span class="pb">Firefox</span>'
                 '<span class="sp"></span>'
                 '<span style="display:inline-flex;align-items:center;gap:9px;padding-right:6px">'
                 + tray("listening", 17, "#C4CDDC") + tray("idle", 17, "#8C97AC")
                 + '</span><span class="pb mono">14:26</span></div>')
    return (f'<div class="{cls}" style="width:{w}px;height:{h}px">'
            f'<div class="dwin" style="{win}">'
            '<div class="dt">Письмо — без имени — Kate</div>'
            f'{text}</div>'
            f'<div style="position:absolute;left:50%;transform:translateX(-50%);{pos}">{pill_html}</div>'
            f'{panel}</div>')


PILL_ALL = [("loading", "загружаю модель", "≤ 5 с"), ("listening", "слушаю (живой уровень)", "пока зажат хоткей"),
            ("silent", "микрофон молчит", "через 2 с тишины"), ("limit", "достигнут лимит записи", "2 с"),
            ("processing", "распознаю", "до результата"), ("done", "готово", "500 мс"),
            ("empty", "ничего не распознано", "1 с"), ("clip", "скопировано в буфер", "1,2 с"),
            ("cancel", "отменено", "0,8 с"), ("error", "ошибка · клик — подробности", "3 с")]


def pill_screen(theme):
    cells = []
    for s, n, dur in PILL_ALL:
        cells.append('<div class="stc"><div class="stn">' + n + f'<span class="code">{dur}</span></div>'
                     '<div style="background:#16233F;border-radius:9px;padding:14px;display:flex;'
                     f'justify-content:center">{pill(s)}</div></div>')
    rubber = ('<div class="stc"><div class="stn">резиновая ширина<span class="code">решение G2</span></div>'
              '<div style="background:#16233F;border-radius:9px;padding:14px;display:flex;'
              'flex-direction:column;align-items:center;gap:10px">'
              + pill("done") + pill("listening") + pill("silent") + pill("empty") + '</div>'
              f'<div class="rul"><i style="width:{PILL_MIN}px"></i>минимум {PILL_MIN} px</div>'
              f'<div class="rul"><i style="width:{PILL_MAX}px"></i>максимум {PILL_MAX} px</div>'
              '<div class="c12" style="margin-top:6px"><b>172 px — минимум для всех состояний</b>, '
              'содержимое центрируется: короткие подписи («Готово», «Отменено») не сжимают окно, иначе '
              'в последовательности слушаю → распознаю → готово пилюля дёргалась бы на глазах. '
              'Дальше ширина растёт по тексту до 320 px, <b>обрезки многоточием быть не должно</b> — '
              'русские подписи длиннее английских, и фиксированные 172 px Handy им малы. '
              'Высота всегда 36 px, радиус 18 px. Длиннее 320 px не бывает: самая длинная подпись — '
              '«Достигнут лимит записи».</div></div>')
    pos = ('<div class="stc"><div class="stn">положение на экране</div>'
           + desk(pill("listening"), False, False, 400, 200, mini=True)
           + '<div class="c12" style="margin:7px 0 9px">Снизу — по умолчанию, отступ 56 px от края '
             'экрана (над панелью KDE).</div>'
           + desk(pill("listening"), False, True, 400, 200, mini=True)
           + '<div class="c12" style="margin-top:7px">Сверху — когда панель задач внизу мешает.</div>'
             '</div>')
    light_desk = ('<div class="stc"><div class="stn">светлый рабочий стол</div>'
                  + desk(pill("listening"), True, False, 400, 200, mini=True)
                  + '<div class="c12" style="margin-top:7px">Пилюля всегда тёмная — и на светлых обоях, '
                    'и на тёмных: так она читается одинаково и не спорит с темой окон.</div></div>')
    dark_desk = ('<div class="stc"><div class="stn">тёмный рабочий стол</div>'
                 + desk(pill("processing"), False, False, 400, 200, mini=True)
                 + '<div class="c12" style="margin-top:7px">Во время распознавания пилюля остаётся '
                   'на месте — не «прыгает» и не меняет положение.</div></div>')
    leg = ("<b>Пилюля-оверлей</b> (PRD F3): отдельное окно поверх всех, <b>не забирает фокус</b>, "
           "не попадает в Alt+Tab, не активируется кликом мимо «×». Состояние всегда несёт форма + "
           "подпись, а не только цвет: столбики уровня, плоские столбики с «!», пульсирующие точки, "
           "галочка, треугольник. Бирюза #12B3A0 — только «слушаю», красный — только ошибка. "
           "<b>«Готово» держится 500 мс</b> (решение 2026-09-08; PRD F3.2 приведён к 500 мс). "
           "Ширина резиновая: <b>минимум 172 px у всех состояний</b> (содержимое по центру), дальше "
           "по тексту до 320 px, без многоточия — иначе окно дёргалось бы при смене состояния.")
    return page(f"A · Пилюля — {th(theme)}",
                f'<b>Пилюля-оверлей</b> — все состояния, резиновая ширина, положение · {th(theme)} тема',
                desk(pill("listening"), theme == "light")
                + sech("Все состояния (F3.2)") + grid(cells, 2)
                + sech("Ширина и положение") + grid([rubber, pos])
                + grid([light_desk, dark_desk]),
                theme, leg, EXTRA_CSS)


def menu(state):
    """Трей-меню: покой · запись · доступно обновление · ошибка микрофона · офлайн."""
    hdr = {
        "idle": (tray("idle", 15, "var(--fg3)"), "Готов · Ctrl + Space", ""),
        "rec": (tray("listening", 15, "var(--accent)"), "Слушаю…", "color:var(--accent-ink)"),
        "update": (tray("idle", 15, "var(--fg3)"), "Готов · Ctrl + Space", ""),
        "micerr": (tray("error", 15, "var(--err-ink)"), "Микрофон недоступен", "color:var(--err-ink)"),
        "offline": (tray("idle", 15, "var(--fg3)"), "Готов · Ctrl + Space", ""),
    }[state]
    rec = state == "rec"
    items = f'<div class="mi hd" style="{hdr[2]}">{hdr[0]} {hdr[1]}</div><div class="msep"></div>'
    if state == "micerr":
        items += ('<div class="mi" style="color:var(--err-ink)">' + ic("alert", 14, "var(--err-ink)")
                  + 'Микрофон молчит — открыть</div><div class="msep"></div>')
    items += f'<div class="mi{"  hl" if rec else " dis"}">Отмена</div>'
    items += ('<div class="mi' + (' dis' if rec else '') + '">Модель<span class="sp"></span>'
              '<span class="c12">GigaAM v3 RNN-T</span>'
              + (ic("chev", 13, DD) if not rec else '') + '</div>')
    items += f'<div class="mi{" dis" if rec else ""}">Скопировать последний текст</div>'
    items += '<div class="msep"></div>'
    items += '<div class="mi">Настройки…<span class="sp"></span><span class="sc">Ctrl+,</span></div>'
    if state == "update":
        items += ('<div class="mi" style="color:var(--primary);font-weight:500">'
                  + ic("down", 14, "var(--primary)") + 'Доступна версия 0.2.1 — установить</div>')
    elif state == "offline":
        items += ('<div class="mi dis">' + ic("lock", 14, "var(--fg-dis)")
                  + 'Проверить обновления<span class="sp"></span>'
                  '<span class="sc">офлайн</span></div>')
    else:
        items += '<div class="mi">Проверить обновления</div>'
    items += '<div class="mi">О программе</div><div class="msep"></div>'
    items += '<div class="mi">Выход<span class="sp"></span><span class="sc">Ctrl+Q</span></div>'
    return f'<div class="menu">{items}</div>'


def tray_screen(theme):
    scene = ('<div class="desk" style="width:880px;height:330px">'
             f'<div style="position:absolute;right:16px;bottom:52px">{menu("idle")}</div>'
             '<div class="panel"><span class="pb">Kate</span><span class="sp"></span>'
             '<span style="display:inline-flex;align-items:center;gap:9px;padding-right:6px">'
             + tray("idle", 17, "#F2F5FA") + tray("idle", 17, "#8C97AC")
             + '</span><span class="pb mono">14:26</span></div></div>')
    icons = []
    for s, n, tip in [("idle", "готов", "Astra Voice — готов · Ctrl + Space"),
                      ("listening", "слушаю", "Слушаю…"),
                      ("processing", "распознаю", "Распознаю…"),
                      ("done", "готово (0,8 с)", "Готово"),
                      ("error", "ошибка микрофона", "Микрофон недоступен — открыть"),
                      ("error", "хоткей не захвачен", "Горячая клавиша не захвачена — выбрать другую")]:
        icons.append('<div class="stc" style="text-align:center">'
                     f'<div class="stn" style="justify-content:center">{n}</div>'
                     '<div style="background:#0B1220;border-radius:8px;padding:10px;display:inline-flex">'
                     + tray(s, 22, "#C4CDDC") + '</div>'
                     f'<div style="margin-top:9px"><span class="tip">{tip}</span></div></div>')
    leg = ("<b>Трей — основной дом программы</b> (PRD F12): левый клик открывает окно настроек, правый — "
           "меню. Значок обязателен всегда, даже когда пилюля выключена, иначе запись стала бы скрытой "
           "(§9.5). Цвет несёт <b>только третья точка знака</b>, сам значок берёт цвет панели — поэтому он "
           "читается и на светлой, и на тёмной панели. Подсказка при наведении повторяет состояние словами: "
           "человеку с нарушением цветовосприятия цвет точки ничего не скажет. "
           "<b>«Проверить обновления» активен всегда</b> (решение 2026-09-08): ручная проверка — явное "
           "действие человека, как «Проверить сейчас» в разделе «Сеть». Недоступен он только в "
           "офлайн-режиме или когда проверки запрещены политикой администратора.")
    return page(f"A · Трей — {th(theme)}",
                f'<b>Трей</b> — значок, подсказки и меню в четырёх состояниях · {th(theme)} тема',
                scene + sech("Меню — состояния") + grid([
                    stcell("в покое", menu("idle"), "idle"),
                    stcell("во время записи / распознавания", menu("rec"), "listening"),
                ]) + grid([
                    stcell("доступно обновление", menu("update"), "update-available"),
                    stcell("ошибка микрофона", menu("micerr"), "error"),
                ]) + grid([
                    stcell("офлайн-режим или запрет политикой — единственный случай, "
                           "когда «Проверить обновления» недоступен", menu("offline"), "offline"),
                ], 1) + sech("Значок и подсказки при наведении") + grid(icons, 3),
                theme, leg, EXTRA_CSS)


# ══════════════════════════════════════════════════════════════════════════
# 10 · ДИАЛОГИ И СИСТЕМНЫЕ ОКНА
# ══════════════════════════════════════════════════════════════════════════
def dialogs(theme):
    del_model = ('<div class="dlg"><div class="dh">' + ic("trash", 14, DD) + 'Удалить модель</div>'
                 f'<div class="db">{ic("trash", 22, "var(--err-ink)")}'
                 '<div><div class="h3">Удалить GigaAM v3 CTC?</div>'
                 '<div class="sm" style="margin-top:6px">С диска будет удалено <b>225 МБ</b>. '
                 'Настройки и статистика останутся, скачать заново можно в любой момент.</div></div></div>'
                 f'<div class="df"><span class="sp"></span>{btn("Отмена")}{btn("Удалить", "pri")}</div></div>')
    reset = ('<div class="dlg"><div class="dh">' + ic("refresh", 14, DD) + 'Сбросить настройки</div>'
             f'<div class="db">{ic("alert", 22, "var(--warn-ink)")}'
             '<div><div class="h3">Вернуть настройки к значениям по умолчанию?</div>'
             '<div class="sm" style="margin-top:6px">Сбросятся: горячая клавиша, режим, микрофон, '
             'индикатор, способ вставки, продвинутые настройки. <b>Останутся</b>: установленные модели, '
             'статистика и выбор активной модели. Сетевые тумблеры вернутся в «выключено».</div>'
             '<div class="c12" style="margin-top:8px">Копию текущих настроек '
             'сохраним</div></div></div>'
             f'<div class="df"><span class="sp"></span>{btn("Отмена")}{btn("Сбросить", "pri")}</div></div>')
    restart = ('<div class="dlg"><div class="dh">' + ic("power", 14, DD) + 'Перезапуск</div>'
               f'<div class="db">{ic("power", 22, "var(--primary)")}'
               '<div><div class="h3">Перезапустить Astra Voice?</div>'
               '<div class="sm" style="margin-top:6px">Версия 0.2.1 установлена. После перезапуска '
               'программа снова свернётся в трей, горячая клавиша продолжит работать. Идущая запись '
               'будет отменена.</div></div></div>'
               f'<div class="df"><span class="sp"></span>{btn("Позже")}'
               f'{btn("Перезапустить", "pri")}</div></div>')
    corrupt = ('<div class="dlg"><div class="dh">' + ic("alert", 14, DD) + 'Настройки</div>'
               f'<div class="db">{ic("alert", 22, "var(--warn-ink)")}'
               '<div><div class="h3">Настройки не читались</div>'
               '<div class="sm" style="margin-top:6px">Файл настроек повреждён. Мы сохранили '
               'копию и вернули значения по умолчанию — проверьте горячую клавишу '
               'и микрофон.</div></div></div>'
               f'<div class="df"><span class="sp"></span>{btn("Открыть папку", "", "folder")}'
               f'{btn("Понятно", "pri")}</div></div>')
    notray = ('<div class="dlg"><div class="dh">' + ic("info", 14, DD) + 'Системный трей</div>'
              f'<div class="db">{ic("info", 22, DD)}'
              '<div><div class="h3">Системный трей недоступен</div>'
              '<div class="sm" style="margin-top:6px">Программа работает, но значка в панели нет. '
              'Закрытие окна свернёт её; вернуть окно можно командой '
              '<span class="mono">astra-voice</span>.</div></div></div>'
              f'<div class="df"><span class="sp"></span>{btn("Понятно", "pri")}</div></div>')
    polkit = ('<div class="dlg" style="width:430px;border-color:var(--fg4)">'
              '<div class="dh">' + ic("lock", 14, DD) + 'Требуется аутентификация — системное окно KDE</div>'
              f'<div class="db">{ic("shield", 22, DD)}'
              '<div><div class="h3">Установка пакета</div>'
              '<div class="sm" style="margin-top:6px">Для установки обновления требуется пароль '
              'администратора.</div>'
              '<div class="field" style="margin-top:10px">Пароль</div>'
              '<div class="c12" style="margin-top:8px">Рисуем схематично: это <b>окно KDE polkit</b>, '
              'не наше. Мы не оформляем его и не можем в него вмешаться — важно только, что оно '
              '<b>одно</b> за всю установку.</div></div></div>'
              f'<div class="df"><span class="sp"></span>{btn("Отмена")}{btn("Подтвердить", "pri")}</div></div>')
    n_update = ('<div class="kn"><div class="knh">' + tray("idle", 14, "#8C97AC")
                + 'Astra Voice</div><b>Обновление установлено</b>'
                'Версия 0.2.1 готова к работе. Перезапустите программу, чтобы она заработала.'
                '<div class="knb"><span class="p">Перезапустить</span><span>Позже</span></div></div>')
    n_mic = ('<div class="kn"><div class="knh">' + tray("error", 14, "#F0645F")
             + 'Astra Voice</div><b>Микрофон молчит</b>'
             'Устройство открыто, но звука нет. Обычно помогает перезапуск звуковой службы.'
             '<div class="knb"><span class="p">Перезапустить звук</span><span>Что это</span></div></div>')
    n_ready = ('<div class="kn"><div class="knh">' + tray("idle", 14, "#8C97AC")
               + 'Astra Voice</div><b>Astra Voice готов</b>'
               'Зажмите <span style="font-family:var(--font-mono)">Ctrl + Space</span> и говорите.'
               '<div class="knb"><span>Открыть настройки</span></div></div>')
    n_avail = ('<div class="kn"><div class="knh">' + tray("idle", 14, "#8C97AC")
               + 'Astra Voice</div><b>Доступна версия 0.2.1</b>'
               'Показать, что нового, и решить — устанавливать или нет.'
               '<div class="knb"><span class="p">Что нового</span><span>Напомнить позже</span></div></div>')
    n_hot = ('<div class="kn"><div class="knh">' + tray("error", 14, "#F0645F")
             + 'Astra Voice</div><b>Горячая клавиша не захвачена</b>'
             'Ctrl + Space занята другим приложением — диктовка не работает.'
             '<div class="knb"><span class="p">Выбрать другую</span></div></div>')
    leg = ("<b>Модальных окон в программе пять, и все — подтверждения необратимого</b> (удаление, сброс, "
           "перезапуск) либо разовые сообщения о поломке. Ни одно состояние сети или обновления модалку "
           "не открывает. Уведомления KDE несут <b>кнопку действия</b>, а не просто текст, и повторяются "
           "не чаще одного раза на событие. Окно polkit — системное, показано схематично: наша "
           "ответственность в том, что оно ровно одно за установку.")
    return page(f"A · Диалоги и уведомления — {th(theme)}",
                f'<b>Диалоги и системные окна</b> — подтверждения, polkit, уведомления KDE · {th(theme)} тема',
                sech("Подтверждения") + grid([
                    stcell("удаление модели", del_model, "confirm-delete"),
                    stcell("сброс настроек", reset, "confirm-reset"),
                ]) + grid([
                    stcell("нужен перезапуск", restart, "restart"),
                    stcell("настройки повреждены", corrupt, "settings-corrupt"),
                ]) + grid([
                    stcell("нет системного трея", notray, "no-tray"),
                    stcell("окно polkit — системное, схематично", polkit, "polkit"),
                ]) + sech("Уведомления KDE") + grid([
                    stcell("готов к работе (после онбординга)", n_ready, "ready"),
                    stcell("микрофон молчит", n_mic, "mic-silent"),
                ]) + grid([
                    stcell("доступно обновление — одно уведомление", n_avail, "update-available"),
                    stcell("обновление установлено", n_update, "update-done"),
                ]) + grid([
                    stcell("горячая клавиша не захвачена", n_hot, "hotkey-not-grabbed"),
                ], 1),
                theme, leg, EXTRA_CSS)


def build():
    out = []
    for theme in ("light", "dark"):
        d = "" if theme == "light" else "-dark"
        out.append(write(f"08-onboarding-1-network{d}.html", onb1(theme)))
        out.append(write(f"08-onboarding-2-model{d}.html", onb2(theme)))
        out.append(write(f"08-onboarding-3-hotkey{d}.html", onb3(theme)))
        out.append(write(f"08-onboarding-4-mic{d}.html", onb4(theme)))
        out.append(write(f"08-onboarding-5-done{d}.html", onb5(theme)))
        out.append(write(f"08-onboarding-progress{d}.html", onb_progress(theme)))
        out.append(write(f"09-pill{d}.html", pill_screen(theme)))
        out.append(write(f"09-tray{d}.html", tray_screen(theme)))
        out.append(write(f"10-dialogs{d}.html", dialogs(theme)))
    return out
