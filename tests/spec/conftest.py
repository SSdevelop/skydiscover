"""Every spec test runs against a throwaway knowledge base: a checkpoint or a finish now snapshots
the run into <kb>/runs/, and no test may write to the real ~/.skydiscover. A test that sets its own
SKYDISCOVER_HOME overrides this one."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_knowledge_base(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("SKYDISCOVER_HOME", str(tmp_path_factory.mktemp("kb-home")))
    monkeypatch.setenv("SKYDISCOVER_HOOK_SYNC", "1")  # hooks/token_usage.py works inline, not detached
