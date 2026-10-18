#!/usr/bin/env python3
"""Experiment input integrity and final-round audit hooks; cadence remains in loop_guard."""

import json
import sys
from pathlib import Path

from run import WORKFLOW, accepted_audits, integrity, read_json, sync_audit, write_json


def reason(project, payload, stopping=False):
    from skydiscover.synthesize.spec.iterations import finished

    manifest = read_json(project / ".experiment/manifest.json")
    integrity(project, manifest)
    run = project / manifest["run"]
    sync_audit(project, run)
    write_json(
        project / ".experiment/hooks-seen.json", {"event": "Stop" if stopping else "PreToolUse"}
    )
    done = len(finished(run))
    budget = read_json(run / "budget.json")["iterations"]
    if stopping:
        if (run / "pause.json").exists():
            return None
        history = project / ".experiment/audit-history.json"
        last = (
            max((r["finished_iterations"] for r in accepted_audits(run, read_json(history))), default=0)
            if history.exists()
            else 0
        )
        if done and last < done:
            update = project / ".experiment/proof-adapter-update.md"
            hint = " Read .experiment/proof-adapter-update.md before retrying the old blocker." if update.exists() else ""
            return f"Round {done} still needs its proof audit outcome, including the final round. Run the auditor." + hint
    elif done >= budget:
        # Do not let a fresh role launch silently turn a 50-round experiment into 51 rounds.
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "experiment_loop_guard", WORKFLOW / "hooks/loop_guard.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        if payload.get("tool_name") in module.AGENT_TOOLS and module.role_of(
            payload.get("tool_input") or {}
        ) in {"coding", "planner"}:
            return f"The {budget}-round budget is spent. Complete auditing and final reporting."
    return None


def main():
    stopping = "--stop" in sys.argv
    try:
        payload = json.load(sys.stdin)
        message = reason(Path(sys.argv[1]).resolve(), payload, stopping)
    except (OSError, ValueError, KeyError) as exc:
        message = "Experiment integrity check failed: " + str(exc)
    if message:
        if stopping:
            print(json.dumps({"decision": "block", "reason": message}))
        else:
            print(message, file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
