"""Binding and upstream coverage checks for the public atomic proof macro API."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from skydiscover.synthesize.spec.proof_source import (
    ATOMIC_PROOF_API_REVISION,
    _ATOMIC_PROOF_MACROS,
    SourceError,
    apply_overlay,
    check_source,
    tokens,
)


# Explicit cases ensure that dropping a binding also drops a passing test.
PUBLIC_API = [
    ("atomic", "open_atomic_update_in_proof", "crate::__audit_open_atomic_update_in_proof"),
    ("atomic", "try_open_atomic_update_in_proof",
     "::__audit_atomic_vstd::atomic::try_open_atomic_update_in_proof"),
    ("atomic", "peek_atomic_update_in_proof", "crate::__audit_peek_atomic_update_in_proof"),
    ("invariant", "open_atomic_invariant_in_proof",
     "::__audit_atomic_vstd::invariant::open_atomic_invariant_in_proof"),
    ("invariant", "open_local_invariant_in_proof",
     "::__audit_atomic_vstd::invariant::open_local_invariant_in_proof"),
]


def test_atomic_proof_api_inventory():
    assert _ATOMIC_PROOF_MACROS == {name: module for module, name, _ in PUBLIC_API}


def test_atomic_proof_api_matches_upstream():
    """Set SKYDISCOVER_VERUS_SOURCE to a checkout/archive of the pinned revision."""
    source = os.environ.get("SKYDISCOVER_VERUS_SOURCE")
    if not source:
        pytest.skip(f"set SKYDISCOVER_VERUS_SOURCE to Verus {ATOMIC_PROOF_API_REVISION}")
    vstd = Path(source) / "source/vstd"
    assert vstd.is_dir(), vstd
    # Scan the entire library, so newly introduced proof APIs in another module
    # cannot silently escape coverage. Internal expansion macros don't end here.
    discovered = {
        name: path.relative_to(vstd).with_suffix("").as_posix().replace("/", "::")
        for path in vstd.rglob("*.rs")
        for name in re.findall(r"macro_rules!\s+(\w+_in_proof)\s*\{", path.read_text())
    }
    assert discovered == _ATOMIC_PROOF_MACROS


@pytest.mark.parametrize("module,name,binding", PUBLIC_API)
@pytest.mark.parametrize("path_form", ["bare", "module", "absolute", "root", "absolute_root", "raw"])
def test_atomic_macro_binding_preserves_source_and_payload(module, name, binding, path_form):
    path = {
        "bare": name,
        "module": f"vstd::{module}::{name}",
        "absolute": f"::vstd::{module}::{name}",
        "root": f"vstd::{name}",
        "absolute_root": f"::vstd::{name}",
        "raw": f"r#vstd::r#{module}::r#{name}",
    }[path_form]
    base = "verus! { fn f() { let x = 1; } }"
    before = next(i for i, t in enumerate(tokens(base)) if t.text == "let")
    payload = "lp, state => { let tracked v = state; assert(v@@ == state@@); v }"
    if module == "invariant":
        payload = "credit => &inv => state => { assert(state@ == state@); }"
    added = f"proof {{ {path}!({payload}); }}"
    prepared = apply_overlay(base, {"insertions": [
        {"before": before, "kind": "proof", "text": added},
    ]})
    expected = f"proof {{ {binding}!({payload}); }}"
    assert prepared.replace("\n" + expected + "\n", "") == base


@pytest.mark.parametrize("module,name,binding", PUBLIC_API)
@pytest.mark.parametrize("payload", [
    "assume(false);", "r#admit();", "evil!();", "use other::f;", "unsafe { f(); }",
    "#[verifier::external_body] proof fn f() {}", "exec fn f() {}",
])
def test_all_atomic_macro_payloads_are_checked(module, name, binding, payload):
    with pytest.raises(SourceError):
        check_source(f"proof {{ {name}!(lp, state => {{ {payload} }}); }}", overlay=True)


@pytest.mark.parametrize("module,name,binding", PUBLIC_API)
@pytest.mark.parametrize("prefix", ["fake::vstd", "crate::vstd", "self::vstd", "vstd::wrong"])
def test_atomic_macro_paths_cannot_impersonate_vstd(module, name, binding, prefix):
    with pytest.raises(SourceError, match="unsupported macro"):
        check_source(f"{prefix}::{module}::{name}!(lp, state => {{ state }});", overlay=True)


@pytest.mark.parametrize("name", [
    "vstd::atomic::open_atomic_update", "vstd::atomic::try_open_atomic_update",
    "vstd::atomic::peek_atomic_update", "vstd::atomic::try_open_atomic_update_internal",
    "vstd::invariant::open_atomic_invariant", "vstd::invariant::open_local_invariant",
    "vstd::invariant::open_atomic_invariant_in_proof_internal",
    "vstd::invariant::open_local_invariant_in_proof_internal",
])
def test_exec_and_internal_macros_remain_unavailable_in_overlays(name):
    with pytest.raises(SourceError, match="unsupported macro"):
        check_source(f"{name}!(lp, state => {{ state }});", overlay=True)


def test_nested_macro_bindings_preserve_verus_payloads():
    base = "verus! { fn f() { let x = 1; } }"
    before = next(i for i, t in enumerate(tokens(base)) if t.text == "let")
    text = """proof {
        open_atomic_update_in_proof!(lp, state => {
            vstd::invariant::open_atomic_invariant_in_proof!(credit => &inv => inner => {
                assert(inner@ == state@);
            });
            state
        });
    }"""
    prepared = apply_overlay(base, {"insertions": [
        {"before": before, "kind": "proof", "text": text},
    ]})
    expected = text.replace("open_atomic_update_in_proof!", "crate::__audit_open_atomic_update_in_proof!")
    expected = expected.replace("vstd::invariant::", "::__audit_atomic_vstd::invariant::")
    assert prepared.replace("\n" + expected + "\n", "") == base
