#!/usr/bin/env python3
"""PreToolUse guard on launching a subagent: keep each iteration's cost bounded, as in the paper.

Two limits, read from <run>/budget.json (`spec.run budget <run> <N> --attempts A --audit-every M`):

- **Coding attempts per iteration** (`max_coding_attempts`, default 5). Each launch of a coding
  agent (`coding-agent` or `dsa`) counts against the iteration in progress. At the cap, another
  launch is refused: the lead closes the round with `spec.iterations fail <run> --reason ...` and
  starts the next one from the best checkpoint, instead of retrying one idea without end.
- **Auditor every M iterations** (`audit_every`, default 15). During the synthesis loop the
  auditor runs once M iterations have finished since its last run, not after every change. Its
  Phase 3 final reviews (production lens, attack, the pass on the selected candidate) and any run
  once the budget is spent are never refused.

Only the synthesis loop is limited: before the first candidate exists nothing is counted. A launch
is recognized by its subagent_type or by the role brief its prompt names. Every decision is
appended to <run>/loop_guard.log.jsonl. A refusal is exit 2 with the reason on stderr; any error in
the guard lets the launch through.
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

AGENT_TOOLS = {"Task", "Agent"}
DEFAULT_ATTEMPTS = int(os.environ.get("SKYDISCOVER_MAX_CODING_ATTEMPTS", "5") or "5")
DEFAULT_AUDIT_EVERY = int(os.environ.get("SKYDISCOVER_AUDIT_EVERY", "15") or "15")
STATE = ".loop_guard.json"
LOG = "loop_guard.log.jsonl"
_FINAL_REVIEW = re.compile(
    r"production[- ]lens|attack mode|\battack\b.*\bmode\b|final review|phase 3|selected candidate",
    re.I,
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _tokens():
    spec = importlib.util.spec_from_file_location(
        "_skysynth_token_usage_lg", Path(__file__).resolve().parent / "token_usage.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def role_of(tool_input: Dict[str, Any]) -> Optional[str]:
    """'coding' or 'auditor' for a launch of one of those roles, else None."""
    kind = str(tool_input.get("subagent_type") or "").lower()
    text = f"{tool_input.get('prompt') or ''}\n{tool_input.get('description') or ''}"
    if kind.endswith(("coding-agent", ":dsa")) or kind == "dsa" or re.search(
        r"(coding-agent|/dsa)\.md\b", text
    ):
        return "coding"
    if kind.endswith("auditor") or re.search(r"\bauditor\.md\b", text):
        return "auditor"
    return None


def limits(run: Path) -> Dict[str, int]:
    doc = _read(run / "budget.json")
    doc = doc if isinstance(doc, dict) else {}

    def positive(key: str, default: int) -> int:
        v = doc.get(key)
        return v if isinstance(v, int) and v > 0 else default

    return {
        "attempts": positive("max_coding_attempts", DEFAULT_ATTEMPTS),
        "audit_every": positive("audit_every", DEFAULT_AUDIT_EVERY),
        "budget": positive("iterations", 0),
    }


def _log(run: Path, entry: Dict[str, Any]) -> None:
    try:
        with open(run / LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"time": _now(), **entry}) + "\n")
    except OSError:
        pass


def _save(run: Path, state: Dict[str, Any]) -> None:
    (run / STATE).write_text(json.dumps(state), encoding="utf-8")


def decide(payload: Dict[str, Any]) -> Optional[str]:
    """The reason to refuse this launch, or None to let it through (after counting it)."""
    if payload.get("tool_name") not in AGENT_TOOLS:
        return None
    tool_input = payload.get("tool_input") or {}
    role = role_of(tool_input)
    if role is None:
        return None
    tu = _tokens()
    transcript = Path(payload.get("transcript_path") or "")
    run = tu.find_run(Path(payload.get("cwd") or os.getcwd()), transcript if transcript.is_file() else None)
    if run is None:
        return None
    iteration = tu.current_iteration(run)
    if iteration == 0:
        return None  # the specification: nothing is counted yet
    done = iteration - 1
    lim = limits(run)
    state = _read(run / STATE) or {}
    if state.get("iteration") != iteration:
        state = {**state, "iteration": iteration, "attempts": 0}

    if role == "coding":
        if state["attempts"] >= lim["attempts"]:
            _log(run, {"refused": "coding", "iteration": iteration, "attempts": state["attempts"], "cap": lim["attempts"]})
            return (
                f"loop-guard: iteration {iteration} has used its {lim['attempts']} coding attempts "
                "(the per-iteration cap). Do not launch another coding agent for it. Close it: "
                f"`python3 -m skydiscover.synthesize.spec.iterations fail {run} --reason \"<what was "
                "tried and why it did not pass>\"`, restore the best checkpoint's "
                ".verification/source into synthesis/impl/ if the working copy is broken, have the "
                f"critic log what this round ruled out, then start iteration {iteration + 1} with the "
                "planner."
            )
        state["attempts"] += 1
        _save(run, state)
        _log(run, {"allowed": "coding", "iteration": iteration, "attempt": state["attempts"], "cap": lim["attempts"]})
        return None

    # the auditor
    text = f"{tool_input.get('prompt') or ''}\n{tool_input.get('description') or ''}"
    last = int(state.get("last_audit_done", 0))
    final = bool(_FINAL_REVIEW.search(text)) or (lim["budget"] and done >= lim["budget"])
    due = done - last >= lim["audit_every"]
    if final or due:
        if due and not final:
            state["last_audit_done"] = done
        _save(run, state)
        _log(run, {"allowed": "auditor", "iteration": iteration, "final": bool(final), "finished": done})
        return None
    _log(run, {"refused": "auditor", "iteration": iteration, "finished": done, "last_audit_finished": last})
    return (
        f"loop-guard: the auditor runs every {lim['audit_every']} iterations during the loop "
        f"(last after iteration {last}; next once iteration {last + lim['audit_every']} is finished). "
        "Skip the audit this iteration and go on to the critic. The auditor still reviews the "
        "selected candidate in Phase 3 (production lens, attack mode, final pass)."
    )


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        reason = decide(payload)
    except Exception:
        return 0  # a bug in the guard must never block a launch
    if reason:
        sys.stderr.write(reason + "\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
