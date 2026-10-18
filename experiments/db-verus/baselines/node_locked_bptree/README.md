# Node-locked B+ tree baseline

The preserved leaf-locked B+ tree used as the fresh experiment's performance baseline.
The implementation is byte-for-byte identical to the separately implemented baseline from
the legacy run; see origin.json for its source hash. It instantiates the frozen database API
and builds through the same Verus adapter and benchmark driver used for candidates.

## Design

- Up to 32 keys per node (33 children per internal node); all values reside in leaves.
  Binary search compares Unicode scalars. Keys and returned snapshots are owned Strings.
- Every node has an actual `vstd::rwlock::RwLock`. Children use safe `Arc` ownership.
  No custom unsafe code, raw pointers, lock adapters, `external_body`, assumptions,
  unchecked indexing, or separate unverified fast implementation is introduced.
- A shared tree latch protects root identity and topology during point operations.
  Reads take shared node locks. Puts take shared internal-node locks and an exclusive
  target-leaf lock. Ancestor locks remain held until the operation returns.
  Existing-key writes and inserts into non-full leaves can run on separate leaves
  concurrently. Overwrites change only the value, retaining the stored key allocation.
- A new key in a full leaf drops all fast-path locks, takes the exclusive tree latch,
  and re-searches before inserting. Splits propagate upward; root splits grow the tree.
  Leaf splits retain the separator in the right leaf; internal splits promote it.
  Splits use bounded `Vec::remove`/`push` operations, avoiding unsupported memory tricks.
- `scan` and `sort` take the exclusive tree latch, excluding puts for the entire
  snapshot. Range traversal prunes children by fences and emits ordered leaf entries.
  There are no sibling links to maintain. Endpoints are inclusive; the frozen API
  requires `lo <= hi`.

The tree latch is shared on ordinary operations, not an exclusive lock over all gets
and puts. Its reader counter can still contend, splits serialize, and snapshots block
point operations. This is a baseline tradeoff, not a claim of optimal scaling or lock
fairness. There is no delete operation, matching the supplied contract.

## Verus boundary

The implementation is Rust inside `verus!`, compiled with the pinned local Verus
`0.2026.09.06.8dea4a2`. It uses Verus's real lock, whose storage is backed by
`vstd::cell::pcell_maybe_uninit::PCell`,
and the specified `Arc`, `Vec`, `String`, and character-iterator operations.
`checks/verified_operations.rs` separately verifies the primitive vocabulary with
`--no-cheating`, including Arc sharing, lock acquire/borrow/release, and vector mutation.

**This database is not yet formally verified.** Runtime compilation uses `--no-verify`.
The node predicate describes local shape/order; subtree fence/content correspondence,
the root ghost-map authority updates, loop/recursion invariants, and consumption of the
five atomic linearization-point objects remain proof work. The frozen contracts have
not been weakened. The primitive compatibility check is not evidence of a completed
database proof and this baseline must not be exported as verified.

## Validation and measurement

Preparation snapshots this directory into `.experiment/baseline/source/` and freezes every
file hash. From the outer repository root, after preparing the experiment:

```sh
outputs/experiments/db-verus/.experiment/bin/python3 \
  outputs/experiments/db-verus/.experiment/baseline/baseline.py \
  --project outputs/experiments/db-verus --validate-only
outputs/experiments/db-verus/.experiment/bin/python3 \
  outputs/experiments/db-verus/.experiment/baseline/baseline.py \
  --project outputs/experiments/db-verus
```

The first command repeats the primitive compatibility check and all ten runtime suites in
`checks/runtime.rs`: all five API operations, ownership, Unicode ordering, concurrent histories,
the scored operating point, split propagation, concurrent inserts, and cross-leaf snapshots.
The second command measures the configured workload with one warm-up and three repetitions.
Neither command launches an agent or records a synthesis iteration or proof stamp.
Results are timestamped under `.experiment/baseline/results/`; smoke runs are labelled separately.
Only a fresh proof audit can establish candidate proof evidence.
