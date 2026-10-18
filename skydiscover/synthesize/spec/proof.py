"""Frozen auditor sessions and the persistent Verus proof loop (stdlib only).

Commands: start, status, tokens, check, replay, feedback, finish-test.
The agents write proof/overlay.json and proof/plan.md; this module owns evidence and accounting.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .paths import Run, front_matter
from .proof_source import (
    SourceError,
    apply_overlay,
    base_source,
    macro_functions,
    spec_attachment,
    tokens,
    verification_view,
)


class ProofError(ValueError):
    pass


def _read(path: Path) -> dict:
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProofError(f"cannot read {path.name}: {exc}") from exc
    if not isinstance(doc, dict):
        raise ProofError(f"{path.name} must contain an object")
    return doc


def _write(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _hash(doc) -> str:
    return hashlib.sha256(
        json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _digest(path: Path) -> str:
    from .checkpoint import _tree_digest

    return _tree_digest(path)


def _relative(root: Path, value: str) -> Path:
    if (
        not isinstance(value, str)
        or not value
        or Path(value).is_absolute()
        or ".." in Path(value).parts
    ):
        raise ProofError("paths must be nonempty relative paths inside the frozen inputs")
    path = root / value
    if path.is_symlink() or not path.resolve().is_relative_to(root.resolve()):
        raise ProofError("paths may not escape the frozen inputs")
    return path


@contextmanager
def _lock(run: Run):
    run.audit.mkdir(parents=True, exist_ok=True)
    with (run.audit / ".sessions.lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def route(run_dir: Path) -> dict:
    run = Run(run_dir)
    task = front_matter(run.task)
    requested = task.get("audit_mode", "test")
    if requested not in {"test", "proof"}:
        raise ProofError("audit_mode must be test or proof")
    config = (
        _read(run.verus_config)
        if not task.get("audit_target") and run.verus_config.exists()
        else {}
    )
    named = task.get("audit_target") or config.get("entry")
    entry = _relative(run.impl, named) if named else run.entry_impl()
    if entry is None or not entry.exists():
        raise ProofError("no implementation target")
    if not entry.resolve().is_relative_to(run.impl.resolve()):
        raise ProofError("audit target must be inside synthesis/impl")
    if entry.is_dir():
        languages = {p.suffix for p in entry.rglob("*") if p.is_file()}
        rust = bool(languages & {".rs"})
        cpp = bool(languages & {".cc", ".cpp", ".cxx", ".c++"})
        if rust and cpp:
            raise ProofError("mixed-language implementation: set audit_target explicitly")
        language = "rust" if rust else "cpp" if cpp else "other"
        if language == "rust" and requested == "proof":
            roots = [entry / name for name in ("lib.rs", "main.rs", "src/lib.rs", "src/main.rs")]
            roots = [p for p in roots if p.is_file()]
            if len(roots) != 1:
                raise ProofError(
                    "Rust proof mode needs an explicit audit_target or verus.json entry"
                )
            entry = roots[0]
    else:
        language = (
            "rust"
            if entry.suffix == ".rs"
            else (
                "cpp"
                if entry.suffix
                in {
                    ".cc",
                    ".cpp",
                    ".cxx",
                    ".c++",
                    ".h",
                    ".hpp",
                }
                else "other"
            )
        )
    if requested == "proof" and language == "other":
        raise ProofError("proof auditing supports Rust; only C++ has an automatic test fallback")
    effective = "proof" if language == "rust" and requested == "proof" else "test"
    return {
        "requested": requested,
        "effective": effective,
        "language": language,
        "entry": entry.relative_to(run.impl).as_posix(),
        "reason": (
            "Verus applies only to Rust; use tests for C++"
            if language == "cpp" and requested == "proof"
            else f"{effective} auditing selected"
        ),
    }


def _bindings(run: Run) -> dict:
    # Exclude discovery clones, but freeze all contract/configuration and interface inputs.
    from .checkpoint import _tree_digest

    return {
        "task": _digest(run.task),
        "impl": _digest(run.impl),
        "specification": _tree_digest(run.specification, ignored=("sources", "profiling")),
        "interface": _digest(run.interface),
    }


def _copy(source: Path, target: Path) -> None:
    if source.is_dir():
        ignored = [".git", "__pycache__", "*.pyc"]
        if target.name == "specification":
            ignored.extend(["sources", "profiling"])
        shutil.copytree(source, target, ignore=shutil.ignore_patterns(*ignored))
    elif source.is_file():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    else:
        target.mkdir(parents=True, exist_ok=True)


def _readonly(root: Path) -> None:
    for path in root.rglob("*"):
        if path.is_file():
            path.chmod(0o444)
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o555)
    root.chmod(0o555)


def _frozen_bindings(session: Path) -> dict:
    return {
        name: _digest(session / "inputs" / ("task.md" if name == "task" else name))
        for name in ("task", "impl", "specification", "interface")
    }


def _config(run: Run, routing: dict) -> dict:
    cfg = _read(run.verus_config)
    allowed = {
        "entry",
        "spec",
        "obligations",
        "max_attempts",
        "attempts_per_review",
        "max_stalled",
        "timeout_secs",
        "rlimit",
        "macro_imports",
    }
    if set(cfg) - allowed:
        raise ProofError(f"unknown Verus configuration keys: {sorted(set(cfg) - allowed)}")
    spec = _relative(run.specification, cfg.get("spec", ""))
    if not spec.is_file() or spec.suffix != ".rs":
        raise ProofError("proof mode requires a Verus .rs specification file")
    imports = cfg.get("macro_imports", {})
    if not isinstance(imports, dict):
        raise ProofError("macro_imports must be an object")
    spec_attachment("spec.rs", imports)  # validate before freezing configuration
    obs = cfg.get("obligations")
    if not isinstance(obs, list) or not obs:
        raise ProofError("proof mode requires a nonempty obligations list")
    ids = set()
    for o in obs:
        if not isinstance(o, dict) or set(o) - {
            "id",
            "function",
            "file",
            "requires",
            "ensures",
            "macro",
        }:
            raise ProofError("invalid obligation fields")
        required = ("id", "function", "macro") if "macro" in o else ("id", "function", "ensures")
        if not all(isinstance(o.get(k), str) and o[k].strip() for k in required):
            raise ProofError("obligations require id, function and ensures (or a frozen macro)")
        if "macro" in o:
            if not re.fullmatch(r"[A-Za-z_]\w*", o["macro"]) or {"requires", "ensures"} & set(o):
                raise ProofError("a macro obligation uses the frozen macro's complete contract")
            spec_tokens = [t.text for t in tokens(spec.read_text())]
            definition = ["#", "[", "macro_export", "]", "macro_rules", "!", o["macro"]]
            if not any(
                spec_tokens[j : j + len(definition)] == definition for j in range(len(spec_tokens))
            ):
                raise ProofError("contract macro must be exported by the frozen specification")
        if o["id"] in ids:
            raise ProofError("duplicate obligation id")
        if "file" in o:
            _relative(run.impl, o["file"])
        ids.add(o["id"])
    cfg = {**cfg, "entry": routing["entry"]}
    if "max_attempts" in cfg and "attempts_per_review" in cfg:
        raise ProofError("use attempts_per_review or its legacy alias max_attempts, not both")
    cfg["attempts_per_review"] = cfg.pop("max_attempts", cfg.get("attempts_per_review", 10))
    for name, default, maximum in (
        ("attempts_per_review", 10, 1000),
        ("max_stalled", 3, 100),
        ("timeout_secs", 120, 3600),
        ("rlimit", 10, 600),
    ):
        value = cfg.get(name, default)
        if type(value) is not int or not 1 <= value <= maximum:
            raise ProofError(f"{name} must be an integer from 1 to {maximum}")
        cfg[name] = value
    return cfg


def _execute(command: list[str], cwd: Path, timeout: int) -> dict:
    # Remove ambient verifier/compiler switches; the runner supplies the entire argument list.
    env = {
        k: v
        for k, v in os.environ.items()
        if k == "VERUS_Z3_PATH"
        or not k.startswith(("VERUS_", "RUSTFLAGS", "CARGO_ENCODED_RUSTFLAGS"))
    }
    started = time.monotonic()
    with subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    ) as proc:
        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            stdout, stderr = proc.communicate()
    return {
        "command": command,
        "returncode": proc.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "timed_out": timed_out,
        "seconds": time.monotonic() - started,
    }


def _tool() -> tuple[str, dict]:
    binary = shutil.which(os.environ.get("SKYDISCOVER_VERUS", "verus"))
    if not binary:
        raise ProofError("Verus is unavailable; set SKYDISCOVER_VERUS to the installed executable")
    binary = str(Path(binary).resolve())
    version = _execute([binary, "--version", "--output-json"], Path(binary).parent, 20)
    try:
        meta = json.loads(version["stdout"])["verus"]
        if version["returncode"] or not meta.get("version"):
            raise ValueError("missing version")
    except (ValueError, KeyError, TypeError) as exc:
        raise ProofError("cannot identify the Verus toolchain") from exc
    root = Path(binary).parent
    solver = os.environ.get("VERUS_Z3_PATH") or (
        str(root / "z3") if (root / "z3").is_file() else shutil.which("z3")
    )
    if not solver or not Path(solver).is_file():
        raise ProofError("Z3 is unavailable; install it or set VERUS_Z3_PATH")
    # Bind the launcher, verifier, libraries, and vstd specifications used by this installation.
    files = {
        Path(binary),
        *(
            root / n
            for n in ("rust_verify", "vstd.vir", "z3", "cargo-verus")
            if (root / n).is_file()
        ),
    }
    for pattern in ("*.rlib", "*.dylib", "*.so"):
        files.update(root.glob(pattern))
    identity = {
        "version": meta,
        "files": {p.name: _digest(p) for p in sorted(files)},
        "solver": _digest(Path(solver).resolve()),
    }
    return binary, identity


def start(run_dir: Path) -> dict:
    run = Run(run_dir)
    with _lock(run):
        routing = route(run.path)
        bindings = _bindings(run)
        identity = {"schema": 2, "routing": routing, "bindings": bindings}
        sid = _hash(identity)[:24]
        session = run.audit / sid
        if session.exists():
            _write(run.audit_session, {"session": sid})
            existing, manifest = _session(run)
            _integrity(run, existing, manifest)
            state = _read(existing / "state.json")
            # Installing a missing verifier can unblock preflight, but never reset search attempts
            # or replace a toolchain already bound to verification evidence.
            if (
                state["status"] == "blocked"
                and state["attempts"] == 0
                and manifest["toolchain"] is None
            ):
                try:
                    manifest["config"] = _config(run, routing)
                    _, manifest["toolchain"] = _tool()
                except (ProofError, SourceError, OSError):
                    return status(run.path)
                manifest["manifest_hash"] = _hash(
                    {k: v for k, v in manifest.items() if k != "manifest_hash"}
                )
                _write(existing / "manifest.json", manifest)
                (existing / "manifest.json").chmod(0o444)
                state.update(status="ready", message="proof preflight recovered")
                _write(existing / "state.json", state)
                _result(existing, manifest, state)
            return status(run.path)
        # Build off-pointer so a failed snapshot cannot poison a resumable session identifier.
        session = Path(tempfile.mkdtemp(prefix=".freezing-", dir=run.audit))
        manifest = {**identity, "session": sid, "config": {}, "toolchain": None}
        for name, source in (
            ("task", run.task),
            ("impl", run.impl),
            ("specification", run.specification),
            ("interface", run.interface),
        ):
            _copy(source, session / "inputs" / ("task.md" if name == "task" else name))
        if _frozen_bindings(session) != bindings or _bindings(run) != bindings:
            raise ProofError("audit inputs changed while freezing them; retry with stable inputs")
        _readonly(session / "inputs")
        state = {
            "status": "ready",
            "attempts": 0,
            "stalled": 0,
            "best_verified": 0,
            "accepted_attempt": None,
            "message": routing["reason"],
        }
        if routing["effective"] == "proof":
            try:
                manifest["config"] = _config(run, routing)
                _, manifest["toolchain"] = _tool()
            except (ProofError, SourceError, OSError) as exc:
                state.update(status="blocked", message=str(exc))
        manifest["manifest_hash"] = _hash(manifest)
        _write(session / "manifest.json", manifest)
        (session / "manifest.json").chmod(0o444)
        _write(session / "state.json", state)
        _write(session / "proof" / "overlay.json", {"insertions": []})
        (session / "proof" / "plan.md").write_text(
            "# Proof plan\n\n## Strategy\n\n## Next brief\n\n## Attempts and decisions\n",
            encoding="utf-8",
        )
        _result(session, manifest, state)
        session.rename(run.audit / sid)
        _write(run.audit_session, {"session": sid})
        return status(run.path)


def _session(run: Run) -> tuple[Path, dict]:
    sid = _read(run.audit_session).get("session")
    if not isinstance(sid, str) or not re.fullmatch(r"[a-f0-9]{24}", sid):
        raise ProofError("invalid audit session pointer")
    session = run.audit / sid
    manifest = _read(session / "manifest.json")
    raw = {k: v for k, v in manifest.items() if k != "manifest_hash"}
    if not all(k in manifest for k in ("schema", "routing", "bindings", "config", "toolchain")):
        raise ProofError("incomplete audit manifest")
    identity = {k: manifest[k] for k in ("schema", "routing", "bindings")}
    if _hash(raw) != manifest.get("manifest_hash") or _hash(identity)[:24] != sid:
        raise ProofError("audit manifest integrity failure")
    return session, manifest


def _integrity(run: Run, session: Path, manifest: dict) -> None:
    if _frozen_bindings(session) != manifest["bindings"]:
        raise ProofError("frozen audit inputs were modified")
    if _bindings(run) != manifest["bindings"] or route(run.path) != manifest["routing"]:
        raise ProofError(
            "audit is stale: specification, implementation or audit configuration changed"
        )
    if manifest["routing"]["effective"] == "proof":
        frozen_config = dict(manifest["config"])
        if "max_attempts" in frozen_config:
            frozen_config["attempts_per_review"] = frozen_config.pop("max_attempts")
        if frozen_config and _config(run, manifest["routing"]) != frozen_config:
            raise ProofError("proof configuration changed")


def _views(session: Path, manifest: dict, overlay: dict) -> tuple[dict, list[str]]:
    """Prepare every frozen Rust file, collecting adapter issues without rejecting the task."""
    cfg = manifest["config"]
    root = session / "inputs" / "impl"
    sources = overlay.get("sources", {})
    source_mode = set(overlay) == {"sources"} and isinstance(sources, dict)
    if not source_mode and (
        set(overlay) != {"insertions"} or not isinstance(overlay["insertions"], list)
    ):
        raise SourceError("overlay must contain only a sources object or an insertions list")
    for name, source in sources.items():
        path = _relative(root, name)
        if (not path.is_file() or path.suffix != ".rs" or not isinstance(source, str)
                or (path.name == "build.rs" and name != cfg["entry"])):
            raise SourceError("proof sources must name frozen Rust verification files")
    by_file = {}
    for insertion in overlay.get("insertions", []):
        if not isinstance(insertion, dict):
            raise SourceError("invalid proof insertion")
        name = insertion.get("file", cfg["entry"])
        path = _relative(root, name)
        if not path.is_file() or path.suffix != ".rs":
            raise SourceError("proof insertions must name a frozen Rust file")
        by_file.setdefault(name, []).append({k: v for k, v in insertion.items() if k != "file"})
    views, issues = {}, []
    for path in sorted(root.rglob("*.rs")):
        name = path.relative_to(root).as_posix()
        source = path.read_text()
        # Build scripts execute as ordinary Rust before verification; they are frozen inputs,
        # not proof targets unless explicitly selected.
        if (
            path.name == "build.rs"
            and (path.parent / "Cargo.toml").is_file()
            and name != cfg["entry"]
        ):
            views[name] = source
            continue
        obligations = [o for o in cfg["obligations"] if o.get("file", cfg["entry"]) == name]
        try:
            base = base_source(source, [o for o in obligations if "macro" not in o])
            for obligation in obligations:
                if "macro" in obligation:
                    sites = macro_functions(base, obligation["macro"])
                    if obligation["function"] not in sites:
                        raise SourceError(
                            "cannot bind frozen macro contract: " + obligation["function"]
                        )
        except SourceError as exc:
            # Still run the real verifier on preserved source; never pretend an unattached
            # contract was checked. The proof planner gets both adapter and Verus diagnostics.
            issues.append(f"{name}: {exc}")
            try:
                base = verification_view(source)
            except SourceError:
                base = source
        if name in by_file:
            base = apply_overlay(
                base,
                {"insertions": by_file[name]},
                contract_macros=tuple({o["macro"] for o in obligations if "macro" in o}),
                protected_functions=tuple(o["function"] for o in obligations),
            )
        views[name] = base
    if sources:
        from .proof_boundary import apply_source

        # Protect model dependencies across file boundaries as well as direct contracts.
        roots = set()
        for spec_file in (session / "inputs" / "specification").rglob("*.rs"):
            roots.update(t.text.removeprefix("r#") for t in tokens(spec_file.read_text()))
        context = list(views.values())
        for name, source in sources.items():
            views[name] = apply_source(
                views[name], source, roots=roots, context=context,
                protected=tuple(o["function"] for o in cfg["obligations"]),
                macros=tuple({o["macro"] for o in cfg["obligations"] if "macro" in o}),
            )
    for obligation in cfg["obligations"]:
        if obligation.get("file", cfg["entry"]) not in views:
            issues.append(f"obligation {obligation['id']}: source file is absent")
    return views, issues


def _base(session: Path, manifest: dict) -> str:
    views, _ = _views(session, manifest, {"insertions": []})
    return views[manifest["config"]["entry"]]


def _source_dependencies(root: Path) -> list[str]:
    """An attempt may load arbitrary source, but acceptance must bind those inputs too."""
    issues = []
    for path in root.rglob("*.rs"):
        try:
            ts = tokens(path.read_text())
        except SourceError:
            continue  # the real parser reports malformed input
        for i, token in enumerate(ts):
            name = token.text.removeprefix("r#")
            arg = None
            if (
                name in {"include", "include_str", "include_bytes"}
                and i + 3 < len(ts)
                and ts[i + 1].text == "!"
            ):
                arg = ts[i + 3].text
            elif (
                name == "path"
                and i >= 2
                and [t.text for t in ts[i - 2 : i]] == ["#", "["]
                and i + 2 < len(ts)
            ):
                arg = ts[i + 2].text
            if arg is None:
                continue
            try:
                literal = json.loads(arg)
                if not isinstance(literal, str):
                    raise ValueError("not a literal path")
                loaded = (path.parent / literal).resolve()
                if not loaded.is_relative_to(root.resolve()):
                    issues.append(
                        f"{path.relative_to(root)}: source dependency is outside frozen inputs: {literal}"
                    )
            except ValueError:
                issues.append(
                    f"{path.relative_to(root)}: source dependency needs a frozen path mapping"
                )
    return issues


def _result(session: Path, manifest: dict, state: dict) -> dict:
    verified = state["status"] == "verified"
    obligations = [
        {
            "id": o["id"],
            "function": o["function"],
            "status": "verified" if verified else "unresolved",
        }
        for o in manifest["config"].get("obligations", [])
    ]
    result = {
        "session": manifest["session"],
        "routing": manifest["routing"],
        "bindings": manifest["bindings"],
        **state,
        "obligations": obligations,
    }
    _write(session / "result.json", result)
    _write(session / "proof" / "obligations.json", {"obligations": obligations})
    return result


def _completed(run: Run, manifest: dict, status: str, **details) -> None:
    """Only accepted evidence or a diagnosed implementation blocker ends this audit."""
    from .iterations import finished

    _write(
        run.audit / "completed.json",
        {
            "session": manifest["session"],
            "bindings": manifest["bindings"],
            "status": status,
            "finished_iterations": len(finished(run.path)),
            **details,
        },
    )


def completion_allows_planning(run_dir: Path, record: dict) -> bool:
    """Validate scheduling completion, including historical implementation handoffs.

    Historical bindings need not match today's candidate. A blocker must still match its
    saved feedback and diagnostic; old generic handoffs and failed checks never qualify.
    This is scheduling evidence, not a proof or a semantic judgment of the proposed redesign.
    """
    if not isinstance(record, dict):
        return False
    if not isinstance(record.get("bindings"), dict) or not isinstance(record.get("session"), str):
        return False
    if record.get("status") in {"verified", "test_complete"}:
        return True
    if record.get("status") != "implementation_blocked":
        return False
    try:
        session = _relative(Run(run_dir).audit, record["session"])
        doc = _read(session / "feedback.json")
        blocker = doc["blocker"]
        return (
            doc["classification"] == "implementation_blocker"
            and doc["owner"] == "planner"
            and doc["session"] == record["session"]
            and doc["bindings"] == record["bindings"]
            and _hash(doc) == record["feedback_hash"]
            and _digest(_relative(session, blocker["diagnostic"])) == blocker["diagnostic_hash"]
        )
    except (ProofError, OSError, KeyError, TypeError):
        return False


def status(run_dir: Path) -> dict:
    session, manifest = _session(Run(run_dir))
    state = _read(session / "state.json")
    # No active checker holds the lock when status is called by the CLI. A running record is a
    # resumable, spent attempt after interruption, not permission to reset the counter.
    return {
        **_read(session / "result.json"),
        **state,
        "directory": str(session),
        "session": manifest["session"],
    }


def _verification_documents(output: str) -> list[dict]:
    decoder = json.JSONDecoder()
    documents, offset = [], 0
    while offset < len(output):
        opening = output.find("{", offset)
        if opening < 0:
            break
        try:
            doc, consumed = decoder.raw_decode(output[opening:])
        except ValueError:
            offset = opening + 1
            continue
        if isinstance(doc, dict) and "verification-results" in doc:
            documents.append(doc)
        offset = opening + consumed
    return documents


def _command(binary: str, project: Path, entry: Path, cfg: dict, destination: Path):
    flags = ["--output-json", "--no-cheating", "--rlimit", str(cfg["rlimit"])]
    direct = [
        binary,
        str(entry.relative_to(project)),
        "--crate-type",
        "lib",
        "--crate-name",
        "audit_target",
        *flags,
    ]
    manifests = [
        p / "Cargo.toml"
        for p in [entry.parent, *entry.parents]
        if p.is_relative_to(project) and (p / "Cargo.toml").is_file()
    ]
    if not manifests:
        return direct, "audit_target", []
    cargo_verus = Path(binary).parent / "cargo-verus"
    if not cargo_verus.is_file():
        return (
            direct,
            "audit_target",
            ["cargo-verus is unavailable; attempted direct Verus instead"],
        )
    manifest_path = manifests[0]
    # Only verification metadata is generated. Dependency/build declarations and lockfile are
    # preserved; Cargo resolves the candidate's actual dependencies, features and build scripts.
    for package_manifest in sorted(project.rglob("Cargo.toml")):
        text = package_manifest.read_text()
        if not re.search(r"(?m)^\[package\]\s*$", text):
            continue
        section = re.search(r"(?m)^\[package\.metadata\.verus\]\s*$", text)
        if section:
            tail = text[section.end() :]
            end = re.search(r"(?m)^\[", tail)
            length = end.start() if end else len(tail)
            body = tail[:length]
            if re.search(r"(?m)^verify\s*=", body):
                body = re.sub(r"(?m)^verify\s*=.*$", "verify = true", body)
            else:
                body = "\nverify = true\n" + body
            text = text[: section.end()] + body + tail[length:]
        else:
            text += "\n[package.metadata.verus]\nverify = true\n"
        package_manifest.write_text(text)
    cargo = shutil.which("cargo") or "cargo"
    metadata = _execute(
        [
            cargo,
            "metadata",
            "--no-deps",
            "--offline",
            "--locked",
            "--format-version",
            "1",
            "--manifest-path",
            str(manifest_path),
        ],
        project,
        cfg["timeout_secs"],
    )
    _write(destination / "cargo-metadata.json", metadata)
    prefix, issues = "audit_target", []
    try:
        doc = json.loads(metadata["stdout"])
        for package in doc["packages"]:
            for dependency in package.get("dependencies", []):
                path = dependency.get("path")
                if path and not Path(path).resolve().is_relative_to(project.resolve()):
                    issues.append("dependency source is outside the frozen implementation: " + path)
        targets = [
            t
            for p in doc["packages"]
            for t in p["targets"]
            if Path(t["src_path"]).resolve() == entry.resolve()
        ]
        if len(targets) != 1:
            raise ValueError("selected entry has no unique Cargo target")
        prefix = targets[0]["name"].replace("-", "_")
    except (ValueError, KeyError, TypeError):
        issues.append("Cargo target discovery failed; inspect cargo-metadata.json")
    command = [
        str(cargo_verus),
        "verify",
        "--offline",
        "--locked",
        "--manifest-path",
        str(manifest_path),
        "--target-dir",
        str(destination / "cargo-target"),
        "--",
        *flags,
        "--extern",
        "vstd=" + str(Path(binary).parent / "libvstd.rlib"),
        "-L",
        "dependency=" + str(Path(binary).parent),
    ]
    return command, prefix, issues


def _verify(session: Path, manifest: dict, overlay: dict, destination: Path) -> dict:
    binary, identity = _tool()
    if identity != manifest["toolchain"]:
        raise ProofError("Verus toolchain changed since the audit inputs were frozen")
    cfg = manifest["config"]
    views, issues = _views(session, manifest, overlay)
    issues.extend(_source_dependencies(session / "inputs" / "impl"))
    issues.extend(_source_dependencies(session / "inputs" / "specification"))
    destination.mkdir(parents=True, exist_ok=True)
    project = destination / "project"
    _copy(session / "inputs" / "impl", project)
    # Copies are attempt outputs; only the session inputs remain read-only.
    for path in [project, *project.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    for name, source in views.items():
        (project / name).write_text(source, encoding="utf-8")
    specification = destination / "specification"
    _copy(session / "inputs" / "specification", specification)
    spec = _relative(specification, cfg["spec"])
    entry = _relative(project, cfg["entry"])
    spec_path = os.path.relpath(spec, entry.parent)
    entry.write_text(entry.read_text() + spec_attachment(spec_path, cfg.get("macro_imports", {})))
    command, expected_prefix, cargo_issues = _command(binary, project, entry, cfg, destination)
    issues.extend(cargo_issues)
    generated_digest = _digest(project)
    generated_spec_digest = _digest(specification)
    invocation = _execute(command, project, cfg["timeout_secs"])
    if _digest(project) != generated_digest:
        issues.append(
            "the build modified generated candidate inputs; source correspondence was lost"
        )
    if _digest(specification) != generated_spec_digest:
        issues.append("the build modified the verification specification; frozen contract was lost")
    _write(destination / "preparation.json", {"issues": issues, "files": sorted(views)})
    _write(destination / "invocation.json", invocation)
    (destination / "diagnostics.txt").write_text(invocation["stdout"] + invocation["stderr"])
    verified = 0
    if invocation["timed_out"]:
        outcome, message = "incomplete", "verifier timed out; no correctness conclusion"
    else:
        try:
            # cargo-verus can print one JSON object per crate. Require successful evidence
            # for every invocation and use the selected crate's required target set below.
            documents = _verification_documents(invocation["stdout"])
            candidates = [
                d
                for d in documents
                if any(
                    name.startswith(expected_prefix + "::") for name in d.get("func-details", {})
                )
            ]
            doc = candidates[-1] if candidates else documents[-1]
            other_failures = any(
                d.get("verification-results", {}).get("success") is not True for d in documents
            )
            vr = doc["verification-results"]
            verified = vr.get("verified", 0)
            if type(verified) is not int or verified < 0:
                raise ValueError("invalid verified count")
            expected = {f"{expected_prefix}::{o['function']}" for o in cfg["obligations"]}
            covered = expected <= set(doc.get("func-details", {}))
            passed = (
                invocation["returncode"] == 0
                and vr.get("success") is True
                and vr.get("is-verifying-entire-crate") is True
                and vr.get("errors") == 0
                and not vr.get("encountered-error")
                and not vr.get("encountered-vir-error")
                and type(verified) is int
                and verified >= len(expected)
                and covered
                and not issues
                and not other_failures
                and doc.get("verus") == manifest["toolchain"]["version"]
            )
            outcome = "verified" if passed else "incomplete"
            message = (
                "all required obligations verified"
                if passed
                else "verification incomplete; inspect diagnostics"
            )
        except (ValueError, KeyError, TypeError, AttributeError, IndexError):
            verified = 0
            outcome, message = "tool_error", "verifier did not return usable verification evidence"
        if invocation["returncode"] != 0 and re.search(
            r"(?:error[^\n]*|unsupported[^\n]*)(?:not (?:yet )?supported|unsupported)|not supported",
            invocation["stderr"],
            re.I,
        ):
            outcome, message = (
                "unsupported",
                "Verus reported an unsupported construct; inspect diagnostics",
            )
    return {
        "status": outcome,
        "message": message,
        "verified_count": verified,
        "source_digest": _digest(project),
        "spec_digest": _digest(specification),
        "preparation_issues": issues,
    }


def check(run_dir: Path) -> dict:
    run = Run(run_dir)
    with _lock(run):
        session, manifest = _session(run)
        _integrity(run, session, manifest)
        state = _read(session / "state.json")
        if manifest["routing"]["effective"] != "proof":
            raise ProofError("this session uses test-based auditing")
        if state["status"] == "blocked" and (
            not manifest["config"] or manifest["toolchain"] is None
        ):
            return _result(session, manifest, state)
        cfg = manifest["config"]
        if any(
            type(state.get(k)) is not int or state[k] < 0
            for k in ("attempts", "stalled", "best_verified")
        ):
            raise ProofError("invalid proof-attempt accounting")
        reserved = [
            int(p.name)
            for p in (session / "attempts").glob("*")
            if re.fullmatch(r"[0-9]{4,}", p.name)
        ]
        if set(reserved) != set(range(1, state["attempts"] + 1)):
            raise ProofError("attempt history and state disagree; refusing to reset attempt accounting")
        if state["status"] == "verified":
            return _result(session, manifest, state)
        # Old frozen sessions keep their configuration bytes and counters. Their former
        # total cap now means a strategy-review interval, just as it does for new sessions.
        review_every = cfg.get("attempts_per_review", cfg.get("max_attempts", 10))
        reviewed_at = state.get("reviewed_at_attempt", 0)
        if type(reviewed_at) is not int or not 0 <= reviewed_at <= state["attempts"]:
            raise ProofError("invalid proof-review accounting")
        overlay = _read(session / "proof" / "overlay.json")
        plan_hash = _digest(session / "proof" / "plan.md")
        if state["stalled"] >= cfg["max_stalled"] or state["attempts"] - reviewed_at >= review_every:
            if plan_hash == state.get("plan_hash"):
                raise ProofError("proof strategy review due; the proof planner must revise plan.md")
            state["stalled"] = 0
            state["reviewed_at_attempt"] = state["attempts"]
        fingerprint = _hash(overlay)
        previous = state.get("last_overlay")
        if fingerprint == previous and state["status"] in {
            "unsupported",
            "incomplete",
            "verified",
            "invalid",
        }:
            raise ProofError(
                "unchanged proof attempt; read diagnostics and change strategy before retrying"
            )
        state.update(
            attempts=state["attempts"] + 1,
            status="running",
            last_overlay=fingerprint,
            message="verification in progress",
            accepted_attempt=None,
            plan_hash=plan_hash,
        )
        attempt = session / "attempts" / f"{state['attempts']:04d}"
        _write(attempt / "overlay.json", overlay)
        _write(session / "state.json", state)  # reserve the attempt before spawning a process
        _result(session, manifest, state)
        try:
            result = _verify(session, manifest, overlay, attempt / "verification")
            _integrity(run, session, manifest)
        except SourceError as exc:
            result = {"status": "invalid", "message": str(exc), "verified_count": 0}
        except (ProofError, OSError) as exc:
            result = {"status": "blocked", "message": str(exc), "verified_count": 0}
        _write(attempt / "result.json", result)
        state.update(status=result["status"], message=result["message"])
        state["preparation_issues"] = result.get("preparation_issues", [])
        state["last_attempt"] = attempt.name
        diagnostics = attempt / "verification" / "diagnostics.txt"
        state["diagnostics"] = (
            str(diagnostics.relative_to(session)) if diagnostics.is_file() else None
        )
        progress = result["verified_count"] > state["best_verified"]
        state["stalled"] = 0 if progress else state["stalled"] + 1
        state["best_verified"] = max(state["best_verified"], result["verified_count"])
        state["next_action"] = (
            "revise proof strategy"
            if state["stalled"] >= cfg["max_stalled"]
            or state["attempts"] - state.get("reviewed_at_attempt", 0) >= review_every
            else "repair proof"
        )
        if result["status"] == "verified":
            state["accepted_attempt"] = attempt.name
            state["next_action"] = "return verified evidence to auditor"
        elif result["status"] in {"unsupported", "blocked", "tool_error"}:
            state["next_action"] = "diagnose blocker within current audit"
        _write(session / "state.json", state)
        if state["status"] == "verified":
            _completed(run, manifest, state["status"])
        return _result(session, manifest, state)


def evidence(run_dir: Path) -> dict:
    run = Run(run_dir)
    session, manifest = _session(run)
    _integrity(run, session, manifest)
    state = _read(session / "state.json")
    expected = "verified" if manifest["routing"]["effective"] == "proof" else "test_complete"
    if state["status"] != expected:
        raise ProofError(f"audit has no accepted evidence: {state['status']}")
    record = {
        "session": manifest["session"],
        "manifest_hash": manifest["manifest_hash"],
        "mode": manifest["routing"]["effective"],
    }
    if expected == "verified":
        attempt = state.get("accepted_attempt")
        if not isinstance(attempt, str) or not re.fullmatch(r"[0-9]{4,}", attempt):
            raise ProofError("missing accepted proof attempt")
        overlay = _read(session / "attempts" / attempt / "overlay.json")
        _, issues = _views(session, manifest, overlay)
        if issues:
            raise ProofError("accepted proof has unresolved source preparation issues")
        record.update(
            attempt=attempt,
            overlay_hash=_hash(overlay),
            obligations=[o["id"] for o in manifest["config"]["obligations"]],
        )
    return record


def replay(run_dir: Path) -> dict:
    """Recheck accepted evidence without consuming proof-search attempts or changing decisions."""
    run = Run(run_dir)
    with _lock(run):
        record = evidence(run.path)
        session, manifest = _session(run)
        if record["mode"] == "test":
            return record
        overlay = _read(session / "attempts" / record["attempt"] / "overlay.json")
        with tempfile.TemporaryDirectory(prefix="skysynth-proof-") as tmp:
            result = _verify(session, manifest, overlay, Path(tmp))
        _integrity(run, session, manifest)
        if result["status"] != "verified":
            raise ProofError(f"proof replay failed: {result['message']}")
        return record


def finish_test(run_dir: Path) -> dict:
    run = Run(run_dir)
    with _lock(run):
        session, manifest = _session(run)
        _integrity(run, session, manifest)
        if manifest["routing"]["effective"] != "test":
            raise ProofError("cannot complete a proof audit with test evidence")
        state = _read(session / "state.json")
        state.update(
            status="test_complete",
            message="test audit complete; test suite and findings remain release checks",
        )
        _write(session / "state.json", state)
        _completed(run, manifest, "test_complete")
        return _result(session, manifest, state)


def feedback(run_dir: Path, owner: str, reason: str, *, blocker: dict | None = None) -> dict:
    run = Run(run_dir)
    with _lock(run):
        session, manifest = _session(run)
        _integrity(run, session, manifest)
        if owner not in {"proof-planner", "planner", "spec-builder"} or not reason.strip():
            raise ProofError("feedback requires a supported owner and a nonempty reason")
        result = _read(session / "result.json")
        if blocker is not None:
            required = {"diagnostic", "implementation_change", "proof_only_analysis"}
            if (
                owner != "planner"
                or manifest["routing"]["effective"] != "proof"
                or result["status"] not in {"incomplete", "unsupported"}
                or not isinstance(blocker, dict)
                or set(blocker) != required
                or any(not isinstance(blocker[k], str) or not blocker[k].strip() for k in required)
            ):
                raise ProofError(
                    "implementation blocker requires a failed proof, planner owner, "
                    "diagnostic, implementation_change, and proof_only_analysis"
                )
            if blocker["diagnostic"] != result.get("diagnostics"):
                raise ProofError("blocker must cite the latest attempt's diagnostic path")
            diagnostic = _relative(session, blocker["diagnostic"])
            if not diagnostic.is_file() or not diagnostic.read_text().strip():
                raise ProofError("blocker requires nonempty verifier diagnostics")
            blocker = {**blocker, "diagnostic_hash": _digest(diagnostic)}
        doc = {
            "session": manifest["session"],
            "bindings": manifest["bindings"],
            "owner": owner,
            "reason": reason,
            "result": result,
            "classification": "implementation_blocker" if blocker is not None else "request",
            "main_plan_updated": False,
        }
        if blocker is not None:
            doc["blocker"] = blocker
        _write(session / "feedback.json", doc)
        if blocker is not None:
            _completed(run, manifest, "implementation_blocked", feedback_hash=_hash(doc))
        return doc


def _token_listing(source: str) -> dict:
    try:
        return {
            "source": source,
            "tokens": [{"index": i, "text": t.text} for i, t in enumerate(tokens(source))],
        }
    except SourceError as exc:
        return {"source": source, "tokens": [], "preparation_issue": str(exc)}


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)
    for name in ("start", "status", "tokens", "check", "replay", "finish-test", "feedback"):
        p = sub.add_parser(name)
        p.add_argument("run_dir", type=Path)
        if name == "feedback":
            p.add_argument(
                "--owner", required=True, choices=("proof-planner", "planner", "spec-builder")
            )
            p.add_argument("--reason", required=True)
            p.add_argument(
                "--blocker", type=Path,
                help="JSON with diagnostic, implementation_change, proof_only_analysis; "
                "only a diagnosed implementation blocker releases the next round",
            )
    args = ap.parse_args(argv)
    try:
        if args.command == "tokens":
            run = Run(args.run_dir)
            session, manifest = _session(run)
            _integrity(run, session, manifest)
            if manifest["routing"]["effective"] != "proof" or not manifest["config"]:
                raise ProofError("token listing requires a configured Rust proof session")
            views, issues = _views(session, manifest, {"insertions": []})
            text = views[manifest["config"]["entry"]]
            print(
                json.dumps(
                    {
                        "source": text,
                        "preparation_issues": issues,
                        "files": {name: _token_listing(source) for name, source in views.items()},
                        "tokens": _token_listing(text)["tokens"],
                    },
                    indent=2,
                )
            )
            return 0
        if args.command == "feedback":
            result = feedback(
                args.run_dir, args.owner, args.reason,
                blocker=_read(args.blocker) if args.blocker else None,
            )
        else:
            fn = {
                "start": start,
                "status": status,
                "check": check,
                "replay": replay,
                "finish-test": finish_test,
            }[args.command]
            result = fn(args.run_dir)
        print(json.dumps(result, indent=2))
        return (
            0 if result.get("status", "verified") in {"ready", "verified", "test_complete"} else 2
        )
    except (ProofError, SourceError, OSError) as exc:
        print(f"proof: {exc}", file=__import__("sys").stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
