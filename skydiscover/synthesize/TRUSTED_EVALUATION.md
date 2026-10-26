# Trusted performance evaluation on Linux and WSL

Scored IDS accepts only receipts issued by the trusted evaluator. A local
`spec.evaluation run` is useful for debugging, but its unsigned receipt cannot create a scored
checkpoint or pass final export. Missing trusted evaluator setup fails closed; there is no environment
variable or per-run flag that opts out of authentication.

## Permission boundary

The trusted evaluator keeps canonical receipts under `/var/lib/skydiscover/trusted-evaluation/receipts/`.
The directory, records and their parent directories must belong to root and must not be writable
by the agent. Verification compares the complete public receipt with this protected record.
Recomputing public log hashes does not let an agent update the canonical copy. No signing secret
is needed on this single-host path. Export verification on another host requires a trusted copy
of this store; copying an agent-supplied store is not authentication.

Use three identities:

- The agent's ordinary Linux user edits the candidate and receives public evidence.
- The root trusted evaluator runs from administrator-owned installed code and owns policy and records.
- A different unprivileged worker user runs both checker and benchmark, with no supplementary
  groups. It receives only the approved environment and writable build directory. Candidate
  inputs and receipt storage are not writable by this user.

The trusted evaluator reads the mutable run directory as the agent UID, extracts only regular files and
directories into a new root-owned snapshot, and pins the task, specification, checker, evaluator,
entry, command and configuration. It never runs candidate code as root or writes as root into the
agent's run directory. The public result is copied back as the agent UID. The returned JSON contains
the exact measured fields to append to the leaderboard.
Failed checks or measurements return their snapshot and diagnostic logs without an accepted
receipt, so DSA/ISA can investigate the failure after temporary execution files are removed.

This boundary assumes the administrator reviews the formal checker and benchmark adapter. The
adapter must measure the intended implementation and handle untrusted candidate output. UID
separation is not a complete sandbox for hostile native code, network access or denial of service;
use an additional isolated execution environment when those are part of the threat model.

## Administrator setup

Install the reviewed framework into a root-owned Python environment, for example
`/opt/skydiscover-evaluation`. Neither that environment, its dependencies, the policy, nor the
trusted evaluator store may be writable by the agent. Use `python -I` to exclude the run directory and
agent-controlled Python environment variables from imports. Do not run a privileged interpreter
against an agent-writable source checkout.

Create a dedicated unprivileged worker account (for example `skybench`); the existing ordinary
user running the agent can be used as the agent account. They must have different nonzero UIDs.
Keep the run directory on the Linux filesystem with normal Unix ownership.

After reviewing the task, checker and benchmark, authorize a named policy once:

```bash
sudo /opt/skydiscover-evaluation/bin/python -I \
  -m skydiscover.synthesize.spec.evaluation_authority authorize counter \
  /home/skyagent/project/.skydiscover/demo \
  --agent-user skyagent --worker-user skybench --config '{"threads":4}' \
  -- bash synthesis/evaluator/benchmark/score.sh
```

The default worker environment contains `/usr/bin:/bin` as PATH. Approve additional toolchain
paths or variables with `--env '<JSON object>'`; they must refer to trusted installations. The
benchmark's last nonempty output line must be a JSON object of finite numeric metrics. Timeouts
can be pinned with `--timeout` and `--check-timeout`. Approving changed checker/benchmark inputs
requires a new policy name; agents cannot modify or overwrite existing policies.

The administrator can then measure a candidate with:

```bash
sudo /opt/skydiscover-evaluation/bin/python -I \
  -m skydiscover.synthesize.spec.evaluation_authority run counter \
  /home/skyagent/project/.skydiscover/demo
```

For unattended use, expose a root-owned **no-argument wrapper** that executes exactly that
approved command with the fixed policy and run directory. Grant the agent permission only to
invoke that wrapper. Do not grant arbitrary privileged Python, shell commands, `authorize`, or
general sudo access. Run the normal agent-side checkpoint/export commands from the protected
framework installation as well.

## WSL

Use the same Linux ownership layout inside the WSL distribution, not a Windows-mounted directory
whose permissions cannot enforce this boundary. The agent must not be able to run
`wsl.exe -u root`, use unrestricted sudo, or edit the trusted installation through the Windows host.
A Windows-host agent with those privileges is outside this protection model; run the synthesis
agent inside the restricted Linux environment or use a separate evaluation host.

## Candidate identity and migration

Input digests include each file's executable bits as well as path and contents. The digest prefix
is `sha256-exec-v2`; previous input digests and unsigned receipts require a fresh evaluation.
Read/write bits are excluded so the same candidate can be checked in a protected read-only copy.
Checkpoint creation compares the staged source and public artifact with the measured snapshot,
including executable bits. Final export also validates the preserved receipt and actual artifact.

## Tests

`tests/spec/test_ids_performance.py` uses an explicitly fake trusted evaluator for orchestration unit
tests. `tests/spec/test_evaluation_authority.py` exercises the real filesystem boundary as root,
dropping child processes to numeric UIDs 65533 and 65534. It creates no users or sudo rules and
writes only temporary test data. Run it in a disposable Linux/WSL test environment. Tests cover
honest export, joint receipt/log/hash forgery, replaced checker/benchmark, denied record writes,
unsigned receipts, untrusted record ownership and privileged-file disclosure through symlinks.
