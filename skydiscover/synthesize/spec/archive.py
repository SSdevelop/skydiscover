"""Keep every iteration of a run in the knowledge base, so nothing a run learned is ever lost.

    <kb>/runs/<slug>_<timestamp>/
    ├── index.json                   every snapshot, and a per-iteration table: its snapshots, its
    │                                checkpoint and score, and the tokens it spent (total and by role)
    ├── iterations.md                the same table, for a person
    ├── snapshots/<NNNN>_iter-<KK>_<label>/
    │                                the whole run directory at that moment, plus manifest.json
    └── transcripts/<session>/       the Claude Code transcripts of every session that worked on the
                                     run: main.jsonl, subagents/, tool-results/ (kept current)

An iteration is one coding-agent change, one scored evaluation, one checkpoint: iteration K is the
work that ends in checkpoint_K. Iteration 0 is the specification, before the first candidate. The
work after checkpoint_K belongs to iteration K+1 until its checkpoint is written, so an attempt that
failed its tests and was never scored is still filed under the iteration it belonged to.

Snapshots are taken:

- at every checkpoint (`spec.checkpoint snapshot`), labelled `checkpoint_<K>`;
- by hooks/token_usage.py whenever a role (subagent) finishes, a lead turn ends, before a
  compaction, and when a session ends, so every attempt inside an iteration is kept too;
- at `run finish`, whether or not the export passed, and before `--delete-run` removes the run.

A snapshot that would equal the previous one is not written again. A file identical to the one in
the previous snapshot is a hard link to it, so an unchanged file costs nothing; archived files are
read-only. Each manifest records how far every mirrored transcript had grown at that moment, so the
conversation behind one snapshot is the transcript bytes between its offsets and the previous ones.

Reference-system clones (a directory holding `.git`) are recorded as {path, url, commit} rather
than copied: they are re-downloadable at that commit and the shared ones stay in
`<home>/.cache/sources/`. Symlinks are kept as symlinks. Runtime caches (`__pycache__`, `*.pyc`,
lock sidecars) are the only files left out.

    archive snapshot <run_dir> [--label <label>] [--domain <name>]
    archive list <domain>
    archive restore <snapshot_dir> <dest>
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .paths import Domain, Run, files_under, near_run

MANIFEST = "manifest.json"
_SKIP_DIRS = {"__pycache__", ".pytest_cache"}
_SKIP_FILES = {".DS_Store"}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _skipped(name: str) -> bool:
    return (
        name in _SKIP_FILES
        or name.endswith((".pyc", ".pyo"))
        or (name.startswith(".") and name.endswith(".lock"))
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _git(repo: Path, *args: str) -> Optional[str]:
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _clone_record(rel: str, repo: Path) -> Dict[str, Any]:
    return {
        "path": rel,
        "url": _git(repo, "remote", "get-url", "origin"),
        "commit": _git(repo, "rev-parse", "HEAD"),
    }


def _write_json(path: Path, value: Any) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


@contextmanager
def _lock(archive: Path):
    """One writer at a time per run: a checkpoint, a finish, and several hooks may race."""
    archive.mkdir(parents=True, exist_ok=True)
    with open(archive / ".archive.lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


# ------------------------------------------------------------------------------------ iterations


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


def current_iteration(run_dir: Path) -> int:
    """0 while the specification is being written (no candidate yet); K+1 once checkpoint_K exists,
    the iteration now in progress."""
    done = len(checkpoints(run_dir))
    if done:
        return done + 1
    impl = Run(run_dir).impl
    return 1 if impl.is_dir() and files_under(impl) else 0


# ------------------------------------------------------------------------------------ the archive


@near_run
def archive_for(run_dir: Path, domain: Optional[str] = None) -> Path:
    """This run's folder under `<kb>/runs/`, created on first use and remembered in `<run>/.archive`
    so every later snapshot, from any process, lands beside the first."""
    run = Run(run_dir)
    if run.archive_pointer.is_file():
        recorded = Path(run.archive_pointer.read_text(encoding="utf-8").strip()).expanduser()
        if recorded.is_absolute():
            recorded.mkdir(parents=True, exist_ok=True)
            return recorded
    slug = domain or run.domain()
    if not slug:
        raise ValueError(
            f"no domain for {run_dir}: put `domain: <name>` in the front matter of {run.task}"
        )
    root = Domain(slug).runs
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")  # local, like outputs/<slug>_<timestamp>/
    path = root / f"{run.slug}_{stamp}"
    n = 1
    while path.exists():
        path = root / f"{run.slug}_{stamp}_{n}"
        n += 1
    path.mkdir(parents=True)
    run.archive_pointer.write_text(f"{path}\n", encoding="utf-8")
    return path


def _snapshots(archive: Path) -> List[Path]:
    """Completed snapshots, oldest first (a snapshot is complete once its manifest is written)."""
    root = archive / "snapshots"
    if not root.is_dir():
        return []
    done = [p for p in root.iterdir() if p.is_dir() and (p / MANIFEST).is_file()]
    return sorted(done, key=lambda p: p.name)


def _label(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "snapshot"


def _copy_tree(
    src: Path,
    dst: Path,
    previous: Optional[Path],
    before: Dict[str, Dict[str, Any]],
    skip_top: Tuple[str, ...] = (),
) -> Dict[str, Any]:
    """Copy src into dst, hard-linking each file whose bytes match the previous snapshot's. A file
    whose size and mtime match the previous manifest is not re-hashed. `skip_top` names entries
    directly under src that are left out."""
    files: Dict[str, Dict[str, Any]] = {}
    links: Dict[str, str] = {}
    clones: List[Dict[str, Any]] = []
    new_bytes = 0
    for here, dirs, names in os.walk(src, followlinks=False):
        base = Path(here)
        rel_dir = base.relative_to(src)
        if rel_dir == Path(".") and skip_top:
            dirs[:] = [d for d in dirs if d not in skip_top]
            names = [n for n in names if n not in skip_top]
        (dst / rel_dir).mkdir(parents=True, exist_ok=True)
        keep = []
        for d in sorted(dirs):
            path, rel = base / d, (rel_dir / d).as_posix()
            if d in _SKIP_DIRS:
                continue
            if path.is_symlink():
                target = os.readlink(path)
                os.symlink(target, dst / rel)
                links[rel] = target
                if (path / ".git").exists():
                    clones.append(_clone_record(rel, path))
                continue
            if d == ".git":
                clones.append(_clone_record(rel_dir.as_posix(), base))
                continue
            if (path / ".git").exists():
                clones.append(_clone_record(rel, path))
                continue
            keep.append(d)
        dirs[:] = keep
        for name in sorted(names):
            if _skipped(name):
                continue
            path, rel = base / name, (rel_dir / name).as_posix()
            out = dst / rel
            if path.is_symlink():
                target = os.readlink(path)
                os.symlink(target, out)
                links[rel] = target
                continue
            if not path.is_file():
                continue  # a socket or fifo holds no data
            st = path.stat()
            prior = before.get(rel) or {}
            if prior.get("bytes") == st.st_size and prior.get("mtime_ns") == st.st_mtime_ns:
                digest = prior.get("sha256") or _sha256(path)
            else:
                digest = _sha256(path)
            linked = False
            if previous is not None and prior.get("sha256") == digest:
                try:
                    os.link(previous / rel, out)
                    linked = True
                except OSError:
                    linked = False
            if not linked:
                shutil.copy2(path, out)
                os.chmod(out, stat.S_IMODE(os.stat(out).st_mode) & ~0o222)
                new_bytes += st.st_size
            files[rel] = {"sha256": digest, "bytes": st.st_size, "mtime_ns": st.st_mtime_ns}
    return {"files": files, "links": links, "clones": clones, "new_bytes": new_bytes}


def _mirror_transcripts(archive: Path, transcript: Path, session_id: str) -> Dict[str, int]:
    """Keep a current copy of a session's transcripts (main, subagents, persisted tool results)
    under `transcripts/<session>/`; they are append-only, so the newest copy holds every earlier
    one. Returns each file's size now, the offsets a snapshot records."""
    dest = archive / "transcripts" / _label(session_id)
    sources: List[Tuple[Path, str]] = [(transcript, "main.jsonl")]
    session_dir = transcript.with_suffix("")
    if session_dir.is_dir():
        sources += [
            (p, p.relative_to(session_dir).as_posix())
            for p in sorted(session_dir.rglob("*"))
            if p.is_file() and not p.is_symlink()
        ]
    offsets: Dict[str, int] = {}
    for src, rel in sources:
        try:
            st = src.stat()
        except OSError:
            continue
        out = dest / rel
        try:
            ost = out.stat()
            fresh = ost.st_size == st.st_size and ost.st_mtime_ns >= st.st_mtime_ns
        except OSError:
            fresh = False
        if not fresh:
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(f".{out.name}.tmp.{os.getpid()}")
            shutil.copy2(src, tmp)
            os.replace(tmp, out)
        offsets[rel] = st.st_size
    index = _read_json(archive / "index.json", {})
    if isinstance(index, dict):
        index.setdefault("transcripts", {})[session_id] = {
            "source": str(transcript),
            "mirror": str(dest),
            "mirrored_at": _utc_now(),
        }
        _write_json(archive / "index.json", index)
    return offsets


def _iteration_table(run: Run, archive: Path, index: Dict[str, Any]) -> Dict[str, Any]:
    """Per iteration: its snapshots, its checkpoint and score, and the tokens it spent. Derived
    from the snapshot list, the checkpoints, and <run>/token_usage.json, so it is always current."""
    table: Dict[str, Dict[str, Any]] = {}

    def row(k: Any) -> Dict[str, Any]:
        return table.setdefault(
            str(k),
            {"snapshots": [], "first_at": None, "last_at": None, "checkpoint": None, "score": None},
        )

    for snap in index.get("snapshots", []):
        r = row(snap.get("iteration", 0))
        r["snapshots"].append(snap["snapshot"])
        r["first_at"] = r["first_at"] or snap.get("created_at")
        r["last_at"] = snap.get("created_at")
    for k, cp in checkpoints(run.path):
        r = row(k)
        score = _read_json(cp / "score.json", {})
        r["checkpoint"] = cp.name
        r["score"] = score.get("score")
        r["became_best"] = score.get("became_best")
        r["checkpoint_at"] = score.get("created_at")
    usage = _read_json(run.token_usage, {})
    for k, tokens in ((usage or {}).get("by_iteration") or {}).items():
        row(k)["tokens"] = tokens
    return dict(sorted(table.items(), key=lambda kv: int(kv[0])))


def _iterations_md(index: Dict[str, Any]) -> str:
    lines = [
        f"# Run `{index.get('run')}`: every iteration",
        "",
        "Iteration 0 is the specification; iteration K ends in checkpoint_K. Tokens include every "
        "subagent. Snapshots are under `snapshots/`, transcripts under `transcripts/`.",
        "",
        "| iteration | checkpoint | score | tokens | by role | snapshots |",
        "|---|---|---|---|---|---|",
    ]
    for k, r in (index.get("iterations") or {}).items():
        tokens = r.get("tokens") or {}
        roles = ", ".join(
            f"{name.split(':')[-1]} {u.get('total_tokens', 0):,}"
            for name, u in sorted(
                (tokens.get("by_agent") or {}).items(), key=lambda kv: -kv[1].get("total_tokens", 0)
            )
        )
        score = ", ".join(f"{m} {v}" for m, v in (r.get("score") or {}).items())
        best = " (best)" if r.get("became_best") else ""
        lines.append(
            f"| {k} | {r.get('checkpoint') or '-'}{best} | {score or '-'} | "
            f"{tokens.get('total_tokens', 0):,} | {roles or '-'} | {len(r.get('snapshots', []))} |"
        )
    lines += ["", "## Snapshots", "", "| snapshot | iteration | trigger | detail | time |", "|---|---|---|---|---|"]
    for s in index.get("snapshots", []):
        lines.append(
            f"| {s['snapshot']} | {s.get('iteration', 0)} | {s.get('trigger') or '-'} | "
            f"{str(s.get('detail') or '-').replace('|', '/')} | {s.get('created_at')} |"
        )
    return "\n".join(lines) + "\n"


def _refresh_index(run: Run, archive: Path, domain: Optional[str], row: Optional[dict]) -> None:
    index = _read_json(archive / "index.json", {})
    if not isinstance(index, dict):
        index = {}
    index.setdefault("run", run.slug)
    index.setdefault("domain", domain or run.domain())
    index["run_dir"] = str(run.path.resolve())
    if row is not None:
        index.setdefault("snapshots", []).append(row)
    index["iterations"] = _iteration_table(run, archive, index)
    _write_json(archive / "index.json", index)
    (archive / "iterations.md").write_text(_iterations_md(index), encoding="utf-8")


@near_run
def snapshot(
    run_dir: Path,
    label: str = "snapshot",
    *,
    domain: Optional[str] = None,
    iteration: Optional[int] = None,
    trigger: str = "manual",
    detail: Optional[str] = None,
    transcript: Optional[Path] = None,
    session_id: Optional[str] = None,
) -> Path:
    """Copy the whole run directory into a new snapshot and return it. When nothing changed since
    the previous snapshot, that one is returned instead of a duplicate (the transcripts and the
    iteration table are still brought up to date)."""
    run = Run(run_dir)
    if not run.path.is_dir():
        raise FileNotFoundError(f"no run directory at {run.path}")
    archive = archive_for(run.path, domain)
    iteration = current_iteration(run.path) if iteration is None else iteration
    with _lock(archive):
        root = archive / "snapshots"
        root.mkdir(exist_ok=True)
        for stale in root.glob(".*.tmp.*"):  # left by a writer that was killed; we hold the lock
            shutil.rmtree(stale, ignore_errors=True)
        offsets: Dict[str, Dict[str, int]] = {}
        if transcript is not None and Path(transcript).is_file():
            sid = session_id or Path(transcript).stem
            offsets[sid] = _mirror_transcripts(archive, Path(transcript), sid)
        done = _snapshots(archive)
        previous = done[-1] if done else None
        prev_manifest = _read_json(previous / MANIFEST, {}) if previous is not None else {}
        before = prev_manifest.get("files") or {}
        number = 1 + max((int(p.name.split("_", 1)[0]) for p in done), default=0)
        name = f"{number:04d}_iter-{iteration:02d}_{_label(label)}"
        staging = root / f".{name}.tmp.{os.getpid()}"
        try:
            copied = _copy_tree(run.path, staging, previous, before)
            unchanged = (
                previous is not None
                and {k: v["sha256"] for k, v in copied["files"].items()}
                == {k: v.get("sha256") for k, v in before.items()}
                and copied["links"] == prev_manifest.get("links")
                and prev_manifest.get("iteration", iteration) == iteration
            )
            if unchanged:
                shutil.rmtree(staging)
                _refresh_index(run, archive, domain, None)
                refresh_kb_version(run.path, domain)
                return previous
            manifest = {
                "label": label,
                "trigger": trigger,
                "detail": detail,
                "iteration": iteration,
                "created_at": _utc_now(),
                "run_dir": str(run.path.resolve()),
                "files": copied["files"],
                "links": copied["links"],
                "clones": copied["clones"],
                "transcripts": offsets,
            }
            _write_json(staging / MANIFEST, manifest)
            os.replace(staging, root / name)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        usage = _read_json(run.token_usage, {})
        total = (usage or {}).get("total") if isinstance(usage, dict) else None
        _refresh_index(
            run,
            archive,
            domain,
            {
                "snapshot": name,
                "label": label,
                "trigger": trigger,
                "detail": detail,
                "iteration": iteration,
                "created_at": manifest["created_at"],
                "files": len(copied["files"]),
                "bytes": sum(v["bytes"] for v in copied["files"].values()),
                "new_bytes": copied["new_bytes"],
                "clones": len(copied["clones"]),
                "total_tokens": total.get("total_tokens") if isinstance(total, dict) else None,
            },
        )
    refresh_kb_version(run.path, domain)
    return root / name


# ------------------------------------------------------------------------------------ versions


# What a version of the knowledge base holds: everything in the domain's folder except the runs'
# own archives and the other versions.
_NOT_IN_VERSION = ("runs", "versions")


@near_run
def refresh_kb_version(run_dir: Path, domain: Optional[str] = None) -> Optional[Path]:
    """Write `<kb>/<domain>/versions/<this run's archive name>/`: the domain's knowledge base
    (tests/, decisions.json, wiki/, ...) as it stands now, i.e. as this run found it at its first
    snapshot and as it leaves it at `run finish`. One version per run, so a run today and a run
    tomorrow leave two; a run only ever rewrites its own. Unchanged files are hard links to the
    version's previous state; an unchanged knowledge base rewrites nothing."""
    run = Run(run_dir)
    slug = domain or run.domain()
    if not slug:
        return None
    store = Domain(slug)
    archive = archive_for(run.path, domain)
    root = store.versions
    root.mkdir(parents=True, exist_ok=True)
    dest = root / archive.name
    with _lock(root):
        manifest = _read_json(dest / MANIFEST, {}) if dest.is_dir() else {}
        before = manifest.get("files") or {}
        staging = root / f".{archive.name}.tmp.{os.getpid()}"
        shutil.rmtree(staging, ignore_errors=True)
        try:
            staging.mkdir()
            if store.path.is_dir():
                copied = _copy_tree(
                    store.path, staging, dest if dest.is_dir() else None, before, _NOT_IN_VERSION
                )
            else:
                copied = {"files": {}, "links": {}, "clones": [], "new_bytes": 0}
            if dest.is_dir() and {k: v["sha256"] for k, v in copied["files"].items()} == {
                k: v.get("sha256") for k, v in before.items()
            }:
                shutil.rmtree(staging)
                return dest
            now = _utc_now()
            _write_json(
                staging / MANIFEST,
                {
                    "run": run.slug,
                    "run_archive": str(archive),
                    "created_at": manifest.get("created_at") or now,
                    "updated_at": now,
                    "files": copied["files"],
                },
            )
            old = root / f".{archive.name}.old.{os.getpid()}"
            if dest.exists():
                os.replace(dest, old)
            os.replace(staging, dest)
            shutil.rmtree(old, ignore_errors=True)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        index = _read_json(root / "index.json", {})
        rows = [r for r in (index.get("versions") or []) if r.get("version") != archive.name]
        prior = next((r for r in index.get("versions") or [] if r.get("version") == archive.name), {})
        files = copied["files"]
        rows.append(
            {
                "version": archive.name,
                "run": run.slug,
                "run_dir": str(run.path.resolve()),
                "created_at": prior.get("created_at") or now,
                "updated_at": now,
                "tests": sum(
                    1
                    for k in files
                    if k.startswith("tests/") and k.count("/") == 1 and k not in ("tests/index.json", "tests/test.sh")
                ),
                "decisions": len(_read_json(dest / "decisions.json", []) or []),
                "wiki_pages": sum(1 for k in files if k.startswith("wiki/") and k.endswith(".md")),
            }
        )
        rows.sort(key=lambda r: r["created_at"])
        _write_json(root / "index.json", {"domain": slug, "versions": rows})
        return dest


def try_snapshot(run_dir: Path, label: str, **kwargs: Any) -> List[str]:
    """snapshot() for callers whose own job must not fail because the archive could not be
    written (a full disk, no domain yet). Returns report lines; a failure is a WARNING line."""
    try:
        path = snapshot(run_dir, label, **kwargs)
    except (OSError, ValueError) as e:
        return [f"  archive: WARNING: could not snapshot {run_dir} ({e})"]
    return [f"  archive: {label} -> {path}"]


def restore(snapshot_dir: Path, dest: Path) -> Path:
    """Copy a snapshot back out as a working run directory (writable, without its manifest)."""
    snapshot_dir = Path(snapshot_dir)
    if not (snapshot_dir / MANIFEST).is_file():
        raise ValueError(f"{snapshot_dir} is not a snapshot (no {MANIFEST})")
    dest = Path(dest)
    if dest.exists():
        raise ValueError(f"{dest} exists; restore into a new path")
    shutil.copytree(snapshot_dir, dest, symlinks=True, ignore=shutil.ignore_patterns(MANIFEST))
    for here, _, names in os.walk(dest):
        for name in names:
            p = Path(here) / name
            if not p.is_symlink():
                os.chmod(p, stat.S_IMODE(os.stat(p).st_mode) | 0o200)
    return dest


def list_runs(domain: str) -> List[Dict[str, Any]]:
    root = Domain(domain).runs
    if not root.is_dir():
        return []
    rows = []
    for archive in sorted(p for p in root.iterdir() if p.is_dir()):
        index = _read_json(archive / "index.json", {})
        rows.append(
            {
                "archive": str(archive),
                "snapshots": index.get("snapshots", []),
                "iterations": index.get("iterations", {}),
            }
        )
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="archive", description="Keep every iteration of a run in the knowledge base."
    )
    sub = ap.add_subparsers(dest="command", required=True)
    s = sub.add_parser("snapshot", help="snapshot the whole run directory now")
    s.add_argument("run_dir")
    s.add_argument("--label", default="snapshot")
    s.add_argument("--domain", default=None)
    ls = sub.add_parser("list", help="every archived run of a domain, per iteration")
    ls.add_argument("domain")
    r = sub.add_parser("restore", help="copy a snapshot out as a working run directory")
    r.add_argument("snapshot_dir")
    r.add_argument("dest")
    args = ap.parse_args(argv)

    if args.command == "snapshot":
        print(snapshot(Path(args.run_dir), args.label, domain=args.domain))
        return 0
    if args.command == "restore":
        print(restore(Path(args.snapshot_dir), Path(args.dest)))
        return 0
    runs = list_runs(args.domain)
    if not runs:
        print(f"(no archived runs for {args.domain!r} in {Domain(args.domain).runs})")
    for row in runs:
        print(row["archive"])
        for k, it in row["iterations"].items():
            tokens = (it.get("tokens") or {}).get("total_tokens")
            print(
                f"  iteration {k:>3}  {it.get('checkpoint') or '-':<14} "
                f"{len(it.get('snapshots', []))} snapshot(s)"
                + (f"  {tokens:,} tokens" if tokens is not None else "")
            )
    return 0


if __name__ == "__main__":
    from .paths import run_cli

    raise SystemExit(run_cli(main, "archive"))
