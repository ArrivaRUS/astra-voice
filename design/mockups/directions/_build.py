# -*- coding: utf-8 -*-
"""Сборка макетов направлений A/B/C для Astra Voice.

    cd design/mockups/directions && python3 _build.py

Пишет A/*.html, B/*.html, C/*.html и index.html. Все файлы самодостаточны:
без внешних ресурсов и скриптов, шрифты — системные PT Root UI / PT Mono с fallback.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _base import page, write, logo, mark, appicon, ic, FONTS, VARS_LIGHT, CSS  # noqa: E402
import _dir_a  # noqa: E402
import _dir_b  # noqa: E402
import _dir_c  # noqa: E402

SCREENS = [
    ("01", "Главный экран — светлая", "01-general.html", "01-general.html", "01-popover.html"),
    ("01", "Главный экран — тёмная", "01-general-dark.html", "01-general-dark.html", "01-popover-dark.html"),
    ("02", "Модели (каталог) — светлая", "02-models.html", "02-models.html", "02-models.html"),
    ("02", "Модели (каталог) — тёмная", "02-models-dark.html", "02-models-dark.html", "02-models-dark.html"),
    ("03", "Обновления + строка внизу", "03-updates.html", "03-updates.html", "03-updates.html"),
    ("03", "Обновления: администратор / ЗПС", "03-updates-admin.html", "03-updates-admin.html",
     "03-updates-admin.html"),
    ("04", "Онбординг 1/5 — сеть", "04-onboarding-network.html", "04-onboarding-network.html",
     "04-onboarding-network.html"),
    ("04", "Онбординг 3/5 — хоткей", "04-onboarding-hotkey.html", "04-onboarding-hotkey.html",
     "04-onboarding-hotkey.html"),
    ("05", "Пилюля-оверлей (все состояния)", "05-pill.html", "05-pill.html", "05-pill.html"),
    ("06", "Трей: значок и меню", "06-tray.html", "06-tray.html", "06-tray.html"),
]

DIRS = [
    ("A", "Панель",
     "Классическое окно настроек с левым сайдбаром — как KDE System Settings и как Handy.",
     "900×620, сайдбар 184 px, 6 разделов + скрытая «Отладка». Строки «заголовок · «?» · контрол справа», "
     "собранные в карточки-группы под серыми CAPS-заголовками. Плотно: на экране «Общие» помещаются все "
     "восемь настроек сразу, без прокрутки.",
     [("Новичок на Astra", "Узнаваемо: выглядит как системные настройки, ничего изучать не надо. "
                           "Но плотность выше, подсказки спрятаны в «?»."),
      ("Корпоративный админ", "Лучший вариант: всё видно за один взгляд, разделы предсказуемы, "
                              "«Сеть и обновления» — отдельный пункт, его легко описать в инструкции.")],
     "Ближе всего к канону категории и к тому, чем заказчик уже пользуется (Handy). "
     "Самая дешёвая реализация: один Qt-сайдбар + StackLayout."),
    ("B", "Лента",
     "Одна колонка карточек-групп без сайдбара, крупный воздух, подсказки видны текстом.",
     "900×620, шапка с поиском по настройкам, полоса «чипов»-разделов вместо сайдбара, контент 620 px "
     "по центру. Сверху главного экрана — блок состояния: «Готов», горячая клавиша, уровень микрофона. "
     "В каталоге метрики разложены в три колонки: Качество · Скорость · <b>Память в работе</b>.",
     [("Новичок на Astra", "Самый понятный: каждая настройка объяснена рядом, крупный шрифт, "
                           "нечего искать. Прокрутка длиннее."),
      ("Корпоративный админ", "Дольше искать конкретную строку в длинной ленте; поиск по настройкам "
                              "частично это лечит.")],
     "Самый «современный минималистичный» и самый читаемый для П1; риск — длинная прокрутка "
     "на разделах «Продвинутые» и «Модели» (12 карточек)."),
    ("C", "Пульт",
     "Компактный поповер от значка в трее; полные настройки — вторым уровнем.",
     "Поповер 380×520: состояние, горячая клавиша, уровень микрофона, активная модель с честными цифрами "
     "и три частых переключателя на одном экране. Второй уровень — окно 820×560 с верхними вкладками; "
     "каталог устроен как «список слева, карточка справа».",
     [("Новичок на Astra", "Каждый день видит только пульт — там ровно то, что нужно. "
                           "Полные настройки надо ещё открыть."),
      ("Корпоративный админ", "Два уровня = два места, где искать; зато каталог из 12 моделей "
                              "не превращается в ленту и карточка помещается целиком.")],
     "Ближе всего к тому, как утилита живёт на самом деле (в трее, а не в окне). "
     "Дороже в реализации: поповер-окно без фокуса + второй уровень."),
]

QML = [
    ("Сайдбар / вкладки / лента", "ListView + StackLayout, штатно", "нет"),
    ("Строка настройки «заголовок · ? · контрол»", "RowLayout + ToolTip", "нет"),
    ("Карточка-группа с разделителями", "Rectangle radius 10–12 + Column, разделители — Rectangle 1 px", "нет"),
    ("Переключатель (Switch)", "Controls 2 Switch со своим стилем", "нет"),
    ("Клавиша-хоткей (моно на подложке)", "Rectangle + Text, PT Mono", "нет"),
    ("Полоски качества/скорости", "два Rectangle (трек + заливка)", "нет"),
    ("Прогресс загрузки", "ProgressBar Controls 2", "нет"),
    ("Поле захвата комбинации", "Item с Keys.onPressed + фильтр модификаторов",
     "<b>да</b> — свой компонент HotkeyCapture"),
    ("Пилюля-оверлей с уровнем", "Window flags: FramelessWindowHint | WindowStaysOnTopHint | "
                                 "WindowDoesNotAcceptFocus | Tool; столбики — Repeater из Rectangle",
     "<b>да</b> — своё окно + сглаживание уровня (prev·0,7 + target·0,3, как в Handy)"),
    ("Поповер от трея (направление C)", "отдельное Window без рамки, позиционирование по геометрии трея",
     "<b>да</b> — только в направлении C"),
    ("Каталог «список + карточка» (C)", "ListView + правая панель", "нет"),
    ("Тема из kdeglobals", "чтение файла до первой отрисовки, свой набор цветов",
     "<b>да</b> — ThemeLoader (урок проекта: палитра Qt врёт)"),
    ("Значок в трее с состояниями", "QSystemTrayIcon + SVG hicolor из лого-пака", "нет"),
]

EXTRA = """
.doc{max-width:1060px;width:100%;background:var(--bg-surface);border:1px solid var(--border);
  border-radius:14px;padding:30px 34px;color:var(--fg1)}
.doc h1{font-size:28px;line-height:1.2;margin:0 0 6px}
.doc h2{font-size:20px;line-height:1.25;margin:30px 0 10px}
.doc h3{font-size:16px;font-weight:500;margin:0 0 4px}
.doc p{margin:0 0 10px;color:var(--fg2)}
.doc a{color:var(--primary)}
.lead{font-size:15px;color:var(--fg2)}
.gate{display:inline-flex;align-items:center;gap:8px;background:var(--warn-bg);color:var(--warn-ink);
  border-radius:8px;padding:8px 12px;font-size:13px;font-weight:500}
.dcard{border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin-bottom:14px}
.dcard.rec{border-color:var(--primary);border-width:1.5px;background:var(--primary-bg)}
.dt{display:flex;align-items:baseline;gap:10px;margin-bottom:6px}
.dt .dn{font-size:22px;font-weight:700}
.dt .dl{font-size:13px;color:var(--fg3)}
.who{display:flex;gap:14px;margin:12px 0 12px;flex-wrap:wrap}
.who div{flex:1;min-width:240px;background:var(--bg-sunk);border-radius:9px;padding:10px 12px;font-size:12.5px;
  color:var(--fg2)}
.who b{display:block;color:var(--fg1);margin-bottom:3px;font-size:12px;letter-spacing:.04em;text-transform:uppercase}
.links{display:flex;flex-wrap:wrap;gap:7px;margin-top:10px}
.links a{font-size:12.5px;border:1px solid var(--border);border-radius:7px;padding:5px 10px;
  text-decoration:none;background:var(--bg-surface)}
.links a:hover{border-color:var(--primary)}
table{border-collapse:collapse;width:100%;font-size:12.5px;margin-top:8px}
th,td{text-align:left;padding:7px 9px;border-bottom:1px solid var(--border-soft);vertical-align:top}
th{color:var(--fg3);font-weight:500;font-size:11.5px;letter-spacing:.05em;text-transform:uppercase}
td.mono{font-family:var(--font-mono);font-size:11.5px}
ul.t{margin:6px 0 0;padding-left:19px;color:var(--fg2);font-size:13px;line-height:1.65}
.swatch{display:inline-flex;align-items:center;gap:7px;font-size:12px;color:var(--fg2);margin-right:14px}
.sw{width:15px;height:15px;border-radius:4px;display:inline-block;border:1px solid rgba(0,0,0,.08)}
"""


def index():
    cards = ""
    for code, name, one, struct, who, verdict in DIRS:
        links = "".join('<a href="%s/%s">%s · %s</a>' % (code, {"A": s[2], "B": s[3], "C": s[4]}[code], s[0], s[1])
                        for s in SCREENS)
        whos = "".join('<div><b>%s</b>%s</div>' % (w, t) for w, t in who)
        rec = " rec" if code == "A" else ""
        badge = ('<span class="bd rec" style="font-size:12px">Рекомендую</span>' if code == "A" else "")
        cards += ('<div class="dcard%s"><div class="dt"><span class="dn">%s · «%s»</span>%s</div>'
                  '<p class="lead">%s</p><p style="font-size:13px">%s</p>'
                  '<div class="who">%s</div><p style="font-size:13px;color:var(--fg1)"><b>Вывод:</b> %s</p>'
                  '<div class="links">%s</div></div>'
                  % (rec, code, name, badge, one, struct, whos, verdict, links))

    qml = "".join('<tr><td>%s</td><td>%s</td><td>%s</td></tr>' % r for r in QML)

    pal = "".join('<span class="swatch"><span class="sw" style="background:%s"></span>%s</span>' % (h, n)
                  for h, n in [("#0E1729", "ink"), ("#1B3A73", "primary"), ("#12B3A0", "accent «слушаю»"),
                               ("#E8A33A", "processing"), ("#2FA36B", "done"), ("#D64545", "error")])

    body = (
        '<div class="doc">'
        '<div style="display:flex;align-items:center;gap:12px;margin-bottom:14px">%s'
        '<span style="flex:1"></span><span class="c12">UXAnalyst · 2026-09-07 · фаза дизайна P8</span></div>'
        '<h1>Три направления макета</h1>'
        '<p class="lead">Направления отличаются <b>структурой и характером</b>, а не цветом: палитра, '
        'типографика и знак у всех одни — фирменные (⛔ G2a пройден). Задача этого шага — выбрать '
        '<b>одно</b> направление, дальше оно разворачивается в полный макет всех экранов и состояний.</p>'
        '<p class="gate">%s ⛔ G2b — выбор направления. Правки внутри выбранного: лимит 2 раунда.</p>'

        '<h2>Как смотреть</h2>'
        '<p>Каждый файл открывается в браузере как есть — внешних ресурсов и скриптов нет. Окно нарисовано '
        'в реальном размере (900×620 для A и B, поповер 380×520 и второй уровень 820×560 для C). '
        'Под каждым макетом — подпись, какие состояния на нём показаны. Полная карта состояний и '
        'микрокопия — в <span class="mono">design/flows.md</span>.</p>'

        '<h2>Направления</h2>%s'

        '<h2>Что общее у всех трёх</h2>'
        '<ul class="t">'
        '<li><b>Палитра и состояния</b> — из <span class="mono">design/brand/brand-basics.md</span>: %s</li>'
        '<li><b>Типографика</b> — PT Root UI (интерфейс) и PT Mono (хоткеи, версии, цифры), обе уже стоят '
        'в Astra 1.8; размеры по шкале бренда: H2 20, H3 16, body 14, подписи 13/12.</li>'
        '<li><b>Знак 06 «Слоги»</b> — в титуле, в трее и на иконке приложения; цвет состояния несёт '
        '<b>только третья точка</b>, форма не меняется никогда.</li>'
        '<li><b>Цифры настоящие</b> из PRD §7.2: GigaAM v3 RNN-T · 232 МБ на диске · 415 МБ ОЗУ (замерено) · '
        'WER 7,6 %% · ≈42× быстрее речи · <span class="mono">Ctrl+Space</span>. Никакого «lorem ipsum» '
        'и никакого английского в интерфейсе.</li>'
        '<li><b>Строка внизу окна</b> — слева активная модель, справа состояние обновления; «Доступна '
        'версия X · Установить» — единственное цветовое пятно внизу (паттерн Handy, требование заказчика).</li>'
        '<li><b>Каталог честный</b>: размер на диске, память в работе с пометкой достоверности '
        '(«замерено на этом компьютере» жирным / «оценка» серым), две полоски с цифрами и источником, '
        'пунктуация, язык, лицензия, происхождение, статус.</li>'
        '</ul>'

        '<h2>Реализуемость в QtQuick Controls 2 (Qt 5.15)</h2>'
        '<p>Всё нарисованное достижимо штатными средствами: скругления 7–12 px, тени только у окон и меню, '
        '<b>без blur / backdrop-filter</b>, без обязательных CSS-анимаций. Ниже — где потребуется свой '
        'QML-компонент.</p>'
        '<table><tr><th>Элемент</th><th>Чем делается</th><th>Нужен свой компонент</th></tr>%s</table>'

        '<h2>Доступность (§8 PRD)</h2>'
        '<ul class="t">'
        '<li>Контраст текста ≥ 4,5 : 1 в обеих темах; бирюза используется как графика, для текста — '
        '<span class="mono">#0B7F71</span> (светлая) / <span class="mono">#2FD9C4</span> (тёмная).</li>'
        '<li><b>Состояние записи никогда не передаётся только цветом</b>: столбики уровня, галочка, '
        'треугольник, подпись словами, значок в трее и (по желанию) звук.</li>'
        '<li>Значок в трее нельзя выключить — иначе запись стала бы скрытой (§9.5). В макетах эта строка '
        'показана как <b>disabled</b>.</li>'
        '<li>Клавиатурная навигация подразумевается везде: Tab/Space/Enter/Esc, Ctrl+, Ctrl+Q, Ctrl+Shift+D.</li>'
        '</ul>'

        '<h2>Чего в макетах нет (но есть в flows.md)</h2>'
        '<p>Показаны ключевые экраны и самые спорные состояния. Остальные состояния — «Проверяю обновления…», '
        '«Установлена последняя версия», «Загрузка 42 %%», «Установка…», «Перезапустить», «Пакет не прошёл '
        'проверку подписи», «Каталог не обновлён», «Модель повреждена», «Недостаточно места», «Микрофон занят», '
        '«Крах воркера», статистика и диагностика — описаны в <span class="mono">design/flows.md</span> '
        '§3–§5 и разворачиваются в полный макет после выбора направления.</p>'

        '<h2>Вопросы на приёмку</h2>'
        '<ul class="t">'
        '<li><b>Какое направление</b> разворачиваем в полный макет — A, B или C?</li>'
        '<li>Пилюля: ширина «резиновая» под русский текст (172 → 260 px) или одна фиксированная?</li>'
        '<li>Каталог: показывать все 12 моделей сразу или «Доступные» под кнопкой «Показать ещё»?</li>'
        '<li>Подтверждение «Готово» — 300 мс (PRD F3.2) успевает прочитаться, или поднять до 600 мс?</li>'
        '</ul>'
        '</div>'
        % (logo(30, 20), ic("alert", 16, "var(--warn-ink)"), cards, pal, qml))

    return page("Astra Voice — направления макета",
                '<b>Astra Voice</b> — макеты, три направления · ⛔ G2b', body, "light", EXTRA, "")


def main():
    files = []
    files += _dir_a.build()
    files += _dir_b.build()
    files += _dir_c.build()
    files.append(write("index.html", index()))
    for f in files:
        print(f)
    print("— всего файлов: %d" % len(files))


if __name__ == "__main__":
    main()
