"""Bounding each iteration's cost, as in the paper:

- an iteration ends in a checkpoint or fails (spec/iterations.py); a failed one counts toward the
  budget, and the archive files later work under the right iteration;
- loop_guard caps coding-agent launches per iteration and runs the auditor every N iterations,
  never refusing its Phase 3 reviews;
- spec.reuse copies an earlier run's discovery and marks which systems are still fresh.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import subprocess
from pathlib import Path

import pytest

from skydiscover.synthesize.spec import archive, checkpoint, iterations, reuse
from skydiscover.synthesize.spec import run as spec_run
from skydiscover.synthesize.spec.paths import Run

HOOKS = Path(__file__).resolve().parents[2] / "skydiscover/synthesize/workflow/hooks"


def _load(name):
    spec = importlib.util.spec_from_file_location(f"_loop_{name}", HOOKS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def loop_guard():
    return _load("loop_guard")


@pytest.fixture(scope="module")
def budget_guard():
    return _load("budget_guard")


def _run(root: Path, slug: str = "kv", *, impl: bool = True) -> Run:
    run = Run(root / "project" / ".skydiscover" / slug).create()
    run.task.write_text("---\ndomain: kv store\n---\n# Task\n", encoding="utf-8")
    if impl:
        run.impl.mkdir(parents=True, exist_ok=True)
        (run.impl / "store.py").write_text("v1\n")
    return run


def _launch(run: Run, *, subagent_type: str = "", prompt: str = "") -> dict:
    return {
        "tool_name": "Agent",
        "tool_input": {"subagent_type": subagent_type, "prompt": prompt, "description": "x"},
        "cwd": str(run.path.parent.parent),
        "transcript_path": "",
    }


# ------------------------------------------------------------------------------ iterations


def test_an_iteration_ends_in_a_checkpoint_or_fails(tmp_path):
    run = _run(tmp_path)
    assert iterations.current(run.path) == 1
    assert iterations.close(run.path, "failed", reason="hangs at 16 threads") == 1
    assert iterations.close(run.path, "checkpoint", checkpoint="checkpoint_1") == 2
    assert iterations.close(run.path, "checkpoint", checkpoint="checkpoint_1") == 2  # idempotent
    assert iterations.current(run.path) == 3
    assert [r["outcome"] for r in iterations.finished(run.path)] == ["failed", "checkpoint"]
    with pytest.raises(ValueError):
        iterations.close(run.path, "abandoned")
    assert iterations.current(_run(tmp_path / "spec", impl=False).path) == 0


def test_a_run_without_a_record_is_read_from_its_checkpoints(tmp_path):
    run = _run(tmp_path)
    out = tmp_path / "project" / "outputs" / "kv_1"
    run.output_pointer.write_text("outputs/kv_1\n")
    for k in (1, 2):
        cp = out / "checkpoints" / f"checkpoint_{k}"
        cp.mkdir(parents=True)
        (cp / "score.json").write_text(json.dumps({"created_at": f"2026-01-0{k}T00:00:00Z"}))
    assert iterations.current(run.path) == 3
    # the first write seeds the record from the checkpoints
    assert iterations.close(run.path, "failed", reason="x") == 3
    assert [r["checkpoint"] for r in iterations.finished(run.path)] == [
        "checkpoint_1",
        "checkpoint_2",
        None,
    ]


def test_the_archive_and_the_budget_count_a_failed_iteration(tmp_path, budget_guard):
    run = _run(tmp_path)
    assert spec_run.main(["budget", str(run.path), "2"]) == 0
    assert budget_guard.decide({"cwd": str(run.path.parent.parent)})  # 0 of 2: keep going
    iterations.close(run.path, "failed", reason="no candidate passed")
    snap = archive.snapshot(run.path, "turn")
    assert "_iter-02_" in snap.name, "work after a failed round belongs to the next iteration"
    iterations.close(run.path, "failed", reason="again")
    assert budget_guard.decide({"cwd": str(run.path.parent.parent)}) is None  # 2 of 2: done


# ------------------------------------------------------------------------------ loop guard


def test_coding_attempts_are_capped_per_iteration(tmp_path, loop_guard, monkeypatch):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    run = _run(tmp_path)
    assert (
        spec_run.main(["budget", str(run.path), "10", "--attempts", "2", "--audit-every", "3"]) == 0
    )
    coder = _launch(run, subagent_type="skysynth:2-synthesis-loop:coding-agent")
    assert loop_guard.decide(coder) is None
    assert loop_guard.decide(coder) is None
    refused = loop_guard.decide(coder)
    assert refused and "2 coding attempts" in refused and "spec.iterations fail" in refused
    # a coding agent launched as a general-purpose agent with the brief is still counted
    by_brief = _launch(
        run, subagent_type="general-purpose", prompt="Read agents/2-synthesis-loop/coding-agent.md"
    )
    assert loop_guard.decide(by_brief)
    # closing the round starts a fresh count
    iterations.close(run.path, "failed", reason="spent")
    assert loop_guard.decide(coder) is None
    # other agents are never counted
    assert loop_guard.decide(_launch(run, subagent_type="Explore")) is None


def test_the_auditor_runs_every_n_iterations_and_always_in_phase_3(
    tmp_path, loop_guard, monkeypatch
):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    run = _run(tmp_path)
    assert spec_run.main(["budget", str(run.path), "10", "--audit-every", "2"]) == 0
    auditor = _launch(
        run, subagent_type="skysynth:2-synthesis-loop:auditor", prompt="audit iteration"
    )
    assert "every 2 iterations" in loop_guard.decide(auditor)  # iteration 1: not due
    for _ in range(2):
        iterations.close(run.path, "failed", reason="x")
    assert loop_guard.decide(auditor) is None  # 2 finished: due
    assert loop_guard.decide(auditor) is None  # a failed/interrupted launch can be retried
    checkpoint.stamp_audit(run.path, [])
    assert json.loads(run.completeness.read_text())["finished_iterations"] == 2
    assert loop_guard.decide(auditor)  # completion, not launch, advances the interval
    final = _launch(
        run, subagent_type="skysynth:2-synthesis-loop:auditor", prompt="production lens: security"
    )
    assert loop_guard.decide(final) is None
    log = [json.loads(x) for x in (run.path / "loop_guard.log.jsonl").read_text().splitlines()]
    assert [e.get("allowed") or "refused" for e in log] == [
        "refused",
        "auditor",
        "auditor",
        "refused",
        "auditor",
    ]


def test_generic_proof_handoff_keeps_current_audit_open(
    tmp_path, loop_guard, monkeypatch
):
    from skydiscover.synthesize.spec import proof

    run = _run(tmp_path)
    monkeypatch.setenv("SKYDISCOVER_RUN", str(run.path))
    run.task.write_text("---\naudit_mode: proof\naudit_target: target.rs\n---\n")
    (run.impl / "target.rs").write_text("pub fn f() {}\n")
    spec_run.main(["budget", str(run.path), "10", "--audit-every", "1"])
    iterations.close(run.path, "failed", reason="first round")
    planner = _launch(run, subagent_type="planner")
    assert "audit is due" in loop_guard.decide(planner)
    assert loop_guard.decide(_launch(run, subagent_type="proof-planner")) is None
    assert proof.start(run.path)["status"] == "blocked"  # missing frozen Verus spec
    assert "audit is due" in loop_guard.decide(planner)
    proof.feedback(run.path, "spec-builder", "supply the required formal contract")
    assert "audit is due" in loop_guard.decide(planner)
    with pytest.raises(proof.ProofError):
        checkpoint.stamp_audit(run.path, [])
    assert not run.completeness.exists()


@pytest.mark.parametrize("status", ["incomplete", "unsupported", "blocked", "tool_error", "handoff",
                                    "implementation_blocked"])
def test_legacy_failure_or_unbacked_blocker_does_not_release_schedule(tmp_path, loop_guard, status):
    run = _run(tmp_path)
    run.audit.mkdir(parents=True, exist_ok=True)
    (run.audit / "completed.json").write_text(json.dumps({
        "session": "legacy", "bindings": {}, "finished_iterations": 1, "status": status,
    }))
    assert loop_guard.last_audit_finished(run.path, 1) == 0


def test_due_audit_blocks_next_iteration_until_stamped(tmp_path, loop_guard, monkeypatch):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    run = _run(tmp_path)
    spec_run.main(["budget", str(run.path), "10", "--audit-every", "2", "--attempts", "1"])
    coder = _launch(run, subagent_type="coding-agent")
    planner = _launch(run, prompt="Read agents/2-synthesis-loop/planner.md")
    auditor = _launch(run, subagent_type="auditor")
    iterations.close(run.path, "failed", reason="first round")
    assert loop_guard.decide(planner) is None
    assert loop_guard.decide(coder) is None
    iterations.close(run.path, "failed", reason="second round")

    # Skipping the auditor does not let the lead begin another iteration.
    assert "audit is due" in loop_guard.decide(planner)
    assert "audit is due" in loop_guard.decide(coder)
    assert loop_guard.decide(_launch(run, subagent_type="critic")) is None
    assert loop_guard.decide(auditor) is None
    assert "audit is due" in loop_guard.decide(coder)  # launch alone is insufficient
    checkpoint.stamp_audit(run.path, [])
    assert loop_guard.decide(planner) is None
    assert loop_guard.decide(coder) is None  # blocked requests did not use coding attempts

    # New implementation bytes do not trigger an off-schedule audit.
    (run.impl / "store.py").write_text("v2\n")
    iterations.close(run.path, "failed", reason="third round")
    assert loop_guard.decide(coder) is None
    assert loop_guard.decide(auditor)
    iterations.close(run.path, "failed", reason="fourth round")
    assert "audit is due" in loop_guard.decide(coder)
    assert loop_guard.decide(auditor) is None


def test_default_audit_interval_is_enforced_without_a_budget(tmp_path, loop_guard, monkeypatch):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    monkeypatch.setattr(loop_guard, "DEFAULT_AUDIT_EVERY", 15)
    run = _run(tmp_path)
    coder = _launch(run, subagent_type="coding-agent")
    auditor = _launch(run, subagent_type="auditor")
    for _ in range(14):
        iterations.close(run.path, "failed", reason="x")
    assert loop_guard.decide(coder) is None
    assert loop_guard.decide(auditor)
    iterations.close(run.path, "failed", reason="x")
    assert "audit is due" in loop_guard.decide(coder)
    assert loop_guard.decide(auditor) is None


@pytest.mark.parametrize(
    "stamp", [None, {}, {"finished_iterations": 99}, {"finished_iterations": "2"}]
)
def test_launch_history_or_invalid_stamp_does_not_satisfy_due_audit(
    tmp_path,
    loop_guard,
    monkeypatch,
    stamp,
):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    run = _run(tmp_path)
    spec_run.main(["budget", str(run.path), "10", "--audit-every", "2"])
    for _ in range(2):
        iterations.close(run.path, "failed", reason="x")
    (run.path / ".loop_guard.json").write_text(json.dumps({"last_audit_done": 2}))
    if stamp is not None:
        run.audit.mkdir(parents=True, exist_ok=True)
        run.completeness.write_text(json.dumps(stamp))
    assert "audit is due" in loop_guard.decide(_launch(run, subagent_type="coding-agent"))


def test_final_audit_is_allowed_after_budget_even_between_intervals(
    tmp_path, loop_guard, monkeypatch
):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    run = _run(tmp_path)
    spec_run.main(["budget", str(run.path), "1", "--audit-every", "15"])
    iterations.close(run.path, "failed", reason="budget spent")
    assert loop_guard.decide(_launch(run, subagent_type="auditor")) is None


@pytest.mark.parametrize(
    "manifest, tool_name",
    [("hooks/hooks.json", "Agent"), ("adapters/codex/hooks.json", "spawn_agent")],
)
def test_audit_schedule_through_registered_hook_command(tmp_path, monkeypatch, manifest, tool_name):
    """Run the manifest's shell command, including its exit-code handling and tool payload."""
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    run = _run(tmp_path)
    spec_run.main(["budget", str(run.path), "10", "--audit-every", "2"])
    workflow = HOOKS.parent
    manifest_path = workflow / manifest
    if tool_name == "spawn_agent":
        # Exercise the actual installer and its idempotent merge, not just the shipped template.
        project = run.path.parent.parent
        for _ in range(2):
            installed = subprocess.run(
                [
                    "bash",
                    str(workflow.parent / "scripts" / "install.sh"),
                    "--agent",
                    "codex",
                    str(project),
                ],
                capture_output=True,
                text=True,
            )
            assert installed.returncode == 0, installed.stdout + installed.stderr
        manifest_path = project / ".codex" / "hooks.json"
    hooks = json.loads(manifest_path.read_text())["hooks"]["PreToolUse"]
    commands = [
        h["command"]
        for entry in hooks
        if re.fullmatch(entry["matcher"], tool_name)
        for h in entry["hooks"]
        if "loop_guard.py" in h["command"]
    ]
    assert len(commands) == 1
    env = {**os.environ, "CLAUDE_PLUGIN_ROOT": str(workflow), "PLUGIN_ROOT": str(workflow)}

    def launch(role):
        payload = _launch(run, subagent_type=role)
        payload["tool_name"] = tool_name
        if tool_name == "spawn_agent":
            payload["tool_input"] = {"agent_type": "default", "message": f"Read {role}.md"}
        return subprocess.run(
            commands[0],
            shell=True,
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            env=env,
            cwd=run.path,
        )

    assert launch("auditor").returncode == 2
    for _ in range(2):
        iterations.close(run.path, "failed", reason="x")
    blocked = launch("coding-agent")
    assert blocked.returncode == 2 and "audit is due" in blocked.stderr
    assert launch("auditor").returncode == 0
    assert launch("coding-agent").returncode == 2
    checkpoint.stamp_audit(run.path, [])
    assert launch("coding-agent").returncode == 0
    assert launch("auditor").returncode == 2


def test_the_specification_is_never_limited(tmp_path, loop_guard, monkeypatch):
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    run = _run(tmp_path, impl=False)
    (run.path / "budget.json").write_text(json.dumps({"iterations": 5, "max_coding_attempts": 1}))
    coder = _launch(run, subagent_type="coding-agent")
    assert loop_guard.decide(coder) is None and loop_guard.decide(coder) is None


# ------------------------------------------------------------------------------ reuse


def _git_repo(path: Path) -> str:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / "a.c").write_text("int x;\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "c"],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    ).stdout.strip()


def test_reuse_copies_an_earlier_runs_discovery_and_marks_what_is_fresh(tmp_path):
    clone = tmp_path / "cache" / "owner-sys1"
    head = _git_repo(clone)
    first = _run(tmp_path, "kv", impl=False)
    for name, sha in (("sys1", head), ("sys2", "0" * 40)):
        d = first.references / name
        d.mkdir(parents=True)
        (d / "spec.json").write_text(
            json.dumps({"source": f"owner/{name} @ {sha}", "axes": {"a": 1}})
        )
        (d / "verification.json").write_text('{"ok": true}\n')
    (first.references / "axes.json").write_text('{"axes": []}\n')
    first.sources.mkdir(parents=True, exist_ok=True)
    (first.sources / "sys1").symlink_to(clone)
    (first.specification / "questions.json").write_text('[{"id": "task-specific"}]\n')
    archive.snapshot(first.path, "after-spec-builder")

    second = _run(tmp_path, "kv-2", impl=False)
    report = reuse.reuse(second.path)
    assert report["fresh"] == ["sys1"] and report["to_reverify"] == ["sys2"]
    assert report["systems"]["sys2"]["clone"] == "missing"
    assert (second.references / "sys1" / "verification.json").is_file()
    assert (second.references / "axes.json").is_file()
    assert (second.sources / "sys1").is_symlink()
    assert not second.questions.exists(), "questions belong to the task, not the domain"
    (second.references / "sys1" / "spec.json").write_text("{}")  # the copy is writable
    acq = json.loads(second.acquisitions.read_text())
    assert acq["reused_from_run"]["systems"] == ["sys1", "sys2"]
    assert spec_run._has_run_reuse(second)
    with pytest.raises(ValueError):
        reuse.reuse(second.path)  # never overwrites discovery silently


def test_reuse_finds_nothing_in_a_cold_domain(tmp_path, capsys):
    run = _run(tmp_path, "cold", impl=False)
    run.task.write_text("---\ndomain: brand new\n---\n", encoding="utf-8")
    assert reuse.main([str(run.path)]) == 0
    assert "run full discovery" in capsys.readouterr().out
