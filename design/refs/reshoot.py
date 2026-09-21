#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Съёмка референсных PNG для design/refs — ровно по design/refs/README.md.

Зачем скрипт в репозитории: раньше он жил в рабочем окружении агента, README на него
только ссылался, и восстановить механизм по памяти было нельзя. Механизм обязан быть
воспроизводимым: расхождение в браузере, шрифтах или масштабе даёт дизайн-ревью ложные Δ.

ЗАПУСК (из корня репозитория, каталог значения не имеет — пути берутся от файла скрипта):

    python3 design/refs/reshoot.py                     переснять все 50 экранов
    python3 design/refs/reshoot.py 09-pill 09-tray     только названные (имя без .html/.png)
    python3 design/refs/reshoot.py --check             НИЧЕГО НЕ ПИШЕТ: сверка
    python3 design/refs/reshoot.py --check-head        контроль механизма (см. ниже)
    python3 design/refs/reshoot.py --out /tmp/кадры    писать не в design/refs, а сюда
    python3 design/refs/reshoot.py -j 8                число параллельных снимков (по умолчанию 4)

ЧТО ПИШЕТ. Без флагов — заменяет `design/refs/<экран>.png` (или пишет в `--out`). Кадр,
совпавший с уже лежащим побайтово, помечается «=» и не перезаписывается, чтобы не плодить
пустых изменений в git. Промежуточные снимки и профили chromium уходят во временный каталог
и удаляются. `--check` и `--check-head` не пишут ничего.

ПЕРЕД СЪЁМКОЙ соберите макеты: `python3 design/mockups/final/_build.py` — скрипт снимает то,
что лежит в `design/mockups/final/*.html`, а не то, что в генераторах.

--check: рендерит текущие рабочие `.html` и сверяет с лежащими PNG. Отвечает на вопрос
«референсы отстали от макетов?». После правки макетов ожидаемо покажет расхождения.

--check-head: КОНТРОЛЬ МЕХАНИЗМА, обязателен перед заменой кадров. Берёт версию каждого
`.html` из `HEAD` (`git show`, только чтение, ничего не коммитит), снимает её и сверяет с
лежащим PNG. Смысл: на экранах, которые вы не трогали, HEAD-версия обязана воспроизвестись
ПОБАЙТОВО. Сошлось — вы снимаете тем же инструментом и в том же масштабе, и можно верить
собственным новым кадрам. Не сошлось — другой chromium, другие шрифты или масштаб, снимать
нельзя. Расхождения на протухших кадрах (см. README) — не отказ механизма, а именно то
протухание; поэтому вывод делит расхождения и совпадения, а не просто падает.

ЗАВИСИМОСТИ: системный `chromium` и Pillow (`python3-pil`). Больше ничего не нужно.

ПАРАМЕТРЫ СЪЁМКИ (менять нельзя, иначе кадры перестанут сравниваться между собой):
ширина 951 светлая / 923 тёмная, `--force-device-scale-factor=1`, кроп по «чернилам»
с полями 12 px сверху и 11 px снизу. Обоснование — в README.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor

try:
    from PIL import Image, ImageChops
except ImportError:
    sys.exit("нужен Pillow: sudo apt install python3-pil")

REFS = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(REFS))
MOCKUPS = os.path.join(ROOT, "design", "mockups", "final")

WIDTH = {"light": 951, "dark": 923}
PAD_TOP, PAD_BOTTOM = 12, 11
CANVAS_HEIGHT = 6000          # заведомо больше самой высокой страницы
BOTTOM_MARGIN = 40            # столько пустоты снизу требуем как доказательство, что влезло
CHROMIUM = os.environ.get("CHROMIUM", "chromium")


def plural(n, one, few, many):
    """«1 экран» / «2 экрана» / «48 экранов» — тексты читает человек."""
    if n % 10 == 1 and n % 100 != 11:
        form = one
    elif n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        form = few
    else:
        form = many
    return "%d %s" % (n, form)


def screens():
    """Все экраны набора: имена .html без расширения, кроме оглавления."""
    return sorted(n[:-5] for n in os.listdir(MOCKUPS)
                  if n.endswith(".html") and n != "index.html")


def width_of(name):
    return WIDTH["dark"] if name.endswith("-dark") else WIDTH["light"]


def render(html_path, name, workdir):
    """Снимок страницы и кроп по «чернилам». Возвращает PIL.Image."""
    raw = os.path.join(workdir, "raw.png")
    width = width_of(name)
    proc = subprocess.run([
        CHROMIUM, "--headless=new", "--no-sandbox", "--disable-gpu", "--hide-scrollbars",
        "--force-device-scale-factor=1", "--virtual-time-budget=5000",
        "--user-data-dir=" + os.path.join(workdir, "profile"),
        "--window-size=%d,%d" % (width, CANVAS_HEIGHT),
        "--screenshot=" + raw,
        "file://" + html_path,
    ], capture_output=True)
    if not os.path.exists(raw):
        raise RuntimeError("chromium не отдал кадр: " + proc.stderr.decode("utf-8", "replace")[-300:])

    im = Image.open(raw).convert("RGB")
    background = im.getpixel((2, 2))
    ink = ImageChops.difference(im, Image.new("RGB", im.size, background)).getbbox()
    if ink is None:
        raise RuntimeError("пустой кадр — страница не отрисовалась")
    _, top_ink, _, bottom_ink = ink
    if bottom_ink > im.size[1] - BOTTOM_MARGIN:
        raise RuntimeError("страница не поместилась в %d px (чернила до %d) — поднимите CANVAS_HEIGHT"
                           % (CANVAS_HEIGHT, bottom_ink))
    top = max(0, top_ink - PAD_TOP)
    bottom = min(im.size[1], bottom_ink + PAD_BOTTOM)
    return im.crop((0, top, im.size[0], bottom))


def html_from_head(name, workdir):
    """Версия макета из HEAD. Только чтение репозитория, ничего не меняет."""
    path = "design/mockups/final/%s.html" % name
    proc = subprocess.run(["git", "-C", ROOT, "show", "HEAD:" + path], capture_output=True)
    if proc.returncode != 0:
        raise RuntimeError("git show HEAD:%s — %s" % (path, proc.stderr.decode("utf-8", "replace").strip()))
    out = os.path.join(workdir, name + ".html")
    with open(out, "wb") as f:
        f.write(proc.stdout)
    return out


def compare(image, png_path):
    """('нет', ...) если файла нет; ('=', ...) если побайтово совпало; иначе описание разницы."""
    if not os.path.exists(png_path):
        return "нет", "референса нет"
    old = Image.open(png_path).convert("RGB")
    if old.size != image.size:
        return "≠", "размер %dx%d -> %dx%d" % (old.size + image.size)
    diff = ImageChops.difference(old, image)
    box = diff.getbbox()
    if box is None:
        return "=", "%dx%d" % image.size
    pixels = sum(diff.convert("L").histogram()[1:])
    return "≠", "размер тот же %dx%d, различий %d пикс, полоса y %d..%d" % (
        image.size + (pixels, box[1], box[3] - 1))


def one(job):
    name, mode, out_dir = job
    workdir = tempfile.mkdtemp(prefix="reshoot-")
    try:
        if mode == "check-head":
            html = html_from_head(name, workdir)
        else:
            html = os.path.join(MOCKUPS, name + ".html")
            if not os.path.exists(html):
                return name, "!", "нет макета %s.html — соберите _build.py" % name
        image = render(html, name, workdir)
        target = os.path.join(out_dir, name + ".png")
        mark, note = compare(image, target)
        if mode == "write" and mark != "=":
            image.save(target)
            note = note + " — записан"
        return name, mark, note
    except Exception as exc:                                   # noqa: BLE001 — отчитываемся, не падаем
        return name, "!", str(exc)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser(add_help=True, description="съёмка референсов design/refs")
    ap.add_argument("names", nargs="*", help="имена экранов без расширения; пусто — все")
    ap.add_argument("--check", action="store_true", help="не писать: сверить PNG с рабочими макетами")
    ap.add_argument("--check-head", action="store_true", help="контроль механизма: HEAD-версия против PNG")
    ap.add_argument("--out", default=REFS, help="куда писать (по умолчанию design/refs)")
    ap.add_argument("-j", type=int, default=4, help="параллельных снимков, по умолчанию 4")
    args = ap.parse_args()

    if args.check and args.check_head:
        sys.exit("--check и --check-head вместе не имеют смысла")
    mode = "check-head" if args.check_head else ("check" if args.check else "write")

    all_names = screens()
    names = args.names or all_names
    unknown = [n for n in names if n not in all_names]
    if unknown:
        sys.exit("нет таких экранов: " + ", ".join(unknown))
    os.makedirs(args.out, exist_ok=True)

    title = {"write": "съёмка", "check": "сверка с рабочими макетами",
             "check-head": "контроль механизма (HEAD-версия макетов)"}[mode]
    print("%s: %s, ширина 951/923, масштаб 1:1\n" % (title, plural(len(names), "экран", "экрана", "экранов")))

    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.j)) as pool:
        for name, mark, note in pool.map(one, [(n, mode, args.out) for n in names]):
            results.append((name, mark, note))
            if mark != "=" or mode == "write":
                print("  %s %-30s %s" % (mark, name, note))

    same = sum(1 for _, m, _ in results if m == "=")
    written = sum(1 for _, m, _ in results if m != "=" and m != "!")
    fail = sum(1 for _, m, _ in results if m == "!")
    if mode == "write":
        parts = ["записано %d" % written, "без изменений %d" % same]
    else:
        parts = ["совпало побайтово: %d из %d" % (same, len(results))]
        if written:
            parts.append("расходится %d" % written)
    if fail:
        parts.append("ошибок %d" % fail)
    print("\n" + ", ".join(parts))

    if mode == "check-head":
        if fail:
            print("ОШИБКИ СЪЁМКИ — механизм не проверен")
        elif written:
            print("Расхождения допустимы ТОЛЬКО на протухших кадрах из README.")
            print("Если расходится кадр, который вы не трогали, — у вас другой chromium,")
            print("другие шрифты или другой масштаб. Снимать нельзя: ревью получит ложные Δ.")
        else:
            print("Механизм подтверждён: все кадры воспроизведены побайтово.")
    if mode == "write":
        return 1 if fail else 0
    # сверка: расхождение — это и есть ответ «референсы отстали», код возврата должен его нести
    return 1 if (fail or written) else 0


if __name__ == "__main__":
    sys.exit(main())
