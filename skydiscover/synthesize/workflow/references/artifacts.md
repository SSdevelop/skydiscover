# Run Files

Where a run writes its state, who owns each file, and what gets published. Sequencing is in
`../SKILL.md`; the layout is defined once, in `spec/paths.py`. Three places, one word each:

| Place | Path | What it is |
|---|---|---|
| the result | `outputs/synthesize/<slug>_<timestamp>/` | `best/` (`artifact/` the system, `tests/`, `score.json`, `spec.md`), `checkpoints/`, and `history.json`; what the user keeps |
| the run | `.skydiscover/<slug>/` in the project | the agents' working files, grouped by phase; kept after publishing (`run finish --delete-run` removes it) and snapshotted whole into `<kb>/runs/` at every checkpoint |
| the knowledge base (`<kb>`) | `.skydiscover/kb/<domain>/` in the project; `spec.paths domain "<name>"` prints it | what every run in the project learned: tests, decisions, wiki, `runs/` (every iteration of every run), and `versions/` (the knowledge base as each run left it, one version per run) |

## The Run Directory

Create it once with `python3 -m skydiscover.synthesize.spec.paths run <slug>` (it prints the path,
makes the three phase folders, and writes a `README.md` that tells a reader where the result went)
and reuse the path. The default root is `.skydiscover/`; `config.toml` or `$SKYDISCOVER_RUNS` may
move it. A fresh agent must be able to resume from this directory alone, so nothing run-specific
goes anywhere else in the project.

```text
.skydiscover/<slug>/
├── README.md              what this directory is and where the result went (generated)
├── task.md                what the user asked for; front matter names the domain (see below)
├── decision_log.json      every question, its answer, who decided it, and every finding, in order
├── report.md              the final report, for the lead's closing message; kept in best/.verification/run/
├── specification/         Phase 1
│   ├── sources/           the reference systems examined
│   ├── references/        what was learned from them
│   ├── questions.json → answers.json → spec.json
│   └── cards/             requirements.json  properties.json  workload.json  environment.json
├── synthesis/             Phase 2
│   ├── plan.md            the designs considered, the ones ruled out, the current brief
│   ├── impl/              the candidate
│   ├── evaluator/         its interface, benchmark, trusted reference, mutants
│   ├── tests/             the kept tests
│   ├── bench/             the leaderboard, raw runs, profiles
│   └── audit/             the reward-hacking audit
└── review/                Phase 3: security.md, reliability.md, attack.md
```

The front matter of `task.md` carries two keys the helpers read:

```yaml
---
domain: job scheduler     # the knowledge base folder this run reads and feeds: .skydiscover/kb/job-scheduler/
checked_by: proof         # only for a formal-proof-driven run; omit for the test-driven path
---
```

### `specification/` (Phase 1)

| Path | Owner | Purpose |
|---|---|---|
| `sources/<name>/` | spec-builder (discovery) | the cloned reference systems, one real `git clone` each (or a symlink into the shared clone cache, `<home>/.cache/sources/`) |
| `references/<name>/` | spec-builder (discovery) | what was extracted from each source: `workflow.md` (the code map), `spec.json` (`{title, source, purpose, interface, axes}`), `design_principles.json`, `properties.json`, `decisions.json`, `verification.{md,json}` (every `file:line` re-checked), `failure_patterns.json` |
| `references/tests/<name>/`, `references/tests.json` | spec-builder (discovery) | the sources' real tests behind each property, copied with the `file:line` each came from, and the index |
| `references/skeleton.json` | spec-builder (discovery) | the module structure the reference systems share, handed to the coding agent |
| `references/axes.json` | spec-builder (discovery) | the domain's property axes, each with the rationale for asking it, and `mechanisms`: the design words a question may not use |
| `references/literature.json` | spec-builder (discovery) | papers, RFCs, and design docs consulted, when the code alone does not settle the design space |
| `references/acquisitions.json` | spec-builder (discovery) | sources reused from the wiki instead of cloned, with the page that pins each |
| `spec.html` | spec-builder (discovery) | optional, only when the user asks to read the spec: a one-screen rendering of the axes and properties with `file:line` links |
| `questions.json` | spec-builder (discovery) | the property questions a from-scratch build must answer, consolidated across sources |
| `answers.json` | `decisions` (exported from the decision log) | the current answer to each question, and who gave it |
| `spec.json` | spec-builder (requirements mode) | the consolidated specification the cards are built from |
| `cards/environment.json` | spec-builder (environment mode) | the resource that bounds the score, its measured limit here, and the ceiling that implies, in the benchmark's unit |
| `profiling/` | spec-builder (workload mode) | scratch: the scripts and dumps used to measure the trace; nothing downstream reads it |
| `cards/workload.json` | spec-builder (workload mode) | the workload, measured in the domain's own terms: `{source, size, dimensions, scored_configuration}` |
| `cards/requirements.json` | `spec.build`, then spec-builder | the interface, the guarantees, the required properties, and the operating point (the spec-builder fills the operating point from the scored configuration and names its checker) |
| `cards/properties.json` | spec-builder (hardening mode) | each requirement as something a test can falsify: `[{id, property, probe, oracle}]`; the evaluators (correctness mode) work from it |

The cards are what the synthesis loop reads; everything above them is how they were grounded. `run check` requires `sources/` (a git clone) or `references/acquisitions.json`,
`references/<name>/spec.json` with `verification.json`, `references/tests/`, `references/tests.json`,
and `references/skeleton.json`. It reports whether `cards/workload.json` and `cards/environment.json`
are present but requires neither: the Spec Builder (workload and environment modes) writes them when
the run has a trace, and when the benchmark scores a rate, a latency, or a cost.

### `synthesis/` (Phase 2)

| Path | Owner | Purpose |
|---|---|---|
| `plan.md` | planner (critic appends to `## Learnings`; on the proof path the ISA rewrites `## Brief`) | the candidate designs, the ones ruled out with the numbers that killed them, and the current brief: the one next change |
| `proof-log.md` | dsa, isa | formal-proof-driven runs only: every proof attempt and why it failed |
| `impl/` | coding-agent (or dsa) | the current candidate: one source file, or a modular codebase built whole (a Python one holds `__init__.py`); `SKYDISCOVER_IMPL` names one file when several candidates sit here |
| `evaluator/interface/` | first coding agent | the public contract the candidate implements and the tests compile against |
| `evaluator/benchmark/` | first coding agent | the scored benchmark harness |
| `evaluator/reference/`, `evaluator/mutants/` | evaluator (correctness mode) | the trusted reference every test must pass, the broken variants every test must catch |
| `tests/` | evaluator (correctness mode), auditor | the suite: one file per kept test, any language, and `test.sh`, which builds and runs them against `$SKYDISCOVER_IMPL` (`bash test.sh [file...]`, exit 0 when every test passes) |
| `bench/leaderboard.json`, `bench/runs/` | evaluator (performance mode) | one entry per scored candidate, and the raw output of each run |
| `bench/profiles/` | evaluator (performance mode) | the bottleneck analysis of each scored candidate |
| `audit/<id>/`, `audit/completeness.json` | auditor | evidence per confirmed hack, and the stamp (`spec.checkpoint stamp-audit`) that the audit covered these implementation bytes |

### `review/` (Phase 3)

| Path | Owner | Purpose |
|---|---|---|
| `review/security.md`, `review/reliability.md` | auditor (production lens mode) | one pass per lens: each finding with its `file:line`, its severity, and where it was routed (a probe sketch or the decision log) |
| `review/attack.md` | auditor (attack mode) | one line per attack class: the defect with its repro, or what was tried and held |
| `../report.md` | lead | the final report, at the top of the run directory; the lead's closing message draws on it |

One hidden file, `.severity_snapshot.json` at the top of the run, records the severity each finding
was first written with (the findings CLI keeps it), so a defect cannot be silently downgraded
before delivery; nobody edits it.

The exact files vary by domain. To repair another role's file, re-run that role. A file not in
these tables is not part of the run: name it here first or do not write it.

## Environment

The lead exports `SKYDISCOVER_RUN`; the delivery hook derives every other path from the layout.

| Variable | Meaning |
|---|---|
| `SKYDISCOVER_RUN` | the run directory |
| `SKYDISCOVER_PRODREADY=1` | same as `--production-ready`: also run the release checks on a candidate: no open defect, audit current |
| `SKYDISCOVER_IMPL` | the delivered candidate (a file or a directory), only when `synthesis/impl/` holds several |
| `SKYDISCOVER_RUNS`, `SKYDISCOVER_OUTPUTS`, `SKYDISCOVER_HOME` | move the runs folder, the results folder, or the knowledge base root (defaults in `skydiscover/synthesize/config.toml`) |

Time budgets (`SKYDISCOVER_TEST_MAX_SECS`, `SKYDISCOVER_SLOW_SECS`) are documented in
`../scripts/README.md`.

## Knowledge Base

`run finish` saves what outlives the run into the domain's knowledge base, `<kb>`: by default
`.skydiscover/kb/<domain>/` (`SKYDISCOVER_HOME` or `config.toml` moves the root), the folder named
by the task's `domain:`. The same spelling rule (`spec.paths domain <text>`) is used
everywhere, so runs in one domain find each other.

```text
<home>/                             <project>/.skydiscover/kb by default
├── <domain>/                       <kb>
│   ├── tests/                      kept tests from finished runs, with index.json, the latest test.sh, and helper subdirectories
│   ├── decisions.json              the user's answers and confirmed reward hacks, saved from decision logs
│   └── wiki/                       optional pages kb-builder writes: sources, properties, benchmarks, profiling, tests, hacks, designs
├── shared/wiki/                   pages that hold for every domain
└── .cache/sources/<owner>-<repo>/  cloned reference systems, shared across runs; re-downloadable
```

The wiki is optional: kb-builder writes it once, when the domain has none and the budget is Standard
or Thorough; a run works without it. The page format and its checker are in `../scripts/kb/`.

## The Published Result

Working files are not the result. Every scored candidate is checkpointed:

```bash
python3 -m skydiscover.synthesize.spec.checkpoint snapshot <run> [--became-best]
```

and `run finish --export-to .` publishes the final checkpoint and `best/`. `snapshot` writes under
the project holding the run; an explicit `--export-root` or `--export-to` is resolved from the
caller's current directory:

```text
outputs/synthesize/<slug>_<timestamp>/          # timestamp: date and time the result was first published
├── best/
│   ├── spec.md            start here: what was asked, what was decided, and the test that checks each answer
│   ├── artifact/          the generated system: the candidate plus its interface, so it compiles standalone
│   ├── tests/             the kept tests
│   └── score.json         its score against the baseline
├── checkpoints/
│   └── checkpoint_<N>/    one per scored iteration
│       ├── artifact/
│       ├── score.json
│       └── tests.json     the test files it was scored against
└── history.json           written at finish: one row per checkpoint, with the tests it fails today
```

Nothing in the result is written by a role.

| File | Written by | What it holds |
|---|---|---|
| `score.json` | `snapshot`, from the leaderboard | `score`: the candidate's declared objective, from a measurement whose inputs match the saved ones. `baselines`: one entry per baseline measured with the same task, specification, evaluator, and configuration (the measurement taken beside this candidate when there is one, else the newest; the baseline the candidate names comes first). `became_best`: whether `snapshot` ran with `--became-best`. Every other measured number stays in `bench/leaderboard.json`. |
| `spec.md` | `spec/render.py`, from the cards and the decision log | a template, one sentence per cell: each property's question, answer, and test; the properties every run requires; the scored workload; the environment and its ceiling; the reward hacks found and the test that closes each; the checkpoint table. The decision log, the benchmark, and the profiles stay in the run directory. |
| `.verification/` (hidden) | `snapshot`, `run finish` | what the checks ran on: checkpoint entries, input hashes, the original source, the evaluator files. `best/.verification/run/` also keeps the final task, specification, implementation, evaluator, tests, decisions, leaderboard, audit, reviews, and report; `checks.log` records the independent final check; the raw benchmark runs stay in the run directory and in its `<kb>/runs/` snapshots. |
| `history.json` | `run finish`, once | one row per checkpoint. From the checkpoint: `checkpoint`, `created_at`, `score`, `became_best` (the loop's call at the time), `tests` (a count; the checkpoint's `tests.json` names them, so the tests added at iteration N are the difference from N-1). From finish: `fails`, the tests the checkpoint fails against the final suite (`[]` when it passes, `null` when the tests could not run), and `published: true` on the row in `best/`. |
| `artifact/` | `snapshot` | the candidate with the source files of `evaluator/interface/` folded in, so it compiles standalone. Lockfiles are kept; caches, clones, transcripts, and profiles are not. A symlink or a dependency outside the run must become an explicit file, and an incomplete copy is rejected. |

Every test runs once against every checkpoint, all at the same time (`test.sh <file>`), and
identical bytes with the same entry and evaluator are tested once. Historical scores are never rewritten; `best/score.json` is
re-measured only if the final checks changed the scoring inputs. A checkpoint that outscored
`best/` but fails a later test is what a reward hack looks like in the history, and `fails` names
the test that closed it (see the hacks table in `spec.md`).

Nothing a run produced is deleted:

- the whole run directory is snapshotted into `<kb>/runs/<slug>_<timestamp>/snapshots/`
  (`spec/archive.py`) at every checkpoint, whenever a role finishes, whenever a lead turn ends,
  before a compaction, when a session ends, and at `run finish` (whether or not the export
  passed). Each snapshot is named for its iteration and what just happened
  (`0007_iter-03_after-coding-agent`, `0008_iter-03_checkpoint_3`), so an attempt that failed its
  tests and was never scored is kept too. Iteration K is the work that ends in checkpoint_K;
  iteration 0 is the specification. An unchanged run adds no snapshot; unchanged files are hard
  links to the previous snapshot; clones are recorded as url and commit.
- the Claude Code transcripts of every session that worked on the run (lead, subagents, persisted
  tool results) are mirrored into `<kb>/runs/<...>/transcripts/<session>/`, so they outlive Claude
  Code's own transcript cleanup; each snapshot's manifest records how far each transcript had
  grown, which ties a snapshot to the conversation behind it.
- `<kb>/runs/<...>/index.json` and `iterations.md` hold the per-iteration table: its snapshots,
  its checkpoint and score, and the tokens it spent, in total and by role.
  `spec.archive list <domain>` prints it; `spec.archive restore <snapshot> <dest>` copies a
  snapshot back out as a working run directory.
- `specification/sources/` stays; shared clones under `<home>/.cache/sources/` stay too.
- the run directory stays (`--delete-run` removes it, only after its final snapshot is safe) and
  `.skydiscover/<slug>.done` records where the result went; while the run is alive,
  `<run>/.output` holds the same path and `<run>/.archive` names its snapshots.
- `token_usage.json` (see below) is copied beside `history.json` in the published result.
- a failed or unavailable final check blocks publication and keeps the working files, and a
  proof-driven run always keeps them for proof replay.

### Guards

- `hooks/loop_guard.py` (PreToolUse on Task/Agent): an iteration gets at most
  `max_coding_attempts` coding-agent launches (`budget.json`, default 5); at the cap the round is
  closed with `spec.iterations fail` and counts toward the budget. The auditor runs during the loop
  only every `audit_every` iterations (default 15); Phase 3 reviews always run. Decisions go to
  `<run>/loop_guard.log.jsonl`. Finished iterations, scored or failed, are in
  `synthesis/bench/iterations.json`.
- `spec.reuse <run>` (Step 1) copies the newest earlier run's discovery of the domain from
  `<kb>/<domain>/runs/` and writes `specification/references/reuse.json` (fresh vs to re-verify).

- `hooks/watchdog.py` (PreToolUse on Bash): when the loop is under way, no role is working, and
  nothing under `synthesis/` or in any role's transcript has changed for four hours
  (`SKYDISCOVER_STALL_SECS`), the lead's next shell command is refused once (at most every 30
  minutes) with an instruction to act on the newest result: checkpoint it or close the iteration.
  Refusals go to `<run>/watchdog.log.jsonl`.
- `hooks/budget_guard.py` (Stop): during the synthesis loop the lead may not end its turn while
  fewer iterations are finished than `<run>/budget.json` (`spec.run budget`) allows, unless a
  role is still working (waiting for it is allowed; Claude Code wakes the lead when it ends),
  `spec.run pause` asked for one stop (kept afterwards as `pause.<time>.json`), the run is done,
  or it has already refused five times without a new checkpoint. Every decision is logged to
  `<run>/budget_guard.log.jsonl`.
- `hooks/history_guard.py` (PreToolUse): agents may not delete, move, or overwrite the knowledge
  base, a run's snapshots or versions, or a checkpoint. Editing a knowledge-base file is allowed,
  and the previous version is first kept under `<kb>/.history/<time>/`. Bytecode, `*.tmp` files,
  and `<kb>/.cache/` are exempt.
- A hook error is written to `<run>/hook_errors.log`, else the runs folder's, else a per-user file
  in the temp directory, and `run finish` prints any it finds.

### Token usage

`hooks/token_usage.py` runs when a turn, a subagent, or a session ends and before a compaction
(Claude Code), detached so it never slows the agent. It recounts the session's transcript and its
subagents' transcripts, each message once, and rewrites `<run>/token_usage.json`: the run's total
(input, output, cache read, cache write, and their sum) per session, per role, per model, and per
iteration with the roles inside each (a message is filed by its timestamp: after checkpoint_(K-1),
up to checkpoint_K). Each firing appends the event, the role that finished, the iteration, and the
running totals to `<run>/token_usage.log.jsonl`, then takes the snapshot above. Errors go to
`<run>/hook_errors.log`. `run finish` prints the total. The snapshots need the skydiscover package
importable by `python3` (`pip install -e <checkout>`); without it tokens are still recorded. To rebuild the token usage file of an earlier
run from its transcripts: `python3 <hooks>/token_usage.py <run>`.
