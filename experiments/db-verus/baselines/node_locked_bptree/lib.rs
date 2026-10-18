// Runtime baseline, not a completed proof. Uses the actual Verus lock and frozen API.
use crate::__audit_spec::*;
use std::sync::Arc;
use vstd::prelude::*;
use vstd::resource::Loc;
use vstd::resource::ghost_var::GhostVarAuth;
use vstd::rwlock::{RwLock, RwLockPredicate};

verus! {
const MAX_KEYS: usize = 32;

struct Link {
    height: usize,
    node: Arc<RwLock<Node, NodeInvariant>>,
}

// Values occur only in leaves. Internal keys are lower fences for children 1..n.
struct Node {
    keys: Vec<String>,
    values: Vec<i32>,
    children: Vec<Link>,
}

struct NodeInvariant { height: usize }
impl RwLockPredicate<Node> for NodeInvariant {
    closed spec fn inv(self, n: Node) -> bool {
        n.keys.len() <= MAX_KEYS
        && (forall|i: int, j: int| 0 <= i < j < n.keys.len()
            ==> key_lt(#[trigger] n.keys[i]@, #[trigger] n.keys[j]@))
        && if self.height == 0 {
            n.children.len() == 0 && n.values.len() == n.keys.len()
        } else {
            n.values.len() == 0 && n.children.len() == n.keys.len() + 1
            && forall|i: int| 0 <= i < n.children.len()
                ==> (#[trigger] n.children[i]).height + 1 == self.height
        }
    }
}

struct Tree {
    root: Link,
    auth: Tracked<GhostVarAuth<Map<Seq<char>, i32>>>,
}
struct TreeInvariant { identity: Loc }
impl RwLockPredicate<Tree> for TreeInvariant {
    closed spec fn inv(self, tree: Tree) -> bool {
        tree.auth@.id() == self.identity
        // Subtree/map correspondence and atomic LP proofs remain future proof work.
    }
}

pub struct Database { tree: RwLock<Tree, TreeInvariant> }
impl Database {
    pub closed spec fn id(&self) -> Loc { self.tree.pred().identity }
}

fn empty_node() -> Node {
    Node { keys: Vec::new(), values: Vec::new(), children: Vec::new() }
}

fn link(node: Node, height: usize) -> Link {
    let lock: RwLock<Node, NodeInvariant> =
        RwLock::new(node, Ghost(NodeInvariant { height }));
    Link { height, node: Arc::new(lock) }
}

fn copy_link(source: &Link) -> Link {
    Link { height: source.height, node: source.node.clone() }
}

// Scalar comparison uses specified String/Chars operations, not an opaque comparator.
fn compare(a: &String, b: &String) -> (bool, bool) {
    let mut left = a.as_str().chars();
    let mut right = b.as_str().chars();
    loop {
        match (left.next(), right.next()) {
            (Some(x), Some(y)) => {
                if x != y { return ((x as u32) < (y as u32), false); }
            },
            (None, Some(_)) => { return (true, false); },
            (Some(_), None) => { return (false, false); },
            (None, None) => { return (false, true); },
        }
    }
}

fn position(keys: &Vec<String>, key: &String) -> (usize, bool) {
    let mut low = 0usize;
    let mut high = keys.len();
    while low < high {
        let mid = low + (high - low) / 2;
        let (less, equal) = compare(&keys[mid], key);
        if equal { return (mid, true); }
        if less { low = mid + 1; } else { high = mid; }
    }
    (low, false)
}

fn child_position(keys: &Vec<String>, key: &String) -> usize {
    let (pos, equal) = position(keys, key);
    if equal { pos + 1 } else { pos }
}

fn lookup(current: &Link, key: &String) -> Option<i32> {
    let handle = current.node.acquire_read();
    let node = handle.borrow();
    let result;
    if current.height == 0 {
        let (pos, found) = position(&node.keys, key);
        result = if found { Some(node.values[pos]) } else { None };
    } else {
        let pos = child_position(&node.keys, key);
        result = lookup(&node.children[pos], key);
    }
    handle.release_read();
    result
}

// Shared ancestors, exclusive target leaf. Inserts fit without changing fences:
// routing already establishes that the new key lies in the chosen child's range.
// Only a full leaf with an absent key asks the caller to retry under the tree latch.
fn try_put(current: &Link, key: &String, value: i32) -> bool {
    if current.height == 0 {
        let (mut node, handle) = current.node.acquire_write();
        let (pos, found) = position(&node.keys, key);
        let success;
        if found {
            node.values.set(pos, value);
            success = true;
        } else if node.keys.len() < MAX_KEYS {
            node.keys.insert(pos, key.clone());
            node.values.insert(pos, value);
            success = true;
        } else {
            success = false;
        }
        handle.release_write(node);
        success
    } else {
        let handle = current.node.acquire_read();
        let node = handle.borrow();
        let pos = child_position(&node.keys, key);
        let success = try_put(&node.children[pos], key, value);
        handle.release_read();
        success
    }
}

// Move a bounded suffix with specified Vec::remove/push operations. No raw memory,
// unsafe code, custom allocation, or unmodelled split_off/iterator adapters.
fn split(node: &mut Node, height: usize) -> (String, Link) {
    let middle = node.keys.len() / 2;
    let separator = node.keys[middle].clone();
    let mut right = empty_node();
    if height == 0 {
        while node.keys.len() > middle {
            right.keys.push(node.keys.remove(middle));
            right.values.push(node.values.remove(middle));
        }
    } else {
        // Internal separator moves up; leaf separator remains in the right leaf.
        let _promoted = node.keys.remove(middle);
        while node.keys.len() > middle {
            right.keys.push(node.keys.remove(middle));
        }
        while node.children.len() > middle + 1 {
            right.children.push(node.children.remove(middle + 1));
        }
    }
    (separator, link(right, height))
}

// Called only with the exclusive tree latch, after dropping all fast-path locks.
// Re-searching handles a concurrent insertion of the same key before this retry.
fn insert(current: &Link, key: String, value: i32) -> Option<(String, Link)> {
    let (mut node, handle) = current.node.acquire_write();
    if current.height == 0 {
        let (pos, found) = position(&node.keys, &key);
        if found {
            node.values.set(pos, value);
        } else {
            node.keys.insert(pos, key);
            node.values.insert(pos, value);
        }
    } else {
        let pos = child_position(&node.keys, &key);
        let divided = insert(&node.children[pos], key, value);
        match divided {
            Some((separator, right)) => {
                node.keys.insert(pos, separator);
                node.children.insert(pos + 1, right);
            },
            None => {},
        }
    }
    let divided = if node.keys.len() > MAX_KEYS {
        Some(split(&mut node, current.height))
    } else { None };
    handle.release_write(node);
    divided
}

fn collect(current: &Link, lo: Option<&String>, hi: Option<&String>,
    rows: &mut Vec<(String, i32)>)
{
    let handle = current.node.acquire_read();
    let node = handle.borrow();
    if current.height == 0 {
        let mut pos = match lo { Some(key) => position(&node.keys, key).0, None => 0 };
        while pos < node.keys.len() {
            let past_end = match hi {
                Some(key) => compare(key, &node.keys[pos]).0,
                None => false,
            };
            if past_end { break; }
            rows.push((node.keys[pos].clone(), node.values[pos]));
            pos = pos + 1;
        }
    } else {
        let mut pos = match lo {
            Some(key) => child_position(&node.keys, key), None => 0,
        };
        while pos < node.children.len() {
            let past_end = if pos > 0 {
                match hi {
                    Some(key) => compare(key, &node.keys[pos - 1]).0,
                    None => false,
                }
            } else { false };
            if past_end { break; }
            collect(&node.children[pos], lo, hi, rows);
            pos = pos + 1;
        }
    }
    handle.release_read();
}
}

crate::impl_db! {
    impl Database {
        new() { verus_exec_expr! {{
            let tracked (auth, var) = GhostVarAuth::new(Map::<Seq<char>, i32>::empty());
            let ghost identity = auth.id();
            let tree = Tree { root: link(empty_node(), 0), auth: Tracked(auth) };
            let db = Database { tree: RwLock::new(tree, Ghost(TreeInvariant { identity })) };
            (db, Tracked(DatabaseState { var, contents: Map::<Seq<char>, i32>::empty() }))
        }} }
        get(db, key, get_lp) { verus_exec_expr! {{
            let handle = db.tree.acquire_read();
            let result = lookup(&handle.borrow().root, key);
            handle.release_read();
            result
        }} }
        put(db, key, value, put_lp) { verus_exec_expr! {{
            let handle = db.tree.acquire_read();
            let done = try_put(&handle.borrow().root, &key, value);
            handle.release_read();
            if !done {
                let (mut tree, handle) = db.tree.acquire_write();
                let divided = insert(&tree.root, key, value);
                match divided {
                    Some((separator, right)) => {
                        let mut parent = empty_node();
                        parent.keys.push(separator);
                        parent.children.push(copy_link(&tree.root));
                        parent.children.push(right);
                        tree.root = link(parent, tree.root.height + 1);
                    },
                    None => {},
                }
                handle.release_write(tree);
            }
        }} }
        scan(db, lo, hi, scan_lp) { verus_exec_expr! {{
            let (tree, handle) = db.tree.acquire_write();
            let mut rows = Vec::new();
            collect(&tree.root, Some(lo), Some(hi), &mut rows);
            handle.release_write(tree);
            rows
        }} }
        sort(db, sort_lp) { verus_exec_expr! {{
            let (tree, handle) = db.tree.acquire_write();
            let mut rows = Vec::new();
            collect(&tree.root, None, None, &mut rows);
            handle.release_write(tree);
            rows
        }} }
    }
}
