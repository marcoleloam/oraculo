"""Adversarial regressions found by the independent F6 review."""

from types import SimpleNamespace

from test_memory_compiler import publisher, snapshot

from oraculo.memory.routines import KnowledgeRoutines
from oraculo.memory.schema import digest


def test_raw_over_selection_byte_limit_is_explicitly_deferred(tmp_path):
    pub = publisher(tmp_path)
    raw = snapshot(pub, "alpha", "x" * (2 * 1024 * 1024 + 1))
    routines = KnowledgeRoutines(SimpleNamespace(publisher=pub, state=None))
    selected, metrics = routines.pending_sources("alpha", 20)
    assert selected == []
    assert metrics["deferred_sources"] == [
        {"path": raw, "sha256": digest("x" * (2 * 1024 * 1024 + 1)),
         "code": "proposal_too_large"}
    ]
    assert routines.pending_sources("alpha", 20)[1]["deferred_sources"] == metrics["deferred_sources"]


