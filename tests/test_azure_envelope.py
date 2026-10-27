"""Only synthetic credentials; preservation of raw-PAT behavior and metadata gates."""

import json
import traceback

import pytest

from oraculo.azure_repos import AzureRepos, credential_reference, load_pat
from oraculo.publication import PublicationError

REF = {
    "keychain_service": "explicit-existing-item",
    "keychain_account": "operator@example.test",
    "format": "pat-envelope-v1",
    "organization": "example",
    "scopes": ["vso.build", "vso.code_write"],
}
ENVELOPE = {
    "token": "synthetic-secret-do-not-log",
    "authorization_id": "11111111-1111-4111-8111-111111111111",
    "owner": "operator@example.test",
    "organization": "example",
    "scope": "vso.code_write vso.build",
    "valid_to": "2099-01-01T00:00:00Z",
}


class Backend:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    def get_password(self, service, account):
        self.calls.append((service, account))
        return self.raw


def test_explicit_envelope(tmp_path):
    backend = Backend(json.dumps(ENVELOPE))
    assert load_pat(tmp_path, REF, backend=backend) == ENVELOPE["token"]
    assert backend.calls == [(REF["keychain_service"], REF["keychain_account"])]
    assert credential_reference(REF) == REF


@pytest.mark.parametrize(
    "change",
    [
        {"owner": "someone-else@example.test"},
        {"organization": "another-org"},
        {"valid_to": "2020-01-01T00:00:00Z"},
        {"valid_to": "2099-01-01T00:00:00"},
        {"valid_to": None},
        {"authorization_id": "invalid"},
        {"scope": "vso.code_write"},
        {"scope": "vso.build vso.code_write vso.build"},
        {"scope": "vso.build vso.code_write vso.security_manage"},
        {"token": "short"},
        {"token": "newline\nsecret"},
        {"unknown": True},
    ],
)
def test_bad_metadata_never_reveals_secret(tmp_path, change, capsys):
    with pytest.raises(PublicationError) as error:
        load_pat(tmp_path, REF, backend=Backend(json.dumps({**ENVELOPE, **change})))
    assert str(error.value) == "azure_credential_invalid"
    assert ENVELOPE["token"] not in "".join(traceback.format_exception(error.value))
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        "[]",
        "{}",
        "x" * 8193,
        json.dumps(ENVELOPE)[:-1] + ',"owner":"operator@example.test"}',
    ],
)
def test_malformed_envelopes(tmp_path, raw):
    with pytest.raises(PublicationError, match="azure_credential_invalid"):
        load_pat(tmp_path, REF, backend=Backend(raw))


@pytest.mark.parametrize(
    "change",
    [
        {"format": "auto"},
        {"organization": "example/other"},
        {"scopes": []},
        {"scopes": ["vso.build", "vso.build"]},
        {"scopes": "vso.build"},
        {"token": "must-never-be-a-reference"},
    ],
)
def test_invalid_reference_before_keychain(tmp_path, change):
    backend = Backend(json.dumps(ENVELOPE))
    with pytest.raises(PublicationError, match="azure_credential_reference_invalid"):
        load_pat(tmp_path, {**REF, **change}, backend=backend)
    assert backend.calls == []


def test_no_auto_detect_and_raw_pat_unchanged(tmp_path):
    ref = {k: REF[k] for k in ("keychain_service", "keychain_account")}
    with pytest.raises(PublicationError, match="azure_credential_invalid"):
        load_pat(tmp_path, ref, backend=Backend(json.dumps(ENVELOPE)))
    assert (
        load_pat(tmp_path, ref, backend=Backend("raw-synthetic-pat"))
        == "raw-synthetic-pat"
    )


def test_org_binding_before_secret_read(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "oraculo.azure_repos.inspect_repository",
        lambda *a, **k: {"host": "azure", "organization": "different"},
    )

    def forbidden(*args, **kwargs):
        pytest.fail("No secret may be read for another organization")

    monkeypatch.setattr("oraculo.azure_repos.load_pat", forbidden)
    with pytest.raises(
        PublicationError, match="azure_credential_organization_mismatch"
    ):
        AzureRepos.from_spec({"cwd": str(tmp_path), "credential": REF})
