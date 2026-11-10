"""Host-only lease probe: production transcriber/diar, fake ASR, pause before load."""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

if __name__ == "__main__":
    root = Path(__file__).resolve().parents[2]
    sys.path.insert(0, str(root / "tests"))
    sys.path.insert(0, str(root / "src"))

from astra_voice.meetings.store import MeetingStore
from astra_voice.worker.meeting_diar_client import DiarProcess
from helpers.meeting_audio import MID, Rig


class PublishedDiar(DiarProcess):
    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        # Parent stdout only. The genuine hardened role never writes any file.
        print(self.process.pid, flush=True)
        sys.stdin.buffer.read(1)  # outer test freezes child and kills this parent


def main() -> None:
    disk = MeetingStore(Path(sys.argv[1]))
    rig = Rig(disk)
    rig.state.diar_factory = PublishedDiar
    rig.load()
    rig.send(
        "meeting.job.start",
        **rig.identity(),
        dir=str(disk.root / MID),
        tracks=["mic"],
        variant="mic_only",
        diarization=True,
        diar_models={
            "segmentation_dir": "/unused/segmentation",
            "embedder_dir": "/unused/embedder",
        },
        self_track="none",
        resume=False,
        echo_gate=False,
        limits={"max_ms": 14_400_000, "max_utterances": 9000, "max_blocks": 12},
    )
    rig.credit(64)
    while True:
        rig.pump()
        time.sleep(0.01)


if __name__ == "__main__":
    main()
