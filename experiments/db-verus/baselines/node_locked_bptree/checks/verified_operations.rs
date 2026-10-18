// Verify the primitive vocabulary used by the baseline against the installed vstd.
// This is an API compatibility proof, not a database correctness proof.
use std::sync::Arc;
use vstd::prelude::*;
use vstd::rwlock::{RwLock, RwLockPredicate};
verus! {
struct Bounded {}
impl RwLockPredicate<Vec<i32>> for Bounded {
    closed spec fn inv(self, values: Vec<i32>) -> bool { values.len() <= 32 }
}
fn primitive_compatibility(key: String) {
    let mut keys: Vec<String> = Vec::new();
    keys.push(key.clone());
    keys.insert(0, key.clone());
    let moved = keys.remove(1);
    keys.set(0, moved);
    assert(keys.len() == 1);
    assert(keys[0]@ == key@);
    let mut chars = key.as_str().chars();
    match chars.next() {
        Some(c) => { let _scalar = c as u32; },
        None => {},
    }
    let lock: Arc<RwLock<Vec<i32>, Bounded>> =
        Arc::new(RwLock::new(Vec::new(), Ghost(Bounded {})));
    let shared = lock.clone();
    let (mut values, write) = shared.acquire_write();
    if values.len() < 32 { values.push(1); }
    write.release_write(values);
    let read = lock.acquire_read();
    let _length = read.borrow().len();
    read.release_read();
}
fn main() {}
}
