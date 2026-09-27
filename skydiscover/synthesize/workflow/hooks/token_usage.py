#!/usr/bin/env python3
"""Token usage and per-iteration history of a run, kept by Claude Code hooks.

Wired as Claude Code's Stop, SubagentStop, PreCompact, and SessionEnd hook. Each firing:

1. re-reads the session's transcript and every subagent transcript beside it and rewrites that
   session's entry in `<run>/token_usage.json`: the run's tokens in total, per session, per role,
   per model, and per iteration (with the roles inside each iteration). A transcript keeps every
   message a compaction summarized, so the count is exact, never a running guess. Messages are
   counted once each, keyed by message id (a streamed message spans several transcript lines);
2. appends one line to `<run>/token_usage.log.jsonl`: the event, the role that finished, the
   iteration in progress, and the run's totals at that moment;
3. snapshots the whole run directory into `<kb>/runs/` (spec/archive.py), labelled with the
   iteration and what just happened (`after-coding-agent`, `turn`, `before-compact`,
   `session-end`), and mirrors the session's transcripts beside the snapshots. An unchanged run
   adds no snapshot.

Iteration K is the work that ends in checkpoint_K; iteration 0 is the specification. A message is
filed under an iteration by its timestamp: after checkpoint_(K-1) and up to checkpoint_K. Messages
after the run's `<slug>.done` marker (later chat in the same session) are not counted.

The work runs detached, so a hook returns at once and never slows the agent; set
SKYDISCOVER_HOOK_SYNC=1 to run it inline. Errors go to `<run>/hook_errors.log`, never to the agent.

    token_usage.py --hook                       read a hook payload on stdin (what hooks.json runs)
    token_usage.py <run_dir> [--transcript PATH]... [--projects-dir DIR] [--until ISO-TIME]
        rebuild the token usage file from transcripts; without --transcript, every Claude Code
        session of the run's project whose transcript names the run directory

The token accounting is stdlib only: the plugin install ships this file without the skydiscover
package. The snapshots need that package (`pip install -e <skydiscover checkout>`); without it the
tokens are still recorded and the missing package is noted in hook_errors.log.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib
import importlib.util
import json
import os
import re
import sys
import traceback
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)
USAGE_FILE = "token_usage.json"
LOG = "token_usage.log.jsonl"
ERRORS = "hook_errors.log"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _zero() -> Dict[str, int]:
    return {**{f: 0 for f in FIELDS}, "total_tokens": 0, "messages": 0}


def _add(into: Dict[str, int], usage: Dict[str, Any]) -> None:
    for f in FIELDS:
        into[f] += int(usage.get(f) or 0)
    into["total_tokens"] = sum(into[f] for f in FIELDS)
    into["messages"] += int(usage.get("messages", 1))


def _parse_time(text: Any) -> Optional[float]:
    if not isinstance(text, str) or not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _meta(transcript: Path) -> Dict[str, Any]:
    meta = _read(transcript.with_suffix(".meta.json"))
    return meta if isinstance(meta, dict) else {}


def _agent_of(transcript: Path) -> str:
    """The role a subagent transcript belongs to, from its sidecar meta.json; `lead` for the main one."""
    if transcript.parent.name != "subagents":
        return "lead"
    return str(_meta(transcript).get("agentType") or "subagent")


def session_transcripts(transcript: Path) -> List[Path]:
    """The main transcript and every subagent transcript Claude Code keeps beside it."""
    out = [transcript]
    sub = transcript.with_suffix("") / "subagents"
    if sub.is_dir():
        out += sorted(sub.glob("*.jsonl"))
    return out


# ------------------------------------------------------------------------------------ iterations


def _published_output(run: Path) -> Optional[Path]:
    try:
        raw = (run / ".output").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    target = Path(raw) if Path(raw).is_absolute() else run.resolve().parent.parent / raw
    return target if target.is_dir() else None


def _checkpoint_times(run: Path) -> List[Tuple[int, float]]:
    """(K, when checkpoint_K was written) for every checkpoint, in order."""
    out = _published_output(run)
    if out is None or not (out / "checkpoints").is_dir():
        return []
    found = []
    for p in (out / "checkpoints").iterdir():
        m = re.fullmatch(r"checkpoint_(\d+)", p.name)
        if not m:
            continue
        score = _read(p / "score.json")
        when = _parse_time(score.get("created_at")) if isinstance(score, dict) else None
        if when is None:
            try:
                when = p.stat().st_mtime
            except OSError:
                continue
        found.append((int(m.group(1)), when))
    return sorted(found)


def _synthesis_start(run: Path) -> Optional[float]:
    """When iteration 1 began: the first archived snapshot filed under an iteration >= 1."""
    try:
        archive = Path((run / ".archive").read_text(encoding="utf-8").strip())
    except OSError:
        return None
    index = _read(archive / "index.json")
    for snap in (index.get("snapshots") if isinstance(index, dict) else None) or []:
        if int(snap.get("iteration") or 0) >= 1:
            return _parse_time(snap.get("created_at"))
    return None


def iteration_of(run: Path) -> Callable[[Optional[float]], int]:
    """A function filing a message's timestamp under its iteration."""
    marks = _checkpoint_times(run)
    start = _synthesis_start(run)
    if start is not None and marks and start > marks[0][1]:
        start = None  # archived only after checkpoint_1 (the hook arrived mid-run): not a boundary
    impl = run / "synthesis" / "impl"
    started = bool(marks) or (impl.is_dir() and any(p.is_file() for p in impl.rglob("*")))
    now = (marks[-1][0] + 1) if marks else (1 if started else 0)

    def assign(when: Optional[float]) -> int:
        if when is None:
            return now  # the iteration in progress
        for k, t in marks:
            if when <= t:
                # before iteration 1 began, the work was the specification
                return 0 if (k == 1 and start is not None and when < start) else k
        if marks:
            return now
        if start is not None:
            return 1 if when >= start else 0
        return now  # no record of when iteration 1 began: everything so far is filed under now

    return assign


def current_iteration(run: Path) -> int:
    return iteration_of(run)(None)


# ------------------------------------------------------------------------------------ counting


def usage(
    transcripts: Iterable[Path],
    until: Optional[float] = None,
    assign: Optional[Callable[[Optional[float]], int]] = None,
) -> Dict[str, Any]:
    """Sum every assistant message's usage once. `until`: ignore messages stamped after it.
    `assign`: file each message under an iteration by its timestamp."""
    seen: Dict[str, Dict[str, Any]] = {}
    for path in transcripts:
        agent = _agent_of(path)
        try:
            fh = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                if '"usage"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                msg = row.get("message") if isinstance(row, dict) else None
                if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
                    continue
                model = msg.get("model") or "unknown"
                if model == "<synthetic>":
                    continue  # a harness placeholder, no API call behind it
                when = _parse_time(row.get("timestamp"))
                if until is not None and when is not None and when > until:
                    continue
                key = msg.get("id") or row.get("requestId") or row.get("uuid") or f"{path}:{len(seen)}"
                u = msg["usage"]
                entry = seen.setdefault(
                    key,
                    {"model": model, "agent": agent, "when": when, **{f: 0 for f in FIELDS}},
                )
                # A streamed message repeats its usage on each line; the largest value is the final one.
                for f in FIELDS:
                    entry[f] = max(entry[f], int(u.get(f) or 0))
                if row.get("isSidechain") and agent == "lead":
                    entry["agent"] = "subagent"
    total, by_model, by_agent, by_iteration = _zero(), {}, {}, {}
    for entry in seen.values():
        _add(total, entry)
        _add(by_model.setdefault(entry["model"], _zero()), entry)
        _add(by_agent.setdefault(entry["agent"], _zero()), entry)
        if assign is not None:
            it = by_iteration.setdefault(str(assign(entry["when"])), {**_zero(), "by_agent": {}})
            _add(it, entry)
            _add(it["by_agent"].setdefault(entry["agent"], _zero()), entry)
    out = {"total": total, "by_model": by_model, "by_agent": by_agent}
    if assign is not None:
        out["by_iteration"] = dict(sorted(by_iteration.items(), key=lambda kv: int(kv[0])))
    return out


# ------------------------------------------------------------------------------------ the usage file


def _write(path: Path, doc: Dict[str, Any]) -> None:
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex[:8]}")
    tmp.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _done_time(run: Path) -> Optional[float]:
    marker = run.parent / f"{run.name}.done"
    try:
        return marker.stat().st_mtime
    except OSError:
        return None


def record(
    run: Path,
    session_id: str,
    transcript: Path,
    event: str = "",
    until: Optional[float] = None,
    role: Optional[str] = None,
) -> Dict[str, Any]:
    """Recount one session from its transcripts and rewrite its entry and the run's totals."""
    assign = iteration_of(run)
    counted = usage(session_transcripts(transcript), until=until or _done_time(run), assign=assign)
    with open(run / f".{USAGE_FILE}.lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            doc = _read(run / USAGE_FILE)
            doc = doc if isinstance(doc, dict) else {}
            sessions = doc.get("sessions") if isinstance(doc.get("sessions"), dict) else {}
            sessions[session_id] = {
                "transcript": str(transcript),
                "updated_at": _now(),
                **counted,
            }
            total, by_model, by_agent, by_iteration = _zero(), {}, {}, {}
            for s in sessions.values():
                _add(total, s.get("total") or {"messages": 0})
                for name, u in (s.get("by_model") or {}).items():
                    _add(by_model.setdefault(name, _zero()), u)
                for name, u in (s.get("by_agent") or {}).items():
                    _add(by_agent.setdefault(name, _zero()), u)
                for k, u in (s.get("by_iteration") or {}).items():
                    it = by_iteration.setdefault(k, {**_zero(), "by_agent": {}})
                    _add(it, u)
                    for name, a in (u.get("by_agent") or {}).items():
                        _add(it["by_agent"].setdefault(name, _zero()), a)
            iteration = assign(None)
            doc = {
                "run": run.name,
                "updated_at": _now(),
                "iteration": iteration,
                "total": total,
                "by_iteration": dict(sorted(by_iteration.items(), key=lambda kv: int(kv[0]))),
                "by_model": by_model,
                "by_agent": by_agent,
                "sessions": sessions,
            }
            _write(run / USAGE_FILE, doc)
            this = by_iteration.get(str(iteration)) or {}
            with open(run / LOG, "a", encoding="utf-8") as log:
                log.write(
                    json.dumps(
                        {
                            "time": doc["updated_at"],
                            "event": event,
                            "role": role,
                            "session": session_id,
                            "iteration": iteration,
                            "iteration_total_tokens": this.get("total_tokens", 0),
                            "session_total_tokens": counted["total"]["total_tokens"],
                            **total,
                        }
                    )
                    + "\n"
                )
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)
    return doc


# ------------------------------------------------------------------------------------ snapshots


def _load_archive():
    """spec/archive.py from the installed skydiscover package, or from the source checkout this
    hook sits in. Loaded as a package of its own so skydiscover/__init__.py (and its heavier
    dependencies) is never imported."""
    candidates = []
    found = importlib.util.find_spec("skydiscover")
    for loc in (found.submodule_search_locations or []) if found else []:
        candidates.append(Path(loc) / "synthesize" / "spec")
    candidates.append(Path(__file__).resolve().parent.parent.parent / "spec")
    for spec_dir in candidates:
        if (spec_dir / "archive.py").is_file() and (spec_dir / "__init__.py").is_file():
            name = "_skysynth_spec"
            if name not in sys.modules:
                pkg = importlib.util.spec_from_file_location(
                    name, spec_dir / "__init__.py", submodule_search_locations=[str(spec_dir)]
                )
                module = importlib.util.module_from_spec(pkg)
                sys.modules[name] = module
                pkg.loader.exec_module(module)
            return importlib.import_module(f"{name}.archive")
    return None


def _subagent_info(payload: Dict[str, Any], transcript: Path) -> Tuple[str, str]:
    """(role, description) of the subagent a SubagentStop names."""
    meta: Dict[str, Any] = {}
    agent_path = payload.get("agent_transcript_path")
    if agent_path:
        meta = _meta(Path(agent_path))
    if not meta and payload.get("agent_id"):
        meta = _meta(transcript.with_suffix("") / "subagents" / f"agent-{payload['agent_id']}.jsonl")
    if not meta:
        subs = sorted(
            (transcript.with_suffix("") / "subagents").glob("*.meta.json"),
            key=lambda p: p.stat().st_mtime,
        )
        meta = _read(subs[-1]) if subs else {}
    role = str(payload.get("agent_type") or meta.get("agentType") or "subagent")
    return role, str(meta.get("description") or "")


_LABELS = {"Stop": "turn", "PreCompact": "before-compact", "SessionEnd": "session-end"}


def work(run: Path, session: str, transcript: Path, payload: Dict[str, Any]) -> None:
    event = str(payload.get("hook_event_name") or "")
    role = description = None
    if event == "SubagentStop":
        role, description = _subagent_info(payload, transcript)
    record(run, session, transcript, event, role=role)
    archive = _load_archive()
    if archive is None:
        raise RuntimeError(
            "no skydiscover package found; tokens were recorded but the run was not snapshotted "
            "(pip install -e <skydiscover checkout>)"
        )
    label = f"after-{role.split(':')[-1]}" if role else _LABELS.get(event, event.lower() or "hook")
    detail = " ".join(x for x in (role, description, payload.get("trigger")) if x) or None
    archive.snapshot(
        run,
        label,
        iteration=current_iteration(run),
        trigger=event or "hook",
        detail=detail,
        transcript=transcript,
        session_id=session,
    )


# ------------------------------------------------------------------------------------ finding the run


def _runs_root(cwd: Path) -> Path:
    raw = os.environ.get("SKYDISCOVER_RUNS")
    if not raw:
        config = Path(__file__).resolve().parent.parent.parent / "config.toml"
        try:
            m = re.search(r'^runs\s*=\s*"([^"]+)"', config.read_text(encoding="utf-8"), re.M)
            raw = m.group(1) if m else None
        except OSError:
            raw = None
    root = Path(raw or ".skydiscover").expanduser()
    return root if root.is_absolute() else cwd / root


def find_run(cwd: Path, transcript: Optional[Path]) -> Optional[Path]:
    """The run this session works on: $SKYDISCOVER_RUN, else the run under the project's runs folder
    that the transcript names (the most recently touched one when it names several or none)."""
    env = os.environ.get("SKYDISCOVER_RUN")
    if env and (Path(env) / "task.md").is_file():
        return Path(env)
    root = _runs_root(cwd)
    if not root.is_dir():
        return None
    runs = [d for d in root.iterdir() if d.is_dir() and (d / "task.md").is_file()]
    if len(runs) <= 1:
        return runs[0] if runs else None
    text = ""
    if transcript is not None:
        try:
            text = transcript.read_text(encoding="utf-8", errors="replace")
        except OSError:
            text = ""
    named = [d for d in runs if f"{root.name}/{d.name}" in text]
    return max(named or runs, key=lambda d: d.stat().st_mtime)


def _projects_dir() -> Path:
    base = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return base / "projects"


def project_transcripts(project: Path, run: Path, projects_dir: Optional[Path]) -> List[Path]:
    """Every main transcript of the project's Claude Code sessions that names the run directory."""
    folder = (projects_dir or _projects_dir()) / re.sub(r"[^A-Za-z0-9]", "-", str(project))
    needle = f"{run.parent.name}/{run.name}"
    out = []
    for t in sorted(folder.glob("*.jsonl")):
        try:
            if needle in t.read_text(encoding="utf-8", errors="replace"):
                out.append(t)
        except OSError:
            continue
    return out


def _log_error(run: Path, what: str) -> None:
    try:
        with open(run / ERRORS, "a", encoding="utf-8") as f:
            f.write(f"[{_now()}] {what}\n")
    except OSError:
        pass


def _detached(fn: Callable[[], None]) -> None:
    """Run fn in a detached grandchild so the hook returns at once; inline under
    SKYDISCOVER_HOOK_SYNC=1 or where fork is unavailable."""
    if os.environ.get("SKYDISCOVER_HOOK_SYNC") == "1" or not hasattr(os, "fork"):
        fn()
        return
    pid = os.fork()
    if pid:
        os.waitpid(pid, 0)  # the child exits as soon as it has forked the worker
        return
    try:
        os.setsid()
        if os.fork():
            os._exit(0)
        devnull = os.open(os.devnull, os.O_RDWR)
        for fd in (0, 1, 2):  # release the hook's pipes so Claude Code is not kept waiting
            os.dup2(devnull, fd)
        fn()
    finally:
        os._exit(0)


def _hook() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    if not isinstance(payload, dict):
        return 0
    transcript = Path(payload.get("transcript_path") or "")
    session = str(payload.get("session_id") or transcript.stem)
    if not transcript.is_file() or not session:
        return 0
    run = find_run(Path(payload.get("cwd") or os.getcwd()), transcript)
    if run is None:
        return 0

    def job() -> None:
        try:
            work(run, session, transcript, payload)
        except Exception:
            _log_error(run, traceback.format_exc().strip())

    _detached(job)
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="token_usage", description=__doc__.split("\n\n")[0])
    ap.add_argument("run_dir", nargs="?")
    ap.add_argument("--hook", action="store_true", help="read a Claude Code hook payload on stdin")
    ap.add_argument("--transcript", action="append", default=[], help="a session's main .jsonl")
    ap.add_argument("--projects-dir", default=None, help="default: ~/.claude/projects")
    ap.add_argument(
        "--until",
        default=None,
        help="ISO time; ignore messages after it (default: when <slug>.done was written, if it was)",
    )
    args = ap.parse_args(argv)
    if args.hook:
        try:
            return _hook()
        except Exception as e:  # never block the agent over bookkeeping
            print(f"token_usage: {e}", file=sys.stderr)
            return 0
    if not args.run_dir:
        ap.error("give a run directory, or --hook")
    run = Path(args.run_dir).resolve()
    if not (run / "task.md").is_file():
        print(f"token_usage: {run} is not a run directory (no task.md)", file=sys.stderr)
        return 2
    transcripts = [Path(t) for t in args.transcript] or project_transcripts(
        run.parent.parent, run, Path(args.projects_dir) if args.projects_dir else None
    )
    if not transcripts:
        print(f"token_usage: no Claude Code transcript names {run}", file=sys.stderr)
        return 1
    until = _parse_time(args.until)
    if args.until and until is None:
        ap.error(f"--until {args.until!r} is not an ISO time")
    doc: Dict[str, Any] = {}
    for t in transcripts:
        doc = record(run, t.stem, t, "rebuild", until=until)
    for sid, s in doc["sessions"].items():
        print(f"  {sid}  {s['total']['total_tokens']:>14,} tokens  ({s['total']['messages']} messages)")
    for k, it in doc["by_iteration"].items():
        roles = ", ".join(
            f"{n.split(':')[-1]} {u['total_tokens']:,}"
            for n, u in sorted(it["by_agent"].items(), key=lambda kv: -kv[1]["total_tokens"])
        )
        print(f"  iteration {k:>3}: {it['total_tokens']:>14,} tokens  ({roles})")
    t = doc["total"]
    print(
        f"total: {t['total_tokens']:,} tokens ({t['input_tokens']:,} input, "
        f"{t['output_tokens']:,} output, {t['cache_read_input_tokens']:,} cache read, "
        f"{t['cache_creation_input_tokens']:,} cache write) -> {run / USAGE_FILE}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
