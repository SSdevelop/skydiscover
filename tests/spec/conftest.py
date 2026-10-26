"""Every spec test runs against a throwaway knowledge base: a checkpoint or a finish now snapshots
the run into <kb>/runs/, and no test may write to the real ~/.skydiscover. A test that sets its own
SKYDISCOVER_HOME overrides this one."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_knowledge_base(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("SKYDISCOVER_HOME", str(tmp_path_factory.mktemp("kb-home")))
    monkeypatch.setenv(
        "SKYDISCOVER_HOOK_SYNC", "1"
    )  # hooks/token_usage.py works inline, not detached


@pytest.fixture
def fake_evaluation_authority(monkeypatch):
    """Only orchestration tests replace the OS boundary with an immutable in-memory record.

    Authentic issuance, user permissions and protected storage have separate Linux tests.
    """
    import json
    import uuid

    from skydiscover.synthesize.spec import evaluation_authority as authority

    issued = {}

    def seal(doc):
        result = {**doc, "attestation": uuid.uuid4().hex}
        issued[result["attestation"]] = json.dumps(result, sort_keys=True)
        return result

    def authenticate(doc):
        if "attestation" not in doc:
            raise ValueError("No trusted evaluation attestation")
        if issued.get(doc["attestation"]) != json.dumps(doc, sort_keys=True):
            raise ValueError("Evaluation receipt differs from the trusted evaluation record")

    monkeypatch.setattr(authority, "seal", seal)
    monkeypatch.setattr(authority, "authenticate", authenticate)
