"""Synthetic flash records used only by tests and screenshot previews."""

from datetime import datetime, timedelta

from src.flash_timing import CST


def sample_history() -> list[dict]:
    """View-only sample data. Never inserted into the user's history store."""
    now = datetime.now(CST)
    rows = []
    examples = [
        ("ESP32-P4", "02:00:00:ed:64:26", "COM34", "success", [0.28, 1.32, 0.84, 8.24, 1.18, 0.32, 0.08]),
        ("ESP32-S3", "02:00:00:18:0a:92", "COM18", "success", [0.31, 1.08, 0.62, 5.68, 0.84, 0.24, 0.06]),
        ("ESP32-C3", "02:00:00:a4:39:07", "COM7", "failed", [0.26, 1.42, 0.55, 2.71, 0.0, 0.0, 0.05]),
        ("ESP32-P4", "02:00:00:ed:64:26", "COM34", "success", [0.25, 1.48, 0.77, 8.58, 1.22, 0.29, 0.07]),
        ("ESP32-S3", "02:00:00:55:27:8e", "COM16", "success", [0.30, 1.15, 0.68, 6.42, 0.92, 0.25, 0.06]),
        ("ESP32-C6", "02:00:00:43:91:bb", "COM9", "success", [0.27, 1.21, 0.51, 4.82, 0.71, 0.22, 0.05]),
        ("ESP32-S3", "02:00:00:91:2c:18", "COM22", "running", [0.29, 1.18, 0.64, 3.22, 0.0, 0.0, 0.0]),
    ]
    for i, (chip, mac, port, status, durations) in enumerate(examples):
        offset, spans = 0.0, []
        for phase, seconds in zip(("prepare", "connect", "erase", "write", "verify", "reset", "finish"), durations):
            if seconds:
                spans.append({"phase": phase, "start_ms": offset, "duration_ms": seconds * 1000})
                offset += seconds * 1000
        started = now - timedelta(minutes=3 * i + 1)
        rows.append({"id": f"demo-{i}", "mac": mac, "chip": chip, "port": port,
                     "status": status, "elapsed_ms": offset, "started_at": started.isoformat(),
                     "finished_at": (started + timedelta(milliseconds=offset)).isoformat(),
                     "firmware": ["HNSKY-Bridge-0.13.156-Factory.bin" if chip == "ESP32-P4" else "sensor_gateway_20261111.bin"],
                     "phases": spans, "demo": True})
    return rows
