#!/usr/bin/env python3
"""PreToolUse guard: the agents may not delete, move, or overwrite a run's saved history.

Saved history is the knowledge base (<project>/.skydiscover/kb/: tests, decisions, wiki, every run's
snapshots and transcripts, every version) and the published checkpoints
(outputs/synthesize/<run>/checkpoints/checkpoint_N/). Wired for Bash and for the file-editing tools:

- a Bash command that deletes or moves (rm, rmdir, unlink, shred, truncate, mv, find -delete,
  git clean, rsync --delete, shutil.rmtree, os.remove / unlink / rename / replace, ...) anything
  in the saved history is refused (exit 2, the reason on stderr). Bytecode (__pycache__, *.pyc),
  temporary files (*.tmp*), and the re-downloadable clone cache (<kb>/.cache/) are exempt;
- a shell redirect (`> file`) onto an existing knowledge-base file first copies that file to
  <kb>/.history/<time>/<path>, then lets the command run; onto a checkpoint it is refused;
- Edit / Write / MultiEdit / NotebookEdit of an existing knowledge-base file first copies it to
  <kb>/.history/<time>/<path>, then lets the edit run, so replacing a wiki page keeps the old
  page; editing a checkpoint, an archived snapshot (<kb>/<domain>/runs/), or a version
  (<kb>/<domain>/versions/) is refused: those are written only by the framework's own tools.

The framework's own tools (python3 -m skydiscover.synthesize.spec.*) are not agents' shell
deletions and are not affected. Any error in the guard lets the tool call through.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple

EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}

_DELETE = re.compile(
    r"(?<![\w./-])(rm|rmdir|unlink|shred|truncate|mv)(?=\s)"
    r"|\bfind\b[^|;&]*\s-(delete|exec\s+(rm|mv))\b"
    r"|\bgit\s+clean\b|\brsync\b[^|;&]*--delete"
    r"|rmtree|os\.remove|os\.unlink|\.unlink\(|os\.rename|os\.replace|shutil\.move|\.rename\("
)
_REDIRECT = re.compile(r"(?<![>&0-9<])>(?![>&])\s*([^\s;&|<>]+)")


def _kb_roots(cwd: Path) -> List[Path]:
    """Every knowledge base root in effect: the project's (as spec.paths resolves it), plus
    $SKYDISCOVER_HOME and the ~/.skydiscover earlier releases used."""
    roots: List[Path] = []
    try:
        spec = importlib.util.spec_from_file_location(
            "_skysynth_clone_guard", Path(__file__).resolve().parent / "clone_reuse_guard.py"
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        here = os.getcwd()
        try:
            os.chdir(cwd)
            roots.append(Path(mod.kb_root()).expanduser().resolve())
        finally:
            os.chdir(here)
    except Exception:
        pass
    env = os.environ.get("SKYDISCOVER_HOME")
    for extra in ([Path(env)] if env else []) + [Path.home() / ".skydiscover"]:
        try:
            roots.append(extra.expanduser().resolve())
        except OSError:
            pass
    return list(dict.fromkeys(roots))


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _exempt(path: Path, root: Optional[Path]) -> bool:
    parts = path.parts
    return (
        "__pycache__" in parts
        or path.suffix in (".pyc", ".pyo")
        or ".tmp" in path.name
        or (root is not None and _under(path, root / ".cache"))
    )


def _is_checkpoint(path: Path) -> bool:
    parts = path.parts
    return any(
        a == "checkpoints" and b.startswith("checkpoint_") for a, b in zip(parts, parts[1:])
    )


def _classify(path: Path, roots: List[Path]) -> Tuple[Optional[str], Optional[Path]]:
    """('kb' | 'archive' | 'checkpoint' | None, the knowledge base root it is under)."""
    for root in roots:
        if _under(path, root):
            rel = path.relative_to(root).parts
            if len(rel) >= 2 and rel[1] in ("runs", "versions"):
                return "archive", root
            return "kb", root
    if _is_checkpoint(path):
        return "checkpoint", None
    return None, None


def _paths_in(command: str, cwd: Path) -> List[Path]:
    """Every word of the command that could name a path, made absolute (following `cd dir`)."""
    try:
        words = shlex.split(command, posix=True)
    except ValueError:
        words = command.split()
    out: List[Path] = []
    base = cwd
    home = str(Path.home())
    for i, w in enumerate(words):
        w = w.replace("${HOME}", home).replace("$HOME", home)
        if w in ("&&", ";", "||", "|"):
            continue
        if i and words[i - 1] == "cd":
            p = Path(w).expanduser()
            base = p if p.is_absolute() else base / p
            continue
        if w.startswith("-") or not w:
            continue
        p = Path(w).expanduser()
        p = p if p.is_absolute() else base / p
        try:
            out.append(Path(os.path.normpath(p)))
        except (OSError, ValueError):
            continue
    return out


def _backup(path: Path, root: Path) -> Optional[Path]:
    """Copy an existing knowledge-base file to <root>/.history/<time>/<its path>."""
    if not path.is_file():
        return None
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    dest = root / ".history" / stamp / path.relative_to(root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(path, dest)
    return dest


def _refuse(what: str) -> str:
    return (
        f"history-guard: {what} is saved history (the knowledge base, a run's snapshots, or a "
        "checkpoint). Agents may not delete, move, or overwrite it. To replace or retire a "
        "knowledge-base page, rewrite it with the Write tool (the previous version is kept under "
        "<kb>/.history/). Bytecode (__pycache__, *.pyc), *.tmp files, and <kb>/.cache/ may be removed."
    )


def decide(payload: dict) -> Optional[str]:
    """The reason to refuse this tool call, or None to let it through (after any backup)."""
    tool = payload.get("tool_name")
    tool_input = payload.get("tool_input") or {}
    cwd = Path(payload.get("cwd") or os.getcwd()).resolve()
    roots = _kb_roots(cwd)
    if tool in EDIT_TOOLS:
        raw = tool_input.get("file_path") or tool_input.get("notebook_path") or ""
        if not raw:
            return None
        target = Path(raw).expanduser()
        target = Path(os.path.normpath(target if target.is_absolute() else cwd / target))
        kind, root = _classify(target, roots)
        if kind in ("archive", "checkpoint"):
            return _refuse(str(target))
        if kind == "kb" and root is not None and not _exempt(target, root):
            _backup(target, root)
        return None
    if tool != "Bash":
        return None
    command = str(tool_input.get("command") or "")
    if not command:
        return None
    paths = _paths_in(command, cwd)
    if _DELETE.search(command):
        hit = []
        for p in paths:
            kind, root = _classify(p, roots)
            if kind and not _exempt(p, root):
                hit.append(p)
        # a path inside a quoted script (python -c "...") is not a separate word: match the text too
        if not hit:
            for root in roots:
                for m in re.finditer(re.escape(str(root)) + r"[^\s'\"]*", command):
                    p = Path(m.group(0))
                    if not _exempt(p, root):
                        hit.append(p)
            for m in re.finditer(r"[^\s'\"]*/checkpoints/checkpoint_[^\s'\"]*", command):
                hit.append(Path(m.group(0)))
        if hit:
            return _refuse(", ".join(str(h) for h in hit[:3]))
    for m in _REDIRECT.finditer(command):
        target = Path(m.group(1)).expanduser()
        target = Path(os.path.normpath(target if target.is_absolute() else cwd / target))
        kind, root = _classify(target, roots)
        if kind in ("archive", "checkpoint"):
            return _refuse(str(target))
        if kind == "kb" and root is not None and not _exempt(target, root):
            _backup(target, root)
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            return 0
        reason = decide(payload)
    except Exception:
        return 0  # a bug in the guard must never block a tool call
    if reason:
        sys.stderr.write(reason + "\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
