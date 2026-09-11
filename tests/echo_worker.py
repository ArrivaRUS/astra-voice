"""Лёгкий IPC-воркер: команды тестового управления передаются в transcribe.file.path."""

from __future__ import annotations

import platform
import signal
import socket
import struct
import sys
from pathlib import Path


def main() -> int:
    """Принимает унаследованный fd, шлёт hello и выполняет команды без движка."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
    from astra_voice.worker import ipc

    reader = ipc.FrameReader()
    silent = False
    error_mode: str | None = None
    with socket.socket(fileno=int(sys.argv[1])) as connection:
        connection.settimeout(5)
        connection.sendall(
            ipc.encode(
                {
                    "type": "hello",
                    "protocol": ipc.PROTOCOL_VERSION,
                    "build": "echo",
                    "runtime": {"python": platform.python_version(), "onnxruntime": None},
                }
            )
        )
        while data := connection.recv(ipc.MAX_FRAME_BYTES):
            for message in reader.feed(data):
                if message["type"] == "transcribe.file":
                    command = message["path"]
                    if command == "silence":
                        silent = True
                    elif command == "resume":
                        silent = False
                    elif command == "ignore-term":
                        signal.signal(signal.SIGTERM, signal.SIG_IGN)
                    elif command.startswith("errors:"):
                        error_mode = command.partition(":")[2]
                    elif command == "bad-frame":
                        connection.sendall(
                            struct.pack(">I", 1) + b"{" + ipc.encode({"type": "pong"})
                        )
                    elif command == "oversized":
                        connection.sendall(struct.pack(">I", 2**31))
                    elif command.startswith("result:"):
                        connection.sendall(
                            ipc.encode(
                                {
                                    "type": "result",
                                    "utterance_id": command.partition(":")[2],
                                    "text": "Проверка",
                                    "t_ms": 1,
                                }
                            )
                        )
                    elif command.startswith("cancelled:"):
                        connection.sendall(
                            ipc.encode(
                                {
                                    "type": "cancelled",
                                    "utterance_id": command.partition(":")[2],
                                }
                            )
                        )
                    else:
                        connection.sendall(
                            ipc.encode(
                                {
                                    "type": "result",
                                    "utterance_id": "file",
                                    "text": "Проверка файла",
                                    "t_ms": 1,
                                }
                            )
                        )
                elif message["type"] in ("recognize", "ping") and error_mode is not None:
                    response = ipc.error("engine-failed", "Тестовая ошибка запроса.")
                    if error_mode == "utterance" and "utterance_id" in message:
                        response["utterance_id"] = message["utterance_id"]
                    elif error_mode == "request":
                        response["request_type"] = message["type"]
                    connection.sendall(ipc.encode(response))
                elif message["type"] == "ping" and not silent:
                    connection.sendall(ipc.encode({"type": "pong"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
