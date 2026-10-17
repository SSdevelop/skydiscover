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
  proof.py                frozen audit sessions, routing, bounded Verus attempts, feedback and replay
  proof_source.py         insertion-only proof overlays over frozen Rust source
  render.py               best/spec.md, filled from the cards
  paths.py                where runs, results, and the knowledge base live (config.toml)
```

Most modules are also commands:

```bash
python3 -m skydiscover.synthesize.spec.decisions <run_dir> list
python3 -m skydiscover.synthesize.spec.run check <run_dir>
```

To point the agents at a new kind of system, see [Adding a domain](../examples/README.md#adding-a-domain).

For Verus audit configuration and a runnable Rust example, see
[Verificable Rust Loop](../examples/verus-audit/README.md).
