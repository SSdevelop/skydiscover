"""Trusted Linux/WSL evaluation. Install and run from administrator-owned code.

Only this privileged entry point issues accepted receipts. It pins the task/checker/benchmark,
runs candidate processes under a separate unprivileged UID, and keeps a canonical receipt in a
root-owned store. Local `evaluation run` remains an untrusted diagnostic tool.
See TRUSTED_EVALUATION.md for installation and the permission boundary.
"""

from __future__ import annotations

import argparse
import json
import os
import pwd
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

from . import checkpoint, evaluation
from .paths import Run, run_cli

# Deliberately not selected by a run, receipt, CLI flag or agent-controlled environment variable.
_ROOT = Path("/var/lib/skydiscover/trusted-evaluation")
_PINNED = ("task", "specification", "tests", "evaluator")


def _canonical(doc: dict) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _require_root() -> None:
    if os.geteuid() != 0:
        raise ValueError("The trusted evaluator requires an administrator-owned process")


def _protected(path: Path) -> None:
    """Reject writable/replaced trust roots, including symlinked or writable parent directories."""
    for part in [*reversed(path.parents), path]:
        try:
            info = part.lstat()
        except OSError as exc:
            raise ValueError(f"Trusted evaluation record is unavailable: {part}") from exc
        sticky_parent = part != path and stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX
        if (
            info.st_uid != 0
            or stat.S_ISLNK(info.st_mode)
            or (info.st_mode & 0o022 and not sticky_parent)
        ):
            raise ValueError(f"Trusted evaluator path must be root-owned and protected: {part}")


def _store() -> None:
    _require_root()
    _ROOT.mkdir(parents=True, exist_ok=True, mode=0o755)
    _protected(_ROOT)
    for name in ("policies", "receipts", "work"):
        path = _ROOT / name
        path.mkdir(exist_ok=True, mode=0o755)
        _protected(path)


def _write_once(path: Path, doc: dict) -> None:
    with path.open("x", encoding="utf-8") as output:
        output.write(_canonical(doc) + "\n")
    path.chmod(0o644)


def seal(doc: dict) -> dict:
    """Called by the trusted evaluator after measurement, never exposed as an agent-facing CLI action."""
    _store()
    issued = {**doc, "attestation": uuid.uuid4().hex}
    _write_once(_ROOT / "receipts" / (issued["attestation"] + ".json"), issued)
    return issued


def authenticate(doc: dict) -> None:
    identity = doc.get("attestation")
    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity):
        raise ValueError("No trusted evaluation attestation; request a trusted evaluation")
    path = _ROOT / "receipts" / (identity + ".json")
    _protected(path)
    try:
        trusted = json.loads(path.read_text(encoding="utf-8"))
        matches = _canonical(trusted) == _canonical(doc)
    except (OSError, ValueError, TypeError) as exc:
        raise ValueError("Invalid trusted evaluation record") from exc
    if not matches:
        raise ValueError("Evaluation receipt differs from the trusted evaluation record")


def _policy_path(name: str) -> Path:
    if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", name):
        raise ValueError("Use a simple evaluation policy name")
    return _ROOT / "policies" / (name + ".json")


def authorize(
    name: str,
    run_dir: Path,
    command: list[str],
    config: dict,
    *,
    agent: tuple[int, int],
    worker: tuple[int, int],
    env: dict | None = None,
    timeout: int = 600,
    check_timeout: int = 600,
) -> Path:
    """Administrator approval of a benchmark, not automatic approval by an optimizing agent."""
    _store()
    if min(*agent, *worker) <= 0 or agent[0] == worker[0]:
        raise ValueError("Agent and benchmark need distinct non-root UIDs")
    if (
        not command
        or not all(isinstance(arg, str) for arg in command)
        or not isinstance(config, dict)
    ):
        raise ValueError("Supply a fixed benchmark command and configuration")
    if min(timeout, check_timeout) <= 0:
        raise ValueError("Use positive evaluation timeouts")
    run = Run(run_dir.resolve())
    entry = run.entry_impl()
    if not run.co_generates_proof() or entry is None:
        raise ValueError("Policy requires a code-and-proof performance candidate")
    inputs = checkpoint.input_digests(run.path)
    policy = {
        "inputs": {key: inputs[key] for key in _PINNED},
        "impl": entry.relative_to(run.path).as_posix(),
        "command": command,
        "config": config,
        "agent": agent,
        "worker": worker,
        "env": {
            "PATH": "/usr/bin:/bin",
            "LANG": "C.UTF-8",
            "PYTHONDONTWRITEBYTECODE": "1",
            **(env or {}),
        },
        "timeout": timeout,
        "check_timeout": check_timeout,
    }
    path = _policy_path(name)
    _write_once(path, policy)
    return path


_COPY_INPUTS = """
import sys, tarfile, tempfile
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from skydiscover.synthesize.spec.evaluation import _copy_inputs
from skydiscover.synthesize.spec.paths import Run
with tempfile.TemporaryDirectory() as tmp:
    copied = _copy_inputs(Run(Path(sys.argv[2])), Path(tmp) / 'inputs')
    with tarfile.open(fileobj=sys.stdout.buffer, mode='w|') as archive:
        archive.add(copied.path, arcname='.')
"""


def _snapshot_as_agent(run: Path, work: Path, agent: tuple[int, int], timeout: int) -> Run:
    # Read the mutable run directory without root privileges. A symlink race must never expose a
    # privileged file. Treat the resulting archive as hostile and extract only ordinary files.
    archive = work / "candidate.tar"
    with archive.open("wb") as stream:
        copied = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                _COPY_INPUTS,
                str(Path(__file__).resolve().parents[3]),
                str(run),
            ],
            user=agent[0],
            group=agent[1],
            extra_groups=[],
            cwd="/",
            env={"PATH": "/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
            stdout=stream,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
    if copied.returncode:
        raise ValueError(
            f"Could not snapshot candidate as agent UID: {copied.stderr.decode(errors='replace')}"
        )
    root = work / "run"
    root.mkdir(mode=0o755)
    with tarfile.open(archive) as source:
        for member in source:
            relative = Path(member.name)
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or not (member.isfile() or member.isdir())
            ):
                raise ValueError("Candidate snapshot must contain only relative regular files")
            target = root / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True, mode=0o755)
            else:
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
                data = source.extractfile(member)
                if data is None:
                    raise ValueError("Candidate snapshot file has no contents")
                with data, target.open("xb") as output:
                    shutil.copyfileobj(data, output)
                target.chmod(0o644 | (member.mode & 0o111))
    return Run(root)


_COPY_RESULT = """
import json, shutil, sys
from pathlib import Path
source, run = map(Path, sys.argv[1:])
destination = run / 'synthesis/bench/runs' / source.name
destination.parent.mkdir(parents=True, exist_ok=True)
destination.mkdir()
# Exclude only this evaluation's scratch products. A candidate may legitimately depend on a
# nested directory called 'build', which must remain in the measured input snapshot.
shutil.copytree(source / 'inputs', destination / 'inputs')
for name in ('checks.log', 'measurement.log', 'evaluation.json'):
    if (source / name).is_file():
        shutil.copy2(source / name, destination / name)
if (destination / 'evaluation.json').is_file():
    doc = json.loads((destination / 'evaluation.json').read_text())
    row = {key: doc[key] for key in ('impl', 'config', 'input_digests', 'metrics')}
    row['evaluation'] = (destination / 'evaluation.json').relative_to(run).as_posix()
else:
    row = {'diagnostics': destination.relative_to(run).as_posix()}
print(json.dumps(row))
"""


def _publish(output: Path, run: Path, agent: tuple[int, int], timeout: int) -> dict:
    # Never write as root into agent-controlled directories (including symlink races).
    published = subprocess.run(
        [sys.executable, "-I", "-c", _COPY_RESULT, str(output), str(run)],
        user=agent[0],
        group=agent[1],
        extra_groups=[],
        umask=0o022,
        cwd="/",
        env={"PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if published.returncode:
        raise ValueError(f"Could not publish evaluation as the agent UID: {published.stderr}")
    return json.loads(published.stdout)


def measure(name: str, run_dir: Path) -> dict:
    """Measure against the approved policy and return a row for the public leaderboard."""
    _require_root()
    policy_path = _policy_path(name)
    _protected(policy_path)
    _protected(_ROOT / "work")
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    agent, worker = tuple(policy["agent"]), tuple(policy["worker"])
    run = Run(run_dir.resolve())
    if run.path.stat().st_uid != agent[0]:
        raise ValueError("The candidate run must belong to the policy's agent UID")
    with tempfile.TemporaryDirectory(prefix="evaluation-", dir=_ROOT / "work") as tmp:
        work = Path(tmp)
        work.chmod(0o755)
        copied = _snapshot_as_agent(run.path, work, agent, policy["timeout"])
        inputs = checkpoint.input_digests(copied.path)
        if any(inputs[key] != policy["inputs"][key] for key in _PINNED):
            raise ValueError(
                "Task, specification, checker or benchmark differs from the approved policy"
            )
        # Entry selection is approved with the policy, not taken from an editable leaderboard.
        copied.bench.mkdir(parents=True, exist_ok=True)
        checkpoint._write_json(copied.leaderboard, [{"impl": policy["impl"]}])
        try:
            receipt = evaluation.evaluate(
                copied.path,
                policy["command"],
                policy["config"],
                timeout=policy["timeout"],
                check_timeout=policy["check_timeout"],
                _worker=worker,
                _env=policy["env"],
            )
        except ValueError as exc:
            failed = next((copied.bench / "runs").glob("evaluation-*"), None)
            if failed is not None:
                report = _publish(failed, run.path, agent, policy["timeout"])
                raise ValueError(f"{exc}; retained diagnostics: {report['diagnostics']}") from exc
            raise
        doc = json.loads(receipt.read_text(encoding="utf-8"))
        doc["policy"] = name
        checkpoint._write_json(receipt, seal(doc))
        return _publish(receipt.parent, run.path, agent, policy["timeout"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="action", required=True)
    init = sub.add_parser("authorize", help="administrator approves a fixed evaluation policy")
    init.add_argument("name")
    init.add_argument("run_dir", type=Path)
    init.add_argument("--agent-user", required=True)
    init.add_argument("--worker-user", required=True)
    init.add_argument("--config", type=json.loads, default={})
    init.add_argument("--env", type=json.loads, default={})
    init.add_argument("--timeout", type=int, default=600)
    init.add_argument("--check-timeout", type=int, default=600)
    measure_parser = sub.add_parser("run", help="measure a candidate under an approved policy")
    measure_parser.add_argument("name")
    measure_parser.add_argument("run_dir", type=Path)
    args = list(sys.argv[1:] if argv is None else argv)
    split = args.index("--") if "--" in args else len(args)
    parsed = ap.parse_args(args[:split])
    if parsed.action == "authorize":
        agent, worker = pwd.getpwnam(parsed.agent_user), pwd.getpwnam(parsed.worker_user)
        print(
            authorize(
                parsed.name,
                parsed.run_dir,
                args[split + 1 :],
                parsed.config,
                agent=(agent.pw_uid, agent.pw_gid),
                worker=(worker.pw_uid, worker.pw_gid),
                env=parsed.env,
                timeout=parsed.timeout,
                check_timeout=parsed.check_timeout,
            )
        )
    else:
        if split != len(args):
            raise ValueError("An agent cannot override the approved benchmark command")
        print(json.dumps(measure(parsed.name, parsed.run_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli(main, "trusted-evaluation"))
