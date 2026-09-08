# -*- coding: utf-8 -*-
"""Разделы «Вывод» (03), «Сеть и обновления» (04), «Продвинутые» (05),
«О программе» (06), «Отладка» (07) — полный макет A «Панель»."""
from _shell import (page, write, shell, head, footer, foot_right, FOOT_STATES, sb,
                    ic, tgl, key, btn, row, card, group, stcell, sech, grid, th, foot,
                    foot_left)
from _p1 import sel, seg

DD = 'var(--fg3)'


def num(v):
    return f'<span class="num" style="color:var(--fg1)">{v}</span>'


def stepper(v, unit=""):
    return ('<span class="sel" style="gap:11px;padding:5px 8px">'
            f'<span style="color:var(--fg3);font-size:14px">−</span>{num(v)}'
            f'<span class="c12">{unit}</span><span style="color:var(--fg3);font-size:14px">+</span></span>')


def slider(pct, label):
    return ('<span style="display:inline-flex;align-items:center;gap:10px">'
            '<span style="width:130px;height:4px;border-radius:2px;background:var(--bg-sunk);'
            'position:relative;display:inline-block">'
            f'<span style="position:absolute;left:0;top:0;height:4px;width:{pct}%;border-radius:2px;'
            'background:var(--primary)"></span>'
            f'<span style="position:absolute;left:calc({pct}% - 7px);top:-5px;width:14px;height:14px;'
            'border-radius:50%;background:var(--bg-surface);border:2px solid var(--primary)"></span>'
            f'</span>{num(label)}</span>')


# ══════════════════════════════════════════════════════════════════════════
# 03 · ВЫВОД
# ══════════════════════════════════════════════════════════════════════════
def klipper_note():
    return (f'<div class="note w" style="margin:9px 0 0">{ic("alert", 15, "var(--warn-ink)")}'
            '<div><b>Пока распознанный текст попадает в историю буфера Klipper</b>'
            'Защита истории появится в версии 0.3 (нужен флаг '
            '<span class="mono">x-kde-passwordManagerHint</span>). До этого текст можно вычистить '
            'вручную: значок Klipper → «Очистить историю».</div></div>')


def output_body():
    g1 = group("Как вставляется текст", [
        row("Способ вставки", sel("Ctrl + V — как обычная вставка", 268),
            "Текст кладётся в буфер как text/plain и вставляется в активное окно"),
        row("В терминалах вставлять Ctrl + Shift + V",
            f'<span style="display:flex;gap:9px;align-items:center">{btn("Список окон", "sm")}{tgl(True)}</span>',
            "Определяем по WM_CLASS: konsole, fly-term, xterm, yakuake, alacritty"),
        row("Задержка перед вставкой", stepper("50", "мс"),
            "Увеличьте, если окно не успевает принять вставку"),
        row("Пробел в конце текста", tgl(True), "Удобно диктовать несколько фраз подряд"),
        row("Первая буква заглавная", tgl(False),
            "Только для моделей без пунктуации — GigaAM v3 расставляет знаки сам"),
    ])
    g2 = group("Буфер обмена", [
        row("Восстанавливать прежнее содержимое буфера", tgl(True),
            "Через 100 мс после вставки. Нетекстовое содержимое (картинку, файл) вернуть нельзя"),
        row("Не оставлять распознанный текст в истории буфера", tgl(False),
            "Klipper запоминает всё, что попадает в буфер"),
    ])
    g3 = group("Проверка", [
        row("Последний распознанный текст",
            f'<span style="display:flex;gap:9px;align-items:center">'
            f'<span class="c12">«Прошу подтвердить сроки по первому этапу»</span>'
            f'{btn("Скопировать", "sm")}</span>',
            "Хранится только в памяти до выхода из программы — на диск не пишется", hint=False),
        row("Проверить вставку",
            f'<span style="display:flex;gap:9px;align-items:center;width:340px">'
            '<span class="field" style="flex:1">Поставьте курсор сюда и продиктуйте фразу</span></span>',
            hint=False),
    ])
    return g1 + g2 + klipper_note() + '<div style="height:16px"></div>' + g3


def output(theme):
    b = shell("Вывод", head("Вывод", "Куда попадает текст и что происходит с буфером обмена"),
              output_body() + sb(6, 230), footer(state="disabled"))
    paste_pop = ('<div class="pop" style="position:static;box-shadow:none;margin:0">'
                 f'<div class="po on">{ic("check", 13, "var(--primary)")}<span>Ctrl + V — как обычная '
                 'вставка<span class="d2">По умолчанию; работает в LibreOffice, браузере, Kate</span>'
                 '</span></div>'
                 '<div class="po"><span style="width:13px"></span><span>Ctrl + Shift + V'
                 '<span class="d2">Терминалы Konsole и fly-term</span></span></div>'
                 '<div class="po"><span style="width:13px"></span><span>Shift + Insert'
                 '<span class="d2">Старые приложения и X-совместимые терминалы</span></span></div>'
                 '<div class="po"><span style="width:13px"></span><span>Не вставлять — только положить '
                 'в буфер<span class="d2">Вставите сами, куда нужно</span></span></div></div>')
    copy_has = ('<div class="card">'
                + row("Последний распознанный текст",
                      f'<span style="display:flex;gap:9px;align-items:center">'
                      f'<span class="c12">«Прошу подтвердить сроки»</span>{btn("Скопировать", "sm")}</span>',
                      "Хранится в памяти до выхода", hint=False) + '</div>')
    copy_empty = ('<div class="card">'
                  + row("Последний распознанный текст",
                        f'<span style="display:flex;gap:9px;align-items:center">'
                        f'<span class="c12" style="color:var(--fg-dis)">Пока ничего не распознано</span>'
                        f'{btn("Скопировать", "sm dis")}</span>',
                        "Появится после первой диктовки", hint=False, dis=True) + '</div>')
    test_idle = ('<div class="card"><div class="r" style="flex-direction:column;align-items:stretch;gap:8px">'
                 '<div class="lbl">Проверить вставку</div>'
                 '<div class="field">Поставьте курсор сюда и продиктуйте фразу</div>'
                 '<div class="c12">Вставка пойдёт в это поле, а не в другое окно</div></div></div>')
    test_ok = ('<div class="card"><div class="r" style="flex-direction:column;align-items:stretch;gap:8px">'
               '<div class="lbl">Проверить вставку</div>'
               '<div class="field" style="color:var(--fg1)">Проверка связи, вставка работает.'
               '<span style="border-left:1.5px solid var(--primary);margin-left:1px">&nbsp;</span></div>'
               '<div class="c12" style="color:var(--ok-ink)">'
               + ic("check", 12, "var(--ok-ink)") +
               ' Вставлено за 0,42 с · буфер восстановлен</div></div></div>')
    test_clip = ('<div class="card"><div class="r" style="flex-direction:column;align-items:stretch;gap:8px">'
                 '<div class="lbl">Проверить вставку</div>'
                 '<div class="field">Поставьте курсор сюда и продиктуйте фразу</div>'
                 '<div class="c12" style="color:var(--warn-ink)">' + ic("alert", 12, "var(--warn-ink)") +
                 ' Активного окна не было — текст остался в буфере обмена</div></div></div>')
    klip_on = ('<div class="card">'
               + row("Не оставлять распознанный текст в истории буфера", tgl(True),
                     "Klipper пропустит текст мимо истории (флаг x-kde-passwordManagerHint)",
                     hint=False) + '</div>')
    leg = ("<b>Вставка через буфер</b> (PRD F5): сохранить буфер → положить text/plain → задержка 50 мс → "
           "комбинация → +100 мс → вернуть буфер. Честно сказано то, что в v1 сделать нельзя: нетекстовый "
           "буфер не восстанавливается, а история Klipper пока хранит распознанный текст — это постоянная "
           "подпись, а не исчезающий тост. Тестовое поле нужно, чтобы проверить вставку, не переключаясь "
           "в чужое окно.")
    return page(f"A · Вывод — {th(theme)}",
                f'<b>Вывод</b> — способ вставки, буфер, проверка · {th(theme)} тема · 900×620',
                b + sech("Состояния") + grid([
                    stcell("способ вставки — раскрытый список", paste_pop, "select"),
                    stcell("защита истории буфера включена (v0.3)", klip_on, "on"),
                ]) + grid([
                    stcell("последний текст есть", copy_has, "success"),
                    stcell("последнего текста ещё нет", copy_empty, "empty"),
                ]) + grid([
                    stcell("тестовое поле — покой", test_idle, "idle"),
                    stcell("тестовое поле — вставка прошла", test_ok, "success"),
                ]) + grid([
                    stcell("активного окна не было — только буфер", test_clip, "clipboard-only"),
                ], 1), theme, leg)


# ══════════════════════════════════════════════════════════════════════════
# 04 · СЕТЬ И ОБНОВЛЕНИЯ
# ══════════════════════════════════════════════════════════════════════════
def network_body(profile="personal"):
    lock = profile == "secure"
    g1 = group("Сетевые проверки", [
        row("Проверять обновления утилиты", tgl(False, lock=lock),
            "Не чаще раза в сутки · github.com/ArrivaRUS/astra-voice", lock=lock),
        row("Проверять обновления моделей", tgl(False, lock=lock),
            "Не чаще раза в сутки · huggingface.co", lock=lock),
        row("Офлайн-режим", tgl(lock, lock=lock),
            "Полностью запрещает сетевые запросы; «Установить из файла» остаётся", lock=lock),
    ])
    g2 = group("Источники", [
        row("Каталог моделей", sel("Встроенный список из пакета", 268), lock=lock),
        row("Адрес каталога",
            f'<span style="display:flex;gap:8px;align-items:center">'
            f'<span class="field mono" style="font-size:12px;width:268px;color:var(--fg-dis)">'
            f'https://mirror.corp.local/av/catalog.json</span>{btn("Проверить", "sm")}</span>',
            "Свой манифест того же формата с отсоединённой подписью (F15)", lock=lock, dis=not lock),
        row("Источник обновлений утилиты", sel("GitHub Releases (по умолчанию)", 268), lock=lock),
    ])
    g3 = group("Вручную", [
        row("Проверить обновления сейчас",
            btn("Проверить сейчас", "sm", "refresh") + " " + btn("Обновить из файла…", "sm", "file"),
            "Ручная проверка работает и при выключенных тумблерах — это ваше явное действие", hint=False),
    ])
    hosts = (f'<div class="note i" style="margin-top:16px">{ic("shield", 15, DD)}'
             '<div><b>Всего четыре сетевых действия — и ни одного больше</b>'
             'Проверка обновлений утилиты · проверка обновлений моделей · скачивание модели по кнопке · '
             'скачивание пакета по кнопке. Телеметрии, отчётов об ошибках и «отправить статистику» нет '
             'вовсе. Полный список хостов — «О программе → Приватность».</div></div>')
    return g1 + g2 + g3 + hosts


def network(theme):
    b = shell("Сеть и обновления", head("Сеть и обновления", "Четыре сетевых действия — и ни одного больше"),
              network_body() + sb(6, 240), footer(state="disabled"))
    url_ok = (f'<div class="note o">{ic("check", 15, "var(--ok-ink)")}'
              '<div><b>Каталог доступен</b>'
              '12 моделей · подпись манифеста проверена ключом из '
              '<span class="mono">/etc/astra-voice/policy.conf</span> · обновлён 07.09.2026.</div></div>')
    url_sig = (f'<div class="note w">{ic("alert", 15, "var(--warn-ink)")}'
               '<div><b>Каталог не обновлён</b>'
               'Подпись манифеста не совпала с ключом — показан предыдущий список моделей. '
               'Это защита от подмены, а не сбой сети.'
               f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Подробнее", "pri")}'
               f'{btn("Вернуть встроенный список")}</div></div></div>')
    url_bad = (f'<div class="note e">{ic("alert", 15, "var(--err-ink)")}'
               '<div><b>Адрес каталога недоступен</b>'
               'Не удалось связаться с <span class="mono">mirror.corp.local</span> (таймаут 3 с). '
               'Проверьте адрес или спросите его у администратора.'
               f'<div style="margin-top:9px">{btn("Повторить", "pri", "refresh")}</div></div></div>')
    url_chk = (f'<div class="note i">{ic("refresh", 15, DD)}'
               '<div><b>Проверяю адрес каталога…</b>'
               'Запрос к <span class="mono">mirror.corp.local</span> · ждём не дольше 3 с</div></div>')
    src_pop = ('<div class="pop" style="position:static;box-shadow:none;margin:0">'
               f'<div class="po on">{ic("check", 13, "var(--primary)")}<span>Встроенный список из пакета'
               '<span class="d2">Работает без сети; обновляется вместе с утилитой</span></span></div>'
               '<div class="po"><span style="width:13px"></span><span>GitHub — наш каталог'
               '<span class="d2">github.com/ArrivaRUS/astra-voice · подпись нашим ключом</span></span></div>'
               '<div class="po"><span style="width:13px"></span><span>Корпоративный адрес'
               '<span class="d2">Свой манифест и свои веса, ключ из policy.conf</span></span></div></div>')
    offline = ('<div class="card">'
               + row("Офлайн-режим", tgl(True),
                     "Все сетевые кнопки скрыты; работает только установка из файла", hint=False)
               + row("Проверить обновления сейчас", btn("Проверить сейчас", "sm dis", "refresh"),
                     "Недоступно: включён офлайн-режим", hint=False, dis=True) + '</div>')
    policy = ('<div class="card">'
              + row("Проверять обновления утилиты", tgl(False, lock=True),
                    "Задано администратором в /etc/astra-voice/policy.conf", lock=True, dis=True) + '</div>')
    leg = ("<b>Оба тумблера по умолчанию пусты во всех профилях</b> (решение заказчика 2026-09-07, PRD "
           "F14.2) — до явного включения программа не ходит в сеть. При этом кнопки «Скачать» и «Проверить "
           "сейчас» работают: это явное действие человека, а тумблеры управляют только фоновыми проверками. "
           "Так же показан профиль «защищённый контур»: строка заблокирована и подписана «Задано "
           "администратором».")
    return page(f"A · Сеть и обновления — {th(theme)}",
                f'<b>Сеть и обновления</b> — тумблеры, каталог, ручная проверка · {th(theme)} тема',
                b + sech("Источник каталога и проверка адреса") + grid([
                    stcell("выбор источника", src_pop, "select"),
                    stcell("проверяю адрес", url_chk, "checking"),
                ]) + grid([
                    stcell("адрес проверен", url_ok, "ok"),
                    stcell("подпись манифеста не совпала", url_sig, "stale"),
                ]) + grid([
                    stcell("адрес недоступен", url_bad, "error"),
                    stcell("офлайн-режим включён пользователем", offline, "offline"),
                ]) + grid([
                    stcell("строка заблокирована политикой администратора", policy, "policy-locked"),
                ], 1), theme, leg)


def upanel(state):
    """Панель обновления утилиты (F9) — все состояния."""
    head_ = ('<div style="display:flex;align-items:center;gap:9px">'
             '<div class="h3">Доступна версия 0.2.1</div><span class="bd rec">Новая</span>'
             '<span style="flex:1"></span><span class="c12">7 сентября 2026 · 74,3 МБ</span></div>')
    whatsnew = ('<div class="sm" style="margin-top:7px;color:var(--fg2)"><b>Что нового</b></div>'
                '<ul class="ul"><li>Каталог моделей: 12 моделей, память в работе замеряется на вашем '
                'компьютере</li><li>Автозапуск при входе в систему</li>'
                '<li>Исправлено: пилюля перекрывалась панелью при смене раскладки</li></ul>')
    if state == "none":
        return (f'<div class="upan n"><div style="display:flex;align-items:center;gap:9px">'
                f'{ic("check", 17, "var(--ok-ink)")}<div class="h3">Установлена последняя версия</div>'
                '<span style="flex:1"></span><span class="c12">Проверено сегодня в 14:02</span></div>'
                '<div class="sm" style="margin-top:6px">Версия <span class="mono">0.2.0</span> от '
                '01.09.2026. Следующая автоматическая проверка — не раньше чем через сутки.</div>'
                f'<div style="margin-top:12px">{btn("Проверить ещё раз", "", "refresh")}</div></div>')
    if state == "checking":
        return (f'<div class="upan n"><div style="display:flex;align-items:center;gap:9px">'
                f'{ic("refresh", 17, DD)}<div class="h3">Проверяю обновления…</div></div>'
                '<div class="sm" style="margin-top:6px">Запрос к '
                '<span class="mono">github.com/ArrivaRUS/astra-voice/releases/latest</span> · '
                'ответ ждём не дольше 3 с.</div>'
                f'<div style="margin-top:12px">{btn("Отмена")}</div></div>')
    if state == "available":
        return (f'<div class="upan">{head_}{whatsnew}'
                '<div style="display:flex;gap:8px;margin-top:12px;align-items:center">'
                + btn("Скачать и установить", "pri", "down") + btn("Пропустить эту версию")
                + btn("Напомнить позже", "gh")
                + '<span style="flex:1"></span>'
                '<span class="c12">Подпись релиза проверяется до установки</span></div></div>')
    if state == "downloading":
        return (f'<div class="upan">{head_}'
                '<div class="prog" style="margin:12px 0 7px"><i style="width:42%"></i></div>'
                '<div style="display:flex;align-items:center;gap:9px">'
                '<span class="num" style="color:var(--fg2);font-size:12.5px">Загрузка 42 % · '
                '31,2 из 74,3 МБ · 3,8 МБ/с · осталось ~11 с</span>'
                f'<span style="flex:1"></span>{btn("Отмена")}</div></div>')
    if state == "verifying":
        return (f'<div class="upan">{head_}'
                '<div class="prog" style="margin:12px 0 7px"><i style="width:100%"></i></div>'
                '<div style="display:flex;align-items:center;gap:9px">'
                f'{ic("shield", 15, DD)}<span class="sm">Проверяю контрольную сумму и подпись пакета…</span>'
                '<span style="flex:1"></span><span class="c12">Отмена недоступна</span></div></div>')
    if state == "polkit":
        return (f'<div class="upan">{head_}'
                f'<div class="note i" style="margin-top:12px">{ic("shield", 15, DD)}'
                '<div><b>Жду подтверждения администратора</b>'
                'Открыто системное окно ввода пароля (polkit). Это единственное окно с паролем за всю '
                'установку.</div></div>'
                f'<div style="margin-top:12px">{btn("Отмена")}</div></div>')
    if state == "installing":
        return (f'<div class="upan">{head_}'
                '<div class="prog" style="margin:12px 0 7px"><i style="width:78%"></i></div>'
                '<div class="sm">Установка пакета <span class="mono">astra-voice_0.2.1_amd64.deb</span>… '
                'Диктовка сейчас недоступна.</div></div>')
    if state == "restart":
        return (f'<div class="upan"><div style="display:flex;align-items:center;gap:9px">'
                f'{ic("check", 17, "var(--ok-ink)")}<div class="h3">Установлена версия 0.2.1</div></div>'
                '<div class="sm" style="margin-top:6px">Чтобы новая версия заработала, программу нужно '
                'перезапустить. Настройки и модели останутся на месте.</div>'
                '<div style="display:flex;gap:8px;margin-top:12px">'
                + btn("Перезапустить сейчас", "pri", "power") + btn("Позже") + '</div></div>')
    if state == "err-net":
        return (f'<div class="upan n"><div class="note e" style="margin:0">'
                f'{ic("alert", 15, "var(--err-ink)")}'
                '<div><b>Не удалось скачать обновление</b>'
                'Соединение с <span class="mono">github.com</span> оборвалось на 42 %. Скачанная часть '
                'сохранена — продолжим с того же места.'
                f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Повторить", "pri", "refresh")}'
                f'{btn("Обновить из файла…", "", "file")}</div></div></div></div>')
    if state == "err-sig":
        return (f'<div class="upan n"><div class="note e" style="margin:0">'
                f'{ic("alert", 15, "var(--err-ink)")}'
                '<div><b>Пакет не прошёл проверку подписи</b>'
                'Файл повреждён или получен не из нашего релиза. Мы его удалили; установка не '
                'выполнялась. Это защита от подмены обновления.'
                f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Проверить ещё раз", "pri")}'
                f'{btn("Подробнее", "gh")}</div></div></div></div>')
    if state == "err-polkit":
        return (f'<div class="upan n"><div class="note w" style="margin:0">'
                f'{ic("alert", 15, "var(--warn-ink)")}'
                '<div><b>Установка отменена</b>'
                'Пакет <span class="mono">astra-voice_0.2.1_amd64.deb</span> сохранён в '
                '<span class="mono">~/.cache/astra-voice/updates/</span>. Установить можно позже.'
                f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Установить сейчас", "pri")}'
                f'{btn("Открыть папку", "", "folder")}</div></div></div></div>')
    if state == "err-agent":
        return (f'<div class="upan n"><div class="note e" style="margin:0">'
                f'{ic("alert", 15, "var(--err-ink)")}'
                '<div><b>Не найден агент авторизации</b>'
                'В сеансе не запущен <span class="mono">polkit-kde-authentication-agent-1</span>. '
                'Установите пакет вручную:<br>'
                '<span class="mono">sudo apt install ./astra-voice_0.2.1_amd64.deb</span>'
                f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Скопировать команду", "pri")}'
                f'{btn("Открыть папку", "", "folder")}</div></div></div></div>')
    if state == "unavailable":
        return (f'<div class="upan n"><div class="note i" style="margin:0">{ic("globe", 15, DD)}'
                '<div><b>Источник обновлений недоступен</b>'
                'Не удалось связаться с <span class="mono">github.com</span>. Это не мешает работе — '
                'диктовка не зависит от сети. Обновиться можно из файла.'
                f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Обновить из файла…", "pri", "file")}'
                f'{btn("Повторить", "", "refresh")}</div></div></div></div>')
    if state == "skipped":
        return (f'<div class="upan n"><div style="display:flex;align-items:center;gap:9px">'
                '<div class="h3">Версия 0.2.1 пропущена</div><span style="flex:1"></span>'
                '<span class="c12">Напомним о следующей версии</span></div>'
                '<div class="sm" style="margin-top:6px">Вы выбрали «Пропустить эту версию» 7 сентября. '
                'Обновиться до неё всё ещё можно.</div>'
                f'<div style="margin-top:12px">{btn("Показать 0.2.1", "")}</div></div>')
    raise KeyError(state)


UPANEL_STATES = [
    ("none", "обновлений нет"), ("checking", "проверяю"), ("available", "доступна версия"),
    ("downloading", "скачивается 42 %"), ("verifying", "проверка подписи"),
    ("polkit", "ожидание polkit"), ("installing", "установка"),
    ("restart", "установлено, нужен перезапуск"), ("err-net", "ошибка: сеть"),
    ("err-sig", "ошибка: подпись не сошлась"), ("err-polkit", "ошибка: отказ polkit"),
    ("err-agent", "нет агента авторизации"), ("unavailable", "источник недоступен"),
    ("skipped", "версия пропущена"),
]


def update_panel(theme):
    body = upanel("available") + '<div style="height:16px"></div>' + network_body() + sb(6, 250)
    b = shell("Сеть и обновления", head("Сеть и обновления", "Доступно обновление утилиты"),
              body, footer(state="available"))
    cells = [stcell(n, upanel(s), s) for s, n in UPANEL_STATES]
    leg = ("<b>Панель обновления раскрывается из строки внизу окна</b> — модалки нет ни в одном состоянии. "
           "Порядок трека A (обычная машина): доступна → скачивается → проверка подписи → одно окно polkit → "
           "установка → «Перезапустить». Любая ошибка называет причину и даёт действие; при отказе polkit "
           "пакет остаётся на диске, и путь к нему написан прямым текстом.")
    return page(f"A · Обновление утилиты — {th(theme)}",
                f'<b>Сеть и обновления</b> — панель обновления, все состояния · {th(theme)} тема',
                b + sech("Панель обновления — 14 состояний") + grid(cells), theme, leg)


def update_admin(theme):
    banner = (f'<div class="note w" style="margin-bottom:14px">{ic("lock", 15, "var(--warn-ink)")}'
              '<div><b>Профиль «защищённый контур»</b>'
              'Часть настроек задана администратором в <span class="mono">/etc/astra-voice/policy.conf</span>: '
              'офлайн-режим, корпоративный каталог, установка обновлений только администратором.</div></div>')
    panel = ('<div class="upan n"><div style="display:flex;align-items:center;gap:9px">'
             + ic("shield", 18, "var(--warn-ink)") + '<div class="h3">Доступна версия 0.2.1</div>'
             '<span class="bd upd">Установку выполняет администратор</span></div>'
             '<div class="sm" style="margin-top:6px">На этом компьютере включена замкнутая программная '
             'среда (<span class="mono">DIGSIG_ELF_MODE=1</span>). Мы скачаем пакет, файл контрольных '
             'сумм, подпись и инструкцию — передайте их администратору. Кнопки «Установить» здесь нет '
             'намеренно.</div>'
             '<ul class="ul"><li>astra-voice_0.2.1_amd64.deb — 74,3 МБ</li>'
             '<li>SHA256SUMS и SHA256SUMS.minisig — подпись проверена нашим ключом</li>'
             '<li>INSTALL-ADMIN.md — порядок установки и подписи для ЗПС</li></ul>'
             '<div style="display:flex;gap:8px;margin-top:12px">'
             + btn("Скачать пакет для администратора", "pri", "down") + btn("Открыть папку", "", "folder")
             + btn("Напомнить позже", "gh") + '</div></div>')
    b = shell("Сеть и обновления", head("Сеть и обновления", "Обновление устанавливает администратор"),
              banner + panel + '<div style="height:16px"></div>' + network_body("secure") + sb(6, 260),
              footer(state="admin-track"))
    ask = ('<div class="dlg" style="width:460px"><div class="dh">' + ic("shield", 14, DD)
           + 'Как устанавливать обновление</div>'
           f'<div class="db">{ic("info", 22, DD)}'
           '<div><div class="h3">На этом компьютере включена замкнутая программная среда?</div>'
           '<div class="sm" style="margin-top:6px">Определить автоматически не удалось: файл '
           '<span class="mono">/etc/astra-voice/policy.conf</span> недоступен для чтения. От ответа '
           'зависит, можем ли мы установить обновление сами или нужно передать пакет администратору.'
           '</div></div></div>'
           f'<div class="df">{btn("Не знаю")}<span class="sp"></span>{btn("Нет")}{btn("Да", "pri")}</div>'
           '</div>')
    noperm = (f'<div class="note w">{ic("shield", 15, "var(--warn-ink)")}'
              '<div><b>Установку выполняет администратор</b>'
              'Ваша учётная запись не входит в группы <span class="mono">astra-admin</span> и '
              '<span class="mono">sudo</span>, поэтому установить пакет отсюда нельзя. Скачаем всё '
              'нужное и покажем, что передать.'
              f'<div style="margin-top:9px">{btn("Скачать пакет для администратора", "pri", "down")}'
              '</div></div></div>')
    policy_admin = (f'<div class="note i">{ic("lock", 15, DD)}'
                    '<div><b>Обновления через администратора — так задано политикой</b>'
                    '<span class="mono">policy.conf: updates=admin</span>. Кнопка установки скрыта на '
                    'всех машинах с этим профилем, даже если права позволяют.</div></div>')
    ready = (f'<div class="note o">{ic("check", 15, "var(--ok-ink)")}'
             '<div><b>Пакет для администратора собран</b>'
             'Четыре файла лежат в <span class="mono">~/astra-voice-update-0.2.1/</span> — передайте '
             'папку администратору целиком, вместе с INSTALL-ADMIN.md.'
             f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Открыть папку", "pri", "folder")}'
             f'{btn("Скопировать путь", "gh")}</div></div></div>')
    leg = ("<b>Трек B — «устанавливает администратор»</b> (PRD F9, S10). Включается тремя путями: ЗПС "
           "(<span class=\"mono\">DIGSIG_ELF_MODE≠0</span>), нет прав администратора, "
           "<span class=\"mono\">policy.conf: updates=admin</span>. Во всех трёх кнопки «Установить» "
           "<b>нет вовсе</b> — вместо неё сбор пакета с подписью и инструкцией. Если определить среду не "
           "удалось, спрашиваем прямо; «Не знаю» ведёт в этот же безопасный трек.")
    return page(f"A · Обновление: администратор — {th(theme)}",
                f'<b>Сеть и обновления</b> — трек «устанавливает администратор» · {th(theme)} тема',
                b + sech("Как сюда попадают и чем заканчивается") + grid([
                    stcell("среду определить не удалось — спрашиваем", ask, "ask-zps"),
                    stcell("нет прав администратора", noperm, "no-rights"),
                ]) + grid([
                    stcell("задано политикой", policy_admin, "policy"),
                    stcell("пакет собран", ready, "ready"),
                ]), theme, leg)


def footer_states(theme):
    cells = []
    for s, n in FOOT_STATES:
        w = ('<div class="win" style="width:860px;height:36px">'
             + foot(foot_left("GigaAM v3 RNN-T", "idle"), foot_right(s)) + '</div>')
        cells.append(f'<div class="stc"><div class="stn">{n}<span class="code">{s}</span></div>{w}</div>')
    left = []
    for txt, st, icon in [("Загружаю GigaAM v3 RNN-T…", "loading", "processing"),
                          ("GigaAM v3 RNN-T", "active", "idle"),
                          ("Переключаю на GigaAM v3 CTC…", "switching", "processing"),
                          ("Модель не загрузилась — открыть «Модели»", "error", "error"),
                          ("Модель не выбрана — установить", "none", "error")]:
        w = ('<div class="win" style="width:420px;height:36px">'
             + foot(foot_left(txt, icon), '') + '</div>')
        left.append(f'<div class="stc"><div class="stn">{st}</div>{w}</div>')
    leg = ("<b>Строка внизу окна</b> — единственное место, где живёт состояние обновления, и единственное "
           "цветовое пятно внизу: акцент включается только для «Доступна версия» и «Перезапустить». "
           "Все проценты и версии набраны PT Mono, чтобы цифры не «прыгали» при обновлении. "
           "Слева — состояние активной модели: загружаю · активна · переключаю · ошибка · не выбрана.")
    return page(f"A · Строка внизу окна — {th(theme)}",
                f'<b>Строка внизу окна</b> — 17 состояний обновления + 5 состояний модели · {th(theme)} тема',
                sech("Справа: состояние обновления") + grid(cells, 1)
                + sech("Слева: активная модель") + grid(left, 1), theme, leg)


# ══════════════════════════════════════════════════════════════════════════
# 05 · ПРОДВИНУТЫЕ
# ══════════════════════════════════════════════════════════════════════════
def advanced(theme):
    g1 = group("Память и скорость", [
        row("Модель в памяти", seg("Держать постоянно", "Выгружать", True),
            "Держать — первая диктовка мгновенная, занято 415 МБ ОЗУ"),
        row("Выгружать через", stepper("5", "мин"),
            "Считается от последней диктовки; следующая после выгрузки ждёт ~2 с", dis=True),
        row("Потоки распознавания (ORT)", stepper("4", "из 8"),
            "По умолчанию min(4, число физических ядер). Больше — не всегда быстрее"),
    ])
    g2 = group("Запись", [
        row("Ограничение длины записи", stepper("120", "с"),
            "Дошли до предела — запись останавливается сама и уходит в распознавание"),
        row("Порог тишины", slider(38, "−60 дБ"),
            "Ниже этого уровня 2 секунды подряд — считаем, что микрофон молчит"),
        row("Отсекать тишину в начале и конце (VAD)", tgl(True),
            "silero-vad · экономит время распознавания на длинных паузах"),
        row("Игнорировать записи короче", stepper("0,3", "с"),
            "Случайное касание клавиши не приводит ни к вставке, ни к ошибке"),
    ])
    g3 = group("Файлы и место", [
        row("Папка моделей",
            f'<span style="display:flex;gap:8px;align-items:center">'
            f'<span class="c12 mono">~/.local/share/astra-voice/models</span>'
            f'{btn("Открыть", "sm", "folder")}{btn("Изменить", "sm")}</span>',
            "Занято 687 МБ · свободно на разделе 42,1 ГБ", hint=False),
        row("Надёжный режим захвата клавиш (evdev)", tgl(False),
            "Если комбинацию перехватывает другое приложение. Нужна группа input — экспериментально"),
        row("Посимвольный ввод вместо буфера (xdotool type)", tgl(False),
            "Аварийный вариант: кириллица вводится ненадёжно. Только если вставка не работает совсем"),
    ])
    g4 = group("Обслуживание", [
        row("Сбросить настройки",
            btn("Сбросить…", "sm") + " " + btn("Экспорт диагностики", "sm", "down"),
            "Сброс не удаляет модели и статистику", hint=False),
    ])
    b = shell("Продвинутые", head("Продвинутые", "То, что почти никому не нужно менять"),
              g1 + g2 + g3 + g4 + sb(6, 210), footer(state="disabled"))
    unload_on = ('<div class="card">'
                 + row("Модель в памяти", seg("Держать постоянно", "Выгружать", False), hint=False)
                 + row("Выгружать через", stepper("5", "мин"),
                       "Следующая диктовка после выгрузки ждёт загрузку ~2 с", hint=False) + '</div>')
    disk_low = (f'<div class="note w">{ic("alert", 15, "var(--warn-ink)")}'
                '<div><b>Мало места на диске</b>'
                'В папке моделей занято 687 МБ, свободно 340 МБ. Скачать Whisper large-v3-turbo '
                '(987 МБ) не получится — освободите место или выберите модель полегче.'
                f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Открыть папку", "pri", "folder")}'
                f'{btn("Показать лёгкие модели", "gh")}</div></div></div>')
    threads = ('<div class="card">'
               + row("Потоки распознавания (ORT)", stepper("8", "из 8"),
                     "Все ядра заняты распознаванием — система может подтормаживать", hint=False)
               + '</div>'
               + f'<div class="note i" style="margin-top:9px">{ic("info", 15, DD)}'
                 '<div>На этой машине 8 потоков дают +6 % скорости против 4 и заметно греют процессор. '
                 'Замер — из локальной статистики, «Отладка».</div></div>')
    leg = ("<b>Продвинутые — не свалка:</b> четыре группы, каждая строка объяснена подписью, "
           "экспериментальные помечены прямо в тексте («экспериментально», «аварийный вариант»), а не "
           "мелким значком. Строка «Выгружать через» <b>недоступна</b>, пока выбрано «Держать постоянно» — "
           "зависимость видна сразу. Место на диске показывается рядом с папкой моделей, а не в отдельном "
           "разделе: решение о скачивании принимается там же.")
    return page(f"A · Продвинутые — {th(theme)}",
                f'<b>Продвинутые</b> — память, запись, файлы, обслуживание · {th(theme)} тема',
                b + sech("Состояния") + grid([
                    stcell("выгрузка включена — строка стала активной", unload_on, "enabled"),
                    stcell("все потоки — предупреждение по производительности", threads, "warn"),
                ]) + grid([
                    stcell("мало места на диске", disk_low, "low-disk"),
                ], 1), theme, leg)


# ══════════════════════════════════════════════════════════════════════════
# 06 · О ПРОГРАММЕ
# ══════════════════════════════════════════════════════════════════════════
def about(theme):
    g1 = group("Версии", [
        row("Astra Voice",
            f'<span style="display:flex;gap:9px;align-items:center">{num("0.2.0")}'
            f'{btn("Проверить обновления", "sm", "refresh")}</span>',
            "Сборка от 01.09.2026 · deb из GitHub Releases", hint=False),
        row("Активная модель", f'<span class="c12 mono">GigaAM v3 RNN-T · a6039be</span>',
            "onnx-asr 0.6.1 · onnxruntime 1.17.1", hint=False),
        row("Среда выполнения", f'<span class="c12 mono">Python 3.11.2 · PyQt5 5.15.9 · Qt 5.15.8</span>',
            "Astra Linux SE 1.8 · KDE 5.27 · X11", hint=False),
    ])
    g2 = group("Лицензии и приватность", [
        row("Лицензия программы",
            f'<span style="display:flex;gap:9px;align-items:center"><span class="c12">GPL-3.0-or-later</span>'
            f'{btn("Открыть LICENSE", "sm", "file")}</span>',
            "Исходный код открыт: github.com/ArrivaRUS/astra-voice", hint=False),
        row("Лицензии компонентов и моделей", btn("NOTICE", "sm", "file"),
            "GigaAM — MIT · Сбер; Whisper — MIT и Apache-2.0 · OpenAI; sherpa-onnx — Apache-2.0; "
            "silero-vad — MIT; NeMo FastConformer — CC-BY-4.0 · NVIDIA", hint=False),
        row("Приватность и сетевые хосты", btn("PRIVACY", "sm", "shield"),
            "Аудио не покидает компьютер и не сохраняется на диск. Хостов всего два: huggingface.co, "
            "github.com — и только по вашей команде", hint=False),
    ])
    g3 = group("Данные на диске", [
        row("Настройки", f'<span style="display:flex;gap:8px;align-items:center">'
            f'<span class="c12 mono">~/.config/astra-voice/</span>{btn("Открыть", "sm", "folder")}</span>',
            hint=False),
        row("Модели", f'<span style="display:flex;gap:8px;align-items:center">'
            f'<span class="c12 mono">~/.local/share/astra-voice/models/ · 687 МБ</span>'
            f'{btn("Открыть", "sm", "folder")}</span>', hint=False),
        row("Логи и статистика", f'<span style="display:flex;gap:8px;align-items:center">'
            f'<span class="c12 mono">~/.local/state/astra-voice/ · 2,1 МБ</span>'
            f'{btn("Открыть", "sm", "folder")}</span>',
            "Логи без аудио и без распознанного текста — только длительности и коды ошибок", hint=False),
    ])
    g4 = group("Действия", [
        row("Пройти настройку заново",
            btn("Открыть онбординг", "sm") + " " + btn("Обновить из файла…", "sm", "file"),
            "Настройки не сбрасываются — просто пройдёте шаги ещё раз", hint=False),
    ])
    disc = (f'<div class="note i" style="margin-top:16px">{ic("info", 15, DD)}'
            '<div><b>О товарных знаках</b>'
            '«Astra Linux» — товарный знак ПАО «Группа Астра»; «GigaAM» — обозначение Сбера; '
            '«Whisper» — обозначение OpenAI; «Vosk» — Alpha Cephei. Программа разработана независимо, '
            '<b>не аффилирована</b> с указанными компаниями и не одобрена ими.</div></div>')
    b = shell("О программе", head("О программе", "Версии, лицензии, приватность и данные на диске"),
              g1 + g2 + g3 + g4 + disc + sb(6, 190), footer(state="disabled"))
    notice = ('<div class="dlg" style="width:520px"><div class="dh">' + ic("file", 14, DD)
              + 'NOTICE — лицензии компонентов и моделей</div>'
              '<div class="db" style="flex-direction:column;gap:0;padding-bottom:14px">'
              '<div class="sm" style="line-height:1.85">'
              '<b>Движки и библиотеки</b><br>'
              'onnxruntime — MIT · Microsoft<br>'
              'sherpa-onnx — Apache-2.0 · Xiaomi Corporation (NOTICE по §4(d) включён)<br>'
              'onnx-asr — MIT · Ilya Stupakov<br>'
              'silero-vad — MIT · Silero Team<br>'
              'PyQt5 — GPL-3.0 · Riverbank Computing<br>'
              '<div style="height:9px"></div><b>Модели</b><br>'
              'GigaAM v3 (RNN-T, CTC, Multilingual) — MIT · Сбер, GigaChat Team, 2024<br>'
              'T-one — Apache-2.0 · Т-Банк<br>'
              'Vosk ru 0.52 / 0.54 — Apache-2.0 · Alpha Cephei<br>'
              'Whisper (base, small, large-v3-turbo) — MIT и Apache-2.0 · OpenAI<br>'
              'NeMo FastConformer ru pc — CC-BY-4.0 · NVIDIA (требуется атрибуция)'
              '</div></div>'
              f'<div class="df">{btn("Скопировать", "", "file")}<span class="sp"></span>'
              f'{btn("Открыть файл", "", "folder")}{btn("Закрыть", "pri")}</div></div>')
    privacy = ('<div class="dlg" style="width:520px"><div class="dh">' + ic("shield", 14, DD)
               + 'PRIVACY — что уходит в сеть</div>'
               '<div class="db" style="flex-direction:column;gap:0;padding-bottom:14px">'
               '<div class="sm" style="line-height:1.8">'
               '<b>Аудио и текст никогда не покидают компьютер.</b> Запись живёт в памяти и стирается '
               'сразу после распознавания; на диск не пишется даже временный файл.<br>'
               '<div style="height:8px"></div><b>Все сетевые обращения — четыре, и каждое по вашей '
               'команде или по включённому вами тумблеру:</b>'
               '<div style="height:5px"></div>'
               '<span class="mono" style="font-size:12px">github.com</span> — проверка новой версии '
               '(раз в сутки, если включено) и скачивание пакета по кнопке<br>'
               '<span class="mono" style="font-size:12px">huggingface.co</span> — проверка обновлений '
               'моделей (раз в сутки, если включено) и скачивание модели по кнопке'
               '<div style="height:8px"></div>'
               'Телеметрии, отчётов об ошибках, «отправить статистику» — нет вовсе. Сейчас оба тумблера '
               '<b>выключены</b>, офлайн-режим выключен.</div></div>'
               f'<div class="df">{btn("Открыть PRIVACY.md", "", "file")}<span class="sp"></span>'
               f'{btn("Выключить сеть совсем")}{btn("Закрыть", "pri")}</div></div>')
    stat_data = ('<div class="card">'
                 + row("Статистика распознавания",
                       f'<span style="display:flex;gap:9px;align-items:center">'
                       f'<span class="c12 num">p50 0,38 с · p95 0,61 с · 148 диктовок</span>'
                       f'{btn("Очистить", "sm")}</span>',
                       "Только на этом компьютере, никуда не отправляется", hint=False) + '</div>')
    stat_empty = ('<div class="card">'
                  + row("Статистика распознавания",
                        f'<span style="display:flex;gap:9px;align-items:center">'
                        f'<span class="c12" style="color:var(--fg-dis)">Пока нет данных</span>'
                        f'{btn("Очистить", "sm dis")}</span>',
                        "Появится после первой диктовки", hint=False, dis=True) + '</div>')
    remove = ('<div class="card">'
              + row("Удалить данные программы",
                    btn("Показать, что удалять", "sm", "trash"),
                    "При удалении пакета остаются: настройки (12 КБ), модели (687 МБ), логи (2,1 МБ)",
                    hint=False) + '</div>')
    leg = ("<b>Экран для ИБ-службы (П3):</b> версии всех компонентов, лицензия <b>GPL-3.0-or-later</b>, "
           "NOTICE с атрибуцией каждой модели, PRIVACY со списком ровно двух хостов и дисклеймер о "
           "товарных знаках (research/legal.md §5 — «Astra Linux» знак ПАО «Группа Астра», продукт не "
           "аффилирован). Пути к данным — с кнопками «Открыть», чтобы удаление и бэкап не требовали "
           "инструкции.")
    return page(f"A · О программе — {th(theme)}",
                f'<b>О программе</b> — версии, лицензии, приватность, знаки · {th(theme)} тема',
                b + sech("Диалоги") + grid([stcell("NOTICE", notice, "notice"),
                                            stcell("PRIVACY", privacy, "privacy")])
                + sech("Статистика и удаление") + grid([
                    stcell("статистика есть", stat_data, "data"),
                    stcell("статистики ещё нет", stat_empty, "empty"),
                ]) + grid([stcell("что останется после удаления пакета", remove, "uninstall")], 1),
                theme, leg)


# ══════════════════════════════════════════════════════════════════════════
# 07 · ОТЛАДКА
# ══════════════════════════════════════════════════════════════════════════
def debug(theme):
    g1 = group("Журнал", [
        row("Уровень подробности", sel("Обычный", 236),
            "Ошибки · Обычный · Подробный. Логи не содержат аудио и распознанного текста", hint=False),
        row("Файлы журнала",
            f'<span style="display:flex;gap:8px;align-items:center">'
            f'<span class="c12 mono">~/.local/state/astra-voice/log/ · 5 × 1 МБ</span>'
            f'{btn("Открыть папку логов", "sm", "folder")}</span>', hint=False),
    ])
    g2 = group("Локальная статистика задержки", [
        row("Отпустил клавишу → текст",
            f'<span class="c12 num">p50 0,38 с · p95 0,61 с</span>',
            "148 диктовок с 01.09.2026 · холодные прогоны исключены", hint=False),
        row("Распределение по моделям",
            f'<span class="c12 num">RNN-T 0,38 с (121) · CTC 0,29 с (27)</span>',
            "Замеры этой машины перекрывают бенчмарк автора в карточках моделей", hint=False),
        row("Очистить статистику", btn("Очистить", "sm", "trash"),
            "Полоски «замерено» в каталоге вернутся к оценкам", hint=False),
    ])
    g3 = group("Диагностика", [
        row("Собрать архив для баг-репорта", btn("Экспорт диагностики", "sm", "down"),
            "Логи, settings.json без домашних путей, версии, вывод wpctl status. Без аудио и текстов",
            hint=False),
        row("Тестовая диктовка",
            f'<span style="display:flex;gap:9px;align-items:center;width:380px">'
            f'<span class="field" style="flex:1">Продиктуйте фразу — вставки в другие окна не будет</span>'
            f'{btn("Начать", "sm")}</span>', hint=False),
    ])
    b = shell("Общие", head("Отладка", "Скрытый раздел: Ctrl + Shift + D"),
              g1 + g2 + g3 + sb(6, 190), footer(state="disabled"), debug_on=True)
    collecting = (f'<div class="note i">{ic("refresh", 15, DD)}'
                  '<div><b>Собираю диагностику…</b>'
                  '<div class="prog" style="margin:8px 0 6px"><i style="width:62%"></i></div>'
                  'Логи → настройки → версии → состояние звука. Читаем только своё.</div></div>')
    done = (f'<div class="note o">{ic("check", 15, "var(--ok-ink)")}'
            '<div><b>Архив собран</b>'
            '<span class="mono">~/astra-voice-diag-2026-09-08.zip</span> · 812 КБ. Аудио и распознанного '
            'текста внутри нет — можно отправлять как есть.'
            f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Открыть папку", "pri", "folder")}'
            f'{btn("Показать содержимое", "gh")}</div></div></div>')
    err = (f'<div class="note e">{ic("alert", 15, "var(--err-ink)")}'
           '<div><b>Не удалось собрать архив</b>'
           'В домашней папке не хватает места (нужно ~5 МБ). Освободите место или выберите другую папку.'
           f'<div style="display:flex;gap:8px;margin-top:9px">{btn("Выбрать папку", "pri", "folder")}'
           f'{btn("Повторить")}</div></div></div>')
    stat_empty = ('<div class="card">'
                  + row("Отпустил клавишу → текст",
                        '<span class="c12" style="color:var(--fg-dis)">Пока нет данных</span>',
                        "Продиктуйте первую фразу — замеры появятся здесь", hint=False, dis=True)
                  + '</div>')
    test_run = ('<div class="card"><div class="r" style="flex-direction:column;align-items:stretch;gap:8px">'
                '<div class="lbl">Тестовая диктовка</div>'
                '<div class="field" style="color:var(--fg1)">Проверка связи, раз, два, три.</div>'
                '<div class="c12 num">Аудио 2,4 с · распознавание 0,31 с · модель GigaAM v3 RNN-T · '
                'вставки не было</div></div></div>')
    leg = ("<b>Отладка открывается по Ctrl + Shift + D</b> и не занимает места в сайдбаре у обычного "
           "пользователя. Статистика <b>локальная</b>: те же числа перекрывают бенчмарк автора в карточках "
           "моделей — это и есть «замерено на этом компьютере». Архив диагностики честно перечисляет, что "
           "внутрь попало, и подчёркивает, чего там нет.")
    return page(f"A · Отладка — {th(theme)}",
                f'<b>Отладка</b> — логи, статистика, диагностика (Ctrl+Shift+D) · {th(theme)} тема',
                b + sech("Состояния") + grid([
                    stcell("диагностика собирается", collecting, "collecting"),
                    stcell("архив готов", done, "done"),
                ]) + grid([
                    stcell("ошибка сборки архива", err, "error"),
                    stcell("статистики ещё нет", stat_empty, "empty"),
                ]) + grid([stcell("тестовая диктовка выполнена", test_run, "success")], 1),
                theme, leg)


def build():
    out = []
    for theme in ("light", "dark"):
        d = "" if theme == "light" else "-dark"
        out.append(write(f"03-output{d}.html", output(theme)))
        out.append(write(f"04-network{d}.html", network(theme)))
        out.append(write(f"04-update-panel{d}.html", update_panel(theme)))
        out.append(write(f"04-update-admin{d}.html", update_admin(theme)))
        out.append(write(f"04-footer-states{d}.html", footer_states(theme)))
        out.append(write(f"05-advanced{d}.html", advanced(theme)))
        out.append(write(f"06-about{d}.html", about(theme)))
        out.append(write(f"07-debug{d}.html", debug(theme)))
    return out
