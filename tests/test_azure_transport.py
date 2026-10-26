"""Synthetic credentials only; actual loopback HTTP exercises urllib failures."""

import io
import json
import threading
import traceback
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from test_azure_repos import REMOTE, existing, policy
from test_azure_repos import azure as azure_fixture

from oraculo.azure_repos import AzureHTTP, AzureRepos, _NoRedirect, load_pat
from oraculo.publication import (
    PublicationError,
    inspect_repository,
    integration_binding,
)

azure = azure_fixture


@pytest.mark.parametrize("redirect_host", ["127.0.0.1", "localhost"])
def test_real_http_redirect_does_not_forward_authorization(redirect_host):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            calls.append((self.path, self.headers.get("Authorization")))
            self.send_response(302)
            self.send_header(
                "Location",
                f"http://{redirect_host}:{self.server.server_port}/must-not-receive",
            )
            self.end_headers()

        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever)
    thread.start()
    local = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    class LocalOpener:
        def open(self, request, timeout):
            assert request.full_url.startswith("https://dev.azure.com/")
            # Only this test routes the synthetic request to loopback; production
            # URLs and transport validation remain HTTPS dev.azure.com only.
            rewritten = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}/redirect",
                headers=dict(request.header_items()),
            )
            return local.open(rewritten, timeout=timeout)

    try:
        with pytest.raises(PublicationError, match="azure_redirect_refused"):
            AzureHTTP("synthetic-pat", opener=LocalOpener())("GET", REMOTE)
        assert len(calls) == 1 and calls[0][0] == "/redirect"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


@pytest.mark.parametrize(
    "code,expected",
    [
        (401, "azure_auth_required"),
        (403, "azure_auth_required"),
        (500, "azure_host_unavailable"),
        (302, "azure_redirect_refused"),
    ],
)
def test_errors_never_relay_secret_headers_body_or_url(code, expected, capsys):
    secret = "synthetic-do-not-log"

    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                "https://invalid.example/" + secret,
                code,
                secret,
                {"Authorization": secret},
                io.BytesIO(secret.encode()),
            )

    try:
        AzureHTTP(secret, opener=Opener())("GET", REMOTE)
    except PublicationError as error:
        rendered = "".join(traceback.format_exception(error))
        assert secret not in rendered and str(error) == expected
    else:
        pytest.fail("HTTP errors must fail closed")
    assert secret not in capsys.readouterr().out


@pytest.mark.parametrize(
    "raw", [b"not-json", json.dumps([1]).encode(), b"x" * (4 * 1024 * 1024 + 1)]
)
def test_http_bounded_and_malformed_response(raw):
    class Response(io.BytesIO):
        status = 200
        headers = {}

    class Opener:
        def open(self, request, timeout):
            return Response(raw)

    transport = AzureHTTP("synthetic", opener=Opener())
    with pytest.raises(PublicationError):
        AzureRepos(transport)._request(
            {"organization": "example", "project": "p"}, "git/repositories/r"
        )


def test_keychain_exception_and_env_symlink_fail_safely(tmp_path):
    secret = "synthetic-do-not-log"

    class Backend:
        def get_password(self, *args):
            raise RuntimeError(secret)

    with pytest.raises(PublicationError) as error:
        load_pat(
            tmp_path,
            {"keychain_service": "existing", "keychain_account": "account"},
            backend=Backend(),
        )
    assert secret not in "".join(traceback.format_exception(error.value))
    outside = tmp_path / "outside"
    outside.write_text("SYS__AZDO_PAT=synthetic\n")
    (tmp_path / ".env").symlink_to(outside)
    with pytest.raises(PublicationError, match="unavailable"):
        load_pat(tmp_path)


@pytest.mark.parametrize(
    "data",
    [
        {"value": []},
        {"count": 1, "value": []},
        {"count": True, "value": [{}]},
        {"count": 1, "value": [None]},
    ],
)
def test_bad_page_schema_fails_closed(azure, data):
    m = inspect_repository(azure[0])
    with pytest.raises(PublicationError, match="azure_response_invalid"):
        AzureRepos(lambda *args: (data, {}))._pages(m, "git/refs")


def test_repeated_page_and_cursor_fail_closed(azure):
    m = inspect_repository(azure[0])
    repeated = {"count": 100, "value": [{"id": i} for i in range(100)]}
    with pytest.raises(PublicationError, match="pagination_invalid"):
        AzureRepos(lambda *args: (repeated, {}))._pages(m, "git/refs")
    count = 0

    def repeated_token(*args):
        nonlocal count
        count += 1
        return {"count": 1, "value": [{"id": count}]}, {
            "x-ms-continuationtoken": "same"
        }

    with pytest.raises(PublicationError, match="pagination_invalid"):
        AzureRepos(repeated_token)._pages(m, "git/refs", mode="token")


@pytest.mark.parametrize(
    "changes",
    [
        {"pr_id": 17, "pr_url": REMOTE + "/pullrequest/18", "reconcile_only": True},
        {"pr_id": True, "reconcile_only": True},
        {"pr_id": 17},
        {
            "pr_url": REMOTE.replace("example-repo", "wrong") + "/pullrequest/17",
            "reconcile_only": True,
        },
    ],
)
def test_explicit_pr_identifiers_reject_mismatch_without_mutations(azure, changes):
    _, spec, host, adapter = azure
    host.rows = [host.row()]
    with pytest.raises(PublicationError):
        adapter.ensure_pr({**spec, **changes})
    assert host.created == host.merges == 0


@pytest.mark.parametrize(
    "change",
    [
        lambda h: h.policies[0].pop("configuration"),
        lambda h: h.policies[0]["configuration"].update(isBlocking="false"),
        lambda h: h.policies[0].update(status="unknown"),
        lambda h: h.iterations[0]["sourceRefCommit"].update(commitId="c" * 40),
        lambda h: h.rows[0]["reviewers"][0].update(vote=True),
        lambda h: h.statuses[0].update(state="success"),
    ],
)
def test_malformed_policy_status_iteration_review_is_never_ready(azure, change):
    repo, spec, host, adapter = azure
    pr = existing(azure)
    change(host)
    with pytest.raises(PublicationError):
        adapter.merge(pr, policy(spec), repo)
    assert host.merges == 0


def test_no_integration_omission_can_skip_ancestry(azure):
    _, spec, _, _ = azure
    with pytest.raises(PublicationError, match="integration_required"):
        integration_binding(
            {**spec, "pr_id": 17, "reconcile_only": True, "source_commit": "c" * 40}
        )


def test_ambiguous_prs_and_exact_ref_prefix_never_create(azure):
    _, spec, host, adapter = azure
    host.rows = [host.row(number=17), host.row(number=18)]
    with pytest.raises(PublicationError, match="ambiguous"):
        adapter.ensure_pr(spec)
    host.rows = []
    del host.refs["refs/heads/" + spec["branch"]]
    host.refs["refs/heads/" + spec["branch"] + "-other"] = spec["head_sha"]
    with pytest.raises(PublicationError, match="remote_ref_mismatch"):
        adapter.ensure_pr(spec)
    assert host.created == 0
