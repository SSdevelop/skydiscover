"""Reuse an earlier run's discovery instead of mining the reference systems again.

Discovery (Phase 1, Step 1) clones the reference systems and turns each into a verified spec with
file:line citations; it is the most expensive part of a run. An earlier run of the same domain in
this project already did it, and its snapshots in the knowledge base keep the result
(<kb>/<domain>/runs/<run>/snapshots/<n>/specification/). This copies the newest one into a new run:

- specification/references/   every per-system spec, its citation check, the real tests, and the
                               cross-system aggregates (axes, skeleton, literature, tests.json);
- specification/sources/       the links into the shared clone cache, where the clone still exists.

Then, for each system, it compares the commit the spec was verified at with the clone's commit
today and writes specification/references/reuse.json: which systems are fresh (reuse as they are)
and which are stale or missing (the spec-builder re-verifies or re-clones only those). The run's
questions, answers, and cards are not copied: they belong to the task, not to the domain.

    reuse <run_dir> [--force]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .paths import Domain, Run, near_run

MANIFEST = "manifest.json"


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _head(repo: Path) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _pinned(spec: Path) -> Optional[str]:
    """The commit a per-system spec was verified at: `"source": "owner/repo @ <sha>"`."""
    doc = _read_json(spec, {})
    text = json.dumps(doc.get("source") if isinstance(doc, dict) else "") or ""
    m = re.search(r"@\s*([0-9a-f]{7,40})", text)
    return m.group(1) if m else None


@near_run
def previous_discovery(run_dir: Path) -> Optional[Path]:
    """The newest snapshot, of an earlier run of this domain, holding verified reference specs."""
    run = Run(run_dir)
    slug = run.domain()
    if not slug:
        return None
    mine = None
    if run.archive_pointer.is_file():
        mine = Path(run.archive_pointer.read_text(encoding="utf-8").strip()).resolve()
    best, best_at = None, ""
    for snap in Domain(slug).runs.glob("*/snapshots/*"):
        if not (snap / MANIFEST).is_file() or snap.parents[1].resolve() == mine:
            continue
        refs = snap / "specification" / "references"
        if not any(refs.glob("*/verification.json")):
            continue
        at = _read_json(snap / MANIFEST, {}).get("created_at") or ""
        if at > best_at:
            best, best_at = snap, at
    return best


def _writable_copy(src: Path, dst: Path) -> None:
    shutil.copytree(src, dst, symlinks=True, dirs_exist_ok=True)
    for here, _, names in os.walk(dst):
        for name in names:
            p = Path(here) / name
            if not p.is_symlink():
                os.chmod(p, stat.S_IMODE(os.stat(p).st_mode) | 0o200)


@near_run
def reuse(run_dir: Path, *, force: bool = False) -> Dict[str, Any]:
    """Copy the previous discovery into this run and report what is fresh. {} when there is none."""
    run = Run(run_dir)
    snap = previous_discovery(run.path)
    if snap is None:
        return {}
    if not force and any(run.references.glob("*/verification.json")):
        raise ValueError(
            f"{run.references} already holds discovery; pass --force to replace it with {snap}"
        )
    src_spec = snap / "specification"
    _writable_copy(src_spec / "references", run.references)
    linked: List[str] = []
    if (src_spec / "sources").is_dir():
        run.sources.mkdir(parents=True, exist_ok=True)
        for link in sorted((src_spec / "sources").iterdir()):
            dest = run.sources / link.name
            if link.is_symlink() and not dest.exists() and Path(os.readlink(link)).exists():
                os.symlink(os.readlink(link), dest)
                linked.append(link.name)
    recorded = {Path(c["path"]).name: c for c in _read_json(snap / MANIFEST, {}).get("clones", [])}
    systems: Dict[str, Dict[str, Any]] = {}
    for spec in sorted(run.references.glob("*/spec.json")):
        name = spec.parent.name
        if not (spec.parent / "verification.json").is_file():
            continue
        pinned = _pinned(spec)
        clone = run.sources / name
        head = _head(clone) if clone.exists() else None
        fresh = bool(pinned and head and (head.startswith(pinned) or pinned.startswith(head)))
        systems[name] = {
            "pinned": pinned,
            "head": head,
            "fresh": fresh,
            "clone": "linked" if name in linked or clone.exists() else "missing",
            **({"url": recorded[name].get("url")} if name in recorded else {}),
        }
    report = {
        "from": str(snap),
        "copied_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "systems": systems,
        "fresh": sorted(n for n, s in systems.items() if s["fresh"]),
        "to_reverify": sorted(n for n, s in systems.items() if not s["fresh"]),
    }
    (run.references / "reuse.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    acq = _read_json(run.acquisitions, {})
    acq = acq if isinstance(acq, dict) else {}
    acq["reused_from_run"] = {"snapshot": str(snap), "systems": sorted(systems)}
    run.acquisitions.write_text(json.dumps(acq, indent=2) + "\n", encoding="utf-8")
    return report


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="reuse", description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir")
    ap.add_argument("--force", action="store_true", help="replace discovery already in the run")
    args = ap.parse_args(argv)
    run = Run(args.run_dir)
    if not run.task.is_file():
        print(f"reuse: {run.path} is not a run directory (no task.md)", file=sys.stderr)
        return 2
    report = reuse(run.path, force=args.force)
    if not report:
        print("reuse: no earlier discovery for this domain in the knowledge base; run full discovery")
        return 0
    print(f"reuse: copied discovery of {len(report['systems'])} system(s) from {report['from']}")
    print(f"  fresh (reuse as is): {', '.join(report['fresh']) or '-'}")
    print(f"  re-verify or re-clone: {', '.join(report['to_reverify']) or '-'}")
    print(f"  details: {run.references / 'reuse.json'}")
    return 0


if __name__ == "__main__":
    from .paths import run_cli

    raise SystemExit(run_cli(main, "reuse"))
