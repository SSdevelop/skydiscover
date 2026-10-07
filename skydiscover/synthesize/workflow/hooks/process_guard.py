#!/usr/bin/env python3
"""PreToolUse guard on Bash: the agents may only stop processes this session started.

The workflow's rule is to touch only what the run owns: never kill, stop, or reconfigure anything
else on the machine, even to make a measurement clean. A shared server (an RDMA memory server,
another user's job) can die by an agent's `pkill` as easily as its own test binary. So:

- `pkill` and `killall` are refused: they select by name and cannot tell this run's processes
  from anyone else's. So is `kill` fed by `pgrep` / `pidof`, which does the same.
- `kill <pid>` is allowed only when <pid> descends from this Claude Code session (the agent
  started it); a pid outside the session, a negative pid (a process group), or -1 is refused.
  `kill %job` and `kill $VAR` (a pid the agent kept from its own launch) are allowed.
- `sudo`, `nohup`, `env`, `timeout`, and `exec` in front of a command are looked through.

A refusal is exit 2 with the reason on stderr; any error in the guard lets the command through.
SKYDISCOVER_SESSION_PID names the session's process when it cannot be found by walking up from
the hook (tests set it).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from typing import List, Optional

_WRAPPERS = {"sudo", "nohup", "env", "exec", "command", "builtin"}
_SPLIT = re.compile(r"\|\||&&|[;|&\n]")


def _parent_and_name(pid: int):
    try:
        out = subprocess.run(
            ["ps", "-o", "ppid=,comm=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None
    if not out:
        return None
    ppid, _, name = out.partition(" ")
    try:
        return int(ppid), name.strip()
    except ValueError:
        return None


def _ancestors(pid: int) -> List[int]:
    chain, seen = [], set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        info = _parent_and_name(pid)
        if info is None:
            break
        pid = info[0]
        chain.append(pid)
    return chain


def session_root() -> Optional[int]:
    """The Claude Code process this hook runs under: the nearest ancestor named like claude."""
    env = os.environ.get("SKYDISCOVER_SESSION_PID")
    if env and env.isdigit():
        return int(env)
    pid = os.getppid()
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        info = _parent_and_name(pid)
        if info is None:
            return None
        if "claude" in os.path.basename(info[1]).lower():
            return pid
        pid = info[0]
    return None


def _words(segment: str) -> List[str]:
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:
        words = segment.split()
    i = 0
    while i < len(words):
        w = words[i]
        if w in _WRAPPERS:  # sudo -n kill ..., env -i kill ...
            i += 1
            while i < len(words) and words[i].startswith("-"):
                i += 1
        elif w == "timeout":  # timeout 10 kill ...
            i += 2
        elif "=" in w and not w.startswith("-"):  # VAR=x kill ...
            i += 1
        else:
            break
    return words[i:]


def decide(payload: dict) -> Optional[str]:
    if payload.get("tool_name") != "Bash":
        return None
    command = str((payload.get("tool_input") or {}).get("command") or "")
    if not re.search(r"\b(kill|pkill|killall)\b", command):
        return None
    for segment in _SPLIT.split(command):
        words = _words(segment.strip())
        if not words:
            continue
        verb = os.path.basename(words[0])
        if verb in ("pkill", "killall"):
            return _refuse(f"`{verb}` selects processes by name, so it cannot tell this run's from anyone else's")
        if verb != "kill":
            continue
        if re.search(r"\b(pgrep|pidof)\b", command):
            return _refuse("`kill` fed by pgrep/pidof selects processes by name")
        root = session_root()
        args = words[1:]
        targets = []
        i = 0
        while i < len(args):
            arg = args[i]
            if arg in ("-s", "-n"):  # kill -s TERM <pid>: the next word is a signal
                i += 2
                continue
            if arg == "--":
                targets += args[i + 1 :]
                break
            named_signal = arg.startswith("-") and not re.fullmatch(r"-\d+", arg)  # -TERM, -KILL
            numbered_signal = i == 0 and len(args) > 1 and re.fullmatch(r"-\d+", arg)  # kill -9 <pid>
            if named_signal or numbered_signal:
                i += 1
                continue
            targets.append(arg)
            i += 1
        for arg in targets:
            if arg.startswith(("%", "$")) or not re.fullmatch(r"-?\d+", arg):
                continue  # a job or a pid the agent kept in a variable
            pid = int(arg)
            if pid <= 0:
                return _refuse(f"`kill {arg}` signals a whole process group or every process")
            if root is not None and root not in _ancestors(pid):
                return _refuse(f"pid {pid} was not started by this session")
    return None


def _refuse(why: str) -> str:
    return (
        f"process-guard: {why}. The workflow forbids stopping, killing, or reconfiguring anything the "
        "run did not start (a shared server, another user's job), even to make a measurement clean. "
        "Use `kill <pid>` only on a process you started in this session (keep its pid from `$!`). "
        "If a foreign process is in the way, record it in the decision log and ask the user."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        reason = decide(payload)
    except Exception:
        return 0  # a bug in the guard must never block a command
    if reason:
        sys.stderr.write(reason + "\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
