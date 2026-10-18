#!/usr/bin/env python3
"""Validate or measure the frozen leaf-locked B+ tree; never starts a synthesis round."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import struct
import subprocess
import sys


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, indent=2) + "\n")


def tree_hashes(root):
    return {str(p.relative_to(root)): digest(p) for p in sorted(root.rglob("*")) if p.is_file()}


def check_inputs(project, manifest):
    for relative, expected in manifest["frozen"].items():
        path = project / relative
        if path.is_symlink() or not path.is_file() or digest(path) != expected:
            raise ValueError(f"frozen input changed: {path}")
    root = project / manifest["baseline"] / "source"
    expected = {
        str(Path(name).relative_to(root.relative_to(project))): value
        for name, value in manifest["frozen"].items()
        if name.startswith(str(root.relative_to(project)) + "/")
    }
    if tree_hashes(root) != expected or any(p.is_symlink() for p in root.rglob("*")):
        raise ValueError("frozen baseline source tree changed")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--project", type=Path, required=True)
    ap.add_argument("--out", type=Path, help="new, empty output directory; defaults to a timestamped run")
    ap.add_argument("--impl", dest="implementation", type=Path,
                    help="measure a candidate with the same harness/traces instead of the baseline")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--smoke", action="store_true", help="small build/workload check; not a score")
    mode.add_argument("--validate-only", action="store_true", help="primitive proof and ten runtime suites")
    args = ap.parse_args(argv)
    project = args.project.resolve()
    manifest = json.loads((project / ".experiment/manifest.json").read_text())
    check_inputs(project, manifest)
    root = project / manifest["baseline"]
    impl = args.implementation.resolve() if args.implementation else root / "source"
    if args.validate_only and args.implementation:
        ap.error("--validate-only checks the frozen baseline; candidate validation belongs to the evaluator")
    settings = json.loads((project / ".experiment/settings.json").read_text())
    if args.smoke:
        settings.update(keys=1000, threads=2, operations_per_thread=1000, repetitions=1)
    mode_name = "validation" if args.validate_only else "smoke" if args.smoke else "scored"
    name = "candidate" if args.implementation else "baseline"
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
    out = args.out.resolve() if args.out else root / "results" / mode_name / (name + "-" + stamp)
    if out.is_relative_to(impl):
        raise ValueError("measurement output must be outside the implementation")
    if out.exists() and any(out.iterdir()):
        raise ValueError(f"refusing to overwrite measurement: {out}")
    out.mkdir(parents=True, exist_ok=True)
    write_json(out / "settings.json", settings)
    before = tree_hashes(impl)
    run = project / manifest["run"]
    driver = root / "source/checks/runtime.rs" if args.validate_only else root / "benchmark.rs"
    build = [
        sys.executable, manifest["build_helper"], "--impl", str(impl),
        "--spec", str(run / "specification/specs.rs"),
        "--config", str(run / "specification/verus.json"),
        "--driver", str(driver), "--out", str(out / "benchmark"), "--verus", manifest["verus"],
    ]
    result = {
        "kind": name, "mode": mode_name, "passed": False, "proof_evidence": False,
        "implementation": str(impl), "source_sha256": before, "settings": settings,
        "build": build, "compiler_flags": ["--compile", "--no-verify", "-C", "opt-level=3"],
        "verus": subprocess.check_output([manifest["verus"], "--version"], text=True).strip(),
        "driver_sha256": digest(driver), "build_helper_sha256": digest(Path(manifest["build_helper"])),
        "spec_sha256": digest(run / "specification/specs.rs"),
    }
    write_json(out / "result.json", result)
    if args.validate_only:
        primitive = [manifest["verus"], str(root / "source/checks/verified_operations.rs"), "--no-cheating"]
        with (out / "primitive-proof.log").open("w") as log:
            subprocess.run(primitive, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=240)
        result["primitive_verification"] = primitive
    with (out / "build.log").open("w") as log:
        subprocess.run(build, stdout=log, stderr=subprocess.STDOUT, check=True, timeout=240)
    if args.validate_only:
        with (out / "runtime-checks.log").open("w") as log:
            subprocess.run([str(out / "benchmark")], stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=240)
        result["runtime_suites"] = [
            line.removeprefix("PASS: ").strip()
            for line in (out / "runtime-checks.log").read_text().splitlines() if line.startswith("PASS: ")
        ]
        if len(result["runtime_suites"]) != 10:
            raise ValueError("expected all ten baseline runtime suites")
    else:
        traces = []
        with (out / "traces.bin").open("wb") as packed:
            packed.write(struct.pack("<I", settings["threads"]))
            for worker in range(settings["threads"]):
                path = out / f"worker-{worker}.jsonl"
                traces.append(path)
                with path.open("w") as stream:
                    subprocess.run([
                        sys.executable, str(root / "trace.py"), "--settings", str(out / "settings.json"),
                        "--worker", str(worker),
                    ], stdout=stream, check=True, timeout=60)
                packed.write(struct.pack("<I", settings["operations_per_thread"]))
                with path.open() as stream:
                    for line in stream:
                        row = json.loads(line)
                        key = row["key"].encode("utf-8")
                        packed.write(struct.pack("<BI", row["op"] == "put", len(key)))
                        packed.write(key)
                        packed.write(struct.pack("<i", row.get("value", 0)))
        command = [str(out / "benchmark"), str(out / "traces.bin"), str(settings["keys"]),
                   str(settings["repetitions"])]
        with (out / "raw.jsonl").open("w") as stdout, (out / "stderr.txt").open("w") as stderr:
            process = subprocess.Popen(command, stdout=stdout, stderr=stderr)
            _, status, usage = os.wait4(process.pid, 0)
            process.returncode = os.waitstatus_to_exitcode(status)
            resources = {key: getattr(usage, key) for key in (
                "ru_utime", "ru_stime", "ru_maxrss", "ru_minflt", "ru_majflt",
                "ru_inblock", "ru_oublock", "ru_nvcsw", "ru_nivcsw",
            )}
            resources.update(returncode=process.returncode, method="wait4 exact benchmark child",
                             maxrss_unit="bytes" if sys.platform == "darwin" else "KiB")
            write_json(out / "resources.json", resources)
            if process.returncode:
                raise subprocess.CalledProcessError(process.returncode, command)
        measurements = [json.loads(line) for line in (out / "raw.jsonl").read_text().splitlines()]
        measured = [row for row in measurements if not row["warmup"]]
        expected_ops = settings["threads"] * settings["operations_per_thread"]
        if (len(measured) != settings["repetitions"] or len(measurements) != len(measured) + 1
                or not measurements[0]["warmup"]
                or any(row["operations"] != expected_ops or row["seconds"] <= 0 for row in measurements)):
            raise ValueError("benchmark output does not match the requested workload")
        result.update(
            measurements=measurements, invocation=command, resources=resources,
            median_ops_per_second=statistics.median(row["ops_per_second"] for row in measured),
            trace_sha256={p.name: digest(p) for p in traces},
            packed_trace_sha256=digest(out / "traces.bin"),
        )
    check_inputs(project, manifest)
    if tree_hashes(impl) != before:
        raise ValueError("implementation changed during measurement")
    result.update(passed=True, binary_sha256=digest(out / "benchmark"))
    write_json(out / "result.json", result)
    write_json(root / f"latest-{name}-{mode_name}.json", {"result": str(out / "result.json")})
    print(json.dumps({"result": str(out / "result.json"), "passed": True, "mode": mode_name,
                      "median_ops_per_second": result.get("median_ops_per_second")}, indent=2))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ValueError, OSError, subprocess.SubprocessError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(2)
