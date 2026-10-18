The performance baseline is the user's existing B+ tree, frozen under
`.experiment/baseline/source/` in the experiment project. Its provenance and every source hash
are recorded. Use this snapshot without modifying it. It is the implemented node/leaf-locked
B+ tree with 32 keys per node, shared ancestor locks, exclusive leaf writes, and an exclusive
tree latch for splits/snapshots. It already implements the exact frozen Database API; no extra
lock, type adapter, or replacement data structure is added.

The initial synthesis implementation is a separate, writable, byte-for-byte copy of that
standalone `lib.rs` at `synthesis/impl/lib.rs` under the run. Its initial hash and source are
recorded in `.experiment/settings.json` as `initial_implementation`. Start the candidate from
this B+ tree. The frozen comparison copy remains unchanged as the working candidate evolves.

Before the first coding brief, the planner must read the actual seed source, its README,
runtime-validation results and fresh profile/measurements. Record its lock/layout/split/snapshot
design, measured bottlenecks, and unresolved proof obligations in the plan. Put the supplied
B+ tree first in `## Candidates`; the first `## Brief` must retain this implementation and name
one evidence-motivated change, or establish its initial evaluation/proof if no change is yet
justified. Bootstrap may supply missing evaluator infrastructure but must preserve the seed.
Later architectural pivots require measured evidence or a diagnosed implementation blocker
and an explicit planner decision. Do not replace the seed with a simpler new implementation
merely because the synthesis history is empty.

Run `.experiment/baseline/baseline.py --project .` with the launcher's Python to measure it.
The script compiles the exact baseline with the frozen spec and records a warm-up, all measured
repetitions, median throughput, resource use, source and trace hashes, compiler version, and flags
under `.experiment/baseline/results/scored/`. Run it with `--validate-only` to repeat the ten
runtime suites and the separate primitive-verification check before scored evaluation.
`--smoke` uses a small workload and is never a scored baseline.

Use the frozen `.experiment/baseline/benchmark.rs` and `trace.py` for both baseline and
candidate performance runs. Both must receive identical packed traces, preload, thread count,
warm-up/repetition counts, and optimization level. Decode/allocate/preload outside timing.
Use the recorded Rust toolchain for both; record any build differences explicitly. Re-measure
the baseline alongside candidates under comparable host load. Report raw candidate throughput
and speedup = candidate median ops/s / baseline median ops/s, with both measurement paths.
Preserve paired results under a unique `--out` directory for each comparison.

The timed mix measures get/put only. The baseline also implements scan/sort for runtime tests.
It is the initial synthesis candidate, with no inherited proof or correctness-oracle authority.
Build the normal correctness oracle and retain all five candidate proof obligations. Start the
fresh pipeline at Phase 1 with zero completed iterations and no inherited audit/checkpoint/KB
state; baseline measurements do not count as synthesis iterations.
