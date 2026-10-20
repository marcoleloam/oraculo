import pytest

from oraculo.memory.bridge import memory_display


@pytest.mark.parametrize(
    "state,label",
    [("found", "consultada"), ("empty", "vazia"), ("unavailable", "indisponível")],
)
def test_start_displays_receipt_and_time(state, label):
    display = memory_display(
        {
            "id": "cycle-1",
            "read_state": state,
            "read_receipt": {"at": "2026-10-20T20:41:12Z"},
        }
    )
    assert display["receipt_id"] == "cycle-1"
    assert display["start"] == {"state": label, "at": "2026-10-20T20:41:12Z"}
    assert display["finish"]["state"] == "pendente"


@pytest.mark.parametrize(
    "receipt",
    [
        {},
        {"state": "pending"},
        {"state": "confirmed", "operation_id": "other", "commit": "sha"},
        {"state": "confirmed", "operation_id": "cycle-1"},
    ],
)
def test_saved_requires_correlated_confirmed_publisher_receipt(receipt):
    assert (
        memory_display(
            {"id": "cycle-1", "write_state": "saved", "write_receipt": receipt}
        )["finish"]["state"]
        == "falhou"
    )


def test_confirmed_publication_displays_saved_with_commit_and_time():
    display = memory_display(
        {
            "id": "cycle-1",
            "write_state": "saved",
            "write_receipt": {
                "state": "confirmed",
                "operation_id": "cycle-1",
                "commit": "abc",
                "at": "now",
            },
        }
    )
    assert display["finish"] == {"state": "salva", "commit": "abc", "at": "now"}
