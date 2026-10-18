#!/usr/bin/env python3
"""Compile a plain candidate plus the frozen DB spec for measurement; this is NOT proof."""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from skydiscover.synthesize.spec.proof_source import spec_attachment


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--impl", type=Path, required=True, help="candidate directory with lib.rs")
    ap.add_argument("--spec", type=Path, required=True)
    ap.add_argument("--config", type=Path, help="defaults to verus.json beside --spec")
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--driver", type=Path, help="append an ordinary Rust main for tests/benchmark")
    ap.add_argument("--verus", default=os.environ.get("SKYDISCOVER_VERUS", "verus"))
    args = ap.parse_args()
    impl = args.impl.resolve()
    output = args.out.resolve()
    if not (impl / "lib.rs").is_file():
        ap.error("--impl must contain lib.rs")
    if (impl / "Cargo.toml").exists():
        ap.error("Cargo projects require a Cargo build wrapper preserving their dependencies")
    if output.is_relative_to(impl):
        ap.error("build outputs must be outside the candidate tree")
    output.parent.mkdir(parents=True, exist_ok=True)
    config = json.loads((args.config or args.spec.parent / "verus.json").read_text())
    with tempfile.TemporaryDirectory(prefix="db-build-", dir=output.parent) as scratch:
        root = Path(scratch)
        project = root / "project"
        shutil.copytree(impl, project)
        shutil.copy2(args.spec, root / "specs.rs")
        entry = project / "lib.rs"
        entry.chmod(0o644)
        driver = ""
        if args.driver:
            # Ordinary Rust callers use the erased API and do not supply ghost
            # AtomicUpdate arguments. Include them only in the final Rust build;
            # leave the candidate's normal ghost/lifetime checking enabled.
            runtime = project / "__audit_runtime_driver.rs"
            if runtime.exists():
                ap.error("candidate collides with reserved __audit_runtime_driver.rs")
            runtime.write_text(args.driver.read_text())
            # Verus's final compilation retains verus_keep_ghost for type
            # declarations, but drops verus_keep_ghost_body (driver.rs upstream).
            driver = '\n#[cfg(not(verus_keep_ghost_body))]\ninclude!("__audit_runtime_driver.rs");\n'
            driver += "#[cfg(verus_keep_ghost_body)]\nfn main() {}\n"
        entry.write_text(
            entry.read_text()
            + spec_attachment("../specs.rs", config.get("macro_imports", {}))
            + driver
        )
        command = [
            args.verus,
            str(entry),
            "--crate-name",
            "db_candidate",
            "--crate-type",
            "bin" if args.driver else "lib",
            "--compile",
            "--no-verify",
            "-C",
            "opt-level=3",
            "-o",
            str(output),
        ]
        print(json.dumps({"command": command, "proof_evidence": False}), flush=True)
        return subprocess.run(command, cwd=project).returncode


if __name__ == "__main__":
    raise SystemExit(main())
