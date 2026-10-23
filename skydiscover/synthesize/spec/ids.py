"""Record IDS design blockers for the outer planner; a failed proof is not a defect verdict.

    ids feedback <run> --report <report.json>
    ids blockers <run>

The report names classification, obligations, source_locations, diagnostics (run-relative files),
attempts, alternatives, requested_decision, and performance_impact. A counterexample report also
needs its concrete counterexample. The command binds the report to the current candidate and
iteration without closing the round, changing the specification, or granting proof acceptance.
"""

from __future__ import annotations

import argparse
import json
import uuid
from pathlib import Path
from typing import Any

from . import checkpoint, iterations
from .paths import Run, run_cli

CLASSIFICATIONS = {"counterexample", "unsupported", "missing_contract", "incomplete", "operational"}


def _strings(value, name: str) -> list[str]:
    if (
        not isinstance(value, list)
        or not value
        or any(not isinstance(item, str) or not item.strip() for item in value)
    ):
        raise ValueError(f"IDS report needs a nonempty list of strings: {name}")
    return value


def feedback(run_dir: Path, report: dict) -> Path:
    run = Run(run_dir.resolve())
    if not run.co_generates_proof():
        raise ValueError(
            "IDS planner feedback requires checked_by: proof and optimize: performance"
        )
    if (
        not isinstance(report, dict)
        or not isinstance(report.get("classification"), str)
        or report["classification"] not in CLASSIFICATIONS
    ):
        raise ValueError("IDS report needs a supported classification")
    if run.entry_impl() is None:
        raise ValueError("No candidate for IDS feedback")
    doc: dict[str, Any] = {"classification": report["classification"]}
    for key in ("obligations", "source_locations", "diagnostics", "attempts", "alternatives"):
        doc[key] = _strings(report.get(key), key)
    for key in ("requested_decision", "performance_impact"):
        if not isinstance(report.get(key), str) or not report[key].strip():
            raise ValueError(f"IDS report needs {key}")
        doc[key] = report[key]
    if doc["classification"] == "counterexample":
        value = report.get("counterexample")
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "A counterexample report needs a concrete execution and violated guarantee"
            )
        doc["counterexample"] = value
    diagnostics = {}
    for named in doc["diagnostics"]:
        path = run.path / named
        if (
            Path(named).is_absolute()
            or ".." in Path(named).parts
            or not path.is_file()
            or not path.resolve().is_relative_to(run.path)
        ):
            raise ValueError(f"Diagnostic must be an existing run-relative file: {named}")
        diagnostics[named] = checkpoint._tree_digest(path)
    directory = run.synthesis / "ids" / "blockers"
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{uuid.uuid4().hex}.json"
    checkpoint._write_json(
        target,
        {
            **doc,
            "schema": 1,
            "created_at": checkpoint._utc_now(),
            "iteration": iterations.current(run.path),
            "input_digests": checkpoint.input_digests(run.path),
            "diagnostic_digests": diagnostics,
            "proof_status": "incomplete",
        },
    )
    return target


def blockers(run_dir: Path) -> list[dict]:
    run = Run(run_dir.resolve())
    inputs, iteration = checkpoint.input_digests(run.path), iterations.current(run.path)
    records = []
    for path in sorted((run.synthesis / "ids" / "blockers").glob("*.json")):
        doc = checkpoint._read_json(path, {})
        if (
            not isinstance(doc, dict)
            or doc.get("schema") != 1
            or not isinstance(doc.get("created_at"), str)
            or not isinstance(doc.get("diagnostic_digests"), dict)
            or not doc["diagnostic_digests"]
        ):
            raise ValueError(f"Malformed IDS feedback: {path}")
        for name in doc["diagnostic_digests"]:
            if (
                not isinstance(name, str)
                or Path(name).is_absolute()
                or ".." in Path(name).parts
                or not (run.path / name).resolve().is_relative_to(run.path)
            ):
                raise ValueError(f"Invalid diagnostic path in IDS feedback: {path}")
        diagnostics_match = all(
            (run.path / name).is_file() and checkpoint._tree_digest(run.path / name) == digest
            for name, digest in doc.get("diagnostic_digests", {}).items()
        )
        records.append(
            {
                **doc,
                "path": path.relative_to(run.path).as_posix(),
                "current": doc.get("input_digests") == inputs
                and doc.get("iteration") == iteration
                and diagnostics_match,
            }
        )
    return sorted(records, key=lambda row: row["created_at"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, prog="ids")
    sub = ap.add_subparsers(dest="action", required=True)
    add = sub.add_parser("feedback", help="record a candidate-bound blocker for the planner")
    add.add_argument("run_dir", type=Path)
    add.add_argument("--report", required=True, type=Path)
    ls = sub.add_parser("blockers", help="list history, marking reports for the current candidate")
    ls.add_argument("run_dir", type=Path)
    args = ap.parse_args(argv)
    if args.action == "feedback":
        print(feedback(args.run_dir, json.loads(args.report.read_text())))
    else:
        print(json.dumps(blockers(args.run_dir), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(run_cli(main, "ids"))
