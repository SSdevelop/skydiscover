"""What the guards need to know about a running loop: are helpers still working, and when did the
run last make progress. Stdlib only, shared by budget_guard.py and watchdog.py.

- A helper (subagent) is **active** when its transcript was written in the last ACTIVE_SECS, or
  when it is in the middle of a tool call (its last turn asked for a tool, or a tool result
  arrived and it has not answered): a role running a multi-hour benchmark writes nothing until the
  benchmark ends, so recency alone would miss it. A helper silent for longer than STALL_SECS is not
  active, whatever its last turn says.
- **Progress** is the newest of: a subagent transcript write, a file written under synthesis/
  (code, tests, bench logs, leaderboard), and a finished iteration. The run is **stalled** when
  none of these happened for STALL_SECS while the loop is under way and the run is not done.

SKYDISCOVER_ACTIVE_SECS (default 300) and SKYDISCOVER_STALL_SECS (default 14400, four hours: the
longest one evaluation may take before it counts as a bug) tune both.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path
from typing import List, Optional

ACTIVE_SECS = int(os.environ.get("SKYDISCOVER_ACTIVE_SECS", "300") or "300")
STALL_SECS = int(os.environ.get("SKYDISCOVER_STALL_SECS", "14400") or "14400")


def tokens():
    """hooks/token_usage.py: finds the run and its finished iterations."""
    spec = importlib.util.spec_from_file_location(
        "_skysynth_token_usage_rs", Path(__file__).resolve().parent / "token_usage.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def subagent_transcripts(transcript: Optional[Path]) -> List[Path]:
    if transcript is None:
        return []
    sub = Path(transcript).with_suffix("") / "subagents"
    return sorted(sub.glob("*.jsonl")) if sub.is_dir() else []


def _tail_lines(path: Path, size: int = 262144) -> List[str]:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            f.seek(max(0, f.tell() - size))
            return f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return []


def mid_tool_call(path: Path) -> bool:
    """True when the helper's last turn asked for a tool, or a tool result has not been answered."""
    for line in reversed(_tail_lines(path)):
        try:
            row = json.loads(line)
        except ValueError:
            continue
        msg = row.get("message") if isinstance(row, dict) else None
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        blocks = content if isinstance(content, list) else []
        if row.get("type") == "user":
            if any(isinstance(b, dict) and b.get("type") == "tool_result" for b in blocks):
                return True
            continue
        if row.get("type") == "assistant":
            reason = msg.get("stop_reason")
            if reason == "end_turn":
                return False
            if reason == "tool_use" or any(
                isinstance(b, dict) and b.get("type") == "tool_use" for b in blocks
            ):
                return True
    return False


def active_helpers(transcript: Optional[Path], now: Optional[float] = None) -> List[str]:
    """The subagents of this session still at work."""
    now = time.time() if now is None else now
    active = []
    for path in subagent_transcripts(transcript):
        try:
            age = now - path.stat().st_mtime
        except OSError:
            continue
        if age < ACTIVE_SECS or (age < STALL_SECS and mid_tool_call(path)):
            active.append(path.stem)
    return active


def last_progress(run: Path, transcript: Optional[Path], marks=()) -> float:
    """Unix time of the newest sign of work on the run (0 when there is none)."""
    newest = max((when for _, when in marks), default=0.0)
    for path in subagent_transcripts(transcript):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            pass
    synthesis = run / "synthesis"
    if synthesis.is_dir():
        for here, dirs, names in os.walk(synthesis):
            dirs[:] = [d for d in dirs if not d.startswith(".") and d != "__pycache__"]
            for name in names:
                if name.startswith("."):
                    continue
                try:
                    newest = max(newest, os.stat(os.path.join(here, name)).st_mtime)
                except OSError:
                    pass
    return newest


def last_result(run: Path) -> str:
    """One line on the newest leaderboard entry, for a guard's message."""
    try:
        rows = json.loads((run / "synthesis" / "bench" / "leaderboard.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "no leaderboard entry yet"
    if not isinstance(rows, list) or not rows:
        return "no leaderboard entry yet"
    row = rows[-1] if isinstance(rows[-1], dict) else {}
    metrics = row.get("metrics") if isinstance(row.get("metrics"), dict) else {}
    head = next(iter(metrics.items()), None)
    return (
        f"the newest leaderboard entry is a {row.get('role', 'candidate')} {row.get('draw', 'scored')} "
        f"measurement" + (f" ({head[0]}={head[1]})" if head else "")
    )


def stalled(run: Path, transcript: Optional[Path], marks=(), now: Optional[float] = None) -> Optional[float]:
    """Hours since the last progress when that exceeds STALL_SECS and no helper is active; else None."""
    now = time.time() if now is None else now
    if active_helpers(transcript, now):
        return None
    idle = now - last_progress(run, transcript, marks)
    return idle / 3600 if idle > STALL_SECS else None
