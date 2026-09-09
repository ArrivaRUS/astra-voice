#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Контактный лист финального лого-пака → logo-pack.html (самодостаточный).

Запускать ПОСЛЕ _build-logo-pack.py:  python3 _build-pack-sheet.py
"""
import base64
import importlib.util
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("b", os.path.join(HERE, "_build-logo-pack.py"))
B = importlib.util.module_from_spec(spec)
spec.loader.exec_module(B)
ICONS = B.ICONS


def raw(p):
    return open(os.path.join(HERE, p), encoding="utf-8").read()


def inline(p):
    """SVG для вставки в HTML: внутренние <style> вырезаем — иначе их правила
    (.ColorScheme-Text, .st, svg{color}) утекают на весь документ."""
    s = raw(p)
    s = re.sub(r"<style[^>]*>.*?</style>", "", s, flags=re.S)
    s = s.replace(' id="current-color-scheme"', "")
    return s


def sized(p, px):
    s = inline(p)
    return re.sub(r'width="[^"]*" height="[^"]*"', 'width="%s" height="%s"' % (px, px), s, count=1)


def b64png(p):
    with open(os.path.join(HERE, p), "rb") as fh:
        return "data:image/png;base64," + base64.b64encode(fh.read()).decode()


# --- эталоны KDE, перерисованные по геометрии Breeze (не копия файлов) -------
# view-more-horizontal-symbolic / content-loading-symbolic: три квадрата 2×2,
# шаг 4, y=7 — «ещё» и «загрузка» в Breeze рисуются ОДНИМ И ТЕМ ЖЕ глифом.
REF16_MORE = ('<svg viewBox="0 0 16 16" width="16" height="16">'
              '<path fill="currentColor" d="M3 7h2v2H3zM7 7h2v2H7zM11 7h2v2h-2z"/></svg>')
REF16_LOAD = REF16_MORE
REF22_MORE = ('<svg viewBox="0 0 22 22" width="22" height="22">'
              '<path fill="currentColor" d="M10 5h2v2h-2zM10 10h2v2h-2zM10 15h2v2h-2z"/></svg>')
REF22_LOAD = ('<svg viewBox="0 0 22 22" width="22" height="22">'
              '<path fill="currentColor" d="M11 4a7 7 0 1 0 7 7h-2a5 5 0 1 1-5-5z"/></svg>')
REF_KBD = ('<svg viewBox="0 0 22 22" width="%s" height="%s"><g fill="currentColor">'
           '<path d="M2 6h18v11H2z" fill="none" stroke="currentColor" stroke-width="1.6"/>'
           '<path d="M5 9h2v1.6H5zm3 0h2v1.6H8zm3 0h2v1.6h-2zm3 0h3v1.6h-3z'
           'M5 12h3v1.6H5zm4 0h2v1.6H9zm3 0h4v1.6h-4zM7 14.6h8v1.4H7z"/></g></svg>')

CSS = """
:root{--bg:#f7f9fc;--fg:#0e1729;--mut:#5a6884;--line:#d7dee9;--card:#fff;
      --ink:#0E1729;--acc:#12B3A0;--accd:#2FD9C4;--tile:#1B3A73}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
  font:14px/1.55 'PT Root UI','PT Astra Sans','Open Sans',Roboto,system-ui,sans-serif}
.wrap{max-width:1120px;margin:0 auto;padding:40px 28px 80px}
h1{font-size:30px;margin:0 0 4px;letter-spacing:.2px}
h2{font-size:19px;margin:44px 0 4px;padding-top:20px;border-top:1px solid var(--line)}
h3{font-size:14px;margin:22px 0 8px;color:var(--mut);font-weight:500;
   text-transform:uppercase;letter-spacing:.7px}
p{margin:6px 0 0;max-width:78ch}
.lead{color:var(--mut)}
code{font:12.5px/1.4 'PT Mono','DejaVu Sans Mono',monospace;background:#eef2f8;
     padding:1px 5px;border-radius:4px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;
      padding:22px;margin-top:14px}
.row{display:flex;flex-wrap:wrap;gap:14px;align-items:flex-end}
.cap{display:block;font-size:11.5px;color:var(--mut);margin-top:8px;
     font-family:'PT Mono','DejaVu Sans Mono',monospace}
.bgs{display:grid;grid-template-columns:1fr 1fr;gap:14px}
.bg{border-radius:12px;padding:30px 26px;border:1px solid var(--line)}
.bg svg{max-width:100%;height:auto;display:block}
.bg.w{background:#fff}.bg.l{background:#eef2f8}
.bg.d{background:#0b1220;border-color:#1e2a40}.bg.b{background:#1b3a73;border-color:#1b3a73}
.bg.d .cap,.bg.b .cap{color:#8fa0bd}
.tiles{display:flex;gap:18px;align-items:flex-end;flex-wrap:wrap}
.tiles figure{margin:0;text-align:center}
.tiles img{display:block;image-rendering:pixelated;margin:0 auto}
.panel{display:flex;align-items:center;gap:0;border-radius:8px;padding:6px 10px;
       font-size:0}
.panel>span{display:inline-flex;align-items:center;justify-content:center;
            width:34px;height:26px}
.panel.dk{background:#31363b;color:#eff0f1}
.panel.lt{background:#eff0f1;color:#232629;border:1px solid var(--line)}
.panel .ours{outline:1px dashed rgba(47,217,196,.65);outline-offset:-2px;border-radius:4px}
.zoom{transform-origin:left center}
.states{display:flex;gap:26px;flex-wrap:wrap}
.states figure{margin:0;text-align:center;font-size:11.5px;color:var(--mut)}
.states .sw{background:#31363b;border-radius:8px;padding:10px 12px;display:block}
.states .sw2{background:#eff0f1;border:1px solid var(--line);border-radius:8px;
             padding:10px 12px;display:block;margin-top:6px;color:#232629}
table{border-collapse:collapse;margin-top:12px;font-size:13px;width:100%}
th,td{border-bottom:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}
th{color:var(--mut);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.5px}
td code{white-space:nowrap}
.no{display:grid;grid-template-columns:repeat(auto-fill,minmax(180px,1fr));gap:14px}
.no figure{margin:0;background:#fff;border:1px solid var(--line);border-radius:10px;
           padding:14px;text-align:center;position:relative;overflow:hidden}
.no figure::after{content:"";position:absolute;inset:0;
  background:linear-gradient(to top right,transparent calc(50% - 1px),#d64545 50%,transparent calc(50% + 1px));
  opacity:.55;pointer-events:none}
.no figcaption{font-size:11.5px;color:var(--mut);margin-top:8px;position:relative;z-index:1}
.no .box{height:56px;display:flex;align-items:center;justify-content:center}
.no .box svg{max-height:56px}\n.clr .in svg{display:block}
.tab{display:inline-flex;align-items:center;gap:8px;background:#dfe3ea;
     border-radius:9px 9px 0 0;padding:8px 16px 8px 12px;font-size:12.5px;color:#3a4457}
.clr{position:relative;display:inline-block;padding:26px;background:
  repeating-linear-gradient(45deg,#eef2f8 0 6px,#e3e9f2 6px 12px);border-radius:10px}
.clr .in{background:#fff;padding:0;display:block;outline:1px dashed #12B3A0}
.sw{display:inline-block}
.chips{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px}
.chip{font:12px/1 'PT Mono',monospace;border:1px solid var(--line);border-radius:20px;
      padding:6px 11px;background:#fff;display:inline-flex;gap:7px;align-items:center}
.chip i{width:11px;height:11px;border-radius:50%;display:inline-block}
"""


def panel(cls, items, zoom=1):
    ins = "".join('<span class="%s">%s</span>' % (c, s) for c, s in items)
    z = ' style="zoom:%s"' % zoom if zoom != 1 else ""
    return '<div class="panel %s"%s>%s</div>' % (cls, z, ins)


def logo_at(f_, wpx):
    """Замок заданной ширины: высоту считаем сами, а не отдаём на откуп браузеру
    (значение auto в SVG-атрибуте невалидно и Chromium раздувает картинку)."""
    h = round(wpx * B.LOCK_H / B.LOCK_W, 2)
    return re.sub(r'width="[^"]*" height="[^"]*"',
                  'width="%s" height="%s"' % (wpx, h), inline(f_), count=1)


def tray(size, state):
    return inline("icons/hicolor/%dx%d/status/astra-voice-tray-%s.svg" % (size, size, state))


def main():
    ic_png = {s: b64png("icons/hicolor/%dx%d/apps/astra-voice.png" % (s, s))
              for s in (16, 22, 24, 32, 48, 64, 128, 256, 512)}
    m, MW, MH = B.master()
    bx, by, bw, bh, _ = m["bar"]
    g1 = (m["dots"][1][0] - m["dots"][1][2]) - (m["dots"][0][0] + m["dots"][0][2])
    g2 = (m["dots"][2][0] - m["dots"][2][2]) - (m["dots"][1][0] + m["dots"][1][2])

    # схема пропорций поверх знака
    sc = []
    for i, (cx, cy, r) in enumerate(m["dots"]):
        sc.append('<circle cx="%s" cy="%s" r="%s" fill="%s"/>'
                  % (B.f(cx), B.f(cy), B.f(r), B.ACCENT if i == 2 else B.INK))
    sc.append('<rect x="0" y="%s" width="100" height="20" rx="10" fill="%s"/>'
              % (B.f(by), B.INK))
    ann = ('<g fill="none" stroke="#12B3A0" stroke-width="0.7" stroke-dasharray="2 2">'
           '<path d="M%s 2 V%s"/><path d="M%s 2 V%s"/><path d="M%s 2 V%s"/><path d="M%s 2 V%s"/>'
           '</g>' % (B.f(m["dots"][0][0] + m["dots"][0][2]), B.f(MH),
                     B.f(m["dots"][1][0] - m["dots"][1][2]), B.f(MH),
                     B.f(m["dots"][1][0] + m["dots"][1][2]), B.f(MH),
                     B.f(m["dots"][2][0] - m["dots"][2][2]), B.f(MH)))
    scheme = ('<svg viewBox="-2 -2 108 %s" width="440">%s%s'
              '<text x="%s" y="%s" font-size="5" fill="#0B7F71" text-anchor="middle"'
              ' font-family="monospace">%.3g</text>'
              '<text x="%s" y="%s" font-size="5" fill="#0B7F71" text-anchor="middle"'
              ' font-family="monospace">%.3g</text></svg>'
              % (B.f(MH + 12), "".join(sc), ann,
                 B.f(m["dots"][0][0] + m["dots"][0][2] + g1 / 2), B.f(MH + 8), g1,
                 B.f(m["dots"][1][0] + m["dots"][1][2] + g2 / 2), B.f(MH + 8), g2))

    ST = B.STATES
    h = []
    a = h.append
    a("<!-- сгенерировано _build-pack-sheet.py, правки вносить туда -->")
    a("<title>Astra Voice — лого-пак</title>")
    a("<style>%s</style>" % CSS)
    a('<div class="wrap">')
    a("<h1>Astra Voice — финальный лого-пак</h1>")
    a('<p class="lead">Знак <b>06 «Слоги»</b>, доводка после ⛔ G2a. Три слога-точки '
      "нарастают и сходятся, ложась сплошной строкой текста. "
      "Дата: 2026-09-07 · brand-designer.</p>")

    # -------------------------------------------------- что изменила доводка
    a("<h2>Что изменила доводка</h2>")
    a('<div class="card"><table>'
      "<tr><th>Было (концепт)</th><th>Стало (финал)</th><th>Зачем</th></tr>"
      "<tr><td>три одинаковые точки, промежутки 21 и 17 (ритм 1,24 : 1)</td>"
      "<td>диаметры <b>1 : 1,38 : 1,90</b>, промежутки <b>2,83 : 1</b></td>"
      "<td>ряд перестаёт читаться как системное «⋯»: и размер, и шаг заметно "
      "неравномерны уже в 16 px</td></tr>"
      "<tr><td>все три точки бирюзовые</td><td>бирюзовая только <b>третья</b></td>"
      "<td>три одинаковых цветных кружка в ряд — это идиома «идёт загрузка/печатает». "
      "Один цветной элемент снимает эту читку и даёт единое правило цвета состояния</td></tr>"
      "<tr><td>строка 54 при ряде 49 (шире на 10 %)</td>"
      "<td>строка <b>шире ряда в 1,23 раза</b>, вынос вправо 17 %</td>"
      "<td>строка стала главным элементом и держит асимметрию — «текст продолжается»</td></tr>"
      "<tr><td>вертикальный зазор = толщине строки</td><td>зазор <b>0,30</b> толщины</td>"
      "<td>точки и строка читаются как один знак, а не как «точки отдельно, черта отдельно»</td></tr>"
      "<tr><td>16 px — уменьшенная копия</td><td>16 px — <b>своя геометрия</b> по пиксельной "
      "сетке (точки 2/3/4 px, шаг 2 и 1, строка 14×4)</td>"
      "<td>производные радиусы дают 1,9 px и точки мылятся в серую грязь</td></tr>"
      "</table></div>")

    # ------------------------------------------------------------ замок
    a("<h2>Замок: знак + имя</h2>")
    a('<p class="lead">Имя набрано PT&nbsp;Root&nbsp;UI Medium и <b>переведено в кривые</b> — '
      "на машине без этого шрифта логотип не поедет. Общий bbox "
      "<code>%.4g × %.4g</code>, знак <code>%.4g × %.4g</code>, просвет <code>%.4g</code>.</p>"
      % (B.LOCK_W, B.LOCK_H, B.MARK_W, B.MARK_H_IN_LOCKUP, B.LOCKUP_GAP))
    a('<div class="bgs">')
    for cls, name, f_ in (("w", "белый #FFFFFF", "logo.svg"),
                          ("l", "светлый #EEF2F8", "logo.svg"),
                          ("d", "тёмный #0B1220", "logo-dark.svg"),
                          ("b", "фирменный #1B3A73", "logo-dark.svg")):
        a('<div class="bg %s">%s<span class="cap">%s — %s</span></div>'
          % (cls, inline(f_), name, f_))
    a("</div>")
    a('<div class="card"><h3>Одноцветная версия (<code>logo-mono.svg</code>, currentColor)</h3>'
      '<div class="row" style="align-items:center">'
      '<div style="color:#0E1729;flex:1 1 260px">%s</div>'
      '<div style="color:#5A6884;flex:1 1 260px">%s</div>'
      '<div style="background:#0b1220;color:#F2F5FA;padding:14px;border-radius:10px;'
      'flex:1 1 260px">%s</div></div></div>'
      % tuple(logo_at("logo-mono.svg", 250) for _ in range(3)))

    # ------------------------------------------------------------ геометрия
    a("<h2>Геометрия знака</h2>")
    a('<div class="card"><div class="row" style="align-items:center;gap:30px">')
    a("<div>%s</div>" % scheme)
    a("<div style='flex:1 1 320px'><table>"
      "<tr><th>параметр</th><th>значение</th></tr>"
      "<tr><td>мастер-сетка (bbox знака)</td><td><code>0 0 %.4g %.4g</code> — %.3f : 1</td></tr>"
      "<tr><td>строка</td><td>длина 100, толщина <b>20</b> (20 %%), торцы круглые</td></tr>"
      "<tr><td>диаметры точек</td><td>13,54 · 18,68 · 25,72 → <b>1 : 1,38 : 1,90</b></td></tr>"
      "<tr><td>промежутки</td><td>%.4g и %.4g → <b>2,83 : 1</b></td></tr>"
      "<tr><td>ряд точек</td><td>81,3 — строка шире в <b>1,23</b> раза, вынос вправо 17,3 %%</td></tr>"
      "<tr><td>зазор точка&nbsp;3 → строка</td><td>6 = <b>0,30</b> толщины строки</td></tr>"
      "<tr><td>цвет состояния несёт</td><td><b>третья (самая крупная) точка</b> — на всех размерах</td></tr>"
      "</table></div>" % (MW, MH, MW / MH, g1, g2))
    a("</div></div>")

    # ------------------------------------------------------------ иконка
    a("<h2>Иконка приложения (hicolor/apps)</h2>")
    a('<p class="lead">Плашка <code>#1B3A73</code>, радиус 22 % стороны, знак светлый '
      "<code>#F2F5FA</code> с акцентом <code>#2FD9C4</code>. Ряд точек прижат влево и "
      "заметно уже строки — иначе «три круга над горизонталью» в квадрате читаются как лицо. "
      "16 px нарисован отдельно, а не уменьшен.</p>")
    a('<div class="card"><div class="tiles">')
    for s in (512, 256, 128, 64, 48, 32, 24, 22, 16):
        show = min(s, 128)
        a('<figure><img src="%s" width="%d" height="%d" alt=""><span class="cap">%d</span></figure>'
          % (ic_png[s], show, show, s))
    a("</div></div>")
    a('<div class="card"><h3>Как выглядит в списке (реальный размер)</h3>'
      '<div class="row" style="align-items:center;gap:22px">')
    for bgc, lab in (("#eff0f1", "светлое меню"), ("#31363b", "тёмное меню")):
        row = "".join('<img src="%s" width="%d" height="%d" style="margin-right:14px" alt="">'
                      % (ic_png[s], s, s) for s in (16, 22, 24, 32, 48))
        a('<div style="background:%s;border-radius:8px;padding:12px 14px;display:flex;'
          'align-items:center">%s</div><span class="cap">%s</span>' % (bgc, row, lab))
    a("</div></div>")

    # ------------------------------------------------------------ трей
    a("<h2>Трей: рядом с системными иконками</h2>")
    a('<p class="lead">Главная задача доводки — чтобы знак не путался с Breeze-иконками '
      "«ещё» и «загрузка». В Breeze это <b>один и тот же глиф</b>: "
      "<code>view-more-horizontal-symbolic</code> и <code>content-loading-symbolic</code> — "
      "три одинаковых квадрата 2×2 с равным шагом 4. Наш знак отличается тремя признаками "
      "сразу: неравные точки, неравные промежутки и тяжёлая строка под ними.</p>")
    for cls, lab in (("dk", "тёмная панель (#31363b)"), ("lt", "светлая панель (#EFF0F1)")):
        a('<div class="card"><h3>%s</h3>' % lab)
        for size, more, load in ((22, REF22_MORE, REF22_LOAD), (16, REF16_MORE, REF16_LOAD)):
            items = [("ours", tray(size, "listening")), ("", more), ("", load),
                     ("", REF_KBD % (size, size))]
            a('<div class="row" style="align-items:center;gap:26px">')
            a(panel(cls, items))
            a('<div class="zoom" style="zoom:4">%s</div>' % panel(cls, items))
            a('<span class="cap">%d px · наш · «ещё» · «загрузка» · клавиатура</span>' % size)
            a("</div>")
        a("</div>")

    # ------------------------------------------------------------ состояния
    a("<h2>Состояния: форма одна, цвет несёт третья точка</h2>")
    a('<p class="lead">Файлы <code>hicolor/{22x22,16x16}/status/astra-voice-tray-*.svg</code> — '
      "<b>шесть состояний</b>. Строка и первые две точки — всегда <code>currentColor</code> "
      "(цвет темы панели), меняется только заливка третьей точки. У <code>idle</code> цвета нет "
      "вовсе. Исключение одно — <code>nokey</code> («горячая клавиша не захвачена»): там третья "
      "точка становится <b>кольцом</b> того же наружного диаметра, потому что состояние нужно "
      "отличать и от покоя, и от ошибки, а свободного цвета для него нет. Приглушать кольцо "
      "нельзя: <code>opacity .55–.7</code> в 16 px пропадает на светлой панели (проверено "
      "рендером rsvg-convert). "
      "Для монохромных панелей есть <code>astra-voice-tray-mono.svg</code> — "
      "форма без цвета; состояние там передают подсказка и оверлей, не иконка.</p>")
    a('<div class="card"><div class="states">')
    for name, lt, dk, ru in ST:
        a('<figure><span class="sw" style="color:#eff0f1">%s%s</span>'
          '<span class="sw2" style="color:#232629">%s%s</span>'
          "<figcaption><b>%s</b><br>%s<br>%s</figcaption></figure>"
          % ('<span style="zoom:2.4;display:inline-block">%s</span>' % tray(22, name),
             '<span style="zoom:2.4;display:inline-block;margin-left:10px">%s</span>' % tray(16, name),
             '<span style="zoom:2.4;display:inline-block">%s</span>' % tray(22, name),
             '<span style="zoom:2.4;display:inline-block;margin-left:10px">%s</span>' % tray(16, name),
             ru, name,
             ("кольцо · currentColor" if name in B.RING_STATES else (lt or "currentColor"))))
    a("</div>")
    a('<div class="chips">')
    for name, lt, dk, ru in ST:
        # у nokey цвета нет — в легенде тоже кольцо, а не диск
        sw = ('border:2px solid #5A6884;background:transparent'
              if name in B.RING_STATES else "background:%s" % (lt or "#5A6884"))
        a('<span class="chip"><i style="%s"></i>%s · %s / %s</span>'
          % (sw, ru, "кольцо" if name in B.RING_STATES else (lt or "currentColor"),
             "кольцо" if name in B.RING_STATES else (dk or "currentColor")))
    a("</div></div>")

    # ------------------------------------------------------------ фавикон
    a("<h2>Фавикон и README</h2>")
    a('<div class="card"><div class="row" style="align-items:center;gap:30px">')
    a('<div><div class="tab">%s Astra Voice — офлайн-диктовка</div>'
      '<span class="cap">favicon.svg в реальном размере вкладки</span></div>'
      % '<img src="%s" width="16" height="16" alt="">' % ic_png[16])
    a('<div><img src="%s" width="32" height="32" style="image-rendering:pixelated">'
      '<span class="cap">favicon-32.png</span></div>' % b64png("favicon-32.png"))
    a("</div>")
    def rmark(acc):
        # <style> вырезан, поэтому акцент задаём атрибутом — иначе он унаследует
        # currentColor и третья точка потеряет цвет (в самом файле всё на месте)
        t = inline("readme-mark.svg").replace('class="acc" ', "")
        t = t.replace('width="', 'style="max-width:420px" width="', 1)
        parts = t.rsplit('fill="currentColor"/><rect', 1)
        return ('fill="%s"/><rect' % acc).join(parts) if len(parts) == 2 else t
    a('<h3>readme-mark.svg — шапка README (свой охранный отступ, тема через '
      "<code>prefers-color-scheme</code>)</h3>"
      '<div class="bgs"><div class="bg w">%s</div><div class="bg d" style="color:#F2F5FA">%s</div></div>'
      % (rmark(B.ACCENT), rmark(B.ACCENT_DARK)))
    a("</div>")

    # ------------------------------------------------------------ правила
    a("<h2>Правила использования</h2>")
    a('<div class="card"><h3>Охранное поле</h3>'
      "<p>Со всех сторон свободно не меньше <b>25 %% высоты знака</b> "
      "(в мастер-сетке — 13 единиц). В <code>logo.svg</code> поле не заложено: bbox плотный, "
      "отступы задаёт тот, кто ставит логотип.</p>"
      '<div class="clr"><span class="in">%s</span></div></div>'
      % logo_at("logo.svg", 340))
    a('<div class="card"><h3>Минимальные размеры</h3><table>'
      "<tr><th>что</th><th>минимум</th><th>ниже — брать</th></tr>"
      "<tr><td>замок (знак + имя)</td><td><b>130 px</b> по ширине</td><td>только знак</td></tr>"
      "<tr><td>знак в цвете</td><td><b>16 px</b> по высоте</td><td>—</td></tr>"
      "<tr><td>иконка приложения</td><td><b>16 px</b></td>"
      "<td>16 px — отдельный файл, не уменьшать 22-й</td></tr>"
      "<tr><td>трей</td><td><b>16 px</b></td><td>—</td></tr></table></div>")
    a('<div class="card"><h3>Нельзя</h3><div class="no">')
    lg = logo_at("logo.svg", 150)
    for style, cap in (
            ('transform:scaleX(1.35)', "растягивать и менять пропорции"),
            ('filter:drop-shadow(2px 3px 3px rgba(0,0,0,.45))', "добавлять тени и обводки"),
            ('filter:hue-rotate(150deg) saturate(2)', "перекрашивать знак"),
            ('opacity:.5', "ставить полупрозрачным"),
    ):
        a('<figure><div class="box"><span style="%s;display:inline-block">%s</span></div>'
          "<figcaption>%s</figcaption></figure>" % (style, lg, cap))
    a('<figure><div class="box" style="background:'
      "repeating-conic-gradient(#9db4d8 0 25%%,#c8d6ea 0 50%%);background-size:16px 16px;"
      'border-radius:6px">%s</div><figcaption>класть на пёстрый фон</figcaption></figure>' % lg)
    a('<figure><div class="box">%s</div>'
      "<figcaption>красить знак в цвета состояний — это привилегия трей-иконки</figcaption></figure>"
      % logo_at("logo.svg", 150).replace('fill="#12B3A0"', 'fill="#D64545"'))
    a("</div>")
    a("<p>Плюс к этому: не ставить в один замок с логотипом Astra Linux, не использовать "
      "марку ГК «Астра» в любом виде, не наклонять, не заменять шрифт имени "
      "(имя — в кривых, это и есть логотип).</p></div>")

    # ------------------------------------------------------------ файлы
    a("<h2>Состав пака</h2>")
    a('<div class="card"><table><tr><th>файл</th><th>что это</th></tr>')
    for p, d in (
        ("logo.svg", "замок для светлого фона, имя в кривых"),
        ("logo-dark.svg", "замок для тёмного фона"),
        ("logo-mono.svg", "замок в один цвет, всё currentColor"),
        ("logo-mark.svg", "знак отдельно, мастер-сетка 100 × 51,72"),
        ("readme-mark.svg", "замок для шапки README, светлая/тёмная через prefers-color-scheme"),
        ("favicon.svg", "фавикон-вектор (геометрия 32 px)"),
        ("favicon-32.png", "фавикон-растр"),
        ("icons/hicolor/scalable/apps/astra-voice.svg", "иконка приложения, вектор"),
        ("icons/hicolor/&lt;size&gt;/apps/astra-voice.png",
         "16 · 22 · 24 · 32 · 48 · 64 · 128 · 256 · 512"),
        ("icons/hicolor/{16x16,22x22}/status/astra-voice-tray-&lt;state&gt;.svg",
         "трей: idle · listening · processing · done · error"),
        ("icons/hicolor/{16x16,22x22}/status/astra-voice-tray-mono.svg",
         "трей для монохромных панелей"),
        ("icons/_src/", "исходники вычищенных мелких размеров и кривые имени"),
        ("_build-logo-pack.py / _build-pack-sheet.py", "сборка пака и этого листа"),
    ):
        a("<tr><td><code>%s</code></td><td>%s</td></tr>" % (p, d))
    a("</table></div>")
    a("</div>")

    out = os.path.join(HERE, "logo-pack.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write("\n".join(h) + "\n")
    print(out, os.path.getsize(out), "байт")


if __name__ == "__main__":
    main()
