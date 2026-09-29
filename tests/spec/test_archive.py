"""Nothing a run learned is lost: spec/archive.py, kept_tests' .history, and the token usage hook.

- every snapshot holds the whole run directory; an unchanged file is a hard link to the previous
  snapshot's copy, and an unchanged run adds no snapshot;
- a snapshot restores to a working run directory;
- a failed export still snapshots the run;
- a kept test or test.sh that a later run replaces, and a body held back, go to tests/.history/;
- the token usage hook counts each message once, per role, and stops at the run's .done marker.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
from pathlib import Path

import pytest

from skydiscover.synthesize.spec import archive, kept_tests
from skydiscover.synthesize.spec import run as spec_run
from skydiscover.synthesize.spec.paths import Domain, Run

HOOK = Path(__file__).resolve().parents[2] / "skydiscover/synthesize/workflow/hooks/token_usage.py"


def _run(tmp_path: Path, domain: str = "kv store") -> Run:
    run = Run(tmp_path / "project" / ".skydiscover" / "kv").create()
    run.task.write_text(f"---\ndomain: {domain}\n---\n# Task\n", encoding="utf-8")
    run.tests.mkdir(parents=True, exist_ok=True)
    (run.tests / "test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (run.tests / "capacity.py").write_text("# Capacity is bounded.\n", encoding="utf-8")
    run.plan.write_text("## Brief\niteration 1\n", encoding="utf-8")
    return run


def test_snapshots_keep_every_iteration_and_link_unchanged_files(tmp_path):
    run = _run(tmp_path)
    first = archive.snapshot(run.path, "checkpoint_1")
    run.plan.write_text("## Brief\niteration 2\n", encoding="utf-8")  # the planner rewrites it
    second = archive.snapshot(run.path, "checkpoint_2")

    assert first.parent == second.parent == Domain("kv store").runs / first.parents[1].name / "snapshots"
    assert "iteration 1" in (first / "synthesis/plan.md").read_text()
    assert "iteration 2" in (second / "synthesis/plan.md").read_text()
    # unchanged bytes are one inode shared by both snapshots
    assert os.stat(first / "task.md").st_ino == os.stat(second / "task.md").st_ino
    assert os.stat(first / "synthesis/plan.md").st_ino != os.stat(second / "synthesis/plan.md").st_ino
    assert not os.access(second / "synthesis/plan.md", os.W_OK), "archived files are read-only"
    # nothing changed: no duplicate snapshot
    assert archive.snapshot(run.path, "finish") == second
    index = json.loads((second.parents[1] / "index.json").read_text())
    assert [s["label"] for s in index["snapshots"]] == ["checkpoint_1", "checkpoint_2"]
    assert run.archive_pointer.read_text().strip() == str(second.parents[1])


def test_restore_gives_back_a_writable_run_directory(tmp_path):
    run = _run(tmp_path)
    (run.path / "link").symlink_to("task.md")
    snap = archive.snapshot(run.path, "checkpoint_1")
    back = archive.restore(snap, tmp_path / "restored")
    assert (back / "synthesis/plan.md").read_text() == run.plan.read_text()
    assert (back / "link").is_symlink() and not (back / archive.MANIFEST).exists()
    (back / "synthesis/plan.md").write_text("edited\n")  # writable again
    assert "iteration 1" in (snap / "synthesis/plan.md").read_text(), "the snapshot is untouched"
    with pytest.raises(ValueError):
        archive.restore(snap, back)


def test_a_failed_export_still_snapshots_the_run(tmp_path, capsys):
    run = _run(tmp_path)
    # no candidate was ever measured, so the export fails; the run must still be kept
    assert spec_run.main_finish([str(run.path), "--export-to", str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "--export-to failed" in err and "archive: finish-export-failed" in err
    (snap,) = Domain("kv store").runs.glob("*/snapshots/*_finish-export-failed")
    assert (snap / "synthesis/tests/capacity.py").is_file()


def test_kept_tests_keep_every_replaced_body(tmp_path):
    run = _run(tmp_path)
    kept_tests.sync(run.path, "kv store")
    tests = Domain("kv store").tests
    (run.tests / "test.sh").write_text("#!/bin/sh\necho v2\n", encoding="utf-8")
    (run.tests / "capacity.py").write_text("# Capacity is bounded, v2.\n", encoding="utf-8")

    held = kept_tests.sync(run.path, "kv store")
    assert held["held"] == ["capacity"]
    assert "v2" in (tests / "test.sh").read_text()
    (old_script,) = tests.glob(".history/*/test.sh")
    assert "exit 0" in old_script.read_text(), "the replaced test.sh is kept"
    (held_body,) = tests.glob(".history/*/held/capacity.py")
    assert "v2" in held_body.read_text(), "the held-back body is kept, not dropped"
    assert "v2" not in (tests / "capacity.py").read_text()

    kept_tests.sync(run.path, "kv store", refresh_bodies=True)
    assert "v2" in (tests / "capacity.py").read_text()
    assert any(
        "v2" not in p.read_text() for p in tests.glob(".history/*/capacity.py")
    ), "the refreshed-over body is kept"
    assert [t["id"] for t in kept_tests.tests_for("kv store")] == ["capacity"]


# ------------------------------------------------------------------------------ token usage hook


@pytest.fixture(scope="module")
def hook():
    spec = importlib.util.spec_from_file_location("token_usage", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _line(mid, t, out, cache_read=0, model="claude-x", **extra):
    return json.dumps(
        {
            "type": "assistant",
            "timestamp": t,
            "message": {
                "id": mid,
                "model": model,
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": out,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": cache_read,
                },
            },
            **extra,
        }
    )


def _session(tmp_path: Path) -> Path:
    main = tmp_path / "projects" / "s1.jsonl"
    main.parent.mkdir(parents=True)
    main.write_text(
        "\n".join(
            [
                _line("m1", "2026-01-01T00:00:00Z", 3, 100),
                _line("m1", "2026-01-01T00:00:00Z", 7, 100),  # the same message, streamed on
                _line("m2", "2026-01-01T00:01:00Z", 1, model="<synthetic>"),
                _line("m3", "2026-01-03T00:00:00Z", 2),  # later chat, after the run ended
                "not json",
            ]
        )
        + "\n"
    )
    sub = main.with_suffix("") / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-a.jsonl").write_text(_line("s1", "2026-01-01T00:02:00Z", 20) + "\n")
    (sub / "agent-a.meta.json").write_text(json.dumps({"agentType": "coding-agent"}))
    return main


def test_token_usage_counts_each_message_once_per_role(tmp_path, hook):
    counted = hook.usage(hook.session_transcripts(_session(tmp_path)))
    assert counted["total"]["messages"] == 3  # m1 once, m3, s1; the synthetic one is no API call
    assert counted["total"]["output_tokens"] == 7 + 2 + 20
    assert counted["by_agent"]["lead"]["messages"] == 2
    assert counted["by_agent"]["coding-agent"]["output_tokens"] == 20
    t = counted["total"]
    assert t["total_tokens"] == sum(t[f] for f in hook.FIELDS)


def test_token_usage_hook_writes_the_run_file_and_stops_at_done(tmp_path, hook, monkeypatch):
    run = _run(tmp_path)
    main = _session(tmp_path)
    done = run.path.parent / "kv.done"
    done.write_text("outputs/x\n")
    cutoff = hook._parse_time("2026-01-02T00:00:00Z")
    os.utime(done, (cutoff, cutoff))
    payload = {
        "session_id": "s1",
        "transcript_path": str(main),
        "cwd": str(run.path.parent.parent),
        "hook_event_name": "Stop",
    }
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert hook.main(["--hook"]) == 0

    doc = json.loads(run.token_usage.read_text())
    assert doc["total"]["messages"] == 2, "m3 came after the run's .done marker"
    assert doc["sessions"]["s1"]["by_agent"]["coding-agent"]["messages"] == 1
    assert doc["by_iteration"]["0"]["total_tokens"] == doc["total"]["total_tokens"]
    log = [json.loads(x) for x in (run.path / "token_usage.log.jsonl").read_text().splitlines()]
    assert log[-1]["event"] == "Stop" and log[-1]["total_tokens"] == doc["total"]["total_tokens"]
    assert log[-1]["iteration"] == 0
    assert any("tokens:" in line for line in spec_run._token_lines(run))
    # the hook also snapshotted the run and mirrored the session's transcripts
    archive_dir = Path(run.archive_pointer.read_text().strip())
    (snap,) = archive_dir.glob("snapshots/*_iter-00_turn")
    assert (snap / "token_usage.json").is_file()
    mirror = archive_dir / "transcripts" / "s1"
    assert (mirror / "main.jsonl").read_bytes() == main.read_bytes()
    assert (mirror / "subagents" / "agent-a.jsonl").is_file()
    offsets = json.loads((snap / archive.MANIFEST).read_text())["transcripts"]["s1"]
    assert offsets["main.jsonl"] == main.stat().st_size
    assert not (run.path / "hook_errors.log").exists()


def test_token_usage_hook_never_blocks(tmp_path, hook, monkeypatch):
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))
    assert hook.main(["--hook"]) == 0
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"transcript_path": "/nope"})))
    assert hook.main(["--hook"]) == 0


def _checkpoint(out: Path, k: int, when: str, score: float) -> None:
    cp = out / "checkpoints" / f"checkpoint_{k}"
    cp.mkdir(parents=True)
    (cp / "score.json").write_text(
        json.dumps({"checkpoint": k, "created_at": when, "score": {"ops": score}})
    )


def test_every_role_and_iteration_is_kept_with_its_tokens(tmp_path, hook, monkeypatch):
    """Two scored iterations and a third in progress: tokens are filed by iteration and role, each
    role's finish is its own snapshot, and index.json / iterations.md tie them together."""
    run = _run(tmp_path)
    (run.impl / "store.py").parent.mkdir(parents=True, exist_ok=True)
    (run.impl / "store.py").write_text("v1\n")
    out = tmp_path / "project" / "outputs" / "kv_1"
    out.mkdir(parents=True)
    run.output_pointer.write_text("outputs/kv_1\n")
    _checkpoint(out, 1, "2026-01-01T01:00:00Z", 1.0)
    _checkpoint(out, 2, "2026-01-01T02:00:00Z", 2.0)
    main = tmp_path / "projects" / "s1.jsonl"
    main.parent.mkdir(parents=True)
    main.write_text(
        "\n".join(
            [
                _line("a", "2026-01-01T00:30:00Z", 1),  # iteration 1
                _line("b", "2026-01-01T01:30:00Z", 2),  # iteration 2
                _line("c", "2026-01-01T03:00:00Z", 4),  # iteration 3, in progress
            ]
        )
        + "\n"
    )
    sub = main.with_suffix("") / "subagents"
    sub.mkdir(parents=True)
    (sub / "agent-x.jsonl").write_text(_line("d", "2026-01-01T01:45:00Z", 8) + "\n")
    (sub / "agent-x.meta.json").write_text(
        json.dumps({"agentType": "skysynth:2-synthesis-loop:coding-agent", "description": "try B-link"})
    )
    monkeypatch.delenv("SKYDISCOVER_RUN", raising=False)

    def fire(event, **extra):
        payload = {"session_id": "s1", "transcript_path": str(main), "cwd": str(run.path.parent.parent),
                   "hook_event_name": event, **extra}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        assert hook.main(["--hook"]) == 0

    fire("SubagentStop", agent_transcript_path=str(sub / "agent-x.jsonl"))
    (run.impl / "store.py").write_text("v2, failed its tests\n")  # an attempt that is never scored
    fire("Stop")

    doc = json.loads(run.token_usage.read_text())
    by_it = doc["by_iteration"]
    assert {k: v["output_tokens"] for k, v in by_it.items()} == {"1": 1, "2": 2 + 8, "3": 4}
    assert by_it["2"]["by_agent"]["skysynth:2-synthesis-loop:coding-agent"]["output_tokens"] == 8
    assert doc["iteration"] == 3

    archive_dir = Path(run.archive_pointer.read_text().strip())
    names = [p.name for p in sorted(archive_dir.glob("snapshots/*")) if not p.name.startswith(".")]
    assert names == ["0001_iter-03_after-coding-agent", "0002_iter-03_turn"]
    assert "failed its tests" in (archive_dir / "snapshots" / names[1] / "synthesis/impl/store.py").read_text()
    index = json.loads((archive_dir / "index.json").read_text())
    assert index["snapshots"][0]["detail"] == "skysynth:2-synthesis-loop:coding-agent try B-link"
    table = index["iterations"]
    assert table["1"]["checkpoint"] == "checkpoint_1" and table["1"]["score"] == {"ops": 1.0}
    assert table["2"]["tokens"]["total_tokens"] == by_it["2"]["total_tokens"]
    assert table["3"]["snapshots"] == names
    page = (archive_dir / "iterations.md").read_text()
    assert "| 2 | checkpoint_2 | ops 2.0 |" in page and "coding-agent" in page


def test_specification_work_is_iteration_zero(tmp_path, hook):
    """Messages before iteration 1 began (the first snapshot filed under it, archived before
    checkpoint_1) are the specification: iteration 0."""
    run = _run(tmp_path)
    (run.impl / "store.py").parent.mkdir(parents=True, exist_ok=True)
    (run.impl / "store.py").write_text("v1\n")
    archive_dir = tmp_path / "archive"
    archive_dir.mkdir()
    run.archive_pointer.write_text(f"{archive_dir}\n")
    (archive_dir / "index.json").write_text(
        json.dumps({"snapshots": [
            {"snapshot": "0001", "iteration": 0, "created_at": "2026-01-01T00:10:00Z"},
            {"snapshot": "0002", "iteration": 1, "created_at": "2026-01-01T00:20:00Z"},
        ]})
    )
    out = tmp_path / "project" / "outputs" / "kv_1"
    out.mkdir(parents=True)
    run.output_pointer.write_text("outputs/kv_1\n")
    _checkpoint(out, 1, "2026-01-01T01:00:00Z", 1.0)
    assign = hook.iteration_of(run.path)
    t = hook._parse_time
    assert assign(t("2026-01-01T00:15:00Z")) == 0
    assert assign(t("2026-01-01T00:30:00Z")) == 1
    assert assign(t("2026-01-01T02:00:00Z")) == 2 == assign(None)


def test_each_run_leaves_its_own_version_of_the_knowledge_base(tmp_path, monkeypatch):
    """No SKYDISCOVER_HOME: the knowledge base is <project>/.skydiscover/kb/, found from the run even
    with the cwd elsewhere. A run today and a run tomorrow leave two versions; the second run's
    changes never touch the first run's version."""
    monkeypatch.delenv("SKYDISCOVER_HOME", raising=False)
    monkeypatch.chdir(tmp_path)
    today = _run(tmp_path)
    kb = (tmp_path / "project" / ".skydiscover" / "kb").resolve()
    archive.snapshot(today.path, "checkpoint_1")
    kept_tests.sync(today.path, "kv store")  # what run finish saves
    archive.snapshot(today.path, "finish")
    assert (kb / "kv-store" / "tests" / "capacity.py").is_file()

    tomorrow = Run(today.path.parent / "kv-2").create()
    tomorrow.task.write_text(today.task.read_text(), encoding="utf-8")
    tomorrow.tests.mkdir(parents=True)
    (tomorrow.tests / "test.sh").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (tomorrow.tests / "latency.py").write_text("# Latency is bounded.\n", encoding="utf-8")
    kept_tests.sync(tomorrow.path, "kv store")
    archive.snapshot(tomorrow.path, "finish")

    versions = kb / "kv-store" / "versions"
    index = json.loads((versions / "index.json").read_text())
    assert [v["run"] for v in index["versions"]] == ["kv", "kv-2"]
    first, second = (versions / v["version"] for v in index["versions"])
    assert (first / "tests" / "capacity.py").is_file() and not (first / "tests" / "latency.py").exists()
    assert (second / "tests" / "capacity.py").is_file() and (second / "tests" / "latency.py").is_file()
    assert [v["tests"] for v in index["versions"]] == [1, 2]
    assert not (second / "runs").exists() and not (second / "versions").exists()
