# -*- coding: utf-8 -*-
"""Общая база для генерации макетов Astra Voice (направления A/B/C).

Правки геометрии и компонентов делать ЗДЕСЬ, html-файлы генерируются:
    python3 _build.py
Каждый .html самодостаточен: без внешних ресурсов и скриптов, шрифты системные.
"""
import os

ROOT = os.path.dirname(os.path.abspath(__file__))

# ── палитра brand-basics §2 ────────────────────────────────────────────────
VARS_LIGHT = """
  --bg-app:#F7F9FC; --bg-surface:#FFFFFF; --bg-sunk:#EDF1F7; --bg-tbar:#E7ECF4;
  --border:#D7DEEA; --border-soft:#EDF1F7;
  --fg1:#0E1729; --fg2:#243350; --fg3:#5A6884; --fg4:#AAB4C7;
  --primary:#1B3A73; --primary-hover:#12294F; --primary-fg:#FFFFFF; --primary-bg:#E8EDF7;
  --accent:#12B3A0; --accent-ink:#0B7F71; --accent-bg:#E3F7F4;
  --ok-ink:#1F7D50; --ok-bg:#E7F5EE;
  --warn-ink:#8F5E12; --warn-bg:#FAF0DC;
  --err-ink:#C0322F; --err-bg:#FBECEB;
  --canvas:#DDE3EE; --canvas-fg:#3A4761; --shadow:rgba(14,23,41,.18);
"""
VARS_DARK = """
  --bg-app:#0B1220; --bg-surface:#151E30; --bg-sunk:#080D18; --bg-tbar:#101A2B;
  --border:rgba(255,255,255,.10); --border-soft:rgba(255,255,255,.06);
  --fg1:#F2F5FA; --fg2:#C4CDDC; --fg3:#8C97AC; --fg4:#5A6884;
  --primary:#6C93E8; --primary-hover:#8AACF0; --primary-fg:#0B1220; --primary-bg:rgba(108,147,232,.16);
  --accent:#2FD9C4; --accent-ink:#2FD9C4; --accent-bg:rgba(47,217,196,.13);
  --ok-ink:#4FBF88; --ok-bg:rgba(79,191,136,.13);
  --warn-ink:#F2B559; --warn-bg:rgba(242,181,89,.13);
  --err-ink:#F0645F; --err-bg:rgba(240,100,95,.13);
  --canvas:#05080F; --canvas-fg:#8C97AC; --shadow:rgba(0,0,0,.55);
"""

CSS = """
*{box-sizing:border-box}
body{margin:0;padding:26px 20px 34px;background:var(--canvas);color:var(--canvas-fg);
  font-family:var(--font-ui);font-size:14px;line-height:1.5;
  display:flex;flex-direction:column;align-items:center;gap:14px;-webkit-font-smoothing:antialiased}
.cap{font-size:13px;letter-spacing:.01em}
.cap b{color:var(--fg1);font-weight:700}
body.dark .cap b{color:#F2F5FA}
.legend{max-width:900px;font-size:12.5px;line-height:1.55;opacity:.95}
.legend b{font-weight:700}
.legend ul{margin:6px 0 0;padding-left:18px}
.mono{font-family:var(--font-mono);letter-spacing:.02em}

/* ── окно ───────────────────────────────────────────── */
.win{background:var(--bg-app);color:var(--fg1);border-radius:8px;overflow:hidden;
  box-shadow:0 14px 44px var(--shadow);display:flex;flex-direction:column;flex:none}
.tbar{height:32px;flex:none;display:flex;align-items:center;gap:8px;padding:0 10px;
  background:var(--bg-tbar);border-bottom:1px solid var(--border)}
.tbar .t{font-size:12.5px;color:var(--fg2);font-weight:500}
.tbar .sp{flex:1}
.wbtn{width:14px;height:14px;border-radius:3px;background:var(--border);display:inline-block}
.wbtn.c{background:#D96B62}
body.dark .wbtn.c{background:#8C3B36}

/* ── типографика ────────────────────────────────────── */
.h1{font-size:28px;line-height:1.2;font-weight:700;color:var(--fg1);margin:0}
.h2{font-size:20px;line-height:1.25;font-weight:700;color:var(--fg1);margin:0}
.h3{font-size:16px;line-height:1.3;font-weight:500;color:var(--fg1);margin:0}
.sm{font-size:13px;line-height:1.45;color:var(--fg3)}
.c12{font-size:12px;line-height:1.4;color:var(--fg3)}
.grp{font-size:11px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:var(--fg3);margin:0 0 7px 2px}
.muted{color:var(--fg3)}
.strong{font-weight:700;color:var(--fg1)}

/* ── карточки и строки настроек ─────────────────────── */
.card{background:var(--bg-surface);border:1px solid var(--border);border-radius:10px;overflow:hidden}
.card>.r+.r{border-top:1px solid var(--border-soft)}
.r{display:flex;align-items:center;gap:10px;padding:11px 14px;min-height:44px}
.r .lbl{font-size:14px;color:var(--fg1);font-weight:400}
.r .sub{font-size:12px;color:var(--fg3);margin-top:2px}
.r .left{flex:1;min-width:0}
.r.dis{opacity:.5}
.hint{width:15px;height:15px;flex:none;border-radius:50%;border:1px solid var(--fg4);color:var(--fg3);
  font-size:10px;line-height:13px;text-align:center;display:inline-block;font-weight:500}
.lockb{display:inline-flex;align-items:center;gap:4px;font-size:11.5px;color:var(--fg3);
  background:var(--bg-sunk);border-radius:5px;padding:2px 6px}

/* ── контролы ───────────────────────────────────────── */
.tgl{width:38px;height:21px;border-radius:11px;background:var(--fg4);position:relative;flex:none}
.tgl i{position:absolute;top:2px;left:2px;width:17px;height:17px;border-radius:50%;background:#fff;display:block}
.tgl.on{background:var(--primary)}
.tgl.on i{left:19px}
.tgl.lock{background:var(--border)}
.btn{font-family:var(--font-ui);font-size:13px;font-weight:500;color:var(--fg1);background:var(--bg-surface);
  border:1px solid var(--border);border-radius:7px;padding:6px 12px;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.btn.pri{background:var(--primary);border-color:var(--primary);color:var(--primary-fg)}
.btn.gh{background:transparent;border-color:transparent;color:var(--primary);padding:6px 8px}
.btn.dis{opacity:.45}
.btn.sm{font-size:12.5px;padding:4px 9px}
.sel{font-size:13px;color:var(--fg1);background:var(--bg-surface);border:1px solid var(--border);
  border-radius:7px;padding:6px 10px;display:inline-flex;align-items:center;gap:8px;white-space:nowrap}
.seg{display:inline-flex;border:1px solid var(--border);border-radius:7px;overflow:hidden;background:var(--bg-surface)}
.seg span{font-size:12.5px;padding:5px 11px;color:var(--fg2)}
.seg span.on{background:var(--primary);color:var(--primary-fg);font-weight:500}
.key{font-family:var(--font-mono);font-size:13px;color:var(--fg1);background:var(--bg-sunk);
  border-radius:6px;padding:3px 8px 2px;border-bottom:2px solid var(--border);display:inline-block;letter-spacing:.02em}
.field{font-size:13px;color:var(--fg3);background:var(--bg-surface);border:1px solid var(--border);
  border-radius:7px;padding:7px 11px;display:flex;align-items:center;gap:8px}
.field.cap{border-color:var(--primary);box-shadow:0 0 0 2px var(--primary-bg);color:var(--fg1)}
.chip{font-size:12.5px;color:var(--fg2);background:var(--bg-surface);border:1px solid var(--border);
  border-radius:14px;padding:4px 11px;display:inline-flex;align-items:center;gap:6px}
.chip.on{background:var(--primary);border-color:var(--primary);color:var(--primary-fg);font-weight:500}

/* ── бейджи и теги ──────────────────────────────────── */
.bd{font-size:11.5px;font-weight:500;border-radius:5px;padding:2px 7px;display:inline-flex;align-items:center;gap:4px;white-space:nowrap}
.bd.act{background:var(--accent-bg);color:var(--accent-ink)}
.bd.rec{background:var(--primary-bg);color:var(--primary)}
.bd.upd{background:var(--warn-bg);color:var(--warn-ink)}
.bd.new{background:var(--bg-sunk);color:var(--fg3)}
.bd.err{background:var(--err-bg);color:var(--err-ink)}
.tag{font-size:12px;color:var(--fg3);white-space:nowrap}
.dot{width:4px;height:4px;border-radius:50%;background:var(--fg4);display:inline-block;vertical-align:middle}

/* ── полоски метрик ─────────────────────────────────── */
.met{display:flex;align-items:center;gap:8px}
.met .mn{font-size:11.5px;color:var(--fg3);width:58px;text-align:right;flex:none}
.trk{width:88px;height:6px;border-radius:3px;background:var(--bg-sunk);overflow:hidden;flex:none}
.trk i{display:block;height:100%;border-radius:3px;background:var(--primary)}
.trk.est i{background:var(--fg4)}
.met .mv{font-size:11.5px;color:var(--fg3);white-space:nowrap}
.met .mv b{color:var(--fg1);font-weight:700}
.prog{height:6px;border-radius:3px;background:var(--bg-sunk);overflow:hidden}
.prog i{display:block;height:100%;background:var(--primary);border-radius:3px}

/* ── полосы-уведомления ─────────────────────────────── */
.note{display:flex;gap:9px;align-items:flex-start;border-radius:9px;padding:10px 12px;font-size:12.5px;line-height:1.45}
.note.i{background:var(--bg-sunk);color:var(--fg2)}
.note.w{background:var(--warn-bg);color:var(--warn-ink)}
.note.e{background:var(--err-bg);color:var(--err-ink)}
.note.o{background:var(--ok-bg);color:var(--ok-ink)}
.note b{display:block;font-weight:700;margin-bottom:2px}

/* ── нижняя строка окна ─────────────────────────────── */
.foot{height:36px;flex:none;display:flex;align-items:center;gap:10px;padding:0 14px;
  background:var(--bg-app);border-top:1px solid var(--border);font-size:12.5px;color:var(--fg3)}
.foot .sp{flex:1}
.foot .up{color:var(--primary);font-weight:500}
.foot .num{font-family:var(--font-mono);font-size:12px}

/* ── пилюля-оверлей ─────────────────────────────────── */
.desk{border-radius:8px;overflow:hidden;position:relative;box-shadow:0 14px 44px var(--shadow);flex:none;
  background:linear-gradient(160deg,#16233F 0%,#0E1729 55%,#1B3A73 100%)}
.dwin{position:absolute;background:#FFFFFF;border-radius:7px;box-shadow:0 8px 26px rgba(0,0,0,.35);overflow:hidden}
.dwin .dt{height:26px;background:#E7ECF4;border-bottom:1px solid #D7DEEA;display:flex;align-items:center;
  padding:0 9px;font-size:11.5px;color:#243350}
.dwin .dc{padding:12px 14px;font-size:12.5px;line-height:1.65;color:#0E1729}
.panel{position:absolute;left:0;right:0;bottom:0;height:40px;background:#0B1220;
  display:flex;align-items:center;gap:8px;padding:0 10px;border-top:1px solid rgba(255,255,255,.08)}
.panel .pb{font-size:11.5px;color:#C4CDDC;background:rgba(255,255,255,.07);border-radius:5px;padding:4px 9px}
.panel .sp{flex:1}
.pill{background:rgba(14,23,41,.94);border-radius:18px;height:36px;display:inline-flex;align-items:center;
  gap:9px;padding:0 10px;color:#F2F5FA;font-size:12.5px;box-shadow:0 6px 18px rgba(0,0,0,.4)}
.pill .lv{display:flex;align-items:flex-end;gap:3px;height:20px}
.pill .lv i{width:6px;border-radius:3px;background:#12B3A0;display:block}
.pill .lv.f i{background:#5A6884}
.pill .x{width:22px;height:22px;border-radius:50%;background:rgba(255,255,255,.12);display:inline-flex;
  align-items:center;justify-content:center;color:#C4CDDC;font-size:13px;line-height:1}
.pill .st{white-space:nowrap}
.pill .st b{font-weight:700}
.gal{display:flex;flex-wrap:wrap;gap:10px;align-items:center;justify-content:center;
  background:var(--bg-sunk);border:1px solid var(--border);border-radius:10px;padding:14px}
.gal .cell{display:flex;flex-direction:column;align-items:center;gap:6px}
.gal .cn{font-size:11px;color:var(--fg3)}

/* ── трей-меню ──────────────────────────────────────── */
.menu{background:var(--bg-surface);border:1px solid var(--border);border-radius:8px;padding:5px;
  box-shadow:0 12px 34px var(--shadow);width:262px}
.mi{display:flex;align-items:center;gap:9px;padding:6px 9px;border-radius:6px;font-size:13px;color:var(--fg1)}
.mi .sp{flex:1}
.mi .sc{font-family:var(--font-mono);font-size:11.5px;color:var(--fg3)}
.mi.dis{color:var(--fg4)}
.mi.hd{color:var(--fg3);font-size:12px;padding-top:3px;padding-bottom:3px}
.mi.hl{background:var(--primary);color:var(--primary-fg)}
.mi.hl .sc{color:rgba(255,255,255,.7)}
.msep{height:1px;background:var(--border-soft);margin:4px 6px}

/* ── прокрутка (визуальная, содержимое обрезано как в реальном окне) ── */
.scr{position:relative;overflow:hidden}
.sb{position:absolute;right:4px;width:5px;border-radius:3px;background:var(--fg4);opacity:.45}

/* ── карточка модели ────────────────────────────────── */
.mc{background:var(--bg-surface);border:1px solid var(--border);border-radius:10px;padding:10px 13px;margin-bottom:9px}
.mc.act{border-color:var(--accent);background:var(--accent-bg)}
.mc.rec{border-color:var(--primary)}
.mc.dis{opacity:.62}
.mtop{display:flex;gap:14px;align-items:flex-start}
.mtop .ml{flex:1;min-width:0}
.mname{font-size:15px;font-weight:500;color:var(--fg1)}
.mven{color:var(--fg3);font-weight:400;font-size:12.5px}
.mpurp{font-size:12.5px;color:var(--fg3);margin-top:2px}
.mbadges{display:flex;gap:5px;flex-wrap:wrap;margin-top:4px}
.mmets{display:flex;flex-direction:column;gap:5px;flex:none}
.mhr{height:1px;background:var(--border-soft);margin:9px 0 8px}
.mbot{display:flex;align-items:center;gap:9px;font-size:12px;color:var(--fg3);flex-wrap:wrap}
.mbot .sp{flex:1}
"""

FONTS = ("--font-ui:'PT Root UI','PT Astra Sans','Open Sans','Roboto',sans-serif;"
         "--font-mono:'PT Mono','DejaVu Sans Mono','Liberation Mono',monospace;")


def page(title, caption, body, theme="light", extra_css="", legend=""):
    v = VARS_DARK if theme == "dark" else VARS_LIGHT
    leg = ('<div class="legend">%s</div>' % legend) if legend else ""
    return (
        '<!doctype html>\n<html lang="ru"><head><meta charset="utf-8">\n'
        '<title>%s</title>\n<style>\n:root{%s%s}\n%s\n%s</style></head>\n'
        '<body class="%s">\n<div class="cap">%s</div>\n%s\n%s\n</body></html>\n'
        % (title, FONTS, v, CSS, extra_css, theme, caption, body, leg))


# ── графика бренда (инлайн SVG, внутренние <style> вырезаны, brand-basics §4.6) ──
def mark(w=20, ink="var(--fg1)", accent="var(--accent)"):
    h = round(w * 0.517238, 2)
    return ('<svg viewBox="0 0 100 51.7238" width="%s" height="%s" aria-hidden="true" style="flex:none">'
            '<circle cx="8.1233" cy="12.8619" r="6.7694" fill="%s"/>'
            '<circle cx="41.4966" cy="12.8619" r="9.3418" fill="%s"/>'
            '<circle cx="69.7928" cy="12.8619" r="12.8619" fill="%s"/>'
            '<rect x="0" y="31.7238" width="100" height="20" rx="10" fill="%s"/></svg>'
            % (w, h, ink, ink, accent, ink))


def appicon(size=48):
    return ('<svg viewBox="0 0 64 64" width="%d" height="%d" aria-hidden="true" style="flex:none">'
            '<rect width="64" height="64" rx="14.08" fill="#1B3A73"/>'
            '<circle cx="9.2776" cy="24.88" r="2.8776" fill="#F2F5FA"/>'
            '<circle cx="23.4642" cy="24.88" r="3.9711" fill="#F2F5FA"/>'
            '<circle cx="35.4926" cy="24.88" r="5.4674" fill="#2FD9C4"/>'
            '<rect x="5.12" y="32.7474" width="54.4" height="8" rx="4" fill="#F2F5FA"/></svg>' % (size, size))


TRAY_STATE = {"idle": "currentColor", "listening": "#12B3A0", "processing": "#E8A33A",
              "done": "#2FA36B", "error": "#D64545"}


def tray(state="idle", size=18, color="currentColor"):
    acc = TRAY_STATE[state]
    if state == "idle":
        acc = color
    return ('<svg viewBox="0 0 22 22" width="%d" height="%d" aria-hidden="true" style="flex:none;color:%s">'
            '<circle cx="3.4622" cy="8.4849" r="1.2185" fill="currentColor"/>'
            '<circle cx="9.4694" cy="8.4849" r="1.6815" fill="currentColor"/>'
            '<circle cx="14.5627" cy="8.4849" r="2.3151" fill="%s"/>'
            '<rect x="2" y="12" width="18" height="4" rx="2" fill="currentColor"/></svg>' % (size, size, color, acc))


def wordmark(size=15, color="var(--fg1)"):
    return ('<span style="font-size:%dpx;font-weight:500;letter-spacing:.03em;color:%s">Astra&nbsp;Voice</span>'
            % (size, color))


def logo(w=20, size=15, color="var(--fg1)"):
    return ('<span style="display:inline-flex;align-items:center;gap:8px">%s%s</span>'
            % (mark(w, color), wordmark(size, color)))


_IC = {
    "chev": 'M6.5 3.5L11 8l-4.5 4.5',
    "chevd": 'M3.5 6.5L8 11l4.5-4.5',
    "check": 'M3 8.4l3.4 3.3L13 4.6',
    "down": 'M8 2.8v7.4m0 0l-3-3m3 3l3-3M2.8 13.2h10.4',
    "folder": 'M2.2 4.4h4.2l1.3 1.7h6.1v7.5H2.2z',
    "refresh": 'M13.2 8a5.2 5.2 0 1 1-1.6-3.7M13.4 2.6v2.9h-2.9',
    "file": 'M4 2.2h5l3 3v8.6H4zM9 2.2v3.2h3',
    "shield": 'M8 2.2l5 1.9v3.7c0 3-2.1 5.3-5 6.1-2.9-.8-5-3.1-5-6.1V4.1z',
    "lock": 'M4.4 7.2h7.2v6H4.4zM5.9 7.2V5.4a2.1 2.1 0 0 1 4.2 0v1.8',
    "alert": 'M8 2.6l6 10.8H2zM8 6.6v3.1M8 11.4v.1',
    "info": 'M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12zM8 7.4v3.6M8 5.2v.1',
    "cog": 'M8 10.1a2.1 2.1 0 1 0 0-4.2 2.1 2.1 0 0 0 0 4.2zM8 1.9v1.7M8 12.4v1.7M2.7 8h1.7M11.6 8h1.7M4.2 4.2l1.2 1.2M10.6 10.6l1.2 1.2M11.8 4.2l-1.2 1.2M5.4 10.6l-1.2 1.2',
    "globe": 'M8 14A6 6 0 1 0 8 2a6 6 0 0 0 0 12zM2.2 8h11.6M8 2a9 9 0 0 1 0 12A9 9 0 0 1 8 2z',
    "trash": 'M3.4 4.6h9.2M6.2 4.6V3.2h3.6v1.4M4.6 4.6l.6 8.4h5.6l.6-8.4',
    "power": 'M8 2.4v5.4M4.6 4.4a4.8 4.8 0 1 0 6.8 0',
    "search": 'M7.2 12a4.8 4.8 0 1 0 0-9.6 4.8 4.8 0 0 0 0 9.6zM10.8 10.8L13.6 13.6',
    "x": 'M4.2 4.2l7.6 7.6M11.8 4.2l-7.6 7.6',
    "chip": 'M5.4 5.4h5.2v5.2H5.4zM6.6 2.6v2.8M9.4 2.6v2.8M6.6 10.6v2.8M9.4 10.6v2.8M2.6 6.6h2.8M2.6 9.4h2.8M10.6 6.6h2.8M10.6 9.4h2.8',
    "sliders": 'M3 5h10M3 11h10M6.2 3.2v3.6M10.4 9.2v3.6',
    "out": 'M6.2 3.2H3.2v9.6h9.6V9.8M9.4 2.8h3.8v3.8M13.2 2.8L7.6 8.4',
}


def ic(name, size=14, color="currentColor", sw=1.5):
    return ('<svg viewBox="0 0 16 16" width="%d" height="%d" fill="none" stroke="%s" stroke-width="%s" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" style="flex:none">'
            '<path d="%s"/></svg>' % (size, size, color, sw, _IC[name]))


# ── компоненты ─────────────────────────────────────────────────────────────
def tgl(on=False, lock=False):
    cls = "tgl" + (" on" if on else "") + (" lock" if lock else "")
    return '<span class="%s"><i></i></span>' % cls


def key(t):
    return '<span class="key">%s</span>' % t


def bd(text, kind="new"):
    return '<span class="bd %s">%s</span>' % (kind, text)


def btn(text, kind="", icon=None):
    i = (ic(icon, 13) + " ") if icon else ""
    return '<span class="btn %s">%s%s</span>' % (kind, i, text)


def row(label, control, sub=None, hint=True, dis=False, lock=False):
    left = '<div class="left"><div class="lbl">%s</div>%s</div>' % (
        label, ('<div class="sub">%s</div>' % sub) if sub else "")
    h = '<span class="hint">?</span>' if hint else ""
    lk = ('<span class="lockb">%s Задано администратором</span>' % ic("lock", 11)) if lock else ""
    return '<div class="r%s">%s%s%s%s</div>' % (" dis" if dis else "", left, h, lk, control)


def card(rows):
    return '<div class="card">%s</div>' % "".join(rows)


def group(title, rows):
    return '<div style="margin-bottom:16px"><div class="grp">%s</div>%s</div>' % (title, card(rows))


def met(name, pct, value, est=False):
    return ('<div class="met"><span class="mn">%s</span>'
            '<span class="trk%s"><i style="width:%d%%"></i></span>'
            '<span class="mv">%s</span></div>' % (name, " est" if est else "", pct, value))


def foot(left, right_html, mono="v0.2.0"):
    return ('<div class="foot">%s<span class="sp"></span>%s<span class="dot" style="margin:0 8px"></span>'
            '<span class="num">%s</span></div>' % (left, right_html, mono))


def foot_left(text, icon_state=None):
    i = (tray(icon_state, 15, "var(--fg3)") + " ") if icon_state else ""
    return '<span style="display:inline-flex;align-items:center;gap:7px">%s%s</span>' % (i, text)


# ── пилюля ─────────────────────────────────────────────────────────────────
LEVELS = [7, 12, 18, 20, 14, 9, 15, 11, 6]


def pill(state, w=None, cls="", scale=1.0):
    """Состояния F3.2 + F6: возвращает html пилюли."""
    def bars(color, flat=False):
        hs = [4] * 9 if flat else LEVELS
        return ('<span class="lv%s">%s</span>' % (
            " f" if flat else "",
            "".join('<i style="height:%dpx;background:%s"></i>' % (max(3, round(h * scale)), color)
                    for h in hs)))
    x = '<span class="x">&#10005;</span>'
    style = (' style="width:%dpx"' % w) if w else ""
    if state == "listening":
        inner = bars("#12B3A0") + '<span class="st"><b>Слушаю</b></span>' + x
    elif state == "silent":
        inner = (bars("#8C97AC", True) + ic("alert", 15, "#F2B559") +
                 '<span class="st">Микрофон молчит</span>' + x)
    elif state == "loading":
        inner = (ic("refresh", 16, "#C4CDDC") + '<span class="st">Загружаю модель…</span>')
    elif state == "processing":
        inner = ('<span class="lv">%s</span><span class="st">Распознаю…</span>' %
                 "".join('<i style="height:%dpx;background:#E8A33A;width:5px"></i>' % h for h in (5, 9, 5)))
    elif state == "done":
        inner = ic("check", 16, "#4FBF88") + '<span class="st"><b>Готово</b></span>'
    elif state == "empty":
        inner = ('<span style="color:#8C97AC;font-size:15px;line-height:1">&#8212;</span>'
                 '<span class="st">Ничего не распознано</span>')
    elif state == "clip":
        inner = ic("file", 15, "#C4CDDC") + '<span class="st">Скопировано в буфер</span>'
    elif state == "cancel":
        inner = ic("x", 15, "#8C97AC") + '<span class="st">Отменено</span>'
    elif state == "limit":
        inner = bars("#12B3A0") + '<span class="st">Достигнут лимит записи</span>'
    elif state == "error":
        inner = (ic("alert", 16, "#F0645F") + '<span class="st" style="color:#F0645F">Микрофон недоступен</span>'
                 + '<span class="x">&#8250;</span>')
    else:
        raise KeyError(state)
    return '<span class="pill %s"%s>%s</span>' % (cls, style, inner)


PILL_NAMES = [("loading", "Загружаю модель"), ("listening", "Слушаю (уровень)"), ("silent", "Микрофон молчит"),
              ("limit", "Лимит записи"), ("processing", "Распознаю"), ("done", "Готово ≤300 мс"),
              ("empty", "Ничего не распознано"), ("clip", "Только в буфер"), ("cancel", "Отменено"),
              ("error", "Ошибка (клик — подробности)")]


def pill_gallery(cls="", width=900, scale=1.0):
    cells = "".join('<div class="cell">%s<span class="cn">%s</span></div>' % (pill(s, None, cls, scale), n)
                    for s, n in PILL_NAMES)
    return '<div class="gal" style="max-width:%dpx">%s</div>' % (width, cells)


# ── данные каталога (PRD §7.2, цифры реальные) ─────────────────────────────
MODELS = [
    dict(id="rnnt", name="GigaAM v3 RNN-T", vendor="Сбер (GigaChat Team)",
         purpose="Русская диктовка с пунктуацией — по умолчанию",
         disk="232 МБ", ram="415 МБ", ramkind="замерено на этом компьютере", measured=True,
         q=90, qv="WER 7,6 %", s=92, sv="≈42× быстрее речи", punct=True, lang="Только русский",
         lic="MIT · Сбер", origin="отечественная", status="active",
         badges=[("Активна", "act"), ("Рекомендуем", "rec")]),
    dict(id="ctc", name="GigaAM v3 CTC", vendor="Сбер (GigaChat Team)",
         purpose="То же, быстрее на ~25 %, точность чуть ниже",
         disk="225 МБ", ram="~420 МБ", ramkind="оценка", measured=False,
         q=87, qv="WER 8,3 %", s=97, sv="≈53× быстрее речи", punct=True, lang="Только русский",
         lic="MIT · Сбер", origin="отечественная", status="installed", badges=[]),
    dict(id="rnnt-np", name="GigaAM v3 RNN-T без пунктуации", vendor="Сбер (GigaChat Team)",
         purpose="Максимальная точность слов, без знаков препинания",
         disk="230 МБ", ram="~425 МБ", ramkind="оценка", measured=False,
         q=90, qv="WER 7,4 %", s=92, sv="≈42× быстрее речи", punct=False, lang="Только русский",
         lic="MIT · Сбер", origin="отечественная", status="downloading",
         badges=[("Обновление доступно", "upd")]),
    dict(id="ml", name="GigaAM Multilingual CTC 220M", vendor="Сбер (GigaChat Team)",
         purpose="Русский + казахский, киргизский, узбекский, английский; без пунктуации",
         disk="214 МБ", ram="~400 МБ", ramkind="оценка", measured=False,
         q=80, qv="WER 10,1 %", s=90, sv="≈38× быстрее речи", punct=False, lang="ru, kk, ky, uz, en",
         lic="MIT · Сбер", origin="отечественная", status="new", badges=[("Новое", "new")]),
    dict(id="tone", name="T-one", vendor="Т-Банк",
         purpose="Русская, лёгкая, без пунктуации; хорошая точность",
         disk="138 МБ", ram="~260 МБ", ramkind="оценка", measured=False,
         q=74, qv="WER 11,5 %", s=96, sv="≈50× быстрее речи", punct=False, lang="Только русский",
         lic="Apache-2.0 · Т-Банк", origin="отечественная", status="avail", badges=[]),
    dict(id="vosk-s", name="Vosk small ru 0.52", vendor="Alpha Cephei",
         purpose="Для слабых машин и малого диска",
         disk="26 МБ", ram="~100 МБ", ramkind="оценка", measured=False,
         q=32, qv="WER 22,1 %", s=98, sv="≈55× быстрее речи", punct=False, lang="Только русский",
         lic="Apache-2.0 · Alpha Cephei", origin="отечественная", status="avail", badges=[]),
    dict(id="wturbo", name="Whisper large-v3-turbo", vendor="OpenAI",
         purpose="Многоязычная, качественно, медленно на процессоре",
         disk="987 МБ", ram="~1,8 ГБ", ramkind="оценка", measured=False,
         q=68, qv="WER 13,0 %", s=14, sv="≈1,8× быстрее речи", punct=True, lang="99 языков",
         lic="MIT / Apache-2.0 · OpenAI", origin="зарубежная", status="lowram", badges=[]),
    dict(id="wbase", name="Whisper base", vendor="OpenAI",
         purpose="Быстрая и лёгкая; для русского не рекомендуется",
         disk="153 МБ", ram="~300 МБ", ramkind="оценка", measured=False,
         q=8, qv="WER 27,8 %", s=55, sv="≈9× быстрее речи", punct=True, lang="Многоязычная",
         lic="MIT / Apache-2.0 · OpenAI", origin="зарубежная", status="avail", badges=[]),
]


def m(mid):
    for x in MODELS:
        if x["id"] == mid:
            return x
    raise KeyError(mid)


def ram_html(mo):
    if mo["measured"]:
        return '<span class="mv"><b>%s ОЗУ</b> · %s</span>' % (mo["ram"], mo["ramkind"])
    return '<span class="mv">%s ОЗУ · %s</span>' % (mo["ram"], mo["ramkind"])


def write(path, html):
    full = os.path.join(ROOT, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(html)
    return path
