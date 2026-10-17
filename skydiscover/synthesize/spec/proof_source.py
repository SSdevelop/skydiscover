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
            if overlay or words[i - 1] != "verus":
                raise SourceError(f"unsupported macro: {words[i - 1]}!")
        if overlay and (
            word in {"use", "mod", "exec", "unsafe"}
            or (word == "spec" and words[i + 1 : i + 2] != ["::"])
        ):
            raise SourceError(f"proof additions cannot introduce {word}")
    return ts


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


def apply_overlay(base: str, overlay: dict) -> str:
    """Only insert balanced proof statements, loop annotations and fresh helper proof functions."""
    if set(overlay) != {"insertions"} or not isinstance(overlay["insertions"], list):
        raise SourceError("overlay must contain only an insertions list")
    ts = tokens(base)
    ps = pairs(ts)
    fs = functions(base)
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
        if kind == "lemma":
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
        edits.append((ts[before].start, "\n" + text + "\n"))
    for offset, text in sorted(edits, reverse=True):
        base = base[:offset] + text + base[offset:]
    return base
