# Specification Modules

The agents call these to write the specification, record every decision, and publish the result.
Stdlib-only Python, tested in `tests/spec/`.

```
spec/
  requirements.py         the questions about what the system must do (questions.json)
  decisions.py            the answers, and who decided each (rows in the decision log)
  findings.py             the decision log itself: answers, spec gaps, and reward hacks found during a run
  build.py                assembles the requirements card from spec.json and the answers
  failure_patterns.py     a reference system's bug history, turned into things to test for
  kept_tests.py           tests saved per domain in the knowledge base
  run.py                  `run check` before the loop, `run finish` after it
  checkpoint.py           each scored iteration, and the best one published under outputs/synthesize/
  evaluation.py           full proof/integration check, then benchmark on the same input snapshot
  evaluation_authority.py approved evaluation under separate UIDs, with protected score records
  ids.py                  candidate-bound proof-search blockers for the outer planner
  render.py               best/spec.md, filled from the cards
  paths.py                where runs, results, and the knowledge base live (config.toml)
```

Most modules are also commands:

```bash
python3 -m skydiscover.synthesize.spec.decisions <run_dir> list
python3 -m skydiscover.synthesize.spec.run check <run_dir>
```

For co-generation before performance evaluation, task front matter includes `checked_by: proof`
and `optimize: performance`. After IDS produces a candidate, invoke the administrator's fixed
trusted-evaluation wrapper; see [Linux/WSL setup](../TRUSTED_EVALUATION.md). The returned receipt
and measured fields accompany its leaderboard row. Local `spec.evaluation run` is an unsigned
debugging path. The trusted evaluator executes the full proof suite before the benchmark. Checkpoint creation
replays that suite and retains the measured code-and-proof snapshot. `spec.ids feedback` records
diagnosed search blockers; `spec.ids blockers` distinguishes current reports from history.
Existing proof-only tasks (no `optimize` key) and test-driven scoring retain their behavior.

To point the agents at a new kind of system, see [Adding a domain](../examples/README.md#adding-a-domain).
