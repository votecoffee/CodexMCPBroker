"""A minimal launcher. Setup installs dependencies; a chat only starts its frontend."""

import json
import os
import subprocess
from pathlib import Path

root = Path(__file__).resolve().parent.parent
runtime_path = root / "runtime.json"
if not runtime_path.is_file():
    raise SystemExit("Universal Tools is not configured. Run scripts/Setup.ps1 first.")
runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
python = Path(runtime["python"])
state = Path(runtime["state"])
if not python.is_file() or not (state / "settings.json").is_file():
    raise SystemExit("Universal Tools runtime is missing. Run scripts/Setup.ps1 to repair it.")
arguments = [str(python), "-B", "-m", "universal_tools", "--state", str(state), "proxy"]
if os.name == "nt":
    arguments = [subprocess.list2cmdline([argument]) for argument in arguments]
os.execv(str(python), arguments)
