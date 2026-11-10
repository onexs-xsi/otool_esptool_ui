"""Automatic flashing policy and worker-only detection deadline."""

import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps

AUTO_DETECT_TIMEOUT_SECONDS = 3
AUTO_RETRY_DELAY_MS = 10_000
AUTO_DETECT_TIMEOUT_ENV = "OTOOL_AUTO_DETECT_TIMEOUT_SECONDS"
AUTO_REMOVAL_POLL_INTERVAL_MS = 3_000


@dataclass
class AutoFlashCycle:
    device_id: str
    port: str
    identity_key: str
    waiting_for_removal: bool = True
    checks: int = 0
    stopped: bool = False
    mac: str = ""
    next_mac: str = ""
    absent_checks: int = 0


@contextmanager
def detection_deadline(module, seconds, emit):
    """Bound esptool's connection/detection call, never erase/write/verify.

    This runs only in the disposable worker. Closing that process also closes
    a stuck serial handle. The lock prevents an expired timer from killing a
    worker after connection returned and flash operations have started.
    """
    original = module.connect_esp

    @wraps(original)
    def connect(*args, **kwargs):
        lock = threading.Lock()
        detecting = True

        def expire():
            with lock:
                if detecting:
                    try:
                        emit({"type": "fatal", "text": f"设备检测超时（{seconds:g} 秒），本次连接已结束。"})
                    finally:
                        os._exit(1)

        timer = threading.Timer(seconds, expire)
        timer.daemon = True
        emit({"type": "log", "text": f"[自动烧录] 设备检测限时 {seconds:g} 秒"})
        timer.start()
        try:
            return original(*args, **kwargs)
        finally:
            with lock:
                detecting = False
                timer.cancel()

    module.connect_esp = connect
    try:
        yield
    finally:
        module.connect_esp = original
