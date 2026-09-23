# -*- coding: utf-8 -*-
"""Предложение: компактная карточка модели, два направления (A и B).

    cd design/mockups/proposals && python3 _build.py

Пишет card-compact-A.html, card-compact-A-dark.html, card-compact-B.html,
card-compact-B-dark.html и index.html. Токены цвета — те же, что в
design/mockups/final (светлая/тёмная), имена сверены с qml/Theme.qml.
Цифры моделей — из data/catalog.json (WER → «Точность» = 100 − WER).
"""
from decimal import Decimal, ROUND_HALF_UP
import html
import pathlib

OUT = pathlib.Path(__file__).resolve().parent

# ── токены (копия :root из final/*.html) ─────────────────────────────────────
TOK_LIGHT = """
  --bg-app:#F7F9FC; --bg-surface:#FFFFFF; --bg-sunk:#EDF1F7; --bg-tbar:#E7ECF4;
  --border:#D7DEEA; --border-soft:#EDF1F7;
  --fg1:#0E1729; --fg2:#243350; --fg3:#5A6884; --fg4:#AAB4C7; --fg-dis:#616D83;
  --primary:#1B3A73; --primary-hover:#12294F; --primary-fg:#FFFFFF; --primary-bg:#E8EDF7;
  --accent:#12B3A0; --accent-ink:#0A776A; --accent-bg:#E3F7F4;
  --ok-ink:#1F7D50; --ok-bg:#E7F5EE;
  --warn-ink:#8F5E12; --warn-bg:#FAF0DC;
  --err-ink:#C0322F; --err-bg:#FBECEB;
  --hover:#EDF1F7;
  --canvas:#DDE3EE; --canvas-fg:#3A4761; --shadow:rgba(14,23,41,.18); --close:#D96B62;
"""
TOK_DARK = """
  --bg-app:#0B1220; --bg-surface:#151E30; --bg-sunk:#080D18; --bg-tbar:#101A2B;
  --border:rgba(255,255,255,.10); --border-soft:rgba(255,255,255,.06);
  --fg1:#F2F5FA; --fg2:#C4CDDC; --fg3:#8C97AC; --fg4:#5A6884; --fg-dis:#7E889B;
  --primary:#6C93E8; --primary-hover:#8AACF0; --primary-fg:#0B1220; --primary-bg:rgba(108,147,232,.12);
  --accent:#2FD9C4; --accent-ink:#2FD9C4; --accent-bg:rgba(47,217,196,.13);
  --ok-ink:#4FBF88; --ok-bg:rgba(79,191,136,.13);
  --warn-ink:#F2B559; --warn-bg:rgba(242,181,89,.13);
  --err-ink:#F0645F; --err-bg:rgba(240,100,95,.13);
  --hover:#232C3C;
  --canvas:#05080F; --canvas-fg:#8C97AC; --shadow:rgba(0,0,0,.55); --close:#8C3B36;
"""

BASE_CSS = """
*{box-sizing:border-box}
body{margin:0;padding:26px 20px 40px;background:var(--canvas);color:var(--canvas-fg);
  font-family:'PT Root UI','PT Astra Sans','Open Sans','Roboto',sans-serif;font-size:14px;line-height:1.5;
  display:flex;flex-direction:column;align-items:center;gap:14px;-webkit-font-smoothing:antialiased}
.cap{font-size:13px;max-width:1060px;width:100%}
.cap b{color:var(--fg1)}
.legend{max-width:1060px;width:100%;font-size:12.5px;line-height:1.55}
.legend b{color:var(--fg1)}
.legend table{border-collapse:collapse;margin:6px 0 2px;font-size:12px}
.legend td,.legend th{border-bottom:1px solid var(--border);padding:4px 10px 4px 0;text-align:left;vertical-align:top}
.legend th{font-weight:500;color:var(--fg3)}
.sech{font-size:12px;font-weight:700;letter-spacing:.06em;text-transform:uppercase;color:var(--canvas-fg);
  margin:18px 0 2px;width:100%;max-width:1060px}
.row{display:flex;gap:18px;flex-wrap:wrap;justify-content:center;align-items:flex-start;width:100%;max-width:1060px}
.wcap{font-size:12px;color:var(--canvas-fg);margin:0 0 6px}
.wcap b{color:var(--fg1)}
.vis{font-weight:700;color:var(--fg1)}

/* окно */
.win{background:var(--bg-app);color:var(--fg1);border-radius:8px;overflow:hidden;
  box-shadow:0 14px 44px var(--shadow);display:flex;flex-direction:column;flex:none}
.tbar{height:32px;flex:none;display:flex;align-items:center;gap:8px;padding:0 10px;
  background:var(--bg-tbar);border-bottom:1px solid var(--border)}
.tbar .t{font-size:12.5px;color:var(--fg2);font-weight:500}
.sp{flex:1}
.wbtn{width:14px;height:14px;border-radius:3px;background:var(--border);display:inline-block}
.wbtn.c{background:var(--close)}
.wrap{display:flex;flex:1;min-height:0}
.side{width:184px;flex:none;background:var(--bg-sunk);border-right:1px solid var(--border);
  display:flex;flex-direction:column;padding:12px 10px}
.slogo{padding:4px 6px 12px;margin-bottom:6px;border-bottom:1px solid var(--border);font-size:15px;font-weight:500;letter-spacing:.03em}
.nav{display:flex;flex-direction:column;gap:2px;margin-top:8px}
.nv{white-space:nowrap;display:flex;align-items:center;gap:9px;padding:7px 9px;border-radius:7px;font-size:13.5px;font-weight:500;color:var(--fg2)}
.nv i{width:17px;height:17px;border-radius:5px;border:1.5px solid currentColor;opacity:.55;flex:none}
.nv.on{background:var(--primary);color:var(--primary-fg)}
.nv .cnt{font-size:11.5px;color:var(--fg3);margin-left:auto}
.nv.on .cnt{color:rgba(255,255,255,.75)}
body.dark .nv.on .cnt{color:rgba(11,18,32,.85)}
.cont{flex:1;min-width:0;display:flex;flex-direction:column}
.chead{padding:12px 22px 8px;display:flex;align-items:center;gap:10px}
.h2{font-size:20px;line-height:25px;font-weight:700;color:var(--fg1)}
.sm{font-size:13px;line-height:1.45;color:var(--fg3)}
.c12{font-size:12px;line-height:1.4;color:var(--fg3)}
.cbody{flex:1;min-height:0;padding:0 22px 16px;display:flex;flex-direction:column}
.col{margin:0 auto;width:100%;flex:1;min-height:0;overflow:hidden;position:relative}
.grp{font-size:11px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:var(--fg3);
  line-height:16.5px;padding-left:2px;margin:0 0 5px}
.foot{height:36px;flex:none;display:flex;align-items:center;gap:10px;padding:0 14px;
  background:var(--bg-app);border-top:1px solid var(--border);font-size:12.5px;color:var(--fg3)}
.dlbar{white-space:nowrap;overflow:hidden;height:36px;flex:none;display:flex;align-items:center;gap:10px;padding:0 22px;
  background:var(--bg-app);border-top:1px solid var(--border);font-size:12.5px;color:var(--fg2)}
.prog{width:160px;height:6px;border-radius:3px;background:var(--bg-sunk);overflow:hidden}
.prog i{display:block;height:100%;background:var(--primary);border-radius:3px}
.chips{display:flex;gap:6px;align-items:center;margin:0 0 12px}
.chip{font-size:12.5px;color:var(--fg2);background:var(--bg-surface);border:1px solid var(--border);
  border-radius:14px;padding:4px 11px;white-space:nowrap}
.chip.on{background:var(--primary);border-color:var(--primary);color:var(--primary-fg);font-weight:500}

/* мастер */
.steps{display:flex;align-items:center;gap:7px}
.sd{width:7px;height:7px;border-radius:50%;background:var(--border)}
.sd.on{background:var(--primary);width:20px;border-radius:4px}
.sd.dn{background:var(--fg4)}
.obody{flex:1;min-height:0;display:flex;flex-direction:column;align-items:center;padding:24px 40px 0;overflow:hidden}
.obar{height:60px;flex:none;border-top:1px solid var(--border);display:flex;align-items:center;gap:10px;padding:0 22px;background:var(--bg-app)}

/* контролы */
.btn{font-family:inherit;font-size:13px;font-weight:500;color:var(--fg1);background:var(--bg-surface);
  border:1px solid var(--border);border-radius:7px;padding:6px 12px;display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.btn.pri{background:var(--primary);border-color:var(--primary);color:var(--primary-fg)}
.btn.gh{background:transparent;border-color:transparent;color:var(--primary);padding:6px 8px}
.btn.dis{color:var(--fg-dis);background:var(--bg-sunk)}
.btn.sm{font-size:12.5px;padding:4px 9px;height:28px}
.bd{font-size:11.5px;font-weight:500;border-radius:5px;padding:2px 7px;display:inline-flex;align-items:center;white-space:nowrap;line-height:17px}
.bd.rec{background:var(--primary-bg);color:var(--primary)}
.bd.ins{background:var(--ok-bg);color:var(--ok-ink)}
.bd.act{background:var(--bg-surface);color:var(--accent-ink)}
/* на выбранной карточке фон «Рекомендуем» совпал бы с фоном карточки — поднимаем на поверхность */
.card.pick .bd.rec{background:var(--bg-surface)}
.bd.busy{background:var(--bg-sunk);color:var(--fg3)}
.bd.new{background:var(--bg-sunk);color:var(--fg3)}
.cbx{width:18px;height:18px;border-radius:5px;border:1.5px solid var(--fg4);background:var(--bg-surface);
  display:inline-flex;align-items:center;justify-content:center;flex:none;margin-top:2px}
.cbx.on{background:var(--primary);border-color:var(--primary)}
.cbx.lock,.cbx.blk{background:var(--bg-sunk);border-color:var(--border)}
.dot{width:4px;height:4px;border-radius:50%;background:var(--fg4);display:inline-block;flex:none}
.foc{outline:2px solid var(--accent-ink);outline-offset:2px}

/* полоски: цвет — оценка по шкале, длина — сравнение */
.bar{height:8px;border-radius:4px;background:var(--bg-sunk);overflow:hidden;flex:none;display:inline-block}
.bar i{display:block;height:100%;border-radius:4px}
.bar.g i{background:var(--ok-ink)}
.bar.a i{background:var(--warn-ink)}
.bar.r i{background:var(--err-ink)}
.bar.nd{background:transparent;border:1px dashed var(--fg4)}
.mv{white-space:nowrap;display:inline-flex;align-items:center;gap:4px;color:var(--fg2)}
.mv.meas{color:var(--fg1);font-weight:500}
.mv.nd{color:var(--fg-dis);font-style:italic}
.mk{flex:none}
.mv .mk,.facts .mk,.b-size .mk{margin-left:3px}
.msg{display:inline-flex;align-items:flex-start;gap:5px;font-size:12px;line-height:18px}
.msg svg{margin-top:3px}
.msg.e{color:var(--err-ink)}
.msg.w{color:var(--warn-ink)}
.msg.i{color:var(--fg3)}
.msg.d{color:var(--fg-dis)}

/* галерея */
.gal{display:grid;grid-template-columns:1fr;gap:12px;width:100%;max-width:1060px}
.stc{background:var(--bg-app);border:1px solid var(--border);border-radius:10px;padding:11px 12px}
.stn{font-size:11.5px;font-weight:700;color:var(--fg3);letter-spacing:.04em;text-transform:uppercase;
  margin-bottom:7px;display:flex;gap:8px;align-items:baseline}
.stn .code{font-family:'PT Mono','DejaVu Sans Mono',monospace;text-transform:none;letter-spacing:0;font-weight:400;color:var(--fg3);font-size:11px}
.stn .hh{margin-left:auto;text-transform:none;letter-spacing:0;font-weight:500;color:var(--fg1)}
.cmp{display:grid;grid-template-columns:1fr;gap:10px;width:100%;max-width:1060px}
"""

# Текущая карточка (для сравнения «было») — CSS из final/*.html, блок .mc.
OLD_CSS = """
.old .mc{background:var(--bg-surface);border:1px solid var(--border);border-radius:10px;padding:9px 13px;margin-bottom:8px}
.old .mc.act{border-color:var(--accent);background:var(--accent-bg)}
.old .mtop{display:flex;gap:14px;align-items:flex-start}
.old .ml{flex:1;min-width:0}
.old .mhead{display:flex;align-items:center;gap:6px;flex-wrap:wrap}
.old .mname{font-size:15px;font-weight:500;color:var(--fg1)}
.old .mven{color:var(--fg3);font-weight:400;font-size:12.5px;white-space:nowrap}
.old .mpurp{font-size:12.5px;color:var(--fg3);margin-top:2px}
.old .mmets{display:flex;flex-direction:column;gap:5px;flex:none}
.old .met{display:flex;align-items:center;gap:8px}
.old .met .mn{font-size:11.5px;color:var(--fg3);width:54px;text-align:right;flex:none}
.old .trk{width:78px;height:6px;border-radius:3px;background:var(--bg-sunk);overflow:hidden;flex:none}
.old .trk i{display:block;height:100%;border-radius:3px;background:var(--fg4)}
.old .met .mv{font-size:11.5px;color:var(--fg3);white-space:nowrap}
.old .msrc{font-size:11.5px;line-height:1.35;color:var(--fg4);text-align:right;margin-top:2px}
.old .mhr{height:1px;background:var(--border-soft);margin:8px 0 7px}
.old .mspace{font-size:12px;line-height:18px;color:var(--fg3)}
.old .mspace b{font-weight:400;color:var(--fg1)}
.old .mbot{display:flex;align-items:center;gap:9px;font-size:12px;color:var(--fg3);flex-wrap:wrap}
"""

# ── направление A «Компактная»: три строки, метрики справа с подписями ───────
A_CSS = """
.card{background:var(--bg-surface);border:1px solid var(--border);border-radius:10px;padding:8px 12px;margin-bottom:6px}
.card.pick{border-color:var(--primary);background:var(--primary-bg)}
.card.act{border-color:var(--accent);background:var(--accent-bg)}
.card.hov{background:var(--hover)}
.a-top{display:flex;gap:12px;align-items:flex-start}
.a-l{flex:1;min-width:0}
.a-head{display:flex;align-items:center;gap:6px;flex-wrap:wrap;min-height:22px}
.mname{font-size:15px;font-weight:500;line-height:22px;color:var(--fg1)}
.mven{font-size:12.5px;color:var(--fg3);white-space:nowrap}
.a-purp{font-size:12.5px;line-height:18px;color:var(--fg3)}
.a-mets{flex:none;display:flex;flex-direction:column;gap:4px}
.m{display:flex;align-items:center;gap:6px;height:18px;font-size:12px}
.m .ml{width:46px;margin-right:2px;text-align:right;font-size:11.5px;color:var(--fg3);flex:none}
.m .bar{width:60px}
.m .mv{width:118px;flex:none}
.a-foot{display:flex;align-items:flex-start;gap:12px;margin-top:6px}
.facts{flex:1;min-width:0;font-size:12px;line-height:18px;color:var(--fg3);display:flex;flex-wrap:wrap;
  align-items:center;column-gap:7px;padding:0}
.a-foot.hasbtn .facts{padding-top:5px}
.facts b{font-weight:400;color:var(--fg1)}
.act-r{display:flex;align-items:center;gap:8px;flex:none;max-width:68%;justify-content:flex-end}
.act-r .msg{flex:1 1 auto;min-width:0}
.card.blk .mname,.card.blk .a-purp,.card.blk .facts,.card.blk .facts b,.card.blk .mven{color:var(--fg-dis)}
"""

# ── направление B «Строка-таблица»: две строки, столбцы с заголовком на список ─
B_CSS = """
.card{background:var(--bg-surface);border:1px solid var(--border);border-radius:10px;padding:8px 12px;margin-bottom:6px}
.card.pick{border-color:var(--primary);background:var(--primary-bg)}
.card.act{border-color:var(--accent);background:var(--accent-bg)}
.card.hov{background:var(--hover)}
.b-r1{display:flex;align-items:flex-start;gap:12px}
.b-r1 .cbx{margin-top:2px}
.b-name{flex:1;min-width:0;display:flex;align-items:center;gap:6px;flex-wrap:wrap;min-height:22px}
.mname{font-size:15px;font-weight:500;line-height:22px;color:var(--fg1)}
.mven{font-size:12.5px;color:var(--fg3);white-space:nowrap}
.cell{flex:none;display:flex;align-items:center;gap:7px;height:22px;font-size:12.5px}
.cell .bar{width:50px}
.cell.q{width:var(--wq)}
.cell.s{width:var(--ws)}
.b-r2{display:flex;gap:12px;align-items:flex-start;margin-top:0;padding-left:30px}
.b-sub{flex:1;min-width:0;font-size:12.5px;line-height:18px;color:var(--fg3)}
.b-size{flex:none;max-width:calc(var(--wq) + var(--ws) + 12px);font-size:12px;line-height:18px;color:var(--fg3);
  display:flex;align-items:center;column-gap:7px;flex-wrap:wrap}
.b-size b{font-weight:400;color:var(--fg1)}
.b-r3{display:flex;align-items:center;gap:8px;margin-top:6px;padding-left:30px;min-height:28px}
.b-r3 .msg{flex:0 1 auto;min-width:0}
.card.blk .mname,.card.blk .b-sub,.card.blk .b-size,.card.blk .b-size b,.card.blk .mven{color:var(--fg-dis)}
.lhead{display:flex;align-items:flex-end;gap:12px;padding:0 13px 0 0;margin:0 0 5px}
.lhead .grp{flex:1;margin:0}
.lhead .hc{flex:none;font-size:11px;font-weight:500;letter-spacing:.08em;text-transform:uppercase;color:var(--fg3);line-height:16.5px}
.lhead .hc.q{width:var(--wq)}
.lhead .hc.s{width:var(--ws)}
:root{--wq:118px;--ws:182px}
"""

# ── иконки ──────────────────────────────────────────────────────────────────
def svg(d, size=12, color="currentColor", sw=1.5):
    return (f'<svg class="mk" viewBox="0 0 16 16" width="{size}" height="{size}" fill="none" stroke="{color}" '
            f'stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">{d}</svg>')

CHECK = '<path d="M3 8.4l3.4 3.3L13 4.6"/>'
ALERT = '<path d="M8 2.6l6 10.8H2zM8 6.6v3.1M8 11.4v.1"/>'
FOLDER = '<path d="M2.2 4.4h4.2l1.3 1.7h6.1v7.5H2.2z"/>'
DOWN = '<path d="M8 2.5v8M4.5 7.5L8 11l3.5-3.5M3 13.5h10"/>'
MEAS = svg('<path d="M3 8.6l3.2 3.1L13 4.8"/>', 11, "var(--primary)", 2)  # маркер «замерено здесь»


# ── данные каталога (data/catalog.json) ─────────────────────────────────────
BEST_RTFX = 83.5
M = {
    "rnnt": dict(name="GigaAM v3 RNN-T", ven="Сбер (GigaChat Team)", vs="Сбер", wer=7.6, rtfx=42.5,
                 purp="Русская диктовка с пунктуацией — модель по умолчанию", disk="226 МБ", ram="419 МБ",
                 tags=["Только русский", "с пунктуацией", "MIT"], dom=True),
    "ctc": dict(name="GigaAM v3 CTC", ven="Сбер (GigaChat Team)", vs="Сбер", wer=7.8, rtfx=52.2,
                purp="Русская диктовка с пунктуацией, работает чуть быстрее", disk="225 МБ", ram="417 МБ",
                tags=["Только русский", "с пунктуацией", "MIT"], dom=True),
    "rnntnp": dict(name="GigaAM v3 RNN-T без пунктуации", ven="Сбер (GigaChat Team)", vs="Сбер", wer=4.39, rtfx=42.8,
                   purp="Русская диктовка без знаков препинания — зато реже ошибается в словах", disk="226 МБ",
                   ram="418 МБ", tags=["Только русский", "MIT"], dom=True),
    "ctcnp": dict(name="GigaAM v3 CTC без пунктуации", ven="Сбер (GigaChat Team)", vs="Сбер", wer=None, rtfx=None,
                  purp="Русская диктовка без знаков препинания. Качество и скорость этой сборки никто не замерял",
                  disk="225 МБ", ram="416 МБ", tags=["Только русский", "MIT"], dom=True),
    "ml": dict(name="GigaAM Multilingual CTC", ven="Сбер (GigaChat Team)", vs="Сбер", wer=8.43, rtfx=None,
               purp="Понимает русский, английский, казахский, киргизский и узбекский. Знаки препинания не ставит",
               disk="225 МБ", ram="416 МБ", tags=["Русский и ещё четыре языка", "MIT"], dom=True),
    "mll": dict(name="GigaAM Multilingual Large CTC", ven="Сбер (GigaChat Team)", vs="Сбер", wer=5.55, rtfx=None,
                purp="Самая точная из многоязычных: русский, английский, казахский, киргизский, узбекский. Знаки препинания не ставит",
                disk="592 МБ", ram="1095 МБ", tags=["Русский и ещё четыре языка", "MIT"], dom=True),
    "tone": dict(name="T-one", ven="Т-Банк (T-Tech)", vs="Т-Банк", wer=6.57, rtfx=None,
                 purp="Русская диктовка от Т-Банка. Знаки препинания не ставит", disk="144 МБ", ram="267 МБ",
                 tags=["Только русский", "Apache-2.0"], dom=True),
    "vosk": dict(name="Vosk ru", ven="Alpha Cephei", vs="Alpha Cephei", wer=9.89, rtfx=70.5,
                 purp="Небольшая и быстрая, для не самых мощных компьютеров. Знаки препинания не ставит",
                 disk="72 МБ", ram="135 МБ", tags=["Только русский", "Apache-2.0"], dom=False),
    "vosks": dict(name="Vosk small ru", ven="Alpha Cephei", vs="Alpha Cephei", wer=14.53, rtfx=83.5,
                  purp="Самая маленькая и самая быстрая, для слабых компьютеров. Ошибается чаще, знаки препинания не ставит",
                  disk="27 МБ", ram="50 МБ", tags=["Только русский", "Apache-2.0"], dom=False),
    "wturbo": dict(name="Whisper large-v3-turbo", ven="OpenAI", vs="OpenAI", wer=10.1, rtfx=3.9,
                   purp="Понимает почти любой язык и ставит знаки препинания. Много весит и требует мощного компьютера",
                   disk="1086 МБ", ram="2007 МБ", tags=["Русский и ещё много языков", "с пунктуацией", "MIT"], dom=False),
    "wsmall": dict(name="Whisper small", ven="OpenAI", vs="OpenAI", wer=None, rtfx=None,
                   purp="Понимает почти любой язык и ставит знаки препинания. Насколько точна по-русски — никто не замерял",
                   disk="250 МБ", ram="461 МБ", tags=["Русский и ещё много языков", "с пунктуацией", "Apache-2.0"], dom=False),
    "nemo": dict(name="NeMo FastConformer ru", ven="NVIDIA", vs="NVIDIA", wer=13.1, rtfx=70.6,
                 purp="Русская диктовка с пунктуацией от NVIDIA. Ошибается чаще, чем GigaAM", disk="132 МБ",
                 ram="244 МБ", tags=["Только русский", "с пунктуацией", "CC-BY-4.0"], dom=False),
}
ORDER = ["rnnt", "ctc", "rnntnp", "ctcnp", "ml", "mll", "tone", "vosk", "vosks", "wturbo", "wsmall", "nemo"]


def dec1(x):
    return str(Decimal(str(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)).replace(".", ",")


def accuracy(wer):
    return Decimal("100") - Decimal(str(wer))


def q_level(acc):
    return "g" if acc >= 92 else "a" if acc >= 88 else "r"


def s_level(x):
    return "g" if x >= 20 else "a" if x >= 5 else "r"


def metric_q(m, width_css=""):
    if m["wer"] is None:
        return '<span class="bar nd"></span>', '<span class="mv nd">нет данных</span>'
    acc = accuracy(m["wer"])
    bar = f'<span class="bar {q_level(acc)}"><i style="width:{acc}%"></i></span>'
    return bar, f'<span class="mv">{dec1(acc)} %</span>'


def metric_s(m, measured=None):
    val = measured if measured is not None else m["rtfx"]
    if val is None:
        return '<span class="bar nd"></span>', '<span class="mv nd">нет данных</span>'
    fill = min(100.0, val / BEST_RTFX * 100)
    bar = f'<span class="bar {s_level(val)}"><i style="width:{fill:.1f}%"></i></span>'
    if measured is not None:
        return bar, (f'<span class="mv meas" title="замерено на этом компьютере">{dec1(val)}× быстрее речи'
                     f'{MEAS}</span>')
    return bar, f'<span class="mv">{dec1(val)}× быстрее речи</span>'


BADGE_TXT = {"rec": "Рекомендуем", "ins": "Установлена", "act": "Установлена и активна", "new": "Новое",
             "dl": "Загружается", "q": "В очереди", "ver": "Проверяю…", "sw": "Переключаю…"}
BADGE_CLS = {"rec": "rec", "ins": "ins", "act": "act", "new": "new", "dl": "busy", "q": "busy", "ver": "busy", "sw": "busy"}


def badges(bs):
    return "".join(f'<span class="bd {BADGE_CLS[b]}">{BADGE_TXT[b]}</span>' for b in bs)


def cbx(kind):
    if kind == "on":
        return f'<span class="cbx on">{svg(CHECK, 12, "var(--primary-fg)")}</span>'
    if kind == "lock":
        return f'<span class="cbx lock">{svg(CHECK, 12, "var(--fg-dis)")}</span>'
    if kind == "blk":
        return '<span class="cbx blk"></span>'
    if kind == "none":
        return '<span class="cbx" style="visibility:hidden"></span>'
    return '<span class="cbx"></span>'


def btn(text, cls="", icon=None):
    ic = svg(icon, 13) if icon else ""
    return f'<span class="btn sm {cls}">{ic}{text}</span>'


def msg(kind, text, icon=True):
    ic = svg(ALERT, 12) if icon else ""
    return f'<span class="msg {kind}">{ic}<span>{text}</span></span>'


# Состояние карточки: (класс карточки, отметка, бейджи, сообщение, кнопки, замер)
def state(code, m):
    rec = ["rec"] if m.get("rec") else []
    S = {
        "avail": ("", "off", rec, None, []),
        "selected": ("pick", "on", rec, None, []),
        "hover": ("hov", "off", rec, None, []),
        "focus": ("foc", "off", rec, None, []),
        "active": ("act", "lock", rec + ["act"], None, [btn("Удалить")]),
        "installed": ("", "lock", rec + ["ins"], None, [btn("Сделать рабочей", "pri"), btn("Удалить")]),
        "update": ("", "lock", rec + ["ins"], None, [btn("Обновить", "", DOWN), btn("Сделать рабочей", "pri"), btn("Удалить")]),
        "downloading": ("pick", "lock", rec + ["dl"], None, [btn("Отмена")]),
        "queued": ("pick", "lock", rec + ["q"], None, [btn("Отмена")]),
        "verifying": ("pick", "lock", rec + ["ver"], msg("d", "Отмена недоступна", False), []),
        "switching": ("act", "lock", rec + ["sw"], None, [btn("Удалить", "dis")]),
        "failed": ("", "off", rec, msg("e", "Не удалось загрузить модель — соединение оборвалось"), [btn("Повторить", "pri")]),
        "nospace": ("blk", "blk", rec, msg("e", "Нужно ещё 126 МБ, свободно 100 МБ"), [btn("Открыть папку моделей", "", FOLDER)]),
        "lowram": ("", "off", rec, msg("w", "Нужно ~2 ГБ памяти — на этом компьютере 3,8 ГБ, может не хватить"), []),
        "policy": ("blk", "blk", rec, msg("i", "Задано администратором: только отечественные модели", False), []),
        "broken": ("", "lock", rec, msg("e", "Файлы модели не читаются — переустановите"), [btn("Переустановить", "pri"), btn("Удалить")]),
        "removed": ("", "lock", rec + ["ins"], msg("i", "Снята с каталога — обновлений не будет", False), [btn("Сделать рабочей", "pri"), btn("Удалить")]),
        "new": ("", "off", rec + ["new"], None, []),
    }
    return S[code]


# ── карточка A ──────────────────────────────────────────────────────────────
def card_a(key, code="avail", rec=False, meas_speed=None, meas_ram=None):
    m = dict(M[key]); m["rec"] = rec
    cls, mark, bs, message, buttons = state(code, m)
    qb, qv = metric_q(m)
    sb, sv = metric_s(m, meas_speed)
    ram = (f'<b>{meas_ram} МБ</b>&nbsp;в памяти{MEAS}' if meas_ram else f'<b>{m["ram"]}</b>&nbsp;в памяти')
    tags = list(m["tags"]) + (["отечественная"] if m["dom"] else [])
    facts = [f'<b>{m["disk"]}</b>&nbsp;на диске', ram] + tags
    facts_html = '<span class="dot"></span>'.join(f'<span>{f}</span>' for f in facts)
    right = ""
    if message or buttons:
        right = f'<span class="act-r">{message or ""}{"".join(buttons)}</span>'
    hasbtn = " hasbtn" if buttons else ""
    return (f'<div class="card {cls}"><div class="a-top">{cbx(mark)}<div class="a-l"><div class="a-head">'
            f'<span class="mname">{m["name"]}</span><span class="mven" title="{m["ven"]}">· {m["vs"]}</span>{badges(bs)}</div>'
            f'<div class="a-purp">{m["purp"]}</div></div>'
            f'<div class="a-mets"><div class="m"><span class="ml">Точность</span>{qb}{qv}</div>'
            f'<div class="m"><span class="ml">Скорость</span>{sb}{sv}</div></div></div>'
            f'<div class="a-foot{hasbtn}"><span class="facts">{facts_html}</span>{right}</div></div>')


# ── карточка B ──────────────────────────────────────────────────────────────
def card_b(key, code="avail", rec=False, meas_speed=None, meas_ram=None):
    m = dict(M[key]); m["rec"] = rec
    cls, mark, bs, message, buttons = state(code, m)
    qb, qv = metric_q(m)
    sb, sv = metric_s(m, meas_speed)
    ven = m["vs"]
    ram = (f'<b>{meas_ram} МБ</b>&nbsp;в памяти{MEAS}' if meas_ram else f'<b>{m["ram"]}</b>&nbsp;в памяти')
    size = f'<span><b>{m["disk"]}</b>&nbsp;на диске</span><span class="dot"></span><span>{ram}</span>'
    if m["dom"]:
        size += '<span class="dot"></span><span>отечественная</span>'
    # В B бейдж состояния живёт в строке действий: шапка — только имя и «Рекомендуем».
    head_bs = [x for x in bs if x in ("rec", "new")]
    state_bs = [x for x in bs if x not in ("rec", "new")]
    r3 = ""
    if message or buttons or state_bs:
        r3 = (f'<div class="b-r3">{badges(state_bs)}{message or ""}<span class="sp"></span>'
              f'{"".join(buttons)}</div>')
    return (f'<div class="card {cls}"><div class="b-r1">{cbx(mark)}<div class="b-name">'
            f'<span class="mname">{m["name"]}</span><span class="mven" title="{m["ven"]}">· {ven}</span>{badges(head_bs)}</div>'
            f'<div class="cell q">{qb}{qv}</div><div class="cell s">{sb}{sv}</div></div>'
            f'<div class="b-r2"><div class="b-sub">{m["purp"]}</div><div class="b-size">{size}</div></div>{r3}</div>')


def lhead_b(caption):
    return (f'<div class="lhead"><div class="grp">{caption}</div><span class="hc q">Точность</span>'
            f'<span class="hc s">Скорость</span></div>')


# ── старая карточка (как сейчас в final/08-onboarding-2-model.html) ─────────
def card_old(key, installed=False):
    m = M[key]
    acc_fill = 90
    btns = ('<span class="sp" style="flex:1"></span><span class="btn sm pri">Сделать рабочей</span>'
            '<span class="btn sm">Удалить</span>') if installed else ""
    bd = '<span class="bd rec">Рекомендуем</span>' + ('<span class="bd ins">Установлена</span>' if installed else "")
    return (f'<div class="old"><div class="mc"><div class="mtop">{cbx("lock" if installed else "off")}<div class="ml">'
            f'<div class="mhead"><span class="mname">{m["name"]} <span class="mven">· {m["ven"]}</span></span>{bd}</div>'
            f'<div class="mpurp">{m["purp"]}</div></div><div class="mmets">'
            f'<div class="met"><span class="mn">Качество</span><span class="trk"><i style="width:{acc_fill}%"></i></span>'
            f'<span class="mv">WER 7,60 %</span></div><div class="met"><span class="mn">Скорость</span>'
            f'<span class="trk"><i style="width:50%"></i></span><span class="mv">42,5× быстрее речи</span></div>'
            f'<div class="msrc">цифры авторов, не с этого компьютера</div></div></div><div class="mhr"></div>'
            f'<div class="mspace">Занимает места: <b>{m["disk"]}</b> на диске <span class="dot"></span> '
            f'<b>{m["ram"]}</b> в памяти при работе</div><div class="mbot" style="margin-top:6px">'
            f'Только русский <span class="dot"></span> с пунктуацией <span class="dot"></span> MIT · Сбер '
            f'<span class="dot"></span> отечественная{btns}</div></div></div>')


# ── окна ────────────────────────────────────────────────────────────────────
def tbar(title):
    return (f'<div class="tbar"><span class="t">{title}</span><span class="sp"></span>'
            '<span class="wbtn"></span><span class="wbtn"></span><span class="wbtn c"></span></div>')


def sidebar():
    items = [("Общие", ""), ("Модели", "2"), ("Вывод", ""), ("Сеть и обновления", ""), ("Продвинутые", ""), ("О программе", "")]
    nav = "".join(f'<div class="nv{" on" if t == "Модели" else ""}"><i></i>{t}'
                  f'{f"<span class=cnt>{c}</span>" if c else ""}</div>' for t, c in items)
    return f'<div class="side"><div class="slogo">Astra Voice</div><div class="nav">{nav}</div></div>'


def models_list(variant):
    card = card_a if variant == "A" else card_b
    inst = [card("rnnt", "active", rec=True, meas_speed=38.7, meas_ram=402),
            card("ctc", "installed")]
    avail = [card("rnntnp", "downloading"), card("mll", "avail"), card("tone", "selected"), card("vosk", "avail"),
             card("vosks", "avail"), card("wturbo", "lowram"), card("wsmall", "avail"), card("nemo", "avail")]
    if variant == "A":
        cap1 = '<div class="grp">Установленные · 2</div>'
        cap2 = '<div class="grp" style="margin-top:8px">Доступные · 10</div>'
    else:
        cap1 = lhead_b("Установленные · 2")
        cap2 = '<div style="margin-top:8px">' + lhead_b("Доступные · 10") + "</div>"
    return cap1 + "".join(inst) + cap2 + "".join(avail)


def win_models(variant, w, strip, colw):
    status = ('<div class="foot"><span>GigaAM v3 RNN-T</span><span class="dot"></span><span>готова к диктовке</span>'
              '<span class="sp"></span><span>Обновлений нет</span></div>')
    dl = ('<div class="dlbar">Загружаю GigaAM v3 RNN-T без пунктуации<span class="prog"><i style="width:45%"></i></span>'
          '<span class="c12">101 из 226 МБ · около минуты</span><span class="sp"></span>'
          '<span class="btn sm">Отмена</span></div>') if strip else ""
    return (f'<div class="win" style="width:{w}px;height:620px">{tbar("Astra Voice")}<div class="wrap">{sidebar()}'
            f'<div class="cont"><div class="chead"><div style="flex:1"><div class="h2">Модели</div>'
            f'<div class="sm" style="margin-top:2px">Какая модель распознаёт речь</div></div>'
            f'<span class="btn sm">{svg(FOLDER, 13)}Установить из файла или папки…</span></div>'
            f'<div class="cbody"><div class="col scrollbox" style="max-width:{colw}px">'
            f'<div class="chips"><span class="chip on">Все языки</span><span class="chip">Только отечественные</span>'
            f'<span class="sp"></span><span class="c12">Установлено 2 из 12 · 451 МБ на диске</span></div>'
            f'<div style="height:4px"></div>{models_list(variant)}</div></div>{dl}</div></div>{status}</div>')


def win_wizard(variant, w, contw):
    card = card_a if variant == "A" else card_b
    cards = [card("rnnt", "selected", rec=True), card("ctc"), card("rnntnp"), card("ctcnp"), card("ml"),
             card("mll"), card("tone"), card("vosk")]
    head = lhead_b("") if variant == "B" else ""
    return (f'<div class="win" style="width:{w}px;height:620px">{tbar("Astra Voice — первый запуск")}'
            '<div style="display:flex;align-items:center;gap:12px;padding:14px 22px 0"><span class="c12">Шаг 2 из 5</span>'
            '<span class="steps"><span class="sd dn"></span><span class="sd on"></span><span class="sd"></span>'
            '<span class="sd"></span><span class="sd"></span></span><span class="sp"></span><span class="c12">Модель</span></div>'
            f'<div class="obody"><div class="scrollbox" style="width:{contw}px;flex:1;min-height:0;overflow:hidden">'
            '<div class="h2">Выберите модель распознавания</div><div class="sm" style="margin:4px 0 14px">Отметьте, что '
            'скачать. Рекомендуем русскую GigaAM: она расставляет знаки препинания сама. Загрузка начнётся, когда '
            f'нажмёте «Продолжить».</div>{head}{"".join(cards)}</div></div>'
            '<div class="obar"><span class="btn">Назад</span><span class="c12">Будет скачано 226 МБ · свободно на диске 42,1 ГБ</span>'
            '<span class="sp"></span><span class="btn gh dis">Пропустить</span><span class="btn pri">Продолжить</span></div></div>')


# ── галерея состояний ───────────────────────────────────────────────────────
GALLERY = [
    ("рекомендована, не установлена", "avail", "rnnt", dict(rec=True)),
    ("выбрана к загрузке", "selected", "rnnt", dict(rec=True)),
    ("наведена мышью", "hover", "ctc", {}),
    ("фокус с клавиатуры (Пробел — отметка)", "focus", "ctc", {}),
    ("установлена и активна · скорость и память замерены здесь (маркер ✓)", "active", "rnnt",
     dict(rec=True, meas_speed=38.7, meas_ram=402)),
    ("установлена, не рабочая", "installed", "ctc", {}),
    ("установлена, есть обновление", "update", "ctc", {}),
    ("загружается (ход — в сквозной полоске)", "downloading", "rnntnp", {}),
    ("в очереди", "queued", "tone", {}),
    ("проверяю модель", "verifying", "tone", {}),
    ("переключаю рабочую модель", "switching", "ctc", {}),
    ("не получилось загрузить", "failed", "rnnt", dict(rec=True)),
    ("не хватает места — выбрать нельзя", "nospace", "mll", {}),
    ("мало памяти — предупреждение, не запрет", "lowram", "wturbo", {}),
    ("запрещено администратором", "policy", "vosk", {}),
    ("нет цифр — полоски пунктиром", "avail", "wsmall", {}),
    ("файлы модели не читаются", "broken", "ctc", {}),
    ("снята с каталога", "removed", "ctc", {}),
    ("новая в каталоге", "new", "ml", {}),
    ("слабые цифры — красная шкала", "avail", "vosks", {}),
]
CODES = {"update": "update-available", "nospace": "no-space", "lowram": "low-ram", "policy": "policy-offline",
         "broken": "corrupted", "removed": "removed-from-catalog", "avail": "available"}


def gallery(variant, width):
    card = card_a if variant == "A" else card_b
    out = []
    for title, code, key, kw in GALLERY:
        c = card(key, code, **kw)
        head = lhead_b("") if variant == "B" else ""
        out.append(f'<div class="stc"><div class="stn">{title}<span class="code">{CODES.get(code, code)}</span>'
                   f'<span class="hh" data-h></span></div><div class="gcol" style="width:{width}px">{head}{c}</div></div>')
    return '<div class="gal">' + "".join(out) + "</div>"


JS = """
<script>
(function(){
  function cards(root){return Array.prototype.slice.call(root.querySelectorAll('.card'));}
  document.querySelectorAll('.stc').forEach(function(s){
    var c=s.querySelector('.card'), h=s.querySelector('[data-h]'); if(!c||!h) return;
    h.textContent='высота '+Math.round(c.getBoundingClientRect().height)+' px';
  });
  document.querySelectorAll('[data-cmp]').forEach(function(el){
    var o=el.querySelector('.old .mc'), n=el.querySelector('.card');
    var ho=Math.round(o.getBoundingClientRect().height), hn=Math.round(n.getBoundingClientRect().height);
    el.querySelector('[data-res]').textContent='было '+ho+' px → стало '+hn+' px ('+Math.round((hn-ho)/ho*100)+' %)';
  });
  document.querySelectorAll('.wblock').forEach(function(b){
    var box=b.querySelector('.scrollbox'); var r=box.getBoundingClientRect(); var n=0;
    cards(box).forEach(function(c){var cr=c.getBoundingClientRect(); if(cr.bottom<=r.bottom+0.5 && cr.height>0) n++;});
    b.querySelector('.vis').textContent=n;
  });
})();
</script>
"""


def page(variant, theme):
    dark = theme == "dark"
    tok = TOK_DARK if dark else TOK_LIGHT
    vcss = A_CSS if variant == "A" else B_CSS
    title = {"A": "Компактная карточка — A «Три строки»", "B": "Компактная карточка — B «Строка-таблица»"}[variant]
    other = "B" if variant == "A" else "A"
    th = "тёмная" if dark else "светлая"
    th_link = f'card-compact-{variant}{"" if dark else "-dark"}.html'
    card = card_a if variant == "A" else card_b

    if variant == "A":
        what = ("<b>A «Три строки».</b> Структура прежняя (отметка · имя и назначение · метрики справа), но: "
                "нет разделителя и отдельной строки «Занимает места» — размер, память и теги слились в одну нижнюю "
                "строку; подписи источника цифр нет; полоски 60×8 цветные; значения — цветом <b>fg-secondary</b>, "
                "а не бледным fg-muted. Вся информация карточки сохранена.")
    else:
        what = ("<b>B «Строка-таблица».</b> Подписи «Точность» и «Скорость» вынесены в заголовок столбцов — "
                "один раз на группу, в той же строке, что «Установленные · 2», поэтому высоты не добавляют. "
                "Карточка — две строки: имя + столбцы метрик; назначение + размер и память. Теги «Только русский», "
                "«с пунктуацией» и лицензия с экрана уходят (язык и пунктуацию уже называет описание модели), "
                "«отечественная» переезжает в строку с размером и памятью. Бейдж состояния («Установлена», «Загружается»…), сообщения и кнопки — третьей строкой, только когда есть; в шапке остаются только «Рекомендуем» и «Новое».")
    legend = f"""
<div class="legend">{what}<br>
<b>Точность</b> = 100 − WER, одна десятая, больше — лучше; полоска заполнена на столько же процентов.
<b>Скорость</b> — «N× быстрее речи», полоска относительно самой быстрой модели каталога (83,5×).
<b>Цвет полоски — оценка по шкале</b>, длина — сравнение:
<table><tr><th></th><th>хорошо — <code>successInk</code></th><th>средне — <code>warningInk</code></th><th>слабо — <code>dangerInk</code></th></tr>
<tr><td>Точность</td><td>≥ 92,0 %</td><td>88,0–91,9 %</td><td>&lt; 88,0 %</td></tr>
<tr><td>Скорость</td><td>≥ 20× (фраза 10 с готова за 0,5 с)</td><td>5–19,9× (до 2 с)</td><td>&lt; 5× (заметное ожидание)</td></tr></table>
<b>Замерено на этом компьютере</b> — та же полоска, но значение цветом <code>fg</code>, вес 500 и маркер ✓ цвета
<code>primary</code> (подсказка при наведении «замерено на этом компьютере»). Слова «замерено» на экране нет.
Нет цифр — пунктирная дорожка и «нет данных». Ни сноски про источник, ни ссылки «Как мы считаем» на экране нет.
&nbsp;·&nbsp; <a href="{th_link}" style="color:var(--primary)">другая тема</a>
&nbsp;·&nbsp; <a href="card-compact-{other}{'-dark' if dark else ''}.html" style="color:var(--primary)">вариант {other}</a>
&nbsp;·&nbsp; <a href="index.html" style="color:var(--primary)">оглавление</a></div>"""

    cmp_blocks = []
    for w in (672, 796):
        cmp_blocks.append(
            f'<div class="stc" data-cmp><div class="stn">было → стало, колонка {w} px'
            f'<span class="hh" data-res></span></div><div style="width:{w}px">{card_old("rnnt")}'
            f'{lhead_b("") if variant == "B" else ""}{card("rnnt", "avail", rec=True)}</div></div>')
        cmp_blocks.append(
            f'<div class="stc" data-cmp><div class="stn">было → стало, установленная, колонка {w} px'
            f'<span class="hh" data-res></span></div><div style="width:{w}px">{card_old("ctc", True)}'
            f'{card("ctc", "installed")}</div></div>')

    wins = []
    for w, strip, colw, note in ((900, False, 672, "900×620 (клиент 588) — как сейчас"),
                                 (900, True, 672, "900×620 + сквозная полоска загрузки — худший случай"),
                                 (988, False, 760, "988×620 — сегодняшний предел полезной ширины"),
                                 (1024, False, 796, "1024×620 — предложение")):
        wins.append(f'<div class="wblock"><div class="wcap"><b>«Модели»</b> · {note} · видно целиком карточек: '
                    f'<span class="vis">?</span></div>{win_models(variant, w, strip, colw)}</div>')
    wiz = []
    for w, cw, note in ((900, 720, "900×620 — как сейчас, колонка 720"), (1024, 796, "1024×620 — предложение, колонка 796")):
        wiz.append(f'<div class="wblock"><div class="wcap"><b>Мастер, шаг 2</b> · {note} · видно целиком карточек: '
                   f'<span class="vis">?</span></div>{win_wizard(variant, w, cw)}</div>')

    body = (f'<div class="cap"><b>{title}</b> · {th} тема · предложение к замечаниям заказчика 23.09 (не финальный макет)</div>'
            + legend
            + '<div class="sech">Было → стало</div><div class="cmp">' + "".join(cmp_blocks) + "</div>"
            + '<div class="sech">Раздел «Модели»: сколько карточек видно без прокрутки</div>'
            + '<div class="row">' + "".join(wins) + "</div>"
            + '<div class="sech">Мастер первого запуска, шаг 2</div><div class="row">' + "".join(wiz) + "</div>"
            + '<div class="sech">Состояния карточки · колонка 672 (окно 900)</div>' + gallery(variant, 672)
            + '<div class="sech">Те же состояния · колонка 796 (окно 1024)</div>' + gallery(variant, 796))
    return (f'<!doctype html>\n<html lang="ru"><head><meta charset="utf-8">\n<title>{html.escape(title)} — {th}</title>\n'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">\n'
            f'<style>\n:root{{{tok}}}\n{BASE_CSS}\n{OLD_CSS}\n{vcss}\n</style></head>\n'
            f'<body class="{"dark" if dark else "light"}">\n{body}\n{JS}</body></html>\n')


INDEX = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><title>Компактная карточка модели — предложения</title>
<style>:root{--bg:#F7F9FC;--fg:#0E1729;--mut:#5A6884;--pri:#1B3A73;--bd:#D7DEEA}
@media (prefers-color-scheme:dark){:root{--bg:#0B1220;--fg:#F2F5FA;--mut:#8C97AC;--pri:#6C93E8;--bd:rgba(255,255,255,.1)}}
body{margin:0;padding:28px 20px;background:var(--bg);color:var(--fg);font-family:'PT Root UI','PT Astra Sans',sans-serif;font-size:14px;line-height:1.55}
main{max-width:760px;margin:0 auto}a{color:var(--pri)}li{margin:4px 0}.m{color:var(--mut);font-size:13px}
table{border-collapse:collapse;font-size:13px}td,th{border-bottom:1px solid var(--bd);padding:5px 12px 5px 0;text-align:left}</style></head>
<body><main><h2 style="margin:0 0 6px">Компактная карточка модели — два направления</h2>
<p class="m">Ответ на замечания заказчика 23.09: карточка слишком высокая, всё бледное, «WER» непонятен.
Не финальные макеты — к выбору направления.</p>
<ul>
<li><b>A «Три строки»</b> — <a href="card-compact-A.html">светлая</a> · <a href="card-compact-A-dark.html">тёмная</a>.
Прежняя раскладка без разделителя и строки источника; всё содержимое сохранено.</li>
<li><b>B «Строка-таблица»</b> — <a href="card-compact-B.html">светлая</a> · <a href="card-compact-B-dark.html">тёмная</a>.
Подписи метрик — заголовком столбцов один раз на группу, две строки на карточку; теги языка и лицензии уходят с экрана.</li>
</ul>
<p class="m">Высоты карточек и число видимых карточек на страницах макетов считает скрипт в самой странице
(поле «высота …» у каждого состояния и «видно целиком карточек» у каждого окна).</p>
</main></body></html>
"""


def main():
    for v in ("A", "B"):
        for t in ("light", "dark"):
            name = f'card-compact-{v}{"-dark" if t == "dark" else ""}.html'
            (OUT / name).write_text(page(v, t), encoding="utf-8")
    (OUT / "index.html").write_text(INDEX, encoding="utf-8")


if __name__ == "__main__":
    main()
