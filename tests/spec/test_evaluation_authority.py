"""Real Linux UID/permission tests. Run as root in a disposable test environment.

No accounts, sudo rules or system configuration are created. All records live under /tmp;
numeric UIDs 65533 (agent) and 65534 (benchmark) exercise the actual kernel permission boundary.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from skydiscover.synthesize.spec import checkpoint, evaluation
from skydiscover.synthesize.spec import evaluation_authority as authority
from skydiscover.synthesize.spec import run as spec_run
from tests.spec.test_ids_performance import _record, _run

pytestmark = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() != 0, reason="requires root for real UID separation"
)

AGENT = (65533, 65533)
WORKER = (65534, 65534)


@pytest.fixture
def layout(monkeypatch):
    with tempfile.TemporaryDirectory(prefix="skydiscover-authority-test-") as tmp:
        root = Path(tmp)
        root.chmod(0o755)
        monkeypatch.setattr(authority, "_ROOT", root / "authority")
        run = _run(root / "agent")
        # User-controlled workspace is distinct from the protected authority tree.
        for path in [root / "agent", *(root / "agent").rglob("*")]:
            os.chown(path, *AGENT)
            if path.is_dir():
                path.chmod(0o755)
        yield root, run


def _authorize(run):
    return authority.authorize(
        "counter",
        run.path,
        [sys.executable, "synthesis/evaluator/score.py"],
        {"threads": 4},
        agent=AGENT,
        worker=WORKER,
    )


def _child(code, *args):
    return subprocess.run(
        [sys.executable, "-I", "-c", code, *map(str, args)],
        user=AGENT[0],
        group=AGENT[1],
        extra_groups=[],
        cwd="/",
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_authority_measures_unprivileged_and_exports_honest_score(layout, monkeypatch):
    root, run = layout
    (run.impl / "build").mkdir()
    (run.impl / "build/dependency.txt").write_text("required dependency\n")
    with (run.tests / "proof.sh").open("a") as stream:
        stream.write('grep -qx "required dependency" "$SKYDISCOVER_IMPL/build/dependency.txt"\n')
    monkeypatch.setenv("AUTHORITY_CANARY_SECRET", "must-not-reach-benchmark")
    with (run.evaluator / "score.py").open("a") as stream:
        stream.write(
            f"assert os.getuid() == {WORKER[0]}\n"
            "assert 'AUTHORITY_CANARY_SECRET' not in os.environ\n"
            "assert os.getgroups() == []\n"
            "for target in [Path(os.environ['SKYDISCOVER_IMPL']) / 'code.txt',\n"
            f"               Path({str(authority._ROOT / 'receipts/forged.json')!r})]:\n"
            "    try:\n"
            "        target.write_text('forged')\n"
            "    except PermissionError:\n"
            "        pass\n"
            "    else:\n"
            "        raise AssertionError('benchmark could write protected inputs or receipts')\n"
        )
    _authorize(run)
    row = authority.measure("counter", run.path)
    receipt = run.path / row["evaluation"]
    assert (
        receipt.parent / "inputs/synthesis/impl/build/dependency.txt"
    ).read_text() == "required dependency\n"
    doc = json.loads(receipt.read_text())
    authority.authenticate(doc)
    verified = _child(
        "import json, sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
        "from skydiscover.synthesize.spec import evaluation_authority as module; "
        "module._ROOT = Path(sys.argv[2]); "
        "module.authenticate(json.loads(Path(sys.argv[3]).read_text()))",
        Path(authority.__file__).resolve().parents[3],
        authority._ROOT,
        receipt,
    )
    assert verified.returncode == 0, verified.stderr
    _record(run, receipt)
    out, _ = checkpoint.snapshot_run(run.path, root, became_best=True)
    checkpoint.stamp_audit(run.path, [])
    spec_run.export_deliverable(run.path, root)
    assert json.loads((out / "best/score.json").read_text())["score"] == {"throughput": 12}

    record = authority._ROOT / "receipts" / (doc["attestation"] + ".json")
    denied = _child(
        "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('{}')", record
    )
    assert denied.returncode != 0 and "PermissionError" in denied.stderr
    denied = _child("from pathlib import Path; import sys; Path(sys.argv[1]).chmod(0o666)", record)
    assert denied.returncode != 0 and "PermissionError" in denied.stderr


def test_joint_forgery_cannot_checkpoint_or_export(layout):
    root, run = layout
    _authorize(run)
    row = authority.measure("counter", run.path)
    receipt = run.path / row["evaluation"]
    forged = _child(
        "import json, sys; from pathlib import Path; sys.path.insert(0, sys.argv[1]); "
        "from skydiscover.synthesize.spec.checkpoint import _tree_digest; "
        "receipt = Path(sys.argv[2]); doc = json.loads(receipt.read_text()); "
        "doc['metrics'] = {'throughput': 999999}; "
        "log = receipt.parent / 'measurement.log'; log.write_text(json.dumps(doc['metrics'])); "
        "doc['logs']['measurement.log'] = _tree_digest(log); "
        "receipt.write_text(json.dumps(doc)); "
        "row = json.loads(sys.argv[4]); row['metrics'] = doc['metrics']; "
        "Path(sys.argv[3]).write_text(json.dumps([row]))",
        Path(authority.__file__).resolve().parents[3],
        receipt,
        run.leaderboard,
        json.dumps(row),
    )
    assert forged.returncode == 0, forged.stderr
    with pytest.raises(ValueError, match="trusted evaluation record"):
        checkpoint.snapshot_run(run.path, root, became_best=True)
    checkpoint.stamp_audit(run.path, [])
    with pytest.raises(ValueError, match="trusted evaluation record"):
        spec_run.export_deliverable(run.path, root)


@pytest.mark.parametrize("target", ["benchmark", "checker"])
def test_agent_cannot_get_attestation_for_replaced_evaluation(layout, target):
    _, run = layout
    _authorize(run)
    path = run.evaluator / "score.py" if target == "benchmark" else run.tests / "proof.sh"
    path.write_text("print('{\"throughput\": 999999}')\n" if target == "benchmark" else "exit 0\n")
    with pytest.raises(ValueError, match="approved policy"):
        authority.measure("counter", run.path)
    assert not list((authority._ROOT / "receipts").glob("*.json"))


def test_unsigned_and_agent_owned_trust_records_are_rejected(layout):
    _, run = layout
    receipt = evaluation.evaluate(run.path, [sys.executable, "synthesis/evaluator/score.py"], {})
    row = _record(run, receipt)
    with pytest.raises(ValueError, match="attestation"):
        evaluation.validate(run.path, row, checkpoint.input_digests(run.path))
    # The diagnostic call above ran in this root test process; in production the agent owns it.
    for path in [run.bench, *run.bench.rglob("*")]:
        os.chown(path, *AGENT)
    _authorize(run)
    row = authority.measure("counter", run.path)
    doc = json.loads((run.path / row["evaluation"]).read_text())
    record = authority._ROOT / "receipts" / (doc["attestation"] + ".json")
    os.chown(record, *AGENT)
    with pytest.raises(ValueError, match="root-owned and protected"):
        authority.authenticate(doc)


def test_agent_cannot_issue_receipts_or_read_privileged_files(layout):
    root, run = layout
    denied = _child(
        "import sys; sys.path.insert(0, sys.argv[1]); "
        "from skydiscover.synthesize.spec.evaluation_authority import seal; seal({})",
        Path(authority.__file__).resolve().parents[3],
    )
    assert denied.returncode != 0 and "administrator-owned process" in denied.stderr
    _authorize(run)
    private = root / "private.txt"
    private.write_text("administrator secret")
    private.chmod(0o600)
    # In particular, decision_log was not included in the non-audit input digests.
    run.decision_log.parent.mkdir(parents=True, exist_ok=True)
    run.decision_log.symlink_to(private)
    with pytest.raises(ValueError, match="snapshot candidate as agent UID"):
        authority.measure("counter", run.path)
    assert not list((authority._ROOT / "receipts").glob("*.json"))


def test_failed_proof_retains_diagnostics_but_cannot_issue_a_score(layout):
    _, run = layout
    _authorize(run)
    (run.impl / "proof.txt").write_text("unfinished\n")
    with pytest.raises(ValueError, match="retained diagnostics"):
        authority.measure("counter", run.path)
    assert "TESTS FAILED" in next(run.bench.rglob("checks.log")).read_text()
    assert not list(run.bench.rglob("evaluation.json"))
    assert not list((authority._ROOT / "receipts").glob("*.json"))
    (run.impl / "proof.txt").write_text("verified\n")
    row = authority.measure("counter", run.path)
    assert row["metrics"] == {"throughput": 12}
    authority.authenticate(json.loads((run.path / row["evaluation"]).read_text()))
