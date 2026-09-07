# -*- coding: utf-8 -*-
"""Генератор раунда 2 бренд-концептов Astra Voice: concept-N.svg, concept-N-tray.svg, concepts-r2.html."""
import os, html

OUT = "/home/astra/Документы/astra-voice/design/brand"
FONT = "'PT Root UI','PT Astra Sans','Open Sans','Roboto',sans-serif"

SVG_STYLE = """  <style>
    /* Без CSS-переменных: librsvg (GTK-миниатюры, конвертеры) их не поддерживает
       и акцентные части пропадают. Только литеральные цвета и currentColor. */
    svg   { color:#0E1729; }
    .ink  { fill:none; stroke:currentColor; stroke-linecap:round; stroke-linejoin:round; }
    .acc  { fill:none; stroke:#12B3A0; stroke-linecap:round; stroke-linejoin:round; }
    .accf { fill:#12B3A0; stroke:none; }
    .inkf { fill:currentColor; stroke:none; }
    .wm   { fill:currentColor; stroke:none; }
    @media (prefers-color-scheme: dark) {
      svg { color:#F2F5FA; } .acc { stroke:#2FD9C4; } .accf { fill:#2FD9C4; }
    }
  </style>"""

TRAY_STYLE = """  <style>
    svg { color:#0E1729; }
    @media (prefers-color-scheme: dark) { svg { color:#F2F5FA; } }
    .s  { fill:none; stroke:currentColor; stroke-linecap:round; stroke-linejoin:round; }
    .st { fill:currentColor; stroke:none; }        /* элемент состояния */
  </style>"""

# ─────────────────────────────────────────────────────────────────────────────
# Каждый знак нарисован в квадрате 64×64 (визуальный центр 32,32).
# ─────────────────────────────────────────────────────────────────────────────

C = []

C.append(dict(
    fid="concept-1", num="01", title="Ёлочки", sub="прямая речь · типографика",
    kind="ref", w=700, ls="-0.2",
    idea="Русские кавычки-ёлочки — знак прямой речи. Открывающая тонкая (сказанное — воздух), "
         "закрывающая жирная (напечатанное — вещь), между ними акцентом лежит сама реплика.",
    note="Из раунда 1, «более-менее». Здесь — только с новым именем, форма не тронута.",
    mark="""    <!-- «ёлочки»: тонкая открывающая (речь) + жирная закрывающая (текст), между ними — сказанное -->
    <path class="ink" stroke-width="4.2" d="M14,15 L6,32 L14,49"/>
    <path class="ink" stroke-width="4.2" d="M22,15 L14,32 L22,49"/>
    <path class="ink" stroke-width="7"   d="M42,15 L50,32 L42,49"/>
    <path class="ink" stroke-width="7"   d="M50,15 L58,32 L50,49"/>
    <circle class="accf" cx="32" cy="32" r="5"/>""",
    tray="""    <path class="s" stroke-width="1.7" d="M8.2,6 L4.6,11 L8.2,16"/>
    <path class="s" stroke-width="2.5" d="M14.6,6 L18.2,11 L14.6,16"/>
    <circle class="st state" cx="11.4" cy="11" r="1.9"/>"""))

C.append(dict(
    fid="concept-1b", num="01b", title="Ёлочка", sub="уточнение 01 · одна пара",
    kind="ref", w=700, ls="-0.2",
    idea="То же самое, но убрана половина элементов: одна тонкая открывающая, одна жирная "
         "закрывающая, реплика между ними. Идея не изменилась — знак стал чище.",
    note="Зачем: у 01 в 16 px четыре шеврона слипаются в «решётку». Здесь три элемента вместо пяти — "
         "силуэт держится до 16 px, и точка-состояние крупнее.",
    mark="""    <!-- одна пара: тонкая (речь) → точка (реплика) → жирная (текст) -->
    <path class="ink" stroke-width="5"   d="M20,14 L10,32 L20,50"/>
    <path class="ink" stroke-width="8.5" d="M44,14 L54,32 L44,50"/>
    <circle class="accf" cx="32" cy="32" r="5.5"/>""",
    tray="""    <path class="s" stroke-width="1.7" d="M7.4,5.2 L3.8,11 L7.4,16.8"/>
    <path class="s" stroke-width="2.7" d="M14.6,5.2 L18.2,11 L14.6,16.8"/>
    <circle class="st state" cx="11" cy="11" r="2.1"/>"""))

C.append(dict(
    fid="concept-3", num="03", title="Клавиша", sub="жест · зажатая клавиша",
    kind="ref", w=700, ls="0",
    idea="Единственный жест продукта: зажал клавишу — говоришь. Форма-контейнер, внутри — "
         "индикатор состояния.",
    note="Из раунда 1, «более-менее». Здесь — только с новым именем, форма не тронута.",
    mark="""    <!-- клавиша: единственный жест продукта — зажать и говорить -->
    <rect class="ink" stroke-width="5" x="8" y="14" width="48" height="40" rx="12"/>
    <path class="acc" stroke-width="4.6" d="M23.5,30 L23.5,38"/>
    <path class="acc" stroke-width="4.6" d="M32,24 L32,44"/>
    <path class="acc" stroke-width="4.6" d="M40.5,28 L40.5,40"/>""",
    tray="""    <rect class="s" stroke-width="1.7" x="2.6" y="4.2" width="16.8" height="13.6" rx="4.4"/>
    <circle class="st state" cx="11" cy="11" r="2.4"/>"""))

C.append(dict(
    fid="concept-3b", num="03b", title="Клавиша", sub="уточнение 03 · одна форма на всех размерах",
    kind="ref", w=700, ls="0",
    idea="Та же клавиша, но внутри — не три штриха, а та самая точка состояния, что и в трее.",
    note="Зачем: у 03 логотип и трей-иконка сейчас разные (три штриха vs точка). Здесь форма одна "
         "на всех размерах — от 128 px до 16 px, и «слушаю» читается одинаково везде.",
    mark="""    <!-- клавиша + точка состояния: одна форма на всех размерах -->
    <rect class="ink" stroke-width="5" x="6" y="13" width="52" height="42" rx="13"/>
    <circle class="accf" cx="32" cy="34" r="6.5"/>""",
    tray="""    <rect class="s" stroke-width="1.7" x="2.6" y="4.2" width="16.8" height="13.6" rx="4.4"/>
    <circle class="st state" cx="11" cy="11" r="2.4"/>"""))

C.append(dict(
    fid="concept-5", num="05", title="Абзац", sub="типографика · ¶ знак абзаца",
    kind="new", w=500, ls="0.4",
    idea="Пилькроу ¶ — типографский знак «здесь начинается новый абзац». Метафора прямая: "
         "сказал — и в документе появился абзац. Головка знака сплошная, как в настоящем шрифте, а внутри неё живёт голос: точка состояния.",
    note="Плюс: типографский и «свой», как ёлочки, но силуэт цельный — не рассыпается на штрихи; внутри готовое место под индикатор, как в клавише. Минус: в 16 px головка становится плотным пятном с двумя ножками — узнаваемо, но менее чётко, чем клавиша; и у части людей ¶ = «показать непечатаемые символы» из Word. Стоит показать двум-трём будущим пользователям без объяснений.",
    mark="""    <!-- ¶ : сказал — появился абзац; внутри головки живёт голос -->
    <path class="inkf" d="M46,11.5 L31,11.5 A11.5,11.5 0 0 0 31,34.5 L46,34.5 Z"/>
    <path class="ink" stroke-width="5" d="M43.5,34.5 L43.5,53"/>
    <path class="ink" stroke-width="5" d="M31,34.5 L31,53"/>
    <circle class="accf" cx="29" cy="23" r="6.5"/>""",
    tray="""    <path class="st" d="M15.8,4.1 L10.6,4.1 A3.95,3.95 0 0 0 10.6,11.9 L15.8,11.9 Z"/>
    <path class="s" stroke-width="1.8" d="M14.9,11.9 L14.9,18"/>
    <path class="s" stroke-width="1.8" d="M10.6,11.9 L10.6,18"/>
    <circle class="st state" cx="9.9" cy="8" r="2.15"/>"""))

C.append(dict(
    fid="concept-6", num="06", title="Слоги", sub="геометрия · речь ложится строкой",
    kind="new", w=500, ls="1.2",
    idea="Речь дискретна — слоги, паузы, промежутки. Текст непрерывен — ровная строка. "
         "Три точки с сужающимися промежутками сверху, сплошная строка снизу: слоги ложатся строкой.",
    note="Плюс: строгая геометрия без единой кривой — это не «звуковая волна», а ритм речи; "
         "две линии дают знаку читаемый прямоугольный силуэт. Минус: самый абстрактный из четырёх — "
         "без подписи метафора считывается не с первого взгляда.",
    mark="""    <!-- слоги (точки, промежутки сужаются) ложатся в строку текста -->
    <circle class="accf" cx="10" cy="20" r="5.5"/>
    <circle class="accf" cx="31" cy="20" r="5.5"/>
    <circle class="accf" cx="48" cy="20" r="5.5"/>
    <path class="ink" stroke-width="10" d="M10,44 L54,44"/>""",
    tray="""    <circle class="st state" cx="4.8" cy="7.2" r="1.8"/>
    <circle class="st state" cx="11" cy="7.2" r="1.8"/>
    <circle class="st state" cx="16.4" cy="7.2" r="1.8"/>
    <path class="s" stroke-width="3.2" d="M4.8,15.4 L17.2,15.4"/>"""))

C.append(dict(
    fid="concept-7", num="07", title="Пилюля", sub="сам продукт · оверлей записи",
    kind="new", w=500, ls="0.2",
    idea="Логотипом становится то, что человек реально видит на экране: плавающая пилюля записи — "
         "слева живая точка «слушаю», справа растущая строка распознанного.",
    note="Плюс: самый честный знак — это буквально интерфейс продукта, и он уже задуман как "
         "контейнер с индикатором. Минус: горизонтальная форма слабее в квадратной иконке "
         "приложения, чем клавиша; и это второй «контейнер с точкой» в наборе после 03.",
    mark="""    <!-- оверлей записи как знак: живая точка + растущая строка внутри пилюли -->
    <rect class="ink" stroke-width="5" x="3" y="18" width="58" height="28" rx="14"/>
    <circle class="accf" cx="18.5" cy="32" r="6.5"/>
    <path class="ink" stroke-width="5.5" d="M31.5,32 L51,32"/>""",
    tray="""    <rect class="s" stroke-width="1.8" x="1.7" y="6.2" width="18.6" height="9.6" rx="4.8"/>
    <circle class="st state" cx="7.2" cy="11" r="2.2"/>
    <path class="s" stroke-width="1.7" d="M12.2,11 L16.6,11"/>"""))

C.append(dict(
    fid="concept-8", num="08", title="Voice V", sub="буквенный знак · воронка голоса",
    kind="new", w=700, ls="-0.2", split=True,
    idea="Буква V из имени Voice и одновременно воронка: голос (точка) входит сверху и сводится "
         "в одну точку — в текст. Знак и первая буква имени — один объект.",
    note="Плюс: единственный в наборе буквенный знак — он же короткое имя продукта, отлично держит "
         "16 px. Со знаком Astra Linux не пересекается: без звезды, без астры-цветка, без «А» и "
         "без круглой эмблемы — родство только по цвету. Минус: «шеврон + точка» — ходовая форма "
         "(похоже на «свернуть»/«скачать»), нужна проверка на конфликт с системными иконками KDE.",
    mark="""    <!-- V (Voice) как воронка: голос сверху сходится в точку — в текст -->
    <path class="ink" stroke-width="7.5" d="M8,14 L32,50 L56,14"/>
    <circle class="accf" cx="32" cy="28" r="6"/>""",
    tray="""    <path class="s" stroke-width="2.5" d="M3.4,5.4 L11,17.6 L18.6,5.4"/>
    <circle class="st state" cx="11" cy="9" r="1.8"/>"""))

VB_W = 306

def wordmark(c):
    if c.get("split"):
        return ('  <text class="wm" x="92" y="51.5" font-family=%s font-size="32" '
                'letter-spacing="%s" xml:space="preserve"><tspan font-weight="400">Astra </tspan>'
                '<tspan font-weight="700">Voice</tspan></text>' % ('"%s"' % FONT, c["ls"]))
    return ('  <text class="wm" x="92" y="51.5" font-family=%s font-size="32" font-weight="%d" '
            'letter-spacing="%s">Astra Voice</text>' % ('"%s"' % FONT, c["w"], c["ls"]))

def logo_svg(c):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d 80" width="%d" height="80" '
            'role="img" aria-label="Astra Voice — концепт %s: %s">\n%s\n'
            '  <g class="mark" transform="translate(8,8)">\n%s\n  </g>\n%s\n</svg>\n'
            % (VB_W, VB_W, c["num"], c["title"], SVG_STYLE, c["mark"], wordmark(c)))

def tray_svg(c):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 22 22" width="22" height="22" '
            'role="img" aria-label="трей-иконка Astra Voice, концепт %s">\n%s\n%s\n</svg>\n'
            % (c["num"], TRAY_STYLE, c["tray"]))

for c in C:
    with open(os.path.join(OUT, c["fid"] + ".svg"), "w", encoding="utf-8") as f:
        f.write(logo_svg(c))
    with open(os.path.join(OUT, c["fid"] + "-tray.svg"), "w", encoding="utf-8") as f:
        f.write(tray_svg(c))

# ─────────────────────────────────────────────────────────────────────────────
#  Контактный лист
# ─────────────────────────────────────────────────────────────────────────────

def h_logo(c):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d 80" role="img" '
            'aria-label="Astra Voice — %s">%s%s</svg>' % (VB_W, c["title"], c["mark"], wordmark(c)))

def h_icon(c):
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64" aria-hidden="true">%s</svg>'
            % c["mark"])

def h_tray(c, state=None):
    st = ' style="--state:%s"' % state if state else ''
    return ('<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 22 22"%s aria-hidden="true">%s</svg>'
            % (st, c["tray"]))

STATES_DARK = [("покой", None), ("слушаю", "#2FD9C4"), ("распознаю", "#F2B559"),
               ("готово", "#4FBF88"), ("ошибка", "#F0645F")]
STATES_LIGHT = [("покой", None), ("слушаю", "#12B3A0"), ("распознаю", "#E8A33A"),
                ("готово", "#2FA36B"), ("ошибка", "#D64545")]

def card(c):
    badge = ('<span class="tag">из раунда 1</span>' if c["kind"] == "ref"
             else '<span class="tag new">новое</span>')
    p = []
    p.append('<section class="card%s">' % (' fresh' if c["kind"] == "new" else ''))
    p.append('  <div class="head">')
    p.append('    <span class="num">КОНЦЕПТ %s</span> %s' % (c["num"], badge))
    p.append('    <h2>%s<span class="sub">%s</span></h2>' % (c["title"], c["sub"]))
    p.append('    <p class="idea">%s</p>' % c["idea"])
    p.append('    <p class="note">%s</p>' % c["note"])
    p.append('  </div>')
    p.append('  <div class="duo">')
    for cls, cap in (("light", "светлый фон"), ("dark", "тёмный фон")):
        p.append('    <div class="pane %s">%s<span class="cap">%s</span></div>' % (cls, h_logo(c), cap))
    p.append('  </div>')
    p.append('  <div class="duo">')
    for cls, cap in (("mono", "один цвет — печать, ч/б"), ("inv", "фирменный фон, выворотка")):
        p.append('    <div class="pane %s">%s<span class="cap">%s</span></div>' % (cls, h_logo(c), cap))
    p.append('  </div>')
    p.append('  <div class="strip">')
    # иконка приложения — светлая
    ic = h_icon(c)
    p.append('    <div><span class="tile lt t128">%s</span><span class="tile lt t48">%s</span>'
             '<span class="tile lt t24">%s</span><span class="tile lt t16">%s</span>'
             '<span class="cap">иконка 128 / 48 / 24 / 16</span></div>' % (ic, ic, ic, ic))
    p.append('    <div><span class="tile dk t128">%s</span><span class="tile dk t48">%s</span>'
             '<span class="tile dk t24">%s</span><span class="tile dk t16">%s</span>'
             '<span class="cap">тёмная иконка</span></div>' % (ic, ic, ic, ic))
    p.append('  </div>')
    p.append('  <div class="strip two">')
    # трей на панелях KDE
    p.append('    <div><span class="panel kde-dark"><span class="dim">%s</span>%s'
             '<span class="dim">%s</span></span><br>'
             '<span class="panel kde-light" style="margin-top:10px"><span class="dim">%s</span>%s'
             '<span class="dim">%s</span></span>'
             '<span class="cap">трей KDE 22 px — тёмная и светлая панель</span></div>'
             % (KDE_NEIGHBOUR, h_tray(c, "#2FD9C4"), KDE_NEIGHBOUR2,
                KDE_NEIGHBOUR, h_tray(c, "#12B3A0"), KDE_NEIGHBOUR2))
    p.append('    <div>')
    for label, tone in (("на тёмной панели", "on-dark"), ("на светлой панели", "on-light")):
        sts = STATES_DARK if tone == "on-dark" else STATES_LIGHT
        p.append('      <div class="states %s">' % tone)
        for name, col in sts:
            p.append('        <span class="one"><span class="box">%s</span>'
                     '<span class="st-label">%s</span></span>' % (h_tray(c, col), name))
        p.append('      </div>')
    p.append('      <span class="cap">пять состояний — «слушаю» бирюзовый</span>')
    p.append('    </div>')
    p.append('  </div>')
    p.append('</section>')
    return "\n".join(p)

# соседи по панели, чтобы оценить вес иконки среди системных
KDE_NEIGHBOUR = ('<svg viewBox="0 0 22 22" aria-hidden="true"><path class="s" stroke-width="1.6" '
                 'd="M4,7 H18 M4,11 H18 M4,15 H13"/></svg>')
KDE_NEIGHBOUR2 = ('<svg viewBox="0 0 22 22" aria-hidden="true"><path class="s" stroke-width="1.6" '
                  'd="M11,4.5 A6.5,6.5 0 1 1 10.99,4.5"/><path class="s" stroke-width="1.6" '
                  'd="M11,7.5 V11 L13.5,12.8"/></svg>')

CSS = open(os.path.join(OUT, "concepts.html"), encoding="utf-8").read()
CSS = CSS[CSS.index("<style>") + 7: CSS.index("</style>")]
CSS += """
/* ── раунд 2 ── */
.inkf{fill:currentColor;stroke:none}
.strip{grid-template-columns:1fr 1fr}
.strip.two{grid-template-columns:320px 1fr}
.states{gap:10px}
.states .one{min-width:54px}
.card.fresh{border-color:#9FE0D6;box-shadow:0 1px 2px rgba(14,23,41,.05),0 0 0 3px var(--a-bg)}
.card > .head h2{display:inline-block;margin-left:10px}
.tag.new{background:var(--a-ink)}
.tag{background:var(--n500)}
.panel{vertical-align:middle}
.states{margin-bottom:14px}
.decided{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:14px;margin:18px 0 0}
.decided > div{background:#fff;border:1px solid var(--n200);border-left:3px solid var(--a);
               border-radius:10px;padding:14px 16px}
.decided b{display:block;font-size:13px;margin-bottom:3px}
.decided span{color:var(--n500);font-size:13px}
.rec-box{background:var(--a-bg);border:1px solid #9FE0D6;border-radius:12px;padding:18px 22px;margin:22px 0 0}
.rec-box p{margin:0 0 8px} .rec-box p:last-child{margin:0}
ol.q{padding-left:20px;max-width:80ch} ol.q li{margin-bottom:10px}
"""

doc = []
doc.append('<!doctype html>')
doc.append('<html lang="ru"><head><meta charset="utf-8">')
doc.append('<meta name="viewport" content="width=device-width,initial-scale=1">')
doc.append('<title>Astra Voice — знак, раунд 2</title>')
doc.append('<style>%s</style></head>' % CSS)
doc.append('<body><div class="wrap">')
doc.append('<p class="eyebrow">astra-voice · шаг G2a · раунд 2 · контактный лист</p>')
doc.append('<h1>Astra Voice — четыре новых направления знака</h1>')
doc.append('<p class="lead">Имя и фирменный цвет приняты, остался знак. Ниже сначала два '
           'направления из раунда 1 («более-менее») — уже с новым именем и с уточнённой '
           'вариацией каждого; затем четыре принципиально новых идеи. Выбрать нужно '
           '<b>одно направление</b>; вариацию внутри направления (01/01b, 03/03b) можно выбрать '
           'отдельно.</p>')
doc.append('<div class="decided">')
doc.append('<div><b>Имя — Astra Voice</b><span>Латиницей. Товарный риск по марке «Астра» принят '
           'заказчиком.</span></div>')
doc.append('<div><b>«Слушаю» — бирюза #12B3A0</b><span>Фирменный сигнальный цвет. '
           'Янтарь — «распознаю», красный остаётся за ошибкой.</span></div>')
doc.append('<div><b>Шрифты — PT Root UI + PT Mono</b><span>Оба уже стоят в ALSE 1.8, '
           'тянуть в пакет не нужно.</span></div>')
doc.append('</div>')
doc.append('<p class="meta">2026-09-07 · brand-designer · раунд 2 из 2 по лимиту правок</p>')
doc.append('<hr>')

doc.append('<p class="eyebrow">A · Точка отсчёта — то, что уже понравилось</p>')
doc.append('<h2>01 Ёлочки и 03 Клавиша с новым именем</h2>')
doc.append('<p class="lead" style="margin:8px 0 22px">Форма прежняя, текстовая часть — '
           '«Astra Voice». Рядом с каждым — уточнённая вариация (01b, 03b): то же самое, но '
           'вычищенное под маленькие размеры.</p>')
for c in C:
    if c["kind"] == "ref":
        doc.append(card(c))

doc.append('<hr>')
doc.append('<p class="eyebrow">B · Четыре новых направления</p>')
doc.append('<h2>Абзац · Слоги · Пилюля · Voice V</h2>')
doc.append('<p class="lead" style="margin:8px 0 22px">Что учтено из отклика на раунд 1: сохранены '
           'типографская «своя» линия (05) и логика «форма-контейнер + индикатор состояния внутри» '
           '(05, 07); звуковая волна и курсор как метафоры больше не используются. '
           'Пятое предложенное направление — «эфир/сигнал в квадрате» — я не рисовал: любые дуги '
           'сигнала читаются как та же звуковая волна, которая не зашла.</p>')
for c in C:
    if c["kind"] == "new":
        doc.append(card(c))

doc.append('<hr>')
doc.append('<p class="eyebrow">C · Рекомендация и вопросы</p>')
doc.append('<h2>Что бы выбрал я</h2>')
doc.append('<div class="rec-box">')
doc.append('<p><b>05 «Абзац» (¶)</b> — первый выбор. Он делает ровно то, за что зацепились '
           'ёлочки (типографский, «свой», из письменных символов), но силуэт цельный, а не '
           'набор штрихов: ёлочки в 16 px рассыпаются, ¶ — нет. И внутри у него готовое место '
           'под индикатор состояния — то, за что зацепилась клавиша. Чем платим: в 16 px ¶ — '
           'плотное пятно с двумя ножками, менее чёткое, чем клавиша.</p>')
doc.append('<p><b>03b «Клавиша»</b> — второй выбор и самый безопасный: честный жест продукта, '
           'лучшая читаемость в трее из всех восьми, одна и та же форма от 128 до 16 px. '
           'Если спор затянется — берите его, он точно не подведёт.</p>')
doc.append('<p><b>08 «Voice V»</b> — если хочется, чтобы знак был буквой имени. Перед выбором '
           'нужна проверка на конфликт с системными иконками KDE (шеврон — ходовая форма).</p>')
doc.append('</div>')
doc.append('<h2 style="margin-top:34px">Вопросы, на которые нужен ответ</h2>')
doc.append('<ol class="q">')
doc.append('<li><b>Одно направление из восьми.</b> Если ни одно не «то» — скажите, какое ближе '
           'всего и что именно в нём мешает: лимит раундов исчерпан, дальше идёт доводка выбранного, '
           'а не новый поиск.</li>')
doc.append('<li><b>01 или 01b, 03 или 03b</b> — если выбор падёт на них.</li>')
doc.append('<li><b>Знак ¶ (05): не читается ли он как «непечатаемые символы» из Word?</b> '
           'Стоит показать двум-трём будущим пользователям без объяснений и спросить, что это.</li>')
doc.append('<li><b>Текстовая часть.</b> Сейчас везде «Astra Voice» одним начертанием (в 08 — '
           '«Astra» тонким, «Voice» жирным). Нужен ли вариант с кириллической подписью '
           '«Астра Войс» / «голосовой ввод» второй строкой для документов и госзакупок?</li>')
doc.append('</ol>')
doc.append('<hr>')
doc.append('<p class="eyebrow">D · База айдентики (без изменений)</p>')
doc.append('<h2>Фирменные цвета</h2>')
doc.append('<div class="sw" style="margin-top:16px">')
for name, hexv, fg in (("brand.ink", "#0E1729", "#fff"), ("brand.primary", "#1B3A73", "#fff"),
                       ("brand.accent «слушаю»", "#12B3A0", "#08201C"),
                       ("accent.ink (текст)", "#0B7F71", "#fff"),
                       ("accent.bg", "#E3F7F4", "#0E1729"),
                       ("распознаю", "#E8A33A", "#2A1B02"),
                       ("готово", "#2FA36B", "#fff"), ("ошибка", "#D64545", "#fff"),
                       ("тёмная тема bg", "#0B1220", "#F2F5FA"),
                       ("тёмная тема accent", "#2FD9C4", "#08201C")):
    doc.append('<div style="background:%s;color:%s;border-color:%s"><b>%s</b><span>%s</span></div>'
               % (hexv, fg, hexv, html.escape(name), hexv))
doc.append('</div>')
doc.append('<h2 style="margin-top:34px">PT Root UI + PT Mono</h2>')
doc.append('<div class="type-demo" style="margin-top:16px">')
doc.append('<div style="font-size:28px;font-weight:700;line-height:1.2">Astra Voice — голосовой ввод</div>')
doc.append('<div style="font-size:20px;font-weight:700;line-height:1.25;margin-top:14px">'
           'Зажмите клавишу и говорите</div>')
doc.append('<div style="font-size:14px;line-height:1.5;margin-top:10px;max-width:66ch">'
           'Текст появляется там, где стоит курсор — в письме, документе или чате. '
           'Ничего не уходит в сеть: распознавание идёт на вашем процессоре. '
           'Ёлки, ёж, всё с «ё» на месте.</div>')
doc.append('<div style="font-size:12px;color:var(--n500);margin-top:12px">'
           'GigaAM v3 · ≈415 МБ ОЗУ · <span style="font-family:var(--mono)">Ctrl + Shift + Space</span></div>')
doc.append('</div>')
doc.append('<p class="meta">design/brand/ — concept-1, 1b, 3, 3b, 5, 6, 7, 8 (+ *-tray) · brand-basics.md</p>')
doc.append('</div></body></html>')

with open(os.path.join(OUT, "concepts-r2.html"), "w", encoding="utf-8") as f:
    f.write("\n".join(doc))

print("ok:", len(C), "концептов")
