---
name: astra-linux-environment
description: The user's machine is Astra Linux 1.8 SE (Debian 12 base) with KDE Plasma on X11; sudo needs a password, so non-interactive sessions cannot use apt, but user-scope Flatpak works; OnlyOffice 9.4.0 is installed that way.
metadata:
  type: project
---

The user's desktop is Astra Linux 1.8 SE x86-64 (Debian bookworm base), KDE Plasma 5 on X11, user `astra` (in astra-admin group). Closed software environment (ЗПС/digsig) is disabled (DIGSIG_ELF_MODE=0), so unsigned binaries run. `sudo` requires a password, so apt/dpkg system installs cannot be completed from a non-interactive Claude session. Flatpak 1.16 is installed and user-scope installs work (bwrap runs, unprivileged user namespaces enabled, ~/.local/share/flatpak exports are already in XDG_DATA_DIRS). Flathub was added as a `--user` remote on 2026-09-09, and OnlyOffice Desktop Editors 9.4.0 (org.onlyoffice.desktopeditors) was installed with it that day; its state lives in ~/.var/app/org.onlyoffice.desktopeditors. libfuse2 is present, so AppImages also work without root. Tools available: xdotool, import (ImageMagick), spectacle, kbuildsycoca5, Xephyr. Not available: strace, Xvfb, coredumpctl; ptrace_scope=3.

The user often works at the machine while a session runs (voice assistant Handy plus their own astra-cowork-daemon from /home/astra/Документы/Astra Cowork). GUI test windows launched on DISPLAY=:0 get closed within seconds, and there is no offscreen X server to test on.

**Why:** Root-only installs stall on the sudo prompt; user-scope Flatpak or AppImage into ~/Applications are the paths that actually complete. Repeated GUI launches on :0 disturb the user and yield ambiguous results.

**How to apply:** When asked to install desktop software, prefer `flatpak install --user flathub <app>` (or an AppImage) instead of apt. If a .deb is the only option, prepare it and give the user the sudo command to run themselves. Verify GUI apps with at most one short launch on :0 and rely on logs/journalctl rather than repeated launches.
