Use the installed skysynth skill to run the full pipeline for the existing run:
{{RUN}}

Read task.md, budget.json, specification/specs.rs, and specification/verus.json there first.
The task contains the settled user choices and declared experiment defaults. Do not recreate
the run, reset the budget, change those frozen files, or ask the settled questions again.
On a relaunch, continue from the persisted state and audit feedback, not iteration 1.

This fresh run already contains the user's B+ tree in `synthesis/impl/lib.rs`. Read the
`initial_implementation` record in `.experiment/settings.json`. Empty iteration/proof history
does not mean an empty implementation. Begin from that exact seed, preserving the separate
frozen performance baseline. During Phase 1 inspect/profile it; the first planner must read
its code and evidence, describe its design and bottlenecks, and retain it in the first brief.
Bootstrap only missing interface/harness infrastructure. Do not substitute a sorted vector or
another newly written architecture at startup. Later pivots need an explicit evidence-based
planner decision. The seed still needs the full correctness evaluation and Verus audit.

Start by running a harmless shell command and confirm `.experiment/hooks-seen.json` exists.
If absent, pause and tell the user to review/enable the project hooks in Codex; do not start
synthesis without the scheduling guard. This probe does not replace the actual guard.

Perform the remaining Phase 1 work with the spec-builder, then `spec.run check`, evaluator
setup, the configured number of iterations, and final review. The supplied formal specification
is authoritative; discovery may inform implementations but cannot change its semantics.
Use role agents for the pipeline and the proof subloop. The evaluator still performs tests
and performance measurement; the auditor persists in the Verus proof loop until verification
succeeds or a diagnosed necessary implementation change ends the audit. Review intervals
require revised proof strategies, never automatic advancement to the next outer round.

Experiment helpers live at {{HELPERS}}:
- trace.py generates reproducible per-worker JSONL traces from `.experiment/settings.json`.
- build.py compiles a plain Verus crate with the frozen spec attached, for evaluator use.
  Compilation with --no-verify is not proof evidence. Cargo layouts need an equivalent build
  wrapper and an offline locked dependency setup; retain the exact candidate source.
- The frozen .experiment/baseline/baseline.py validates/measures the existing leaf-locked B+ tree.
  Use its benchmark.rs and trace.py for candidate timing too; --impl selects a candidate.
  Preserve paired measurements and report speedup against this baseline, never a legacy score.
- run.py status --project . reports iteration/audit coverage and saves summary.json.

The code guard schedules audits; obey its decisions. Every finished round, including a failed
round, needs verified evidence or a structured implementation blocker before the next round.
The auditor records the latter with `spec.proof feedback --owner planner --reason "..."
--blocker <file.json>`; the file names the latest diagnostic, the required implementation change,
and why proof-only repair cannot resolve it. The main planner handles that change next round.
Timeouts, unsupported results, tool errors, and generic feedback keep the audit pending.
In particular, complete the last round's audit before final reporting. Use the existing
run/proof/iteration helpers to record outcomes; do not fabricate completion stamps.

If you need user input or encounter an installation blocker, use `spec.run pause` with the
concrete reason and retain partial progress. Do not rewrite framework files to bypass a blocker.
The pause remains active across Stop-hook retries; the launcher archives it on explicit relaunch.
Helper functions without return summaries support checked `helper_ensures` overlays. Consult
the installed proof-coder brief before treating missing helper contracts as an adapter blocker.
At the end, report the number of end-to-end rounds, verified/incomplete/unsupported outcomes,
selected throughput, and artifact paths. Failure to find a proof is a valid experiment outcome.
