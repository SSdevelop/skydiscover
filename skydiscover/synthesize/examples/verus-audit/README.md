# Verificable Rust Loop

This example audits an existing Rust counter against a frozen Verus specification. The first
attempt lacks a loop invariant and termination argument. Adding `invariant i <= n, decreases n - i,`
proves the supplied contract without changing the implementation.

## Configuration

Set `audit_mode: proof` and `audit_target: counter.rs` in task.md. The target is relative to
`synthesis/impl/`. C++ always uses test auditing; Rust uses the requested mode. Other languages
retain test auditing by default, but a proof request for them is a blocker. Mixed-language targets
need an explicit selection. This setting is independent of the `checked_by: proof` synthesis path.

`specification/verus.json` fixes the Verus model file, obligation IDs, executable function names,
and requires/ensures clauses. The clauses are Verus expressions inserted into the frozen function
headers. For plain Rust return types the adapter adds the verification-only name `result`;
existing Verus named return bindings are preserved. The `.rs` spec module defines predicates
available through `spec::...`.

Every Rust candidate enters proof search, including modules, impls, traits, generics, attributes,
macros, unsafe code, and Cargo projects. The runner copies the entire frozen implementation and
specification trees into each attempt. It adds Verus wrappers and frozen contracts where it can
locate their targets; source it cannot prepare still reaches the real verifier. Preparation issues
and verifier diagnostics return to the proof planner. No task-shape allowlist decides admission.

For multiple files, an obligation may include `"file": "src/counter.rs"` and a fully qualified
`"function": "counter::Counter::get"`. The file defaults to the selected entry. Overlays may also
include `"file"`; token indices are local to that file, and `tokens` lists every Rust file. Frozen
contracts are never rewritten by proof workers. Generated verification views preserve executable
source; source or build changes needed for verification return through the auditor.

Cargo targets use the pinned `cargo-verus` with the candidate's Cargo manifest and lockfile.
Only verification metadata and the pinned vstd link are added to the generated view. Cargo runs
with `--offline --locked`; missing cached dependencies or a missing lockfile produce actionable
build diagnostics, rather than excluding the task or falling back to test evidence. Plain targets
use Verus directly. The runner records both Cargo metadata diagnostics and verifier output.

Attempting is distinct from accepting: success requires all required targets and their attached
contracts to be checked, no unresolved preparation issues, unchanged generated inputs after the
build, and successful whole-crate verification. A macro-generated target that cannot yet be
annotated is still attempted, but cannot be declared proved merely because Verus exits zero.

For a specification that exports a contract macro, an obligation can instead use
`{"id": "db_get", "function": "Database::get", "macro": "impl_db"}`. This uses the complete
contract from the frozen macro, without `requires`/`ensures` overrides. The supported binding
form is an unconditional, unattributed crate-root `crate::impl_db! { impl Database {
get(db, key, lp) { ... } ... } }` invocation. Verus expands the original macro from the appended
`__audit_spec` module. Proof overlays can annotate its method bodies. Other macro forms still
reach Verus but cannot be accepted without a resolved contract binding. See
`experiments/db-verus/README.md` for a full performance experiment using atomic contracts.
Macros resolve some identifiers at their call site. Use top-level `macro_imports` in verus.json
to bind these names explicitly, e.g. `{"DatabaseState": "crate::__audit_spec::DatabaseState"}`.
Paths may refer to the frozen model, vstd, or std. The runner appends these imports to the entry;
shadowing definitions become compiler errors. The evaluator build must use the same bindings.

Strategy review triggers are `attempts_per_review` (default 10) and `max_stalled` (3).
`max_attempts` is accepted as a legacy alias for the review interval, including existing frozen
sessions; it no longer caps total proof search. Per-invocation limits remain `timeout_secs` (120)
and `rlimit` (10).
Install Verus separately; set `SKYDISCOVER_VERUS` if it is not on PATH. Z3 must be available through
the Verus installation, PATH, or `VERUS_Z3_PATH`. The runner pins the verifier version and installed
verifier, library, and solver hashes. It controls verification flags, including `--no-cheating`,
whole-crate checking, lifetime checking, and erasure checking.

Review the supplied model, preconditions, and requirement mapping before auditing. A successful
proof establishes those exact contracts, not that prose was translated correctly or that a
contradictory precondition is meaningful.

## Running the example

Copy this directory to a working run directory, then use that path as `<run>`:

```text
python3 -m skydiscover.synthesize.spec.proof start <run>
python3 -m skydiscover.synthesize.spec.proof check <run>
python3 -m skydiscover.synthesize.spec.proof tokens <run>
```

`start` returns the session directory. The first `check` returns incomplete with Verus diagnostics.
In the token listing locate the `{` opening `while i < n`. Write the following to the session's
`proof/overlay.json`, replacing `42` with that token's actual integer index:

```json
{
  "insertions": [
    {
      "before": 42,
      "kind": "invariant",
      "text": "invariant i <= n, decreases n - i,"
    }
  ]
}
```

All indices refer to the fixed source before overlays; insertions do not shift other indices.
Then run:

```text
python3 -m skydiscover.synthesize.spec.proof check <run>
python3 -m skydiscover.synthesize.spec.proof replay <run>
python3 -m skydiscover.synthesize.spec.checkpoint stamp-audit <run>
```

`workflow/scripts/run_verus.py --run <run>` performs one attempt; `--replay` checks accepted evidence
again without consuming proof-search budget. Use the installed scripts path outside this directory.

Permitted overlays insert balanced proof blocks, loop annotations, fresh ghost bindings, fresh
helper proof functions, or checked helper postconditions. They cannot remove or replace source tokens. Verification source is
regenerated from frozen inputs on every check and replay; editing generated files cannot change
the proof that gets accepted.

Use `kind: "helper_ensures"` at an executable helper's opening body brace to add, for example,
`ensures audit_result.0 <= keys.len(), audit_result.1 ==> audit_result.0 < keys.len(),`.
The adapter names an unnamed return `audit_result` using inserted ghost syntax; an existing
named return keeps its original name. The helper body must prove its postcondition during
whole-target verification. This supports modular proofs without editing executable code.
Existing contracts, authoritative obligation functions, preconditions, and executable bodies
cannot be changed by this insertion. An incorrect summary fails verification.

Proof overlays support the complete public atomic-update and invariant proof macro API
of Verus commit `8dea4a2196ebf99449fe2f141a2fb30acae3f17c`:

| Module | Macro | Behavior |
|---|---|---|
| `vstd::atomic` | `open_atomic_update_in_proof!` | Commits the update, wrapping the body's tracked value in `Commit` |
| `vstd::atomic` | `try_open_atomic_update_in_proof!` | Takes a body returning `Tracked<Y>` and returns `Tracked<Result<(), AtomicUpdate<...>>>`; an abort returns ownership of the update |
| `vstd::atomic` | `peek_atomic_update_in_proof!` | Wraps the body's tracked value in `Err`, checks the abort postcondition, and returns the tracked update for later reuse |
| `vstd::invariant` | `open_atomic_invariant_in_proof!` | Opens and restores an atomic invariant, consuming an existing credit |
| `vstd::invariant` | `open_local_invariant_in_proof!` | Opens and restores a local invariant, consuming an existing credit |

Calls may be unqualified, module-qualified, or crate-root-qualified (`vstd::macro_name!`),
with an optional leading `::` on qualified paths. Raw identifiers are also supported.
The adapter checks the entire payload, including nested calls, and binds each macro to
the pinned vstd through an explicit crate alias. Candidate macros cannot replace these
bindings; collisions with reserved adapter names are compiler errors.

The `open` and `peek` compatibility wrappers work around this revision's missing
`au_commit_wrap_proof`, `au_abort_wrap_proof`, and `au_abort_unwrap_proof` helpers using
the real `try_open_atomic_update_in_proof!`. They preserve raw payload tokens for Verus
syntax such as `@`, `@@`, and `let tracked`. For example, a result-valued atomic contract
can abort and then commit in the same proof:

```rust
let tracked again = peek_atomic_update_in_proof!(lp, state => { state });
let tracked outcome = try_open_atomic_update_in_proof!(again, state => {
    Tracked::new(Result::<int, int>::Ok(state))
});
assert(outcome@ is Ok);
```

Invariant calls use `macro!(credit => &inv => inner => { ... })`. The credit must already
exist; creating it requires an executable call outside the proof overlay. Verus checks
the atomic postconditions, invariant restoration and masks, linear resource use, and
proof erasure. The adapter introduces no assumptions or unchecked proof functions.
Executable and internal expansion macros remain unavailable in proof overlays, as do
other macros, attributes, imports, assumptions, and executable declarations.

The API registry lives in `spec/proof_source.py`. To check its coverage against an upstream
source checkout or extracted archive, set `SKYDISCOVER_VERUS_SOURCE` to that directory and
run `pytest tests/spec/test_atomic_proof_api.py`. The coverage test scans all of vstd for
public proof-mode macro definitions; integration tests in `test_proof.py` exercise all five
macros with the real verifier, including rejection and replay cases.

## Results and handoffs

`status` reports the current outcome, directory, attempt count, and next action. Attempts retain
their generated source and raw diagnostics. Unchanged failed overlays cannot be repeated. After
the no-progress limit or review interval the proof planner must revise its plan and continue
the same audit; restarting never resets attempt history. An interrupted attempt remains recorded.
Proof search continues until success or a diagnosed necessary implementation change.

An unsupported result identifies a specific diagnostic, not a verdict that unsafe code as a class
is unprovable. An incomplete proof is not a confirmed defect. Missing tools and stale inputs block
acceptance. A compilation blocker affects this whole target; independent targets must be audited
separately rather than silently excluding a failing module.

Generic `spec.proof feedback <run> --owner planner|spec-builder --reason "..."` records a request
without releasing the next implementation round. Operational and spec issues must be resolved
within this audit or explicitly paused for resolution.

For a necessary implementation change, the auditor writes a blocker file, for example:

```json
{
  "diagnostic": "attempts/0004/verification/diagnostics.txt",
  "implementation_change": "Return the computed counter instead of the literal zero.",
  "proof_only_analysis": "The n=1 execution returns 0. Loop-invariant repair cannot change that return value."
}
```

Then run `spec.proof feedback <run> --owner planner --reason "wrong return expression"
--blocker <file.json>`. Cite the latest diagnostic and explain the attempted proof strategies,
specific required change, and why proof-only work cannot resolve it. The runner validates and
hash-binds the diagnostic and feedback; assessing necessity remains the auditor's responsibility.
The lead hands this evidence to the main planner for redesign in the next round. Proof workers
own only the proof plan and overlay. Changed code or specs require new frozen sessions.

Only success or a structured implementation blocker records scheduling completion in
`audit/completed.json`. Failed checks, timeouts, unsupported results, and legacy generic handoffs
cannot release the outer loop. A blocker grants no correctness coverage: only accepted evidence
can produce the release audit stamp.

Test-mode auditing also starts a frozen session and calls `finish-test` before stamping. The
ordinary suite and defect checks still apply. New runs declare `audit_mode`; legacy runs without
that field or a session retain their existing audit format.

Publication carries the session under `.verification/run/synthesis/audit/` and replays the accepted
proof against relocated source, including exports without a production-ready claim. This example
demonstrates auditing; it is not a performance benchmark or a production-readiness assessment.
