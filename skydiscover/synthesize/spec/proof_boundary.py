"""Parsed proof-source overlays: immutable execution and authoritative specification.

The pinned Verus parser identifies erasable syntax; the verifier still checks the entire
annotated crate, including helper contracts, ghost erasure and linear resource ownership.
Unknown syntax is preserved by the projection, never optimistically classified as ghost code.
Workload-specific targets, model dependencies and contract macros are supplied by the frozen
run configuration; this module has no dependency on experiment implementations or layouts.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from .proof_source import (
    SourceError, _ATOMIC_PROOF_MACROS, _ATOMIC_PROOF_SHIMS, _ATOMIC_VSTD_ALIAS,
    _atomic_proof_macro_start, _bind_proof_macros, pairs, tokens,
)


_CRATE = Path(__file__).resolve().parents[1] / "proof_boundary"
_PROOF_MACROS = {"seq", "set", "map", "assert_seqs_equal", "calc"}
_ATTRIBUTES = {
    "trigger", "auto_trigger", "all_triggers", "no_auto_trigger",
    "verifier::type_invariant", "verifier::opaque", "verifier::inline",
    "verifier::spinoff_prover", "verifier::rlimit", "verifier::broadcast_forall",
}


def _checked_fragment(source: str) -> str:
    ts = tokens(source)
    words = [t.text.removeprefix("r#") for t in ts]
    ps = pairs(ts)
    forbidden = {
        "assume", "admit", "assume_specification", "external", "external_body", "axiom",
        "inline_air_stmt", "uninterp", "cfg", "cfg_attr", "extern", "unsafe", "use", "mod",
        "exec", "decreases_when", "exec_allows_no_decreases", "allow_in_spec",
    }
    edits = []
    for i, word in enumerate(words):
        if word in forbidden:
            raise SourceError(f"untrusted proof construct: {word}")
        if word == "#":
            if words[i + 1:i + 2] != ["["]:
                raise SourceError("unsupported proof attribute")
            end = ps[i + 1]
            attribute = ""
            for part in words[i + 2:end]:
                if part in {"(", "="} or (attribute and part != "::" and not attribute.endswith("::")):
                    break
                attribute += part
            if attribute not in _ATTRIBUTES:
                raise SourceError(f"unsupported proof attribute: {attribute}")
        if word != "!" or not i or not words[i - 1].isidentifier():
            continue
        if _atomic_proof_macro_start(words, i) is not None:
            continue  # Bound below using the same trusted atomic API as legacy overlays.
        start = i - 1
        while start >= 2 and words[start - 1] == "::" and words[start - 2].isidentifier():
            start -= 2
        if start and words[start - 1] == "::":
            start -= 1
        path = "".join(words[start:i])
        name = words[i - 1]
        if name in _PROOF_MACROS and path in {name, f"vstd::{name}", f"::vstd::{name}"}:
            edits.append((ts[start].start, ts[i - 1].end, f"::{_ATOMIC_VSTD_ALIAS}::{name}"))
            continue
        trusted = {f"::{_ATOMIC_VSTD_ALIAS}::{n}" for n in _PROOF_MACROS}
        trusted.update(f"::{_ATOMIC_VSTD_ALIAS}::{m}::{n}" for n, m in _ATOMIC_PROOF_MACROS.items())
        trusted.update(f"crate::{n}" for n in _ATOMIC_PROOF_SHIMS.values())
        if path not in trusted:
            raise SourceError(f"unsupported proof macro: {path}!")
    for start, end, binding in reversed(edits):
        source = source[:start] + binding + source[end:]
    return _bind_proof_macros(source, tokens(source))


def apply_source(base: str, source: str, *, protected=(), macros=(), roots=(), context=()) -> str:
    """Check correspondence before returning a verifier-ready annotated source."""
    if not isinstance(source, str):
        raise SourceError("proof sources must contain strings")
    # Cargo checks source/dependency fingerprints even when a cached binary exists. Offline
    # operation prevents a proof attempt from downloading or changing the pinned parser.
    command = ["cargo", "build", "--offline", "--locked", "--quiet", "--manifest-path",
               str(_CRATE / "Cargo.toml"), "--target-dir", str(_CRATE / "target")]
    try:
        build = subprocess.run(command, capture_output=True, text=True, timeout=120)
        if build.returncode:
            raise SourceError("proof boundary checker unavailable; build its pinned Cargo crate: "
                              + build.stderr[-4000:])
        checked = subprocess.run(
            [str(_CRATE / "target/debug/skydiscover-proof-boundary")],
            input=json.dumps({"before": base, "after": source, "protected": list(protected),
                              "macros": list(macros), "roots": list(roots),
                              "context": list(context)}),
            capture_output=True, text=True, timeout=30,
        )
        result = json.loads(checked.stdout)
        if checked.returncode or not result.get("ok"):
            raise SourceError("proof boundary: " + result.get("error", checked.stderr))
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise SourceError(f"proof boundary checker unavailable: {exc}") from exc
    # Parser spans are UTF-8 byte offsets. Bind only erased proof regions, leaving all
    # executable and authoritative contract tokens exactly as supplied by the checker.
    data = source.encode("utf-8")
    regions = []
    for start, end in result["ranges"]:
        if regions and start < regions[-1][1]:
            if end > regions[-1][1]:
                raise SourceError("overlapping proof syntax regions")
            continue
        regions.append((start, end))
    for start, end in reversed(regions):
        replacement = _checked_fragment(data[start:end].decode("utf-8")).encode("utf-8")
        data = data[:start] + replacement + data[end:]
    return data.decode("utf-8")
