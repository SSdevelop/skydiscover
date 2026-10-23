"""Check a frozen code-and-proof candidate before running its performance command.

    evaluation run <run> --config '{"threads": 4}' -- bash synthesis/evaluator/score.sh

The command runs from a copy of the run's inputs. SKYDISCOVER_RUN, SKYDISCOVER_IMPL and
SKYDISCOVER_INTERFACE name that copy; SKYDISCOVER_EVALUATION names the directory for build
outputs. The task's full test.sh must invoke its proof checker and required integration tests.
This records execution and input identity, not theorem semantics: the task owns its checker,
toolchain pins, and the build/extraction correspondence between proved and measured code.
"""

from __future__ import annotations

import argparse
import json
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


def _execute(command: list[str], cwd: Path, env: dict[str, str], log: Path, timeout: int) -> None:
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


def _environment(run: Run, entry: Path, output: Path) -> dict[str, str]:
    return {
        **os.environ,
        "SKYDISCOVER_RUN": str(run.path.resolve()),
        "SKYDISCOVER_IMPL": str(entry.resolve()),
        "SKYDISCOVER_INTERFACE": str(run.interface.resolve()),
        "SKYDISCOVER_EVALUATION": str(output.resolve()),
    }


def _check(run: Run, entry: Path, output: Path, log: Path, timeout: int) -> None:
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
        {**_environment(run, entry, output), "SKYDISCOVER_SLOW_SECS": str(timeout)},
        log,
        timeout,
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


def evaluate(
    run_dir: Path,
    command: list[str],
    config: dict[str, Any],
    *,
    timeout: int = 600,
    check_timeout: int = 600,
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

    def unchanged() -> None:
        if any(checkpoint.input_digests(r.path, audit=True) != bindings for r in (run, frozen)):
            raise ValueError(
                f"Inputs changed during evaluation; discard the measurement. Logs: {output}"
            )

    unchanged()
    _check(frozen, frozen_entry, output, output / "checks.log", check_timeout)
    unchanged()
    _execute(
        command,
        frozen.path,
        _environment(frozen, frozen_entry, output),
        output / "measurement.log",
        timeout,
    )
    unchanged()
    receipt = output / "evaluation.json"
    checkpoint._write_json(
        receipt,
        {
            "schema": 1,
            "status": "passed",
            "created_at": checkpoint._utc_now(),
            "input_digests": inputs,
            "verification_inputs": bindings,
            "impl": relative_entry.as_posix(),
            "config": config,
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
        or doc.get("schema") != 1
        or doc.get("status") != "passed"
        or doc.get("input_digests") != inputs
        or doc.get("config") != row.get("config")
        or entry is None
        or doc.get("impl") != entry.resolve().relative_to(run.path).as_posix()
    ):
        raise ValueError("Evaluation receipt does not cover this candidate and configuration")
    frozen = Run(receipt.parent / "inputs")
    if checkpoint.input_digests(frozen.path) != inputs or checkpoint.input_digests(
        frozen.path, audit=True
    ) != doc.get("verification_inputs"):
        raise ValueError("Evaluation inputs changed after checking")
    logs = doc.get("logs")
    if not isinstance(logs, dict):
        raise ValueError("Evaluation receipt has no log digests")
    for name in ("checks.log", "measurement.log"):
        log = receipt.parent / name
        if not log.is_file() or logs.get(name) != checkpoint._tree_digest(log):
            raise ValueError(f"Evaluation evidence is missing or changed: {name}")
    return receipt.parent


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
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli(main, "evaluation"))
