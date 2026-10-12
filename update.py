"""Run one autopilot cycle from Task Scheduler or the terminal."""

import json
import socket
import subprocess
import sys
from pathlib import Path

import agent


def ensure_dashboard() -> None:
    """Safety net: if the dashboard is down (e.g. its logon task failed), start its scheduled task."""
    with socket.socket() as probe:
        probe.settimeout(1)
        if probe.connect_ex(("127.0.0.1", 8501)) == 0:
            return
    subprocess.run(["powershell.exe", "-NoProfile", "-Command", "Start-ScheduledTask -TaskName 'WorkWorm - Tablero'"],
                   capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


if __name__ == "__main__":
    ensure_dashboard()
    result = agent.run(force_collect="--collect" in sys.argv)
    log = Path(__file__).resolve().parent / "data" / "scheduler.log"
    log.parent.mkdir(exist_ok=True)
    with log.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(result, ensure_ascii=False) + "\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
