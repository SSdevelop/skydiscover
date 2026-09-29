#!/usr/bin/env python3
"""Stop hook: the lead may not end its turn before the run's iteration budget is spent.

A run is only as long as the lead keeps going; nothing else counts iterations. A lead that ends
its turn in the middle of the synthesis loop (to report, to wait for a background role, or after
losing its way) leaves an interactive session idle and ends a headless one. This hook refuses that
stop and tells the lead how many iterations remain.

It blocks only when all of these hold:

- the synthesis loop has started (a candidate exists under synthesis/impl/, or a checkpoint);
- the budget is known: <run>/budget.json ({"iterations": N}, written by `spec.run budget`), else
  the budget row of the decision log ("50 iterations", or Quick / Standard / Thorough);
- fewer than N checkpoints exist, and the run is not finished (no <slug>.done);
- the lead has not asked to pause: `spec.run pause <run> --reason "..."` writes <run>/pause.json,
  which lets exactly one stop through (so the lead can ask the user something) and is then kept as
  pause.<time>.json, never deleted;
- the lead has not already been refused MAX_BLOCKS times in a row without a new checkpoint, so a
  lead that cannot make progress is never trapped in a loop.

Every decision is appended to <run>/budget_guard.log.jsonl. The payload arrives on stdin; a refusal
is {"decision": "block", "reason": ...} on stdout. Any error lets the stop through.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

MAX_BLOCKS = int(os.environ.get("SKYDISCOVER_BUDGET_MAX_BLOCKS", "5") or "5")
_NAMED = {"quick": 20, "standard": 60, "thorough": 200}
STATE = ".budget_guard.json"
LOG = "budget_guard.log.jsonl"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _tokens():
    """hooks/token_usage.py, beside this file: it knows how to find the run and its checkpoints."""
    spec = importlib.util.spec_from_file_location(
        "_skysynth_token_usage", Path(__file__).resolve().parent / "token_usage.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def budget(run: Path) -> Optional[int]:
    """N from budget.json, else from the decision log's budget row."""
    doc = _read(run / "budget.json")
    if isinstance(doc, dict) and isinstance(doc.get("iterations"), int) and doc["iterations"] > 0:
        return doc["iterations"]
    rows = _read(run / "decision_log.json")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        text = f"{row.get('title') or ''} {row.get('detail') or ''}"
        if "budget" not in text.lower():
            continue
        m = re.search(r"\b(\d{1,4})\s*(?:completed\s+)?iterations?\b", text, re.I)
        if m and int(m.group(1)) > 0:
            return int(m.group(1))
        for word, n in _NAMED.items():
            if re.search(rf"\b{word}\b", text, re.I):
                return n
    return None


def _log(run: Path, entry: Dict[str, Any]) -> None:
    try:
        with open(run / LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": _now(), **entry}) + "\n")
    except OSError:
        pass


def decide(payload: Dict[str, Any]) -> Optional[str]:
    """The reason to refuse this stop, or None to let it through."""
    tu = _tokens()
    transcript = Path(payload.get("transcript_path") or "")
    run = tu.find_run(Path(payload.get("cwd") or os.getcwd()), transcript if transcript.is_file() else None)
    if run is None:
        return None
    if (run.parent / f"{run.name}.done").exists():
        return None
    marks = tu._checkpoint_times(run)
    impl = run / "synthesis" / "impl"
    if not marks and not (impl.is_dir() and any(p.is_file() for p in impl.rglob("*"))):
        return None  # still in the specification: the lead asks the user questions there
    n = budget(run)
    if n is None:
        return None
    done = len(marks)
    if done >= n:
        _log(run, {"allowed": "budget spent", "checkpoints": done, "budget": n})
        return None
    pause = run / "pause.json"
    if pause.is_file():
        reason = (_read(pause) or {}).get("reason", "")
        kept = run / f"pause.{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}.json"
        try:
            os.replace(pause, kept)
        except OSError:
            pass
        _log(run, {"allowed": "pause", "reason": reason, "checkpoints": done, "budget": n})
        return None
    state = _read(run / STATE) or {}
    streak = state.get("blocks", 0) + 1 if state.get("checkpoints") == done else 1
    if streak > MAX_BLOCKS:
        _log(run, {"allowed": f"{MAX_BLOCKS} refusals without a new checkpoint", "checkpoints": done, "budget": n})
        (run / STATE).write_text(json.dumps({"checkpoints": done, "blocks": 0}), encoding="utf-8")
        return None
    (run / STATE).write_text(json.dumps({"checkpoints": done, "blocks": streak}), encoding="utf-8")
    _log(run, {"blocked": True, "checkpoints": done, "budget": n, "streak": streak})
    return (
        f"Iteration budget: {done} of {n} iterations have a checkpoint. Do not end your turn: the run "
        f"is not finished. Continue with iteration {done + 1} now (planner if a design decision is "
        "due, then coding agent, evaluator in performance mode with its checkpoint, auditor, critic), "
        "and block on each role until it reports instead of waiting. If you truly need the user's "
        "input, first run `python3 -m skydiscover.synthesize.spec.run pause "
        f"{run} --reason \"<your question>\"`, then ask and end your turn. If the loop must end "
        "early (a measured ceiling), record it in the decision log and go on to Phase 3 and "
        "`run finish`."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        reason = decide(payload)
    except Exception:
        return 0  # a bug in the guard must never trap the lead
    if reason:
        print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
