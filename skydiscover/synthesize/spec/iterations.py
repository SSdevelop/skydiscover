"""The run's iterations: which ones are finished, and how each ended.

    <run>/synthesis/bench/iterations.json   [{n, outcome, checkpoint, reason, at}, ...]

An iteration is one round of the synthesis loop. It ends in one of two ways:

- `checkpoint`: a candidate passed the tests and was scored; `spec.checkpoint snapshot` records it;
- `failed`: the round spent its coding attempts without a scored candidate (hooks/loop_guard.py
  caps them); the lead records it with `spec.iterations fail <run> --reason "..."`, and the next
  round starts from the best checkpoint. As in the paper, a failed round still counts toward the
  budget, so the loop moves on instead of retrying one idea forever.

Iteration 0 is the specification; the iteration in progress is the number of finished ones plus
one. A run that has no iteration record yet (one started before it existed) is read from its checkpoints,
one finished iteration each, and the record is seeded from them on its first write.

    iterations list <run_dir>
    iterations fail <run_dir> --reason "<why the round produced no scored candidate>"
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import sys
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .paths import Run, files_under


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def record_path(run_dir: Path) -> Path:
    return Run(run_dir).bench / "iterations.json"


def published_output(run_dir: Path) -> Optional[Path]:
    """The run's output folder, from `<run>/.output` (checkpoint.published_output, without the
    import cycle)."""
    run = Run(run_dir)
    try:
        raw = run.output_pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    target = Path(raw)
    if not target.is_absolute():
        target = run.path.resolve().parent.parent / raw
    return target if target.is_dir() else None


def checkpoints(run_dir: Path) -> List[Tuple[int, Path]]:
    """(K, checkpoint_K/) for every checkpoint written, in order."""
    out = published_output(run_dir)
    if out is None or not (out / "checkpoints").is_dir():
        return []
    found = []
    for p in (out / "checkpoints").iterdir():
        m = re.fullmatch(r"checkpoint_(\d+)", p.name)
        if m and p.is_dir():
            found.append((int(m.group(1)), p))
    return sorted(found)


def _from_checkpoints(run_dir: Path) -> List[Dict[str, Any]]:
    rows = []
    for i, (_, cp) in enumerate(checkpoints(run_dir), 1):
        score = _read_json(cp / "score.json", {})
        rows.append(
            {"n": i, "outcome": "checkpoint", "checkpoint": cp.name, "reason": "",
             "at": score.get("created_at") if isinstance(score, dict) else None}
        )
    return rows


def finished(run_dir: Path) -> List[Dict[str, Any]]:
    """Every finished iteration, in order."""
    rows = _read_json(record_path(run_dir), None)
    if isinstance(rows, list):
        return rows
    return _from_checkpoints(run_dir)


def current(run_dir: Path) -> int:
    """0 while the specification is being written (no candidate yet); otherwise the iteration in
    progress, one past the finished ones."""
    done = len(finished(run_dir))
    if done:
        return done + 1
    impl = Run(run_dir).impl
    return 1 if impl.is_dir() and files_under(impl) else 0


@contextmanager
def _lock(run_dir: Path):
    path = record_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(".iterations.json.lock"), "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def close(run_dir: Path, outcome: str, *, checkpoint: str = "", reason: str = "") -> int:
    """Record the iteration in progress as finished and return its number. Recording the same
    checkpoint twice returns the iteration it already closed."""
    if outcome not in ("checkpoint", "failed"):
        raise ValueError(f"an iteration ends in a checkpoint or fails, not {outcome!r}")
    with _lock(run_dir):
        path = record_path(run_dir)
        rows = _read_json(path, None)
        if not isinstance(rows, list):
            rows = [r for r in _from_checkpoints(run_dir) if r["checkpoint"] != checkpoint]
        if checkpoint:
            for r in rows:
                if r.get("checkpoint") == checkpoint:
                    return int(r["n"])
        n = len(rows) + 1
        rows.append(
            {"n": n, "outcome": outcome, "checkpoint": checkpoint or None, "reason": reason,
             "at": _utc_now()}
        )
        tmp = path.with_name(f".iterations.json.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
        tmp.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return n


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="iterations", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    ls = sub.add_parser("list", help="every finished iteration and how it ended")
    ls.add_argument("run_dir")
    f = sub.add_parser("fail", help="close the iteration in progress without a scored candidate")
    f.add_argument("run_dir")
    f.add_argument("--reason", required=True)
    args = ap.parse_args(argv)
    run = Run(args.run_dir)
    if not run.task.is_file():
        print(f"iterations: {run.path} is not a run directory (no task.md)", file=sys.stderr)
        return 2
    if args.command == "fail":
        if current(run.path) == 0:
            print("iterations: the synthesis loop has not started; nothing to fail", file=sys.stderr)
            return 2
        n = close(run.path, "failed", reason=args.reason)
        print(f"iteration {n}: failed ({args.reason}); iteration {n + 1} starts from the best checkpoint")
        return 0
    for r in finished(run.path):
        print(f"  {r['n']:>3}  {r['outcome']:<10} {r.get('checkpoint') or '-':<14} {r.get('at') or ''}  {r.get('reason') or ''}")
    print(f"in progress: iteration {current(run.path)}")
    return 0


if __name__ == "__main__":
    from .paths import run_cli

    raise SystemExit(run_cli(main, "iterations"))
