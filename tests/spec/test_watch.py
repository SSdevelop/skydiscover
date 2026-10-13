"""A lead waiting on roles is allowed to wait; a lead polling a stalled run is told to act.

- run_state: a role is working until its last turn is a final answer (mid tool call it may write
  nothing for hours); a role that just finished is done; a run is stalled when nothing progressed
  for hours;
- budget_guard lets the lead end its turn while a role works, and names a stall when refusing;
- watchdog refuses one Bash call (then lets the next through) when the loop has stalled.
"""

from __future__ import annotations

import importlib.util
import json
import os
import time
from pathlib import Path

import pytest

from skydiscover.synthesize.spec import iterations
from skydiscover.synthesize.spec.paths import Run

HOOKS = Path(__file__).resolve().parents[2] / "skydiscover/synthesize/workflow/hooks"
HOUR = 3600


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_watch_{name}", HOOKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def run_state():
    return _load("run_state")


@pytest.fixture(scope="module")
def budget_guard():
    return _load("budget_guard")


@pytest.fixture(scope="module")
def watchdog():
    return _load("watchdog")


def _line(kind: str, **message) -> str:
    return json.dumps({"type": kind, "message": message})


ENDED = _line("assistant", stop_reason="end_turn", content=[{"type": "text", "text": "done"}])
WAITING_ON_TOOL = _line("assistant", stop_reason="tool_use", content=[{"type": "tool_use", "name": "Bash"}])
TOOL_RESULT = _line("user", content=[{"type": "tool_result", "content": "ok"}])


def _age(path: Path, seconds: float) -> None:
    t = time.time() - seconds
    os.utime(path, (t, t))


def _setup(tmp_path: Path, helper_last: str, helper_age: float, files_age: float):
    run = Run(tmp_path / "project" / ".skydiscover" / "kv").create()
    run.task.write_text("---\ndomain: kv store\n---\n# Task\n", encoding="utf-8")
    run.impl.mkdir(parents=True, exist_ok=True)
    (run.impl / "store.py").write_text("v1\n")
    _age(run.impl / "store.py", files_age)
    (run.path / "budget.json").write_text(json.dumps({"iterations": 10}))
    main = tmp_path / "transcripts" / "s1.jsonl"
    (main.with_suffix("") / "subagents").mkdir(parents=True)
    main.write_text(ENDED + "\n")
    helper = main.with_suffix("") / "subagents" / "agent-eval.jsonl"
    helper.write_text(TOOL_RESULT + "\n" + helper_last + "\n")
    _age(helper, helper_age)
    payload = {"cwd": str(run.path.parent.parent), "transcript_path": str(main),
               "hook_event_name": "Stop", "tool_name": "Bash", "tool_input": {"command": "sleep 560"}}
    return run, main, payload


def test_a_role_is_working_until_it_finishes(tmp_path, run_state):
    for last, age, working in [
        (ENDED, 60, False),                 # finished a minute ago: done, not busy
        (WAITING_ON_TOOL, 2 * HOUR, True),  # in a long benchmark: silent but not done
        (TOOL_RESULT, 2 * HOUR, True),
        (ENDED, 2 * HOUR, False),           # finished two hours ago
        (WAITING_ON_TOOL, 30 * HOUR, False),  # silent past the stall bound: gone
    ]:
        _, main, _ = _setup(tmp_path / f"{age}-{len(last)}", last, age, 0)
        assert bool(run_state.active_helpers(main)) is working, (last[:40], age)


def test_the_budget_guard_lets_the_lead_wait_for_a_working_role(tmp_path, budget_guard, monkeypatch):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    run, _, payload = _setup(tmp_path, WAITING_ON_TOOL, 2 * HOUR, 2 * HOUR)
    assert budget_guard.decide(payload) is None
    log = [json.loads(x) for x in (run.path / "budget_guard.log.jsonl").read_text().splitlines()]
    assert log[-1]["allowed"] == "helpers still working"


def test_a_role_that_just_finished_does_not_let_the_lead_stop(tmp_path, budget_guard, monkeypatch):
    """Iteration 18 of the rdma-bptree run: the evaluator finished, the lead reported, said it would
    open iteration 19, and ended its turn minutes later. That stop must be refused."""
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    _, _, payload = _setup(tmp_path, ENDED, 120, 120)
    reason = budget_guard.decide(payload)
    assert reason and "0 of 10" in reason


def test_the_guards_find_the_run_from_a_folder_below_the_project(tmp_path, budget_guard, monkeypatch):
    """The rdma-bptree lead cd'ed into synthesis/bench/runs; from then on every hook looked for
    <that folder>/.skydiscover, found no run, and did nothing (no snapshots, no guards)."""
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    run, _, payload = _setup(tmp_path, ENDED, 120, 120)
    deep = run.bench / "runs"
    deep.mkdir(parents=True, exist_ok=True)
    reason = budget_guard.decide({**payload, "cwd": str(deep)})
    assert reason and "0 of 10" in reason


def test_the_budget_guard_names_a_stall(tmp_path, budget_guard, monkeypatch):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    _, _, payload = _setup(tmp_path, ENDED, 29 * HOUR, 29 * HOUR)
    reason = budget_guard.decide(payload)
    assert reason.startswith("Stalled:") and "spec.iterations fail" in reason


def test_the_watchdog_interrupts_polling_of_a_stalled_run_once(tmp_path, watchdog, monkeypatch):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    run, _, payload = _setup(tmp_path, ENDED, 29 * HOUR, 29 * HOUR)
    iterations.close(run.path, "checkpoint", checkpoint="checkpoint_1")
    marks_file = run.bench / "iterations.json"
    rows = json.loads(marks_file.read_text())
    rows[0]["at"] = "2026-01-01T00:00:00Z"  # finished long ago
    marks_file.write_text(json.dumps(rows))
    _age(marks_file, 29 * HOUR)
    reason = watchdog.decide(payload)
    assert reason and reason.startswith("watchdog: Stalled:") and "iteration 2" in reason
    assert watchdog.decide(payload) is None, "the next command, the one that acts, goes through"
    assert (run.path / "watchdog.log.jsonl").read_text().count("stalled_hours") == 1


def test_the_watchdog_leaves_a_working_run_alone(tmp_path, watchdog, monkeypatch):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    _, _, busy = _setup(tmp_path / "busy", WAITING_ON_TOOL, 2 * HOUR, 29 * HOUR)
    assert watchdog.decide(busy) is None  # a role is mid benchmark
    _, _, fresh = _setup(tmp_path / "fresh", ENDED, 29 * HOUR, 60)
    assert watchdog.decide(fresh) is None  # code changed a minute ago
    run, _, spec = _setup(tmp_path / "spec", ENDED, 29 * HOUR, 29 * HOUR)
    (run.impl / "store.py").unlink()
    assert watchdog.decide(spec) is None  # still in the specification
    assert watchdog.decide({**busy, "tool_name": "Read"}) is None
