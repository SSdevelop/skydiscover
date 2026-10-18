"""Restricted, insertion-only Verus overlays over frozen Rust tokens.

This is deliberately not a Rust rewriter. A delimiter-aware lexer identifies insertion sites;
Verus parses and checks the resulting program, including ghost erasure and lifetimes. Unsupported
source is still submitted to Verus; annotation-placement failures never grant correctness evidence.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


class SourceError(ValueError):
    pass


@dataclass(frozen=True)
class Token:
    text: str
    start: int
    end: int


def tokens(source: str) -> list[Token]:
    out = []
    i = 0
    while i < len(source):
        start = i
        if source[i].isspace():
            i += 1
            continue
        if source.startswith("//", i):
            j = source.find("\n", i)
            i = len(source) if j < 0 else j
            continue
        if source.startswith("/*", i):
            depth = 1
            i += 2
            while i < len(source) and depth:
                if source.startswith("/*", i):
                    depth += 1
                    i += 2
                elif source.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            if depth:
                raise SourceError("unterminated block comment")
            continue
        raw = re.match(r'(?:br|cr|r)(#*)"', source[i:])
        if raw:
            ending = '"' + raw[1]
            end = source.find(ending, i + raw.end())
            if end < 0:
                raise SourceError("unterminated raw string")
            i = end + len(ending)
        elif source[i] == '"' or source[i : i + 2] in ('b"', 'c"'):
            i += 1 if source[i] == '"' else 2
            while i < len(source):
                if source[i] == "\\":
                    i += 2
                elif source[i] == '"':
                    i += 1
                    break
                else:
                    i += 1
            else:
                raise SourceError("unterminated string")
        else:
            char = re.match(
                r"(?:b)?'(?:\\(?:u\{[0-9a-fA-F]+\}|x[0-9a-fA-F]{2}|.)|[^'\\\n])'", source[i:]
            )
            ident = re.match(r"(?:r#)?[A-Za-z_][A-Za-z_0-9]*", source[i:])
            number = re.match(r"[0-9][A-Za-z_0-9.]*", source[i:])
            op = re.match(
                r"<==>|==>|&&&|\|\|\||::|->|=>|==|!=|<=|>=|&&|\|\||\+=|-=|\*=|/=|\.\.", source[i:]
            )
            match = char or ident or number or op
            i += match.end() if match else 1
        out.append(Token(source[start:i], start, i))
    pairs(out)  # reject unbalanced snippets before interpreting insertion boundaries
    return out


def pairs(ts: list[Token]) -> dict[int, int]:
    stack = []
    result = {}
    close = {"}": "{", ")": "(", "]": "["}
    for i, t in enumerate(ts):
        if t.text in close.values():
            stack.append(i)
        elif t.text in close:
            if not stack or ts[stack[-1]].text != close[t.text]:
                raise SourceError("unbalanced delimiters")
            j = stack.pop()
            result[j] = i
            result[i] = j
    if stack:
        raise SourceError("unbalanced delimiters")
    return result


# Public proof-mode API in vstd at this revision. Internal expansion macros and
# exec-mode macros are deliberately excluded. Keep the upstream inventory test
# in sync when upgrading the supported API.
ATOMIC_PROOF_API_REVISION = "8dea4a2196ebf99449fe2f141a2fb30acae3f17c"
_ATOMIC_PROOF_MACROS = {
    "open_atomic_update_in_proof": "atomic",
    "try_open_atomic_update_in_proof": "atomic",
    "peek_atomic_update_in_proof": "atomic",
    "open_atomic_invariant_in_proof": "invariant",
    "open_local_invariant_in_proof": "invariant",
}
_ATOMIC_VSTD_ALIAS = "__audit_atomic_vstd"
_ATOMIC_CORE_ALIAS = "__audit_atomic_core"
_ATOMIC_PROOF_SHIM = "__audit_open_atomic_update_in_proof"
_ATOMIC_PEEK_SHIM = "__audit_peek_atomic_update_in_proof"
_ATOMIC_PROOF_SHIMS = {
    "open_atomic_update_in_proof": _ATOMIC_PROOF_SHIM,
    "peek_atomic_update_in_proof": _ATOMIC_PEEK_SHIM,
}


def _atomic_proof_macro_start(words: list[str], bang: int) -> int | None:
    """Recognize public vstd proof macros, never an arbitrary path suffix."""
    name = words[bang - 1]
    module = _ATOMIC_PROOF_MACROS.get(name)
    if module is None:
        return None
    start = bang - 1
    while (
        start >= 2
        and words[start - 1] == "::"
        and re.fullmatch(r"[A-Za-z_]\w*", words[start - 2])
    ):
        start -= 2
    if start and words[start - 1] == "::":
        start -= 1
    path = words[start:bang]
    qualified = ["vstd", "::", module, "::", name]
    exported = ["vstd", "::", name]
    if path in ([name], qualified, ["::", *qualified], exported, ["::", *exported]):
        return start
    return None


def check_source(source: str, *, overlay: bool = False) -> list[Token]:
    """Validate added proof text and contract expressions, not admission of candidate source."""
    ts = tokens(source)
    words = [t.text.removeprefix("r#") for t in ts]
    forbidden = {
        "assume",
        "admit",
        "assume_specification",
        "external",
        "external_body",
        "axiom",
        "inline_air_stmt",
        "uninterp",
        "cfg",
        "cfg_attr",
        "extern",
    }
    for i, word in enumerate(words):
        if word in forbidden:
            raise SourceError(f"untrusted or unsupported construct: {word}")
        # No attributes: path/cfg/procedural attributes can load or replace executable source.
        if word == "#":
            raise SourceError("proof additions cannot introduce attributes")
        if word == "!" and i and re.fullmatch(r"[A-Za-z_]\w*", words[i - 1]):
            trusted_atomic = overlay and _atomic_proof_macro_start(words, i) is not None
            if not trusted_atomic and (overlay or words[i - 1] != "verus"):
                raise SourceError(f"unsupported macro: {words[i - 1]}!")
        if overlay and (
            word in {"use", "mod", "exec", "unsafe"}
            or (word == "spec" and words[i + 1 : i + 2] != ["::"])
        ):
            raise SourceError(f"proof additions cannot introduce {word}")
    return ts


def _bind_proof_macros(source: str, ts: list[Token]) -> str:
    """Bind admitted calls to the runner's vstd, bypassing candidate macro shadowing.

    The entry file receives an explicit extern-crate binding in spec_attachment. An
    absolute path also bypasses aliases in nested modules; a crate-root collision is
    a compiler error. Only inserted proof text is rewritten, never candidate tokens.
    Validation still scans the entire macro payload for forbidden constructs.
    """
    words = [t.text.removeprefix("r#") for t in ts]
    edits = []
    for i, word in enumerate(words):
        if word == "!" and i:
            start = _atomic_proof_macro_start(words, i)
            if start is not None:
                name = words[i - 1]
                binding = (
                    f"crate::{_ATOMIC_PROOF_SHIMS[name]}"
                    if name in _ATOMIC_PROOF_SHIMS
                    else f"::{_ATOMIC_VSTD_ALIAS}::{_ATOMIC_PROOF_MACROS[name]}::{name}"
                )
                edits.append((ts[start].start, ts[i - 1].end, binding))
    for start, end, binding in reversed(edits):
        source = source[:start] + binding + source[end:]
    return source


def functions(source: str) -> dict[str, tuple[int, int, int]]:
    """Locate function bodies through modules and impls without imposing a task grammar.

    This index guides annotation placement. Rust/Verus remains the parser and verifier. Macro
    bodies other than verus! are opaque; an unresolved target is reported after a real attempt.
    """
    ts = tokens(source)
    ps = pairs(ts)
    result = {}

    def walk(start, end, scope):
        i = start
        while i < end:
            word = ts[i].text
            if word == "fn" and i + 1 < end:
                j = i + 2
                while j < end and ts[j].text not in {"{", ";"}:
                    j = ps[j] + 1 if ts[j].text in {"(", "["} else j + 1
                if j < end and ts[j].text == "{":
                    name = "::".join([*scope, ts[i + 1].text])
                    if name in result:
                        raise SourceError(f"ambiguous annotation target: {name}")
                    result[name] = (i, j, ps[j])
                    i = ps[j] + 1
                    continue
            if word in {"mod", "impl", "trait"}:
                j = i + 1
                while j < end and ts[j].text not in {"{", ";"}:
                    j = ps[j] + 1 if ts[j].text in {"(", "["} else j + 1
                if j < end and ts[j].text == "{":
                    head = [t.text for t in ts[i + 1 : j]]
                    if word == "impl":
                        # Remove generic parameter lists before reading the implemented type.
                        depth, flat = 0, []
                        for part in head:
                            if part == "<":
                                depth += 1
                            elif part == ">":
                                depth -= 1
                            elif depth == 0:
                                flat.append(part)
                        if "for" in flat:
                            flat = flat[flat.index("for") + 1 :]
                        name = next((x for x in flat if re.fullmatch(r"[A-Za-z_]\w*", x)), "impl")
                    else:
                        name = head[0]
                    walk(j + 1, ps[j], [*scope, name])
                    i = ps[j] + 1
                    continue
            if word == "verus" and [t.text for t in ts[i + 1 : i + 3]] == ["!", "{"]:
                walk(i + 3, ps[i + 2], scope)
                i = ps[i + 2] + 1
                continue
            if word in {"{", "(", "["}:
                i = ps[i] + 1
            else:
                i += 1

    walk(0, len(ts), [])
    return result


def verification_view(source: str) -> str:
    """Add Verus wrappers, preserving external module declarations and crate attributes."""
    ts = tokens(source)
    ps = pairs(ts)
    if any(
        t.text == "verus" and [x.text for x in ts[i + 1 : i + 2]] == ["!"] for i, t in enumerate(ts)
    ):
        return source
    # Non-inline modules cannot be loaded from a procedural macro. Leave these declarations,
    # along with their attributes/visibility, at Rust's item level.
    chunks, start, item, i = [], 0, 0, 0
    while i < len(ts):
        if ts[i].text == "#" and [x.text for x in ts[i + 1 : i + 3]] == ["!", "["]:
            end = ps[i + 2]
            chunks.append(source[start : ts[end].end])
            start = ts[end].end
            item = start
            i = end + 1
            continue
        if ts[i].text == "mod" and i + 2 < len(ts) and ts[i + 2].text == ";":
            prefix = source[start:item]
            if prefix.strip():
                chunks.append("verus! {\n" + prefix + "\n}\n")
            chunks.append(source[item : ts[i + 2].end])
            start = ts[i + 2].end
            item = start
            i += 3
            continue
        if ts[i].text in {"{", "(", "["}:
            end = ps[i]
            if ts[i].text == "{":
                item = ts[end].end
            i = end + 1
        else:
            if ts[i].text == ";":
                item = ts[i].end
            i += 1
    if source[start:].strip():
        chunks.append("verus! {\n" + source[start:] + "\n}\n")
    return "\n".join(chunks) + "\nuse vstd::prelude::*;\n"


def base_source(implementation: str, obligations: list[dict]) -> str:
    """Attach frozen Verus clauses to frozen function headers; no executable token is replaced."""
    implementation = verification_view(implementation)
    fs = functions(implementation)
    ts = tokens(implementation)
    edits = []
    names = set()
    for obligation in obligations:
        requested = obligation["function"]
        matches = [name for name in fs if requested == name or requested.endswith("::" + name)]
        if len(matches) != 1:
            raise SourceError(f"cannot uniquely place frozen contract: {requested}")
        name = matches[0]
        if name in names or name not in fs:
            raise SourceError(f"duplicate or missing obligation function: {name}")
        names.add(name)
        fn, body, _ = fs[name]
        if fn and ts[fn - 1].text in {"spec", "proof"}:
            raise SourceError("an obligation must name an executable function")
        header = [t.text for t in ts[fn:body]]
        if any(w in header for w in ("requires", "ensures", "recommends", "decreases", "returns")):
            raise SourceError("put function contracts in the frozen obligation configuration")
        if "->" in header:
            arrow = fn + header.index("->")
            return_tokens = ts[arrow + 1 : body]
            named = (
                len(return_tokens) > 3
                and return_tokens[0].text == "("
                and return_tokens[2].text == ":"
            )
            if not named:
                if "result" in header or "where" in header or not return_tokens:
                    raise SourceError("cannot introduce the Verus result binding in this signature")
                edits.extend([(return_tokens[0].start, "(result: "), (ts[body - 1].end, ")")])
        clauses = []
        for clause in ("requires", "ensures"):
            value = obligation.get(clause, "")
            if not isinstance(value, str) or (clause == "ensures" and not value.strip()):
                raise SourceError("each obligation needs a nonempty Verus ensures expression")
            if value:
                refs = tokens(value)
                for j in range(len(refs) - 2, -1, -1):
                    if (
                        refs[j].text == "spec"
                        and refs[j + 1].text == "::"
                        and (j == 0 or refs[j - 1].text != "::")
                    ):
                        value = (
                            value[: refs[j].start] + "crate::__audit_spec" + value[refs[j].end :]
                        )
                expr = check_source(value)
                if any(t.text in {"{", "}", ";", "requires", "ensures", "fn"} for t in expr):
                    raise SourceError(
                        "contract expressions must not contain blocks or declarations"
                    )
                clauses.append(f"{clause} {value},\n")
        edits.append((ts[body].start, "\n" + "".join(clauses)))
    for offset, text in sorted(edits, reverse=True):
        implementation = implementation[:offset] + text + implementation[offset:]
    return implementation


def macro_functions(source: str, macro: str) -> dict[str, tuple[int, int, int]]:
    """Index an unconditional crate-root invocation of a frozen contract macro.

    Supported invocation syntax is `crate::name! { impl Type { method(args) { body } ... } }`.
    Rust expands the actual macro from the frozen spec; we only locate insertion sites. Calls
    hidden inside other macros, attributed items, functions or modules do not establish coverage.
    Other source shapes still reach Verus and receive a preparation diagnostic.
    """
    ts = tokens(source)
    ps = pairs(ts)
    words = [t.text for t in ts]
    found = {}
    i, attributed = 0, False
    while i < len(ts):
        if words[i] == "#":
            attributed = True
        if words[i : i + 5] == ["crate", "::", macro, "!", "{"]:
            if attributed:
                raise SourceError(
                    "contract macro invocation must be unconditional and unattributed"
                )
            opening = i + 4
            if (
                len(words) <= opening + 3
                or words[opening + 1] != "impl"
                or words[opening + 3] != "{"
            ):
                raise SourceError("contract macro requires impl Type { method(args) { body } }")
            typename = words[opening + 2]
            if not re.fullmatch(r"[A-Za-z_]\w*", typename):
                raise SourceError("contract macro type must be a crate-root identifier")
            end = ps[opening + 3]
            if end + 1 != ps[opening]:
                raise SourceError("unexpected tokens after contract macro impl")
            j = opening + 4
            while j < end:
                if j + 1 >= end or words[j + 1] != "(":
                    raise SourceError("contract macro method requires an argument list")
                body = ps[j + 1] + 1
                if body >= end or words[body] != "{":
                    raise SourceError("contract macro method requires a block body")
                name = typename + "::" + words[j]
                if name in found:
                    raise SourceError("duplicate contract macro method: " + name)
                found[name] = (j, body, ps[body])
                j = ps[body] + 1
            i = ps[opening] + 1
            continue
        if words[i] in {"{", "(", "["}:
            end = ps[i]
            # An attribute is a prefix, not a completed item.
            if words[i] == "{":
                attributed = False
            i = end + 1
        else:
            if words[i] == ";":
                attributed = False
            i += 1
    return found


def spec_attachment(spec_path: str, macro_imports: dict[str, str]) -> str:
    """Attach the frozen model and explicit macro-name bindings in verification/build views.

    Explicit imports prevent a candidate declaration from silently shadowing a glob-imported
    model name used by a macro's call-site resolution. Conflicts become real compiler errors.
    """
    import json

    lines = [
        "\n#[path = " + json.dumps(spec_path) + "] pub mod __audit_spec;",
        f"extern crate vstd as {_ATOMIC_VSTD_ALIAS};",
        f"extern crate core as {_ATOMIC_CORE_ALIAS};",
        # The pinned 8dea4a2 vstd public macro calls an absent au_commit_wrap_proof.
        # Express its commit-only operation using the real proof-mode try macro.
        # Verus still checks the atomic block, linear resource and Commit payload;
        # there is no trusted function body or added logical assumption here.
        # Forward raw body tokens: a Rust `block` fragment hides Verus @/tracked
        # syntax from the downstream ghost-expression procedural macro.
        f"""
#[macro_export]
macro_rules! {_ATOMIC_PROOF_SHIM} {{
    ($au:expr, $state:pat => {{ $($body:tt)* }}) => {{{{
        let _ = ::{_ATOMIC_VSTD_ALIAS}::atomic::try_open_atomic_update_in_proof!(
            $au, $state => {{
                let tracked audit_commit_value = {{ $($body)* }};
                ::{_ATOMIC_VSTD_ALIAS}::prelude::Tracked::new(
                    ::{_ATOMIC_VSTD_ALIAS}::atomic::Commit(audit_commit_value)
                )
            }}
        );
    }}}};
}}
// The same revision lacks au_abort_wrap_proof/au_abort_unwrap_proof.
// Return the still-owned update after a checked abort; tracked_unwrap_err
// requires the verifier to establish that the operation really aborted.
#[macro_export]
macro_rules! {_ATOMIC_PEEK_SHIM} {{
    ($au:expr, $state:pat => {{ $($body:tt)* }}) => {{{{
        #[verifier::proof]
        let audit_peek_result =
            ::{_ATOMIC_VSTD_ALIAS}::atomic::try_open_atomic_update_in_proof!(
                $au, $state => {{
                    let tracked audit_abort_value = {{ $($body)* }};
                    ::{_ATOMIC_VSTD_ALIAS}::prelude::Tracked::new(
                        ::{_ATOMIC_CORE_ALIAS}::result::Result::Err(audit_abort_value)
                    )
                }}
            ).get();
        ::{_ATOMIC_VSTD_ALIAS}::std_specs::result::ResultAdditionalSpecFns::tracked_unwrap_err(
            audit_peek_result
        )
    }}}};
}}
""",
    ]
    if macro_imports:
        lines.extend(["extern crate vstd;", "extern crate std;"])
    for name, path in sorted(macro_imports.items()):
        if (
            not re.fullmatch(r"[A-Za-z_]\w*", name)
            or not isinstance(path, str)
            or not re.fullmatch(r"(?:crate::__audit_spec|vstd|std)(?:::[A-Za-z_]\w*)+", path)
        ):
            raise SourceError(
                "macro_imports must bind identifiers to frozen spec, vstd or std paths"
            )
        lines.append(f"use {path} as {name};")
    return "\n".join(lines) + "\n"


def apply_overlay(
    base: str, overlay: dict, *, contract_macros: tuple[str, ...] = (),
    protected_functions: tuple[str, ...] = (),
) -> str:
    """Insert checked proof annotations without changing executable tokens or frozen contracts."""
    if set(overlay) != {"insertions"} or not isinstance(overlay["insertions"], list):
        raise SourceError("overlay must contain only an insertions list")
    ts = tokens(base)
    ps = pairs(ts)
    fs = functions(base)
    for macro in contract_macros:
        fs.update(macro_functions(base, macro))
    words = {t.text for t in ts}
    edits = []
    used = set()
    helper_names = set()
    wrappers = [
        i + 2 for i in range(len(ts) - 2) if [t.text for t in ts[i : i + 3]] == ["verus", "!", "{"]
    ]
    for item in overlay["insertions"]:
        if not isinstance(item, dict) or set(item) != {"before", "kind", "text"}:
            raise SourceError("insertion fields are before (token index), kind, text")
        before, kind, text = item["before"], item["kind"], item["text"]
        if type(before) is not int or not 0 <= before < len(ts) or before in used:
            raise SourceError("invalid or duplicate insertion token index")
        if not isinstance(text, str):
            raise SourceError("insertion text must be a string")
        used.add(before)
        added = check_source(text, overlay=True)
        ap = pairs(added)
        aw = [t.text for t in added]
        if kind == "helper_ensures":
            targets = [(name, fn, body) for name, (fn, body, _) in fs.items() if body == before]
            if len(targets) != 1:
                raise SourceError("helper_ensures goes immediately before a helper function body")
            name, fn, body = targets[0]
            if ts[fn].text != "fn" or any(
                name == target or target.endswith("::" + name) for target in protected_functions
            ):
                raise SourceError("helper_ensures cannot alter an authoritative obligation")
            if fn and ts[fn - 1].text in {"spec", "proof"}:
                raise SourceError("helper_ensures requires an executable helper")
            header = [t.text for t in ts[fn:body]]
            clauses = {"requires", "ensures", "recommends", "returns", "decreases",
                       "opens_invariants", "no_unwind", "when", "where"}
            if clauses.intersection(header):
                raise SourceError("helper_ensures cannot modify an existing function contract")
            if (
                aw[:1] != ["ensures"] or len(aw) < 2
                or (clauses | {"{", "}", ";", "fn"}).intersection(aw[1:])
            ):
                raise SourceError("helper_ensures accepts only postcondition expressions")
            if "->" in header:
                arrow = fn + header.index("->")
                returned = ts[arrow + 1:body]
                named = len(returned) > 3 and returned[0].text == "(" and returned[2].text == ":"
                if not named:
                    if not returned or "audit_result" in words:
                        raise SourceError("cannot introduce a fresh audit_result binding")
                    edits.extend([(returned[0].start, "(audit_result: "),
                                  (ts[body - 1].end, ")")])
        elif kind == "lemma":
            if before not in {ps[w] for w in wrappers} or aw[:2] != ["proof", "fn"] or len(aw) < 5:
                raise SourceError("lemmas go before the verus! closing brace")
            name = aw[2]
            if not name.startswith("audit_lemma_") or name in words or name in helper_names:
                raise SourceError("helper name must be fresh and start with audit_lemma_")
            helper_names.add(name)
            parsed = functions("verus! {" + text + "}")
            if list(parsed) != [name]:
                raise SourceError("one proof function per lemma insertion")
        else:
            if not any(body < before <= end for _, body, end in fs.values()):
                raise SourceError("proof statements belong inside an executable function body")
            if kind == "proof":
                if aw[:2] != ["proof", "{"] or ap.get(1) != len(added) - 1:
                    raise SourceError("proof insertion must be exactly one proof { ... } block")
                if ts[before - 1].text not in {"{", ";", "}"}:
                    raise SourceError("proof blocks must start at a statement boundary")
            elif kind == "invariant":
                if ts[before].text != "{" or not aw or aw[0] != "invariant":
                    raise SourceError("invariants go immediately before a loop body")
                if any(w in aw for w in ("{", "}", ";", "fn", "requires", "ensures")):
                    raise SourceError("loop annotations cannot contain blocks or declarations")
                # Keep the supported loop header grammar explicit: no block-valued conditions.
                j = before - 1
                while j >= 0 and ts[j].text not in {"{", "}", ";", "while", "loop", "for"}:
                    j -= 1
                if j < 0 or ts[j].text not in {"while", "loop", "for"}:
                    raise SourceError("invariant insertion is not at a loop body")
            elif kind == "ghost":
                if aw[:2] != ["let", "ghost"] or not aw[-1:] == [";"]:
                    raise SourceError("ghost insertion must be a let ghost declaration")
                if len(aw) < 6 or not aw[2].startswith("audit_ghost_") or aw[2] in words:
                    raise SourceError("ghost binding must have a fresh audit_ghost_ name")
                if any(w in aw[3:-1] for w in (";", "{", "}", "fn")):
                    raise SourceError("ghost initializer must be a single expression")
                if ts[before - 1].text not in {"{", ";", "}"}:
                    raise SourceError("ghost bindings must start at a statement boundary")
            else:
                raise SourceError(f"unsupported insertion kind: {kind}")
        edits.append((ts[before].start, "\n" + _bind_proof_macros(text, added) + "\n"))
    for offset, text in sorted(edits, reverse=True):
        base = base[:offset] + text + base[offset:]
    return base
