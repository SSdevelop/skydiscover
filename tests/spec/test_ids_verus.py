"""Optional smoke test: a real Verus proof followed by a build and runtime measurement."""

from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

from skydiscover.synthesize.spec import checkpoint, evaluation, evaluation_authority
from skydiscover.synthesize.spec import run as spec_run
from skydiscover.synthesize.spec.paths import Run


@pytest.mark.integration
@pytest.mark.usefixtures("fake_evaluation_authority")
def test_verus_candidate_is_proved_before_runtime_measurement(tmp_path, monkeypatch):
    verus = os.environ.get("SKYDISCOVER_VERUS") or shutil.which("verus")
    if not verus:
        pytest.skip("Verus is not installed")
    monkeypatch.setenv("SKYDISCOVER_VERUS", verus)
    run = Run(tmp_path / "run").create()
    run.task.write_text("---\ndomain: counter\nchecked_by: proof\noptimize: performance\n---\n")
    run.impl.mkdir()
    source = run.impl / "counter.rs"
    source.write_text(
        "use vstd::prelude::*;\n"
        "verus! {\n"
        "pub fn increment(x: u64) -> (r: u64)\n"
        "    requires x < u64::MAX,\n"
        "    ensures r == x + 1,\n"
        "{ x + 1 }\n}\n"
    )
    run.interface.mkdir(parents=True)
    (run.interface / "contract.rs").write_text(
        "verus! {\n"
        "fn required_obligation(x: u64)\n"
        "    requires x < u64::MAX,\n"
        "{ let r = increment(x); assert(r == x + 1); }\n}\n"
    )
    run.tests.mkdir()
    run.test_script.write_text(f'"{sys.executable}" proof.py\n')
    (run.tests / "proof.py").write_text(
        "import os, subprocess, tempfile\nfrom pathlib import Path\n"
        "with tempfile.TemporaryDirectory() as scratch:\n"
        "    source = Path(scratch) / 'counter.rs'\n"
        "    source.write_text(Path(os.environ['SKYDISCOVER_IMPL']).read_text() + '\\n' +\n"
        "        (Path(os.environ['SKYDISCOVER_INTERFACE']) / 'contract.rs').read_text())\n"
        "    subprocess.run([os.environ['SKYDISCOVER_VERUS'], str(source), '--crate-type', 'lib'],\n"
        "                   cwd=scratch, check=True)\n"
    )
    (run.evaluator / "score.py").write_text(
        "import os, subprocess, json, time\nfrom pathlib import Path\n"
        "output = Path(os.environ['SKYDISCOVER_EVALUATION'])\n"
        "source = output / 'runtime.rs'\n"
        "source.write_text(Path(os.environ['SKYDISCOVER_IMPL']).read_text() + '\\n' +\n"
        "    (Path(os.environ['SKYDISCOVER_INTERFACE']) / 'contract.rs').read_text() +\n"
        "    '\\nfn main() { let mut x = 0_u64; for _ in 0..10000 { '\n"
        "    'x = increment(std::hint::black_box(x)); } println!(\"{}\", x); }\\n')\n"
        "binary = output / 'counter'\n"
        "subprocess.run([os.environ['SKYDISCOVER_VERUS'], str(source), '--compile', '-C',\n"
        "                'opt-level=3', '-o', str(binary)], cwd=output, check=True)\n"
        "start = time.perf_counter_ns()\n"
        "result = subprocess.run([str(binary)], capture_output=True, text=True, check=True)\n"
        "elapsed = time.perf_counter_ns() - start\n"
        "assert result.stdout.strip() == '10000'\n"
        "print(json.dumps({'elapsed_ns': elapsed}))\n"
    )
    command = [sys.executable, "synthesis/evaluator/score.py"]
    receipt = evaluation.evaluate(run.path, command, {}, timeout=120, check_timeout=120)
    checkpoint._write_json(receipt, evaluation_authority.seal(json.loads(receipt.read_text())))
    log = (receipt.parent / "measurement.log").read_text()
    metrics = json.loads(log.splitlines()[-1])
    assert metrics["elapsed_ns"] > 0
    assert "0 errors" in (receipt.parent / "checks.log").read_text()
    doc = json.loads(receipt.read_text())
    run.leaderboard.write_text(
        json.dumps(
            [
                {
                    "impl": doc["impl"],
                    "config": doc["config"],
                    "input_digests": doc["input_digests"],
                    "evaluation": receipt.relative_to(run.path).as_posix(),
                    "metrics": metrics,
                    "objective": "elapsed_ns",
                    "direction": "min",
                }
            ]
        )
    )
    out, saved = checkpoint.snapshot_run(run.path, tmp_path, became_best=True)
    assert (saved / ".verification/source/counter.rs").read_text() == source.read_text()
    checkpoint.stamp_audit(run.path, [])
    spec_run.export_deliverable(run.path, tmp_path)
    assert "TESTS PASSED" in (out / "best/.verification/checks.log").read_text()
    source.write_text(source.read_text().replace("{ x + 1 }", "{ x }"))
    with pytest.raises(ValueError, match="exited"):
        evaluation.evaluate(run.path, command, {}, timeout=120, check_timeout=120)
    assert len(list(run.bench.rglob("evaluation.json"))) == 1
