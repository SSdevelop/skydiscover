# Local DB + Verus experiment

Runs the complete SkySynth pipeline with Codex: specification discovery/cards, planner,
implementation, correctness/performance evaluation, proof-mode auditor, critic, and final review.
The input is a byte-for-byte snapshot of the bundled [authoritative specification](specification/specs.rs).
Performance is compared with
the existing [node/leaf-locked B+ tree](baselines/node_locked_bptree/README.md), snapshotted with
source hashes during preparation. A writable copy of its standalone `lib.rs` seeds
`synthesis/impl/lib.rs`. The first planner inspects and profiles this seed and starts its first
brief from that implementation. Later redesigns require an evidence-based planner decision.

Defaults: **50 outer iterations**, **audit every 1**, **60% get / 40% put**, local machine,
100,000 preloaded keys, uniform access, up to 8 threads, 100,000 operations per worker,
3 measured repetitions and a warm-up. The last settings are configurable experiment defaults.
All five DB operations remain proof obligations, including scan and sort. Inner proof search
reviews its strategy every 10 attempts (`--proof-review-every`, legacy `--proof-attempts`) or
after 3 stalled attempts, with 300 seconds per Verus invocation. It keeps repairing proofs in
the same audit until verification succeeds or a necessary implementation change is diagnosed.
Those attempts are distinct from the 50 outer rounds and have no total-attempt cap.

## Setup and launch

Use Python 3.11+, Codex, and Verus (including its Rust toolchain and solver). These scripts use
only Python's standard library. From the repository root:

```bash
python3 experiments/db-verus/run.py prepare
python3 experiments/db-verus/run.py doctor
outputs/experiments/db-verus/.experiment/bin/python3 \
  outputs/experiments/db-verus/.experiment/baseline/baseline.py \
  --project outputs/experiments/db-verus --validate-only
outputs/experiments/db-verus/.experiment/bin/python3 \
  outputs/experiments/db-verus/.experiment/baseline/baseline.py \
  --project outputs/experiments/db-verus
python3 experiments/db-verus/run.py launch
```

If `python3` is older, prefix commands with `uv run --no-project --python 3.13 python` instead of `python3`.
`prepare` saves the interpreter and uses it for the workflow helpers/hooks. It installs roles
and guards only in `outputs/experiments/db-verus/`, creates a separate Git project, and refuses
to overwrite an existing experiment. It never edits the original spec. Override the Verus path
with `prepare --verus /path/to/verus`, or set `SKYDISCOVER_VERUS` before preparation.
The default specification is `experiments/db-verus/specification/specs.rs`, resolved relative
to the launcher. Use `prepare --spec /path/to/specs.rs` to select another compatible contract.
The default `--baseline` is `experiments/db-verus/baselines/node_locked_bptree`; preparation
copies it into `.experiment/baseline/source/`, along with frozen benchmark/trace helpers.
It also copies the standalone implementation into the run's writable `synthesis/impl/lib.rs`
and records its original hash in `.experiment/settings.json`. Preparation imports no old
plans, measurements, checkpoints, audits or knowledge-base state. The seed is not a completed
iteration and is not implicitly verified.
The tree keeps its original 32-key nodes, shared ancestor locks and exclusive leaf writes.
The baseline is runtime-tested, not formally proved; its measurements are separate from
candidate proof outcomes and do not spend synthesis iterations.

The first launch is interactive so you can review/trust this project's Codex configuration and
hooks. The launcher does not bypass trust, sandboxing, or approval. Codex requires trust for
non-managed hooks ([official documentation](https://developers.openai.com/plugins/build/plugins)).
The lead checks that an actual hook ran before beginning synthesis. The cadence is enforced
by `loop_guard.py`, not inferred from the prompt. An experiment hook checks frozen inputs,
records audit history, and blocks stopping before the last round's audit. The installed Stop
budget guard also discourages early exit. A paused/interrupted agent is resumable, not complete.

After reviewing hooks, optional headless launch and status:

```bash
python3 experiments/db-verus/run.py launch --headless --dry-run
python3 experiments/db-verus/run.py launch --headless
python3 experiments/db-verus/run.py status
```

`launch` again resumes the same **on-disk run** in a new Codex conversation; it does not reset
iterations or proof attempts. It archives an explicit pause after preflight succeeds. A pause
otherwise remains active across every Stop hook and retry; within an existing conversation use
`spec.run resume <run>` after resolving the blocker. Model selection is inherited from your Codex configuration unless
you pass `launch --model ...`. Headless runs save timestamped JSONL events, stderr, and the final
message under `.experiment/`. An incomplete run returns exit 3 even if Codex exited normally.
No model request is made by `prepare`, `doctor`, `status`, or `--dry-run`.

For a separate inexpensive smoke experiment:

```bash
python3 experiments/db-verus/run.py prepare --project outputs/experiments/db-smoke \
  --iterations 1 --keys 1000 --threads 2 --operations 1000 --repetitions 1
python3 experiments/db-verus/run.py launch --project outputs/experiments/db-smoke
```

## Exact contract binding

The supplied spec expresses atomic contracts inside `impl_db!`, not standalone ensures
expressions. `verus.json` therefore binds each `Database::{new,get,put,scan,sort}` obligation
to the macro exported by the frozen spec. Candidates invoke `crate::impl_db!` unconditionally
at crate root. The runner loads the original macro, checks that every target is present in
Verus's whole-crate result, and allows insertion-only proof annotations inside method bodies.
The task seeds `verus_exec_expr!` wrappers inside Rust block arguments, allowing later Verus
annotations to pass Rust's macro matcher without modifying the original specification.
No hand-translated, weakened, or tautological replacement contracts are used.

The coding agent writes a public `Database` and `lib.rs`; the remaining implementation layout
and algorithms are open. The runner supplies `__audit_spec`; candidates import it rather than
defining it themselves. See task.md for the invocation shape. `build.py` attaches the same frozen
spec for ordinary evaluator compilation. It deliberately uses `--no-verify` for measurement
builds; only the auditor's normal runner can produce proof evidence. Cargo implementations need
an equivalent evaluator build wrapper and a cached, locked dependency graph.

The spec-only `doctor` check does **not** prove DB methods: the macro must be instantiated by a
candidate first. Unsupported constructs or missing proofs remain recorded experiment outcomes.
The local toolchain has been exercised with an instantiated atomic contract and rejects failing
method bodies; this is infrastructure validation, not a proof of a database implementation.

## Workload and outputs

`trace.py --settings <project>/.experiment/settings.json --worker 0` writes JSONL operations to
stdout. Generate one trace per worker outside timing. Preload all keys, then run workers against
one shared database with a start barrier. Use `.experiment/baseline/benchmark.rs` for both
baseline and candidate timing. The frozen `baseline.py` also accepts `--impl <candidate-dir>`
to measure a candidate through that same harness, workload and build helper. Each invocation
creates a timestamped results directory; `--out` can select a new empty directory for paired
comparisons. Record raw throughput and speedup relative to a baseline measured under comparable
host load. `--smoke` is labelled separately and must never supply the scored denominator.
The evaluator builds its correctness oracle/tests as part of the normal pipeline;
preparation does not invent a passing reference or a candidate score.

Keep the entire experiment project to resume. Important files are:

- `.experiment/{manifest,provenance,settings,host,preflight}.json`: frozen-input hashes and setup.
- `.experiment/baseline/`: frozen baseline source, test/measurement helpers and fresh measurements.
- `.experiment/{audit-history,summary}.json`: round coverage and audit outcomes.
- `.skydiscover/db-verus/`: full live pipeline state, plans, benchmarks, and frozen audit sessions.
- `outputs/synthesize/`: checkpoints and, if the release checks succeed, the selected artifact
  with its replayable proof bundle.

50 completed rounds means 50 accepted audit outcomes, **not** 50 successful proofs: verification
or a structured implementation blocker backed by diagnostics and proof-only analysis. Failed
coding rounds count toward the budget and must also complete the audit on their last candidate.
Generic feedback, exhausted review intervals, and tool errors do not release another round.
Legacy history is retained for inspection but its failure/handoff records do not count as audit
completion. A run with no proved candidate preserves evidence and reports failure to verify;
it must not export an unproved candidate as verified.
