import pytest
from pydantic import ValidationError

from oraculo.protocol import Envelope, ProtocolError, is_control_event, parse_envelope


def test_envelope_rejects_extra_and_control_events():
    value = {
        "schema_version": 1,
        "request_id": "x",
        "kind": "objective.submit",
        "project_id": "p",
        "origin": {},
        "payload": {},
    }
    envelope = Envelope.model_validate(value)
    assert parse_envelope(envelope.model_dump_json().encode()) == envelope
    with pytest.raises(ValidationError):
        Envelope.model_validate(dict(value, override_budget=True))
    assert is_control_event({"marker": "ORACULO_CONTROL_V1"})
    assert is_control_event({"kind": "status_event"})
    with pytest.raises(ProtocolError):
        parse_envelope(b" " * 1048577)
