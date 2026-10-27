"""Public payload privacy with real disposable Git and synthetic REST only."""

import hashlib
import json

import pytest
from test_azure_repos import azure as azure_fixture

from oraculo.publication import PublicationError

azure = azure_fixture


def test_exact_product_body_and_private_intent_before_post(azure):
    _, spec, host, adapter = azure
    spec["body"] = "Entrega do produto.\n\nValidação: testes aprovados.\n"
    host.lose_create = True
    events = []
    original = host.__call__

    def transport(method, url, body=None):
        if method == "POST":
            assert events == ["intent"]
            assert body == {
                "sourceRefName": "refs/heads/" + spec["branch"],
                "targetRefName": "refs/heads/" + spec["base"],
                "title": spec["title"],
                "description": spec["body"],
                "isDraft": False,
            }
            assert spec["task_id"] not in json.dumps(body)
            assert hashlib.sha256(
                spec["task_id"].encode()
            ).hexdigest() not in json.dumps(body)
            assert "oraculo-task:" not in json.dumps(body)
            events.append("post")
        return original(method, url, body)

    adapter.transport = transport
    receipt = adapter.ensure_pr(spec, before_create=lambda: events.append("intent"))
    assert host.created == 1 and events == ["intent", "post"]
    assert host.rows[0]["description"] == spec["body"]
    again = adapter.ensure_pr(
        {**spec, "reconcile_only": True},
        before_create=lambda: pytest.fail("Replay must never emit another intent"),
    )
    assert again["url"] == receipt["url"] and host.created == 1


def test_failed_intent_never_posts_unmarked_pr(azure):
    _, spec, host, adapter = azure

    def fail():
        raise OSError("synthetic persistence failure")

    with pytest.raises(PublicationError, match="publication_intent_failed"):
        adapter.ensure_pr(spec, before_create=fail)
    assert host.created == 0


@pytest.mark.parametrize(
    "fault,code",
    [
        ("invisible", "publication_uncertain"),
        ("ambiguous", "publication_pr_ambiguous"),
        ("sha", "pr_head_changed"),
        ("source", "azure_pr_identity_invalid"),
        ("target", "azure_pr_identity_invalid"),
        ("repo", "azure_repository_mismatch"),
    ],
)
def test_unmarked_reconciliation_never_weakens_identity_or_reemits(azure, fault, code):
    _, spec, host, adapter = azure
    adapter.ensure_pr(spec)
    assert "oraculo-task:" not in host.rows[0]["description"]
    if fault == "invisible":
        host.invisible = True
    elif fault == "ambiguous":
        host.rows.append(host.row(number=18))
    elif fault == "sha":
        host.rows[0]["lastMergeSourceCommit"]["commitId"] = "c" * 40
    elif fault == "source":
        host.rows[0]["sourceRefName"] = "refs/heads/another"
    elif fault == "target":
        host.rows[0]["targetRefName"] = "refs/heads/another"
    else:
        host.rows[0]["repository"]["id"] = "33333333-3333-3333-3333-333333333333"
    with pytest.raises(PublicationError, match=code):
        adapter.ensure_pr({**spec, "reconcile_only": True})
    assert host.created == 1 and host.merges == 0


@pytest.mark.parametrize("ours", [True, False])
def test_legacy_marker_compatibility_and_conflict_remain(azure, ours):
    _, spec, host, adapter = azure
    task_id = spec["task_id"] if ours else "a-different-private-task"
    host.rows = [host.row()]
    host.rows[0]["description"] += (
        "\n<!-- oraculo-task:" + hashlib.sha256(task_id.encode()).hexdigest() + " -->"
    )
    if ours:
        assert adapter.ensure_pr({**spec, "reconcile_only": True})["number"] == 17
    else:
        with pytest.raises(PublicationError, match="publication_pr_task_mismatch"):
            adapter.ensure_pr({**spec, "reconcile_only": True})
    assert host.created == host.merges == 0
