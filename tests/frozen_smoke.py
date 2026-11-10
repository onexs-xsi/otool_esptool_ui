"""Software-only smoke checks for a copied Windows onefile executable.

Usage: python tests/frozen_smoke.py dist/otool_esptool_ui.exe REPORT.json
Leaves the isolated directory available for subsequent manual GUI checks.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

source = Path(sys.argv[1]).resolve()
report_path = Path(sys.argv[2]).resolve()
root = Path(tempfile.mkdtemp(prefix="OTool 独立 验证 "))
exe = root / "OTool 测试.exe"
shutil.copy2(source, exe)
env = {key: value for key, value in os.environ.items()
       if not key.upper().startswith(("PYTHON", "QT_", "VIRTUAL_ENV", "OTOOL_"))}
env["PATH"] = os.pathsep.join([os.path.join(env["SYSTEMROOT"], "System32"), env["SYSTEMROOT"]])
checks = []


def run(arguments):
    result = subprocess.run([str(exe), *arguments], cwd=root, env=env,
                            capture_output=True, encoding="utf-8", errors="replace",
                            timeout=45, creationflags=subprocess.CREATE_NO_WINDOW)
    checks.append({"args": arguments, "exit": result.returncode,
                   "stdout": result.stdout, "stderr": result.stderr})
    return result


for tool in ("esptool", "espefuse", "espsecure"):
    result = run([tool, "--help"])
    assert result.returncode == 0, checks[-1]
    assert tool in result.stdout.lower(), checks[-1]
assert run(["__otool_qt_startup_smoke__"]).returncode == 0
assert "5.6.0" in run(["esptool", "version"]).stdout

first, second, merged = (root / filename for filename in ("首段.bin", "second data.bin", "合并.bin"))
first.write_bytes(b"FIRST")
second.write_bytes(b"SECOND")
result = run(["__otool_worker__", "esptool", "--chip", "esp32", "merge-bin",
              "-o", str(merged), "0x0", str(first), "0x1000", str(second)])
assert result.returncode == 0, checks[-1]
events = [json.loads(line.removeprefix("[[OTOOL_EVENT]]"))
          for line in result.stdout.splitlines() if line.startswith("[[OTOOL_EVENT]]")]
assert events[0]["text"] == "[Worker] 工作进程已启动", events
assert not any(event["type"] == "fatal" for event in events), events
assert merged.read_bytes() == b"FIRST" + b"\xff" * 4091 + b"SECOND"

report = {"isolated_directory": str(root), "executable": str(exe),
          "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
          "checks": checks, "merged_bytes": merged.stat().st_size,
          "path_contains_only_windows": env["PATH"]}
report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
print(json.dumps({"checks_passed": len(checks), "merged_bytes": report["merged_bytes"],
                  "isolated_directory": str(root)}, ensure_ascii=False))
