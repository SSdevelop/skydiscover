---
domain: concurrent in-memory key-value store
audit_mode: proof
audit_target: lib.rs
---

Build and optimize a Rust database against the exact frozen `specification/specs.rs`.
Start from the supplied B+ tree already copied into `synthesis/impl/lib.rs`; the new run has
fresh planning, evaluation and proof history, while retaining this initial implementation.
Run the entire SkySynth performance synthesis pipeline with Verus proof auditing.
Use the installed role agents. Keep this task, formal spec, verus.json, and budget immutable.
No C++ implementation. This is the ordinary performance loop with a proof-mode auditor;
do not switch to the separate `checked_by: proof` Rocq/Lean workflow.

The user chose Codex, this local computer, 60% reads / 40% writes, and 50 rounds from
implementation through proof auditing. The experiment settings below fix the selected budget
(a separately prepared smoke run may override 50). Each round must verify or reach a diagnosed
implementation blocker, including failed candidates. Incomplete proofs stay in the auditor's
proof-planning/repair loop. A failed proof is not a confirmed bug.
The guard schedules auditing with `audit_every: 1`. After the final round, finish its audit too.
Do not stop at a performance plateau before the selected budget. Never claim 50 verified
implementations merely because 50 rounds completed.

## Contract and implementation boundary

The contract defines `new`, `get`, `put`, inclusive-range `scan`, and `sort`. Preserve the
abstract map, representation correspondence, linearization-point proof objects, key ordering,
and every atomic/pre/postcondition. Concurrent snapshots must meet those exact clauses.
No durability, persistence, delete, distributed protocol, or transactions are requested.

The crate entry is `synthesis/impl/lib.rs`, with a public crate-root type `Database`. Other
modules, dependencies and data structures are design choices. Import `crate::__audit_spec::*`
and `vstd::prelude::*`. Put your types/helper impls in `verus!` blocks. At the crate root, invoke
the frozen macro with this shape, supplying all five real method bodies:

```rust
crate::impl_db! {
    impl Database {
        new() { verus_exec_expr! { { /* implementation */ } } }
        get(db, key, get_lp) { verus_exec_expr! { { /* implementation */ } } }
        put(db, key, value, put_lp) { verus_exec_expr! { { /* implementation */ } } }
        scan(db, lo, hi, scan_lp) { verus_exec_expr! { { /* implementation */ } } }
        sort(db, sort_lp) { verus_exec_expr! { { /* implementation */ } } }
    }
}
```

Use `verus_exec_expr!` inside block arguments so Rust's macro matcher can accept Verus proof
syntax such as loop invariants. The proof coder inserts annotations inside that expression.
The runner attaches `__audit_spec` from the frozen file; do not define or shadow that module or
`impl_db!`, copy weakened contracts, add conditional compilation around the invocation, or
replace obligations with `ensures true`. The frozen `macro_imports` explicitly bind model and
standard type names, preventing call-site shadowing; use glob imports rather than duplicating
those explicit imports at crate root. The experiment's build helper attaches the same file
for ordinary compilation. A concrete `id()` and storage/state correspondence belong to the
implementation. Every audit freezes the complete candidate and spec; proof agents add only
proof overlays. Executable fixes return through the auditor to the lead and main planner.
If a tool cannot verify a construct, diagnose proof-only and adapter remedies within the audit.
Only a necessary implementation change with a concrete diagnostic and proof-only analysis
allows a handoff to the next outer round. Otherwise resolve the operational issue or pause for
resumption; do not silently switch to test auditing.

## Workload and measurement

{{BASELINE}}

```json
{{SETTINGS}}
```

Reads mean `get`; writes mean `put` updates to preloaded keys. The additional workload settings
are experiment defaults, not additional requirements on the API. Key IDs are sampled uniformly
from `[0, keys)`, formatted as 16 lowercase ASCII hexadecimal characters. Values are `i32`.
Initially key `i` maps to `i % 2147483647`. Use the provided deterministic trace generator:
each worker executes shuffled blocks of six gets and four puts, with independent seeds.
All workers share one database. Preload, trace generation/decoding, and compilation are outside
timing. Start workers with a barrier; measure aggregate operations divided by wall-clock run
time. Use a fresh database and the same trace for each repetition. Report median throughput
over the repetitions plus all raw measurements and resource use. Run one warm-up before the
measured repetitions. The workload is closed-loop; do not report a bounded-latency guarantee.

The performance harness must exercise the exact implementation audited, after Verus ghost
erasure, with identical executable code and recorded compiler/optimization flags. Do not use
a separate fast unverified implementation behind a benchmark wrapper. The evaluator owns
correctness oracles and validates tests on a trusted reference and targeted mutants. Cover all
five operations, absent keys, overwrite, empty/full ranges, Unicode ordering, concurrency and
the scored operating point. `scan` and `sort` are correctness/proof targets even with 0% of the
timed mix. Retain the normal evaluator's correctness checks; replace auditor test-search with
the Verus loop. Measure this host for the environment card; inventory is not a measured ceiling.

## Completion

Retain every checkpoint, audit input manifest, proof overlay/plan, verifier invocation and raw
diagnostic, and feedback to the main planner. Finish/replay the selected candidate using the
normal release machinery. If none is proved, preserve the experiment and report that fact;
do not publish an unproved candidate as verified. Report throughput separately from proof
outcomes, attempts, and end-to-end iteration count.
