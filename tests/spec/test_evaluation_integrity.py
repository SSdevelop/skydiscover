"""Regression tests for measured scores and executable candidate identity."""

import json
import os
from pathlib import Path

import pytest

from skydiscover.synthesize.spec import checkpoint, evaluation
from skydiscover.synthesize.spec import run as spec_run
from tests.spec.test_ids_performance import _evaluate, _record, _run

pytestmark = pytest.mark.usefixtures("fake_evaluation_authority")


def _forge(receipt, metrics):
    log = receipt.parent / "measurement.log"
    log.write_text(json.dumps(metrics) + "\n")
    doc = json.loads(receipt.read_text())
    doc["metrics"] = metrics
    doc["logs"]["measurement.log"] = checkpoint._tree_digest(log)
    receipt.write_text(json.dumps(doc))


@pytest.mark.parametrize("stage", ["checkpoint", "export"])
@pytest.mark.parametrize(
    "forge_evidence", [False, True], ids=["score-only", "score-log-receipt-hash"]
)
def test_forged_score_cannot_be_published(tmp_path, stage, forge_evidence):
    run = _run(tmp_path)
    receipt = _evaluate(run)
    row = _record(run, receipt)
    assert json.loads((receipt.parent / "measurement.log").read_text()) == {"throughput": 12}
    row["metrics"] = {"throughput": 999999}
    run.leaderboard.write_text(json.dumps([row]))
    if forge_evidence:
        _forge(receipt, row["metrics"])
    checkpoint.stamp_audit(run.path, [])
    with pytest.raises(ValueError, match="metrics|trusted evaluation"):
        if stage == "checkpoint":
            checkpoint.snapshot_run(run.path, tmp_path, became_best=True)
        else:
            spec_run.export_deliverable(run.path, tmp_path)


def test_saved_score_and_all_its_evidence_cannot_be_forged(tmp_path):
    run = _run(tmp_path)
    _record(run, _evaluate(run))
    out, saved = checkpoint.snapshot_run(run.path, tmp_path, became_best=True)
    score = json.loads((saved / "score.json").read_text())
    score["score"] = {"throughput": 999999}
    (saved / "score.json").write_text(json.dumps(score))
    _forge(saved / ".verification/evaluation/evaluation.json", score["score"])
    checkpoint.stamp_audit(run.path, [])
    with pytest.raises(ValueError, match="trusted evaluation"):
        spec_run.export_deliverable(run.path, tmp_path)
    assert json.loads((out / "best/score.json").read_text())["score"] == {"throughput": 12}


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable permissions")
def test_chmod_after_measurement_invalidates_the_candidate(tmp_path):
    run = _run(tmp_path)
    helper = run.impl / "helper.sh"
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)
    with (run.tests / "proof.sh").open("a") as stream:
        stream.write('test -x "$SKYDISCOVER_IMPL/helper.sh"\n')
    _record(run, _evaluate(run))
    helper.chmod(0o644)
    with pytest.raises(ValueError, match="exited"):
        evaluation._check(run, run.entry_impl(), tmp_path, tmp_path / "fresh.log", 30)
    with pytest.raises(ValueError, match="No leaderboard measurement|permissions"):
        checkpoint.snapshot_run(run.path, tmp_path)
    helper.chmod(0o755)
    checkpoint.snapshot_run(run.path, tmp_path, became_best=True)
    checkpoint.stamp_audit(run.path, [])
    spec_run.export_deliverable(run.path, tmp_path)


@pytest.mark.skipif(os.name != "posix", reason="POSIX executable permissions")
def test_copy_must_preserve_the_measured_executable_permissions(tmp_path, monkeypatch):
    run = _run(tmp_path)
    helper = run.impl / "helper.sh"
    helper.write_text("#!/bin/sh\nexit 0\n")
    helper.chmod(0o755)
    _record(run, _evaluate(run))
    copytree = checkpoint.shutil.copytree

    def damaged_copy(source, target, *args, **kwargs):
        result = copytree(source, target, *args, **kwargs)
        path = Path(target)
        if path.name == "artifact":
            (path / "helper.sh").chmod(0o644)
        return result

    monkeypatch.setattr(checkpoint.shutil, "copytree", damaged_copy)
    with pytest.raises(ValueError, match="permissions"):
        checkpoint.snapshot_run(run.path, tmp_path)


@pytest.mark.parametrize(
    "result",
    ["", '{"throughput": true}', '{"throughput": NaN}', '{"throughput": 12, "throughput": 999999}'],
)
def test_invalid_benchmark_results_cannot_issue_receipts(tmp_path, result):
    run = _run(tmp_path)
    (run.evaluator / "score.py").write_text(f"print({result!r})\n")
    with pytest.raises(ValueError, match="finite numeric metrics"):
        _evaluate(run)
    assert not list(run.bench.rglob("evaluation.json"))


def test_honest_multiple_metrics_can_be_exported(tmp_path):
    run = _run(tmp_path)
    (run.evaluator / "score.py").write_text(
        "print('benchmark diagnostics')\n"
        'print(\'{"throughput": 12, "latency": 1.5}\')\nprint()\n'
    )
    row = _record(run, _evaluate(run))
    row.update(objective="latency", direction="min")
    run.leaderboard.write_text(json.dumps([row]))
    out, _ = checkpoint.snapshot_run(run.path, tmp_path, became_best=True)
    checkpoint.stamp_audit(run.path, [])
    spec_run.export_deliverable(run.path, tmp_path)
    assert json.loads((out / "best/score.json").read_text())["score"] == {"latency": 1.5}


def test_export_keeps_selected_checkpoints_own_measurement(tmp_path, monkeypatch):
    run = _run(tmp_path)
    (run.evaluator / "score.py").write_text(
        "import json, os\nprint(json.dumps({'throughput': int(os.environ['TEST_SCORE'])}))\n"
    )
    monkeypatch.setenv("TEST_SCORE", "12")
    _record(run, _evaluate(run))
    out, _ = checkpoint.snapshot_run(run.path, tmp_path, became_best=True)
    monkeypatch.setenv("TEST_SCORE", "24")
    _record(run, _evaluate(run))
    checkpoint.stamp_audit(run.path, [])
    spec_run.export_deliverable(run.path, tmp_path)
    assert json.loads((out / "best/score.json").read_text())["score"] == {"throughput": 12}
