---
name: kde-icon-name-dash-fallback
description: Fly's start button icon is the theme name astra-simplified (a star); KDE dash-truncation makes unknown names like astra-cowork silently fall back to astra
metadata:
  type: project
---

The Plasma Kickoff (start) button icon lives in `[Containments][2][Applets][21][Configuration][General]` of `~/.config/plasma-org.kde.plasma.desktop-appletsrc`. As of 2026-09-21 it is set to `icon=astra-simplified` — the stock Fly start-menu star, per `/usr/share/fly-wm/theme/default.themerc:88` (`StartButtonIcon = astra-simplified`) and `/usr/share/fly-wm/toolbar/start.desktop`. It resolves from the active `astra-proxima` theme (which inherits fly-astra-flat → fly-astra).

Earlier that session the button carried Astra Cowork logos. Those icons are still installed under `~/.local/share/icons/hicolor/{16..512}/apps/` and `scalable/apps/`: `astra-cowork` (Galaxy G3), `astra-cowork-c3` (bare C3 contour mark), `astra-cowork-c3-badge` (C3 on a navy disc — the variant that was on the panel). The C3 SVGs were generated from the project's own polygons (`morph_logo._polys_at(0.0)`, `_CYAN=#38BDF8`) using the project venv. To put one back, set `icon=` to its absolute SVG path.

**Why the absolute path:** the theme name `astra-cowork` does not work. KIconLoader tries each theme in order and truncates at the last dash as a fallback, so `astra-cowork` silently becomes `astra` — the blue Astra star emblem in fly-astra-flat, searched before hicolor. `kiconfinder5 astra-cowork` shows this.

**How to apply:** For anything on Astra Linux referencing a logo by icon name (the project's `.desktop` files use `Icon=astra-cowork`), pick a dash-free name, install into the active theme, or use an absolute path. Change Plasma applet config through `qdbus org.kde.plasmashell /PlasmaShell evaluateScript`, never by editing the file while plasmashell runs. Verify with `spectacle -b -n -o file.png` plus a bottom-left crop. Related: [[astra-linux-plasma-environment]].
