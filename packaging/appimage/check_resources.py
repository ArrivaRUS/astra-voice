"""Смоук ресурсов AppDir — те же проверки, что у .deb (packaging/smoke-installed.sh).

Запуск: python3.11 -I check_resources.py <AppDir>/usr/lib/astra-voice
Без ASTRA_VOICE_RESOURCES: корень ресурсов находит сам код по маркеру сборки
(`paths.bundle_root()`), так же как в работающей копии. Окон не создаёт, Qt не
инициализирует.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(sys.argv[1]).resolve()))

from astra_voice.core import paths  # noqa: E402
from astra_voice.core.model_source import smoke_wav_path  # noqa: E402
from astra_voice.platform.session import SessionKind  # noqa: E402
from astra_voice.ui.tray_icons import (  # noqa: E402
    TrayIconProvider,
    TrayState,
    find_tray_icon_path,
)

checked = 0
failures: list[str] = []
for kind in (SessionKind.FLY, SessionKind.KDE):
    provider = TrayIconProvider(kind)
    for state in TrayState:
        for size in (16, 22):
            checked += 1
            try:
                find_tray_icon_path(provider.icon_name(state), size)
            except FileNotFoundError as error:
                failures.append(str(error))

bundle = paths.bundle_root()
if bundle is None or not paths.resource_root().is_relative_to(bundle):
    failures.append(f"Корень ресурсов не в бандле: {paths.resource_root()}")

for path in (
    paths.qml_dir() / "Pill.qml",
    paths.qml_dir() / "Main.qml",
    paths.data_dir_static() / "vad" / "silero_vad.onnx",
    paths.data_dir_static() / "catalog.json",
    paths.data_dir_static() / "catalog.json.sig",
    paths.data_dir_static() / "catalog.schema.json",
    smoke_wav_path(),
    paths.resource_root() / "docs" / "NOTICE",
    paths.resource_root() / "docs" / "PRIVACY.md",
):
    checked += 1
    if not path.is_file():
        failures.append(f"Нет файла ресурса: {path}")

print(f"Ресурсы: проверено {checked}, ошибок {len(failures)}", flush=True)
for error in failures:
    print(error, file=sys.stderr)
sys.exit(1 if failures else 0)
