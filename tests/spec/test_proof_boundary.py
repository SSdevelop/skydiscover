"""Correspondence regressions; successful proofs are also exercised against real Verus."""
import pytest

from skydiscover.synthesize.spec.proof_boundary import apply_source
from skydiscover.synthesize.spec.proof_source import SourceError


def test_private_contracts_and_struct_literal_headers():
    source = '''verus! {
fn helper(n: u64) -> (r: u64)
    requires (Predicate { limit: 10 }).inv(n),
    ensures r <= n,
    decreases n,
{ n }
}'''
    annotated = source.replace('ensures r <= n,', 'ensures r == n,').replace('limit: 10', 'limit: 20')
    assert apply_source(source, annotated) == annotated


@pytest.mark.parametrize('edit', [
    lambda s: s.replace('{ n }', '{ 0 }'),
    lambda s: s.replace('n: u64', 'n: u32'),
    lambda s: s.replace('ensures r == n', 'ensures true'),
    lambda s: s.replace('requires n < 10', 'requires n < 5'),
    lambda s: s.replace('fn f', '#[verifier::external_body] fn f'),
    lambda s: 'use other::*; ' + s,
])
def test_authoritative_and_executable_boundary(edit):
    source = 'verus! { fn f(n: u64) -> (r: u64) requires n < 10, ensures r == n, { n } }'
    with pytest.raises(SourceError, match='boundary'):
        apply_source(source, edit(source), protected=['f'])


def test_loop_annotations_can_be_replaced_before_existing_decreases():
    source = 'verus! { fn f(n: u64) { let mut i = 0u64; while i < n invariant i <= n, decreases n-i, { i = i + 1; } } }'
    annotated = source.replace('invariant i <= n,', 'invariant i <= n, i >= 0,')
    assert apply_source(source, annotated) == annotated
    with pytest.raises(SourceError):
        apply_source(source, annotated.replace('i < n invariant', 'i <= n invariant'))


def test_internal_predicate_edit_and_protected_transitive_meaning():
    source = '''verus! {
closed spec fn internal(n: int) -> bool { n >= 0 }
closed spec fn intermediate(n: int) -> bool { internal(n) }
pub closed spec fn identity(n: int) -> bool { intermediate(n) }
}'''
    with pytest.raises(SourceError, match='authoritative contract'):
        apply_source(source, source.replace('n >= 0', 'true'))
    source = source[:source.index('pub closed')] + '}'
    assert 'true' in apply_source(source, source.replace('n >= 0', 'true'))
    with pytest.raises(SourceError, match='authoritative contract'):
        apply_source(source, source.replace('n >= 0', 'true'), roots=['intermediate'])
    with pytest.raises(SourceError, match='authoritative contract'):
        apply_source(source, source.replace('n >= 0', 'true'), context=[
            'verus! { pub closed spec fn identity(n: int) -> bool { intermediate(n) } }'
        ])


@pytest.mark.parametrize('body', [
    'assume(false);', 'admit();', 'r#admit();',
    '#[verifier::external_body] proof fn audit_evil() ensures false {}',
    '#[verifier::assume_termination] proof fn audit_evil() {}',
    'evil!();', 'fake::seq![];', 'unsafe { evil(); }',
    '#[verifier::custom_req_err("x")] assert(true);',
])
def test_no_trust_escape_in_erased_proof(body):
    source = 'verus! { fn f() {} }'
    with pytest.raises(SourceError):
        apply_source(source, 'verus! { fn f() { proof { ' + body + ' } } }')


def test_fresh_lemmas_and_trusted_macros_with_unicode():
    source = '// λ\nverus! { fn f() {} }'
    annotation = '''proof fn audit_lemma() {
        assert(seq![1int].len() == 1);
        assert forall|x: int| #[trigger] (x + 1) == x + 1 by {};
    }'''
    result = apply_source(source, source[:-1] + annotation + '}')
    assert '::__audit_atomic_vstd::seq!' in result
    # A Unicode comment on the same line exercises parser byte offsets too.
    result = apply_source(source, '// λ verus\nverus! { fn f() { /* λ */ proof { assert(true); } } }')
    assert 'assert(true)' in result


@pytest.mark.parametrize('macro,typename,method', [
    ('codec_contract', 'Decoder', 'decode'),
    ('queue_contract', 'Mailbox', 'receive'),
    ('numeric_contract', 'Accumulator', 'sum'),
])
def test_macro_bodies_keep_their_frozen_binding(macro, typename, method):
    source = ('crate::MACRO! { impl TYPE { METHOD(k) { verus_exec_expr! {{ k }} } } }'
              .replace('MACRO', macro).replace('TYPE', typename).replace('METHOD', method))
    annotated = source.replace('{{ k }}', '{{ proof { assert(true); } k }}')
    assert apply_source(source, annotated, macros=[macro]) == annotated
    with pytest.raises(SourceError):
        apply_source(source, annotated.replace(f'{method}(k)', f'{method}(other)'), macros=[macro])


def test_ghost_and_tracked_state_is_proof_only():
    source = 'verus! { fn f() { let n = 1u64; } }'
    annotated = source.replace('let n', 'let ghost audit_x = 3int; let tracked audit_y = (); proof { assert(audit_x == 3); } let n')
    assert apply_source(source, annotated) == annotated
    with pytest.raises(SourceError):
        apply_source(source, annotated.replace('let n = 1', 'let n = 2'))


def test_frozen_spec_macros_are_not_rebound():
    source = 'verus! { pub closed spec fn identity() -> Seq<int> { seq![1int] } fn f() {} }'
    annotated = source.replace('fn f() {}', 'fn f() { proof { assert(true); } }')
    assert apply_source(source, annotated) == annotated


def test_adding_shadowing_helper_in_new_scope_is_rejected():
    source = 'verus! { mod m { spec fn value() -> bool { true } } fn f() {} }'
    with pytest.raises(SourceError, match='fresh audit_'):
        apply_source(source, source.replace('fn f() {}', 'fn f() { spec fn value() -> bool { false } }'))


@pytest.mark.parametrize('declaration', [
    'fn f(n: u64) decreases n when false {}',
    'proof fn audit_f(n: nat) decreases n when false {}',
])
def test_conditional_termination_cannot_disable_checks(declaration):
    source = 'verus! { fn f(n: u64) {} }'
    candidate = 'verus! { ' + (declaration if declaration.startswith('fn') else 'fn f(n: u64) {} ' + declaration) + ' }'
    with pytest.raises(SourceError, match='conditional termination'):
        apply_source(source, candidate)
