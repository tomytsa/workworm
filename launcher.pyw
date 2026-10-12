"""Windowless launcher for Task Scheduler: `pythonw launcher.pyw board|agent`. Output goes to data/<mode>.log."""

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PYTHON = ROOT / ".venv" / "Scripts" / "python.exe"
COMMANDS = {
    "board": [str(PYTHON), "-m", "streamlit", "run", "app.py", "--server.address", "127.0.0.1",
              "--server.port", "8501", "--server.headless", "true"],
    "agent": [str(PYTHON), "update.py"],
}

mode = sys.argv[1] if len(sys.argv) > 1 else "agent"
(ROOT / "data").mkdir(exist_ok=True)
with open(ROOT / "data" / f"{mode}.log", "a", encoding="utf-8") as log:
    sys.exit(subprocess.call(COMMANDS[mode], cwd=ROOT, stdout=log, stderr=subprocess.STDOUT,
                             creationflags=subprocess.CREATE_NO_WINDOW))
