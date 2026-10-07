"""What blocked publishing the rdma-bptree run, and the pkill it ran into a shared server:

- `_`-prefixed files in a suite are helpers, not tests, and travel to the knowledge base with it;
- `run finish` re-tests checkpoints at most SKYDISCOVER_TEST_WORKERS tests at a time;
- process_guard lets an agent stop only processes its own session started.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from skydiscover.synthesize.spec import kept_tests
from skydiscover.synthesize.spec import run as spec_run
from skydiscover.synthesize.spec.paths import Domain, Run, helper_files
from skydiscover.synthesize.spec.paths import test_files as suite_tests

HOOKS = Path(__file__).resolve().parents[2] / "skydiscover/synthesize/workflow/hooks"


def _suite(tmp_path: Path, n_tests: int = 2) -> Run:
    run = Run(tmp_path / "project" / ".skydiscover" / "kv").create()
    run.task.write_text("---\ndomain: kv store\n---\n# Task\n", encoding="utf-8")
    run.tests.mkdir(parents=True, exist_ok=True)
    (run.tests / "test.sh").write_text("#!/bin/sh\nexit 0\n")
    for i in range(n_tests):
        (run.tests / f"t{i}.c").write_text(f"// Property {i} holds under load.\n")
    (run.tests / "_pool_sim.h").write_text("// shared simulator\n")
    (run.tests / "_workload.c").write_text("// shared workload\n")
    return run


def test_underscore_files_are_helpers_not_tests(tmp_path):
    run = _suite(tmp_path)
    assert [p.name for p in suite_tests(run.tests)] == ["t0.c", "t1.c"]
    assert [p.name for p in helper_files(run.tests)] == ["_pool_sim.h", "_workload.c"]
    kept_tests.sync(run.path, "kv store")
    kept = Domain("kv store").tests
    assert (kept / "_pool_sim.h").is_file() and (kept / "_workload.c").is_file()
    assert sorted(t["id"] for t in kept_tests.tests_for("kv store")) == ["t0", "t1"]


def test_finish_runs_at_most_the_configured_tests_at_once(tmp_path, monkeypatch):
    run = _suite(tmp_path, n_tests=10)
    monkeypatch.setenv("SKYDISCOVER_TEST_WORKERS", "3")
    monkeypatch.setattr(spec_run.artifact_store, "entry_in", lambda cp, rd: run.impl)
    running, peak, lock = [0], [0], threading.Lock()

    def one(checkpoint, entry, suite, name):
        with lock:
            running[0] += 1
            peak[0] = max(peak[0], running[0])
        time.sleep(0.05)
        with lock:
            running[0] -= 1
        return 1 if name == "t3.c" else 0

    monkeypatch.setattr(spec_run, "_run_one", one)
    assert spec_run._failing_tests(tmp_path / "cp", run.path) == ["t3.c"]
    assert peak[0] == 3


# ------------------------------------------------------------------------------ process guard


@pytest.fixture(scope="module")
def guard():
    spec = importlib.util.spec_from_file_location("_pg", HOOKS / "process_guard.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _bash(command: str) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}}


@pytest.fixture
def own_child(monkeypatch):
    monkeypatch.setenv("SKYDISCOVER_SESSION_PID", str(os.getpid()))
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    yield child.pid
    child.kill()
    child.wait()


@pytest.mark.parametrize(
    "command",
    [
        "pkill -f dm_compiler_rt_memory",
        "sudo killall dm_compiler_rt_memory",
        "kill $(pgrep -f dm_compiler_rt_memory)",
        "kill -9 1",
        "sudo -n kill -TERM 1",
        "kill -9 -1",
        "cd /tmp && kill 1",
    ],
)
def test_stopping_a_foreign_process_is_refused(guard, own_child, command):
    reason = guard.decide(_bash(command))
    assert reason and reason.startswith("process-guard:")


def test_stopping_an_own_process_is_allowed(guard, own_child):
    for command in (
        f"kill {own_child}",
        f"kill -9 {own_child}",
        f"kill -s TERM {own_child}",
        "kill %1",
        'kill "$SERVER_PID"',
        "echo skill set; ls -la",
    ):
        assert guard.decide(_bash(command)) is None, command
