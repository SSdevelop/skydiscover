mod check_0 {
// Owned results remain usable across later operations. No latency requirement.
use crate::Database;

pub fn main() {
    let db = Database::new().0;
    let a = String::from("a");
    let b = String::from("b");
    let c = String::from("c");
    db.put(a.clone(), 11);
    db.put(b.clone(), 22);
    let old_get = db.get(&a);
    assert_eq!(old_get, Some(11));
    db.put(a.clone(), 12);
    assert_eq!(db.get(&a), Some(12));
    assert_eq!(old_get, Some(11));

    let old_scan = db.scan(&a, &b);
    let expected_scan = vec![(a.clone(), 12), (b.clone(), 22)];
    assert_eq!(old_scan, expected_scan);
    db.put(a.clone(), 13);
    db.put(c.clone(), 33);
    assert_eq!(db.get(&a), Some(13));
    assert_eq!(db.get(&c), Some(33));
    assert_eq!(old_scan, expected_scan);

    let old_sort = db.sort();
    let expected_sort = vec![(a.clone(), 13), (b.clone(), 22), (c.clone(), 33)];
    assert_eq!(old_sort, expected_sort);
    // Keep several generations alive, so access cannot be tied to the newest result.
    let mut retained = vec![(old_sort, expected_sort)];
    for generation in 0..16 {
        let value = 100 + generation;
        db.put(a.clone(), value);
        db.put(c.clone(), -value);
        assert_eq!(db.get(&a), Some(value));
        assert_eq!(db.get(&b), Some(22));
        assert_eq!(db.get(&c), Some(-value));
        let expected = vec![(a.clone(), value), (b.clone(), 22), (c.clone(), -value)];
        let fresh_sort = db.sort();
        let fresh_scan = db.scan(&a, &c);
        assert_eq!(fresh_sort, expected);
        assert_eq!(fresh_scan, expected);
        retained.push((fresh_sort, expected.clone()));
        retained.push((fresh_scan, expected));
        assert_eq!(old_get, Some(11));
        assert_eq!(old_scan, expected_scan);
        for (snapshot, original) in &retained {
            assert_eq!(snapshot, original);
        }
    }
    // Caller-owned strings/vectors are independent of database storage as well.
    let mut editable = db.sort();
    editable[0].0.push_str("-caller");
    editable[0].1 = i32::MIN;
    editable.clear();
    assert_eq!(db.get(&a), Some(115));
    assert_eq!(db.get(&c), Some(-115));
    assert_eq!(db.sort(), vec![(a, 115), (b, 22), (c, -115)]);
}

}
mod check_1 {
// Seeded by Crossbeam's concurrent_insert_get_same_key (issue #1023).
// Strengthened for this contract: range/full snapshots must be one atomic state.
use crate::Database;
use std::collections::{BTreeMap, HashSet};
use std::sync::{Arc, Barrier, Mutex};
use std::sync::atomic::{AtomicU64, Ordering};

#[derive(Clone, Debug)]
enum Op { Get(String), Put(String, i32), Scan(String, String), Sort }
#[derive(Clone, Debug, PartialEq)]
enum Reply { Value(Option<i32>), Written, Rows(Vec<(String, i32)>) }
#[derive(Clone, Debug)]
struct Event { begin: u64, end: u64, op: Op, reply: Reply }
type Model = BTreeMap<String, i32>;

fn step(map: &Model, event: &Event) -> Option<Model> {
    let mut next = map.clone();
    let expected = match &event.op {
        Op::Get(key) => Reply::Value(map.get(key).copied()),
        Op::Put(key, value) => { next.insert(key.clone(), *value); Reply::Written },
        Op::Scan(lo, hi) => Reply::Rows(map.iter()
            .filter(|(key, _)| lo <= *key && *key <= hi)
            .map(|(k, v)| (k.clone(), *v)).collect()),
        Op::Sort => Reply::Rows(map.iter().map(|(k, v)| (k.clone(), *v)).collect()),
    };
    (expected == event.reply).then_some(next)
}

fn linearizable(initial: &Model, events: &[Event]) -> bool {
    assert!(events.len() < 64);
    let predecessors: Vec<u64> = events.iter().map(|e| events.iter().enumerate()
        .filter(|(_, p)| p.end < e.begin).fold(0, |mask, (i, _)| mask | (1 << i))).collect();
    fn search(events: &[Event], predecessors: &[u64], done: u64, map: &Model,
              dead: &mut HashSet<(u64, Vec<(String, i32)>)>) -> bool {
        if done == (1u64 << events.len()) - 1 { return true; }
        let state = (done, map.iter().map(|(k, v)| (k.clone(), *v)).collect());
        if dead.contains(&state) { return false; }
        for (i, event) in events.iter().enumerate() {
            let bit = 1u64 << i;
            if done & bit == 0 && predecessors[i] & !done == 0 {
                if let Some(next) = step(map, event) {
                    if search(events, predecessors, done | bit, &next, dead) { return true; }
                }
            }
        }
        dead.insert(state);
        false
    }
    search(events, &predecessors, 0, initial, &mut HashSet::new())
}

fn record(db: &Database, clock: &AtomicU64, events: &Mutex<Vec<Event>>, op: Op) {
    let begin = clock.fetch_add(1, Ordering::SeqCst);
    let reply = match &op {
        Op::Get(key) => Reply::Value(db.get(key)),
        Op::Put(key, value) => { db.put(key.clone(), *value); Reply::Written },
        Op::Scan(lo, hi) => Reply::Rows(db.scan(lo, hi)),
        Op::Sort => Reply::Rows(db.sort()),
    };
    let end = clock.fetch_add(1, Ordering::SeqCst);
    events.lock().unwrap().push(Event { begin, end, op, reply });
}

// The checker itself must accept the legal state between two separate puts,
// reject a torn state, and enforce completed-before-invoked order.
fn check_oracle() {
    let initial: Model = [("a".into(), 0), ("b".into(), 0)].into_iter().collect();
    let mut events = vec![
        Event { begin: 1, end: 2, op: Op::Put("a".into(), 1), reply: Reply::Written },
        Event { begin: 3, end: 4, op: Op::Put("b".into(), 1), reply: Reply::Written },
        Event { begin: 0, end: 5, op: Op::Sort,
            reply: Reply::Rows(vec![("a".into(), 1), ("b".into(), 0)]) },
    ];
    assert!(linearizable(&initial, &events));
    events[2].reply = Reply::Rows(vec![("a".into(), 0), ("b".into(), 1)]);
    assert!(!linearizable(&initial, &events));
    events[2].reply = Reply::Rows(vec![("a".into(), 0), ("b".into(), 0)]);
    events[2].begin = 5; events[2].end = 6;
    assert!(!linearizable(&initial, &events));
}

pub fn main() {
    check_oracle();
    for scenario in 0..3 {
        for round in 0..24 {
            let db = Arc::new(Database::new().0);
            assert_eq!(db.sort(), Vec::<(String, i32)>::new());
            let mut initial = Model::new();
            for key in ["a", "b"] {
                db.put(key.into(), 0);
                initial.insert(key.into(), 0);
            }
            let clock = Arc::new(AtomicU64::new(0));
            let events = Arc::new(Mutex::new(Vec::new()));
            let barrier = Arc::new(Barrier::new(8));
            let mut workers = Vec::new();
            for worker in 0..8 {
                let db = db.clone(); let clock = clock.clone();
                let events = events.clone(); let barrier = barrier.clone();
                workers.push(std::thread::spawn(move || {
                    barrier.wait();
                    // Bounded schedule perturbation; no timing or overlap assertion.
                    let yields = if worker == 0 { 64 + round * 7 } else { (worker + round) % 7 };
                    for _ in 0..yields { std::thread::yield_now(); }
                    let ops = match worker {
                        0 => vec![Op::Put("a".into(), 1), Op::Put("b".into(), 1)],
                        1 if scenario == 1 => vec![Op::Put("a".into(), -1), Op::Get("a".into())],
                        1 if scenario == 2 => vec![Op::Put("c".into(), 9), Op::Get("c".into())],
                        1 => vec![Op::Get("a".into()), Op::Get("b".into())],
                        2 | 5 => vec![Op::Sort, Op::Get("a".into())],
                        3 | 6 => vec![Op::Scan("a".into(), "z".into()), Op::Get("b".into())],
                        _ => vec![Op::Get("a".into()), Op::Scan("b".into(), "c".into())],
                    };
                    for op in ops { record(&db, &clock, &events, op); }
                }));
            }
            for worker in workers { worker.join().expect("database operation panicked"); }
            record(&db, &clock, &events, Op::Sort);
            let mut history = events.lock().unwrap().clone();
            history.sort_by_key(|e| e.begin);
            assert!(linearizable(&initial, &history),
                "non-linearizable history scenario={scenario} round={round}: {history:#?}");
        }
    }
    println!("72 histories, eight workers, 17 recorded operations/history; all linearizable");
}

}
mod check_2 {
// Seed: Crossbeam skiplist tests/map.rs:420 (present and absent lookups).
// Extend to the frozen String/i32 domain and read-preservation requirement.
use crate::Database;
use std::collections::BTreeMap;

fn check_reads(db: &Database, model: &BTreeMap<String, i32>, keys: &[String]) {
    let expected: Vec<_> = model.iter().map(|(k, v)| (k.clone(), *v)).collect();
    assert_eq!(db.sort(), expected);
    for key in keys {
        assert_eq!(db.get(key), model.get(key).copied(), "lookup {key:?}");
        assert_eq!(db.get(key), model.get(key).copied(), "repeated lookup {key:?}");
        assert_eq!(db.sort(), expected, "read mutated the map for {key:?}");
    }
}

pub fn main() {
    let db = Database::new().0;
    let mut model = BTreeMap::new();
    let keys: Vec<String> = ["", "a", "ab", "abc", "a\0", "a\0b", "\0", "not hex!",
        "0000000000000001", "1", "é", "e\u{301}", "中文", "🦀", "\u{10ffff}",
        "never inserted", "abcd", "a\0bc"].iter().map(|s| (*s).to_string()).collect();
    check_reads(&db, &model, &keys);
    let values = [0, -1, i32::MIN, i32::MAX, 17, -9001];
    for index in 0..15 {
        let key = keys[index].clone();
        let value = values[index % values.len()];
        db.put(key.clone(), value);
        model.insert(key, value);
        check_reads(&db, &model, &keys);
    }
    for pass in 0..values.len() {
        for index in (0..15).rev() {
            let key = keys[index].clone();
            let value = values[(index + pass) % values.len()];
            db.put(key.clone(), value);
            model.insert(key, value);
            check_reads(&db, &model, &keys);
        }
    }
}

}
mod check_3 {
// Property db_new: fresh state is empty and separate instances stay independent.
// Seed: frozen new contract and properties.json db_new probe.
use crate::Database;

fn assert_empty(db: &Database) {
    for key in ["", "absent", "a\0b", "数据库", "😀", "\u{10ffff}"] {
        assert_eq!(db.get(&key.to_owned()), None, "fresh key: {key:?}");
    }
    assert!(db.sort().is_empty(), "fresh sort must be empty");
    for (lo, hi) in [("", ""), ("", "\u{10ffff}"), ("a", "z"), ("数据库", "数据库")] {
        assert!(db.scan(&lo.to_owned(), &hi.to_owned()).is_empty());
    }
}

pub fn main() {
    let first = Database::new().0;
    let second = Database::new().0;
    assert_empty(&first);
    assert_empty(&second);
    for (key, value) in [("", 0), ("absent", -1), ("数据库", i32::MAX), ("😀", i32::MIN)] {
        first.put(key.to_owned(), value);
    }
    assert_empty(&second);
    second.put("separate".to_owned(), 27);
    assert_eq!(first.get(&"separate".to_owned()), None);
    drop(first);
    let third = Database::new().0;
    assert_empty(&third);
    assert_eq!(third.get(&"separate".to_owned()), None);
    drop(second);
    let fourth = Database::new().0;
    assert_empty(&fourth);
}

}
mod check_4 {
// Extends the insertion/read seed in references/tests/dashmap/src/lib.rs:1433.
// One property: each put performs exactly the designated map update.
use crate::Database;
use std::collections::HashMap;

fn update(db: &Database, model: &mut HashMap<String, i32>, key: String, value: i32) {
    db.put(key.clone(), value);
    model.insert(key.clone(), value);
    assert_eq!(db.get(&key), Some(value), "designated binding after put {key:?}");
    let mut expected: Vec<_> = model.iter().map(|(k, v)| (k.clone(), *v)).collect();
    expected.sort_by(|a, b| a.0.chars().cmp(b.0.chars()));
    assert_eq!(db.sort(), expected, "put must retain exactly the model bindings");
    for (k, v) in model.iter() {
        assert_eq!(db.get(k), Some(*v), "unrelated binding {k:?} changed");
    }
}

pub fn main() {
    let db = Database::new().0;
    let mut model = HashMap::new();
    let keys = ["", "a", "aa", "a\0", "a\0b", "ab", "not-a-hex-key", "2", "10", "é", "e\u{301}", "中", "😀", "\u{10ffff}"];
    for (i, key) in keys.iter().enumerate() {
        update(&db, &mut model, (*key).to_owned(), i as i32);
    }
    for value in [i32::MIN, i32::MIN, 0, -1, i32::MAX, i32::MAX, 17] {
        for key in keys.iter().rev() {
            update(&db, &mut model, (*key).to_owned(), value);
        }
    }
    // Deterministic new keys exercise growth and lengths outside the timed hex domain.
    for i in 0..1024usize {
        let key = format!("outside preload / {i:05} / {}", "x".repeat(i % 41));
        update(&db, &mut model, key, (i as i32).wrapping_mul(-7919));
        update(&db, &mut model, keys[i % keys.len()].to_owned(), i as i32);
    }
}

}
mod check_5 {
// Range/completeness seed: crossbeam-skiplist/tests/map.rs:635,659,
// pinned at 099d0469efcfacb32527bd9451a4fb62586ae537.
// Only inclusive, non-reversed ranges are required by frozen specs.rs:112.
use crate::Database;
use std::cmp::Ordering;

fn cmp(a: &str, b: &str) -> Ordering {
    a.chars().map(|c| c as u32).cmp(b.chars().map(|c| c as u32))
}

fn check(db: &Database, model: &[(String, i32)], bounds: &[String]) {
    let mut all = model.to_vec();
    all.sort_by(|a, b| cmp(&a.0, &b.0));
    for lo in bounds {
        for hi in bounds {
            if cmp(lo, hi) == Ordering::Greater { continue; }
            let expected: Vec<_> = all.iter()
                .filter(|(k, _)| cmp(lo, k) != Ordering::Greater
                    && cmp(k, hi) != Ordering::Greater)
                .cloned().collect();
            assert_eq!(db.scan(lo, hi), expected, "inclusive scan {lo:?}..={hi:?}");
            assert_eq!(db.sort(), all, "scan must preserve the complete map");
        }
    }
}

pub fn main() {
    let db = Database::new().0;
    // Distinct spellings, scalar boundaries, NUL, prefixes, and nonnumeric order.
    let keys = ["z", "", "a\0", "aa", "a", "10", "2", "\0", "é", "e\u{301}",
        "\u{7f}", "\u{80}", "中", "\u{e000}", "\u{ffff}", "\u{10000}",
        "😀", "\u{10ffff}", "\u{10ffff}tail"];
    let mut bounds: Vec<String> = keys.iter().map(|k| k.to_string()).collect();
    // Absent equal endpoints and gaps; neither a nonexistent 'maximum char'
    // sentinel nor a fixed benchmark key space is assumed.
    bounds.extend(["a\0x", "ab", "b", "11", "\u{81}", "\u{10ffff}tailx"]
        .iter().map(|k| k.to_string()));
    let mut model = Vec::new();
    check(&db, &model, &bounds);
    for (i, key) in keys.iter().enumerate() {
        let value = match i % 4 { 0 => i32::MIN, 1 => i32::MAX, 2 => 0, _ => -(i as i32) };
        db.put(key.to_string(), value);
        model.push((key.to_string(), value));
    }
    check(&db, &model, &bounds);
    db.put("a".to_string(), 123);
    model.iter_mut().find(|(k, _)| k == "a").unwrap().1 = 123;
    db.put("ab".to_string(), -987);
    model.push(("ab".to_string(), -987));
    check(&db, &model, &bounds);
}

}
mod check_6 {
// Property db_sort. Seed: crossbeam-skiplist/tests/map.rs iter, line 635:
// shuffled insertion followed by equality of the complete ordered traversal.
use crate::Database;

fn check(db: &Database, model: &[(String, i32)]) {
    let mut expected = model.to_vec();
    expected.sort_by(|a, b| a.0.chars().cmp(b.0.chars()));
    for _ in 0..3 {
        assert_eq!(db.sort(), expected, "sort must return the exact ordered map");
        for (key, value) in model {
            assert_eq!(db.get(key), Some(*value), "sort must preserve bindings");
        }
        assert_eq!(db.get(&"never inserted".to_owned()), None);
    }
}

fn write(db: &Database, model: &mut Vec<(String, i32)>, key: String, value: i32) {
    db.put(key.clone(), value);
    if let Some(entry) = model.iter_mut().find(|entry| entry.0 == key) {
        entry.1 = value;
    } else {
        model.push((key, value));
    }
}

pub fn main() {
    let db = Database::new().0;
    let mut model = Vec::new();
    check(&db, &model);
    write(&db, &mut model, "prefix".to_owned(), i32::MIN);
    check(&db, &model);
    for (i, key) in ["😀", "prefix/z", "", "prefix/a", "数据库", "a\0b", "10",
                     "prefix/", "é", "e\u{301}", "\u{10ffff}", "2", "pref", "a"].iter().enumerate() {
        write(&db, &mut model, (*key).to_owned(), (i as i32 - 7) * 101);
    }
    check(&db, &model);
    for value in [0, i32::MAX, -1, i32::MIN, i32::MIN, 42] {
        write(&db, &mut model, "prefix".to_owned(), value);
        write(&db, &mut model, "数据库".to_owned(), value);
        check(&db, &model);
    }
    for i in 0..257 {
        let index = (i * 131) % 257;
        write(&db, &mut model, format!("prefix/{index:03}/尾"), index - 128);
    }
    check(&db, &model);
    write(&db, &mut model, "after-sorting".to_owned(), i32::MAX);
    check(&db, &model);
}

}
mod check_7 {
// Exact workload from experiments/db-verus/trace.py, embedded to keep the suite relocatable.
// Runtime correctness only: instrumentation and checking are not performance measurements.
use crate::Database;
use std::collections::HashSet;
use std::process::Command;
use std::sync::{Arc, Barrier, atomic::{AtomicU64, Ordering}};

const KEYS: usize = 100_000;
const WORKERS: usize = 8;
const OPS: usize = 100_000;

#[derive(Clone, Copy)]
struct Operation { key: usize, write: bool, value: i32 }
#[derive(Clone, Copy)]
struct Event { start: u64, end: u64, write: bool, value: i32 }

fn traces() -> Vec<Vec<Operation>> {
    // Same random.Random seed, shuffle, randrange call order and key domain as supplied
    // operations(settings, worker). Only transport differs: fixed-width binary instead of JSONL.
    let script = r#"
import random, struct, sys
for worker in range(8):
    rng = random.Random(20261017 + worker)
    for _ in range(100000 // 10):
        block = ['get'] * 6 + ['put'] * 4
        rng.shuffle(block)
        for op in block:
            key = rng.randrange(100000)
            value = rng.randrange(-(2**31), 2**31) if op == 'put' else 0
            sys.stdout.buffer.write(struct.pack('<BIi', op == 'put', key, value))
"#;
    let output = Command::new("python3").arg("-c").arg(script).output().unwrap();
    assert!(output.status.success(), "trace generation: {}", String::from_utf8_lossy(&output.stderr));
    assert_eq!(output.stdout.len(), WORKERS * OPS * 9);
    output.stdout.chunks_exact(OPS * 9).map(|worker| {
        let decoded: Vec<_> = worker.chunks_exact(9).map(|row| Operation {
            write: row[0] == 1,
            key: u32::from_le_bytes(row[1..5].try_into().unwrap()) as usize,
            value: i32::from_le_bytes(row[5..9].try_into().unwrap()),
        }).collect();
        for block in decoded.chunks_exact(10) {
            assert_eq!(block.iter().filter(|op| op.write).count(), 4);
            assert!(block.iter().all(|op| op.key < KEYS));
        }
        decoded
    }).collect()
}

// Exact per-key linearizability search. Disjoint-key histories compose. A predecessor
// is an operation whose response precedes this invocation; overlapping writes may
// linearize in either order. Matching enabled reads can safely be placed first because
// they neither alter state nor violate any real-time predecessor.
fn search(events: &[Event], before: &[u128], mut remaining: u128, value: i32,
          final_value: i32, failed: &mut HashSet<(u128, i32)>) -> bool {
    loop {
        let read = (0..events.len()).find(|&i| remaining & (1u128 << i) != 0
            && before[i] & remaining == 0 && !events[i].write && events[i].value == value);
        match read { Some(i) => remaining &= !(1u128 << i), None => break }
    }
    if remaining == 0 { return value == final_value; }
    if failed.contains(&(remaining, value)) { return false; }
    for i in 0..events.len() {
        if remaining & (1u128 << i) != 0 && before[i] & remaining == 0 && events[i].write {
            if search(events, before, remaining & !(1u128 << i), events[i].value,
                      final_value, failed) { return true; }
        }
    }
    failed.insert((remaining, value));
    false
}

fn check_history(events: &[Event], initial: i32, final_value: i32) -> bool {
    // The frozen traces have fewer than 128 accesses per key, independently of scheduling.
    assert!(events.len() < 128, "fixture exceeds checker bitset capacity");
    let before: Vec<u128> = events.iter().map(|event| events.iter().enumerate()
        .filter(|(_, other)| other.end < event.start)
        .fold(0u128, |bits, (j, _)| bits | (1u128 << j))).collect();
    search(events, &before, (1u128 << events.len()) - 1, initial, final_value,
           &mut HashSet::new())
}

fn repetition(traces: &[Vec<Operation>], repetition: usize) {
    let db = Arc::new(Database::new().0);
    let keys: Arc<Vec<String>> = Arc::new((0..KEYS).map(|i| format!("{i:016x}")).collect());
    for (i, key) in keys.iter().enumerate() { db.put(key.clone(), i as i32); }
    let barrier = Arc::new(Barrier::new(WORKERS));
    let clock = Arc::new(AtomicU64::new(0));
    let records = std::thread::scope(|scope| {
        let mut workers = Vec::new();
        for trace in traces {
            let db = Arc::clone(&db);
            let keys = Arc::clone(&keys);
            let barrier = Arc::clone(&barrier);
            let clock = Arc::clone(&clock);
            workers.push(scope.spawn(move || {
                let mut events = Vec::with_capacity(OPS);
                barrier.wait();
                for op in trace {
                    let start = clock.fetch_add(1, Ordering::SeqCst);
                    let value = if op.write {
                        db.put(keys[op.key].clone(), op.value);
                        op.value
                    } else {
                        db.get(&keys[op.key]).expect("preloaded key disappeared at operating point")
                    };
                    let end = clock.fetch_add(1, Ordering::SeqCst);
                    events.push(Event { start, end, write: op.write, value });
                }
                events
            }));
        }
        workers.into_iter().map(|worker| worker.join().unwrap()).collect::<Vec<_>>()
    });
    assert_eq!(clock.load(Ordering::SeqCst), (2 * WORKERS * OPS) as u64);
    let mut by_key: Vec<Vec<Event>> = (0..KEYS).map(|_| Vec::new()).collect();
    for (trace, events) in traces.iter().zip(&records) {
        assert_eq!(events.len(), OPS);
        for (op, event) in trace.iter().zip(events) { by_key[op.key].push(*event); }
    }
    let snapshot = db.sort();
    assert_eq!(snapshot.len(), KEYS, "full domain retained");
    let mut untouched = 0;
    for (i, (key, value)) in snapshot.iter().enumerate() {
        assert_eq!(key, &keys[i], "complete strictly ordered key domain");
        assert!(check_history(&by_key[i], i as i32, *value),
            "key {key}: read/final values incompatible with completed-before-invoked order");
        if by_key[i].iter().all(|event| !event.write) {
            untouched += 1;
            assert_eq!(*value, i as i32, "untouched key changed");
        }
        assert_eq!(db.get(key), Some(*value), "post-run lookup disagrees with sort");
    }
    assert!(untouched > 0, "fixture must exercise untouched bindings");
    assert_eq!(db.scan(&keys[0], &keys[KEYS - 1]), snapshot, "full-range snapshot disagreement");
    assert_eq!(db.sort(), snapshot, "observations changed state");
    println!("operating point repetition {repetition}: {KEYS} keys, {WORKERS} workers, {} operations, {untouched} untouched keys checked", WORKERS * OPS);
}

pub fn main() {
    // Checker sanity: same-key overlapping writes may finish in either final state;
    // a completed write cannot be followed by a stale read.
    let overlapping = [Event { start: 0, end: 3, write: true, value: 1 },
                       Event { start: 1, end: 2, write: true, value: 2 }];
    assert!(check_history(&overlapping, 0, 1));
    assert!(check_history(&overlapping, 0, 2));
    assert!(!check_history(&overlapping, 0, 0));
    let stale = [Event { start: 0, end: 1, write: true, value: 1 },
                 Event { start: 2, end: 3, write: false, value: 0 }];
    assert!(!check_history(&stale, 0, 1));
    let traces = traces();
    for repeat in 0..4 { repetition(&traces, repeat); } // One warm-up plus three fresh repetitions.
}

}
mod check_8 {
use crate::Database;
use std::cmp::Ordering;

// Frozen key_lt compares numeric Unicode scalars recursively, then prefix length.
// Decode independently of Rust String/BTreeMap ordering used by the reference.
fn scalar_cmp(a: &str, b: &str) -> Ordering {
    let left: Vec<u32> = a.chars().map(|c| c as u32).collect();
    let right: Vec<u32> = b.chars().map(|c| c as u32).collect();
    for i in 0..left.len().min(right.len()) {
        if left[i] < right[i] { return Ordering::Less; }
        if left[i] > right[i] { return Ordering::Greater; }
    }
    left.len().cmp(&right.len())
}

pub fn main() {
    let keys = [
        "\u{10000}", "\u{e000}", "\u{ffff}", "\u{1f600}", "\u{10ffff}",
        "", "\0", "\0a", "a", "a\0", "aa", "a\u{e000}", "a\u{10000}",
        "2", "10", "02", "\u{7f}", "\u{80}", "\u{d7ff}",
        "é", "e\u{301}", "e", "É", "Z",
    ];
    // Values identify original strings; normalization must not merge their bindings.
    let mut expected: Vec<(String, i32)> = keys.iter().enumerate()
        .map(|(index, key)| (key.to_string(), index as i32 - 12)).collect();
    let db = Database::new().0;
    for (key, value) in &expected { db.put(key.clone(), *value); }
    expected.sort_by(|a, b| scalar_cmp(&a.0, &b.0));
    assert_eq!(db.sort(), expected, "sort must use scalar order with distinct spellings");

    let mut bounds: Vec<String> = keys.iter().map(|k| k.to_string()).collect();
    bounds.extend(["\u{d8000}", "a\u{ffff}", "\u{e001}", "\u{10001}", "e\u{300}"]
        .iter().map(|s| s.to_string()));
    bounds.sort_by(|a, b| scalar_cmp(a, b));
    bounds.dedup();
    for (i, lo) in bounds.iter().enumerate() {
        for hi in &bounds[i..] {
            let wanted: Vec<(String, i32)> = expected.iter()
                .filter(|(key, _)| scalar_cmp(lo, key) != Ordering::Greater
                    && scalar_cmp(key, hi) != Ordering::Greater)
                .cloned().collect();
            assert_eq!(db.scan(lo, hi), wanted, "scalar inclusive range {lo:?}..={hi:?}");
        }
    }
    for (key, value) in &expected { assert_eq!(db.get(key), Some(*value)); }
    assert_eq!(db.sort(), expected);
}

}
mod check_9 {
use crate::Database;
use std::collections::BTreeMap;
use std::sync::{Arc, Barrier};

fn differential() {
    for order in 0..3 {
        let db = Database::new().0;
        let mut model = BTreeMap::new();
        let mut indices: Vec<usize> = (0..12000).collect();
        if order == 1 { indices.reverse(); }
        if order == 2 {
            let mut seed = 0x91e10da5u64;
            for i in (1..indices.len()).rev() {
                seed ^= seed << 13; seed ^= seed >> 7; seed ^= seed << 17;
                indices.swap(i, seed as usize % (i + 1));
            }
        }
        for i in indices {
            // Unicode, prefixes and NUL cross many leaf/internal split boundaries.
            let key = format!("{}{:08x}\0tail", ["", "é", "中", "😀"][i % 4], i);
            let value = (i as i32).wrapping_mul(-37);
            db.put(key.clone(), value);
            model.insert(key, value);
        }
        let keys: Vec<_> = model.keys().cloned().collect();
        for (i, key) in keys.iter().enumerate() {
            assert_eq!(db.get(key), model.get(key).copied());
            if i % 3 == 0 {
                db.put(key.clone(), i32::MIN);
                model.insert(key.clone(), i32::MIN);
            }
        }
        assert_eq!(db.get(&"absent".into()), None);
        for i in (0..keys.len()).step_by(137) {
            let end = (i + 121).min(keys.len() - 1);
            let expected: Vec<_> = model.range(keys[i].clone()..=keys[end].clone())
                .map(|(k, v)| (k.clone(), *v)).collect();
            assert_eq!(db.scan(&keys[i], &keys[end]), expected);
            assert_eq!(db.scan(&keys[i], &keys[i]), vec![(keys[i].clone(), model[&keys[i]])]);
        }
        assert_eq!(db.sort(), model.into_iter().collect::<Vec<_>>());
    }
}

fn concurrent_splits() {
    let db = Arc::new(Database::new().0);
    let barrier = Arc::new(Barrier::new(9));
    let mut workers = Vec::new();
    for worker in 0..8 {
        let db = db.clone(); let barrier = barrier.clone();
        workers.push(std::thread::spawn(move || {
            barrier.wait();
            for i in 0..3000 {
                let id = i * 8 + worker;
                let key = format!("k{id:08}");
                db.put(key.clone(), id as i32);
                assert_eq!(db.get(&key), Some(id as i32));
                // All threads also race on the same key, including when leaves fill.
                db.put("shared".into(), worker as i32);
            }
        }));
    }
    barrier.wait();
    for _ in 0..80 {
        let rows = db.sort();
        assert!(rows.windows(2).all(|w| w[0].0 < w[1].0));
        for (key, value) in rows {
            if key != "shared" { assert_eq!(key, format!("k{value:08}")); }
        }
    }
    for worker in workers { worker.join().unwrap(); }
    let rows = db.sort();
    assert_eq!(rows.len(), 24001);
    for (id, row) in rows[..24000].iter().enumerate() {
        assert_eq!(row, &(format!("k{id:08}"), id as i32));
    }
    assert!((0..8).contains(&db.get(&"shared".into()).unwrap()));
}

fn snapshots_across_leaves() {
    let db = Arc::new(Database::new().0);
    for i in 0..2048 { db.put(format!("k{i:08}"), 0); }
    let lo = "k00000000".to_string();
    let hi = "k00002047".to_string();
    let barrier = Arc::new(Barrier::new(5));
    let mut workers = Vec::new();
    for writer in 0..4 {
        let db = db.clone(); let barrier = barrier.clone();
        workers.push(std::thread::spawn(move || {
            barrier.wait();
            for version in 1..=2000 {
                db.put(format!("k{writer:08}"), version);
                db.put(format!("k{:08}", 2044 + writer), version);
            }
        }));
    }
    barrier.wait();
    for i in 0..300 {
        let rows = if i % 2 == 0 { db.sort() } else { db.scan(&lo, &hi) };
        assert_eq!(rows.len(), 2048);
        for writer in 0..4 {
            let first = rows[writer].1;
            let last = rows[2044 + writer].1;
            // Exactly the possible states of two sequential puts, even if a writer
            // completes many versions while a snapshot traverses separate leaves.
            assert!(first >= last && first - last <= 1,
                "torn cross-leaf snapshot: first={first}, last={last}");
        }
    }
    for worker in workers { worker.join().unwrap(); }
    let saved = db.sort();
    db.put(lo, -1);
    assert_eq!(saved[0].1, 2000, "returned snapshots must own their contents");
}

pub fn main() {
    differential();
    concurrent_splits();
    snapshots_across_leaves();
    println!("B+ tree: ordered/reverse/shuffled splits, concurrent splits, cross-leaf snapshots passed");
}

}
fn main() {
check_0::main(); println!("PASS: access_lifetime.rs");
check_1::main(); println!("PASS: concurrent_histories.rs");
check_2::main(); println!("PASS: db_get.rs");
check_3::main(); println!("PASS: db_new.rs");
check_4::main(); println!("PASS: db_put.rs");
check_5::main(); println!("PASS: db_scan.rs");
check_6::main(); println!("PASS: db_sort.rs");
check_7::main(); println!("PASS: operating_point.rs");
check_8::main(); println!("PASS: ordering.rs");
check_9::main(); println!("PASS: bptree.rs");
}
