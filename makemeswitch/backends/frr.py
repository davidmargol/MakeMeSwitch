"""FRRouting backend (optional, Layer 3 only).

FRR is not required for the Layer-2 MVP.  Every method reports whether it is
actually available instead of pretending to configure routing.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from makemeswitch.utils.logging import get_logger

log = get_logger("makemeswitch.frr")


class FrrBackend:
    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    @property
    def installed(self) -> bool:
        return bool(shutil.which("vtysh"))

    def status(self) -> dict[str, Any]:
        if not self.installed:
            return {"installed": False, "running": False, "version": None, "detail": "vtysh not found"}
        try:
            proc = subprocess.run(["vtysh", "-c", "show version"], capture_output=True, text=True, timeout=self.timeout, check=False)
            running = proc.returncode == 0
            version = None
            for line in proc.stdout.splitlines():
                if line.lower().startswith("frr"):
                    version = line.strip()
                    break
            return {"installed": True, "running": running, "version": version, "detail": ""}
        except (OSError, subprocess.TimeoutExpired) as exc:
            return {"installed": True, "running": False, "version": None, "detail": str(exc)}

    def run(self, commands: list[str]) -> str:
        if not self.installed:
            raise RuntimeError("FRRouting is not installed")
        argv = ["vtysh"]
        for command in commands:
            argv += ["-c", command]
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=self.timeout, check=False)
        if proc.returncode != 0:
            raise RuntimeError((proc.stderr or proc.stdout).strip() or "vtysh failed")
        return proc.stdout

    def neighbors(self) -> list[dict[str, Any]]:
        if not self.installed:
            return []
        proc = subprocess.run(
            ["vtysh", "-c", "show ip route json"], capture_output=True, text=True, timeout=self.timeout, check=False
        )
        if proc.returncode != 0:
            return []
        try:
            data = json.loads(proc.stdout or "{}")
        except json.JSONDecodeError:
            return []
        return list(data.keys())
