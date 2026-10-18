use vstd::prelude::*;
use vstd::resource::Loc;
use vstd::resource::ghost_var::GhostVar;

verus! {

// Abstract map state of a database. Candidate implementations need to establish
// the correspondence of DatabaseState and the actual storage representation
// formally!
pub tracked struct DatabaseState {
    pub tracked var: GhostVar<Map<Seq<char>, i32>>,
    pub ghost contents: Map<Seq<char>, i32>,
}

impl DatabaseState {
    // DatabaseState must be uniquely identified
    pub open spec fn linked(self, id: Loc) -> bool {
        self.var.id() == id && self.var@ == self.contents
    }
}

pub open spec fn key_lt(a: Seq<char>, b: Seq<char>) -> bool
    decreases a.len(),
{
    if b.len() == 0 {
        false
    } else if a.len() == 0 {
        true
    } else if a[0] != b[0] {
        (a[0] as u32) < (b[0] as u32)
    } else {
        key_lt(a.drop_first(), b.drop_first())
    }
}

pub open spec fn key_le(a: Seq<char>, b: Seq<char>) -> bool {
    a == b || key_lt(a, b)
}

}

// Atomic contracts don't work on traits in Verus, so use macro_rules! for providing
// general concurrent specs for db operators.
#[macro_export]
macro_rules! impl_db {
    (
        impl $database:ty {
            new() { $($new_body:tt)* }
            get($get_self:ident, $get_key:ident, $get_lp:ident) $get_body:block
            put($put_self:ident, $put_key:ident, $put_value:ident, $put_lp:ident) $put_body:block
            scan(
                $scan_self:ident,
                $scan_lo:ident,
                $scan_hi:ident,
                $scan_lp:ident
            ) $scan_body:block
            sort($sort_self:ident, $sort_lp:ident) $sort_body:block
        }
    ) => {
        verus! {

        impl $database {
            pub fn new() -> (result: (Self, Tracked<DatabaseState>))
                ensures
                    result.1@.linked(result.0.id()),
                    result.1@.contents == Map::<Seq<char>, i32>::empty(),
            {
                $($new_body)*
            }

            pub fn get(&self, $get_key: &String) -> (result: Option<i32>)
                // get_lp (as well as *_lp in other operations) are proof objects
                // that are consumed for exactly once at the linearization point of
                // the concurrent operations. The proof itself needs to establish
                // the atomic contract by explicitly leveraging get_lp at the linearization
                // point.
                atomically ($get_lp) {
                    (state: DatabaseState) -> (post: vstd::atomic::Commit<DatabaseState>),
                    requires state.linked(self.id()),
                    ensures post@.linked(self.id()) && post@.contents == state.contents,
                    outer_mask any,
                    inner_mask none,
                },
                ensures result == if state.contents.dom().contains($get_key@) {
                    Some(state.contents[$get_key@])
                } else {
                    None
                },
            {
                let $get_self = self;
                $get_body
            }

            pub fn put(
                &self,
                $put_key: String,
                $put_value: i32,
            )
                atomically ($put_lp) {
                    (state: DatabaseState) -> (post: vstd::atomic::Commit<DatabaseState>),
                    requires state.linked(self.id()),
                    ensures post@.linked(self.id()) && post@.contents
                        == state.contents.insert($put_key@, $put_value),
                    outer_mask any,
                    inner_mask none,
                },
            {
                let $put_self = self;
                $put_body
            }

            pub fn scan(
                &self,
                $scan_lo: &String,
                $scan_hi: &String,
            ) -> (result: Vec<(String, i32)>)
                atomically ($scan_lp) {
                    (state: DatabaseState) -> (post: vstd::atomic::Commit<DatabaseState>),
                    requires state.linked(self.id()),
                    ensures post@.linked(self.id()) && post@.contents == state.contents,
                    outer_mask any,
                    inner_mask none,
                },
                requires key_le($scan_lo@, $scan_hi@),
                ensures
                    forall|i: int, j: int| #![trigger result@[i], result@[j]]
                        0 <= i < j < result@.len()
                            ==> key_lt(result@[i].0@, result@[j].0@),
                    forall|i: int| #![trigger result@[i]]
                        0 <= i < result@.len()
                            ==> key_le($scan_lo@, result@[i].0@)
                                && key_le(result@[i].0@, $scan_hi@),
                    forall|i: int| #![trigger result@[i]]
                        0 <= i < result@.len()
                            ==> state.contents.dom().contains(result@[i].0@)
                                && state.contents[result@[i].0@] == result@[i].1,
                    forall|key: Seq<char>|
                        #![trigger state.contents.dom().contains(key)]
                        state.contents.dom().contains(key)
                                && key_le($scan_lo@, key)
                                && key_le(key, $scan_hi@)
                            ==> exists|i: int| #![trigger result@[i]]
                                0 <= i < result@.len() && result@[i].0@ == key,
            {
                let $scan_self = self;
                $scan_body
            }

            pub fn sort(&self) -> (result: Vec<(String, i32)>)
                atomically ($sort_lp) {
                    (state: DatabaseState) -> (post: vstd::atomic::Commit<DatabaseState>),
                    requires state.linked(self.id()),
                    ensures post@.linked(self.id()) && post@.contents == state.contents,
                    outer_mask any,
                    inner_mask none,
                },
                ensures
                    forall|i: int, j: int| #![trigger result@[i], result@[j]]
                        0 <= i < j < result@.len()
                            ==> key_lt(result@[i].0@, result@[j].0@),
                    forall|i: int| #![trigger result@[i]]
                        0 <= i < result@.len()
                            ==> state.contents.dom().contains(result@[i].0@)
                                && state.contents[result@[i].0@] == result@[i].1,
                    forall|key: Seq<char>|
                        #![trigger state.contents.dom().contains(key)]
                        state.contents.dom().contains(key)
                            ==> exists|i: int| #![trigger result@[i]]
                                0 <= i < result@.len() && result@[i].0@ == key,
            {
                let $sort_self = self;
                $sort_body
            }
        }

        }
    };
}
