"""Subprocess fake for transcriber transport/kill tests; never used by production."""

from __future__ import annotations

import os
import signal
import socket
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from astra_voice.worker import ipc
from astra_voice.worker.meeting_diar import DiarReceiver


def main() -> None:
    connection = socket.socket(fileno=int(sys.argv[1]))
    receiver = DiarReceiver()
    mode = sys.argv[2]
    labels: set[str] = set()

    def send(event: dict[str, Any]) -> None:
        connection.sendall(ipc.encode(event, role="meeting-diar"))

    send(ipc.make_hello(role="meeting-diar"))
    while True:
        received = receiver.receive(connection)
        if received is None:
            continue
        raw, fds = received
        message = ipc.decode(raw, role="meeting-diar")
        for fd in fds:
            os.close(fd)
        kind = message["type"]
        reply = {k: message[k] for k in ("req", "request_type", "meeting_id", "meeting_generation")}
        if kind == "diar.load":
            send({"type": "diar.loaded", **reply})
        elif kind == "diar.block":
            index = message["block_index"]
            if mode == "kill-second" and index == 1:
                os.kill(os.getpid(), signal.SIGKILL)
            if mode == "hang":
                send({"type": "meeting.ack", **reply})
                continue  # keep processing control; deliberately never finish a block
            label = f"S{index + 1}"
            labels.add(label)
            send(
                {
                    "type": "diar.block.part",
                    **{k: reply[k] for k in ("meeting_id", "meeting_generation")},
                    "block_index": index,
                    "seq": 0,
                    "last": True,
                    "segments": [
                        [
                            message["offset_ms"],
                            message["offset_ms"] + round(message["block_s"] * 1000),
                            label,
                        ]
                    ],
                }
            )
            send({"type": "diar.block.done", **reply, "block_index": index, "segments": []})
        elif kind == "diar.finish":
            send(
                {
                    "type": "diar.finished",
                    **reply,
                    "label_map": {k: "S1" for k in labels},
                    "speakers_total": int(bool(labels)),
                }
            )
            return
        elif kind == "diar.cancel":
            send({"type": "diar.cancelled", **reply})
            return


if __name__ == "__main__":
    main()
