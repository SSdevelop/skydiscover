"""The hooks that keep a run going and its history safe.

- budget_guard: during the synthesis loop the lead may not end its turn before the budget is spent,
  except once per `spec.run pause`, after the run is done, or after five refusals without progress;
- history_guard: agents may not delete, move, or overwrite the knowledge base, a run's snapshots,
  or a checkpoint; an edited knowledge-base file is backed up first;
- a hook error is never lost: it falls back to the runs folder when the run dir cannot be written,
  and `run finish` reports it.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path

import pytest

from skydiscover.synthesize.spec import run as spec_run
from skydiscover.synthesize.spec.paths import Run

HOOKS = Path(__file__).resolve().parents[2] / "skydiscover/synthesize/workflow/hooks"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_test_{name}", HOOKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def budget_guard():
    return _load("budget_guard")


@pytest.fixture(scope="module")
def history_guard():
    return _load("history_guard")


def _project(tmp_path: Path, *, impl: bool = True, checkpoints: int = 0) -> Run:
    run = Run(tmp_path / "project" / ".skydiscover" / "kv").create()
    run.task.write_text("---\ndomain: kv store\n---\n# Task\n", encoding="utf-8")
    if impl:
        run.impl.mkdir(parents=True, exist_ok=True)
        (run.impl / "store.py").write_text("v1\n")
    if checkpoints:
        out = tmp_path / "project" / "outputs" / "kv_1"
        run.output_pointer.write_text("outputs/kv_1\n")
        for k in range(1, checkpoints + 1):
            cp = out / "checkpoints" / f"checkpoint_{k}"
            cp.mkdir(parents=True)
            (cp / "score.json").write_text(json.dumps({"created_at": f"2026-01-0{k}T00:00:00Z"}))
    return run


def _stop(run: Run) -> dict:
    return {"cwd": str(run.path.parent.parent), "transcript_path": "", "hook_event_name": "Stop"}


# ------------------------------------------------------------------------------ budget guard


def test_budget_guard_holds_the_lead_to_the_budget(tmp_path, budget_guard, monkeypatch):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    run = _project(tmp_path, checkpoints=1)
    assert spec_run.main(["budget", str(run.path), "3"]) == 0
    reason = budget_guard.decide(_stop(run))
    assert reason and "1 of 3" in reason and "iteration 2" in reason and "spec.run pause" in reason

    # a pause lets exactly one stop through, and is kept, not deleted
    assert spec_run.main(["pause", str(run.path), "--reason", "which server?"]) == 0
    assert budget_guard.decide(_stop(run)) is None
    assert not (run.path / "pause.json").exists() and list(run.path.glob("pause.*.json"))
    assert budget_guard.decide(_stop(run))

    # a finished run may stop
    (run.path.parent / "kv.done").write_text("outputs/kv_1\n")
    assert budget_guard.decide(_stop(run)) is None


def test_budget_guard_never_traps_a_lead_that_cannot_progress(tmp_path, budget_guard, monkeypatch):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    run = _project(tmp_path)
    (run.path / "budget.json").write_text(json.dumps({"iterations": 20}))
    refusals = [budget_guard.decide(_stop(run)) for _ in range(budget_guard.MAX_BLOCKS + 1)]
    assert all(refusals[:-1]) and refusals[-1] is None
    log = [json.loads(x) for x in (run.path / "budget_guard.log.jsonl").read_text().splitlines()]
    assert log[-1]["allowed"].startswith(f"{budget_guard.MAX_BLOCKS} refusals")


def test_budget_guard_stays_out_of_the_specification_and_a_spent_budget(tmp_path, budget_guard, monkeypatch):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    spec_only = _project(tmp_path / "a", impl=False)
    (spec_only.path / "budget.json").write_text(json.dumps({"iterations": 20}))
    assert budget_guard.decide(_stop(spec_only)) is None  # the lead asks the user questions here
    spent = _project(tmp_path / "b", checkpoints=2)
    (spent.path / "budget.json").write_text(json.dumps({"iterations": 2}))
    assert budget_guard.decide(_stop(spent)) is None


@pytest.mark.parametrize(
    "row, n",
    [
        ({"title": "Budget: 50 iterations (user, 2026-09-28)", "detail": "..."}, 50),
        ({"title": "knowledge base wiki: skip", "detail": "The Step 4 budget is Quick (20 iterations)"}, 20),
        ({"title": "budget", "detail": "Thorough"}, 200),
    ],
)
def test_budget_is_read_from_the_decision_log_when_not_recorded(tmp_path, budget_guard, row, n):
    run = _project(tmp_path)
    run.decision_log.write_text(json.dumps([{"title": "unrelated 7 iterations", "detail": ""}, row]))
    assert budget_guard.budget(run.path) == n


def test_budget_guard_blocks_through_the_hook_protocol(tmp_path, budget_guard, monkeypatch, capsys):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    run = _project(tmp_path)
    (run.path / "budget.json").write_text(json.dumps({"iterations": 2}))
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(_stop(run))))
    assert budget_guard.main() == 0
    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block" and "0 of 2" in out["reason"]


# ------------------------------------------------------------------------------ history guard


@pytest.fixture
def kb(tmp_path, monkeypatch):
    root = tmp_path / "kbhome"
    (root / "kv-store" / "wiki").mkdir(parents=True)
    (root / "kv-store" / "wiki" / "page.md").write_text("old page\n")
    (root / "kv-store" / "decisions.json").write_text("[]\n")
    (root / "kv-store" / "runs" / "kv_1" / "snapshots").mkdir(parents=True)
    (root / ".cache" / "sources" / "repo").mkdir(parents=True)
    monkeypatch.setenv("SKYDISCOVER_HOME", str(root))
    return root.resolve()


def _bash(command: str, cwd: Path) -> dict:
    return {"tool_name": "Bash", "tool_input": {"command": command}, "cwd": str(cwd)}


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf {kb}/kv-store/wiki/page.md",
        "rm -rf {kb}/kv-store",
        "cd {kb} && rm -rf kv-store/runs",
        "mv {kb}/kv-store/decisions.json /tmp/x",
        "find {kb}/kv-store -name '*.md' -delete",
        "python3 -c \"import shutil; shutil.rmtree('{kb}/kv-store/runs')\"",
        "rm -rf {out}/checkpoints/checkpoint_2",
        "echo x > {kb}/kv-store/runs/kv_1/index.json",
    ],
)
def test_history_guard_refuses_deleting_or_moving_saved_history(tmp_path, kb, history_guard, command):
    out = tmp_path / "project" / "outputs" / "kv_1"
    reason = history_guard.decide(_bash(command.format(kb=kb, out=out), tmp_path))
    assert reason and reason.startswith("history-guard:")


@pytest.mark.parametrize(
    "command",
    [
        "rm -rf {kb}/kv-store/wiki/tests/__pycache__ {kb}/kv-store/wiki/x.pyc",
        "rm -rf {kb}/.cache/sources/repo",
        "ls -la {kb}/kv-store",
        "rm -rf {tmp}/project/.skydiscover/kv/synthesis/impl/build",
        "python3 -m skydiscover.synthesize.spec.run finish .skydiscover/kv --export-to . --delete-run",
    ],
)
def test_history_guard_lets_everything_else_through(tmp_path, kb, history_guard, command):
    assert history_guard.decide(_bash(command.format(kb=kb, tmp=tmp_path), tmp_path)) is None


def test_history_guard_backs_up_before_an_edit_and_refuses_archives(tmp_path, kb, history_guard):
    page = kb / "kv-store" / "wiki" / "page.md"
    write = {"tool_name": "Write", "tool_input": {"file_path": str(page)}, "cwd": str(tmp_path)}
    assert history_guard.decide(write) is None
    (kept,) = kb.glob(".history/*/kv-store/wiki/page.md")
    assert kept.read_text() == "old page\n"

    redirect = _bash(f"echo '[1]' > {kb}/kv-store/decisions.json", tmp_path)
    assert history_guard.decide(redirect) is None
    assert list(kb.glob(".history/*/kv-store/decisions.json"))

    snap = kb / "kv-store" / "runs" / "kv_1" / "snapshots" / "x.json"
    edit = {"tool_name": "Edit", "tool_input": {"file_path": str(snap)}, "cwd": str(tmp_path)}
    assert history_guard.decide(edit).startswith("history-guard:")


# ------------------------------------------------------------------------------ hook errors


def test_a_hook_error_is_never_lost(tmp_path):
    tu = _load("token_usage")
    run = _project(tmp_path)
    os.chmod(run.path, 0o500)  # the run dir cannot be written
    try:
        tu._log_error(run.path, "boom")
    finally:
        os.chmod(run.path, 0o755)
    fallback = run.path.parent / "hook_errors.log"
    text = fallback.read_text()
    assert "boom" in text and f"run={run.path}" in text and "could not write" in text
    lines = spec_run._hook_error_lines(run)
    assert "WARNING: 1 line(s)" in lines[0] and "boom" in lines[1]
    assert spec_run._hook_error_lines(_project(tmp_path / "other")) == ["  hook errors: none"]
