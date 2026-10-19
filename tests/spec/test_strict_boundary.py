"""Strict immutable-spec regressions, including aliases and name binding changes."""
import pytest

from skydiscover.synthesize.spec.proof_boundary import apply_source
from skydiscover.synthesize.spec.proof_source import SourceError


@pytest.mark.parametrize('definition,reference,imports', [
    ('valid', 'checked', 'use self::valid as checked;'),
    ('r#valid', 'checked', 'use self::r#valid as checked;'),
    ('valid', 'r#checked', 'use self::valid as r#checked;'),
    ('valid', 'checked', 'use self::{valid as checked};'),
    ('valid', 'checked_again', 'use self::valid as checked; use self::checked as checked_again;'),
    ('valid', 'checked', 'pub use self::valid as checked;'),
])
@pytest.mark.parametrize('edit', ['weaken', 'delete', 'rename'])
def test_alias_never_exempts_an_original_spec(definition, reference, imports, edit):
    predicate = f'closed spec fn {definition}(n: int, r: int) -> bool {{ r == n }}'
    source = imports + 'verus! { ' + predicate + f' fn count(n: int) -> (r: int) ensures {reference}(n, r), {{ n }} }}'
    candidate = {'weaken': source.replace('r == n', 'true'),
                 'delete': source.replace(predicate, ''),
                 'rename': source.replace('spec fn ' + definition, 'spec fn audit_other')}[edit]
    with pytest.raises(SourceError, match='authoritative contract'):
        apply_source(source, candidate, protected=['count'])


@pytest.mark.parametrize('reference', ['model::valid', 'checked::valid', 'valid'])
def test_module_alias_and_glob_cannot_hide_a_dependency(reference):
    source = '''use self::model as checked; use self::model::*;
verus! { mod model { pub closed spec fn valid() -> bool { false } }
fn f() ensures REF(), {} }'''.replace('REF', reference)
    with pytest.raises(SourceError, match='authoritative contract'):
        apply_source(source, source.replace('{ false }', '{ true }'), protected=['f'])


def test_cross_file_alias_protects_dependency_but_unreferenced_spec_can_evolve():
    source = 'verus! { closed spec fn valid() -> bool { false } }'
    context = ['use model::valid as checked; verus! { fn f() ensures checked(), {} }']
    assert apply_source(source, source.replace('false', 'true'))
    with pytest.raises(SourceError, match='authoritative contract'):
        apply_source(source, source.replace('false', 'true'), context=context, protected=['f'])


def test_same_basename_in_different_modules_is_not_confused():
    source = '''verus! {
mod a { spec fn valid() -> bool { true } }
mod b { spec fn valid() -> bool { false } }
fn f() {} }'''
    assert apply_source(source, source.replace('fn f() {}', 'fn f() { proof { assert(true); } }'))
    assert apply_source(source, source.replace('{ false }', '{ true }'))


@pytest.mark.parametrize('extra', [
    'use other::valid;', 'use other::*;',
    'spec fn audit_alias() -> bool { true }',
    'spec fn r#audit_alias() -> bool { true }',
])
def test_new_import_or_helper_cannot_rebind_existing_alias(extra):
    source = 'use self::valid as audit_alias; verus! { spec fn valid() -> bool { false } fn f() ensures audit_alias(), {} }'
    with pytest.raises(SourceError):
        apply_source(source, source[:-1] + extra + '}', protected=['f'])


@pytest.mark.parametrize('original', [
    'use model::*; verus! { fn f() ensures audit_valid(), {} }',
    'verus! { fn f() {} }',
])
def test_new_helper_respects_frozen_context_and_qualified_root(original):
    candidate = original[:-1] + 'spec fn audit_valid() -> bool { true } }'
    for options in [dict(context=['verus! { fn g() ensures audit_valid(), {} }']),
                    dict(roots=['model::r#audit_valid'])]:
        with pytest.raises(SourceError, match='fresh audit_'):
            apply_source(original, candidate, **options)


def test_private_spec_macros_are_not_rebound():
    source = 'verus! { closed spec fn valid() -> Seq<int> { seq![1int] } fn f() {} }'
    candidate = source.replace('fn f() {}', 'fn f() { proof { assert(true); } }')
    assert apply_source(source, candidate) == candidate


@pytest.mark.parametrize('contract', ['requires n < 10,', 'ensures r <= n,', 'recommends n < 10,'])
def test_existing_private_contract_can_be_rewritten(contract):
    source = f'verus! {{ fn helper(n: u64) -> (r: u64) {contract} {{ n }} }}'
    assert apply_source(source, source.replace(contract, contract.split()[0] + ' true,'))


def test_private_summary_and_preconditions_can_be_added():
    source = 'verus! { fn helper(n: u64) -> u64 { n } }'
    candidate = source.replace('-> u64', '-> (r: u64) ensures r == n, decreases n,')
    assert apply_source(source, candidate) == candidate
    assert apply_source(source, candidate.replace('ensures', 'requires n < 10, ensures'))


def test_existing_return_binding_cannot_be_renamed():
    source = 'verus! { fn helper(n: u64) -> (r: u64) ensures r == n, { n } }'
    with pytest.raises(SourceError, match='existing helper contract'):
        apply_source(source, source.replace('(r: u64)', '(x: u64)').replace('r == n', 'x == n'))


def test_internal_proof_contract_can_evolve_but_public_contract_is_frozen():
    source = 'verus! { proof fn lemma(n: nat) ensures n == n, {} }'
    candidate = source.replace('ensures n == n, {}', 'ensures n == n, decreases n, { assert(n == n); }')
    assert apply_source(source, candidate) == candidate
    assert apply_source(source, candidate.replace('ensures n == n', 'requires n > 0, ensures true'))
    with pytest.raises(SourceError, match='existing proof contract'):
        apply_source(source.replace('proof fn', 'pub proof fn'),
                     candidate.replace('proof fn', 'pub proof fn').replace('ensures n == n', 'ensures true'))


def test_new_internal_type_invariant_is_allowed_but_exposed_contract_is_frozen():
    source = 'verus! { struct Cell { value: u64 } impl Cell {} }'
    candidate = source.replace('impl Cell {}', 'impl Cell { #[verifier::type_invariant] spec fn audit_inv(self) -> bool { false } }')
    assert apply_source(source, candidate)
    with pytest.raises(SourceError, match='implicit type contract'):
        apply_source(source.replace('struct Cell', 'pub struct Cell'),
                     candidate.replace('struct Cell', 'pub struct Cell'))


@pytest.mark.parametrize('exposure', [
    'pub fn read(x: Cell) {}',
    'type Alias = Cell; pub fn read(x: Alias) {}',
    'use self::Cell as Alias; pub fn read(x: Alias) {}',
    'pub struct Wrapper { cell: Cell }',
])
def test_internal_type_invariant_cannot_change_an_exposed_alias_or_wrapper(exposure):
    source = 'verus! { struct Cell { value: u64 } impl Cell {} ' + exposure + ' }'
    candidate = source.replace('impl Cell {}', '''impl Cell {
        #[verifier::type_invariant] spec fn audit_inv(self) -> bool { false }
    }''')
    with pytest.raises(SourceError, match='implicit type contract'):
        apply_source(source, candidate)


def test_glob_reexport_chain_and_constant_do_not_hide_contract_dependencies():
    source = '''verus! {
mod model { closed spec fn valid() -> bool { false } }
mod export { use super::model::valid as checked; }
use self::export::*;
const EXPECTED: bool = checked();
fn f() ensures EXPECTED, {}
}'''
    with pytest.raises(SourceError, match='authoritative contract'):
        apply_source(source, source.replace('{ false }', '{ true }'), protected=['f'])


def test_alias_of_internal_predicate_can_evolve_when_not_authoritative():
    source = '''use self::valid as checked;
verus! { closed spec fn valid(n: int) -> bool { n >= 0 }
fn helper(n: int) requires checked(n), {} }'''
    assert apply_source(source, source.replace('n >= 0', 'n > 0'))


def test_fresh_spec_and_proof_helpers_remain_supported():
    source = 'verus! { fn f() {} }'
    candidate = source[:-1] + '''
spec fn audit_identity(n: int) -> int { n }
proof fn audit_lemma(n: int) ensures audit_identity(n) == n, { assert(audit_identity(n) == n); }
}'''
    assert apply_source(source, candidate) == candidate


@pytest.mark.parametrize('binding', ['EXPECTED', 'r#EXPECTED'])
def test_new_return_binding_cannot_shadow_an_existing_contract_name(binding):
    source = 'verus! { const EXPECTED: u64 = 1; fn helper() -> u64 returns EXPECTED, { 0 } }'
    candidate = source.replace('-> u64 returns', f'-> ({binding}: u64) ensures true, returns')
    with pytest.raises(SourceError, match='new return binding must not shadow'):
        apply_source(source, candidate)


@pytest.mark.parametrize('source', [
    'external_model!(); verus! { fn f() {} }',
    'verus! { spec fn valid() -> bool { hidden_predicate!() } fn f() {} }',
    'verus! { spec fn valid() -> bool { other::verus!() } fn f() {} }',
])
def test_opaque_macro_expansions_cannot_be_rebound_by_new_helpers(source):
    candidate = source.replace('fn f() {}', 'fn f() {} spec fn audit_new() -> bool { true }')
    with pytest.raises(SourceError, match='new name bindings across opaque macros'):
        apply_source(source, candidate)
    # Ordinary proof statements do not introduce module-level names.
    annotated = source.replace('fn f() {}', 'fn f() { proof { assert(true); } }')
    assert apply_source(source, annotated) == annotated


def test_opaque_macro_in_other_file_blocks_new_return_binding():
    source = 'verus! { fn helper(n: u64) -> u64 { n } }'
    candidate = source.replace('-> u64', '-> (r: u64) ensures r == n,')
    with pytest.raises(SourceError, match='new name bindings across opaque macros'):
        apply_source(source, candidate, context=['external_model!();'])
