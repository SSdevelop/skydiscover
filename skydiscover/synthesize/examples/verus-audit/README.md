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

Limits are `max_attempts` (default 10), `max_stalled` (3), `timeout_secs` (120), and `rlimit` (10).
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

Permitted overlays insert balanced proof blocks, loop annotations, fresh ghost bindings, or fresh
helper proof functions. They cannot remove or replace source tokens. Verification source is
regenerated from frozen inputs on every check and replay; editing generated files cannot change
the proof that gets accepted.

## Results and handoffs

`status` reports the current outcome, directory, budget usage, and next action. Attempts retain
their generated source and raw diagnostics. Unchanged failed overlays cannot be repeated. After
the no-progress limit the proof planner must revise its plan; restarting does not reset the total
budget. An interrupted attempt consumes its reserved slot.

An unsupported result identifies a specific diagnostic, not a verdict that unsafe code as a class
is unprovable. An incomplete proof is not a confirmed defect. Missing tools and stale inputs block
acceptance. A compilation blocker affects this whole target; independent targets must be audited
separately rather than silently excluding a failing module.

Return a versioned request through `spec.proof feedback <run> --owner planner --reason "..."`, or
use owner `spec-builder` for spec questions. The auditor hands it to the lead, which invokes the
main planner to update the main plan. Proof workers own only the proof plan and overlay. Code or
spec changes create new sessions.

An audit result or explicit outer-planner handoff records scheduling completion in
`audit/completed.json`. This lets the main loop repair an unresolved candidate. It does not grant
correctness coverage: only accepted evidence can produce the release audit stamp.

Test-mode auditing also starts a frozen session and calls `finish-test` before stamping. The
ordinary suite and defect checks still apply. New runs declare `audit_mode`; legacy runs without
that field or a session retain their existing audit format.

Publication carries the session under `.verification/run/synthesis/audit/` and replays the accepted
proof against relocated source, including exports without a production-ready claim. This example
demonstrates auditing; it is not a performance benchmark or a production-readiness assessment.
