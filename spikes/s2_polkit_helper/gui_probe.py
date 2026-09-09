#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""S2: минимальный GUI-зонд — QProcess → pkexec → update-helper.

Критерий спайка (docs/plans.md M0.S2): из GUI-процесса ровно ОДНО окно ввода
пароля polkit, коды 0 / 126 / 127 различимы, GUI не блокируется, dpkg-query
показывает 0.0.2.

ВНИМАНИЕ (урок .patches/002): скрипт трогает D-Bus (агент polkit) и открывает
окно на реальном дисплее. Запускать ТОЛЬКО по команде Юрки в живой сессии
заказчика, после того как заказчик выполнил install-spike.sh.

Запуск:
    python3 gui_probe.py [путь к .deb]
по умолчанию — packages/astra-voice-spike_0.0.2_all.deb рядом со скриптом.
"""

import os
import sys

from PyQt5.QtCore import QProcess, QTimer
from PyQt5.QtWidgets import QApplication, QLabel, QPushButton, QPlainTextEdit, QVBoxLayout, QWidget

HELPER = "/usr/libexec/astra-voice/update-helper"
DEFAULT_DEB = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "packages", "astra-voice-spike_0.0.2_all.deb")

EXIT_HINTS = {
    0: "0 — помощник отработал (см. JSON в stdout)",
    126: "126 — pkexec: авторизация не получена (диалог закрыт/отменён)",
    127: "127 — pkexec: не удалось запустить программу (нет действия/агента/пути)",
}


class Probe(QWidget):
    def __init__(self, deb_path):
        super().__init__()
        self.deb_path = deb_path
        self.setWindowTitle("S2 polkit probe")
        self.resize(720, 420)

        self.status = QLabel("Готов. Ожидается ровно одно окно пароля polkit.")
        self.ticker = QLabel("GUI жив: 0")
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.button = QPushButton("pkexec update-helper install …")
        self.button.clicked.connect(self.start)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("deb: " + deb_path))
        layout.addWidget(self.button)
        layout.addWidget(self.status)
        layout.addWidget(self.ticker)
        layout.addWidget(self.log)

        # доказательство «GUI не блокируется» во время диалога polkit
        self.ticks = 0
        timer = QTimer(self)
        timer.timeout.connect(self.tick)
        timer.start(200)

        self.proc = None

    def tick(self):
        self.ticks += 1
        self.ticker.setText("GUI жив: %d" % self.ticks)

    def start(self):
        self.button.setEnabled(False)
        self.status.setText("Запущен pkexec…")
        self.proc = QProcess(self)
        self.proc.setProgram("pkexec")
        self.proc.setArguments([HELPER, "install", self.deb_path])
        self.proc.readyReadStandardOutput.connect(self.on_stdout)
        self.proc.readyReadStandardError.connect(self.on_stderr)
        self.proc.finished.connect(self.on_finished)
        self.proc.start()

    def on_stdout(self):
        self.append("stdout", bytes(self.proc.readAllStandardOutput()))

    def on_stderr(self):
        self.append("stderr", bytes(self.proc.readAllStandardError()))

    def append(self, tag, data):
        text = data.decode("utf-8", "replace").rstrip()
        if text:
            self.log.appendPlainText("[%s] %s" % (tag, text))
            # дубль в консоль: JSON помощника нужен как артефакт прогона
            print("[%s] %s" % (tag, text), flush=True)

    def on_finished(self, code, _status):
        hint = EXIT_HINTS.get(code, "%d — код помощника (см. README «Коды выхода»)" % code)
        self.status.setText("Код возврата: " + hint)
        self.log.appendPlainText("[exit] %d" % code)
        print("exit=%d  %s" % (code, hint), flush=True)
        self.button.setEnabled(True)


def main():
    deb = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_DEB
    if not os.path.isabs(deb):
        deb = os.path.abspath(deb)
    if not os.path.isfile(deb):
        print("нет файла: %s" % deb, file=sys.stderr)
        return 2
    app = QApplication(sys.argv)
    probe = Probe(deb)
    probe.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
