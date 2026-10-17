use vstd::prelude::*;

verus! {
pub open spec fn counted(n: u64, result: u64) -> bool {
    result == n
}
}
