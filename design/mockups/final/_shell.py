# -*- coding: utf-8 -*-
"""Полный макет направления A «Панель» — общая оболочка окна и данные.

Принято на ⛔ G2 (decisions/log.md, 2026-09-08):
  · направление A «Панель», окно 900×620, сайдбар 184 px, 6 разделов + скрытая «Отладка»;
  · пилюля — РЕЗИНОВАЯ под текст (min 172 → max 320 px), ellipsis не допускается;
  · каталог моделей показывается ЦЕЛИКОМ с прокруткой (без «показать ещё»);
  · подтверждение «Готово» в пилюле — 500 мс (правка PRD F3.2: было 300).

Сборка:  cd design/mockups/final && python3 _build.py
Каждый .html самодостаточен: без внешних ресурсов и скриптов, шрифты системные.
"""
import os
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.abspath(os.path.join(ROOT, "..", "directions")))

from _base import (  # noqa: E402
    page as _page, mark, logo, appicon, tray, ic, tgl, key, bd, btn, row, card,
    met, foot, foot_left, pill as _pill, MODELS as BASE_MODELS, LEVELS,
)


def group(title, rows, mb=8):
    """Группа настроек: серый CAPS-заголовок + карточка. Отступ плотнее базового."""
    return f'<div style="margin-bottom:{mb}px"><div class="grp">{title}</div>{card(rows)}</div>'

W, H = 900, 620
TB, FT = 32, 36
BODY_H = H - TB - FT  # 552

LIGHT = "светлая"
DARK = "тёмная"


def th(theme):
    return LIGHT if theme == "light" else DARK


# ── CSS поверх _base.CSS: доступность + компоненты полного макета ──────────
CSS_FINAL = """
/* --- токены доступности: цвет «выключено» с контрастом >= 4,5:1 ---------- */
:root{--fg-dis:#67748B}
body.dark{--fg-dis:#8C97AC}
.mi.dis{color:var(--fg-dis)}
.btn.dis{opacity:1;color:var(--fg-dis);background:var(--bg-sunk);border-color:var(--border)}
.r.dis{opacity:1}
.r.dis .lbl,.r.dis .sub{color:var(--fg-dis)}
.tgl.dis{background:var(--border)}

/* --- сайдбар ------------------------------------------------------------- */
.side{width:184px;flex:none;background:var(--bg-sunk);border-right:1px solid var(--border);
  display:flex;flex-direction:column;padding:12px 10px}
.slogo{display:flex;align-items:center;gap:8px;padding:4px 6px 12px;margin-bottom:6px;
  border-bottom:1px solid var(--border)}
.nav{display:flex;flex-direction:column;gap:2px;margin-top:8px}
.nv{display:flex;align-items:center;gap:9px;padding:7px 9px;border-radius:7px;font-size:13.5px;
  font-weight:500;color:var(--fg2)}
.nv.on{background:var(--primary);color:var(--primary-fg)}
.nv.sub{font-weight:400;color:var(--fg3);font-size:13px}
.nv .sp{flex:1}
.nv .cnt{font-size:11.5px;color:var(--fg3)}
.nv.on .cnt{color:rgba(255,255,255,.75)}
body.dark .nv.on .cnt{color:rgba(11,18,32,.7)}
.cont{flex:1;min-width:0;display:flex;flex-direction:column}
.chead{padding:12px 22px 8px}
.cbody{flex:1;padding:0 22px 16px}
.wrap{display:flex;flex:1;min-height:0}
.r{padding:7px 14px;min-height:38px}
.grp{margin:0 0 5px 2px}

/* --- карточка модели: плотнее по горизонтали, чтобы не рвалась в колонке 672 px --- */
.mven{white-space:nowrap}
.mc{padding:9px 13px;margin-bottom:8px}
.met .mn{width:54px}
.trk{width:78px}
.mhr{margin:8px 0 7px}

/* --- фокус клавиатуры (§8 PRD): видимая рамка 2 px ------------------------ */
.foc{outline:2px solid var(--primary);outline-offset:2px;border-radius:8px}

/* --- выпадающий список (раскрытый) --------------------------------------- */
.pop{position:absolute;background:var(--bg-surface);border:1px solid var(--border);border-radius:8px;
  box-shadow:0 12px 34px var(--shadow);padding:5px;z-index:5;min-width:236px}
.po{display:flex;align-items:center;gap:8px;padding:7px 9px;border-radius:6px;font-size:13px;color:var(--fg1)}
.po .sp{flex:1}
.po.on{background:var(--primary-bg);color:var(--primary);font-weight:500}
.po.dis{color:var(--fg-dis)}
.po .d2{font-size:11.5px;color:var(--fg3);display:block;margin-top:1px;font-weight:400}
.rel{position:relative}

/* --- галереи состояний --------------------------------------------------- */
.sec{max-width:900px;width:100%}
.sech{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--canvas-fg);
  margin:16px 0 8px;width:100%;max-width:900px}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;width:100%;max-width:900px}
.grid1{display:grid;grid-template-columns:1fr;gap:10px;width:100%;max-width:900px}
.grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:10px;width:100%;max-width:900px}
.stc{background:var(--bg-app);border:1px solid var(--border);border-radius:10px;padding:11px 12px;
  min-width:0;overflow-wrap:break-word}
.grid>*,.grid1>*,.grid3>*{min-width:0}
.stc div{flex-wrap:wrap}
.stc .win div,.stc .desk div,.stc .menu div{flex-wrap:nowrap}
.grid .mono,.grid3 .mono{overflow-wrap:anywhere}
.stn{font-size:11.5px;font-weight:700;color:var(--fg3);letter-spacing:.04em;text-transform:uppercase;
  margin-bottom:7px;display:flex;align-items:center;gap:6px}
.stn .code{font-family:var(--font-mono);text-transform:none;letter-spacing:0;font-weight:400;
  color:var(--fg4);font-size:11px}

/* --- диалог -------------------------------------------------------------- */
.dlg{background:var(--bg-app);border:1px solid var(--border);border-radius:9px;width:420px;
  box-shadow:0 16px 44px var(--shadow);overflow:hidden}
.dlg .dh{height:30px;background:var(--bg-tbar);border-bottom:1px solid var(--border);display:flex;
  align-items:center;padding:0 10px;font-size:12.5px;color:var(--fg2);gap:7px}
.dlg .db{padding:16px 18px 6px;display:flex;gap:13px}
.dlg .df{display:flex;gap:8px;align-items:center;padding:12px 18px 14px}
.dlg .df .sp{flex:1}

/* --- уведомление KDE ------------------------------------------------------ */
.kn{width:340px;background:#1B2331;border:1px solid rgba(255,255,255,.12);border-radius:8px;
  box-shadow:0 14px 36px rgba(0,0,0,.5);padding:11px 12px;color:#F2F5FA;font-size:12.5px}
.kn .knh{display:flex;align-items:center;gap:8px;font-size:11.5px;color:#8C97AC;margin-bottom:6px}
.kn b{display:block;font-size:13.5px;margin-bottom:3px}
.kn .knb{display:flex;gap:7px;margin-top:10px}
.kn .knb span{font-size:12px;background:rgba(255,255,255,.10);border-radius:6px;padding:5px 10px}
.kn .knb span.p{background:#6C93E8;color:#0B1220;font-weight:500}

/* --- панель обновления --------------------------------------------------- */
.upan{background:var(--bg-surface);border:1px solid var(--primary);border-radius:10px;padding:14px 16px}
.upan.n{border-color:var(--border)}
.ul{margin:6px 0 0;padding-left:17px;font-size:12.5px;color:var(--fg2);line-height:1.6}

/* --- онбординг ------------------------------------------------------------ */
.steps{display:flex;align-items:center;gap:7px}
.sd{width:7px;height:7px;border-radius:50%;background:var(--border)}
.sd.on{background:var(--primary);width:20px;border-radius:4px}
.sd.dn{background:var(--fg4)}
.obody{flex:1;display:flex;flex-direction:column;align-items:center;padding:24px 40px 0;overflow:hidden}
.obar{height:60px;flex:none;border-top:1px solid var(--border);display:flex;align-items:center;
  gap:10px;padding:0 22px;background:var(--bg-app)}

/* --- индикатор уровня микрофона ------------------------------------------ */
.lvl{display:flex;align-items:flex-end;gap:3px;height:34px}
.lvl i{width:7px;border-radius:3px;background:var(--accent);display:block}
.lvl.flat i{background:var(--fg4)}
.lvbox{background:var(--bg-sunk);border-radius:8px;padding:10px 12px;display:flex;align-items:center;gap:12px}

/* --- таблица «экран → состояние → файл» в оглавлении --------------------- */
table.t{border-collapse:collapse;width:100%;font-size:12.5px}
table.t th,table.t td{border-bottom:1px solid var(--border);padding:6px 9px;text-align:left;vertical-align:top}
table.t th{color:var(--fg3);font-weight:500;font-size:11.5px;text-transform:uppercase;letter-spacing:.05em}
table.t td a{color:var(--primary);text-decoration:none}
table.t td.f{font-family:var(--font-mono);font-size:11.5px}
"""


def page(title, caption, body, theme="light", legend="", extra=""):
    return _page(title, caption, body, theme, CSS_FINAL + extra, legend)


def write(path, html):
    full = os.path.join(ROOT, path)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "w", encoding="utf-8") as f:
        f.write(html)
    return path


# ── оболочка окна ──────────────────────────────────────────────────────────
NAV = [("Общие", "cog", ""), ("Модели", "chip", "3"), ("Вывод", "out", ""),
       ("Сеть и обновления", "refresh", ""), ("Продвинутые", "sliders", ""), ("О программе", "info", "")]


def side(active, debug_on=False):
    items = ""
    for n, i, cnt in NAV:
        on = " on" if n == active else ""
        c = f'<span class="sp"></span><span class="cnt">{cnt}</span>' if cnt else ""
        items += f'<div class="nv{on}">{ic(i, 17, "currentColor")}<span>{n}</span>{c}</div>'
    dbg = " on" if debug_on else " sub"
    return (f'<div class="side"><div class="slogo">{logo(22, 15, "var(--fg1)")}</div>'
            f'<div class="nav">{items}</div><div style="flex:1"></div>'
            f'<div class="nv{dbg}">{ic("sliders", 17, "currentColor")}'
            f'<span>Отладка (Ctrl+Shift+D)</span></div></div>')


def titlebar(title="Astra Voice — Настройки"):
    return (f'<div class="tbar">{mark(16)}<span class="t">{title}</span><span class="sp"></span>'
            f'<span class="wbtn"></span><span class="wbtn"></span><span class="wbtn c"></span></div>')


def shell(active, head_html, body, footer, title="Astra Voice — Настройки", debug_on=False, over=""):
    return (f'<div class="win rel" style="width:{W}px;height:{H}px">{titlebar(title)}'
            f'<div class="wrap">{side(active, debug_on)}<div class="cont">'
            f'<div class="chead">{head_html}</div><div class="cbody scr rel">{body}</div></div></div>'
            f'{footer}{over}</div>')


def head(h2, sub, right=""):
    r = f'<span style="flex:1"></span>{right}' if right else ""
    return ('<div style="display:flex;align-items:center;gap:10px">'
            f'<div><div class="h2">{h2}</div><div class="sm" style="margin-top:2px">{sub}</div></div>{r}</div>')


def sb(top=6, height=200):
    """Визуальная полоса прокрутки — содержимое обрезано, как в реальном окне."""
    return f'<div class="sb" style="top:{top}px;height:{height}px"></div>'


# ── строка внизу окна: все состояния (flows.md §3.2) ───────────────────────
def foot_right(state):
    P, M, D = "var(--primary)", "var(--fg3)", "var(--err-ink)"
    if state == "disabled":
        return '<span class="muted">Проверка обновлений отключена</span>'
    if state == "policy-locked":
        return (f'<span class="muted" style="display:inline-flex;align-items:center;gap:5px">'
                f'{ic("lock", 12, M)}Проверка обновлений отключена (задано администратором)</span>')
    if state == "checking":
        return '<span class="muted">Проверяю обновления…</span>'
    if state == "uptodate":
        return (f'<span class="muted" style="display:inline-flex;align-items:center;gap:5px">'
                f'{ic("check", 12, M)}Установлена последняя версия</span>')
    if state == "available":
        return '<span class="up">Доступна версия 0.2.1 · Установить</span>'
    if state == "admin-track":
        return (f'<span class="up" style="display:inline-flex;align-items:center;gap:5px">'
                f'{ic("shield", 13, P)}Доступна 0.2.1 · Скачать для администратора</span>')
    if state == "unavailable":
        return '<span class="muted">Источник обновлений недоступен · Обновить из файла…</span>'
    if state == "downloading":
        return ('<span style="display:inline-flex;align-items:center;gap:8px">'
                '<span class="prog" style="width:120px"><i style="width:42%"></i></span>'
                '<span class="num" style="color:var(--fg2)">Загрузка… 42 %</span></span>')
    if state == "verifying":
        return ('<span style="display:inline-flex;align-items:center;gap:8px">'
                '<span class="prog" style="width:120px"><i style="width:100%"></i></span>'
                '<span style="color:var(--fg2)">Проверяю подпись…</span></span>')
    if state == "installing":
        return ('<span style="display:inline-flex;align-items:center;gap:8px">'
                '<span class="prog" style="width:120px"><i style="width:78%"></i></span>'
                '<span style="color:var(--fg2)">Установка…</span></span>')
    if state == "polkit":
        return '<span style="color:var(--fg2)">Жду подтверждения администратора…</span>'
    if state == "restart":
        return '<span class="up">Установлено 0.2.1 · Перезапустить</span>'
    if state == "error-sig":
        return (f'<span style="color:{D};display:inline-flex;align-items:center;gap:5px">'
                f'{ic("alert", 12, D)}Пакет не прошёл проверку подписи · Подробнее</span>')
    if state == "error-net":
        return (f'<span style="color:{D};display:inline-flex;align-items:center;gap:5px">'
                f'{ic("alert", 12, D)}Не удалось скачать обновление · Повторить</span>')
    if state == "error-polkit":
        return (f'<span style="color:{D};display:inline-flex;align-items:center;gap:5px">'
                f'{ic("alert", 12, D)}Установка отменена · Открыть папку</span>')
    if state == "model-update":
        return '<span class="muted">Обновление модели: GigaAM v3 RNN-T</span>'
    if state == "skipped":
        return '<span class="muted">Версия 0.2.1 пропущена · Показать</span>'
    raise KeyError(state)


FOOT_STATES = [
    ("disabled", "Проверки выключены (по умолчанию)"),
    ("policy-locked", "Запрещено администратором"),
    ("checking", "Проверяю"),
    ("uptodate", "Обновлений нет (3 с после ручной проверки)"),
    ("available", "Доступна версия — единственный акцент"),
    ("admin-track", "Трек «устанавливает администратор»"),
    ("downloading", "Скачивается 42 %"),
    ("verifying", "Проверка подписи"),
    ("polkit", "Ожидание polkit"),
    ("installing", "Установка"),
    ("restart", "Установлено, нужен перезапуск"),
    ("error-net", "Ошибка: сеть"),
    ("error-sig", "Ошибка: подпись не сошлась"),
    ("error-polkit", "Ошибка: отказ polkit"),
    ("unavailable", "Источник недоступен"),
    ("model-update", "Обновление модели (не акцент)"),
    ("skipped", "Версия пропущена пользователем"),
]


def footer(model="GigaAM v3 RNN-T", state="disabled", icon="idle"):
    return foot(foot_left(model, icon), foot_right(state))


# ── каталог: 12 моделей PRD §7.2 (8 из _base + 4 добавленные) ──────────────
EXTRA_MODELS = [
    dict(id="vosk", name="Vosk ru 0.54", vendor="Alpha Cephei",
         purpose="Лёгкая полная Vosk; точность ниже GigaAM",
         disk="70 МБ", ram="~150 МБ", ramkind="оценка", measured=False,
         q=44, qv="WER 18,4 %", s=97, sv="≈52× быстрее речи", punct=False, lang="Только русский",
         lic="Apache-2.0 · Alpha Cephei", origin="происхождение уточняется", status="avail", badges=[]),
    dict(id="wsmall", name="Whisper small", vendor="OpenAI",
         purpose="Многоязычная, средняя; по-русски слабее GigaAM",
         disk="357 МБ", ram="~700 МБ", ramkind="оценка", measured=False,
         q=52, qv="WER 16,9 %", s=32, sv="≈4,1× быстрее речи", punct=True, lang="99 языков",
         lic="MIT / Apache-2.0 · OpenAI", origin="зарубежная", status="avail", badges=[]),
    dict(id="mllarge", name="GigaAM Multilingual Large CTC", vendor="Сбер (GigaChat Team)",
         purpose="«Качество любой ценой»: тяжёлая, без пунктуации",
         disk="564 МБ", ram="~1 ГБ", ramkind="оценка", measured=False,
         q=93, qv="WER 6,9 %", s=61, sv="≈11× быстрее речи", punct=False, lang="ru, kk, ky, uz, en",
         lic="MIT · Сбер", origin="отечественная", status="avail", badges=[]),
    dict(id="nemo", name="NeMo FastConformer ru pc", vendor="NVIDIA",
         purpose="Самая быстрая, с пунктуацией, точность ниже",
         disk="125 МБ", ram="~250 МБ", ramkind="оценка", measured=False,
         q=58, qv="WER 15,2 %", s=99, sv="≈58× быстрее речи", punct=True, lang="Только русский",
         lic="CC-BY-4.0 (атрибуция) · NVIDIA", origin="зарубежная", status="avail", badges=[]),
]

MODELS = list(BASE_MODELS) + EXTRA_MODELS
CATALOG_ORDER = ["rnnt", "ctc", "rnnt-np", "ml", "tone", "vosk", "vosk-s",
                 "wturbo", "wsmall", "wbase", "mllarge", "nemo"]


def m(mid):
    for x in MODELS:
        if x["id"] == mid:
            return dict(x)
    raise KeyError(mid)


# ── резиновая пилюля (решение G2): min 172 → max 320, без ellipsis ─────────
PILL_MIN, PILL_MAX = 172, 320


def pill(state, w=None, cls="", scale=1.0):
    """Обёртка над _base.pill: «Готово» держится 500 мс (решение 2026-09-08)."""
    return _pill(state, w, cls, scale)


def level_bars(kind="live", n=9, h=34):
    hs = {"live": [9, 16, 26, 31, 20, 12, 22, 15, 8],
          "quiet": [5, 7, 6, 8, 6, 5, 7, 6, 5],
          "flat": [3] * 9}[kind]
    cls = "lvl flat" if kind == "flat" else "lvl"
    bars = "".join(f'<i style="height:{v}px"></i>' for v in hs[:n])
    return f'<span class="{cls}" style="height:{h}px">{bars}</span>'


def stcell(name, html, code=""):
    c = f'<span class="code">{code}</span>' if code else ""
    return f'<div class="stc"><div class="stn">{name}{c}</div>{html}</div>'


def sech(text):
    return f'<div class="sech">{text}</div>'


def grid(cells, cols=2):
    cls = {1: "grid1", 2: "grid", 3: "grid3"}[cols]
    return f'<div class="{cls}">{"".join(cells)}</div>'
