from test_memory_schema import proposal, publisher

from oraculo.memory import Maintenance


def test_lint_detects_corruption_and_request_has_scoped_references(tmp_path):
    pub = publisher(tmp_path)
    pub.publish(proposal())
    maintenance = Maintenance(pub)
    assert maintenance.lint("alpha") == []
    request = maintenance.request("alpha", "query", "Decisão")
    assert request["requires_executor_grant"]
    assert request["context"]["documents"][0]["path"].endswith("/index.md")
    path = pub.vault / proposal()["changes"][0]["path"]
    path.write_text(path.read_text() + "\n[[absent]]")
    findings = maintenance.lint("alpha")
    assert {"integrity", "broken_link"}.issubset({f["code"] for f in findings})
