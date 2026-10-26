"""Locally debug a frozen code-and-proof candidate and its performance command.

    evaluation run <run> --config '{"threads": 4}' -- bash synthesis/evaluator/score.sh

The command runs from a copy of the run's inputs. SKYDISCOVER_RUN, SKYDISCOVER_IMPL and
SKYDISCOVER_INTERFACE name that copy; SKYDISCOVER_EVALUATION names the directory for build
outputs. The task's full test.sh must invoke its proof checker and required integration tests.
The benchmark's final nonempty output line must be a JSON object of finite numeric metrics,
for example {"throughput": 12}. These measured values are bound to the receipt and leaderboard.
This local command creates an unsigned receipt. Official scores require evaluation_authority,
whose protected canonical record is authenticated during checkpoint creation and export.
This records execution and input identity, not theorem semantics: the task owns its checker,
toolchain pins, and the build/extraction correspondence between proved and measured code.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from . import checkpoint
from .paths import Run, run_cli

_RUN_TESTS = Path(__file__).resolve().parent.parent / "workflow/scripts/run_tests.py"


def _execute(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    log: Path,
    timeout: int,
    *,
    worker: tuple[int, int] | None = None,
) -> None:
    """Run one bounded process group, keeping diagnostics even on failure."""
    with log.open("w", encoding="utf-8") as output:
        try:
            proc = subprocess.Popen(
                command,
                cwd=cwd,
                env=env,
                stdout=output,
                stderr=subprocess.STDOUT,
                start_new_session=True,
                user=worker[0] if worker is not None else None,
                group=worker[1] if worker is not None else None,
                extra_groups=[] if worker is not None else None,
                umask=0o077 if worker is not None else -1,
            )
        except OSError as exc:
            raise ValueError(f"Could not start {command[0]}: {exc}; logs: {log}") from exc
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise ValueError(f"Command timed out; no accepted evaluation. Logs: {log}") from exc
        finally:
            # Kill only this command's group, including children surviving their parent.
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()
    if code:
        raise ValueError(f"Command exited {code}; no accepted evaluation. Logs: {log}")


def _environment(run: Run, entry: Path, output: Path, base=None) -> dict[str, str]:
    return {
        **(os.environ if base is None else base),
        "SKYDISCOVER_RUN": str(run.path.resolve()),
        "SKYDISCOVER_IMPL": str(entry.resolve()),
        "SKYDISCOVER_INTERFACE": str(run.interface.resolve()),
        "SKYDISCOVER_EVALUATION": str(output.resolve()),
    }


def _check(
    run: Run, entry: Path, output: Path, log: Path, timeout: int, *, worker=None, env=None
) -> None:
    _execute(
        [
            sys.executable,
            str(_RUN_TESTS),
            "--run",
            str(run.path),
            "--impl",
            str(entry),
            "--show-output",
        ],
        run.path,
        {**_environment(run, entry, output, env), "SKYDISCOVER_SLOW_SECS": str(timeout)},
        log,
        timeout,
        worker=worker,
    )


def _copy_inputs(run: Run, destination: Path) -> Run:
    frozen = Run(destination).create()
    for attr in ("task", "specification", "impl", "evaluator", "tests", "decision_log"):
        source, target = getattr(run, attr), getattr(frozen, attr)
        if not source.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                dirs_exist_ok=True,
                ignore=lambda directory, names: checkpoint._copy_ignore(directory, names)
                | ({"sources", "profiling"} if Path(directory) == run.specification else set()),
            )
        else:
            shutil.copy2(source, target)
    return frozen


def _measurement_metrics(log: Path) -> dict[str, int | float]:
    """Read the benchmark's explicit result, never infer numbers from diagnostic prose."""

    def unique_keys(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate metric: {key}")
            result[key] = value
        return result

    try:
        lines = [line for line in log.read_text(encoding="utf-8").splitlines() if line.strip()]
        metrics = json.loads(lines[-1], object_pairs_hook=unique_keys)
        if (
            not isinstance(metrics, dict)
            or not metrics
            or any(
                not key or type(value) not in (int, float) or not math.isfinite(value)
                for key, value in metrics.items()
            )
        ):
            raise ValueError("Expected finite numeric metrics")
    except (OSError, UnicodeError, ValueError, IndexError, OverflowError) as exc:
        raise ValueError(
            f"Benchmark must end with a JSON object of finite numeric metrics. Logs: {log}"
        ) from exc
    return metrics


def evaluate(
    run_dir: Path,
    command: list[str],
    config: dict[str, Any],
    *,
    timeout: int = 600,
    check_timeout: int = 600,
    _worker: tuple[int, int] | None = None,
    _env: dict[str, str] | None = None,
) -> Path:
    """Return a receipt only after full checking followed by successful measurement."""
    run = Run(run_dir.resolve())
    if not run.co_generates_proof():
        raise ValueError("evaluation run requires checked_by: proof and optimize: performance")
    if not command or not isinstance(config, dict) or min(timeout, check_timeout) <= 0:
        raise ValueError("Supply a command, a configuration object, and positive timeouts")
    # Round-trip before starting any command: the recorded configuration must be JSON data.
    config = json.loads(json.dumps(config, allow_nan=False))
    entry = run.entry_impl()
    if entry is None:
        raise ValueError("No code-and-proof candidate under synthesis/impl")
    relative_entry = entry.resolve().relative_to(run.path)
    inputs = checkpoint.input_digests(run.path)
    bindings = checkpoint.input_digests(run.path, audit=True)
    runs = run.bench / "runs"
    runs.mkdir(parents=True, exist_ok=True)
    output = Path(tempfile.mkdtemp(prefix="evaluation-", dir=runs))
    frozen = _copy_inputs(run, output / "inputs")
    frozen_entry = frozen.path / relative_entry
    build = output
    if _worker is not None:
        # The trusted evaluator owns inputs/logs; candidate processes can write only build products.
        output.chmod(0o755)
        for path in [frozen.path, *frozen.path.rglob("*")]:
            path.chmod(0o755 if path.is_dir() else 0o444 | (path.stat().st_mode & 0o111))
        build = output / "build"
        build.mkdir(mode=0o700)
        os.chown(build, *_worker)

    def unchanged() -> None:
        if any(checkpoint.input_digests(r.path, audit=True) != bindings for r in (run, frozen)):
            raise ValueError(
                f"Inputs changed during evaluation; discard the measurement. Logs: {output}"
            )

    unchanged()
    _check(
        frozen, frozen_entry, build, output / "checks.log", check_timeout, worker=_worker, env=_env
    )
    unchanged()
    _execute(
        command,
        frozen.path,
        _environment(frozen, frozen_entry, build, _env),
        output / "measurement.log",
        timeout,
        worker=_worker,
    )
    unchanged()
    metrics = _measurement_metrics(output / "measurement.log")
    receipt = output / "evaluation.json"
    checkpoint._write_json(
        receipt,
        {
            "schema": 2,
            "status": "passed",
            "created_at": checkpoint._utc_now(),
            "input_digests": inputs,
            "verification_inputs": bindings,
            "impl": relative_entry.as_posix(),
            "config": config,
            "metrics": metrics,
            "command": command,
            "check_timeout": check_timeout,
            "logs": {
                name: checkpoint._tree_digest(output / name)
                for name in ("checks.log", "measurement.log")
            },
        },
    )
    return receipt


def validate(run_dir: Path, row: dict, inputs: dict) -> Path:
    """Reject missing/stale evidence and return its directory for checkpoint preservation."""
    run = Run(run_dir.resolve())
    named = row.get("evaluation")
    if (
        not isinstance(named, str)
        or not named
        or Path(named).is_absolute()
        or ".." in Path(named).parts
    ):
        raise ValueError("Proof-scored candidates require a run-relative evaluation receipt")
    receipt = run.path / named
    if (
        receipt.name != "evaluation.json"
        or receipt.is_symlink()
        or not receipt.resolve().is_relative_to((run.bench / "runs").resolve())
    ):
        raise ValueError("Evaluation receipt must be under synthesis/bench/runs")
    doc = checkpoint._read_json(receipt, {})
    entry = run.entry_impl()
    if (
        not isinstance(doc, dict)
        or doc.get("schema") != 2
        or doc.get("status") != "passed"
        or doc.get("input_digests") != inputs
        or doc.get("config") != row.get("config")
        or entry is None
        or doc.get("impl") != entry.resolve().relative_to(run.path).as_posix()
    ):
        raise ValueError(
            "Evaluation receipt does not cover this candidate and configuration; "
            "rerun evaluation for a schema 2 receipt with measured metrics"
        )
    metrics = _validate_evidence(receipt.parent, doc, inputs)
    row_metrics = row.get("metrics")
    if (
        not isinstance(row_metrics, dict)
        or any(type(value) not in (int, float) for value in row_metrics.values())
        or row_metrics != metrics
    ):
        raise ValueError("Leaderboard metrics differ from the measured metrics")
    return receipt.parent


def _validate_evidence(output: Path, doc: dict, inputs: dict) -> dict:
    """Authenticate the issued result, then verify the accompanying snapshot and logs."""
    if (
        not isinstance(doc, dict)
        or doc.get("schema") != 2
        or doc.get("status") != "passed"
        or doc.get("input_digests") != inputs
    ):
        raise ValueError("Evaluation receipt lacks bound metrics; rerun evaluation")
    from .evaluation_authority import authenticate

    authenticate(doc)
    frozen = Run(output / "inputs")
    if checkpoint.input_digests(frozen.path) != inputs or checkpoint.input_digests(
        frozen.path, audit=True
    ) != doc.get("verification_inputs"):
        raise ValueError("Evaluation inputs changed after checking")
    logs = doc.get("logs")
    if not isinstance(logs, dict):
        raise ValueError("Evaluation receipt has no log digests")
    for name in ("checks.log", "measurement.log"):
        log = output / name
        if not log.is_file() or logs.get(name) != checkpoint._tree_digest(log):
            raise ValueError(f"Evaluation evidence is missing or changed: {name}")
    # The protected record binds both these metrics and the log digest. Re-parsing an editable
    # log cannot add authenticity; only the trusted evaluator parses benchmark output.
    return doc["metrics"]


def validate_checkpoint_score(saved: Path) -> None:
    """Do not export a saved score without its own matching measurement evidence."""
    record = checkpoint.read_record(saved)
    output = saved / ".verification/evaluation"
    doc = checkpoint._read_json(output / "evaluation.json", {})
    metrics = _validate_evidence(output, doc, record.get("inputs", {}))
    if doc.get("config") != record.get("config"):
        raise ValueError("Checkpoint configuration differs from the measured configuration")
    score = checkpoint.read_score(saved).get("score")
    if (
        not isinstance(score, dict)
        or len(score) != 1
        or any(
            key not in metrics or type(value) not in (int, float) or value != metrics[key]
            for key, value in score.items()
        )
    ):
        raise ValueError("Checkpoint score differs from the measured metrics")


def recheck(run_dir: Path, row: dict, inputs: dict) -> Path:
    """Replay the checker on the measured snapshot before creating a scored checkpoint."""
    output = validate(run_dir, row, inputs)
    doc = checkpoint._read_json(output / "evaluation.json", {})
    frozen = Run(output / "inputs")
    timeout = doc.get("check_timeout")
    if type(timeout) is not int or timeout <= 0:
        raise ValueError("Invalid check timeout in evaluation receipt")
    _check(frozen, frozen.path / doc["impl"], output, output / "recheck.log", timeout)
    validate(run_dir, row, inputs)
    if checkpoint.input_digests(run_dir) != inputs:
        raise ValueError("Working inputs changed during proof replay")
    return output


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, prog="evaluation")
    sub = ap.add_subparsers(dest="action", required=True)
    run = sub.add_parser("run", help="check a candidate, then run its benchmark")
    run.add_argument("run_dir", type=Path)
    run.add_argument("--config", type=json.loads, default={})
    run.add_argument("--timeout", type=int, default=600)
    run.add_argument("--check-timeout", type=int, default=600)
    # Split explicitly so options for the benchmark are never consumed by this parser.
    args = list(sys.argv[1:] if argv is None else argv)
    split = args.index("--") if "--" in args else len(args)
    parsed = ap.parse_args(args[:split])
    command = args[split + 1 :]
    receipt = evaluate(
        parsed.run_dir,
        command,
        parsed.config,
        timeout=parsed.timeout,
        check_timeout=parsed.check_timeout,
    )
    doc = checkpoint._read_json(receipt, {})
    print(
        json.dumps(
            {
                "evaluation": receipt.relative_to(parsed.run_dir.resolve()).as_posix(),
                "input_digests": doc["input_digests"],
                "impl": doc["impl"],
                "config": doc["config"],
                "metrics": doc["metrics"],
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli(main, "evaluation"))
