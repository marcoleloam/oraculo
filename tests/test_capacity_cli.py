import json

import pytest

from oraculo.cli import main
from oraculo.protocol import parse_envelope


@pytest.mark.parametrize(
    ("arguments", "kind", "project", "payload"),
    [
        (["capacity"], "capacity.get", "*", {}),
        (["capacity", "--set", "2"], "capacity.set", "*", {"max_executors": 2}),
        (
            ["plan-configure", "alpha", "objective", "--max-parallel", "1"],
            "plan.configure",
            "alpha",
            {"objective_id": "objective", "max_parallel": 1},
        ),
    ],
)
def test_capacity_controls_send_valid_local_envelopes(
    service_config, monkeypatch, capsys, arguments, kind, project, payload
):
    _, path = service_config
    received = []
    response = {"ok": True, "state": "configured", "data": {"effective": 2}}

    async def capture(config, envelope):
        parse_envelope(json.dumps(envelope).encode())
        received.append(envelope)
        return response

    monkeypatch.setattr("oraculo.cli.request", capture)
    assert main(["--config", str(path), *arguments]) == 0
    assert json.loads(capsys.readouterr().out) == response
    assert len(received) == 1
    assert received[0]["kind"] == kind
    assert received[0]["project_id"] == project
    assert received[0]["payload"] == payload
    assert received[0]["origin"] == {}


@pytest.mark.parametrize("value", ["0", "5", "1.5"])
def test_capacity_rejects_invalid_limits_before_request(
    service_config, monkeypatch, value
):
    _, path = service_config

    async def unexpected_request(config, envelope):
        raise AssertionError("invalid limit must not reach daemon")

    monkeypatch.setattr("oraculo.cli.request", unexpected_request)
    with pytest.raises(SystemExit) as error:
        main(["--config", str(path), "capacity", "--set", value])
    assert error.value.code == 2
