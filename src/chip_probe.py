"""Asynchronous, read-only chip/MAC probing for an adapter that can stay online."""

from __future__ import annotations

import json
import re

from PyQt6.QtCore import QObject, QProcess, QProcessEnvironment, QTimer, pyqtSignal

from .auto_flash import AUTO_DETECT_TIMEOUT_ENV
from .constants import _build_tool_worker_command, _inject_local_esptool_pythonpath, _INTERNAL_WORKER_EVENT_PREFIX, TOOL_DIR
from .flash_timing import normalize_mac


def readable_output(output: str) -> str:
    lines = []
    for line in output.splitlines():
        if line.startswith(_INTERNAL_WORKER_EVENT_PREFIX):
            try:
                event = json.loads(line[len(_INTERNAL_WORKER_EVENT_PREFIX):])
                line = str(event.get("text", "")) if isinstance(event, dict) else ""
            except ValueError:
                continue
        lines.append(line)
    return "\n".join(lines)


def chip_information(output: str) -> dict:
    text = readable_output(output)
    mac = re.search(r"\b(?:BASE\s+)?MAC:\s*((?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2})", text, re.I)
    chip = re.search(r"(?:Chip (?:type|is):?\s*|Connected to\s+)(ESP[\w-]+[^\n:]*)", text, re.I)
    features = re.search(r"Features:\s*(.+)", text, re.I)
    return {"mac": normalize_mac(mac.group(1)) if mac else "",
            "chip": chip.group(1).strip() if chip else "",
            "features": features.group(1).strip() if features else ""}


def classify_probe(output: str, exit_code: int) -> dict:
    info = chip_information(output)
    text = readable_output(output)
    if info["mac"]:
        return {**info, "status": "present", "message": "已读取芯片 MAC"}
    if re.search(r"access is denied|permissionerror|permission denied|in use|被占用|拒绝访问|winerror\s*5", text, re.I):
        return {**info, "status": "busy", "message": "串口被占用，尚未确认芯片连接状态"}
    if re.search(r"failed to connect|no serial data|no response|检测超时|serial data stream stopped|cannot find the file|系统找不到指定", text, re.I):
        return {**info, "status": "absent", "message": "未读取到芯片信息"}
    return {**info, "status": "error", "message": f"芯片信息读取失败（{exit_code}），等待重检"}


class ChipInfoProbe(QObject):
    finished = pyqtSignal(dict)

    def __init__(self, port: str, registry, parent=None):
        super().__init__(parent)
        self.port, self.registry = port, registry
        self.process: QProcess | None = None
        self.lease = None
        self.output = bytearray()
        self.done = False
        self.canceled = False
        self.timed_out = False
        self.watchdog = QTimer(self)
        self.watchdog.setSingleShot(True)
        self.watchdog.timeout.connect(self._timeout)

    def start(self):
        self.lease = self.registry.acquire(self.port, f"chip-probe:{id(self)}", "读取芯片信息")
        if self.lease is None:
            QTimer.singleShot(0, lambda: self._deliver({"status": "busy", "message": "串口正由其他任务使用，稍后重检", "mac": ""}))
            return
        command = _build_tool_worker_command("esptool", "--chip", "auto", "--port", self.port,
                                             "--baud", "115200", "--no-stub", "--before", "default-reset",
                                             "--after", "hard-reset", "read-mac")
        process = QProcess(self)
        self.process = process
        process.setProgram(command[0])
        process.setArguments(command[1:])
        process.setWorkingDirectory(str(TOOL_DIR))
        process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        env = QProcessEnvironment.systemEnvironment()
        _inject_local_esptool_pythonpath(env)
        env.insert("PYTHONUTF8", "1")
        env.insert(AUTO_DETECT_TIMEOUT_ENV, "3")
        process.setProcessEnvironment(env)
        process.readyRead.connect(self._read)
        process.finished.connect(self._finished)
        process.errorOccurred.connect(self._error)
        self.watchdog.start(5000)
        process.start()

    def _read(self):
        if self.process is not None:
            self.output.extend(bytes(self.process.readAll()))
            if len(self.output) > 131072:
                del self.output[:-131072]

    def _finished(self, code, status):
        self._read()
        if self.canceled:
            self._deliver({"status": "canceled", "message": "检测已取消", "mac": ""})
        elif self.timed_out:
            self._deliver({"status": "error", "message": "读取进程超时，尚未确认芯片离线", "mac": ""})
        else:
            self._deliver(classify_probe(self.output.decode("utf-8", "replace"), code))

    def _error(self, error):
        if error == QProcess.ProcessError.FailedToStart:
            self._deliver({"status": "error", "message": "芯片检测进程启动失败", "mac": ""})

    def _timeout(self):
        self.timed_out = True
        if self.process is not None:
            self.process.kill()

    def _deliver(self, result: dict):
        if self.done:
            return
        self.done = True
        self.watchdog.stop()
        if self.lease is not None:
            self.lease.release()
            self.lease = None
        self.finished.emit({**result, "port": self.port})
        self.deleteLater()

    def cancel(self):
        self.canceled = True
        self.watchdog.stop()
        if self.process is not None and self.process.state() != QProcess.ProcessState.NotRunning:
            self.process.kill()
            return self.process.waitForFinished(1000)
        self._deliver({"status": "canceled", "message": "检测已取消", "mac": ""})
        return True
