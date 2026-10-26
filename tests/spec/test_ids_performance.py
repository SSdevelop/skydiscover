"""Toolchain-free checks of IDS orchestration, not evidence of a real theorem proof."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from skydiscover.synthesize.spec import checkpoint, evaluation
from skydiscover.synthesize.spec import evaluation_authority as authority
from skydiscover.synthesize.spec import ids, iterations
from skydiscover.synthesize.spec import run as spec_run
from skydiscover.synthesize.spec.paths import Run

pytestmark = pytest.mark.usefixtures("fake_evaluation_authority")


def _run(tmp_path: Path) -> Run:
    run = Run(tmp_path / "project/.skydiscover/demo").create()
    run.task.write_text("---\ndomain: counter\nchecked_by: proof\noptimize: performance\n---\n")
    run.impl.mkdir()
    (run.impl / "code.txt").write_text("value=1\n")
    (run.impl / "proof.txt").write_text("verified\n")
    run.interface.mkdir(parents=True)
    (run.interface / "contract.txt").write_text("value=1\n")
    run.tests.mkdir()
    run.test_script.write_text("bash proof.sh\n")
    (run.tests / "proof.sh").write_text(
        'grep -qx "value=1" "$SKYDISCOVER_IMPL/code.txt" && '
        'grep -qx verified "$SKYDISCOVER_IMPL/proof.txt"\n'
    )
    (run.evaluator / "score.py").write_text(
        "import os\nfrom pathlib import Path\n"
        "root = Path(os.environ['SKYDISCOVER_RUN'])\n"
        "assert root.name == 'inputs'\n"
        "assert Path(os.environ['SKYDISCOVER_IMPL']).is_relative_to(root)\n"
        "print('{\"throughput\": 12}')\n"
    )
    return run


def _evaluate(run: Run, **kwargs) -> Path:
    receipt = evaluation.evaluate(
        run.path, [sys.executable, "synthesis/evaluator/score.py"], {"threads": 4}, **kwargs
    )
    checkpoint._write_json(receipt, authority.seal(json.loads(receipt.read_text())))
    return receipt


def _record(run: Run, receipt: Path) -> dict:
    doc = json.loads(receipt.read_text())
    row = {
        "impl": doc["impl"],
        "input_digests": doc["input_digests"],
        "config": doc["config"],
        "evaluation": receipt.relative_to(run.path).as_posix(),
        "metrics": doc["metrics"],
        "objective": "throughput",
    }
    run.leaderboard.write_text(json.dumps([row]))
    return row


def test_correctness_and_scoring_are_independent(tmp_path):
    run = _run(tmp_path)
    assert run.is_proof_run() and run.is_scored_run() and run.co_generates_proof()
    run.task.write_text("---\nchecked_by: proof\n---\n")
    assert run.is_proof_run() and not run.is_scored_run()
    assert checkpoint.score_from_run(run.path)["score"] == {}
    run.task.write_text("plain test-driven task")
    assert run.is_scored_run() and not run.co_generates_proof()
    run.task.write_text("---\nchecked_by: proof\noptimize: typo\n---\n")
    with pytest.raises(ValueError, match="optimize"):
        checkpoint.score_from_run(run.path)


def test_full_check_precedes_benchmark_on_the_same_snapshot(tmp_path, monkeypatch):
    run = _run(tmp_path)
    monkeypatch.setenv("SKYDISCOVER_RUN", "/wrong/inherited/run")
    with (run.tests / "proof.sh").open("a") as f:
        f.write('touch "$SKYDISCOVER_EVALUATION/checked"\n')
    with (run.evaluator / "score.py").open("a") as f:
        f.write("assert (Path(os.environ['SKYDISCOVER_EVALUATION']) / 'checked').exists()\n")
    receipt = _evaluate(run)
    row = _record(run, receipt)
    assert evaluation.validate(run.path, row, checkpoint.input_digests(run.path)) == receipt.parent
    assert "throughput" in (receipt.parent / "measurement.log").read_text()
    assert not (run.path / "checked").exists()


def test_failing_proof_never_launches_the_benchmark(tmp_path):
    run = _run(tmp_path)
    (run.impl / "proof.txt").write_text("unfinished\n")
    with pytest.raises(ValueError, match="exited 1"):
        _evaluate(run)
    assert not list(run.bench.rglob("measurement.log"))
    assert not list(run.bench.rglob("evaluation.json"))
    assert "TESTS FAILED" in next(run.bench.rglob("checks.log")).read_text()


@pytest.mark.parametrize(
    "target", ["impl/code.txt", "impl/proof.txt", "tests/proof.sh", "evaluator/score.py"]
)
def test_benchmark_cannot_change_checked_inputs(tmp_path, target):
    run = _run(tmp_path)
    with (run.evaluator / "score.py").open("a") as f:
        f.write(f"(root / 'synthesis/{target}').write_text('changed')\n")
    with pytest.raises(ValueError, match="Inputs changed"):
        _evaluate(run)
    assert not list(run.bench.rglob("evaluation.json"))
    assert (run.impl / "proof.txt").read_text() == "verified\n"


def test_checker_cannot_modify_inputs_and_then_get_measured(tmp_path):
    run = _run(tmp_path)
    with (run.tests / "proof.sh").open("a") as f:
        f.write('echo changed > "$SKYDISCOVER_IMPL/code.txt"\n')
    with pytest.raises(ValueError, match="Inputs changed"):
        _evaluate(run)
    assert not list(run.bench.rglob("measurement.log"))


@pytest.mark.parametrize(
    "script, message",
    [("raise SystemExit(3)", "exited 3"), ("import time; time.sleep(30)", "timed out")],
)
def test_failed_or_timed_out_measurements_are_not_accepted(tmp_path, script, message):
    run = _run(tmp_path)
    (run.evaluator / "score.py").write_text(script)
    with pytest.raises(ValueError, match=message):
        _evaluate(run, timeout=1)
    assert not list(run.bench.rglob("evaluation.json"))


def test_proof_timeout_prevents_benchmark(tmp_path):
    run = _run(tmp_path)
    (run.tests / "proof.sh").write_text("sleep 30\n")
    with pytest.raises(ValueError, match="timed out"):
        _evaluate(run, check_timeout=1)
    assert not list(run.bench.rglob("measurement.log"))


def test_a_score_without_checked_evaluation_is_refused_even_with_override(tmp_path):
    run = _run(tmp_path)
    run.bench.mkdir()
    run.leaderboard.write_text(
        json.dumps(
            [
                {
                    "impl": "synthesis/impl",
                    "input_digests": checkpoint.input_digests(run.path),
                    "config": {},
                    "metrics": {"throughput": 99},
                }
            ]
        )
    )
    with pytest.raises(ValueError, match="evaluation receipt"):
        checkpoint.snapshot_run(run.path, tmp_path, require_evaluation=False)


@pytest.mark.parametrize("mutation", ["candidate", "proof", "config", "log", "snapshot"])
def test_changed_candidate_or_evidence_cannot_be_checkpointed(tmp_path, mutation):
    run = _run(tmp_path)
    receipt = _evaluate(run)
    row = _record(run, receipt)
    if mutation == "candidate":
        (run.impl / "code.txt").write_text("value=2\n")
    elif mutation == "proof":
        (run.impl / "proof.txt").write_text("new proof\n")
    elif mutation == "config":
        row["config"] = {"threads": 8}
        run.leaderboard.write_text(json.dumps([row]))
    elif mutation == "log":
        (receipt.parent / "measurement.log").write_text("new score")
    else:
        (receipt.parent / "inputs/synthesis/impl/proof.txt").write_text("new proof")
    with pytest.raises(ValueError):
        checkpoint.snapshot_run(run.path, tmp_path)


def test_checkpoint_replays_proof_instead_of_trusting_receipt(tmp_path):
    run = _run(tmp_path)
    with (run.tests / "proof.sh").open("a") as f:
        f.write('test ! -f "$SKYDISCOVER_EVALUATION/recheck.log"\n')
    _record(run, _evaluate(run))
    with pytest.raises(ValueError, match="exited 1"):
        checkpoint.snapshot_run(run.path, tmp_path)
    assert not run.output_pointer.exists()


def test_scored_checkpoint_preserves_code_proof_and_replayable_evidence(tmp_path):
    run = _run(tmp_path)
    _record(run, _evaluate(run))
    out, saved = checkpoint.snapshot_run(run.path, tmp_path, became_best=True)
    assert json.loads((saved / "score.json").read_text())["score"] == {"throughput": 12}
    assert (saved / ".verification/source/proof.txt").read_text() == "verified\n"
    assert (
        saved / ".verification/evaluation/inputs/synthesis/impl/code.txt"
    ).read_text() == "value=1\n"
    assert (out / "best/.verification/evaluation/evaluation.json").is_file()
    assert len(iterations.finished(run.path)) == 1
    checkpoint.stamp_audit(run.path, [])
    spec_run.export_deliverable(run.path, tmp_path)
    assert "TESTS PASSED" in (out / "best/.verification/checks.log").read_text()


def _report(run: Run) -> dict:
    diagnostics = run.synthesis / "diagnostics.txt"
    diagnostics.write_text("unsupported operation X\n")
    return {
        "classification": "unsupported",
        "obligations": ["update_preserves_state"],
        "source_locations": ["synthesis/impl/code.txt:1"],
        "diagnostics": ["synthesis/diagnostics.txt"],
        "attempts": ["Tried a different invariant; operation X is still unsupported"],
        "alternatives": ["Use operation Y", "Extend the adapter"],
        "requested_decision": "Choose a supported synchronization mechanism",
        "performance_impact": "Unknown; the candidate has not been measured",
    }


def test_ids_feedback_is_bound_to_candidate_and_does_not_close_iteration(tmp_path):
    run = _run(tmp_path)
    report = _report(run)
    ids.feedback(run.path, report)
    assert ids.blockers(run.path)[0]["current"] is True
    assert not iterations.finished(run.path)
    assert not run.leaderboard.exists()
    (run.impl / "code.txt").write_text("new design\n")
    assert ids.blockers(run.path)[0]["current"] is False
    assert ids.blockers(run.path)[0]["proof_status"] == "incomplete"


@pytest.mark.parametrize("field", ["diagnostics", "attempts", "alternatives", "requested_decision"])
def test_ids_feedback_requires_actionable_evidence(tmp_path, field):
    run = _run(tmp_path)
    report = _report(run)
    del report[field]
    with pytest.raises(ValueError):
        ids.feedback(run.path, report)


def test_timeout_cannot_be_recorded_as_a_counterexample_without_execution(tmp_path):
    run = _run(tmp_path)
    report = {**_report(run), "classification": "counterexample"}
    with pytest.raises(ValueError, match="concrete execution"):
        ids.feedback(run.path, report)


def test_feedback_becomes_historical_when_diagnostics_or_iteration_change(tmp_path):
    run = _run(tmp_path)
    ids.feedback(run.path, _report(run))
    (run.synthesis / "diagnostics.txt").write_text("different diagnostic")
    assert not ids.blockers(run.path)[0]["current"]
    ids.feedback(run.path, _report(run))
    assert ids.blockers(run.path)[-1]["current"]
    iterations.close(run.path, "failed", reason="unsupported operation")
    assert not any(row["current"] for row in ids.blockers(run.path))


def test_evaluation_cli_keeps_benchmark_arguments_and_returns_row_fields(tmp_path, capsys):
    run = _run(tmp_path)
    with (run.evaluator / "score.py").open("a") as f:
        f.write("import sys\nassert sys.argv[1:] == ['--timeout', 'benchmark-option']\n")
    assert (
        evaluation.main(
            [
                "run",
                str(run.path),
                "--config",
                '{"threads": 4}',
                "--",
                sys.executable,
                "synthesis/evaluator/score.py",
                "--timeout",
                "benchmark-option",
            ]
        )
        == 0
    )
    row = json.loads(capsys.readouterr().out)
    assert row["config"] == {"threads": 4}
    assert row["metrics"] == {"throughput": 12}
    with pytest.raises(ValueError, match="attestation"):
        evaluation.validate(run.path, row, checkpoint.input_digests(run.path))


def test_successful_checker_output_is_preserved(tmp_path):
    run = _run(tmp_path)
    with (run.tests / "proof.sh").open("a") as f:
        f.write("echo 'checker version pinned; all required obligations checked'\n")
    receipt = _evaluate(run)
    assert "all required obligations checked" in (receipt.parent / "checks.log").read_text()


def test_invalid_receipt_log_schema_fails_cleanly(tmp_path):
    run = _run(tmp_path)
    receipt = _evaluate(run)
    row = _record(run, receipt)
    doc = json.loads(receipt.read_text())
    doc["logs"] = []
    receipt.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="trusted evaluation record"):
        checkpoint.snapshot_run(run.path, tmp_path)


def test_ids_cli_records_and_lists_feedback(tmp_path, capsys):
    run = _run(tmp_path)
    report = run.synthesis / "report.json"
    report.write_text(json.dumps(_report(run)))
    assert ids.main(["feedback", str(run.path), "--report", str(report)]) == 0
    assert Path(capsys.readouterr().out.strip()).is_file()
    assert ids.main(["blockers", str(run.path)]) == 0
    assert json.loads(capsys.readouterr().out)[0]["current"] is True


def test_ids_rejects_diagnostics_outside_run_even_in_modified_history(tmp_path):
    run = _run(tmp_path)
    report = _report(run)
    report["diagnostics"] = ["../outside.txt"]
    with pytest.raises(ValueError, match="run-relative"):
        ids.feedback(run.path, report)
    path = ids.feedback(run.path, _report(run))
    doc = json.loads(path.read_text())
    doc["diagnostic_digests"] = {"../outside.txt": "bad"}
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="Invalid diagnostic"):
        ids.blockers(run.path)
