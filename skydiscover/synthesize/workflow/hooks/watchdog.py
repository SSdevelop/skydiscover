#!/usr/bin/env python3
"""PreToolUse watchdog on Bash: catch a lead that polls a stalled run inside one endless turn.

The budget guard acts only when the lead tries to end its turn. A lead that instead waits on disk
with shell loops ("until checkpoint_9/score.json exists; sleep") never tries, and when what it waits
for has already ended, the run sits idle indefinitely. Every one of those loops is a Bash call, so
here each Bash call checks the run (run_state.py): when the synthesis loop is under way, the run is
not done, no helper is working, and nothing has progressed for SKYDISCOVER_STALL_SECS (default four
hours), the call is refused (exit 2) with an instruction to act on the newest result instead.

It refuses at most once per SKYDISCOVER_WATCHDOG_EVERY seconds (default 1800), so the lead's next
commands, the ones that act, go through. Every refusal is appended to <run>/watchdog.log.jsonl. Any
error lets the call through.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

EVERY = int(os.environ.get("SKYDISCOVER_WATCHDOG_EVERY", "1800") or "1800")
STATE = ".watchdog.json"
LOG = "watchdog.log.jsonl"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"_skysynth_{name}_wd", Path(__file__).resolve().parent / f"{name}.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def decide(payload: Dict[str, Any], now: Optional[float] = None) -> Optional[str]:
    if payload.get("tool_name") != "Bash":
        return None
    now = time.time() if now is None else now
    tu = _load("token_usage")
    transcript = Path(payload.get("transcript_path") or "")
    session = transcript if transcript.is_file() else None
    run = tu.find_run(Path(payload.get("cwd") or os.getcwd()), session)
    if run is None or (run.parent / f"{run.name}.done").exists():
        return None
    if tu.current_iteration(run) == 0:
        return None  # the specification: long waits on the user are normal there
    bg = _load("budget_guard")
    n = bg.budget(run)
    marks = tu._checkpoint_times(run)
    if n is not None and len(marks) >= n:
        return None
    rs = _load("run_state")
    idle_hours = rs.stalled(run, session, marks, now)
    if idle_hours is None:
        return None
    state = _read(run / STATE) or {}
    if now - float(state.get("warned_at", 0)) < EVERY:
        return None
    (run / STATE).write_text(json.dumps({"warned_at": now}), encoding="utf-8")
    try:
        with open(run / LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "time": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                "stalled_hours": round(idle_hours, 1),
                "finished": len(marks),
                "budget": n,
                "command": str((payload.get("tool_input") or {}).get("command") or "")[:300],
            }) + "\n")
    except OSError:
        pass
    return "watchdog: " + bg.stall_message(run, idle_hours, len(marks), n or 0, rs)


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        reason = decide(payload)
    except Exception:
        return 0  # a bug in the watchdog must never block a command
    if reason:
        sys.stderr.write(reason + "\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
