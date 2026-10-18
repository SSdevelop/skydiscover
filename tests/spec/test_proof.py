from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from skydiscover.synthesize.spec import checkpoint, proof
from skydiscover.synthesize.spec.paths import Run
from skydiscover.synthesize.spec.proof_source import SourceError, apply_overlay, tokens

EXAMPLE = Path(__file__).resolve().parents[2] / "skydiscover/synthesize/examples/verus-audit"

MACRO_SPEC = """
use vstd::prelude::*;
#[macro_export]
macro_rules! audit_counter {
    (impl $ty:ty { count($n:ident) $body:block }) => {
        verus! { impl $ty {
            pub fn count($n: u64) -> (result: u64) ensures result == $n, $body
        } }
    };
}
"""


def macro_candidate(run, source):
    (run.impl / "counter.rs").write_text(source)
    (run.specification / "contract.rs").write_text(MACRO_SPEC)
    cfg = json.loads(run.verus_config.read_text())
    cfg["obligations"] = [{"id": "count", "function": "Counter::count", "macro": "audit_counter"}]
    run.verus_config.write_text(json.dumps(cfg))


def test_frozen_macro_contract_accepts_proof_overlay_and_replays(run, verus):
    macro_candidate(
        run,
        """
use vstd::prelude::*;
verus! { pub struct Counter {} }
crate::audit_counter! { impl Counter { count(n) { verus_exec_expr! { {
    let mut i = 0u64;
    while i < n { i = i + 1; }
    i
} } } } }
""",
    )
    assert proof.start(run.path)["status"] == "ready"
    first = proof.check(run.path)
    assert first["status"] == "incomplete", first
    invariant(run)
    passed = proof.check(run.path)
    assert passed["status"] == "verified", passed
    assert proof.replay(run.path)["obligations"] == ["count"]


@pytest.mark.parametrize(
    "hidden",
    [
        "#[cfg(any())] crate::audit_counter! { impl Counter { count(n) { 0 } } }",
        "macro_rules! unused { () => { crate::audit_counter! { impl Counter { count(n) { 0 } } } } }",
    ],
)
def test_inactive_macro_cannot_supply_contract_coverage(run, verus, hidden):
    macro_candidate(
        run,
        """
use vstd::prelude::*;
verus! { pub struct Counter {} impl Counter { pub fn count(n: u64) -> u64 { 0 } } }
""" + hidden,
    )
    assert proof.start(run.path)["status"] == "ready"
    result = proof.check(run.path)
    assert result["status"] != "verified"
    session, _ = proof._session(run)
    attempts = list((session / "attempts").glob("*/verification/preparation.json"))
    assert attempts and json.loads(attempts[0].read_text())["issues"]


def test_macro_contract_cannot_be_overridden(run):
    macro_candidate(run, "use vstd::prelude::*; verus! {}")
    cfg = json.loads(run.verus_config.read_text())
    cfg["obligations"][0]["ensures"] = "true"
    run.verus_config.write_text(json.dumps(cfg))
    with pytest.raises(proof.ProofError, match="complete contract"):
        proof._config(run, proof.route(run.path))


def test_macro_call_site_cannot_shadow_frozen_model_predicate(run, verus):
    macro_candidate(
        run,
        """
use vstd::prelude::*;
verus! {
pub struct Counter {}
pub open spec fn correct(result: u64, n: u64) -> bool { true }
}
crate::audit_counter! { impl Counter { count(n) { 0 } } }
""",
    )
    spec = MACRO_SPEC.replace("result == $n", "correct(result, $n)")
    spec += "\nverus! { pub open spec fn correct(result: u64, n: u64) -> bool { result == n } }\n"
    (run.specification / "contract.rs").write_text(spec)
    cfg = json.loads(run.verus_config.read_text())
    cfg["macro_imports"] = {"correct": "crate::__audit_spec::correct"}
    run.verus_config.write_text(json.dumps(cfg))
    assert proof.start(run.path)["status"] == "ready"
    assert proof.check(run.path)["status"] != "verified"
    session, _ = proof._session(run)
    diagnostics = next((session / "attempts").glob("*/verification/diagnostics.txt")).read_text()
    assert "defined multiple times" in diagnostics


@pytest.mark.parametrize(
    "path",
    [
        "open_atomic_update_in_proof",
        "vstd::atomic::open_atomic_update_in_proof",
        "::vstd::atomic::open_atomic_update_in_proof",
        "r#vstd::atomic::r#open_atomic_update_in_proof",
    ],
)
def test_atomic_proof_macro_is_bound_without_changing_candidate_tokens(path):
    base = "verus! { fn f() { let x = 1; } }"
    before = next(i for i, t in enumerate(tokens(base)) if t.text == "let")
    text = f"proof {{ {path}!(lp, state => {{ state }}); }}"
    prepared = apply_overlay(
        base, {"insertions": [{"before": before, "kind": "proof", "text": text}]}
    )
    bound = "proof { crate::__audit_open_atomic_update_in_proof!(lp, state => { state }); }"
    assert prepared.replace("\n" + bound + "\n", "") == base


@pytest.mark.parametrize(
    "text",
    [
        "other::atomic::open_atomic_update_in_proof!(lp, state => { state });",
        "fake::vstd::atomic::open_atomic_update_in_proof!(lp, state => { state });",
        "vstd::atomic::open_atomic_update!(lp, state => { state });",
        "vstd::atomic::try_open_atomic_update_internal!(lp, state => { state });",
        "open_atomic_update_in_proof!(lp, state => { assume(false); state });",
        "open_atomic_update_in_proof!(lp, state => { r#admit(); state });",
        "open_atomic_update_in_proof!(lp, state => { evil!(); state });",
        "open_atomic_update_in_proof!(lp, state => { #[verifier::external_body] proof fn f() {} state });",
        "open_atomic_update_in_proof!(lp, state => { unsafe { f(); } state });",
        "open_atomic_update_in_proof!(lp, state => { exec fn f() {} state });",
        "open_atomic_update_in_proof!(lp, state => { use other::f; state });",
        "open_atomic_update_in_proof!(lp, state => { macro_rules! f { () => {} } state });",
    ],
)
def test_atomic_macro_does_not_hide_unsupported_proof_constructs(text):
    base = "verus! { fn f() { let x = 1; } }"
    before = next(i for i, t in enumerate(tokens(base)) if t.text == "let")
    with pytest.raises(SourceError):
        apply_overlay(
            base,
            {"insertions": [{"before": before, "kind": "proof", "text": "proof {" + text + "}"}]},
        )


def atomic_candidate(
    run, *, body="state", shadow="", twice=False, proof_text=None, result_contract=False
):
    macro_candidate(
        run,
        "use vstd::prelude::*;\n"
        + shadow
        + """
verus! { pub struct Counter {} }
crate::audit_counter! { impl Counter { count(n, lp) { verus_exec_expr! { {
    let mut result = n;
    result
} } } } }
""",
    )
    (run.specification / "contract.rs").write_text(
        """
use vstd::prelude::*;
#[macro_export]
macro_rules! audit_counter {
    (impl $ty:ty { count($n:ident, $lp:ident) $body:block }) => {
        verus! { impl $ty {
            pub fn count($n: u64) -> (result: u64)
                atomically ($lp) {
                    (state: int) -> (post: vstd::atomic::Commit<int>),
                    requires state == $n,
                    ensures post@ == state,
                    outer_mask any,
                    inner_mask none,
                },
                ensures result == state,
            $body
        } }
    };
}
"""
    )
    if result_contract:
        spec = run.specification / "contract.rs"
        spec.write_text(
            spec.read_text()
            .replace("vstd::atomic::Commit<int>", "Result<int, int>")
            .replace("post@ == state", "match post { Ok(v) => v == state, Err(v) => v == state }")
        )
    assert proof.start(run.path)["status"] == "ready"
    path, source = overlay_path(run)
    # The final result expression follows the initializer's semicolon.
    ts = tokens(source)
    before = next(i for i, t in enumerate(ts) if t.text == ";" and i and ts[i - 1].text == "n") + 1
    call = "open_atomic_update_in_proof!(lp, state => { " + body + " });"
    if proof_text is None:
        proof_text = call + (call if twice else "")
    path.write_text(
        json.dumps(
            {
                "insertions": [
                    {
                        "before": before,
                        "kind": "proof",
                        "text": "proof { " + proof_text + " }",
                    }
                ]
            }
        )
    )


def test_atomic_proof_overlay_verifies_and_replays(run, verus):
    atomic_candidate(run)
    path, _ = overlay_path(run)
    overlay = path.read_text()
    path.write_text('{"insertions": []}')
    assert proof.check(run.path)["status"] == "incomplete"
    path.write_text(overlay)
    result = proof.check(run.path)
    assert result["status"] == "verified", result
    assert proof.replay(run.path)["obligations"] == ["count"]


def test_atomic_proof_macro_preserves_verus_payload_syntax(run, verus):
    atomic_candidate(
        run,
        body="let tracked value = vstd::atomic::Commit(state); "
        "let tracked wrapped = vstd::atomic::Commit(value); "
        "assert(wrapped@@ == state); wrapped.get().get()",
    )
    result = proof.check(run.path)
    assert result["status"] == "verified", result
    assert proof.replay(run.path)["obligations"] == ["count"]


def test_atomic_proof_macro_cannot_change_executable_state(run, verus):
    atomic_candidate(run, body="result = 0; state")
    result = proof.check(run.path)
    assert result["status"] != "verified", result
    session, _ = proof._session(run)
    diagnostics = next((session / "attempts").glob("*/verification/diagnostics.txt")).read_text()
    assert "exec" in diagnostics and "proof" in diagnostics


def test_candidate_macro_shadow_cannot_replace_atomic_proof_macro(run, verus):
    # Expanding this lookalike would fail compilation. The trusted absolute binding
    # must instead resolve to the real vstd macro, even for unqualified overlay text.
    atomic_candidate(
        run,
        shadow='macro_rules! open_atomic_update_in_proof { ($($t:tt)*) => { '
        'compile_error!("shadow invoked"); }; }',
    )
    result = proof.check(run.path)
    assert result["status"] == "verified", result


@pytest.mark.parametrize(
    "shadow",
    [
        "mod __audit_atomic_vstd {}",
        "mod __audit_atomic_core {}",
        "#[macro_export] macro_rules! __audit_open_atomic_update_in_proof { ($($t:tt)*) => {}; }",
        "#[macro_export] macro_rules! __audit_peek_atomic_update_in_proof { ($($t:tt)*) => {}; }",
    ],
)
def test_candidate_cannot_replace_atomic_adapter_bindings(run, verus, shadow):
    atomic_candidate(run, shadow=shadow)
    result = proof.check(run.path)
    assert result["status"] != "verified", result
    session, _ = proof._session(run)
    diagnostics = next((session / "attempts").glob("*/verification/diagnostics.txt")).read_text()
    assert "defined multiple times" in diagnostics


def test_atomic_proof_macro_checks_update_postcondition(run, verus):
    atomic_candidate(run, body="state + 1")
    result = proof.check(run.path)
    assert result["status"] == "incomplete", result
    session, _ = proof._session(run)
    diagnostics = next((session / "attempts").glob("*/verification/diagnostics.txt")).read_text()
    assert "postcondition" in diagnostics


def test_atomic_proof_macro_cannot_consume_update_twice(run, verus):
    atomic_candidate(run, twice=True)
    result = proof.check(run.path)
    assert result["status"] == "incomplete", result
    session, _ = proof._session(run)
    diagnostics = next((session / "attempts").glob("*/verification/diagnostics.txt")).read_text()
    assert "moved value" in diagnostics


@pytest.mark.parametrize("peek", [False, True], ids=["try_abort", "peek"])
@pytest.mark.parametrize("bad_abort", [False, True], ids=["valid", "bad_postcondition"])
def test_atomic_abort_then_commit(run, verus, peek, bad_abort):
    value = "state + 1" if bad_abort else """{
        let tracked value = vstd::atomic::Commit(vstd::atomic::Commit(state));
        assert(value@@ == state); value.get().get()
    }"""
    abort = (
        "let tracked again = peek_atomic_update_in_proof!(lp, state => { " + value + " });"
        if peek else
        "let tracked aborted = try_open_atomic_update_in_proof!(lp, state => { "
        "Tracked::new(Result::<int, int>::Err(" + value + ")) });"
        "assert(aborted@ is Err); let tracked again = aborted.get().tracked_unwrap_err();"
    )
    atomic_candidate(run, result_contract=True, proof_text=abort + """
        assert(again == lp);
        let tracked committed = try_open_atomic_update_in_proof!(again, state => {
            let tracked value = vstd::atomic::Commit(state);
            assert(value@ == state);
            Tracked::new(Result::<int, int>::Ok(value.get()))
        });
        assert(committed@ is Ok);
    """)
    result = proof.check(run.path)
    if bad_abort:
        assert result["status"] == "incomplete", result
        session, _ = proof._session(run)
        diagnostics = next((session / "attempts").glob("*/verification/diagnostics.txt")).read_text()
        assert "postcondition" in diagnostics
    else:
        assert result["status"] == "verified", result
        assert proof.replay(run.path)["obligations"] == ["count"]


@pytest.mark.parametrize("peek", [False, True], ids=["try_open", "peek"])
@pytest.mark.parametrize("case", ["shadow", "reuse_update", "exec_write"])
def test_new_atomic_macros_preserve_proof_boundaries(run, verus, peek, case):
    name = "peek_atomic_update_in_proof" if peek else "try_open_atomic_update_in_proof"
    shadow = (
        f'macro_rules! {name} {{ ($($t:tt)*) => {{ compile_error!("shadow invoked"); }}; }}'
        if case == "shadow" else ""
    )
    body = ("result = 0; " if case == "exec_write" else "") + (
        "state" if peek else "Tracked::new(Result::<int, int>::Err(state))"
    )
    abort = f"let tracked again = {name}!(lp, state => {{ {body} }});"
    if not peek:
        abort += "let tracked again = again.get().tracked_unwrap_err();"
    target = "lp" if case == "reuse_update" else "again"
    commit = f"""try_open_atomic_update_in_proof!({target}, state => {{
        Tracked::new(Result::<int, int>::Ok(state))
    }});"""
    atomic_candidate(run, shadow=shadow, result_contract=True, proof_text=abort + commit)
    result = proof.check(run.path)
    if case == "shadow":
        assert result["status"] == "verified", result
    else:
        assert result["status"] == "incomplete", result
        session, _ = proof._session(run)
        diagnostics = next((session / "attempts").glob("*/verification/diagnostics.txt")).read_text()
        assert ("moved value" if case == "reuse_update" else "exec") in diagnostics


@pytest.mark.parametrize("local", [False, True], ids=["atomic", "local"])
@pytest.mark.parametrize("case", [
    "valid", "shadow", "bad_invariant", "reuse_credit", "create_credit", "exec_write",
])
def test_invariant_proof_macro(run, verus, local, case):
    inv_type = "LocalInvariant" if local else "AtomicInvariant"
    macro = "open_local_invariant_in_proof" if local else "open_atomic_invariant_in_proof"
    shadow = (
        f'macro_rules! {macro} {{ ($($t:tt)*) => {{ compile_error!("shadow invoked"); }}; }}'
        if case == "shadow" else ""
    )
    (run.impl / "counter.rs").write_text(shadow + """
use vstd::prelude::*;
use vstd::invariant::*;
verus! {
pub struct Predicate {}
impl InvariantPredicate<int, int> for Predicate {
    open spec fn inv(k: int, value: int) -> bool { value == k }
}
pub fn count(n: u64, Tracked(inv): Tracked<INV<int, int, Predicate>>) -> (result: u64)
{
    let credit_wrapper = create_open_invariant_credit();
    let tracked credit = credit_wrapper.get();
    let mut result = n;
    result
}
}
""".replace("INV<int", inv_type + "<int"))
    assert proof.start(run.path)["status"] == "ready"
    path, source = overlay_path(run)
    ts = tokens(source)
    before = next(i for i, t in enumerate(ts) if t.text == ";" and i and ts[i - 1].text == "n") + 1
    body = {"bad_invariant": "inner = inner + 1;", "exec_write": "result = 0;"}.get(
        case, "assert(inner == inv.constant());"
    )
    credit = "create_open_invariant_credit().get()" if case == "create_credit" else "credit"
    call = f"{macro}!({credit} => &inv => inner => {{ {body} }});"
    path.write_text(json.dumps({"insertions": [{
        "before": before, "kind": "proof",
        "text": "proof { " + call + (call if case == "reuse_credit" else "") + " }",
    }]}))
    result = proof.check(run.path)
    if case in {"valid", "shadow"}:
        assert result["status"] == "verified", result
        assert proof.replay(run.path)["obligations"] == ["counter_returns_n"]
    else:
        assert result["status"] == "incomplete", result
        session, _ = proof._session(run)
        diagnostics = next((session / "attempts").glob("*/verification/diagnostics.txt")).read_text()
        expected = {
            "bad_invariant": "invariant", "reuse_credit": "moved value",
            "exec_write": "exec", "create_credit": "exec",
        }
        assert expected[case] in diagnostics


@pytest.fixture
def run(tmp_path):
    target = tmp_path / "run"
    shutil.copytree(EXAMPLE, target)
    return Run(target).create()


@pytest.fixture
def verus():
    binary = shutil.which(os.environ.get("SKYDISCOVER_VERUS", "verus"))
    if not binary:
        pytest.skip("real Verus integration unavailable: install Verus or set SKYDISCOVER_VERUS")
    return binary


def overlay_path(run):
    session, manifest = proof._session(run)
    return session / "proof/overlay.json", proof._base(session, manifest)


def invariant(run, text="invariant i <= n, decreases n - i,"):
    path, source = overlay_path(run)
    ts = tokens(source)
    start = next(i for i, t in enumerate(ts) if t.text == "while")
    before = next(i for i in range(start, len(ts)) if ts[i].text == "{")
    path.write_text(
        json.dumps({"insertions": [{"before": before, "kind": "invariant", "text": text}]})
    )


def test_real_verus_repairs_proof_and_replays_exported_inputs(run, verus, tmp_path):
    initial = proof.start(run.path)
    assert initial["status"] == "ready", initial
    original = proof._bindings(run)
    first = proof.check(run.path)
    assert first["status"] == "incomplete", first
    invariant(run)
    passed = proof.check(run.path)
    assert passed["status"] == "verified", passed
    assert passed["attempts"] == 2
    assert proof._bindings(run) == original
    record = proof.replay(run.path)
    assert record["obligations"] == ["counter_returns_n"]
    checkpoint.stamp_audit(run.path, [])
    assert checkpoint.audit_covers(run.completeness, run.impl) is None

    # The proof bundle carries all source inputs and uses no reference to the original workspace.
    relocated = Run(tmp_path / "relocated")
    shutil.copytree(run.path, relocated.path)
    assert proof.replay(relocated.path) == record
    assert proof.status(relocated.path)["attempts"] == 2  # replay is not proof search


@pytest.mark.parametrize(
    "language,requested,effective",
    [
        ("cpp", "proof", "test"),
        ("cpp", "test", "test"),
        ("rs", "test", "test"),
        ("rs", "proof", "proof"),
    ],
)
def test_routing(run, language, requested, effective):
    run.task.write_text(f"---\naudit_mode: {requested}\naudit_target: target.{language}\n---\n")
    (run.impl / f"target.{language}").write_text("// candidate\n")
    assert proof.route(run.path)["effective"] == effective


def test_cpp_proof_request_needs_no_verus_and_uses_frozen_test_session(run, monkeypatch):
    run.task.write_text("---\naudit_mode: proof\naudit_target: target.cpp\n---\n")
    (run.impl / "target.cpp").write_text("int f() { return 1; }\n")
    monkeypatch.setattr(proof, "_tool", lambda: pytest.fail("test fallback must not invoke Verus"))
    assert proof.start(run.path)["routing"]["effective"] == "test"
    with pytest.raises(proof.ProofError, match="accepted evidence"):
        checkpoint.stamp_audit(run.path, [])
    proof.finish_test(run.path)
    checkpoint.stamp_audit(run.path, [])
    assert proof.evidence(run.path)["mode"] == "test"
    (run.impl / "target.cpp").write_text("int f() { return 2; }\n")
    with pytest.raises(proof.ProofError, match="stale"):
        proof.finish_test(run.path)


def test_proof_mode_cannot_fall_back_when_spec_or_verifier_missing(run, monkeypatch):
    run.verus_config.unlink()
    result = proof.start(run.path)
    assert result["status"] == "blocked"
    assert result["routing"]["effective"] == "proof"
    with pytest.raises(proof.ProofError, match="cannot complete"):
        proof.finish_test(run.path)
    with pytest.raises(proof.ProofError):
        checkpoint.stamp_audit(run.path, [])


@pytest.mark.parametrize("which", ["implementation", "specification", "snapshot"])
def test_frozen_input_edits_are_refused(run, verus, which):
    proof.start(run.path)
    session, _ = proof._session(run)
    path = {
        "implementation": run.impl / "counter.rs",
        "specification": run.specification / "contract.rs",
        "snapshot": session / "inputs/impl/counter.rs",
    }[which]
    path.chmod(0o644)
    path.write_text(path.read_text() + "\n// changed\n")
    with pytest.raises(proof.ProofError, match="modified|stale"):
        proof.check(run.path)


@pytest.mark.parametrize(
    "text",
    [
        "invariant true { i = n; }",
        "invariant true; return n;",
        "invariant assume(false),",
    ],
)
def test_overlay_cannot_edit_executable_code_or_assume_the_answer(run, verus, text):
    proof.start(run.path)
    invariant(run, text)
    result = proof.check(run.path)
    assert result["status"] == "invalid", result
    assert proof.status(run.path)["attempts"] == 1


def test_workspace_cannot_replace_contracts(run, verus):
    proof.start(run.path)
    path, source = overlay_path(run)
    body = next(i for i, t in enumerate(tokens(source)) if t.text == "ensures")
    malicious = {"insertions": [{"before": body, "kind": "invariant", "text": "invariant true,"}]}
    with pytest.raises(SourceError):
        apply_overlay(source, malicious)


@pytest.mark.parametrize("legacy", [False, True])
def test_review_persists_and_proof_continues_past_old_budget(run, verus, legacy):
    cfg = json.loads(run.verus_config.read_text())
    cfg.pop("max_attempts", None)
    cfg.pop("attempts_per_review", None)
    cfg["max_attempts" if legacy else "attempts_per_review"] = 2
    run.verus_config.write_text(json.dumps(cfg))
    proof.start(run.path)
    session, manifest = proof._session(run)
    if legacy:
        # An already-frozen session must resume without rewriting inputs or losing accounting.
        manifest["config"]["max_attempts"] = manifest["config"].pop("attempts_per_review")
        manifest["manifest_hash"] = proof._hash({k: v for k, v in manifest.items() if k != "manifest_hash"})
        proof._write(session / "manifest.json", manifest)
    assert proof.check(run.path)["status"] == "incomplete"
    with pytest.raises(proof.ProofError, match="unchanged"):
        proof.check(run.path)
    assert proof.start(run.path)["attempts"] == 1
    invariant(run, "invariant true,")
    second = proof.check(run.path)
    assert second["attempts"] == 2 and second["next_action"] == "revise proof strategy"
    assert not (run.audit / "completed.json").exists()
    assert proof.start(run.path)["attempts"] == 2
    invariant(run)
    with pytest.raises(proof.ProofError, match="review due"):
        proof.check(run.path)
    (session / "proof/plan.md").write_text("Use a bounded counter invariant and decreasing measure.")
    passed = proof.check(run.path)
    assert passed["status"] == "verified" and passed["attempts"] == 3
    assert passed["reviewed_at_attempt"] == 2
    assert proof.completion_allows_planning(run.path, proof._read(run.audit / "completed.json"))


def test_feedback_preserves_main_plan_and_carries_versioned_diagnostics(run, verus):
    run.plan.write_text("original design\n")
    proof.start(run.path)
    proof.check(run.path)
    result = proof.feedback(run.path, "planner", "need an implementation decision")
    assert result["bindings"] == proof._bindings(run)
    assert result["classification"] == "request"
    assert not result["main_plan_updated"]
    assert run.plan.read_text() == "original design\n"
    assert not (run.audit / "completed.json").exists()


def test_unsafe_is_not_automatically_rejected():
    from skydiscover.synthesize.spec.proof_source import check_source

    assert check_source("unsafe { *p }")


def configure_target(run, files, entry, function, ensures, obligation_file=None):
    run.task.write_text(f"---\naudit_mode: proof\naudit_target: {entry}\n---\n")
    (run.impl / "counter.rs").unlink()
    for name, source in files.items():
        path = run.impl / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)
    cfg = json.loads(run.verus_config.read_text())
    obligation = {"id": "required", "function": function, "ensures": ensures}
    if obligation_file:
        obligation["file"] = obligation_file
    cfg.update(entry=entry, obligations=[obligation])
    run.verus_config.write_text(json.dumps(cfg))


@pytest.mark.parametrize("tuple_return", [False, True])
def test_checked_helper_summary_exposes_result_without_changing_execution(run, verus, tuple_return):
    return_type = "(u64, bool)" if tuple_return else "u64"
    value = "(n, true)" if tuple_return else "n"
    access = ".0" if tuple_return else ""
    configure_target(run, {"lib.rs": f"""
use vstd::prelude::*;
verus! {{
fn helper(n: u64) -> {return_type} {{ {value} }}
pub fn count(n: u64) -> u64 {{ helper(n){access} }}
}}
"""}, "lib.rs", "count", "result == n")
    proof.start(run.path)
    assert proof.check(run.path)["status"] == "incomplete"
    frozen = proof._bindings(run)
    path, source = overlay_path(run)
    from skydiscover.synthesize.spec.proof_source import functions

    before = functions(source)["helper"][1]
    def summary(expression):
        path.write_text(json.dumps({"insertions": [{
            "before": before, "kind": "helper_ensures", "text": "ensures " + expression + ",",
        }]}))

    # A summary is checked against its actual body, never assumed.
    summary(f"audit_result{access} != n")
    assert proof.check(run.path)["status"] == "incomplete"
    summary(f"audit_result{access} == n")
    assert proof.check(run.path)["status"] == "verified"
    assert proof.replay(run.path)["obligations"] == ["required"]
    assert proof._bindings(run) == frozen


@pytest.mark.parametrize("text", ["requires false, ensures true,", "ensures true, requires false,",
                                 "ensures true, { return 0; }", "ensures assume(false),"])
def test_helper_summary_cannot_add_preconditions_or_replace_code(text):
    from skydiscover.synthesize.spec.proof_source import functions

    source = "verus! { fn helper(n: u64) -> u64 { n } }"
    with pytest.raises(SourceError):
        apply_overlay(source, {"insertions": [{
            "before": functions(source)["helper"][1], "kind": "helper_ensures", "text": text,
        }]})


@pytest.mark.parametrize("target", ["obligation", "helper", "helper_with_contract", "loop"])
def test_checked_termination_annotations_verify_and_replay(run, verus, target):
    from skydiscover.synthesize.spec.proof_source import functions

    recursive = "if n == 0 { 0 } else { helper(n - 1) }"
    if target == "obligation":
        source = "pub fn count(n: u64) -> u64 { " + recursive.replace("helper", "count") + " }"
    elif target == "loop":
        source = "pub fn count(n: u64) -> u64 { let mut i = n; while i > 0 { i = i - 1; } 0 }"
    else:
        contract = "ensures answer == 0," if target == "helper_with_contract" else ""
        returned = "(answer: u64)" if contract else "u64"
        source = (
            f"fn helper(n: u64) -> {returned} {contract} {{ {recursive} }}\n"
            "pub fn count(n: u64) -> u64 { helper(n) }"
        )
    configure_target(run, {"lib.rs": "use vstd::prelude::*; verus! {" + source + "}"},
                     "lib.rs", "count", "result == 0")
    proof.start(run.path)
    assert proof.check(run.path)["status"] == "incomplete"
    frozen = proof._bindings(run)
    path, base = overlay_path(run)
    if target == "loop":
        ts = tokens(base)
        start = next(i for i, t in enumerate(ts) if t.text == "while")
        before = next(i for i in range(start, len(ts)) if ts[i].text == "{")
        metric = "i"
    else:
        before = functions(base)["count" if target == "obligation" else "helper"][1]
        metric = "n"
    kind = "helper_ensures" if target == "helper" else "decreases"
    prefix = "ensures audit_result == 0, " if target == "helper" else ""
    def write_metric(value):
        path.write_text(json.dumps({"insertions": [{
            "before": before, "kind": kind, "text": prefix + f"decreases {value},",
        }]}))

    write_metric("0")  # An admitted annotation must still prove strict decrease.
    assert proof.check(run.path)["status"] == "incomplete"
    write_metric(metric)
    result = proof.check(run.path)
    assert result["status"] == "verified", result
    assert proof.replay(run.path)["obligations"] == ["required"]
    assert proof._bindings(run) == frozen


@pytest.mark.parametrize("kind,prefix", [("decreases", ""), ("helper_ensures", "ensures true, ")])
@pytest.mark.parametrize("text", [
    "decreases", "decreases ,", "decreases n, requires false,",
    "decreases n, ensures false,", "decreases n when false,",
    "decreases n, decreases_when false,", "decreases n via trick,",
    "decreases n, decreases_by trick,", "decreases n, { return 0; }",
    "decreases n, fn replacement() {}", "decreases assume(false),",
    "decreases n, #[verifier::exec_allows_no_decreases_clause]",
])
def test_termination_annotation_cannot_weaken_contract_or_disable_checks(kind, prefix, text):
    from skydiscover.synthesize.spec.proof_source import functions

    source = "verus! { fn helper(n: u64) -> u64 { n } }"
    with pytest.raises(SourceError):
        apply_overlay(source, {"insertions": [{
            "before": functions(source)["helper"][1], "kind": kind, "text": prefix + text,
        }]})


def test_function_termination_preserves_contracts_and_accepts_lexicographic_metrics():
    from skydiscover.synthesize.spec.proof_source import functions

    source = "verus! { fn count(n: u64, m: u64) -> (r: u64) requires n > 0, ensures r == n, { n } }"
    before = functions(source)["count"][1]
    result = apply_overlay(source, {"insertions": [{
        "before": before, "kind": "decreases", "text": "decreases n, m,",
    }]}, protected_functions=("count",))
    assert result.replace("\ndecreases n, m,\n", "") == source
    existing = source.replace("{ n }", "decreases n, { n }")
    with pytest.raises(SourceError, match="existing termination"):
        apply_overlay(existing, {"insertions": [{
            "before": functions(existing)["count"][1], "kind": "decreases", "text": "decreases m,",
        }]})


def test_termination_annotation_cannot_target_arbitrary_block():
    source = "verus! { fn helper() { if true { } } }"
    ts = tokens(source)
    before = next(i for i, t in enumerate(ts) if t.text == "true") + 1
    with pytest.raises(SourceError, match="loop body"):
        apply_overlay(source, {"insertions": [{
            "before": before, "kind": "decreases", "text": "decreases 0,",
        }]})


@pytest.mark.parametrize("contract", ["", "ensures result == n,"])
def test_helper_summary_cannot_change_frozen_contract(contract):
    from skydiscover.synthesize.spec.proof_source import functions

    source = f"verus! {{ fn count(n: u64) -> (result: u64) {contract} {{ n }} }}"
    overlay = {"insertions": [{"before": functions(source)["count"][1],
                               "kind": "helper_ensures", "text": "ensures true,"}]}
    with pytest.raises(SourceError, match="authoritative"):
        apply_overlay(source, overlay, protected_functions=("count",))
    if contract:
        with pytest.raises(SourceError, match="existing function contract"):
            apply_overlay(source, overlay)


@pytest.mark.parametrize(
    "source,function,ensures",
    [
        (
            "pub mod inner { pub struct Counter { pub n:u64 } impl Counter { pub fn get(&self)->u64 {self.n} } }",
            "inner::Counter::get",
            "result == self.n",
        ),
        ("pub fn identity<T>(x: T) -> T { x }", "identity", "result == x"),
        (
            "pub trait Read { fn get(&self)->u64; } pub struct Counter {pub n:u64} impl Read for Counter {fn get(&self)->u64 {self.n}}",
            "Counter::get",
            "result == self.n",
        ),
        (
            "#[derive(Copy, Clone)] pub struct Counter { pub n:u64 } impl Counter { pub fn get(&self)->u64 {self.n} }",
            "Counter::get",
            "result == self.n",
        ),
    ],
)
def test_rust_shapes_enter_and_can_pass_verification(run, verus, source, function, ensures):
    configure_target(run, {"lib.rs": source}, "lib.rs", function, ensures)
    assert proof.start(run.path)["status"] == "ready"
    result = proof.check(run.path)
    assert result["status"] == "verified", result
    assert proof.replay(run.path)["obligations"] == ["required"]


def test_multifile_module_proof_repair(run, verus):
    configure_target(
        run,
        {
            "lib.rs": "pub mod counter;",
            "counter.rs": "pub fn count(n:u64)->u64 { let mut i:u64=0; while i<n {i=i+1;} i }",
        },
        "lib.rs",
        "counter::count",
        "result == n",
        "counter.rs",
    )
    proof.start(run.path)
    first = proof.check(run.path)
    assert first["status"] == "incomplete", first
    session, manifest = proof._session(run)
    views, issues = proof._views(session, manifest, {"insertions": []})
    assert not issues
    ts = tokens(views["counter.rs"])
    loop = next(i for i, t in enumerate(ts) if t.text == "while")
    before = next(i for i in range(loop, len(ts)) if ts[i].text == "{")
    (session / "proof/overlay.json").write_text(
        json.dumps(
            {
                "insertions": [
                    {
                        "file": "counter.rs",
                        "before": before,
                        "kind": "invariant",
                        "text": "invariant i <= n, decreases n - i,",
                    }
                ]
            }
        )
    )
    result = proof.check(run.path)
    assert result["status"] == "verified", result
    assert proof.replay(run.path)["obligations"] == ["required"]


@pytest.mark.parametrize(
    "source,function",
    [
        ("pub unsafe fn read(p:*const u64)->u64 { unsafe { *p } }", "read"),
        ("macro_rules! make { () => { pub fn generated()->u64 {1} } } make!();", "generated"),
        ("use missing_dependency::Value; pub fn read(v:Value)->u64 {v.get()}", "read"),
        ("pub fn broken( {", "broken"),
    ],
)
def test_unhandled_source_still_reaches_real_verus(run, verus, source, function):
    configure_target(run, {"lib.rs": source}, "lib.rs", function, "true")
    assert proof.start(run.path)["status"] == "ready"
    result = proof.check(run.path)
    session, _ = proof._session(run)
    invocation = session / "attempts/0001/verification/invocation.json"
    assert invocation.is_file(), result
    diagnostic = json.loads(invocation.read_text())
    assert diagnostic["stderr"] or diagnostic["stdout"]
    assert result["status"] != "verified"


def test_cargo_project_uses_native_verus_build(run, verus):
    configure_target(
        run,
        {
            "Cargo.toml": '[package]\nname="counter-project"\nversion="0.1.0"\nedition="2021"\n',
            "Cargo.lock": 'version = 4\n[[package]]\nname="counter-project"\nversion="0.1.0"\n',
            "src/lib.rs": "pub mod counter;",
            "src/counter.rs": "pub struct Counter {pub n:u64} impl Counter {pub fn get(&self)->u64 {self.n}}",
        },
        "src/lib.rs",
        "counter::Counter::get",
        "result == self.n",
        "src/counter.rs",
    )
    assert proof.start(run.path)["status"] == "ready"
    frozen = proof._bindings(run)
    result = proof.check(run.path)
    session, _ = proof._session(run)
    invocation = json.loads((session / "attempts/0001/verification/invocation.json").read_text())
    assert "cargo-verus" in invocation["command"][0]
    assert result["status"] == "verified", (result, invocation["stderr"])
    assert proof._bindings(run) == frozen
    assert proof.replay(run.path)["obligations"] == ["required"]


def test_cargo_dependency_and_build_script_are_attempted_together(run, verus):
    configure_target(
        run,
        {
            "Cargo.toml": '[package]\nname="counter-project"\nversion="0.1.0"\nedition="2021"\n[dependencies]\nhelper={path="helper"}\n',
            "Cargo.lock": 'version=4\n[[package]]\nname="counter-project"\nversion="0.1.0"\ndependencies=["helper"]\n[[package]]\nname="helper"\nversion="0.1.0"\n',
            "build.rs": 'fn main() { let p = std::env::var("OUT_DIR").unwrap(); std::fs::write(std::path::Path::new(&p).join("marker"), "built").unwrap(); }',
            "helper/Cargo.toml": '[package]\nname="helper"\nversion="0.1.0"\nedition="2021"\n',
            "helper/src/lib.rs": "use vstd::prelude::*; verus! { pub fn identity(n:u64)->(r:u64) ensures r == n {n} }",
            "src/lib.rs": "pub fn identity(n:u64)->u64 { helper::identity(n) }",
        },
        "src/lib.rs",
        "identity",
        "result == n",
    )
    assert proof.start(run.path)["status"] == "ready"
    result = proof.check(run.path)
    session, _ = proof._session(run)
    invocation = json.loads((session / "attempts/0001/verification/invocation.json").read_text())
    assert result["status"] == "verified", (result, invocation["stderr"])
    assert proof.replay(run.path)["obligations"] == ["required"]


def test_source_loading_outside_snapshot_is_attempted_but_not_accepted(run, verus, tmp_path):
    outside = tmp_path / "outside.rs"
    outside.write_text("pub fn unused() {}")
    configure_target(
        run,
        {"lib.rs": f"#[path={json.dumps(str(outside))}] mod outside; pub fn f()->u64 {{1}}"},
        "lib.rs",
        "f",
        "result == 1",
    )
    assert proof.start(run.path)["status"] == "ready"
    result = proof.check(run.path)
    session, _ = proof._session(run)
    assert (session / "attempts/0001/verification/invocation.json").is_file()
    assert result["status"] != "verified"
    assert any("outside frozen" in issue for issue in result["preparation_issues"])


@pytest.mark.parametrize("changed", ["implementation", "specification"])
def test_verifier_cannot_certify_modified_generated_inputs(run, verus, monkeypatch, changed):
    proof.start(run.path)
    invariant(run)
    real_execute = proof._execute

    def execute(command, cwd, timeout):
        result = real_execute(command, cwd, timeout)
        if "--version" not in command:
            if changed == "implementation":
                (cwd / "counter.rs").write_text("pub fn count(n:u64)->u64 {0}")
            else:
                spec = cwd.parent / "specification/contract.rs"
                spec.chmod(0o644)
                spec.write_text("// changed contract")
        return result

    monkeypatch.setattr(proof, "_execute", execute)
    result = proof.check(run.path)
    assert result["status"] == "incomplete"
    assert any("was lost" in issue for issue in result["preparation_issues"])


def test_wrong_implementation_returns_to_outer_planner(run, verus):
    from skydiscover.synthesize.spec import iterations
    from skydiscover.synthesize.workflow.hooks.loop_guard import last_audit_finished

    iterations.close(run.path, "failed", reason="counterexample")
    source = run.impl / "counter.rs"
    source.write_text(source.read_text().replace("    i\n", "    0\n"))
    proof.start(run.path)
    invariant(run)
    result = proof.check(run.path)
    assert result["status"] == "incomplete"
    frozen = proof._bindings(run)
    blocker = {
        "diagnostic": result["diagnostics"],
        "implementation_change": "Return the counter i instead of the literal zero.",
        "proof_only_analysis": "For n=1 the frozen function returns 0; no invariant or lemma "
                               "can establish result==n without changing that return expression.",
    }
    request = proof.feedback(run.path, "planner", "return value does not meet counter_returns_n",
                             blocker=blocker)
    assert request["result"]["obligations"][0]["status"] == "unresolved"
    assert request["classification"] == "implementation_blocker"
    completed = proof._read(run.audit / "completed.json")
    assert proof.completion_allows_planning(run.path, completed)
    assert last_audit_finished(run.path, 1) == 1
    for field in blocker:
        with pytest.raises(proof.ProofError, match="blocker|diagnostic"):
            proof.feedback(run.path, "planner", "incomplete blocker",
                           blocker={**blocker, field: ""})
    with pytest.raises(proof.ProofError, match="latest attempt"):
        proof.feedback(run.path, "planner", "wrong diagnostic",
                       blocker={**blocker, "diagnostic": "../diagnostics.txt"})
    with pytest.raises(proof.ProofError, match="planner owner"):
        proof.feedback(run.path, "spec-builder", "spec ambiguity", blocker=blocker)
    assert proof._bindings(run) == frozen
    with pytest.raises(proof.ProofError):
        checkpoint.stamp_audit(run.path, [])
    session, _ = proof._session(run)
    (session / result["diagnostics"]).write_text("replaced diagnostics")
    assert not proof.completion_allows_planning(run.path, completed)
    assert last_audit_finished(run.path, 1) == 0


@pytest.mark.parametrize("outcome", ["incomplete", "unsupported", "blocked", "tool_error", "invalid"])
def test_unsuccessful_checks_never_release_outer_loop(run, verus, monkeypatch, outcome):
    proof.start(run.path)
    monkeypatch.setattr(proof, "_verify", lambda *args: {
        "status": outcome, "message": "diagnose this failure", "verified_count": 0,
    })
    assert proof.check(run.path)["status"] == outcome
    assert not (run.audit / "completed.json").exists()
    proof.feedback(run.path, "planner", "still working")
    assert not (run.audit / "completed.json").exists()


def test_changed_toolchain_cannot_reuse_or_replace_session_binding(run, verus, monkeypatch):
    proof.start(run.path)
    invariant(run)
    real_tool = proof._tool
    binary, identity = real_tool()
    monkeypatch.setattr(proof, "_tool", lambda: (binary, {**identity, "solver": "different"}))
    assert proof.check(run.path)["status"] == "blocked"
    assert proof.start(run.path)["status"] == "blocked"
    monkeypatch.setattr(proof, "_tool", real_tool)
    recovered = proof.check(run.path)
    assert recovered["status"] == "verified" and recovered["attempts"] == 2


def test_stalled_strategy_must_be_revised(run, verus):
    cfg = json.loads(run.verus_config.read_text())
    cfg["max_stalled"] = 1
    run.verus_config.write_text(json.dumps(cfg))
    proof.start(run.path)
    proof.check(run.path)
    invariant(run)
    with pytest.raises(proof.ProofError, match="planner must revise"):
        proof.check(run.path)
    session, _ = proof._session(run)
    (session / "proof/plan.md").write_text(
        "Add a bounded counter invariant and decreasing measure.\n"
    )
    assert proof.check(run.path)["status"] == "verified"


def test_codex_generator_installs_proof_roles(tmp_path):
    import subprocess
    import sys

    installer = EXAMPLE.parents[1] / "workflow/adapters/codex/agents.py"
    target = tmp_path / "codex"
    subprocess.run([sys.executable, str(installer), str(target), "--quiet"], check=True)
    for name in ("proof-planner", "proof-coder"):
        text = (target / "agents" / f"{name}.toml").read_text()
        assert "developer_instructions" in text and "frozen" in text.lower()


def test_lexer_ignores_braces_in_strings_and_nested_comments():
    source = 'proof { let s = r##"} assume(false)"##; /* { /* } */ } */ assert(true); }'
    assert tokens(source)[-1].text == "}"


def test_missing_verifier_can_recover_without_new_session(run, verus, monkeypatch):
    real_tool = proof._tool

    def missing():
        raise proof.ProofError("missing verifier")

    monkeypatch.setattr(proof, "_tool", missing)
    first = proof.start(run.path)
    assert first["status"] == "blocked"
    monkeypatch.setattr(proof, "_tool", real_tool)
    recovered = proof.start(run.path)
    assert recovered["session"] == first["session"]
    assert recovered["status"] == "ready" and recovered["attempts"] == 0


@pytest.mark.parametrize(
    "output,stderr,code,timeout,expected",
    [
        ("", "", -9, True, "incomplete"),
        ("", "verifier crashed", 1, False, "tool_error"),
        ("", "error: this pointer operation is not supported", 1, False, "unsupported"),
        ('{"verification-results":{"verified":"wrong"}}', "", 0, False, "tool_error"),
        (
            '{"verification-results":{"verified":99,"success":true,"errors":0,"is-verifying-entire-crate":true}}',
            "",
            0,
            False,
            "incomplete",
        ),
    ],
)
def test_verifier_failures_never_create_accepted_evidence(
    run, verus, monkeypatch, output, stderr, code, timeout, expected
):
    proof.start(run.path)
    real_execute = proof._execute

    def execute(command, cwd, limit):
        if "--version" in command:
            return real_execute(command, cwd, limit)
        return {"stdout": output, "stderr": stderr, "returncode": code, "timed_out": timeout}

    monkeypatch.setattr(proof, "_execute", execute)
    result = proof.check(run.path)
    assert result["status"] == expected, result
    assert result["attempts"] == 1
    with pytest.raises(proof.ProofError, match="accepted evidence"):
        proof.evidence(run.path)


def test_interrupted_attempt_spends_budget_and_resumes(run, verus, monkeypatch):
    proof.start(run.path)
    real_verify = proof._verify

    def interrupted(*args):
        raise KeyboardInterrupt

    monkeypatch.setattr(proof, "_verify", interrupted)
    with pytest.raises(KeyboardInterrupt):
        proof.check(run.path)
    assert proof.start(run.path)["attempts"] == 1
    monkeypatch.setattr(proof, "_verify", real_verify)
    invariant(run)
    assert proof.check(run.path)["attempts"] == 2
    assert proof.evidence(run.path)["mode"] == "proof"


def test_release_replays_proof_and_repeated_export_refreshes_frozen_inputs(run, verus, tmp_path):
    from skydiscover.synthesize.spec import run as delivery
    from skydiscover.synthesize.spec.findings import Finding, Findings

    # A separate smoke check remains part of the existing delivery pipeline; Verus supplies
    # correctness evidence. The test must exercise the actual export subprocess, not a mock.
    run.tests.mkdir(parents=True, exist_ok=True)
    (run.tests / "check_target.sh").write_text('test -f "$SKYDISCOVER_IMPL"\n')
    run.test_script.write_text("bash check_target.sh\n")
    Findings(str(run.decision_log), track_severity=True).add(
        Finding(
            id="counter",
            title="counter contract restored",
            kind="spec",
            detail="returns n",
            severity="defect",
            status="waived",
            answered_by="ai",
            proof_obligation="counter_returns_n",
        )
    )
    run.bench.mkdir(parents=True, exist_ok=True)
    run.leaderboard.write_text(
        json.dumps(
            [
                {
                    "impl": "synthesis/impl/counter.rs",
                    "metrics": {"throughput": 1},
                    "config": {},
                    "input_digests": checkpoint.input_digests(run.path),
                }
            ]
        )
    )
    proof.start(run.path)
    invariant(run)
    assert proof.check(run.path)["status"] == "verified"
    checkpoint.stamp_audit(run.path, [])
    out, _ = checkpoint.snapshot_run(run.path, tmp_path / "exports", became_best=True)
    delivery._verify_export(out / "best", run)
    delivery._verify_export(out / "best", run)
    copied = Run(out / "best/.verification/run")
    assert proof.replay(copied.path)["mode"] == "proof"
    session, _ = proof._session(copied)
    accepted = session / "attempts/0001/overlay.json"
    accepted.write_text('{"insertions": []}')
    with pytest.raises(proof.ProofError, match="replay failed"):
        proof.replay(copied.path)
