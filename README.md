## 1. Prerequisite dependencies

- Linux or macOS, with Git and Bash.
- [uv](https://docs.astral.sh/uv/) to run Python 3.13. The experiment scripts use only the Python standard library.
- Codex installed, authenticated, and available on `PATH`, with your desired model configured.
- Verus with its matching Rust toolchain and solver. Tested Verus commit:
  `8dea4a2196ebf99449fe2f141a2fb30acae3f17c`.

## 2. Configure the workload

Run from the repository root. Edit the paths and options below, then prepare a new experiment.
The specification and B+ tree baseline are included in the repository. `RUN` must name a new or
empty directory; preparation freezes the configuration and baseline.

```bash
RUN="outputs/experiments/db-proof-$(date +%Y%m%d-%H%M%S)"
SPEC="experiments/db-verus/specification/specs.rs"
BASELINE="experiments/db-verus/baselines/node_locked_bptree"
VERUS="verus"  # executable on PATH, or a path to your Verus executable

uv run --no-project --python 3.13 python experiments/db-verus/run.py prepare \
  --project "$RUN" \
  --spec "$SPEC" \
  --baseline "$BASELINE" \
  --verus "$VERUS" \
  --keys 100000 \
  --threads 8 \
  --operations 100000 \
  --repetitions 3 \
  --seed 20261017 \
  --iterations 50 \
  --coding-attempts 5 \
  --proof-review-every 10 \
  --proof-timeout 300
```

| Option | Controls |
|---|---|
| `--keys` | Number of preloaded keys |
| `--threads` | Concurrent workers |
| `--operations` | Operations per worker; must be divisible by 10 |
| `--repetitions` | Measured repetitions after one warm-up |
| `--seed` | Workload random seed |
| `--iterations` | Outer synthesis rounds |
| `--coding-attempts` | Maximum coding attempts per round |
| `--proof-review-every` | Proof attempts between strategy reviews; not a total attempt limit |
| `--proof-timeout` | Seconds per Verus invocation |

The current harness uses **60% get / 40% put with uniform key access**. The mix and distribution
are fixed in `experiments/db-verus/trace.py`, not configurable CLI options. All five DB operations
remain correctness and proof obligations. Preparation enables proof auditing after every round.

## 3. Run the full loop

```bash
"$RUN/.experiment/bin/python3" experiments/db-verus/run.py launch --project "$RUN"
```
