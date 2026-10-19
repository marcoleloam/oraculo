import hashlib
import json

from oraculo.cli import main


def test_memory_propose_forwards_executor_identity(
    service_config, tmp_path, monkeypatch, capsys
):
    config, path = service_config
    proposal = tmp_path / "proposal.json"
    proposal.write_text('{"schema_version":1}')
    received = []

    async def capture(config, envelope):
        received.append(envelope)
        return {"ok": True, "state": "published"}

    monkeypatch.setattr("oraculo.cli.request", capture)
    result = main(
        [
            "--config",
            str(path),
            "memory",
            "propose",
            "alpha",
            "--file",
            str(proposal),
            "--attempt-id",
            "attempt",
            "--generation",
            "7",
            "--thread-id",
            "child",
            "--run-id",
            "run",
        ]
    )
    assert result == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    assert len(received) == 1
    envelope = received[0]
    assert envelope["origin"] == {"thread_id": "child", "run_id": "run"}
    assert envelope["payload"]["attempt_id"] == "attempt"
    assert envelope["payload"]["generation"] == 7
    assert (
        envelope["payload"]["sha256"]
        == hashlib.sha256(proposal.read_bytes()).hexdigest()
    )
    staged = config.state_dir / "staging" / envelope["payload"]["artifact"]
    assert staged.read_bytes() == proposal.read_bytes()
    assert staged.stat().st_mode & 0o777 == 0o600


def test_memory_propose_missing_identity_does_not_stage_or_dispatch(
    service_config, tmp_path, monkeypatch, capsys
):
    config, path = service_config
    proposal = tmp_path / "proposal.json"
    proposal.write_text("{}")

    async def unexpected_request(config, envelope):
        raise AssertionError("missing identity must fail before socket dispatch")

    monkeypatch.setattr("oraculo.cli.request", unexpected_request)
    result = main(
        [
            "--config",
            str(path),
            "memory",
            "propose",
            "alpha",
            "--file",
            str(proposal),
            "--attempt-id",
            "attempt",
            "--generation",
            "7",
        ]
    )
    assert result == 1
    assert "--thread-id" in capsys.readouterr().err
    assert not (config.state_dir / "staging").exists()


def test_memory_promote_submits_shared_ingest(service_config, tmp_path, monkeypatch):
    _, path = service_config
    source = tmp_path / "sanitized.md"
    source.write_text("General learning")
    envelopes = []

    async def capture(config, envelope):
        envelopes.append(envelope)
        return {"ok": True}

    monkeypatch.setattr("oraculo.cli.request", capture)
    assert (
        main(
            [
                "--config",
                str(path),
                "memory",
                "promote",
                "alpha",
                "--source",
                str(source),
            ]
        )
        == 0
    )
    assert envelopes[0]["project_id"] == "alpha"
    assert envelopes[0]["payload"]["memory_scope"] == "shared"
    assert envelopes[0]["payload"]["memory_operation"] == "ingest"
    assert envelopes[0]["payload"]["source_path"] == str(source.resolve())


def test_memory_propose_shared_source_stages_wrapper(
    service_config, tmp_path, monkeypatch
):
    config, path = service_config
    proposal = tmp_path / "proposal.json"
    proposal.write_text('{"project_id":"shared"}')
    source = tmp_path / "sanitized.md"
    source.write_text("Generalized source")
    envelopes = []

    async def capture(config, envelope):
        envelopes.append(envelope)
        return {"ok": True}

    monkeypatch.setattr("oraculo.cli.request", capture)
    assert (
        main(
            [
                "--config",
                str(path),
                "memory",
                "propose",
                "alpha",
                "--file",
                str(proposal),
                "--shared-source",
                str(source),
                "--attempt-id",
                "a",
                "--generation",
                "1",
                "--thread-id",
                "child",
                "--run-id",
                "run",
            ]
        )
        == 0
    )
    staged = config.state_dir / "staging" / envelopes[0]["payload"]["artifact"]
    assert json.loads(staged.read_text()) == {
        "scope": "shared",
        "sanitized_source": "Generalized source",
        "proposal": {"project_id": "shared"},
    }
