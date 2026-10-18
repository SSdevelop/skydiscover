#!/usr/bin/env python3
"""Prepare, launch, and inspect the local DB synthesis + Verus experiment (stdlib only)."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import platform
import shlex
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
WORKFLOW = REPO / "skydiscover/synthesize/workflow"
DEFAULT_PROJECT = REPO / "outputs/experiments/db-verus"
SLUG = "db-verus"
sys.path.insert(0, str(REPO))


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2) + "\n")


def read_json(path):
    return json.loads(path.read_text())


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def capture(command, **kwargs):
    result = subprocess.run(command, text=True, capture_output=True, timeout=60, **kwargs)
    return {
        "command": command,
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def env_for(project, manifest):
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(REPO) + os.pathsep + env.get("PYTHONPATH", ""),
            "PATH": str(project / ".experiment/bin") + os.pathsep + env.get("PATH", ""),
            "SKYDISCOVER_RUN": str(project / manifest["run"]),
            "SKYDISCOVER_RUNS": str(project / ".skydiscover"),
            "SKYDISCOVER_HOME": str(project / ".skydiscover/kb"),
            "SKYDISCOVER_OUTPUTS": str(project / "outputs/synthesize"),
            "SKYDISCOVER_VERUS": manifest["verus"],
        }
    )
    return env


def integrity(project, manifest):
    for relative, expected in manifest["frozen"].items():
        path = project / relative
        if not path.is_file() or digest(path) != expected:
            raise ValueError(f"frozen experiment input changed: {path}; prepare a new project")
    if manifest.get("baseline"):
        root = project / manifest["baseline"] / "source"
        actual = {str(p.relative_to(project)) for p in root.rglob("*") if p.is_file()}
        expected = {name for name in manifest["frozen"] if name.startswith(str(root.relative_to(project)) + "/")}
        if actual != expected or any(p.is_symlink() for p in root.rglob("*")):
            raise ValueError("frozen baseline source tree changed; prepare a new project")


def prepare(args):
    from skydiscover.synthesize.spec.paths import Run
    from skydiscover.synthesize.spec.run import main_budget

    project = args.project.resolve()
    if project.exists() and any(project.iterdir()):
        raise ValueError(f"refusing to overwrite {project}; use launch/status or a new --project")
    source = args.spec.expanduser().resolve(strict=True)
    binary = shutil.which(args.verus) or args.verus
    binary = str(Path(binary).expanduser().resolve(strict=True))
    if not shutil.which("codex"):
        raise ValueError("codex is not on PATH")
    spec_text = source.read_text()
    if "macro_rules! impl_db" not in spec_text:
        raise ValueError("this experiment expects the supplied impl_db! database contract")
    baseline = args.baseline.expanduser().resolve(strict=True) if args.baseline else None
    if baseline:
        if not (baseline / "lib.rs").is_file():
            raise ValueError("--baseline must contain lib.rs with the frozen database API")
        if any(p.is_symlink() for p in baseline.rglob("*")):
            raise ValueError("baseline snapshot must not contain symlinks")
    project.mkdir(parents=True, exist_ok=True)
    meta = project / ".experiment"
    meta.mkdir()
    run = Run(project / ".skydiscover" / SLUG).create()
    formal = run.specification / "specs.rs"
    formal.write_bytes(source.read_bytes())
    settings = {
        "iterations": args.iterations,
        "audit_every": 1,
        "coding_attempts": args.coding_attempts,
        "proof_attempts": args.proof_attempts,
        "proof_timeout_secs": args.proof_timeout,
        "read_percent": 60,
        "write_percent": 40,
        "keys": args.keys,
        "threads": args.threads,
        "operations_per_thread": args.operations,
        "repetitions": args.repetitions,
        "seed": args.seed,
        "distribution": "uniform",
    }
    baseline_files = []
    if baseline:
        baseline_root = meta / "baseline"
        snapshot = baseline_root / "source"
        shutil.copytree(baseline, snapshot)
        for name in ("baseline.py", "benchmark.rs", "trace.py"):
            shutil.copy2(HERE / name, baseline_root / name)
        settings["performance_baseline"] = {
            "name": "node_locked_bptree",
            "path": ".experiment/baseline",
            "max_keys_per_node": 32,
            "api": "original frozen Database API; no runtime adapter",
            "scope": "get/put performance only; not the correctness oracle or a proved candidate",
        }
        write_json(baseline_root / "provenance.json", {
            "source": str(baseline),
            "git": capture(["git", "-C", str(baseline), "rev-parse", "HEAD"]),
            "git_status": capture(["git", "-C", str(baseline), "status", "--porcelain"]),
            "files": {str(p.relative_to(snapshot)): digest(p) for p in sorted(snapshot.rglob("*")) if p.is_file()},
            "note": "Exact working-tree snapshot; original project is unchanged. No proof claim.",
        })
        baseline_files = [p for p in baseline_root.rglob("*") if p.is_file()]
        # This experiment's baseline is a standalone Rust implementation. Keep its test
        # drivers outside the candidate tree, and retain an independent frozen comparator.
        seed = snapshot / "lib.rs"
        run.impl.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(seed, run.impl / "lib.rs")
        (run.impl / "lib.rs").chmod(0o644)
        settings["initial_implementation"] = {
            "path": str(run.impl.relative_to(project)),
            "source": str(seed.relative_to(project)),
            "sha256": digest(seed),
            "policy": "Start from this B+ tree; inspect and profile it before the first coding brief.",
            "proof_status": "unverified",
        }
    write_json(meta / "settings.json", settings)
    write_json(
        run.verus_config,
        {
            "entry": "lib.rs",
            "spec": "specs.rs",
            "macro_imports": {
                "DatabaseState": "crate::__audit_spec::DatabaseState",
                "key_lt": "crate::__audit_spec::key_lt",
                "key_le": "crate::__audit_spec::key_le",
                "Map": "vstd::prelude::Map",
                "Seq": "vstd::prelude::Seq",
                "Tracked": "vstd::prelude::Tracked",
                "String": "std::string::String",
                "Vec": "std::vec::Vec",
                "Option": "std::option::Option",
                "Some": "std::option::Option::Some",
                "None": "std::option::Option::None",
            },
            "obligations": [
                {"id": "db_" + method, "function": "Database::" + method, "macro": "impl_db"}
                for method in ("new", "get", "put", "scan", "sort")
            ],
            "attempts_per_review": args.proof_attempts,
            "max_stalled": 3,
            "timeout_secs": args.proof_timeout,
            "rlimit": 20,
        },
    )
    task = (HERE / "task.md").read_text().replace("{{SETTINGS}}", json.dumps(settings, indent=2))
    task = task.replace("{{BASELINE}}", (HERE / "baseline.md").read_text() if baseline else "")
    run.task.write_text(task)
    main_budget(
        [
            str(run.path),
            str(args.iterations),
            "--attempts",
            str(args.coding_attempts),
            "--audit-every",
            "1",
        ]
    )
    write_json(
        meta / "host.json",
        {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "logical_cpus": os.cpu_count(),
            "python": sys.version,
            "note": "Inventory only. Spec builder must measure the performance environment card.",
        },
    )
    provenance = {
        "source": str(source),
        "sha256": digest(formal),
        "git": capture(["git", "-C", str(source.parent), "rev-parse", "HEAD"]),
        "source_git_status": capture(["git", "-C", str(source.parent), "status", "--porcelain"]),
        "framework_commit": capture(["git", "-C", str(REPO), "rev-parse", "HEAD"]),
        "framework_status": capture(["git", "-C", str(REPO), "status", "--porcelain"]),
    }
    write_json(meta / "provenance.json", provenance)
    (meta / "bin").mkdir()
    (meta / "bin/python3").symlink_to(Path(sys.executable).resolve())
    manifest = {
        "schema": 1,
        "created": datetime.now(timezone.utc).isoformat(),
        "run": str(run.path.relative_to(project)),
        "verus": binary,
        "frozen": {},
    }
    if baseline:
        manifest["baseline"] = ".experiment/baseline"
        manifest["build_helper"] = str(HERE / "build.py")
    env = env_for(project, manifest)
    subprocess.run(
        [
            "bash",
            str(REPO / "skydiscover/synthesize/scripts/install.sh"),
            "--agent",
            "codex",
            str(project),
        ],
        env=env,
        check=True,
    )
    # Keep the experiment a distinct Codex project, with its own roles and hook trust review.
    subprocess.run(["git", "init", "--quiet", str(project)], check=True)
    hooks_path = project / ".codex/hooks.json"
    hooks = read_json(hooks_path)
    guard = shlex.join([sys.executable, str(HERE / "guard.py"), str(project)])
    hooks["hooks"].setdefault("PreToolUse", []).insert(
        0, {"matcher": ".*", "hooks": [{"type": "command", "command": guard, "timeout": 30}]}
    )
    hooks["hooks"].setdefault("Stop", []).append(
        {
            "hooks": [
                {"type": "command", "command": guard + " --stop", "timeout": 30},
                {
                    "type": "command",
                    "command": shlex.join(
                        [sys.executable, str(WORKFLOW / "hooks/budget_guard.py")]
                    ),
                    "timeout": 30,
                },
            ]
        }
    )
    write_json(hooks_path, hooks)
    (project / "AGENTS.md").write_text(
        "Run the SkySynth workflow with its installed role agents. Read .experiment/prompt.md.\n"
        "The specification, task, budget and experiment settings are immutable.\n"
        "Work only in this experiment; do not modify the framework or the source specification.\n"
        "Use python3 from the experiment launcher environment for all spec.* commands.\n"
    )
    prompt = (
        (HERE / "prompt.md")
        .read_text()
        .replace("{{RUN}}", str(run.path))
        .replace("{{HELPERS}}", str(HERE))
    )
    (meta / "prompt.md").write_text(prompt)
    frozen = [formal, run.verus_config, run.task, run.path / "budget.json", meta / "settings.json", *baseline_files]
    manifest["frozen"] = {str(p.relative_to(project)): digest(p) for p in frozen}
    for path in frozen:
        path.chmod(0o444)
    write_json(meta / "manifest.json", manifest)
    print(f"Prepared {project}\nNext: python3 {HERE / 'run.py'} doctor --project {project}")


def doctor(project, manifest):
    from skydiscover.synthesize.spec.proof import _verification_documents
    from skydiscover.synthesize.spec.proof_source import spec_attachment

    integrity(project, manifest)
    env = env_for(project, manifest)
    run = project / manifest["run"]
    checks = {
        "codex": capture(["codex", "--version"], env=env),
        "verus": capture([manifest["verus"], "--version"], env=env),
        "spec": capture(
            [
                manifest["verus"],
                str(run / "specification/specs.rs"),
                "--crate-type",
                "lib",
                "--output-json",
                "--no-cheating",
            ],
            env=env,
        ),
        "helpers": capture(
            [
                str(project / ".experiment/bin/python3"),
                "-c",
                "from skydiscover.synthesize.spec import proof; print(proof.__file__)",
            ],
            env=env,
        ),
    }
    if manifest.get("baseline"):
        checks["baseline_smoke"] = capture([
            sys.executable, str(project / manifest["baseline"] / "baseline.py"),
            "--project", str(project), "--smoke",
        ], env=env)
    # Merely verifying specs.rs leaves impl_db! uninstantiated. This deliberately broken
    # candidate must reach all five method obligations and be rejected for its bodies.
    with tempfile.TemporaryDirectory(prefix="db-probe-", dir=project / ".experiment") as scratch:
        root = Path(scratch)
        shutil.copy2(run / "specification/specs.rs", root / "specs.rs")
        config = read_json(run / "specification/verus.json")
        source = """use vstd::prelude::*;
verus! {
pub struct Database { pub ghost loc: vstd::resource::Loc }
impl Database { pub open spec fn id(&self) -> vstd::resource::Loc { self.loc } }
}
crate::impl_db! { impl Database {
new() { verus_exec_expr! { { unimplemented!() } } }
get(db, key, lp) { verus_exec_expr! { { unimplemented!() } } }
put(db, key, value, lp) { verus_exec_expr! { { unimplemented!() } } }
scan(db, lo, hi, lp) { verus_exec_expr! { { unimplemented!() } } }
sort(db, lp) { verus_exec_expr! { { unimplemented!() } } }
} }
"""
        (root / "probe.rs").write_text(
            source + spec_attachment("specs.rs", config["macro_imports"])
        )
        probe = capture(
            [
                manifest["verus"],
                str(root / "probe.rs"),
                "--crate-type",
                "lib",
                "--crate-name",
                "db_probe",
                "--output-json",
                "--no-cheating",
            ],
            env=env,
        )
        documents = _verification_documents(probe["stdout"])
        expected = {"db_probe::" + o["function"] for o in config["obligations"]}
        rejected = (
            probe["returncode"] != 0
            and any(
                expected <= set(doc.get("func-details", {}))
                and doc.get("verification-results", {}).get("success") is False
                for doc in documents
            )
            and probe["stderr"].count("precondition not satisfied") >= 5
        )
        checks["atomic_negative_control"] = {
            **probe,
            "verifier_returncode": probe["returncode"],
            "returncode": 0 if rejected else 1,
            "meaning": "all five broken method bodies must be rejected",
        }
    write_json(project / ".experiment/preflight.json", checks)
    for name, result in checks.items():
        print(f"{name}: {'OK' if result['returncode'] == 0 else 'FAILED'}")
    print("Spec preflight checks the model only; candidate methods must be proved during audits.")
    print("First launch: review/trust the project and its hooks in Codex. No trust bypass is used.")
    return 0 if all(r["returncode"] == 0 for r in checks.values()) else 2


def summarize(project, manifest):
    from skydiscover.synthesize.spec.iterations import finished

    integrity(project, manifest)
    run = project / manifest["run"]
    sync_audit(project, run)
    rows = finished(run)
    history_path = project / ".experiment/audit-history.json"
    history = read_json(history_path) if history_path.exists() else []
    audited = {r["finished_iterations"] for r in accepted_audits(run, history)}
    budget = read_json(run / "budget.json")["iterations"]
    value = {
        "iterations": len(rows),
        "budget": budget,
        "audited_iterations": sorted(audited),
        "missing_audits": [r["n"] for r in rows if r["n"] not in audited],
        "audit_outcomes": history,
        "complete": len(rows) == budget and set(range(1, budget + 1)) <= audited,
        "note": "Complete means all rounds audited, not all candidates proved or released.",
    }
    write_json(project / ".experiment/summary.json", value)
    return value


def accepted_audits(run, history):
    from skydiscover.synthesize.spec.proof import completion_allows_planning

    return [r for r in history if completion_allows_planning(run, r)]


def sync_audit(project, run):
    path = run / "synthesis/audit/completed.json"
    records = [read_json(path)] if path.exists() else []
    # Re-auditing unchanged bytes may replay an accepted session and produce a newer release
    # stamp without consuming a fresh proof-search attempt. Match loop_guard's scheduling rule.
    stamp_path = run / "synthesis/audit/completeness.json"
    if stamp_path.exists():
        stamp = read_json(stamp_path)
        evidence = stamp.get("audit_evidence", {})
        if evidence.get("mode") == "proof" and evidence.get("session"):
            records.append(
                {
                    "finished_iterations": stamp.get("finished_iterations"),
                    "session": evidence["session"],
                    "bindings": evidence.get("bindings", {}),
                    "status": "verified",
                }
            )
    records = [
        r
        for r in accepted_audits(run, records)
        if type(r.get("finished_iterations")) is int and r["finished_iterations"] >= 1
    ]
    if not records:
        return
    history_path = project / ".experiment/audit-history.json"
    with (project / ".experiment/audit-history.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        history = read_json(history_path) if history_path.exists() else []
        for record in records:
            if record not in history:
                history.append(record)
        write_json(history_path, history)


def launch(args, manifest):
    project = args.project.resolve()
    integrity(project, manifest)
    env = env_for(project, manifest)
    prompt = (project / ".experiment/prompt.md").read_text()
    command = ["codex"]
    if args.headless:
        command += [
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-o",
            str(project / ".experiment/last-message.md"),
        ]
    command += ["-C", str(project), "--sandbox", "workspace-write"]
    if args.model:
        command += ["--model", args.model]
    command += ["-" if args.headless else prompt]
    if args.dry_run:
        print(
            shlex.join(command[:-1])
            + (" - < .experiment/prompt.md" if args.headless else " '<saved prompt>'")
        )
        return 0
    with (project / ".experiment/launch.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ValueError("this experiment already has a launcher running") from exc
        if doctor(project, manifest):
            return 2
        from skydiscover.synthesize.spec.run import main_resume

        main_resume([str(project / manifest["run"])])
        (project / ".experiment/hooks-seen.json").unlink(missing_ok=True)
        if args.headless:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
            log = project / ".experiment" / ("codex-" + stamp + ".jsonl")
            with log.open("w") as out, log.with_suffix(".stderr.log").open("w") as err:
                print(f"Codex events: {log}", flush=True)
                result = subprocess.run(
                    command, input=prompt, text=True, cwd=project, env=env, stdout=out, stderr=err
                )
        else:
            result = subprocess.run(command, cwd=project, env=env)
    summary = summarize(project, manifest)
    print(json.dumps(summary, indent=2))
    return result.returncode or (0 if summary["complete"] else 3)


def positive(value):
    value = int(value)
    if value < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return value


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("prepare", "doctor", "launch", "status"):
        parser = sub.add_parser(name)
        parser.add_argument("--project", type=Path, default=DEFAULT_PROJECT)
        if name == "prepare":
            parser.add_argument(
                "--spec", type=Path, default=HERE / "specification/specs.rs",
                help="authoritative DB contract (default: bundled specification/specs.rs)",
            )
            parser.add_argument("--baseline", type=Path, default=HERE / "baselines/node_locked_bptree",
                                help="snapshot the standalone B+ tree baseline and seed synthesis/impl/lib.rs from it")
            parser.add_argument(
                "--verus",
                default=os.environ.get("SKYDISCOVER_VERUS")
                or shutil.which("verus")
                or str(Path.home() / "verus/verus"),
            )
            for flag, default in (
                ("iterations", 50),
                ("coding-attempts", 5),
                ("proof-timeout", 300),
                ("keys", 100000),
                ("threads", min(8, os.cpu_count() or 1)),
                ("operations", 100000),
                ("repetitions", 3),
            ):
                parser.add_argument("--" + flag, type=positive, default=default)
            parser.add_argument("--proof-review-every", "--proof-attempts", dest="proof_attempts",
                                type=positive, default=10,
                                help="proof attempts between strategy reviews; not a total cap")
            parser.add_argument("--seed", type=int, default=20261017)
        if name == "launch":
            parser.add_argument("--headless", action="store_true")
            parser.add_argument("--dry-run", action="store_true")
            parser.add_argument("--model", help="otherwise use your configured Codex model")
    args = ap.parse_args(argv)
    try:
        if sys.version_info < (3, 11):
            raise ValueError(
                "use Python 3.11+ (for example: uv run --no-project --python 3.13 python run.py ...)"
            )
        if args.command == "prepare":
            if args.operations % 10:
                raise ValueError("--operations must be divisible by 10 for exact 60:40")
            prepare(args)
            return 0
        manifest = read_json(args.project / ".experiment/manifest.json")
        if args.command == "launch":
            return launch(args, manifest)
        if args.command == "doctor":
            return doctor(args.project.resolve(), manifest)
        print(json.dumps(summarize(args.project.resolve(), manifest), indent=2))
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"db-verus: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
