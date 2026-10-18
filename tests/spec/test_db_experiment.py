from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from skydiscover.synthesize.spec import iterations

SCRIPTS = Path(__file__).resolve().parents[2] / "experiments/db-verus"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


runner = load("db_experiment", "run.py")
trace = load("db_trace", "trace.py")


@pytest.fixture
def project(tmp_path, monkeypatch):
    target = tmp_path / "experiment with spaces"
    spec = tmp_path / "specs.rs"
    spec.write_text("#[macro_export] macro_rules! impl_db { () => {} }\n")
    monkeypatch.setattr(runner.shutil, "which", lambda name: "/usr/bin/true")

    # Installation is exercised separately against the actual local checkout. Keep this test
    # independent of Codex, Verus, network access and user configuration.
    def command(command, **kwargs):
        if command[0] == "bash":
            path = Path(command[-1]) / ".codex"
            path.mkdir()
            runner.write_json(path / "hooks.json", {"hooks": {}})
        return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    monkeypatch.setattr(runner.subprocess, "run", command)
    assert (
        runner.main(
            ["prepare", "--project", str(target), "--spec", str(spec), "--verus", "/usr/bin/true"]
        )
        == 0
    )
    return target, spec


def test_setup_freezes_original_and_configures_end_to_end_budget(project):
    target, original = project
    manifest = runner.read_json(target / ".experiment/manifest.json")
    run = target / manifest["run"]
    assert (run / "specification/specs.rs").read_bytes() == original.read_bytes()
    budget = runner.read_json(run / "budget.json")
    assert budget["iterations"] == 50 and budget["audit_every"] == 1
    assert len(runner.read_json(run / "specification/verus.json")["obligations"]) == 5
    baseline = target / manifest["baseline"]
    assert (baseline / "source/lib.rs").read_bytes() == (
        SCRIPTS / "baselines/node_locked_bptree/lib.rs"
    ).read_bytes()
    assert "node/leaf-locked" in (run / "task.md").read_text()
    assert "{{BASELINE}}" not in (run / "task.md").read_text()
    initial = runner.read_json(target / ".experiment/settings.json")["initial_implementation"]
    candidate = run / "synthesis/impl/lib.rs"
    assert candidate.read_bytes() == (baseline / "source/lib.rs").read_bytes()
    assert initial["sha256"] == runner.digest(candidate)
    assert initial["path"] == str(candidate.parent.relative_to(target))
    assert initial["proof_status"] == "unverified"
    assert candidate.stat().st_ino != (baseline / "source/lib.rs").stat().st_ino
    assert candidate.stat().st_mode & 0o200
    assert not list((run / "synthesis/impl").glob("checks"))
    assert iterations.finished(run) == []
    assert not (run / "synthesis/audit/current.json").exists()
    assert "retain this implementation" in (run / "task.md").read_text()
    # The working seed may evolve without modifying or invalidating the frozen comparator.
    candidate.write_text(candidate.read_text() + "\n// candidate-only change\n")
    assert runner.digest(baseline / "source/lib.rs") == initial["sha256"]
    assert not (target / ".experiment/audit-history.json").exists()
    original.write_text("changed upstream after preparation")
    runner.integrity(target, manifest)  # frozen experiment is independent of upstream changes
    assert runner.main(["prepare", "--project", str(target)]) == 2
    assert runner.main(["launch", "--project", str(target), "--headless", "--dry-run"]) == 0
    frozen = run / "specification/specs.rs"
    frozen.chmod(0o644)
    frozen.write_text("changed frozen contract")
    with pytest.raises(ValueError, match="frozen experiment input changed"):
        runner.integrity(target, manifest)


def test_prepare_defaults_to_bundled_spec_from_another_directory(project, tmp_path, monkeypatch):
    target = tmp_path / "bundled specification experiment"
    monkeypatch.chdir(tmp_path)
    assert runner.main([
        "prepare", "--project", str(target), "--verus", "/usr/bin/true",
    ]) == 0
    manifest = runner.read_json(target / ".experiment/manifest.json")
    bundled = SCRIPTS / "specification/specs.rs"
    relative = manifest["run"] + "/specification/specs.rs"
    assert (target / relative).read_bytes() == bundled.read_bytes()
    assert manifest["frozen"][relative] == runner.digest(bundled)
    assert not (target / relative).stat().st_mode & 0o222
    provenance = runner.read_json(target / ".experiment/provenance.json")
    assert provenance["source"] == str(bundled.resolve())
    runner.integrity(target, manifest)


def test_baseline_snapshot_rejects_changed_or_added_source(project):
    target, _ = project
    manifest = runner.read_json(target / ".experiment/manifest.json")
    baseline = target / manifest["baseline"] / "source"
    extra = baseline / "unexpected.rs"
    extra.write_text("// new source must not silently enter the frozen baseline\n")
    with pytest.raises(ValueError, match="baseline source tree changed"):
        runner.integrity(target, manifest)
    extra.unlink()
    library = baseline / "lib.rs"
    library.chmod(0o644)
    library.write_text(library.read_text() + "\n// changed\n")
    with pytest.raises(ValueError, match="frozen experiment input changed"):
        runner.integrity(target, manifest)


def test_measurement_build_runs_driver_against_erased_atomic_api(tmp_path):
    verus = shutil.which(os.environ.get("SKYDISCOVER_VERUS", "verus"))
    if not verus:
        pytest.skip("real Verus integration requires an installed verifier")
    impl = tmp_path / "impl"
    impl.mkdir()
    (impl / "lib.rs").write_text("""
use vstd::prelude::*;
verus! { pub struct Counter {} }
crate::audit_counter!();
""")
    spec = tmp_path / "specs.rs"
    spec.write_text("""
use vstd::prelude::*;
#[macro_export]
macro_rules! audit_counter {
    () => { verus! { impl Counter {
        pub fn count(n: u64) -> (result: u64)
            atomically (lp) {
                (state: int) -> (post: vstd::atomic::Commit<int>),
                requires state == n,
                ensures post@ == state,
            },
            ensures result == n,
        { n }
    } } };
}
""")
    (tmp_path / "verus.json").write_text('{"macro_imports": {}}')
    driver = tmp_path / "driver.rs"
    driver.write_text('fn main() { println!("{}", Counter::count(41)); }')
    binary = tmp_path / "benchmark"
    result = subprocess.run([
        sys.executable, str(SCRIPTS / "build.py"), "--impl", str(impl), "--spec", str(spec),
        "--driver", str(driver), "--out", str(binary), "--verus", verus,
    ], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert subprocess.check_output([str(binary)], text=True, timeout=10) == "41\n"
    assert "--no-lifetime" not in result.stdout


def test_final_round_requires_audit_and_history_survives_next_round(project, monkeypatch):
    target, _ = project
    manifest = runner.read_json(target / ".experiment/manifest.json")
    run = target / manifest["run"]
    monkeypatch.setitem(sys.modules, "run", runner)
    guard = load("db_guard", "guard.py")
    iterations.close(run, "failed", reason="candidate failed")
    assert "proof audit" in guard.reason(target, {}, stopping=True)
    runner.write_json(
        run / "synthesis/audit/completed.json",
        {"finished_iterations": 1, "session": "first", "status": "incomplete", "bindings": {}},
    )
    # Preserve legacy history for inspection, but never count it as scheduling completion.
    runner.write_json(target / ".experiment/audit-history.json", [
        {"finished_iterations": 1, "session": "first", "status": "handoff", "bindings": {}},
    ])
    assert "proof audit" in guard.reason(target, {}, stopping=True)
    assert runner.summarize(target, manifest)["missing_audits"] == [1]
    runner.write_json(
        run / "synthesis/audit/completed.json",
        {"finished_iterations": 1, "session": "first", "status": "verified", "bindings": {}},
    )
    assert guard.reason(target, {}, stopping=True) is None
    iterations.close(run, "failed", reason="next candidate failed")
    runner.write_json(
        run / "synthesis/audit/completed.json",
        {"finished_iterations": 2, "session": "second", "status": "verified", "bindings": {}},
    )
    report = runner.summarize(target, manifest)
    assert report["audited_iterations"] == [1, 2]
    assert report["complete"] is False
    assert report["missing_audits"] == []
    iterations.close(run, "failed", reason="unchanged candidate replay")
    runner.write_json(
        run / "synthesis/audit/completeness.json",
        {
            "finished_iterations": 3,
            "audit_evidence": {"session": "first", "mode": "proof", "bindings": {}},
        },
    )
    assert guard.reason(target, {}, stopping=True) is None
    assert runner.summarize(target, manifest)["audited_iterations"] == [1, 2, 3]


def test_trace_has_exact_mix_and_reproducible_independent_workers():
    settings = {"threads": 2, "operations_per_thread": 1000, "seed": 4, "keys": 100}
    a = list(trace.operations(settings, 0))
    assert a == list(trace.operations(settings, 0))
    assert a != list(trace.operations(settings, 1))
    assert sum(row["op"] == "get" for row in a) == 600
    assert sum(row["op"] == "put" for row in a) == 400
    assert all(len(row["key"]) == 16 and int(row["key"], 16) < 100 for row in a)
    assert all(-(2**31) <= row["value"] < 2**31 for row in a if row["op"] == "put")


def test_guard_blocks_iteration_51_and_honors_explicit_pause(project, monkeypatch):
    target, _ = project
    manifest = runner.read_json(target / ".experiment/manifest.json")
    run = target / manifest["run"]
    monkeypatch.setitem(sys.modules, "run", runner)
    guard = load("db_guard_cap", "guard.py")
    for _ in range(50):
        iterations.close(run, "failed", reason="test round")
    assert "budget is spent" in guard.reason(
        target, {"tool_name": "spawn_agent", "tool_input": {"agent_type": "coding-agent"}}
    )
    assert "proof audit" in guard.reason(target, {}, stopping=True)
    runner.write_json(run / "pause.json", {"reason": "installation blocker"})
    assert guard.reason(target, {}, stopping=True) is None


def test_both_stop_guards_honor_pause_in_any_order_until_resume(project, monkeypatch):
    from skydiscover.synthesize.spec import run as spec_run
    from skydiscover.synthesize.workflow.hooks import budget_guard

    target, _ = project
    manifest = runner.read_json(target / ".experiment/manifest.json")
    run = target / manifest["run"]
    monkeypatch.setitem(sys.modules, "run", runner)
    monkeypatch.setenv("SKYDISCOVER_RUN", str(run))
    guard = load("db_guard_pause", "guard.py")
    iterations.close(run, "failed", reason="pending proof")
    payload = {"cwd": str(target)}
    assert spec_run.main(["pause", str(run), "--reason", "adapter needs repair"]) == 0
    for _ in range(3):
        assert guard.reason(target, payload, stopping=True) is None
        assert budget_guard.decide(payload) is None
        assert guard.reason(target, payload, stopping=True) is None
    assert spec_run.main(["resume", str(run)]) == 0
    assert "proof audit" in guard.reason(target, payload, stopping=True)
    assert budget_guard.decide(payload)
